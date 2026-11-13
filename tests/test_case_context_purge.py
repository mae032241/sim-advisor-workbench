"""Purging a tutorial library removes its text from retained model overviews."""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3

import pytest

from advisor import case_context
from advisor import case_context_index as index
from advisor import case_context_store
from advisor.index_manifest import publish_manifest
from advisor.library_search import LibrarySearch, _hash
from advisor.object_index import representation_stats
from advisor.qmd_patch import patch_keyword_documents
from test_case_context_provider import contextual_provider
from test_case_navigation_search import navigation


SENTINEL = "PURGE_DERIVED_TUTORIAL_SENTINEL_987654"


@pytest.fixture
def patched_context(contextual_provider):
    fixture = contextual_provider
    source = fixture.tutorial
    source["text"] += " " + SENTINEL
    with closing(fixture.store.connect()) as db, db:
        db.execute("UPDATE revisions SET payload=? WHERE id=?", (json.dumps(source), source["id"]))
    profile = copy.deepcopy(fixture.profile)
    profile["text"] = source["text"]
    profile["content_hash"] = _hash(profile["text"])
    profile["excerpts"][0].update(text=source["text"], source_text_end=len(source["text"]))
    root = fixture.store.root / "search"
    active = fixture.search._load_active(summary=True)
    keyword = Path(active["directory"]) / "qmd-keyword.sqlite3"
    entry, update, stats = index._profile_entry(fixture.store, profile)
    locator = entry["qmd_path"].split("/")
    with closing(sqlite3.connect(keyword)) as db:
        fixture.base_document = db.execute("SELECT d.id,d.title,c.doc FROM documents d JOIN content c ON c.hash=d.hash WHERE d.collection=? AND d.path=?", locator).fetchone()
    engine = LibrarySearch(root)
    patch_keyword_documents(keyword, [update], runtime_dir=root / "runtime", **index._runtime(engine))
    index._update_stats(Path(active["directory"]) / "objects.sqlite3", {fixture.case["id"]: stats})
    case_context_store.write_contexts(root, active, [entry], {fixture.case["id"]: profile["model_completeness"]})
    publish_manifest(Path(active["directory"]), index._mark_manifest(active, {fixture.case["id"]: entry}, root), root)
    fixture.root, fixture.keyword, fixture.profile = root, keyword, entry
    fixture.rich_active = fixture.search._load_active(summary=True)
    fixture.model_archive = copy.deepcopy(fixture.store.get(fixture.case["id"]))
    return fixture


def forbid_rebuild(monkeypatch, fixture):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Purge must patch existing rows without rebuilding or extracting source text")
    monkeypatch.setattr(LibrarySearch, "build", forbidden)
    monkeypatch.setattr(fixture.store, "reindex", forbidden)
    monkeypatch.setattr(case_context, "build_case_context", forbidden)


def test_manual_close_delete_purge_restores_original_overview_without_rebuild(patched_context, monkeypatch):
    fixture = patched_context
    with closing(sqlite3.connect(fixture.keyword)) as db:
        assert db.execute("SELECT 1 FROM content WHERE doc LIKE ?", ("%" + SENTINEL + "%",)).fetchone()
    fixture.store.set_enabled("manuals", False)
    fixture.store.delete_library("manuals")
    forbid_rebuild(monkeypatch, fixture)
    result = fixture.store.purge_library("manuals", confirmed=True)
    assert result["status"] == "purged" and result["indexes"]["embedding_invoked"] is False
    assert result["indexes"]["case_context_cleanup"] == {"restored_overviews": 1, "removed_profiles": 1}
    active = fixture.search._load_active(summary=True)
    assert active["directory"] == fixture.rich_active["directory"]
    assert active["modes"]["qmd_keyword"]["ready"]
    assert "manuals" not in active["libraries"]
    assert fixture.store.get(fixture.case["id"]) == fixture.model_archive
    assert fixture.store.get(fixture.tutorial["id"]) is None
    with closing(sqlite3.connect(fixture.keyword)) as db:
        locator = fixture.profile["qmd_path"].split("/")
        document = db.execute("SELECT d.id,d.title,c.doc FROM documents d JOIN content c ON c.hash=d.hash WHERE d.collection=? AND d.path=?", locator).fetchone()
        assert document == fixture.base_document
        assert not db.execute("SELECT 1 FROM content WHERE doc LIKE ?", ("%" + SENTINEL + "%",)).fetchone()
        assert not db.execute("SELECT 1 FROM documents_fts WHERE documents_fts MATCH ?", (SENTINEL,)).fetchone()
    assert SENTINEL.encode() not in fixture.keyword.read_bytes()
    metadata = case_context_store.validated_contexts(fixture.root, active)
    assert case_context_store.read_contexts(fixture.root, active) == {}
    assert metadata["meta"]["context_count"] == 0
    assert SENTINEL.encode() not in Path(metadata["path"]).read_bytes()
    with closing(sqlite3.connect(Path(active["directory"]) / "objects.sqlite3")) as db:
        assert not representation_stats(db, fixture.case["id"]).get("case_context_included")
    hits = fixture.provider.search("overview", limit=40, kind="case", filters={"library_ids": ["models"]})
    assert fixture.case["id"] in {hit["id"] for hit in hits}
    raw = fixture.provider.get_document(fixture.case["id"])
    assert raw["text"] == fixture.model_archive["text"]
    assert "search_context" not in raw.get("data", {})


def test_purge_unrelated_library_retains_other_context_and_its_dependency_guard(patched_context, monkeypatch):
    fixture = patched_context
    fixture.store.delete_library("closed")
    forbid_rebuild(monkeypatch, fixture)
    result = fixture.store.purge_library("closed", confirmed=True)
    assert result["indexes"]["case_context_cleanup"] == {"restored_overviews": 0, "removed_profiles": 0}
    active = fixture.search._load_active(summary=True)
    cached = case_context_store.read_contexts(fixture.root, active)
    assert set(cached) == {fixture.case["id"]}
    assert SENTINEL in cached[fixture.case["id"]]["text"]
    assert SENTINEL in fixture.provider.get_document(fixture.case["id"])["text"]
    fixture.store.set_enabled("manuals", False)
    assert fixture.case["id"] not in {hit["id"] for hit in fixture.search.search("needle", library_ids=["models"], limit=100)}
    assert SENTINEL not in fixture.provider.get_document(fixture.case["id"])["text"]
