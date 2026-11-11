#!/usr/bin/env python3
"""Exercise PageIndex/Agents/MCP 1 together without invoking any language model.

Run using the distribution being verified, not the workspace interpreter::

    python-unified/python.exe -I -B scripts/verify_unified_sdk.py \
        --work-dir SCRATCH --output REPORT.json

The stdio child is the official MCP SDK's FastMCP server. PageIndex uses a
separate empty local store; neither an index generation nor an LLM is mocked
as successful. External socket connections are denied in both processes.
Pass --base-only to test the minimal runtime without any PageIndex/Agents imports.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import importlib
import importlib.metadata
import importlib.util
import ipaddress
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import threading
import time
import traceback


def isolation() -> list[dict]:
    """Leave loopback available for Windows asyncio's internal socket pair."""
    os.environ.update({
        "PYTHON_DOTENV_DISABLED": "1",
        "OPENAI_AGENTS_DISABLE_TRACING": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "LITELLM_TELEMETRY": "False",
        "DO_NOT_TRACK": "1",
        "HF_HUB_OFFLINE": "1",
        "OPENAI_API_KEY": "offline-sdk-test-no-model",
        "ANTHROPIC_API_KEY": "",
        "PAGEINDEX_API_KEY": "",
    })
    denied: list[dict] = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def check(address):
        host = address[0] if isinstance(address, tuple) and address else None
        if host is None:
            return  # AF_UNIX/pipe-like local endpoints, not internet sockets.
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = str(host).lower() == "localhost"
        if not local:
            denied.append({"host": str(host), "api": "socket", "stack": traceback.format_stack(limit=5)})
            raise RuntimeError(f"External network is disabled during SDK verification: {host}")

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, port, *args, **kwargs):
        # Resolve only explicit local endpoints. Avoid even external DNS lookup.
        if host is not None:
            check((host, port))
        return original_getaddrinfo(host, port, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
    return denied


def text_content(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_content(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return str(getattr(value, "text", ""))


def stdio_server() -> None:
    denied = isolation()
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("unified-sdk-local-echo")

    @server.tool()
    def echo(text: str) -> dict:
        """Return the supplied text; this is a local SDK transport test."""
        return {"echo": text, "network_attempts": len(denied), "model_called": False}

    @server.tool()
    def deliberate_failure() -> str:
        """Return a real tool error to verify the MCP 1 isError contract."""
        raise ValueError("intentional offline SDK verification failure")

    server.run(transport="stdio")


class DirectSDKStdio:
    """Small test harness using the real MCP 1 ClientSession, no Agent SDK."""

    def __init__(self, params, name=None, client_session_timeout_seconds=10):
        self.params = params
        self.name = name
        self.timeout = client_session_timeout_seconds
        self.stack = AsyncExitStack()

    async def __aenter__(self):
        from mcp import ClientSession, StdioServerParameters, stdio_client
        try:
            read, write = await self.stack.enter_async_context(
                stdio_client(StdioServerParameters(**self.params)))
            self.session = await self.stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=self.timeout)))
            self.server_initialize_result = await self.session.initialize()
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *error):
        return await self.stack.__aexit__(*error)

    async def list_tools(self):
        return (await self.session.list_tools()).tools

    async def call_tool(self, name, arguments):
        return await self.session.call_tool(name, arguments)


async def verify_base(work: Path, result: dict) -> None:
    """No addon: actual MCP 1 initialize/list/call/error and HTTP bridge."""
    unwanted = ["pageindex", "agents", "litellm"]
    absent = {name: importlib.util.find_spec(name) is None for name in unwanted}
    assert all(absent.values()), f"Base runtime unexpectedly exposes addon modules: {absent}"
    result["addon_modules_absent"] = absent
    params = {"command": sys.executable,
              "args": ["-I", "-B", str(Path(__file__).resolve()), "--stdio-server"]}
    async with DirectSDKStdio(params, name="minimal-official-sdk-stdio") as server:
        tools = await server.list_tools()
        assert {tool.name for tool in tools} == {"echo", "deliberate_failure"}
        value = "Minimal Python MCP 1 without PageIndex or Agents"
        reply = await server.call_tool("echo", {"text": value})
        assert reply.isError is not True
        assert json.loads(text_content(reply.content)) == {
            "echo": value, "network_attempts": 0, "model_called": False}
        error = await server.call_tool("deliberate_failure", {})
        assert error.isError is True
        result["official_mcp_stdio"] = {
            "client": "mcp.ClientSession (official MCP 1 SDK)",
            "initialize": server.server_initialize_result.model_dump(mode="json", by_alias=True),
            "tools": [tool.name for tool in tools],
            "reply": reply.model_dump(mode="json", by_alias=True),
            "error_is_error": error.isError,
        }
    await verify_http_bridge(result, base_only=True)
    assert not any(name in sys.modules for name in unwanted), "Base probe imported an addon module"
    result["addon_modules_imported"] = []


async def verify(work: Path, result: dict) -> None:
    from agents import Agent, FunctionTool, set_tracing_disabled
    from agents.mcp import MCPServerStdio, MCPUtil
    from agents.mcp import _compat
    from agents.tool_context import ToolContext
    from agents.extensions.models.litellm_model import LitellmModel
    from mcp import ClientSession, StdioServerParameters, Tool, stdio_client
    from mcp.client.session import MessageHandlerFnT
    from mcp.types import CallToolResult, ListToolsResult, PaginatedRequestParams
    from pageindex import PageIndexLocalClient
    from pageindex.integrations.openai_agents import build_mcp_server

    set_tracing_disabled(True)
    assert not _compat.MCP_V2, "The unified distribution must exercise Agents' MCP 1 branch"
    required = [ClientSession, StdioServerParameters, Tool, stdio_client,
                MessageHandlerFnT, CallToolResult, ListToolsResult, PaginatedRequestParams]
    result["mcp_interfaces"] = {"agents_mcp_v2": _compat.MCP_V2, "required_interfaces_present": len(required)}

    # Actual PageIndex local constructor and its public OpenAI Tools adapter.
    # Browsing an empty local store does not call any document/model pipeline.
    pageindex = PageIndexLocalClient(storage_path=work / "pageindex-local-store")
    assert not getattr(pageindex, "api_key", None)
    local_server = build_mcp_server(pageindex)
    local_tools = await local_server.list_tools()
    names = [tool.name for tool in local_tools]
    assert "browse_documents" in names, names
    pageindex_result = await local_server.call_tool("browse_documents", {})
    assert pageindex_result.isError is not True, text_content(pageindex_result.content)
    pageindex_tools = pageindex.as_openai_tools()
    assert pageindex_tools and all(isinstance(tool, FunctionTool) for tool in pageindex_tools)
    local_function = next(tool for tool in pageindex_tools if tool.name == "browse_documents")
    context = ToolContext(context=None, tool_name=local_function.name,
                          tool_call_id="offline-pageindex-browse", tool_arguments="{}")
    local_function_result = await local_function.on_invoke_tool(context, "{}")
    result["pageindex"] = {
        "client_class": type(pageindex).__name__, "mode": "local",
        "mcp_server_tools": names, "agents_function_tools": [tool.name for tool in pageindex_tools],
        "direct_mcp_browse": pageindex_result.model_dump(mode="json", by_alias=True),
        "agents_function_browse": text_content(local_function_result),
        "model_called": False,
    }
    # Local SDK data errors should remain tool errors, not import/ABI failures.
    missing_document = await local_server.call_tool("get_document_structure", {"doc_id": "offline-nonexistent-document"})
    assert missing_document.isError is True
    result["pageindex"]["missing_document_is_error"] = missing_document.isError
    await local_server.cleanup()

    # Real official-SDK stdio initialize -> tools/list -> tools/call, through
    # the Agents adapter (not a hand-written transport or a fake ClientSession).
    params = {"command": sys.executable,
              "args": ["-I", "-B", str(Path(__file__).resolve()), "--stdio-server"],
              "env": {key: os.environ[key] for key in ["PYTHON_DOTENV_DISABLED", "OPENAI_AGENTS_DISABLE_TRACING", "LITELLM_LOCAL_MODEL_COST_MAP", "DO_NOT_TRACK"]}}
    server = MCPServerStdio(params=params, name="unified-sdk-stdio", client_session_timeout_seconds=10)
    async with server:
        initialize = server.server_initialize_result
        assert initialize is not None
        tools = await server.list_tools()
        tool_names = [tool.name for tool in tools]
        assert set(tool_names) == {"echo", "deliberate_failure"}, tool_names
        message = "PageIndex + Agents + MCP 1 share one Python"
        direct = await server.call_tool("echo", {"text": message})
        assert direct.isError is not True
        echo_result = json.loads(text_content(direct.content))
        assert echo_result == {"echo": message, "network_attempts": 0, "model_called": False}
        error = await server.call_tool("deliberate_failure", {})
        assert error.isError is True
        function = MCPUtil.to_function_tool(next(tool for tool in tools if tool.name == "echo"), server, False)
        assert isinstance(function, FunctionTool)
        arguments = json.dumps({"text": "Converted FunctionTool works"})
        context = ToolContext(context=None, tool_name=function.name, tool_call_id="offline-mcp-function", tool_arguments=arguments)
        converted = await function.on_invoke_tool(context, arguments)
        assert json.loads(text_content(converted))["echo"] == "Converted FunctionTool works"
        model = LitellmModel(model="openai/offline-probe", api_key="offline-dummy-not-a-key")
        agent = Agent(name="Offline SDK compatibility only", model=model, tools=[function, *pageindex_tools], mcp_servers=[server])
        assert agent.mcp_servers == [server]
        result["agents_mcp_stdio"] = {
            "initialize": initialize.model_dump(mode="json", by_alias=True),
            "tool_names": tool_names,
            "direct_call": direct.model_dump(mode="json", by_alias=True),
            "error_is_error": error.isError,
            "converted_function_call": text_content(converted),
            "agent_constructed": type(agent).__name__, "runner_called": False,
            "model_adapter_constructed": type(model).__name__, "model_request_sent": False,
        }
    await verify_http_bridge(result)


async def verify_http_bridge(result: dict, *, base_only=False) -> None:
    """Production MCP bridge + real loopback HTTP; HTTP API data is a fixture."""
    if base_only:
        client_class = DirectSDKStdio
    else:
        from agents.mcp import MCPServerStdio
        client_class = MCPServerStdio

    requests_seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, value):
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            requests_seen.append({"method": "GET", "path": self.path})
            if self.path != "/api/tools":
                return self.respond(404, {"error": "fixture route not found"})
            self.respond(200, {"tools": [{"name": "echo", "description": "Controlled local HTTP fixture", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}}, {"name": "deliberate_failure", "description": "Controlled HTTP validation failure", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            requests_seen.append({"method": "POST", "path": self.path, "tool": payload.get("name")})
            if self.path != "/api/tools/call":
                return self.respond(404, {"error": "fixture route not found"})
            if payload.get("name") == "echo":
                return self.respond(200, {"result": {"echo": payload["arguments"]["text"], "controlled_fixture": True}})
            self.respond(422, {"error": "intentional fixture validation failure"})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}"
        bridge = client_class(params={"command": sys.executable,
            "args": ["-I", "-B", str(Path(__file__).resolve()), "--bridge-url", url]},
            name="unified-production-http-bridge", client_session_timeout_seconds=10)
        async with bridge:
            tools = await bridge.list_tools()
            assert {tool.name for tool in tools} == {"echo", "deliberate_failure"}
            reply = await bridge.call_tool("echo", {"text": "HTTP -> MCP stdio -> SDK client"})
            assert reply.isError is not True
            assert json.loads(text_content(reply.content)) == {"echo": "HTTP -> MCP stdio -> SDK client", "controlled_fixture": True}
            error = await bridge.call_tool("deliberate_failure", {})
            assert error.isError is True
            assert "intentional fixture validation failure" in text_content(error.content)
            result["workbench_http_mcp_bridge"] = {
                "http_api": "controlled loopback fixture, not a production library",
                "bridge": "actual workbench_mcp.py WorkbenchBridge + official MCP stdio Server",
                "client": client_class.__name__,
                "initialize": bridge.server_initialize_result.model_dump(mode="json", by_alias=True),
                "tools": [tool.name for tool in tools],
                "reply": reply.model_dump(mode="json", by_alias=True),
                "http_422_is_error": error.isError, "http_requests": requests_seen,
            }
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdio-server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--bridge-url", help=argparse.SUPPRESS)
    parser.add_argument("--base-only", action="store_true", help="Test minimal MCP runtime without PageIndex/Agents/LiteLLM")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    if args.stdio_server:
        stdio_server()
        return 0
    if args.bridge_url:
        isolation()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from workbench_mcp import main as bridge_main
        bridge_main(["--url", args.bridge_url, "--timeout", "10"])
        return 0
    if args.output is None or args.work_dir is None:
        parser.error("--output and --work-dir are required")
    began = time.monotonic()
    result = {"ok": False, "python": sys.executable, "python_version": sys.version,
              "scope": "Official MCP 1 stdio + production HTTP bridge; no addon imports" if args.base_only else "PageIndex local tools + Agents MCP 1 stdio transport/conversion; no LLM/index generation", "stages": []}
    denied = isolation()
    try:
        work = args.work_dir.resolve()
        if work.exists() and any(work.iterdir()):
            raise ValueError("--work-dir must be empty; existing library/model data is never modified")
        work.mkdir(parents=True, exist_ok=True)
        expected = {"mcp": "1.29.1", "httpx": "0.28.1"}
        if not args.base_only:
            expected.update({"pageindex": "0.5.3", "openai-agents": "0.20.0"})
        result["versions"] = {name: importlib.metadata.version(name) for name in expected}
        assert result["versions"] == expected, result["versions"]
        asyncio.run(asyncio.wait_for((verify_base if args.base_only else verify)(work, result), timeout=60))
        assert not denied, denied
        result["ok"] = True
    except BaseException:
        result["error"] = traceback.format_exc()
    result["external_network_attempts"] = denied
    result["elapsed_seconds"] = round(time.monotonic() - began, 3)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": result["ok"], "report": str(args.output.resolve()), "elapsed_seconds": result["elapsed_seconds"], "error": result.get("error")}, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
