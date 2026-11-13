"""Transport and lifecycle checks; no model/runtime process is started."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

import agent_cli
import tools_cli
from advisor.planning_service import tool_definitions
from advisor.research_policy import load_planner_instructions, text_sha256
from scripts import benchmark_product_cli as cli


SID = "a" * 32


class FakeProduct:
    def __init__(self, outcome="saved", event=None):
        self.outcome, self.event = outcome, event
        self.calls, self.active, self.connected = [], False, False
        self.model, self.effort = "default-model", "medium"
        self.messages = []
        self.session = {"id": SID, "revision": 3, "goal": "Public input and measured geometry",
                        "model": {"facts": [{"key": "size", "value": 2}]}, "sources": [],
                        "plan": {"engine": "intake-only", "steps": [{"title": "Placeholder"}]},
                        "history": [{"revision": 1, "event": "created"}], "plan_stale": True}

    def status(self):
        return {"session_id": SID, "status": "running" if self.active else "idle" if self.connected else "disconnected",
                "connected": self.connected, "alive": self.connected, "thread_id": "test-thread" if self.connected else None,
                "active_turn_id": "test-turn" if self.active else None, "messages": self.messages,
                "model": self.model, "reasoning_effort": self.effort, "managed_backend": "codex",
                "connection_mode": "managed", "backend": "codex-app-server", "error": None}

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        parsed = urlsplit(path)
        if parsed.path == "/api/tools":
            tools = tool_definitions()
            if self.outcome == "schema_drift":
                tools[0]["description"] = "Changed contract"
            return {"tools": tools}
        if parsed.path == "/api/tools/call":
            assert body == {"name": "get_task_context", "arguments": {"session_id": SID}}
            return {"result": {key: self.session.get(key) for key in
                ("id", "revision", "goal", "model", "plan", "sources", "plan_stale")}}
        if parsed.path == f"/api/sessions/{SID}":
            return copy.deepcopy(self.session)
        if parsed.path.endswith("/export"):
            return "# Product saved plan\r\nExact exported content.  \r\n"
        action = parsed.path.rsplit("/", 1)[-1]
        if action == "settings":
            assert body == {"model": "gpt-6-luna", "reasoning_effort": "high",
                            "managed_backend": "codex", "connection_mode": "managed"}
            self.model, self.effort = body["model"], body["reasoning_effort"]
            if self.outcome == "model_mismatch":
                self.model = "unexpected-fallback"
        elif action == "message":
            assert body == {"text": cli.MESSAGE}
            self.connected = self.active = True
            self.messages += [{"role": "user", "text": body["text"]}]
            if self.event is not None:
                self.event.set()
        elif action == "interrupt":
            self.active = False
        elif action == "disconnect":
            self.connected = self.active = False
        elif action == "agent" and self.active and self.outcome in {"saved", "chat_only"}:
            self.active = False
            self.messages += [{"role": "assistant", "text": "Chat-only report is not the saved plan."}]
            if self.outcome == "saved":
                self.session.update(revision=4, plan_stale=False,
                    plan={"engine": "codex-app-server / gpt-6-luna / high", "steps": [{"title": "Saved implementation"}]})
                self.session["history"].append({"event": "agent_plan_updated", "revision": 4})
        return copy.deepcopy(self.status())

    def runner(self, command, **kwargs):
        assert kwargs["cwd"] == cli.APP and kwargs["timeout"] > 0
        if kwargs["cancel_event"] is not None and kwargs["cancel_event"].is_set():
            raise cli._Cancelled()
        if Path(command[1]).name == "agent_cli.py":
            action = command[2]
            assert command[3] == SID
            body = None
            if action == "message":
                body = {"text": command[4]}
            elif action == "settings":
                body = {"model": command[command.index("--model") + 1],
                        "reasoning_effort": command[command.index("--effort") + 1],
                        "managed_backend": command[command.index("--managed-backend") + 1],
                        "connection_mode": command[command.index("--connection-mode") + 1]}
            path = f"/api/sessions/{SID}/agent" + ("/" + action if action != "status" else "")
            value = self.request("GET" if action == "status" else "POST", path, body or ({} if action != "status" else None))
        else:
            assert Path(command[1]).name == "tools_cli.py"
            action = command[4]
            body = {"name": command[5], "arguments": json.loads(command[7])} if action == "call" else None
            value = self.request("POST" if body else "GET", "/api/tools/call" if body else "/api/tools", body)
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "RAW_STDERR_NOT_STORED")

    def reader(self, url, *, timeout):
        return self.request("GET", urlsplit(url).path + ("?" + urlsplit(url).query if urlsplit(url).query else ""))


def arguments(destination, arm="rag", **kwargs):
    return SimpleNamespace(destination=destination, arm=arm, case="case-01", model="gpt-6-luna", effort="high",
                           session_id=SID, url=kwargs.pop("url", "http://127.0.0.1:12345"),
                           timeout=kwargs.pop("timeout", 10), poll_interval=0.01, **kwargs)


@pytest.mark.parametrize("arm", ["rag", "no-library"])
def test_shipped_clis_use_http_settings_context_and_saved_output(tmp_path, arm, monkeypatch):
    product = FakeProduct()

    def fake_open(request, *, timeout):
        assert timeout > 0
        body = json.loads(request.data) if request.data is not None else None
        value = product.request(request.get_method(), urlsplit(request.full_url).path, body)
        return io.StringIO(json.dumps(value))

    monkeypatch.setattr(agent_cli, "urlopen", fake_open)
    monkeypatch.setattr(tools_cli, "build_opener", lambda *_: SimpleNamespace(open=fake_open))

    def run_shipped_cli(command, **kwargs):
        # Exercise the real CLI parsers and HTTP request construction in process,
        # with HTTP replaced at its transport boundary; never open a socket.
        entry = agent_cli if Path(command[1]).name == "agent_cli.py" else tools_cli
        output = io.StringIO()
        with monkeypatch.context() as patch, redirect_stdout(output):
            patch.setattr(sys, "argv", command[1:])
            returncode = entry.main() or 0
        return subprocess.CompletedProcess(command, returncode, output.getvalue(), "")

    args = arguments(tmp_path / "run", arm)
    result = cli.plan(tmp_path / "private-reference-must-not-be-read", args,
                      runner=run_shipped_cli, http_reader=product.reader)
    assert result["status"] == "complete" and result["saved_plan"] is True
    assert result["message_count"] == result["agent_execution_count"] == 1
    assert result["automatic_retry_count"] == 0 and result["evaluation_performed"] is False
    assert result["reported_model"] == "gpt-6-luna" and result["reported_effort"] == "high"
    assert result["product_contract"]["public_tool_schema_verified"] is True
    instructions = load_planner_instructions(cli.APP / "prompts/comsol_planner.v1.txt")
    assert result["product_contract"]["instructions_sha256"] == text_sha256(instructions)
    assert result["product_contract"]["tool_schema_sha256"] == cli._sha(cli._canonical(tool_definitions(bound=True, include_alias=True)))
    assert (args.destination / "plan.md").read_bytes() == b"# Product saved plan\r\nExact exported content.  \r\n"
    assert json.loads((args.destination / "saved-plan.json").read_text(encoding="utf-8"))["revision"] == 4
    assert json.loads((args.destination / "context-initial.json").read_text(encoding="utf-8"))["plan"]["engine"] == "intake-only"
    assert json.loads((args.destination / "context-final.json").read_text(encoding="utf-8"))["revision"] == 4
    assert json.loads((args.destination / "chat.json").read_text(encoding="utf-8"))[-1]["role"] == "assistant"
    actions = [path.rsplit("/", 1)[-1] for method, path, _ in product.calls if method == "POST" and "/agent/" in path]
    assert actions == ["settings", "message", "disconnect"]


@pytest.mark.parametrize("outcome,expected", [("chat_only", "no_saved_plan"), ("cancel", "cancelled"), ("timeout", "timed_out")])
def test_unsaved_cancelled_and_timed_out_runs_are_not_repaired_or_retried(tmp_path, outcome, expected):
    event = threading.Event() if outcome == "cancel" else None
    product = FakeProduct(outcome, event)
    args = arguments(tmp_path / "run", timeout=0.12 if outcome == "timeout" else 5)
    result = cli.plan(tmp_path, args, runner=product.runner, http_reader=product.reader, cancel_event=event)
    assert result["status"] == expected and result["saved_plan"] is False
    assert not (args.destination / "plan.md").exists() and not (args.destination / "saved-plan.json").exists()
    assert result["message_count"] == 1 and result["automatic_retry_count"] == 0
    actions = [call["action"] for call in result["cli_calls"] if call["client"] == "agent_cli.py"]
    assert actions.count("message") == 1 and actions[-1] == "disconnect"
    assert ("interrupt" in actions) == (outcome != "chat_only")
    assert (args.destination / "context-final.json").exists()
    assert "RAW_STDERR_NOT_STORED" not in (args.destination / "run.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("boundary", ["existing_agent", "schema_drift", "model_mismatch", "saved_without_event"])
def test_preflight_and_save_contract_fail_closed_without_preempting_agents(tmp_path, boundary):
    product = FakeProduct(boundary if boundary in {"schema_drift", "model_mismatch"} else "chat_only")
    if boundary == "existing_agent":
        product.connected = True
    if boundary == "saved_without_event":
        product.session["plan"] = {"engine": "looks-final", "steps": [{"title": "Not an actual save"}]}
    result = cli.plan(tmp_path, arguments(tmp_path / "run"), runner=product.runner, http_reader=product.reader)
    if boundary == "saved_without_event":
        assert result["status"] == "no_saved_plan" and result["save_plan_count"] == 0
    else:
        assert result["status"] == "incomplete" and result["message_count"] == 0
        assert result["failure_code"] == {"existing_agent": "task_not_fresh", "schema_drift": "public_tool_schema_mismatch",
                                          "model_mismatch": "runtime_identity_mismatch"}[boundary]
        assert not any(item["action"] in {"message", "interrupt", "disconnect"} for item in result["cli_calls"])
    assert "agent_sessions" not in cli.product_contract.__globals__
    assert "INSTRUCTIONS" not in vars(cli) and "CodexActionBackend" not in vars(cli)


class LifecycleProduct(FakeProduct):
    """One native turn: warning while active, then a separate terminal poll."""
    def __init__(self, terminal="completed", *, supported=True, stale=False):
        super().__init__("native")
        self.terminal, self.supported = terminal, supported
        self.last_turn = {"turn_id": "older-turn", "status": "failed"} if stale else None

    def status(self):
        state = super().status()
        state.update(error="Generic runtime warning" if self.active else None,
                     error_metadata={"code": "codex_error", "category": "httpConnectionFailed",
                                     "will_retry": True, "turn_id": "test-turn"} if self.active else None)
        if self.supported:
            state.update(turn_outcome_supported=True, last_turn=self.last_turn)
        return state

    def request(self, method, path, body=None):
        if method == "GET" and urlsplit(path).path.endswith("/agent") and self.active:
            self.active = False
            if self.terminal == "disconnected":
                self.connected = False
            elif self.terminal != "missing":
                self.last_turn = {"turn_id": "test-turn", "status": self.terminal}
            if self.terminal == "completed":
                self.session.update(revision=4, plan_stale=False,
                    plan={"engine": "product-agent", "steps": [{"title": "Saved after native recovery"}]})
                self.session["history"].append({"event": "agent_plan_updated", "revision": 4})
        return super().request(method, path, body)


@pytest.mark.parametrize("supported,stale", [(True, False), (True, True), (False, False)])
def test_running_error_waits_for_same_turn_completion_without_interrupt_or_new_message(tmp_path, supported, stale):
    product = LifecycleProduct(supported=supported, stale=stale)
    result = cli.plan(tmp_path, arguments(tmp_path / "run"), runner=product.runner, http_reader=product.reader)
    assert result["status"] == "complete" and result["runtime_warning_observed"]
    assert result["message_count"] == result["agent_execution_count"] == 1
    assert result["last_error_metadata"]["will_retry"] is True
    actions = [item["action"] for item in result["cli_calls"] if item["client"] == "agent_cli.py"]
    assert actions.count("message") == 1 and "interrupt" not in actions
    if supported:
        assert result["last_turn"] == {"turn_id": "test-turn", "status": "completed"}


@pytest.mark.parametrize("terminal", ["failed", "interrupted"])
def test_explicit_unsuccessful_terminal_without_error_text_is_never_complete(tmp_path, terminal):
    product = LifecycleProduct(terminal)
    result = cli.plan(tmp_path, arguments(tmp_path / "run"), runner=product.runner, http_reader=product.reader)
    assert result["status"] == "incomplete" and result["failure_code"] == "agent_turn_" + terminal
    assert result["last_turn"]["status"] == terminal
    actions = [item["action"] for item in result["cli_calls"] if item["client"] == "agent_cli.py"]
    assert actions.count("message") == 1 and actions[-1] == "disconnect" and "interrupt" not in actions


@pytest.mark.parametrize("terminal,expected", [("missing", "timed_out"), ("disconnected", "incomplete")])
def test_native_outcome_must_be_observed_and_runtime_must_remain_alive(tmp_path, terminal, expected):
    product = LifecycleProduct(terminal)
    result = cli.plan(tmp_path, arguments(tmp_path / "run", timeout=0.2), runner=product.runner, http_reader=product.reader)
    assert result["status"] == expected and result["saved_plan"] is False
    actions = [item["action"] for item in result["cli_calls"] if item["client"] == "agent_cli.py"]
    assert actions.count("message") == 1 and "interrupt" in actions


def test_saved_plan_state_recognizes_continuation_with_only_persisted_partial_edits():
    initial = {"revision": 2}
    final = {"revision": 4, "plan": {"engine": "product-agent", "steps": [{"id": "retained-step"}]},
             "history": [{"event": "agent_plan_updated", "revision": 2},
                         {"event": "agent_steps_updated", "revision": 3},
                         {"event": "agent_steps_updated", "revision": 4}]}
    result = cli.saved_plan_state(initial, final)
    assert result["saved_plan"] is True
    assert result["save_plan_count"] == 2 and result["save_plan_revisions"] == [3, 4]
    assert result["final_revision"] == 4


def test_saved_plan_state_does_not_count_inherited_updates_without_a_new_persisted_edit():
    initial = {"revision": 4}
    final = {"revision": 5, "plan": {"engine": "product-agent", "steps": [{"id": "old-step"}]},
             "history": [{"event": "agent_plan_updated", "revision": 2},
                         {"event": "agent_steps_updated", "revision": 3},
                         {"event": "agent_steps_updated", "revision": 4},
                         {"event": "answers_saved", "revision": 5}]}
    result = cli.saved_plan_state(initial, final)
    assert result["saved_plan"] is False
    assert result["save_plan_count"] == 0 and result["save_plan_revisions"] == []


@pytest.mark.parametrize("engine", ["intake-only", "reference-baseline"])
def test_saved_plan_state_still_rejects_placeholder_engines_after_partial_event(engine):
    final = {"revision": 2, "plan": {"engine": engine, "steps": [{"id": "placeholder"}]},
             "history": [{"event": "agent_steps_updated", "revision": 2}]}
    result = cli.saved_plan_state({"revision": 1}, final)
    assert result["saved_plan"] is False and result["save_plan_count"] == 1


def test_saved_plan_state_retains_full_save_compatibility_and_rejects_invalid_revisions():
    final = {"revision": 4, "plan": {"engine": "product-agent", "steps": [{"id": "authored-step"}]},
             "history": [{"event": "agent_plan_updated", "revision": 4},
                         {"event": "agent_steps_updated", "revision": "5"},
                         {"event": "agent_steps_updated", "revision": True}]}
    result = cli.saved_plan_state({"revision": 3}, final)
    assert result["saved_plan"] is True
    assert result["save_plan_count"] == 1 and result["save_plan_revisions"] == [4]
    final["revision"] = 3
    assert cli.saved_plan_state({"revision": 3}, final)["saved_plan"] is False
