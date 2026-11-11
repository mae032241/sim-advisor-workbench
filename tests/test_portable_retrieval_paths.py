"""External dependency paths must survive installing the workbench elsewhere."""
from pathlib import Path
import sys
import json
import shutil
import subprocess
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.library_search import LibrarySearch
from advisor.library_search import _NODE_BRIDGE, BRIDGE_MARKER
from advisor import retrieval_backends


def test_external_qmd_package_and_node_are_shared_by_both_search_paths(tmp_path, monkeypatch):
    package = tmp_path / "external tools" / "qmd"
    cli = package / "dist" / "cli" / "qmd.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("// dependency-location fixture", encoding="utf-8")
    node = tmp_path / "node runtime"
    node.write_text("fixture", encoding="utf-8")
    cache = tmp_path / "user cache"
    monkeypatch.setenv("SIM_ADVISOR_QMD_DIR", str(package))
    monkeypatch.setenv("SIM_ADVISOR_NODE", str(node))
    monkeypatch.setenv("SIM_ADVISOR_SEARCH_CACHE", str(cache))
    monkeypatch.delenv("SIM_ADVISOR_QMD", raising=False)
    search = LibrarySearch(tmp_path / "data")
    assert search.qmd_dir == package.resolve()
    assert search.node == str(node.resolve())
    assert search.cache_dir == cache.resolve()
    assert retrieval_backends.qmd_command() == [str(node.resolve()), str(cli.resolve())]
    assert not (package / "cache").exists()


def test_explicit_missing_node_does_not_use_another_node_for_library_vectors(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_ADVISOR_NODE", str(tmp_path / "missing-node"))
    monkeypatch.setattr("advisor.library_search.shutil.which", lambda _: "unexpected-node")
    assert LibrarySearch(tmp_path / "data").node is None


def test_pageindex_python_can_live_outside_package_without_silent_fallback(tmp_path, monkeypatch):
    interpreter = tmp_path / "external python"
    interpreter.write_text("fixture", encoding="utf-8")
    monkeypatch.setenv("SIM_ADVISOR_PAGEINDEX_PYTHON", str(interpreter))
    monkeypatch.setattr(retrieval_backends, "_pageindex_sdk_available", lambda *_: True)
    assert retrieval_backends._pageindex_python() == interpreter.resolve()
    interpreter.unlink()
    assert retrieval_backends._pageindex_python() is None


def test_pageindex_is_not_installed_just_because_base_python_exists(tmp_path, monkeypatch):
    interpreter = tmp_path / "minimal python.exe"
    interpreter.write_text("fixture", encoding="utf-8")
    monkeypatch.setenv("SIM_ADVISOR_PAGEINDEX_PYTHON", str(interpreter))
    monkeypatch.setattr(retrieval_backends, "_pageindex_sdk_available", lambda *_: False)
    assert retrieval_backends._pageindex_python() is None


@pytest.mark.skipif(not shutil.which("node"), reason="optional Node runtime not installed")
def test_qmd_store_and_global_tokenizer_receive_same_local_offline_model(tmp_path):
    package = tmp_path / "relocated qmd"
    (package / "dist").mkdir(parents=True)
    (package / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    (package / "dist/index.js").write_text(
        "const tokenizerModelAtImport=process.env.QMD_EMBED_MODEL;"
        "export async function createStore(options) {"
        " if('config' in options)throw new Error('reading an index must use DB-only mode');"
        " const storeModel=process.env.QMD_EMBED_MODEL;const oldDbModel='D:/old/cache/old-model.gguf';"
        " let collectionPath='D:/old/search/corpus';let locatorUpdates=0;"
        " const db={prepare(sql){"
        "  if(sql==='SELECT path FROM store_collections WHERE name = ?')return {get(name){"
        "   if(name!=='objects')throw new Error('wrong collection');return {path:collectionPath};}};"
        "  if(sql==='UPDATE store_collections SET path = ? WHERE name = ?')return {run(path,name){"
        "   if(name!=='objects')throw new Error('wrong collection');collectionPath=path;locatorUpdates++;}};"
        "  throw new Error('unexpected SQL mutation: '+sql);}};"
        " return {internal:{db,llm:{async embed(query,options){"
        "  if(options.model!==storeModel||options.isQuery!==true)throw new Error('query embedding must use selected local model');"
        "  return {embedding:[1,0]};}},async searchVec(query,model,limit,collection){"
        "  if(tokenizerModelAtImport!==storeModel)throw new Error('tokenizer/store mismatch');"
        "  return [{model:storeModel,tokenizerModel:tokenizerModelAtImport,index_model_uri:model,query,"
        "   source:'vec',collection,collectionPath,locatorUpdates,oldDbModel,cwd:process.cwd()}];}},"
        " async searchVector(){throw new Error('original vector model identity was not supplied');},"
        " async update(){throw new Error('indexing is forbidden during relocation');},"
        " async embed(){throw new Error('re-embedding is forbidden during relocation');},async close(){} }; }", encoding="utf-8")
    (package / "dist/llm.js").write_text(
        "export function formatQueryForEmbedding(query,model){return query;}", encoding="utf-8")
    model = str(tmp_path / "offline model.gguf")
    request = {"op": "vector", "model_uri": model, "qmd_dir": str(package), "db_path": str(tmp_path / "index.sqlite"),
               "corpus": str(tmp_path / 'corpus'), "query": "thermal expansion", "limit": 1, "index_model_uri": "hf:original/embeddinggemma.gguf",
               "env": {"QMD_EMBED_MODEL": "hf:remote/default.gguf"}}
    result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", _NODE_BRIDGE],
                            input=json.dumps(request), cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout.split(BRIDGE_MARKER, 1)[1])
    assert output[0]["model"] == model
    assert output[0]["tokenizerModel"] == model
    assert output[0]["index_model_uri"] == request["index_model_uri"]
    assert output[0]["source"] == "vec" and output[0]["collection"] == "objects"
    assert output[0]["collectionPath"] == "corpus" and output[0]["locatorUpdates"] == 1
    assert Path(output[0]["cwd"]) == tmp_path
    assert output[0]["model"] != output[0]["oldDbModel"], "DB-only mode must not load a model locator from stored DB configuration"
