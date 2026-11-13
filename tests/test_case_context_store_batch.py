"""Bounded context transactions and reusable structural metadata validation."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from advisor import case_context_store as contexts


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "search"
    directory = root / "generations" / "fixture"
    directory.mkdir(parents=True)
    objects = directory / "objects.sqlite3"
    with closing(sqlite3.connect(objects)) as db:
        db.execute("CREATE TABLE objects(id TEXT PRIMARY KEY)")
    with closing(sqlite3.connect(tmp_path / "catalog.sqlite3")) as db, db:
        db.execute("CREATE TABLE revisions(id TEXT PRIMARY KEY,library_id TEXT,object_id TEXT)")
        db.execute("INSERT INTO revisions VALUES('source','manuals','source-object')")
    active = {"generation": "fixture", "corpus_hash": "fixture-corpus", "directory": str(directory)}
    return SimpleNamespace(root=root, active=active, objects=objects)


def profile(index, text="source-derived context"):
    return {"schema": contexts.SCHEMA, "case_id": "case-" + str(index), "library_id": "models",
            "case_group": "group-" + str(index), "text": text, "is_read_evidence": False,
            "source_refs": [{"source_id": "source", "library_id": "manuals", "object_id": "source-object"}],
            "qmd_path": "lib_" + "a" * 20 + "/case-" + str(index) + ".md",
            "qmd_hash": hashlib.sha256(text.encode()).hexdigest()}


def rows(store):
    with closing(sqlite3.connect(contexts.context_path(store.root))) as db:
        return {table: db.execute("SELECT * FROM " + table + " ORDER BY 1,2").fetchall()
                for table in ("contexts", "dependencies", "capabilities", "meta")}


def touch_objects(store):
    info = store.objects.stat()
    os.utime(store.objects, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))


def test_batches_cover_more_than_130_cases_without_reading_or_replacing_unrelated_profiles(store, monkeypatch):
    profiles = [profile(index) for index in range(137)]
    capabilities = {entry["case_id"]: {"classification": "configured"} for entry in profiles}
    statements = []
    original = contexts.sqlite3.connect

    def traced(*args, **kwargs):
        db = original(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(contexts.sqlite3, "connect", traced)
    for start in range(0, len(profiles), 64):
        batch = profiles[start:start + 64]
        undo = contexts.prepare_context_batch(store.root, store.active, batch, capabilities=capabilities)
        assert len(undo["case_ids"]) <= 64
        assert not undo["contexts"] and not undo["dependencies"]
        assert len(json.dumps(undo)) < 20_000
        statements.clear()
        report = contexts.upsert_context_batch(store.root, store.active, batch, capabilities=capabilities, expected=undo)
        assert report["context_count"] == min(start + 64, len(profiles))
        if start:
            assert undo["capability_override_applied"] is False and "capabilities" not in undo
            assert not any("DELETE FROM capabilities" in sql or "INSERT INTO capabilities" in sql
                           or "SELECT case_id,classification,evidence_json FROM capabilities" in sql for sql in statements)
        assert not any("SELECT * FROM contexts" in sql for sql in statements)
    assert contexts.read_contexts(store.root, store.active) == {entry["case_id"]: entry for entry in profiles}
    assert contexts.validated_contexts(store.root, store.active)["meta"]["dependency_count"] == 137
    assert not Path(str(contexts.context_path(store.root)) + "-wal").exists()


def test_restore_replaces_only_selected_rows_and_rebinds_current_objects_signature(store):
    profiles = [profile(index) for index in range(134)]
    capabilities = {"case-0": "configured", "case-1": "geometry_only"}
    contexts.write_contexts(store.root, store.active, profiles, capabilities)
    before = rows(store)
    changed = [profile(0, "new documented text"), profile(200, "new overview")]
    undo = contexts.prepare_context_batch(store.root, store.active, changed, capabilities={"case-0": "unknown"})
    assert undo["existed"] and len(undo["contexts"]) == 1 and len(undo["dependencies"]) == 1
    assert undo["capability_override_applied"] and len(undo["capabilities"]) == 2
    assert "case-133" not in json.dumps(undo)
    touch_objects(store)
    contexts.upsert_context_batch(store.root, store.active, changed, capabilities={"case-0": "unknown"}, expected=undo)
    after = rows(store)
    assert [row for row in before["contexts"] if row[0] != "case-0"] == [
        row for row in after["contexts"] if row[0] not in {"case-0", "case-200"}]
    touch_objects(store)
    report = contexts.restore_context_batch(store.root, store.active, json.loads(json.dumps(undo)))
    restored = rows(store)
    for table in ("contexts", "dependencies", "capabilities"):
        assert restored[table] == before[table]
    old_meta, new_meta = dict(before["meta"]), dict(restored["meta"])
    old_meta.pop("objects_signature")
    new_meta.pop("objects_signature")
    assert old_meta == new_meta
    assert report["meta"] == contexts.validated_contexts(store.root, store.active)["meta"]


def test_injected_failure_rolls_back_profiles_dependencies_capabilities_and_meta(store, monkeypatch):
    contexts.write_contexts(store.root, store.active, [profile(index) for index in range(132)], {"case-0": "configured"})
    before = rows(store)
    batch = [profile(0, "changed"), profile(200, "new")]
    undo = contexts.prepare_context_batch(store.root, store.active, batch, capabilities={"case-0": "unknown"})

    def fail(_db, _meta):
        raise RuntimeError("injected metadata failure after row writes")

    monkeypatch.setattr(contexts, "_write_meta", fail)
    with pytest.raises(RuntimeError, match="injected metadata failure"):
        contexts.upsert_context_batch(store.root, store.active, batch, capabilities={"case-0": "unknown"}, expected=undo)
    assert rows(store) == before
    assert contexts.validated_contexts(store.root, store.active)["meta"]["context_count"] == 132


def test_new_sidecar_failure_and_restore_leave_no_database_or_journal(store, monkeypatch):
    batch = [profile(0)]
    undo = contexts.prepare_context_batch(store.root, store.active, batch)
    assert undo["existed"] is False
    original = contexts._write_meta

    def fail(_db, _meta):
        raise RuntimeError("injected initial metadata failure")

    monkeypatch.setattr(contexts, "_write_meta", fail)
    with pytest.raises(RuntimeError, match="injected initial metadata failure"):
        contexts.upsert_context_batch(store.root, store.active, batch, expected=undo)
    assert list((store.root / "runtime").glob(contexts.FILENAME + "*")) == []
    monkeypatch.setattr(contexts, "_write_meta", original)
    contexts.upsert_context_batch(store.root, store.active, batch, expected=undo)
    assert contexts.validated_contexts(store.root, store.active)
    assert contexts.restore_context_batch(store.root, store.active, undo) is None
    assert list((store.root / "runtime").glob(contexts.FILENAME + "*")) == []


def test_prepared_batch_rejects_changed_sidecar_and_batch_bounds(store):
    contexts.write_contexts(store.root, store.active, [profile(0)], {})
    undo = contexts.prepare_context_batch(store.root, store.active, [profile(0, "new")])
    with closing(sqlite3.connect(contexts.context_path(store.root))) as db, db:
        db.execute("UPDATE contexts SET qmd_hash=?", ("b" * 64,))
    with pytest.raises(contexts.CaseContextStale, match="after preparing"):
        contexts.upsert_context_batch(store.root, store.active, [profile(0, "new")], expected=undo)
    with pytest.raises(ValueError, match="profile limit"):
        contexts.prepare_context_batch(store.root, store.active, [profile(index) for index in range(65)])
    contexts.write_contexts(store.root, store.active, [profile(0, "x" * contexts.MAX_BATCH_BYTES)], {})
    with pytest.raises(ValueError, match="undo exceeds"):
        contexts.prepare_context_batch(store.root, store.active, [profile(0)])


def test_old_v1_capability_metadata_is_compared_once_then_reuses_hash(store):
    caps = {"case-0": "configured"}
    contexts.write_contexts(store.root, store.active, [profile(0)], caps)
    with closing(sqlite3.connect(contexts.context_path(store.root))) as db, db:
        db.execute("DELETE FROM meta WHERE key='capability_hash'")
    undo = contexts.prepare_context_batch(store.root, store.active, [profile(1)], capabilities=caps)
    assert not undo["capability_override_applied"] and "capabilities" not in undo
    contexts.upsert_context_batch(store.root, store.active, [profile(1)], capabilities=caps, expected=undo)
    assert "capability_hash" in contexts.validated_contexts(store.root, store.active)["meta"]


def test_warm_schema_validation_does_not_repeat_full_counts_and_never_caches_pending(store, monkeypatch):
    contexts.write_contexts(store.root, store.active, [profile(index) for index in range(134)], {})
    contexts._VALIDATED.clear()
    statements = []
    original = contexts._connect

    def traced(path):
        db = original(path)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(contexts, "_connect", traced)
    contexts.load_context(store.root, store.active, "case-0")
    assert sum("SELECT count(*)" in sql for sql in statements) == 3
    statements.clear()
    for index in range(134):
        assert contexts.load_context(store.root, store.active, "case-" + str(index))
    assert not any("SELECT count(*)" in sql or "NOT EXISTS" in sql for sql in statements)
    contexts.pending_path(store.root).write_text("pending", encoding="utf-8")
    with pytest.raises(RuntimeError, match="pending"):
        contexts.load_context(store.root, store.active, "case-0")
    contexts.pending_path(store.root).unlink()
    touch_objects(store)
    with pytest.raises(contexts.CaseContextStale):
        contexts.validated_contexts(store.root, store.active)


def test_schema_cache_invalidates_uncheckpointed_wal_changes_and_atomic_replacement(store):
    contexts.write_contexts(store.root, store.active, [profile(0)], {})
    assert contexts.validated_contexts(store.root, store.active)
    target = contexts.context_path(store.root)
    original = target.read_bytes()
    with closing(sqlite3.connect(target)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("UPDATE dependencies SET case_id='lost-profile'")
        db.commit()
        assert Path(str(target) + "-wal").stat().st_size > 0
        with pytest.raises(contexts.CaseContextStale):
            contexts.validated_contexts(store.root, store.active)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    stage = target.with_name("replacement.sqlite3")
    stage.write_bytes(original)
    os.replace(stage, target)
    assert contexts.validated_contexts(store.root, store.active)
    with closing(sqlite3.connect(target)) as db, db:
        db.execute("UPDATE meta SET value=? WHERE key='corpus_hash'", (json.dumps("stale"),))
    with pytest.raises(contexts.CaseContextStale):
        contexts.validated_contexts(store.root, store.active)
