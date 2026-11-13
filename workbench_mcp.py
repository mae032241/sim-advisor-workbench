"""Backend-independent MCP stdio bridge to an already running local workbench.

Run with the project interpreter: python workbench_mcp.py [--url http://127.0.0.1:PORT].
Tool definitions and validation belong to the HTTP workbench, not to an Agent
backend. This process never starts Codex, a model, or the workbench itself.
Requires the official MCP Python SDK 1.x; see requirements-mcp.txt.
"""
from __future__ import annotations

import argparse
from functools import partial
import ipaddress
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpx
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, CallToolResult, ErrorData, TextContent, Tool


class WorkbenchHTTPError(Exception):
    def __init__(self, payload, status=None):
        self.payload = payload
        self.status = status
        super().__init__(str(payload.get("error", "Workbench HTTP request failed")))


def loopback_url(value: str) -> str:
    """Accept a plain HTTP loopback origin, with no credentials or URL routing."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("工作台地址必须是本机 HTTP 地址。")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ValueError("工作台地址无效。") from exc
    if (parsed.scheme != "http" or not host or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("仅支持无凭据、无路径的本机 HTTP 地址。")
    if host == "localhost":
        host = "127.0.0.1"  # Do not depend on proxies or hostname resolution.
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("工作台地址必须使用 localhost 或 loopback IP。") from exc
    if not address.is_loopback or "%" in host:
        raise ValueError("仅支持 loopback 工作台地址。")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("工作台端口无效。")
    netloc = f"[{address}]" if address.version == 6 else str(address)
    if port is not None:
        netloc += f":{port}"
    return urlunsplit(("http", netloc, "", "", ""))


def resolve_url(url=None, *, data_dir=None):
    if url is None:
        directory = Path(data_dir) if data_dir is not None else Path(os.environ.get("SIM_ADVISOR_DATA", Path(__file__).resolve().parent / "data"))
        try:
            value = json.loads((directory / "server.json").read_text(encoding="utf-8"))
            url = value["url"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("无法读取 data/server.json；先启动工作台，或使用 --url 指定本机地址。") from exc
    return loopback_url(url)


def tool_result(value, *, error=False):
    """Preserve the HTTP result object as JSON text, including error objects."""
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False, allow_nan=False))], isError=error)


class WorkbenchBridge:
    def __init__(self, url, *, timeout=60, max_response_bytes=8 * 1024 * 1024,
                 max_request_bytes=2 * 1024 * 1024):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("HTTP 超时必须为 0–300 秒之间的正数。")
        if type(max_response_bytes) is not int or max_response_bytes <= 0 or type(max_request_bytes) is not int or max_request_bytes <= 0:
            raise ValueError("HTTP 大小限制必须是正整数。")
        self.url = loopback_url(url)
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.max_request_bytes = max_request_bytes
        self.client = httpx.AsyncClient(base_url=self.url, trust_env=False, follow_redirects=False, timeout=timeout)

    async def aclose(self):
        await self.client.aclose()

    async def _json(self, method, path, payload=None):
        try:
            content = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise WorkbenchHTTPError({"error": "工具参数必须是合法 JSON。"}) from exc
        if content is not None and len(content) > self.max_request_bytes:
            raise WorkbenchHTTPError({"error": "工具请求超过 MCP 桥接大小限制；请缩小本次更新。"})
        try:
            with anyio.fail_after(self.timeout):
                async with self.client.stream(method, path, content=content,
                                              headers={"Accept": "application/json", "Content-Type": "application/json"}) as response:
                    # Never follow redirects, even when the first origin is local.
                    if 300 <= response.status_code < 400:
                        raise WorkbenchHTTPError({"error": "工作台返回重定向；桥接不会跟随。"}, response.status_code)
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_response_bytes:
                            raise WorkbenchHTTPError({"error": "工作台响应超过 MCP 桥接大小限制；请分页读取。"}, response.status_code)
                        chunks.append(chunk)
                    try:
                        value = json.loads(b"".join(chunks))
                    except (ValueError, UnicodeError) as exc:
                        raise WorkbenchHTTPError({"error": "工作台未返回合法 JSON。"}, response.status_code) from exc
                    if not isinstance(value, dict):
                        raise WorkbenchHTTPError({"error": "工作台响应必须是 JSON 对象。"}, response.status_code)
                    if not 200 <= response.status_code < 300:
                        raise WorkbenchHTTPError(value, response.status_code)
                    return value
        except TimeoutError as exc:
            raise WorkbenchHTTPError({"error": "本机工作台请求超时；操作结果尚未确认，请读取当前版本后再重试。"}) from exc
        except httpx.HTTPError as exc:
            raise WorkbenchHTTPError({"error": "无法连接已运行的本机工作台，或连接中断；请检查服务状态。"}) from exc

    async def list_tools(self):
        value = await self._json("GET", "/api/tools")
        definitions = value.get("tools")
        if not isinstance(definitions, list):
            raise WorkbenchHTTPError({"error": "工作台工具清单缺少 tools 数组。"})
        result, seen = [], set()
        for definition in definitions:
            if (not isinstance(definition, dict) or not isinstance(definition.get("name"), str)
                    or not definition["name"] or definition["name"] in seen
                    or not isinstance(definition.get("description"), str) or not isinstance(definition.get("inputSchema"), dict)):
                raise WorkbenchHTTPError({"error": "工作台工具清单包含无效或重复定义。"})
            # Pass schemas through; session scope and future tools are owned by
            # the workbench API, rather than duplicated in this transport.
            result.append(Tool(name=definition["name"], description=definition["description"], inputSchema=definition["inputSchema"]))
            seen.add(definition["name"])
        return result

    async def call_tool(self, name, arguments=None):
        if not isinstance(name, str) or not name or (arguments is not None and not isinstance(arguments, dict)):
            return tool_result({"error": "工具名称或参数对象无效。"}, error=True)
        try:
            value = await self._json("POST", "/api/tools/call", {"name": name, "arguments": arguments or {}})
            if not isinstance(value.get("result"), dict):
                raise WorkbenchHTTPError({"error": "工作台工具响应缺少 result 对象。"})
            return tool_result(value["result"])
        except WorkbenchHTTPError as exc:
            return tool_result(exc.payload, error=True)


def create_server(bridge):
    server = Server("comsol-workbench", version="1.0.0", instructions="工作台工具独立于 Agent 后端。复杂或不确定字段先用get_tool_contract按输入JSON pointer查询实际字段、枚举和语义；契约可分页读取，无需方案且不读取资料正文。可先查询资料库目录并按需要启用指定库，无需确认，不自动启用全部库。先检索再读取正文；保存方案须明确 session_id 与当前 revision。")

    @server.list_tools()
    async def list_tools():
        try:
            return await bridge.list_tools()
        except WorkbenchHTTPError as exc:
            # Discovery failures are protocol errors; pretending the tool list
            # is empty would hide an unavailable workbench.
            raise McpError(ErrorData(code=INTERNAL_ERROR, message="工作台工具发现失败。", data=exc.payload)) from exc

    @server.call_tool()
    async def call_tool(name, arguments):
        return await bridge.call_tool(name, arguments)

    return server


async def serve(url, *, timeout=60):
    bridge = WorkbenchBridge(url, timeout=timeout)
    try:
        server = create_server(bridge)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await bridge.aclose()


def main(argv=None):
    parser = argparse.ArgumentParser(description="将已运行的本机工作台 HTTP 工具 API 暴露为 MCP stdio；不启动 Agent 或 Codex。")
    parser.add_argument("--url", help="本机 HTTP 地址，默认读取 data/server.json")
    parser.add_argument("--timeout", type=float, default=60, help="单次 HTTP 请求总超时秒数（默认60，最多300）")
    args = parser.parse_args(argv)
    try:
        url = resolve_url(args.url)
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 300:
            raise ValueError("--timeout 必须为 0–300 秒之间的正数。")
    except ValueError as exc:
        parser.error(str(exc))
    anyio.run(partial(serve, url, timeout=args.timeout))


if __name__ == "__main__":
    main()
