"""Offline tutorial migration keeps citations, boundaries, and staged deltas."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from advisor import compact_case
from advisor.library_store import LibraryStore, encoded, file_hash
from advisor.tutorial_migration import (
    TutorialMigrationPlan, apply_plan_to_catalog, build_tutorial_migration_plan,
    ensure_tutorial_association_schema,
)


def _put(store, objects):
    with closing(store.connect()) as db, db:
        for obj in objects:
            db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (obj["id"], obj["object_id"], obj["library_id"], encoded(obj)))
            db.execute("INSERT INTO active VALUES(?,?,?)", (obj["library_id"], obj["object_id"], obj["id"]))
            if obj.get("case_group"):
                db.execute("INSERT INTO case_members VALUES(?,?,?)", (obj["case_group"], obj["local_id"], obj["id"]))


def _record(path, root, root_id):
    return {"path": str(path), "root_id": root_id, "relative_path": path.relative_to(root).as_posix(), "content_hash": file_hash(path)}


def _source(local_id, type_, title, text, path, locator="document", data=None):
    return {"local_id": local_id, "type": type_, "kind": "manual" if type_ == "manual_topic" else "case",
            "title": title, "text": text, "data": data or {},
            "evidence": [{"asset_path": str(path), "locator": locator, "method": "fixture", "status": "documented"}]}


def fixture(tmp_path, *, second_document=False):
    docs, models = tmp_path / "docs", tmp_path / "models"
    models.mkdir()
    docs.mkdir()
    store = LibraryStore(tmp_path / "libraries")
    manuals = store.register("COMSOL manuals", "official-manuals", [{"id": "docs", "path": str(docs), "role": "docs"}],
                             "6.3", library_id="cs63-documents")
    manuals = store.set_enabled(manuals["id"], True)
    model_library = store.register("COMSOL models", "official-models", [
        {"id": "models", "path": str(models)}, {"id": "docs", "path": str(docs), "role": "docs"}], "6.3", library_id="models")
    model_library = store.set_enabled(model_library["id"], True)
    originals, cases, tutorial_copies, pdfs = [], [], [], []
    for number in range(2 if second_document else 1):
        name = "first" if number == 0 else "second"
        package = docs / ("com.comsol.help.models.test." + name)
        package.mkdir()
        html = package / (name + ".html")
        html.write_text('<html><head><title>' + name + '</title></head><body>\n'
                        '<h1>' + name + '</h1>\n<h2 id="intro">Introduction</h2>\n'
                        '<p id="intro-p">A complete explanation for ' + name + '.</p>\n'
                        '<h2 id="notes">Notes About Implementation</h2>\n<p id="notes-p">Do not lose this arbitrary section.</p>\n'
                        '<h2 id="instructions">Modeling Instructions</h2>\n<h3 id="mesh">Mesh</h3>\n'
                        '<p id="mesh-p">Set the mesh size for ' + name + '.</p>\n</body></html>', encoding="utf-8")
        pdf = package / ("models.test." + name + ".pdf")
        pdf.write_bytes(b"This PDF is not parsed during migration: " + name.encode())
        source = store._prepare(manuals, _record(html, docs, "docs"),
                                _source("topic", "manual_topic", name, html.read_text(encoding="utf-8"), html))
        originals.append(source)
        for page in range(1, 3):
            obj = store._prepare(manuals, _record(pdf, docs, "docs"),
                                 _source(f"page-{page}", "manual_topic", f"{name} {page}", "Repeated PDF text", pdf, f"page:{page}"))
            pdfs.append(obj)
        # Identical model bytes deliberately reference different documents.
        mph = models / (name + ".mph")
        mph.write_bytes(b"same preview model bytes; never reparsed")
        database = store.root / "artifacts" / name / compact_case.DATABASE
        database.parent.mkdir(parents=True)
        text = "A complete explanation for " + name + "."
        with closing(sqlite3.connect(database)) as db:
            compact_case._schema(db, native_index=False)
            db.execute("INSERT INTO model(id,format,source_path,source_sha256,source_size_bytes,parser_signature,xml_member,xml_size_bytes) VALUES(1,?,?,?,?,?,'dmodel.xml',0)",
                       (compact_case.FORMAT, str(mph), file_hash(mph), mph.stat().st_size, "fixture"))
            db.execute("INSERT INTO symbols VALUES(1,'associated_tutorial_html')")
            db.execute("INSERT INTO documents VALUES(1,?,?,1,NULL)", (html.as_uri(), file_hash(html)))
            db.execute("INSERT INTO text_pool VALUES(1,?)", (text,))
            db.execute("INSERT INTO metadata_pool VALUES(1,?)", (encoded({"locators": ["html:#intro-p"], "truncated": False}),))
            db.execute("INSERT INTO passages VALUES(1,1,1,NULL,NULL,1,1)")
            db.commit()
        authority = store._prepare(model_library, _record(mph, models, "models"), _source(
            "model-case", "model_case", name, "preview only", mph, "compact:case",
            {"compact_case": {"path": database.relative_to(store.root).as_posix(), "sha256": file_hash(database), "format": compact_case.FORMAT}}))
        parts = []
        # Three fragments must produce one full compact passage read/reuse.
        for part in range(3):
            parts.append(store._prepare(model_library, _record(mph, models, "models"), _source(
                f"compact-passage-1-{part+1}", "case_context", "Introduction", text[part:part+5], html, "html:#intro-p",
                {"compact_ref": {"passage_id": 1, "text_start": part, "text_end": part+5}})))
        pinned = store._pin_case_group([authority, *parts])
        cases.append(pinned[0])
        tutorial_copies.extend(pinned[1:])
    _put(store, [*originals, *pdfs, *cases, *tutorial_copies])
    return store, originals, cases, tutorial_copies, pdfs


def test_plan_is_read_only_streamed_complete_and_deduplicates_passage_fragments(tmp_path, monkeypatch):
    store, originals, cases, copies, pdfs = fixture(tmp_path)
    generation = store.current_generation()
    monkeypatch.setattr(store, "objects", lambda *a, **k: pytest.fail("Plan must stream compressed catalog objects"))
    monkeypatch.setattr(store, "read", lambda *a, **k: pytest.fail("Do not hydrate every fragment into the complete passage"))
    plan = build_tutorial_migration_plan(store, tmp_path / "plan.sqlite3")
    assert store.current_generation() == generation
    assert all(store.get(obj["id"]) == obj for obj in [*originals, *cases, *copies, *pdfs])
    targets = list(plan.iter_canonical_objects())
    assert targets and all(obj["library_id"] == "cs63-documents" and obj["kind"] == "manual" for obj in targets)
    text = "\n".join(obj["text"] for obj in targets)
    assert text.count("A complete explanation for first.") == 1
    assert text.count("Do not lose this arbitrary section.") == 1
    assert text.count("Set the mesh size for first.") == 1
    assert plan.summary()["retirements_by_reason"] == {"model_tutorial_copy": 3, "verified_html_companion": 2, "whole_tutorial_html": 1}
    reused = list(plan.iter_compact_text_reuse())
    assert len(reused) == 1 and reused[0]["passage_id"] == 1
    assert reused[0]["text_sha256"] == hashlib.sha256("A complete explanation for first.".encode()).hexdigest()
    source_index = list(plan.iter_document_sources())
    assert len(source_index) == len(targets)
    assert {item["citation_id"]: item["anchor"] for item in source_index} == {
        item["id"]: item["evidence"][0]["locator"] for item in targets}
    relations = list(plan.iter_relations())
    assert len(relations) == len(list(plan.iter_retirements())) + len(cases)
    assert len({item["source_id"] for item in relations}) == len(relations)
    assert plan.metadata()["index_policy"] == {"native": "incremental", "qmd": "affected_libraries_only", "embed": False}


def test_publication_preserves_old_revision_payloads_and_is_transactional(tmp_path):
    store, originals, cases, copies, pdfs = fixture(tmp_path)
    plan = build_tutorial_migration_plan(store, tmp_path / "plan.sqlite3")
    before = store.current_generation()
    with closing(store.connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        apply_plan_to_catalog(db, plan)
        assert db.execute("SELECT COUNT(*) FROM tutorial_associations").fetchone()[0] > 0
        db.rollback()
    assert store.current_generation() == before
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        apply_plan_to_catalog(db, plan)
    active = store.objects()
    assert cases[0] in active
    assert all(obj not in active for obj in [*originals, *copies, *pdfs])
    for old in [*originals, *copies, *pdfs]:
        assert store.get(old["id"]) == old
    with closing(store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM tutorial_document_sources").fetchone()[0] == len(list(plan.iter_document_sources()))
        retired = {row[0]: tuple(row[1:]) for row in db.execute("SELECT * FROM tutorial_retired_sources")}
        assert len(retired) == len(list(plan.iter_retirements()))
        assert retired[copies[0]["id"]] == ("models", "model_tutorial_copy", originals[0]["asset_hash"])
        assert db.execute("SELECT COUNT(*) FROM case_members WHERE group_id=?", (cases[0]["case_group"],)).fetchone()[0] == 4
    # No active whole HTML remains to parse again on a second migration.
    again = build_tutorial_migration_plan(store, tmp_path / "again.sqlite3")
    assert again.summary()["counts"]["retirements"] == 0
    assert again.summary()["counts"]["canonical_objects"] == 0


def test_identical_preview_mph_hashes_do_not_merge_distinct_document_passages(tmp_path):
    store, originals, cases, copies, pdfs = fixture(tmp_path, second_document=True)
    assert cases[0]["asset_hash"] == cases[1]["asset_hash"]
    plan = build_tutorial_migration_plan(store, tmp_path / "plan.sqlite3")
    assert plan.summary()["counts"]["selected_html"] == 2
    targets = {item["id"]: item for item in plan.iter_canonical_objects()}
    relations = list(plan.iter_relations())
    for case, original in zip(cases, originals):
        links = [item for item in relations if item["source_id"] == case["id"]]
        assert links and {item["source_sha256"] for item in links} == {original["asset_hash"]}
        assert all(targets[item["target_id"]]["asset_hash"] == original["asset_hash"] for item in links)
    assert len(list(plan.iter_compact_text_reuse())) == 2


def test_canonical_payloads_match_normal_unchanged_manual_preparation(tmp_path):
    from advisor.tutorial_sources import extract_tutorial_objects
    store, originals, *_ = fixture(tmp_path)
    plan = build_tutorial_migration_plan(store, tmp_path / "plan.sqlite3")
    library = store.library("cs63-documents")
    root = Path(library["roots"][0]["path"])
    path = root / originals[0]["evidence"][0]["relative_path"]
    extracted = extract_tutorial_objects(path, root=root)
    ordinary = [store._prepare(library, _record(path, root, "docs"),
                              dict(obj, coverage=obj.get("coverage") or extracted["coverage"]))
                for obj in extracted["objects"]]
    assert list(plan.iter_canonical_objects()) == ordinary


def test_unverified_or_unselected_html_does_not_retire_pdf_or_model_copies(tmp_path):
    store, originals, cases, copies, pdfs = fixture(tmp_path)
    path = next(Path(root["path"]) for root in store.library("cs63-documents")["roots"]) / originals[0]["evidence"][0]["relative_path"]
    path.write_text("Changed HTML source", encoding="utf-8")
    plan = build_tutorial_migration_plan(store, tmp_path / "plan.sqlite3")
    assert not list(plan.iter_retirements()) and not list(plan.iter_canonical_objects())
    assert any(item["code"] == "tutorial_html_preserved" for item in plan.iter_warnings())


def test_closed_owner_boundaries_and_stale_plan_are_enforced(tmp_path):
    store, originals, cases, copies, pdfs = fixture(tmp_path)
    store.set_enabled("models", False)
    plan = build_tutorial_migration_plan(store, tmp_path / "plan.sqlite3")
    assert "models" in plan.summary()["protected_owner_libraries"]
    assert not any(item["library_id"] == "models" for item in plan.iter_retirements())
    store.set_enabled("cs63-documents", False)
    with closing(store.connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="ownership/lifecycle"):
            apply_plan_to_catalog(db, plan)
        db.rollback()
    closed = build_tutorial_migration_plan(store, tmp_path / "closed.sqlite3")
    assert not list(closed.iter_retirements()) and not list(closed.iter_canonical_objects())


def test_user_manual_tutorial_is_not_a_model_copy_and_cannot_be_official_canonical_owner(tmp_path):
    store, originals, *_ = fixture(tmp_path)
    official = store.library('cs63-documents')
    root = Path(official['roots'][0]['path'])
    path = root / originals[0]['evidence'][0]['relative_path']
    custom = store.register('My tutorial manual', 'user-manuals', [{'id': 'docs', 'path': str(root)}], library_id='custom-manual')
    custom = store.set_enabled(custom['id'], True)
    item = store._prepare(custom, _record(path, root, 'docs'),
                          _source('my-tutorial', 'case_context', 'My tutorial context', 'Independent manual context', path))
    _put(store, [item])
    plan = build_tutorial_migration_plan(store, tmp_path / 'plan.sqlite3')
    assert not any(row['library_id'] == custom['id'] for row in plan.iter_retirements())
    with closing(sqlite3.connect(plan.path)) as db:
        assert db.execute('SELECT COUNT(*) FROM tutorial_sources WHERE library_id=?', (custom['id'],)).fetchone() == (0,)
    assert store.get(item['id']) == item
    with pytest.raises(ValueError, match='official-manuals'):
        build_tutorial_migration_plan(store, tmp_path / 'custom-canonical.sqlite3', canonical_library_id=custom['id'])


def test_schema_creation_does_not_commit_or_require_plan_publication(tmp_path):
    store, *_ = fixture(tmp_path)
    with closing(store.connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_tutorial_association_schema(db)
        assert db.in_transaction
        db.rollback()
    with pytest.raises(ValueError, match="caller-owned"):
        with closing(store.connect()) as db:
            apply_plan_to_catalog(db, tmp_path / "absent-plan.sqlite3")


def test_existing_plan_is_never_overwritten(tmp_path):
    store, *_ = fixture(tmp_path)
    path = tmp_path / "plan.sqlite3"
    plan = build_tutorial_migration_plan(store, path)
    with pytest.raises(FileExistsError):
        build_tutorial_migration_plan(store, path)
    assert TutorialMigrationPlan(plan.path).summary()["status"] == "ready"


def test_source_change_between_review_and_publication_keeps_catalog_intact(tmp_path):
    store, originals, *_ = fixture(tmp_path)
    plan = build_tutorial_migration_plan(store, tmp_path / "plan.sqlite3")
    before = store.current_generation()
    root = Path(store.library("cs63-documents")["roots"][0]["path"])
    path = root / originals[0]["evidence"][0]["relative_path"]
    path.write_text("HTML changed during review", encoding="utf-8")
    with closing(store.connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="source changed after"):
            apply_plan_to_catalog(db, plan)
        db.rollback()
    assert store.current_generation() == before
