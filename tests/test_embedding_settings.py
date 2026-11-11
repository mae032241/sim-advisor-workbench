"""Embedding is a persisted explicit capability, independent of installed files."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from advisor.library_search import LibrarySearch, BRIDGE_MARKER
from advisor.library_store import LibraryStore, LibraryLifecycleConflict, encoded
from advisor.retrieval_settings import read, update, settings_root
from advisor.server import make_server
from test_library_lifecycle_api import request
from test_library_search import FakeEngineSearch, obj


def model_fixture(tmp_path):
    model = tmp_path / "local-embedding.gguf"
    model.write_bytes(b"GGUFtest-local-embedding-identity")
    return model


def test_old_profile_and_installed_weights_do_not_enable_or_check_embedding(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    model = model_fixture(tmp_path)
    profile = {"schema": "sim-advisor-offline-dependencies/v1", "bundleRoot": str(tmp_path),
               "paths": {"embedModelPath": model.name}, "externalPaths": {}}
    (data / "dependency-profile.json").write_text(json.dumps(profile))
    monkeypatch.setenv("SIM_ADVISOR_EMBED_MODEL", str(model))
    before = model.read_bytes()
    original = Path.stat

    def guarded(path, *args, **kwargs):
        if path == model:
            raise AssertionError("Disabled embedding must not stat weights")
        return original(path, *args, **kwargs)

    with patch.object(Path, "stat", guarded), patch("advisor.library_search.local_model_path", side_effect=AssertionError("No model resolution")), \
         patch.object(LibrarySearch, "_model_info", side_effect=AssertionError("No model status checks")):
        search = LibrarySearch(data / "libraries/search", embed_model=model)
        status = search.status()
        assert status["modes"]["qmd_vector"]["state"] == "disabled"
        assert not search.dependency_status()["embedding_enabled"]
        assert search.model_uri == "" and search.dependencies["model_path"] is None
        assert not read(data)["enabled"]
        for operation in (lambda: search.prepare_embedding(), lambda: search.search("mesh", mode="qmd_vector"),
                          lambda: search.build([obj("one")], modes=["qmd_vector"], generation="g")):
            with pytest.raises(ValueError, match="disabled"):
                operation()
    assert model.read_bytes() == before and not (data / "retrieval-settings.json").exists()


def test_disabling_old_ready_vectors_preserves_files_and_native_queries(tmp_path):
    root = tmp_path / "data/libraries/search"
    fake = FakeEngineSearch(root)
    built = fake.build([obj("one")], modes=["native", "qmd_keyword", "qmd_vector"], generation="g")
    vector = Path(built["libraries"]["cases"]["vector_db_path"])
    before = (vector.read_bytes(), fake.active_path.read_bytes())
    update(root, enabled=False)
    with patch.object(LibrarySearch, "_model_info", side_effect=AssertionError("No model checks while off")):
        search = LibrarySearch(root)
        assert search.status()["modes"]["qmd_vector"] == {"ready": False, "state": "disabled", "enabled": False}
        assert search.search("valve", mode="native")
        with pytest.raises(ValueError, match="disabled"):
            search.search("valve", mode="qmd_vector")
    assert (vector.read_bytes(), fake.active_path.read_bytes()) == before


@pytest.mark.parametrize("operation", ["build", "start_build", "reindex"])
def test_store_rejects_vector_request_before_source_scan_or_job_creation(tmp_path, operation):
    store = LibraryStore(tmp_path / "data/libraries")
    with patch.object(store, "_buildable_library", side_effect=AssertionError("No source read")), \
         patch.object(store, "_save_job", side_effect=AssertionError("No queued job")), \
         patch.object(store, "scan", side_effect=AssertionError("No scan")):
        with pytest.raises(ValueError, match="disabled"):
            if operation == "reindex":
                store.reindex(["native", "qmd_vector"])
            else:
                getattr(store, operation)("unknown", modes=["native", "qmd_vector"])
    assert store.jobs() == [] and not (store.root / "artifacts").exists()


@pytest.mark.parametrize("embedding", [{"enabled": True}, {"enabled": True, "model_path": "relative.gguf"},
    {"enabled": True, "model_path": "hf:some/model.gguf"}, {"enabled": True, "model_path": "C:/model.gguf"},
    {"enabled": "true", "model_path": "C:/model.gguf"}])
def test_malformed_enabled_policy_fails_closed_without_model_checks(tmp_path, embedding):
    (tmp_path / "retrieval-settings.json").write_text(json.dumps({"schema": "retrieval-settings/v1", "embedding": embedding}))
    with patch("advisor.library_search.local_model_path", side_effect=AssertionError("No model resolution")):
        assert read(tmp_path)["enabled"] is False
        assert LibrarySearch(tmp_path).status()["modes"]["qmd_vector"]["state"] == "disabled"


def test_enable_validates_local_identity_once_and_same_search_instance_observes_toggles(tmp_path):
    model = model_fixture(tmp_path)
    search = LibrarySearch(tmp_path / "data/libraries/search")
    assert not search.dependency_status()["embedding_enabled"]
    configured = update(search.data_dir, enabled=True, model_path=str(model))
    assert configured["enabled"] and configured["configured"]
    assert configured["identity"]["sha256"] == hashlib.sha256(model.read_bytes()).hexdigest()
    with patch("advisor.library_search._hash_model_file", side_effect=AssertionError("Do not hash validated identity again")):
        assert search.dependency_status()["embedding_model"]["available"]
        assert search.prepare_embedding()["sha256"] == configured["identity"]["sha256"]
    update(search.data_dir, enabled=False)
    with patch.object(search, "_model_info", side_effect=AssertionError("Disabled means no model read")):
        assert search.dependency_status()["embedding_model"]["state"] == "disabled"
    assert model.exists() and read(tmp_path / "another-data")["enabled"] is False


def test_keyword_engine_uses_node_sdk_without_model_status_or_embedding_configuration(tmp_path, monkeypatch):
    node = tmp_path / "node.exe"
    node.write_bytes(b"node executable fixture")
    sdk = tmp_path / "qmd"
    (sdk / "dist").mkdir(parents=True)
    (sdk / "dist/index.js").write_text("SDK fixture")
    search = LibrarySearch(tmp_path / "data/libraries/search", node=node, qmd_dir=sdk)
    monkeypatch.setattr(search, "_model_info", lambda: pytest.fail("Keyword must not inspect any model"))
    calls = []

    def run(*args, **kwargs):
        payload = json.loads(kwargs["input_text"])
        calls.append(payload)
        assert payload["embedding_enabled"] is False and payload["model_uri"] == ""
        assert "QMD_EMBED_MODEL" not in payload["env"]
        return SimpleNamespace(returncode=0, stderr="", stdout=BRIDGE_MARKER + '{"indexed":1}')

    monkeypatch.setattr("advisor.library_search.run_text", run)
    parent = tmp_path / "index"
    (parent / "corpus").mkdir(parents=True)
    assert search._engine({"op": "update", "db_path": str(parent / "qmd-keyword.sqlite3"), "corpus": str(parent / "corpus")}) == {"indexed": 1}
    assert len(calls) == 1 and search.dependencies["embedding_errors"] == []


def test_settings_api_explicit_enable_disable_and_busy_conflicts(tmp_path):
    store = LibraryStore(tmp_path / "data/libraries")
    model = model_fixture(tmp_path)
    bad = tmp_path / "bad.gguf"
    bad.write_bytes(b"not a GGUF")
    server = make_server(SimpleNamespace(libraries=store), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    route = "/api/retrieval/embedding-settings"
    try:
        assert request(base, route)[1]["enabled"] is False
        for body in ({}, {"enabled": "true"}, {"enabled": True, "model_path": None},
                     {"enabled": True, "model_path": str(bad)}, {"enabled": True, "model_path": "hf:some/model.gguf"},
                     {"enabled": True, "model_path": "relative.gguf"}, {"enabled": False, "other": True}):
            assert request(base, route, body)[0] == 422
        code, value = request(base, route, {"enabled": True, "model_path": str(model)})
        assert code == 200 and value["enabled"] and value["configured"]
        assert (settings_root(store.root) / "retrieval-settings.json").is_file()
        store._build_lock.acquire()
        try:
            assert request(base, route, {"enabled": False})[0] == 409
        finally:
            store._build_lock.release()
        assert request(base, route, {"enabled": False})[1]["enabled"] is False
        assert model.exists() and request(base, route)[1]["configured"]
        with closing(store.connect()) as db, db:
            db.execute("INSERT INTO builds VALUES(?,?)", ("a" * 32, encoded({"id": "a" * 32, "status": "queued", "pid": os.getpid()})))
        assert request(base, route, {"enabled": True, "model_path": str(model)})[0] == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
