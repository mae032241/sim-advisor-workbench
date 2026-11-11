"""Large imports keep bounded live progress and complete final diagnostics."""
from contextlib import closing
import os
from types import SimpleNamespace

import pytest

from advisor.library_store import LibraryStore, digest, now


def manual_fixture(tmp_path, count=201):
    source = tmp_path / "manuals"
    source.mkdir()
    for index in range(count):
        (source / f"{index:04}.txt").write_text(f"Documented mesh procedure {index}.", encoding="utf-8")
    store = LibraryStore(tmp_path / "libraries")
    library = store.register("Manuals", "official-manuals", [{"id": "docs", "path": str(source)}])
    library = store.set_enabled(library["id"], True)
    return store, library


def test_large_import_checkpoints_progress_and_retains_every_final_item(tmp_path, monkeypatch):
    from advisor import library_text
    store, library = manual_fixture(tmp_path)
    clock = [0.0]
    monkeypatch.setattr("advisor.library_store.time", SimpleNamespace(monotonic=lambda: clock[0]))
    extract = library_text.extract_text_objects
    observed = []

    def paced_extract(*args, **kwargs):
        clock[0] += .04
        return extract(*args, **kwargs)

    save = store._save_job

    def record_save(job):
        save(job)
        observed.append((clock[0], job["stage"], job.get("completed")))

    monkeypatch.setattr(library_text, "extract_text_objects", paced_extract)
    monkeypatch.setattr(store, "_save_job", record_save)
    result = store.build(library["id"], modes=[])
    checkpoints = [item for item in observed if item[1] == "extracting"]
    assert len(checkpoints) < 10  # Previously two growing JSON writes per file.
    assert any(0 < completed < 201 for _, _, completed in checkpoints)
    assert result["status"] == "completed" and result["completed"] == 201
    assert len(result["items"]) == len(store.job(result["id"])["items"]) == 201
    summary = store.job_summary(result["id"])
    assert summary["items_total"] == 201 and summary["items_truncated"] is True
    assert summary["counts"] == {"succeeded": 201, "partial": 0, "failed": 0}
    assert [item["path"] for item in summary["items"]] == [f"{index:04}.txt" for index in range(193, 201)]


def test_cancel_is_observed_between_files_even_when_progress_write_is_throttled(tmp_path, monkeypatch):
    from advisor import library_text
    store, library = manual_fixture(tmp_path, 101)
    monkeypatch.setattr("advisor.library_store.time", SimpleNamespace(monotonic=lambda: 1.0))
    extract = library_text.extract_text_objects
    attempted = []

    def cancel_on_third(path, **kwargs):
        attempted.append(path)
        if len(attempted) == 3:
            job = store.jobs_summary()[0]
            assert store.cancel_build(job["id"])["status"] == "cancel_requested"
        return extract(path, **kwargs)

    monkeypatch.setattr(library_text, "extract_text_objects", cancel_on_third)
    result = store.build(library["id"], modes=[])
    assert result["status"] == "cancelled" and result["cancelled_stage"] == "extracting"
    assert len(attempted) == 3 and len(result["items"]) == 2
    assert store.job(result["id"])["counts"] == {"succeeded": 2, "partial": 0, "failed": 0}
    assert not store.has_objects() and not (store.root / ".build.lock").exists()


@pytest.mark.parametrize("stale_owner", [False, True])
def test_summary_preview_preserves_full_counts_and_stale_owner_diagnostics(tmp_path, monkeypatch, stale_owner):
    store = LibraryStore(tmp_path / "libraries")
    items = [{"path": str(index), "coverage": {"deep_api": {"status": "partial" if index == 0 else "observed",
             "read_contract": "fixture/v1", "read_errors": 4 if index == 0 else 0}}} for index in range(20)]
    job = {"id": "a" * 32, "status": "running" if stale_owner else "partial", "stage": "extracting",
           "pid": os.getpid(), "started_at": now(), "items": items, "failed": [],
           "indexes": {"mapping": {"retained": "full diagnostic"}}}
    store._save_job(job)
    monkeypatch.setattr("advisor.library_store.process_alive", lambda _pid: not stale_owner)
    summary = store.job_summary(job["id"])
    assert summary["status"] == ("interrupted" if stale_owner else "partial")
    assert summary["counts"] == {"succeeded": 19, "partial": 1, "failed": 0}
    assert summary["coverage_summary"]["properties_unresolved"] == 0
    assert summary["partial_extractions"] == 1 and len(summary["items"]) == 8
    assert summary["items_total"] == 20 and summary["items_truncated"] is True
    full = store.job(job["id"])
    assert len(full["items"]) == 20 and "items_truncated" not in full
    assert full["indexes"] == job["indexes"]


def test_construction_without_indexes_never_decodes_other_libraries(tmp_path):
    store, library = manual_fixture(tmp_path, 1)
    # Deliberately unreadable payload makes accidental all-library hydration
    # observable; citation identities remain sufficient to form the generation.
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", ("foreign-citation", "foreign-object", "foreign-library", "unreadable fixture"))
        db.execute("INSERT INTO active VALUES(?,?,?)", ("foreign-library", "foreign-object", "foreign-citation"))
    result = store.build(library["id"], modes=[])
    own = store.objects([library["id"]])
    assert result["status"] == "completed" and len(own) == 1
    assert result["generation"] == digest(sorted(["foreign-citation", own[0]["id"]]))[:24]
    assert result["failed_indexes"] == [] and "indexes" not in result
