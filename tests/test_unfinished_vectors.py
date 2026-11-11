"""Narrow, confirmed cleanup of unpublished vector artifacts in fixture roots."""
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from advisor.library_search import LibrarySearch, _hash
from advisor.library_store import LibraryLifecycleConflict, LibraryStore, encoded
from advisor.server import make_server
from test_library_lifecycle_api import request


FILES = ("qmd-vector.sqlite3", "qmd-vector.sqlite3-wal", "qmd-vector.sqlite3-shm", "qmd-vector.progress.json")


def generation(store, name, *, ready=False, state="building", libraries=("cases",)):
    root = store.root / "search"
    directory = root / "generations" / name
    directory.mkdir(parents=True)
    (directory / "objects.sqlite3").write_bytes(b"native FTS source objects keep")
    manifest = {"path_schema": "search-root-relative-v1", "directory": "generations/" + name,
                "generation": name, "ready": True, "libraries": {},
                "modes": {"native": {"ready": True, "state": "ready"},
                          "qmd_keyword": {"ready": True, "state": "ready"},
                          "qmd_vector": {"ready": ready, "state": state}}}
    for library in libraries:
        key = "lib_" + _hash(library)[:20]
        parent = directory / key
        (parent / "corpus").mkdir(parents=True)
        (parent / "corpus/o-source.md").write_text("original representation", encoding="utf-8")
        (parent / "qmd-keyword.sqlite3").write_bytes(b"keyword keep")
        with closing(sqlite3.connect(parent / FILES[0])) as db, db:
            db.execute("CREATE TABLE content_vectors(hash TEXT,seq INTEGER)")
            db.execute("INSERT INTO content_vectors VALUES('partial-real-sqlite-fixture',0)")
        for filename in FILES[1:3]:
            (parent / filename).write_bytes(("retained partial " + filename).encode())
        (parent / FILES[3]).write_text(encoded({"chunksEmbedded": 408, "totalChunks": 408,
            "bytesProcessed": 573094, "totalBytes": 1637106, "errors": 0}), encoding="utf-8")
        relative = "generations/" + name + "/" + key + "/"
        manifest["libraries"][library] = {"key": key, "corpus": relative + "corpus",
            "db_path": relative + "qmd-keyword.sqlite3", "vector_db_path": relative + FILES[0]}
    save(directory / "manifest.json", manifest)
    return directory, manifest


def save(path, value):
    path.write_text(encoded(value), encoding="utf-8")


def fixture(tmp_path):
    store = LibraryStore(tmp_path / "data/libraries")
    partial, partial_manifest = generation(store, "old-unfinished")
    complete, complete_manifest = generation(store, "completed", ready=True, state="ready")
    save(store.root / "search/active.json", complete_manifest)
    save(store.root / "search/last-build.json", partial_manifest)
    (tmp_path / "source.mph").write_bytes(b"source model")
    (tmp_path / "embedding.gguf").write_bytes(b"original embedding weights")
    return store, partial, partial_manifest, complete


def hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and path.name != "build.lock"}


def vector_parent(directory, manifest, library="cases"):
    return directory / manifest["libraries"][library]["key"]


def test_preview_is_read_only_and_cleanup_preserves_completed_keyword_native_and_sources(tmp_path):
    store, partial, manifest, complete = fixture(tmp_path)
    prior = hashes(tmp_path)
    with patch("advisor.library_search.retrieval_runtime", side_effect=AssertionError("no runtime dependency")), \
         patch.object(store, "jobs", side_effect=AssertionError("preview must not recover jobs")):
        preview = store.unfinished_vectors()
        assert hashes(tmp_path) == prior
        assert preview["file_count"] == 4 and preview["generation_count"] == 1
        assert preview["can_clean"] and not preview["building"]
        assert len(preview["preview_token"]) == 64
        result = store.clean_unfinished_vectors(confirmed=True, token=preview["preview_token"])
    assert result["cleaned"] and result["status"] == "cleaned" and result["bytes"] == preview["bytes"]
    assert not result["embedding_invoked"]
    after = hashes(tmp_path)
    removed = {str((vector_parent(partial, manifest) / name).relative_to(tmp_path)).replace("\\", "/") for name in FILES}
    changed = {"data/libraries/search/last-build.json", "data/libraries/search/generations/old-unfinished/manifest.json"}
    for name, digest in prior.items():
        if name not in removed | changed:
            assert after[name] == digest, name
    assert not any((vector_parent(partial, manifest) / name).exists() for name in FILES)
    value = json.loads((partial / "manifest.json").read_text())
    assert value["modes"]["qmd_vector"]["state"] == "cleaned"
    assert value["modes"]["native"] == manifest["modes"]["native"]
    assert value["modes"]["qmd_keyword"] == manifest["modes"]["qmd_keyword"]
    assert all((next(complete.glob("lib_*")) / name).exists() for name in FILES)
    assert not store._build_lock.locked() and not (store.root / ".build.lock").exists()
    assert not (store.root / "search/build-lease.json").exists()
    again = store.unfinished_vectors()
    assert again["file_count"] == 0 and not again["can_clean"]
    assert store.clean_unfinished_vectors(confirmed=True, token=again["preview_token"])["file_count"] == 0


@pytest.mark.parametrize("state", ["failed", "cancelled", "interrupted", "building"])
def test_known_unfinished_states_with_missing_progress_are_eligible(tmp_path, state):
    store, partial, manifest, _ = fixture(tmp_path)
    manifest["modes"]["qmd_vector"]["state"] = state
    save(partial / "manifest.json", manifest)
    save(store.root / "search/last-build.json", manifest)
    (vector_parent(partial, manifest) / FILES[-1]).unlink()
    assert store.unfinished_vectors()["file_count"] == 3


@pytest.mark.parametrize("fence", ["ready_alias", "ambiguous_alias", "missing_manifest", "wrong_directory", "not_requested", "embedding_record", "complete_progress"])
def test_completed_and_ambiguous_artifacts_are_preserved(tmp_path, fence):
    store, partial, manifest, _ = fixture(tmp_path)
    if fence in {"ready_alias", "ambiguous_alias"}:
        alias = copy.deepcopy(manifest)
        alias["modes"]["qmd_vector"] = {"ready": True, "state": "ready"} if fence == "ready_alias" else {}
        save(store.root / "search/last-build.json", alias)
    elif fence == "missing_manifest":
        (partial / "manifest.json").unlink()
    elif fence == "wrong_directory":
        manifest["directory"] = "generations/other"
        save(partial / "manifest.json", manifest)
    elif fence == "not_requested":
        manifest["modes"]["qmd_vector"]["state"] = "not_requested"
        save(partial / "manifest.json", manifest)
    elif fence == "embedding_record":
        manifest["modes"]["qmd_vector"]["embedding_records"] = {"cases": {"state": {"needsEmbedding": 0, "hasVectorIndex": True}, "embedding_rows": [{"chunks": 8}], "probe_source": "vec"}}
        save(partial / "manifest.json", manifest)
        save(store.root / "search/last-build.json", manifest)
    else:
        save(vector_parent(partial, manifest) / FILES[-1], {"chunksEmbedded": 1, "totalChunks": 1, "bytesProcessed": 100, "totalBytes": 100, "errors": 0})
    prior = hashes(tmp_path)
    preview = store.unfinished_vectors()
    assert preview["file_count"] == 0 and not preview["can_clean"]
    store.clean_unfinished_vectors(confirmed=True, token=preview["preview_token"])
    assert hashes(tmp_path) == prior


def test_completed_library_in_failed_generation_survives(tmp_path):
    store = LibraryStore(tmp_path / "libraries")
    directory, manifest = generation(store, "failed-multi", libraries=("finished", "unfinished"), state="failed")
    manifest["modes"]["qmd_vector"]["embedding_records"] = {"finished": {"probe_source": "vec"}}
    save(directory / "manifest.json", manifest)
    finished = hashes(vector_parent(directory, manifest, "finished"))
    preview = store.unfinished_vectors()
    assert preview["file_count"] == 4
    store.clean_unfinished_vectors(confirmed=True, token=preview["preview_token"])
    assert hashes(vector_parent(directory, manifest, "finished")) == finished


@pytest.mark.parametrize("race", ["replacement", "manifest", "new_generation"])
def test_stale_confirmation_deletes_nothing(tmp_path, race):
    store, partial, manifest, _ = fixture(tmp_path)
    preview = store.unfinished_vectors()
    if race == "replacement":
        target = vector_parent(partial, manifest) / FILES[0]
        target.unlink()
        target.write_bytes(b"new vector bytes must survive")
    elif race == "manifest":
        manifest["modes"]["qmd_vector"] = {"ready": True, "state": "ready"}
        save(store.root / "search/active.json", manifest)
    else:
        generation(store, "new-unfinished")
    prior = hashes(tmp_path)
    with pytest.raises(LibraryLifecycleConflict, match="变化"):
        store.clean_unfinished_vectors(confirmed=True, token=preview["preview_token"])
    assert hashes(tmp_path) == prior


@pytest.mark.parametrize("busy", ["store", "search", "queued", "running", "cancel_requested", "store_lease", "search_lease"])
def test_busy_builds_prevent_cleanup_without_rewriting_jobs(tmp_path, busy):
    store, _, _, _ = fixture(tmp_path)
    preview = store.unfinished_vectors()
    search = LibrarySearch.for_maintenance(store.root / "search")
    lock = None
    if busy in {"store", "search"}:
        lock = store._build_lock if busy == "store" else search.lock
        lock.acquire()
    elif busy.endswith("lease"):
        save(store.root / (".build.lock" if busy == "store_lease" else "search/build-lease.json"), {"pid": os.getpid()})
    else:
        with closing(store.connect()) as db, db:
            db.execute("INSERT INTO builds VALUES(?,?)", ("a" * 32, encoded({"id": "a" * 32, "status": busy, "pid": os.getpid()})))
    prior = hashes(tmp_path)
    try:
        assert store.unfinished_vectors()["building"] is True
        with pytest.raises(LibraryLifecycleConflict):
            store.clean_unfinished_vectors(confirmed=True, token=preview["preview_token"])
    finally:
        if lock:
            lock.release()
    assert hashes(tmp_path) == prior


def test_os_search_lease_prevents_cross_process_cleanup(tmp_path):
    store, _, _, _ = fixture(tmp_path)
    preview = store.unfinished_vectors()
    ready = tmp_path / "ready"
    script = "import sys;from pathlib import Path;from advisor.library_search import LibrarySearch;s=LibrarySearch.for_maintenance(Path(sys.argv[1]));h,t=s._acquire_lease('other');Path(sys.argv[2]).touch();sys.stdin.read(1);s._release_lease(h,t)"
    worker = subprocess.Popen([sys.executable, "-c", script, str(store.root / "search"), str(ready)], stdin=subprocess.PIPE)
    try:
        import time
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        prior = hashes(store.root)
        with pytest.raises(LibraryLifecycleConflict):
            store.clean_unfinished_vectors(confirmed=True, token=preview["preview_token"])
        assert hashes(store.root) == prior
    finally:
        worker.communicate(b"x", timeout=5)


@pytest.mark.parametrize("target", ["root", "generation", "library", "file", "manifest"])
def test_symlink_or_junction_is_refused(tmp_path, target):
    store, partial, manifest, _ = fixture(tmp_path)
    parent = vector_parent(partial, manifest)
    path = {"root": store.root / "search", "generation": partial, "library": parent,
            "file": parent / FILES[0], "manifest": partial / "manifest.json"}[target]
    original = path.with_name(path.name + "-real")
    path.rename(original)
    try:
        path.symlink_to(original, target_is_directory=original.is_dir())
    except OSError:
        junction = subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(original)], capture_output=True) if os.name == "nt" and original.is_dir() else None
        if junction is None or junction.returncode:
            original.rename(path)
            pytest.skip("Host does not allow file symlinks")
    with pytest.raises(ValueError, match="symlink/junction"):
        store.unfinished_vectors()
    assert original.exists()


def test_hardlinked_vector_is_refused(tmp_path):
    store, partial, manifest, _ = fixture(tmp_path)
    path = vector_parent(partial, manifest) / FILES[0]
    shared = tmp_path / "shared-vector"
    shared.hardlink_to(path)
    with pytest.raises(ValueError, match="普通文件"):
        store.unfinished_vectors()
    assert shared.read_bytes() == path.read_bytes()


def test_preview_sees_uncheckpointed_queued_jobs_without_touching_catalog(tmp_path):
    store, _, _, _ = fixture(tmp_path)
    with closing(store.connect()) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO builds VALUES(?,?)", ("b" * 32, encoded({"id": "b" * 32, "status": "queued"})))
        writer.commit()
        before = hashes(store.root)
        assert store.unfinished_vectors()["building"] is True
        assert hashes(store.root) == before


def test_unknown_filenames_are_never_removed(tmp_path):
    store, partial, manifest, _ = fixture(tmp_path)
    parent = vector_parent(partial, manifest)
    for name in ("qmd-vector.sqlite3.backup", "qmd-vector.progress.json.tmp", "model.gguf", "original.mph"):
        (parent / name).write_bytes(b"keep")
    preview = store.unfinished_vectors()
    assert preview["file_count"] == 4
    store.clean_unfinished_vectors(confirmed=True, token=preview["preview_token"])
    assert sorted(path.name for path in parent.iterdir() if path.is_file()) == ["model.gguf", "original.mph", "qmd-keyword.sqlite3", "qmd-vector.progress.json.tmp", "qmd-vector.sqlite3.backup"]


def test_api_requires_preview_confirmation_and_exact_body(tmp_path):
    store, _, _, _ = fixture(tmp_path)
    server = make_server(SimpleNamespace(libraries=store), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    route = "/api/library-indexes/unfinished-vectors"
    try:
        prior = hashes(tmp_path)
        code, preview = request(base, route)
        assert code == 200 and preview["file_count"] == 4
        assert hashes(tmp_path) == prior
        for body in ({}, {"confirmed": "yes", "token": preview["preview_token"]}, {"confirmed": True},
                     {"confirmed": True, "token": "wrong"}, {"confirmed": True, "token": preview["preview_token"], "root": "elsewhere"}):
            assert request(base, route, body)[0] == 422
        assert hashes(tmp_path) == prior
        code, result = request(base, route, {"confirmed": True, "token": preview["preview_token"]})
        assert code == 200 and result["cleaned"] is True
        assert request(base, route, {"confirmed": True, "token": preview["preview_token"]})[0] == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
