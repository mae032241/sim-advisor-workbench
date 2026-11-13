"""Agent knowledge views prioritize evidence bodies over repeated metadata."""
import copy
import json

from advisor.planning_views import candidate_for_agent, library_scope_for_agent, source_page_for_agent


def oversized_source():
    huge = "NESTED_BODY_SENTINEL" * 10000
    return {
        "id": "lo-source@revision-7", "title": "Saved mesh", "kind": "case", "type": "saved_configuration",
        "revision_id": "revision-7", "library_id": "models", "asset_key": "models:case.mph",
        "text": "READ_BODY_SENTINEL" * 10000,
        "retrieval": {"backend": "qmd_vector", "generation": "generation-23", "scope": {
            "kind": "case", "feature_branch": "mesh", "filter_stage": "before_retrieval", "library_ids": ["models"]}},
        "evidence": [{"root_id": "derived", "relative_path": huge, "locator": huge, "sha256": "a" * 64}] * 5000,
        "coverage": {"raw_xml": {"status": "partial", "truncated": True, "normalization": {
            "properties_unresolved": 12, "properties_partial": 3, "canonical_evidence": [huge] * 1000}},
            "deep_api": {"status": "not_requested", "read_errors": [huge] * 1000}, "canonical_evidence": [huge] * 1000},
        "data": {"summary": "Observed mesh settings; effectiveness remains unverified.", "feature_branch": "mesh",
            "effective_activation": "not_evaluated", "physical_validation": "not_run", "status": "partial",
            "instructions": [huge] * 1000, "canonical_tutorial": {"nested": [huge] * 1000},
            "model_snapshot": {"nodes": [huge] * 1000}, "search_context": {"provenance": [huge] * 1000}},
    }


def test_candidate_has_small_metadata_and_no_authoritative_bodies():
    source = oversized_source()
    before = copy.deepcopy(source)
    result = candidate_for_agent(source)
    encoded = json.dumps(result, ensure_ascii=False)
    assert "READ_BODY_SENTINEL" not in encoded
    assert "NESTED_BODY_SENTINEL" not in encoded
    assert "text" not in result and "data" not in result
    assert result["excerpt"] == source["data"]["summary"]
    assert len(encoded) < 1800
    assert len(json.dumps([candidate_for_agent(source)] * 6, ensure_ascii=False)) < 11000
    assert result["candidate_only"] and result["read_required"]
    assert source == before


def test_code_candidate_never_excerpts_code_even_when_legacy_excerpt_is_code():
    source = {"id": "code", "kind": "code", "type": "implementation_snippet", "excerpt": "CODE_BODY" * 10000,
              "text": "CODE_BODY" * 10000, "data": {"code": "CODE_BODY" * 10000}}
    assert "CODE_BODY" not in json.dumps(candidate_for_agent(source))


def test_candidate_excerpt_is_bounded_with_visible_omission():
    result = candidate_for_agent({"id": "manual", "excerpt": "a" * 10000})
    assert len(result["excerpt"]) == 400
    assert result["excerpt"].endswith("…")


def test_closed_source_candidate_retains_identity_without_body_or_empty_excerpt():
    source = oversized_source()
    source["availability"] = {"readable": False, "state": "unavailable"}
    before = copy.deepcopy(source)
    result = candidate_for_agent(source)
    assert result["id"] == source["id"]
    assert result["availability"]["readable"] is False
    assert not {"excerpt", "text", "data", "coverage", "related_sources"} & set(result)
    assert "BODY_SENTINEL" not in json.dumps(result)
    assert source == before


def test_page_preserves_exact_body_pagination_identity_and_retrieval():
    source = oversized_source()
    page = {**source, "text": source["text"][500:12500], "offset": 500, "next_offset": 12500,
            "total_characters": len(source["text"])}
    before = copy.deepcopy(page)
    result = source_page_for_agent(page)
    assert next(iter(result)) == "text"
    for key in ("text", "offset", "next_offset", "total_characters", "id", "title", "kind", "type",
                "library_id", "revision_id", "asset_key", "retrieval"):
        assert result[key] == page[key]
    metadata = {key: value for key, value in result.items() if key != "text"}
    assert len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) < 2000
    assert "NESTED_BODY_SENTINEL" not in json.dumps(metadata)
    assert result["coverage"]["raw_xml"]["status"] == "partial"
    assert result["coverage"]["raw_xml"]["normalization"]["properties_unresolved"] == 12
    assert result["source_context"]["status"] == "partial"
    assert result["source_context"]["physical_validation"] == "not_run"
    assert page == before
    result["retrieval"]["scope"]["library_ids"].append("changed")
    assert page["retrieval"] == before["retrieval"]


def test_final_page_does_not_invent_more_text_or_fake_completion():
    page = {"id": "s", "text": "all text", "offset": 999, "next_offset": None, "total_characters": 1007}
    assert source_page_for_agent(page)["next_offset"] is None
    assert source_page_for_agent(page)["text"] == "all text"


def test_navigation_keeps_exact_ids_and_unresolved_state_with_omitted_counts():
    related = [{"id": "dependency-exact-1", "revision_id": "r1", "library_id": "other",
                "relation": "requires", "status": "pinned", "alignment": "not_verified", "text": "RAW" * 10000},
               {"title": "missing-function", "relation": "requires", "status": "unresolved", "reason": "not pinned"}]
    page = {"id": "source", "text": "body", "offset": 0, "next_offset": None, "total_characters": 4,
            "related_sources": related + [{"id": f"dep-{index}"} for index in range(100)],
            "linked_topics": [{"id": "linked-topic", "title": "Topic"}],
            "data": {"prerequisites": [{"id": "exact-prerequisite", "status": "unresolved"}]}}
    before = copy.deepcopy(page)
    result = source_page_for_agent(page)
    assert result["related_sources"][0]["id"] == "dependency-exact-1"
    assert result["related_sources"][0]["revision_id"] == "r1"
    assert result["related_sources"][1]["status"] == "unresolved"
    assert result["related_sources_omitted"] == 98
    assert result["linked_topics"][0]["id"] == "linked-topic"
    assert result["prerequisites"][0]["id"] == "exact-prerequisite"
    assert "RAW" not in json.dumps(result)
    assert page == before


def test_nested_long_titles_are_bounded_but_exact_reference_ids_are_usable():
    source = {"id": "s", "text": "body", "related_sources": [
        {"id": "exact-source-id", "title": "long" * 100000, "reason": "why" * 100000,
         "data": {"raw": "private" * 100000}}] * 1000}
    result = source_page_for_agent(source)
    assert result["related_sources"][0]["id"] == "exact-source-id"
    assert len(result["related_sources"][0]["title"]) <= 72
    assert result["related_sources_omitted"] > 0
    assert len(json.dumps({key: value for key, value in result.items() if key != "text"})) < 2000


def test_existing_compact_source_context_and_retrieval_are_preserved_detached():
    source = {"id": "s", "source_context": {"role": "case_tutorial", "case_identity": "models.mesh.case",
        "applicability": "not_verified", "status": "partial", "source_validity": {"status": "unresolved"}},
        "retrieval": {"backend": "native", "generation": "exact", "new_backend_field": {"contract": [1, 2, 3]}}}
    result = candidate_for_agent(source)
    assert result["source_context"] == source["source_context"]
    assert result["retrieval"] == source["retrieval"]
    result["source_context"]["source_validity"]["status"] = "changed"
    assert source["source_context"]["source_validity"]["status"] == "unresolved"


def test_nested_coverage_schema_is_bounded_even_when_every_allowed_branch_is_large():
    branches = ("raw_xml", "deep_api", "tutorial", "saved_features", "source_container", "normalization", "properties",
                "selections", "traversal", "property_statuses", "selection_statuses", "read_coverage")
    leaf = {"status": "partial", "unresolved": 50, "properties_partial": 3, "read_errors": ["huge" * 100000] * 1000,
            "read_contract": "long" * 100000, "physical_validation": "not_run"}
    nested = {branch: leaf for branch in branches}
    source = {"id": "s", "text": "complete requested text", "coverage": {branch: nested for branch in branches}}
    result = source_page_for_agent(source)
    assert result["text"] == source["text"]
    assert len(json.dumps({key: value for key, value in result.items() if key != "text"})) < 2000
    assert "partial" in json.dumps(result["coverage"])
    assert "unresolved" in json.dumps(result["coverage"])
    assert result["coverage"]["details_omitted"]


def test_model_completeness_keeps_classification_and_explicit_validity_without_evidence_trees():
    source = {"id": "overview", "data": {"model_completeness": {
        "classification": "geometry_only", "basis": "saved_configuration", "reason": "explicit_zero_counts",
        "physical_validation": "not_run", "effective_activation": "not_evaluated", "evidence": ["RAW" * 10000] * 1000}}}
    result = candidate_for_agent(source)
    assert result["source_context"]["model_completeness"]["classification"] == "geometry_only"
    assert result["source_context"]["model_completeness"]["physical_validation"] == "not_run"
    assert "RAW" not in json.dumps(result)


def test_library_scope_lists_catalog_once_without_root_paths_or_duplicate_objects():
    libraries = [{"id": "models", "name": "Models", "type": "official-models", "version": "6.3", "enabled": True,
                  "object_count": 123, "roots": [{"id": "root", "role": "models", "path": "PRIVATE_ROOT_PATH" * 10000}],
                  "capability_counts": {"configured": 45, "geometry_only": 10}, "raw_metadata": "RAW" * 10000}]
    scope = {"schema": "library-scope/v1", "libraries": libraries,
             "groups": [{"id": "official", "name": "Official", "groups": [
                 {"id": "official-models", "name": "Models", "libraries": libraries}]}],
             "enabled_library_ids": ["models"], "library_count": 1, "enabled_library_count": 1,
             "global_library_count": 15, "matched_library_count": 1, "filter_applied": True,
             "query": "Models", "type": None, "query_match_mode": "literal_substring", "next_action": "Choose explicitly.",
             "case_context_dependencies": {"models": ["tutorials"]}, "case_context_disabled_dependencies": ["tutorials"]}
    before = copy.deepcopy(scope)
    result = library_scope_for_agent(scope)
    assert result["libraries"][0] == {"id": "models", "name": "Models", "type": "official-models", "version": "6.3",
                                      "enabled": True, "object_count": 123, "root_count": 1,
                                      "capability_counts": {"configured": 45, "geometry_only": 10}}
    assert result["groups"][0]["groups"][0]["library_ids"] == ["models"]
    for key in ("global_library_count", "matched_library_count", "filter_applied", "query", "type", "next_action",
                "case_context_dependencies", "case_context_disabled_dependencies", "enabled_library_ids"):
        assert result[key] == scope[key]
    assert "PRIVATE_ROOT_PATH" not in json.dumps(result) and "RAW" not in json.dumps(result)
    assert len(json.dumps(result)) < 1500
    assert scope == before
    result["case_context_dependencies"]["models"].append("mutated")
    assert scope == before


def test_large_library_catalog_exposes_omissions_without_changing_actual_scope_counts():
    libraries = [{"id": f"lib-{index}", "name": f"Library {index}", "type": "user-models", "enabled": True,
                  "version": "6.3", "object_count": 1, "roots": [{"path": "PRIVATE" * 10000}]} for index in range(200)]
    scope = {"libraries": libraries, "groups": [{"id": "custom", "libraries": libraries}],
             "enabled_library_ids": [library["id"] for library in libraries], "library_count": 200,
             "global_library_count": 200, "matched_library_count": 200, "enabled_library_count": 200,
             "next_action": "Select libraries relevant to this task."}
    result = library_scope_for_agent(scope)
    assert len(result["libraries"]) == 32
    assert result["libraries_omitted"] == 168
    assert result["groups"][0]["libraries_omitted"] == 168
    assert result["enabled_library_ids_omitted"] == 168
    assert result["library_count"] == result["matched_library_count"] == 200
    assert result["next_action"] == scope["next_action"]
    assert "query or type" in result["view_next_action"]
    assert len(json.dumps(result)) < 8500
