"""Lifecycle changes inspect active ownership without loading build history."""
from contextlib import closing
import json
import os

import pytest

from advisor.library_store import LibraryLifecycleConflict, LibraryStore, encoded


def fixture(tmp_path):
    source = tmp_path / "sources"
    source.mkdir()
    store = LibraryStore(tmp_path / "libraries")
    library = store.register("Lifecycle fixture", "official-manuals", [{"id": "docs", "path": str(source)}], library_id="fixture")
    library = store.set_enabled(library["id"], True)
    return store, library


def insert_job(store, job):
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO builds VALUES(?,?)", (job["id"], encoded(job)))


def test_completed_bulk_history_is_not_hydrated_for_disable_or_restore(tmp_path, monkeypatch):
    store, library = fixture(tmp_path)
    items = [{"path": f"item-{number}", "diagnostic": "large retained diagnostics " * 80} for number in range(100)]
    for number in range(30):
        insert_job(store, {"id": f"completed-{number}", "library_id": library["id"], "status": "completed", "items": items})
    monkeypatch.setattr(store, "jobs", lambda *args, **kwargs: pytest.fail("Lifecycle must not fetch recent full job history"))
    monkeypatch.setattr(store, "_job_view", lambda *args, **kwargs: pytest.fail("Completed items must not be decoded/recounted"))
    assert store.set_enabled(library["id"], False)["enabled"] is False
    assert store.delete_library(library["id"])["deleted_at"]
    assert store.restore_library(library["id"])["deleted_at"] is None
    with closing(store.connect()) as db:
        assert db.execute("SELECT count(*) FROM builds").fetchone()[0] == 30
        query = "SELECT id FROM builds WHERE json_extract(body,'$.status') IN ('queued','running','cancel_requested')"
        assert any("USING INDEX builds_status" in row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query))


@pytest.mark.parametrize("pid", [None, "live"])
def test_live_or_unassigned_active_owner_blocks_without_hydrating_its_large_items(tmp_path, monkeypatch, pid):
    store, library = fixture(tmp_path)
    insert_job(store, {"id": "active", "library_ids": ["another", library["id"]], "scope": "selected", "kind": "reindex",
                       "status": "queued" if pid is None else "running", "pid": os.getpid() if pid else None,
                       "workspace_root": str(store.paths.root), "items": [{"large": "x" * 100000}]})
    monkeypatch.setattr(store, "job", lambda *args, **kwargs: pytest.fail("Live/unassigned job headers are sufficient"))
    with pytest.raises(LibraryLifecycleConflict, match="active"):
        store.set_enabled(library["id"], False)
    assert store.library(library["id"])["enabled"] is True


def test_stale_active_owner_recovers_full_diagnostics_before_lifecycle_change(tmp_path, monkeypatch):
    store, library = fixture(tmp_path)
    items = [{"path": f"item-{number}", "detail": "Preserve every diagnostic"} for number in range(60)]
    insert_job(store, {"id": "stale", "library_id": library["id"], "status": "running", "pid": 123,
                       "workspace_root": str(store.paths.root), "items": items, "failed": []})
    monkeypatch.setattr("advisor.library_store.process_alive", lambda pid: False)
    assert store.set_enabled(library["id"], False)["enabled"] is False
    with closing(store.connect()) as db:
        saved = json.loads(db.execute("SELECT body FROM builds WHERE id='stale'").fetchone()[0])
    assert saved["status"] == "interrupted"
    assert [(item["path"], item["detail"]) for item in saved["items"]] == [(item["path"], item["detail"]) for item in items]
    assert saved["counts"]["succeeded"] == len(items)


def test_new_active_owner_between_recovery_and_write_still_blocks_transactionally(tmp_path, monkeypatch):
    store, library = fixture(tmp_path)
    def new_owner():
        insert_job(store, {"id": "racing-worker", "library_id": library["id"], "status": "running", "pid": os.getpid(),
                           "workspace_root": str(store.paths.root)})
    monkeypatch.setattr(store, "_recover_active_job_owners", new_owner)
    with pytest.raises(LibraryLifecycleConflict, match="racing-worker"):
        store.set_enabled(library["id"], False)
    assert store.library(library["id"])["enabled"] is True
