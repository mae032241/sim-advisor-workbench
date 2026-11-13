"""Derived case excerpts keep exact source permissions and reading credit."""
from contextlib import closing
import copy
import json

import pytest

from advisor import case_context_store
from advisor import library_provider as provider_module
from advisor import library_search as search_module
from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_search import _hash
from advisor.object_index import qmd_path
from advisor.planning_service import PlanningService
from advisor.sessions import SessionStore
from test_case_navigation_search import navigation
from test_workflow import FakePlanner, GOAL


@pytest.fixture
def contextual_provider(navigation, monkeypatch):
    fixture = navigation
    source = fixture.tutorial
    source["case_group"] = "tutorial-context-group"
    with closing(fixture.store.connect()) as db, db:
        db.execute("UPDATE revisions SET payload=? WHERE id=?", (json.dumps(source), source["id"]))
        db.execute("INSERT INTO case_members VALUES(?,?,?)", (source["case_group"], "documented-step", source["id"]))
    case = fixture.overviews[0]
    ref = {"id": source["id"], "source_id": source["id"], "object_id": source["object_id"],
           "library_id": source["library_id"], "section": "mesh", "tutorial_order": 1,
           "source_sha256": "a" * 64, "locators": [{"kind": "tutorial_step", "section": "mesh", "order": 1}],
           "basis": "documented_not_model_validated"}
    profile = {"schema": case_context_store.SCHEMA, "case_id": case["id"], "library_id": case["library_id"],
               "case_group": case["case_group"], "text": source["text"],
               "basis": "documented_not_model_validated", "content_hash": _hash(source["text"]),
               "source_refs": [ref], "lineage_ids": [case["id"]],
               "excerpts": [{"text": source["text"], "source_id": source["id"], "library_id": source["library_id"],
                             "section": "mesh", "source_text_start": 0, "source_text_end": len(source["text"]),
                             "basis": "documented_not_model_validated"}],
               "model_completeness": {"classification": "configured", "basis": "saved_metadata_only",
                                      "evidence": {"sections": {"geometry": 1, "mesh": 1}}},
               "is_read_evidence": False, "qmd_path": qmd_path(case), "qmd_hash": _hash("fixture overview")}
    active = fixture.search._load_active(summary=True)
    case_context_store.write_contexts(fixture.store.root / "search", active, [profile], {
        case["id"]: profile["model_completeness"]})
    monkeypatch.setattr(provider_module, "LibrarySearch", lambda *_args: fixture.search)
    monkeypatch.setattr(fixture.provider, "related_sources", lambda *_args, **_kwargs: [])
    sessions = SessionStore(fixture.store.root.parent, fixture.provider, FakePlanner())
    session = sessions.create(GOAL, None)
    fixture.tools = PlanningService(fixture.store.root.parent, sessions, fixture.provider)
    fixture.session, fixture.profile, fixture.case = session, profile, case
    return fixture


def read_case(fixture):
    return fixture.tools.call(fixture.session["id"], "read_source", {
        "id": fixture.case["id"], "offset": 0, "length": 16000})


def test_scope_exposes_required_manual_library_without_body_or_read_credit(contextual_provider):
    fixture = contextual_provider
    fixture.store.set_enabled("manuals", False)
    result = fixture.tools.call_public("get_library_scope", {})
    assert result["case_context_dependencies"] == {"models": ["manuals"]}
    assert result["case_context_disabled_dependencies"] == ["manuals"]
    assert fixture.tutorial["text"] not in json.dumps(result)
    assert fixture.tools.read_sources(fixture.session["id"]) == []


def test_manual_close_after_search_decoration_still_removes_the_contextual_hit(contextual_provider, monkeypatch):
    fixture = contextual_provider
    decorate = fixture.provider._decorate_case_context
    def closing(source, **kwargs):
        result = decorate(source, **kwargs)
        if source["id"] == fixture.case["id"]:
            fixture.store.set_enabled("manuals", False)
        return result
    monkeypatch.setattr(fixture.provider, "_decorate_case_context", closing)
    result = fixture.provider.search_cases("needle", library_ids=["models"], limit=8)
    assert fixture.case["id"] not in {item["id"] for item in result}


def test_enabled_manual_outside_requested_libraries_is_visible_as_documented_candidate(contextual_provider):
    fixture = contextual_provider
    result = fixture.tools.call(fixture.session["id"], "search_cases", {"query": "needle", "library_ids": ["models"]})
    candidate = next(value for value in result["results"] if value["id"] == fixture.case["id"])
    assert candidate["excerpt"] == fixture.tutorial["text"]
    assert candidate["candidate_only"] and candidate["read_required"]
    assert "data" not in candidate and "text" not in candidate
    assert candidate["id"] == fixture.case["id"] and candidate["library_id"] == fixture.case["library_id"]
    assert candidate["source_context"]["role"] == "model_overview"
    assert candidate["source_context"]["applicability"] == "not_verified"
    # Full dependency provenance stays in the provider's authoritative source;
    # the Agent receives the exact case ID and a bounded candidate summary.
    source = fixture.provider.get_document(fixture.case["id"])
    context = source["data"]["search_context"]
    assert context["model_settings_agreement"] == "not_verified"
    assert context["basis"] == "documented_not_model_validated"
    assert context["source_refs"][0]["source_id"] == fixture.tutorial["id"]
    assert source["read_scope"] == "model_metadata_and_bounded_documented_context"
    assert fixture.tools.read_sources(fixture.session["id"]) == []


def test_read_source_appends_exact_cited_excerpt_without_crediting_manual_as_read(contextual_provider):
    fixture = contextual_provider
    archive = copy.deepcopy(fixture.store.get(fixture.case["id"]))
    page = read_case(fixture)
    assert page["text"].startswith(archive["text"])
    assert fixture.tutorial["text"] in page["text"]
    assert "[来源 " + fixture.tutorial["id"] + " · mesh]" in page["text"]
    assert "与模型设置的一致性未核实" in page["text"]
    assert page["read_scope"] == "model_metadata_and_bounded_documented_context"
    assert "data" not in page and page["metadata_scope"] == "bounded_summary"
    source = fixture.provider.get_document(fixture.case["id"])
    assert source["data"]["search_context"]["source_refs"][0]["id"] == fixture.tutorial["id"]
    assert page["text"] == source["text"]
    assert page["offset"] == 0 and page["next_offset"] is None
    assert page["total_characters"] == page["returned_characters"] == len(source["text"])
    assert page["evidence_refs"] == [{"source_id": fixture.case["id"], "context": "source_text",
        "locator": "text:0:" + str(len(page["text"]))}]
    assert fixture.tools.read_sources(fixture.session["id"]) == [fixture.case["id"]]
    assert fixture.store.get(fixture.case["id"]) == archive
    assert archive["data"] == fixture.case["data"] and archive["evidence"] == fixture.case["evidence"]
    # A real separate manual read, not an overview read, earns its own credit.
    fixture.tools.call(fixture.session["id"], "read_source", {"id": fixture.tutorial["id"], "length": 500})
    assert fixture.tools.read_sources(fixture.session["id"]) == [fixture.case["id"], fixture.tutorial["id"]]


def test_closed_manual_hides_new_rich_candidates_but_raw_model_remains_readable(contextual_provider):
    fixture = contextual_provider
    old = fixture.provider.get_document(fixture.case["id"])
    assert fixture.provider.source_available(old)
    fixture.store.set_enabled("manuals", False)
    assert not fixture.provider.source_available(old), "An old decorated body cannot retain derived-source permission"
    assert fixture.provider.source_available(fixture.case), "Raw model permission is independent of its tutorial"
    result = fixture.tools.call(fixture.session["id"], "search_cases", {"query": "needle", "library_ids": ["models"]})
    assert fixture.case["id"] not in {value["id"] for value in result["results"]}
    page = read_case(fixture)
    assert page["text"] == fixture.case["text"]
    assert fixture.tutorial["text"] not in json.dumps(page, ensure_ascii=False)
    assert "search_context" not in page.get("data", {})
    assert fixture.tools.read_sources(fixture.session["id"]) == [fixture.case["id"]]


@pytest.mark.parametrize("heldout", ["source", "object", "group", "path"])
def test_new_reads_omit_derived_text_for_every_dependency_holdout(contextual_provider, monkeypatch, heldout):
    fixture = contextual_provider
    old = fixture.provider.get_document(fixture.case["id"])
    options = {"token": "read-policy-" + heldout}
    source = fixture.tutorial
    if heldout == "source":
        options["citation_ids"] = frozenset([source["id"]])
    elif heldout == "object":
        options["object_ids"] = frozenset([source["object_id"]])
    elif heldout == "group":
        options["case_groups"] = frozenset([source["case_group"]])
    else:
        options["qmd_paths"] = frozenset([qmd_path(source)])
    policy = HoldoutPolicy(**options)
    monkeypatch.setattr(fixture.store, "evaluation_policy", lambda: policy)
    monkeypatch.setattr(fixture.provider.store, "evaluation_policy", lambda: policy)
    monkeypatch.setattr(search_module, "exclusion_snapshot", lambda _root: policy)
    assert not fixture.provider.source_available(old)
    page = read_case(fixture)
    assert page["text"] == fixture.case["text"]
    assert "search_context" not in page.get("data", {})
    assert fixture.tutorial["text"] not in json.dumps(page, ensure_ascii=False)
    assert fixture.tools.read_sources(fixture.session["id"]) == [fixture.case["id"]]
    assert fixture.provider.source_available(fixture.case)


def test_manual_close_does_not_block_saved_configuration_default(contextual_provider, monkeypatch):
    fixture = contextual_provider
    fixture.store.set_enabled("manuals", False)
    requests = []
    def saved_configuration(case_id, **options):
        requests.append((case_id, options))
        return {"id": case_id, "source": "saved", "returned": 1, "nodes": [
            {"path": "compact:node/7", "properties": [{"name": "hmax", "value": "0.2[mm]", "status": "decoded_saved"}]}],
            "effective_activation": "not_evaluated", "physical_validation": "not_run"}
    monkeypatch.setattr(fixture.provider.store, "configuration", saved_configuration)
    result = fixture.tools.call(fixture.session["id"], "read_case_configuration", {"case_id": fixture.case["id"]})
    assert requests == [(fixture.case["id"], {"category": None, "node_path": None, "source": "saved", "limit": 1})]
    assert result["nodes"][0]["properties"][0]["value"] == "0.2[mm]"
    assert result["physical_validation"] == "not_run"
    assert "search_context" not in result
    assert fixture.tools.read_sources(fixture.session["id"]) == [fixture.case["id"]]


def test_six_context_hits_share_manifest_and_dependency_batches(contextual_provider, monkeypatch):
    from advisor import case_context_index
    fixture = contextual_provider
    profiles = []
    for case in fixture.overviews[:6]:
        profile = copy.deepcopy(fixture.profile)
        profile.update(case_id=case['id'], case_group=case['case_group'],
                       lineage_ids=[case['id']], qmd_path=qmd_path(case))
        profiles.append(profile)
    case_context_store.write_contexts(fixture.store.root / 'search',
                                     fixture.search._load_active(summary=True), profiles, {})
    active_reads, batches = [], []
    original_active, original_readable = case_context_index._active, case_context_index.readable_contexts

    def active(*args, **kwargs):
        active_reads.append(1)
        return original_active(*args, **kwargs)

    def readable(store, values):
        batches.append(len(values))
        return original_readable(store, values)

    monkeypatch.setattr(case_context_index, '_active', active)
    monkeypatch.setattr(case_context_index, 'readable_contexts', readable)
    hits = fixture.provider.search_cases('needle', library_ids=['models'], limit=8)
    assert sum(bool(hit.get('data', {}).get('search_context')) for hit in hits) == 6
    assert active_reads == [1]
    assert batches == [6, 6], 'Each phase reads one result batch, with fresh final permissions'


def test_dependency_superseded_after_decoration_is_not_returned(contextual_provider, monkeypatch):
    fixture = contextual_provider
    original = fixture.provider._decorate_case_context

    def supersede(source, **kwargs):
        result = original(source, **kwargs)
        with closing(fixture.store.connect()) as db, db:
            db.execute('DELETE FROM active WHERE citation_id=?', (fixture.tutorial['id'],))
        return result

    monkeypatch.setattr(fixture.provider, '_decorate_case_context', supersede)
    hits = fixture.provider.search_cases('needle', library_ids=['models'])
    assert fixture.case['id'] not in {hit['id'] for hit in hits}


def test_dependency_close_during_final_owner_check_fails_closed(contextual_provider, monkeypatch):
    fixture = contextual_provider
    original = fixture.provider._available_navigation_ids

    def close_after_owner_check(references):
        result = original(references)
        fixture.store.set_enabled('manuals', False)
        return result

    monkeypatch.setattr(fixture.provider, '_available_navigation_ids', close_after_owner_check)
    with pytest.raises(RuntimeError, match='visibility changed'):
        fixture.provider.search_cases('needle', library_ids=['models'])
