from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from advisor import case_navigation as module, catalog_storage, object_index
from advisor.evaluation_sets import HoldoutPolicy
from advisor.index_manifest_cache import read_manifest


def fixture(tmp_path):
    root = tmp_path / "libraries"
    directory = root / "search/generations" / ("a" * 20 + "-" + "b" * 8)
    directory.mkdir(parents=True)
    compact = root / "artifacts/case/compactcase.sqlite3"
    compact.parent.mkdir(parents=True)
    with closing(sqlite3.connect(compact)) as db:
        db.executescript("""CREATE TABLE symbols(id INTEGER PRIMARY KEY,value TEXT);
          INSERT INTO symbols VALUES(1,'MeshFeature'),(2,'Size'),(3,'Frame'),(4,'View'),(5,'hmax'),(6,'showPhysicsSymbols');
          CREATE TABLE nodes(id INTEGER PRIMARY KEY,kind_id INTEGER,type_id INTEGER);
          INSERT INTO nodes VALUES(1,1,2),(2,3,3),(3,4,4),(4,1,2);
          CREATE TABLE properties(node_id INTEGER,name_id INTEGER);
          INSERT INTO properties VALUES(1,5),(3,6),(4,6);
          CREATE TABLE selections(node_id INTEGER);""")
    compact_ref = {"path": compact.relative_to(root).as_posix(), "sha256": "immutable-reference"}

    def obj(identity, *, group, local, kind="model_case", library="models", branch=None, logical=None, data=None):
        data = dict(data or {})
        if branch:
            data["feature_branch"] = branch
        return {"id": identity, "object_id": logical or identity + "-logical", "library_id": library,
                "case_group": group, "local_id": local, "type": kind, "kind": "manual" if library == "docs" else "case",
                "title": "Metadata title " + identity, "text": "FORBIDDEN_SOURCE_BODY_COPY", "asset_key": "models:case.mph",
                "asset_hash": "same-immutable-mph", "data": data}

    base = obj("case-base", group="saved-group", local="model-case", logical="case-logical", data={"compact_case": compact_ref})
    authority = obj("case-api", group="api-group", local="model-case", logical="case-logical",
                    data={"compact_case": compact_ref, "base_case_id": base["id"], "base_case_group": base["case_group"]})
    saved = obj("saved-mesh", group="saved-group", local="compact-node-1-1", kind="saved_configuration", branch="mesh")
    frame = obj("saved-frame", group="saved-group", local="compact-node-2-1", kind="saved_configuration", branch="geometry")
    gui = obj("saved-gui", group="saved-group", local="compact-node-3-1", kind="saved_configuration", branch="misc")
    display = obj("saved-display", group="saved-group", local="compact-node-4-1", kind="saved_configuration", branch="mesh")
    api = obj("api-mesh", group="api-group", local="deep-api-node-1-1", kind="api_configuration", branch="solver")
    other = obj("case-other", group="other-group", local="model-case", data={"compact_case": compact_ref})
    tutorial = obj("tutorial-step", group="", local="tutorial-step", library="docs", kind="method_step", branch="mesh")
    rows = [base, authority, saved, frame, gui, display, api, other, tutorial]
    catalog = root / "catalog.sqlite3"
    with closing(sqlite3.connect(catalog)) as db, db:
        catalog_storage.initialize_revisions(db)
        db.execute("CREATE TABLE active(library_id TEXT,object_id TEXT,citation_id TEXT,PRIMARY KEY(library_id,object_id))")
        db.execute("CREATE TABLE case_members(group_id TEXT,local_id TEXT,citation_id TEXT,PRIMARY KEY(group_id,local_id))")
        db.executescript("""CREATE TABLE tutorial_model_documents(source_id TEXT,owner_library_id TEXT,source_sha256 TEXT);
          CREATE TABLE tutorial_associations(source_id TEXT,source_library_id TEXT,source_sha256 TEXT);
          CREATE TABLE tutorial_document_sources(source_sha256 TEXT,owner_library_id TEXT,citation_id TEXT,category TEXT);
          INSERT INTO tutorial_model_documents VALUES('case-base','models','canonical-hash');
          INSERT INTO tutorial_document_sources VALUES('canonical-hash','docs','tutorial-step','mesh');""")
        for item in rows:
            db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (item["id"], item["object_id"], item["library_id"], json.dumps(item)))
            if item["id"] != "case-base":
                db.execute("INSERT INTO active VALUES(?,?,?)", (item["library_id"], item["object_id"], item["id"]))
            if item["case_group"]:
                db.execute("INSERT INTO case_members VALUES(?,?,?)", (item["case_group"], item["local_id"], item["id"]))
    with closing(sqlite3.connect(directory / "objects.sqlite3")) as db, db:
        object_index.create_schema(db)
        for item in rows:
            if item["id"] != "case-base":
                object_index.insert_object(db, item, "temporary build text", {}, "")
    active = {"generation": "existing-generation", "corpus_hash": "existing-corpus", "directory": str(directory),
              "libraries": {}, "object_count": len(rows)-1, "modes": {"qmd_keyword": {"ready": True}}}
    (root / "search/active.json").write_text(json.dumps(active), encoding="utf-8")
    store = SimpleNamespace(root=root, db_path=catalog, enabled=False, policy=HoldoutPolicy(token="policy-v1"))
    store.evaluation_policy = lambda: store.policy
    return store, active, {item["id"]: item for item in rows}


def test_all_overviews_use_scalar_identity_joins_without_decoding_any_payload(tmp_path, monkeypatch):
    store, active, _ = fixture(tmp_path)
    monkeypatch.setattr(catalog_storage, "_unpack", lambda *_: pytest.fail("Overview identities must not decode source payloads"))
    result = module.build_navigation(store)
    assert result["overview_count"] == 2 and result["pilot_member_count"] == 0
    navigation = module.validated_navigation(store.root / "search", active)
    with closing(sqlite3.connect(navigation["path"])) as db:
        assert db.execute("SELECT id FROM overviews ORDER BY id").fetchall() == [("case-api",), ("case-other",)]
        assert db.execute("SELECT count(*) FROM members").fetchone() == (0,)
    assert not store.enabled


def test_pilot_unites_exact_pinned_base_api_and_canonical_tutorial_without_copying_body(tmp_path):
    store, active, _ = fixture(tmp_path)
    preserved = {path: path.read_bytes() for path in (store.db_path, Path(active["directory"]) / "objects.sqlite3", store.root / "search/active.json")}
    result = module.build_navigation(store, case_ids=["case-api"])
    assert result["overview_count"] == 2 and result["prepared_case_ids"] == ["case-api"]
    assert result["excluded_default_members"] == 3 and not result["source_text_stored"]
    assert {path: path.read_bytes() for path in preserved} == preserved and not store.enabled
    assert b"FORBIDDEN_SOURCE_BODY_COPY" not in Path(result["path"]).read_bytes()
    scope = module.case_scope(store.root / "search", active, "case-api")
    assert scope["library_id"] == "models" and scope["case_group"] == "api-group"
    with closing(sqlite3.connect(result["path"])) as db:
        rows = {row[0]: row[1:] for row in db.execute("SELECT object_id,role,stage,global_visible FROM members")}
        assert rows["saved-mesh"] == ("implementation", "mesh", 1)
        assert rows["api-mesh"] == ("implementation", "solver", 1)
        assert rows["tutorial-step"] == ("reference", "mesh", 1)
        assert rows["case-api"] == ("overview", "overview", 1)
        for identity in ("saved-frame", "saved-gui", "saved-display"):
            assert rows[identity][0] == "support" and rows[identity][2] == 0
        assert "case-base" not in rows and "case-other" not in rows
        plan = db.execute("EXPLAIN QUERY PLAN SELECT 1 FROM members WHERE object_id=? AND global_visible=1", ("saved-mesh",)).fetchall()
        assert any("members_object" in row[3] for row in plan)


def test_unprepared_case_and_old_overview_are_never_silently_widened(tmp_path):
    store, active, _ = fixture(tmp_path)
    module.build_navigation(store, case_ids=["case-api"])
    with pytest.raises(module.NavigationNotPrepared, match="not_prepared"):
        module.case_scope(store.root / "search", active, "case-other")
    with pytest.raises(FileNotFoundError, match="exact model_case"):
        module.case_scope(store.root / "search", active, "case-base")


def test_changed_object_directory_or_generation_is_stale_without_source_decoding(tmp_path):
    store, active, _ = fixture(tmp_path)
    module.build_navigation(store)
    with pytest.raises(module.NavigationStale):
        module.validated_navigation(store.root / "search", dict(active, generation="changed"))
    with closing(sqlite3.connect(Path(active["directory"]) / "objects.sqlite3")) as db, db:
        db.execute("DELETE FROM objects WHERE id='saved-frame'")
    with pytest.raises(module.NavigationStale):
        module.validated_navigation(store.root / "search", active)


@pytest.mark.parametrize("policy", [HoldoutPolicy(citation_ids=frozenset({"case-api"}), token="blocked"),
                                    HoldoutPolicy(case_groups=frozenset({"saved-group"}), token="blocked")])
def test_heldout_pilot_or_pinned_saved_base_cannot_replace_valid_navigation(tmp_path, policy):
    store, active, _ = fixture(tmp_path)
    result = module.build_navigation(store)
    original = Path(result["path"]).read_bytes()
    store.policy = policy
    with pytest.raises(PermissionError):
        module.build_navigation(store, case_ids=["case-api"])
    assert Path(result["path"]).read_bytes() == original
    assert not list(Path(result["path"]).parent.glob(module.FILENAME + ".*.tmp*"))
    # Physical overview IDs do not authorize a source; web policy changes do
    # not require recreating the unchanged metadata directory.
    assert module.validated_navigation(store.root / "search", active)["meta"]["overview_count"] == 2


def test_missing_or_inconsistent_saved_base_refuses_preparation(tmp_path):
    store, _, sources = fixture(tmp_path)
    with closing(sqlite3.connect(store.db_path)) as db, db:
        catalog_storage.register_catalog_functions(db)
        changed = dict(sources["case-api"], data=dict(sources["case-api"]["data"], base_case_group="wrong-group"))
        db.execute("UPDATE revisions SET payload=? WHERE id='case-api'", (json.dumps(changed),))
    with pytest.raises(ValueError, match="exact pinned saved base"):
        module.build_navigation(store, case_ids=["case-api"])


def test_failed_atomic_publication_preserves_old_sidecar_and_removes_stage(tmp_path, monkeypatch):
    store, _, _ = fixture(tmp_path)
    result = module.build_navigation(store)
    original = Path(result["path"]).read_bytes()
    monkeypatch.setattr(module.os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError("active reader")))
    with pytest.raises(PermissionError, match="active reader"):
        module.build_navigation(store, case_ids=["case-api"])
    assert Path(result["path"]).read_bytes() == original
    assert not list(Path(result["path"]).parent.glob(module.FILENAME + ".*.tmp*"))


@pytest.mark.parametrize("trigger", ["build", "reindex"])
@pytest.mark.parametrize("outcome", ["ready", "metadata-failed", "qmd-failed"])
def test_catalog_publication_refreshes_overviews_without_recursive_lease_or_downgrading_qmd(tmp_path, monkeypatch, trigger, outcome):
    from advisor.library_search import LibrarySearch
    from test_library_store import registered
    store, library, _ = registered(tmp_path)
    calls = []

    def built(_self, _objects, **kwargs):
        ready = outcome != "qmd-failed"
        return {"ready": ready, "generation": kwargs["generation"], "modes": {"qmd_keyword": {"ready": ready}}}

    def navigation(current_store, output=None, *, case_ids=()):
        assert current_store is store and store._build_lock.locked()
        assert (store.root / ".build.lock").is_file()
        assert case_ids is None
        calls.append(True)
        if outcome == "metadata-failed":
            raise RuntimeError("disposable metadata needs preparation")
        return {"overview_count": 2, "prepared_case_ids": [], "source_text_stored": False}

    monkeypatch.setattr(LibrarySearch, "build", built)
    monkeypatch.setattr(module, "build_navigation", navigation)
    monkeypatch.setattr('advisor.case_guides.build_case_guides', lambda *_args: {'state': 'ready', 'count': 2})
    result = store.build(library["id"], modes=["qmd_keyword"]) if trigger == "build" else store.reindex(["qmd_keyword"])
    indexes = result.get("indexes", result)
    assert calls == ([] if outcome == "qmd-failed" else [True])
    assert indexes["modes"]["qmd_keyword"]["ready"] is (outcome != "qmd-failed")
    if outcome == "metadata-failed":
        assert result["status"] == "completed"
        assert indexes["case_navigation"]["state"] == "need_rebuild"
    elif outcome == "ready":
        assert result["status"] == "completed" and indexes["case_navigation"]["state"] == "ready"
    else:
        assert result["status"] == "partial" and "case_navigation" not in indexes
    assert not store._build_lock.locked() and not (store.root / ".build.lock").exists()


def test_unchanged_publication_preserves_already_prepared_pilots(tmp_path, monkeypatch):
    from advisor.library_store import LibraryStore
    store, active, _ = fixture(tmp_path)
    prepared = module.build_navigation(store, case_ids=["case-api"])
    original = Path(prepared["path"]).read_bytes()
    monkeypatch.setattr(module, "build_navigation", lambda *_args, **_kwargs: pytest.fail("An unchanged valid generation retains its pilot scopes"))
    indexes = {"unchanged": True, "modes": {"qmd_keyword": {"ready": True}}}
    LibraryStore._refresh_case_navigation(store, indexes)
    assert indexes["case_navigation"]["unchanged"]
    assert module.case_scope(store.root / "search", active, "case-api")["case_id"] == "case-api"
    assert Path(prepared["path"]).read_bytes() == original


def test_purge_removes_complete_derived_caches_and_owned_temps_only(tmp_path):
    from advisor.library_index_purge import purge_library_indexes
    from test_library_index_purge import fixture as indexed
    root, search, _, keeper, generation = indexed(tmp_path)
    runtime = root / "runtime"
    runtime.mkdir(exist_ok=True)
    derived = [runtime / module.FILENAME, runtime / "case-guides.json",
               runtime / ("case-guides.json." + "a" * 32 + ".tmp")]
    for path in derived:
        path.write_bytes(b"Derived hints referring to the deleted library")
    custom = runtime / "custom-notes.json"
    custom.write_bytes(b"User-owned notes remain")
    result = purge_library_indexes(root, "deleted", generation=generation)
    assert result["navigation_cache_files_removed"] == 3
    assert not any(path.exists() for path in derived)
    assert custom.read_bytes() == b"User-owned notes remain"
    assert search.search("laminar", mode="qmd_keyword")[0]["id"] == keeper["id"]
