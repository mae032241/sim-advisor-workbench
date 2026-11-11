"""Isolated, generation-bound native/QMD keyword/QMD embedding search.

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
import threading
import time
import uuid
from contextlib import closing, contextmanager
from collections import OrderedDict
from pathlib import Path
from urllib.parse import unquote

from .knowledge import expand_query
from .process_runner import run_text
from .offline_profile import local_model_path, retrieval_runtime
from .build_control import BuildCancelled, check_cancelled
from .index_paths import resolve_index_manifest, serialize_index_manifest
from .index_manifest_cache import read_manifest
from .index_manifest import publish_manifest
from . import native_index
from .workspace_paths import WorkspacePaths
from .case_metadata import normalize_feature_branch, object_feature_branch
from .retrieval_settings import read as read_embedding_settings, require_enabled
from .evaluation_sets import exclusion_snapshot

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODES = ("native", "qmd_keyword", "qmd_vector")
DEFAULT_EMBED_MODEL = "hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf"
BRIDGE_MARKER = "LIBRARY_SEARCH_RESULT="
BRIDGE_VERSION = "2"
REPRESENTATION_CHARS = 1400
FILTER_ADAPTER_VERSION = "qmd-2.8.3-scope-v1"
SHARED_KEYWORD_STORAGE = "shared-collections-v1"
_DIRECTORY_LOCKS = {}
_DIRECTORY_LOCKS_GUARD = threading.Lock()
_MODEL_HASH_CACHE = OrderedDict()
_MODEL_HASH_CACHE_LIMIT = 32
_MODEL_HASH_CACHE_LOCK = threading.Lock()
_HOLDOUT_CACHE_LOCK = threading.Lock()
_HOLDOUT_SCHEMA = "evaluation-search-exclusions/v2"


def _holdout_clause(*, compact, alias="o"):
    clause = f"NOT EXISTS(SELECT 1 FROM evaluation_holdout.identities h WHERE h.id={alias}.id)"
    if compact:
        clause += f" AND NOT EXISTS(SELECT 1 FROM evaluation_holdout.paths h WHERE h.path={alias}.qmd_path)"
    return clause


class LibraryVisibilityError(ValueError):
    """The managed library lifecycle catalog cannot safely authorize a query."""


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
const envelope=JSON.parse(readFileSync(0,'utf8'));
for(const [key,value] of Object.entries(envelope.env)) process.env[key]=value;
if(!envelope.embedding_enabled)delete process.env.QMD_EMBED_MODEL;
// Chunk tokenization also uses QMD's process-wide default LLM. Keep it on the
// same resolved local model as this store; an HF URI could otherwise download
// despite an offline receipt and a store-specific local model configuration.
if(envelope.op!=='pull'&&envelope.model_uri)process.env.QMD_EMBED_MODEL=envelope.model_uri;
const quiet=(...args)=>process.stderr.write(args.map(x=>typeof x==='string'?x:JSON.stringify(x)).join(' ')+'\n');
console.log=quiet;
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
  if(!Array.isArray(envelope.queries)||!envelope.queries.length||envelope.queries.some(query=>typeof query!=='string'||!query.trim()))throw new Error('Invalid shared QMD query variants');
  if(!Number.isInteger(envelope.limit)||envelope.limit<1||envelope.limit>100)throw new Error('Invalid shared QMD query limit');
  const owners=new Set(),collections=new Set();
  for(const item of envelope.collections){
    if(!item||typeof item.library_key!=='string'||!item.library_key.trim()||owners.has(item.library_key)||!/^lib_[a-f0-9]{20}$/.test(item.collection)||item.collection!==item.key||collections.has(item.collection))throw new Error('Shared QMD collections must have unique library identities');
    owners.add(item.library_key);collections.add(item.collection);
    if(item.allowed_paths!==undefined&&(!Array.isArray(item.allowed_paths)||item.allowed_paths.some(path=>typeof path!=='string'||path.length>200||!/^[A-Za-z0-9][A-Za-z0-9._-]*\.md$/.test(path))))throw new Error('Invalid shared QMD allowed paths');
  }
}
// One SDK module import for the entire query, retaining one closed store per DB.
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
  const lexicalCompiler=async()=>{
    const {searchFTS}=await import(pathToFileURL(join(req.qmd_dir,'dist/store.js')).href);
    if(typeof searchFTS!=='function')throw new Error('QMD 2.8.3 lexical query compiler unavailable');
    return query=>{
      let match=null,prepared=0;
      searchFTS({prepare(sql){
        if(++prepared!==1||!/WITH\s+fts_matches\s+AS\s*\(/i.test(sql)||!/FROM\s+documents_fts\b/i.test(sql)||!/documents_fts\s+MATCH\s+\?/i.test(sql)||!/bm25\(documents_fts,\s*1\.5,\s*4\.0,\s*1\.0\)/i.test(sql))throw new Error('Unsupported QMD 2.8.3 lexical compiler SQL shape');
        return {all(...params){if(typeof params[0]!=='string')throw new Error('Invalid QMD compiled lexical query');match=params[0];return [];}};
      }},query,1);
      return match;
    };
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
    if(req.op==='keyword'&&!req.holdout_db_path){
      // The SDK limits FTS candidates before collection joins. A scoped TEMP
      // FTS index is therefore needed; merely post-filtering IDs can starve.
      db.exec("CREATE VIRTUAL TABLE temp.documents_fts USING fts5(filepath,title,body,tokenize='porter unicode61')");
      db.exec(`INSERT INTO temp.documents_fts(rowid,filepath,title,body)
        SELECT f.rowid,f.filepath,f.title,f.body FROM main.documents_fts f
        JOIN main.documents d ON d.id=f.rowid JOIN temp.query_scope_paths s ON s.path=d.path
        WHERE d.collection='objects' AND d.active=1 AND ${holdoutWhere('d')}`);
    }
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
    // Reuse the SDK's complete lexical parser without loading document bodies
    // or contexts. Its exported searchFTS reaches this proxy only after parsing.
    const compile=await lexicalCompiler();
    const registered=new Set(db.prepare('SELECT name FROM store_collections').all().map(row=>row.name));
    if(req.collections.some(item=>!registered.has(item.collection)))throw new Error('Shared QMD collection is missing');
    db.exec('PRAGMA temp_store=MEMORY; CREATE TEMP TABLE shared_query_collections(collection TEXT PRIMARY KEY,restricted INTEGER NOT NULL); CREATE TEMP TABLE shared_query_paths(collection TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(collection,path))');
    const addCollection=db.prepare('INSERT INTO temp.shared_query_collections VALUES(?,?)');
    const addPath=db.prepare('INSERT OR IGNORE INTO temp.shared_query_paths VALUES(?,?)');
    db.transaction(()=>{
      for(const item of req.collections){
        addCollection.run(item.collection,item.allowed_paths===undefined?0:1);
        for(const path of item.allowed_paths||[])addPath.run(item.collection,path);
      }
    })();
    // CROSS JOIN fixes the FTS cursor first. Collection/path restrictions are
    // applied to every matching row before sorting and TopK, without copying FTS.
    const select=db.prepare(`SELECT 'qmd://' || d.collection || '/' || d.path AS filepath,
        bm25(documents_fts,1.5,4.0,1.0) AS bm25_score
      FROM documents_fts CROSS JOIN documents d ON d.id=documents_fts.rowid
      CROSS JOIN temp.shared_query_collections s ON s.collection=d.collection
      WHERE documents_fts MATCH ? AND d.active=1
        AND ${holdoutWhere('d')}
        AND (s.restricted=0 OR EXISTS(SELECT 1 FROM temp.shared_query_paths p WHERE p.collection=d.collection AND p.path=d.path))
      ORDER BY bm25_score ASC LIMIT ?`);
    const pool=new Map();
    for(let i=0;i<req.queries.length;i++){
      const match=compile(req.queries[i]);
      if(match===null)continue;
      const hits=select.all(match,req.limit);
      for(let rank=0;rank<hits.length;rank++){
        const hit=hits[rank],old=pool.get(hit.filepath)||{filepath:hit.filepath,score:0,source:'fts'};
        old.score+=(i===0?2:1)/(60+rank+1);pool.set(hit.filepath,old);
      }
    }
    return [...pool.values()].sort((a,b)=>b.score-a.score).slice(0,req.limit);
  };
  const searchExcludedKeyword=async()=>{
    const compile=await lexicalCompiler(),db=store.internal.db;
    const scope=req.allowed_paths===undefined?'':' AND EXISTS(SELECT 1 FROM temp.query_scope_paths s WHERE s.path=d.path)';
    // Evaluate exclusions against every FTS match before ranking/TopK. This
    // neither materializes the corpus nor lets held-out bodies reach the SDK.
    const select=db.prepare(`SELECT 'qmd://objects/' || d.path AS filepath,
        bm25(documents_fts,1.5,4.0,1.0) AS bm25_score
      FROM main.documents_fts CROSS JOIN main.documents d ON d.id=documents_fts.rowid
      WHERE documents_fts MATCH ? AND d.collection='objects' AND d.active=1
        AND ${holdoutWhere('d')}${scope} ORDER BY bm25_score ASC LIMIT ?`);
    const pool=new Map(),queries=req.queries?.length?req.queries:[req.query];
    for(let i=0;i<queries.length;i++){
      const match=compile(queries[i]);if(match===null)continue;
      const hits=select.all(match,req.limit);
      for(let rank=0;rank<hits.length;rank++){
        const hit=hits[rank],old=pool.get(hit.filepath)||{filepath:hit.filepath,score:0,source:'fts'};
        old.score+=(i===0?2:1)/(60+rank+1);pool.set(hit.filepath,old);
      }
    }
    return [...pool.values()].sort((a,b)=>b.score-a.score).slice(0,req.limit);
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
    else if(req.op==='keyword'&&req.holdout_db_path)result=await searchExcludedKeyword();
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
    }else if(req.op==='keyword'){
      const pool=new Map();
      const queries=req.queries?.length?req.queries:[req.query];
      for(let i=0;i<queries.length;i++){
        const hits=await store.searchLex(queries[i],{collection:'objects',limit:req.limit});
        for(let rank=0;rank<hits.length;rank++){
          const hit=hits[rank],old=pool.get(hit.filepath)||{...hit,score:0};
          old.score+=(i===0?2:1)/(60+rank+1);old.source='fts';pool.set(hit.filepath,old);
        }
      }
      result=[...pool.values()].sort((a,b)=>b.score-a.score).slice(0,req.limit);
    }
    else if(req.op==='vector'){
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
process.stdout.write('LIBRARY_SEARCH_RESULT='+JSON.stringify(result)+'\n');
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
    queries = request.get("queries")
    if not isinstance(queries, list) or not queries or any(not isinstance(query, str) or not query.strip() for query in queries):
        raise ValueError("Invalid shared QMD query variants")
    if type(request.get("limit")) is not int or not 1 <= request["limit"] <= 100:
        raise ValueError("Invalid shared QMD query limit")
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


def _cjk_tokens(text):
    output = []
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        output.extend(run[i:i+2] for i in range(len(run)-1))
        if len(run) == 1:
            output.append(run)
    return list(dict.fromkeys(output))


def _lex_terms(query):
    expanded = expand_query(query)["terms"]
    local = {"压降": "pressure drop", "壓降": "pressure drop", "压差": "pressure difference", "简化": "simplification", "簡化": "simplification"}
    for key, value in local.items():
        if key in query:
            expanded.extend(value.split())
    return list(dict.fromkeys(expanded + _cjk_tokens(query)))[:60]


def _representation(item):
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
    return representation, {"policy": "bounded-object-with-labelled-inferred-annotation-v1", "original_text_characters": len(text),
        "original_data_characters": len(_json(data)), "indexed_characters": len(representation), "body_characters_retained": len(excerpt),
        "truncated": code_selected or excerpt != text, "annotation_included": bool(annotation_fields), "annotation_is_observed": False,
        "annotation_fields": list(annotation_fields), "feature_structure_included": bool(structure), "character_budget": REPRESENTATION_CHARS}


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

    def _engine(self, request, *, timeout=120):
        check_cancelled()
        if request.get("op") == "pull":
            raise ValueError("Automatic embedding model downloads are disabled; select a local GGUF")
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
        result = run_text([self.node, str(script)], input_text=_json(payload), cwd=operation_cwd,
                          timeout=max(5, min(int(timeout), 1800)), max_output=4 * 1024 * 1024)
        check_cancelled()
        if result.returncode:
            # No model/context output is persisted. Useful bounded native errors
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
        """Run one bounded Node process, preserving independent per-library hits."""
        request = {"op": "batch", "requests": requests}
        _query_batch_requests(request)
        response = self._engine(request, timeout=180)
        check_cancelled()
        return _query_batch_results(requests, response)

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
        value = read_manifest(path, summary, cancellable=cancellable)
        if value is None:
            return None
        value.update(resolve_index_manifest({key: value[key] for key in _MANIFEST_LOCATORS if key in value}, self.data_dir))
        value.pop("path_schema", None)
        return value

    def _load_active(self, *, summary=False, cancellable=True):
        return self._load_manifest(self.active_path, summary=summary, cancellable=cancellable)

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
        # Older manifests already include the native model identity returned by
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
                paths = ([Path(result["directory"]) / "objects.sqlite3"] if mode == "native" else
                         [Path(library["vector_db_path" if mode == "qmd_vector" and "vector_db_path" in library else "db_path"]) for library in result.get("libraries", {}).values()])
                if item.get("ready") and (not paths or any(not path.is_file() for path in set(paths))):
                    item.update(ready=False, state="unavailable", error="Index files are missing from the current search root; the former root is not used")
            if result.get("bridge_version") != BRIDGE_VERSION:
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

    def _build_keyword(self, native_path, output, libraries, *, on_progress=None):
        from .qmd_shared import build_keyword_index
        return build_keyword_index(native_path, output, qmd_dir=self.qmd_dir, node=self.node,
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
        if not modes or set(modes) - set(MODES):
            raise ValueError("modes must contain native, qmd_keyword and/or qmd_vector")
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
                        progress("indexing_" + ("native" if mode == "native" else "keyword" if mode == "qmd_keyword" else "vector"),
                                 0, 0, "objects" if mode == "native" else "indexes", "Generation already built; no work repeated")
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
            if "native" in modes:
                progress("indexing_native", 0, object_count, "objects", "Building native FTS and source representations")
            library_exported = {}
            catalog_path = self.data_dir.parent / "catalog.sqlite3"
            managed_sources = (getattr(objects, "path", None) is not None
                               and Path(objects.path).resolve() == catalog_path.resolve())
            indexed_characters = truncated_objects = 0
            with closing(sqlite3.connect(db_path)) as db, db, _object_cursor(objects) as source_objects:
                native_index.create_schema(db)
                for completed, item in enumerate(source_objects, 1):
                    check_cancelled()
                    library = item["library_id"]
                    if "native" in modes and library not in library_exported:
                        progress("indexing_native", completed - 1, object_count, "objects", f"Starting library {library}")
                    text, stats = _representation(item)
                    check_cancelled()
                    if not managed_sources:
                        representations[item["id"]] = stats
                    indexed_characters += stats["indexed_characters"]
                    truncated_objects += int(stats["truncated"])
                    native_index.insert_object(db, item, text, stats, " ".join(_cjk_tokens(item["title"] + " " + text)), standalone=not managed_sources)
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
                    if "native" in modes and (completed % 25 == 0 or library_exported[library] == library_totals[library]):
                        progress("indexing_native", completed, object_count, "objects", f"Exported library {library}" if library_exported[library] == library_totals[library] else f"Exporting library {library}")
                # Raising here rolls back all inserts in this staging database.
                check_cancelled()
            check_cancelled()
            if library_exported != library_totals:
                raise ValueError("Catalog snapshot changed during source export")
            manifest = {"ready": False, "generation": generation, "corpus_hash": fingerprint, "directory": str(directory), "object_count": object_count,
                        "libraries": libraries, "mapping": mapping, "modes": {mode: {"ready": False, "state": "building" if mode in modes else "not_requested"} for mode in MODES}, "bridge_version": BRIDGE_VERSION,
                        "keyword_storage": "shared-collections-v1", "object_lookup": {"schema": native_index.LOOKUP_SCHEMA},
                        "representation": {"object_count": object_count, "indexed_objects": object_count, "unindexed_objects": 0,
                            "truncated_objects": truncated_objects, "indexed_characters": indexed_characters,
                            "character_budget_per_object": REPRESENTATION_CHARS, "objects": representations}}
            if "native" in modes:
                manifest["modes"]["native"] = {"ready": True, "state": "ready", "backend": "sqlite-fts5", "build_seconds": round(time.monotonic() - started, 2)}
                self._publish(directory, manifest)
                progress("indexing_native", object_count, object_count, "objects", "Native index completed")
            qmd_modes = set(modes) - {"native"}
            lexical_ok = bool(qmd_modes)
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
                    self._build_keyword(db_path, directory / "qmd-keyword.sqlite3", libraries,
                                        on_progress=keyword_progress)
                    check_cancelled()
                    keyword_completed = len(libraries)
                    manifest["modes"]["qmd_keyword"] = {"ready": True, "state": "ready", "backend": "qmd-shared-fts", "qmd_version": self._qmd_version(), "build_seconds": round(time.monotonic() - keyword_started, 2)}
                    self._publish(directory, manifest)
                    progress("indexing_keyword", keyword_completed, len(libraries), "indexes", "Keyword phase completed")
                except BuildCancelled:
                    raise
                except Exception as exc:
                    lexical_ok = False
                    for mode in qmd_modes:
                        manifest["modes"][mode] = {"ready": False, "state": "failed", "error": str(exc)[:2000]}
                    progress("indexing_keyword", keyword_completed, len(libraries), "indexes", "Keyword phase failed: " + str(exc)[:400])
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
            if all(manifest["modes"][mode].get("ready") for mode in modes) and set(modes) - {"native"}:
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
        if manifest["ready"]:
            previous = self._load_active(summary=True)
            if previous and previous.get("directory") != str(directory):
                from .index_retention import retire_generation
                warning = retire_generation(self.data_dir, previous["directory"])
                if warning:
                    manifest.setdefault("retention_warnings", []).append(warning)
        publish_manifest(directory, manifest, self.data_dir, write_json=_write_json,
                         active=manifest["ready"], last_build=True)

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
        if not (policy.citation_ids or policy.object_ids or policy.qmd_paths):
            return None
        token = _hash(_json([_HOLDOUT_SCHEMA, policy.token, active["directory"], active["corpus_hash"]]))
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
                    cache.execute("CREATE TABLE excluded_counts(library_id TEXT PRIMARY KEY,object_count INTEGER NOT NULL) WITHOUT ROWID")
                    cache.executemany("INSERT OR IGNORE INTO identities VALUES(?)", ((value,) for value in identities))
                    cache.executemany("INSERT OR IGNORE INTO paths VALUES(?)", ((value,) for value in policy.qmd_paths))
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
                        for path, sid in self._load_active()["mapping"].items():
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

    def search(self, query, *, mode="native", library_ids=None, limit=8, feature_branch=None, kind="all"):
        if mode not in MODES:
            raise ValueError("Unknown retrieval mode")
        if mode == "qmd_vector":
            self._require_embedding()
        feature_branch = normalize_feature_branch(feature_branch)
        if not isinstance(kind, str) or not kind.strip() or len(kind) > 100:
            raise ValueError("kind must be a nonempty bounded string")
        kind = kind.strip().lower()
        filtered = feature_branch is not None or kind != "all"
        self.last_search_scope = {"mode": mode, "feature_branch": feature_branch, "kind": kind,
            "filter_stage": "before_retrieval" if filtered else "library_before_retrieval", "eligible_objects": 0, "libraries": {},
            "adapter": "native-fts-scope-v1" if mode == "native" else FILTER_ADAPTER_VERSION if filtered else "qmd-sdk-collection",
            "strategy": ("native-fts-where" if mode == "native" else "temporary-scoped-fts" if mode == "qmd_keyword" else "exact-allowed-vector-cosine") if filtered else "library-collection"}
        if not isinstance(query, str) or not query.strip():
            return []
        if isinstance(library_ids, str):
            library_ids = [library_ids]
        if library_ids is not None and (not isinstance(library_ids, list) or any(not isinstance(value, str) or not value for value in library_ids)):
            raise ValueError("library_ids must be a list of nonempty strings")
        visible_at_start = self._searchable_library_ids(library_ids)
        if visible_at_start is not None and not visible_at_start:
            return []
        policy = exclusion_snapshot(self.data_dir.parent)
        state = self.status(summary=True)
        if not state.get("modes", {}).get(mode, {}).get("ready"):
            raise RuntimeError(f"{mode} is not ready: " + str(state.get("modes", {}).get(mode, {}).get("error", "index not built")))
        active = self._load_active(summary=True)
        shared_keyword = mode == "qmd_keyword" and active.get("keyword_storage") == SHARED_KEYWORD_STORAGE
        if shared_keyword:
            self.last_search_scope.update(adapter="qmd-2.8.3-shared-scope-v1", strategy="shared-collection-fts-where")
        requested = set(active["libraries"]) if library_ids is None else set(library_ids)
        if visible_at_start is not None:
            requested &= visible_at_start
        selected = {key: value for key, value in active["libraries"].items() if key in requested}
        if not selected:
            return []
        limit = max(1, min(int(limit), 100))
        hits = []
        directory = Path(active["directory"])
        with closing(sqlite3.connect((directory / "objects.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
            compact = native_index.is_compact(db)
            holdout_path = self._holdout_database(policy, db, active, compact=compact)
            holdout_where = ""
            if holdout_path:
                db.execute("ATTACH DATABASE ? AS evaluation_holdout", (holdout_path.as_uri() + "?mode=ro",))
                holdout_where = " AND " + _holdout_clause(compact=compact)
                self.last_search_scope["evaluation_exclusions"] = "before_retrieval"
                self.last_search_scope["filter_stage"] = "before_retrieval"
                if not shared_keyword:
                    self.last_search_scope.update(
                        adapter="native-fts-scope-v1" if mode == "native" else FILTER_ADAPTER_VERSION,
                        strategy="native-fts-where" if mode == "native" else
                                 "keyword-fts-not-exists" if mode == "qmd_keyword" else "exact-allowed-vector-cosine")
            placeholders = ",".join("?" for _ in selected)
            eligible = None
            eligible_by_library = {library: [] for library in selected}
            if filtered:
                eligible = set()
                if compact:
                    clauses, params = [f"library_id IN ({placeholders})"], list(selected)
                    if kind != "all":
                        clauses.append("kind=?")
                        params.append(kind)
                    if feature_branch is not None:
                        clauses.append("feature_branch=?")
                        params.append(feature_branch)
                    candidates = db.execute("SELECT id,library_id FROM objects o WHERE " + " AND ".join(clauses) + holdout_where, params)
                else:
                    def legacy_candidates():
                        for sid, library, raw in db.execute(f"SELECT id,library_id,object_json FROM objects o WHERE library_id IN ({placeholders})" + holdout_where, list(selected)):
                            item = json.loads(raw)
                            if (kind == "all" or item.get("kind") == kind) and (feature_branch is None or object_feature_branch(item) == feature_branch):
                                yield sid, library
                    candidates = legacy_candidates()
                for sid, library in candidates:
                    eligible.add(sid)
                    eligible_by_library[library].append(sid)
                counts = {library: len(ids) for library, ids in eligible_by_library.items()}
                self.last_search_scope.update(eligible_objects=len(eligible), libraries=counts)
                if not eligible:
                    return []
                db.execute("PRAGMA temp_store=MEMORY")
                db.execute("CREATE TEMP TABLE query_scope_ids(id TEXT PRIMARY KEY)")
                db.executemany("INSERT INTO query_scope_ids VALUES(?)", [(sid,) for sid in eligible])
            else:
                # Keep this aggregate on the library covering index. Applying
                # per-row locator predicates here would read the large objects
                # payload table merely to count it. Exclusions were counted by
                # primary/unique-key probes once per policy and generation.
                counts = dict(db.execute(f"SELECT library_id,count(*) FROM objects WHERE library_id IN ({placeholders}) GROUP BY library_id", list(selected)))
                if holdout_path:
                    excluded = dict(db.execute(f"SELECT library_id,object_count FROM evaluation_holdout.excluded_counts WHERE library_id IN ({placeholders})", list(selected)))
                    counts = {library: count - excluded.get(library, 0) for library, count in counts.items()
                              if count > excluded.get(library, 0)}
                self.last_search_scope.update(eligible_objects=sum(counts.values()), libraries=counts)
                if not counts:
                    return []
            if mode == "native":
                terms = _lex_terms(query)
                if not terms:
                    return []
                match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                scope_join = " JOIN temp.query_scope_ids scope ON scope.id=o.id" if filtered else ""
                identity_join = "o.rowid=object_fts.rowid" if compact else "o.id=object_fts.id"
                rows = db.execute(f"SELECT o.id,bm25(object_fts,0,6,1,1) AS score FROM object_fts JOIN objects o ON {identity_join}{scope_join} WHERE object_fts MATCH ? AND o.library_id IN ({placeholders}){holdout_where} ORDER BY score LIMIT ?", [match, *selected, limit]).fetchall()
                hits = [{"id": sid, "score": -score, "source": "fts"} for sid, score in rows]
            elif shared_keyword:
                shared_path = (directory / "qmd-keyword.sqlite3").resolve()
                collections = []
                by_collection = {}
                for library, item in selected.items():
                    if Path(item["db_path"]).resolve() != shared_path or item.get("collection") != item["key"]:
                        raise RuntimeError("Shared QMD manifest has inconsistent collection/database ownership")
                    if filtered and not eligible_by_library[library]:
                        continue
                    scope = {"library_key": library, "key": item["key"], "collection": item["collection"]}
                    if filtered:
                        rows = (db.execute("SELECT o.qmd_path,o.id FROM objects o JOIN temp.query_scope_ids s ON s.id=o.id WHERE o.library_id=?", (library,)) if compact else
                                self._load_active()["mapping"].items())
                        filenames = {}
                        for path, sid in rows:
                            if path.startswith(item["key"] + "/") and sid in eligible:
                                filename = path[len(item["key"]) + 1:]
                                if len(filename) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md", filename):
                                    raise RuntimeError("Invalid generated object path in retrieval scope")
                                filenames[sid] = filename
                        if set(filenames) != set(eligible_by_library[library]):
                            raise RuntimeError("Retrieval scope is missing authoritative object mappings")
                        scope["allowed_paths"] = sorted(filenames.values())
                    collections.append(scope)
                    by_collection[item["collection"]] = library
                request = {"op": "shared_keyword", "db_path": str(shared_path), "query": query,
                           "queries": list(dict.fromkeys([query] + _lex_terms(query)[:16])), "limit": limit,
                           "collections": collections}
                if holdout_path:
                    request["holdout_db_path"] = str(holdout_path)
                for row in self._engine_shared_keyword(request):
                    if not isinstance(row, dict) or row.get("source") != "fts":
                        raise RuntimeError("Shared QMD hit source does not match keyword mode")
                    path = unquote(str(row.get("filepath", "")))
                    parts = path[len("qmd://"):].split("/") if path.startswith("qmd://") else []
                    if len(parts) != 2 or parts[0] not in by_collection or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md", parts[1]):
                        raise RuntimeError("Shared QMD returned a source outside the requested collection scope")
                    sid = native_index.lookup_id(db, "/".join(parts)) if compact else self._load_active()["mapping"].get("/".join(parts))
                    owner = db.execute("SELECT library_id FROM objects WHERE id=?", (sid,)).fetchone() if sid else None
                    if not owner or owner[0] != by_collection[parts[0]] or eligible is not None and sid not in eligible:
                        raise RuntimeError("Shared QMD returned an object outside the authoritative retrieval scope")
                    if holdout_path and db.execute("SELECT 1 FROM objects o WHERE o.id=?" + holdout_where, (sid,)).fetchone() is None:
                        raise RuntimeError("QMD returned an evaluation-excluded object")
                    hits.append({"id": sid, "score": float(row["score"]), "source": "fts"})
            else:
                # Each DB contains only this library: no global top-k/post-filter.
                requests = []
                for library, item in selected.items():
                    if filtered and not eligible_by_library[library]:
                        continue
                    terms = _lex_terms(query)[:16]
                    request = {"op": "vector" if mode == "qmd_vector" else "keyword", "query": query, "queries": list(dict.fromkeys([query] + terms)), "limit": limit, **item,
                               "library_key": library}
                    if holdout_path:
                        request.update(holdout_db_path=str(holdout_path), holdout_collection=item["key"])
                    if filtered:
                        filenames = {}
                        paths = (db.execute("SELECT o.qmd_path,o.id FROM objects o JOIN temp.query_scope_ids s ON s.id=o.id WHERE o.library_id=?", (library,)) if compact else
                                 self._load_active()["mapping"].items())
                        for path, sid in paths:
                            if path.startswith(item["key"] + "/") and sid in eligible:
                                name = path[len(item["key"]) + 1:]
                                if len(name) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md", name):
                                    raise RuntimeError("Invalid generated object path in retrieval scope")
                                filenames[sid] = name
                        if set(filenames) != set(eligible_by_library[library]):
                            raise RuntimeError("Retrieval scope is missing authoritative object mappings")
                        request["allowed_paths"] = sorted(filenames.values())
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
                        sid = native_index.lookup_id(db, key) if compact else self._load_active()["mapping"].get(key)
                        if sid:
                            if holdout_path and db.execute("SELECT 1 FROM objects o WHERE o.id=?" + holdout_where, (sid,)).fetchone() is None:
                                raise RuntimeError("QMD returned an evaluation-excluded object")
                            if eligible is not None and sid not in eligible:
                                raise RuntimeError("QMD returned an object outside the requested retrieval scope")
                            hits.append({"id": sid, "score": float(row["score"]), "source": row.get("source"), "chunk_position": row.get("chunkPos")})
                hits.sort(key=lambda value: value["score"], reverse=True)
            output, seen = [], set()
            for hit in hits:
                if hit["id"] in seen:
                    continue
                row = db.execute("SELECT library_id FROM objects o WHERE id=?" + holdout_where, (hit["id"],)).fetchone()
                if not row or row[0] not in selected:
                    continue
                item = native_index.hydrate_object(db, hit["id"], catalog_path=self.data_dir.parent / "catalog.sqlite3")
                if item is None or policy.blocks_object(item):
                    continue
                item["retrieval"] = {"backend": mode, "generation": active["generation"], "corpus_hash": active["corpus_hash"], "score": hit["score"],
                                     "rank": len(output) + 1, "source": hit["source"], "model_sha256": active["modes"][mode].get("model_sha256")}
                item["retrieval"]["representation"] = (native_index.representation_stats(db, item["id"]) if compact else
                    _representation(item)[1] if mode == "native" else
                    copy.deepcopy(self._load_active()["representation"]["objects"][item["id"]]))
                item["retrieval"]["recipe_version"] = active["bridge_version"]
                item["retrieval"]["search_scope"] = {**self.last_search_scope, "library_id": item["library_id"]}
                if hit.get("chunk_position") is not None:
                    item["retrieval"]["chunk_position"] = hit["chunk_position"]
                output.append(item)
                seen.add(hit["id"])
                if len(output) == limit:
                    break
            visible_now = self._searchable_library_ids(list(selected))
            policy_now = exclusion_snapshot(self.data_dir.parent)
            if policy_now.token != policy.token:
                output = [item for item in output if not policy_now.blocks_object(item)]
                for rank, item in enumerate(output, 1):
                    item["retrieval"]["rank"] = rank
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
