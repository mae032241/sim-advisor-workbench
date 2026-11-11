"""Exact revision storage and offline publication against isolated catalogs."""
from contextlib import closing
import json
import shutil
import sqlite3
import threading

import pytest

from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.catalog_storage import compact_catalog, register_catalog_functions, revision_storage
from advisor.library_store import LibraryStore, encoded


def legacy_fixture(tmp_path, count=260):
    root = tmp_path / "libraries"
    root.mkdir()
    with closing(sqlite3.connect(root / "catalog.sqlite3")) as db, db:
        db.execute("CREATE TABLE revisions(id TEXT PRIMARY KEY, object_id TEXT NOT NULL, library_id TEXT NOT NULL, payload TEXT NOT NULL)")
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "manual.txt").write_text("original evidence", encoding="utf-8")
    store = LibraryStore(root)
    library = store.register("Fixture", "official-manuals", [{"id": "manuals", "path": str(sources)}], library_id="fixture")
    library = store.set_enabled(library["id"], True)
    raw = {}
    with closing(store.connect()) as db, db:
        for number in range(count):
            identity = f"citation-{number:04}"
            value = {"id": identity, "object_id": f"object-{number // 2}", "library_id": library["id"],
                     "type": "manual_chunk", "title": "證據 🧪", "text": "identical repeated explanation. " * 300,
                     "data": {"number": number, "nested": [None, True, 1.25]}, "evidence": []}
            # Deliberately noncanonical TEXT; compaction must retain whitespace
            # and Unicode escapes, not merely equivalent decoded JSON.
            raw[identity] = " \r\n" + json.dumps(value, ensure_ascii=number % 2 == 0, indent=1) + "\n "
            db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (identity, value["object_id"], library["id"], raw[identity]))
            if number % 2:
                db.execute("INSERT INTO active VALUES(?,?,?)", (library["id"], value["object_id"], identity))
        db.execute("INSERT INTO builds VALUES(?,?)", ("job", ' {"id":"job", "status":"completed", "items":[]} '))
        db.execute("INSERT INTO case_members VALUES(?,?,?)", ("case", "local", "citation-0001"))
        # Nonrevision schema and rowids survive too.
        db.execute("CREATE TABLE audit_fixture(id INTEGER PRIMARY KEY AUTOINCREMENT, note BLOB)")
        db.execute("INSERT INTO audit_fixture(id,note) VALUES(17,?)", (b"\x00bytes\xff",))
        db.execute("CREATE INDEX audit_note ON audit_fixture(note)")
    return store, library, sources, raw


def logical_snapshot(store):
    with closing(store.connect()) as db:
        result = {name: [tuple(row) for row in db.execute("SELECT rowid,* FROM " + name + " ORDER BY rowid")]
                  for name in ("libraries", "active", "builds", "case_members", "audit_fixture", "sqlite_sequence")}
        result["revisions"] = [tuple(row) for row in db.execute("SELECT * FROM revisions ORDER BY id")]
        return result


def assert_no_stage(store):
    assert not (store.root / ".catalog-migration.lock").exists()
    assert not list(store.root.glob(".catalog.sqlite3.compact-*"))


def test_new_catalog_is_compact_and_sql_payload_contract_remains_writable(tmp_path):
    store = LibraryStore(tmp_path / "libraries")
    raw = ' { "id" : "a", "library_id": "l", "evidence":[{"relative_path":"原始.txt"}]}\r\n'
    with closing(store.connect()) as db, db:
        assert revision_storage(db) == "catalog-revisions/zlib-v1"
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", ("a", "o", "l", raw))
        db.execute("INSERT OR IGNORE INTO revisions VALUES(?,?,?,?)", ("a", "ignored", "ignored", "ignored"))
        assert tuple(db.execute("SELECT * FROM revisions").fetchone()) == ("a", "o", "l", raw)
        assert db.execute("SELECT json_extract(payload,'$.id') FROM revisions").fetchone()[0] == "a"
        assert db.execute("SELECT json_extract(e.value,'$.relative_path') FROM revisions r,json_each(r.payload,'$.evidence') e").fetchone()[0] == "原始.txt"
        replacement = raw + " "
        db.execute("UPDATE revisions SET payload=? WHERE id=?", (replacement, "a"))
        assert db.execute("SELECT payload FROM revisions WHERE id='a'").fetchone()[0] == replacement
    with closing(store.connect()) as db:
        db.execute("DELETE FROM revisions WHERE id='a'")
        db.rollback()
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 1
        db.execute("DELETE FROM revisions WHERE library_id='l'")
        db.commit()
        assert not db.execute("SELECT * FROM revisions").fetchall()


def test_migration_retains_exact_payloads_identities_order_history_and_other_tables(tmp_path):
    store, _, _, raw = legacy_fixture(tmp_path)
    before = logical_snapshot(store)
    objects, generation = store.objects(), store.current_generation()
    with store.object_stream() as stream:
        streamed = list(stream)
    result = compact_catalog(store.db_path, offline=True)
    assert result["status"] == "compacted" and result["after_bytes"] < result["before_bytes"] / 2
    assert result["tables"]["revisions"]["rows"] == len(raw)
    assert logical_snapshot(store) == before
    assert store.objects() == objects and store.current_generation() == generation
    with store.object_stream() as stream:
        assert list(stream) == streamed
    for identity, payload in raw.items():
        assert store.get(identity) == json.loads(payload)
    with closing(store.connect()) as db:
        assert dict(db.execute("SELECT id,payload FROM revisions")) == raw
        assert db.execute("SELECT typeof(payload) FROM _revision_payloads LIMIT 1").fetchone()[0] == "blob"
        assert db.execute("SELECT name FROM sqlite_master WHERE name='audit_note'").fetchone()
    assert compact_catalog(store.db_path, offline=True)["status"] == "already_compact"
    assert_no_stage(store)


def test_legacy_catalog_is_not_implicitly_migrated_when_opened(tmp_path):
    store, _, _, _ = legacy_fixture(tmp_path, count=2)
    LibraryStore(store.root)
    with closing(store.connect()) as db:
        assert revision_storage(db) == "json-text"
    with pytest.raises(ValueError, match="offline"):
        compact_catalog(store.db_path)
    assert_no_stage(store)


@pytest.mark.parametrize("phase", ["copy", "validate", "publish"])
def test_cancellation_leaves_original_logical_catalog_and_cleans_stage(tmp_path, phase):
    store, _, _, _ = legacy_fixture(tmp_path)
    before = logical_snapshot(store)
    event = threading.Event()

    def progress(update):
        if update["phase"] == phase and (phase != "copy" or update.get("completed", 0) >= 128):
            event.set()

    with build_control(BuildControl(tmp_path / "never-created", event)):
        with pytest.raises(BuildCancelled):
            compact_catalog(store.db_path, offline=True, on_progress=progress)
    assert logical_snapshot(store) == before
    with closing(store.connect()) as db:
        assert revision_storage(db) == "json-text"
    assert_no_stage(store)


def test_interrupted_atomic_publication_preserves_source_and_retry_works(tmp_path, monkeypatch):
    from advisor import catalog_storage
    store, _, _, _ = legacy_fixture(tmp_path)
    before = logical_snapshot(store)

    def interrupted(*_):
        raise PermissionError("fixture interrupted atomic replacement")

    with monkeypatch.context() as context:
        context.setattr(catalog_storage.os, "replace", interrupted)
        with pytest.raises(PermissionError, match="interrupted"):
            compact_catalog(store.db_path, offline=True)
    assert logical_snapshot(store) == before
    assert_no_stage(store)
    assert compact_catalog(store.db_path, offline=True)["status"] == "compacted"


def test_validation_rejects_even_json_equivalent_payload_text_change(tmp_path, monkeypatch):
    from advisor import catalog_storage
    store, _, _, _ = legacy_fixture(tmp_path, count=4)
    before = logical_snapshot(store)
    pack = catalog_storage._pack
    monkeypatch.setattr(catalog_storage, "_pack", lambda payload: pack(payload + "\n"))
    with pytest.raises(ValueError, match="logical rows"):
        compact_catalog(store.db_path, offline=True)
    assert logical_snapshot(store) == before
    assert_no_stage(store)


def test_migration_fences_new_connections_and_refuses_existing_build_lease(tmp_path):
    store, _, _, _ = legacy_fixture(tmp_path, count=2)
    lock = store.root / ".catalog-migration.lock"
    lock.write_text("fixture migration", encoding="utf-8")
    with pytest.raises(RuntimeError, match="maintenance"):
        store.connect()
    with pytest.raises(RuntimeError, match="maintenance"):
        with store.object_stream():
            pass
    with pytest.raises(FileExistsError):
        compact_catalog(store.db_path, offline=True)
    assert lock.read_text(encoding="utf-8") == "fixture migration"
    lock.unlink()
    lease = store.root / ".build.lock"
    lease.write_text("existing fixture lease", encoding="utf-8")
    with pytest.raises(RuntimeError, match="lease"):
        compact_catalog(store.db_path, offline=True)
    assert lease.read_text(encoding="utf-8") == "existing fixture lease"
    assert_no_stage(store)


def test_migration_refuses_outstanding_sqlite_reader(tmp_path):
    store, _, _, _ = legacy_fixture(tmp_path, count=2)
    before = logical_snapshot(store)
    with closing(store.connect()) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM revisions").fetchone()
        with pytest.raises((sqlite3.OperationalError, RuntimeError)):
            compact_catalog(store.db_path, offline=True)
        assert reader.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 2
    assert logical_snapshot(store) == before
    assert_no_stage(store)


def test_migrated_catalog_keeps_closed_gates_rebind_history_and_purge_counts(tmp_path):
    store, library, sources, raw = legacy_fixture(tmp_path, count=4)
    compact_catalog(store.db_path, offline=True)
    before = store.get("citation-0000")  # Historical, inactive revision.
    store.set_enabled(library["id"], False)
    assert store.get("citation-0000") == before and not store.source_available("citation-0000")
    with pytest.raises(PermissionError):
        store.read("citation-0000")
    store.delete_library(library["id"])
    assert store.get("citation-0000") == before
    store.restore_library(library["id"])
    store.set_enabled(library["id"], True)
    rebound = tmp_path / "rebound"
    shutil.copytree(sources, rebound)
    store.rebind(library["id"], "manuals", rebound)
    with closing(store.connect()) as db:
        assert dict(db.execute("SELECT id,payload FROM revisions")) == raw
    store.delete_library(library["id"])
    result = store.purge_library(library["id"], confirmed=True)
    assert result["counts"]["revisions"] == 4 and result["counts"]["active"] == 2
    assert store.get("citation-0000") is None
    assert (sources / "manual.txt").read_text(encoding="utf-8") == "original evidence"
    assert (rebound / "manual.txt").is_file()


def test_readonly_external_connection_can_register_payload_codec(tmp_path):
    store = LibraryStore(tmp_path / "libraries")
    raw = encoded({"id": "a", "library_id": "fixture", "text": "test" * 1000})
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", ("a", "a", "fixture", raw))
    with closing(sqlite3.connect(store.db_path.as_uri() + "?mode=ro", uri=True)) as db:
        register_catalog_functions(db)
        db.execute("PRAGMA query_only=ON")
        assert db.execute("SELECT payload FROM revisions WHERE id='a'").fetchone()[0] == raw
