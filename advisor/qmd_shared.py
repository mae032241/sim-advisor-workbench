"""Shared keyword collections, built from saved QMD rows or temporary inputs.

Only newly named outputs are constructed. Source databases are opened read only
and vectors, model parsing, embeddings and Markdown exports are excluded.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .build_control import BuildCancelled, BuildControl, build_control, check_cancelled
from .process_runner import run_text

KEYWORD_STORAGE = "shared-collections-v1"
QMD_VERSION = "2.8.3"
INPUT_APPLICATION_ID = 0x514D4449
INPUT_VERSION = 1
_COLLECTION = re.compile(r"lib_[a-f0-9]{20}\Z")
_DOCUMENT = re.compile(r"o-[a-f0-9]{32}\.md\Z")

_WORKER = r'''
import {readFileSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
const req=JSON.parse(readFileSync(0,'utf8'));
for(const [key,value] of Object.entries(req.env||{}))process.env[key]=value;
delete process.env.QMD_EMBED_MODEL;
if(JSON.parse(readFileSync(join(req.qmd_dir,'package.json'),'utf8')).version!=='2.8.3')throw new Error('QMD 2.8.3 required');
const {createStore,normalizeCjkForFTS,extractTitle}=await import(pathToFileURL(join(req.qmd_dir,'dist/store.js')).href);
const Database=createRequire(pathToFileURL(join(req.qmd_dir,'package.json')))('better-sqlite3');
const sha=text=>createHash('sha256').update(text).digest('hex');
const signature=()=>createHash('sha256');
const addSignature=(digest,row)=>digest.update(JSON.stringify(row)+'\n');
const progress=(phase,completed,total,library_id)=>{try{writeFileSync(req.progress_path,JSON.stringify({phase,completed,total,library_id}));}catch{}};
const store=createStore(req.output),db=store.db;
// Enable pointer-map reclamation while the new schema has no user rows.
// Collection purge can then return free pages without rewriting the whole DB.
if(db.prepare('PRAGMA auto_vacuum').get().auto_vacuum!==2)db.exec('PRAGMA auto_vacuum=INCREMENTAL; VACUUM');
const openSource=path=>new Database(path,{readonly:true,fileMustExist:true});
const schema=source=>{
  for(const [table,columns] of Object.entries({documents:['id','collection','path','title','hash','created_at','modified_at','active'],content:['hash','doc','created_at'],store_collections:['name','path','pattern'],documents_fts:['filepath','title','body'],content_vectors:['hash','seq','model']})){
    const found=new Set(source.prepare('PRAGMA table_info('+table+')').all().map(row=>row.name));
    if(columns.some(column=>!found.has(column)))throw new Error('Unsupported QMD 2.8.3 schema: '+table);
  }
  if(source.prepare('SELECT 1 FROM content_vectors LIMIT 1').get()||source.prepare("SELECT 1 FROM sqlite_master WHERE name='vectors_vec'").get())throw new Error('Keyword merge refuses vector storage');
};
const result={keyword_storage:'shared-collections-v1',documents:0,collections:{},source_databases:0,embedding_invoked:false};
let source=null,inputs=null;
try{
  const triggers=db.prepare("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN ('documents_ai','documents_au')").all();
  const insertContent=db.prepare('INSERT OR IGNORE INTO content(hash,doc,created_at) VALUES(?,?,?)');
  const insertDocument=db.prepare('INSERT INTO documents(collection,path,title,hash,created_at,modified_at,active) VALUES(?,?,?,?,?,?,1)');
  const insertFts=db.prepare('INSERT INTO documents_fts(rowid,filepath,title,body) VALUES(?,?,?,?)');
  const insertCollection=db.prepare('INSERT INTO store_collections(name,path,pattern,context) VALUES(?,?,?,?)');
  const now=new Date().toISOString();
  const insert=(collection,path,title,hash,body,created,modified,contentCreated,digest)=>{
    if(!/^o-[a-f0-9]{32}\.md$/.test(path))throw new Error('Invalid generated QMD object path');
    if(sha(body)!==hash)throw new Error('Saved QMD content hash mismatch');
    insertContent.run(hash,body,contentCreated);
    const row=insertDocument.run(collection,path,title,hash,created,modified);
    insertFts.run(row.lastInsertRowid,normalizeCjkForFTS(collection+'/'+path),normalizeCjkForFTS(title),normalizeCjkForFTS(body));
    addSignature(digest,[path,title,hash,created,modified,sha(body)]);
  };
  db.exec('BEGIN IMMEDIATE');
  try{
    for(const row of triggers)db.exec('DROP TRIGGER '+row.name);
    if(req.mode==='inputs'){
      inputs=openSource(req.inputs_path);
      if(inputs.pragma('application_id',{simple:true})!==0x514d4449||inputs.pragma('user_version',{simple:true})!==1)throw new Error('Unsealed temporary QMD keyword inputs');
    }
    const opened=new Set();
    for(const item of req.libraries){
      const digest=signature();let count=0,expected=0,context=null;
      if(req.mode==='merge'){
        if(source)source.close();
        source=openSource(item.db_path);schema(source);opened.add(item.db_path);
        const collection=source.prepare('SELECT * FROM store_collections WHERE name=?').get(item.source_collection);
        if(!collection)throw new Error('Source QMD collection missing: '+item.source_collection);
        context=collection.context||null;
        const hasConfig=source.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_config'").get();
        const globalContext=hasConfig?source.prepare("SELECT value FROM store_config WHERE key='global_context'").get()?.value:null;
        if(globalContext){
          const original=context?JSON.parse(context):{},roots=[globalContext],specific={};
          for(const [prefix,value] of Object.entries(original)){
            if((prefix.startsWith('/')?prefix:'/'+prefix)==='/')roots.push(value);else specific[prefix]=value;
          }
          context=JSON.stringify({'/':roots.join('\n\n'),...specific});
        }
        expected=source.prepare('SELECT count(*) AS n FROM documents WHERE collection=? AND active=1').get(item.source_collection).n;
        const rows=source.prepare(`SELECT d.path,d.title,d.hash,d.created_at,d.modified_at,c.doc,c.created_at AS content_created
          FROM documents d JOIN content c ON c.hash=d.hash WHERE d.collection=? AND d.active=1 ORDER BY d.path`);
        for(const row of rows.iterate(item.source_collection)){
          insert(item.key,row.path,row.title,row.hash,row.doc,row.created_at,row.modified_at,row.content_created,digest);
          if(++count%1000===0)progress('merging_keyword_collection',count,expected,item.library_id);
        }
      }else{
        expected=inputs.prepare('SELECT count(*) AS n FROM keyword_inputs WHERE library_id=?').get(item.library_id).n;
        for(const row of inputs.prepare('SELECT qmd_path,title,text FROM keyword_inputs WHERE library_id=? ORDER BY qmd_path').iterate(item.library_id)){
          const path=row.qmd_path.split('/').pop(),body=('# '+row.title+'\n\n'+row.text).replaceAll('\n',req.newline);
          if(row.qmd_path!==item.key+'/'+path)throw new Error('Object QMD library mapping mismatch');
          insert(item.key,path,extractTitle(body,path),sha(body),body,now,now,now,digest);
          if(++count%1000===0)progress('building_keyword_collection',count,expected,item.library_id);
        }
      }
      if(count!==expected)throw new Error('Source QMD active rows reference missing content');
      insertCollection.run(item.key,item.key+'/corpus','**/*.md',context);
      result.collections[item.library_id]={key:item.key,collection:item.key,expected_count:expected,documents:count,semantic_sha256:digest.digest('hex')};
      result.documents+=count;progress('completed_keyword_collection',count,expected,item.library_id);
    }
    for(const row of triggers)db.exec(row.sql);
    db.exec('COMMIT');result.source_databases=opened.size;
  }catch(error){db.exec('ROLLBACK');throw error;}
  if(source){source.close();source=null;}if(inputs){inputs.close();inputs=null;}
  for(const item of req.libraries){
    const digest=signature();let count=0;
    for(const row of db.prepare(`SELECT d.path,d.title,d.hash,d.created_at,d.modified_at,c.doc FROM documents d
      JOIN content c ON c.hash=d.hash WHERE d.collection=? AND d.active=1 ORDER BY d.path`).iterate(item.key)){
      addSignature(digest,[row.path,row.title,row.hash,row.created_at,row.modified_at,sha(row.doc)]);count++;
    }
    const expected=result.collections[item.library_id];
    if(count!==expected.documents||digest.digest('hex')!==expected.semantic_sha256)throw new Error('Shared keyword semantic verification failed');
  }
  db.exec("INSERT INTO documents_fts(documents_fts) VALUES('optimize')");
  // One construction-time rewrite packs random hash/index insertions. Purge
  // continues to use incremental_vacuum rather than repeating this rewrite.
  db.exec('VACUUM');
  db.exec("INSERT INTO documents_fts(documents_fts) VALUES('integrity-check')");
  if(db.prepare('PRAGMA integrity_check').get().integrity_check!=='ok'||db.prepare('PRAGMA foreign_key_check').get())throw new Error('Shared QMD integrity check failed');
  result.content_rows=db.prepare('SELECT count(*) AS n FROM content').get().n;
  result.contexts=db.prepare('SELECT count(*) AS n FROM store_collections WHERE context IS NOT NULL').get().n;
  db.exec('PRAGMA wal_checkpoint(TRUNCATE)');
  process.stdout.write('QMD_SHARED_RESULT='+JSON.stringify(result)+'\n');
}finally{if(source)source.close();if(inputs)inputs.close();store.close();}
'''


def _key(library):
    return "lib_" + hashlib.sha256(library.encode("utf-8")).hexdigest()[:20]


def _libraries(libraries, *, merge):
    output = []
    for library, item in sorted(libraries.items()):
        key = item.get("key", _key(library))
        if key != _key(library) or not _COLLECTION.fullmatch(key):
            raise ValueError("Shared QMD collection must equal its generated library key")
        entry = {"library_id": library, "key": key}
        if merge:
            path = Path(item["db_path"]).expanduser().resolve(strict=True)
            source_collection = item.get("collection", "objects")
            if source_collection != "objects" and not _COLLECTION.fullmatch(source_collection):
                raise ValueError("Invalid source keyword collection")
            entry.update(db_path=str(path), source_collection=source_collection)
        output.append(entry)
    return output


def _run(request, output, *, qmd_dir, node, env, on_progress, timeout):
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise FileExistsError("Shared keyword output already exists")
    qmd_dir, node = Path(qmd_dir).expanduser().resolve(strict=True), str(Path(node).expanduser().resolve(strict=True))
    package = json.loads((qmd_dir / "package.json").read_text(encoding="utf-8"))
    if package.get("version") != QMD_VERSION:
        raise ValueError("Shared keyword construction requires QMD 2.8.3")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Helpers are kept outside generation payloads and contain no source data.
    runtime = output.parent.parent.parent / "runtime" if output.parent.parent.name == "generations" else output.parent / ".qmd-runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    worker = runtime / ("qmd_shared_" + hashlib.sha256(_WORKER.encode()).hexdigest()[:16] + ".mjs")
    if not worker.exists():
        worker.write_text(_WORKER, encoding="utf-8")
    progress_path = output.with_name(output.name + ".progress.json")
    request.update(output=str(output), qmd_dir=str(qmd_dir), env=env or {}, progress_path=str(progress_path), newline=os.linesep)
    stopped = threading.Event()
    control = BuildControl(progress_path.with_name(progress_path.name + ".cancel"), stopped)
    def launch():
        with build_control(control):
            return run_text([node, str(worker)], input_text=json.dumps(request, ensure_ascii=False), cwd=output.parent,
                            timeout=timeout, max_output=2 * 1024 * 1024)
    previous = None
    def progress():
        nonlocal previous
        try:
            value = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if value != previous:
            check_cancelled()
            if on_progress:
                on_progress({"stage": "shared_keyword", **value, "unit": "objects"})
            check_cancelled()
            previous = value
    try:
        check_cancelled()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(launch)
            try:
                while not future.done():
                    check_cancelled()
                    progress()
                    time.sleep(.1)
                result = future.result()
            except BaseException:
                stopped.set()
                raise
        progress()
        if result.returncode:
            raise RuntimeError("Shared QMD construction failed: " + (result.stderr or result.stdout)[-1800:])
        marker = result.stdout.rfind("QMD_SHARED_RESULT=")
        if marker < 0:
            raise RuntimeError("Shared QMD worker returned no verification report")
        report = json.loads(result.stdout[marker + len("QMD_SHARED_RESULT="):].splitlines()[0])
        report.update(db_path=str(output), bytes=output.stat().st_size)
        check_cancelled()
        return report
    except BaseException:
        for suffix in ("", "-wal", "-shm", "-journal"):
            output.with_name(output.name + suffix).unlink(missing_ok=True)
        check_cancelled()
        raise
    finally:
        progress_path.unlink(missing_ok=True)


def merge_keyword_indexes(libraries, output, *, qmd_dir, node, env=None, on_progress=None, timeout=1800):
    """Merge per-library or existing shared saved QMD collections losslessly."""
    entries = _libraries(libraries, merge=True)
    target = Path(output).expanduser().resolve()
    if any(Path(item["db_path"]) == target for item in entries):
        raise ValueError("Shared output cannot replace a source database")
    return _run({"mode": "merge", "libraries": entries}, output, qmd_dir=qmd_dir, node=node, env=env,
                on_progress=on_progress, timeout=timeout)


class _KeywordInputs:
    """A disposable export of this build's exact revision representations."""

    def __init__(self, directory):
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / (".qmd-keyword-inputs-" + uuid.uuid4().hex + ".sqlite3")
        self._db = sqlite3.connect(self.path)
        try:
            # This is staging data, never an index authority or recovery source.
            # No journal or WAL may retain a second copy of exported text.
            self._db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY; "
                f"PRAGMA application_id={INPUT_APPLICATION_ID}; "
                "CREATE TABLE keyword_inputs(library_id TEXT NOT NULL,qmd_path TEXT PRIMARY KEY,title TEXT NOT NULL,text TEXT NOT NULL); "
                "CREATE INDEX keyword_inputs_library ON keyword_inputs(library_id,qmd_path)")
        except BaseException:
            self.close()
            raise

    def add(self, library_id, qmd_path, title, text):
        check_cancelled()
        if self._db is None:
            raise ValueError("Temporary keyword inputs are already sealed")
        if (not isinstance(library_id, str) or not library_id or not isinstance(qmd_path, str)
                or qmd_path.split('/') != [_key(library_id), qmd_path.rsplit('/', 1)[-1]]
                or not _DOCUMENT.fullmatch(qmd_path.rsplit('/', 1)[-1])
                or not isinstance(title, str) or not isinstance(text, str)):
            raise ValueError("Invalid temporary QMD keyword input")
        self._db.execute("INSERT INTO keyword_inputs VALUES(?,?,?,?)", (library_id, qmd_path, title, text))

    def seal(self):
        check_cancelled()
        if self._db is None:
            return
        self._db.execute(f"PRAGMA user_version={INPUT_VERSION}")
        self._db.commit()
        self._db.close()
        self._db = None
        check_cancelled()

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
        for suffix in ("", "-wal", "-shm", "-journal"):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)


@contextmanager
def keyword_input_spool(directory):
    """Export only title/text until keyword construction finishes, then erase it.

    Call ``add(library_id,qmd_path,title,text)`` for each exact source revision,
    then ``seal()`` before passing ``path`` to ``build_keyword_index``. Leaving
    the context removes staging data on success, failure and cancellation.
    """
    inputs = _KeywordInputs(directory)
    try:
        yield inputs
    finally:
        inputs.close()


def build_keyword_index(inputs_path, output, *, qmd_dir, node, libraries=None, env=None, on_progress=None, timeout=1800):
    """Build from a sealed temporary export; the object directory stores no text."""
    inputs = Path(inputs_path).expanduser().resolve(strict=True)
    with closing(sqlite3.connect(inputs.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        if (db.execute("PRAGMA application_id").fetchone()[0] != INPUT_APPLICATION_ID
                or db.execute("PRAGMA user_version").fetchone()[0] != INPUT_VERSION
                or {row[1] for row in db.execute("PRAGMA table_info(keyword_inputs)")} != {"library_id", "qmd_path", "title", "text"}):
            raise ValueError("Shared build requires sealed temporary QMD keyword inputs")
        counts = dict(db.execute("SELECT library_id,count(*) FROM keyword_inputs GROUP BY library_id"))
    libraries = libraries if libraries is not None else {library: {} for library in counts}
    if not counts or set(libraries) != set(counts):
        raise ValueError("Shared keyword libraries differ from the temporary inputs")
    return _run({"mode": "inputs", "inputs_path": str(inputs), "libraries": _libraries(libraries, merge=False)}, output,
                qmd_dir=qmd_dir, node=node, env=env, on_progress=on_progress, timeout=timeout)


def _validate_keyword(db):
    for table, columns in {"documents": {"id", "collection", "path", "hash", "active"},
                           "content": {"hash", "doc"}, "store_collections": {"name", "context"},
                           "documents_fts": {"filepath", "title", "body"}, "content_vectors": {"hash"}}.items():
        if not columns <= {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}:
            raise ValueError("Unsupported shared keyword schema: " + table)
    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise ValueError("Unsupported QMD keyword schema version")
    if db.execute("SELECT 1 FROM content_vectors LIMIT 1").fetchone() or db.execute("SELECT 1 FROM sqlite_master WHERE name='vectors_vec'").fetchone():
        raise ValueError("Shared keyword purge refuses vector storage")
    trigger = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='documents_ad'").fetchone()
    if not trigger or " ".join(trigger[0].split()).casefold() != "create trigger documents_ad after delete on documents begin delete from documents_fts where rowid = old.id; end":
        raise ValueError("Unsupported shared keyword deletion trigger")
    known = {"content", "documents", "sqlite_sequence", "llm_cache", "content_vectors", "store_collections", "store_config",
             "documents_fts", "documents_fts_data", "documents_fts_idx", "documents_fts_content", "documents_fts_docsize", "documents_fts_config"}
    actual = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if actual - known:
        raise ValueError("Shared keyword purge refuses unknown payload tables")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='view' LIMIT 1").fetchone():
        raise ValueError("Shared keyword purge refuses unknown views")
    triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if triggers - {"documents_ai", "documents_ad", "documents_au"}:
        raise ValueError("Shared keyword purge refuses unknown triggers")


def purge_collection(path, collection, *, force_compact=False):
    """Delete one keyword collection; retry compaction after journaled failure."""
    if not isinstance(collection, str) or not _COLLECTION.fullmatch(collection):
        raise ValueError("Invalid generated shared keyword collection")
    path = Path(path).expanduser().resolve(strict=True)
    if path.stat().st_nlink > 1:
        raise ValueError("Shared keyword purge refuses hard-linked databases")
    before = path.stat().st_size
    compaction = "not_needed"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        _validate_keyword(db)
        exists = db.execute("SELECT 1 FROM store_collections WHERE name=?", (collection,)).fetchone() or db.execute("SELECT 1 FROM documents WHERE collection=?", (collection,)).fetchone()
    removed_documents = removed_content = removed_collections = removed_contexts = 0
    if exists or force_compact:
        check_cancelled()
        with closing(sqlite3.connect(path, timeout=30)) as db:
            _validate_keyword(db)
            db.execute("PRAGMA temp_store=MEMORY")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("CREATE TEMP TABLE purge_candidate_hashes(hash TEXT PRIMARY KEY)")
            with db:
                db.execute("INSERT INTO temp.purge_candidate_hashes SELECT DISTINCT hash FROM documents WHERE collection=?", (collection,))
                removed_contexts = db.execute("SELECT count(*) FROM store_collections WHERE name=? AND context IS NOT NULL", (collection,)).fetchone()[0]
                removed_documents = db.execute("DELETE FROM documents WHERE collection=?", (collection,)).rowcount
                removed_collections = db.execute("DELETE FROM store_collections WHERE name=?", (collection,)).rowcount
                removed_content = db.execute("""DELETE FROM content WHERE hash IN
                  (SELECT hash FROM temp.purge_candidate_hashes) AND NOT EXISTS
                  (SELECT 1 FROM documents WHERE documents.hash=content.hash)""").rowcount
                db.execute("INSERT INTO documents_fts(documents_fts) VALUES('optimize')")
            check_cancelled()
            if db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2:
                db.execute("PRAGMA incremental_vacuum").fetchall()
                compaction = "incremental_vacuum"
            elif force_compact:
                db.execute("VACUUM")
                compaction = "full_vacuum_legacy"
            else:
                compaction = "full_vacuum_deferred_legacy"
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise RuntimeError("Shared keyword WAL checkpoint is busy")
            if (db.execute("SELECT 1 FROM documents WHERE collection=? LIMIT 1", (collection,)).fetchone()
                    or db.execute("SELECT 1 FROM store_collections WHERE name=?", (collection,)).fetchone()):
                raise ValueError("Shared keyword target collection remains after purge")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        empty = all(db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone() is None for table in
                    ("documents", "content", "store_collections", "llm_cache", "content_vectors"))
        empty &= db.execute("SELECT 1 FROM store_config WHERE key NOT IN ('fts_cjk_normalized_version','config_hash') AND value IS NOT NULL AND value<>'' LIMIT 1").fetchone() is None
    after = path.stat().st_size
    return {"removed_documents": removed_documents, "removed_content": removed_content, "removed_collections": removed_collections,
            "removed_contexts": removed_contexts, "before_bytes": before, "after_bytes": after, "reclaimed_bytes": before-after,
            "unchanged": not bool(exists or force_compact), "keyword_only": True, "owned_schema": True,
            "empty": bool(empty), "compaction": compaction}
