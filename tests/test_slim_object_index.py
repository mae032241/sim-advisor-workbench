"""Slim object metadata without rebuilding or losing revision statistics."""
from contextlib import closing
import json
import os
import sqlite3

import pytest

from advisor import object_index
from advisor.build_control import BuildCancelled
from scripts.slim_object_index import slim_indexes
from test_object_index import historical


def full_stats():
    return {"policy": "bounded-object-with-labelled-inferred-annotation-v1",
            "original_text_characters": 18431, "original_data_characters": 17652,
            "indexed_characters": 1398, "body_characters_retained": 1049,
            "truncated": True, "annotation_included": False, "annotation_is_observed": False,
            "annotation_fields": [], "feature_structure_included": True, "character_budget": 1400}


def source_database(path, *, metadata="native_metadata", postings=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        historical(db, metadata=metadata)
        if not postings:
            db.execute("DROP TABLE object_fts")
        db.execute("INSERT INTO " + metadata + " VALUES('revision-policy','exact-historical-revisions')")
        db.execute("UPDATE objects SET representation_json=?,search_input=?,source_json=NULL WHERE id='a'",
                   (object_index.pack(full_stats()), object_index.pack(["Title", os.urandom(24000).hex(), ""])))
        db.execute("UPDATE objects SET representation_json=? WHERE id='b'",
                   (object_index.pack({"indexed_characters": 0, "truncated": False,
                                       "extension": {"value": [1, False, "中文"]}}),))
    return path


def retained_rows(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT rowid,id,library_id,kind,feature_branch,qmd_path,source_json FROM objects ORDER BY rowid").fetchall()


@pytest.mark.parametrize("stats", [full_stats(), {}, {"indexed_characters": 0},
                                   {"policy": "future/v7", "character_budget": 900, "annotation_is_observed": True,
                                    "annotation_fields": ["objective", "checks"], "unknown": {"real": False}},
                                   {"indexed_characters": 0.0, "truncated": 0, "annotation_included": None}])
def test_short_statistics_round_trip_preserves_missing_defaults_and_unknown_fields(stats):
    encoded = object_index.encode_representation(stats)
    expected = json.loads(json.dumps(stats))
    assert object_index.decode_representation(encoded) == expected
    assert object_index.unpack(encoded) == expected
    assert object_index.decode_representation(object_index.pack(stats)) == expected
    assert {key: type(value) for key, value in object_index.decode_representation(encoded).items()} == {
        key: type(value) for key, value in expected.items()}


def test_repeated_statistic_names_policy_budget_and_false_values_are_not_stored():
    stats = full_stats()
    encoded = object_index.encode_representation(stats)
    assert len(encoded) < len(object_index.pack(stats)) // 2
    assert not any(value in encoded for value in (b"indexed_characters", b"bounded-object", b"1400", b"false"))
    decoded = object_index.decode_representation(encoded)
    decoded["annotation_fields"].append("changed")
    assert object_index.decode_representation(encoded)["annotation_fields"] == []


@pytest.mark.parametrize("raw", [b"RS1[2048,0,[]]", b"RS1[0,1,[1]]", b"RS1[1,1,[]]",
                                 b'RS1[0,0,[],{"policy":"overlap"}]'])
def test_invalid_compact_statistics_are_refused(raw):
    with pytest.raises(ValueError):
        object_index.decode_representation(raw)


@pytest.mark.parametrize("metadata", ["native_metadata", "object_metadata"])
@pytest.mark.parametrize("postings", [False, True])
def test_slimming_is_staged_preserves_metadata_rowids_sources_and_full_statistics(tmp_path, metadata, postings):
    path = source_database(tmp_path / "objects.sqlite3", metadata=metadata, postings=postings)
    qmd = tmp_path / "qmd-keyword.sqlite3"
    qmd.write_bytes(b"QMD keyword and vectors remain untouched")
    original, retained = path.read_bytes(), retained_rows(path)
    with closing(sqlite3.connect(path)) as db:
        statistics = dict(object_index.iter_representation_stats(db))
    preview = object_index.slim_object_index(path)
    assert preview["dry_run"] and not preview["changed"] and preview["objects"] == 2
    assert preview["representation_after_bytes"] < preview["representation_before_bytes"]
    assert path.read_bytes() == original and not list(tmp_path.glob("*.slim-*"))
    with pytest.raises(ValueError, match="offline"):
        object_index.slim_object_index(path, dry_run=False)
    result = object_index.slim_object_index(path, dry_run=False, offline=True)
    assert result["changed"] and result["reclaimed_bytes"] > 0 and result["statistics_verified"] == 2
    assert result["rowids_preserved"] and result["metadata_columns_preserved"] and result["statistics_preserved"]
    assert result["representation_after_bytes"] == preview["representation_after_bytes"]
    assert retained_rows(path) == retained
    assert qmd.read_bytes() == b"QMD keyword and vectors remain untouched"
    with closing(sqlite3.connect(path)) as db:
        assert object_index.owned_schema(db) and object_index.is_slim(db)
        assert "search_input" not in {row[1] for row in db.execute("PRAGMA table_info(objects)")}
        assert not object_index.has_legacy_fts(db)
        assert dict(object_index.iter_representation_stats(db)) == statistics
        assert list(object_index.iter_representation_stats(db, library_id="other")) == [("b", statistics["b"])]
        assert object_index.representation_stats(db, "a") == full_stats()
        assert db.execute("SELECT value FROM object_metadata WHERE key='revision-policy'").fetchone() == ("exact-historical-revisions",)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    original = path.read_bytes()
    assert not object_index.slim_object_index(path, dry_run=False, offline=True)["changed"]
    assert path.read_bytes() == original and not list(tmp_path.glob("*.slim-*"))


def test_cancelled_conversion_preserves_original_and_removes_only_staging(tmp_path):
    path = source_database(tmp_path / "objects.sqlite3")
    original = path.read_bytes()

    def cancel(_value):
        raise BuildCancelled()

    with pytest.raises(BuildCancelled):
        object_index.slim_object_index(path, dry_run=False, offline=True, on_progress=cancel)
    assert path.read_bytes() == original and not list(tmp_path.glob("*.slim-*"))


@pytest.mark.parametrize("unsafe", ["extra-table", "hard-link", "transaction-sidecar"])
def test_unsafe_targets_are_refused_without_changes(tmp_path, unsafe):
    path = source_database(tmp_path / "objects.sqlite3")
    if unsafe == "extra-table":
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE independent(payload)")
    elif unsafe == "hard-link":
        try:
            os.link(path, tmp_path / "linked.sqlite3")
        except OSError:
            pytest.skip("Creating hard links is not permitted on this host")
    else:
        path.with_name(path.name + "-wal").write_bytes(b"active SQLite transaction")
    original = path.read_bytes()
    with pytest.raises((ValueError, RuntimeError), match="owned|unshared|sidecar"):
        object_index.slim_object_index(path, dry_run=False, offline=True)
    assert path.read_bytes() == original and not list(tmp_path.glob("*.slim-*"))


def test_offline_script_does_not_modify_manifests_catalog_or_qmd(tmp_path):
    libraries = tmp_path / "libraries"
    directory = libraries / "search/generations" / ("a" * 20 + "-" + "b" * 8)
    path = source_database(directory / "objects.sqlite3")
    catalog = libraries / "catalog.sqlite3"
    catalog.write_bytes(b"Catalog is only located, never opened or migrated")
    manifest = directory / "manifest.json"
    manifest.write_text('{"generation":"existing","modes":{"qmd_keyword":{"ready":true}}}', encoding="utf-8")
    qmd = directory / "qmd-keyword.sqlite3"
    qmd.write_bytes(b"Saved QMD and embeddings")
    original = {file: file.read_bytes() for file in (path, catalog, manifest, qmd)}
    preview = slim_indexes(libraries)
    assert preview["dry_run"] and preview["databases_changed"] == 0
    assert {file: file.read_bytes() for file in original} == original
    result = slim_indexes(libraries, apply=True)
    assert result["databases_changed"] == 1 and result["objects"] == 2
    assert result["reclaimed_bytes"] > 0 and result["search_input_bytes_removed"] > 1000
    assert not result["catalog_opened"] and not result["qmd_opened"] and not result["embedding_invoked"]
    assert {file: file.read_bytes() for file in (catalog, manifest, qmd)} == {
        file: original[file] for file in (catalog, manifest, qmd)}
    assert not list(directory.glob("*.slim-*")) and not list(directory.glob("*.bak"))
