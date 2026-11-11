"""Shared keyword storage preserves saved text and collection boundaries."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import threading

import pytest

from advisor import native_index
from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.library_search import LibrarySearch
from advisor.qmd_shared import build_keyword_index, merge_keyword_indexes, purge_collection, _key
from test_tutorial_index_migration import qmd_schema, qmd_insert


def runtime(tmp_path):
    search = LibrarySearch(tmp_path / "search")
    if not search.node or not (search.qmd_dir / "dist/index.js").is_file():
        pytest.skip("Optional installed QMD SDK unavailable")
    return search, {"qmd_dir": search.qmd_dir, "node": search.node}


def source(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        qmd_schema(db)
        db.execute("ALTER TABLE store_collections ADD COLUMN context TEXT")
        db.execute("UPDATE store_collections SET context=?", ('{"/":"source context"}',))
        for identity, title, body, active in rows:
            filename = "o-" + hashlib.sha256(identity.encode()).hexdigest()[:32] + ".md"
            qmd_insert(db, filename, title, body)
            if not active:
                db.execute("UPDATE documents SET active=0 WHERE path=?", (filename,))


def saved_rows(path, collection):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("""SELECT d.path,d.title,d.hash,d.created_at,d.modified_at,c.doc FROM documents d
          JOIN content c ON c.hash=d.hash WHERE d.collection=? AND d.active=1 ORDER BY d.path""", (collection,)).fetchall()


def test_merge_preserves_raw_crlf_titles_hashes_deduplicates_and_remerges_shared(tmp_path):
    _, options = runtime(tmp_path)
    first, second = tmp_path / "a.sqlite3", tmp_path / "b.sqlite3"
    shared_body = "# Shared\r\n\r\n网格 日本語 한국어\r\n"
    source(first, [("a1", "Saved title differs from heading", shared_body, True),
                   ("a2", "Only A", "Unique_A_Secret\r\n", True)])
    source(second, [("b1", "Other saved title", shared_body, True), ("inactive", "Hidden", "Inactive secret", False)])
    old_bytes = first.read_bytes(), second.read_bytes()
    libraries = {"a": {"key": _key("a"), "db_path": str(first)}, "b": {"key": _key("b"), "db_path": str(second)}}
    output = tmp_path / "merged.sqlite3"
    report = merge_keyword_indexes(libraries, output, **options)
    assert report["documents"] == 3 and report["content_rows"] == 2 and report["source_databases"] == 2
    assert report["collections"]["a"]["expected_count"] == 2
    assert report["embedding_invoked"] is False
    assert saved_rows(first, "objects") == saved_rows(output, _key("a"))
    assert saved_rows(second, "objects") == saved_rows(output, _key("b"))
    assert (first.read_bytes(), second.read_bytes()) == old_bytes
    with closing(sqlite3.connect(output)) as db:
        assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert not db.execute("SELECT 1 FROM content WHERE doc='Inactive secret'").fetchone()
        assert db.execute("SELECT body FROM documents_fts WHERE body LIKE '%网 格%'").fetchone()
        assert not db.execute("SELECT 1 FROM content_vectors").fetchone()
    new_libraries = {library: {"key": _key(library), "collection": _key(library), "db_path": str(output)} for library in libraries}
    rebuilt = tmp_path / "remerged.sqlite3"
    again = merge_keyword_indexes(new_libraries, rebuilt, **options)
    assert again["documents"] == 3 and again["source_databases"] == 1
    for library in libraries:
        assert saved_rows(output, _key(library)) == saved_rows(rebuilt, _key(library))
    assert again["collections"] == report["collections"]


def test_native_build_matches_sdk_title_hash_body_without_markdown_exports(tmp_path):
    search, options = runtime(tmp_path)
    native = tmp_path / "native.sqlite3"
    objects = [{"id": "n1", "library_id": "a", "title": "Notes", "text": "## Secondary\n网格 日本語 한국어\r\n", "kind": "manual", "data": {}},
               {"id": "n2", "library_id": "b", "title": "First\nSecond", "text": "Preserve CRLF\r\n", "kind": "case", "data": {}}]
    with closing(sqlite3.connect(native)) as db, db:
        native_index.create_schema(db)
        for obj in objects:
            native_index.insert_object(db, obj, obj["text"], {"indexed_characters": len(obj["text"])}, "", standalone=False)
    report = build_keyword_index(native, tmp_path / "shared.sqlite3", **options)
    assert report["documents"] == 2
    assert not list(tmp_path.glob("**/corpus"))
    for obj in objects:
        corpus = tmp_path / ("expected-" + obj["library_id"]) / "corpus"
        corpus.mkdir(parents=True)
        filename = native_index.qmd_path(obj).split("/")[-1]
        (corpus / filename).write_text("# " + obj["title"] + "\n\n" + obj["text"], encoding="utf-8")
        expected = corpus.parent / "qmd-keyword.sqlite3"
        search._engine({"op": "update", "db_path": str(expected), "corpus": str(corpus), "embedding_enabled": False, "model_uri": None})
        actual = saved_rows(tmp_path / "shared.sqlite3", _key(obj["library_id"]))[0]
        wanted = saved_rows(expected, "objects")[0]
        assert actual[:3] == wanted[:3] and actual[-1] == wanted[-1]


def test_purge_preserves_shared_content_other_collection_and_is_retryable(tmp_path):
    _, options = runtime(tmp_path)
    first, second = tmp_path / "a.sqlite3", tmp_path / "b.sqlite3"
    source(first, [("a1", "Shared", "Shared retained body", True), ("a2", "A only", "Unique_A_Secret", True)])
    source(second, [("b1", "Shared", "Shared retained body", True)])
    output = tmp_path / "shared.sqlite3"
    merge_keyword_indexes({"a": {"db_path": str(first)}, "b": {"db_path": str(second)}}, output, **options)
    other = saved_rows(output, _key("b"))
    result = purge_collection(output, _key("a"))
    assert result["removed_documents"] == 2 and result["removed_content"] == 1
    assert result["removed_contexts"] == 1 and result["owned_schema"] and not result["empty"]
    assert result["compaction"] == "incremental_vacuum"
    assert saved_rows(output, _key("b")) == other
    assert b"Unique_A_Secret" not in output.read_bytes()
    original = output.read_bytes()
    assert purge_collection(output, _key("a"))["unchanged"]
    assert output.read_bytes() == original
    assert not purge_collection(output, _key("a"), force_compact=True)["unchanged"]
    assert purge_collection(output, _key("b"))["empty"]


def test_purge_rejects_unknown_triggers_before_any_mutation(tmp_path):
    _, options = runtime(tmp_path)
    original = tmp_path / "a.sqlite3"
    source(original, [("a1", "A", "Unchanged", True)])
    output = tmp_path / "shared.sqlite3"
    merge_keyword_indexes({"a": {"db_path": str(original)}}, output, **options)
    with closing(sqlite3.connect(output)) as db, db:
        db.execute("CREATE TRIGGER unexpected AFTER DELETE ON documents BEGIN DELETE FROM content; END")
    before = output.read_bytes()
    with pytest.raises(ValueError, match="unknown triggers"):
        purge_collection(output, _key("a"))
    assert output.read_bytes() == before


def test_purge_only_cleans_target_hashes_and_preserves_unrelated_orphan_content(tmp_path):
    _, options = runtime(tmp_path)
    original = tmp_path / "a.sqlite3"
    source(original, [("a1", "A", "Target collection body", True)])
    output = tmp_path / "shared.sqlite3"
    merge_keyword_indexes({"a": {"db_path": str(original)}}, output, **options)
    orphan = "Unrelated historical orphan must not be swept"
    digest = hashlib.sha256(orphan.encode()).hexdigest()
    with closing(sqlite3.connect(output)) as db, db:
        db.execute("INSERT INTO content VALUES(?,?,?)", (digest, orphan, "unrelated"))
    result = purge_collection(output, _key("a"))
    assert result["removed_documents"] == 1 and result["removed_content"] == 1
    assert result["empty"] is False
    with closing(sqlite3.connect(output)) as db:
        assert db.execute("SELECT hash,doc,created_at FROM content").fetchall() == [(digest, orphan, "unrelated")]


def test_cancellation_removes_unpublished_shared_output_and_keeps_sources(tmp_path):
    _, options = runtime(tmp_path)
    original = tmp_path / "a.sqlite3"
    source(original, [("a1", "A", "Unchanged", True)])
    before = original.read_bytes()
    event = threading.Event()
    def progress(value):
        event.set()
    output = tmp_path / "cancelled.sqlite3"
    with build_control(BuildControl(tmp_path / "cancel", event)), pytest.raises(BuildCancelled):
        merge_keyword_indexes({"a": {"db_path": str(original)}}, output, on_progress=progress, **options)
    assert original.read_bytes() == before and not output.exists()
    with pytest.raises(ValueError, match="cannot replace"):
        merge_keyword_indexes({"a": {"db_path": str(original)}}, original, **options)
