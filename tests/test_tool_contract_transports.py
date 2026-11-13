"""Actual catalog/contract service through the supported Agent transports."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from urllib.request import ProxyHandler, build_opener

import pytest

import agent_tools
import tools_cli
from advisor.agent_sessions import planning_tools
from advisor.chat_bridge_tools import ChatBridgeTools, external_tool_definitions
from advisor.codex_runtime import CodexRuntime
from advisor.planning_service import PlanningService, tool_definitions
from advisor.server import make_server
from advisor.tool_contracts import read_contract
from external_agent_client import ExternalAgentClient, ExternalAgentError


class NoDataAccess:
    def __getattr__(self, name):
        raise AssertionError("Contract discovery must not open task or library data: " + name)


@pytest.fixture
def contract_server(tmp_path):
    service = PlanningService(tmp_path, NoDataAccess(), NoDataAccess())
    calls = []

    def call(name, arguments):
        calls.append((name, arguments))
        return service.call_public(name, arguments)

    app = SimpleNamespace(tools=SimpleNamespace(call_public=call), chat_tools=ChatBridgeTools(NoDataAccess()))
    server = make_server(app, 0)
    server.RequestHandlerClass.log_message = lambda *_args: None
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", calls=calls)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def get_json(url):
    with build_opener(ProxyHandler({})).open(url, timeout=5) as response:
        return json.load(response)


def test_http_catalog_selects_exact_bound_definitions_without_mailbox_tools(contract_server):
    public = get_json(contract_server.url + "/api/tools")
    bound = get_json(contract_server.url + "/api/tools?bound=true")
    assert public["tools"] == tool_definitions() + external_tool_definitions()
    assert public["bound"] is False
    assert bound["tools"] == tool_definitions(bound=True, include_alias=True)
    assert bound["bound"] is True
    assert all("session_id" not in tool["inputSchema"]["properties"] for tool in bound["tools"])
    mailbox = {tool["name"] for tool in external_tool_definitions()}
    assert not mailbox & {tool["name"] for tool in bound["tools"]}
    assert contract_server.calls == []


def test_local_cli_discovers_contract_without_opening_or_creating_data(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("No knowledge provider should be created")
    monkeypatch.setattr(agent_tools, "KnowledgeBase", forbidden)
    monkeypatch.setattr(agent_tools, "LibraryStore", forbidden)
    directory = tmp_path / "does-not-exist"
    monkeypatch.setattr(sys, "argv", ["agent_tools.py", "--data-dir", str(directory), "list"])
    out = io.StringIO()
    with redirect_stdout(out):
        assert agent_tools.main() == 0
    definitions = json.loads(out.getvalue())["tools"]
    assert definitions == tool_definitions() + external_tool_definitions()
    monkeypatch.setattr(sys, "argv", ["agent_tools.py", "--data-dir", str(directory), "contract", "save_plan", "--path", "/design_brief/simplification/candidates/0/status", "--length", "23"])
    out = io.StringIO()
    with redirect_stdout(out):
        assert agent_tools.main() == 0
    actual = json.loads(out.getvalue())
    assert actual == read_contract(definitions, {"tool": "save_plan", "path": "/design_brief/simplification/candidates/0/status", "length": 23})
    assert not directory.exists()


def test_local_cli_preserves_contract_field_error_and_next_action(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agent_tools.py", "contract", "save_plan", "--path", "/invented_field"])
    out = io.StringIO()
    with redirect_stdout(out):
        assert agent_tools.main() == 1
    error = json.loads(out.getvalue())
    assert error["tool"] == "get_tool_contract"
    assert error["errors"][0]["path"] == "/path"
    assert error["next_action"]["tool"] == "get_tool_contract"


def test_http_cli_contract_uses_global_tool_and_preserves_input_pointer(contract_server, monkeypatch):
    arguments = {"tool": "save_plan", "path": "/design_brief/simplification/candidates/0/status", "offset": 5, "length": 19}
    monkeypatch.setattr(sys, "argv", ["tools_cli.py", "--url", contract_server.url, "contract", arguments["tool"], "--path", arguments["path"], "--offset", "5", "--length", "19"])
    out = io.StringIO()
    with redirect_stdout(out):
        assert tools_cli.main() == 0
    assert contract_server.calls == [("get_tool_contract", arguments)]
    assert json.loads(out.getvalue())["result"]["read_authority"] == "none"


def test_external_client_can_query_every_public_contract_without_a_task_or_registration(contract_server):
    client = ExternalAgentClient(url=contract_server.url)
    definitions = client.list_tools()
    for definition in definitions:
        result = client.call_tool("get_tool_contract", {"tool": definition["name"], "path": "", "length": 47})
        assert result == read_contract(definitions, {"tool": definition["name"], "path": "", "length": 47})
    assert client.session_id is None and client.binding_id is None
    count = len(contract_server.calls)
    with pytest.raises(ValueError, match="session_id"):
        client.register("Requires a real task")
    with pytest.raises(RuntimeError, match="register"):
        client.call_bound_tool("get_tool_contract", {"tool": "save_plan"})
    assert len(contract_server.calls) == count
    with pytest.raises(ValueError, match="session_id"):
        ExternalAgentClient(url=contract_server.url, binding_id="private")


def test_codex_dynamic_registration_keeps_the_bound_contract_schema():
    definitions = planning_tools()
    specs, names = CodexRuntime._tools(definitions)
    definition = next(tool for tool in definitions if tool["name"] == "get_tool_contract")
    registered = next(tool for tool in specs if tool["name"] == "get_tool_contract")
    assert "get_tool_contract" in names
    assert registered["inputSchema"] == definition["inputSchema"]
    assert registered["description"] == definition["description"]
    assert "session_id" not in registered["inputSchema"]["properties"]


@pytest.mark.parametrize("name,arguments,path,code,expected", [
    ("register_external_agent", {"session_id": "a" * 32, "name": "n" * 129, "pid": 1}, "/name", "maxLength", {"maxLength": 128}),
    ("register_external_agent", {"session_id": "a" * 32, "name": "n", "pid": 0x100000000}, "/pid", "maximum", {"maximum": 0xFFFFFFFF}),
    ("register_external_agent", {"session_id": "a" * 32, "name": "n", "pid": 1, "reasoning_effort": "e" * 65}, "/reasoning_effort", "maxLength", {"maxLength": 64}),
    ("publish_external_event", {"session_id": "a" * 32, "binding_id": "private", "event": {"type": "message", "text": "reply"}}, "/event/message_id", "required", {}),
    ("publish_external_event", {"session_id": "a" * 32, "binding_id": "private", "event": {"type": "state"}}, "/event/status", "required", {}),
    ("publish_external_event", {"session_id": "a" * 32, "binding_id": "private", "event": {"type": "activity", "text": "x" * 1001}}, "/event/text", "maxLength", {"maxLength": 1000}),
    ("publish_external_event", {"session_id": "a" * 32, "binding_id": "private", "event": {"type": "error", "text": "x" * 2001}}, "/event/text", "maxLength", {"maxLength": 2000}),
])
def test_mailbox_errors_preserve_field_expectations_before_data_access(contract_server, name, arguments, path, code, expected):
    client = ExternalAgentClient(url=contract_server.url)
    with pytest.raises(ExternalAgentError) as caught:
        client.call_tool(name, arguments)
    payload = caught.value.payload
    assert caught.value.status == 422 and payload["tool"] == name
    error = next(error for error in payload["errors"] if error["path"] == path and error["code"] == code)
    assert error["expected"].items() >= expected.items()
    assert payload["next_action"]["tool"] == "get_tool_contract"
    assert payload["next_action"]["arguments"]["tool"] == name


def test_real_mcp_stdio_discovers_contract_and_preserves_paging_and_errors(contract_server):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        app = Path(__file__).resolve().parents[1]
        params = StdioServerParameters(command=sys.executable, args=[str(app / "workbench_mcp.py"), "--url", contract_server.url])
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                catalog = await session.list_tools()
                definitions = {tool["name"]: tool for tool in tool_definitions() + external_tool_definitions()}
                assert {tool.name for tool in catalog.tools} == definitions.keys()
                contract = next(tool for tool in catalog.tools if tool.name == "get_tool_contract")
                assert contract.inputSchema == definitions["get_tool_contract"]["inputSchema"]
                arguments = {"tool": "save_plan", "path": "/design_brief/simplification/candidates/0/status", "length": 23}
                response = await session.call_tool("get_tool_contract", arguments)
                assert not response.isError
                first = json.loads(response.content[0].text)
                expected = read_contract(list(definitions.values()), arguments)
                assert first == expected
                response = await session.call_tool("get_tool_contract", dict(arguments, offset=first["next_offset"]))
                assert not response.isError
                second = json.loads(response.content[0].text)
                assert second["offset"] == first["next_offset"] and second["schema_sha256"] == first["schema_sha256"]
                error = await session.call_tool("get_tool_contract", {"tool": "save_plan", "path": "/invented_field"})
                assert error.isError
                payload = json.loads(error.content[0].text)
                assert payload["tool"] == "get_tool_contract" and payload["errors"][0]["path"] == "/path"
    asyncio.run(run())
