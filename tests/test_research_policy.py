from advisor import agent_sessions
from advisor.planning_service import tool_definitions
from advisor.research_policy import (COMMON_PLANNING_POLICY, COMMON_POLICY_MARKER,
    RESEARCH_POLICY, RESEARCH_POLICY_MARKER, SEARCH_KNOWLEDGE_DESCRIPTION, READ_SOURCE_DESCRIPTION)
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
