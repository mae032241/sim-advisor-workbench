"""One native DSH planning task, with final output judged only after completion."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from anyio.from_thread import start_blocking_portal
from advisor.dsh_command import discover_dsh_command, launch_command
from advisor.process_runner import _ProcessTree
from advisor.research_policy import COMMON_PLANNING_POLICY, text_sha256
from scripts.benchmark_codex_cli import public_question
from scripts.benchmark_native_cli import instructions, read_ledger
from scripts.benchmark_native_mcp import NativeKnowledgeBridge, LIBRARY_IDS, TOOLS
from scripts.benchmark_cli import core

HOST = APP / "integrations/dsh-benchmark-host"
MAX_BYTES = 2 * 1024 * 1024


class DshTask:
    def __init__(self, config, *, portal=None, bridge=None):
        self.config, self.portal, self.bridge = config, portal, bridge
        self.token = secrets.token_urlsafe(32)
        self.ready, self.finished = threading.Event(), threading.Event()
        self.final = None
        self.failure = None
        self.selection = None
        self.visible_tools = None
        self.usage = None
        self.process = self.tree = self.server = self.server_thread = None
        self.workers = []

    def callback(self, path, data):
        expected = {k: self.config[k] for k in ("provider", "model", "reasoning_effort")}
        if path == "/ready":
            selected = {k: data.get(k) for k in expected}
            names = data.get("tool_names")
            if (selected != expected or data.get("ephemeral") is not True
                    or not isinstance(names, list) or sorted(names) != sorted(t["name"] for t in self.config["tools"])):
                self.failure = "ready_boundary_mismatch"
                self.finished.set()
                return {"accepted": False}
            self.selection, self.visible_tools = selected, names
            self.ready.set()
            return {"accepted": True}
        if path == "/tool":
            if not self.ready.is_set() or self.bridge is None or data.get("name") not in TOOLS:
                return {"success": False, "error": "Tool is unavailable in this task."}
            result = self.portal.call(self.bridge.call_tool, data["name"], data.get("arguments"))
            payload = json.loads(result.content[0].text)
            if result.isError:
                message = payload.get("error", "Knowledge tool returned an error.")
                return {"success": False, "error": message[:2000] if isinstance(message, str) else "Knowledge tool returned an error."}
            return {"success": True, "value": payload}
        if path == "/complete":
            selected = data.get("selected")
            status = data.get("status")
            text = data.get("text")
            if selected != expected or status not in {"completed", "failed", "interrupted"} or not isinstance(text, str):
                self.failure = "completion_metadata_invalid"
            else:
                self.final = text
                self.failure = None if status == "completed" else "native_turn_" + status
                self.usage = numeric_usage(data.get("usage"))
            self.finished.set()
            return {"accepted": True}
        if path == "/error":
            code = data.get("code")
            allowed = {"startup_failed", "prepare_failed", "output_overflow", "agent_failed", "callback_failed", "boundary_failed", "host_failed", "session_prepare_failed",
                "invalid_configuration", "invalid_tools", "session_persistence_enabled", "invalid_module_base", "native_executor_unavailable",
                "selection_mismatch", "tool_visibility_mismatch", "ready_rejected", "callback_rejected", "message_overflow", "completion_delivery_failed"}
            self.failure = code if code in allowed else "native_host_error"
            if isinstance(data.get("partial_text"), str):
                self.final = data["partial_text"]
            self.finished.set()
            return {"accepted": True}
        raise ValueError("Unknown private callback")

    def start(self, command, patch):
        task = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, obj, status=200):
                body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
                if len(body) > MAX_BYTES:
                    status, body = 422, b'{"error":"callback resource limit"}'
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def dispatch(self, method):
                if self.headers.get("Origin") or not hmac.compare_digest(self.headers.get("X-DSH-Bridge-Token", "").encode(), task.token.encode()):
                    return self.respond({"error": "Unauthorized"}, 403)
                try:
                    if method == "GET" and self.path == "/config":
                        return self.respond(task.config)
                    length = int(self.headers.get("Content-Length", "0"))
                    if method != "POST" or not 0 < length <= MAX_BYTES or self.headers.get("Transfer-Encoding"):
                        raise ValueError()
                    self.connection.settimeout(10)
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError()
                    return self.respond(task.callback(self.path, data))
                except Exception:
                    return self.respond({"error": "Private callback failed"}, 422)

            def do_GET(self):
                self.dispatch("GET")

            def do_POST(self):
                self.dispatch("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.server_thread.start()
        env = dict(os.environ, SIM_ADVISOR_DSH_CALLBACK=f"http://127.0.0.1:{self.server.server_port}", SIM_ADVISOR_DSH_BRIDGE_TOKEN=self.token)
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        self.process = subprocess.Popen(launch_command(command, patch), cwd=self.config["cwd"], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **flags)
        self.tree = _ProcessTree(self.process)
        for stream in (self.process.stdout, self.process.stderr):
            worker = threading.Thread(target=self.drain, args=(stream,), daemon=True)
            self.workers.append(worker)
            worker.start()

    @staticmethod
    def drain(stream):
        try:
            while stream.read(65536):
                pass  # Never persist SDK output, tool bodies or reasoning.
        except OSError:
            pass

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        startup_deadline = min(deadline, time.monotonic() + 90)
        while not self.finished.wait(0.1):
            if self.process.poll() is not None:
                self.failure = "native_process_exited"
                break
            if not self.ready.is_set() and time.monotonic() > startup_deadline:
                self.failure = "startup_timeout"
                break
            if time.monotonic() > deadline:
                self.failure = "resource_timeout"
                break

    def close(self):
        if self.process:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.tree:
            self.tree.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.server_thread:
            self.server_thread.join(timeout=1)


def numeric_usage(value):
    if not isinstance(value, dict):
        return None
    return {k: v for k, v in value.items() if k in {
        "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens",
        "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens", "totalTokens",
        "promptTokens", "completionTokens", "cachedTokens", "modelCalls"}
        and type(v) in {int, float} and v >= 0}


def make_patch(path):
    disabled = json.loads((HOST / "disabled-plugins.json").read_text(encoding="utf-8"))
    if not isinstance(disabled, list) or any(not isinstance(s, str) for s in disabled):
        raise ValueError("Invalid disabled plugin list")
    layers = [{"id": name, "disabled": True} for name in disabled]
    layers.append({"insert": [{"id": "sim-advisor-benchmark-host", "name": (HOST / "index.js").as_uri(), "config": {}}]})
    path.write_text(json.dumps(layers, ensure_ascii=False, indent=2), encoding="utf-8")


def plan(root, args):
    root = Path(root).resolve()
    destination = Path(args.destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Destination must be empty")
    command = discover_dsh_command()
    if not command:
        raise ValueError("DSH is unavailable; no fallback model is used")
    if args.arm == "rag" and not args.workbench_url:
        raise ValueError("RAG requires a local workbench URL")
    question = public_question(root, args.case, destination)
    prompt = json.dumps({"instructions": instructions(args.arm), "question": question}, ensure_ascii=False)
    (destination / "model-input.json").write_text(prompt, encoding="utf-8")
    report = dict(schema="simulation-native-dsh-run/v1", case_id=args.case, arm=args.arm,
        engine="native-dsh-ephemeral-agent", requested_provider=args.provider, requested_model=args.model,
        requested_effort=args.effort, model_display_name=args.display_name,
        common_planning_policy_sha256=text_sha256(COMMON_PLANNING_POLICY),
        instructions_sha256=text_sha256(instructions(args.arm)), input_packet_sha256=text_sha256(json.dumps(question, ensure_ascii=False)),
        model_input_sha256=text_sha256(prompt), library_ids=sorted(LIBRARY_IDS) if args.arm == "rag" else [],
        private_reference_supplied=False, thought_chain_stored=False, raw_native_logs_stored=False,
        solver_run=False, evaluation_performed=False, automatic_retry_count=0,
        action_loop=False, citation_acceptance_gate=False, model_call_count=None,
        total_timeout_seconds=args.timeout, planning_only=True,
        started_at=datetime.now(timezone.utc).isoformat())
    started = time.monotonic()
    task = bridge = None
    try:
        with tempfile.TemporaryDirectory(prefix="benchmark-dsh-") as temp, start_blocking_portal() as portal:
            cwd = Path(temp)
            ledger = cwd / "knowledge-calls.jsonl"
            specs = []
            if args.arm == "rag":
                bridge = NativeKnowledgeBridge(args.workbench_url, ledger=ledger)
                report["libraries"] = portal.call(bridge.access_scope)
                specs = [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                         for t in portal.call(bridge.list_tools)]
            config = dict(provider=args.provider, model=args.model, reasoning_effort=args.effort,
                cwd=str(cwd), instructions=instructions(args.arm), prompt=json.dumps({"question": question}, ensure_ascii=False), tools=specs,
                module_resolution_base=str(Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")) / "profiles/sdk/package.json"))
            task = DshTask(config, portal=portal, bridge=bridge)
            patch = cwd / "benchmark.patch.yml"
            make_patch(patch)
            try:
                task.start(command, patch)
                task.wait(args.timeout)
            finally:
                task.close()
                report.update(status="complete" if task.finished.is_set() and not task.failure else "execution_error",
                    failure_code=task.failure, selected_model=task.selection, visible_tools=task.visible_tools,
                    final_message_present=task.final is not None, usage=task.usage,
                    ephemeral_session_confirmed=task.ready.is_set(), knowledge_calls=read_ledger(ledger),
                    knowledge_audit_complete=not bridge.audit_failed if bridge else True)
                (destination / "plan.md").write_text(task.final or "", encoding="utf-8", newline="")
                if bridge:
                    portal.call(bridge.aclose)
    except Exception as exc:
        report.update(status="execution_error", failure_code=type(exc).__name__)
        if not (destination / "plan.md").exists():
            (destination / "plan.md").write_text("", encoding="utf-8")
    finally:
        calls = report.get("knowledge_calls", [])
        report.update(seconds=round(time.monotonic() - started, 3), completed_at=datetime.now(timezone.utc).isoformat(),
            knowledge_call_counts=dict(Counter(c["tool"] for c in calls)),
            read_source_ids=sorted({sid for c in calls if c.get("tool") == "read_source" and c.get("status") == "complete" for sid in c.get("evidence_ids", [])}))
        if not (destination / "plan.md").exists():
            (destination / "plan.md").write_text("", encoding="utf-8")
        report["plan_sha256"] = core.sha256(destination / "plan.md")
        core.dump_json(destination / "run.json", report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--destination", type=Path, required=True)
    p.add_argument("--arm", choices=("rag", "no-library"), required=True)
    p.add_argument("--provider", default="deepseek-official")
    p.add_argument("--model", default="deepseek-flash")
    p.add_argument("--display-name", default="DeepSeek-V4.1-Flash")
    p.add_argument("--effort", default="high")
    p.add_argument("--workbench-url")
    p.add_argument("--timeout", type=int, default=1200)
    args = p.parse_args()
    result = plan(args.root, args)
    print(json.dumps({k:result.get(k) for k in ("case_id","arm","status","failure_code","seconds","knowledge_call_counts","selected_model")}, ensure_ascii=False))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
