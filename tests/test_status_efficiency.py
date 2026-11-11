"""Status views avoid document bodies and retrieval manifests on hot paths."""
import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from advisor.library_search import LibrarySearch
from advisor.library_store import LibraryStore, digest, encoded, now
from advisor.server import App
from advisor.ui_status import status_summary
from test_library_search import FakeEngineSearch, obj


def test_generation_uses_active_citation_ids_without_loading_any_document_body(tmp_path, monkeypatch):
    store = LibraryStore(tmp_path / "libraries")
    ids = ["lo-c", "lo-a", "lo-b"]
    with closing(store.connect()) as db, db:
        for identity in ids:
            value = {"id": identity, "library_id": "fixture", "text": "body" * 10000}
            db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (identity, identity, "fixture", encoded(value)))
            db.execute("INSERT INTO active VALUES(?,?,?)", ("fixture", identity, identity))
    monkeypatch.setattr(store, "objects", Mock(side_effect=AssertionError("Status must not load full objects")))
    assert store.current_generation() == digest(sorted(ids))[:24]
    with closing(store.connect()) as db, db:
        db.execute("DELETE FROM active WHERE citation_id=?", (ids[0],))
    assert store.current_generation() == digest(sorted(ids[1:]))[:24]
    assert store.has_objects(["fixture"]) and store.has_objects()
    assert not store.has_objects(["unknown"]) and not store.has_objects([])


def test_summary_manifest_is_pruned_before_locator_resolution_and_full_copy(tmp_path, monkeypatch):
    import advisor.library_search as module
    search = FakeEngineSearch(tmp_path / "search")
    search.build([obj("one")], modes=["native"], generation="fixture")
    for path in (search.active_path, search.last_build_path):
        value = json.loads(path.read_text(encoding="utf-8"))
        value["representation"] = {"one": "unused-index-body" * 10000}
        path.write_text(json.dumps(value), encoding="utf-8")
    original = {path: path.read_bytes() for path in (search.active_path, search.last_build_path)}
    full = search.status()
    resolve = module.resolve_index_manifest
    resolved = []
    def inspect(value, root):
        assert "mapping" not in value and "representation" not in value
        resolved.append(value)
        return resolve(value, root)
    monkeypatch.setattr(module, "resolve_index_manifest", inspect)
    compact = search.status(summary=True)
    assert len(resolved) == 2
    assert compact == status_summary(full)
    assert all(path.read_bytes() == body for path, body in original.items())


@pytest.mark.parametrize("stale_owner", [False, True])
def test_job_projection_keeps_bulk_out_of_decode_and_preserves_full_persisted_job(tmp_path, monkeypatch, stale_owner):
    import advisor.library_store as module
    store = LibraryStore(tmp_path / "libraries")
    job = {"id": "a" * 32, "library_id": "fixture", "status": "running" if stale_owner else "completed",
           "stage": "indexing", "pid": 1234, "started_at": now(), "completed": 1, "total": 1,
           "items": [], "failed": [], "indexes": {"generation": "keep", "modes": {"native": {"ready": True}},
               "mapping": {"x": "body" * 20000}, "representation": {"x": "index" * 20000},
               "last_build": {"mapping": {"x": "nested"}, "representation": {"x": "nested"}}}}
    store._save_job(job)
    monkeypatch.setattr(module, "process_alive", lambda _pid: not stale_owner)
    import sqlite3
    with sqlite3.connect(store.db_path) as db:
        projected = db.execute(f"SELECT {store._job_body_sql(True)} FROM builds").fetchone()[0]
    assert "mapping" not in projected and "representation" not in projected
    view = store.jobs_summary()[0]
    assert view["status"] == ("interrupted" if stale_owner else "completed")
    assert view["indexes"]["generation"] == "keep" and "mapping" not in view["indexes"]
    full = store.job(job["id"])
    assert full["indexes"] == job["indexes"]
    assert store.job_summary(job["id"]) == status_summary(full)


def test_app_summary_selects_the_early_summary_provider_without_full_status(monkeypatch):
    from advisor import server
    kb = SimpleNamespace(status=Mock(side_effect=AssertionError("Full manifest not needed")),
                         status_summary=Mock(return_value={"ready": True, "library_indexes": {"ready": True}}))
    app = SimpleNamespace(kb=kb, agents=SimpleNamespace(backend_status=lambda: {"mode": "disabled"}),
                          sessions=SimpleNamespace(list=lambda: []))
    monkeypatch.setattr(server, "parser_status", lambda: {})
    monkeypatch.setattr(server.config, "examples", lambda: [])
    result = App.status(app, summary=True)
    assert result["corpus"]["library_indexes"]["ready"]
    kb.status.assert_not_called()
