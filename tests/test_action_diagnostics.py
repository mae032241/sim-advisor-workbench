import json

import pytest

from scripts import benchmark_codex_cli as cli
from test_benchmark_codex_cli import tool_action, final_action, scripted_case


@pytest.mark.parametrize("change, detail", [
    (lambda action: None, "root_type"),
    (lambda action: {**action, "UNTRUSTED_FIELD_CANARY": "RAW_VALUE_CANARY"}, "root_fields"),
    (lambda action: {**action, "tool_requests": None}, "tool_requests_type"),
    (lambda action: {**action, "tool_requests": []}, "tool_request_count"),
    (lambda action: {**action, "plan_markdown": "MODEL_TEXT_CANARY"}, "tool_final_fields"),
    (lambda action: {**action, "tool_requests": [{"arguments": {}}]}, "tool_call_fields"),
    (lambda action: {**action, "tool_requests": [{"name": "TOOL_NAME_CANARY", "arguments": {}}]}, "tool_name"),
    (lambda action: {**action, "tool_requests": [{"name": "read_source", "arguments": "ARGUMENT_TEXT_CANARY"}]}, "tool_arguments_type"),
    (lambda action: {**action, "tool_requests": [{"name": "read_source", "arguments": {}}]}, "tool_arguments_fields"),
    (lambda action: tool_action("read_source", id="ID_CANARY", query="QUERY_CANARY"), "inactive_tool_arguments"),
    (lambda action: {**final_action(), "tool_requests": [{}]}, "final_tool_requests"),
    (lambda action: {**final_action(), "plan_markdown": None}, "final_plan_type"),
    (lambda action: {**final_action(), "plan_markdown": " "}, "final_plan_empty"),
    (lambda action: {**final_action(), "cited_source_ids": None}, "final_citations_type"),
    (lambda action: {**final_action(), "cited_source_ids": []}, "final_citations_empty"),
    (lambda action: {**final_action(), "cited_source_ids": [""]}, "final_citation_id"),
    (lambda action: {**action, "action": "ACTION_VALUE_CANARY"}, "action_value"),
])
def test_invalid_actions_have_fixed_detail_codes_and_safe_shapes(change, detail):
    action = change(tool_action("read_source", id="ID_CANARY", offset=0, length=12000))
    with pytest.raises(cli.PlanningValidationError) as error:
        cli.validate_action(action)
    assert error.value.code == "invalid_action"
    assert error.value.detail_code == detail
    summary = json.dumps(cli.action_shape_summary(action))
    for canary in ("UNTRUSTED_FIELD_CANARY", "RAW_VALUE_CANARY", "MODEL_TEXT_CANARY", "TOOL_NAME_CANARY",
                   "ARGUMENT_TEXT_CANARY", "ID_CANARY", "QUERY_CANARY", "ACTION_VALUE_CANARY"):
        assert canary not in summary


def test_invalid_tool_action_persists_only_shape_and_fixed_validator_detail(tmp_path, monkeypatch):
    bad = tool_action("read_source", id="evidence-2", offset=0, length=12000, query="MODEL_QUERY_CANARY")
    args, prompts, run = scripted_case(tmp_path, monkeypatch, [bad])
    with pytest.raises(cli.PlanningValidationError):
        run()
    report = cli.core.load_json(args.destination / "run.json")
    failure = report["action_validation_failure"]
    assert failure["round"] == 3 and failure["code"] == "invalid_action"
    assert failure["detail_code"] == "inactive_tool_arguments"
    shape = failure["shape"]["tool_request_shapes"][0]
    assert shape["name"] == "read_source"
    assert shape["nonnull_argument_fields"] == ["query", "id", "offset", "length"]
    assert shape["argument_types"]["query"] == "string"
    assert "MODEL_QUERY_CANARY" not in json.dumps(report)
    assert report["citation_repair_count"] == 0 and report["model_call_count"] == 3
    assert not (args.destination / "plan.md").exists()


def test_safe_shape_is_bounded_and_never_saves_unknown_key_or_nested_text():
    action = tool_action("read_source", id="ID_CANARY", offset=0, length=12000)
    action["tool_requests"] *= 80
    action["UNKNOWN_CANARY"] = {"text": "RAW_CANARY"}
    action["tool_requests"][0]["arguments"]["UNKNOWN_ARGUMENT_CANARY"] = "RAW_CANARY"
    summary = cli.action_shape_summary(action)
    assert summary["tool_request_count"] == 80
    assert len(summary["tool_request_shapes"]) == cli.MAX_BATCH_CALLS
    assert summary["unknown_field_count"] == 1
    assert summary["tool_request_shapes"][0]["unknown_argument_count"] == 1
    saved = json.dumps(summary)
    assert "CANARY" not in saved and len(saved) < 6000


def test_explicit_controller_semantics_keep_the_existing_schema_and_research_policy():
    assert "仅 id/offset/length 可非 null" in cli.INSTRUCTIONS
    assert "query/kind/mode/library_ids/feature_branch 必须为 null" in cli.INSTRUCTIONS
    assert cli.INSTRUCTIONS.count(cli.COMMON_PLANNING_POLICY) == 1
    assert cli.INSTRUCTIONS.count(cli.RESEARCH_POLICY) == 1
    assert (cli.MAX_SEARCH_CALLS, cli.MAX_READ_CALLS, cli.MAX_BATCH_CALLS) == (12, 8, 6)


def test_citation_feedback_bounds_discovered_ids_and_does_not_echo_unknown_values():
    known = {f"evidence-{number:03}" for number in range(60)}
    long_id = "known-" + "x" * 400
    unknown_id = "UNKNOWN_MODEL_TEXT_CANARY" * 30
    declared = sorted(known) + [long_id, unknown_id]
    known.add(long_id)
    action = {"cited_source_ids": declared, "plan_markdown": " ".join(sorted(known)[:4])}
    read = {"evidence-000"}
    _, diagnostics = cli.final_citation_diagnostics(action, read, known)
    details = cli.citation_feedback_details(action, diagnostics, read, known)
    for key in ("unread_declared_citation_ids", "declared_citation_ids_missing_from_text",
                "unread_source_ids_in_text", "unread_but_discovered_source_ids", "declared_citation_ids"):
        assert len(details[key]) <= cli.CITATION_FEEDBACK_ID_LIMIT
        assert all(sid in known and len(sid) <= cli.CITATION_FEEDBACK_ID_MAX_CHARACTERS for sid in details[key])
    assert "evidence-000" not in details["unread_but_discovered_source_ids"]
    assert details["unrecognized_declared_citation_count"] == 1
    assert details["omitted_citation_diagnostic_counts"]["unread_declared_citation_ids"] > 0
    saved = json.dumps(details)
    assert unknown_id not in saved and long_id not in saved


def test_feedback_reports_unread_inline_ids_without_automatic_source_reads(tmp_path, monkeypatch):
    bad = final_action("evidence-1")
    bad["plan_markdown"] += " Additional unsupported citation [evidence-2]."
    args, prompts, run = scripted_case(tmp_path, monkeypatch, [bad, final_action("evidence-1")])
    report = run()
    feedback = prompts[-1]["citation_repair"]
    assert feedback["unread_declared_citation_ids"] == []
    assert feedback["declared_citation_ids_missing_from_text"] == []
    assert feedback["unread_source_ids_in_text"] == ["evidence-2"]
    assert feedback["unread_but_discovered_source_ids"] == ["evidence-2"]
    assert report["knowledge_call_counts"] == {"search_knowledge": 1, "read_source": 1}
    assert report["read_source_ids"] == ["evidence-1"]
    assert report["citation_repair_count"] == 1 and report["model_call_count"] == 4
