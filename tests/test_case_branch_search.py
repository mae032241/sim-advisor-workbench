"""Feature scopes use old stored object metadata without rebuilding vectors."""
import copy
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.library_search import BRIDGE_VERSION, FILTER_ADAPTER_VERSION, KEYWORD_ADAPTER_VERSION, LibrarySearch, _representation
from test_library_search import FakeEngineSearch, obj


def feature(key, branch, *, kind="code", library="cases", legacy=False, text="mesh sizing"):
    item = obj(key, library, text)
    item.update(kind=kind, type="implementation_snippet" if kind == "code" else "method_step")
    item["data"]["scope_hint" if legacy else "feature_branch"] = branch
    return item


def test_keyword_filters_branch_and_kind_before_limit_not_after_global_ranking(tmp_path):
    physics = [feature(f"physics-{i}", "physics", text="mesh sizing " * 12) for i in range(220)]
    mesh_code = feature("mesh-code", "mesh", text="mesh sizing " * 8)
    mesh_case = feature("mesh-case", "mesh", kind="case", text="mesh sizing controls tetrahedral elements")
    search = LibrarySearch(tmp_path)
    search.build(physics + [mesh_code, mesh_case], modes=["qmd_keyword"], generation="branch-keyword")
    assert search.search("mesh sizing", limit=1)[0]["id"] != mesh_case["id"]
    hits = search.search("mesh sizing", kind="case", feature_branch="mesh", limit=1)
    assert [hit["id"] for hit in hits] == [mesh_case["id"]]
    assert hits[0]["retrieval"]["search_scope"]["filter_stage"] == "before_retrieval"
    assert hits[0]["retrieval"]["search_scope"]["eligible_objects"] == 1


class ScopedEngineSpy(FakeEngineSearch):
    def _engine(self, request, *, timeout=120):
        return super()._engine(request, timeout=timeout)


@pytest.mark.parametrize("mode", ["qmd_keyword", "qmd_vector"])
def test_legacy_scope_and_absolute_active_work_across_libraries_without_build(tmp_path, mode):
    search = ScopedEngineSpy(tmp_path)
    objects = [feature(f"noise-{i}", "physics", library="large") for i in range(120)]
    target = feature("mesh-target", "mesh", library="small", legacy=True)
    geom = feature("geom-target", "geom", library="small", legacy=True)
    solver = feature("solver-target", "sol", library="small", legacy=True)
    built = search.build(objects + [target, geom, solver], modes=["qmd_keyword", "qmd_vector"], generation="legacy")
    # The historical manifest had absolute paths and no path_schema; the object
    # payloads already contain scope_hint, not the new feature metadata.
    search.active_path.write_text(json.dumps(built), encoding="utf-8")
    before = search.active_path.read_bytes()
    search.calls.clear()
    assert search.search("mesh", mode=mode, feature_branch="mesh", kind="code", limit=1)[0]["id"] == target["id"]
    assert search.search("mesh", mode=mode, feature_branch="geometry", kind="code", limit=1)[0]["id"] == geom["id"]
    assert search.search("mesh", mode=mode, feature_branch="solver", kind="code", limit=1)[0]["id"] == solver["id"]
    if mode == 'qmd_keyword':
        assert all(call['op'] == 'shared_keyword' for call in search.calls)
        assert all(len(call['collections']) == 1 and call['collections'][0]['library_key'] == 'small'
                   and 'allowed_paths' not in call['collections'][0]
                   and call['authoritative_scope']['kind'] == 'code' for call in search.calls)
    else:
        assert all(call["op"] == "vector" for call in search.calls)
        assert all(call["corpus"] == built["libraries"]["small"]["corpus"] for call in search.calls)
        assert all(len(call["allowed_paths"]) == 1 for call in search.calls)
    assert search.active_path.read_bytes() == before and built["bridge_version"] == BRIDGE_VERSION
    search.calls.clear()
    assert search.search("mesh", mode=mode, feature_branch="material") == []
    assert not search.calls, "An empty scope must not load a query model or search globally"


def test_scope_validation_does_not_silently_relax_invalid_filters(tmp_path):
    search = LibrarySearch(tmp_path)
    for branch in ("meshh", [], 1):
        with pytest.raises(ValueError):
            search.search("mesh", feature_branch=branch)
    with pytest.raises(ValueError):
        search.search("mesh", kind=[])


def test_feature_structure_survives_long_code_prefix_without_indexing_all_code():
    item = feature("mesh", "mesh", text="// long boilerplate\n" * 2000 + 'model.component("comp1").mesh("mesh1").create("ftet1","FreeTet");')
    item["data"].update(summary="Build a FreeTet mesh on the selected valve fluid domains", feature_type="FreeTet",
        api_object_paths=['model.component("comp1").mesh("mesh1").feature("ftet1")'], source_order=42,
        function={"name": "runMesh", "ordinal": 2}, execution_status="not_executed", standalone=False)
    text, stats = _representation(item)
    assert len(text) <= 1400 and "FreeTet" in text and 'feature_branch":"mesh' in text
    assert 'model.component(\\"comp1\\")' in text and "runMesh" in text
    assert "not_executed" in text and stats["feature_structure_included"] and stats["truncated"]


def test_actual_qmd_283_shared_keyword_scope_filters_before_topk_without_fts_copy(tmp_path):
    probe = LibrarySearch(tmp_path / "probe", cache_dir=tmp_path / "cache")
    if not probe.node or not (probe.qmd_dir / "dist/index.js").is_file():
        pytest.skip("Optional local QMD/Node integration dependencies unavailable")
    if probe._qmd_version() != "2.8.3":
        pytest.skip("This integration test requires the supported QMD 2.8.3 adapter")
    wrapper = tmp_path / "SDK wrapper"
    (wrapper / "dist").mkdir(parents=True)
    (wrapper / "package.json").write_text('{"type":"module","version":"2.8.3"}', encoding="utf-8")
    guard = tmp_path / "retrieval-only"
    source = """import {existsSync} from 'node:fs';
      globalThis.fetch=()=>{throw new Error('Network forbidden');};
      const sdk=await import(SDK);
      export async function createStore(options){
        const store=await sdk.createStore(options),update=store.update.bind(store);
        store.update=(...args)=>{if(existsSync(GUARD))throw new Error('Retrieval must not update');return update(...args);};
        store.embed=()=>{throw new Error('Document embedding forbidden');};
        store.internal.llm.embed=()=>{throw new Error('Keyword search must not query-embed');};
        return store;
      }""".replace("SDK", json.dumps((probe.qmd_dir / "dist/index.js").as_uri())).replace("GUARD", json.dumps(str(guard)))
    (wrapper / "dist/index.js").write_text(source, encoding="utf-8")
    (wrapper / 'dist/store.js').write_text('export * from ' + json.dumps((probe.qmd_dir / 'dist/store.js').as_uri()) + ';', encoding='utf-8')
    sqlite_proxy = wrapper / 'node_modules/better-sqlite3'
    sqlite_proxy.mkdir(parents=True)
    (sqlite_proxy / 'package.json').write_text('{"main":"index.cjs"}', encoding='utf-8')
    (sqlite_proxy / 'index.cjs').write_text("module.exports=require('node:module').createRequire(" +
        json.dumps(str(probe.qmd_dir / 'package.json')) + ")('better-sqlite3');", encoding='utf-8')
    search = LibrarySearch(tmp_path / "search", qmd_dir=wrapper, node=probe.node, cache_dir=tmp_path / "cache")
    objects = [feature(f"strong-{i}", "physics", text="mesh sizing " * 12) for i in range(180)]
    target = feature("weak-mesh", "mesh", text="mesh sizing", legacy=True)
    built = search.build(objects + [target], modes=["qmd_keyword"], generation="real-keyword")
    assert built["modes"]["qmd_keyword"]["ready"], built["modes"]["qmd_keyword"]
    guard.touch()
    db_path = built["libraries"]["cases"]["db_path"]

    def permanent_snapshot():
        with closing(sqlite3.connect(db_path)) as db:
            return {name: db.execute("SELECT * FROM " + name + " ORDER BY 1").fetchall()
                    for name in ("documents", "content", "content_vectors", "store_collections")}

    before = permanent_snapshot()
    assert search.search("mesh sizing", mode="qmd_keyword", limit=1)[0]["id"] != target["id"]
    hits = search.search("mesh sizing", mode="qmd_keyword", feature_branch="mesh", kind="code", limit=1)
    assert [hit["id"] for hit in hits] == [target["id"]]
    assert hits[0]["retrieval"]["search_scope"]["adapter"] == KEYWORD_ADAPTER_VERSION
    assert permanent_snapshot() == before
    # Unknown SDK versions may never silently fall back to unrestricted recall.
    (wrapper / "package.json").write_text('{"type":"module","version":"9.0.0"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="supported 2.8.3"):
        search.search("mesh sizing", mode="qmd_keyword", feature_branch="mesh", limit=1)


def test_actual_scoped_vectors_reject_mixed_model_or_fingerprint_before_query_embedding(tmp_path):
    """Numerical 2-D fixtures use real sqlite-vec; no model inference occurs."""
    from test_library_search import enable_test_embedding
    enable_test_embedding(tmp_path / "search")
    probe = LibrarySearch(tmp_path / "probe", cache_dir=tmp_path / "cache")
    if not probe.node or not (probe.qmd_dir / "dist/index.js").is_file() or probe._qmd_version() != "2.8.3":
        pytest.skip("Optional supported QMD/Node integration dependencies unavailable")
    wrapper, database = tmp_path / "SDK wrapper", tmp_path / "library/index.sqlite3"
    (wrapper / "dist").mkdir(parents=True)
    database.parent.mkdir()
    (database.parent / "corpus").mkdir()
    (wrapper / "package.json").write_text('{"type":"module","version":"2.8.3"}', encoding="utf-8")
    index_url = (probe.qmd_dir / "dist/index.js").as_uri()
    store_url = (probe.qmd_dir / "dist/store.js").as_uri()
    llm_url = (probe.qmd_dir / "dist/llm.js").as_uri()
    model, calls = "hf:fixture/embeddinggemma.gguf", tmp_path / "query-calls"
    setup = """import {createStore} from INDEX;import {getEmbeddingFingerprint} from STORE;
      const model=MODEL,now='2026-11-08T00:00:00Z';
      const s=await createStore({dbPath:DB,config:{collections:{objects:{path:'corpus',pattern:'**/*.md'}},models:{embed:model}}});
      try{
        s.internal.ensureVecTable(2);
        for(const [name,identity,fingerprint,vector] of [
          ['valid.md',model,getEmbeddingFingerprint(model),[0,1]],
          ['wrong-model.md','hf:other/embeddinggemma.gguf',getEmbeddingFingerprint(model),[1,0]],
          ['wrong-fp.md',model,'ffffff',[1,0]],['missing.md',model,'',null]]){
          const hash=name;s.internal.insertContent(hash,'numerical test '+name,now);
          s.internal.insertDocument('objects',name,name,hash,now,now);
          if(vector)s.internal.insertEmbedding(hash,0,0,new Float32Array(vector),identity,now,1,fingerprint);
        }
      }finally{await s.close();}
    """.replace("INDEX", json.dumps(index_url)).replace("STORE", json.dumps(store_url)).replace("MODEL", json.dumps(model)).replace("DB", json.dumps(str(database)))
    result = subprocess.run([probe.node, "--input-type=module", "-e", setup], cwd=database.parent,
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    source = """import {appendFileSync} from 'node:fs';import {createStore as original} from INDEX;
      globalThis.fetch=()=>{throw new Error('Network forbidden');};
      export async function createStore(options){
        if(options.config||options.configPath)throw new Error('Query must not sync configuration');
        const s=await original(options);
        s.update=s.embed=()=>{throw new Error('Document indexing/embedding forbidden');};
        s.internal.llm.embed=async(_text,opts)=>{if(opts.isQuery!==true)throw new Error('Document embedding forbidden');
            if(/^(hf:|https?:)/.test(opts.model))throw new Error('Remote query embedding model forbidden');
          appendFileSync(CALLS,'query\\n');return {embedding:[1,0],model:opts.model};};
        return s;
      }""".replace("INDEX", json.dumps(index_url)).replace("CALLS", json.dumps(str(calls)))
    (wrapper / "dist/index.js").write_text(source, encoding="utf-8")
    (wrapper / "dist/store.js").write_text("export {getEmbeddingFingerprint} from " + json.dumps(store_url) + ";", encoding="utf-8")
    (wrapper / "dist/llm.js").write_text("export {formatQueryForEmbedding} from " + json.dumps(llm_url) + ";", encoding="utf-8")
    search = LibrarySearch(tmp_path / "search", qmd_dir=wrapper, node=probe.node, cache_dir=tmp_path / "cache")
    request = {"op": "vector", "db_path": str(database), "corpus": str(database.parent / "corpus"),
               "query": "mesh", "index_model_uri": model, "allowed_paths": ["valid.md"], "limit": 1}

    def snapshot():
        with closing(sqlite3.connect(database)) as db:
            tables = ["documents", "content", "content_vectors", "store_collections"] + [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'vectors_vec_%' ORDER BY name")]
            return {name: db.execute('SELECT * FROM "' + name + '" ORDER BY 1').fetchall() for name in tables}

    before = snapshot()
    hits = search._engine(request)
    assert hits[0]["filepath"] == "qmd://objects/valid.md" and hits[0]["source"] == "vec"
    assert calls.read_text().splitlines() == ["query"]
    for paths in (["valid.md", "wrong-model.md"], ["wrong-fp.md"], ["missing.md"]):
        with pytest.raises(RuntimeError, match="model/fingerprint"):
            search._engine(dict(request, allowed_paths=paths))
        assert calls.read_text().splitlines() == ["query"], "Identity validation must precede query model work"
    assert snapshot() == before
