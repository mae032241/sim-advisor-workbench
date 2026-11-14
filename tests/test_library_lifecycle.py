"""Recoverable registry state and content-free default model enumeration."""
from contextlib import closing
import json
import os
from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from advisor.library_store import LibraryLifecycleConflict, LibraryStore, encoded, file_hash, now


def registered(tmp_path, kind="official-manuals", count=1):
    sources = tmp_path / "sources"
    sources.mkdir()
    suffix = ".txt" if kind == "official-manuals" else ".mph"
    for i in range(count):
        (sources / f"source-{i:02}{suffix}").write_text(f"Synthetic content {i}", encoding="utf-8")
    store = LibraryStore(tmp_path / "data/libraries")
    library = store.register("Fixture", kind, [{"id": "models", "path": str(sources), "role": "models"}])
    library = store.set_enabled(library["id"], True)
    return store, library, sources


def body(store, library_id):
    with closing(store.connect()) as db:
        return json.loads(db.execute("SELECT body FROM libraries WHERE id=?", (library_id,)).fetchone()[0])


def save_body(store, value):
    with closing(store.connect()) as db, db:
        db.execute("UPDATE libraries SET body=? WHERE id=?", (encoded(value), value["id"]))


def test_legacy_defaults_and_strict_searchable_visibility(tmp_path):
    store, library, _ = registered(tmp_path)
    value = body(store, library["id"])
    value.pop("enabled")
    value.pop("deleted_at")
    save_body(store, value)
    assert store.library(library["id"])["enabled"] is True
    assert store.library(library["id"])["deleted_at"] is None
    assert store.searchable_library_ids() == [library["id"]]
    assert store.searchable_library_ids([]) == []
    assert store.searchable_library_ids(["unknown", library["id"], library["id"]]) == [library["id"]]
    for wrong in ("id", {library["id"]}, [4]):
        with pytest.raises(ValueError):
            store.searchable_library_ids(wrong)
    value["enabled"] = "false"
    save_body(store, value)
    with pytest.raises(ValueError, match="lifecycle"):
        store.searchable_library_ids()


def test_disable_delete_restore_keep_every_immutable_table_and_source(tmp_path):
    store, library, sources = registered(tmp_path)
    assert store.build(library["id"], modes=[])["status"] == "completed"
    original = store.objects()
    citation = original[0]["id"]
    generation = store.current_generation()
    marker = store.root / "search/generations/kept/vector-cache.bin"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"Owned embedding cache is retained unchanged")
    cache = store.root / "artifacts/case/extraction.json"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"Existing extraction cache")
    with closing(store.connect()) as db:
        saved = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)] for table in ("revisions", "active", "case_members")}
    for operation in (lambda: store.set_enabled(library["id"], False), lambda: store.delete_library(library["id"]),
                      lambda: store.restore_library(library["id"]), lambda: store.set_enabled(library["id"], True)):
        with patch("advisor.library_store.file_hash", side_effect=AssertionError("Lifecycle must not read/rehash source or rebuild indexes")), patch.object(store, "build", side_effect=AssertionError("No constructor")):
            operation()
        assert store.objects() == original
        assert store.current_generation() == generation
        assert store.get(citation) == original[0]
        if store.source_available(citation):
            assert store.read_evidence(citation)["source_hash_verified"]
        else:
            with pytest.raises(PermissionError, match="关闭|删除"):
                store.read_evidence(citation)
        if store.source_available(citation):
            assert store.related_sources(citation) == []
        else:
            with pytest.raises(PermissionError, match="关闭|删除"):
                store.related_sources(citation)
        with closing(store.connect()) as db:
            assert {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)] for table in saved} == saved
        assert marker.read_bytes() == b"Owned embedding cache is retained unchanged"
        assert cache.read_bytes() == b"Existing extraction cache"
        assert (sources / "source-00.txt").read_text(encoding="utf-8") == "Synthetic content 0"


def test_deleted_is_hidden_restored_disabled_and_cannot_build(tmp_path):
    store, library, _ = registered(tmp_path)
    lid = library["id"]
    for wrong in (0, 1, "false", None):
        with pytest.raises(ValueError, match="boolean"):
            store.set_enabled(lid, wrong)
    store.set_enabled(lid, False)
    assert store.searchable_library_ids() == []
    with patch.object(store, "_save_job", side_effect=AssertionError("Disabled library must not queue/start")):
        with pytest.raises(ValueError, match="关闭"):
            store.build(lid)
        with pytest.raises(ValueError, match="关闭"):
            store.start_build(lid)
    deleted = store.delete_library(lid)
    assert deleted["enabled"] is False and deleted["deleted_at"]
    assert store.libraries() == []
    assert store.libraries(include_deleted=True)[0]["id"] == lid
    assert store.library(lid, include_deleted=True)["deleted_at"] == deleted["deleted_at"]
    assert store.delete_library(lid)["deleted_at"] == deleted["deleted_at"]
    for operation in (lambda: store.library(lid), lambda: store.set_enabled(lid, True), lambda: store.build(lid), lambda: store.start_build(lid), lambda: store.model_candidates(lid)):
        with pytest.raises(FileNotFoundError):
            operation()
    restored = store.restore_library(lid)
    assert restored["deleted_at"] is None and restored["enabled"] is False
    assert store.searchable_library_ids() == []
    assert store.set_enabled(lid, True)["enabled"] is True
    assert store.searchable_library_ids() == [lid]


@pytest.mark.parametrize("status", ["queued", "running", "cancel_requested"])
@pytest.mark.parametrize("operation", ["disable", "delete", "restore"])
def test_active_library_jobs_reject_lifecycle_until_finished(tmp_path, status, operation):
    store, library, _ = registered(tmp_path)
    job = {"id": "a"*32, "library_id": library["id"], "status": status, "pid": os.getpid(), "started_at": now()}
    store._save_job(job)
    with pytest.raises(LibraryLifecycleConflict, match="中断"):
        {"disable": lambda: store.set_enabled(library["id"], False), "delete": lambda: store.delete_library(library["id"]),
         "restore": lambda: store.restore_library(library["id"])}[operation]()
    assert store.library(library["id"])["enabled"] is True
    job["status"] = "cancelled"
    store._save_job(job)
    assert store.set_enabled(library["id"], False)["enabled"] is False


@pytest.mark.parametrize("global_scope", [False, True])
def test_covering_index_jobs_block_but_other_library_does_not(tmp_path, global_scope):
    store, library, _ = registered(tmp_path)
    lid = library["id"]
    store._save_job({"id": "other", "library_id": "another", "status": "running", "pid": os.getpid()})
    assert store.set_enabled(lid, False)["enabled"] is False
    store._save_job({"id": "index", "library_id": None, "library_ids": [] if global_scope else [lid],
                     "kind": "reindex", "scope": "all_libraries" if global_scope else "selected", "status": "running", "pid": os.getpid()})
    with pytest.raises(LibraryLifecycleConflict):
        store.delete_library(lid)


def test_constructor_final_commit_preserves_latest_lifecycle_body(tmp_path):
    store, library, _ = registered(tmp_path, "user-models")
    lid = library["id"]
    def extract(path, **_):
        # Simulate a legacy writer racing with a newer worker. Public lifecycle
        # methods reject this while active, but publication must never revive it.
        latest = body(store, lid)
        latest.update(enabled=False, deleted_at="2026-11-08T12:00:00+00:00", name="Concurrent new registry name")
        save_body(store, latest)
        return {"objects": [{"local_id": "case", "type": "model_case", "title": "Fixture", "text": "Original observations",
                             "evidence": [{"asset_path": str(path), "method": "fixture"}]}]}
    with patch("advisor.library_model.extract_case", side_effect=extract):
        job = store.build(lid, modes=[])
    assert job["status"] == "completed", job
    latest = store.library(lid, include_deleted=True)
    assert latest["enabled"] is False and latest["deleted_at"]
    assert latest["name"] == "Concurrent new registry name"
    assert latest["generation"] == store.current_generation()
    assert store.objects([lid])


def test_rebind_cannot_overwrite_lifecycle_changes_during_hashing(tmp_path):
    store, library, sources = registered(tmp_path)
    store.build(library["id"], modes=[])
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "source-00.txt").write_text("Synthetic content 0", encoding="utf-8")
    def hash_and_disable(path):
        store.set_enabled(library["id"], False)
        return file_hash(path)
    with patch("advisor.library_store.file_hash", side_effect=hash_and_disable):
        result = store.rebind(library["id"], "models", replacement)
    assert result["enabled"] is False
    assert store.library(library["id"])["enabled"] is False


def test_global_reindex_includes_disabled_and_soft_deleted_registry_ids(tmp_path):
    store, first, sources = registered(tmp_path)
    second = store.register("Second", "official-manuals", [{"path": str(sources)}])
    second = store.set_enabled(second["id"], True)
    store.set_enabled(first["id"], False)
    store.delete_library(second["id"])
    class Index:
        def __init__(self, *_):
            pass
        def build(self, objects, **_):
            return {"modes": {}}
    with patch("advisor.library_search.LibrarySearch", Index):
        result = store.reindex(["qmd_keyword"])
    job = store.job(result["job_id"])
    assert set(job["library_ids"]) == {first["id"], second["id"]}


def insert_model_case(store, library, local_id, relative):
    obj = {"type": "model_case", "asset_key": "models:" + relative, "evidence": []}
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (local_id, local_id, library["id"], encoded(obj)))
        db.execute("INSERT INTO active VALUES(?,?,?)", (library["id"], local_id, local_id))


def test_model_candidates_default_all_directory_scope_without_reading_case_payloads(tmp_path):
    store, library, sources = registered(tmp_path, "official-models", count=17)
    insert_model_case(store, library, "case-built", "source-16.mph")
    insert_model_case(store, library, "case-missing", "no-longer-present.mph")
    outside = tmp_path / "outside.mph"
    outside.write_bytes(b"Do not include outside registry")
    insert_model_case(store, library, "case-outside", "../outside.mph")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "attachment.mph").write_bytes(b"A documentation attachment is not a model-root candidate")
    value = body(store, library["id"])
    value["roots"].append({"id": "docs", "role": "docs", "path": str(docs)})
    save_body(store, value)
    with patch("advisor.library_store.file_hash", side_effect=AssertionError("No content hashing")), patch.object(store, "scan", side_effect=AssertionError("No fingerprint scan")), patch.object(store, "objects", side_effect=AssertionError("Do not load large model payloads")), patch("advisor.library_model.extract_case", side_effect=AssertionError("No extraction")):
        all_models = store.model_candidates(library["id"])
        limited = store.model_candidates(library["id"], limit=3)
    assert all_models["limit"] is None and all_models["has_more"] is False
    assert len(all_models["files"]) == 17
    assert all_models["files"][0]["relative_path"] == "source-00.mph"
    assert all_models["status"] == "ready" and not all_models["missing_roots"]
    assert len(limited["files"]) == 3 and limited["has_more"] is True
    assert limited["files"][0] == all_models["files"][0]
    assert all(Path(item["path"]).parent == sources for item in all_models["files"])
    store.set_enabled(library["id"], False)
    assert len(store.model_candidates(library["id"])["files"]) == 17


def test_candidates_offline_empty_manual_and_limit_validation(tmp_path):
    store, library, sources = registered(tmp_path, "user-models")
    for wrong in (0, -1, True, "10"):
        with pytest.raises(ValueError):
            store.model_candidates(library["id"], wrong)
    (sources / "source-00.mph").unlink()
    assert store.model_candidates(library["id"])["status"] == "empty"
    sources.rmdir()
    state = store.model_candidates(library["id"])
    assert state["status"] == "offline" and not state["files"]
    assert state["missing_roots"] == [{"root_id": "models", "path": str(sources)}]
    sources.mkdir()
    manuals = store.register("Manuals", "official-manuals", [{"path": str(sources)}])
    manuals = store.set_enabled(manuals["id"], True)
    with patch("advisor.library_store.os.walk", side_effect=AssertionError("Manual library must not enumerate models")):
        assert store.model_candidates(manuals["id"])["status"] == "not_applicable"


def test_candidate_directory_errors_are_partial_not_false_complete(tmp_path):
    store, library, _ = registered(tmp_path, "user-models")
    def failed_walk(path, *, onerror, **_):
        onerror(PermissionError("Fixture enumeration denied"))
        return iter([])
    with patch("advisor.library_store.os.walk", failed_walk):
        result = store.model_candidates(library["id"])
    assert result["status"] == "partial"
    assert result["missing_roots"][0]["reason"] == "directory_enumeration_failed"


def test_delete_during_rebind_verification_cannot_restore_old_body(tmp_path):
    store, library, sources = registered(tmp_path)
    store.build(library["id"], modes=[])
    def delete_and_hash(path):
        store.delete_library(library["id"])
        return file_hash(path)
    with patch("advisor.library_store.file_hash", side_effect=delete_and_hash):
        with pytest.raises(FileNotFoundError, match="deleted"):
            store.rebind(library["id"], "models", sources)
    assert store.library(library["id"], include_deleted=True)["enabled"] is False
    assert store.library(library["id"], include_deleted=True)["deleted_at"]


def test_disable_winning_queue_race_leaves_failed_job_not_permanent_queue(tmp_path):
    store, library, _ = registered(tmp_path, "user-models")
    original = store._save_job
    completed = threading.Event()
    def save(job):
        if job["status"] == "queued":
            store.set_enabled(library["id"], False)
        original(job)
        if job["status"] == "failed":
            completed.set()
    with patch.object(store, "_save_job", side_effect=save), patch("advisor.library_model.extract_case", side_effect=AssertionError("Disabled worker must not extract")):
        queued = store.start_build(library["id"], modes=[])
        assert completed.wait(3)
        assert store.job(queued["id"])["status"] == "failed"
    assert store.library(library["id"])["enabled"] is False


def test_running_constructor_lifecycle_conflict_is_real_and_does_not_cancel_worker(tmp_path):
    store, library, _ = registered(tmp_path, "user-models")
    started, release = threading.Event(), threading.Event()
    def extract(path, **_):
        started.set()
        assert release.wait(3)
        return {"objects": [{"local_id": "case", "type": "model_case", "title": "Fixture", "text": "Observed fixture",
                             "evidence": [{"asset_path": str(path), "method": "fixture"}]}]}
    result = []
    with patch("advisor.library_model.extract_case", side_effect=extract):
        worker = threading.Thread(target=lambda: result.append(store.build(library["id"], modes=[])))
        worker.start()
        try:
            assert started.wait(3)
            with pytest.raises(LibraryLifecycleConflict):
                store.set_enabled(library["id"], False)
            assert store.library(library["id"])["enabled"] is True
        finally:
            release.set()
            worker.join(5)
    assert result[0]["status"] == "completed"
    assert store.delete_library(library["id"])["deleted_at"]
