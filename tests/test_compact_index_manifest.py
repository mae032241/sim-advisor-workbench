import copy
import json
from pathlib import Path

import pytest

from advisor import index_manifest as manifests
from advisor import index_manifest_cache as cache


def legacy(root, generation="current", suffix="a"):
    directory = root / "generations" / ("1" * 20 + "-" + suffix * 8)
    directory.mkdir(parents=True)
    library = "lib_" + "2" * 20
    return {"generation": generation, "directory": str(directory), "ready": True,
            "object_count": 1, "modes": {"qmd_keyword": {"ready": True}},
            "libraries": {"cases": {"key": library, "corpus": str(directory / library / "corpus"),
                                     "db_path": str(directory / library / "qmd-keyword.sqlite3")}},
            "mapping": {library + "/o-" + "3" * 32 + ".md": "citation"},
            "representation": {"indexed_objects": 1, "indexed_characters": 25,
                               "objects": {"citation": {"indexed_characters": 25}}}}


def test_publication_has_one_small_authority_and_relocatable_root_pointers(tmp_path):
    root = tmp_path / "original"
    value = legacy(root)
    original = copy.deepcopy(value)
    manifests.publish_manifest(value["directory"], value, root)
    assert value == original
    pointer = json.loads((root / "active.json").read_text())
    assert set(pointer) == {"manifest_schema", "manifest", "generation"}
    assert pointer == json.loads((root / "last-build.json").read_text())
    stored = json.loads((Path(value["directory"]) / "manifest.json").read_text())
    assert "mapping" not in stored and "objects" not in stored["representation"]
    assert stored["representation"]["indexed_characters"] == 25
    assert stored["object_lookup"]["schema"] == manifests.LOOKUP_SCHEMA
    moved = tmp_path / "moved"
    root.rename(moved)
    loaded = cache.read_manifest(moved / "active.json")
    assert loaded == stored and loaded["generation"] == "current"
    assert "representation" not in cache.read_manifest(moved / "active.json", summary=True)


def test_retired_native_mode_does_not_invalidate_retained_qmd_generation(tmp_path):
    value = legacy(tmp_path)
    value["modes"] = {"native": {"ready": True, "backend": "sqlite-fts5"},
                      "qmd_keyword": {"ready": True, "backend": "qmd-shared-fts"},
                      "qmd_vector": {"ready": True, "embedding_records": {"cases": [{"chunks": 9}]}}}
    value["requested_modes"] = ["native", "qmd_keyword", "qmd_vector"]
    value["libraries"]["cases"]["vector_reused_from_directory"] = value["directory"]
    store_legacy(tmp_path, value)
    before = {path: path.read_bytes() for path in (tmp_path / "active.json", tmp_path / "last-build.json")}
    loaded = cache.read_manifest(tmp_path / "active.json")
    assert set(loaded["modes"]) == {"qmd_keyword", "qmd_vector"} and loaded["ready"]
    assert loaded["generation"] == value["generation"] and loaded["libraries"] == value["libraries"]
    assert loaded["modes"]["qmd_vector"] == value["modes"]["qmd_vector"]
    assert loaded["mapping"] == value["mapping"]
    assert loaded["requested_modes"] == ["qmd_keyword", "qmd_vector"]
    assert {path: path.read_bytes() for path in before} == before


def test_native_only_legacy_manifest_requests_qmd_without_claiming_readiness(tmp_path):
    value = legacy(tmp_path)
    value["modes"] = {"native": {"ready": True}}
    store_legacy(tmp_path, value)
    loaded = cache.read_manifest(tmp_path / "active.json", summary=True)
    assert "native" not in loaded["modes"] and loaded["ready"] is False
    assert loaded["modes"]["qmd_keyword"]["state"] == "not_built"
    assert "QMD keyword index is required" in loaded["modes"]["qmd_keyword"]["error"]


def test_pointer_cache_checks_authority_replacement_and_deletion(tmp_path):
    value = legacy(tmp_path)
    manifests.publish_manifest(value["directory"], value, tmp_path)
    path = tmp_path / "active.json"
    assert cache.read_manifest(path, summary=True)["ready"]
    authority = Path(value["directory"]) / "manifest.json"
    changed = json.loads(authority.read_text())
    changed["ready"] = False
    manifests._write_json(authority, changed)
    assert cache.read_manifest(path, summary=True)["ready"] is False
    authority.unlink()
    assert cache.read_manifest(path) is None
    assert cache.read_manifest(path, summary=True) is None


@pytest.mark.parametrize("locator", ["../elsewhere/manifest.json", "/generations/x/manifest.json",
                                      "generations/../manifest.json", "https://example.com/manifest.json",
                                      "active.json", "generations/x/last-build.json"])
def test_pointer_targets_fail_closed(tmp_path, locator):
    path = tmp_path / "active.json"
    path.write_text(json.dumps({"manifest_schema": manifests.POINTER_SCHEMA,
                                "manifest": locator, "generation": "current"}))
    with pytest.raises(ValueError, match="must name"):
        cache.read_manifest(path, summary=True)


@pytest.mark.parametrize("case", ["chain", "identity", "directory"])
def test_pointer_authority_must_match_generation_and_owner(tmp_path, case):
    value = legacy(tmp_path)
    manifests.publish_manifest(value["directory"], value, tmp_path)
    authority = Path(value["directory"]) / "manifest.json"
    stored = json.loads(authority.read_text())
    if case == "chain":
        stored = json.loads((tmp_path / "active.json").read_text())
    elif case == "identity":
        stored["generation"] = "unrelated"
    else:
        stored["directory"] = "generations/unrelated"
        stored["libraries"] = {}
    manifests._write_json(authority, stored)
    with pytest.raises(ValueError):
        cache.read_manifest(tmp_path / "active.json")


def test_publication_interruptions_keep_visible_pointers_complete(tmp_path):
    first = legacy(tmp_path, "first")
    manifests.publish_manifest(first["directory"], first, tmp_path)
    second = legacy(tmp_path, "second", "b")
    writes = []

    def interrupted(path, value, *, cancellable=True):
        writes.append(Path(path).name)
        if Path(path).name == "last-build.json":
            raise OSError("simulated publication interruption")
        manifests._write_json(path, value, cancellable=cancellable)

    with pytest.raises(OSError, match="interruption"):
        manifests.publish_manifest(second["directory"], second, tmp_path, write_json=interrupted)
    assert writes == ["manifest.json", "active.json", "last-build.json"]
    assert cache.read_manifest(tmp_path / "active.json")["generation"] == "second"
    assert cache.read_manifest(tmp_path / "last-build.json")["generation"] == "first"


def store_legacy(root, value):
    raw = json.dumps(value)
    for path in (root / "active.json", root / "last-build.json", Path(value["directory"]) / "manifest.json"):
        path.write_text(raw)
    (Path(value["directory"]) / "objects.sqlite3").write_bytes(b"native fixture")


def test_migration_validates_lookup_before_replacing_json_and_preserves_provenance(tmp_path):
    value = legacy(tmp_path)
    store_legacy(tmp_path, value)
    called = []

    def validate(database, original):
        assert original["mapping"] == value["mapping"]
        assert json.loads((tmp_path / "active.json").read_text())["mapping"]
        called.append(database)
        return True

    result = manifests.compact_existing_index(tmp_path, ensure_lookup=validate)
    assert result == {"generations": 1, "pointers": 2}
    assert called == [Path(value["directory"]) / "objects.sqlite3"]
    loaded = cache.read_manifest(tmp_path / "active.json")
    assert loaded["generation"] == value["generation"]
    assert loaded["libraries"]["cases"]["key"] == value["libraries"]["cases"]["key"]
    assert (Path(value["directory"]) / "objects.sqlite3").read_bytes() == b"native fixture"
    assert manifests.compact_existing_index(tmp_path, ensure_lookup=lambda *_: True) == result


@pytest.mark.parametrize("failure", ["lookup", "divergent"])
def test_migration_refuses_incomplete_lookup_or_divergent_alias_without_json_writes(tmp_path, failure):
    value = legacy(tmp_path)
    store_legacy(tmp_path, value)
    if failure == "divergent":
        divergent = dict(value, ready=False)
        (tmp_path / "last-build.json").write_text(json.dumps(divergent))
    paths = [tmp_path / "active.json", tmp_path / "last-build.json", Path(value["directory"]) / "manifest.json"]
    before = {path: path.read_bytes() for path in paths}
    with pytest.raises(ValueError):
        manifests.compact_existing_index(tmp_path, ensure_lookup=lambda *_: failure != "lookup")
    assert {path: path.read_bytes() for path in paths} == before


def pointer_vectors(tmp_path):
    from test_unfinished_vectors import fixture

    store, partial, partial_manifest, complete = fixture(tmp_path)
    root = store.root / "search"
    complete_manifest = json.loads((complete / "manifest.json").read_text())
    manifests.publish_manifest(complete, complete_manifest, root, last_build=False)
    manifests.publish_manifest(partial, partial_manifest, root, active=False)
    return store, root, partial


def test_vector_cleanup_stamps_pointer_authority_and_updates_only_authority(tmp_path):
    store, root, partial = pointer_vectors(tmp_path)
    before = {name: (root / name).read_bytes() for name in ("active.json", "last-build.json")}
    preview = store.unfinished_vectors()
    assert preview["file_count"] == 4
    result = store.clean_unfinished_vectors(confirmed=True, token=preview["preview_token"])
    assert result["cleaned"] and result["embedding_invoked"] is False
    assert {name: (root / name).read_bytes() for name in before} == before
    assert cache.read_manifest(root / "last-build.json")["modes"]["qmd_vector"]["state"] == "cleaned"
    assert json.loads((partial / "manifest.json").read_text())["modes"]["qmd_keyword"]["ready"]


@pytest.mark.parametrize("changed", ["pointer", "authority"])
def test_vector_cleanup_rejects_changed_pointer_or_authority_without_deletion(tmp_path, changed):
    from advisor.library_store import LibraryLifecycleConflict
    from test_unfinished_vectors import hashes

    store, root, partial = pointer_vectors(tmp_path)
    preview = store.unfinished_vectors()
    if changed == "pointer":
        (root / "last-build.json").write_bytes((root / "active.json").read_bytes())
    else:
        path = partial / "manifest.json"
        value = json.loads(path.read_text())
        value["modes"]["qmd_vector"]["state"] = "failed"
        manifests._write_json(path, value)
    before = hashes(tmp_path)
    with pytest.raises(LibraryLifecycleConflict, match="变化"):
        store.clean_unfinished_vectors(confirmed=True, token=preview["preview_token"])
    assert hashes(tmp_path) == before
