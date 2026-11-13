import pytest

from advisor import agent_sessions
from advisor.planning_service import tool_definitions
from advisor.research_policy import (COMMON_PLANNING_POLICY, COMMON_POLICY_MARKER,
    RESEARCH_POLICY, RESEARCH_POLICY_MARKER, SEARCH_KNOWLEDGE_DESCRIPTION,
    READ_SOURCE_DESCRIPTION, load_planner_instructions)
from scripts import benchmark_cli, benchmark_codex_cli, benchmark_no_library_cli


def test_product_and_benchmark_transports_share_exact_task_independent_policies():
    for instructions in (agent_sessions.INSTRUCTIONS, benchmark_cli.API_INSTRUCTIONS,
                         benchmark_codex_cli.INSTRUCTIONS, benchmark_no_library_cli.INSTRUCTIONS):
        assert instructions.count(COMMON_PLANNING_POLICY) == 1
        assert COMMON_POLICY_MARKER not in instructions
        assert RESEARCH_POLICY_MARKER not in instructions
    for instructions in (agent_sessions.INSTRUCTIONS, benchmark_cli.API_INSTRUCTIONS,
                         benchmark_codex_cli.INSTRUCTIONS):
        assert instructions.count(RESEARCH_POLICY) == 1
    assert RESEARCH_POLICY not in benchmark_no_library_cli.INSTRUCTIONS
    assert "search_knowledge" not in benchmark_no_library_cli.INSTRUCTIONS
    assert "read_source" not in benchmark_no_library_cli.INSTRUCTIONS
    assert "通常约 3 次" not in benchmark_codex_cli.INSTRUCTIONS


def test_template_renders_shared_policy_without_changing_surrounding_instructions(tmp_path):
    template = tmp_path / "planner.txt"
    template.write_text("任务边界 {literal}\n" + COMMON_POLICY_MARKER + "\n研究\n" +
                        RESEARCH_POLICY_MARKER + "\n结束", encoding="utf-8")
    assert load_planner_instructions(template) == (
        "任务边界 {literal}\n" + COMMON_PLANNING_POLICY + "\n研究\n" + RESEARCH_POLICY + "\n结束")


@pytest.mark.parametrize("marker", [COMMON_POLICY_MARKER, RESEARCH_POLICY_MARKER])
@pytest.mark.parametrize("occurrences", [0, 2])
def test_template_rejects_missing_or_duplicate_shared_policy(tmp_path, marker, occurrences):
    other = RESEARCH_POLICY_MARKER if marker == COMMON_POLICY_MARKER else COMMON_POLICY_MARKER
    template = tmp_path / "planner.txt"
    template.write_text("\n".join([other] + [marker] * occurrences), encoding="utf-8")
    with pytest.raises(ValueError, match="each shared policy marker exactly once"):
        load_planner_instructions(template)


def test_staged_policy_fields_and_discovery_tool_are_exposed_by_product_contract():
    tools = {tool["name"]: tool for tool in tool_definitions()}
    guidance = tools["get_mechanism_checks"]["inputSchema"]
    assert guidance["required"] == []  # Discovery does not need a plan or selected profile.
    assert guidance["properties"]["mechanisms"]["items"]["type"] == "string"
    for name in ("get_mechanism_checks", "save_plan_settings", "get_plan_settings", "review_plan"):
        assert name in COMMON_PLANNING_POLICY
        assert name in tools
    assert all(field in COMMON_PLANNING_POLICY for field in ("check_ids", "implementation", "step_ids",
                                                            "proposed_value"))
    for name, field in (("save_plan_settings", "settings"), ("save_plan", "plan_settings"),
                        ("review_plan", "plan_settings")):
        row = tools[name]["inputSchema"]["properties"][field]["items"]
        assert {"check_ids", "implementation"}.isdisjoint(row["required"])
        assert row["properties"]["check_ids"]["items"]["type"] == "string"
        binding = row["properties"]["implementation"]
        assert set(binding["properties"]) == {"step_id", "field"}
        assert set(binding["required"]) == {"step_id", "field"}
        assert set(binding["properties"]["field"]["enum"]) == {"action", "inputs", "checks"}


def test_product_and_benchmark_tools_explain_source_context_and_navigation():
    product = {tool["name"]: tool["description"] for tool in tool_definitions()}
    benchmark = {tool["function"]["name"]: tool["function"]["description"]
                 for tool in benchmark_cli.weak_tools()}
    for name, description in (("search_knowledge", SEARCH_KNOWLEDGE_DESCRIPTION),
                              ("read_source", READ_SOURCE_DESCRIPTION)):
        assert product[name].startswith(description)
        assert benchmark[name] == description


def test_saved_evidence_summaries_bound_context_and_exclude_unrelated_payloads():
    source = {"id": "known", "kind": "case", "library_id": "enabled", "text": "x" * 4000,
              "source_context": {"role": "saved_configuration", "case_identity": "a" * 1000,
                                 "physics": ["solid"] * 50, "secret": "MUST_NOT_PERSIST"},
              "access_token": "MUST_NOT_PERSIST", "raw": {"api_key": "MUST_NOT_PERSIST"}}
    result = benchmark_cli.source_ledger_summary(source)
    assert len(result["content_excerpt"]) == 500
    assert len(result["source_context"]["case_identity"]) == 240
    assert len(result["source_context"]["physics"]) == 8
    assert "MUST_NOT_PERSIST" not in str(result)
