import copy
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.knowledge import KnowledgeBase
from advisor.library_provider import LibraryKnowledgeProvider
from advisor.library_search import LibrarySearch, LibraryVisibilityError
from advisor.library_store import LibraryStore, encoded
from advisor.knowledge import source_id
from test_library_search import FakeEngineSearch, obj
from test_knowledge import write_topic


def modern_fixture(tmp_path):
    store = LibraryStore(tmp_path / "data/libraries")
    source = tmp_path / "sources"
    source.mkdir()
    for lid in ("a", "b"):
        store.register(lid, "user-models", [{"id": "main", "path": str(source), "role": "models"}], library_id=lid)
        store.set_enabled(lid, True)
    objects = [obj("object-" + lid, library=lid, text="mesh flow pressure") for lid in ("a", "b")]
    with closing(store.connect()) as db, db:
        for item in objects:
            db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (item["id"], item["object_id"], item["library_id"], encoded(item)))
            db.execute("INSERT INTO active VALUES(?,?,?)", (item["library_id"], item["object_id"], item["id"]))
    search = FakeEngineSearch(store.root / "search")
    built = search.build(objects, modes=["native", "qmd_keyword", "qmd_vector"], generation=store.current_generation())
    search.calls.clear()
    return store, search, built, objects


@pytest.mark.parametrize("mode", ["native", "qmd_keyword", "qmd_vector"])
def test_direct_search_hides_closed_deleted_and_reuses_unchanged_indexes_on_enable(tmp_path, mode):
    store, search, manifest, objects = modern_fixture(tmp_path)
    generation, original = store.current_generation(), copy.deepcopy(store.objects())
    before_manifest = search.active_path.read_bytes()
    vectors = {lid: Path(item["vector_db_path"]).read_bytes() for lid, item in manifest["libraries"].items()}
    assert {item["library_id"] for item in search.search("flow", mode=mode)} == {"a", "b"}
    store.set_enabled("a", False)
    search.calls.clear()
    assert search.search("flow", mode=mode, library_ids=["a"]) == []
    assert not search.calls
    assert {item["library_id"] for item in search.search("flow", mode=mode)} == {"b"}
    store.set_enabled("a", True)
    assert search.search("flow", mode=mode, library_ids="a")[0]["library_id"] == "a"
    store.delete_library("a")
    assert search.search("flow", mode=mode, library_ids=["a"]) == []
    assert {item["library_id"] for item in search.search("flow", mode=mode)} == {"b"}
    store.restore_library("a")
    assert search.search("flow", mode=mode, library_ids=["a"]) == [], "Restoration retains the disabled state"
    store.set_enabled("a", True)
    assert search.search("flow", mode=mode, library_ids=["a"])[0]["library_id"] == "a"
    assert store.current_generation() == generation and store.objects() == original
    assert search.active_path.read_bytes() == before_manifest
    assert all(Path(manifest["libraries"][lid]["vector_db_path"]).read_bytes() == value for lid, value in vectors.items())
    assert all(call["op"] in {"shared_keyword", "keyword", "vector"} for call in search.calls)


@pytest.mark.parametrize("mode", ["native", "qmd_keyword", "qmd_vector"])
def test_direct_search_rechecks_lifecycle_before_returning_inflight_results(tmp_path, monkeypatch, mode):
    store, search, _, _ = modern_fixture(tmp_path)
    original = search._searchable_library_ids
    reads = 0

    def racing_snapshot(requested=None):
        nonlocal reads
        visible = original(requested)
        reads += 1
        if reads == 1:
            store.set_enabled("a", False)
        return visible

    monkeypatch.setattr(search, "_searchable_library_ids", racing_snapshot)
    assert search.search("flow", mode=mode, library_ids=["a"]) == []
    assert reads == 2


class LegacySpy:
    def __init__(self, callback=None):
        self.calls = 0
        self.callback = callback

    def search(self, *_args, **_kwargs):
        self.calls += 1
        if self.callback:
            self.callback()
        return [{"id": "legacy-manual", "title": "Independent legacy manual", "kind": "manual", "text": "flow"}]

    def get_document(self, *_args):
        return None

    def status(self):
        return {"ready": True}


def test_provider_explicit_closed_scopes_and_old_id_reads_are_blocked_but_internal_archives_remain(tmp_path):
    store, _, _, objects = modern_fixture(tmp_path)
    legacy = LegacySpy()
    provider = LibraryKnowledgeProvider(legacy, store.root)
    store.set_enabled("a", False)
    for filters in ({"library_ids": ["a"]}, {"library_ids": []}, {"library_ids": ["unknown"]}, {"feature_branch": "mesh"}):
        assert provider.search("flow", filters=filters) == []
    assert legacy.calls == 0
    store.set_enabled("b", False)
    assert provider.search("flow") == [], "Unregistered archives are not a retrieval fallback"
    assert legacy.calls == 0
    store.delete_library("a")
    with pytest.raises(PermissionError, match="资料库已关闭或删除"):
        provider.get_document(objects[0]["id"])
    assert store.get(objects[0]["id"]) == objects[0]


def test_provider_rechecks_after_slow_legacy_query(tmp_path):
    store, _, _, _ = modern_fixture(tmp_path)
    legacy = LegacySpy(lambda: store.set_enabled("a", False))
    provider = LibraryKnowledgeProvider(legacy, store.root)
    hits = provider.search("flow")
    assert {item["library_id"] for item in hits} == {"a", "b"}
    assert legacy.calls == 0


def test_visibility_status_counts_do_not_change_physical_index_readiness(tmp_path):
    store, _, _, _ = modern_fixture(tmp_path)
    provider = LibraryKnowledgeProvider(LegacySpy(), store.root)
    store.set_enabled("a", False)
    status = provider.status()
    assert status["library_visibility"] == {"enabled_library_count": 1, "disabled_library_count": 1,
        "deleted_library_count": 0, "searchable_object_count": 1, "physical_object_count": 2}
    assert status["library_indexes"]["modes"]["qmd_keyword"]["ready"]
    store.delete_library("a")
    status = provider.status()
    assert status["library_visibility"]["deleted_library_count"] == 1
    assert status["library_visibility"]["disabled_library_count"] == 0
    assert status["library_visibility"]["physical_object_count"] == 2
    assert status["library_indexes"]["modes"]["qmd_keyword"]["ready"]


@pytest.mark.parametrize("patch", [{"enabled": "false"}, {"enabled": 1}, {"deleted_at": ""}, {"deleted_at": False}])
def test_bad_catalog_flags_fail_closed_for_direct_and_provider_search(tmp_path, patch):
    store, search, _, _ = modern_fixture(tmp_path)
    with closing(store.connect()) as db, db:
        body = json.loads(db.execute("SELECT body FROM libraries WHERE id='a'").fetchone()[0])
        body.update(patch)
        db.execute("UPDATE libraries SET body=? WHERE id='a'", (json.dumps(body),))
    with pytest.raises(LibraryVisibilityError, match="lifecycle"):
        search.search("flow")
    legacy = LegacySpy()
    provider = LibraryKnowledgeProvider(legacy, store.root)
    with pytest.raises(LibraryVisibilityError, match="lifecycle"):
        provider.search("flow")
    assert legacy.calls == 0


def test_standalone_index_without_catalog_keeps_legacy_behavior_but_bad_database_does_not(tmp_path):
    search = LibrarySearch(tmp_path / "search")
    search.build([obj("standalone")], modes=["native"], generation="standalone")
    assert search.search("valve") and not (tmp_path / "catalog.sqlite3").exists()
    (tmp_path / "catalog.sqlite3").write_bytes(b"not a sqlite catalog")
    with pytest.raises(LibraryVisibilityError, match="catalog"):
        search.search("valve")


def test_legacy_catalog_rows_default_enabled_and_closed_scope_does_not_check_index(tmp_path, monkeypatch):
    store, search, _, _ = modern_fixture(tmp_path)
    with closing(store.connect()) as db, db:
        body = json.loads(db.execute("SELECT body FROM libraries WHERE id='a'").fetchone()[0])
        body.pop("enabled")
        body.pop("deleted_at")
        db.execute("UPDATE libraries SET body=? WHERE id='a'", (json.dumps(body),))
    assert search.search("flow", library_ids=["a"])
    store.set_enabled("a", False)
    monkeypatch.setattr(search, "status", lambda: pytest.fail("A closed explicit scope must not inspect/load an index"))
    assert search.search("flow", mode="qmd_vector", library_ids=["a"]) == []


def test_catalog_disappearing_midquery_does_not_become_standalone_enable_all(tmp_path, monkeypatch):
    store, search, _, _ = modern_fixture(tmp_path)
    original = search._searchable_library_ids
    calls = 0

    def remove_after_first_snapshot(requested=None):
        nonlocal calls
        result = original(requested)
        calls += 1
        if calls == 1:
            store.db_path.rename(store.db_path.with_suffix(".archived"))
        return result

    monkeypatch.setattr(search, "_searchable_library_ids", remove_after_first_snapshot)
    with pytest.raises(LibraryVisibilityError, match="disappeared"):
        search.search("flow", library_ids=["a"])
    assert not store.db_path.exists()


def manual_fixture(tmp_path, *, strong=1):
    docs = tmp_path / "docs"
    for index in range(strong):
        write_topic(docs, "com.comsol.help.closed_part", f"closed-{index}.html", "Mesh sizing",
                    ["Mesh sizing mesh sizing tetrahedral mesh controls."])
    write_topic(docs, "com.comsol.help.closedXpart", "open.html", "Independent meshing guide", ["Mesh sizing is set using documented element sizes."])
    kb = KnowledgeBase(tmp_path / "data", docs)
    assert kb.build()["ready"]
    store = LibraryStore(tmp_path / "data/libraries")
    return kb, store


@pytest.mark.parametrize('manual_type', ['official-manuals', 'user-manuals'])
def test_disabled_manual_subtree_blocks_legacy_body_reads_without_search_fallback(tmp_path, manual_type):
    kb, store = manual_fixture(tmp_path)
    closed_id = source_id("com.comsol.help.closed_part/closed-0.html")
    open_id = source_id("com.comsol.help.closedXpart/open.html")
    historical = kb.get_document(closed_id)
    library = store.register("Closed manuals", manual_type,
                             [{"id": "manual", "path": str(kb.doc_root / "com.comsol.help.closed_part")}])
    provider = LibraryKnowledgeProvider(kb, store.root)
    with pytest.raises(PermissionError):
        provider.get_document(closed_id)
    assert provider.get_document(open_id)
    assert provider.search("mesh sizing") == []
    assert kb.get_document(closed_id)["text"] == historical["text"]
    store.set_enabled(library["id"], True)
    assert provider.get_document(closed_id)
    store.delete_library(library["id"])
    with pytest.raises(PermissionError):
        provider.get_document(closed_id)


@pytest.mark.parametrize('manual_type', ['official-manuals', 'user-manuals'])
def test_whole_manual_root_closing_during_legacy_read_wins(tmp_path, monkeypatch, manual_type):
    kb, store = manual_fixture(tmp_path)
    library = store.register("All manuals", manual_type, [{"id": "manual", "path": str(kb.doc_root)}])
    store.set_enabled(library["id"], True)
    provider = LibraryKnowledgeProvider(kb, store.root)
    original = kb.get_document
    def close_during_read(*args, **kwargs):
        result = original(*args, **kwargs)
        store.set_enabled(library["id"], False)
        return result
    monkeypatch.setattr(kb, "get_document", close_during_read)
    with pytest.raises(PermissionError):
        provider.get_document(source_id("com.comsol.help.closed_part/closed-0.html"))
    monkeypatch.setattr(kb, "search", lambda *args, **kwargs: pytest.fail("Retired search must not run"))
    assert provider.search("mesh sizing") == []


@pytest.mark.parametrize("type,role", [("user-models", "docs"), ("official-models", "docs")])
def test_unrelated_model_library_does_not_hide_archived_manual_citation(tmp_path, type, role):
    kb, store = manual_fixture(tmp_path)
    store.register("Models", type, [{"id": "main", "role": role, "path": str(kb.doc_root)}])
    provider = LibraryKnowledgeProvider(kb, store.root)
    assert provider.get_document(source_id("com.comsol.help.closedXpart/open.html"))
    assert provider.search("mesh sizing") == []


def test_retired_prefix_search_options_are_explicit(tmp_path):
    kb, _ = manual_fixture(tmp_path)
    for value in (["../escape"], ["D:/escape"], "not-a-list", ["com.comsol.help.closed_part"]):
        with pytest.raises(ValueError, match="exclude_relpath_prefixes"):
            kb.search("mesh", filters={"exclude_relpath_prefixes": value})
