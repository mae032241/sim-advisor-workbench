"""External inbox/event transport and explicit echo subprocess, without a model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from external_agent_client import ExternalAgentClient, ExternalAgentError, resolve_workbench_url
from advisor.process_runner import _ProcessTree


SID = "b" * 32


def wait_for(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError("Timed out waiting for test transport")


def pid_is_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    # The Windows venv python.exe launcher can spawn the actual interpreter;
    # registration must use that worker's os.getpid(), not Popen's launcher PID.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
    finally:
        kernel.CloseHandle(handle)


@pytest.fixture
def workbench():
    state = {"lock": threading.RLock(), "requests": [], "events": [], "commands": [], "registered": None,
             "binding": None, "connected": True, "poll_count": 0, "unregister_count": 0, "poll_error": False, "redirect": False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, body):
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            with state["lock"]:
                state["requests"].append({"method": "GET", "path": self.path})
            if self.path == "/api/tools":
                return self.respond(200, {"tools": [{"name": "search_knowledge", "description": "Search", "inputSchema": {"type": "object"}}, {"name": "get_task_context", "description": "Context", "inputSchema": {"type": "object"}}]})
            self.respond(404, {"error": "unknown route"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with state["lock"]:
                state["requests"].append({"method": "POST", "path": self.path, "body": body})
                if state["redirect"]:
                    self.send_response(302)
                    self.send_header("Location", "http://192.0.2.1/forbidden")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/api/tools/call":
                    return self.respond(200, {"result": {"name": body["name"], "arguments": body["arguments"]}})
                prefix = f"/api/sessions/{SID}/external/"
                if not self.path.startswith(prefix):
                    return self.respond(404, {"error": "wrong session"})
                action = self.path[len(prefix):]
                if action == "register":
                    state["registered"], state["binding"], state["connected"] = body, "external-fixture-binding", True
                    return self.respond(200, {"binding_id": state["binding"], "session_id": SID, "instructions": "测试规划提示词", "ttl_seconds": 30})
                if not state["binding"] or body.get("binding_id") != state["binding"]:
                    return self.respond(403, {"error": "invalid binding"})
                if action == "poll":
                    state["poll_count"] += 1
                    if state["poll_error"]:
                        return self.respond(503, {"error": "temporary transport error"})
                    commands = [c for c in state["commands"] if c["id"] > body["after"]]
                    cursor = max([body["after"]] + [c["id"] for c in state["commands"]])
                    return self.respond(200, {"commands": commands, "cursor": cursor, "connected": state["connected"]})
                if action == "event":
                    state["events"].append(body["event"])
                    return self.respond(200, {"accepted": True})
                if action == "tool":
                    return self.respond(200, {"result": {"name": body["name"], "arguments": body["arguments"], "bound_session": SID}})
                if action == "unregister":
                    state["binding"], state["connected"] = None, False
                    state["unregister_count"] += 1
                    return self.respond(200, {"unregistered": True})
                self.respond(404, {"error": "unknown action"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    def enqueue(kind, text=None):
        with state["lock"]:
            command = {"id": len(state["commands"]) + 1, "type": kind, "at": "test-time"}
            if text is not None:
                command["text"] = text
            state["commands"].append(command)
            return command
    state["enqueue"] = enqueue
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("url", ["https://127.0.0.1", "http://example.com", "http://0.0.0.0", "http://127.0.0.1/path", "http://user:pw@127.0.0.1", "http://127.0.0.1?x=1", "http://127.0.0.1#x", "http://127.0.0.1:99999", "http://[::1%25zone]", "file:///tmp/server"])
def test_reject_non_loopback_and_routed_urls(url):
    with pytest.raises(ValueError):
        resolve_workbench_url(url)


def test_default_server_record_and_session_validation(tmp_path, monkeypatch):
    (tmp_path / "server.json").write_text('{"url":"http://localhost:8765/"}', encoding="utf-8")
    monkeypatch.setenv("SIM_ADVISOR_DATA", str(tmp_path))
    assert resolve_workbench_url() == "http://127.0.0.1:8765"
    assert resolve_workbench_url("http://[::1]:8765") == "http://[::1]:8765"
    for value in ["../other", "a" * 31, ""]:
        with pytest.raises(ValueError):
            ExternalAgentClient(value)
    (tmp_path / "server.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="server.json"):
        resolve_workbench_url()


def test_register_poll_events_tools_and_unregister_without_backend(workbench, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://192.0.2.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    client = ExternalAgentClient(SID, workbench["url"])
    assert len(client.list_tools()) == 2  # Public tools work before registration.
    assert client.call_tool("search_knowledge", {"query": "pressure"})["arguments"] == {"query": "pressure"}
    registered = client.register("Existing harness", model="example-backend", reasoning_effort="xhigh", conversation_id="existing-conversation")
    assert client.instructions == registered["instructions"] == "测试规划提示词"
    assert workbench["registered"] == {"name": "Existing harness", "pid": os.getpid(), "model": "example-backend", "reasoning_effort": "xhigh", "conversation_id": "existing-conversation"}
    with pytest.raises(RuntimeError):
        client.register("duplicate")
    command = workbench["enqueue"]("message", "继续完善网格")
    assert client.poll()["commands"] == [command] and client.cursor == 1
    assert client.poll()["commands"] == []
    event = {"type": "delta", "message_id": "m1", "text": "正在读取资料", "turn_id": "t1"}
    assert client.publish(event) == {"accepted": True} and workbench["events"][-1] == event
    args = {"session_id": SID, "revision": 2, "steps": []}
    assert client.call_tool("update_steps", args)["arguments"] == args
    assert client.unregister() == {"unregistered": True}
    assert client.binding_id is None and not client.connected
    with pytest.raises(RuntimeError):
        client.publish({"type": "state", "status": "idle"})


def test_background_pump_keeps_heartbeats_during_slow_work_and_delivers_commands_once(workbench):
    client = ExternalAgentClient(SID, workbench["url"])
    client.register("long-callback fixture")
    try:
        client.start_pump(interval=.03)
        command = workbench["enqueue"]("message", "slow model callback")
        assert client.receive(timeout=2) == command
        before = workbench["poll_count"]
        time.sleep(.22)  # A synchronous harness callback; heartbeat runs independently.
        assert workbench["poll_count"] >= before + 3
        assert client.receive(timeout=0) is None
        with pytest.raises(RuntimeError, match="receive"):
            client.poll()
        stop = workbench["enqueue"]("interrupt")
        assert client.receive(timeout=2) == stop
        bye = workbench["enqueue"]("disconnect")
        workbench["connected"] = False
        assert client.receive(timeout=2) == bye
        assert client.receive() is None  # Does not hang after disconnect.
    finally:
        client.unregister()
    assert not client._pump.is_alive()


def test_pump_exposes_transport_failure_and_bad_binding_without_silent_reconnect(workbench):
    client = ExternalAgentClient(SID, workbench["url"])
    client.register("failure fixture")
    try:
        workbench["poll_error"] = True
        client.start_pump(interval=.03)
        wait_for(lambda: client.last_error)
        with pytest.raises(ExternalAgentError, match="temporary transport error"):
            client.receive(timeout=.1)
        assert not client.connected
    finally:
        client.unregister()
    foreign = ExternalAgentClient(SID, workbench["url"], binding_id="invalid")
    with pytest.raises(ExternalAgentError) as caught:
        foreign.poll()
    assert caught.value.status == 403 and caught.value.payload == {"error": "invalid binding"}


def test_redirect_is_not_followed_and_failed_write_not_retried(workbench):
    workbench["redirect"] = True
    client = ExternalAgentClient(SID, workbench["url"])
    with pytest.raises(ExternalAgentError) as caught:
        client.register("redirect fixture")
    assert caught.value.status == 302
    assert len(workbench["requests"]) == 1
    assert client.binding_id is None


def test_bound_tool_transport_carries_binding_and_backend_without_managed_adapter(workbench):
    client=ExternalAgentClient(SID,workbench["url"])
    with pytest.raises(RuntimeError,match="register"):
        client.call_bound_tool("get_task_context",{})
    client.register("DSH harness",backend="dsh",model="gpt-6.1-sol",reasoning_effort="xhigh",conversation_id="dsh-conversation")
    try:
        assert workbench["registered"]["backend"]=="dsh"
        result=client.call_bound_tool("get_task_context",{})
        assert result["bound_session"]==SID
        assert workbench["requests"][-1]["body"]=={"binding_id":client.binding_id,"name":"get_task_context","arguments":{}}
        assert workbench["requests"][-1]["path"]==f"/api/sessions/{SID}/external/tool"
    finally:
        client.unregister()


def test_echo_demo_real_process_labels_replies_interrupts_and_disconnects(workbench):
    process = subprocess.Popen([sys.executable, str(APP / "scripts/demo_external_agent.py"), SID, "--url", workbench["url"], "--delay", ".3"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=APP,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        start_new_session=os.name != "nt")
    tree = _ProcessTree(process)
    try:
        wait_for(lambda: workbench["registered"])
        assert pid_is_alive(workbench["registered"]["pid"])
        assert "DEMO" in workbench["registered"]["name"] and workbench["registered"]["model"] == "echo-demo"
        wait_for(lambda: any(e["type"] == "message" for e in workbench["events"]))
        workbench["enqueue"]("message", "取消这条回显")
        workbench["enqueue"]("interrupt")
        wait_for(lambda: any("已停止" in e.get("text", "") for e in workbench["events"]))
        assert not any("收到：取消" in e.get("text", "") for e in workbench["events"])
        workbench["enqueue"]("message", "你好 外部Agent")
        wait_for(lambda: any("收到：你好 外部Agent" in e.get("text", "") for e in workbench["events"]))
        reply = next(e for e in workbench["events"] if "收到：你好" in e.get("text", ""))
        assert "DEMO echo" in reply["text"] and "非 LLM" in reply["text"] and "未生成/修改方案" in reply["text"]
        workbench["enqueue"]("disconnect")
        assert process.wait(timeout=8) == 0
        assert workbench["unregister_count"] == 1
        assert all("/agent" not in r["path"] and "/api/tools/call" != r["path"] for r in workbench["requests"])
    finally:
        tree.close()
        for stream in (process.stdout, process.stderr):
            stream.close()


def test_cli_register_owner_stays_alive_then_unregisters(workbench):
    process = subprocess.Popen([sys.executable, str(APP / "external_agent_client.py"), "register", SID, "--url", workbench["url"], "--interval", ".05"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=APP,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        start_new_session=os.name != "nt")
    tree = _ProcessTree(process)
    try:
        wait_for(lambda: workbench["registered"])
        assert process.poll() is None and pid_is_alive(workbench["registered"]["pid"])
        workbench["enqueue"]("disconnect")
        assert process.wait(timeout=8) == 0
        output = process.stdout.read().decode("utf-8")
        lines = [json.loads(line) for line in output.splitlines()]
        assert lines[0]["binding_id"] and any(line.get("command", {}).get("type") == "disconnect" for line in lines)
        assert workbench["unregister_count"] == 1
    finally:
        tree.close()
        for stream in (process.stdout, process.stderr):
            stream.close()
