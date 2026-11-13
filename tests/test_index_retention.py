import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor import index_retention
from advisor.build_control import BuildCancelled
from advisor.index_paths import serialize_index_manifest
from advisor.library_search import LibrarySearch
from test_library_search import FakeEngineSearch, obj


def history(root, count=2):
    search = FakeEngineSearch(root, preserve_history=True)
    manifests = [search.build([obj(str(number))], modes=["qmd_keyword"], generation="g" + str(number))
                 for number in range(count)]
    for manifest in manifests:
        os.utime(manifest["directory"], (time.time() - 86400, time.time() - 86400))
    return search, manifests


def store(search, manifest, *, pointers=False):
    value = json.dumps(serialize_index_manifest(manifest, search.data_dir))
    (Path(manifest["directory"]) / "manifest.json").write_text(value, encoding="utf-8")
    if pointers:
        search.active_path.write_text(value, encoding="utf-8")
        search.last_build_path.write_text(value, encoding="utf-8")


def prune(search, current):
    return index_retention.prune_index_generations(search.data_dir, current_directory=current["directory"])


def test_successful_builds_remove_only_unused_search_generations(tmp_path, monkeypatch):
    root = tmp_path / "libraries/search"
    protected = [tmp_path / "source.mph", root.parent / "catalog.sqlite3",
                 root.parent / "blobs/evidence.json", root.parent / "artifacts/extraction.json"]
    for path in protected:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".sqlite3":
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE libraries(id TEXT, body TEXT)")
                db.execute("INSERT INTO libraries VALUES(?,?)", ("cases", json.dumps({"id": "cases"})))
        else:
            path.write_bytes(b"keep this evidence")
    before = {path: path.read_bytes() for path in protected}
    search = LibrarySearch(root)
    first = search.build([obj("first")], modes=["qmd_keyword"], generation="first")
    size = sum(path.stat().st_size for path in Path(first["directory"]).rglob("*") if path.is_file())
    original = index_retention.prune_index_generations
    def locked(*args, **kwargs):
        assert search.lock.locked() and (root / "build-lease.json").exists()
        return original(*args, **kwargs)
    monkeypatch.setattr(index_retention, "prune_index_generations", locked)
    second = search.build([obj("second")], modes=["qmd_keyword"], generation="second")
    assert second["retention"]["retained_recent_generations"] == 1
    assert Path(first["directory"]).exists()
    os.utime(first["directory"], (time.time() - 86400, time.time() - 86400))
    third = search.build([obj("third")], modes=["qmd_keyword"], generation="third")
    assert third["retention"]["state"] == "completed"
    assert third["retention"]["removed_generations"] == 1
    assert third["retention"]["removed_bytes"] == size
    assert not Path(first["directory"]).exists()
    assert Path(second["directory"]).is_dir()
    assert search.search("valve")[0]["id"] == "lo-third"
    assert all(path.read_bytes() == before[path] for path in protected)


def test_completed_noop_can_reclaim_legacy_history_without_rebuilding(tmp_path):
    _, (first, second) = history(tmp_path)
    search = FakeEngineSearch(tmp_path)
    result = search.build([obj("1")], modes=["qmd_keyword"], generation="g1")
    assert result["unchanged"] and search.calls == []
    assert result["retention"]["removed_generations"] == 1
    assert not Path(first["directory"]).exists() and Path(second["directory"]).exists()


def test_retention_accepts_shared_keyword_database_and_counts_each_physical_file_once(tmp_path):
    search = LibrarySearch(tmp_path, preserve_history=True)
    old = search.build([obj('a', 'first'), obj('b', 'second')], modes=['qmd_keyword'], generation='old')
    directory = Path(old['directory'])
    old['keyword_storage'] = 'shared-collections-v1'
    for item in old['libraries'].values():
        item.update(db_path=str(directory / 'qmd-keyword.sqlite3'), collection=item['key'])
    for suffix in ('', '-wal', '-shm', '-journal'):
        (directory / ('qmd-keyword.sqlite3' + suffix)).write_bytes(b'owned shared keyword bytes' * 7)
    store(search, old)
    size = sum(path.stat().st_size for path in directory.rglob('*') if path.is_file())
    current = search.build([obj('current')], modes=['qmd_keyword'], generation='current')
    os.utime(directory, (time.time() - 86400, time.time() - 86400))
    result = prune(search, current)
    assert result['state'] == 'completed' and result['removed_generations'] == 1
    assert result['removed_bytes'] == size and not directory.exists()


def test_retention_accepts_immutable_context_dependencies_and_refuses_similar_unowned_names(tmp_path):
    from advisor.case_context_dependencies import mark_generation_dependencies
    search, (old, current) = history(tmp_path)
    old = mark_generation_dependencies(tmp_path, old, {"case-0": {
        "case_id": "case-0", "library_id": "models",
        "qmd_path": "lib_" + "a" * 20 + "/case-0.md",
        "source_refs": [{"id": "source-0", "library_id": "manuals"}]}})
    store(search, old)
    directory = Path(old["directory"])
    similar = directory / "case-context-dependencies-unowned.sqlite3"
    similar.write_bytes(b"do not infer snapshot ownership from a prefix")
    os.utime(directory, (time.time() - 86400, time.time() - 86400))
    result = prune(search, current)
    assert result["state"] == "partial" and result["removed_generations"] == 0
    assert similar.exists() and directory.exists()
    similar.unlink()
    os.utime(directory, (time.time() - 86400, time.time() - 86400))
    size = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
    result = prune(search, current)
    assert result["state"] == "completed" and result["removed_generations"] == 1
    assert result["removed_bytes"] == size and not directory.exists()


def test_explicit_directory_aliases_are_protected_transitively(tmp_path):
    search, manifests = history(tmp_path, 4)
    unused, base, alias, current = manifests
    alias["libraries"]["cases"]["keyword_reused_from_directory"] = base["directory"]
    base["libraries"]["cases"]["vector_reused_from_directory"] = alias["directory"]
    current["libraries"]["cases"]["vector_reused_from_directory"] = alias["directory"]
    for manifest in (base, alias, current):
        store(search, manifest, pointers=manifest is current)
    result = prune(search, current)
    assert result["removed_generations"] == 1
    assert not Path(unused["directory"]).exists()
    assert all(Path(item["directory"]).exists() for item in (base, alias, current))


def test_last_build_pointer_is_an_independent_protection_root(tmp_path):
    search, (first, second) = history(tmp_path)
    search.last_build_path.write_text(json.dumps(serialize_index_manifest(first, tmp_path)), encoding="utf-8")
    result = prune(search, second)
    assert result["state"] == "completed" and result["removed_generations"] == 0
    assert Path(first["directory"]).exists()


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_failed_or_cancelled_build_does_not_prune_retained_indexes(tmp_path, monkeypatch, outcome):
    search = FakeEngineSearch(tmp_path)
    first = search.build([obj("first")], modes=["qmd_keyword"], generation="first")
    def unexpected(*args, **kwargs):
        pytest.fail("Incomplete builds must not invoke retention")
    monkeypatch.setattr(index_retention, "prune_index_generations", unexpected)
    if outcome == "failed":
        search.fail_embed = True
        result = search.build([obj("second")], modes=["qmd_keyword", "qmd_vector"], generation="second")
        assert result["modes"]["qmd_keyword"]["ready"] and not result["modes"]["qmd_vector"]["ready"]
    else:
        def progress(value):
            if value["stage"] == "indexing_objects" and value["progress"]["completed"]:
                raise BuildCancelled()
        with pytest.raises(BuildCancelled):
            search.build([obj("second")], modes=["qmd_keyword"], generation="second", on_progress=progress)
    assert Path(first["directory"]).exists()
    assert len(list((tmp_path / "generations").iterdir())) == 2


@pytest.mark.parametrize("which", ["pointer", "generation", "wrong_owner"])
def test_malformed_manifests_skip_cleanup_conservatively(tmp_path, which):
    search, (first, second) = history(tmp_path)
    path = search.last_build_path if which == "pointer" else Path(first["directory"]) / "manifest.json"
    if which == "wrong_owner":
        path.write_text(json.dumps(serialize_index_manifest(second, tmp_path)), encoding="utf-8")
    else:
        path.write_text("{broken", encoding="utf-8")
    result = prune(search, second)
    assert result["state"] == "skipped" and result["warnings"]
    assert Path(first["directory"]).exists()


def test_unknown_entries_and_their_alias_dependencies_are_retained(tmp_path):
    search, (dependency, unknown, current) = history(tmp_path, 3)
    unknown["libraries"]["cases"]["keyword_reused_from_directory"] = dependency["directory"]
    store(search, unknown)
    source = Path(unknown["directory"]) / "unrecognized.mph"
    source.write_bytes(b"do not guess ownership")
    os.utime(unknown["directory"], (time.time() - 86400, time.time() - 86400))
    extra = tmp_path / "generations/user-folder"
    extra.mkdir()
    result = prune(search, current)
    assert result["state"] == "partial" and result["removed_generations"] == 0
    assert len(result["warnings"]) == 2
    assert source.read_bytes() == b"do not guess ownership" and extra.is_dir()
    assert Path(dependency["directory"]).exists()


def test_junction_is_refused_before_descending(tmp_path, monkeypatch):
    search, (old, current) = history(tmp_path)
    blocked = Path(old["libraries"]["cases"]["corpus"])
    blocked.mkdir(parents=True, exist_ok=True)
    os.utime(old["directory"], (time.time() - 86400, time.time() - 86400))
    original = Path.iterdir
    lstat = Path.lstat
    def never_descend(path):
        assert path != blocked, "Retention must not traverse a junction"
        return original(path)
    def junction(path):
        info = lstat(path)
        if path == blocked:
            return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return info
    monkeypatch.setattr(Path, "lstat", junction)
    monkeypatch.setattr(Path, "iterdir", never_descend)
    result = prune(search, current)
    assert result["warnings"] and result["removed_generations"] == 0
    assert Path(old["directory"]).exists()


def test_nested_symlink_cannot_delete_external_source(tmp_path):
    search, (old, current) = history(tmp_path / "search")
    external = tmp_path / "external"
    external.mkdir()
    (external / "source.mph").write_bytes(b"original")
    link = Path(old["libraries"]["cases"]["corpus"]) / "linked"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.utime(old["directory"], (time.time() - 86400, time.time() - 86400))
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are not permitted on this host")
    result = prune(search, current)
    assert result["warnings"] and result["removed_generations"] == 0
    assert (external / "source.mph").read_bytes() == b"original"


def test_gc_failure_is_nonfatal_and_does_not_claim_removal(tmp_path, monkeypatch):
    search, (old, _) = history(tmp_path)
    search.preserve_history = False
    def locked(*args, **kwargs):
        raise PermissionError("old index is locked")
    monkeypatch.setattr(index_retention.shutil, "rmtree", locked)
    current = search.build([obj("new")], modes=["qmd_keyword"], generation="new")
    assert current["modes"]["qmd_keyword"]["ready"]
    assert current["retention"]["state"] == "partial"
    assert current["retention"]["removed_generations"] == current["retention"]["removed_bytes"] == 0
    assert "old index is locked" in current["retention"]["warnings"][0]
    assert Path(old["directory"]).exists()
    assert search.search("valve")[0]["id"] == "lo-new"


def test_inflight_old_query_survives_two_rapid_publications(tmp_path):
    reader = FakeEngineSearch(tmp_path)
    old = reader.build([obj("old")], modes=["qmd_keyword"], generation="old")
    os.utime(old["directory"], (time.time() - 86400, time.time() - 86400))
    writer = LibrarySearch(tmp_path)
    receipts = []
    def publish(request):
        if request["op"] == "shared_keyword":
            for generation in ("middle", "new"):
                receipts.append(writer.build([obj(generation)], modes=["qmd_keyword"], generation=generation))
    reader.observer = publish
    hits = reader.search("valve", mode="qmd_keyword")
    assert hits[0]["id"] == "lo-old"
    assert Path(old["directory"]).exists()
    assert receipts[-1]["retention"]["retained_recent_generations"] == 2
    assert writer.search("valve")[0]["id"] == "lo-new"


def test_retirement_timestamp_failure_preserves_history_and_build_success(tmp_path, monkeypatch):
    search, (old, _) = history(tmp_path)
    search.preserve_history = False
    def denied(*args, **kwargs):
        raise PermissionError("cannot mark retirement")
    monkeypatch.setattr(index_retention.os, "utime", denied)
    current = search.build([obj("new")], modes=["qmd_keyword"], generation="new")
    assert current["ready"] and current["retention"]["state"] == "skipped"
    assert "cannot mark retirement" in current["retention_warnings"][0]
    assert Path(old["directory"]).exists()


def test_unexpected_retention_error_preserves_success_with_unknown_counts(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("maintenance failed")
    monkeypatch.setattr(index_retention, "prune_index_generations", broken)
    built = LibrarySearch(tmp_path).build([obj("new")], modes=["qmd_keyword"], generation="new")
    assert built["ready"] and built["retention"]["state"] == "failed"
    assert built["retention"]["removed_bytes"] is None
    assert "maintenance failed" in built["retention"]["warnings"][0]

@pytest.mark.parametrize('shared', [True, False])
def test_shared_reader_grace_is_one_query_deadline_not_one_per_library(tmp_path, shared):
    search=FakeEngineSearch(tmp_path, preserve_history=True)
    first=search.build([obj(str(i), 'library-'+str(i)) for i in range(50)],modes=['qmd_keyword'],generation='old-many')
    if not shared:
        first.pop('keyword_storage')
        for item in first['libraries'].values():
            item.pop('collection')
            item['db_path']=str(Path(first['directory'])/item['key']/'qmd-keyword.sqlite3')
        store(search,first,pointers=True)
    current=search.build([obj('new')],modes=['qmd_keyword'],generation='current')
    os.utime(first['directory'],(time.time()-215,time.time()-215))
    result=prune(search,current)
    assert result['state']=='completed'
    assert result['removed_generations']==int(shared)
    assert Path(first['directory']).exists() is (not shared)


def test_crashed_keyword_spools_are_owned_only_in_retired_generation_cleanup(tmp_path):
    search, (old, current) = history(tmp_path)
    for manifest in (old, current):
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(manifest["directory"]) / (".qmd-keyword-inputs-" + "c" * 32 + ".sqlite3" + suffix)
            path.write_bytes(b"temporary input")
    os.utime(old["directory"], (time.time() - 86400, time.time() - 86400))
    result = prune(search, current)
    assert result["removed_generations"] == 1 and not Path(old["directory"]).exists()
    assert len(list(Path(current["directory"]).glob(".qmd-keyword-inputs-*"))) == 4
