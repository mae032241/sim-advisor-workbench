"""Source identity survives existing compact indexes and public tool boundaries."""
import copy
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest

from advisor.planning_service import PlanningService
from advisor.query_terms import normalize_mode
from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_store import encoded
from test_library_search import FakeEngineSearch, obj
from test_native_index_compaction import catalog


@pytest.mark.parametrize("mode", ["native", "qmd_keyword", "qmd_vector"])
def test_existing_compact_hit_keeps_tutorial_identity_in_search_and_read_without_reindex(tmp_path, mode):
    source = obj("tutorial", "documents", "specific method and settings")
    source.update(kind="manual", type="case_context", title="Notes About the COMSOL Implementation",
                  package="documents", physics=[], asset_key="help:com.comsol.help.models.test.example/example.html")
    source["data"] = {"tutorial_identity": "models.test.example", "status": "documented",
                      "model_agreement": "not_verified", "section": source["title"],
                      "canonical_tutorial": {"schema": "canonical-tutorial/v1", "ordinal": 5}}
    original = copy.deepcopy(source)
    store = catalog(tmp_path / "libraries", [source])
    search = FakeEngineSearch(store.root / "search")
    with store.object_stream() as objects:
        manifest = search.build(objects, modes=["native", "qmd_keyword", "qmd_vector"], generation="already-built")
    native = Path(manifest["directory"]) / "objects.sqlite3"
    with closing(sqlite3.connect(native)) as db:
        assert db.execute("SELECT source_json FROM objects").fetchone() == (None,), "Search must hydrate the exact catalog revision"
    protected = [native, search.active_path]
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}

    kb = Mock()
    kb.search.side_effect = lambda query, limit, kind, filters: search.search(
        query, limit=limit, kind=kind, mode=filters["mode"], library_ids=filters["library_ids"])
    kb.get_document.side_effect = store.read
    kb.related_sources.side_effect = store.related_sources
    kb.source_available.side_effect = lambda value: store.source_available(value["id"] if isinstance(value, dict) else value)
    tools = PlanningService(tmp_path, None, kb)
    result = tools.call(None, "search_knowledge", {"query": "specific method", "kind": "manual", "mode": mode,
                                                  "library_ids": ["documents"]})
    hit = result["results"][0]
    assert hit["source_context"]["case_identity"] == "models.test.example"
    assert hit["source_context"]["role"] == "case_tutorial"
    assert hit["source_context"]["model_agreement"] == "not_verified"
    assert hit["retrieval"]["backend"] == normalize_mode(mode)
    assert hit["retrieval"]["generation"] == "already-built"
    assert hit["retrieval"]["search_scope"]["kind"] == "manual"
    assert hit["retrieval"]["search_scope"]["library_id"] == "documents"
    assert hit["read_required"] and hit["candidate_only"]
    kb.get_document.assert_not_called()
    page = tools.call(None, "read_source", {"id": hit["id"], "offset": 0, "length": 500})
    assert page["source_context"] == hit["source_context"]
    assert page["text"] == source["text"]
    assert "text" not in page["source_context"] and "canonical_tutorial" not in page["source_context"]
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected} == hashes
    assert store.get(source["id"]) == original


def test_public_read_keeps_real_library_on_link_navigation(tmp_path):
    source = {"id": "source", "title": "Source", "kind": "manual", "type": "manual_topic", "text": "source body",
              "relative_path": "package/source.html", "related_links": [{"href": "target.html"}]}
    target = {"id": "target", "title": "Target", "library_id": "actual-target-library"}
    kb = Mock()
    kb.get_document.side_effect = [source, target]
    kb.related_sources.return_value = []
    kb.source_available.return_value = True
    tools = PlanningService(tmp_path, None, kb)
    result = tools.call(None, "read_source", {"id": "source", "offset": 0, "length": 500})
    assert result["linked_topics"] == [{"id": "target", "title": "Target", "library_id": "actual-target-library"}]


def test_api_without_explicit_relations_navigates_to_exact_case_group_overview(tmp_path, monkeypatch):
    common = {"library_id": "cases", "asset_key": "models:sample.mph", "asset_hash": "same-source-hash",
              "case_group": "original-group", "kind": "case", "data": {}, "relations": []}
    case = dict(obj("original-overview"), **common, type="model_case", local_id="model-case", title="Original case")
    observation = {**obj("observation"), **common, "type": "api_configuration", "local_id": "api-node",
                   "data": {"feature_branch": "physics", "source": "comsol_api_getter_observation"}}
    store = catalog(tmp_path / "libraries", [case, observation])
    with closing(store.connect()) as db, db:
        for source in (case, observation):
            db.execute("INSERT INTO case_members VALUES(?,?,?)", (source["case_group"], source["local_id"], source["id"]))
        latest = dict(case, id="lo-new-overview", revision_id="new-revision", case_group="new-group", title="Replacement case")
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (latest["id"], latest["object_id"], latest["library_id"], encoded(latest)))
        db.execute("UPDATE active SET citation_id=? WHERE object_id=?", (latest["id"], latest["object_id"]))
        db.execute("INSERT INTO case_members VALUES(?,?,?)", (latest["case_group"], latest["local_id"], latest["id"]))
    # Navigation is a metadata reference, never a body or COMSOL snapshot read.
    monkeypatch.setattr(store, "read", lambda *_args: pytest.fail("Navigation must not hydrate a model body"))
    links = store.related_sources(observation["id"])
    assert links[0] == {"id": case["id"], "library_id": "cases", "title": "Original case", "type": "model_case",
                        "relation": "same_case", "status": "pinned_case_version"}
    assert all(item["id"] != latest["id"] for item in links)
    assert all(not {"text", "data", "nodes", "code"} & set(item) for item in links)
    monkeypatch.setattr(store, "evaluation_policy", lambda: HoldoutPolicy(citation_ids=frozenset([case["id"]]), token="held-case"))
    assert store.related_sources(observation["id"]) == [], "A reference cannot advertise a held-out overview"


def test_closed_search_cannot_hydrate_candidate_context_or_return_source_hints(tmp_path, monkeypatch):
    source = obj("closed", "documents", "specific method")
    source.update(kind="manual", type="case_context", data={"tutorial_identity": "models.test.private"})
    store = catalog(tmp_path / "libraries", [source])
    search = FakeEngineSearch(store.root / "search")
    with store.object_stream() as objects:
        search.build(objects, modes=["native"], generation="existing")
    with closing(store.connect()) as db, db:
        library = json.loads(db.execute("SELECT body FROM libraries WHERE id='documents'").fetchone()[0])
        library["enabled"] = False
        db.execute("UPDATE libraries SET body=? WHERE id='documents'", (encoded(library),))
    monkeypatch.setattr("advisor.native_index.hydrate_object", lambda *_args, **_kwargs: pytest.fail("Closed content must not be hydrated"))
    assert search.search("specific method", library_ids=["documents"]) == []
