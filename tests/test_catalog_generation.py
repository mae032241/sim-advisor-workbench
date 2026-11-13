"""Persistent identity generations remain exact across SQL writers and stores."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from advisor import library_store
from advisor.library_store import LibraryStore, digest


def catalog(tmp_path, ids=("a", "b", "c")):
    store = LibraryStore(tmp_path / "libraries")
    with closing(sqlite3.connect(store.db_path)) as db, db:
        db.executemany("INSERT INTO active VALUES(?,?,?)", (("fixture", str(number), cid) for number, cid in enumerate(ids)))
    return store


def expected(store):
    with closing(sqlite3.connect(store.db_path)) as db:
        return digest([row[0] for row in db.execute("SELECT citation_id FROM active ORDER BY citation_id")])[:24]


def count_hashes(monkeypatch):
    original = library_store._ordered_identity_hash
    calls = []

    def hashed(rows):
        result = original(rows)
        calls.append(result)
        return result

    monkeypatch.setattr(library_store, "_ordered_identity_hash", hashed)
    return calls


@pytest.mark.parametrize("ids", [(), ("z", "a", "a", "證據 🧪", 'quoted"\\\n', "é")])
def test_cache_matches_legacy_json_hash_including_unicode_duplicates_and_empty(tmp_path, monkeypatch, ids):
    store = catalog(tmp_path, ids)
    calls = count_hashes(monkeypatch)
    generation = expected(store)
    assert store.current_generation() == generation
    assert store.current_generation() == generation
    assert len(calls) == 1


def test_warm_status_and_new_store_instances_never_read_active(tmp_path, monkeypatch):
    store = catalog(tmp_path, (f"citation-{number:06}" for number in range(5000)))
    generation = store.current_generation()
    monkeypatch.setattr(library_store, "_ordered_identity_hash", lambda _rows: pytest.fail("Warm generation must not hash"))

    def deny_active(action, table, *_args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == "active" else sqlite3.SQLITE_OK

    for _ in range(3):
        reopened = LibraryStore(store.root)
        connect = reopened.connect

        def guarded_connect():
            db = connect()
            db.set_authorizer(deny_active)
            return db

        monkeypatch.setattr(reopened, "connect", guarded_connect)
        assert reopened.current_generation() == generation
        assert reopened.index_status_summary()["desired_generation"] == generation


def test_warm_cache_survives_independent_python_process(tmp_path):
    store = catalog(tmp_path)
    generation = store.current_generation()
    script = """
import sys
from advisor import library_store
def forbidden(_rows):
    raise AssertionError('Restart must reuse persisted generation')
library_store._ordered_identity_hash = forbidden
assert library_store.LibraryStore(sys.argv[1]).current_generation() == sys.argv[2]
"""
    result = subprocess.run([sys.executable, "-c", script, str(store.root), generation],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    writer = """
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE active SET citation_id='other-process' WHERE object_id='0'")
"""
    result = subprocess.run([sys.executable, "-c", writer, str(store.db_path)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert store.current_generation() == expected(store) != generation


@pytest.mark.parametrize("statements", [
    ["INSERT INTO active VALUES('fixture','new','d')"],
    ["DELETE FROM active WHERE object_id='0'"],
    ["UPDATE active SET citation_id='changed' WHERE object_id='0'"],
    ["DELETE FROM active WHERE object_id='0'", "INSERT INTO active VALUES('fixture','new','d')"],
    ["INSERT OR REPLACE INTO active VALUES('fixture','0','replaced')"],
    ["UPDATE OR REPLACE active SET object_id='1' WHERE object_id='0'"],
    ["UPDATE OR REPLACE active SET rowid=2 WHERE object_id='0'"],
])
def test_sql_identity_changes_invalidate_once_including_same_count_and_replace(tmp_path, monkeypatch, statements):
    store = catalog(tmp_path)
    calls = count_hashes(monkeypatch)
    before = store.current_generation()
    with closing(sqlite3.connect(store.db_path)) as db, db:
        assert db.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        for statement in statements:
            db.execute(statement)
    after = expected(store)
    assert after != before
    assert store.current_generation() == after
    assert store.current_generation() == after
    assert len(calls) == 2


def test_two_store_instances_share_invalidation_and_only_one_recomputes(tmp_path, monkeypatch):
    store = catalog(tmp_path)
    second = LibraryStore(store.root)
    calls = count_hashes(monkeypatch)
    assert store.current_generation() == second.current_generation()
    assert len(calls) == 1
    with closing(sqlite3.connect(store.db_path)) as db, db:
        db.execute("UPDATE active SET citation_id='new' WHERE object_id='0'")
    assert second.current_generation() == store.current_generation() == expected(store)
    assert len(calls) == 2


def test_noop_update_and_rolled_back_changes_reuse_committed_generation(tmp_path, monkeypatch):
    store = catalog(tmp_path)
    calls = count_hashes(monkeypatch)
    before = store.current_generation()
    with closing(sqlite3.connect(store.db_path)) as db:
        db.execute("UPDATE active SET citation_id=citation_id,object_id=object_id,library_id=library_id,rowid=rowid")
        db.commit()
        assert store.current_generation() == before
        db.execute("UPDATE active SET citation_id='uncommitted' WHERE object_id='0'")
        assert db.execute("SELECT generation FROM _active_generation_cache").fetchone()[0] is None
        # WAL readers see the committed identities and their committed cache.
        assert store.current_generation() == before
        db.rollback()
    assert store.current_generation() == before
    assert len(calls) == 1


def test_library_lifecycle_metadata_does_not_invalidate_generation(tmp_path, monkeypatch):
    store = catalog(tmp_path)
    source = tmp_path / "sources"
    source.mkdir()
    store.register("Fixture", "user-manuals", [{"path": str(source)}], library_id="fixture")
    store.set_enabled("fixture", True)
    calls = count_hashes(monkeypatch)
    before = store.current_generation()
    for change in (lambda: store.set_enabled("fixture", False), lambda: store.delete_library("fixture"),
                   lambda: store.restore_library("fixture"), lambda: store.set_enabled("fixture", True)):
        change()
        assert store.current_generation() == before
    assert len(calls) == 1


def test_legacy_catalog_initialization_preserves_existing_index_generation(tmp_path, monkeypatch):
    root = tmp_path / "libraries"
    root.mkdir()
    with closing(sqlite3.connect(root / "catalog.sqlite3")) as db, db:
        db.execute("CREATE TABLE active(library_id TEXT,object_id TEXT,citation_id TEXT NOT NULL,PRIMARY KEY(library_id,object_id))")
        db.executemany("INSERT INTO active VALUES('fixture',?,?)", [("one", "lo-a"), ("two", "lo-b")])
    store = LibraryStore(root)
    generation = digest(["lo-a", "lo-b"])[:24]
    from advisor.library_search import LibrarySearch
    monkeypatch.setattr(LibrarySearch, "status", lambda _self, **_kw: {
        "generation": generation, "ready": True, "modes": {"native": {"ready": True, "state": "ready"}}})
    calls = count_hashes(monkeypatch)
    assert store.index_status_summary()["ready"]
    assert LibraryStore(root).index_status_summary()["ready"]
    assert len(calls) == 1


@pytest.mark.parametrize("warm_replacement", [False, True])
def test_existing_store_detects_atomic_catalog_replacement(tmp_path, monkeypatch, warm_replacement):
    store = catalog(tmp_path / "original")
    before = store.current_generation()
    replacement = catalog(tmp_path / "replacement", ("different",))
    wanted = expected(replacement)
    if warm_replacement:
        assert replacement.current_generation() == wanted
    # Checkpoint and close all handles before replacing a Windows database.
    for path in (store.db_path, replacement.db_path):
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
    os.replace(replacement.db_path, store.db_path)
    calls = count_hashes(monkeypatch)
    assert store.current_generation() == wanted != before
    assert store.current_generation() == wanted
    assert len(calls) == (0 if warm_replacement else 1)


def test_rebuilt_active_table_drops_tracking_and_forces_safe_reinitialization(tmp_path, monkeypatch):
    store = catalog(tmp_path)
    before = store.current_generation()
    with closing(sqlite3.connect(store.db_path)) as db, db:
        db.execute("DROP TABLE active")
        db.execute("CREATE TABLE active(library_id TEXT,object_id TEXT,citation_id TEXT NOT NULL,PRIMARY KEY(library_id,object_id))")
        db.execute("INSERT INTO active VALUES('fixture','one','migrated')")
    calls = count_hashes(monkeypatch)
    assert store.current_generation() == expected(store) != before
    with closing(sqlite3.connect(store.db_path)) as db, db:
        db.execute("UPDATE active SET citation_id='later'")
    assert store.current_generation() == expected(store)
    assert len(calls) == 2


def test_cold_hash_cannot_publish_over_a_concurrent_identity_update(tmp_path, monkeypatch):
    store = catalog(tmp_path)
    before = expected(store)
    hashing, release_hash, writer_attempting, writer_committed = (threading.Event() for _ in range(4))
    original = library_store._ordered_identity_hash

    def paused_hash(rows):
        hashing.set()
        assert release_hash.wait(10)
        return original(rows)

    monkeypatch.setattr(library_store, "_ordered_identity_hash", paused_hash)

    def write():
        assert hashing.wait(10)
        with closing(sqlite3.connect(store.db_path, timeout=10)) as db, db:
            writer_attempting.set()
            db.execute("UPDATE active SET citation_id='concurrent' WHERE object_id='0'")
        writer_committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        read_result = pool.submit(store.current_generation)
        write_result = pool.submit(write)
        try:
            assert writer_attempting.wait(10)
            assert not writer_committed.wait(0.05)
        finally:
            release_hash.set()
        assert read_result.result(10) == before
        write_result.result(10)
    assert writer_committed.is_set()
    assert store.current_generation() == expected(store) != before


def test_simultaneous_cold_readers_only_hash_once(tmp_path, monkeypatch):
    store = catalog(tmp_path)
    hashing, release_hash, second_started = (threading.Event() for _ in range(3))
    original = library_store._ordered_identity_hash
    calls = []

    def paused_hash(rows):
        calls.append(1)
        hashing.set()
        assert release_hash.wait(10)
        return original(rows)

    def second_read():
        second_started.set()
        return LibraryStore(store.root).current_generation()

    monkeypatch.setattr(library_store, "_ordered_identity_hash", paused_hash)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.current_generation)
        try:
            assert hashing.wait(10)
            second = pool.submit(second_read)
            assert second_started.wait(10)
        finally:
            release_hash.set()
        assert first.result(10) == second.result(10) == expected(store)
    assert len(calls) == 1


def test_cancelled_hash_does_not_persist_partial_generation(tmp_path, monkeypatch):
    store = catalog(tmp_path)
    from advisor.build_control import BuildCancelled

    def cancelled(_rows):
        raise BuildCancelled()

    with monkeypatch.context() as context:
        context.setattr(library_store, "_ordered_identity_hash", cancelled)
        with pytest.raises(BuildCancelled):
            store.current_generation()
    assert store.current_generation() == expected(store)
