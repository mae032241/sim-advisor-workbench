"""Public object metadata survives retirement of independent native postings."""
from contextlib import closing
import json
import sqlite3

import pytest

from advisor import object_index
from advisor.build_control import BuildCancelled
from scripts.remove_legacy_object_fts import remove_indexes


def item(identity="a", library="models"):
    return {"id": identity, "library_id": library, "title": "Valve mesh " + identity,
            "kind": "case", "type": "saved_configuration", "data": {"feature_branch": "mesh"}}


def historical(db, *, compact=True, metadata="native_metadata"):
    """Only tests reconstruct retired formats; production creates metadata only."""
    if compact:
        assert metadata in {"object_metadata", "native_metadata"}
        db.execute(object_index.V1_OBJECT_SQL)
        db.execute("CREATE TABLE " + metadata + "(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        db.execute("INSERT INTO " + metadata + " VALUES('schema',?)",
                   (object_index.LEGACY_SCHEMA if metadata == "native_metadata" else object_index.V1_SCHEMA,))
        db.execute("CREATE INDEX objects_scope ON objects(library_id,kind,feature_branch)")
        db.execute(object_index.LEGACY_FTS_SQL)
    else:
        db.execute(object_index.LEGACY_OBJECT_SQL)
        db.execute(object_index.LEGACY_CONTENT_FTS_SQL)
    for rowid, obj in [(2, item()), (200, item("b", "other"))]:
        text = "retained mesh representation " + obj["id"]
        if compact:
            db.execute("INSERT INTO objects(rowid,id,library_id,kind,feature_branch,qmd_path,representation_json,search_input,source_json) VALUES(?,?,?,?,?,?,?,?,?)",
                       (rowid, obj["id"], obj["library_id"], obj["kind"], "mesh", object_index.qmd_path(obj),
                        object_index.pack({"indexed_characters": len(text)}),
                        object_index.pack([obj["title"], text, "mesh"]), object_index.pack(obj)))
        else:
            db.execute("INSERT INTO objects(rowid,id,library_id,title,text,object_json) VALUES(?,?,?,?,?,?)",
                       (rowid, obj["id"], obj["library_id"], obj["title"], text, json.dumps(obj)))
        db.execute("INSERT INTO object_fts(rowid,id,title,text,cjk) VALUES(?,?,?,?,?)",
                   (rowid, obj["id"], obj["title"], text, "mesh"))


def rows(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT rowid,* FROM objects ORDER BY rowid").fetchall()


def test_new_storage_has_no_postings_and_keeps_shared_lookup_scope_and_payload(tmp_path):
    with closing(sqlite3.connect(tmp_path / "objects.sqlite3")) as db, db:
        object_index.create_schema(db)
        obj = item()
        stats = {"indexed_characters": 22}
        object_index.insert_object(db, obj, "bounded representation", stats, "", rowid=19)
        assert object_index.owned_schema(db)
        assert object_index.is_slim(db)
        assert "search_input" not in {row[1] for row in db.execute("PRAGMA table_info(objects)")}
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'object_fts%'").fetchone()
        assert db.execute("SELECT value FROM object_metadata WHERE key='schema'").fetchone() == (object_index.SCHEMA,)
        assert object_index.hydrate_object(db, obj["id"]) == obj
        assert object_index.lookup_id(db, object_index.qmd_path(obj)) == obj["id"]
        assert object_index.representation_stats(db, obj["id"]) == stats
        where, params = object_index.scope_predicate(compact=True, library_ids=["models"], kind="case", feature_branch="mesh")
        assert db.execute("SELECT id FROM objects o WHERE " + where, params).fetchall() == [("a",)]
        object_index.delete_library(db, "models")
        assert not db.execute("SELECT 1 FROM objects").fetchone()


@pytest.mark.parametrize("compact", [False, True])
def test_fts_removal_is_read_only_until_apply_preserves_raw_rows_and_is_idempotent(tmp_path, compact):
    path = tmp_path / "objects.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        historical(db, compact=compact)
        assert object_index.owned_schema(db)
    original, expected = path.read_bytes(), rows(path)
    preview = object_index.remove_legacy_fts(path)
    assert preview["dry_run"] and preview["legacy_fts_present"] and preview["removed_tables"] == 0
    assert preview["objects"] == 2 and path.read_bytes() == original
    with pytest.raises(ValueError, match="offline"):
        object_index.remove_legacy_fts(path, dry_run=False)
    result = object_index.remove_legacy_fts(path, dry_run=False, offline=True)
    assert result["changed"] and result["removed_tables"] >= 5
    assert result["metadata_unchanged"] and result["reclaimed_bytes"] > 0
    assert not result["qmd_opened"] and not result["embedding_invoked"]
    assert result["objects_sha256"] == preview["objects_sha256"] and rows(path) == expected
    with closing(sqlite3.connect(path)) as db, db:
        assert object_index.owned_schema(db) and not object_index.has_legacy_fts(db)
        assert object_index.hydrate_object(db, "a") == item()
        object_index.delete_library(db, "models")
        assert db.execute("SELECT rowid,id FROM objects").fetchall() == [(200, "b")]
    original = path.read_bytes()
    repeated = object_index.remove_legacy_fts(path, dry_run=False, offline=True)
    assert not repeated["changed"] and repeated["removed_tables"] == 0
    assert path.read_bytes() == original


def test_drop_and_metadata_verification_share_one_transaction(tmp_path, monkeypatch):
    path = tmp_path / "objects.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        historical(db)
    original, expected = path.read_bytes(), rows(path)
    fingerprint, calls = object_index._object_fingerprint, 0

    def cancel_after_drop(db):
        nonlocal calls
        calls += 1
        if calls == 3:
            assert not object_index.has_legacy_fts(db)
            raise BuildCancelled()
        return fingerprint(db)

    monkeypatch.setattr(object_index, "_object_fingerprint", cancel_after_drop)
    with pytest.raises(BuildCancelled):
        object_index.remove_legacy_fts(path, dry_run=False, offline=True)
    assert path.read_bytes() == original and rows(path) == expected
    with closing(sqlite3.connect(path)) as db:
        assert object_index.has_legacy_fts(db)


@pytest.mark.parametrize("unexpected", ["CREATE TABLE foreign_payload(value)",
                                        "CREATE VIEW foreign_view AS SELECT id FROM objects",
                                        "CREATE TRIGGER foreign_trigger AFTER DELETE ON objects BEGIN SELECT 1; END"])
def test_unknown_schema_is_refused_without_changes(tmp_path, unexpected):
    path = tmp_path / "objects.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        historical(db)
        db.execute(unexpected)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="unowned"):
        object_index.remove_legacy_fts(path, dry_run=False, offline=True)
    assert path.read_bytes() == original


def test_offline_script_reports_counts_normalizes_modes_and_preserves_qmd_files(tmp_path):
    libraries = tmp_path / "libraries"
    directory = libraries / "search" / "generations" / ("a" * 20 + "-" + "b" * 8)
    directory.mkdir(parents=True)
    # The script only reads catalog location; no catalog migration/SQL runs.
    (libraries / "catalog.sqlite3").write_bytes(b"catalog must remain byte exact")
    path = directory / "objects.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        historical(db)
    qmd, vector = directory / "qmd-keyword.sqlite3", directory / "qmd-vector.sqlite3"
    qmd.write_bytes(b"existing QMD keyword payload")
    vector.write_bytes(b"existing vector and embedding payload")
    manifest = {"generation": "retained-generation", "ready": True,
                "directory": str(directory), "libraries": {"models": {"db_path": str(qmd), "vector_db_path": str(vector)}},
                "modes": {"native": {"ready": True}, "qmd_keyword": {"ready": True},
                          "qmd_vector": {"ready": True, "embedding_records": {"models": [{"chunks": 4}]}}}}
    authority = directory / "manifest.json"
    authority.write_text(json.dumps(manifest), encoding="utf-8")
    original = {file: file.read_bytes() for file in (path, qmd, vector, authority, libraries / "catalog.sqlite3")}
    preview = remove_indexes(libraries)
    assert preview["dry_run"] and preview["databases_with_legacy_fts"] == 1
    assert preview["manifests_to_normalize"] == 1 and preview["removed_tables"] == 0
    assert {file: file.read_bytes() for file in original} == original
    result = remove_indexes(libraries, apply=True)
    assert result["removed_tables"] == 5 and result["manifests_normalized"] == 1
    assert result["qmd_databases_checked"] == 2 and result["qmd_bytes_unchanged"]
    assert qmd.read_bytes() == original[qmd] and vector.read_bytes() == original[vector]
    assert (libraries / "catalog.sqlite3").read_bytes() == original[libraries / "catalog.sqlite3"]
    saved = json.loads(authority.read_text())
    assert "native" not in saved["modes"] and saved["generation"] == manifest["generation"]
    assert saved["libraries"] == manifest["libraries"] and saved["modes"]["qmd_vector"] == manifest["modes"]["qmd_vector"]
    assert not remove_indexes(libraries, apply=True)["removed_tables"]
