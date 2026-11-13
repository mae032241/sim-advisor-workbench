"""Extractive context construction is independent of query and case names."""
import copy
import hashlib
import json
import sqlite3

import pytest

from advisor import case_context as context
from advisor.evaluation_sets import HoldoutPolicy


class Store:
    def __init__(self, root):
        self.root = root
        self.db_path = root / "catalog.sqlite3"
        self.sources = {}
        self.gets = []
        self.policy = HoldoutPolicy()
        self.enabled = set()  # Explicit construction can inspect closed archives.
        with sqlite3.connect(self.db_path) as db:
            db.executescript("""
              CREATE TABLE revisions(id TEXT PRIMARY KEY,object_id TEXT,library_id TEXT,payload TEXT);
              CREATE TABLE active(library_id TEXT,object_id TEXT,citation_id TEXT);
              CREATE TABLE tutorial_document_sources(source_sha256 TEXT,identity TEXT,
                owner_library_id TEXT,citation_id TEXT,anchor TEXT,category TEXT);
              CREATE TABLE tutorial_model_documents(source_id TEXT,owner_library_id TEXT,
                source_sha256 TEXT,identity TEXT);
              CREATE TABLE tutorial_associations(source_id TEXT,source_library_id TEXT,
                target_id TEXT,target_library_id TEXT,relation TEXT,source_sha256 TEXT,version TEXT);
            """)

    def connect(self):
        db = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)
        db.execute("PRAGMA query_only=ON")
        return db

    def get(self, identity):
        self.gets.append(identity)
        return copy.deepcopy(self.sources.get(identity))

    def evaluation_policy(self):
        return self.policy

    def require_not_heldout(self, identity, source=None):
        if self.policy.blocks_identity(identity) or source is not None and self.policy.blocks_object(source):
            raise PermissionError("heldout")

    def add(self, source, *, active=True, document=None, anchor=None):
        self.sources[source["id"]] = copy.deepcopy(source)
        with sqlite3.connect(self.db_path) as db:
            db.execute("INSERT OR REPLACE INTO revisions VALUES(?,?,?,?)",
                       (source["id"], source["object_id"], source["library_id"], json.dumps(source)))
            if active:
                db.execute("INSERT INTO active VALUES(?,?,?)", (source["library_id"], source["object_id"], source["id"]))
            if document:
                db.execute("INSERT INTO tutorial_document_sources VALUES(?,?,?,?,?,?)",
                           (document, "unfamiliar.document", source["library_id"], source["id"], anchor or "html:#" + source["id"], "context"))

    def link(self, identity="case", document="document-sha", owner="models"):
        with sqlite3.connect(self.db_path) as db:
            db.execute("INSERT INTO tutorial_model_documents VALUES(?,?,?,?)", (identity, owner, document, "unfamiliar.document"))


def overview(**changes):
    value = {"id": "case", "object_id": "case-object", "library_id": "models", "asset_key": "models:unseen_channel.mph",
             "asset_hash": "asset-sha", "case_group": "immutable-group", "type": "model_case", "title": "Unseen channel",
             "data": {}, "coverage": {"source_container": {"status": "saved_model"},
             "raw_xml": {"status": "bounded_saved_state", "nodes_omitted_by_limit": 0, "truncated": True},
             "sections": {"geometry": 8, "materials": 2, "physics": 3, "studies": 1, "solvers": 4},
             "deep_api": {"status": "partial", "read_errors": 2}}}
    value.update(changes)
    return value


def section(identity, title, order, text, *, document="document-sha", part=1, chapter=None, type_="case_context"):
    return {"id": identity, "object_id": identity + "-object", "library_id": "manuals", "type": type_,
            "title": title, "text": text, "data": {"section": title, "tutorial_order": order,
            "chapter_order": chapter or order, "part": part, "parts": 1,
            "canonical_tutorial": {"source_sha256": document, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}},
            "evidence": [{"locator": "html:#" + identity, "method": "tutorial_html_section"}]}


@pytest.fixture
def prepared(tmp_path):
    store = Store(tmp_path)
    store.add(overview())
    # Names and subject deliberately differ from the five deployment pilots.
    store.add(section("z-intro", "Introduction", 1,
        "The example investigates a rotating channel carrying a reactive suspension under a prescribed periodic inlet.\n"
        "The objective is to compare the outlet concentration over time for several rotation rates."), document="document-sha")
    store.add(section("a-definition", "Model Definition", 2,
        "The channel uses an axisymmetric approximation with an insulated exterior wall and a fixed inlet temperature.\n"
        "Momentum transport is coupled to species transport through the temperature dependent reaction rate."), document="document-sha")
    store.add(section("route", "Modeling Approach", 3,
        "The first stationary solution initializes the subsequent transient study while the inlet remains prescribed."), document="document-sha")
    store.add(section("results", "Results and Discussion", 4,
        "THIS RESULT MUST NOT BECOME THE DOCUMENTED OBJECTIVE OR THE INITIAL CONDITIONS OF THE CASE."), document="document-sha")
    store.add(section("steps", "Modeling Instructions", 5,
        "Click this fictional command to configure the exact interface for the tutorial procedure.", type_="method_step"), document="document-sha")
    store.add(section("whole-body", "Unseen channel", 6,
        "WHOLE PAGE DUPLICATION MUST NEVER ENTER THE OVERVIEW OR ITS CONTEXT SEARCH FIELDS.", type_="manual_topic"), document="document-sha")
    store.add(section("unrelated", "Introduction", 1,
        "An unrelated document has the same title but must never be linked by a keyword or fuzzy title match.", document="other-sha"), document="other-sha")
    store.link()
    return store


def test_generic_context_uses_canonical_sections_and_exact_owned_slices(prepared):
    result = context.build_case_context(prepared, "case")
    assert result["schema"] == "case-context/v1" and result["state"] == "ready"
    assert result["case_id"] == "case" and result["asset_key"] == "models:unseen_channel.mph"
    assert result["library_id"] == "models" and result["asset_hash"] == "asset-sha"
    assert result["case_group"] == "immutable-group" and result["object_id"] == "case-object"
    assert "outlet concentration" in result["text"] and "insulated exterior wall" in result["text"]
    assert "temperature dependent reaction rate" in result["text"] and "stationary solution" in result["text"]
    assert not any(word in result["text"] for word in ("THIS RESULT", "WHOLE PAGE", "fictional command", "unrelated document"))
    assert {ref["id"] for ref in result["source_refs"]} == {"z-intro", "a-definition", "route"}
    assert {ref["library_id"] for ref in result["source_refs"]} == {"manuals"}
    assert result["text"] == "\n\n".join(excerpt["text"] for excerpt in result["excerpts"])
    for excerpt in result["excerpts"]:
        source = prepared.sources[excerpt["source_id"]]
        assert source["text"][excerpt["source_text_start"]:excerpt["source_text_end"]] == excerpt["text"]
        assert excerpt["locators"] == ["html:#" + source["id"]]
        assert excerpt["basis"] == "documented_not_model_validated"
    assert not result["is_read_evidence"] and result["model_settings_agreement"] == "not_verified"
    assert result["model_completeness"]["classification"] == "configured"
    assert result["model_completeness"]["api_status"] == "partial"
    assert result["physical_validation"] == "not_run" and result["effective_activation"] == "not_evaluated"
    assert result == context.build_case_context(prepared, "case")
    original_hash = result.pop("content_hash")
    assert original_hash == context._digest(context._json(result))


def test_ordinary_disabled_archives_are_internal_construction_without_enabling(prepared):
    result = context.build_case_context(prepared, "case")
    assert result["state"] == "ready" and prepared.enabled == set()


def test_heldout_cases_and_linked_sources_are_checked_before_body_reads(prepared):
    prepared.policy = HoldoutPolicy(citation_ids=frozenset({"case"}))
    with pytest.raises(PermissionError):
        context.build_case_context(prepared, "case")
    assert prepared.gets == []
    prepared.policy = HoldoutPolicy(object_ids=frozenset({"z-intro-object"}))
    result = context.build_case_context(prepared, "case")
    assert "z-intro" not in prepared.gets
    assert "outlet concentration" not in result["text"]
    assert "canonical_source_excluded" in {gap["code"] for gap in result["gaps"]}
    prepared.gets.clear()
    prepared.policy = HoldoutPolicy(source_hashes=frozenset({"document-sha"}))
    result = context.build_case_context(prepared, "case")
    assert prepared.gets == ["case"] and result["state"] == "degraded"
    assert not result["text"] and not result["source_refs"]


def test_pinned_base_is_same_asset_and_never_hydrates_heldout_base(prepared):
    base = overview(id="base", object_id="base-object", case_group="base-group")
    prepared.add(base, active=False)
    prepared.sources["case"]["data"] = {"base_case_id": "base", "base_case_group": "base-group"}
    with sqlite3.connect(prepared.db_path) as db:
        db.execute("DELETE FROM tutorial_model_documents")
    prepared.link("base")
    result = context.build_case_context(prepared, "case")
    assert result["lineage_ids"] == ["case", "base"] and result["state"] == "ready"
    prepared.gets.clear()
    prepared.policy = HoldoutPolicy(case_groups=frozenset({"base-group"}))
    with pytest.raises(PermissionError):
        context.build_case_context(prepared, "case")
    assert "base" not in prepared.gets
    prepared.policy = HoldoutPolicy()
    prepared.sources["base"]["asset_hash"] = "different-asset"
    with pytest.raises(ValueError, match="immutable case asset"):
        context.build_case_context(prepared, "case")


def test_missing_or_degraded_tutorial_never_gets_an_invented_summary(prepared):
    with sqlite3.connect(prepared.db_path) as db:
        db.execute("DELETE FROM tutorial_model_documents")
    result = context.build_case_context(prepared, "case")
    assert result["state"] == "degraded" and result["text"] == "" and result["excerpts"] == []
    assert result["gaps"] == [{"code": "canonical_tutorial_not_found"}]
    prepared.link()
    for source in prepared.sources.values():
        if source.get("library_id") == "manuals":
            source["text"] += " Changed after pinned extraction."
    result = context.build_case_context(prepared, "case")
    assert result["state"] == "degraded" and not result["source_refs"]
    assert "canonical_source_text_hash_mismatch" in {gap["code"] for gap in result["gaps"]}


def test_identity_owner_or_digest_mismatch_is_rejected(prepared):
    prepared.sources["z-intro"]["library_id"] = "wrong-owner"
    prepared.sources["a-definition"]["data"]["canonical_tutorial"]["source_sha256"] = "wrong-document"
    result = context.build_case_context(prepared, "case")
    assert {ref["id"] for ref in result["source_refs"]} == {"route"}
    assert "canonical_source_identity_mismatch" in {gap["code"] for gap in result["gaps"]}


def test_context_cap_complete_sentences_boilerplate_and_text_deduplication(tmp_path):
    store = Store(tmp_path)
    store.add(overview())
    intro = "Copyright 2026. All rights reserved.\nOBJECTIVE HEADER ONLY\n" + "\n".join(
        f"The documented objective numbered {n} compares measured quantities under clearly specified loading conditions. "
        "The requested comparison uses the outlet response during the complete interval of prescribed excitation." for n in range(100))
    definition = "\n".join(f"The documented condition numbered {n} prescribes a finite inlet value and a uniform wall temperature. "
        "The exterior boundary follows the documented thermal condition while material coefficients remain fixed." for n in range(100))
    store.add(section("intro", "Introduction", 1, intro), document="document-sha")
    store.add(section("definition", "Model Definition", 2, definition), document="document-sha")
    store.add(section("duplicate", "Introduction", 3, intro), document="document-sha")
    store.link()
    result = context.build_case_context(store, "case")
    assert 2000 < len(result["text"]) <= context.MAX_CONTEXT_CHARS
    assert len(result["excerpts"]) <= context.MAX_EXCERPTS
    assert "Copyright" not in result["text"] and "HEADER ONLY" not in result["text"]
    assert len({excerpt["text"] for excerpt in result["excerpts"]}) == len(result["excerpts"])
    assert all(excerpt["text"].endswith(".") for excerpt in result["excerpts"])
    assert {excerpt["role"] for excerpt in result["excerpts"]} == {"objective_and_scope", "conditions_and_mechanism"}


def test_chunk_continuations_and_arbitrary_word_prefixes_are_not_returned(tmp_path):
    store = Store(tmp_path)
    store.add(overview())
    source = section("intro", "Introduction", 1,
        "continued from a preceding source chunk with no independent subject. "
        "The complete documented sentence describes a prescribed velocity and a finite temperature. "
        "The trailing sentence is deliberately incomplete", part=2)
    store.add(source, document="document-sha")
    store.link()
    result = context.build_case_context(store, "case")
    assert result["text"] == "The complete documented sentence describes a prescribed velocity and a finite temperature."


def test_unfamiliar_opening_header_uses_structured_chapter_order(tmp_path):
    store = Store(tmp_path)
    store.add(overview())
    store.add(section("early", "An Unfamiliar Problem", 1,
        "This unfamiliar example measures the response of a thermal device to a prescribed incident pulse.", chapter=1), document="document-sha")
    store.add(section("late", "Some Later Analysis", 8,
        "This later analysis must not silently replace the missing introductory objective and model assumptions.", chapter=8), document="document-sha")
    store.link()
    result = context.build_case_context(store, "case")
    assert result["state"] == "ready" and [ref["id"] for ref in result["source_refs"]] == ["early"]


def test_geometry_scope_requires_saved_inventory_not_filename_or_absent_counts():
    source = overview(asset_key="models:complete_geom_sequence.mph")
    assert context.classify_case_metadata(source)["classification"] == "configured"
    source["coverage"]["sections"].update(materials=0, physics=0, studies=0, solvers=0)
    assert context.classify_case_metadata(source)["classification"] == "geometry_only"
    source["asset_key"] = "models:unfamiliar_file.mph"
    assert context.classify_case_metadata(source)["classification"] == "geometry_only"
    source["coverage"]["raw_xml"]["nodes_omitted_by_limit"] = 2
    assert context.classify_case_metadata(source)["classification"] == "unknown"
    source["coverage"]["raw_xml"]["nodes_omitted_by_limit"] = 0
    del source["coverage"]["sections"]["physics"]
    assert context.classify_case_metadata(source)["classification"] == "unknown"
    source["coverage"]["sections"]["physics"] = 3
    source["coverage"]["raw_xml"]["nodes_omitted_by_limit"] = 20
    assert context.classify_case_metadata(source)["classification"] == "configured"
    source["coverage"]["source_container"]["status"] = "preview_only"
    assert context.classify_case_metadata(source)["classification"] == "unknown"
    assert context.classify_case_metadata(overview(coverage={}))['classification'] == "unknown"


def test_missing_catalog_schema_and_missing_overview_are_explicit(prepared):
    with pytest.raises(FileNotFoundError):
        context.build_case_context(prepared, "missing")
    with pytest.raises(ValueError):
        context.build_case_context(prepared, "z-intro")
    with sqlite3.connect(prepared.db_path) as db:
        db.execute("DROP TABLE tutorial_document_sources")
    result = context.build_case_context(prepared, "case")
    assert result["state"] == "degraded"
    assert "canonical_tutorial_catalog_unavailable" in {gap["code"] for gap in result["gaps"]}
