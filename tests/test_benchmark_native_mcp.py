import asyncio
import json

import pytest
from mcp.types import Tool

from scripts.benchmark_native_mcp import NativeKnowledgeBridge, LIBRARY_IDS, TOOLS
from workbench_mcp import tool_result


class FakeWorkbench:
    def __init__(self):
        self.enabled = set(LIBRARY_IDS)
        self.holdout = False
        self.calls = []
        self.outside = False

    async def aclose(self):
        pass

    async def _json(self, method, path, payload=None):
        if path.endswith("evaluation-sets"):
            return {"retrieval_enabled": self.holdout, "access_token": "MANAGEMENT_SECRET_CANARY", "title": "HELD_OUT_CANARY"}
        return {"result": {"libraries": [{"id": identifier, "name": identifier, "enabled": identifier in self.enabled}
            for identifier in LIBRARY_IDS] + [{"id": "private", "name": "HELD_OUT_CANARY", "enabled": False}]}}

    async def list_tools(self):
        return [Tool(name=name, description=name, inputSchema={"type": "object", "properties": {
            "session_id": {"type": "string"}}, "required": []}) for name in (*TOOLS, "save_plan", "set_libraries_enabled")]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "get_library_scope":
            payload = (await self._json("POST", "/api/tools/call"))["result"]
            if arguments.get("query"):
                payload["libraries"] = [library for library in payload["libraries"] if arguments["query"] in library["name"]]
            return tool_result(payload)
        if name == "read_source":
            if arguments.get("id") == "typo":
                return tool_result({"error": "资料不存在。"}, error=True)
            return tool_result({"id": arguments["id"], "library_id": "private" if self.outside else LIBRARY_IDS[0],
                "title": "Definition", "kind": "manual", "offset": 0, "next_offset": None, "total_characters": 20,
                "source_context": {"role": "general_manual", "raw": "SECRET_CONTEXT_CANARY"}, "text": "DOCUMENT_BODY_CANARY"})
        return tool_result({"results": [{"id": "source", "title": "Definition", "type": "manual_page", "library_id": LIBRARY_IDS[0], "text": "DOCUMENT_BODY_CANARY"}]})


def test_native_manifest_only_exposes_three_readonly_tools_without_sessions():
    async def run():
        bridge = NativeKnowledgeBridge("http://127.0.0.1:61679", upstream=FakeWorkbench())
        tools = await bridge.list_tools()
        assert {tool.name for tool in tools} == set(TOOLS)
        assert all("session_id" not in tool.inputSchema["properties"] for tool in tools)
        assert all(tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False for tool in tools)
    asyncio.run(run())


def test_direct_id_does_not_require_prior_search_and_typo_is_normal_mcp_error(tmp_path):
    async def run():
        upstream = FakeWorkbench()
        ledger = tmp_path / "ledger.jsonl"
        bridge = NativeKnowledgeBridge("http://127.0.0.1:61679", ledger=ledger, upstream=upstream)
        error = await bridge.call_tool("read_source", {"id": "typo", "offset": 0, "length": 500})
        assert error.isError is True
        result = await bridge.call_tool("read_source", {"id": "direct-source", "offset": 0, "length": 500})
        assert result.isError is False
        assert [arguments["id"] for _, arguments in upstream.calls] == ["typo", "direct-source"]
        calls = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert calls[0]["status"] == "error" and calls[0]["evidence_ids"] == []
        assert calls[1]["status"] == "complete" and calls[1]["evidence_ids"] == ["direct-source"]
        assert calls[1]["source_summaries"][0]["source_context"] == {"role": "general_manual"}
        assert calls[1]["read_range"]["requested_length"] == 500
        assert "DOCUMENT_BODY_CANARY" not in ledger.read_text(encoding="utf-8")
        assert "MANAGEMENT_SECRET_CANARY" not in ledger.read_text(encoding="utf-8")
        assert "SECRET_CONTEXT_CANARY" not in ledger.read_text(encoding="utf-8")
    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["holdout", "closed", "expanded", "outside_source"])
def test_access_errors_return_iserror_without_terminating_agent_or_exposing_body(tmp_path, boundary):
    async def run():
        upstream = FakeWorkbench()
        bridge = NativeKnowledgeBridge("http://127.0.0.1:61679", ledger=tmp_path / "ledger.jsonl", upstream=upstream)
        if boundary == "holdout":
            upstream.holdout = True
        elif boundary == "closed":
            upstream.enabled.remove(LIBRARY_IDS[0])
        elif boundary == "outside_source":
            upstream.outside = True
        arguments = {"query": "heat", "library_ids": ["private"]} if boundary == "expanded" else {"id": "direct-source"}
        name = "search_knowledge" if boundary == "expanded" else "read_source"
        result = await bridge.call_tool(name, arguments)
        assert result.isError is True
        assert "DOCUMENT_BODY_CANARY" not in result.content[0].text
        upstream.holdout, upstream.outside, upstream.enabled = False, False, set(LIBRARY_IDS)
        # The next call remains possible; access policy never ends the Agent.
        assert (await bridge.call_tool("read_source", {"id": "direct-source"})).isError is False
    asyncio.run(run())


def test_scope_hides_holdout_and_closed_library_metadata():
    async def run():
        bridge = NativeKnowledgeBridge("http://127.0.0.1:61679", upstream=FakeWorkbench())
        result = await bridge.call_tool("get_library_scope", {})
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        assert {library["id"] for library in payload["libraries"]} == set(LIBRARY_IDS)
        assert "HELD_OUT_CANARY" not in result.content[0].text
        assert "MANAGEMENT_SECRET_CANARY" not in result.content[0].text
        filtered = await bridge.call_tool("get_library_scope", {"query": "acdc"})
        assert [library["id"] for library in json.loads(filtered.content[0].text)["libraries"]] == ["cs63-models-acdc-module"]
    asyncio.run(run())


def test_extended_fixed_scope_still_blocks_holdout_and_unlisted_libraries():
    async def run():
        allowed = (LIBRARY_IDS[0], "cs63-models-wave-optics-module")

        class ExtendedWorkbench(FakeWorkbench):
            async def _json(self, method, path, payload=None):
                value = await super()._json(method, path, payload)
                if not path.endswith("evaluation-sets"):
                    value["result"]["libraries"].append({"id": allowed[1], "name": "Optics", "enabled": True})
                return value

        upstream = ExtendedWorkbench()
        upstream.enabled = {allowed[0]}
        bridge = NativeKnowledgeBridge("http://127.0.0.1:61679", library_ids=allowed, upstream=upstream)
        result = await bridge.call_tool("search_knowledge", {"query": "dipole"})
        assert result.isError is False
        assert upstream.calls[-1][1]["library_ids"] == sorted(allowed)
        denied = await bridge.call_tool("search_knowledge", {"query": "dipole", "library_ids": [LIBRARY_IDS[1]]})
        assert denied.isError is True
        upstream.holdout = True
        assert (await bridge.call_tool("read_source", {"id": "direct-source"})).isError is True
    asyncio.run(run())


def test_search_ledger_has_bounded_queries_and_source_metadata_without_body(tmp_path):
    async def run():
        ledger = tmp_path / "ledger.jsonl"
        bridge = NativeKnowledgeBridge("http://127.0.0.1:61679", ledger=ledger, upstream=FakeWorkbench())
        await bridge.call_tool("search_knowledge", {"query": "q" * 1000, "kind": "manual", "mode": "qmd_keyword", "feature_branch": "physics"})
        record = json.loads(ledger.read_text(encoding="utf-8"))
        assert len(record["query"]) == 320 and record["kind"] == "manual" and record["feature_branch"] == "physics"
        assert record["mode"] == "qmd_keyword"
        assert record["source_summaries"][0]["title"] == "Definition"
        assert "DOCUMENT_BODY_CANARY" not in ledger.read_text(encoding="utf-8")
    asyncio.run(run())


def test_audit_write_failure_does_not_override_tool_success_or_repeat_marker(tmp_path, monkeypatch, capsys):
    async def run():
        bridge = NativeKnowledgeBridge("http://127.0.0.1:61679", ledger=tmp_path / "ledger.jsonl", upstream=FakeWorkbench())
        from pathlib import Path

        def failed_open(*args, **kwargs):
            raise OSError("PRIVATE_PATH_ERROR_CANARY")

        monkeypatch.setattr(Path, "open", failed_open)
        for _ in range(2):
            result = await bridge.call_tool("read_source", {"id": "direct-source"})
            assert result.isError is False
            assert "DOCUMENT_BODY_CANARY" in result.content[0].text
        assert bridge.audit_failed is True

    asyncio.run(run())
    stderr = capsys.readouterr().err
    assert stderr.count("BENCHMARK_KNOWLEDGE_AUDIT_WRITE_FAILED") == 1
    assert "PRIVATE_PATH_ERROR_CANARY" not in stderr and "DOCUMENT_BODY_CANARY" not in stderr


def test_only_native_bridge_uses_the_queue_compatible_http_timeout(monkeypatch):
    import inspect
    import workbench_mcp
    from scripts import benchmark_native_mcp as native
    captured = {}

    def factory(url, **kwargs):
        captured.update(kwargs)
        return FakeWorkbench()

    monkeypatch.setattr(native, "WorkbenchBridge", factory)
    native.NativeKnowledgeBridge("http://127.0.0.1:61679")
    assert captured["timeout"] == 240
    assert inspect.signature(workbench_mcp.WorkbenchBridge).parameters["timeout"].default == 60


@pytest.mark.parametrize("message, expected", [
    ("本机工作台请求超时；操作结果尚未确认，请读取当前版本后再重试。", "timeout"),
    ("工具参数字段与公开schema不符；PRIVATE_ERROR_CANARY", "schema"),
    ("arguments.kind不是允许的取值；PRIVATE_ERROR_CANARY", "schema"),
    ("PRIVATE_ERROR_CANARY", "upstream"),
])
def test_error_ledger_records_fixed_kind_without_original_message(tmp_path, message, expected):
    async def run():
        class ErrorWorkbench(FakeWorkbench):
            async def call_tool(self, name, arguments):
                return tool_result({"error": message}, error=True)

        ledger = tmp_path / "ledger.jsonl"
        bridge = NativeKnowledgeBridge("http://127.0.0.1:61679", ledger=ledger, upstream=ErrorWorkbench())
        response = await bridge.call_tool("search_knowledge", {"query": "Darcy", "kind": "manual", "mode": "native"})
        assert response.isError is True
        record = json.loads(ledger.read_text(encoding="utf-8"))
        assert record["error_kind"] == expected and record["mode"] == "native"
        assert record["evidence_ids"] == []
        assert "PRIVATE_ERROR_CANARY" not in ledger.read_text(encoding="utf-8")
    asyncio.run(run())
