"""Real QMD scopes gate derived overview text by every live dependency."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from advisor import case_context_store as contexts
from advisor import library_search as module
from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_search import _hash
from advisor.object_index import qmd_path
from test_case_navigation_search import navigation


@pytest.fixture
def enriched(navigation):
    fixture = navigation
    case, source = fixture.overviews[0], fixture.tutorial
    profile = {"schema": contexts.SCHEMA, "case_id": case["id"], "library_id": case["library_id"],
               "case_group": case["case_group"], "text": "needle documented tutorial context",
               "is_read_evidence": False,
               "source_refs": [{"source_id": source["id"], "library_id": source["library_id"],
                                "object_id": source["object_id"]}],
               "qmd_path": qmd_path(case), "qmd_hash": _hash("fixture QMD overview")}
    active = fixture.search._load_active(summary=True)
    report = contexts.write_contexts(fixture.store.root / "search", active, [profile], {
        case["id"]: {"classification": "configured"},
        fixture.overviews[1]["id"]: {"classification": "geometry_only"},
        fixture.overviews[2]["id"]: {"classification": "unknown"}})
    fixture.context_profile, fixture.context_report = profile, report
    fixture.active = active
    return fixture


def ids(items):
    return {item["id"] for item in items}


def test_enabled_manual_dependency_outside_requested_model_libraries_authorizes_context(enriched):
    fixture = enriched
    hits = fixture.search.search("needle", library_ids=["models"], retrieval_view="case_overviews", limit=100)
    assert fixture.overviews[0]["id"] in ids(hits)
    assert fixture.tutorial["id"] not in ids(hits)
    statements = [json.loads(line) for line in fixture.trace.read_text(encoding="utf-8").splitlines()]
    query = next(value["sql"] for value in statements if value["event"] == "query")
    assert "case_contexts.dependencies" in query
    assert "case_context_catalog.active" in query
    assert "case_context_catalog.libraries" in query
    assert query.index("case_contexts.dependencies") < query.index("ORDER BY documents_fts.rank")
    assert "bm25(1.5,4.0,1.0)" in query


@pytest.mark.parametrize("deleted", [False, True])
def test_disabled_or_deleted_dependency_hides_enriched_overview_even_in_original_search(enriched, deleted):
    fixture = enriched
    if deleted:
        with closing(fixture.store.connect()) as db, db:
            row = db.execute("SELECT body FROM libraries WHERE id='manuals'").fetchone()
            body = json.loads(row[0])
            body["deleted_at"] = "2026-11-12T01:00:00Z"
            db.execute("UPDATE libraries SET body=? WHERE id='manuals'", (json.dumps(body),))
    else:
        fixture.store.set_enabled("manuals", False)
    case_id = fixture.overviews[0]["id"]
    assert case_id not in ids(fixture.search.search("needle", library_ids=["models"], limit=100))
    hits = fixture.search.search("needle", library_ids=["models"], retrieval_view="case_overviews", limit=100)
    assert case_id not in ids(hits) and fixture.overviews[2]["id"] in ids(hits)
    assert fixture.search.last_search_scope["eligible_objects"] == 5


@pytest.mark.parametrize("kind", ["source", "object", "group", "path"])
def test_heldout_dependency_is_gated_before_topk(enriched, monkeypatch, kind):
    fixture = enriched
    source = fixture.tutorial
    options = {"token": "hold-dependency-" + kind}
    if kind == "source":
        options["citation_ids"] = frozenset([source["id"]])
    elif kind == "object":
        options["object_ids"] = frozenset([source["object_id"]])
    elif kind == "group":
        options["case_groups"] = frozenset(["document-group"])
        with closing(fixture.store.connect()) as db, db:
            db.execute("INSERT INTO case_members VALUES(?,?,?)", ("document-group", "manual-step", source["id"]))
    else:
        options["qmd_paths"] = frozenset([qmd_path(source)])
    monkeypatch.setattr(module, "exclusion_snapshot", lambda _root: HoldoutPolicy(**options))
    assert fixture.overviews[0]["id"] not in ids(fixture.search.search("needle", library_ids=["models"], limit=100))
    hits = fixture.search.search("needle", library_ids=["models"], retrieval_view="case_overviews", limit=1)
    assert len(hits) == 1 and hits[0]["id"] != fixture.overviews[0]["id"]


def test_deleted_or_superseded_exact_source_is_not_authorized_by_old_index(enriched):
    fixture = enriched
    with closing(fixture.store.connect()) as db, db:
        db.execute("DELETE FROM active WHERE citation_id=?", (fixture.tutorial["id"],))
    assert fixture.overviews[0]["id"] not in ids(fixture.search.search("needle", library_ids=["models"], limit=100))


def test_geometry_only_is_excluded_only_from_case_search_with_explicit_opt_in(enriched):
    fixture = enriched
    normal = fixture.search.search("needle", retrieval_view="case_overviews", limit=100)
    assert fixture.overviews[1]["id"] not in ids(normal)
    assert fixture.overviews[2]["id"] in ids(normal), "Unknown completeness stays eligible"
    included = fixture.search.search("needle", retrieval_view="case_overviews", include_geometry_only=True, limit=100)
    assert fixture.overviews[1]["id"] in ids(included)
    assert fixture.search.last_search_scope["eligible_objects"] == 7
    assert fixture.overviews[1]["id"] in ids(fixture.search.search("needle", limit=100))


def test_sidecar_missing_required_or_stale_or_incomplete_fails_closed(enriched, monkeypatch):
    fixture = enriched
    path = Path(fixture.context_report["path"])
    original_status = fixture.search.status
    monkeypatch.setattr(fixture.search, "status", lambda **kwargs: {
        **original_status(**kwargs), "case_context_schema": contexts.SCHEMA, "case_contexts_required": True})
    original = path.read_bytes()
    path.unlink()
    with pytest.raises(contexts.CaseContextStale, match="missing"):
        fixture.search.search("needle")
    path.write_bytes(original)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE meta SET value=? WHERE key='corpus_hash'", (json.dumps("outdated"),))
    with pytest.raises(contexts.CaseContextStale, match="stale"):
        fixture.search.search("needle")
    path.write_bytes(original)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM dependencies")
    with pytest.raises(contexts.CaseContextStale, match="stale"):
        fixture.search.search("needle")


def test_any_pending_marker_blocks_all_searches_before_backend(enriched, monkeypatch):
    fixture = enriched
    path = contexts.pending_path(fixture.store.root / "search")
    path.write_text("interrupted", encoding="utf-8")
    monkeypatch.setattr(fixture.search, "_engine_shared_keyword", lambda *_args: pytest.fail("Pending update must block QMD"))
    with pytest.raises(RuntimeError, match="pending"):
        fixture.search.search("needle")
    with pytest.raises(RuntimeError, match="pending"):
        fixture.search.search("", retrieval_view="case_overviews")


def test_context_metadata_is_bound_and_does_not_alter_authoritative_source(enriched):
    fixture = enriched
    cached = contexts.read_contexts(fixture.store.root / "search", fixture.active)
    profile = contexts.load_context(fixture.store.root / "search", fixture.active, fixture.overviews[0]["id"])
    assert cached[profile["case_id"]]["qmd_hash"] == fixture.context_profile["qmd_hash"]
    assert "qmd_hash" not in profile and profile["is_read_evidence"] is False
    with closing(fixture.store.connect()) as db:
        raw = json.loads(db.execute("SELECT payload FROM revisions WHERE id=?", (profile["case_id"],)).fetchone()[0])
    assert raw == fixture.overviews[0]


def test_context_change_or_dependency_policy_change_during_query_requires_retry(enriched, monkeypatch):
    fixture = enriched
    query = fixture.search._engine_shared_keyword
    def close_manual_after_query(request):
        result = query(request)
        fixture.store.set_enabled("manuals", False)
        return result
    monkeypatch.setattr(fixture.search, "_engine_shared_keyword", close_manual_after_query)
    with pytest.raises(RuntimeError, match="dependency visibility changed"):
        fixture.search.search("needle", library_ids=["models"])


def test_python_rejects_bridge_hit_from_an_unavailable_dependency(enriched, monkeypatch):
    fixture = enriched
    fixture.store.set_enabled("manuals", False)
    monkeypatch.setattr(fixture.search, "_engine_shared_keyword", lambda _request: [
        {"filepath": "qmd://" + qmd_path(fixture.overviews[0]), "score": 1.0, "source": "fts"}])
    with pytest.raises(RuntimeError, match="authoritative retrieval scope"):
        fixture.search.search("needle", library_ids=["models"], limit=1)


def test_metadata_replacement_while_qmd_runs_requires_retry(enriched, monkeypatch):
    fixture = enriched
    query = fixture.search._engine_shared_keyword
    def change_metadata_after_query(request):
        result = query(request)
        with closing(sqlite3.connect(fixture.context_report["path"])) as db, db:
            db.execute("UPDATE meta SET value=? WHERE key='source_text_is_read_evidence'", (json.dumps("changed"),))
        return result
    monkeypatch.setattr(fixture.search, "_engine_shared_keyword", change_metadata_after_query)
    with pytest.raises(RuntimeError, match="context metadata changed"):
        fixture.search.search("needle", library_ids=["models"])


def test_atomic_metadata_failure_preserves_previous_profiles(enriched, monkeypatch):
    fixture = enriched
    path = Path(fixture.context_report["path"])
    previous = path.read_bytes()
    def fail_replace(*_args):
        raise OSError("injected sidecar publication failure")
    monkeypatch.setattr(contexts.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publication failure"):
        contexts.write_contexts(fixture.store.root / "search", fixture.active, [fixture.context_profile], {})
    assert path.read_bytes() == previous
    assert list(path.parent.glob(contexts.FILENAME + ".*.tmp")) == []


def test_no_context_sidecar_preserves_historical_scope(navigation):
    fixture = navigation
    assert contexts.validated_contexts(fixture.store.root / "search", fixture.search._load_active(summary=True)) is None
    assert fixture.overviews[1]["id"] in ids(fixture.search.search("needle", retrieval_view="case_overviews", limit=100))
    assert fixture.search.search("needle", limit=1)[0]["type"] == "saved_configuration"
