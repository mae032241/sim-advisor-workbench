"""A failed literal catalog filter must not look like an empty catalog."""
from contextlib import closing
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.library_store import LibraryStore


def make_store(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    store = LibraryStore(tmp_path / "libraries")
    store.register("General documentation", "official-manuals", [{"id": "root", "role": "manuals", "path": str(source)}], library_id="manual")
    store.register("Model collection", "official-models", [{"id": "root", "role": "models", "path": str(source)}], library_id="models")
    store.set_enabled("manual", True)
    return store


def test_zero_phrase_match_explains_full_catalog_and_never_changes_enabled_choices(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    with closing(store.connect()) as db:
        before = db.execute("SELECT id,body FROM libraries ORDER BY id").fetchall()
    for name in ("jobs", "objects", "object_stream", "index_status", "set_enabled", "set_enabled_many"):
        monkeypatch.setattr(store, name, lambda *args, **kwargs: pytest.fail("Catalog discovery must not load bodies or change scope"))
    result = store.library_scope(query="current task mechanism")
    assert result["library_count"] == result["matched_library_count"] == 0
    assert result["global_library_count"] == 2 and result["filter_applied"] is True
    assert result["query"] == "current task mechanism" and result["query_match_mode"] == "literal_substring"
    assert "get_library_scope({})" in result["next_action"]
    full = store.library_scope()
    assert full["filter_applied"] is False and full["global_library_count"] == full["matched_library_count"] == full["library_count"] == 2
    assert full["enabled_library_ids"] == ["manual"]
    with closing(store.connect()) as db:
        assert db.execute("SELECT id,body FROM libraries ORDER BY id").fetchall() == before


def test_literal_phrase_and_type_filters_keep_old_counts_and_exclude_deleted_catalog_entries(tmp_path):
    store = make_store(tmp_path)
    result = store.library_scope(query="GENERAL DOC", type="official-manuals")
    assert [item["id"] for item in result["libraries"]] == ["manual"]
    assert result["library_count"] == result["matched_library_count"] == result["enabled_library_count"] == 1
    assert result["global_library_count"] == 2 and result["type"] == "official-manuals"
    # No semantic token expansion or reranking is introduced.
    assert store.library_scope(query="documentation general")["matched_library_count"] == 0
    store.delete_library("models")
    assert store.library_scope(type="official-models")["global_library_count"] == 1


def test_actually_empty_catalog_is_distinct_from_empty_filter_result(tmp_path):
    result = LibraryStore(tmp_path / "libraries").library_scope(query="anything")
    assert result["global_library_count"] == result["matched_library_count"] == 0
    assert "目录为空" in result["next_action"]
