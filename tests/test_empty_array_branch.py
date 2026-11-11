from copy import deepcopy

import pytest

from advisor.case_metadata import FEATURE_BRANCHES
from advisor.planning_service import planning_tools
from scripts import benchmark_cli
from scripts import benchmark_codex_cli as cli
from test_benchmark_codex_cli import tool_action, final_action, scripted_case


@pytest.mark.parametrize("kind, field, flag", [
    ("tools", "cited_source_ids", "cited_source_ids_empty_array_to_null"),
    ("final", "tool_requests", "tool_requests_empty_array_to_null"),
])
def test_only_inactive_empty_arrays_are_normalized_without_mutating_actions(kind, field, flag):
    action = tool_action("read_source", id="evidence-1", offset=0, length=12000) if kind == "tools" else final_action()
    action[field] = []
    original = deepcopy(action)
    flags = []
    calls = cli.validate_action(action, normalization_flags=flags)
    assert (calls is None) == (kind == "final")
    assert flags == [flag]
    assert action == original


@pytest.mark.parametrize("action, detail", [
    ({**tool_action("get_library_scope"), "plan_markdown": ""}, "tool_final_fields"),
    ({**tool_action("get_library_scope"), "cited_source_ids": ["evidence-1"]}, "tool_final_fields"),
    ({**final_action(), "tool_requests": [{"name": "read_source"}]}, "final_tool_requests"),
    ({**tool_action("get_library_scope"), "tool_requests": []}, "tool_request_count"),
    ({**final_action(), "cited_source_ids": []}, "final_citations_empty"),
])
def test_nonempty_inactive_fields_drafts_and_empty_active_arrays_still_fail(action, detail):
    with pytest.raises(cli.PlanningValidationError) as error:
        cli.validate_action(action)
    assert error.value.detail_code == detail


def test_normalizations_are_logged_without_extra_rounds_reads_or_feedback(tmp_path, monkeypatch):
    read = tool_action("read_source", id="evidence-2", offset=0, length=12000)
    read["cited_source_ids"] = []
    final = final_action("evidence-2")
    final["tool_requests"] = []
    args, prompts, run = scripted_case(tmp_path, monkeypatch, [read, final])
    report = run()
    assert report["status"] == "complete" and report["model_call_count"] == 4
    assert report["action_normalizations"] == [
        {"round": 3, "flags": ["cited_source_ids_empty_array_to_null"]},
        {"round": 4, "flags": ["tool_requests_empty_array_to_null"]},
    ]
    assert report["knowledge_call_counts"] == {"search_knowledge": 1, "read_source": 2}
    assert report["citation_repair_count"] == 0 and report["rejected_candidates"] == []
    assert (args.destination / "plan.md").read_text(encoding="utf-8").strip() == final["plan_markdown"]


def test_all_branch_schemas_share_the_product_categories_and_reject_case_as_a_branch():
    action_enum = cli.ARGUMENT_PROPERTIES["feature_branch"]["enum"]
    tools = {tool["function"]["name"]: tool["function"]["parameters"] for tool in benchmark_cli.weak_tools()}
    product = {tool["name"]: tool["inputSchema"] for tool in planning_tools()}
    assert action_enum == [*FEATURE_BRANCHES, None]
    assert tools["search_knowledge"]["properties"]["feature_branch"]["enum"] == list(FEATURE_BRANCHES)
    assert product["search_knowledge"]["properties"]["feature_branch"]["enum"] == list(FEATURE_BRANCHES)
    assert "case" not in action_enum
    action = tool_action("search_knowledge", query="thermal expansion", kind="case", mode="native", feature_branch="case")
    with pytest.raises(cli.PlanningValidationError) as error:
        cli.validate_action(action)
    assert error.value.detail_code == "feature_branch_value"
    action["tool_requests"][0]["arguments"]["feature_branch"] = "physics"
    assert cli.validate_action(action)[0]["arguments"]["feature_branch"] == "physics"
