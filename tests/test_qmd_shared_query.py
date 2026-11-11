"""Shared keyword queries use the real SDK parser and scope before global TopK."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from advisor import library_search as module
from advisor import native_index
from advisor.index_manifest import publish_manifest
from advisor.library_search import BRIDGE_VERSION, LibrarySearch, _hash, _representation
from advisor.library_store import LibraryStore
from test_library_search import obj


def collection(library):
    return "lib_" + _hash(library)[:20]


def runtime(tmp_path):
    probe = LibrarySearch(tmp_path / "probe", cache_dir=tmp_path / "cache")
    if not probe.node or not (probe.qmd_dir / "dist/store.js").is_file() or probe._qmd_version() != "2.8.3":
        pytest.skip("Real local QMD 2.8.3 and Node are required")
    wrapper = tmp_path / "wrapper"
    (wrapper / "dist").mkdir(parents=True)
    (wrapper / "package.json").write_text('{"type":"module","version":"2.8.3"}', encoding="utf-8")
    trace = tmp_path / "shared-query-trace.jsonl"
    source = r"""import {appendFileSync} from 'node:fs';
      globalThis.fetch=()=>{throw new Error('Network forbidden');};
      const sdk=await import(INDEX);
      const trace=(event,extra={})=>appendFileSync(process.env.QMD_SHARED_TRACE,JSON.stringify({event,pid:process.pid,...extra})+'\n');
      trace('import');
      export async function createStore(options){
        if(options.config||options.configPath)throw new Error('Query must use DB-only mode');
        const store=await sdk.createStore(options),db=store.internal.db,prepare=db.prepare.bind(db),exec=db.exec.bind(db);
        trace('open');
        db.exec=sql=>{if(/CREATE VIRTUAL TABLE temp\./i.test(sql))throw new Error('Full temporary FTS copy forbidden');return exec(sql);};
        db.prepare=sql=>{
          if(/CREATE VIRTUAL TABLE temp\.documents_fts/i.test(sql))throw new Error('Full temporary FTS copy forbidden');
          const statement=prepare(sql);
          if(/FROM documents_fts CROSS JOIN/i.test(sql)){
            const all=statement.all.bind(statement);
            statement.all=(...args)=>{trace('query',{sql,match:args[0]});return all(...args);};
          }
          return statement;
        };
        store.update=store.embed=store.internal.llm.embed=()=>{throw new Error('Indexing and embedding forbidden');};
        const close=store.close.bind(store);
        store.close=async()=>{trace('close');await close();};
        return store;
      }""".replace("INDEX", json.dumps((probe.qmd_dir / "dist/index.js").as_uri()))
    (wrapper / "dist/index.js").write_text(source, encoding="utf-8")
    (wrapper / "dist/store.js").write_text("export {searchFTS} from " + json.dumps((probe.qmd_dir / "dist/store.js").as_uri()) + ";", encoding="utf-8")
    return probe, wrapper, trace


def make_database(tmp_path, probe, rows, path=None):
    path = path or tmp_path / "shared.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    setup = tmp_path / "create-fixture.mjs"
    setup.write_text("""import {createHash} from 'node:crypto';
      import {readFileSync} from 'node:fs';
      import {createStore} from STORE;
      globalThis.fetch=()=>{throw new Error('Network forbidden');};
      const rows=JSON.parse(readFileSync(0,'utf8')),store=createStore(DB),db=store.db,now='2026-11-10T00:00:00Z';
      try{db.transaction(()=>{
        for(const collection of new Set(rows.map(row=>row.collection)))
          db.prepare('INSERT INTO store_collections(name,path,pattern) VALUES(?,?,?)').run(collection,collection+'/corpus','**/*.md');
        for(const row of rows){
          const hash=createHash('sha256').update(row.body).digest('hex');
          store.insertContent(hash,row.body,now);
          store.insertDocument(row.collection,row.path,row.title,hash,now,now);
        }
      })();}finally{store.close();}
    """.replace("STORE", json.dumps((probe.qmd_dir / "dist/store.js").as_uri())).replace("DB", json.dumps(str(path))), encoding="utf-8")
    result = subprocess.run([probe.node, str(setup)], input=json.dumps(rows), capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    return path


def search_runtime(tmp_path, wrapper, trace, node, data_dir=None):
    search = LibrarySearch(data_dir or tmp_path / "search", qmd_dir=wrapper, node=node, cache_dir=tmp_path / "cache")
    environment = search._env
    search._env = lambda: {**environment(), "QMD_SHARED_TRACE": str(trace)}
    return search


def shared_request(database, libraries, queries, *, limit=40, paths=None):
    scopes = [{"library_key": library, "key": collection(library), "collection": collection(library)} for library in libraries]
    if paths is not None:
        for scope in scopes:
            scope["allowed_paths"] = paths.get(scope["library_key"], [])
    return {"op": "shared_keyword", "db_path": str(database), "queries": queries, "limit": limit, "collections": scopes}


def sdk_reference(tmp_path, probe, database, name, queries, limit):
    script = tmp_path / "sdk-reference.mjs"
    script.write_text("""import {createStore} from INDEX;
      const store=await createStore({dbPath:DB}),pool=new Map(),queries=QUERIES;
      try{for(let i=0;i<queries.length;i++){
        const hits=await store.searchLex(queries[i],{collection:COLLECTION,limit:LIMIT});
        for(let rank=0;rank<hits.length;rank++){
          const hit=hits[rank],old=pool.get(hit.filepath)||{filepath:hit.filepath,score:0,source:'fts'};
          old.score+=(i===0?2:1)/(60+rank+1);pool.set(hit.filepath,old);
        }
      }
      process.stdout.write(JSON.stringify([...pool.values()].sort((a,b)=>b.score-a.score).slice(0,LIMIT)));
      }finally{await store.close();}
    """.replace("INDEX", json.dumps((probe.qmd_dir / "dist/index.js").as_uri())).replace("DB", json.dumps(str(database)))
        .replace("QUERIES", json.dumps(queries)).replace("COLLECTION", json.dumps(name)).replace("LIMIT", str(limit)), encoding="utf-8")
    result = subprocess.run([probe.node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_real_sdk_lexical_parser_results_match_without_loading_sdk_bodies_or_context(tmp_path):
    probe, wrapper, trace = runtime(tmp_path)
    texts = ["alpha beta mesh 压降 model-a v1.2.3", "alpha beta thermal mesh model-b v1.2.4",
             "alpha gamma mesh 压差 model-a v2.1.3", "beta gamma structural geometry"]
    rows = [{"collection": collection("first"), "path": f"document-{index}.md", "title": f"Fixture {index}", "body": text}
            for index, text in enumerate(texts)]
    database = make_database(tmp_path, probe, rows)
    queries = ['"alpha beta"', 'model-a', 'v1.2.3', 'mesh -thermal', '压降']
    expected = sdk_reference(tmp_path, probe, database, collection("first"), queries, 10)
    search = search_runtime(tmp_path, wrapper, trace, probe.node)
    assert search._engine_shared_keyword(shared_request(database, ["first"], queries, limit=10)) == expected
    recorded = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
    assert len({event["pid"] for event in recorded}) == 1
    assert [event["event"] for event in recorded].count("open") == 1
    assert [event["event"] for event in recorded].count("close") == 1
    statements = [event for event in recorded if event["event"] == "query"]
    assert len(statements) == len(queries)
    assert all("content.doc" not in event["sql"] and "context" not in event["sql"] for event in statements)


def test_real_shared_sql_filters_collections_and_allowed_paths_before_topk(tmp_path):
    probe, wrapper, trace = runtime(tmp_path)
    rows = [{"collection": collection("closed"), "path": f"noise-{index}.md", "title": "needle " * 30, "body": "needle"}
            for index in range(450)]
    rows.extend([{"collection": collection("first"), "path": "geometry.md", "title": "needle " * 20, "body": "needle geometry"},
                 {"collection": collection("first"), "path": "mesh.md", "title": "Mesh", "body": "needle mesh"}])
    database = make_database(tmp_path, probe, rows)
    search = search_runtime(tmp_path, wrapper, trace, probe.node)
    hit = search._engine_shared_keyword(shared_request(database, ["first"], ["needle"], limit=1))[0]
    assert hit["filepath"] == "qmd://" + collection("first") + "/geometry.md"
    hit = search._engine_shared_keyword(shared_request(database, ["first"], ["needle"], limit=1, paths={"first": ["mesh.md"]}))[0]
    assert hit["filepath"] == "qmd://" + collection("first") + "/mesh.md"


def public_fixture(tmp_path):
    probe, wrapper, trace = runtime(tmp_path)
    sources = tmp_path / "sources"
    sources.mkdir()
    store = LibraryStore(tmp_path / "data/libraries")
    for library in ("first", "second", "closed"):
        store.register(library, "official-models", [{"id": "models", "path": str(sources)}], library_id=library)
        store.set_enabled(library, True)
    objects = []
    for library, branch, kind, text in (("first", "geometry", "code", "needle " * 30),
                                        ("first", "mesh", "case", "needle mesh"),
                                        ("second", "mesh", "case", "needle second"),
                                        ("closed", "mesh", "case", "needle " * 40)):
        item = obj(library + "-" + branch, library, text)
        item["data"]["feature_branch"] = branch
        item["kind"] = kind
        objects.append(item)
    generation = store.root / "search/generations/shared-fixture"
    generation.mkdir(parents=True)
    rows = []
    with closing(sqlite3.connect(generation / "objects.sqlite3")) as db, db:
        native_index.create_schema(db)
        for item in objects:
            text, stats = _representation(item)
            native_index.insert_object(db, item, text, stats, " ".join(module._cjk_tokens(item["title"] + " " + text)))
            rows.append({"collection": collection(item["library_id"]), "path": native_index.qmd_path(item).split("/", 1)[1],
                         "title": item["title"], "body": text})
    database = make_database(tmp_path, probe, rows, generation / "qmd-keyword.sqlite3")
    libraries = {library: {"key": collection(library), "collection": collection(library), "db_path": str(database),
                           "corpus": str(generation / collection(library) / "corpus"),
                           "vector_db_path": str(generation / collection(library) / "qmd-vector.sqlite3")}
                 for library in ("first", "second", "closed")}
    manifest = {"generation": "shared-fixture", "directory": str(generation), "libraries": libraries, "ready": True,
                "keyword_storage": "shared-collections-v1", "bridge_version": BRIDGE_VERSION, "corpus_hash": "fixture",
                "object_count": len(objects), "object_lookup": {"schema": native_index.LOOKUP_SCHEMA},
                "modes": {"native": {"ready": True}, "qmd_keyword": {"ready": True}, "qmd_vector": {"ready": False, "state": "disabled"}}}
    publish_manifest(generation, manifest, store.root / "search")
    search = search_runtime(tmp_path, wrapper, trace, probe.node, store.root / "search")
    return store, search, trace, objects


def test_public_shared_query_obeys_closed_library_and_branch_kind_scopes_before_limit(tmp_path, monkeypatch):
    store, search, trace, objects = public_fixture(tmp_path)
    store.set_enabled("closed", False)
    calls = []
    original = module.run_text
    def observe(*args, **kwargs):
        calls.append(json.loads(kwargs["input_text"]))
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "run_text", observe)
    hits = search.search("needle", mode="qmd_keyword", library_ids=["first", "closed"], feature_branch="mesh", kind="case", limit=1)
    assert [hit["id"] for hit in hits] == [objects[1]["id"]]
    assert len(calls) == 1 and calls[0]["op"] == "shared_keyword"
    assert [scope["library_key"] for scope in calls[0]["collections"]] == ["first"]
    assert len(calls[0]["collections"][0]["allowed_paths"]) == 1
    assert hits[0]["retrieval"]["search_scope"]["strategy"] == "shared-collection-fts-where"
    calls.clear()
    assert search.search("needle", mode="qmd_keyword", library_ids=["closed"]) == []
    assert calls == []
    # A close while the query runs remains a post-query visibility gate.
    original_shared = search._engine_shared_keyword
    def closes_after_query(request):
        result = original_shared(request)
        store.set_enabled("first", False)
        return result
    monkeypatch.setattr(search, "_engine_shared_keyword", closes_after_query)
    assert search.search("needle", mode="qmd_keyword", library_ids=["first"]) == []


def test_shared_query_rejects_wrong_collection_owner_missing_collections_and_unknown_sdk(tmp_path):
    probe, wrapper, trace = runtime(tmp_path)
    database = make_database(tmp_path, probe, [{"collection": collection("first"), "path": "one.md", "title": "One", "body": "needle"}])
    search = search_runtime(tmp_path, wrapper, trace, probe.node)
    request = shared_request(database, ["first"], ["needle"])
    bad = dict(request, collections=[{"library_key": "other", "key": collection("first"), "collection": collection("first")}])
    with pytest.raises(ValueError, match="unique library identities"):
        search._engine_shared_keyword(bad)
    with pytest.raises(RuntimeError, match="collection is missing"):
        search._engine_shared_keyword(shared_request(database, ["second"], ["needle"]))
    (wrapper / "package.json").write_text('{"type":"module","version":"9.0.0"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="supported 2.8.3"):
        search._engine_shared_keyword(request)
