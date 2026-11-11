"""Offline same-generation keyword publication and recoverable cleanup."""
from contextlib import closing
import base64
import json
from pathlib import Path
import sqlite3

import pytest

from advisor.library_search import LibrarySearch
from advisor.library_store import LibraryStore
from advisor.qmd_shared import KEYWORD_STORAGE
from scripts import merge_qmd_indexes as cli
from test_tutorial_migration import _put, _record, _source
from qmd_fixtures import keyword_schema, keyword_insert


def fixture(tmp_path):
    store = LibraryStore(tmp_path / "data/libraries")
    for name in ("a", "b"):
        source = tmp_path / name
        source.mkdir()
        path = source / "source.mph"
        path.write_bytes(b"Saved model fixture, never parsed")
        library = store.register(name, "user-models", [{"id": "models", "path": str(source)}], library_id=name)
        library = store.set_enabled(library["id"], True)
        obj = store._prepare(library, _record(path, source, "models"),
                             _source("saved", "saved_configuration", name, "Saved native " + name, path))
        _put(store, [obj])
    search = LibrarySearch(store.root / "search", preserve_history=True)
    if not search.node or not (search.qmd_dir / "dist/index.js").is_file():
        pytest.skip("Optional local QMD SDK unavailable")
    with store.object_stream() as objects:
        search.build(objects, modes=["native"], generation=objects.generation)
    old = search._load_active()
    old.pop("keyword_storage", None)
    for library, item in old["libraries"].items():
        item.pop("collection", None)
        item["db_path"] = str(Path(old["directory"]) / item["key"] / "qmd-keyword.sqlite3")
        database = Path(item["db_path"])
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as db, db:
            keyword_schema(db)
            with closing(sqlite3.connect(Path(old["directory"]) / "objects.sqlite3")) as native:
                for (locator,) in native.execute("SELECT qmd_path FROM objects WHERE library_id=?", (library,)):
                    keyword_insert(db, "objects", locator.split("/")[-1], library, "Saved native " + library)
        if library == "b":
            Path(item["vector_db_path"]).write_bytes(b"Vector file must remain untouched")
    old["modes"]["qmd_keyword"] = {"ready": True, "state": "ready", "backend": "qmd-searchLex"}
    search._publish(Path(old["directory"]), old)
    return store, search, search._load_active()


def test_merge_keeps_native_generation_vectors_catalog_and_releases_leases(tmp_path):
    store, search, old = fixture(tmp_path)
    native = Path(old["directory"]) / "objects.sqlite3"
    native_bytes, catalog_bytes = native.read_bytes(), store.db_path.read_bytes()
    vector = Path(old["libraries"]["b"]["vector_db_path"])
    result = cli.merge_indexes(store.root)
    active = search._load_active()
    assert result["status"] == "merged" and result["generation"] == old["generation"]
    assert active["keyword_storage"] == KEYWORD_STORAGE
    assert len({item["db_path"] for item in active["libraries"].values()}) == 1
    assert all(item["collection"] == item["key"] for item in active["libraries"].values())
    assert native.read_bytes() == native_bytes and store.db_path.read_bytes() == catalog_bytes
    assert vector.read_bytes() == b"Vector file must remain untouched"
    assert all(not Path(item["db_path"]).exists() for item in old["libraries"].values())
    assert not Path(old["libraries"]["a"]["db_path"]).parent.exists()
    assert not (store.root / ".build.lock").exists()
    assert not (store.root / ".qmd-shared-migration.json").exists()
    assert cli.merge_indexes(store.root)["status"] == "unchanged"


def test_failed_publication_restores_authority_and_sources(tmp_path, monkeypatch):
    store, search, old = fixture(tmp_path)
    root = store.root / "search"
    paths = [root / "active.json", root / "last-build.json", Path(old["directory"]) / "manifest.json"]
    before = {path: path.read_bytes() for path in paths}
    publish = cli.publish_manifest
    def fail(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError("injected publication failure")
    monkeypatch.setattr(cli, "publish_manifest", fail)
    with pytest.raises(RuntimeError, match="publication failure"):
        cli.merge_indexes(store.root)
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert all(Path(item["db_path"]).is_file() for item in old["libraries"].values())
    assert not (Path(old["directory"]) / "qmd-keyword.sqlite3").exists()


def test_cleanup_resume_handles_publication_to_journal_update_gap(tmp_path, monkeypatch):
    store, search, old = fixture(tmp_path)
    remove = cli._remove_legacy_databases
    monkeypatch.setattr(cli, "_remove_legacy_databases", lambda *_: (_ for _ in ()).throw(OSError("cleanup interrupted")))
    with pytest.raises(OSError, match="cleanup interrupted"):
        cli.merge_indexes(store.root)
    journal = store.root / ".qmd-shared-migration.json"
    saved = json.loads(journal.read_text(encoding="utf-8"))
    saved["phase"] = "publication_pending"
    journal.write_text(json.dumps(saved), encoding="utf-8")
    assert search._load_active()["keyword_storage"] == KEYWORD_STORAGE
    monkeypatch.setattr(cli, "_remove_legacy_databases", remove)
    monkeypatch.setattr(cli, "merge_keyword_indexes", lambda *_args, **_kwargs: pytest.fail("Resume must not remerge content"))
    assert cli.merge_indexes(store.root)["status"] == "resumed_cleanup"
    assert not journal.exists()
    assert all(not Path(item["db_path"]).exists() for item in old["libraries"].values())


def test_unpublished_journal_restores_old_manifest_and_retries_without_deleting_sources(tmp_path):
    store, search, old = fixture(tmp_path)
    root, directory = store.root / "search", Path(old["directory"])
    shared = directory / "qmd-keyword.sqlite3"
    shared.write_bytes(b"Interrupted unpublished construction")
    files = [root / "active.json", root / "last-build.json", directory / "manifest.json"]
    journal = {"schema": "qmd-shared-migration/v1", "phase": "publication_pending", "generation": old["generation"],
               "directory": str(directory), "legacy_databases": [item["db_path"] for item in old["libraries"].values()],
               "verification": {}, "previous_files": {str(path): base64.b64encode(path.read_bytes()).decode("ascii") for path in files}}
    (store.root / ".qmd-shared-migration.json").write_text(json.dumps(journal), encoding="utf-8")
    assert cli.merge_indexes(store.root)["status"] == "merged"
    assert search._load_active()["keyword_storage"] == KEYWORD_STORAGE
