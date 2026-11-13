"""Synthetic route-card contracts, real-read provenance and live source gates."""
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path

import pytest

from advisor.case_methods import CaseMethodStale
from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_store import encoded
from advisor.plan_drafts import schema_errors
from advisor.route_cards import (
    MAX_PAGE_BYTES, RouteCardBuilder, RouteCardStore, SCHEMA, SECTIONS,
    card_schema, constructor_tool_definitions, tool_definitions, validate_card,
)
from test_case_methods import methods  # noqa: F401; synthetic real-service setup
from test_planning_case_tools import case_tools  # noqa: F401; fixture dependency


@pytest.fixture
def routes(methods, tmp_path):
    methods.route_store = RouteCardStore(tmp_path / "routes-experiment", methods.provider)
    methods.route_builder = RouteCardBuilder(methods.route_store,
        lambda name, args: methods.tools.call_public(name, dict(args, session_id=methods.sid)),
        case_id=methods.overview["id"])
    return methods


def card(case_id, ref, *, origin="source_text", title="Synthetic staged method"):
    value = {"schema": SCHEMA, "title": title, "family": "Synthetic stage family",
             "summary": "Documented stages with assumptions still requiring transfer review.",
             "source_case_ids": [case_id], "sections": {
        name: [{"explanation": "The returned evidence has not established this method aspect.",
                "origin": "unknown", "evidence_refs": [], "limitations": ["No physical validation claim."]}]
        for name in SECTIONS}}
    value["sections"]["mechanism"] = [{
        "explanation": "The documented method combines stages under its stated assumptions.",
        "origin": origin, "evidence_refs": [copy.deepcopy(ref)],
        "limitations": ["Transfer requires checking target inputs and assumptions."]}]
    return value


def grounded(routes):
    routes.route_builder.call("read_source", {"id": routes.overview["id"]})
    page = routes.route_builder.call("read_source", {"id": routes.document["id"], "length": 500})
    return card(routes.overview["id"], page["evidence_refs"][0])


def submit(routes, value):
    return routes.route_builder.call("submit_method_card", {"card": value})


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def test_exact_complete_contracts_and_nine_bounded_sections(routes):
    value = grounded(routes)
    assert schema_errors(value, card_schema()) == []
    assert set(SECTIONS) == {"goals", "mechanism", "applicability", "exclusions", "alternatives",
                             "required_inputs", "coupling_chain", "implementation", "gaps"}
    assert constructor_tool_definitions()[0]["inputSchema"]["properties"]["card"] == card_schema()
    tools = {tool["name"]: tool for tool in routes.route_builder.tool_definitions()}
    assert set(tools) == {"read_source", "read_case_configuration", "submit_method_card", "get_tool_contract"}
    contract = routes.route_builder.call("get_tool_contract", {"tool": "submit_method_card", "path": "/card/sections/exclusions/items/origin"})
    assert contract["schema"]["enum"] == ["source_text", "saved_configuration", "api_configuration", "llm_inference", "unknown"]
    published = {tool["name"]: tool["inputSchema"] for tool in tool_definitions()}
    assert set(published) == {"list_route_methods", "read_method_card"}
    assert published["list_route_methods"]["properties"]["limit"]["default"] == 3
    assert published["read_method_card"]["properties"]["section"]["enum"] == ["overview", *SECTIONS]


def test_real_service_reads_publish_brief_card_and_dependency_binding(routes):
    value = grounded(routes)
    saved = submit(routes, value)
    assert saved["saved"] and saved["state"] == "ready"
    pack = json.loads(Path(saved["path"]).read_text(encoding="utf-8"))
    assert pack["card"] == value
    assert set(pack["dependencies"]) == {routes.overview["id"], routes.document["id"]}
    assert routes.document["text"] not in Path(saved["path"]).read_text(encoding="utf-8")
    assert not {"text", "nodes", "mph"} & set(pack)
    listing = routes.route_store.list_methods()
    assert listing["total"] == listing["returned"] == 1
    assert set(listing["methods"][0]) == {"method_id", "title", "family", "summary", "section_count"}
    assert listing["methods"][0]["section_count"] == 9
    overview = routes.route_store.read_method(saved["method_id"])
    assert overview["state"] == "ready" and "claims" not in overview and "card" not in overview
    assert overview["source_case_ids"] == [routes.overview["id"]]
    assert len(overview["sections"]) == 9
    assert size(listing) <= MAX_PAGE_BYTES and size(overview) <= MAX_PAGE_BYTES


def test_same_case_can_publish_multiple_server_hashed_cards_without_overwrite(routes):
    first = grounded(routes)
    second = copy.deepcopy(first)
    second["title"] = "Another synthetic method"
    one, two = submit(routes, first), submit(routes, second)
    assert one["method_id"] != two["method_id"]
    canonical = json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert one["method_id"] == "method-" + hashlib.sha256(canonical).hexdigest()
    before = Path(one["path"]).read_bytes()
    with pytest.raises(FileExistsError):
        submit(routes, first)
    assert Path(one["path"]).read_bytes() == before
    assert routes.route_store.list_methods()["total"] == 2


def test_directory_search_and_explicit_claim_pagination_never_return_entire_card(routes):
    value = grounded(routes)
    value["sections"]["mechanism"] *= 3
    for index, claim in enumerate(value["sections"]["mechanism"]):
        claim["explanation"] = f"Documented stage {index}."
    identity = submit(routes, value)["method_id"]
    assert routes.route_store.list_methods("synthetic assumptions")["total"] == 1
    assert routes.route_store.list_methods("unrelated")["total"] == 0
    seen, offset = [], 0
    while True:
        page = routes.route_store.read_method(identity, "mechanism", offset=offset)
        assert page["returned"] == 1 and size(page) <= MAX_PAGE_BYTES
        assert "sections" not in page and "card" not in page
        seen += page["claims"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert seen == value["sections"]["mechanism"]
    assert routes.route_store.read_method(identity, "mechanism", offset=99)["returned"] == 0
    with pytest.raises(ValueError, match="overview"):
        routes.route_store.read_method(identity, offset=1)
    with pytest.raises(ValueError, match="published schema"):
        routes.route_store.read_method(identity, section="all")


def test_utf8_page_budget_reduces_count_and_preserves_every_claim_and_locator(routes):
    value = grounded(routes)
    claim = value["sections"]["mechanism"][0]
    claim["explanation"] = "测" * 500
    claim["limitations"] = [letter * 180 for letter in "甲乙丙"]
    value["sections"]["mechanism"] = [copy.deepcopy(claim) for _ in range(3)]
    identity = submit(routes, value)["method_id"]
    offset, seen = 0, []
    while True:
        page = routes.route_store.read_method(identity, "mechanism", offset, 3)
        assert 1 <= page["returned"] < 3 and size(page) <= MAX_PAGE_BYTES
        seen.extend(page["claims"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert seen == value["sections"]["mechanism"]


def test_directory_byte_pages_keep_all_metadata_without_silent_truncation(routes):
    value = grounded(routes)
    for index in range(3):
        value["title"] = str(index) + "😀" * 99
        value["family"] = "😀" * 100
        value["summary"] = "😀" * 350
        submit(routes, value)
    page = routes.route_store.list_methods()
    assert page["returned"] == 2 and page["next_offset"] == 2
    tail = routes.route_store.list_methods(offset=page["next_offset"])
    assert tail["returned"] == 1 and tail["next_offset"] is None
    assert len({entry["method_id"] for entry in page["methods"] + tail["methods"]}) == 3
    assert all(entry["summary"] == "😀" * 350 for entry in page["methods"] + tail["methods"])
    assert size(page) <= MAX_PAGE_BYTES and size(tail) <= MAX_PAGE_BYTES


def test_handwritten_receipts_and_unread_source_refs_fail_closed(routes):
    ref = {"source_id": routes.document["id"], "context": "source_text", "locator": "text:0:500"}
    value = card(routes.overview["id"], ref)
    with pytest.raises(ValueError, match="private real-read ledger"):
        validate_card(value, receipts={"references": [ref]})
    with pytest.raises(ValueError, match="forged read reference"):
        submit(routes, value)
    value = grounded(routes)
    value["sections"]["mechanism"][0]["evidence_refs"][0]["locator"] = "text:0:501"
    with pytest.raises(ValueError, match="forged read reference"):
        submit(routes, value)


def test_configuration_modes_unknown_and_inference_keep_distinct_scopes(routes):
    value = grounded(routes)
    mechanism = value["sections"]["mechanism"][0]
    mechanism["origin"] = "saved_configuration"
    with pytest.raises(ValueError, match="distinct evidence scopes"):
        submit(routes, value)
    mechanism["origin"] = "source_text"
    value["sections"]["applicability"][0].update(origin="llm_inference", evidence_refs=copy.deepcopy(mechanism["evidence_refs"]))
    submit(routes, value)
    value["title"] = "Unknown cannot carry evidence"
    value["sections"]["applicability"][0]["origin"] = "unknown"
    with pytest.raises(ValueError, match="unknown is a gap"):
        submit(routes, value)
    value["sections"]["applicability"][0]["origin"] = "observed"
    with pytest.raises(ValueError, match="published schema"):
        submit(routes, value)


def test_native_properties_require_actual_delivery_not_parent_node_or_cache_presence(routes):
    case_id = routes.overview["id"]
    raw = routes.tools.call_public("read_case_configuration", {"case_id": case_id, "node_path": "compact:node/3", "source": "saved"})
    prop = copy.deepcopy(raw["nodes"][0]["properties"][0])
    hidden = copy.deepcopy(raw)
    hidden["nodes"][0]["properties"] = []
    routes.route_builder.record_delivery("read_case_configuration", {"case_id": case_id, "source": "saved"}, hidden)
    ref = {"source_id": case_id, "context": "case_configuration", "locator": prop["evidence_locator"]}
    value = card(case_id, ref, origin="saved_configuration")
    with pytest.raises(ValueError, match="forged read reference"):
        submit(routes, value)
    routes.route_builder.record_delivery("read_case_configuration", {"case_id": case_id, "source": "saved"}, raw)
    result = submit(routes, value)
    claim = routes.route_store.read_method(result["method_id"], "mechanism")["claims"][0]
    assert claim["evidence_refs"][0]["locator"] == prop["evidence_locator"]


def test_generic_json_fragments_cannot_grant_configuration_or_text_credit(routes):
    case_id = routes.overview["id"]
    routes.route_builder.record_delivery("read_case_configuration", {"case_id": case_id},
        {"case_id": case_id, "text": '{"name":"part of property"}', "offset": 0, "evidence_refs": []})
    routes.route_builder.record_delivery("read_source", {"id": case_id},
        {"id": case_id, "text": '{"raw_json":', "offset": 0, "evidence_refs": []})
    assert routes.route_builder._receipts.references == {}
    assert routes.route_builder.authorize_read("read_tool_page", {}) is None
    assert routes.route_builder.record_delivery("get_case_evidence_directory", {}, {}) is None


@pytest.mark.parametrize("target_name", ["case", "dependency"])
@pytest.mark.parametrize("gate", ["disabled", "heldout", "deleted"])
def test_every_dependency_gate_invalidates_both_directory_and_direct_reads(routes, monkeypatch, target_name, gate):
    identity = submit(routes, grounded(routes))["method_id"]
    target = routes.overview if target_name == "case" else routes.document
    if gate == "disabled":
        routes.store.set_enabled(target["library_id"], False)
    elif gate == "heldout":
        policy = HoldoutPolicy(citation_ids=frozenset([target["id"]]), token="synthetic-route-heldout")
        monkeypatch.setattr(routes.provider.store, "evaluation_policy", lambda: policy)
    else:
        with closing(routes.store.connect()) as db, db:
            db.execute("DELETE FROM revisions WHERE id=?", (target["id"],))
    with pytest.raises(PermissionError):
        routes.route_store.read_method(identity)
    assert routes.route_store.list_methods()["methods"] == []
    with pytest.raises(PermissionError):
        routes.route_builder.authorize_read("read_source", {"id": target["id"]})


@pytest.mark.parametrize("field", ["revision_id", "asset_hash"])
def test_changed_binding_is_stale_without_returning_card_metadata(routes, field):
    identity = submit(routes, grounded(routes))["method_id"]
    source = copy.deepcopy(routes.document)
    source[field] = "synthetic-changed-binding"
    with closing(routes.store.connect()) as db, db:
        db.execute("UPDATE revisions SET payload=? WHERE id=?", (encoded(source), source["id"]))
    view = routes.route_store.read_method(identity)
    assert view["state"] == "stale" and "summary" not in view and "claims" not in view
    assert routes.route_store.list_methods()["total"] == 0
    with pytest.raises(CaseMethodStale):
        routes.route_builder.record_delivery("read_source", {"id": source["id"]},
            {"id": source["id"], "text": "Changed", "offset": 0,
             "evidence_refs": [{"source_id": source["id"], "context": "source_text", "locator": "text:0:7"}]})


def test_old_archived_source_id_never_reads_new_revision_material(routes):
    identity = submit(routes, grounded(routes))["method_id"]
    source = copy.deepcopy(routes.document)
    source["id"], source["revision_id"] = "lo-" + "d" * 20, "synthetic-route-r2"
    with closing(routes.store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (source["id"], source["object_id"], "notes", encoded(source)))
        db.execute("UPDATE active SET citation_id=? WHERE library_id=? AND object_id=?", (source["id"], "notes", source["object_id"]))
    assert routes.route_store.read_method(identity)["state"] == "stale"
    assert routes.route_store.list_methods()["total"] == 0


def test_cached_pages_must_recheck_policy_before_any_delivery(routes):
    grounded(routes)
    routes.store.set_enabled("notes", False)
    with pytest.raises(PermissionError):
        routes.route_builder.authorize_read("read_source", {"id": routes.document["id"]})
    with pytest.raises(PermissionError):
        routes.route_builder.record_delivery("read_source", {"id": routes.document["id"]}, {})


def test_disabled_experiment_and_card_navigation_grant_no_planner_read_credit(routes):
    identity = submit(routes, grounded(routes))["method_id"]
    before = routes.tools.read_sources(routes.sid)
    assert routes.route_store.read_method(identity)["is_read_evidence"] is False
    assert routes.route_store.read_method(identity, "mechanism")["is_read_evidence"] is False
    assert routes.route_store.list_methods()["is_read_evidence"] is False
    assert routes.tools.read_sources(routes.sid) == before
    assert routes.route_store.read_method(identity, enabled=False) == {
        "state": "not_prepared", "method_id": identity, "is_read_evidence": False}
    assert routes.route_store.list_methods(enabled=False)["methods"] == []


def test_frozen_catalog_is_deterministic_immutable_and_still_source_gated(routes):
    value = grounded(routes)
    identity = submit(routes, value)["method_id"]
    frozen = routes.route_store.freeze_catalog()
    first = {key: Path(frozen[key]).read_bytes() for key in ("catalog_path", "manifest_path")}
    assert routes.route_store.freeze_catalog() == frozen
    assert first == {key: Path(frozen[key]).read_bytes() for key in first}
    assert frozen["method_count"] == 1
    value["title"] = "Late card after freeze"
    with pytest.raises(FileExistsError, match="frozen"):
        submit(routes, value)
    routes.store.set_enabled("notes", False)
    assert routes.route_store.list_methods()["total"] == 0
    with pytest.raises(PermissionError):
        routes.route_store.read_method(identity)
    assert first == {key: Path(frozen[key]).read_bytes() for key in first}


def test_submission_rechecks_gates_and_cannot_bind_another_case(routes):
    value = grounded(routes)
    value["source_case_ids"] = ["lo-synthetic-other-case"]
    with pytest.raises(PermissionError, match="bound"):
        submit(routes, value)
    value["source_case_ids"] = [routes.overview["id"]]
    routes.store.set_enabled("notes", False)
    with pytest.raises(PermissionError):
        submit(routes, value)
    assert not routes.route_store.directory.exists()


@pytest.mark.parametrize("field", ["method_id", "evaluation_answer", "read_receipts"])
def test_unpublished_fields_and_model_assigned_ids_are_rejected(routes, field):
    value = grounded(routes)
    value[field] = "synthetic-unpublished-value"
    with pytest.raises(ValueError, match="published schema"):
        submit(routes, value)


def test_shared_library_paths_and_oversized_atomic_claims_are_refused(routes):
    with pytest.raises(ValueError, match="outside shared libraries"):
        RouteCardStore(routes.store.root / "routes", routes.provider)
    value = grounded(routes)
    refs = []
    for offset in range(3):
        identity = routes.document["id"]
        locator = "saved:" + (str(offset) + "测" * 500)
        ref = {"source_id": identity, "context": "case_configuration", "locator": locator}
        refs.append(ref)
        # Trusted native page fixture: the locator is actually displayed here.
        routes.route_builder.record_delivery("read_case_configuration", {"case_id": routes.overview["id"], "source": "saved"},
            {"case_id": routes.overview["id"], "source": "saved", "returned": 1,
             "nodes": [{"path": locator.removeprefix("saved:"), "properties": []}],
             "evidence_refs": [{**ref, "source_id": routes.overview["id"]}]})
        refs[-1]["source_id"] = routes.overview["id"]
    claim = value["sections"]["mechanism"][0]
    claim.update(origin="saved_configuration", explanation="测" * 500,
                 limitations=[letter * 180 for letter in "甲乙丙"], evidence_refs=refs)
    with pytest.raises(ValueError, match="atomic method page"):
        submit(routes, value)
