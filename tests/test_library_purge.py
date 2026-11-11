"""Physical registered-library cleanup; all source trees are isolated fixtures."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from advisor.library_store import LibraryLifecycleConflict, LibraryStore, digest, encoded, file_hash, now
from advisor.library_search import LibrarySearch
from advisor.server import make_server
from test_library_lifecycle_api import request


def fake_extract(path, *, artifact_dir, **_):
    fingerprint = file_hash(path)
    output = artifact_dir / ("mph-" + fingerprint[:20])
    output.mkdir(parents=True, exist_ok=True)
    shared = output / "saved-state.json"
    shared.write_text(json.dumps({"nodes": [{"path": "Model/Study", "type": "fixture"}]}), encoding="utf-8")
    unique = output / "unique.json"
    unique.write_text(json.dumps({"text": "PURGE_" + path.stem.upper() + "_ONLY_SECRET"}), encoding="utf-8")
    code = output / "export.java"
    code.write_text('// Source fixture ' + path.stem, encoding="utf-8")
    return {"model_snapshot": {"path": str(path), "fingerprint": fingerprint}, "objects": [
        {"local_id": "model-case", "type": "model_case", "title": path.stem, "text": "PURGE_" + path.stem.upper() + "_ONLY_SECRET",
         "data": {}, "evidence": [{"asset_path": str(path), "method": "fixture"},
                                    {"asset_path": str(shared), "method": "native_mph_xml_snapshot"},
                                    {"asset_path": str(unique), "method": "fixture"}]},
        {"local_id": "code", "type": "implementation_snippet", "title": "code", "text": "code " + path.stem,
         "data": {"feature_branch": "mesh"}, "kind": "code", "evidence": [{"asset_path": str(code), "method": "fixture"}]}],
        "assets": [{"path": str(path)}, {"path": str(shared)}, {"path": str(unique)}, {"path": str(code)}]}


def fixture(tmp_path, *, modes=()):
    root = tmp_path / "data/libraries"
    store = LibraryStore(root)
    libraries = []
    for name in ("a", "b"):
        source = tmp_path / ("sources-" + name)
        source.mkdir()
        (source / (name + ".mph")).write_bytes(("original read-only " + name).encode())
        libraries.append(store.register(name, "user-models", [{"id": "models", "path": str(source)}]))
        store.set_enabled(libraries[-1]["id"], True)
    with patch("advisor.library_model.extract_case", side_effect=fake_extract), patch.object(LibrarySearch, "_engine", side_effect=AssertionError("No embedding/Node operation")):
        for lib in libraries:
            assert store.build(lib["id"], deep=False, modes=list(modes))["status"] == "completed"
    return store, libraries[0], libraries[1]


def target_artifacts(store, lid):
    return [p.parent for p in (store.root / "artifacts").glob("*/library-owner.json") if json.loads(p.read_text(encoding="utf-8"))["library_id"] == lid]


def test_strict_confirmation_and_soft_deleted_precondition(tmp_path):
    store, first, _ = fixture(tmp_path)
    for value in (False, None, 1, "true", [], {}):
        with pytest.raises(ValueError, match="确认"):
            store.purge_library(first["id"], confirmed=value)
    with pytest.raises(ValueError, match="已删除"):
        store.purge_library(first["id"], confirmed=True)
    assert store.library(first["id"])["enabled"] is True


def test_purge_removes_catalog_private_caches_and_exclusive_blobs_keeps_shared(tmp_path):
    store, first, second = fixture(tmp_path)
    own = store.objects([first["id"]])
    other = store.objects([second["id"]])
    own_blobs = {e["relative_path"] for obj in own for e in obj["evidence"] if e["root_id"] == "derived"}
    other_blobs = {e["relative_path"] for obj in other for e in obj["evidence"] if e["root_id"] == "derived"}
    assert own_blobs & other_blobs
    private = target_artifacts(store, first["id"])
    other_dirs = target_artifacts(store, second["id"])
    originals = {p: p.read_bytes() for lib in (first, second) for p in Path(lib["roots"][0]["path"]).iterdir()}
    own_ids = {obj["id"] for obj in own}
    for job in store.jobs():
        if job.get("library_id") == first["id"]:
            marker = store._cancel_path(job["id"])
            marker.parent.mkdir(exist_ok=True)
            marker.touch()
    key = "lib_" + hashlib.sha256(first["id"].encode()).hexdigest()[:20]
    shared_job = {"id": "shared-history", "library_id": None, "library_ids": [first["id"], second["id"]], "kind": "reindex", "status": "completed",
                  "indexes": {"libraries": {first["id"]: {"key": key}, second["id"]: {"key": "keep"}},
                              "mapping": {key + "/x.md": own[0]["id"], "keep/y.md": other[0]["id"]},
                              "representation": {"objects": {own[0]["id"]: {"chars": 1}, other[0]["id"]: {"chars": 2}}}},
                  "note": "Historical free text mentioning " + first["id"]}
    legacy_indexes = shared_job["indexes"]
    store._save_job(shared_job)
    # Simulate history created before bulk diagnostics became references.
    shared_job["indexes"] = legacy_indexes
    with closing(store.connect()) as db, db:
        db.execute("UPDATE builds SET body=? WHERE id=?", (encoded(shared_job), shared_job["id"]))
    store.delete_library(first["id"])
    result = store.purge_library(first["id"], confirmed=True)
    assert result["status"] == "purged" and result["original_sources"] == "untouched"
    assert result["counts"]["revisions"] == len(own)
    assert result["counts"]["case_members"] == len(own)
    assert result["counts"]["shared_blobs_retained"] == len(own_blobs & other_blobs)
    assert all(not path.exists() for path in private)
    assert all(path.is_dir() for path in other_dirs)
    assert all(not (store.root / p).exists() for p in own_blobs - other_blobs)
    assert all((store.root / p).is_file() for p in other_blobs)
    assert store.objects() == other
    for citation in own_ids:
        assert store.get(citation) is None
        with pytest.raises(FileNotFoundError):
            store.read_evidence(citation)
        with pytest.raises(FileNotFoundError):
            store.related_sources(citation)
        with pytest.raises(FileNotFoundError):
            store.configuration(citation)
    with pytest.raises(FileNotFoundError):
        store.restore_library(first["id"])
    assert all(path.read_bytes() == content for path, content in originals.items())
    retained_job = store.job("shared-history")
    assert retained_job["library_ids"] == [second["id"]]
    assert set(retained_job["indexes"]["libraries"]) == {second["id"]}
    assert all(identity not in own_ids for identity in retained_job["indexes"]["mapping"].values())
    assert set(retained_job["indexes"]["representation"]["objects"]) == {other[0]["id"]}
    assert first["id"] in retained_job["note"]  # Shared audit prose is not globally replaced.
    assert all(job.get("library_id") != first["id"] for job in store.jobs())
    assert not list((store.root / "build-control").glob("*.cancel"))
    assert result["catalog_compacted"] is True


def test_real_native_current_and_historical_cleanup_keeps_other_qmd_file_bytes(tmp_path):
    store, first, second = fixture(tmp_path, modes=("native",))
    before = store.objects([second["id"]])
    others = {}
    target_dirs = []
    for manifest_path in (store.root / "search/generations").glob("*/manifest.json"):
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        for lid, item in value["libraries"].items():
            directory = manifest_path.parent / item["key"]
            directory.mkdir(parents=True, exist_ok=True)
            for name in ("qmd-keyword.sqlite3", "qmd-vector.sqlite3"):
                file = directory / name
                data = ("Synthetic vector storage fixture " + lid + name).encode()
                file.write_bytes(data)
                if lid == second["id"]:
                    others[file] = data
                else:
                    target_dirs.append(directory)
    store.delete_library(first["id"])
    with patch.object(LibrarySearch, "_engine", side_effect=AssertionError("Purge must not invoke QMD/embedding")):
        result = store.purge_library(first["id"], confirmed=True)
    assert result["indexes"]["native_objects_removed"] >= 2
    assert result["indexes"]["embedding_invoked"] is False
    assert all(file.read_bytes() == data for file, data in others.items())
    assert all(not path.exists() for path in target_dirs)
    assert store.objects() == before
    assert store.index_status()["modes"]["native"]["ready"] is True
    for native in (store.root / "search/generations").glob("*/objects.sqlite3"):
        assert b"PURGE_A_ONLY_SECRET" not in native.read_bytes()
    assert b"PURGE_A_ONLY_SECRET" not in store.db_path.read_bytes()


def test_index_failure_retains_soft_deleted_registry_then_retry_succeeds(tmp_path):
    store, first, _ = fixture(tmp_path)
    store.delete_library(first["id"])
    original = store.objects([first["id"]])
    dirs = target_artifacts(store, first["id"])
    with patch("advisor.library_index_purge.purge_library_indexes", side_effect=RuntimeError("Fixture index cleanup interrupted")):
        with pytest.raises(RuntimeError, match="interrupted"):
            store.purge_library(first["id"], confirmed=True)
    assert store.library(first["id"], include_deleted=True)["deleted_at"]
    assert store.objects([first["id"]]) == original
    assert all(path.is_dir() for path in dirs)
    assert not (store.root / ".build.lock").exists()
    assert store.purge_library(first["id"], confirmed=True)["status"] == "purged"


def test_file_failure_does_not_commit_registry_deletion_and_can_retry(tmp_path):
    store, first, _ = fixture(tmp_path, modes=("native",))
    store.delete_library(first["id"])
    # The index helper also calls rmtree. Fail only on this library's private
    # extraction directory, after actual native/index cleanup has succeeded.
    import shutil
    original = shutil.rmtree
    private = target_artifacts(store, first["id"])[0]
    def fail(path, *args, **kwargs):
        if Path(path).is_relative_to(private):
            raise PermissionError("Fixture locked extraction cache")
        return original(path, *args, **kwargs)
    with patch("advisor.library_store.shutil.rmtree", side_effect=fail):
        with pytest.raises(PermissionError, match="locked"):
            store.purge_library(first["id"], confirmed=True)
    assert store.library(first["id"], include_deleted=True)["deleted_at"]
    assert store.objects([first["id"]])
    assert store.purge_library(first["id"], confirmed=True)["status"] == "purged"


def test_failed_cache_owner_and_legacy_saved_state_are_removed_unknown_is_retained(tmp_path):
    store, first, _ = fixture(tmp_path)
    lid = first["id"]
    fingerprint = "a" * 64
    relative = "failed.mph"
    legacy = store.root / "artifacts" / digest([lid, relative, fingerprint])[:24]
    saved = legacy / ("mph-" + fingerprint[:20]) / "saved-state.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(json.dumps({"schema": "mph-saved-state/v1", "source_sha256": fingerprint}), encoding="utf-8")
    store._save_job({"id": "f"*32, "library_id": lid, "status": "failed", "failed": [{"path": relative}]})
    unknown = store.root / "artifacts" / ("0"*24)
    unknown.mkdir()
    (unknown / "unattributed.bin").write_bytes(b"Do not guess ownership")
    (unknown / "extraction.json").write_text("Damaged legacy cache with unknown owner", encoding="utf-8")
    store.delete_library(lid)
    result = store.purge_library(lid, confirmed=True)
    assert not legacy.exists()
    assert unknown.is_dir() and result["remaining_unattributed"] >= 1
    assert any("无法确定归属" in warning for warning in result["warnings"])


def test_new_failed_extraction_owner_allows_cleanup_without_any_revision(tmp_path):
    store = LibraryStore(tmp_path / "data/libraries")
    source = tmp_path / "source"
    source.mkdir()
    model = source / "broken.mph"
    model.write_bytes(b"Original failed model stays intact")
    library = store.register("Failed fixture", "user-models", [{"path": str(source)}])
    library = store.set_enabled(library["id"], True)
    with patch("advisor.library_model.extract_case", side_effect=ValueError("Fixture parse failed")):
        assert store.build(library["id"], modes=[])["status"] == "failed"
    dirs = target_artifacts(store, library["id"])
    assert len(dirs) == 1 and not store.objects()
    store.delete_library(library["id"])
    result = store.purge_library(library["id"], confirmed=True)
    assert result["counts"]["cache_directories"] == 1
    assert all(not path.exists() for path in dirs)
    assert model.read_bytes() == b"Original failed model stays intact"


def test_busy_other_constructor_and_global_task_reject_physical_gc(tmp_path):
    store, first, second = fixture(tmp_path)
    store.delete_library(first["id"])
    store._save_job({"id": "busy", "library_id": second["id"], "pid": os.getpid(), "status": "running", "started_at": now()})
    with pytest.raises(LibraryLifecycleConflict):
        store.purge_library(first["id"], confirmed=True)
    assert store.library(first["id"], include_deleted=True)["deleted_at"]


def test_original_root_overlap_refuses_before_any_index_cleanup(tmp_path):
    store, first, _ = fixture(tmp_path)
    private = target_artifacts(store, first["id"])[0]
    owner = store.register("Original files under an owned-looking path", "user-models", [{"path": str(private)}])
    owner = store.set_enabled(owner["id"], True)
    store.delete_library(first["id"])
    with patch("advisor.library_index_purge.purge_library_indexes", side_effect=AssertionError("Unsafe plan must fail before index mutation")):
        with pytest.raises(ValueError, match="原始资料"):
            store.purge_library(first["id"], confirmed=True)
    assert private.is_dir() and store.library(owner["id"])


def test_purge_progress_remains_readable_while_catalog_write_transaction_is_held(tmp_path):
    store, first, _ = fixture(tmp_path)
    store.delete_library(first["id"])
    assert store.purge_progress(first["id"])["status"] == "idle"
    entered, release = threading.Event(), threading.Event()
    result = []

    def slow_indexes(*args, **kwargs):
        kwargs["on_progress"]({"stage": "indexes", "completed": 2, "total": 7, "unit": "generations"})
        entered.set()
        assert release.wait(5)
        return {"warnings": []}

    server = make_server(SimpleNamespace(libraries=store), 0)
    service = threading.Thread(target=server.serve_forever, daemon=True)
    service.start()
    worker = threading.Thread(target=lambda: result.append(store.purge_library(first["id"], confirmed=True)))
    try:
        with patch("advisor.library_index_purge.purge_library_indexes", side_effect=slow_indexes):
            worker.start()
            assert entered.wait(5)
            with patch.object(store, "connect", side_effect=AssertionError("Progress must not open the locked catalog")):
                code, progress = request(f"http://127.0.0.1:{server.server_port}", f"/api/libraries/{first['id']}/purge-progress")
            assert code == 200 and progress["status"] == "running"
            assert progress["completed"] == 2 and progress["total"] == 7
            assert progress["unit"] == "generations" and progress["updated_at"]
            release.set()
            worker.join(timeout=5)
        assert result[0]["status"] == "purged"
        assert store.purge_progress(first["id"])["status"] == "completed"
    finally:
        release.set()
        worker.join(timeout=5)
        server.shutdown()
        server.server_close()
        service.join(timeout=3)


def test_failed_purge_progress_is_observable_and_retry_replaces_error(tmp_path):
    store, first, _ = fixture(tmp_path)
    store.delete_library(first["id"])
    with patch("advisor.library_index_purge.purge_library_indexes", side_effect=RuntimeError("fixture interrupted")):
        with pytest.raises(RuntimeError):
            store.purge_library(first["id"], confirmed=True)
    progress = store.purge_progress(first["id"])
    assert progress["status"] == "failed" and "fixture interrupted" in progress["error"]
    store.purge_library(first["id"], confirmed=True)
    progress = store.purge_progress(first["id"])
    assert progress["status"] == "completed" and "error" not in progress


def test_plan_projects_ownership_from_large_records_and_keeps_normalized_shared_blobs(tmp_path):
    store = LibraryStore(tmp_path / "data/libraries")
    source = tmp_path / "source"
    source.mkdir()
    target = store.register("target", "user-models", [{"id": "models", "path": str(source)}])
    target = store.set_enabled(target["id"], True)
    keeper = store.register("keeper", "user-models", [{"path": str(source)}])
    keeper = store.set_enabled(keeper["id"], True)
    blobs = store.root / "blobs"
    blobs.mkdir()
    exclusive, shared = blobs / "exclusive.bin", blobs / "shared.bin"
    exclusive.write_bytes(b"exclusive")
    shared.write_bytes(b"shared")
    unused = "UNUSED_LARGE_PURGE_PAYLOAD" * 16000
    revision = {"id": "target-revision", "asset_key": "models:model.mph", "asset_hash": "a" * 64,
                "text": unused, "evidence": [{"root_id": "derived", "relative_path": "blobs/exclusive.bin"},
                                             {"root_id": "derived", "relative_path": "blobs/shared.bin"}]}
    other = {"id": "keeper-revision", "text": unused,
             "evidence": [{"root_id": "derived", "relative_path": "blobs/../blobs/shared.bin"}]}
    fingerprint = "b" * 64
    legacy = store.root / "artifacts" / digest([target["id"], "failed.mph", fingerprint])[:24]
    saved = legacy / ("mph-" + fingerprint[:20]) / "saved-state.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(encoded({"schema": "mph-saved-state/v1", "source_sha256": fingerprint}), encoding="utf-8")
    job = {"id": "f" * 32, "library_id": target["id"], "status": "failed", "indexes": {"unused": unused},
           "items": [None, "scalar", 3, {"path": "model.mph", "snapshot": unused}], "failed": [{"path": "failed.mph"}]}
    with closing(store.connect()) as db, db:
        for lid, value in ((target["id"], revision), (keeper["id"], other)):
            db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (value["id"], value["id"], lid, encoded(value)))
        db.execute("INSERT INTO builds VALUES(?,?)", (job["id"], encoded(job)))
    loads = json.loads

    def metadata_only(value, *args, **kwargs):
        assert "UNUSED_LARGE_PURGE_PAYLOAD" not in value, "Cleanup must not deserialize retained text/snapshots"
        return loads(value, *args, **kwargs)

    updates = []
    with closing(store.connect()) as db, patch("advisor.library_store.json.loads", side_effect=metadata_only):
        plan = store._purge_file_plan(db, target, on_progress=updates.append)
    assert plan["revision_ids"] == {revision["id"]}
    assert plan["blobs"] == [exclusive] and plan["shared_blobs"] == 1
    assert plan["artifacts"] == [legacy]
    assert plan["inventory"] == {legacy: (1, saved.stat().st_size), exclusive: (1, len(b"exclusive"))}
    assert [update["phase"] for update in updates] == ["revisions", "jobs", "artifacts", "evidence", "validation"]
    assert shared.read_bytes() == b"shared"


def test_initial_purge_progress_is_published_before_job_recovery(tmp_path):
    store, target, _ = fixture(tmp_path)
    store.delete_library(target["id"])
    recover = store.jobs_summary

    def recovery():
        state = store.purge_progress(target["id"])
        assert state["status"] == "running" and state["phase"] == "jobs"
        return recover()

    with patch.object(store, "jobs_summary", side_effect=recovery):
        assert store.purge_library(target["id"], confirmed=True)["status"] == "purged"


def test_inventory_rejects_nested_junction_before_index_mutation(tmp_path, monkeypatch):
    store, target, _ = fixture(tmp_path)
    private = target_artifacts(store, target["id"])[0]
    blocked = private / "nested"
    blocked.mkdir()
    import advisor.library_store as module
    scandir = module.os.scandir

    class Entry:
        def __init__(self, entry):
            self.entry = entry
            self.path = entry.path

        def stat(self, **kwargs):
            info = self.entry.stat(**kwargs)
            if Path(self.path) == blocked:
                return SimpleNamespace(st_mode=info.st_mode, st_reparse_tag=0xA0000003)
            return info

    class Entries:
        def __init__(self, path):
            assert Path(path) != blocked, "Do not descend into a junction"
            self.entries = scandir(path)

        def __enter__(self):
            return (Entry(entry) for entry in self.entries.__enter__())

        def __exit__(self, *args):
            return self.entries.__exit__(*args)

    store.delete_library(target["id"])
    with monkeypatch.context() as context:
        context.setattr(module.os, "scandir", Entries)
        with patch("advisor.library_index_purge.purge_library_indexes", side_effect=AssertionError("Unsafe inventory precedes index mutation")):
            with pytest.raises(ValueError, match="符号链接或联接"):
                store.purge_library(target["id"], confirmed=True)
    assert private.is_dir() and store.objects([target["id"]])


def test_purge_preserves_source_hardlinks_and_allows_sibling_directory_name_prefix(tmp_path):
    store, target, _ = fixture(tmp_path)
    private = target_artifacts(store, target["id"])[0]
    sibling_source = private.with_name(private.name + "0")
    sibling_source.mkdir()
    sibling_file = sibling_source / "original.mph"
    sibling_file.write_bytes(b"Original source in a similarly named sibling")
    keeper = store.register("prefix sibling", "user-models", [{"path": str(sibling_source)}])
    keeper = store.set_enabled(keeper["id"], True)
    source_file = next(Path(target["roots"][0]["path"]).glob("*.mph"))
    original = source_file.read_bytes()
    (private / "linked-original.mph").hardlink_to(source_file)
    store.delete_library(target["id"])
    assert store.purge_library(target["id"], confirmed=True)["status"] == "purged"
    assert source_file.read_bytes() == original
    assert sibling_file.read_bytes() == b"Original source in a similarly named sibling"
    assert store.library(keeper["id"]) and not private.exists()

