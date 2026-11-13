"""Incremental staging preserves model retrieval and publishes no pointers."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import threading

import pytest

from advisor import object_index as native_index
from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.index_paths import resolve_index_manifest
from advisor.library_search import LibrarySearch, _corpus_hash
from advisor.qmd_shared import keyword_input_spool
from advisor.tutorial_index_migration import stage_tutorial_indexes, _backup, _export_manual, _manual_sql_delta
from advisor.tutorial_migration import apply_plan_to_catalog, build_tutorial_migration_plan
from test_tutorial_migration import _put, _record, _source, fixture as migration_fixture


def qmd_schema(db):
    db.executescript("""
      PRAGMA user_version=1;
      CREATE TABLE content(hash TEXT PRIMARY KEY,doc TEXT NOT NULL,created_at TEXT NOT NULL);
      CREATE TABLE documents(id INTEGER PRIMARY KEY AUTOINCREMENT,collection TEXT NOT NULL,path TEXT NOT NULL,
        title TEXT NOT NULL,hash TEXT NOT NULL,created_at TEXT NOT NULL,modified_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,UNIQUE(collection,path));
      CREATE TABLE store_collections(name TEXT PRIMARY KEY,path TEXT NOT NULL,pattern TEXT NOT NULL);
      INSERT INTO store_collections VALUES('objects','corpus','**/*.md');
      CREATE TABLE content_vectors(hash TEXT,seq INTEGER,pos INTEGER,model TEXT,embed_fingerprint TEXT);
      CREATE VIRTUAL TABLE documents_fts USING fts5(filepath,title,body,tokenize='porter unicode61');
      CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
        DELETE FROM documents_fts WHERE rowid = old.id; END;
    """)


def qmd_insert(db, filename, title, body):
    digest = hashlib.sha256(body.encode()).hexdigest()
    db.execute("INSERT OR IGNORE INTO content VALUES(?,?,?)", (digest, body, "fixture"))
    cursor = db.execute("INSERT INTO documents(collection,path,title,hash,created_at,modified_at) VALUES('objects',?,?,?,?,?)",
                        (filename, title, digest, "fixture", "fixture"))
    db.execute("INSERT INTO documents_fts(rowid,filepath,title,body) VALUES(?,?,?,?)", (cursor.lastrowid, "objects/" + filename, title, body))


class FakeEngine(LibrarySearch):
    calls = []

    def _qmd_version(self):
        return "2.8.3"

    def _engine(self, request, *, timeout=120):
        self.calls.append((dict(request), timeout))
        assert request["op"] == "update", "Migration must not embed or query models"
        assert request["embedding_enabled"] is False and request["model_uri"] is None
        database = Path(request["db_path"])
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as db, db:
            qmd_schema(db)
            for path in sorted(Path(request["corpus"]).glob("*.md")):
                raw = path.read_text(encoding="utf-8")
                title, body = raw.split("\n\n", 1)
                qmd_insert(db, path.name, title.removeprefix("# "), body)
        return {"indexed": True}


def fixture(tmp_path):
    store, originals, cases, copies, pdfs = migration_fixture(tmp_path)
    model_lib = store.library("models")
    model_path = Path(model_lib["roots"][0]["path"]) / "configured.mph"
    model_path.write_bytes(b"saved state fixture")
    retained = store._prepare(model_lib, _record(model_path, model_path.parent, "models"), _source(
        "saved-size", "saved_configuration", "Saved mesh hmax", "hmax min(20[mm],lam0/4) RetainedMeshExact",
        model_path, data={"feature_branch": "mesh", "compact_ref": {"node_id": 1}}))
    _put(store, [retained])
    untouched_lib = store.register("Unrelated manual", "official-manuals", [{"id": "docs", "path": str(tmp_path / "docs")}], library_id="untouched")
    untouched_lib = store.set_enabled(untouched_lib["id"], True)
    untouched_path = tmp_path / "docs/unrelated.html"
    untouched_path.write_text("UnrelatedManualExact", encoding="utf-8")
    untouched = store._prepare(untouched_lib, _record(untouched_path, untouched_path.parent, "docs"),
                               _source("topic", "manual_topic", "Unrelated", "UnrelatedManualExact", untouched_path))
    _put(store, [untouched])
    search = LibrarySearch(store.root / "search", preserve_history=True)
    with store.object_stream() as objects:
        search.build(objects, modes=["qmd_keyword"], generation=objects.generation)
    old = search._load_active()
    # Feed the historical per-library fixture from the real saved QMD bodies,
    # never from an object-directory search-text cache.
    saved_keywords = {library: (item['db_path'], item['collection']) for library, item in old['libraries'].items()}
    old.pop('keyword_storage', None)
    for library, item in old['libraries'].items():
        source, collection = saved_keywords[library]
        item.pop('collection', None)
        item['db_path'] = str(Path(old['directory']) / item['key'] / 'qmd-keyword.sqlite3')
        database = Path(item['db_path'])
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source)) as saved, closing(sqlite3.connect(database)) as db, db:
            qmd_schema(db)
            for locator, title, body in saved.execute("""SELECT d.path,d.title,c.doc FROM documents d
                JOIN content c ON c.hash=d.hash WHERE d.collection=? AND d.active=1 ORDER BY d.path""", (collection,)):
                qmd_insert(db, locator, title, body)
    old["modes"]["qmd_keyword"] = {"ready": True, "state": "ready", "qmd_version": "2.8.3"}
    search._publish(Path(old["directory"]), old)
    plan = build_tutorial_migration_plan(store, tmp_path / "plan.sqlite3")
    FakeEngine.calls = []
    return store, plan, search._load_active(), retained, untouched


def database_rows(path):
    with closing(sqlite3.connect(path)) as db:
        return [(*row, native_index.representation_stats(db, row[1])) for row in
                db.execute("SELECT rowid,id,library_id,kind,feature_branch,qmd_path,source_json FROM objects ORDER BY rowid")]



def qmd_hits(path, query):
    with closing(sqlite3.connect(path)) as db:
        return [row[0] for row in db.execute("SELECT d.path FROM documents_fts f JOIN documents d ON d.id=f.rowid WHERE documents_fts MATCH ?", (query,))]


def test_stage_preserves_retained_native_rows_keyword_recall_and_catalog(tmp_path, monkeypatch):
    store, plan, old, retained, untouched = fixture(tmp_path)
    before_generation = store.current_generation()
    old_native = Path(old["directory"]) / "objects.sqlite3"
    old_rows = database_rows(old_native)
    old_bytes = {Path(item["db_path"]): Path(item["db_path"]).read_bytes() for item in old["libraries"].values()}
    old_pointer = (store.root / "search/active.json").read_bytes()
    monkeypatch.setattr(store, "objects", lambda *_: pytest.fail("Stage must stream the catalog"))
    result = stage_tutorial_indexes(store, plan, engine_factory=FakeEngine, manual_strategy="sdk")
    assert store.current_generation() == before_generation
    assert (store.root / "search/active.json").read_bytes() == old_pointer
    assert not (Path(result["directory"]) / "manifest.json").exists()
    assert all(path.read_bytes() == raw for path, raw in old_bytes.items())
    assert database_rows(old_native) == old_rows
    retired = set(plan.iter_retirement_ids())
    new_rows = database_rows(result["native_path"])
    old_retained = {row[1]: row for row in old_rows if row[1] not in retired}
    new_retained = {row[1]: row for row in new_rows if row[1] in old_retained}
    assert new_retained == old_retained
    assert all(row[1] not in retired for row in new_rows)
    assert all(row[-2] is None for row in new_rows)
    assert not list(Path(result["directory"]).glob(".qmd-keyword-inputs-*"))
    manifest = resolve_index_manifest(result["manifest"], store.root / "search")
    assert result["manifest"]["directory"].startswith("generations/")
    assert "native" not in manifest["modes"] and manifest["modes"]["qmd_keyword"]["ready"]
    assert manifest["modes"]["qmd_vector"] == {"ready": False, "state": "disabled", "enabled": False}
    assert len(FakeEngine.calls) == 1 and FakeEngine.calls[0][1] == 1800
    assert FakeEngine.calls[0][0]["key"] == manifest["libraries"]["cs63-documents"]["key"]
    assert qmd_hits(manifest["libraries"]["models"]["db_path"], "RetainedMeshExact")
    assert qmd_hits(manifest["libraries"]["cs63-documents"]["db_path"], '"arbitrary section"')
    assert qmd_hits(manifest["libraries"]["untouched"]["db_path"], "UnrelatedManualExact")
    assert not qmd_hits(manifest["libraries"]["cs63-documents"]["db_path"], '"Repeated PDF"')
    assert not Path(manifest["libraries"]["models"]["corpus"]).exists()
    with closing(sqlite3.connect(result["native_path"])) as db:
        assert native_index.owned_schema(db)
        assert not native_index.has_legacy_fts(db)
        assert db.execute("SELECT id FROM objects WHERE id=?", (retained["id"],)).fetchone() == (retained["id"],)
    for library in manifest["libraries"]:
        assert Path(manifest["libraries"][library]["db_path"]).stat().st_nlink == 1
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        apply_plan_to_catalog(db, plan)
    with store.object_stream() as objects:
        assert result["manifest"]["generation"] == objects.generation
        assert result["manifest"]["corpus_hash"] == _corpus_hash(objects)


def test_cancellation_cleans_only_new_stage_and_keeps_current_generation(tmp_path):
    store, plan, old, *_ = fixture(tmp_path)
    existing = set((store.root / "search/generations").iterdir())
    pointer = (store.root / "search/active.json").read_bytes()
    event = threading.Event()
    def progress(value):
        if value["phase"] == "retired_native":
            event.set()
    with build_control(BuildControl(tmp_path / "cancel", event)):
        with pytest.raises(BuildCancelled):
            stage_tutorial_indexes(store, plan, progress, engine_factory=FakeEngine, manual_strategy="sdk")
    assert set((store.root / "search/generations").iterdir()) == existing
    assert (store.root / "search/active.json").read_bytes() == pointer


def test_schema_version_or_trigger_changes_fail_closed_and_clean_stage(tmp_path):
    store, plan, old, *_ = fixture(tmp_path)
    existing = set((store.root / "search/generations").iterdir())
    database = old["libraries"]["models"]["db_path"]
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("DROP TRIGGER documents_ad")
    with pytest.raises(ValueError, match="deletion trigger"):
        stage_tutorial_indexes(store, plan, engine_factory=FakeEngine, manual_strategy="sdk")
    assert set((store.root / "search/generations").iterdir()) == existing


def test_stale_plan_or_sdk_version_never_stages(tmp_path):
    store, plan, old, *_ = fixture(tmp_path)
    class WrongVersion(FakeEngine):
        def _qmd_version(self):
            return "3.0.0"
    with pytest.raises(ValueError, match="2.8.3"):
        stage_tutorial_indexes(store, plan, engine_factory=WrongVersion, manual_strategy="sdk")
    with closing(store.connect()) as db, db:
        db.execute("DELETE FROM active WHERE citation_id=?", (next(plan.iter_retirement_ids()),))
    with pytest.raises(ValueError, match="Catalog changed"):
        stage_tutorial_indexes(store, plan, engine_factory=FakeEngine, manual_strategy="sdk")


@pytest.mark.parametrize('strategy', ['sdk', 'sql_delta'])
def test_shared_keyword_manifest_is_refused_before_hashing_copying_or_sdk_work(tmp_path, monkeypatch, strategy):
    from advisor import tutorial_index_migration as migration
    store, plan, old, *_ = fixture(tmp_path)
    old['keyword_storage'] = 'shared-collections-v1'
    for item in old['libraries'].values():
        item['collection'] = item['key']
        item['db_path'] = str(Path(old['directory']) / 'qmd-keyword.sqlite3')
    engine = FakeEngine(store.root / 'search')
    engine._publish(Path(old['directory']), old)
    pointer = (store.root / 'search/active.json').read_bytes()
    directories = set((store.root / 'search/generations').iterdir())
    def forbidden(*args, **kwargs):
        pytest.fail('A shared manifest must be refused before reading catalog payloads or staging files')
    monkeypatch.setattr(store, 'object_stream', forbidden)
    monkeypatch.setattr(migration, '_desired_fingerprints', forbidden)
    monkeypatch.setattr(migration, '_backup', forbidden)
    monkeypatch.setattr(migration, '_manual_sql_delta', forbidden)
    monkeypatch.setattr(FakeEngine, '_qmd_version', forbidden)
    with pytest.raises(ValueError, match='does not support shared keyword collections'):
        stage_tutorial_indexes(store, plan, engine_factory=FakeEngine, manual_strategy=strategy)
    assert (store.root / 'search/active.json').read_bytes() == pointer
    assert set((store.root / 'search/generations').iterdir()) == directories


def test_available_real_qmd_manual_rebuild_matches_released_schema(tmp_path):
    store, plan, old, *_ = fixture(tmp_path)
    search = LibrarySearch(store.root / "search")
    if not search.node or not (search.qmd_dir / "dist/index.js").is_file():
        pytest.skip("Optional local QMD SDK unavailable")
    result = stage_tutorial_indexes(store, plan)
    manifest = resolve_index_manifest(result["manifest"], store.root / "search")
    item = manifest["libraries"]["cs63-documents"]
    rows = search._engine({"op": "keyword", **item, "query": "arbitrary", "limit": 10,
                           "embedding_enabled": False, "model_uri": None})
    assert rows and all(row["source"] == "fts" for row in rows)
    with closing(sqlite3.connect(result["native_path"])) as db:
        assert all(native_index.lookup_id(db, item["key"] + "/" + row["filepath"].split("/")[-1]) for row in rows)
    assert search._load_active()["generation"] == old["generation"]


def test_bulk_sql_matches_sdk_markdown_titles_hashes_bodies_and_cjk_fts(tmp_path):
    from types import SimpleNamespace
    search = LibrarySearch(tmp_path / "search")
    if not search.node or not (search.qmd_dir / "dist/index.js").is_file():
        pytest.skip("Optional local QMD SDK unavailable")
    library = "manuals"
    canonical = [("中文日本語한국어", "网格設定 日本語 한국어"),
                 ("Notes", "## SecondaryHeading\nChoose this heading."),
                 ("First line\nSecond line", "Line one\r\nLine two\nLast line.")]
    def native(path, entries):
        items = []
        with closing(sqlite3.connect(path)) as db, db:
            native_index.create_schema(db)
            for identity, title, text in entries:
                item = {"id": identity, "library_id": library, "title": title, "text": text, "kind": "manual", "type": "manual_topic", "data": {}}
                native_index.insert_object(db, item, text, {"indexed_characters": len(text)}, "", standalone=False)
                items.append(item)
        return items
    old_native, new_native = tmp_path / "old-native.sqlite3", tmp_path / "new-native.sqlite3"
    old_items = native(old_native, [("retained", "Retained", "Retained stable body."), ("retired", "Retired", "Remove this old copy.")])
    new_items = native(new_native, [("retained", "Retained", "Retained stable body."), *[("canonical-" + str(i), title, text) for i, (title, text) in enumerate(canonical)]])
    plan_path = tmp_path / "plan.sqlite3"
    with closing(sqlite3.connect(plan_path)) as db, db:
        db.executescript("CREATE TABLE canonical_objects(id TEXT PRIMARY KEY,library_id TEXT); CREATE TABLE retirements(id TEXT PRIMARY KEY,library_id TEXT);")
        db.executemany("INSERT INTO canonical_objects VALUES(?,?)", (("canonical-" + str(i), library) for i in range(len(canonical))))
        db.execute("INSERT INTO retirements VALUES('retired',?)", (library,))
    def sdk_build(name, source, items):
        directory = tmp_path / name
        with keyword_input_spool(directory) as inputs, closing(sqlite3.connect(source)) as db:
            for item in items:
                inputs.add(library, native_index.qmd_path(item), item["title"], item["text"])
            inputs.seal()
            _export_manual(db, library, directory / "corpus", None, inputs_path=inputs.path)
        item = {"db_path": str(directory / "qmd-keyword.sqlite3"), "corpus": str(directory / "corpus")}
        search._engine({"op": "update", **item, "embedding_enabled": False, "model_uri": None})
        return item
    old, expected = sdk_build("old", old_native, old_items), sdk_build("expected", new_native, new_items)
    actual = {"db_path": str(tmp_path / "actual/qmd-keyword.sqlite3"), "corpus": str(tmp_path / "actual/corpus")}
    _backup(old["db_path"], actual["db_path"])
    events = []
    with keyword_input_spool(tmp_path / "sql-inputs") as inputs:
        for item in new_items:
            if item["id"] != "retained":
                inputs.add(library, native_index.qmd_path(item), item["title"], item["text"])
        inputs.seal()
        result = _manual_sql_delta(search, actual["db_path"], library, new_native, SimpleNamespace(path=plan_path), events.append,
                                   inputs_path=inputs.path)
    assert result["inserted"] == 3 and result["retired"] == 1
    assert events and events[-1]["completed"] == 3
    def rows(item):
        with closing(sqlite3.connect(item["db_path"])) as db:
            return db.execute("""SELECT d.path,d.title,d.hash,c.doc,f.filepath,f.title,f.body FROM documents d
              JOIN content c ON c.hash=d.hash JOIN documents_fts f ON f.rowid=d.id
              WHERE d.active=1 ORDER BY d.path""").fetchall()
    assert rows(actual) == rows(expected)
    with closing(sqlite3.connect(old["db_path"])) as old_db, closing(sqlite3.connect(actual["db_path"])) as actual_db:
        path = native_index.qmd_path({"id": "retained", "library_id": library}).split("/")[-1]
        assert old_db.execute("SELECT * FROM documents WHERE path=?", (path,)).fetchone() == actual_db.execute("SELECT * FROM documents WHERE path=?", (path,)).fetchone()
    for query in ("网格", "日本語", "한국어", "SecondaryHeading"):
        def hits(item):
            return sorted(row["filepath"] for row in search._engine({"op": "keyword", **item, "query": query, "limit": 10,
                                                                    "embedding_enabled": False, "model_uri": None}))
        assert hits(actual) == hits(expected)
    assert not Path(actual["corpus"]).exists()


def test_stage_slims_an_owned_v1_copy_without_reusing_its_search_text_cache(tmp_path):
    from advisor.library_search import _representation
    store, plan, old, *_ = fixture(tmp_path)
    source = Path(old["directory"]) / "objects.sqlite3"
    with closing(sqlite3.connect(source)) as db, db:
        rows = db.execute("SELECT rowid,id,library_id,kind,feature_branch,qmd_path,source_json FROM objects ORDER BY rowid").fetchall()
        statistics = dict(native_index.iter_representation_stats(db))
        db.execute("DROP TABLE objects")
        db.execute(native_index.V1_OBJECT_SQL)
        db.execute("CREATE INDEX objects_scope ON objects(library_id,kind,feature_branch)")
        db.execute("UPDATE object_metadata SET value=? WHERE key='schema'", (native_index.V1_SCHEMA,))
        for rowid, identity, owner, kind, branch, locator, source_json in rows:
            item = store.get(identity)
            text, _ = _representation(item)
            db.execute("""INSERT INTO objects(rowid,id,library_id,kind,feature_branch,qmd_path,representation_json,search_input,source_json)
                VALUES(?,?,?,?,?,?,?,?,?)""", (rowid, identity, owner, kind, branch, locator,
                native_index.pack(statistics[identity]), native_index.pack([item["title"], text, ""]), source_json))
        assert native_index.owned_schema(db)
    original = source.read_bytes()
    expected = database_rows(source)
    result = stage_tutorial_indexes(store, plan, engine_factory=FakeEngine, manual_strategy="sdk")
    assert source.read_bytes() == original
    retired = set(plan.iter_retirement_ids())
    assert {row[1]: row for row in database_rows(result["native_path"]) if row[1] not in retired and row[1] in {r[1] for r in expected}} == {
        row[1]: row for row in expected if row[1] not in retired}
    with closing(sqlite3.connect(result["native_path"])) as db:
        assert "search_input" not in {row[1] for row in db.execute("PRAGMA table_info(objects)")}
        assert db.execute("SELECT value FROM object_metadata WHERE key='schema'").fetchone() == (native_index.SCHEMA,)
    assert not list(Path(result["directory"]).glob(".qmd-keyword-inputs-*"))
