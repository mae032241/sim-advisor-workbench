import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.index_paths import PATH_SCHEMA, SHARED_KEYWORD_STORAGE, resolve_index_manifest, serialize_index_manifest
from advisor.library_search import LibrarySearch, _hash
from advisor.workspace_paths import WorkspacePaths

KEY = "lib_" + "a" * 20


def manifest():
    directory = "generations/0123456789abcdef0123-01234567"
    return {"path_schema": PATH_SCHEMA, "directory": directory, "generation": "opaque-generation",
            "corpus_hash": "unchanged", "libraries": {"library": {"key": KEY, "corpus": f"{directory}/{KEY}/corpus",
            "db_path": f"{directory}/{KEY}/qmd-keyword.sqlite3", "vector_db_path": f"{directory}/{KEY}/qmd-vector.sqlite3",
            "keyword_reused_from_generation": "older-generation", "keyword_reused_from_directory": "generations/older"}},
            "modes": {"qmd_vector": {"model_uri": "D:/external/models/model.gguf", "model_sha256": "f" * 64}},
            "source_uri": "file:///D:/external/source.html"}


def test_manifest_conversion_changes_only_owned_locators_and_is_repeatable(tmp_path):
    original = manifest()
    frozen = copy.deepcopy(original)
    runtime = resolve_index_manifest(original, tmp_path)
    assert original == frozen
    assert Path(runtime["directory"]).is_relative_to(tmp_path)
    assert runtime["modes"] == original["modes"] and runtime["source_uri"] == original["source_uri"]
    assert runtime["libraries"]["library"]["keyword_reused_from_generation"] == "older-generation"
    assert serialize_index_manifest(runtime, tmp_path) == original
    assert serialize_index_manifest(original, tmp_path) == original


def test_windows_legacy_paths_relocate_by_exact_layout_without_reading_old_drive(tmp_path):
    original = manifest()
    original.pop("path_schema")
    old_root = "Z:\\offline-old\\libraries\\search\\"
    original["directory"] = old_root + original["directory"].replace("/", "\\")
    for field in ("corpus", "db_path", "vector_db_path", "keyword_reused_from_directory"):
        original["libraries"]["library"][field] = old_root + original["libraries"]["library"][field].replace("/", "\\")
    runtime = resolve_index_manifest(original, tmp_path)
    assert str(tmp_path) in runtime["directory"] and "Z:" not in runtime["directory"]
    assert runtime["libraries"]["library"]["corpus"] == str(Path(runtime["directory"]) / KEY / "corpus")
    assert serialize_index_manifest(runtime, tmp_path)["path_schema"] == PATH_SCHEMA


@pytest.mark.parametrize("legacy", [False, True])
def test_shared_keyword_locators_relocate_and_round_trip_with_independent_vectors(tmp_path, legacy):
    value = manifest()
    value["keyword_storage"] = SHARED_KEYWORD_STORAGE
    item = value["libraries"]["library"]
    item.update(collection=KEY, db_path=value["directory"] + "/qmd-keyword.sqlite3")
    stored = copy.deepcopy(value)
    if legacy:
        value.pop("path_schema")
        value["directory"] = "Z:/old-search/" + value["directory"]
        for field in ("corpus", "db_path", "vector_db_path", "keyword_reused_from_directory"):
            item[field] = "Z:/old-search/" + item[field]
    runtime = resolve_index_manifest(value, tmp_path / "moved search")
    directory = Path(runtime["directory"])
    assert Path(runtime["libraries"]["library"]["db_path"]) == directory / "qmd-keyword.sqlite3"
    assert Path(runtime["libraries"]["library"]["vector_db_path"]) == directory / KEY / "qmd-vector.sqlite3"
    assert serialize_index_manifest(runtime, tmp_path / "moved search") == stored


@pytest.mark.parametrize("field,bad", [
    ("collection", "objects"),
    ("collection", "lib_" + "b" * 20),
    ("db_path", "generations/0123456789abcdef0123-01234567/" + KEY + "/qmd-keyword.sqlite3"),
    ("db_path", "generations/other/qmd-keyword.sqlite3"),
])
def test_shared_keyword_manifest_rejects_mismatched_collection_and_database(tmp_path, field, bad):
    value = manifest()
    value["keyword_storage"] = SHARED_KEYWORD_STORAGE
    item = value["libraries"]["library"]
    item.update(collection=KEY, db_path=value["directory"] + "/qmd-keyword.sqlite3")
    item[field] = bad
    with pytest.raises(ValueError):
        resolve_index_manifest(value, tmp_path)


def test_shared_keyword_layout_requires_explicit_supported_storage_marker(tmp_path):
    value = manifest()
    value["libraries"]["library"]["db_path"] = value["directory"] + "/qmd-keyword.sqlite3"
    with pytest.raises(ValueError, match="layout"):
        resolve_index_manifest(value, tmp_path)
    value["keyword_storage"] = "unknown-shared-layout"
    with pytest.raises(ValueError, match="keyword_storage"):
        resolve_index_manifest(value, tmp_path)


@pytest.mark.parametrize("field,bad", [
    ("directory", "../outside"),
    ("directory", "/outside/generations/valid"),
    ("directory", "generations/../valid"),
    ("corpus", "generations/other/" + KEY + "/corpus"),
    ("corpus", "generations/0123456789abcdef0123-01234567/" + KEY + "/../corpus"),
    ("db_path", "D:/outside/qmd-keyword.sqlite3"),
    ("vector_db_path", "generations/0123456789abcdef0123-01234567/" + KEY + "/arbitrary.sqlite3"),
    ("keyword_reused_from_directory", "../generations/older"),
])
def test_relative_manifest_rejects_escape_and_mismatched_layout(tmp_path, field, bad):
    value = manifest()
    target = value if field == "directory" else value["libraries"]["library"]
    target[field] = bad
    with pytest.raises(ValueError):
        resolve_index_manifest(value, tmp_path)


def test_legacy_locator_must_match_same_old_root_not_just_basename(tmp_path):
    value = resolve_index_manifest(manifest(), tmp_path / "old")
    value["libraries"]["library"]["db_path"] = str(tmp_path / "another" / "generations" / Path(value["directory"]).name / KEY / "qmd-keyword.sqlite3")
    with pytest.raises(ValueError, match="layout"):
        resolve_index_manifest(value, tmp_path / "new")


def test_existing_symlink_cannot_redirect_resolved_index_outside_search_root(tmp_path):
    root, outside = tmp_path / "search", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "generations").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is not permitted on this host")
    with pytest.raises(ValueError, match="escapes"):
        resolve_index_manifest(manifest(), root)


@pytest.mark.parametrize("bad", [[], {"directory": 42}, {"libraries": []},
    {"directory": "generations/valid", "path_schema": PATH_SCHEMA, "libraries": {"x": None}},
    {"directory": "generations/valid", "path_schema": PATH_SCHEMA, "libraries": {"x": {"key": KEY}}}])
def test_malformed_locator_shapes_raise_valueerror(bad, tmp_path):
    with pytest.raises(ValueError):
        resolve_index_manifest(bad, tmp_path)


@pytest.mark.parametrize("anchor", ["workspace", "legacy-index"])
def test_moved_data_bound_hf_receipt_resolves_new_cache_without_rewriting_receipt(tmp_path, monkeypatch, anchor):
    a, b = tmp_path / "A", tmp_path / "B moved"
    cache = a / "cache/library-search"
    model = cache / "cache/qmd/models/embeddinggemma.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"GGUFportable-cached-model")
    sha = hashlib.sha256(model.read_bytes()).hexdigest()
    uri = "hf:original/embeddinggemma.gguf"
    item = {"uri": uri, "path": str(model), "sha256": sha, "size": model.stat().st_size, "mtime_ns": model.stat().st_mtime_ns}
    receipt = cache / ("embedding-" + _hash(uri)[:16] + ".json")
    receipt.write_text(json.dumps(item), encoding="utf-8")
    if anchor == "workspace":
        WorkspacePaths(a)
    else:
        active = a / "libraries/search/active.json"
        active.parent.mkdir(parents=True)
        active.write_text(json.dumps({"directory": str(active.parent / "generations/old-generation")}), encoding="utf-8")
    shutil.copytree(a, b)
    a.rename(tmp_path / "A offline")
    new_cache = b / "cache/library-search"
    new_receipt = new_cache / receipt.name
    before = new_receipt.read_bytes()
    from test_library_search import enable_test_embedding
    enable_test_embedding(b / "libraries/search")
    search = LibrarySearch(b / "libraries/search", embed_model=uri, cache_dir=new_cache)
    monkeypatch.setattr(search, "_engine", lambda *_args, **_kwargs: pytest.fail("Relocating a receipt must not pull or embed"))
    result = search.prepare_embedding()
    assert result["available"] and result["sha256"] == sha
    assert result["path"] == str(b / model.relative_to(a))
    assert new_receipt.read_bytes() == before


def test_external_hf_receipt_model_is_not_relocated_with_workspace(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    cache = a / "cache/library-search"
    cache.mkdir(parents=True)
    external = tmp_path / "external model.gguf"
    external.write_bytes(b"GGUFexternal-model")
    uri = "hf:original/external-model.gguf"
    item = {"uri": uri, "path": str(external), "sha256": hashlib.sha256(external.read_bytes()).hexdigest(),
            "size": external.stat().st_size, "mtime_ns": external.stat().st_mtime_ns}
    (cache / ("embedding-" + _hash(uri)[:16] + ".json")).write_text(json.dumps(item), encoding="utf-8")
    WorkspacePaths(a)
    shutil.copytree(a, b)
    from test_library_search import enable_test_embedding
    enable_test_embedding(b / "libraries/search")
    result = LibrarySearch(b / "libraries/search", embed_model=uri, cache_dir=b / "cache/library-search")._model_info()
    assert result["path"] == str(external) and result["available"]
