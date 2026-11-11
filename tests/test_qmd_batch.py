"""One real Node process handles all selected QMD query stores, without reranking."""
import json
from pathlib import Path
import shutil

import pytest

from advisor import library_search as module
from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.index_manifest import publish_manifest
from advisor.library_search import LibrarySearch
from advisor import native_index
from test_library_search import enable_test_embedding, obj


NODE = shutil.which("node")

FAKE_SDK = r"""
import {readFileSync,appendFileSync} from 'node:fs';
const trace=(event,extra={})=>appendFileSync(process.env.QMD_BATCH_TRACE,JSON.stringify({event,pid:process.pid,...extra})+'\n');
trace('import');
export async function createStore(options){
  const spec=JSON.parse(readFileSync(options.dbPath,'utf8'));
  const paths=new Set();
  trace('open',{db:options.dbPath,options});
  const hits=()=>spec.hits.filter(hit=>!paths.size||paths.has(hit.filepath.split('/').at(-1)));
  const db={
    exec:()=>{},transaction:fn=>()=>fn(),
    prepare(sql){
      return {
        get:()=>({path:'corpus'}),
        run:path=>{if(sql.includes('INSERT OR IGNORE INTO temp.query_scope_paths'))paths.add(path);},
        all:()=>sql.includes('PRAGMA main.table_info')?
          ['id','collection','path','hash','title','active','doc','filepath','body'].map(name=>({name})):
          hits().map((hit,id)=>({id:id+1,path:hit.filepath.split('/').at(-1),title:hit.title,hash:'hash-'+id}))
      };
    }
  };
  return {
    internal:{db,llm:{embed:async(query,options)=>{trace('embed',{db:options.dbPath,model:options.model});return {embedding:[1,0]};}},
      searchVec:async(query,identity,limit)=>{trace('vector',{db:options.dbPath,identity});return hits().slice(0,limit).map(hit=>({...hit,source:'vec'}));}},
    searchLex:async(query,options)=>{
      trace('keyword',{db:options.dbPath,query,limit:options.limit,paths:[...paths]});
      if(spec.fail)throw new Error('injected library failure');
      return hits().slice(0,options.limit);
    },
    close:async()=>{trace('close',{db:options.dbPath});}
  };
}
"""


def sdk_fixture(tmp_path, *, embedding=False):
    if not NODE:
        pytest.skip("Node is required for the real process transport test")
    sdk = tmp_path / "sdk"
    (sdk / "dist").mkdir(parents=True)
    (sdk / "package.json").write_text(json.dumps({"type": "module", "version": "2.8.3"}), encoding="utf-8")
    (sdk / "dist/index.js").write_text(FAKE_SDK, encoding="utf-8")
    (sdk / "dist/llm.js").write_text("export const formatQueryForEmbedding=(query,model)=>query;", encoding="utf-8")
    if embedding:
        enable_test_embedding(tmp_path / "search")
    search = LibrarySearch(tmp_path / "search", qmd_dir=sdk, node=NODE)
    trace = tmp_path / "trace.jsonl"
    environment = search._env
    search._env = lambda: {**environment(), "QMD_BATCH_TRACE": str(trace)}
    return search, trace


def query_request(tmp_path, library, *, op="keyword", hits=None, fail=False):
    directory = tmp_path / library
    directory.mkdir(parents=True)
    database = directory / "qmd-keyword.sqlite3"
    rows = hits or [{"filepath": f"qmd://objects/{library}.md", "score": .8,
                     "source": "fts", "body": "Unneeded SDK body", "title": library, "chunkPos": 11}]
    database.write_text(json.dumps({"hits": rows, "fail": fail}), encoding="utf-8")
    return {"op": op, "library_key": library, "db_path": str(database), "corpus": str(directory / "corpus"),
            "query": "hmax", "queries": ["hmax"], "limit": 5}


def events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def compact(row):
    return {key: row[key] for key in ("filepath", "score", "source", "chunkPos") if key in row}


def test_real_node_batch_imports_sdk_once_closes_every_store_and_preserves_single_results(tmp_path):
    search, trace = sdk_fixture(tmp_path)
    requests = [query_request(tmp_path, library) for library in ("first", "second", "third")]
    expected = [[compact(row) for row in search._engine(request)] for request in requests]
    trace.unlink()
    response = search._engine_batch(requests)
    assert [item["results"] for item in response] == expected
    assert [item["library_key"] for item in response] == [item["library_key"] for item in requests]
    recorded = events(trace)
    assert len({item["pid"] for item in recorded}) == 1
    assert sum(item["event"] == "import" for item in recorded) == 1
    assert sum(item["event"] == "open" for item in recorded) == 3
    assert sum(item["event"] == "close" for item in recorded) == 3
    assert all(set(row) == {"filepath", "score", "source", "chunkPos"} for item in response for row in item["results"])
    # Single-operation compatibility still includes the SDK's complete row.
    assert "body" in search._engine(requests[0])[0]


def test_mid_batch_library_failure_closes_opened_stores_and_fails_entire_query(tmp_path):
    search, trace = sdk_fixture(tmp_path)
    requests = [query_request(tmp_path, "first"), query_request(tmp_path, "broken", fail=True), query_request(tmp_path, "third")]
    with pytest.raises(RuntimeError, match="injected library failure"):
        search._engine_batch(requests)
    recorded = events(trace)
    assert [item["db"] for item in recorded if item["event"] == "open"] == [item["db_path"] for item in requests[:2]]
    assert [item["db"] for item in recorded if item["event"] == "close"] == [item["db_path"] for item in requests[:2]]


def test_vector_batch_uses_verified_local_model_and_preserves_per_library_index_identity(tmp_path):
    search, trace = sdk_fixture(tmp_path, embedding=True)
    requests = [query_request(tmp_path, library, op="vector") for library in ("first", "second")]
    for request in requests:
        request["index_model_uri"] = "prior-index-model-" + request["library_key"]
    expected = [[compact(row) for row in search._engine(request)] for request in requests]
    trace.unlink()
    actual = search._engine_batch(requests)
    assert [item["results"] for item in actual] == expected
    recorded = events(trace)
    assert len({item["pid"] for item in recorded}) == 1
    assert {item["model"] for item in recorded if item["event"] == "embed"} == {str(Path(search.model_uri).resolve())}
    assert [item["identity"] for item in recorded if item["event"] == "vector"] == [item["index_model_uri"] for item in requests]


def test_vector_batch_cannot_bypass_disabled_embedding_or_create_missing_database(tmp_path, monkeypatch):
    search, _ = sdk_fixture(tmp_path)
    request = query_request(tmp_path, "first", op="vector")
    monkeypatch.setattr(module, "run_text", lambda *args, **kwargs: pytest.fail("Rejected batches must not start Node"))
    with pytest.raises(ValueError, match="disabled"):
        search._engine_batch([request])
    Path(request["db_path"]).unlink()
    with pytest.raises(ValueError, match="existing database"):
        search._engine_batch([dict(request, op="keyword")])
    assert not Path(request["db_path"]).exists()


@pytest.mark.parametrize("alter", [
    lambda requests: [],
    lambda requests: [{**requests[0], "op": "update"}],
    lambda requests: [requests[0], requests[0]],
    lambda requests: [{**requests[0], "env": {}}],
])
def test_batch_rejects_empty_mutating_duplicate_or_runtime_override_requests(tmp_path, monkeypatch, alter):
    search, _ = sdk_fixture(tmp_path)
    request = query_request(tmp_path, "first")
    monkeypatch.setattr(module, "run_text", lambda *args, **kwargs: pytest.fail("Invalid batches must not start Node"))
    with pytest.raises(ValueError):
        search._engine_batch(alter([request]))


@pytest.mark.parametrize("response", [[], [{"library_key": "unknown", "results": []}],
                                       [{"library_key": "first", "results": []}, {"library_key": "extra", "results": []}],
                                       [{"library_key": "first", "results": {}}]])
def test_batch_rejects_missing_extraneous_or_malformed_library_responses(tmp_path, monkeypatch, response):
    search, _ = sdk_fixture(tmp_path)
    request = query_request(tmp_path, "first")
    monkeypatch.setattr(search, "_engine", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError, match="batch"):
        search._engine_batch([request])


def search_fixture(tmp_path):
    search, trace = sdk_fixture(tmp_path)
    objects = []
    for library in ("first", "second"):
        for branch in ("geometry", "mesh"):
            item = obj(library + "-" + branch, library, "Saved hmax keyword text")
            item["data"]["feature_branch"] = branch
            objects.append(item)
    search.build(objects, modes=["native"], generation="batch-fixture")
    manifest = search._load_active()
    # Deliberately construct the supported historical keyword layout. New
    # constructors publish shared collections; this fixture covers old indexes.
    manifest.pop('keyword_storage', None)
    for library, item in manifest["libraries"].items():
        item.pop('collection', None)
        item['db_path'] = str(Path(manifest['directory']) / item['key'] / 'qmd-keyword.sqlite3')
        database = Path(item["db_path"])
        database.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"filepath": "qmd://objects/" + native_index.qmd_path(obj).split("/", 1)[1], "score": .8,
                 "source": "fts", "body": "large SDK body", "title": obj["title"]}
                for obj in objects if obj["library_id"] == library]
        database.write_text(json.dumps({"hits": rows}), encoding="utf-8")
    manifest["modes"]["qmd_keyword"] = {"ready": True, "state": "ready"}
    publish_manifest(Path(manifest["directory"]), manifest, search.data_dir)
    return search, trace, objects


@pytest.mark.parametrize("requested", [None, ["second"]])
def test_public_search_spawns_one_batch_even_for_one_library_and_keeps_stable_ranking(tmp_path, monkeypatch, requested):
    search, trace, objects = search_fixture(tmp_path)
    calls = []
    original = module.run_text
    def observe(*args, **kwargs):
        calls.append((json.loads(kwargs["input_text"]), kwargs["timeout"]))
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "run_text", observe)
    hits = search.search("hmax", mode="qmd_keyword", library_ids=requested, limit=4)
    assert len(calls) == 1 and calls[0][0]["op"] == "batch" and calls[0][1] == 180
    assert [item["library_key"] for item in calls[0][0]["requests"]] == (requested or ["first", "second"])
    selected = [item for item in objects if requested is None or item["library_id"] in requested]
    expected = sorted(selected, key=lambda item: 0 if item["data"]["feature_branch"] == "geometry" else 1)
    assert [item["id"] for item in hits] == [item["id"] for item in expected]
    assert [item["retrieval"]["score"] for item in hits] == [2 / (61 + (index // len(requested or ["first", "second"]))) for index in range(len(hits))]
    assert len({item["pid"] for item in events(trace)}) == 1


def test_scoped_batch_keeps_independent_allowed_paths_before_candidate_limit(tmp_path, monkeypatch):
    search, _, objects = search_fixture(tmp_path)
    calls = []
    original = module.run_text
    def observe(*args, **kwargs):
        calls.append(json.loads(kwargs["input_text"]))
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "run_text", observe)
    hits = search.search("hmax", mode="qmd_keyword", feature_branch="mesh", limit=1)
    assert len(calls) == 1
    requests = calls[0]["requests"]
    assert len(requests) == 2
    for request in requests:
        eligible = next(obj for obj in objects if obj["library_id"] == request["library_key"] and obj["data"]["feature_branch"] == "mesh")
        assert request["allowed_paths"] == [native_index.qmd_path(eligible).split("/", 1)[1]]
    assert hits[0]["data"]["feature_branch"] == "mesh"


def test_post_batch_visibility_and_cancellation_still_fail_closed(tmp_path, monkeypatch):
    search, _, _ = search_fixture(tmp_path)
    calls = [0]
    def visibility(_requested):
        calls[0] += 1
        return {"first", "second"} if calls[0] == 1 else {"first"}
    monkeypatch.setattr(search, "_searchable_library_ids", visibility)
    assert {item["library_id"] for item in search.search("hmax", mode="qmd_keyword")} == {"first"}
    request = query_request(tmp_path, "cancelled")
    marker = tmp_path / "cancel"
    def cancelled(*args, **kwargs):
        marker.touch()
        return [{"library_key": "cancelled", "results": []}]
    monkeypatch.setattr(search, "_engine", cancelled)
    with build_control(BuildControl(marker)):
        with pytest.raises(BuildCancelled):
            search._engine_batch([request])
