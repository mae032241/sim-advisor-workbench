"""Isolated, generation-bound QMD keyword and QMD embedding search.

All modes resolve hits back to the unchanged supplied objects. QMD vector mode
uses searchVector (query embedding + vector lookup), never query expansion,
generation, reranking or a lexical substitute. Shared keyword collections apply
library/object scopes before global TopK. Legacy keyword and vector databases
remain separate per library.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat as stat_types
import threading
import time
import uuid
from contextlib import ExitStack, closing, contextmanager, nullcontext
from contextvars import ContextVar
from collections import OrderedDict
from pathlib import Path
from urllib.parse import unquote

from .process_runner import run_text
from .qmd_worker import get_worker, close_workers, worker_epoch, worker_preparation
from .offline_profile import local_model_path, retrieval_runtime
from .build_control import BuildCancelled, check_cancelled
from .index_paths import serialize_index_manifest
from .index_manifest_cache import read_manifest
from .index_manifest import publish_manifest
from . import object_index, query_scope_counts
from .query_terms import MODES, cjk_tokens as _cjk_tokens, lex_terms as _lex_terms, normalize_mode, normalize_modes
from .workspace_paths import WorkspacePaths
from .case_metadata import FEATURE_BRANCHES, normalize_feature_branch, object_feature_branch
from .retrieval_settings import read as read_embedding_settings, require_enabled
from .evaluation_sets import exclusion_snapshot
from . import case_context_store

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EMBED_MODEL = "hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf"
BRIDGE_MARKER = "LIBRARY_SEARCH_RESULT="
BRIDGE_VERSION = "2"
READABLE_BRIDGE_VERSIONS = {BRIDGE_VERSION, "2-context1", "2-context2"}
REPRESENTATION_CHARS = 1400
FILTER_ADAPTER_VERSION = "qmd-2.8.3-scope-v1"
KEYWORD_ADAPTER_VERSION = "qmd-2.8.3-terms-or-bm25-scope-v1"
KEYWORD_QUERY_POLICY = "expanded-terms-or-single-bm25-v1"
SHARED_KEYWORD_STORAGE = "shared-collections-v1"
_DIRECTORY_LOCKS = {}
_DIRECTORY_LOCKS_GUARD = threading.Lock()
_MODEL_HASH_CACHE = OrderedDict()
_MODEL_HASH_CACHE_LIMIT = 32
_MODEL_HASH_CACHE_LOCK = threading.Lock()
_HOLDOUT_CACHE_LOCK = threading.Lock()
_HOLDOUT_SCHEMA = "evaluation-search-exclusions/v3"
_QUERY_MANIFEST_RECEIPT = ContextVar("query_manifest_receipt", default=None)
NAVIGATION_STAGES = ("overview", *FEATURE_BRANCHES, "reference")


def _holdout_clause(*, compact, alias="o"):
    clause = f"NOT EXISTS(SELECT 1 FROM evaluation_holdout.identities h WHERE h.id={alias}.id)"
    if compact:
        clause += f" AND NOT EXISTS(SELECT 1 FROM evaluation_holdout.paths h WHERE h.path={alias}.qmd_path)"
    return clause


def _excluded_scope_counts(db, *, compact, scope_where, scope_params):
    # Start from the small policy tables and probe object unique keys. Applying
    # NOT EXISTS to the base COUNT would probe exclusions for the whole corpus.
    if compact:
        source = """(
            SELECT x.rowid AS rid FROM evaluation_holdout.identities h
            CROSS JOIN objects x ON x.id=h.id
            UNION
            SELECT x.rowid AS rid FROM evaluation_holdout.paths h
            CROSS JOIN objects x ON x.qmd_path=h.path
        ) excluded CROSS JOIN objects o ON o.rowid=excluded.rid"""
    else:
        # Legacy policy construction already expands path rules into unique IDs.
        source = "evaluation_holdout.identities h CROSS JOIN objects o ON o.id=h.id"
    return dict(db.execute("SELECT o.library_id,count(*) FROM " + source + " WHERE " + scope_where +
                           " GROUP BY o.library_id", scope_params))


def _navigation_options(retrieval_view, case_id, stage):
    if retrieval_view not in ("knowledge", "case_overviews"):
        raise ValueError("retrieval_view must be knowledge or case_overviews")
    if case_id is not None and (not isinstance(case_id, str) or not case_id.strip() or len(case_id) > 256):
        raise ValueError("case_id must be an exact nonempty overview citation ID")
    if stage is not None and stage not in NAVIGATION_STAGES:
        raise ValueError("Unsupported case navigation stage")
    if retrieval_view == "case_overviews" and (case_id is not None or stage is not None):
        raise ValueError("Overview search does not accept case_id or stage")
    if stage is not None and case_id is None:
        raise ValueError("stage requires an exact case_id")
    return retrieval_view != "knowledge" or case_id is not None


def _navigation_predicate(retrieval_view, case_id, stage, *, alias="o"):
    if retrieval_view == "case_overviews":
        return f"EXISTS(SELECT 1 FROM case_navigation.overviews n WHERE n.id={alias}.id)", []
    clause = (f"EXISTS(SELECT 1 FROM case_navigation.members n WHERE n.object_id={alias}.id "
              "AND n.case_id=? AND n.role IN ('implementation','reference') AND n.global_visible=1")
    params = [case_id]
    if stage is not None:
        clause += " AND n.stage=?"
        params.append(stage)
    return clause + ")", params


def _navigation_counts(db, retrieval_view, case_id, stage, *, scope_where, scope_params, holdout_where):
    # These live counts start from navigation's small indexed ID tables. They
    # never decode source bodies or expand the full corpus into a Python set.
    if retrieval_view == "case_overviews":
        source = "case_navigation.overviews n CROSS JOIN objects o ON o.id=n.id"
        where, params = "1", []
    else:
        source = "case_navigation.members n CROSS JOIN objects o ON o.id=n.object_id"
        where = "n.case_id=? AND n.role IN ('implementation','reference') AND n.global_visible=1"
        params = [case_id]
        if stage is not None:
            where += " AND n.stage=?"
            params.append(stage)
    return dict(db.execute("SELECT o.library_id,count(*) FROM " + source + " WHERE " + where +
                           " AND " + scope_where + holdout_where + " GROUP BY o.library_id", [*params, *scope_params]))


class LibraryVisibilityError(ValueError):
    """The managed library lifecycle catalog cannot safely authorize a query."""


class _QuerySnapshot:
    """One engine's validated metadata for exactly one upcoming query."""

    def __init__(self, owner, state, signatures, receipt):
        self._owner, self._state, self._signatures = owner, state, signatures
        self._receipt = receipt
        self._used = False
        self._lock = threading.Lock()

    @property
    def status(self):
        # Provider generation checks cannot mutate the state consumed by search.
        return copy.deepcopy(self._state)

    def consume(self, owner):
        with self._lock:
            if owner is not self._owner or self._used:
                raise ValueError("A query snapshot belongs to one engine and can be used only once")
            self._used = True
        current = owner._query_file_signatures(self._signatures)
        runtime = str(owner.data_dir / "runtime")
        created = current.get(runtime)
        if (runtime in self._signatures and self._signatures[runtime] is None and created is not None
                and stat_types.S_ISDIR(created[2]) and not created[3]):
            # The optional cache folder can be created by a reader after the
            # snapshot. Its guarded children must still match individually;
            # a newly introduced symlink/junction is never accepted here.
            current[runtime] = None
        if current != self._signatures or not owner._query_receipt_matches(current, self._receipt):
            raise RuntimeError("Library index changed during query preparation; retry the query")
        return self._state


def _hash_model_file(path):
    check_cancelled()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            check_cancelled()
            digest.update(block)
    check_cancelled()
    return digest.hexdigest()


def _cached_model_hash(path, stat):
    """Share bounded read-only hash results across short-lived status objects."""
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    # Keep concurrent polling misses from hashing the same large file together.
    with _MODEL_HASH_CACHE_LOCK:
        cached = _MODEL_HASH_CACHE.get(key)
        if cached is not None:
            _MODEL_HASH_CACHE.move_to_end(key)
            return cached
        digest = _hash_model_file(path)
        _MODEL_HASH_CACHE[key] = digest
        while len(_MODEL_HASH_CACHE) > _MODEL_HASH_CACHE_LIMIT:
            _MODEL_HASH_CACHE.popitem(last=False)
        return digest

_NODE_BRIDGE = r'''
import {readFileSync,mkdirSync,writeFileSync,renameSync,statSync} from 'node:fs';
import {join,dirname,relative} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createInterface} from 'node:readline';
const quiet=(...args)=>process.stderr.write(args.map(x=>typeof x==='string'?x:JSON.stringify(x)).join(' ')+'\n');
console.log=quiet;
const dispatch=async(envelope)=>{
for(const [key,value] of Object.entries(envelope.env)) process.env[key]=value;
if(!envelope.embedding_enabled)delete process.env.QMD_EMBED_MODEL;
// Chunk tokenization also uses QMD's process-wide default LLM. Keep it on the
// same resolved local model as this store; an HF URI could otherwise download
// despite an offline receipt and a store-specific local model configuration.
if(envelope.embedding_enabled&&envelope.op!=='pull'&&envelope.model_uri)process.env.QMD_EMBED_MODEL=envelope.model_uri;
if(envelope.op==='warmup'){
  await import(pathToFileURL(join(envelope.qmd_dir,'dist/index.js')).href);
  return {ready:true};
}
if(envelope.op==='batch'){
  if(!Array.isArray(envelope.requests)||!envelope.requests.length)throw new Error('QMD query batch must contain requests');
  const libraries=new Set();
  for(const item of envelope.requests){
    if(!item||typeof item!=='object'||Array.isArray(item)||!['keyword','vector'].includes(item.op))throw new Error('QMD batches support keyword/vector queries only');
    if(typeof item.library_key!=='string'||!item.library_key.trim()||item.library_key.length>100||/[\0\r\n]/.test(item.library_key)||libraries.has(item.library_key))throw new Error('QMD query batch requires unique library keys');
    libraries.add(item.library_key);
    if(['requests','qmd_dir','env','model_uri','embedding_enabled','model_cache'].some(key=>key in item))throw new Error('QMD batch requests cannot override the shared runtime');
    if(typeof item.db_path!=='string'||!statSync(item.db_path).isFile())throw new Error('QMD query batch requires existing database files');
    if(item.op==='vector'&&(!envelope.embedding_enabled||!envelope.model_uri||/^(?:hf:|https?:)/i.test(envelope.model_uri)))throw new Error('QMD vector batch requires a verified local embedding model');
  }
}
if(envelope.op==='shared_keyword'){
  if(JSON.parse(readFileSync(join(envelope.qmd_dir,'package.json'),'utf8')).version!=='2.8.3')throw new Error('Shared QMD retrieval requires the supported 2.8.3 SQLite adapter');
  if(typeof envelope.db_path!=='string'||!statSync(envelope.db_path).isFile())throw new Error('Shared QMD queries require an existing database');
  if(!Array.isArray(envelope.collections)||!envelope.collections.length)throw new Error('Shared QMD queries require collection scopes');
  if(typeof envelope.query!=='string'||!envelope.query.trim())throw new Error('Invalid shared QMD query');
  if(!Number.isInteger(envelope.limit)||envelope.limit<1||envelope.limit>100)throw new Error('Invalid shared QMD query limit');
  if(envelope.authoritative_scope!==undefined){
    const scope=envelope.authoritative_scope;
    if(!scope||typeof scope!=='object'||Array.isArray(scope)||Object.keys(scope).some(key=>!['db_path','kind','feature_branch','navigation','case_context'].includes(key))||typeof scope.db_path!=='string'||!statSync(scope.db_path).isFile())throw new Error('Invalid authoritative QMD scope database');
    if(typeof scope.kind!=='string'||!scope.kind.trim()||scope.kind.length>100||scope.feature_branch!==null&&!['geometry','mesh','physics','material','multiphysics','selection','study','solver','result','param','func','variable','misc'].includes(scope.feature_branch))throw new Error('Invalid authoritative QMD scope filters');
    if(scope.navigation!==undefined){
      const n=scope.navigation;
      if(!n||typeof n!=='object'||Array.isArray(n)||Object.keys(n).some(key=>!['db_path','view','case_id','stage'].includes(key))||typeof n.db_path!=='string'||!statSync(n.db_path).isFile()||!['knowledge','case_overviews'].includes(n.view))throw new Error('Invalid QMD navigation scope');
      if(n.view==='case_overviews'&&(n.case_id!==null||n.stage!==null)||n.view==='knowledge'&&(typeof n.case_id!=='string'||!n.case_id.trim()||n.case_id.length>256)||n.stage!==null&&!['overview','geometry','mesh','physics','material','multiphysics','selection','study','solver','result','param','func','variable','misc','reference'].includes(n.stage))throw new Error('Invalid QMD navigation filters');
    }
    if(scope.case_context!==undefined){
      const c=scope.case_context;
      if(!c||typeof c!=='object'||Array.isArray(c)||Object.keys(c).length!==4||Object.keys(c).some(key=>!['db_path','catalog_path','revision_table','exclude_geometry_only'].includes(key))||['db_path','catalog_path'].some(key=>typeof c[key]!=='string'||!statSync(c[key]).isFile())||!['_revision_payloads','revisions'].includes(c.revision_table)||typeof c.exclude_geometry_only!=='boolean')throw new Error('Invalid QMD case context scope');
    }
  }
  const owners=new Set(),collections=new Set();
  for(const item of envelope.collections){
    if(!item||typeof item.library_key!=='string'||!item.library_key.trim()||owners.has(item.library_key)||!/^lib_[a-f0-9]{20}$/.test(item.collection)||item.collection!==item.key||collections.has(item.collection))throw new Error('Shared QMD collections must have unique library identities');
    owners.add(item.library_key);collections.add(item.collection);
    if(item.allowed_paths!==undefined&&(!Array.isArray(item.allowed_paths)||item.allowed_paths.some(path=>typeof path!=='string'||path.length>200||!/^[A-Za-z0-9][A-Za-z0-9._-]*\.md$/.test(path))))throw new Error('Invalid shared QMD allowed paths');
  }
}
// Node caches the SDK module for the worker lifetime. Stores remain per-request
// so attached policy databases/TEMP scopes and generation handles never linger.
const createStore=envelope.op==='pull'?null:(await import(pathToFileURL(join(envelope.qmd_dir,'dist/index.js')).href)).createStore;
const runOne=async(req)=>{
let result;
if(req.op==='pull'){
  const {pullModels}=await import(pathToFileURL(join(req.qmd_dir,'dist/llm.js')).href);
  result=await pullModels([req.model_uri],{cacheDir:req.model_cache,cli:false});
}else{
  const sharedKeyword=req.op==='shared_keyword';
  if(!sharedKeyword&&relative(dirname(req.db_path),req.corpus).replaceAll('\\','/')!=='corpus')throw new Error('QMD corpus must be the sibling corpus directory');
  const config={collections:{objects:{path:req.corpus,pattern:'**/*.md'}}};
  if(req.embedding_enabled&&req.model_uri)config.models={embed:req.model_uri};
  mkdirSync(dirname(req.db_path),{recursive:true});
  const reading=['keyword','vector','status','shared_keyword'].includes(req.op);
  // Opening a moved index must not reindex or regenerate embeddings. Inline
  // config sync has wider side effects, so reads use DB-only mode and change
  // only this collection's physical locator. Virtual document IDs stay intact.
  const store=await createStore(reading?{dbPath:req.db_path}:{dbPath:req.db_path,config});
  const holdoutWhere=alias=>req.holdout_db_path?`NOT EXISTS(SELECT 1 FROM evaluation_holdout.paths h WHERE h.path=${sharedKeyword?alias+'.collection':"'"+req.holdout_collection+"'"} || '/' || ${alias}.path)`:'1';
  const prepareHoldout=()=>{if(req.holdout_db_path!==undefined){
    if(!['keyword','vector','shared_keyword'].includes(req.op)||typeof req.holdout_db_path!=='string'||!statSync(req.holdout_db_path).isFile())throw new Error('Evaluation exclusions require an existing query-only policy database');
    if(!sharedKeyword&&!/^lib_[a-f0-9]{20}$/.test(req.holdout_collection||''))throw new Error('Evaluation exclusions require an authoritative collection key');
    if(JSON.parse(readFileSync(join(req.qmd_dir,'package.json'),'utf8')).version!=='2.8.3')throw new Error('Evaluation exclusions require the supported QMD 2.8.3 adapter; no global fallback');
    // better-sqlite3 does not enable SQLite URI filenames. This is our
    // disposable ID-only cache, never a catalog, registry or source index;
    // every use of the attached schema below is a SELECT predicate.
    store.internal.db.prepare('ATTACH DATABASE ? AS evaluation_holdout').run(req.holdout_db_path);
    store.internal.db.prepare('SELECT path FROM evaluation_holdout.paths LIMIT 0').all();
  }};
  const keywordMatch=async()=>{
    if(JSON.parse(readFileSync(join(req.qmd_dir,'package.json'),'utf8')).version!=='2.8.3')throw new Error('QMD keyword retrieval requires the supported 2.8.3 SQLite adapter');
    if(!Array.isArray(req.keyword_terms)||req.keyword_terms.length>60||req.keyword_terms.some(term=>typeof term!=='string'||!term))throw new Error('Invalid QMD keyword terms');
    const {normalizeCjkForFTS}=await import(pathToFileURL(join(req.qmd_dir,'dist/store.js')).href);
    if(typeof normalizeCjkForFTS!=='function')throw new Error('QMD 2.8.3 CJK normalization unavailable');
    // Match QMD's stored CJK token boundaries term by term. The whole query is
    // one quoted OR expression and one BM25 pass, with no searchLex parser.
    const match=req.keyword_terms.map(term=>'"'+normalizeCjkForFTS(term).replaceAll('"','""')+'"').join(' OR ');
    return match||null;
  };
  const portableLocator=()=>{
    const collection=store.internal.db.prepare('SELECT path FROM store_collections WHERE name = ?').get('objects');
    if(!collection)throw new Error('QMD index has no objects collection');
    if(collection.path!=='corpus')store.internal.db.prepare('UPDATE store_collections SET path = ? WHERE name = ?').run('corpus','objects');
  };
  let scopeDocs;
  const prepareScope=()=>{
    if(req.allowed_paths===undefined)return;
    const version=JSON.parse(readFileSync(join(req.qmd_dir,'package.json'),'utf8')).version;
    const db=store.internal?.db;
    if(version!=='2.8.3'||!db?.exec||!db?.transaction)throw new Error('QMD scoped retrieval requires the supported 2.8.3 SQLite adapter; no global fallback');
    if(!Array.isArray(req.allowed_paths)||req.allowed_paths.some(p=>typeof p!=='string'||p.length>200||!/^[A-Za-z0-9][A-Za-z0-9._-]*\.md$/.test(p)))throw new Error('Invalid safe object paths in retrieval scope');
    for(const [table,columns] of Object.entries({documents:['id','collection','path','hash','title','active'],content:['hash','doc'],documents_fts:['filepath','title','body']})){
      const actual=new Set(db.prepare(`PRAGMA main.table_info(${table})`).all().map(x=>x.name));
      if(columns.some(c=>!actual.has(c)))throw new Error('QMD 2.8.3 scoped retrieval schema unavailable: '+table+'; no global fallback');
    }
    db.exec('PRAGMA temp_store=MEMORY; CREATE TEMP TABLE query_scope_paths(path TEXT PRIMARY KEY)');
    const insert=db.prepare('INSERT OR IGNORE INTO temp.query_scope_paths(path) VALUES(?)');
    db.transaction(()=>{for(const path of req.allowed_paths)insert.run(path);})();
    scopeDocs=db.prepare(`SELECT d.id,d.path,d.title,d.hash FROM main.documents d
      JOIN temp.query_scope_paths s ON s.path=d.path WHERE d.collection='objects' AND d.active=1 AND ${holdoutWhere('d')}`).all();
  };
  const scopedVector=async()=>{
    if(!scopeDocs.length)return [];
    const db=store.internal.db;
    const columns=new Set(db.prepare('PRAGMA main.table_info(content_vectors)').all().map(x=>x.name));
    if(!['hash','seq','pos','model','embed_fingerprint'].every(x=>columns.has(x))||!db.prepare("SELECT name FROM main.sqlite_master WHERE type='table' AND name='vectors_vec'").get())throw new Error('QMD 2.8.3 scoped vector schema unavailable; no global fallback');
    const model=req.index_model_uri||req.model_uri;
    const pathJoin=req.allowed_paths===undefined?'':' JOIN temp.query_scope_paths s ON s.path=d.path';
    let fingerprint;
    try{
      const {getEmbeddingFingerprint}=await import(pathToFileURL(join(req.qmd_dir,'dist/store.js')).href);
      if(typeof getEmbeddingFingerprint==='function')fingerprint=getEmbeddingFingerprint(model);
    }catch{}
    if(!/^[a-f0-9]{6}$/.test(fingerprint||''))throw new Error('QMD 2.8.3 fingerprint API unavailable; no global fallback');
    const inconsistent=db.prepare(`SELECT 1 FROM main.content_vectors cv
      JOIN main.documents d ON d.hash=cv.hash ${pathJoin}
      WHERE d.collection='objects' AND d.active=1 AND ${holdoutWhere('d')} AND (cv.model<>? OR cv.embed_fingerprint<>?) LIMIT 1`).get(model,fingerprint);
    if(inconsistent)throw new Error('Scoped vectors have inconsistent model/fingerprint identity; refusing mixed-vector retrieval');
    const chunks=db.prepare(`SELECT DISTINCT cv.hash,cv.seq,cv.pos FROM main.content_vectors cv
      JOIN main.documents d ON d.hash=cv.hash ${pathJoin}
      WHERE d.collection='objects' AND d.active=1 AND ${holdoutWhere('d')} AND cv.model=? AND cv.embed_fingerprint=?`).all(model,fingerprint);
    const matchingHashes=new Set(chunks.map(x=>x.hash));
    if(scopeDocs.some(doc=>!matchingHashes.has(doc.hash)))throw new Error('Scoped vector entries are missing for the index model/fingerprint');
    if(typeof store.internal.llm?.embed!=='function')throw new Error('QMD 2.8.3 scoped query embedding API unavailable; no global fallback');
    const {formatQueryForEmbedding}=await import(pathToFileURL(join(req.qmd_dir,'dist/llm.js')).href);
    const query=await store.internal.llm.embed(formatQueryForEmbedding(req.query,model),{model:req.model_uri,isQuery:true});
    if(!query?.embedding?.length||query.embedding.some(x=>!Number.isFinite(x)))throw new Error('QMD query embedding failed; no lexical fallback');
    const embedding=new Float32Array(query.embedding);
    const docsByHash=new Map(),best=new Map();
    for(const doc of scopeDocs){const list=docsByHash.get(doc.hash)||[];list.push(doc);docsByHash.set(doc.hash,list);}
    for(let offset=0;offset<chunks.length;offset+=400){
      const batch=chunks.slice(offset,offset+400),info=new Map(batch.map(x=>[x.hash+'_'+x.seq,x]));
      // Match the SDK's safe exact-cosine scan, never JOIN sqlite-vec's ANN
      // MATCH table and never fall back to global ANN for a large scope.
      const rows=db.prepare(`SELECT hash_seq,vec_distance_cosine(embedding,?) AS distance FROM vectors_vec WHERE hash_seq IN (${batch.map(()=>'?').join(',')})`).all(embedding,...info.keys());
      for(const row of rows){
        const chunk=info.get(row.hash_seq);if(!chunk||!Number.isFinite(row.distance))continue;
        for(const doc of docsByHash.get(chunk.hash)||[]){const old=best.get(doc.path);
          if(!old||row.distance<old.distance)best.set(doc.path,{filepath:'qmd://objects/'+doc.path,score:1-row.distance,source:'vec',chunkPos:chunk.pos,distance:row.distance});}
      }
    }
    return [...best.values()].sort((a,b)=>b.score-a.score||a.filepath.localeCompare(b.filepath)).slice(0,req.limit).map(({distance,...row})=>row);
  };
  const searchSharedKeyword=async()=>{
    const version=JSON.parse(readFileSync(join(req.qmd_dir,'package.json'),'utf8')).version;
    const db=store.internal?.db;
    if(version!=='2.8.3'||!db?.prepare||!db?.exec||!db?.transaction)throw new Error('Shared QMD retrieval requires the supported 2.8.3 SQLite adapter');
    const match=await keywordMatch();
    if(match===null)return [];
    const registered=new Set(db.prepare('SELECT name FROM store_collections').all().map(row=>row.name));
    if(req.collections.some(item=>!registered.has(item.collection)))throw new Error('Shared QMD collection is missing');
    let authoritativeWhere='',authoritativeParams=[];
    if(req.authoritative_scope!==undefined){
      // Attach the unchanged object lookup, never the large revision payloads.
      // All access to this attached schema is SELECT-only and before TopK.
      db.prepare('ATTACH DATABASE ? AS authoritative_objects').run(req.authoritative_scope.db_path);
      const columns=new Set(db.prepare('PRAGMA authoritative_objects.table_info(objects)').all().map(row=>row.name));
      if(!['id','library_id','kind','feature_branch','qmd_path','source_json'].every(column=>columns.has(column)))throw new Error('Authoritative compact QMD scope schema unavailable');
      authoritativeWhere=' AND EXISTS(SELECT 1 FROM authoritative_objects.objects o WHERE o.qmd_path=d.collection || \'/\' || d.path AND o.library_id=s.library_id';
      if(req.authoritative_scope.kind!=='all'){authoritativeWhere+=' AND o.kind=?';authoritativeParams.push(req.authoritative_scope.kind);}
      if(req.authoritative_scope.feature_branch!==null){authoritativeWhere+=' AND o.feature_branch=?';authoritativeParams.push(req.authoritative_scope.feature_branch);}
      const navigation=req.authoritative_scope.navigation;
      if(navigation!==undefined){
        db.prepare('ATTACH DATABASE ? AS case_navigation').run(navigation.db_path);
        if(navigation.view==='case_overviews')authoritativeWhere+=' AND EXISTS(SELECT 1 FROM case_navigation.overviews n WHERE n.id=o.id)';
        else{
          authoritativeWhere+=" AND EXISTS(SELECT 1 FROM case_navigation.members n WHERE n.object_id=o.id AND n.case_id=? AND n.role IN ('implementation','reference') AND n.global_visible=1";
          authoritativeParams.push(navigation.case_id);
          if(navigation.stage!==null){authoritativeWhere+=' AND n.stage=?';authoritativeParams.push(navigation.stage);}
          authoritativeWhere+=')';
        }
      }
      const context=req.authoritative_scope.case_context;
      if(context!==undefined){
        if(!['_revision_payloads','revisions'].includes(context.revision_table))throw new Error('Invalid case context catalog identity table');
        db.prepare('ATTACH DATABASE ? AS case_contexts').run(context.db_path);
        db.prepare('ATTACH DATABASE ? AS case_context_catalog').run(context.catalog_path);
        let blocked="NOT EXISTS(SELECT 1 FROM case_context_catalog.libraries l WHERE l.id=dep.library_id AND coalesce(json_extract(l.body,'$.enabled'),1)=1 AND json_extract(l.body,'$.deleted_at') IS NULL) OR NOT EXISTS(SELECT 1 FROM case_context_catalog."+context.revision_table+" r JOIN case_context_catalog.active a ON a.library_id=r.library_id AND a.object_id=r.object_id AND a.citation_id=r.id WHERE r.id=dep.source_id AND r.library_id=dep.library_id AND r.object_id=dep.object_id)";
        let contextBlocked='0';
        if(req.holdout_db_path){
          blocked+=" OR EXISTS(SELECT 1 FROM evaluation_holdout.identities h WHERE h.id=dep.source_id OR h.id=dep.object_id) OR EXISTS(SELECT 1 FROM evaluation_holdout.case_groups h JOIN case_context_catalog.case_members m ON m.group_id=h.case_group WHERE m.citation_id=dep.source_id) OR EXISTS(SELECT 1 FROM evaluation_holdout.paths h JOIN authoritative_objects.objects x ON x.qmd_path=h.path WHERE x.id=dep.source_id)";
          contextBlocked='EXISTS(SELECT 1 FROM evaluation_holdout.case_groups h WHERE h.case_group=c.case_group)';
        }
        authoritativeWhere+=' AND NOT EXISTS(SELECT 1 FROM case_contexts.contexts c WHERE c.case_id=o.id AND ('+contextBlocked+' OR EXISTS(SELECT 1 FROM case_contexts.dependencies dep WHERE dep.case_id=c.case_id AND ('+blocked+'))))';
        if(context.exclude_geometry_only)authoritativeWhere+=" AND NOT EXISTS(SELECT 1 FROM case_contexts.capabilities cap WHERE cap.case_id=o.id AND cap.classification='geometry_only')";
      }
      if(req.holdout_db_path)authoritativeWhere+=' AND NOT EXISTS(SELECT 1 FROM evaluation_holdout.identities h WHERE h.id=o.id)';
      authoritativeWhere+=')';
    }
    db.exec('PRAGMA temp_store=MEMORY; CREATE TEMP TABLE shared_query_collections(collection TEXT PRIMARY KEY,library_id TEXT NOT NULL,restricted INTEGER NOT NULL); CREATE TEMP TABLE shared_query_paths(collection TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(collection,path))');
    const addCollection=db.prepare('INSERT INTO temp.shared_query_collections VALUES(?,?,?)');
    const addPath=db.prepare('INSERT OR IGNORE INTO temp.shared_query_paths VALUES(?,?)');
    db.transaction(()=>{
      for(const item of req.collections){
        addCollection.run(item.collection,item.library_key,item.allowed_paths===undefined?0:1);
        for(const path of item.allowed_paths||[])addPath.run(item.collection,path);
      }
    })();
    // CROSS JOIN fixes the FTS cursor first. Collection/path restrictions are
    // applied to every matching row before TopK, without copying FTS. The rank
    // mapping keeps the SDK's exact BM25 weights while FTS supplies score order.
    const select=db.prepare(`SELECT 'qmd://' || d.collection || '/' || d.path AS filepath,
        documents_fts.rank AS bm25_score
      FROM documents_fts CROSS JOIN documents d ON d.id=documents_fts.rowid
      CROSS JOIN temp.shared_query_collections s ON s.collection=d.collection
      WHERE documents_fts MATCH ? AND documents_fts.rank MATCH 'bm25(1.5,4.0,1.0)' AND d.active=1
        AND ${holdoutWhere('d')}
        ${authoritativeWhere}
        AND (s.restricted=0 OR EXISTS(SELECT 1 FROM temp.shared_query_paths p WHERE p.collection=d.collection AND p.path=d.path))
      ORDER BY documents_fts.rank ASC LIMIT ?`);
    return select.all(match,...authoritativeParams,req.limit).map(hit=>({filepath:hit.filepath,
      score:Math.abs(hit.bm25_score)/(1+Math.abs(hit.bm25_score)),source:'fts'}));
  };
  const searchKeyword=async()=>{
    const match=await keywordMatch(),db=store.internal.db;
    if(match===null)return [];
    const scope=req.allowed_paths===undefined?'':' AND EXISTS(SELECT 1 FROM temp.query_scope_paths s WHERE s.path=d.path)';
    // Evaluate exclusions against every FTS match before ranking/TopK. This
    // neither materializes the corpus nor lets held-out bodies reach the SDK.
    const select=db.prepare(`SELECT 'qmd://objects/' || d.path AS filepath,
        documents_fts.rank AS bm25_score
      FROM main.documents_fts CROSS JOIN main.documents d ON d.id=documents_fts.rowid
      WHERE documents_fts MATCH ? AND documents_fts.rank MATCH 'bm25(1.5,4.0,1.0)' AND d.collection='objects' AND d.active=1
        AND ${holdoutWhere('d')}${scope} ORDER BY documents_fts.rank ASC LIMIT ?`);
    return select.all(match,req.limit).map(hit=>({filepath:hit.filepath,
      score:Math.abs(hit.bm25_score)/(1+Math.abs(hit.bm25_score)),source:'fts'}));
  };
  try{
    prepareHoldout();
    if(reading&&!sharedKeyword)portableLocator();
    if(req.allowed_paths!==undefined){if(!['keyword','vector'].includes(req.op))throw new Error('Retrieval scopes are query-only');prepareScope();}
    if(req.op==='vector'&&req.holdout_db_path&&scopeDocs===undefined){
      scopeDocs=store.internal.db.prepare(`SELECT d.id,d.path,d.title,d.hash FROM main.documents d
        WHERE d.collection='objects' AND d.active=1 AND ${holdoutWhere('d')}`).all();
    }
    if(sharedKeyword)result=await searchSharedKeyword();
    else if(req.op==='keyword')result=await searchKeyword();
    else if(req.op==='update') result=await store.update();
    else if(req.op==='embed'){
      const onProgress=info=>{
        if(!req.progress_path)return;
        const progress={updated_at:new Date().toISOString()};
        for(const key of ['chunksEmbedded','totalChunks','bytesProcessed','totalBytes','errors'])progress[key]=info[key];
        writeFileSync(req.progress_path+'.tmp',JSON.stringify(progress));renameSync(req.progress_path+'.tmp',req.progress_path);
      };
      const embedded=await store.embed({model:req.model_uri,maxDocsPerBatch:8,maxBatchBytes:262144,chunkStrategy:'regex',onProgress});
      const state=await store.getStatus();
      const rows=store.internal.db.prepare('SELECT model,embed_fingerprint,count(*) AS chunks FROM content_vectors GROUP BY model,embed_fingerprint').all();
      const schema=store.internal.db.prepare("SELECT sql FROM sqlite_master WHERE name='vectors_vec'").get()?.sql||'';
      const dimension=schema.match(/float\s*\[(\d+)\]/i);
      result={embedded,state,embedding_rows:rows,embedding_dimensions:dimension?Number(dimension[1]):null};
    }else if(req.op==='vector'){
      if(scopeDocs!==undefined)result=await scopedVector();
      else{
        const identity=req.index_model_uri||req.model_uri;
        const {formatQueryForEmbedding}=await import(pathToFileURL(join(req.qmd_dir,'dist/llm.js')).href);
        const query=await store.internal.llm.embed(formatQueryForEmbedding(req.query,identity),{model:req.model_uri,isQuery:true});
        if(!query?.embedding?.length||query.embedding.some(x=>!Number.isFinite(x)))throw new Error('Local query embedding failed');
        result=await store.internal.searchVec(req.query,identity,req.limit,'objects',undefined,query.embedding);
      }
    }
    else if(req.op==='status') result=req.index_model_uri?store.internal.getStatus(req.index_model_uri):await store.getStatus();
    else throw new Error('Unsupported operation');
  }finally{try{if(!reading)portableLocator();}finally{await store.close();}}
}
return result;
};
let result;
if(envelope.op==='batch'){
  result=[];
  for(const item of envelope.requests){
    const request={...item,qmd_dir:envelope.qmd_dir,embedding_enabled:envelope.embedding_enabled,
      model_uri:envelope.model_uri,model_cache:envelope.model_cache};
    const hits=await runOne(request);
    result.push({library_key:item.library_key,results:hits.map(({filepath,score,source,chunkPos})=>({filepath,score,source,...(chunkPos===undefined?{}:{chunkPos})}))});
  }
}else result=await runOne(envelope);
return result;
};
if(process.argv.includes('--worker')){
  const lines=createInterface({input:process.stdin,crlfDelay:Infinity});
  for await(const line of lines){
    let id=null;
    try{
      const message=JSON.parse(line);id=message.id;
      if(!Number.isSafeInteger(id)||id<1||!message.request||typeof message.request!=='object')throw new Error('Invalid QMD worker request');
      if(!['warmup','keyword','shared_keyword','batch'].includes(message.request.op))throw new Error('Operation is not enabled for the QMD query worker');
      if(message.request.op==='batch'&&message.request.requests?.some(item=>item.op!=='keyword'))throw new Error('Persistent QMD batches are keyword-only');
      const result=await dispatch(message.request);
      process.stdout.write('LIBRARY_SEARCH_RESPONSE='+JSON.stringify({id,ok:true,result})+'\n');
    }catch(error){process.stdout.write('LIBRARY_SEARCH_RESPONSE='+JSON.stringify({id,ok:false,error:String(error?.message||error)})+'\n');}
  }
}else{
  const result=await dispatch(JSON.parse(readFileSync(0,'utf8')));
  process.stdout.write('LIBRARY_SEARCH_RESULT='+JSON.stringify(result)+'\n');
}
'''


def _query_batch_requests(request):
    requests = request.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ValueError("QMD query batch must contain requests")
    seen = set()
    shared = {"requests", "qmd_dir", "env", "model_uri", "embedding_enabled", "model_cache"}
    for item in requests:
        if not isinstance(item, dict) or item.get("op") not in {"keyword", "vector"}:
            raise ValueError("QMD batches support keyword/vector queries only")
        key = item.get("library_key")
        if not isinstance(key, str) or not key.strip() or len(key) > 100 or any(char in key for char in "\0\r\n") or key in seen:
            raise ValueError("QMD query batch requires unique library keys")
        if shared.intersection(item):
            raise ValueError("QMD batch requests cannot override the shared runtime")
        if not isinstance(item.get("db_path"), str) or not Path(item["db_path"]).is_file():
            raise ValueError("QMD query batch requires existing database files")
        seen.add(key)
    return requests


def _query_batch_results(requests, response):
    if not isinstance(response, list) or len(response) != len(requests):
        raise RuntimeError("QMD query batch returned missing or extraneous library responses")
    for request, item in zip(requests, response):
        if (not isinstance(item, dict) or set(item) != {"library_key", "results"}
                or item["library_key"] != request["library_key"] or not isinstance(item["results"], list)):
            raise RuntimeError("QMD query batch response does not match its library request")
    return response


def _shared_keyword_collections(request):
    if not isinstance(request.get("db_path"), str) or not Path(request["db_path"]).is_file():
        raise ValueError("Shared QMD queries require an existing database")
    if not isinstance(request.get("query"), str) or not request["query"].strip():
        raise ValueError("Invalid shared QMD query")
    if type(request.get("limit")) is not int or not 1 <= request["limit"] <= 100:
        raise ValueError("Invalid shared QMD query limit")
    if "authoritative_scope" in request:
        scope = request["authoritative_scope"]
        if (not isinstance(scope, dict) or set(scope) - {"db_path", "kind", "feature_branch", "navigation", "case_context"}
                or not {"db_path", "kind", "feature_branch"} <= set(scope)
                or not isinstance(scope["db_path"], str) or not Path(scope["db_path"]).is_file()):
            raise ValueError("Invalid authoritative QMD scope database")
        if (not isinstance(scope["kind"], str) or not scope["kind"].strip() or len(scope["kind"]) > 100
                or normalize_feature_branch(scope["feature_branch"]) != scope["feature_branch"]):
            raise ValueError("Invalid authoritative QMD scope filters")
        if "navigation" in scope:
            navigation = scope["navigation"]
            if (not isinstance(navigation, dict) or set(navigation) != {"db_path", "view", "case_id", "stage"}
                    or not isinstance(navigation["db_path"], str) or not Path(navigation["db_path"]).is_file()):
                raise ValueError("Invalid QMD navigation scope")
            if not _navigation_options(navigation["view"], navigation["case_id"], navigation["stage"]):
                raise ValueError("Invalid QMD navigation filters")
        if "case_context" in scope:
            context = scope["case_context"]
            if (not isinstance(context, dict) or set(context) != {"db_path", "catalog_path", "revision_table", "exclude_geometry_only"}
                    or any(not isinstance(context[key], str) or not Path(context[key]).is_file()
                           for key in ("db_path", "catalog_path"))
                    or context["revision_table"] not in {"_revision_payloads", "revisions"}
                    or not isinstance(context["exclude_geometry_only"], bool)):
                raise ValueError("Invalid QMD case context scope")
    collections = request.get("collections")
    if not isinstance(collections, list) or not collections:
        raise ValueError("Shared QMD queries require collection scopes")
    owners, seen = set(), set()
    for item in collections:
        if not isinstance(item, dict):
            raise ValueError("Shared QMD collections must have unique library identities")
        owner, collection = item.get("library_key"), item.get("collection")
        if (not isinstance(owner, str) or not owner.strip() or len(owner) > 100 or owner in owners
                or not isinstance(collection, str) or collection != "lib_" + _hash(owner)[:20]
                or item.get("key") != collection or collection in seen):
            raise ValueError("Shared QMD collections must have unique library identities")
        paths = item.get("allowed_paths")
        if "allowed_paths" in item and (not isinstance(paths, list) or any(
                not isinstance(path, str) or len(path) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md", path)
                for path in paths)):
            raise ValueError("Invalid shared QMD allowed paths")
        owners.add(owner)
        seen.add(collection)
    return collections


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def _object_cursor(objects):
    rows = iter(objects)
    try:
        yield rows
    finally:
        close = getattr(rows, "close", None)
        if close is not None:
            close()


def _corpus_hash(objects):
    """Hash the canonical JSON array without materializing the whole corpus.

    Each object uses the same encoder as before; array delimiters preserve the
    exact existing fingerprint while temporary strings are one object wide.
    """
    digest = hashlib.sha256()
    digest.update(b"[")
    with _object_cursor(objects) as rows:
        for index, item in enumerate(rows):
            check_cancelled()
            if index:
                digest.update(b",")
            digest.update(_json(item).encode("utf-8"))
    digest.update(b"]")
    check_cancelled()
    return digest.hexdigest()


_MANIFEST_LOCATORS = ("directory", "libraries", "path_schema", "keyword_storage")


def _serialize_manifest(value, root):
    """Convert owned locators without copying per-object retrieval maps."""
    stored = dict(value)
    stored.update(serialize_index_manifest({key: value[key] for key in _MANIFEST_LOCATORS if key in value}, root))
    return stored


def _write_json(path, value, *, cancellable=True):
    if cancellable:
        check_cancelled()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(_json(value), encoding="utf-8")
        if cancellable:
            check_cancelled()
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()



def _representation(item, case_context=None):
    """Bounded retrieval view; authoritative full object remains unchanged."""
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    annotation = data.get("annotation") if isinstance(data.get("annotation"), dict) else {}
    parts = []
    branch = object_feature_branch(item)
    structure = {}
    if branch:
        structure["feature_branch"] = branch
    for key in ("feature_type", "source_order", "execution_status", "code_origin", "standalone"):
        if data.get(key) is not None:
            structure[key] = str(data[key])[:90] if isinstance(data[key], str) else data[key]
    if isinstance(data.get("function"), dict):
        structure["function"] = {key: str(data["function"][key])[:90] for key in ("name", "ordinal") if key in data["function"]}
    elif data.get("function"):
        structure["function"] = str(data["function"])[:90]
    paths = data.get("api_object_paths")
    if isinstance(paths, list):
        structure["api_object_paths"] = [str(path)[:100] for path in paths[:4]]
    if data.get("summary"):
        structure["summary"] = str(data["summary"])[:220]
    for key in ("objects_created", "references", "prerequisites"):
        if isinstance(data.get(key), list) and data[key]:
            structure[key] = [({k: str(value[k])[:80] for k in ("path", "type", "kind", "status") if k in value}
                               if isinstance(value, dict) else str(value)[:100]) for value in data[key][:2]]
    if structure:
        parts.append("FEATURE STRUCTURE (source description; execution not verified): " + _json(structure)[:680])
    annotation_fields = {}
    for key, maximum in (("objective", 140), ("purpose", 90), ("summary", 140), ("applicability", 90),
                         ("required_inputs", 80), ("checks", 80), ("reuse_cautions", 80)):
        if annotation.get(key):
            value = annotation[key]
            annotation_fields[key] = (value if isinstance(value, str) else _json(value))[:maximum]
    if annotation_fields:
        parts.append("INFERRED ANNOTATION (not observed model facts): " + _json(annotation_fields)[:420 if structure else 850])
    metadata = {key: item[key] for key in ("type", "kind", "version", "asset_key", "section_path") if item.get(key)}
    metadata.update({key: data[key] for key in ("section", "scope_hint", "language", "step_status", "code_origin") if data.get(key)})
    parts.append("SOURCE CONTEXT: " + _json(metadata)[:260])
    text = str(item.get("text", ""))
    chosen = text
    code_selected = False
    if item.get("type") == "implementation_snippet":
        lines = [line for line in text.splitlines() if re.search(r"model\.|\.set\(|\.create\(|public static|return model|^\s*//", line)]
        if lines:
            chosen = "\n".join(lines)
            code_selected = chosen != text
    prefix = "\n\n".join(parts)
    available = max(0, REPRESENTATION_CHARS - len(prefix) - 15)
    excerpt = chosen[:available]
    if len(excerpt) < len(chosen) and "\n" in excerpt:
        excerpt = excerpt.rsplit("\n", 1)[0] or excerpt
    representation = (prefix + "\n\nSOURCE EXCERPT:\n" + excerpt)[:REPRESENTATION_CHARS]
    stats = {"policy": "bounded-object-with-labelled-inferred-annotation-v1", "original_text_characters": len(text),
        "original_data_characters": len(_json(data)), "indexed_characters": len(representation), "body_characters_retained": len(excerpt),
        "truncated": code_selected or excerpt != text, "annotation_included": bool(annotation_fields), "annotation_is_observed": False,
        "annotation_fields": list(annotation_fields), "feature_structure_included": bool(structure), "character_budget": REPRESENTATION_CHARS}
    if case_context is not None:
        from .case_context_index import context_representation
        return context_representation(item, representation, stats, case_context)
    return representation, stats


class LibrarySearch:
    @classmethod
    def for_maintenance(cls, data_dir):
        """Use the build locks without resolving an SDK, model, or runtime."""
        instance = cls.__new__(cls)
        instance.data_dir = Path(data_dir).resolve()
        with _DIRECTORY_LOCKS_GUARD:
            instance.lock = _DIRECTORY_LOCKS.setdefault(os.path.normcase(str(instance.data_dir)), threading.Lock())
        return instance

    def __init__(self, data_dir: Path, *, qmd_dir=None, node=None, embed_model=None, cache_dir=None, preserve_history=False):
        self.data_dir = Path(data_dir).resolve()
        self.active_path = self.data_dir / "active.json"
        self.last_build_path = self.data_dir / "last-build.json"
        self.preserve_history = preserve_history
        self._runtime_options = dict(qmd_dir=qmd_dir, node=node, embed_model=embed_model, cache_dir=cache_dir)
        self.dependencies = retrieval_runtime(self.data_dir, PROJECT_ROOT, qmd_dir=qmd_dir, node=node,
                                              embed_model=embed_model, cache_dir=cache_dir)
        self.qmd_dir = Path(self.dependencies["qmd_dir"] or self.data_dir / "missing-qmd-sdk")
        self.node = self.dependencies["node"]
        self.model_uri = self.dependencies["model_uri"]
        self.cache_dir = Path(self.dependencies["cache_dir"])
        with _DIRECTORY_LOCKS_GUARD:
            self.lock = _DIRECTORY_LOCKS.setdefault(os.path.normcase(str(self.data_dir)), threading.Lock())
        self._model_cached = None
        settings = read_embedding_settings(self.data_dir)
        self._embedding_signature = (settings["enabled"], settings["model_path"])

    def _embedding_enabled(self):
        settings = read_embedding_settings(self.data_dir)
        signature = (settings["enabled"], settings["model_path"])
        if getattr(self, "_embedding_signature", signature) != signature or self.dependencies["embedding_enabled"] != settings["enabled"]:
            self.dependencies = retrieval_runtime(self.data_dir, PROJECT_ROOT, **self._runtime_options)
            self.model_uri = self.dependencies["model_uri"]
            self._model_cached = None
        self._embedding_signature = signature
        return settings["enabled"]

    def _require_embedding(self):
        require_enabled(self.data_dir)
        self._embedding_enabled()

    def _acquire_lease(self, generation):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        handle = (self.data_dir / "build.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("Another process is building this search directory") from exc
        token = uuid.uuid4().hex
        try:
            _write_json(self.data_dir / "build-lease.json", {"pid": os.getpid(), "token": token, "generation": generation, "started_at": time.time()})
        except Exception:
            handle.close()
            raise
        return handle, token

    def _release_lease(self, handle, token):
        try:
            lease_path = self.data_dir / "build-lease.json"
            if json.loads(lease_path.read_text(encoding="utf-8")).get("token") == token:
                lease_path.unlink()
        except (OSError, ValueError):
            pass
        handle.close()  # OS releases the file lock, including after process death.

    @staticmethod
    def _pid_alive(pid):
        if not isinstance(pid, int) or pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5  # Inaccessible is not proof of death.
            try:
                code = wintypes.DWORD()
                return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _env(self):
        value = {"XDG_CACHE_HOME": str(self.cache_dir / "cache"), "XDG_CONFIG_HOME": str(self.cache_dir / "config"),
                "QMD_CONFIG_DIR": str(self.cache_dir / "config/qmd"), "HF_HOME": str(self.cache_dir / "hf"),
                "HF_HUB_CACHE": str(self.cache_dir / "hf/hub"), "QMD_FORCE_CPU": "1", "QMD_LLAMA_GPU": "false",
                "QMD_EMBED_PARALLELISM": "1",
                "GGML_BACKEND_SILENT": "1", "GGML_LOG_LEVEL": "error", "LLAMA_LOG_LEVEL": "error"}
        if self._embedding_enabled() and self.model_uri:
            value["QMD_EMBED_MODEL"] = self.model_uri
        return value

    def _engine(self, request, *, timeout=120, _stop_event=None, _worker_epoch=None):
        check_cancelled()
        if request.get("op") == "pull":
            raise ValueError("Automatic embedding model downloads are disabled; select a local GGUF")
        # Prepare terms for direct bridge callers as well as public search.
        # Existing per-library databases use exactly the same keyword policy.
        if request.get("op") in {"keyword", "shared_keyword"} and "keyword_terms" not in request:
            request = {**request, "keyword_terms": _lex_terms(request.get("query", ""))}
        elif request.get("op") == "batch" and isinstance(request.get("requests"), list):
            request = {**request, "requests": [
                {**item, "keyword_terms": _lex_terms(item.get("query", ""))}
                if isinstance(item, dict) and item.get("op") == "keyword" and "keyword_terms" not in item else item
                for item in request["requests"]]}
        batch = _query_batch_requests(request) if request.get("op") == "batch" else None
        if request.get("op") == "shared_keyword":
            _shared_keyword_collections(request)
        embedding_operation = request.get("op") in {"embed", "vector", "pull", "status"} or bool(
            batch and any(item["op"] == "vector" for item in batch))
        if embedding_operation:
            self._require_embedding()
        if not self.node or not Path(self.node).is_file() or not (self.qmd_dir / "dist/index.js").is_file():
            raise RuntimeError(f"QMD SDK/Node unavailable: node={self.dependencies['node_requested']}; SDK={self.qmd_dir}; "
                               + "; ".join(self.dependencies["errors"]))
        persistent = request.get("op") in {"warmup", "keyword", "shared_keyword"} or bool(
            batch and all(item["op"] == "keyword" for item in batch))
        preparation = worker_preparation(self.data_dir, stop_event=_stop_event, expected_epoch=_worker_epoch) if persistent else nullcontext()
        with preparation:
            runtime = self.data_dir / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            script = runtime / ("qmd_bridge_" + _hash(_NODE_BRIDGE)[:16] + ".mjs")
            if not script.exists():
                temporary = runtime / (uuid.uuid4().hex + ".tmp")
                try:
                    temporary.write_text(_NODE_BRIDGE, encoding="utf-8")
                    check_cancelled()
                    os.replace(temporary, script)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            info = self._model_info() if embedding_operation else {}
            if embedding_operation and (not info.get("available") or not info.get("path") or local_model_path(info["path"]) is None):
                raise RuntimeError("A verified local embedding GGUF is required; no download attempted")
            payload = {"qmd_dir": str(self.qmd_dir), "env": self._env(), "model_uri": info.get("path", self.model_uri),
                       "embedding_enabled": self._embedding_enabled(),
                       "model_cache": str(self.cache_dir / "cache/qmd/models"), **request}
            if embedding_operation:
                payload["model_uri"] = info["path"]
            operation_cwd = Path(request["db_path"]).parent if request.get("db_path") else runtime
            operation_cwd.mkdir(parents=True, exist_ok=True)
            if persistent:
                worker = get_worker(self.data_dir, self.node, script, runtime, payload, stop_event=_stop_event)
        if persistent:
            response = worker.request(payload, timeout=max(5, min(int(timeout), 1800)))
            check_cancelled()
            return _query_batch_results(batch, response) if batch is not None else response
        result = run_text([self.node, str(script)], input_text=_json(payload), cwd=operation_cwd,
                          timeout=max(5, min(int(timeout), 1800)), max_output=4 * 1024 * 1024)
        check_cancelled()
        if result.returncode:
            # No model/context output is persisted. Useful bounded process errors
            # are retained for mode diagnostics, with common secret forms redacted.
            detail = (result.stderr or result.stdout)[-1800:]
            detail = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token)[=:]\s*)\S+", r"\1[redacted]", detail)
            raise RuntimeError(f"QMD {request['op']} failed ({result.returncode}): {detail}")
        marker = result.stdout.rfind(BRIDGE_MARKER)
        if marker < 0:
            raise RuntimeError("QMD bridge returned no structured result")
        response = json.loads(result.stdout[marker + len(BRIDGE_MARKER):].splitlines()[0])
        return _query_batch_results(batch, response) if batch is not None else response

    def _engine_batch(self, requests):
        """Reuse the keyword worker, preserving independent per-library hits."""
        request = {"op": "batch", "requests": requests}
        _query_batch_requests(request)
        response = self._engine(request, timeout=180)
        check_cancelled()
        return _query_batch_results(requests, response)

    def warmup_worker(self, *, stop_event=None, startup_epoch=None):
        """Load the local keyword SDK, without opening an index or embedding."""
        if self.node and Path(self.node).is_file() and (self.qmd_dir / "dist/index.js").is_file():
            epoch = worker_epoch(self.data_dir) if startup_epoch is None else startup_epoch
            return self._engine({"op": "warmup"}, timeout=30, _stop_event=stop_event, _worker_epoch=epoch)
        return {"ready": False}

    def close_worker(self):
        close_workers(self.data_dir)

    def _engine_shared_keyword(self, request):
        _shared_keyword_collections(request)
        rows = self._engine(request, timeout=180)
        check_cancelled()
        if not isinstance(rows, list) or len(rows) > request["limit"]:
            raise RuntimeError("Invalid shared QMD keyword response")
        return rows

    def _model_info(self):
        if not self._embedding_enabled():
            return {"available": False, "enabled": False, "state": "disabled"}
        receipt = self.cache_dir / ("embedding-" + _hash(self.model_uri)[:16] + ".json")
        try:
            local = local_model_path(self.model_uri) if self.model_uri else None
        except ValueError as exc:
            return {"uri": self.model_uri, "available": False, "error": str(exc)}
        if local is not None:
            try:
                stat = local.stat()
                key = (str(local), stat.st_size, stat.st_mtime_ns)
                if self._model_cached and self._model_cached[0] == key:
                    return self._model_cached[1]
                with local.open("rb") as source:
                    if source.read(4) != b"GGUF":
                        raise ValueError("Embedding model is not a GGUF file")
                try:
                    cached = read_embedding_settings(self.data_dir).get("identity") or json.loads(receipt.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    cached = {}
                if (cached.get("uri", self.model_uri) == self.model_uri and cached.get("path") == str(local)
                        and cached.get("size") == stat.st_size and cached.get("mtime_ns") == stat.st_mtime_ns
                        and re.fullmatch(r"[a-f0-9]{64}", str(cached.get("sha256", "")))):
                    digest = cached["sha256"]
                else:
                    digest = _cached_model_hash(local, stat)
                item = {"uri": self.model_uri, "path": str(local), "available": True, "sha256": digest,
                        "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "source": "local-gguf-file",
                        "runtime": "CPU / node-llama-cpp"}
                self._model_cached = (key, item)
                return item
            except (OSError, ValueError) as exc:
                return {"uri": self.model_uri, "path": str(local), "available": False,
                        "error": f"Local embedding GGUF unavailable: {exc}; no download attempted"}
        try:
            item = json.loads(receipt.read_text(encoding="utf-8"))
            path = Path(item["path"])
            # Only a receipt which already exists in a data-bound cache may
            # consult workspace migration anchors. Ordinary runtime diagnostics
            # must not create a data root/anchor or borrow a different workspace.
            workspace_root = (self.data_dir.parent.parent if self.data_dir.name == "search" and self.data_dir.parent.name == "libraries"
                              else Path(self.dependencies["profile"]["data_root"]))
            if self.cache_dir.is_relative_to(workspace_root):
                relocated = WorkspacePaths(workspace_root).resolve(path)
                if relocated.is_relative_to(self.cache_dir):
                    path = relocated
            item["path"] = str(path)
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns)
            if self._model_cached and self._model_cached[0] == key:
                return self._model_cached[1]
            if item.get("size") != stat.st_size or item.get("mtime_ns") != stat.st_mtime_ns:
                item.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns, sha256=_hash_model_file(path))
            item["available"] = True
            self._model_cached = (key, item)
            return item
        except (OSError, ValueError, KeyError, TypeError):
            return {"uri": self.model_uri, "available": False}

    def prepare_embedding(self, *, timeout=900):
        """Use an explicitly enabled local GGUF; never download weights."""
        self._require_embedding()
        check_cancelled()
        current = self._model_info()
        check_cancelled()
        if current.get("sha256"):
            if current.get("source") == "local-gguf-file":
                _write_json(self.cache_dir / ("embedding-" + _hash(self.model_uri)[:16] + ".json"), current)
            return current
        raise RuntimeError(current.get("error") or "No usable local embedding GGUF; no download attempted")

    def dependency_status(self):
        """Availability/provenance only; a model file is never an embedding index."""
        enabled = self._embedding_enabled()
        value = copy.deepcopy(self.dependencies)
        value["qmd_available"] = bool(self.node and Path(self.node).is_file() and (self.qmd_dir / "dist/index.js").is_file())
        value["embedding_model"] = self._model_info() if enabled else {"enabled": False, "available": False, "state": "disabled"}
        value["download_policy"] = "local-only" if enabled else "disabled"
        return value

    def _load_manifest(self, path, *, summary=False, cancellable=True):
        receipt = _QUERY_MANIFEST_RECEIPT.get()
        options = {"receipt": receipt} if receipt is not None else {}
        value = read_manifest(path, summary, cancellable=cancellable, resolve_paths=True, **options)
        if value is not None and "modes" in value:
            previous = value["modes"]
            value["modes"] = {mode: previous.get(mode, {"ready": False, "state": "not_built"}) for mode in MODES}
            value["ready"] = any(item.get("ready") for item in value["modes"].values())
        return value

    def _load_active(self, *, summary=False, cancellable=True):
        return self._load_manifest(self.active_path, summary=summary, cancellable=cancellable)

    @staticmethod
    def _query_file_signatures(paths):
        signatures = {}
        for value in paths:
            try:
                info = Path(value).lstat()
                identity = (info.st_dev, info.st_ino, info.st_mode,
                            getattr(info, "st_file_attributes", 0) & getattr(stat_types, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
                            getattr(info, "st_reparse_tag", None))
                # Creating policy/cache files changes a directory's timestamps
                # without changing its ownership or its index artifacts.
                signatures[value] = identity if stat_types.S_ISDIR(info.st_mode) else (
                    *identity, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            except OSError:
                signatures[value] = None
        return signatures

    @staticmethod
    def _query_receipt_matches(signatures, receipt):
        # File content identities come from the read itself. Sampling a new
        # authority stamp after status could otherwise bless an older state.
        for filename, identity in receipt.items():
            current = signatures.get(filename)
            if identity is None or current is None or (current[0], current[1], *current[-3:]) != identity:
                return False
        return True

    def _query_guard_paths(self, state):
        paths = {self.active_path, self.last_build_path, self.data_dir / "build-lease.json"}
        navigation = self.data_dir / "runtime" / "case-navigation.sqlite3"
        paths.update(Path(str(navigation) + suffix) for suffix in ("", "-wal", "-journal"))
        contexts = case_context_store.context_path(self.data_dir)
        paths.update(Path(str(contexts) + suffix) for suffix in ("", "-wal", "-journal"))
        paths.add(case_context_store.pending_path(self.data_dir))
        settings_root = self.data_dir.parent if self.data_dir.name == "search" else self.data_dir
        if settings_root.name == "libraries":
            settings_root = settings_root.parent
        paths.add(settings_root / "retrieval-settings.json")
        for manifest in (state, state.get("last_build", {})):
            if manifest.get("directory"):
                directory = Path(manifest["directory"])
                paths.update((directory / "manifest.json", directory / "objects.sqlite3"))
            for item in manifest.get("libraries", {}).values():
                paths.update(Path(item[key]) for key in ("db_path", "vector_db_path") if item.get(key))
        paths.update((self.qmd_dir / "dist/index.js", self.qmd_dir / "package.json"))
        if self.node:
            paths.add(Path(self.node))
        model = state.get("dependencies", {}).get("embedding_model", {})
        if model.get("path"):
            paths.add(Path(model["path"]))
        # A moved directory replaced by a link must invalidate a prepared query
        # even when it still points to the very same underlying database file.
        paths.update(parent for path in tuple(paths) for parent in path.parents)
        return {str(path) for path in paths}

    def query_snapshot(self):
        """Validate once for provider readiness/generation checks and search.

        This holds no lifecycle or holdout authorization. Search reads those
        policies afresh, and rejects snapshots changed since preparation.
        """
        before = self._query_file_signatures((str(self.active_path), str(self.last_build_path)))
        receipt = {}
        token = _QUERY_MANIFEST_RECEIPT.set(receipt)
        try:
            state = self.status(summary=True)
        finally:
            _QUERY_MANIFEST_RECEIPT.reset(token)
        if state.get("modes", {}).get("qmd_vector", {}).get("ready"):
            model = self._model_info()
            previous = state.get("dependencies", {}).get("embedding_model", {})
            if any(model.get(key) != previous.get(key) for key in ("available", "sha256", "path", "size", "mtime_ns")):
                raise RuntimeError("Embedding model changed during query preparation; retry the query")
            if model.get("path"):
                try:
                    path = Path(model["path"]).absolute()
                    info = path.stat()
                    receipt[str(path)] = (info.st_dev, info.st_ino, model.get("size", info.st_size),
                                          model.get("mtime_ns", info.st_mtime_ns), info.st_ctime_ns)
                except OSError as exc:
                    raise RuntimeError("Embedding model changed during query preparation; retry the query") from exc
        signatures = self._query_file_signatures(self._query_guard_paths(state) | receipt.keys())
        if (any(signatures[path] != identity for path, identity in before.items())
                or not self._query_receipt_matches(signatures, receipt)):
            raise RuntimeError("Library index changed during query preparation; retry the query")
        return _QuerySnapshot(self, state, signatures, receipt)

    @staticmethod
    def _same_embedding_model(vector, model):
        # A file locator is not model identity. A relocated, available GGUF with
        # the exact original SHA can query existing vectors without rewriting
        # their original QMD model/fingerprint records.
        sha = vector.get("model_sha256")
        return bool(model.get("available") and re.fullmatch(r"[a-f0-9]{64}", str(sha or "")) and sha == model.get("sha256"))

    @staticmethod
    def _index_model_uri(vector, library):
        if vector.get("embedding_model_uri"):
            return vector["embedding_model_uri"]
        rows = vector.get("embedding_records", {}).get(library, {}).get("embedding_rows", [])
        identities = {row.get("model") for row in rows if isinstance(row.get("model"), str) and row["model"]}
        # Older manifests already include the stored model identity returned by
        # QMD. Keep it when unambiguous rather than replacing it with a moved
        # file path or an HF receipt alias.
        return next(iter(identities)) if len(identities) == 1 else vector.get("model_uri")

    def status(self, *, summary=False):
        embedding_enabled = self._embedding_enabled()
        active = self._load_active(summary=summary)
        if not active:
            result = {"ready": False, "generation": None, "modes": {mode: {"ready": False, "state": "not_built"} for mode in MODES}}
        else:
            result = active if summary else copy.deepcopy(active)
            model = self._model_info() if embedding_enabled else {}
            vector = result["modes"].get("qmd_vector", {})
            progress = {}
            for library, item in result.get("libraries", {}).items() if embedding_enabled else []:
                try:
                    progress[library] = json.loads(Path(item.get("vector_db_path", item["db_path"])).with_suffix(".progress.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            if progress:
                vector["progress"] = progress
            if embedding_enabled and vector.get("ready") and not self._same_embedding_model(vector, model):
                vector.update(ready=False, state="stale", error="Embedding model fingerprint changed or is unavailable")
            for mode, item in result["modes"].items():
                if mode == "qmd_vector" and not embedding_enabled:
                    continue
                paths = [Path(result["directory"]) / "objects.sqlite3", *(
                    Path(library["vector_db_path" if mode == "qmd_vector" and "vector_db_path" in library else "db_path"])
                    for library in result.get("libraries", {}).values())]
                if item.get("ready") and (not paths or any(not path.is_file() for path in set(paths))):
                    item.update(ready=False, state="unavailable", error="Index files are missing from the current search root; the former root is not used")
            if result.get("bridge_version") not in READABLE_BRIDGE_VERSIONS:
                for item in result["modes"].values():
                    if item.get("ready"):
                        item.update(ready=False, state="stale", error="Retrieval representation/bridge version changed; rebuild required")
            result["ready"] = any(item.get("ready") for item in result["modes"].values())
        try:
            last_build = self._load_manifest(self.last_build_path, summary=summary) or {}
            result["last_build"] = last_build if summary else copy.deepcopy(last_build)
            if result["last_build"].get("state") == "stale" and result.get("generation") == result["last_build"].get("generation"):
                for item in result["modes"].values():
                    if item.get("ready"):
                        item.update(ready=False, state="stale", error=result["last_build"]["error"])
                result["ready"] = False
        except (OSError, ValueError):
            pass
        try:
            lease = json.loads((self.data_dir / "build-lease.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            lease = None
        result["building"] = self.lock.locked() or bool(lease and self._pid_alive(lease.get("pid")))
        result["lease"] = lease
        if not result["building"]:
            for item in result.get("modes", {}).values():
                if item.get("state") == "building":
                    item.update(ready=False, state="interrupted", error="Build owner exited before this mode completed; completed modes remain usable")
        if not embedding_enabled:
            result["modes"]["qmd_vector"] = {"ready": False, "state": "disabled", "enabled": False}
            result["ready"] = any(item.get("ready") for item in result["modes"].values())
        result["dependencies"] = self.dependency_status()
        return result

    def _build_keyword(self, inputs_path, output, libraries, *, on_progress=None):
        from .qmd_shared import build_keyword_index
        return build_keyword_index(inputs_path, output, qmd_dir=self.qmd_dir, node=self.node,
                                   libraries=libraries, env=self._env(), on_progress=on_progress)

    def build(self, objects, *, modes: list[str], generation: str, on_progress=None) -> dict:
        check_cancelled()
        if on_progress is not None and not callable(on_progress):
            raise ValueError("on_progress must be callable")

        def progress(stage, completed, total, unit, detail):
            check_cancelled()
            if on_progress is not None:
                on_progress({"stage": stage, "progress": {"completed": completed, "total": total,
                    "unit": unit, "current_file": None, "detail": detail}})
            check_cancelled()

        def cleanup_progress(info):
            progress("cleaning_corpus", info["completed"], info["total"], "files",
                     "Removing temporary keyword export files")

        if not isinstance(generation, str) or not generation or len(generation) > 256:
            raise ValueError("generation must be a nonempty bounded string")
        modes = normalize_modes(modes)
        if "qmd_vector" in modes:
            self._require_embedding()
        def object_id(item):
            if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key] for key in ("id", "revision_id", "library_id", "title")):
                raise ValueError("Each object requires id, revision_id, library_id and title")
            return item["id"]

        if isinstance(objects, list):
            # Sorting owns only the pointer list. Authoritative nested payloads
            # are never copied or changed during indexing.
            objects = sorted(objects, key=object_id)
        else:
            from .library_store import _CatalogObjectStream
            if not isinstance(objects, _CatalogObjectStream):
                raise ValueError("objects must be a nonempty list or catalog snapshot stream")
        object_count = len(objects)
        if not object_count:
            raise ValueError("objects must be a nonempty list or catalog snapshot stream")
        library_totals, first_titles = {}, {}

        def validated_objects():
            previous, count = None, 0
            with _object_cursor(objects) as rows:
                for item in rows:
                    check_cancelled()
                    identity = object_id(item)
                    if previous is not None and identity <= previous:
                        raise ValueError("Object reference IDs must be unique and ordered")
                    previous, count = identity, count + 1
                    library = item["library_id"]
                    library_totals[library] = library_totals.get(library, 0) + 1
                    first_titles.setdefault(library, item["title"])
                    yield item
            if count != object_count:
                raise ValueError("Catalog snapshot object count does not match its active identities")

        # Validation, canonical hash and small per-library metadata share one
        # cursor pass. The second pass exports source objects and retrieval text.
        fingerprint = _corpus_hash(validated_objects())
        check_cancelled()
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("Another library index build is active")
        try:
            lease_handle, lease_token = self._acquire_lease(generation)
        except Exception:
            self.lock.release()
            raise
        started = time.monotonic()
        directory = manifest = None
        keyword_resources = ExitStack()
        try:
            check_cancelled()
            old = self._load_active(summary=True)
            if old and old["generation"] == generation and old["corpus_hash"] != fingerprint:
                state = {"ready": False, "state": "stale", "generation": generation, "desired_corpus_hash": fingerprint, "error": "Generation reused with different object content; choose a new generation"}
                _write_json(self.last_build_path, serialize_index_manifest(state, self.data_dir))
                return state
            if old and old["generation"] == generation and old["corpus_hash"] == fingerprint:
                current = self.status()
                shared_keyword = "qmd_keyword" not in modes or old.get("keyword_storage") == "shared-collections-v1"
                if shared_keyword and all(current["modes"].get(mode, {}).get("ready") for mode in modes):
                    # status() sees this request's lease; it must not make an
                    # unrequested, previously interrupted mode look active.
                    for mode, item in current["modes"].items():
                        if mode not in modes and item.get("state") == "building":
                            item.update(ready=False, state="interrupted", error="This request did not resume the unfinished mode")
                    for mode in modes:
                        progress("indexing_" + ("keyword" if mode == "qmd_keyword" else "vector"),
                                 0, 0, "indexes", "Generation already built; no work repeated")
                    cleanup = self._cleanup_exported_corpus(current, on_progress=cleanup_progress)
                    if cleanup is not None:
                        # Retry a cancelled cleanup using the small authority;
                        # status also includes transient UI/runtime fields.
                        authority = self._load_active()
                        authority["corpus_cleanup"] = current["corpus_cleanup"] = cleanup
                        self._publish(Path(authority["directory"]), authority)
                    current = dict(current, unchanged=True, building=False, lease=None)
                    self._prune_history(current)
                    return current
            name = _hash(generation + fingerprint)[:20] + "-" + uuid.uuid4().hex[:8]
            directory = self.data_dir / "generations" / name
            directory.mkdir(parents=True, exist_ok=False)
            db_path = directory / "objects.sqlite3"
            libraries = {}
            mapping = {}
            representations = {}
            keyword_inputs = None
            if "qmd_keyword" in modes:
                from .qmd_shared import keyword_input_spool
                keyword_inputs = keyword_resources.enter_context(keyword_input_spool(directory))
            progress("indexing_objects", 0, object_count, "objects", "Building object lookup and source representations")
            library_exported = {}
            catalog_path = self.data_dir.parent / "catalog.sqlite3"
            managed_sources = (getattr(objects, "path", None) is not None
                               and Path(objects.path).resolve() == catalog_path.resolve())
            from .case_context_index import profiles_for_build, context_title
            context_profiles = profiles_for_build(self.data_dir) if managed_sources else {}
            used_contexts = {}
            self._build_case_contexts = used_contexts
            self._context_publish_key = None
            indexed_characters = truncated_objects = 0
            with closing(sqlite3.connect(db_path)) as db, db, _object_cursor(objects) as source_objects:
                object_index.create_schema(db)
                for completed, item in enumerate(source_objects, 1):
                    check_cancelled()
                    library = item["library_id"]
                    if library not in library_exported:
                        progress("indexing_objects", completed - 1, object_count, "objects", f"Starting library {library}")
                    context = context_profiles.get(item["id"]) if item.get("type") == "model_case" else None
                    text, stats = _representation(item, context)
                    if context is not None:
                        used_contexts[item["id"]] = context
                    check_cancelled()
                    if not managed_sources:
                        representations[item["id"]] = stats
                    indexed_characters += stats["indexed_characters"]
                    truncated_objects += int(stats["truncated"])
                    object_index.insert_object(db, item, text, stats, "", standalone=not managed_sources)
                    if keyword_inputs is not None:
                        keyword_inputs.add(item["library_id"], object_index.qmd_path(item), context_title(item, context), text)
                    lib = "lib_" + _hash(item["library_id"])[:20]
                    corpus = directory / lib / "corpus"
                    filename = "o-" + _hash(item["id"])[:32] + ".md"
                    if "qmd_vector" in modes:
                        corpus.mkdir(parents=True, exist_ok=True)
                        (corpus / filename).write_text("# " + item["title"] + "\n\n" + text, encoding="utf-8")
                    if not managed_sources:
                        mapping[lib + "/" + filename] = item["id"]
                    libraries[item["library_id"]] = {"key": lib, "collection": lib, "corpus": str(corpus), "db_path": str(directory / "qmd-keyword.sqlite3"), "vector_db_path": str(directory / lib / "qmd-vector.sqlite3")}
                    library_exported[library] = library_exported.get(library, 0) + 1
                    if completed % 25 == 0 or library_exported[library] == library_totals[library]:
                        progress("indexing_objects", completed, object_count, "objects", f"Exported library {library}" if library_exported[library] == library_totals[library] else f"Exporting library {library}")
                # Raising here rolls back all inserts in this staging database.
                check_cancelled()
            check_cancelled()
            if library_exported != library_totals:
                raise ValueError("Catalog snapshot changed during source export")
            if keyword_inputs is not None:
                keyword_inputs.seal()
            manifest = {"ready": False, "generation": generation, "corpus_hash": fingerprint, "directory": str(directory), "object_count": object_count,
                        "libraries": libraries, "mapping": mapping, "modes": {mode: {"ready": False, "state": "building" if mode in modes else "not_requested"} for mode in MODES}, "bridge_version": BRIDGE_VERSION,
                        "keyword_storage": "shared-collections-v1", "object_lookup": {"schema": object_index.LOOKUP_SCHEMA},
                        "representation": {"object_count": object_count, "indexed_objects": object_count, "unindexed_objects": 0,
                            "truncated_objects": truncated_objects, "indexed_characters": indexed_characters,
                            "character_budget_per_object": REPRESENTATION_CHARS, "objects": representations}}
            qmd_modes = set(modes)
            lexical_ok = True
            if "qmd_keyword" in modes:
                keyword_started = time.monotonic()
                keyword_completed = 0
                progress("indexing_keyword", 0, len(libraries), "indexes", "Preparing keyword indexes")
                try:
                    offsets, offset = {}, 0
                    for library in sorted(libraries):
                        offsets[library] = offset
                        offset += library_totals[library]
                    def keyword_progress(info):
                        library = info.get("library_id")
                        completed = offsets.get(library, 0) + info.get("completed", 0)
                        detail = info.get("detail") or f"Building shared QMD collection {library}"
                        progress("indexing_keyword", completed, object_count, "objects", detail)
                    self._build_keyword(keyword_inputs.path, directory / "qmd-keyword.sqlite3", libraries,
                                        on_progress=keyword_progress)
                    check_cancelled()
                    keyword_completed = len(libraries)
                    manifest["modes"]["qmd_keyword"] = {"ready": True, "state": "ready", "backend": "qmd-shared-fts", "query_policy": KEYWORD_QUERY_POLICY, "qmd_version": self._qmd_version(), "build_seconds": round(time.monotonic() - keyword_started, 2)}
                    self._publish(directory, manifest)
                    progress("indexing_keyword", keyword_completed, len(libraries), "indexes", "Keyword phase completed")
                except BuildCancelled:
                    raise
                except Exception as exc:
                    lexical_ok = False
                    for mode in qmd_modes:
                        manifest["modes"][mode] = {"ready": False, "state": "failed", "error": str(exc)[:2000]}
                    progress("indexing_keyword", keyword_completed, len(libraries), "indexes", "Keyword phase failed: " + str(exc)[:400])
                finally:
                    # Embedding reads its own corpus/database; exported keyword
                    # text must disappear as soon as this phase finishes.
                    keyword_resources.close()
            if "qmd_vector" in modes and lexical_ok:
                vector_started = time.monotonic()
                vector_completed = 0
                progress("indexing_vector", None, None, "chunks", "Preparing embedding model; chunk total is not yet known")
                try:
                    model = self.prepare_embedding(timeout=900)
                    check_cancelled()
                    embedding_records = {}
                    for library, item in libraries.items():
                        check_cancelled()
                        progress("indexing_vector", vector_completed, len(libraries), "indexes", f"Embedding library {library}; live chunk progress is in its progress file")
                        # Vector work has its own DB; published keyword queries
                        # never wait on embedding writes or model initialization.
                        previous = old.get("libraries", {}).get(library) if old and old.get("bridge_version") == BRIDGE_VERSION else None
                        old_vector = old.get("modes", {}).get("qmd_vector", {}) if old else {}
                        old_vector_path = previous.get("vector_db_path", previous["db_path"]) if previous else None
                        reuse_vectors = previous and old_vector.get("ready") and old_vector.get("model_uri") == self.model_uri and self._same_embedding_model(old_vector, model) and Path(old_vector_path).is_file()
                        Path(item["vector_db_path"]).parent.mkdir(parents=True, exist_ok=True)
                        if reuse_vectors:
                            with closing(sqlite3.connect(Path(old_vector_path).as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(item["vector_db_path"])) as target:
                                source.backup(target, pages=128, progress=lambda *_: check_cancelled())
                            item["vector_reused_from_generation"] = old["generation"]
                        self._engine({"op": "update", **item, "db_path": item["vector_db_path"]}, timeout=240)
                        result = self._engine({"op": "embed", **item, "db_path": item["vector_db_path"], "progress_path": str(Path(item["vector_db_path"]).with_suffix(".progress.json"))}, timeout=1800)
                        check_cancelled()
                        if result["state"]["needsEmbedding"] != 0 or not result["state"]["hasVectorIndex"] or not result["embedding_rows"]:
                            raise RuntimeError("QMD embeddings are incomplete or no actual vectors were created")
                        probe = self._engine({"op": "vector", **item, "db_path": item["vector_db_path"], "query": first_titles[library], "limit": 1}, timeout=180)
                        check_cancelled()
                        if not probe or probe[0].get("source") != "vec":
                            raise RuntimeError("Actual QMD vector probe returned no vector hit")
                        embedding_records[library] = dict(result, probe_source="vec")
                        vector_completed += 1
                        progress("indexing_vector", vector_completed, len(libraries), "indexes", f"Completed actual vector index and probe for {library}")
                    manifest["modes"]["qmd_vector"] = {"ready": True, "state": "ready", "backend": "qmd-searchVector", "model_uri": self.model_uri,
                        "embedding_model_uri": model.get("path", self.model_uri),
                        "model_sha256": model["sha256"], "model_size": model["size"], "qmd_version": self._qmd_version(), "embedding_records": embedding_records, "query_policy": "original-query-no-LLM-expansion", "build_seconds": round(time.monotonic() - vector_started, 2)}
                    progress("indexing_vector", vector_completed, len(libraries), "indexes", "Vector phase completed")
                except BuildCancelled:
                    raise
                except Exception as exc:
                    manifest["modes"]["qmd_vector"] = {"ready": False, "state": "failed", "error": str(exc)[:2000], "model_uri": self.model_uri}
                    progress("indexing_vector", vector_completed, len(libraries), "indexes", "Vector phase failed: " + str(exc)[:400])
            manifest["ready"] = any(item.get("ready") for item in manifest["modes"].values())
            manifest["build_seconds"] = round(time.monotonic() - started, 2)
            self._publish(directory, manifest)
            if all(manifest["modes"][mode].get("ready") for mode in modes):
                cleanup = self._cleanup_exported_corpus(manifest, on_progress=cleanup_progress)
                if cleanup is not None:
                    manifest["corpus_cleanup"] = cleanup
                    self._publish(directory, manifest)
            if not self.preserve_history and all(manifest["modes"][mode].get("ready") for mode in modes):
                check_cancelled()
                self._prune_history(manifest)
            return manifest
        except BuildCancelled:
            self._record_cancelled(directory, manifest, generation, modes, started)
            raise
        finally:
            try:
                keyword_resources.close()
            finally:
                try:
                    self._release_lease(lease_handle, lease_token)
                finally:
                    self.lock.release()

    def _prune_history(self, manifest):
        if self.preserve_history:
            return
        from .index_retention import POLICY, prune_index_generations
        try:
            manifest["retention"] = prune_index_generations(self.data_dir, current_directory=manifest["directory"])
        except Exception as exc:
            manifest["retention"] = {"policy": POLICY, "state": "failed", "removed_generations": None,
                                     "removed_bytes": None, "warnings": ["Index retention may be incomplete: " + str(exc)[:300]]}

    def _cleanup_exported_corpus(self, manifest, *, on_progress=None):
        """Finish temporary exports after durable QMD publication, including retries."""
        if (not manifest.get("object_lookup")
                or not any(manifest.get("modes", {}).get(mode, {}).get("ready") for mode in ("qmd_keyword", "qmd_vector"))
                or not any(Path(item["corpus"]).is_dir() for item in manifest.get("libraries", {}).values())):
            return None
        from .index_corpus import cleanup_generated_corpus
        return cleanup_generated_corpus(self.data_dir, manifest, on_progress=on_progress)

    def _record_cancelled(self, directory, manifest, generation, modes, started):
        """Record interruption without publishing any uncommitted mode as ready."""
        active = self._load_active(cancellable=False)
        published = bool(directory and active and active.get("directory") == str(directory))
        if published:
            value = dict(active, modes=copy.deepcopy(active["modes"]))
        else:
            value = dict(manifest, modes=copy.deepcopy(manifest["modes"])) if manifest else {"generation": generation, "directory": str(directory) if directory else None,
                "modes": {mode: {"ready": False, "state": "building" if mode in modes else "not_requested"} for mode in MODES}}
        for mode, state in value["modes"].items():
            # Only the ready modes in the published manifest have passed their
            # publication boundary. In-memory ready flags are not sufficient.
            if mode in modes and not (published and state.get("ready")):
                state.update(ready=False, state="cancelled", error="Build cancelled; partial files retained without a resume guarantee")
        value.update(state="cancelled", cancelled=True, ready=any(item.get("ready") for item in value["modes"].values()),
                     build_seconds=round(time.monotonic() - started, 2))
        # Cancellation is latched; bookkeeping deliberately bypasses checks.
        # Existing active generations are untouched until this build has
        # actually published a usable mode.
        if directory:
            publish_manifest(directory, value, self.data_dir, write_json=_write_json, cancellable=False,
                             active=published, last_build=True)
        else:
            _write_json(self.last_build_path, _serialize_manifest(value, self.data_dir), cancellable=False)

    def _publish(self, directory, manifest):
        check_cancelled()
        manifest["ready"] = any(item.get("ready") for item in manifest["modes"].values())
        if manifest["ready"] and (self.data_dir / "runtime/case-contexts.sqlite3").exists():
            from .case_context_index import rebind_for_publish
            profiles = getattr(self, "_build_case_contexts", {})
            key = (str(directory), tuple((identity, profile["content_hash"]) for identity, profile in sorted(profiles.items())))
            if getattr(self, "_context_publish_key", None) != key:
                manifest.update(rebind_for_publish(self.data_dir, manifest, profiles))
                self._context_publish_key = key
        if manifest["ready"]:
            previous = self._load_active(summary=True)
            if previous and previous.get("directory") != str(directory):
                from .index_retention import retire_generation
                warning = retire_generation(self.data_dir, previous["directory"])
                if warning:
                    manifest.setdefault("retention_warnings", []).append(warning)
        publish_manifest(directory, manifest, self.data_dir, write_json=_write_json,
                         active=manifest["ready"], last_build=True)
        if manifest["ready"] and manifest.get("case_context_dependency_index"):
            from .case_context_index import _prune_dependencies
            _prune_dependencies(self.data_dir, manifest)

    def _qmd_version(self):
        try:
            return json.loads((self.qmd_dir / "package.json").read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError):
            return "unavailable"

    def _holdout_database(self, policy, db, active, *, compact):
        """Cache only exclusion identities/locators; never alter an index.

        QMD receives a filename, rather than thousands of IDs or an allowlist
        of the corpus. Citation-to-path expansion uses primary-key probes once
        per policy/generation, including historical manifest path mappings.
        """
        if not (policy.citation_ids or policy.object_ids or policy.qmd_paths or policy.case_groups):
            return None
        token = _hash(_json([_HOLDOUT_SCHEMA, policy.token, active["directory"], active["corpus_hash"],
                            query_scope_counts.database_signature(Path(active["directory"]) / "objects.sqlite3")]))
        runtime = self.data_dir / "runtime"
        target = runtime / ("evaluation-exclusions-" + token + ".sqlite3")
        with _HOLDOUT_CACHE_LOCK:
            if target.is_file():
                return target
            runtime.mkdir(parents=True, exist_ok=True)
            stage = runtime / (uuid.uuid4().hex + ".tmp")
            identities = policy.citation_ids | policy.object_ids
            try:
                with closing(sqlite3.connect(stage)) as cache, cache:
                    cache.execute("CREATE TABLE identities(id TEXT PRIMARY KEY) WITHOUT ROWID")
                    cache.execute("CREATE TABLE paths(path TEXT PRIMARY KEY) WITHOUT ROWID")
                    cache.execute("CREATE TABLE case_groups(case_group TEXT PRIMARY KEY) WITHOUT ROWID")
                    cache.execute("CREATE TABLE excluded_counts(library_id TEXT PRIMARY KEY,object_count INTEGER NOT NULL) WITHOUT ROWID")
                    cache.executemany("INSERT OR IGNORE INTO identities VALUES(?)", ((value,) for value in identities))
                    cache.executemany("INSERT OR IGNORE INTO paths VALUES(?)", ((value,) for value in policy.qmd_paths))
                    cache.executemany("INSERT OR IGNORE INTO case_groups VALUES(?)", ((value,) for value in policy.case_groups))
                    if compact:
                        blocked = {}
                        ids = tuple(identities)
                        for offset in range(0, len(ids), 400):
                            batch = ids[offset:offset + 400]
                            rows = db.execute("SELECT id,library_id,qmd_path FROM objects WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch).fetchall()
                            cache.executemany("INSERT OR IGNORE INTO paths VALUES(?)", ((row[2],) for row in rows))
                            blocked.update((row[0], row[1]) for row in rows)
                        paths = tuple(policy.qmd_paths)
                        for offset in range(0, len(paths), 400):
                            batch = paths[offset:offset + 400]
                            rows = db.execute("SELECT id,library_id FROM objects WHERE qmd_path IN (" + ",".join("?" for _ in batch) + ")", batch)
                            blocked.update(rows)
                    else:
                        mapping = active["mapping"] if "mapping" in active else self._load_active()["mapping"]
                        for path, sid in mapping.items():
                            if sid in identities or path in policy.qmd_paths:
                                cache.execute("INSERT OR IGNORE INTO paths VALUES(?)", (path,))
                                cache.execute("INSERT OR IGNORE INTO identities VALUES(?)", (sid,))
                        blocked = {}
                        ids = tuple(row[0] for row in cache.execute("SELECT id FROM identities"))
                        for offset in range(0, len(ids), 400):
                            batch = ids[offset:offset + 400]
                            blocked.update(db.execute("SELECT id,library_id FROM objects WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch))
                    excluded_counts = {}
                    for library in blocked.values():
                        excluded_counts[library] = excluded_counts.get(library, 0) + 1
                    cache.executemany("INSERT INTO excluded_counts VALUES(?,?)", excluded_counts.items())
                check_cancelled()
                os.replace(stage, target)
            finally:
                if stage.exists():
                    stage.unlink()
        return target

    def _searchable_library_ids(self, requested=None):
        """Read lifecycle flags without constructing/mutating LibraryStore.

        None means a standalone historical index with no catalog. An existing
        malformed/disappearing catalog must never become an enable-all fallback.
        """
        catalog = self.data_dir.parent / "catalog.sqlite3"
        if not catalog.exists():
            return None
        if not catalog.is_file() or not catalog.resolve().is_relative_to(self.data_dir.parent):
            raise LibraryVisibilityError("Library lifecycle catalog is not an owned regular database")
        try:
            with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                db.execute("PRAGMA query_only=ON")
                rows = db.execute("SELECT id,body FROM libraries").fetchall()
            visible = set()
            for library_id, raw in rows:
                body = json.loads(raw)
                if not isinstance(body, dict) or body.get("id") != library_id:
                    raise LibraryVisibilityError("Library lifecycle catalog has an invalid library body/id")
                enabled, deleted = body.get("enabled", True), body.get("deleted_at")
                if not isinstance(enabled, bool):
                    raise LibraryVisibilityError("Library lifecycle enabled must be a boolean")
                if deleted is not None and (not isinstance(deleted, str) or not deleted.strip()):
                    raise LibraryVisibilityError("Library lifecycle deleted_at must be null or a nonempty timestamp")
                if enabled and deleted is None:
                    visible.add(library_id)
            return visible if requested is None else visible & set(requested)
        except LibraryVisibilityError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise LibraryVisibilityError("Library lifecycle catalog is unreadable or invalid") from exc

    def search(self, query, *, mode="qmd_keyword", library_ids=None, limit=8, feature_branch=None, kind="all", candidate_limit=None, snapshot=None, retrieval_view="knowledge", case_id=None, stage=None, include_geometry_only=False):
        check_cancelled()
        case_context_store.assert_no_pending(self.data_dir)
        if not isinstance(include_geometry_only, bool):
            raise ValueError("include_geometry_only must be a boolean")
        mode = normalize_mode(mode)
        navigation_requested = _navigation_options(retrieval_view, case_id, stage)
        if navigation_requested and mode != "qmd_keyword":
            raise ValueError("Case navigation search requires qmd_keyword; no embedding is performed")
        if mode == "qmd_vector":
            self._require_embedding()
        feature_branch = normalize_feature_branch(feature_branch)
        if not isinstance(kind, str) or not kind.strip() or len(kind) > 100:
            raise ValueError("kind must be a nonempty bounded string")
        kind = kind.strip().lower()
        filtered = feature_branch is not None or kind != "all" or navigation_requested
        self.last_search_scope = {"mode": mode, "feature_branch": feature_branch, "kind": kind,
            "filter_stage": "before_retrieval" if filtered else "library_before_retrieval", "eligible_objects": 0, "libraries": {},
            "adapter": KEYWORD_ADAPTER_VERSION if mode == "qmd_keyword" else FILTER_ADAPTER_VERSION if filtered else "qmd-sdk-collection",
            "strategy": ("keyword-fts-where" if mode == "qmd_keyword" else "exact-allowed-vector-cosine") if filtered else "library-collection"}
        if not isinstance(query, str) or not query.strip():
            return []
        if isinstance(library_ids, str):
            library_ids = [library_ids]
        if library_ids is not None and (not isinstance(library_ids, list) or any(not isinstance(value, str) or not value for value in library_ids)):
            raise ValueError("library_ids must be a list of nonempty strings")
        # Context dependencies may belong to enabled manuals outside the model
        # libraries requested by this query. Validate the global lifecycle map.
        visible_global_at_start = self._searchable_library_ids()
        visible_at_start = (visible_global_at_start if library_ids is None or visible_global_at_start is None else
                            visible_global_at_start & set(library_ids))
        if visible_at_start is not None and not visible_at_start:
            return []
        policy = exclusion_snapshot(self.data_dir.parent)
        if snapshot is None:
            state = self.status(summary=True)
        elif isinstance(snapshot, _QuerySnapshot):
            state = snapshot.consume(self)
        else:
            raise ValueError("snapshot must come from this engine's query_snapshot()")
        if not state.get("modes", {}).get(mode, {}).get("ready"):
            raise RuntimeError(f"{mode} is not ready: " + str(state.get("modes", {}).get(mode, {}).get("error", "index not built")))
        # Status already carries the resolved, validated active manifest.
        active = {key: value for key, value in state.items() if key not in {"last_build", "building", "lease", "dependencies"}}
        contexts = case_context_store.validated_contexts(self.data_dir, active)
        context_signature = (query_scope_counts.database_signature(contexts["path"], change_counter=True)
                             if contexts is not None else None)
        if contexts is not None:
            if visible_global_at_start is None:
                raise LibraryVisibilityError("Case context dependencies require the live library lifecycle catalog")
            filtered = True
            self.last_search_scope.update(case_context_dependencies="before_retrieval", filter_stage="before_retrieval")
            if retrieval_view == "case_overviews":
                self.last_search_scope["include_geometry_only"] = include_geometry_only
        navigation, navigation_signature = None, None
        if navigation_requested:
            from .case_navigation import case_scope, validated_navigation
            navigation = (case_scope(self.data_dir, active, case_id) if case_id is not None else
                          validated_navigation(self.data_dir, active))
            navigation_signature = query_scope_counts.database_signature(navigation["path"], change_counter=True)
            if case_id is not None:
                owner_visibility = self._searchable_library_ids([navigation["library_id"]])
                if (policy.blocks_identity(case_id) or navigation["case_group"] in policy.case_groups
                        or owner_visibility is not None and navigation["library_id"] not in owner_visibility):
                    raise PermissionError("Selected case is unavailable or evaluation-excluded")
            self.last_search_scope.update(retrieval_view=retrieval_view, case_id=case_id, stage=stage,
                                          navigation_scope="prepared_case" if case_id is not None else "all_active_case_overviews")
        shared_keyword = mode == "qmd_keyword" and active.get("keyword_storage") == SHARED_KEYWORD_STORAGE
        if shared_keyword:
            self.last_search_scope.update(adapter=KEYWORD_ADAPTER_VERSION, strategy="shared-collection-fts-where")
        requested = set(active["libraries"]) if library_ids is None else set(library_ids)
        if visible_at_start is not None:
            requested &= visible_at_start
        selected = {key: value for key, value in active["libraries"].items() if key in requested}
        if not selected:
            return []
        limit = max(1, min(int(limit), 100))
        # Keep the candidate-count API while hydrating only the requested
        # prefix. Keyword scores come from one OR query and one BM25 pass.
        engine_limit = limit if candidate_limit is None else max(limit, min(int(candidate_limit), 100))
        hits = []
        keyword_terms = _lex_terms(query) if mode == "qmd_keyword" else None
        if mode == "qmd_keyword" and not keyword_terms:
            return []
        directory = Path(active["directory"])
        with closing(sqlite3.connect((directory / "objects.sqlite3").as_uri() + "?mode=ro", uri=True)) as db, ExitStack() as resources:
            compact = object_index.is_compact(db)
            legacy = None
            if not compact:
                legacy = self._load_active()
                if not legacy or any(legacy.get(key) != active.get(key) for key in ("generation", "directory", "corpus_hash")):
                    raise RuntimeError("Library index changed during query preparation; retry the query")
            if navigation is not None:
                db.execute("ATTACH DATABASE ? AS case_navigation", (Path(navigation["path"]).as_uri() + "?mode=ro",))
            holdout_path = self._holdout_database(policy, db, legacy or active, compact=compact)
            holdout_where = ""
            if holdout_path:
                db.execute("ATTACH DATABASE ? AS evaluation_holdout", (holdout_path.as_uri() + "?mode=ro",))
                holdout_where = " AND " + _holdout_clause(compact=compact)
                self.last_search_scope["evaluation_exclusions"] = "before_retrieval"
                self.last_search_scope["filter_stage"] = "before_retrieval"
                if not shared_keyword:
                    self.last_search_scope.update(
                        adapter=KEYWORD_ADAPTER_VERSION if mode == "qmd_keyword" else FILTER_ADAPTER_VERSION,
                        strategy="keyword-fts-not-exists" if mode == "qmd_keyword" else "exact-allowed-vector-cosine")
            context_where, context_scope = "", None
            if contexts is not None:
                from .catalog_storage import revision_storage
                catalog_path = self.data_dir.parent / "catalog.sqlite3"
                with closing(object_index.catalog_connection(catalog_path)) as identity_db:
                    revision_table = ("_revision_payloads" if revision_storage(identity_db) == "catalog-revisions/zlib-v1" else "revisions")
                db.execute("ATTACH DATABASE ? AS case_contexts", (Path(contexts["path"]).as_uri() + "?mode=ro",))
                db.execute("ATTACH DATABASE ? AS case_context_catalog", (catalog_path.as_uri() + "?mode=ro",))
                # The same alias appears in the QMD gate's path exclusions.
                db.execute("ATTACH DATABASE ? AS authoritative_objects", ((directory / "objects.sqlite3").as_uri() + "?mode=ro",))
                context_where = case_context_store.dependency_predicate(revision_table=revision_table, holdout=bool(holdout_path))
                if retrieval_view == "case_overviews" and not include_geometry_only:
                    context_where += " AND " + case_context_store.capabilities_predicate()
                context_scope = {"db_path": contexts["path"], "catalog_path": str(catalog_path),
                                 "revision_table": revision_table,
                                 "exclude_geometry_only": retrieval_view == "case_overviews" and not include_geometry_only}
            placeholders = ",".join("?" for _ in selected)
            if not compact:
                object_index.register_scope_functions(db)
            scope_where, scope_params = object_index.scope_predicate(
                compact=compact, library_ids=selected, kind=kind, feature_branch=feature_branch)
            ordinary_scope_where = scope_where
            if context_where:
                scope_where += " AND " + context_where
            base_scope_where, base_scope_params = scope_where, scope_params
            if navigation is not None:
                navigation_where, navigation_params = _navigation_predicate(retrieval_view, case_id, stage)
                scope_where += " AND " + navigation_where
                scope_params = [*scope_params, *navigation_params]
                if case_id is not None and db.execute("SELECT 1 FROM objects o WHERE o.id=?" + holdout_where, (case_id,)).fetchone() is None:
                    raise PermissionError("Selected case is unavailable or evaluation-excluded")
            scoped_where = scope_where + holdout_where
            if navigation is not None:
                counts = {library: 0 for library in selected}
                counts.update(_navigation_counts(db, retrieval_view, case_id, stage, scope_where=base_scope_where,
                                                scope_params=base_scope_params, holdout_where=holdout_where))
                self.last_search_scope.update(eligible_objects=sum(counts.values()), libraries=counts)
                if not any(counts.values()):
                    return []
            elif compact:
                counts = query_scope_counts.counts(db, directory / "objects.sqlite3", active,
                    policy_token=policy.token, holdout_path=holdout_path, library_ids=selected,
                    kind=kind, feature_branch=feature_branch)
                if contexts is not None:
                    # Subtract only sidecar identities from cached counts. No
                    # new full object scan or source-body decode per query.
                    excluded_source = "SELECT case_id FROM case_contexts.contexts"
                    if context_scope["exclude_geometry_only"]:
                        excluded_source += " UNION SELECT case_id FROM case_contexts.capabilities WHERE classification='geometry_only'"
                    excluded = dict(db.execute("SELECT o.library_id,count(*) FROM (" + excluded_source +
                        ") excluded CROSS JOIN objects o ON o.id=excluded.case_id WHERE " + ordinary_scope_where +
                        holdout_where + " AND NOT (" + context_where + ") GROUP BY o.library_id", scope_params))
                    counts = {library: count - excluded.get(library, 0) for library, count in counts.items()}
                if not filtered:
                    counts = {library: count for library, count in counts.items() if count}
                self.last_search_scope.update(eligible_objects=sum(counts.values()), libraries=counts)
            elif filtered:
                counts = {library: 0 for library in selected}
                counts.update(dict(db.execute("SELECT o.library_id,count(*) FROM objects o WHERE " + scope_where +
                                              " GROUP BY o.library_id", scope_params)))
                if holdout_path:
                    excluded = _excluded_scope_counts(db, compact=compact, scope_where=scope_where, scope_params=scope_params)
                    counts = {library: count - excluded.get(library, 0) for library, count in counts.items()}
                self.last_search_scope.update(eligible_objects=sum(counts.values()), libraries=counts)
                if not any(counts.values()):
                    return []
            else:
                # Preserve the covering-index aggregate on unfiltered reads.
                counts = dict(db.execute(f"SELECT library_id,count(*) FROM objects WHERE library_id IN ({placeholders}) GROUP BY library_id", list(selected)))
                if holdout_path:
                    excluded = dict(db.execute(f"SELECT library_id,object_count FROM evaluation_holdout.excluded_counts WHERE library_id IN ({placeholders})", list(selected)))
                    counts = {library: count - excluded.get(library, 0) for library, count in counts.items()
                              if count > excluded.get(library, 0)}
                self.last_search_scope.update(eligible_objects=sum(counts.values()), libraries=counts)
                if not counts:
                    return []
            check_cancelled()
            # Persisted counts describe the scope, never authorize it. Prove
            # empty scopes before starting a QMD query process or model.
            eligible_libraries = None
            if navigation is not None:
                eligible_libraries = {library for library, count in counts.items() if count}
            elif filtered:
                eligible_libraries = {library for library in selected if db.execute(
                    "SELECT 1 FROM objects o WHERE " + scoped_where + " AND o.library_id=? LIMIT 1",
                    [*scope_params, library]).fetchone() is not None}
                if not eligible_libraries:
                    return []

            def allowed_paths(library, item):
                # Per-library compatibility adapter for legacy/shared and vector
                # stores. Compact shared-keyword reads never enter it.
                if compact:
                    rows = db.execute("SELECT o.qmd_path,o.id FROM objects o WHERE " + scoped_where +
                                      " AND o.library_id=?", [*scope_params, library])
                    eligible_ids = None
                else:
                    eligible_ids = {row[0] for row in db.execute("SELECT o.id FROM objects o WHERE " + scoped_where +
                                                               " AND o.library_id=?", [*scope_params, library])}
                    rows = legacy["mapping"].items()
                filenames = {}
                for path, sid in rows:
                    if path.startswith(item["key"] + "/") and (eligible_ids is None or sid in eligible_ids):
                        name = path[len(item["key"]) + 1:]
                        if len(name) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md", name):
                            raise RuntimeError("Invalid generated object path in retrieval scope")
                        filenames[sid] = name
                if len(filenames) != counts.get(library, 0) or eligible_ids is not None and set(filenames) != eligible_ids:
                    raise RuntimeError("Retrieval scope is missing authoritative object mappings")
                return sorted(filenames.values())
            if shared_keyword:
                shared_path = (directory / "qmd-keyword.sqlite3").resolve()
                collections = []
                by_collection = {}
                for library, item in selected.items():
                    if Path(item["db_path"]).resolve() != shared_path or item.get("collection") != item["key"]:
                        raise RuntimeError("Shared QMD manifest has inconsistent collection/database ownership")
                    if eligible_libraries is not None and library not in eligible_libraries:
                        continue
                    scope = {"library_key": library, "key": item["key"], "collection": item["collection"]}
                    if filtered and not compact:
                        scope["allowed_paths"] = allowed_paths(library, item)
                    collections.append(scope)
                    by_collection[item["collection"]] = library
                request = {"op": "shared_keyword", "db_path": str(shared_path), "query": query,
                           "keyword_terms": keyword_terms, "limit": engine_limit,
                           "collections": collections}
                if compact and filtered:
                    request["authoritative_scope"] = {"db_path": str(directory / "objects.sqlite3"),
                                                      "kind": kind, "feature_branch": feature_branch}
                    if navigation is not None:
                        request["authoritative_scope"]["navigation"] = {"db_path": navigation["path"], "view": retrieval_view,
                                                                     "case_id": case_id, "stage": stage}
                    if context_scope is not None:
                        request["authoritative_scope"]["case_context"] = context_scope
                if holdout_path:
                    request["holdout_db_path"] = str(holdout_path)
                for row in self._engine_shared_keyword(request):
                    if not isinstance(row, dict) or row.get("source") != "fts":
                        raise RuntimeError("Shared QMD hit source does not match keyword mode")
                    path = unquote(str(row.get("filepath", "")))
                    parts = path[len("qmd://"):].split("/") if path.startswith("qmd://") else []
                    if len(parts) != 2 or parts[0] not in by_collection or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md", parts[1]):
                        raise RuntimeError("Shared QMD returned a source outside the requested collection scope")
                    sid = object_index.lookup_id(db, "/".join(parts)) if compact else legacy["mapping"].get("/".join(parts))
                    owner = db.execute("SELECT library_id FROM objects WHERE id=?", (sid,)).fetchone() if sid else None
                    if not owner or owner[0] != by_collection[parts[0]]:
                        raise RuntimeError("Shared QMD returned an object outside the authoritative retrieval scope")
                    if holdout_path and db.execute("SELECT 1 FROM objects o WHERE o.id=?" + holdout_where, (sid,)).fetchone() is None:
                        raise RuntimeError("QMD returned an evaluation-excluded object")
                    if db.execute("SELECT 1 FROM objects o WHERE o.id=? AND " + scope_where, [sid, *scope_params]).fetchone() is None:
                        raise RuntimeError("Shared QMD returned an object outside the authoritative retrieval scope")
                    hits.append({"id": sid, "score": float(row["score"]), "source": "fts"})
            else:
                # Each DB contains only this library: no global top-k/post-filter.
                requests = []
                for library, item in selected.items():
                    if eligible_libraries is not None and library not in eligible_libraries:
                        continue
                    request = {"op": "vector" if mode == "qmd_vector" else "keyword", "query": query, "limit": engine_limit, **item,
                               "library_key": library}
                    if holdout_path:
                        request.update(holdout_db_path=str(holdout_path), holdout_collection=item["key"])
                    if filtered:
                        request["allowed_paths"] = allowed_paths(library, item)
                    if mode == "qmd_keyword":
                        request["keyword_terms"] = keyword_terms
                    if mode == "qmd_vector":
                        request["db_path"] = item.get("vector_db_path", item["db_path"])
                        request["index_model_uri"] = self._index_model_uri(active["modes"][mode], library)
                    requests.append(request)
                responses = self._engine_batch(requests)
                for request, response in zip(requests, responses):
                    check_cancelled()
                    library = request["library_key"]
                    item = selected[library]
                    rows = response["results"]
                    for row in rows:
                        if row.get("source") != ("vec" if mode == "qmd_vector" else "fts"):
                            raise RuntimeError("QMD hit source does not match requested mode")
                        path = unquote(str(row.get("filepath", "")))
                        prefix = "qmd://objects/"
                        if not path.startswith(prefix) or "/" in path[len(prefix):] or "\\" in path[len(prefix):]:
                            continue
                        key = item["key"] + "/" + path[len(prefix):]
                        sid = object_index.lookup_id(db, key) if compact else legacy["mapping"].get(key)
                        if sid:
                            if holdout_path and db.execute("SELECT 1 FROM objects o WHERE o.id=?" + holdout_where, (sid,)).fetchone() is None:
                                raise RuntimeError("QMD returned an evaluation-excluded object")
                            if db.execute("SELECT 1 FROM objects o WHERE o.id=? AND " + scope_where, [sid, *scope_params]).fetchone() is None:
                                raise RuntimeError("QMD returned an object outside the requested retrieval scope")
                            hits.append({"id": sid, "score": float(row["score"]), "source": row.get("source"), "chunk_position": row.get("chunkPos")})
                hits.sort(key=lambda value: value["score"], reverse=True)
            if navigation is not None and query_scope_counts.database_signature(navigation["path"], change_counter=True) != navigation_signature:
                raise RuntimeError("Case navigation changed during retrieval; retry the query")
            case_context_store.assert_no_pending(self.data_dir)
            if contexts is not None and query_scope_counts.database_signature(contexts["path"], change_counter=True) != context_signature:
                raise RuntimeError("Case context metadata changed during retrieval; retry the query")
            output, seen = [], set()
            catalog_path, catalog_db = self.data_dir.parent / "catalog.sqlite3", None
            for hit in hits:
                check_cancelled()
                if hit["id"] in seen:
                    continue
                columns = "library_id,source_json IS NULL" if compact else "library_id"
                row = db.execute("SELECT " + columns + " FROM objects o WHERE id=?" + holdout_where, (hit["id"],)).fetchone()
                if not row or row[0] not in selected:
                    continue
                if compact and row[1] and catalog_db is None:
                    if not catalog_path.is_file():
                        raise RuntimeError("Authoritative catalog required by the compact object index is missing")
                    catalog_db = resources.enter_context(closing(object_index.catalog_connection(catalog_path)))
                item = object_index.hydrate_object(db, hit["id"], catalog_path=catalog_path, catalog_db=catalog_db)
                if item is None or policy.blocks_object(item):
                    continue
                item["retrieval"] = {"backend": mode, "generation": active["generation"], "corpus_hash": active["corpus_hash"], "score": hit["score"],
                                     "rank": len(output) + 1, "source": hit["source"], "model_sha256": active["modes"][mode].get("model_sha256")}
                item["retrieval"]["representation"] = (object_index.representation_stats(db, item["id"]) if compact else
                    copy.deepcopy(legacy["representation"]["objects"][item["id"]]))
                if mode == "qmd_keyword":
                    item["retrieval"]["query_policy"] = KEYWORD_QUERY_POLICY
                item["retrieval"]["recipe_version"] = active["bridge_version"]
                item["retrieval"]["search_scope"] = {**self.last_search_scope, "library_id": item["library_id"]}
                if hit.get("chunk_position") is not None:
                    item["retrieval"]["chunk_position"] = hit["chunk_position"]
                output.append(item)
                seen.add(hit["id"])
                if len(output) == limit:
                    break
            check_cancelled()
            visible_global_now = self._searchable_library_ids()
            visible_now = None if visible_global_now is None else visible_global_now & set(selected)
            policy_now = exclusion_snapshot(self.data_dir.parent)
            if navigation is not None and case_id is not None:
                owner_visibility = self._searchable_library_ids([navigation["library_id"]])
                if (policy_now.blocks_identity(case_id) or navigation["case_group"] in policy_now.case_groups
                        or owner_visibility is not None and navigation["library_id"] not in owner_visibility):
                    raise PermissionError("Selected case became unavailable or evaluation-excluded during retrieval")
            if policy_now.token != policy.token:
                output = [item for item in output if not policy_now.blocks_object(item)]
                for rank, item in enumerate(output, 1):
                    item["retrieval"]["rank"] = rank
            if contexts is not None and (visible_global_now != visible_global_at_start or policy_now.token != policy.token):
                # Return no text derived from a dependency whose live policy
                # changed after QMD evaluated its pre-TopK gate.
                raise RuntimeError("Case context dependency visibility changed during retrieval; retry the query")
            if contexts is not None and any(db.execute(
                    "SELECT 1 FROM objects o WHERE o.id=? AND " + context_where, (item["id"],)).fetchone() is None
                    for item in output):
                raise RuntimeError("Case context dependency identities changed during retrieval; retry the query")
            if visible_at_start is not None and visible_now is None:
                raise LibraryVisibilityError("Library lifecycle catalog disappeared during the query")
            if visible_now is not None:
                output = [item for item in output if item["library_id"] in visible_now]
                self.last_search_scope["libraries"] = {key: value for key, value in self.last_search_scope["libraries"].items() if key in visible_now}
                self.last_search_scope["eligible_objects"] = sum(self.last_search_scope["libraries"].values())
                for rank, item in enumerate(output, 1):
                    item["retrieval"]["rank"] = rank
                    item["retrieval"]["search_scope"].update(self.last_search_scope)
            return output
