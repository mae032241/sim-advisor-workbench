"""Read-only knowledge MCP for a native Agent run; no evaluation or call budget."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import time

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, ToolAnnotations
from workbench_mcp import WorkbenchBridge, tool_result

TOOLS = ("get_library_scope", "search_knowledge", "read_source")
AUDIT_WRITE_FAILURE_MARKER = "BENCHMARK_KNOWLEDGE_AUDIT_WRITE_FAILED"
HTTP_TIMEOUT_SECONDS = 240
LIBRARY_IDS = (
    "cs63-documents", "cs63-models-acdc-module", "cs63-models-comsol-multiphysics",
    "cs63-models-heat-transfer-module", "cs63-models-optimization-module",
    "cs63-models-porous-media-flow-module", "cs63-models-subsurface-flow-module",
)


def metadata_value(value, depth=0):
    if isinstance(value, str):
        return value[:320]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if depth < 2 and isinstance(value, list):
        return [metadata_value(item, depth + 1) for item in value[:8]]
    return None


def source_summary(source):
    result = {key: metadata_value(source[key]) for key in ("id", "title", "kind", "type", "library_id") if key in source}
    context = source.get("source_context")
    if isinstance(context, dict):
        fields = ("role", "case_identity", "case_name", "package", "feature_branch", "feature_type",
                  "section", "physics", "status", "model_agreement", "execution_status", "applicability")
        result["source_context"] = {key: metadata_value(context[key]) for key in fields if key in context}
    return result


def tool_error_kind(result):
    """Classify ordinary errors without persisting their messages or payloads."""
    try:
        payload = json.loads(result.content[0].text)
        message = payload.get("error")
    except (ValueError, TypeError, AttributeError, IndexError):
        return "other"
    if not isinstance(message, str):
        return "upstream"
    if message.startswith("本机工作台请求超时"):
        return "timeout"
    if message.startswith(("工具参数", "工具arguments", "检索参数", "feature_branch", "library_ids", "文档分页")) or message.startswith("arguments."):
        return "schema"
    return "upstream"


def public_libraries(libraries, fixed_ids):
    return [{key: library[key] for key in ("id", "name", "type", "version", "enabled") if key in library}
            for library in libraries if library.get("id") in fixed_ids]


def scoped_navigation(value, fixed_ids):
    if isinstance(value, list):
        return [scoped_navigation(item, fixed_ids) for item in value]
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for key in ("parent", "children", "linked_topics", "related_sources"):
        items = result.get(key)
        if isinstance(items, dict) and items.get("library_id") and items["library_id"] not in fixed_ids:
            result.pop(key)
        elif isinstance(items, list):
            result[key] = [item for item in items if isinstance(item, dict)
                           and (not item.get("library_id") or item["library_id"] in fixed_ids)]
    return {key: scoped_navigation(item, fixed_ids) for key, item in result.items()}


class AccessDenied(Exception):
    pass


class NativeKnowledgeBridge:
    def __init__(self, url, *, library_ids=LIBRARY_IDS, ledger=None, upstream=None):
        self.upstream = upstream or WorkbenchBridge(url, timeout=HTTP_TIMEOUT_SECONDS)
        identities = tuple(library_ids)
        if (not identities or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,240}", item)
                                  for item in identities) or len(set(identities)) != len(identities)):
            raise ValueError("The native benchmark needs an explicit fixed ordinary-library allowlist")
        self.fixed_ids = frozenset(identities)
        self.ledger = Path(ledger) if ledger else None
        self.counter = 0
        self.audit_failed = False

    async def aclose(self):
        await self.upstream.aclose()

    async def access_scope(self):
        # These management responses are inspected locally; titles/tokens and
        # closed-library metadata never enter model output or the audit log.
        state = await self.upstream._json("GET", "/api/libraries/evaluation-sets")
        scope = await self.upstream._json("POST", "/api/tools/call", {"name": "get_library_scope", "arguments": {}})
        libraries = scope["result"]["libraries"]
        enabled = {library["id"] for library in libraries if library.get("enabled") is True}
        if state.get("retrieval_enabled") is not False or enabled != self.fixed_ids:
            raise AccessDenied()
        return public_libraries(libraries, self.fixed_ids)

    async def list_tools(self):
        definitions = await self.upstream.list_tools()
        result = []
        for tool in definitions:
            if tool.name not in TOOLS:
                continue
            schema = copy.deepcopy(tool.inputSchema)
            schema.get("properties", {}).pop("session_id", None)
            schema["required"] = [key for key in schema.get("required", []) if key != "session_id"]
            result.append(Tool(name=tool.name, description=tool.description, inputSchema=schema,
                               annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False)))
        if {tool.name for tool in result} != set(TOOLS):
            raise RuntimeError("The three ordinary knowledge tools are unavailable")
        return result

    def record(self, record):
        try:
            if self.ledger:
                self.ledger.parent.mkdir(parents=True, exist_ok=True)
                with self.ledger.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            return True
        except OSError:
            # Observation must never replace a tool result or stop the Agent.
            # Emit only a fixed marker; no path, exception text or payload.
            if not self.audit_failed:
                try:
                    print(AUDIT_WRITE_FAILURE_MARKER, file=sys.stderr, flush=True)
                except OSError:
                    pass
            self.audit_failed = True
            return False

    async def call_tool(self, name, arguments=None):
        self.counter += 1
        started = time.monotonic()
        record = {"index": self.counter, "tool": name if name in TOOLS else "unregistered",
                  "status": "error", "evidence_ids": []}
        if isinstance(arguments, dict):
            for key in ("query", "kind", "mode", "feature_branch", "type"):
                if isinstance(arguments.get(key), str):
                    record[key] = arguments[key][:320]
        try:
            if name not in TOOLS:
                raise AccessDenied()
            libraries = await self.access_scope()
            if not isinstance(arguments, dict):
                record["error_code"] = "invalid_arguments"
                record["error_kind"] = "schema"
                return tool_result({"error": "工具参数必须是JSON对象。"}, error=True)
            if "session_id" in arguments:
                raise AccessDenied()
            arguments = dict(arguments)
            if name == "search_knowledge":
                requested = arguments.get("library_ids", sorted(self.fixed_ids))
                if not isinstance(requested, list) or any(not isinstance(item, str) for item in requested):
                    record["error_code"] = "invalid_arguments"
                    record["error_kind"] = "schema"
                    return tool_result({"error": "library_ids必须为资料库ID数组。"}, error=True)
                if not set(requested) <= self.fixed_ids:
                    raise AccessDenied()
                arguments["library_ids"] = requested
            # Exact IDs and ordinary query/type filters go to the real tool.
            # No discovery prerequisite, fuzzy matching, substitute or retry.
            result = await self.upstream.call_tool(name, arguments)
            if result.isError:
                record["error_code"] = "workbench_tool_error"
                record["error_kind"] = tool_error_kind(result)
                return result
            payload = json.loads(result.content[0].text)
            await self.access_scope()
            if name == "get_library_scope":
                payload = {"libraries": public_libraries(payload.get("libraries", []), self.fixed_ids),
                           "enabled_library_ids": sorted(self.fixed_ids), "scope_fixed": True}
            elif name == "search_knowledge":
                hits = payload.get("results", [])
                if any(hit.get("library_id") not in self.fixed_ids for hit in hits):
                    raise AccessDenied()
                record["result_count"] = len(hits)
                record["library_ids"] = sorted({hit["library_id"] for hit in hits})
                record["source_summaries"] = [source_summary(hit) for hit in hits[:16]]
                record["omitted_source_summaries"] = max(0, len(hits) - 16)
            elif name == "read_source":
                if payload.get("library_id") not in self.fixed_ids:
                    raise AccessDenied()
                if isinstance(payload.get("id"), str) and isinstance(payload.get("text"), str) and payload["text"].strip():
                    if len(payload["id"]) <= 240:
                        record["evidence_ids"] = [payload["id"]]
                    else:
                        record["source_id_sha256"] = hashlib.sha256(payload["id"].encode("utf-8")).hexdigest()
                    record["library_ids"] = [payload["library_id"]]
                    record["source_summaries"] = [source_summary(payload)]
                    record["read_range"] = {key: payload[key] for key in ("offset", "next_offset", "total_characters")
                        if key in payload and (payload[key] is None or type(payload[key]) is int)}
                    if type(arguments.get("length")) is int:
                        record["read_range"]["requested_length"] = arguments["length"]
                    for key in ("kind", "type", "offset", "next_offset"):
                        value = payload.get(key)
                        if value is None or isinstance(value, (int, bool)):
                            record[key] = value
                        elif isinstance(value, str):
                            record[key] = value[:80]
            record["status"] = "complete"
            return tool_result(scoped_navigation(payload, self.fixed_ids))
        except AccessDenied:
            record["error_code"] = "access_denied"
            record["error_kind"] = "other"
            return tool_result({"error": "本次仅允许预先固定的普通资料库，且测试集检索须关闭；访问范围不符，未返回正文。"}, error=True)
        except Exception:
            record["error_code"] = "tool_service_error"
            record["error_kind"] = "other"
            return tool_result({"error": "知识工具暂未完成本次请求；请核对参数或服务状态后自行决定下一步。"}, error=True)
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            self.record(record)


def create_server(bridge):
    server = Server("benchmark-knowledge", version="1.0.0",
                    instructions="仅提供固定范围内三个普通只读知识工具。工具错误以isError返回，由Agent决定修正或结束；没有检索次数、先后顺序、引用或物理评分验收。")

    @server.list_tools()
    async def list_tools():
        return await bridge.list_tools()

    @server.call_tool()
    async def call_tool(name, arguments):
        return await bridge.call_tool(name, arguments)

    return server


async def serve(url, ledger, library_ids=LIBRARY_IDS):
    bridge = NativeKnowledgeBridge(url, ledger=ledger, library_ids=library_ids)
    try:
        server = create_server(bridge)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await bridge.aclose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--library-id", dest="library_ids", action="append")
    args = parser.parse_args(argv)
    anyio.run(serve, args.url, args.ledger, args.library_ids or LIBRARY_IDS)


if __name__ == "__main__":
    main()
