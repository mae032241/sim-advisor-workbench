"""Real SDK client -> stdio bridge -> temporary HTTP API, with no LLM."""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from workbench_mcp import WorkbenchBridge, WorkbenchHTTPError, loopback_url, resolve_url


SID = "a" * 32


def manifest():
    scope = {"session_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}}
    specs = [
        ("get_task_context", scope, ["session_id"]),
        ("search_knowledge", dict(scope, query={"type": "string"}), ["query"]),
        ("read_source", dict(scope, id={"type": "string"}), ["id"]),
        ("save_plan", dict(scope, revision={"type": "integer"}, plan={"type": "object"}), ["session_id", "revision", "plan"]),
        ("update_steps", dict(scope, revision={"type": "integer"}, steps={"type": "array"}), ["session_id", "revision", "steps"]),
        ("get_library_scope", dict(scope, query={"type": "string"}, type={"type": "string"}), []),
        ("set_libraries_enabled", dict(scope, library_ids={"type": "array", "items": {"type": "string"}}, enabled={"type": "boolean"}), ["library_ids", "enabled"]),
    ]
    return [{"name": name, "description": "公开工作台工具 " + name,
             "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}
            for name, properties, required in specs]


@pytest.fixture
def workbench():
    state = {"requests": [], "tools": manifest(), "get_mode": "normal", "delay": 0, "response_mode": "normal"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, value=None, raw=None):
            body = raw if raw is not None else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            state["requests"].append({"method": "GET", "path": self.path})
            if state["get_mode"] == "redirect":
                self.send_response(302)
                self.send_header("Location", "http://192.0.2.1/private")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if state["get_mode"] == "bad_json":
                return self.send(200, raw=b"not json")
            if state["get_mode"] == "offline":
                return self.send(503, {"error": "maintenance"})
            if self.path == "/api/tools":
                return self.send(200, {"tools": state["tools"]})
            self.send(404, {"error": "unknown route"})

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"method": "POST", "path": self.path, "body": value})
            if state["delay"]:
                time.sleep(state["delay"])
            if state["response_mode"] == "bad_json":
                return self.send(200, raw=b"<html>unexpected</html>")
            if state["response_mode"] == "wrong_shape":
                return self.send(200, {"result": []})
            name, args = value["name"], value["arguments"]
            if name in {"get_task_context", "get_plan", "save_plan", "update_steps"} and not args.get("session_id"):
                return self.send(422, {"error": "session_id required"})
            if name in {"get_task_context", "get_plan"}:
                return self.send(200, {"result": {"id": args["session_id"], "revision": 11, "plan": {"steps": []}}})
            if name == "search_knowledge":
                return self.send(200, {"result": {"query": args["query"], "results": [{"id": "fixture-source", "retrieval": {"backend": "native"}}]}})
            if name == "read_source":
                return self.send(200, {"result": {"id": args["id"], "text": "真实传输的测试文档", "recorded_for_session": args.get("session_id")}})
            if name == "get_library_scope":
                return self.send(200, {"result": {"schema": "library-scope/v1", "libraries": [{"id": "manual-a", "enabled": False}]}})
            if name == "set_libraries_enabled":
                return self.send(200, {"result": {"library_ids": args["library_ids"], "enabled": args["enabled"], "changed": len(args["library_ids"]), "index_rebuilt": False}})
            if name in {"save_plan", "update_steps"}:
                if args["revision"] != 11:
                    return self.send(409, {"error": "revision conflict", "current_revision": 11})
                return self.send(200, {"result": {"saved": True, "revision": 12}})
            self.send(400, {"error": "unknown public tool"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8765", "http://0.0.0.0:8765", "http://192.168.1.1", "http://example.com", "http://127.0.0.1.example.com",
    "http://user:secret@127.0.0.1", "http://127.0.0.1/api", "http://127.0.0.1?x=1", "http://127.0.0.1#fragment",
    "http://127.0.0.1:0", "http://127.0.0.1:99999", "http://[fe80::1]", "http://127.0.0.1\n", "file:///tmp/server", "http://[::1%25zone]",
])
def test_only_loopback_http_origin_is_allowed(url):
    with pytest.raises(ValueError):
        loopback_url(url)


def test_url_defaults_to_server_record_and_validates_it(tmp_path, monkeypatch):
    (tmp_path / "server.json").write_text(json.dumps({"url": "http://localhost:8765/"}), encoding="utf-8")
    monkeypatch.setenv("SIM_ADVISOR_DATA", str(tmp_path))
    assert resolve_url() == "http://127.0.0.1:8765"
    assert loopback_url("http://[::1]:8000/") == "http://[::1]:8000"
    assert resolve_url("http://127.0.0.2:9") == "http://127.0.0.2:9"
    assert resolve_url(data_dir=tmp_path) == "http://127.0.0.1:8765"
    (tmp_path / "server.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="server.json"):
        resolve_url(data_dir=tmp_path)


def test_http_transport_preserves_schema_scope_and_original_result(workbench):
    async def run():
        bridge = WorkbenchBridge(workbench["url"])
        try:
            tools = await bridge.list_tools()
            assert [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools] == manifest()
            result = await bridge.call_tool("search_knowledge", {"query": "入口压力 混合"})
            assert not result.isError
            assert json.loads(result.content[0].text) == {"query": "入口压力 混合", "results": [{"id": "fixture-source", "retrieval": {"backend": "native"}}]}
            args = {"id": "fixture-source", "session_id": SID}
            read = await bridge.call_tool("read_source", args)
            assert json.loads(read.content[0].text)["recorded_for_session"] == SID
            assert workbench["requests"][-1]["body"] == {"name": "read_source", "arguments": args}
        finally:
            await bridge.aclose()
    asyncio.run(run())


def test_library_tools_are_discovered_and_forwarded_without_plan_or_approval():
    """Exercise the real MCP adapter without requiring sandbox loopback sockets."""
    requests = []
    def handle(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'tools': manifest()})
        body = json.loads(request.content)
        requests.append(body)
        if body['name'] == 'get_library_scope':
            return httpx.Response(200, json={'result': {'schema': 'library-scope/v1', 'libraries': [{'id': 'manual-a', 'enabled': False}]}})
        return httpx.Response(200, json={'result': {'library_ids': body['arguments']['library_ids'], 'enabled': body['arguments']['enabled'], 'changed': 2, 'index_rebuilt': False}})
    async def run():
        bridge = WorkbenchBridge('http://127.0.0.1:8765')
        await bridge.client.aclose()
        bridge.client = httpx.AsyncClient(base_url=bridge.url, transport=httpx.MockTransport(handle))
        try:
            definitions = {tool.name: tool.inputSchema for tool in await bridge.list_tools()}
            assert 'session_id' not in definitions['get_library_scope']['required']
            scope = await bridge.call_tool('get_library_scope', {'query': 'acoustics'})
            assert not scope.isError and json.loads(scope.content[0].text)['libraries'][0]['enabled'] is False
            args = {'library_ids': ['manual-a', 'model-b'], 'enabled': True}
            enabled = await bridge.call_tool('set_libraries_enabled', args)
            assert not enabled.isError
            assert json.loads(enabled.content[0].text) == {**args, 'changed': 2, 'index_rebuilt': False}
            assert requests == [
                {'name': 'get_library_scope', 'arguments': {'query': 'acoustics'}},
                {'name': 'set_libraries_enabled', 'arguments': args},
            ]
        finally:
            await bridge.aclose()
    asyncio.run(run())


def test_http_errors_remain_normal_tool_errors_and_do_not_retry_mutations(workbench):
    async def run():
        bridge = WorkbenchBridge(workbench["url"])
        try:
            result = await bridge.call_tool("save_plan", {"session_id": SID, "revision": 9, "plan": {}})
            assert result.isError and json.loads(result.content[0].text) == {"error": "revision conflict", "current_revision": 11}
            assert len(workbench["requests"]) == 1
            missing = await bridge.call_tool("get_task_context", {})
            assert missing.isError and json.loads(missing.content[0].text)["error"] == "session_id required"
            assert (await bridge.call_tool("unknown", {})).isError
        finally:
            await bridge.aclose()
    asyncio.run(run())


def test_discovery_rejects_redirect_invalid_json_and_duplicate_definitions(workbench):
    async def run():
        bridge = WorkbenchBridge(workbench["url"])
        try:
            for mode in ["redirect", "bad_json", "offline"]:
                workbench["get_mode"] = mode
                with pytest.raises(WorkbenchHTTPError):
                    await bridge.list_tools()
            workbench["get_mode"] = "normal"
            workbench["tools"] = [manifest()[0], manifest()[0]]
            with pytest.raises(WorkbenchHTTPError, match="重复"):
                await bridge.list_tools()
            assert all(request["path"] == "/api/tools" for request in workbench["requests"])
        finally:
            await bridge.aclose()
    asyncio.run(run())


def test_bounded_responses_requests_timeout_and_malformed_tool_result(workbench):
    async def run():
        small = WorkbenchBridge(workbench["url"], max_response_bytes=100, max_request_bytes=80)
        try:
            with pytest.raises(WorkbenchHTTPError, match="大小限制"):
                await small.list_tools()
            assert (await small.call_tool("read_source", {"id": "A" * 200})).isError
        finally:
            await small.aclose()
        bridge = WorkbenchBridge(workbench["url"], timeout=.05)
        try:
            workbench["delay"] = .2
            timed = await bridge.call_tool("search_knowledge", {"query": "flow"})
            assert timed.isError and "尚未确认" in timed.content[0].text
            workbench["delay"] = 0
            for mode in ["bad_json", "wrong_shape"]:
                workbench["response_mode"] = mode
                assert (await bridge.call_tool("search_knowledge", {"query": "flow"})).isError
        finally:
            await bridge.aclose()
    asyncio.run(run())


def test_bridge_ignores_http_proxy_environment(workbench, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://192.0.2.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    async def run():
        bridge = WorkbenchBridge(workbench["url"])
        try:
            assert len(await bridge.list_tools()) == 7
        finally:
            await bridge.aclose()
    asyncio.run(run())


def test_real_mcp_stdio_client_list_call_alias_and_http_error_without_llm(workbench):
    async def run():
        parameters = StdioServerParameters(command=sys.executable,
            args=[str(APP / "workbench_mcp.py"), "--url", workbench["url"], "--timeout", "5"], cwd=APP,
            env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "SIM_ADVISOR_CODEX": "must-never-be-started"})
        with anyio.fail_after(20):
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=8)) as client:
                    initialized = await client.initialize()
                    assert initialized.serverInfo.name == "comsol-workbench"
                    tools = await client.list_tools()
                    assert [t.name for t in tools.tools] == [d["name"] for d in manifest()]
                    search = await client.call_tool("search_knowledge", {"query": "压降与混合"})
                    assert not search.isError and json.loads(search.content[0].text)["query"] == "压降与混合"
                    scope = await client.call_tool("get_library_scope", {})
                    assert not scope.isError and json.loads(scope.content[0].text)["libraries"][0]["enabled"] is False
                    enabled = await client.call_tool("set_libraries_enabled", {"library_ids": ["manual-a"], "enabled": True})
                    assert not enabled.isError and json.loads(enabled.content[0].text)["index_rebuilt"] is False
                    independent_read = await client.call_tool("read_source", {"id": "fixture-source"})
                    assert not independent_read.isError and json.loads(independent_read.content[0].text)["recorded_for_session"] is None
                    context = await client.call_tool("get_task_context", {"session_id": SID})
                    assert not context.isError and json.loads(context.content[0].text)["revision"] == 11
                    alias = await client.call_tool("get_plan", {"session_id": SID})
                    assert not alias.isError and json.loads(alias.content[0].text)["id"] == SID
                    saved = await client.call_tool("save_plan", {"session_id": SID, "revision": 11, "plan": {}})
                    assert not saved.isError and json.loads(saved.content[0].text)["saved"]
                    rejected = await client.call_tool("update_steps", {"session_id": SID, "revision": 9, "steps": []})
                    assert rejected.isError and json.loads(rejected.content[0].text)["error"] == "revision conflict"
                    count = len(workbench["requests"])
                    invalid = await client.call_tool("get_task_context", {})
                    assert invalid.isError and len(workbench["requests"]) == count  # SDK input-schema check.
    asyncio.run(run())
    assert all(request["path"] in {"/api/tools", "/api/tools/call"} for request in workbench["requests"])
