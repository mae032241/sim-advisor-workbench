"""Whole-corpus indexing stays streamed and hot metadata stays bounded."""
import copy
from contextlib import closing
from pathlib import Path

import pytest

from advisor import library_search as module
from advisor.build_control import BuildCancelled
from advisor.library_store import LibraryStore, encoded
from test_library_search import FakeEngineSearch, obj


def catalog(tmp_path, objects):
    store = LibraryStore(tmp_path / "catalog")
    with closing(store.connect()) as db, db:
        for item in objects:
            db.execute("INSERT INTO revisions VALUES(?,?,?,?)",
                       (item["id"], item["object_id"], item["library_id"], encoded(item)))
            db.execute("INSERT INTO active VALUES(?,?,?)", (item["library_id"], item["object_id"], item["id"]))
    return store


def test_catalog_stream_and_list_produce_identical_fingerprint_and_search_results(tmp_path):
    objects = [obj("z", "second"), obj("a", "first"), obj("m", "second")]
    before = copy.deepcopy(objects)
    store = catalog(tmp_path, objects)
    listed = FakeEngineSearch(tmp_path / "list" / "search")
    streamed = FakeEngineSearch(tmp_path / "stream" / "search")
    left = listed.build(objects, modes=["native", "qmd_keyword"], generation="same")
    with store.object_stream() as source:
        right = streamed.build(source, modes=["native", "qmd_keyword"], generation="same")
        assert not source._cursors
    assert left["corpus_hash"] == right["corpus_hash"]
    assert left["mapping"] == right["mapping"]
    assert left["representation"] == right["representation"]
    for mode in ("native", "qmd_keyword"):
        for library in ("first", "second"):
            a = listed.search("valve", mode=mode, library_ids=[library])
            b = streamed.search("valve", mode=mode, library_ids=[library])
            assert [(hit["id"], hit["text"], hit["data"], hit["retrieval"]["representation"]) for hit in a] == [
                (hit["id"], hit["text"], hit["data"], hit["retrieval"]["representation"]) for hit in b]
    assert objects == before


@pytest.mark.parametrize("phase", ["fingerprint", "export"])
def test_cancelled_index_pass_closes_cursor_before_catalog_snapshot_exits(tmp_path, monkeypatch, phase):
    objects = [obj(str(index).zfill(2)) for index in range(30)]
    store = catalog(tmp_path, objects)
    search = FakeEngineSearch(tmp_path / "index" / "search")
    if phase == "fingerprint":
        original = module._json

        def interrupt(item):
            if isinstance(item, dict) and item.get("id") == "lo-04":
                raise BuildCancelled()
            return original(item)

        monkeypatch.setattr(module, "_json", interrupt)

    def progress(event):
        if phase == "export" and event["progress"]["completed"] == 25:
            raise BuildCancelled()

    with store.object_stream() as source:
        with pytest.raises(BuildCancelled):
            search.build(source, modes=["native"], generation="cancelled", on_progress=progress)
        assert not source._cursors
        assert len(list(source)) == len(objects)
    assert not search.lock.locked()
    assert not (search.data_dir / "build-lease.json").exists()


def test_native_query_uses_summary_metadata_and_preserves_representation_stats(tmp_path, monkeypatch):
    search = FakeEngineSearch(tmp_path / "search")
    built = search.build([obj("one")], modes=["native"], generation="native")
    read = module.read_manifest
    requests = []

    def summary_only(path, summary=False, **kwargs):
        requests.append(summary)
        assert summary, "Native retrieval must not load per-object manifest maps"
        return read(path, summary, **kwargs)

    monkeypatch.setattr(module, "read_manifest", summary_only)
    hit = search.search("valve")[0]
    assert requests and all(requests)
    assert hit["retrieval"]["representation"] == built["representation"]["objects"]["lo-one"]


def test_full_status_and_qmd_hits_do_not_expose_cached_mutable_metadata(tmp_path):
    search = FakeEngineSearch(tmp_path / "search")
    search.build([obj("one")], modes=["native", "qmd_keyword"], generation="isolated")
    original = search.status()
    changed = search.status()
    assert "mapping" not in changed and "objects" not in changed["representation"]
    changed["representation"]["indexed_objects"] = 99
    changed["last_build"]["representation"]["indexed_objects"] = 99
    changed["modes"]["native"]["ready"] = False
    assert search.status() == original
    hit = search.search("valve", mode="qmd_keyword")[0]
    hit["retrieval"]["representation"]["policy"] = "caller mutation"
    assert search.search("valve", mode="qmd_keyword")[0]["retrieval"]["representation"]["policy"] != "caller mutation"


def test_warm_metadata_rechecks_artifact_availability_and_replaced_active_pointer(tmp_path):
    search = FakeEngineSearch(tmp_path / "search")
    first = search.build([obj("old")], modes=["native"], generation="old")
    assert search.search("valve")[0]["id"] == "lo-old"
    db = Path(first["directory"]) / "objects.sqlite3"
    moved = db.with_suffix(".moved")
    db.rename(moved)
    assert search.status(summary=True)["modes"]["native"]["state"] == "unavailable"
    with pytest.raises(RuntimeError, match="not ready"):
        search.search("valve")
    moved.rename(db)
    assert search.search("valve")[0]["id"] == "lo-old"
    search.build([obj("new")], modes=["native"], generation="new")
    assert search.search("valve")[0]["id"] == "lo-new"
    search.active_path.unlink()
    assert not search.status(summary=True)["ready"]
    with pytest.raises(RuntimeError, match="not ready"):
        search.search("valve")


def test_manifest_conversion_only_copies_owned_locator_projection(tmp_path, monkeypatch):
    search = FakeEngineSearch(tmp_path / "search")
    built = search.build([obj("one")], modes=["native"], generation="conversion")

    class NoCopy(dict):
        def __deepcopy__(self, memo):
            raise AssertionError("Bulk retrieval maps must not be copied for locator conversion")

    raw = dict(built, mapping=NoCopy(built["mapping"]), representation=NoCopy(built["representation"]))
    stored = module._serialize_manifest(raw, search.data_dir)
    assert stored["mapping"] is raw["mapping"] and stored["representation"] is raw["representation"]
    monkeypatch.setattr(module, "read_manifest", lambda *args, **kwargs: dict(stored))
    loaded = search._load_active()
    assert loaded["directory"] == built["directory"]
    assert loaded["mapping"] is raw["mapping"] and loaded["representation"] is raw["representation"]
