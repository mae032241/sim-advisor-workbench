"""Retired tutorial citations resolve explicitly after duplicate bodies leave SQL."""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from advisor.catalog_storage import register_catalog_functions
from advisor.tutorial_aliases import apply_relink, purge_aliases
from advisor.tutorial_catalog import refresh_links
from advisor.tutorial_migration import apply_plan_to_catalog, build_tutorial_migration_plan
from test_tutorial_migration import fixture


def migrated(tmp_path):
    store, originals, cases, copies, pdfs = fixture(tmp_path)
    plan = build_tutorial_migration_plan(store, tmp_path / "plan.sqlite3")
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        apply_plan_to_catalog(db, plan)
        refresh_links(db)
    return store, plan, originals, cases, copies, pdfs


def relink(store, plan):
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        return apply_relink(db, plan)


def test_relink_removes_bodies_and_redundant_metadata_without_changing_active_citations(tmp_path):
    store, plan, originals, cases, copies, pdfs = migrated(tmp_path)
    before = store.current_generation()
    active = store.objects()
    result = relink(store, plan)
    retired = [*originals, *copies, *pdfs]
    assert result["aliases"] == result["payloads_removed"] == len(retired)
    assert store.current_generation() == before and store.objects() == active
    assert store.get(cases[0]["id"]) == cases[0]
    with closing(store.connect()) as db:
        assert db.execute("SELECT count(*) FROM tutorial_aliases").fetchone()[0] == len(retired)
        for old in retired:
            assert not db.execute("SELECT 1 FROM revisions WHERE id=?", (old["id"],)).fetchone()
            assert not db.execute("SELECT 1 FROM _revision_payloads WHERE id=?", (old["id"],)).fetchone()
            assert not db.execute("SELECT 1 FROM tutorial_associations WHERE source_id=?", (old["id"],)).fetchone()
        assert db.execute("SELECT count(*) FROM tutorial_retired_sources").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM case_members WHERE group_id=?", (cases[0]["case_group"],)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM tutorial_associations WHERE source_id=?", (cases[0]["id"],)).fetchone()[0] == 1
    assert relink(store, plan)["payloads_removed"] == 0


def test_old_get_read_related_and_evidence_route_to_explicit_canonical_alias_without_model_db(tmp_path, monkeypatch):
    store, plan, originals, cases, copies, pdfs = migrated(tmp_path)
    relink(store, plan)
    compact = store.root / cases[0]["data"]["compact_case"]["path"]
    compact.unlink()
    monkeypatch.setattr(store, "_compact_context", lambda *_: pytest.fail("Alias must not open removed model passages"))
    for old in [*originals, *copies, *pdfs]:
        resolved = store.get(old["id"])
        assert resolved["id"] != old["id"] and resolved["library_id"] == "cs63-documents"
        assert resolved["alias"]["requested_id"] == old["id"]
        assert resolved["alias"]["resolved_id"] == resolved["id"]
        assert resolved["alias"]["original_owner_library_id"] == old["library_id"]
        assert resolved["read_scope"] == "canonical_reference"
        # Target is an ordinary HTML paragraph and has no compact authority.
        monkeypatch.setattr(store, "_compact_context", lambda *_: None)
        assert store.read(old["id"])["alias"] == resolved["alias"]
        assert store.source_available(old["id"])
        assert store.related_sources(old["id"])[0]["id"] == resolved["id"]
        evidence = store.read_evidence(old["id"], limit=20)
        assert evidence["id"] == resolved["id"] and evidence["alias"] == resolved["alias"]
        assert "complete explanation" in evidence["text"]


@pytest.mark.parametrize("owner", ["models", "cs63-documents"])
@pytest.mark.parametrize("lifecycle", ["close", "delete"])
def test_alias_get_and_public_routes_gate_original_and_target_owners(tmp_path, owner, lifecycle):
    store, plan, _, cases, copies, _ = migrated(tmp_path)
    relink(store, plan)
    old = copies[0]["id"]
    if lifecycle == "close":
        store.set_enabled(owner, False)
    else:
        store.delete_library(owner)
    assert not store.source_available(old)
    for reader in (store.get, store.read, store.read_evidence, store.related_sources):
        with pytest.raises(PermissionError):
            reader(old)
    if owner == "cs63-documents":
        assert store.get(cases[0]["id"]) is not None


def test_relink_is_transactional_and_rejects_active_sources_or_missing_canonical_targets(tmp_path):
    store, plan, _, _, copies, _ = migrated(tmp_path)
    with closing(store.connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        apply_relink(db, plan)
        db.rollback()
    assert store.get(copies[0]["id"]) == copies[0]
    with closing(store.connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO active VALUES(?,?,?)", (copies[0]["library_id"], copies[0]["object_id"], copies[0]["id"]))
        with pytest.raises(ValueError, match="Active tutorial"):
            apply_relink(db, plan)
        db.rollback()
    with closing(store.connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        target = next(plan.iter_canonical_objects())
        db.execute("DELETE FROM active WHERE citation_id=?", (target["id"],))
        with pytest.raises(ValueError, match="active canonical"):
            apply_relink(db, plan)
        db.rollback()


def test_explicit_source_proof_allows_relinking_a_staged_catalog_clone(tmp_path):
    store, plan, *_ = migrated(tmp_path)
    clone = tmp_path / "staged-catalog.sqlite3"
    with closing(store.connect()) as old, closing(sqlite3.connect(clone)) as new:
        old.backup(new)
    with closing(register_catalog_functions(sqlite3.connect(clone))) as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="explicitly name"):
            apply_relink(db, plan)
        result = apply_relink(db, plan, source_catalog=store.db_path)
        assert result["aliases"] > 0
        db.rollback()


def test_active_target_validation_uses_complete_active_primary_key(tmp_path):
    store, plan, *_ = migrated(tmp_path)
    statements = []
    with closing(store.connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        db.set_trace_callback(statements.append)
        apply_relink(db, plan)
        db.set_trace_callback(None)
        query = next(sql for sql in statements if "LEFT JOIN active a ON" in sql)
        steps = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query)]
        lookup = next(step for step in steps if "SEARCH a " in step)
        assert "library_id=? AND object_id=?" in lookup
        db.rollback()


@pytest.mark.parametrize("owner", ["models", "cs63-documents"])
def test_permanent_purge_of_either_owner_removes_aliases(tmp_path, owner):
    store, plan, _, _, copies, _ = migrated(tmp_path)
    relink(store, plan)
    with closing(store.connect()) as db, db:
        assert purge_aliases(db, owner) > 0
    assert store.get(copies[0]["id"]) is None and not store.source_available(copies[0]["id"])


@pytest.mark.parametrize("owner", ["models", "cs63-documents"])
def test_library_store_permanent_purge_removes_cross_library_aliases(tmp_path, owner, monkeypatch):
    store, plan, _, _, copies, _ = migrated(tmp_path)
    relink(store, plan)
    monkeypatch.setattr("advisor.library_index_purge.purge_library_indexes", lambda *args, **kwargs: {"warnings": []})
    store.delete_library(owner)
    result = store.purge_library(owner, confirmed=True)
    assert result["counts"]["tutorial_aliases"] > 0
    assert store.get(copies[0]["id"]) is None
    assert not store.source_available(copies[0]["id"])


def test_removed_old_tutorial_relations_do_not_return_obsolete_unresolved_stubs(tmp_path):
    store, plan, _, cases, copies, _ = migrated(tmp_path)
    # Add a historical local relation before relinking; its payload stays exact.
    import json
    with closing(store.connect()) as db, db:
        case = dict(cases[0], relations=[{"type": "context", "target_local_id": copies[0]["local_id"]}])
        db.execute("UPDATE revisions SET payload=? WHERE id=?", (json.dumps(case), case["id"]))
    relink(store, plan)
    assert all(row["status"] != "unresolved" for row in store.related_sources(case["id"], limit=40))
    store.set_enabled("cs63-documents", False)
    assert store.related_sources(case["id"], limit=40) == []
