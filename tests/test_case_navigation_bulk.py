"""Full-library membership preparation remains bounded and revision pinned."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from advisor import case_navigation as module, catalog_storage, object_index
from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.case_context_store import write_contexts
from advisor.evaluation_sets import HoldoutPolicy, _watermark, exclusion_snapshot
from test_case_navigation import fixture


def extend(store, active, sources, *, cases=12, unrelated=0):
    """Add active cases plus bodies that the compiler must never decode."""
    rows = []
    for index in range(cases):
        identity = f"bulk-{index:03d}"
        case = dict(sources["case-other"], id=identity, object_id=identity + "-logical",
                    case_group=identity + "-group", asset_key=identity + ".mph", asset_hash=identity + "-hash")
        rows.append(case)
        for member in range(3):
            item = dict(case, id=f"{identity}-member-{member}", object_id=f"{identity}-member-{member}-logical",
                        local_id=f"node-{member}", type="saved_configuration", data={"feature_branch": "mesh"})
            rows.append(item)
    for index in range(unrelated):
        rows.append(dict(sources["tutorial-step"], id=f"unrelated-{index}", object_id=f"unrelated-{index}-logical"))
    with closing(sqlite3.connect(store.db_path)) as catalog, catalog, closing(sqlite3.connect(
            Path(active["directory"]) / "objects.sqlite3")) as objects, objects:
        catalog_storage.register_catalog_functions(catalog)
        for item in rows:
            catalog.execute("INSERT INTO revisions VALUES(?,?,?,?)",
                            (item["id"], item["object_id"], item["library_id"], json.dumps(item)))
            catalog.execute("INSERT INTO active VALUES(?,?,?)", (item["library_id"], item["object_id"], item["id"]))
            if item["case_group"]:
                catalog.execute("INSERT INTO case_members VALUES(?,?,?)", (item["case_group"], item["local_id"], item["id"]))
            object_index.insert_object(objects, item, "ignored build text", {}, "")
    return {row["id"]: row for row in rows}


def replace_payload(store, source):
    with closing(sqlite3.connect(store.db_path)) as db, db:
        catalog_storage.register_catalog_functions(db)
        db.execute("UPDATE revisions SET payload=? WHERE id=?", (json.dumps(source), source["id"]))


def test_full_navigation_streams_all_cases_without_decoding_member_bodies(tmp_path, monkeypatch):
    store, active, sources = fixture(tmp_path)
    added = extend(store, active, sources, cases=24, unrelated=1500)
    preserved = {path: path.read_bytes() for path in (
        store.db_path, Path(active["directory"]) / "objects.sqlite3", store.root / "search/active.json")}
    decoded = []
    unpack = catalog_storage._unpack

    def record_unpack(raw):
        value = unpack(raw)
        decoded.append(json.loads(value)["id"])
        return value

    monkeypatch.setattr(catalog_storage, "_unpack", record_unpack)
    live_rows = []
    pilot_rows = module._pilot_rows

    class CaseRows(list):
        def __del__(self):
            live_rows.clear()

    def bounded_rows(*args):
        assert not live_rows, "The preceding case's members must be released before reading another"
        live_rows.append(True)
        return CaseRows(pilot_rows(*args))

    monkeypatch.setattr(module, "_pilot_rows", bounded_rows)
    progress = []
    result = module.build_navigation(store, case_ids=None, progress=progress.append)
    assert result["build_scope"] == "all" and result["prepared_case_count"] == result["overview_count"] == 26
    assert result["prepared_case_ids"] is None
    assert result["excluded_default_members"] == 3
    assert len(decoded) == 27 and set(decoded) == {"case-api", "case-other", "case-base", *(
        item["id"] for item in added.values() if item["type"] == "model_case")}
    assert not live_rows
    assert progress[-1]["processed_cases"] == progress[-1]["prepared_case_count"] == 26
    assert {path: path.read_bytes() for path in preserved} == preserved
    assert not store.enabled and not result["qmd_rebuilt"] and not result["embedding_invoked"]
    assert b"FORBIDDEN_SOURCE_BODY_COPY" not in Path(result["path"]).read_bytes()
    navigation = module.validated_navigation(store.root / "search", active)
    assert "prepared_case_ids" not in navigation["meta"]
    assert len(list(module.iter_prepared_case_ids(navigation))) == 26
    assert module.case_scope(store.root / "search", active, "bulk-023")["case_group"] == "bulk-023-group"
    with closing(sqlite3.connect(result["path"])) as db:
        assert db.execute("SELECT role,stage,global_visible FROM members WHERE object_id='saved-frame'").fetchone() == ("support", "geometry", 0)
        assert db.execute("SELECT role,stage FROM members WHERE object_id='tutorial-step'").fetchone() == ("reference", "mesh")
        assert db.execute("SELECT count(*) FROM members WHERE object_id LIKE 'unrelated-%'").fetchone() == (0,)


def test_explicit_cases_have_no_five_case_limit(tmp_path):
    store, active, sources = fixture(tmp_path)
    added = extend(store, active, sources)
    selected = [item["id"] for item in added.values() if item["type"] == "model_case"]
    report = module.build_navigation(store, case_ids=iter([*selected, selected[0]]))
    assert report["prepared_case_ids"] == selected and report["prepared_case_count"] == 12
    assert report["build_scope"] == "selected"
    assert set(module.iter_prepared_case_ids(module.validated_navigation(store.root / "search", active))) == set(selected)
    with pytest.raises(module.NavigationNotPrepared):
        module.case_scope(store.root / "search", active, "case-api")


def test_full_navigation_skips_holdouts_and_only_confirmed_geometry(tmp_path):
    store, active, sources = fixture(tmp_path)
    added = extend(store, active, sources)
    geometry = dict(added["bulk-000"], coverage={"sections": {
        "geometry": 1, "physics": 0, "materials": 0, "studies": 0, "solvers": 0},
        "raw_xml": {"status": "saved_state", "nodes_omitted_by_limit": 0}})
    replace_payload(store, geometry)
    configured = dict(added["bulk-001"], coverage={"sections": {"physics": 1}, "raw_xml": {"status": "saved_state"}})
    replace_payload(store, configured)
    store.policy = HoldoutPolicy(citation_ids=frozenset({"bulk-002"}),
        case_groups=frozenset({"saved-group", "bulk-003-group"}), source_hashes=frozenset({"bulk-004-hash"}),
        asset_keys=frozenset({("models", "bulk-005.mph")}),
        qmd_paths=frozenset({object_index.qmd_path(added["bulk-006"])}), token="full-holdouts")
    report = module.build_navigation(store, case_ids=None)
    assert report["overview_count"] == 14 and report["prepared_case_count"] == 7
    assert report["skipped_heldout"] == 6 and report["skipped_geometry_only"] == 1
    prepared = set(module.iter_prepared_case_ids(module.validated_navigation(store.root / "search", active)))
    assert prepared == {"case-other", "bulk-001", *(f"bulk-{index:03d}" for index in range(7, 12))}
    # Explicit preparation can still target an intentionally selected geometry case.
    assert module.build_navigation(store, case_ids=["bulk-000"])["prepared_case_count"] == 1


def test_bound_search_capabilities_are_used_for_full_scope(tmp_path):
    store, active, _ = fixture(tmp_path)
    write_contexts(store.root / "search", active, {}, {"case-other": "geometry_only"})
    result = module.build_navigation(store, case_ids=None)
    assert result["prepared_case_count"] == 1 and result["skipped_geometry_only"] == 1
    assert list(module.iter_prepared_case_ids(module.validated_navigation(store.root / "search", active))) == ["case-api"]


def test_full_navigation_keeps_administrator_holdouts_closed_when_retrieval_is_open(tmp_path):
    store, active, _ = fixture(tmp_path)
    with closing(module._read_only(store.db_path)) as db:
        watermark = _watermark(db)
    exclusions = {name: [] for name in ("citation_ids", "object_ids", "case_groups", "source_hashes", "asset_keys", "qmd_paths")}
    exclusions["citation_ids"] = ["case-other"]
    registry = {"schema": "evaluation-sets/v1", "sets": [{"id": "heldout", "name": "Held out",
        "models": [{"library_id": "models", "asset_key": "models:case.mph"}]}],
        "exclusions": exclusions, "catalog_revision_watermark": watermark}
    (store.root / "evaluation-sets.json").write_text(json.dumps(registry), encoding="utf-8")
    (store.root / "evaluation-access.json").write_text(json.dumps({"schema": "evaluation-access/v1", "enabled": True}), encoding="utf-8")
    store.evaluation_policy = lambda: exclusion_snapshot(store.root)
    assert not store.evaluation_policy().blocks_identity("case-other")
    result = module.build_navigation(store, case_ids=None)
    assert result["prepared_case_count"] == result["skipped_heldout"] == 1
    navigation = module.validated_navigation(store.root / "search", active)
    assert list(module.iter_prepared_case_ids(navigation)) == ["case-api"]
    assert navigation["meta"]["compiled_policy_token"] != store.evaluation_policy().token


def test_v2_case_scope_uses_indexed_sql_without_enumerating_prepared_cases(tmp_path, monkeypatch):
    store, active, _ = fixture(tmp_path)
    result = module.build_navigation(store, case_ids=["case-api"])
    with closing(sqlite3.connect(result["path"])) as db, db:
        db.executemany("INSERT INTO overviews VALUES(?,?,?)", ((f"extra-{index}", "models", f"group-{index}") for index in range(4000)))
        db.executemany("INSERT INTO prepared_cases VALUES(?)", ((f"extra-{index}",) for index in range(4000)))
        db.execute("UPDATE meta SET value='4001' WHERE key='prepared_case_count'")
    statements = []
    read_only = module._read_only

    def traced(path):
        db = read_only(path)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(module, "_read_only", traced)
    scope = module.case_scope(store.root / "search", active, "extra-3999")
    assert scope["case_id"] == "extra-3999" and scope["prepared_case_count"] == 4001
    assert not any(sql.startswith("SELECT case_id FROM prepared_cases") for sql in statements)
    assert "prepared_case_ids" not in scope
    query = next(sql for sql in statements if "LEFT JOIN prepared_cases" in sql)
    with closing(sqlite3.connect(result["path"])) as db:
        plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query))
    assert "SEARCH o USING" in plan and "SEARCH p USING" in plan and "SCAN" not in plan


def test_legacy_v1_pilot_navigation_remains_readable(tmp_path):
    store, active, _ = fixture(tmp_path)
    result = module.build_navigation(store, case_ids=["case-api"])
    with closing(sqlite3.connect(result["path"])) as db, db:
        db.execute("UPDATE meta SET value=? WHERE key='schema'", (json.dumps(module.LEGACY_SCHEMA),))
        db.execute("INSERT INTO meta VALUES('prepared_case_ids',?)", (json.dumps(["case-api"]),))
        db.execute("DROP TABLE prepared_cases")
    navigation = module.validated_navigation(store.root / "search", active)
    assert list(module.iter_prepared_case_ids(navigation)) == ["case-api"]
    assert module.case_scope(store.root / "search", active, "case-api")["case_id"] == "case-api"
    with pytest.raises(module.NavigationNotPrepared):
        module.case_scope(store.root / "search", active, "case-other")


@pytest.mark.parametrize("change", ["cancel", "catalog", "generation", "policy", "classification"])
def test_interrupted_or_changed_snapshot_preserves_published_navigation(tmp_path, change):
    store, active, sources = fixture(tmp_path)
    if change == "catalog":
        with closing(sqlite3.connect(store.db_path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
    if change == "classification":
        write_contexts(store.root / "search", active, {}, {"case-other": "unknown"})
    old = module.build_navigation(store)
    old_bytes = Path(old["path"]).read_bytes()
    stop = threading.Event()
    changed = False

    def progress(update):
        nonlocal changed
        if update["processed_cases"] != 1 or changed:
            return
        changed = True
        if change == "cancel":
            stop.set()
        elif change == "catalog":
            with closing(sqlite3.connect(store.db_path)) as db, db:
                catalog_storage.register_catalog_functions(db)
                extra = dict(sources["case-other"], id="new-revision", object_id="new-logical")
                db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (extra["id"], extra["object_id"], extra["library_id"], json.dumps(extra)))
        elif change == "generation":
            (store.root / "search/active.json").write_text(json.dumps(dict(active, generation="changed")), encoding="utf-8")
        elif change == "policy":
            store.policy = HoldoutPolicy(token="changed")
        else:
            # Appending bytes changes the immutable metadata file signature
            # without requiring a write lock against the compiler's reader.
            with (store.root / "search/runtime/case-contexts.sqlite3").open("ab") as stream:
                stream.write(b"changed")

    with build_control(BuildControl(tmp_path / "cancel", stop)):
        with pytest.raises(BuildCancelled if change == "cancel" else module.NavigationStale):
            module.build_navigation(store, case_ids=None, progress=progress)
    assert changed and Path(old["path"]).read_bytes() == old_bytes
    assert not list(Path(old["path"]).parent.glob(module.FILENAME + ".*.tmp*"))


def test_invalidation_removes_new_guide_databases_and_owned_temps(tmp_path):
    search = tmp_path / "search"
    runtime = search / "runtime"
    runtime.mkdir(parents=True)
    files = [runtime / ("case-guides.sqlite3" + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    files.append(runtime / ("case-guides.sqlite3." + "a" * 32 + ".tmp-journal"))
    for path in files:
        path.write_bytes(b"derived")
    custom = runtime / "case-guides-user-notes.json"
    custom.write_text("retain", encoding="utf-8")
    assert module.invalidate_navigation(search)["removed_files"] == len(files)
    assert all(not path.exists() for path in files) and custom.read_text(encoding="utf-8") == "retain"
