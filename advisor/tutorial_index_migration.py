"""Stage tutorial index deltas without publishing or changing the catalog.

The coordinator owns the offline/build lease, catalog transaction, publication,
and retention grace period. Existing indexes are read only; SQLite backup makes
independent staged files. Model retrieval rows are never re-exported to QMD.
"""
from __future__ import annotations

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid

from . import object_index
from .build_control import BuildCancelled, BuildControl, build_control, check_cancelled
from .index_manifest import _owned, stored_manifest
from .library_search import BRIDGE_VERSION, LibrarySearch, REPRESENTATION_CHARS, SHARED_KEYWORD_STORAGE, _json, _representation
from .tutorial_migration import TutorialMigrationPlan
from .process_runner import run_text
from .qmd_shared import keyword_input_spool

QMD_VERSION = "2.8.3"
_FILENAME = re.compile(r"o-[a-f0-9]{32}\.md\Z")
_DELETE_TRIGGER = """CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
  DELETE FROM documents_fts WHERE rowid = old.id; END"""

_MANUAL_SQL_WORKER = r'''
import {readFileSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
const req=JSON.parse(readFileSync(0,'utf8'));
if(JSON.parse(readFileSync(join(req.qmd_dir,'package.json'),'utf8')).version!=='2.8.3')throw new Error('QMD 2.8.3 required');
const {extractTitle,normalizeCjkForFTS}=await import(pathToFileURL(join(req.qmd_dir,'dist/store.js')).href);
const {openDatabase}=await import(pathToFileURL(join(req.qmd_dir,'dist/db.js')).href);
if(typeof extractTitle!=='function'||typeof normalizeCjkForFTS!=='function')throw new Error('QMD normalization exports unavailable');
const hash=text=>createHash('sha256').update(text).digest('hex');
const progress=(completed,total,phase)=>{
  // Advisory telemetry must not abort the data transaction when a Windows
  // reader temporarily holds a handle without delete/write sharing.
  try{writeFileSync(req.progress_path,JSON.stringify({completed,total,phase}));}catch{}
};
const db=openDatabase(req.db_path);
const Database=createRequire(pathToFileURL(join(req.qmd_dir,'package.json')))('better-sqlite3');
const native=new Database(req.native_path,{readonly:true,fileMustExist:true});
const inputs=new Database(req.inputs_path,{readonly:true,fileMustExist:true});
const plan=new Database(req.plan_path,{readonly:true,fileMustExist:true});
try{
  const total=plan.prepare('SELECT count(*) AS n FROM canonical_objects WHERE library_id=?').get(req.library_id).n;
  const insertContent=db.prepare('INSERT OR IGNORE INTO content(hash,doc,created_at) VALUES(?,?,?)');
  const insertDocument=db.prepare("INSERT INTO documents(collection,path,title,hash,created_at,modified_at,active) VALUES('objects',?,?,?,?,?,1)");
  const deleteFts=db.prepare('DELETE FROM documents_fts WHERE rowid=?');
  const insertFts=db.prepare('INSERT INTO documents_fts(rowid,filepath,title,body) VALUES(?,?,?,?)');
  const now=new Date().toISOString();
  let inserted=0,retired=0;
  db.exec('PRAGMA secure_delete=ON; BEGIN IMMEDIATE');
  try{
    const remove=db.prepare("DELETE FROM documents WHERE collection='objects' AND path=?");
    const retireTotal=plan.prepare('SELECT count(*) AS n FROM retirements WHERE library_id=?').get(req.library_id).n;
    let retiredProcessed=0;
    for(const row of plan.prepare('SELECT id FROM retirements WHERE library_id=? ORDER BY id').iterate(req.library_id)){
      retired+=remove.run('o-'+hash(row.id).slice(0,32)+'.md').changes;
      if(++retiredProcessed%1000===0)progress(retiredProcessed,retireTotal,'retiring_manual_keyword_delta');
    }
    progress(0,total,'inserting_manual_keyword_delta');
    const findNative=native.prepare('SELECT id,qmd_path FROM objects WHERE id=? AND library_id=?');
    const findInput=inputs.prepare('SELECT title,text FROM keyword_inputs WHERE library_id=? AND qmd_path=?');
    for(const canonical of plan.prepare('SELECT id FROM canonical_objects WHERE library_id=? ORDER BY id').iterate(req.library_id)){
      const row=findNative.get(canonical.id,req.library_id);
      if(!row)throw new Error('Canonical native row missing');
      const input=findInput.get(req.library_id,row.qmd_path);
      if(!input)throw new Error('Canonical keyword input missing');
      const {title:originalTitle,text}=input;
      const filename=row.qmd_path.split('/').pop();
      if(!/^o-[a-f0-9]{32}\.md$/.test(filename))throw new Error('Invalid canonical QMD filename');
      const content=('# '+originalTitle+'\n\n'+text).replaceAll('\n',req.newline);
      const contentHash=hash(content),title=extractTitle(content,filename);
      insertContent.run(contentHash,content,now);
      const result=insertDocument.run(filename,title,contentHash,now,now);
      // Match SDK insertDocument/rebuildDocumentFTS, including its normalized
      // replacement of the raw FTS row inserted by documents_ai.
      deleteFts.run(result.lastInsertRowid);
      insertFts.run(result.lastInsertRowid,normalizeCjkForFTS('objects/'+filename),normalizeCjkForFTS(title),normalizeCjkForFTS(content));
      inserted++;
      if(inserted%1000===0)progress(inserted,total,'inserting_manual_keyword_delta');
    }
    if(inserted!==total)throw new Error('Missing canonical native rows in manual delta');
    db.exec('DELETE FROM content WHERE hash NOT IN (SELECT hash FROM documents); COMMIT');
  }catch(error){db.exec('ROLLBACK');throw error;}
  db.exec('PRAGMA wal_checkpoint(TRUNCATE)');
  progress(inserted,total,'inserted_manual_keyword_delta');
  process.stdout.write('TUTORIAL_SQL_RESULT='+JSON.stringify({inserted,retired,timestamp_origin:'import_time'})+'\n');
}finally{plan.close();inputs.close();native.close();db.close();}
'''


def _progress(callback, phase, completed=None, total=None, unit="objects", **extra):
    check_cancelled()
    if callback is not None:
        callback({"stage": "tutorial_indexes", "phase": phase, "completed": completed,
                  "total": total, "unit": unit, **extra})
    check_cancelled()


def _connect(path, *, read_only=False):
    path = Path(path).resolve()
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30) if read_only else sqlite3.connect(path, timeout=30)
    if read_only:
        db.execute("PRAGMA query_only=ON")

    def interrupted():
        try:
            check_cancelled()
        except BuildCancelled:
            return 1
        return 0

    db.set_progress_handler(interrupted, 10000)
    return db


def _backup(source, destination):
    check_cancelled()
    source, destination = Path(source), Path(destination)
    if not source.is_file() or destination.exists():
        raise ValueError("Index backup requires an existing source and a new destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(source, read_only=True)) as old, closing(_connect(destination)) as new:
        old.backup(new, pages=256, progress=lambda *_: check_cancelled())
    check_cancelled()


def _remaining(objects, retirement_ids):
    retired = iter(retirement_ids)
    target = next(retired, None)
    for item in objects:
        check_cancelled()
        identity = item["id"]
        if target is not None and target < identity:
            raise ValueError("Retired citation is absent from the source catalog snapshot")
        if identity == target:
            target = next(retired, None)
        else:
            yield item
    if target is not None:
        raise ValueError("Retired citation is absent from the source catalog snapshot")


def _desired_fingerprints(store, plan, callback):
    """Merge ordered snapshot cursors, holding at most one object per source."""
    corpus, identities, count = hashlib.sha256(b"["), hashlib.sha256(b"["), 0
    metadata = plan.metadata()
    with store.object_stream() as current:
        if current.generation != metadata["source_generation"]:
            raise ValueError("Catalog changed after tutorial planning")
        canonical = plan.iter_canonical_objects(order_by_id=True)
        retired = plan.iter_retirement_ids()
        try:
            merged = heapq.merge(_remaining(iter(current), retired), canonical, key=lambda item: item["id"])
            previous = None
            for item in merged:
                check_cancelled()
                identity = item["id"]
                if previous is not None and identity <= previous:
                    raise ValueError("Canonical tutorial IDs overlap existing active revisions")
                if count:
                    corpus.update(b",")
                    identities.update(b",")
                corpus.update(_json(item).encode("utf-8"))
                identities.update(_json(identity).encode("utf-8"))
                previous, count = identity, count + 1
                if count % 1000 == 0:
                    _progress(callback, "hashing_catalog_delta", count)
        finally:
            canonical.close()
            retired.close()
    corpus.update(b"]")
    identities.update(b"]")
    _progress(callback, "hashed_catalog_delta", count, count)
    return corpus.hexdigest(), identities.hexdigest()[:24], count


def _delete_native(db, plan, callback):
    """Delete shared metadata and keep retired QMD paths in TEMP storage."""
    db.execute("CREATE TEMP TABLE tutorial_retired(id TEXT PRIMARY KEY,library_id TEXT NOT NULL,qmd_path TEXT)")
    db.executemany("INSERT INTO temp.tutorial_retired(id,library_id) VALUES(?,?)",
                   ((row["id"], row["library_id"]) for row in plan.iter_retirements()))
    expected = db.execute("SELECT count(*) FROM temp.tutorial_retired").fetchone()[0]
    if db.execute("SELECT count(*) FROM temp.tutorial_retired r JOIN objects o ON o.id=r.id AND o.library_id=r.library_id").fetchone()[0] != expected:
        raise ValueError("Native index does not contain every planned retirement")
    db.execute("UPDATE temp.tutorial_retired SET qmd_path=(SELECT qmd_path FROM objects WHERE objects.id=tutorial_retired.id)")
    completed = db.execute("DELETE FROM objects WHERE id IN (SELECT id FROM temp.tutorial_retired)").rowcount
    _progress(callback, "retired_native", completed, expected)
    return completed


def _native_summary(db):
    indexed = truncated = count = 0
    for _, stats in object_index.iter_representation_stats(db):
        check_cancelled()
        indexed += stats.get("indexed_characters", 0)
        truncated += int(bool(stats.get("truncated")))
        count += 1
    return {"object_count": count, "indexed_objects": count, "unindexed_objects": 0,
            "truncated_objects": truncated, "indexed_characters": indexed,
            "character_budget_per_object": REPRESENTATION_CHARS}


def _native_generation(db):
    digest, count = hashlib.sha256(b"["), 0
    for (identity,) in db.execute("SELECT id FROM objects ORDER BY id"):
        check_cancelled()
        if count:
            digest.update(b",")
        digest.update(_json(identity).encode("utf-8"))
        count += 1
    digest.update(b"]")
    return digest.hexdigest()[:24]


def _qmd_schema(db):
    """Pin the SDK layout and its exact deletion trigger before SQL mutation."""
    required = {"documents": {"id", "collection", "path", "hash", "title", "active"},
                "content": {"hash", "doc", "created_at"},
                "documents_fts": {"filepath", "title", "body"},
                "store_collections": {"name", "path", "pattern"},
                "content_vectors": {"hash", "seq", "pos", "model", "embed_fingerprint"}}
    for table, columns in required.items():
        if not columns <= {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}:
            raise ValueError("Unsupported QMD 2.8.3 schema: " + table)
    normalize = lambda text: " ".join(text.split()).casefold()
    trigger = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='documents_ad'").fetchone()
    if not trigger or normalize(trigger[0]) != normalize(_DELETE_TRIGGER):
        raise ValueError("Unsupported QMD 2.8.3 documents deletion trigger")
    fts = db.execute("SELECT sql FROM sqlite_master WHERE name='documents_fts'").fetchone()
    if not fts or not re.search(r"using\s+fts5\s*\(\s*filepath\s*,\s*title\s*,\s*body\s*,\s*tokenize\s*=\s*'porter unicode61'\s*\)", fts[0], re.I):
        raise ValueError("Unsupported QMD 2.8.3 FTS schema")
    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise ValueError("Unsupported QMD 2.8.3 user_version")
    if db.execute("SELECT 1 FROM content_vectors LIMIT 1").fetchone():
        raise ValueError("Tutorial keyword migration refuses a database containing vectors")
    if not db.execute("SELECT 1 FROM store_collections WHERE name='objects'").fetchone():
        raise ValueError("QMD objects collection is missing")


def _clean_qmd(path, library, native, *, mutate):
    with closing(_connect(path)) as db:
        _qmd_schema(db)
        db.execute("CREATE TEMP TABLE tutorial_retired_paths(path TEXT PRIMARY KEY)")
        def paths():
            for (locator,) in native.execute("SELECT qmd_path FROM temp.tutorial_retired WHERE library_id=?", (library,)):
                check_cancelled()
                filename = locator.split("/", 1)[-1]
                if not _FILENAME.fullmatch(filename):
                    raise ValueError("Retired native row has an invalid QMD path")
                yield (filename,)
        db.executemany("INSERT INTO temp.tutorial_retired_paths VALUES(?)", paths())
        with db:
            if mutate:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("DELETE FROM documents WHERE collection='objects' AND path IN (SELECT path FROM temp.tutorial_retired_paths)")
                db.execute("DELETE FROM content WHERE hash NOT IN (SELECT hash FROM documents)")
                db.execute("INSERT INTO documents_fts(documents_fts) VALUES('optimize')")
            db.execute("UPDATE store_collections SET path='corpus' WHERE name='objects'")
        if mutate:
            db.execute("VACUUM")
        db.execute("INSERT INTO documents_fts(documents_fts) VALUES('integrity-check')")
        if db.execute("SELECT 1 FROM documents WHERE collection='objects' AND path IN (SELECT path FROM temp.tutorial_retired_paths) LIMIT 1").fetchone():
            raise ValueError("Staged QMD index still contains retired tutorial paths")
        db.execute("ATTACH DATABASE ? AS staged_native", (str(Path(native.execute("PRAGMA database_list").fetchone()[2]).resolve()),))
        key = "lib_" + hashlib.sha256(library.encode("utf-8")).hexdigest()[:20]
        if db.execute("""SELECT 1 FROM documents d LEFT JOIN staged_native.objects o
          ON o.qmd_path=? || '/' || d.path AND o.library_id=?
          WHERE d.collection='objects' AND d.active=1 AND o.id IS NULL LIMIT 1""", (key, library)).fetchone():
            raise ValueError("Staged QMD documents have no current native object mapping")
        actual = db.execute("SELECT count(*) FROM documents WHERE collection='objects' AND active=1").fetchone()[0]
        expected = native.execute("SELECT count(*) FROM objects WHERE library_id=?", (library,)).fetchone()[0]
        if actual != expected:
            raise ValueError("Staged QMD active document count differs from native mapping")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Staged QMD SQLite integrity check failed")
        db.commit()
        return actual


def _export_manual(native, library, corpus, callback, *, inputs_path, previous_path=None):
    """Export new exact inputs and retained QMD bodies only for the SDK update."""
    corpus.mkdir(parents=True, exist_ok=False)
    total = native.execute("SELECT count(*) FROM objects WHERE library_id=?", (library,)).fetchone()[0]
    completed = 0
    with closing(_connect(inputs_path, read_only=True)) as inputs:
        previous = _connect(previous_path, read_only=True) if previous_path is not None else None
        try:
            for (locator,) in native.execute("SELECT qmd_path FROM objects WHERE library_id=? ORDER BY id", (library,)):
                check_cancelled()
                filename = locator.split("/", 1)[-1]
                if not _FILENAME.fullmatch(filename):
                    raise ValueError("Manual object lookup has an invalid QMD filename")
                added = inputs.execute("SELECT title,text FROM keyword_inputs WHERE library_id=? AND qmd_path=?",
                                       (library, locator)).fetchone()
                if added is not None:
                    body = ("# " + added[0] + "\n\n" + added[1]).replace("\n", os.linesep)
                else:
                    saved = previous.execute("""SELECT c.doc FROM documents d JOIN content c ON c.hash=d.hash
                        WHERE d.collection='objects' AND d.path=? AND d.active=1""", (filename,)).fetchone() if previous is not None else None
                    if saved is None:
                        raise ValueError("Retained QMD keyword body is missing")
                    body = saved[0]
                (corpus / filename).write_bytes(body.encode("utf-8"))
                completed += 1
                if completed % 1000 == 0:
                    _progress(callback, "exporting_manual_keyword", completed, total)
        finally:
            if previous is not None:
                previous.close()
    _progress(callback, "exported_manual_keyword", completed, total)


def _manual_sql_delta(engine, path, library, native_path, plan, callback, *, inputs_path, timeout=1800):
    """Use the released SDK's exact title/CJK helpers in one SQL transaction."""
    path = Path(path)
    with closing(_connect(path)) as db:
        _qmd_schema(db)
    runtime = _owned(engine.data_dir, engine.data_dir / "runtime")
    runtime.mkdir(parents=True, exist_ok=True)
    worker = runtime / ("tutorial_sql_" + hashlib.sha256(_MANUAL_SQL_WORKER.encode()).hexdigest()[:16] + ".mjs")
    if not worker.exists():
        worker.write_text(_MANUAL_SQL_WORKER, encoding="utf-8")
    progress_path = path.with_name("qmd-keyword.progress.json")
    request = {"qmd_dir": str(engine.qmd_dir), "db_path": str(path), "library_id": library,
               "native_path": str(Path(native_path).resolve()), "plan_path": str(plan.path),
               "inputs_path": str(Path(inputs_path).resolve()),
               "progress_path": str(progress_path), "newline": os.linesep}
    stopped = threading.Event()
    control = BuildControl(progress_path.with_name("qmd-keyword.cancel"), stopped)
    def run():
        with build_control(control):
            return run_text([engine.node, str(worker)], input_text=_json(request), cwd=path.parent,
                            timeout=timeout, max_output=2 * 1024 * 1024)
    previous = None
    def report():
        nonlocal previous
        try:
            value = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if value != previous:
            _progress(callback, value["phase"], value["completed"], value["total"], library_id=library)
            previous = value
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run)
            try:
                while not future.done():
                    check_cancelled()
                    report()
                    time.sleep(0.1)
                result = future.result()
            except BaseException:
                stopped.set()
                raise
        report()
        if result.returncode:
            raise RuntimeError("QMD manual SQL delta failed: " + (result.stderr or result.stdout)[-1800:])
        marker = result.stdout.rfind("TUTORIAL_SQL_RESULT=")
        if marker < 0:
            raise RuntimeError("QMD manual SQL delta returned no result")
        return json.loads(result.stdout[marker + len("TUTORIAL_SQL_RESULT="):].splitlines()[0])
    finally:
        progress_path.unlink(missing_ok=True)
        progress_path.with_name(progress_path.name + ".tmp").unlink(missing_ok=True)


def stage_tutorial_indexes(store, plan, on_progress=None, *, engine_factory=LibrarySearch, keyword_timeout=1800,
                           manual_strategy="sql_delta"):
    """Return validated, independent staged indexes; publish nothing.

    ``engine_factory(search_root)`` supplies LibrarySearch-compatible
    ``_load_active``, ``_qmd_version``, and ``_engine`` methods. The SDK is used
    only for its pinned normalization exports during a manual SQL delta.
    ``manual_strategy='sdk'`` retains the explicit file-export/update fallback.
    """
    started, directory, created = time.monotonic(), None, False
    root = (Path(store.root) / "search").resolve()
    plan = plan if isinstance(plan, TutorialMigrationPlan) else TutorialMigrationPlan(plan)
    metadata, summary = plan.metadata(), plan.summary()
    if manual_strategy not in {"sql_delta", "sdk"}:
        raise ValueError("manual_strategy must be sql_delta or sdk")
    if metadata.get("status") != "ready" or Path(metadata.get("source_catalog", "")).resolve() != Path(store.db_path).resolve():
        raise ValueError("Ready tutorial plan for this catalog is required")
    engine = engine_factory(root)
    old = engine._load_active()
    if old and old.get("keyword_storage") == SHARED_KEYWORD_STORAGE:
        raise ValueError("Legacy tutorial index migration does not support shared keyword collections; "
                         "this completed one-time migration requires the original per-library keyword layout")
    if (not old or old.get("bridge_version") != BRIDGE_VERSION or old.get("generation") != metadata["source_generation"] or
            not old.get("modes", {}).get("qmd_keyword", {}).get("ready")):
        raise ValueError("Current ready QMD keyword index must match the tutorial plan")
    if engine._qmd_version() != QMD_VERSION:
        raise ValueError("Tutorial index migration requires QMD 2.8.3")
    fingerprint, generation, count = _desired_fingerprints(store, plan, on_progress)
    try:
        name = hashlib.sha256((generation + fingerprint).encode("utf-8")).hexdigest()[:20] + "-" + uuid.uuid4().hex[:8]
        directory = _owned(root, root / "generations" / name)
        directory.mkdir(parents=True, exist_ok=False)
        created = True
        source_native = _owned(root, Path(old["directory"]) / "objects.sqlite3")
        destination = directory / "objects.sqlite3"
        _progress(on_progress, "copying_native", unit="databases")
        _backup(source_native, destination)
        object_index.slim_object_index(destination, dry_run=False, offline=True)
        libraries, corpus_directories = {}, []
        affected = {row["library_id"]: row for row in summary["affected_libraries"]}
        canonical_owner = metadata["canonical_library_id"]
        with keyword_input_spool(directory) as inputs, closing(_connect(destination)) as native:
            if not object_index.owned_schema(native) or not object_index.is_compact(native):
                raise ValueError("Tutorial migration requires the owned compact object schema")
            if native.execute("SELECT count(*) FROM objects").fetchone()[0] != old.get("object_count"):
                raise ValueError("Source native count differs from its active manifest")
            with native:
                # Retire historical independent postings in the staged copy.
                # Canonical insertions now update shared metadata only.
                if object_index.has_legacy_fts(native):
                    native.execute("DROP TABLE object_fts")
                retired = _delete_native(native, plan, on_progress)
                inserted = 0
                for item in plan.iter_canonical_objects():
                    check_cancelled()
                    if item["library_id"] != canonical_owner:
                        raise ValueError("Canonical tutorial objects must belong to the manual owner")
                    text, stats = _representation(item)
                    object_index.insert_object(native, item, text, stats, "", standalone=False)
                    inputs.add(item["library_id"], object_index.qmd_path(item), item["title"], text)
                    inserted += 1
                    if inserted % 1000 == 0:
                        _progress(on_progress, "inserting_canonical_native", inserted, summary["counts"]["canonical_objects"])
            inputs.seal()
            representation = _native_summary(native)
            if representation["object_count"] != count or _native_generation(native) != generation:
                raise ValueError("Staged native identities differ from the planned catalog generation")
            totals = dict(native.execute("SELECT library_id,count(*) FROM objects GROUP BY library_id"))
            for completed, library in enumerate(sorted(totals), 1):
                check_cancelled()
                key = "lib_" + hashlib.sha256(library.encode("utf-8")).hexdigest()[:20]
                item = {"key": key, "corpus": str(directory / key / "corpus"),
                        "db_path": str(directory / key / "qmd-keyword.sqlite3"),
                        "vector_db_path": str(directory / key / "qmd-vector.sqlite3")}
                delta = affected.get(library, {})
                if library == canonical_owner and (delta.get("retired") or delta.get("inserted")):
                    previous = old.get("libraries", {}).get(library)
                    if not previous or previous.get("key") != key:
                        raise ValueError("Canonical manual owner has no previous keyword database")
                    if manual_strategy == "sdk":
                        _export_manual(native, library, Path(item["corpus"]), on_progress,
                                       inputs_path=inputs.path, previous_path=_owned(root, Path(previous["db_path"])))
                        corpus_directories.append(item["corpus"])
                        _progress(on_progress, "rebuilding_manual_keyword", completed - 1, len(totals), "indexes", library_id=library)
                        engine._engine({"op": "update", **item, "embedding_enabled": False, "model_uri": None}, timeout=keyword_timeout)
                        item["tutorial_keyword_update"] = "canonical_manual_rebuild"
                    else:
                        _backup(_owned(root, Path(previous["db_path"])), Path(item["db_path"]))
                        _progress(on_progress, "updating_manual_keyword_delta", unit="indexes", library_id=library)
                        item["tutorial_keyword_delta"] = _manual_sql_delta(engine, item["db_path"], library, destination,
                                                                         plan, on_progress, inputs_path=inputs.path, timeout=keyword_timeout)
                        item["tutorial_keyword_update"] = "canonical_manual_sql_delta"
                    _clean_qmd(item["db_path"], library, native, mutate=True)
                else:
                    previous = old.get("libraries", {}).get(library)
                    if not previous or previous.get("key") != key:
                        raise ValueError("Retained library has no previous QMD keyword index")
                    _backup(_owned(root, Path(previous["db_path"])), Path(item["db_path"]))
                    _clean_qmd(item["db_path"], library, native, mutate=bool(delta.get("retired")))
                    item["tutorial_keyword_update"] = "incremental_delete" if delta.get("retired") else "independent_backup"
                    item["keyword_reused_from_generation"] = old["generation"]
                libraries[library] = item
                _progress(on_progress, "staged_keyword_library", completed, len(totals), "indexes", library_id=library)
            if native.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Staged native SQLite integrity check failed")
        manifest = {"ready": True, "generation": generation, "corpus_hash": fingerprint, "directory": str(directory),
                    "object_count": count, "libraries": libraries, "bridge_version": BRIDGE_VERSION,
                    "object_lookup": {"schema": object_index.LOOKUP_SCHEMA}, "representation": representation,
                    "modes": {"qmd_keyword": {"ready": True, "state": "ready", "backend": "qmd-searchLex", "qmd_version": QMD_VERSION},
                              "qmd_vector": {"ready": False, "state": "disabled", "enabled": False}},
                    "tutorial_migration": {"migration_id": metadata["migration_id"], "source_generation": old["generation"],
                                           "retired_objects": retired, "canonical_objects": inserted, "embedding_invoked": False,
                                           "stage_seconds": round(time.monotonic() - started, 2)}}
        object_index.validate_manifest_lookup(destination, manifest)
        _progress(on_progress, "indexes_staged", count, count)
        return {"manifest": stored_manifest(manifest, root), "directory": str(directory), "native_path": str(destination),
                "corpus_directories": corpus_directories, "previous_manifest": stored_manifest(old, root),
                "counts": {"objects": count, "retired": retired, "inserted": inserted, "libraries": len(libraries)}}
    except BaseException:
        if created and directory is not None and directory.exists():
            shutil.rmtree(_owned(root, directory))
        check_cancelled()
        raise
