"""Public reads cannot bypass a closed library via archived citation IDs."""
import json
import threading
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from advisor.library_store import LibraryStore, encoded, file_hash
from advisor.server import make_server
from advisor.library_provider import LibraryKnowledgeProvider
from test_library_lifecycle_api import request
import library_cli


@pytest.fixture
def source(tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()
    store = LibraryStore(tmp_path / "data/libraries")
    library = store.register("Cases", "user-models", [{"id": "main", "path": str(folder), "role": "models"}], library_id="cases")
    library = store.set_enabled(library["id"], True)
    blob = store.root / "blobs/config.json"
    blob.parent.mkdir(exist_ok=True)
    blob.write_text(json.dumps({"nodes": [{"path": "mesh/size", "propertyValue": "PRIVATE_VALUE"}]}), encoding="utf-8")
    cid = "lo-" + "a" * 20
    value = {"id": cid, "object_id": "object", "revision_id": "rev", "library_id": "cases",
             "type": "model_case", "title": "Case", "text": "PRIVATE_BODY", "coverage": {},
             "evidence": [{"method": "comsol_standalone_api_snapshot", "root_id": "derived",
                           "relative_path": "blobs/config.json", "asset_hash": file_hash(blob)}]}
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (cid, "object", "cases", encoded(value)))
        db.execute("INSERT INTO active VALUES(?,?,?)", ("cases", "object", cid))
    return store, cid, value, blob


@pytest.mark.parametrize("action", ["close", "delete", "restore_closed"])
def test_raw_archive_survives_but_public_read_configuration_evidence_are_denied(source, action):
    store, cid, saved, _ = source
    assert store.source_available(cid)
    assert store.read(cid) == saved
    assert store.configuration(cid)["nodes"][0]["propertyValue"] == "PRIVATE_VALUE"
    if action == "close":
        store.set_enabled("cases", False)
    else:
        store.delete_library("cases")
        if action == "restore_closed":
            store.restore_library("cases")
    assert store.get(cid) == saved  # Internal archive/history is preserved.
    assert not store.source_available(cid)
    for reader in (store.read, store.configuration, store.read_evidence):
        with pytest.raises(PermissionError, match="关闭|删除"):
            reader(cid)
    if action != "delete":
        store.set_enabled("cases", True)
        assert store.read(cid) == saved and store.source_available(cid)


@pytest.mark.parametrize("method", ["configuration", "read_evidence"])
def test_close_during_artifact_read_cannot_return_body(source, monkeypatch, method):
    import advisor.library_store as module
    store, cid, _saved, _blob = source
    original = module.file_hash
    def hash_then_close(path):
        result = original(path)
        store.set_enabled("cases", False)
        return result
    monkeypatch.setattr(module, "file_hash", hash_then_close)
    with pytest.raises(PermissionError, match="关闭"):
        getattr(store, method)(cid)


def test_source_availability_does_not_read_document_payload(source, monkeypatch):
    store, cid, _saved, _blob = source
    monkeypatch.setattr(store, "get", Mock(side_effect=AssertionError("A permission check must not decode body")))
    assert store.source_available(cid)
    store.set_enabled("cases", False)
    assert not store.source_available(cid)


def test_closed_public_cli_read_is_denied(source, monkeypatch, capsys):
    store, cid, _saved, _blob = source
    store.set_enabled("cases", False)
    monkeypatch.setattr(library_cli, "LibraryStore", lambda *_args: store)
    monkeypatch.setattr("sys.argv", ["library_cli.py", "read", cid])
    assert library_cli.main() == 1
    output = capsys.readouterr()
    assert "PRIVATE_BODY" not in output.out + output.err


def test_closed_http_configuration_and_evidence_are_forbidden(source):
    store, cid, _saved, _blob = source
    store.set_enabled("cases", False)
    provider = LibraryKnowledgeProvider(SimpleNamespace(get_document=lambda _id: None), store.root)
    server = make_server(SimpleNamespace(libraries=store, kb=provider), 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        for method in ("configuration", "evidence"):
            status, value = request(base, f"/api/library-objects/{cid}/{method}")
            assert status == 403
            assert "PRIVATE" not in json.dumps(value)
        assert request(base, f"/api/sources/{cid}")[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
