"""Branch labels are an explicit hard filter, including when no source matches."""
import copy
from unittest.mock import Mock, call

import pytest

from advisor.case_metadata import FEATURE_BRANCHES
from advisor.planning_service import PlanningService, tool_definitions


@pytest.mark.parametrize("branch", [None, *FEATURE_BRANCHES])
def test_branch_scope_hint_preserves_filters_without_extra_provider_calls(tmp_path, branch):
    kb = Mock()
    kb.search.return_value = []
    tools = PlanningService(tmp_path, None, kb)
    arguments = {"query": "interface definition", "kind": "manual", "mode": "native", "library_ids": ["enabled-only"]}
    filters = {"mode": "native", "library_ids": ["enabled-only"]}
    if branch is not None:
        arguments["feature_branch"] = branch
        filters["feature_branch"] = branch
    response = tools.call_public("search_knowledge", arguments)
    scope = response["search_scope"]
    assert scope["feature_branch"] == branch
    assert scope["unlabeled_sources_excluded"] is (branch is not None)
    assert scope["branch_filter_mode"] == ("explicit_labels_only" if branch is not None else "not_applied")
    if branch is not None:
        assert "未标注" in scope["note"] and "省略feature_branch" in scope["note"]
        assert len(scope["note"]) <= 160
    else:
        assert "note" not in scope
    assert response["results"] == []
    assert kb.mock_calls == [call.search("interface definition", limit=6, kind="manual", filters=filters)]


def test_branch_scope_hint_does_not_replace_exact_backend_scope_or_read_a_candidate(tmp_path):
    source = {"id": "candidate", "kind": "manual", "type": "manual_topic", "title": "A candidate",
              "retrieval": {"backend": "native", "generation": "pinned-generation",
                            "search_scope": {"kind": "manual", "feature_branch": "physics", "library_id": "enabled-only",
                                             "filter_stage": "before_retrieval", "evaluation_exclusions": "before_retrieval"}}}
    before = copy.deepcopy(source)
    kb = Mock()
    kb.search.return_value = [source]
    tools = PlanningService(tmp_path, None, kb)
    result = tools.call_public("search_knowledge", {"query": "definition", "kind": "manual", "mode": "native",
                                                   "feature_branch": "physics", "library_ids": ["enabled-only"]})
    assert result["results"][0]["retrieval"] == before["retrieval"]
    assert result["results"][0]["candidate_only"] and result["results"][0]["read_required"]
    assert source == before
    assert kb.mock_calls == [call.search("definition", limit=6, kind="manual",
                                        filters={"mode": "native", "feature_branch": "physics", "library_ids": ["enabled-only"]})]


def test_product_branch_schema_keeps_legal_enum_and_explains_missing_labels(tmp_path):
    definition = next(item for item in tool_definitions() if item["name"] == "search_knowledge")
    schema = definition["inputSchema"]["properties"]["feature_branch"]
    assert schema["enum"] == list(FEATURE_BRANCHES)
    assert "未标注" in schema["description"] and "省略" in schema["description"]
    kb = Mock()
    tools = PlanningService(tmp_path, None, kb)
    with pytest.raises(ValueError):
        tools.call_public("search_knowledge", {"query": "definition", "kind": "manual", "mode": "native", "feature_branch": "geometry_typo"})
    kb.search.assert_not_called()
