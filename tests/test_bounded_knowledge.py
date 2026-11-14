"""Synthetic delivery contracts; no library, model, or external service reads."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

import pytest

from advisor.bounded_knowledge import BoundedKnowledge
from advisor.case_methods import CaseMethodBuilder


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def source_reader(text, *, extra=None):
    calls = []

    def read(name, args):
        calls.append((name, deepcopy(args)))
        assert name == "read_source"
        offset, length = args.get("offset", 0), args["length"]
        body = text[offset:offset + length]
        return {"id": "source-one", "title": "Exact source", "type": "manual_topic", "library_id": "library-one",
                "source_context": {"role": "case_tutorial", "applicability": "not_verified"},
                "text": body, "offset": offset, "length": length, "returned_characters": len(body),
                "total_characters": len(text), "next_offset": offset + len(body) if offset + len(body) < len(text) else None,
                "read_scope": "extracted_text", "text_truncated": True,
                "evidence_refs": [{"source_id": "source-one", "context": "source_text", "locator": f"text:{offset}:{offset + len(body)}"}],
                **(extra or {})}

    return read, calls


def configuration(properties):
    return {"case_id": "case-one", "source": "xml", "partial": False, "returned": 1,
            "source_context": {"role": "saved_configuration"}, "read_scope": "saved_configuration",
            "nodes": [{"id": 4, "path": "compact:node/4", "label": "Native node", "type": "physics",
                       "properties": [dict(prop, evidence_locator="saved:compact:node/4::property/" + str(index))
                                      for index, prop in enumerate(properties)]}],
            "evidence_refs": [{"source_id": "case-one", "context": "case_configuration", "locator": "saved:compact:node/4"}]}


def reconstruct(adapter, handle, path=""):
    pieces, offset = [], 0
    while True:
        page = adapter.call("read_tool_page", {"handle": handle, "path": path, "offset": offset})
        assert size(page) <= adapter.max_bytes
        assert page["delivered_evidence_refs"] == []
        assert page["source_range"] == {"json_pointer": path, "start": offset, "end": offset + len(page["text"])}
        pieces.append(page["text"])
        if page["next_offset"] is None:
            return json.loads("".join(pieces))
        assert page["next_offset"] > offset
        offset = page["next_offset"]


def test_case_reading_handoff_survives_bounded_source_projection():
    actions = {"original": {"tool": "inspect_case_source", "arguments": {"case_id": "case-one", "limit": 4}}}
    reader, _ = source_reader("Exact body " * 200, extra={"type": "model_case", "case_reading": actions})
    adapter = BoundedKnowledge(reader)
    result = adapter.call("read_source", {"id": "case-one", "length": 1000})
    assert result["case_reading"] == actions
    assert result["text"] == ("Exact body " * 200)[:1000]


def test_long_source_pages_are_complete_original_text_and_truthful_refs():
    text = ("中文🙂 and quoted \"text\"\\ " * 700) + "tail"
    reader, calls = source_reader(text, extra={"data": {"huge": "native metadata" * 2000}})
    adapter = BoundedKnowledge(reader)
    parts, offset = [], 0
    while True:
        page = adapter.call("read_source", {"id": "source-one", "offset": offset, "length": 16000})
        assert calls[-1][1]["length"] == 1200
        assert size(page) <= 7000
        assert page["read_scope"] == "extracted_text" and page["text_truncated"] is True
        assert page["evidence_refs"] == [{"source_id": "source-one", "context": "source_text",
                                          "locator": f"text:{offset}:{offset + len(page['text'])}"}]
        assert page["metadata_view"]["kind"] == "projection"
        parts.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(parts) == text
    assert all(item["source_text_delivery_complete"] is True for item in adapter.audit)
    assert all(item["source_text_truncated"] is True for item in adapter.audit)
    assert reconstruct(adapter, page["metadata_view"]["original"]["arguments"]["handle"], "/data") == {"huge": "native metadata" * 2000}


def test_small_results_keep_semantics_and_json_strings_are_decoded():
    value = {"goal": "Keep exact model facts", "model": {"domains": [1, 2], "has_contact": False}, "state": "ready"}
    adapter = BoundedKnowledge(lambda name, args: json.dumps(value))
    assert adapter.call("get_task_context", {}) == value
    assert adapter.call("other_read", {}) == value
    assert adapter.audit[-1]["delivery_partial"] is False


@pytest.mark.parametrize("bad", ["not-json", "null", "1", None, 9, {"invalid": float("nan")}])
def test_invalid_tool_results_fail_closed(bad):
    adapter = BoundedKnowledge(lambda name, args: bad)
    with pytest.raises(ValueError):
        adapter.call("read", {})
    assert adapter.audit == []


def test_array_result_uses_an_explicit_lossless_envelope():
    adapter = BoundedKnowledge(lambda name, args: '[1,{"exact":false}]')
    assert adapter.call("read", {}) == {"result": [1, {"exact": False}], "result_type": "array"}


def test_source_never_grants_unrelated_or_undisplayed_refs():
    read, _ = source_reader("exact source text")

    def read_with_extra_refs(name, args):
        raw = read(name, args)
        raw["evidence_refs"].append({"source_id": "source-one", "context": "source_text", "locator": "text:0:99999"})
        return raw

    adapter = BoundedKnowledge(read_with_extra_refs)
    page = adapter.call("read_source", {"id": "source-one"})
    assert page["evidence_refs"] == [{"source_id": "source-one", "context": "source_text", "locator": "text:0:17"}]
    assert "evidence_refs (outside displayed text range)" in page["metadata_view"]["omitted_fields"]


def test_constructor_contract_does_not_receive_unpublished_request_fields():
    def read(name, args):
        assert name == "get_tool_contract"
        assert args == {"tool": "submit_route_card", "path": "/card"}
        return {"schema": {"properties": {"large": {"description": "x" * 9000}}}}

    adapter = BoundedKnowledge(read)
    page = adapter.call("get_tool_contract", {"tool": "submit_route_card", "path": "/card"})
    assert page["format"] == "serialized_json" and page["next_offset"] is not None


def test_configuration_pages_grant_only_complete_visible_properties():
    raw = configuration([{"id": index, "name": f"p{index}", "value": "native value " * 20} for index in range(18)])
    raw.update(physical_validation="not_evaluated", effective_activation="not_verified")
    seen_calls, delivered = [], []
    adapter = BoundedKnowledge(lambda name, args: seen_calls.append(args) or deepcopy(raw),
                               observer=lambda name, args, page: delivered.append((name, args, page)))
    page = adapter.call("read_case_configuration", {"case_id": "case-one", "node_path": "compact:node/4"})
    assert seen_calls == [{"case_id": "case-one", "node_path": "compact:node/4", "limit": 1}]
    collected = []
    while True:
        assert page["physical_validation"] == "not_evaluated"
        assert page["effective_activation"] == "not_verified"
        props = page["nodes"][0]["properties"]
        refs = page["delivered_evidence_refs"]
        assert {ref["locator"] for ref in refs} == {"saved:compact:node/4"} | {prop["evidence_locator"] for prop in props}
        ledger = CaseMethodBuilder._returned_references("read_case_configuration", "case-one", {"source": "saved"}, page)
        assert {key[2] for key in ledger} == {ref["locator"] for ref in refs}
        collected.extend(props)
        assert size(page) <= 7000
        if page["next_offset"] is None:
            break
        assert page["offset_unit"] == "property_index"
        page = adapter.call("read_tool_page", page["next_page"]["arguments"])
    assert collected == raw["nodes"][0]["properties"]
    assert len(seen_calls) == 1
    assert all(item[0] == "read_case_configuration" for item in delivered)
    assert adapter.audit[-1]["delivered_evidence_refs"] == page["delivered_evidence_refs"]


def test_huge_single_property_is_recoverable_with_no_whole_property_credit():
    raw = configuration([{"id": 0, "name": "huge", "value": "原值🙂\\\"" * 6000}, {"id": 1, "name": "small", "value": 17}])
    adapter = BoundedKnowledge(lambda name, args: deepcopy(raw))
    first = adapter.call("read_case_configuration", {"case_id": "case-one"})
    assert first["nodes"][0]["properties"] == []
    assert first["next_offset"] == 1
    assert first["delivery_partial"] is True
    assert [ref["locator"] for ref in first["delivered_evidence_refs"]] == ["saved:compact:node/4"]
    deferred = first["deferred_properties"][0]["original"]["arguments"]
    assert reconstruct(adapter, deferred["handle"], deferred["path"]) == raw["nodes"][0]["properties"][0]
    assert all(not record["delivered_evidence_refs"] for record in adapter.audit[1:])
    second = adapter.call("read_tool_page", first["next_page"]["arguments"])
    assert second["nodes"][0]["properties"] == [raw["nodes"][0]["properties"][1]]
    assert second["next_offset"] is None


def test_cache_pages_reauthorize_and_do_not_repeat_real_service_queries():
    state = {"allowed": True, "calls": 0}

    def read(name, args):
        state["calls"] += 1
        return {"large": "x" * 10000}

    def authorize(name, args):
        assert name == "read" and args == {"id": "exact"}
        if not state["allowed"]:
            raise PermissionError("closed source")

    adapter = BoundedKnowledge(read, authorize=authorize)
    first = adapter.call("read", {"id": "exact"})
    state["allowed"] = False
    with pytest.raises(PermissionError, match="closed source"):
        adapter.call("read_tool_page", {"handle": first["handle"]})
    assert state["calls"] == 1 and len(adapter.audit) == 1


def test_concurrent_calls_keep_distinct_cached_results_and_page_handles():
    participants = 12
    ready = Barrier(participants)

    def read(name, args):
        ready.wait(timeout=10)
        return {"request_id": args["request_id"], "payload": str(args["request_id"]) * 10000}

    adapter = BoundedKnowledge(read)

    def request(request_id):
        return request_id, adapter.call("read", {"request_id": request_id})["handle"]

    with ThreadPoolExecutor(max_workers=participants) as workers:
        results = list(workers.map(request, range(participants * 2)))
    handles = [handle for _, handle in results]
    assert len(set(handles)) == participants * 2
    assert {int(handle.removeprefix("tool-page-")) for handle in handles} == set(range(1, participants * 2 + 1))
    for request_id, handle in results:
        assert reconstruct(adapter, handle) == {"request_id": request_id, "payload": str(request_id) * 10000}


def test_search_projection_marks_previews_and_preserves_full_candidate_access():
    raw = {"mode": "existing", "results": [{"id": f"source-{index}", "title": "Real title", "type": "model_case",
             "library_id": "library-one", "excerpt": "original excerpt " * 250,
             "data": {"important": "full metadata" * 200}} for index in range(9)]}
    adapter = BoundedKnowledge(lambda name, args: deepcopy(raw))
    page = adapter.call("search_cases", {"query": "synthetic"})
    assert 3 <= len(page["results"]) <= 6 and page["has_more"] is True
    assert page["results"][0]["summary_view"]["is_source_text"] is False
    assert page["results"][0]["detail"]["arguments"] == {"id": "source-0"}
    handle = page["metadata_view"]["original"]["arguments"]["handle"]
    assert reconstruct(adapter, handle, "/results/0") == raw["results"][0]


def test_large_context_and_recursive_contract_are_paged_without_erasing_facts():
    raw = {"goal": "precise user goal" * 900, "model": {"contacts": ["physical fact"] * 2500},
           "schema": {"properties": {f"field{i}": {"enum": [str(i), "full"]} for i in range(80)}}}
    args_seen = []
    adapter = BoundedKnowledge(lambda name, args: args_seen.append(args) or deepcopy(raw))
    first = adapter.call("get_task_context", {})
    assert reconstruct(adapter, first["handle"]) == raw
    contract = adapter.call("get_tool_contract", {"tool": "synthetic", "length": 16000})
    assert args_seen[-1]["length"] == 1200
    assert reconstruct(adapter, contract["handle"], "/schema") == raw["schema"]


def test_guide_stage_heads_keep_original_details_accessible():
    raw = {"case_id": "case-one", "state": "ready", "is_read_evidence": False,
           "stages": [{"id": "physics", "title": "Physics", "observed": [{"native": "x" * 9000}]}]}
    adapter = BoundedKnowledge(lambda name, args: raw)
    head = adapter.call("get_case_guide", {"case_id": "case-one"})
    assert head["stages"][0]["id"] == "physics" and "observed" not in head["stages"][0]
    link = head["stages"][0]["detail"]["arguments"]
    assert reconstruct(adapter, link["handle"], link["path"]) == raw["stages"][0]


def test_byte_budget_accounts_for_unicode_and_runtime_json_spaces():
    raw = {"nested": {"a/b": {"~key": "🙂中文\\\"" * 5000}}}
    adapter = BoundedKnowledge(lambda name, args: raw, max_bytes=1500)
    first = adapter.call("read", {})
    assert size(first) <= 1500
    assert reconstruct(adapter, first["handle"], "/nested/a~1b/~0key") == raw["nested"]["a/b"]["~key"]
    assert all(record["bytes"] <= 1500 for record in adapter.audit)


@pytest.mark.parametrize("args", [{"handle": "no-such-handle"}, {"handle": "tool-page-1", "offset": -1},
    {"handle": "tool-page-1", "length": 1201}, {"handle": "tool-page-1", "path": "/missing"},
    {"handle": "tool-page-1", "path": "/a~2b"}])
def test_page_arguments_are_checked(args):
    adapter = BoundedKnowledge(lambda name, args: {"a": "x" * 10000})
    adapter.call("read", {})
    with pytest.raises(ValueError):
        adapter.call("read_tool_page", args)
