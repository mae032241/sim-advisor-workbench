"""Run one prepared task through the shipped, plan-bound product CLI.

The caller owns task creation, library/service isolation and concurrency. This
module never builds a model prompt, runs an action loop, repairs a plan, scores
an answer or retries a turn. ``plan(root, args, cancel_event=...)`` returns a run
record even when generation fails. ``root`` identifies the benchmark only; no
benchmark files (in particular no private references) are read here.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from advisor.planning_service import tool_definitions
from advisor.research_policy import load_planner_instructions


# Exactly the same normal user message is submitted in both arms. Task facts
# arrive through the product's get_task_context, populated by the caller.
MESSAGE = "请根据当前任务的需求、已导入的几何/模型事实与输入数据，制定并保存可实施、可审阅的完整 COMSOL 仿真方案。"
EFFORTS = ("off", "low", "medium", "high", "xhigh", "max")


class ProductRunError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _Cancelled(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _sha(value):
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def product_contract():
    """Derive the actual production contract without importing AgentSessions.

    The source guard makes a changed production wiring fail visibly rather than
    silently reporting a hash for instructions/tools that are no longer used.
    """
    source_path = APP / "advisor/agent_sessions.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    assignments = {node.targets[0].id: node.value for node in tree.body
                   if isinstance(node, ast.Assign) and len(node.targets) == 1
                   and isinstance(node.targets[0], ast.Name)}
    rendered = assignments.get("INSTRUCTIONS")
    wrapper = next((node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "planning_tools"), None)
    expected_wrapper = ast.parse("def planning_tools():\n return tool_definitions(bound=True,include_alias=True)").body[0]
    starts = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == "start"]
    production_wiring = any(
        {kw.arg: ast.dump(kw.value) for kw in node.keywords}.get("instructions") == ast.dump(ast.Name(id="INSTRUCTIONS", ctx=ast.Load()))
        and {kw.arg: ast.dump(kw.value) for kw in node.keywords}.get("tools") == ast.dump(ast.Call(func=ast.Name(id="planning_tools", ctx=ast.Load()), args=[], keywords=[]))
        for node in starts)
    if (rendered is None or ast.dump(rendered) != ast.dump(ast.parse("load_planner_instructions(PROMPT_PATH)", mode="eval").body)
            or wrapper is None or ast.dump(wrapper) != ast.dump(expected_wrapper) or not production_wiring):
        raise ProductRunError("product_contract_changed", "Production planner wiring changed; update the contract reader")
    prompt = APP / "prompts/comsol_planner.v1.txt"
    instructions = load_planner_instructions(prompt)
    tools = tool_definitions(bound=True, include_alias=True)
    return {
        "instructions_source": "advisor.agent_sessions.INSTRUCTIONS (static wiring checked)",
        "instructions_sha256": _sha(instructions),
        "prompt_template_sha256": _sha(prompt.read_bytes()),
        "tool_schema_sha256": _sha(_canonical(tools)),
        "tool_names": [tool["name"] for tool in tools],
        "agent_sessions_source_sha256": _sha(source_path.read_bytes()),
        "runtime_adapter_source_sha256": _sha((APP / "advisor/runtime_adapters.py").read_bytes()),
        "codex_runtime_source_sha256": _sha((APP / "advisor/codex_runtime.py").read_bytes()),
    }


def _http_read(url, *, timeout):
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(url, timeout=timeout) as response:
        content = response.read().decode("utf-8")
        return json.loads(content) if "json" in response.headers.get("Content-Type", "") else content


def _run_process(command, *, cwd, timeout, cancel_event):
    """Cancel the CLI client only; the real interrupt/disconnect stops its Agent."""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", env=environment, **options)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise _Cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def saved_plan_state(initial, final):
    """Count persisted full/partial edits after this turn's initial revision.

    A chat report, inherited prior save, or intake placeholder is not a new
    saved plan. ``update_steps`` persists the same authored plan contract as
    ``save_plan`` and records ``agent_steps_updated`` rather than a full save.
    """
    events = [item for item in final.get("history", []) if isinstance(item, dict)
              and item.get("event") in {"agent_plan_updated", "agent_steps_updated"}
              and type(item.get("revision")) is int and item["revision"] > initial["revision"]]
    value = final.get("plan")
    saved = bool(events and isinstance(value, dict) and value.get("steps")
                 and value.get("engine") not in {"intake-only", "reference-baseline"}
                 and final.get("revision", 0) > initial["revision"])
    return {"saved_plan": saved, "save_plan_count": len(events),
            "save_plan_revisions": [item["revision"] for item in events],
            "final_revision": final.get("revision"),
            "planning_incomplete": bool(final.get("planning_incomplete")),
            "plan_stale": bool(final.get("plan_stale"))}


def plan(root, args, *, cancel_event=None, runner=None, http_reader=None):
    """Execute one already-created unique task session, with no internal parallelism.

    args: url (or workbench_url), session_id (or session), case, arm,
    destination, model, effort, timeout. Optional poll_interval and audit_path.
    runner/http_reader are dependency seams for tests, never model substitutes.
    """
    url = getattr(args, "url", None) or getattr(args, "workbench_url", None)
    sid = getattr(args, "session_id", None) or getattr(args, "session", None)
    parsed = urlsplit(url or "")
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("A loopback workbench URL is required")
    if not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{32}", sid):
        raise ValueError("A prepared 32-character task session ID is required")
    if args.arm not in {"rag", "no-library"} or args.effort not in EFFORTS or args.timeout <= 0:
        raise ValueError("Invalid benchmark arm, effort or timeout")
    destination = Path(args.destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Run directory must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    url = url.rstrip("/")
    runner, read = runner or _run_process, http_reader or _http_read
    started = time.monotonic()
    deadline = started + args.timeout
    report = {
        "schema": "simulation-plan-run/v1", "engine": "product-agent-cli", "case_id": args.case,
        "arm": args.arm, "status": "running", "session_id": sid, "workbench_url": url,
        "benchmark_root": str(Path(root).resolve()), "requested_model": args.model,
        "requested_effort": args.effort, "timeout_seconds": args.timeout,
        "message": MESSAGE, "message_sha256": _sha(MESSAGE), "message_count": 0,
        "agent_execution_count": 0, "model_call_count": None,
        "automatic_retry_count": 0, "retry_count": 0, "evaluation_performed": False,
        "execution_feedback_supplied": False, "physical_feedback_supplied": False,
        "private_reference_supplied": False, "solver_run": False, "scoring_run": False,
        "saved_plan": False, "cli_calls": [], "cleanup_errors": [],
        "isolation_owner": "caller_provided_service_and_unique_task_session",
        "knowledge_library_enabled": args.arm == "rag",
        "tool_audit_path": str(getattr(args, "audit_path", "") or "") or None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    initial = None
    message_attempted = False
    terminal = False
    phase = "preflight"

    def checkpoint():
        report["seconds"] = round(time.monotonic() - started, 3)
        _write(destination / "run.json", report)

    def remaining():
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled()
        value = deadline - time.monotonic()
        if value <= 0:
            raise subprocess.TimeoutExpired("product-agent-turn", args.timeout)
        return value

    def call(script, action, *, cleanup=False, extra=()):
        command = [sys.executable, str(APP / script)]
        if script == "agent_cli.py":
            command += [action, sid, *extra, "--url", url]
        else:
            command += ["--url", url, action, *extra]
        record = {"client": script, "action": action, "cleanup": cleanup}
        report["cli_calls"].append(record)
        began = time.monotonic()
        try:
            result = runner(command, cwd=APP, timeout=125 if cleanup else min(125, remaining()),
                            cancel_event=None if cleanup else cancel_event)
            record["returncode"] = result.returncode
            if result.returncode != 0:
                raise ProductRunError("product_cli_failed", f"{script} {action} failed")
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ProductRunError("invalid_cli_response", "Product CLI did not return an object")
            return value
        finally:
            record["seconds"] = round(time.monotonic() - began, 3)
            checkpoint()

    def context(*, cleanup=False):
        value = call("tools_cli.py", "call", cleanup=cleanup,
                     extra=("get_task_context", "--args", _canonical({"session_id": sid})))
        if not isinstance(value.get("result"), dict):
            raise ProductRunError("invalid_task_context", "Product task context is missing")
        return value["result"]

    def verify_settings(state):
        if (state.get("model") != args.model or state.get("reasoning_effort") != args.effort
                or state.get("managed_backend") != "codex" or state.get("connection_mode") != "managed"
                or state.get("backend") != "codex-app-server"):
            raise ProductRunError("runtime_identity_mismatch", "Product did not retain the requested runtime/model/effort")
        report.update(reported_model=state["model"], reported_effort=state["reasoning_effort"],
                      backend=state["backend"], conversation_id=state.get("thread_id"), pid=state.get("pid"))

    checkpoint()
    try:
        remaining()
        report["product_contract"] = product_contract()
        remote_tools = call("tools_cli.py", "list")
        expected = tool_definitions()
        remote = {item["name"]: item for item in remote_tools.get("tools", [])}
        if any(remote.get(item["name"]) != item for item in expected):
            raise ProductRunError("public_tool_schema_mismatch", "Service schemas differ from the production tools")
        report["product_contract"].update(public_tool_schema_verified=True,
            public_tool_schema_sha256=_sha(_canonical(expected)))
        _write(destination / "product-tools.json", tool_definitions(bound=True, include_alias=True))
        initial = read(url + f"/api/sessions/{sid}", timeout=min(30, remaining()))
        _write(destination / "session-initial.json", initial)
        state = call("agent_cli.py", "status")
        if (state.get("connected") or state.get("alive") or state.get("thread_id") or state.get("messages")
                or any(item.get("event") in {"agent_plan_updated", "agent_steps_updated"}
                       for item in initial.get("history", []))):
            raise ProductRunError("task_not_fresh", "A unique disconnected task without prior Agent output is required")
        task_context = context()
        _write(destination / "context-initial.json", task_context)
        report["initial_context_sha256"] = _sha(_canonical(task_context))
        report["initial_revision"] = initial["revision"]
        phase = "settings"
        state = call("agent_cli.py", "settings", extra=("--managed-backend", "codex", "--connection-mode", "managed",
                     "--model", args.model, "--effort", args.effort))
        verify_settings(state)
        phase = "message"
        remaining()
        message_attempted = True
        report["message_count"] = report["agent_execution_count"] = 1
        state = call("agent_cli.py", "message", extra=(MESSAGE,))
        expected_turn_id = state.get("active_turn_id") or (state.get("last_turn") or {}).get("turn_id")
        phase = "waiting_for_turn"
        while True:
            remaining()
            verify_settings(state)
            report["last_agent_status"] = state.get("status")
            active_turn_id = state.get("active_turn_id")
            if active_turn_id:
                if expected_turn_id and active_turn_id != expected_turn_id:
                    raise ProductRunError("agent_turn_changed", "Product Agent changed turns without another message")
                expected_turn_id = active_turn_id
            last_turn = state.get("last_turn")
            if isinstance(last_turn, dict) and last_turn.get("turn_id") == expected_turn_id:
                report["last_turn"] = {key: last_turn.get(key) for key in ("turn_id", "status")}
                if last_turn.get("status") in {"failed", "interrupted"}:
                    terminal = True
                    raise ProductRunError("agent_turn_" + last_turn["status"], "Product Agent turn ended without completing")
            if state.get("error") or state.get("warning") or state.get("error_metadata"):
                # A native error notification can precede an automatic retry of
                # this same turn. Poll for its outcome; never submit new input.
                report["runtime_warning_observed"] = True
                if isinstance(state.get("error_metadata"), dict):
                    report["last_error_metadata"] = {key: state["error_metadata"][key] for key in
                        ("code", "category", "will_retry", "turn_id", "http_status") if key in state["error_metadata"]}
            if state.get("status") in {"error", "disconnected"} or state.get("alive") is False:
                raise ProductRunError("agent_disconnected", "Product Agent disconnected before completing its turn")
            if state.get("status") == "idle" and not state.get("active_turn_id"):
                if state.get("turn_outcome_supported"):
                    completed = isinstance(last_turn, dict) and last_turn.get("turn_id") == expected_turn_id and last_turn.get("status") == "completed"
                else:
                    # Existing adapters without outcome reporting retain their
                    # idle contract, including idle errors being real failures.
                    completed = not state.get("error")
                    if not completed:
                        terminal = True
                        raise ProductRunError("agent_turn_error", "Product Agent reported a turn error")
                if completed:
                    terminal = True
                    report["status"] = "turn_completed"
                    break
            delay = min(max(0.01, getattr(args, "poll_interval", 1.0)), remaining())
            if cancel_event is not None:
                cancel_event.wait(delay)
            else:
                threading.Event().wait(delay)
            state = call("agent_cli.py", "status")
    except _Cancelled:
        report.update(status="cancelled", failure_code="cancelled", failure_phase=phase)
    except subprocess.TimeoutExpired:
        report.update(status="timed_out", failure_code="turn_timeout", failure_phase=phase)
    except Exception as exc:
        report.update(status="incomplete", failure_code=getattr(exc, "code", "product_runtime_failure"),
                      failure_type=type(exc).__name__, failure_phase=phase)
    finally:
        # Never disconnect/preempt somebody else's existing Agent. Once our
        # message is attempted, this task owns cleanup, including startup errors.
        if message_attempted:
            for action in (["disconnect"] if terminal else ["interrupt", "disconnect"]):
                try:
                    call("agent_cli.py", action, cleanup=True)
                except Exception as exc:
                    report["cleanup_errors"].append({"action": action, "error_type": type(exc).__name__})
            try:
                final = read(url + f"/api/sessions/{sid}", timeout=30)
                _write(destination / "session-final.json", final)
                final_context = context(cleanup=True)
                _write(destination / "context-final.json", final_context)
                report["final_context_sha256"] = _sha(_canonical(final_context))
                report.update(saved_plan_state(initial, final))
                chat = read(url + f"/api/sessions/{sid}/agent", timeout=30)
                _write(destination / "chat.json", chat.get("messages", []))
                _write(destination / "agent-final.json", chat)
                if report["saved_plan"]:
                    _write(destination / "saved-plan.json", {key: final.get(key) for key in
                        ("id", "revision", "plan", "plan_settings", "design_brief", "questions", "answers", "sources",
                         "evidence_gaps", "planning_incomplete", "plan_stale", "research_trace")})
                    markdown = read(url + f"/api/sessions/{sid}/export?format=markdown", timeout=30)
                    if not isinstance(markdown, str):
                        raise ProductRunError("invalid_product_export", "Product plan export was not text")
                    (destination / "plan.md").write_text(markdown, encoding="utf-8", newline="")
                    report["plan_sha256"] = _sha((destination / "plan.md").read_bytes())
                if report["status"] == "turn_completed":
                    report["status"] = "complete" if report["saved_plan"] else "no_saved_plan"
            except Exception as exc:
                report["collection_error"] = {"code": getattr(exc, "code", "result_collection_failed"),
                                               "error_type": type(exc).__name__}
                if report["status"] == "turn_completed":
                    report["status"] = "incomplete"
            if report["cleanup_errors"] and report["status"] in {"complete", "no_saved_plan"}:
                report["status"] = "incomplete"
                report["failure_code"] = "cleanup_failed"
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        checkpoint()
    return report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, default=APP / "benchmarks/comsol-plan-v3")
    result.add_argument("--url", required=True)
    result.add_argument("--session-id", required=True)
    result.add_argument("--case", required=True)
    result.add_argument("--arm", choices=("rag", "no-library"), required=True)
    result.add_argument("--destination", type=Path, required=True)
    result.add_argument("--model", default="gpt-6-luna")
    result.add_argument("--effort", choices=EFFORTS, default="high")
    result.add_argument("--timeout", type=float, default=3600)
    result.add_argument("--poll-interval", type=float, default=1.0)
    result.add_argument("--audit-path", type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    result = plan(args.root, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
