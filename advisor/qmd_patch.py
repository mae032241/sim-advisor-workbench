"""Bounded in-place patches of existing QMD 2.8.3 keyword documents.

The caller owns the offline build lease, worker suspension and any metadata
journal. This module never opens createStore (which can migrate/rebuild old
indexes), scans the corpus, copies a database, or invokes embeddings. Plans and
undo values contain source text and must be kept out of application logs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .process_runner import run_text

PATCH_SCHEMA = "qmd-keyword-document-patch/v1"
QMD_VERSION = "2.8.3"
_COLLECTION = re.compile(r"lib_[a-f0-9]{20}\Z")
_PATH = re.compile(r"lib_[a-f0-9]{20}/o-[a-f0-9]{32}\.md\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_MAX_UPDATES = 64
_MAX_INPUT_BYTES = 8 * 1024 * 1024

_WORKER = r'''
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
const req=JSON.parse(readFileSync(0,'utf8'));
const VERSION='qmd-keyword-document-patch/v1';
if(req.schema!==VERSION||!['prepare','patch','restore'].includes(req.mode))throw new Error('Unsupported QMD patch request');
for(const [key,value] of Object.entries(req.env||{}))process.env[key]=value;
delete process.env.QMD_EMBED_MODEL;
if(JSON.parse(readFileSync(join(req.qmd_dir,'package.json'),'utf8')).version!=='2.8.3')throw new Error('QMD 2.8.3 required');
// These SDK functions do not initialize a store or access an LLM. In particular
// createStore must never be called against an untrusted existing schema.
const {insertContent,updateDocument,normalizeCjkForFTS,extractTitle}=await import(pathToFileURL(join(req.qmd_dir,'dist/store.js')).href);
const Database=createRequire(pathToFileURL(join(req.qmd_dir,'package.json')))('better-sqlite3');
const sha=value=>createHash('sha256').update(value).digest('hex');
const fail=message=>{throw new Error(message);};
// Recovery journals may sort JSON object keys. Compare structure rather than
// insertion order while preserving the meaningful order of arrays.
const stable=value=>Array.isArray(value)?value.map(stable):value&&typeof value==='object'
  ?Object.fromEntries(Object.keys(value).sort().map(key=>[key,stable(value[key])])):value;
const equal=(a,b)=>JSON.stringify(stable(a))===JSON.stringify(stable(b));
const normalize=sql=>(sql||'').replace(/--[^\r\n]*/g,'').replace(/\s+/g,'').replace(/;$/,'').toLowerCase();
const canonicalTriggers={
documents_ai:`CREATE TRIGGER documents_ai AFTER INSERT ON documents WHEN new.active = 1 BEGIN
INSERT INTO documents_fts(rowid, filepath, title, body) SELECT new.id, new.collection || '/' || new.path,
new.title, (SELECT doc FROM content WHERE hash = new.hash) WHERE new.active = 1; END`,
documents_ad:`CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN DELETE FROM documents_fts WHERE rowid = old.id; END`,
documents_au:`CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
DELETE FROM documents_fts WHERE rowid = old.id AND new.active = 0;
INSERT OR REPLACE INTO documents_fts(rowid, filepath, title, body) SELECT new.id, new.collection || '/' || new.path,
new.title, (SELECT doc FROM content WHERE hash = new.hash) WHERE new.active = 1; END`
};
const schema=db=>{
  if(db.pragma('user_version',{simple:true})!==1)fail('Unsupported QMD schema version');
  const columns={content:['hash','doc','created_at'],documents:['id','collection','path','title','hash','created_at','modified_at','active'],
    store_collections:['name','path','pattern','ignore_patterns','include_by_default','update_command','context'],
    store_config:['key','value'],content_vectors:['hash','seq','pos','model','embed_fingerprint','total_chunks','embedded_at'],
    llm_cache:['hash','result','created_at'],documents_fts:['filepath','title','body']};
  for(const [table,names] of Object.entries(columns)){
    if(!equal(db.prepare('PRAGMA table_info('+table+')').all().map(row=>row.name),names))fail('Unsupported QMD schema: '+table);
  }
  const tableSql=name=>normalize(db.prepare("SELECT sql FROM sqlite_master WHERE type='table' AND name=?").get(name)?.sql);
  if(tableSql('content')!==normalize('CREATE TABLE content(hash TEXT PRIMARY KEY,doc TEXT NOT NULL,created_at TEXT NOT NULL)')||
     tableSql('documents')!==normalize(`CREATE TABLE documents(id INTEGER PRIMARY KEY AUTOINCREMENT,collection TEXT NOT NULL,path TEXT NOT NULL,
       title TEXT NOT NULL,hash TEXT NOT NULL,created_at TEXT NOT NULL,modified_at TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,
       FOREIGN KEY (hash) REFERENCES content(hash) ON DELETE CASCADE,UNIQUE(collection,path))`))fail('Unsupported QMD content/document constraints');
  const fts=db.prepare("SELECT sql FROM sqlite_master WHERE type='table' AND name='documents_fts'").get();
  if(normalize(fts?.sql)!==normalize("CREATE VIRTUAL TABLE documents_fts USING fts5(filepath,title,body,tokenize='porter unicode61')"))fail('Unsupported QMD FTS schema');
  if(db.prepare("SELECT value FROM store_config WHERE key='fts_cjk_normalized_version'").get()?.value!=='1')fail('QMD CJK normalization migration required; patch refused');
  const triggers=db.prepare("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all();
  if(triggers.length!==3||triggers.some(row=>!canonicalTriggers[row.name]||normalize(row.sql)!==normalize(canonicalTriggers[row.name])))fail('Unsupported QMD sync triggers');
  const hashIndex=db.prepare('PRAGMA index_info(idx_documents_hash)').all();
  if(hashIndex.length!==1||hashIndex[0].name!=='hash')fail('Indexed QMD hash references required');
  const documentIndexes=db.prepare('PRAGMA index_list(documents)').all();
  if(!documentIndexes.some(index=>index.unique===1&&equal(db.prepare('PRAGMA index_info("'+index.name.replaceAll('"','""')+'")').all().map(row=>row.name),['collection','path'])))fail('Unique QMD collection/path index required');
  if(db.prepare("SELECT 1 FROM sqlite_master WHERE name='vectors_vec' OR name IN ('collections','path_contexts')").get()||db.prepare('SELECT 1 FROM content_vectors LIMIT 1').get())fail('Patch requires a current keyword-only QMD store');
};
const readDocument=(db,collection,path)=>db.prepare(`SELECT d.id,d.collection,d.path,d.title,d.hash,d.created_at,d.modified_at,d.active,
c.doc,c.created_at AS content_created_at FROM documents d LEFT JOIN content c ON c.hash=d.hash
WHERE d.collection=? AND d.path=?`).get(collection,path);
const metadata=row=>({id:row.id,collection:row.collection,path:row.path,title:row.title,hash:row.hash,
created_at:row.created_at,modified_at:row.modified_at,active:row.active,content_created_at:row.content_created_at});
const checkDocument=(db,row)=>{
  if(!row||row.active!==1||typeof row.doc!=='string')fail('Existing active QMD document/content required');
  if(sha(row.doc)!==row.hash)fail('Saved QMD content hash mismatch');
  const fts=db.prepare('SELECT rowid,filepath,title,body FROM documents_fts WHERE rowid=?').get(row.id);
  const wanted={rowid:row.id,filepath:normalizeCjkForFTS(row.collection+'/'+row.path),title:normalizeCjkForFTS(row.title),body:normalizeCjkForFTS(row.doc)};
  if(!equal(fts,wanted))fail('Exact QMD document FTS verification failed');
};
const validUpdate=item=>{
  if(!item||typeof item!=='object'||!/^lib_[a-f0-9]{20}$/.test(item.collection)||!/^lib_[a-f0-9]{20}\/o-[a-f0-9]{32}\.md$/.test(item.path)||item.path.split('/')[0]!==item.collection||typeof item.title!=='string'||typeof item.text!=='string')fail('Invalid immutable QMD update path');
};
const makePlan=db=>{
  if(!Array.isArray(req.updates)||!req.updates.length||req.updates.length>64)fail('Bounded nonempty QMD update list required');
  const seen=new Set(),records=[],now=new Date().toISOString();
  for(const item of req.updates){
    validUpdate(item);
    if(seen.has(item.path))fail('Duplicate QMD update path');seen.add(item.path);
    if(!db.prepare('SELECT 1 FROM store_collections WHERE name=?').get(item.collection))fail('Existing QMD collection required');
    const path=item.path.split('/')[1],old=readDocument(db,item.collection,path);checkDocument(db,old);
    const body=('# '+item.title+'\n\n'+item.text).replaceAll('\n',req.newline),hash=sha(body),title=extractTitle(body,path);
    const previouslyApplied=req.mode==='patch'&&req.expected_plan?.records?.some(row=>equal(metadata(old),row.after)&&row.new_body===body&&old.doc===body);
    if(item.expected_old_hash!==undefined&&item.expected_old_hash!==old.hash&&!previouslyApplied)fail('Stale QMD old content hash');
    const content=db.prepare('SELECT doc,created_at FROM content WHERE hash=?').get(hash);
    if(content&&(content.doc!==body||sha(content.doc)!==hash))fail('New QMD content hash collision');
    const unchanged=old.hash===hash&&old.title===title;
    const after={...metadata(old),title,hash,modified_at:unchanged?old.modified_at:now,content_created_at:content?.created_at||now};
    records.push({before:metadata(old),after,old_body:old.doc,new_body:body,new_content_absent:!content,unchanged});
  }
  return {schema:VERSION,db_path:req.db_path,updates_sha256:sha(JSON.stringify(stable(req.updates))),records};
};
const verifyPlan=(actual,expected)=>{
  if(!expected||expected.schema!==VERSION||expected.db_path!==req.db_path||expected.updates_sha256!==actual.updates_sha256||expected.records.length!==actual.records.length)fail('QMD patch plan mismatch');
  const states=[];
  for(let n=0;n<actual.records.length;n++){
    const got=actual.records[n],wanted=expected.records[n];
    if(got.new_body!==wanted.new_body||got.after.hash!==wanted.after.hash||got.after.title!==wanted.after.title)fail('Invalid QMD planned replacement');
    if(!wanted.unchanged&&equal(got.before,wanted.after)&&got.old_body===wanted.new_body){states.push('after');continue;}
    if(!equal(got.before,wanted.before)||got.old_body!==wanted.old_body||got.new_body!==wanted.new_body||got.new_content_absent!==wanted.new_content_absent||got.unchanged!==wanted.unchanged)fail('Stale QMD patch plan');
    if(!wanted.unchanged)states.push('before');
    const after={...got.after,modified_at:wanted.after.modified_at,content_created_at:wanted.after.content_created_at};
    if(!equal(after,wanted.after))fail('Invalid QMD planned after state');got.after=wanted.after;
  }
  if(new Set(states).size>1)fail('Mixed QMD patch transaction state');
  return states.length>0&&states[0]==='after';
};
const undoFor=plan=>({schema:VERSION,db_path:plan.db_path,records:plan.records.filter(row=>!row.unchanged).map(row=>({before:row.before,after:row.after,old_body:row.old_body})),
created_content_hashes:[...new Set(plan.records.filter(row=>!row.unchanged&&row.new_content_absent).map(row=>row.after.hash))]});
const report=(plan,extra={})=>({schema:VERSION,db_path:req.db_path,requested:plan.records.length,
changed:plan.records.filter(row=>!row.unchanged).length,unchanged:plan.records.filter(row=>row.unchanged).length,
changed_document_ids:plan.records.filter(row=>!row.unchanged).map(row=>row.before.id),
before:plan.records.map(row=>row.before),after:plan.records.map(row=>row.after),undo:undoFor(plan),embedding_invoked:false,...extra});
const deleteOrphan=db=>db.prepare(`DELETE FROM content WHERE hash=? AND NOT EXISTS
(SELECT 1 FROM documents INDEXED BY idx_documents_hash WHERE documents.hash=content.hash)`);
const restore=db=>{
  const undo=req.undo;
  if(!undo||undo.schema!==VERSION||undo.db_path!==req.db_path||!Array.isArray(undo.records)||undo.records.length>64||!Array.isArray(undo.created_content_hashes))fail('Invalid QMD undo value');
  const states=[];
  for(const item of undo.records){
    const b=item.before,a=item.after;
    if(!b||!a||b.id!==a.id||b.collection!==a.collection||b.path!==a.path||!/^lib_[a-f0-9]{20}$/.test(b.collection)||!/^o-[a-f0-9]{32}\.md$/.test(b.path)||sha(item.old_body)!==b.hash)fail('Invalid QMD undo record');
    const row=readDocument(db,b.collection,b.path);checkDocument(db,row);
    if(equal(metadata(row),b)&&row.doc===item.old_body){states.push('before');continue;}
    if(!equal(metadata(row),a))fail('Stale QMD undo state');states.push('after');
  }
  if(new Set(states).size>1)fail('Mixed QMD undo transaction state');
  if(!states.length||states[0]==='before')return {schema:VERSION,restored:0,unchanged:true,embedding_invoked:false};
  for(const item of undo.records){
    const b=item.before;
    const existing=db.prepare('SELECT doc,created_at FROM content WHERE hash=?').get(b.hash);
    if(existing&&(existing.doc!==item.old_body||existing.created_at!==b.content_created_at))fail('Conflicting QMD undo content');
    insertContent(db,b.hash,item.old_body,b.content_created_at);
    updateDocument(db,b.id,b.title,b.hash,b.modified_at);
    const row=readDocument(db,b.collection,b.path);checkDocument(db,row);
    if(!equal(metadata(row),b))fail('QMD undo verification failed');
  }
  let removed=0;
  const allowed=new Set(undo.records.map(row=>row.after.hash));
  for(const hash of undo.created_content_hashes){
    if(!/^[a-f0-9]{64}$/.test(hash)||!allowed.has(hash))fail('Invalid QMD undo created hash');
    removed+=deleteOrphan(db).run(hash).changes;
  }
  return {schema:VERSION,restored:undo.records.length,removed_content:removed,unchanged:false,embedding_invoked:false};
};
let db;
try{
  // A read-only preflight precedes even opening a writer. Check again under the
  // writer lock below to prevent a schema change between validation and apply.
  if(req.mode!=='prepare'){
    const probe=new Database(req.db_path,{readonly:true,fileMustExist:true,timeout:30000});
    try{schema(probe);}finally{probe.close();}
  }
  db=new Database(req.db_path,{readonly:req.mode==='prepare',fileMustExist:true,timeout:30000});
  db.pragma('foreign_keys=ON');
  db.exec(req.mode==='prepare'?'BEGIN':'BEGIN IMMEDIATE');
  try{
    schema(db);
    let result;
    if(req.mode==='restore')result=restore(db);
    else{
      const plan=makePlan(db);
      const applied=req.expected_plan?verifyPlan(plan,req.expected_plan):false;
      if(req.mode==='prepare')result={...report(plan),plan};
      else if(applied)result=report(plan,{inserted_content:0,removed_content:0,already_applied:true,undo:undoFor(req.expected_plan)});
      else{
        let inserted=0,removed=0,completed=0;
        const oldHashes=new Set();
        for(const row of plan.records){
          if(row.unchanged)continue;
          const existed=db.prepare('SELECT 1 FROM content WHERE hash=?').get(row.after.hash);
          insertContent(db,row.after.hash,row.new_body,row.after.content_created_at);if(!existed)inserted++;
          updateDocument(db,row.before.id,row.after.title,row.after.hash,row.after.modified_at);
          oldHashes.add(row.before.hash);
          if(req._test_fail_after!==undefined&&++completed===req._test_fail_after)fail('Injected QMD patch failure');
        }
        for(const hash of oldHashes)removed+=deleteOrphan(db).run(hash).changes;
        if(req._test_fail_after===completed+1)fail('Injected QMD patch failure after obsolete content cleanup');
        for(const row of plan.records){
          const actual=readDocument(db,row.after.collection,row.after.path);checkDocument(db,actual);
          if(!equal(metadata(actual),row.after)||actual.doc!==row.new_body)fail('QMD patch verification failed');
        }
        result=report(plan,{inserted_content:inserted,removed_content:removed});
      }
    }
    db.exec('COMMIT');process.stdout.write('QMD_PATCH_RESULT='+JSON.stringify(result)+'\n');
  }catch(error){db.exec('ROLLBACK');throw error;}
}finally{if(db)db.close();}
'''


def _updates(updates):
    if not isinstance(updates, (list, tuple)) or not 1 <= len(updates) <= _MAX_UPDATES:
        raise ValueError("A bounded nonempty list of QMD document updates is required")
    output, seen = [], set()
    for item in updates:
        if not isinstance(item, dict) or set(item) - {"collection", "path", "title", "text", "expected_old_hash"}:
            raise ValueError("Unknown QMD document update fields")
        collection, path = item.get("collection"), item.get("path")
        if (not isinstance(collection, str) or not _COLLECTION.fullmatch(collection)
                or not isinstance(path, str) or not _PATH.fullmatch(path) or path.split("/")[0] != collection):
            raise ValueError("QMD path must be the exact generated collection/object path")
        if path in seen:
            raise ValueError("Duplicate QMD document update path")
        seen.add(path)
        if any(not isinstance(item.get(key), str) or "\0" in item[key] for key in ("title", "text")):
            raise ValueError("QMD document title and representation text must be strings without NUL")
        if "expected_old_hash" in item and (not isinstance(item["expected_old_hash"], str) or not _HASH.fullmatch(item["expected_old_hash"])):
            raise ValueError("Expected QMD content hash must be SHA-256")
        output.append(dict(item))
    if len(json.dumps(output, ensure_ascii=False).encode("utf-8")) > _MAX_INPUT_BYTES:
        raise ValueError("QMD incremental patch input exceeds the bounded size limit")
    return output


def _run(mode, db_path, *, qmd_dir, node, updates=None, expected_plan=None, undo=None,
         env=None, timeout=120, runtime_dir=None, _test_fail_after=None):
    path = Path(db_path).expanduser().absolute()
    if path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise ValueError("QMD patch requires an existing regular database path")
    qmd_dir = Path(qmd_dir).expanduser().resolve(strict=True)
    node = Path(node).expanduser().resolve(strict=True)
    if json.loads((qmd_dir / "package.json").read_text(encoding="utf-8")).get("version") != QMD_VERSION:
        raise ValueError("QMD document patches require QMD 2.8.3")
    request = {"schema": PATCH_SCHEMA, "mode": mode, "db_path": str(path), "qmd_dir": str(qmd_dir),
               "env": env or {}, "newline": os.linesep}
    if updates is not None:
        request["updates"] = _updates(updates)
    if expected_plan is not None:
        request["expected_plan"] = expected_plan
    if undo is not None:
        request["undo"] = undo
    if _test_fail_after is not None:
        if not isinstance(_test_fail_after, int) or _test_fail_after < 1:
            raise ValueError("Failure injection requires a positive document count")
        request["_test_fail_after"] = _test_fail_after
    payload = json.dumps(request, ensure_ascii=False)
    if len(payload.encode("utf-8")) > 6 * _MAX_INPUT_BYTES:
        raise ValueError("QMD patch request exceeds the bounded size limit")
    # The helper contains code only. Source bodies travel solely through stdin
    # and the bounded return value, never a temporary Markdown/JSON export.
    with tempfile.TemporaryDirectory(prefix="qmd-patch-", dir=runtime_dir) as temporary:
        helper = Path(temporary) / "patch.mjs"
        helper.write_text(_WORKER, encoding="utf-8")
        result = run_text([str(node), str(helper)], input_text=payload, cwd=temporary,
                          timeout=timeout, max_output=12 * _MAX_INPUT_BYTES)
    if result.returncode:
        # Do not include SDK output or a request dump: both may contain bodies.
        detail = next((line.strip() for line in result.stderr.splitlines() if line.startswith("Error:")), "QMD patch worker failed")
        raise RuntimeError(detail[:240])
    marker = "QMD_PATCH_RESULT="
    found = result.stdout.rfind(marker)
    if found < 0:
        raise RuntimeError("QMD patch worker returned no verification report")
    return json.loads(result.stdout[found + len(marker):].splitlines()[0])


def prepare_keyword_document_patch(db_path, updates, *, qmd_dir, node, env=None, timeout=120, runtime_dir=None):
    """Read exact target rows and return ``plan`` and a small crash-recovery ``undo``.

    Each update has collection, full object_index.qmd_path path, title, text and
    optional expected_old_hash. ``text`` is the new representation; the normal
    ``# title`` Markdown wrapper and platform newlines match qmd_shared builds.
    Save the returned undo in the caller's journal before applying the plan.
    """
    return _run("prepare", db_path, updates=updates, qmd_dir=qmd_dir, node=node,
                env=env, timeout=timeout, runtime_dir=runtime_dir)


def patch_keyword_documents(db_path, updates, *, qmd_dir, node, expected_plan=None,
                            env=None, timeout=120, runtime_dir=None, _test_fail_after=None):
    """Patch only existing exact row IDs, atomically; return verified before/after.

    ``expected_plan`` is the ``plan`` from prepare_keyword_document_patch. It
    detects intervening title/timestamp/content changes as well as stale hashes.
    Repeating current title/text is a no-op. An explicitly stale old hash still
    rejects the request, including on retries.
    """
    return _run("patch", db_path, updates=updates, expected_plan=expected_plan,
                qmd_dir=qmd_dir, node=node, env=env, timeout=timeout,
                runtime_dir=runtime_dir, _test_fail_after=_test_fail_after)


def restore_keyword_document_patch(db_path, undo, *, qmd_dir, node, env=None, timeout=120, runtime_dir=None):
    """Atomically restore one prepared patch; reject any later document changes."""
    return _run("restore", db_path, undo=undo, qmd_dir=qmd_dir, node=node,
                env=env, timeout=timeout, runtime_dir=runtime_dir)
