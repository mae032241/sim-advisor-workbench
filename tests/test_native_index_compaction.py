"""Storage migrations preserve existing postings, identities and QMD bytes."""
import copy
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor import object_index as native_index
from advisor.build_control import BuildCancelled
from advisor.index_manifest import compact_existing_index
from advisor.library_search import _cjk_tokens, _json, _representation, _serialize_manifest
from advisor.library_store import LibraryStore, encoded
from test_library_search import FakeEngineSearch, obj


def legacy_index(root, objects):
    search = FakeEngineSearch(root, preserve_history=True)
    manifest = search.build(objects, modes=["qmd_keyword", "qmd_vector"], generation="existing")
    path = Path(manifest["directory"]) / "objects.sqlite3"
    path.unlink()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE objects(id TEXT PRIMARY KEY, library_id TEXT NOT NULL, title TEXT, text TEXT, object_json TEXT NOT NULL)")
        db.execute("CREATE VIRTUAL TABLE object_fts USING fts5(id UNINDEXED,title,text,cjk,tokenize='porter unicode61')")
        for item in sorted(objects, key=lambda value: value["id"]):
            text, _ = _representation(item)
            db.execute("INSERT INTO objects VALUES(?,?,?,?,?)", (item["id"], item["library_id"], item["title"], text, _json(item)))
            db.execute("INSERT INTO object_fts VALUES(?,?,?,?)", (item["id"], item["title"], text, " ".join(_cjk_tokens(item["title"] + " " + text))))
    for target in (search.active_path, search.last_build_path, path.parent / "manifest.json"):
        target.write_text(_json(_serialize_manifest(manifest, root)), encoding="utf-8")
    return search, manifest, path


def catalog(root, objects):
    store = LibraryStore(root)
    with closing(store.connect()) as db, db:
        for library in sorted({item["library_id"] for item in objects}):
            db.execute("INSERT INTO libraries VALUES(?,?)", (library, encoded({"id": library, "enabled": True, "deleted_at": None, "roots": []})))
        for item in objects:
            db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (item["id"], item["object_id"], item["library_id"], encoded(item)))
            db.execute("INSERT INTO active VALUES(?,?,?)", (item["library_id"], item["object_id"], item["id"]))
    return store


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("managed", [False, True])
def test_migration_preserves_exact_hits_scores_rowids_and_qmd_without_originals(tmp_path, managed):
    objects = [obj("a", "one", "valve pressure 阀门压降 " * 800), obj("b", "one"), obj("c", "two")]
    objects[0]["data"]["feature_branch"] = "mesh"
    objects[1]["kind"] = "document"
    objects[1]["data"]["feature_branch"] = "mesh"
    root = tmp_path / "libraries"
    store = catalog(root, objects) if managed else None
    search, manifest, path = legacy_index(root / "search", objects)
    queries = [(mode, "valve", {}) for mode in ("qmd_keyword", "qmd_vector")]
    queries += [("qmd_keyword", "阀门压降", {}), ("qmd_keyword", "valve", {"kind": "document", "feature_branch": "mesh"})]
    before = [search.search(query, mode=mode, **options) for mode, query, options in queries]
    search.calls.clear()
    qmd = {str(file): sha(file) for file in path.parent.rglob("qmd-*.sqlite3")}
    with closing(sqlite3.connect(path)) as db:
        rowids = db.execute("SELECT rowid,id FROM objects ORDER BY rowid").fetchall()
    events = []
    result = native_index.compact_native_index(path, manifest=manifest, catalog_path=store.db_path if store else None,
                                               offline=True, on_progress=events.append)
    assert result["changed"] and result["catalog_references"] == (len(objects) if managed else 0)
    assert result["after_bytes"] < result["before_bytes"]
    assert events[-1]["completed"] == len(objects)
    assert [search.search(query, mode=mode, **options) for mode, query, options in queries] == before
    assert {str(file): sha(file) for file in path.parent.rglob("qmd-*.sqlite3")} == qmd
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT rowid,id FROM objects ORDER BY rowid").fetchall() == rowids
        assert (db.execute("SELECT count(*) FROM objects WHERE source_json IS NULL").fetchone()[0]) == (len(objects) if managed else 0)
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'object_fts%'").fetchone()
        assert native_index.owned_schema(db)
    compact_existing_index(search.data_dir, ensure_lookup=native_index.validate_manifest_lookup)
    assert [search.search(query, mode=mode, **options) for mode, query, options in queries] == before
    assert not native_index.compact_native_index(path, offline=True)["changed"]
    assert all(call["op"] in {"shared_keyword", "keyword", "vector"} for call in search.calls)


def test_cancelled_migration_keeps_old_database_and_manifests_byte_exact(tmp_path):
    search, manifest, path = legacy_index(tmp_path / "search", [obj("a"), obj("b")])
    hits = search.search("valve")
    original = {file: file.read_bytes() for file in (path, search.active_path, search.last_build_path, path.parent / "manifest.json")}

    def cancel(_event):
        raise BuildCancelled()

    with pytest.raises(BuildCancelled):
        native_index.compact_native_index(path, manifest=manifest, offline=True, on_progress=cancel)
    assert {file: file.read_bytes() for file in original} == original
    assert not list(path.parent.glob("*.compact-*"))
    assert search.search("valve") == hits


def test_managed_build_references_historical_revision_and_relocates_with_catalog(tmp_path):
    original = obj("old")
    store = catalog(tmp_path / "A" / "libraries", [original])
    search = FakeEngineSearch(store.root / "search")
    with store.object_stream() as objects:
        built = search.build(objects, modes=["qmd_keyword"], generation="old")
    assert not built["mapping"] and not built["representation"]["objects"]
    assert not Path(built["libraries"][original["library_id"]]["corpus"]).exists()
    with closing(sqlite3.connect(Path(built["directory"]) / "objects.sqlite3")) as db:
        assert db.execute("SELECT source_json FROM objects").fetchone() == (None,)
    # An active/latest revision must never substitute for the indexed citation.
    newer = dict(original, id="lo-new", revision_id="new", text="replacement")
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (newer["id"], newer["object_id"], newer["library_id"], encoded(newer)))
        db.execute("UPDATE active SET citation_id=?", (newer["id"],))
    moved = tmp_path / "B" / "libraries"
    shutil.copytree(store.root, moved)
    relocated = FakeEngineSearch(moved / "search")
    assert relocated.search("valve")[0]["text"] == original["text"]
    # Catalog failures are explicit and cannot fall through to active/latest.
    with closing(native_index.catalog_connection(moved / "catalog.sqlite3")) as db:
        assert db.execute("SELECT id FROM revisions WHERE id=?", (original["id"],)).fetchone()
    with closing(sqlite3.connect(moved / "catalog.sqlite3")) as db, db:
        db.execute("DELETE FROM libraries")
    assert relocated.search("valve") == []


def test_migration_refuses_unowned_schema_and_incomplete_mapping(tmp_path):
    _, manifest, path = legacy_index(tmp_path / "search", [obj("a")])
    before = path.read_bytes()
    bad = copy.deepcopy(manifest)
    bad["mapping"].clear()
    with pytest.raises(ValueError, match="mapping"):
        native_index.compact_native_index(path, manifest=bad, offline=True)
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE independent(payload TEXT)")
        db.execute("INSERT INTO independent VALUES('must survive')")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unowned"):
        native_index.compact_native_index(path, manifest=manifest, offline=True)
    assert path.read_bytes() == before


def test_unchanged_generation_finishes_retained_export_cleanup_without_qmd_update(tmp_path):
    search = FakeEngineSearch(tmp_path / "search")
    source = obj("retained-export")
    built = search.build([source], modes=["qmd_keyword"], generation="ready")
    library = built["libraries"][source["library_id"]]
    corpus = Path(library["corpus"])
    assert not corpus.exists()
    # A cleanup cancelled after publication can leave a subset of known files.
    corpus.mkdir(parents=True)
    (corpus / Path(native_index.qmd_path(source)).name).write_text("temporary export", encoding="utf-8")
    previous = Path(library["db_path"]).read_bytes()
    search.calls.clear()
    result = search.build([source], modes=["qmd_keyword"], generation="ready")
    assert result["unchanged"] and result["corpus_cleanup"]["removed_files"] == 1
    assert not search.calls and not corpus.exists()
    assert Path(library["db_path"]).read_bytes() == previous
    assert search.search("valve", mode="qmd_keyword")[0]["id"] == source["id"]
