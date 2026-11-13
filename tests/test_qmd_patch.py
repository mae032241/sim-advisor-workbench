"""Real installed QMD SDK checks for bounded, transactional keyword patches."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

from advisor import object_index
from advisor.qmd_patch import (
    patch_keyword_documents, prepare_keyword_document_patch,
    restore_keyword_document_patch,
)

OFFLINE_ROOT = Path(r"D:\software\sim-offline\sim-advisor-offline-win-x64-0.2.2")


@pytest.fixture
def sdk():
    options = {"node": OFFLINE_ROOT / "node/node.exe", "qmd_dir": OFFLINE_ROOT / "qmd/node_modules/@tobilu/qmd"}
    if not options["node"].is_file() or not (options["qmd_dir"] / "dist/store.js").is_file():
        pytest.skip("Installed offline QMD 2.8.3 SDK unavailable")
    return options


def _node(sdk, script, payload):
    result = subprocess.run(
        [str(sdk["node"]), "--input-type=module", "-e", script],
        input=json.dumps({"qmd_dir": str(sdk["qmd_dir"]), **payload}, ensure_ascii=False),
        text=True, encoding="utf-8", capture_output=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _body(title, text):
    return ("# " + title + "\n\n" + text).replace("\n", os.linesep)


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture
def store(tmp_path, sdk):
    path = tmp_path / "shared.sqlite3"
    items = [
        {"id": "a-one", "library_id": "a", "title": "Original one", "text": "Old bounded representation"},
        {"id": "a-two", "library_id": "a", "title": "Original two", "text": "Second old representation"},
        {"id": "b-other", "library_id": "b", "title": "Other collection", "text": "Unchanged unrelated original 网格"},
    ]
    for item in items:
        item["path"] = object_index.qmd_path(item)
        item["collection"] = item["path"].split("/")[0]
        item["body"] = _body(item["title"], item["text"])
    _node(sdk, r"""
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
const req=JSON.parse(readFileSync(0,'utf8'));
const {createStore,upsertStoreCollection}=await import(pathToFileURL(join(req.qmd_dir,'dist/store.js')).href);
const store=createStore(req.db_path);
try{
  for(const row of req.rows){
    upsertStoreCollection(store.db,row.collection,{path:row.collection+'/corpus',pattern:'**/*.md',context:{'/':'saved context'}});
    const hash=createHash('sha256').update(row.body).digest('hex');
    store.insertContent(hash,row.body,'2020-01-01T00:00:00.000Z');
    store.insertDocument(row.collection,row.path.split('/')[1],row.title,hash,'2020-01-01T00:00:00.000Z','2020-01-02T00:00:00.000Z');
  }
  store.db.prepare('INSERT INTO llm_cache VALUES(?,?,?)').run('unrelated','saved cache','original');
  const orphan='Unrelated historical orphan';
  store.insertContent(createHash('sha256').update(orphan).digest('hex'),orphan,'original orphan');
  process.stdout.write(JSON.stringify({ok:true}));
}finally{store.close();}
""", {"db_path": str(path), "rows": items})
    return path, items


def _snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        return {table: db.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
                for table in ("content", "documents", "documents_fts", "store_collections", "store_config", "content_vectors", "llm_cache", "sqlite_sequence")}


def _unrelated(path, item):
    with closing(sqlite3.connect(path)) as db:
        row = db.execute("SELECT * FROM documents WHERE collection=? AND path=?", (item["collection"], item["path"].split("/")[1])).fetchone()
        return {
            "document": row,
            "content": db.execute("SELECT * FROM content WHERE hash=?", (row[4],)).fetchone(),
            "fts": db.execute("SELECT rowid,* FROM documents_fts WHERE rowid=?", (row[0],)).fetchone(),
            "collection": db.execute("SELECT * FROM store_collections WHERE name=?", (item["collection"],)).fetchone(),
            "orphan": db.execute("SELECT * FROM content WHERE hash=?", (_hash("Unrelated historical orphan"),)).fetchone(),
            "cache": db.execute("SELECT * FROM llm_cache").fetchall(),
            "sequence": db.execute("SELECT * FROM sqlite_sequence").fetchall(),
        }


def _update(item, text="New source-based representation 日本語 网格 湍流", **extra):
    return {"collection": item["collection"], "path": item["path"], "title": item["title"], "text": text, **extra}


def test_patch_preserves_rowids_and_unrelated_rows_sdk_cjk_search_and_idempotence(store, sdk):
    path, items = store
    before = _snapshot(path)
    other = _unrelated(path, items[2])
    updates = [_update(items[0]), _update(items[1], "Second source-based representation 隔膜阀")]
    prepared = prepare_keyword_document_patch(path, updates, **sdk)
    assert _snapshot(path) == before
    assert prepared["changed"] == 2 and prepared["embedding_invoked"] is False
    result = patch_keyword_documents(path, updates, expected_plan=prepared["plan"], **sdk)
    assert result["changed_document_ids"] == [1, 2]
    assert result["inserted_content"] == result["removed_content"] == 2
    assert result["undo"] == prepared["undo"]
    assert _unrelated(path, items[2]) == other
    with closing(sqlite3.connect(path)) as db:
        assert not db.execute("SELECT 1 FROM content_vectors LIMIT 1").fetchone()
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='vectors_vec'").fetchone()
        assert db.execute("SELECT id,created_at FROM documents ORDER BY id").fetchall() == [(i, "2020-01-01T00:00:00.000Z") for i in (1, 2, 3)]
        assert "网 格" in db.execute("SELECT body FROM documents_fts WHERE rowid=1").fetchone()[0]
    hits = _node(sdk, r"""
import {readFileSync} from 'node:fs';import {pathToFileURL} from 'node:url';import {join} from 'node:path';
import {createRequire} from 'node:module';
const req=JSON.parse(readFileSync(0,'utf8'));
const {searchFTS}=await import(pathToFileURL(join(req.qmd_dir,'dist/store.js')).href);
const Database=createRequire(pathToFileURL(join(req.qmd_dir,'package.json')))('better-sqlite3');
const db=new Database(req.db_path,{readonly:true,fileMustExist:true});
try{process.stdout.write(JSON.stringify(searchFTS(db,'湍流',10,req.collection)));}finally{db.close();}
""", {"db_path": str(path), "collection": items[0]["collection"]})
    assert len(hits) == 1 and items[0]["path"] in json.dumps(hits)
    unchanged = _snapshot(path)
    original_bytes = path.read_bytes()
    repeated = patch_keyword_documents(path, updates, **sdk)
    assert repeated["changed"] == 0 and repeated["unchanged"] == 2
    assert repeated["inserted_content"] == repeated["removed_content"] == 0
    assert _snapshot(path) == unchanged and path.read_bytes() == original_bytes
    resumed = patch_keyword_documents(path, updates, expected_plan=prepared["plan"], **sdk)
    assert resumed["already_applied"] and resumed["changed"] == 0
    assert resumed["undo"] == prepared["undo"]
    assert _snapshot(path) == unchanged


@pytest.mark.parametrize("fail_after", [1, 2, 3])
def test_injected_failure_rolls_back_every_document_and_content(store, sdk, fail_after):
    path, items = store
    before = _snapshot(path)
    with pytest.raises(RuntimeError, match="Injected QMD patch failure"):
        patch_keyword_documents(path, [_update(items[0]), _update(items[1])], _test_fail_after=fail_after, **sdk)
    assert _snapshot(path) == before


def test_stale_hash_and_stale_prepared_plan_rejected_without_partial_writes(store, sdk):
    path, items = store
    before = _snapshot(path)
    updates = [_update(items[0]), _update(items[1], expected_old_hash="0" * 64)]
    with pytest.raises(RuntimeError, match="Stale QMD old content hash"):
        patch_keyword_documents(path, updates, **sdk)
    assert _snapshot(path) == before
    plan = prepare_keyword_document_patch(path, [_update(items[0])], **sdk)["plan"]
    patch_keyword_documents(path, [_update(items[0], "A distinct intervening representation")], **sdk)
    newer = _snapshot(path)
    with pytest.raises(RuntimeError, match="Stale QMD patch plan"):
        patch_keyword_documents(path, [_update(items[0])], expected_plan=plan, **sdk)
    assert _snapshot(path) == newer


def test_generated_paths_collections_duplicates_and_missing_rows_fail_closed(store, sdk):
    path, items = store
    before = _snapshot(path)
    with pytest.raises(ValueError, match="exact generated"):
        patch_keyword_documents(path, [_update(items[0], collection=items[2]["collection"])], **sdk)
    with pytest.raises(ValueError, match="exact generated"):
        patch_keyword_documents(path, [_update(items[0], path=items[0]["path"].split("/")[1])], **sdk)
    with pytest.raises(ValueError, match="Duplicate"):
        patch_keyword_documents(path, [_update(items[0]), _update(items[0])], **sdk)
    missing = dict(items[0], path=items[0]["collection"] + "/o-" + "0" * 32 + ".md")
    with pytest.raises(RuntimeError, match="Existing active QMD document"):
        patch_keyword_documents(path, [_update(missing)], **sdk)
    assert _snapshot(path) == before


@pytest.mark.parametrize("mutation,reason", [
    ("PRAGMA user_version=0", "schema version"),
    ("UPDATE store_config SET value='0' WHERE key='fts_cjk_normalized_version'", "CJK normalization"),
    ("CREATE TRIGGER unexpected AFTER UPDATE ON documents BEGIN DELETE FROM content; END", "sync triggers"),
    ("DROP INDEX idx_documents_hash", "hash references"),
    ("INSERT INTO content_vectors VALUES('x',0,0,'model','',1,'now')", "keyword-only"),
    ("UPDATE documents_fts SET body='incorrect saved posting' WHERE rowid=1", "FTS verification"),
])
def test_unsupported_state_never_opens_store_or_repairs_schema(store, sdk, mutation, reason):
    path, items = store
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(mutation)
    before = path.read_bytes(), _snapshot(path)
    with pytest.raises(RuntimeError, match=reason):
        patch_keyword_documents(path, [_update(items[0])], **sdk)
    assert (path.read_bytes(), _snapshot(path)) == before


def test_small_undo_restores_old_bodies_timestamps_and_postings_exactly(store, sdk):
    path, items = store
    before = _snapshot(path)
    updates = [_update(items[0]), _update(items[1])]
    prepared = prepare_keyword_document_patch(path, updates, **sdk)
    assert restore_keyword_document_patch(path, prepared["undo"], **sdk)["unchanged"]
    patch_keyword_documents(path, updates, expected_plan=prepared["plan"], **sdk)
    restored = restore_keyword_document_patch(path, prepared["undo"], **sdk)
    assert restored["restored"] == 2 and restored["embedding_invoked"] is False
    assert _snapshot(path) == before
    assert restore_keyword_document_patch(path, prepared["undo"], **sdk)["unchanged"]


def test_old_hash_is_retained_for_an_unaffected_document_and_existing_new_orphan_survives_undo(store, sdk):
    path, items = store
    updates = [_update(items[0])]
    old_hash = _hash(items[0]["body"])
    new_body = _body(updates[0]["title"], updates[0]["text"])
    with closing(sqlite3.connect(path)) as db, db:
        # A different document shares the first target's original content.
        db.execute("UPDATE documents SET hash=?,title=? WHERE id=2", (old_hash, items[0]["title"]))
        # Match SDK CJK normalization (this fixture's original body is ASCII).
        db.execute("INSERT INTO content VALUES(?,?,?)", (_hash(new_body), new_body, "pre-existing orphan"))
    before = _snapshot(path)
    prepared = prepare_keyword_document_patch(path, updates, **sdk)
    result = patch_keyword_documents(path, updates, expected_plan=prepared["plan"], **sdk)
    assert result["removed_content"] == 0 and result["inserted_content"] == 0
    assert result["undo"]["created_content_hashes"] == []
    restore_keyword_document_patch(path, prepared["undo"], **sdk)
    assert _snapshot(path) == before


def test_stale_undo_refuses_to_overwrite_a_later_edit(store, sdk):
    path, items = store
    updates = [_update(items[0])]
    prepared = prepare_keyword_document_patch(path, updates, **sdk)
    patch_keyword_documents(path, updates, expected_plan=prepared["plan"], **sdk)
    patch_keyword_documents(path, [_update(items[0], "Later independent source enrichment")], **sdk)
    before = _snapshot(path)
    with pytest.raises(RuntimeError, match="Stale QMD undo state"):
        restore_keyword_document_patch(path, prepared["undo"], **sdk)
    assert _snapshot(path) == before


def test_expected_old_hash_supports_exact_crash_resume_but_otherwise_remains_optimistic(store, sdk):
    path, items = store
    updates = [_update(items[0], expected_old_hash=_hash(items[0]["body"]))]
    prepared = prepare_keyword_document_patch(path, updates, **sdk)
    patch_keyword_documents(path, updates, expected_plan=prepared["plan"], **sdk)
    assert patch_keyword_documents(path, updates, expected_plan=prepared["plan"], **sdk)["already_applied"]
    with pytest.raises(RuntimeError, match="Stale QMD old content hash"):
        patch_keyword_documents(path, updates, **sdk)


def test_sorted_json_journal_roundtrip_preserves_plan_resume_and_undo_semantics(store, sdk):
    path, items = store
    before = _snapshot(path)
    updates = [_update(items[0]), _update(items[1])]
    original = prepare_keyword_document_patch(path, updates, **sdk)
    journal = json.loads(json.dumps({"updates": updates, "plan": original["plan"], "undo": original["undo"]}, sort_keys=True))
    assert restore_keyword_document_patch(path, journal["undo"], **sdk)["unchanged"]
    result = patch_keyword_documents(path, journal["updates"], expected_plan=journal["plan"], **sdk)
    assert result["changed"] == 2
    assert patch_keyword_documents(path, journal["updates"], expected_plan=journal["plan"], **sdk)["already_applied"]
    assert restore_keyword_document_patch(path, journal["undo"], **sdk)["restored"] == 2
    assert _snapshot(path) == before
