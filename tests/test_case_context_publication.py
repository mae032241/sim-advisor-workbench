"""Real small-store publication, recovery, and normal rebuild integration."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from advisor import case_context_index as coordinator
from advisor import case_context_store as contexts
from advisor import index_manifest
from advisor.case_navigation import build_navigation, validated_navigation
from advisor.library_search import LibrarySearch
from advisor.library_store import LibraryStore, encoded
from advisor.object_index import qmd_path, representation_stats
from advisor.qmd_worker import close_workers
from advisor.tutorial_catalog import register_objects
from scripts.enrich_case_contexts import main as enrich_main
from test_library_search import obj

OFFLINE = Path(r"D:\software\sim-offline\sim-advisor-offline-win-x64-0.2.2")


def _put(store, items):
    with closing(store.connect()) as db, db:
        for item in items:
            db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (item["id"], item["object_id"], item["library_id"], encoded(item)))
            db.execute("INSERT INTO active VALUES(?,?,?)", (item["library_id"], item["object_id"], item["id"]))
            if item.get("case_group"):
                db.execute("INSERT INTO case_members VALUES(?,?,?)", (item["case_group"], item["local_id"], item["id"]))
        register_objects(db, items)


@pytest.fixture
def publication(tmp_path, monkeypatch):
    node, sdk = OFFLINE / "node/node.exe", OFFLINE / "qmd/node_modules/@tobilu/qmd"
    if not node.is_file() or not (sdk / "dist/store.js").is_file():
        pytest.skip("Installed offline QMD 2.8.3 SDK required")
    monkeypatch.setenv("SIM_ADVISOR_NODE", str(node))
    monkeypatch.setenv("SIM_ADVISOR_QMD_DIR", str(sdk))
    monkeypatch.setenv("SIM_ADVISOR_SEARCH_CACHE", str(tmp_path / "cache"))
    sources = tmp_path / "sources"
    sources.mkdir()
    archive = sources / "unseen_channel.mph"
    archive.write_bytes(b"immutable typed model archive\x00\xffsaved state")
    html = sources / "unseen_channel.html"
    html.write_text("Canonical source archive fixture", encoding="utf-8")
    store = LibraryStore(tmp_path / "libraries")
    for library, kind in (("models", "official-models"), ("manuals", "official-manuals")):
        store.register(library, kind, [{"id": "root", "path": str(sources)}], library_id=library)
        store.set_enabled(library, True)
    cases = []
    for index in range(2):
        item = obj("publication-case-" + str(index), "models", "Original bounded model overview")
        item.update(type="model_case", local_id="model-case", case_group="publication-group-" + str(index),
                    asset_key="root:unseen_channel.mph", asset_hash="saved-archive-hash",
                    coverage={"source_container": {"status": "saved_model"},
                              "raw_xml": {"status": "bounded_saved_state", "nodes_omitted_by_limit": 0},
                              "sections": {"geometry": 2, "materials": 1, "physics": 1, "studies": 1, "solvers": 1}})
        item["data"]["tutorial_refs"] = [{"source_sha256": "canonical-document-sha", "identity": "unseen_channel"}]
        cases.append(item)
    mesh = obj("publication-mesh", "models", "Original saved mesh implementation")
    mesh.update(type="saved_configuration", local_id="mesh-setting", case_group=cases[0]["case_group"])
    mesh["data"]["feature_branch"] = "mesh"
    sections = []
    for index, (title, text) in enumerate([
        ("Introduction", "The example investigates a rotating channel carrying a reactive suspension under a prescribed periodic inlet. "
                         "The objective is to compare the outlet concentration over time for several rotation rates."),
        ("Model Definition", "The channel uses an axisymmetric approximation with an insulated exterior wall and a fixed inlet temperature. "
                             "Momentum transport is coupled to species transport through the temperature dependent reaction rate."),
    ], 1):
        section = obj("publication-section-" + str(index), "manuals", text)
        section.update(type="case_context", kind="manual", title=title)
        section["data"].update(section=title, tutorial_order=index, chapter_order=index, part=1, parts=1,
                               canonical_tutorial={"source_sha256": "canonical-document-sha", "text_sha256": hashlib.sha256(text.encode()).hexdigest()})
        section["evidence"] = [{"locator": "html:#section-" + str(index), "method": "tutorial_html_section"}]
        sections.append(section)
    items = [*cases, mesh, *sections]
    _put(store, items)
    search = LibrarySearch(store.root / "search", qmd_dir=sdk, node=node, cache_dir=tmp_path / "cache")
    with store.object_stream() as objects:
        built = search.build(objects, modes=["qmd_keyword"], generation=objects.generation)
    assert built["modes"]["qmd_keyword"]["ready"]
    navigation = build_navigation(store, case_ids=[case["id"] for case in cases])
    result = SimpleNamespace(store=store, search=search, cases=cases, sections=sections, mesh=mesh,
                             items=items, root=store.root / "search", archives=[archive, html],
                             runtime={"qmd_dir": sdk, "node": node}, navigation=navigation)
    yield result
    close_workers()


def _active(fixture):
    return fixture.search._load_active(summary=True)


def _qmd(fixture):
    path = Path(_active(fixture)["directory"]) / "qmd-keyword.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        return {row[2] + "/" + row[3]: row for row in db.execute("""
          SELECT d.id,d.rowid,d.collection,d.path,d.title,d.hash,d.created_at,d.modified_at,d.active,c.doc,c.created_at,
                 f.filepath,f.title,f.body FROM documents d JOIN content c ON c.hash=d.hash
          JOIN documents_fts f ON f.rowid=d.id ORDER BY d.id""")}


def _stats(fixture):
    path = Path(_active(fixture)["directory"]) / "objects.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        return {item["id"]: representation_stats(db, item["id"]) for item in fixture.items}


def _source_snapshot(fixture):
    with closing(fixture.store.connect()) as db:
        rows = [tuple(row) for row in db.execute("SELECT id,object_id,library_id,payload FROM revisions ORDER BY id")]
    return rows, {str(path): path.read_bytes() for path in fixture.archives}


def _sidecar_rows(fixture):
    path = contexts.context_path(fixture.root)
    if not path.exists():
        return None
    with closing(sqlite3.connect(path)) as db:
        return {table: db.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
                for table in ("contexts", "dependencies", "capabilities")}


def test_metadata_rebind_after_file_identity_change_keeps_qmd_and_sources_exact(publication):
    fixture = publication
    coordinator.update_case_contexts(fixture.store.root, [fixture.cases[0]["id"]], apply=True)
    before, source, stats = _qmd(fixture), _source_snapshot(fixture), _stats(fixture)
    path = Path(_active(fixture)["directory"]) / "objects.sqlite3"
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    with pytest.raises(contexts.CaseContextStale):
        contexts.validated_contexts(fixture.root, _active(fixture))
    result = coordinator.rebind_case_contexts(fixture.store.root)
    assert result["metadata_only"] and result["qmd_documents_changed"] == 0
    assert contexts.validated_contexts(fixture.root, _active(fixture))
    assert _qmd(fixture) == before and _source_snapshot(fixture) == source and _stats(fixture) == stats


def test_script_dry_run_leaves_qmd_source_archives_and_index_metadata_unchanged(publication, tmp_path, capsys):
    fixture = publication
    old_active, old_qmd, old_stats, source = _active(fixture), _qmd(fixture), _stats(fixture), _source_snapshot(fixture)
    path = Path(old_active["directory"]) / "qmd-keyword.sqlite3"
    qmd_bytes = path.read_bytes()
    report = tmp_path / "dry-run.json"
    assert enrich_main(["--data-dir", str(fixture.store.root), "--case", fixture.cases[0]["id"], "--report", str(report)]) == 0
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["dry_run"] and result["profiles"][fixture.cases[0]["id"]]["state"] == "ready"
    assert result["source_parsing"] is result["embedding_invoked"] is result["full_qmd_rebuild"] is False
    assert result["capability_counts"] == {"configured": 2}
    assert "profiles" not in json.loads(capsys.readouterr().out)
    assert path.read_bytes() == qmd_bytes and _qmd(fixture) == old_qmd
    assert _stats(fixture) == old_stats and _active(fixture) == old_active
    assert _source_snapshot(fixture) == source
    assert not contexts.context_path(fixture.root).exists() and not contexts.pending_path(fixture.root).exists()


def test_script_apply_changes_only_selected_overview_and_repeating_is_idempotent(publication, capsys):
    fixture = publication
    before, old_stats, source, active = _qmd(fixture), _stats(fixture), _source_snapshot(fixture), _active(fixture)
    arguments = ["--data-dir", str(fixture.store.root), "--case", fixture.cases[0]["id"], "--apply"]
    assert enrich_main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert type(first["changed_documents"]) is int and first["changed_documents"] == 1
    after = _qmd(fixture)
    changed = [path for path in before if before[path] != after[path]]
    assert changed == [qmd_path(fixture.cases[0])]
    assert before[changed[0]][:4] == after[changed[0]][:4]
    assert "DOCUMENTED CASE CONTEXT" in after[changed[0]][9]
    assert _source_snapshot(fixture) == source
    assert _active(fixture)["generation"] == active["generation"] and _active(fixture)["directory"] == active["directory"]
    stats = _stats(fixture)
    assert [identity for identity in stats if stats[identity] != old_stats[identity]] == [fixture.cases[0]["id"]]
    assert stats[fixture.cases[0]["id"]]["case_context_included"] is True
    assert contexts.validated_contexts(fixture.root, _active(fixture))["meta"]["context_count"] == 1
    validated_navigation(fixture.root, _active(fixture))
    assert _active(fixture)["bridge_version"] == "2-context2"
    hits = fixture.search.search("reactive suspension", library_ids=["models"], retrieval_view="case_overviews", limit=20)
    assert [hit["id"] for hit in hits] == [fixture.cases[0]["id"]]
    profiles = _sidecar_rows(fixture)
    assert enrich_main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["changed_documents"] == 0
    assert _qmd(fixture) == after and _stats(fixture) == stats
    assert _sidecar_rows(fixture) == profiles and _source_snapshot(fixture) == source
    assert not contexts.pending_path(fixture.root).exists()


@pytest.mark.parametrize("existing_profile", [False, True])
def test_publication_failure_rolls_back_small_qmd_undo_profiles_manifest_and_statistics(publication, monkeypatch, existing_profile):
    fixture = publication
    if existing_profile:
        coordinator.update_case_contexts(fixture.store.root, [fixture.cases[0]["id"]], apply=True)
    target = fixture.cases[1 if existing_profile else 0]["id"]
    before, stats, source, manifest, sidecar = _qmd(fixture), _stats(fixture), _source_snapshot(fixture), _active(fixture), _sidecar_rows(fixture)
    real_publish, calls = index_manifest.publish_manifest, []
    def fail_once(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("injected context publication failure")
        return real_publish(*args, **kwargs)
    monkeypatch.setattr(index_manifest, "publish_manifest", fail_once)
    with pytest.raises(OSError, match="injected context publication failure"):
        coordinator.update_case_contexts(fixture.store.root, [target], apply=True)
    assert len(calls) == 2
    assert _qmd(fixture) == before and _stats(fixture) == stats
    assert _active(fixture) == manifest and _sidecar_rows(fixture) == sidecar
    assert _source_snapshot(fixture) == source
    assert not contexts.pending_path(fixture.root).exists()
    assert not (fixture.store.root / ".build.lock").exists()
    validated_navigation(fixture.root, _active(fixture))
    if existing_profile:
        contexts.validated_contexts(fixture.root, _active(fixture))


@pytest.mark.parametrize("existing_profile", [False, True])
@pytest.mark.parametrize("rollback_completed", [False, True])
def test_pending_recover_restores_interrupted_committed_patch_then_is_idempotent(publication, monkeypatch, capsys, existing_profile, rollback_completed):
    from advisor import qmd_patch
    fixture = publication
    if existing_profile:
        coordinator.update_case_contexts(fixture.store.root, [fixture.cases[0]["id"]], apply=True)
    before, stats, source, manifest = _qmd(fixture), _stats(fixture), _source_snapshot(fixture), _active(fixture)
    sidecar = _sidecar_rows(fixture)
    target = fixture.cases[1 if existing_profile else 0]["id"]
    real_publish = index_manifest.publish_manifest
    real_undo = qmd_patch.restore_keyword_document_patch
    def fail_publish(*args, **kwargs):
        raise OSError("publication and immediate rollback unavailable")
    def fail_undo(*args, **kwargs):
        raise OSError("publication and immediate rollback unavailable")
    monkeypatch.setattr(index_manifest, "publish_manifest", fail_publish)
    if not rollback_completed:
        monkeypatch.setattr(qmd_patch, "restore_keyword_document_patch", fail_undo)
    with pytest.raises(OSError, match="publication and immediate rollback unavailable"):
        coordinator.update_case_contexts(fixture.store.root, [target], apply=True)
    marker = contexts.pending_path(fixture.root)
    assert marker.exists()
    journal = json.loads(marker.read_text(encoding="utf-8"))
    assert journal["undo"]["records"] and len(journal["undo"]["records"]) == 1
    assert (_qmd(fixture) == before) is rollback_completed
    if not rollback_completed:
        # An actual crashed writer also leaves its small catalog build lease.
        dead_pid = 2147483000
        assert not LibrarySearch._pid_alive(dead_pid)
        journal["pid"] = dead_pid
        marker.write_text(json.dumps(journal, sort_keys=True), encoding="utf-8")
        (fixture.store.root / ".build.lock").write_text(json.dumps({"pid": dead_pid, "token": "dead-context-writer"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="pending"):
        fixture.search.search("rotating channel")
    monkeypatch.setattr(index_manifest, "publish_manifest", real_publish)
    monkeypatch.setattr(qmd_patch, "restore_keyword_document_patch", real_undo)
    assert enrich_main(["--data-dir", str(fixture.store.root), "--recover"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["recovered"] and report["restored_documents"] == 1
    assert not marker.exists()
    assert not (fixture.store.root / ".build.lock").exists()
    assert _qmd(fixture) == before and _stats(fixture) == stats and _active(fixture) == manifest
    assert _source_snapshot(fixture) == source and _sidecar_rows(fixture) == sidecar
    if existing_profile:
        contexts.validated_contexts(fixture.root, _active(fixture))
    assert enrich_main(["--data-dir", str(fixture.store.root), "--recover"]) == 0
    assert json.loads(capsys.readouterr().out)["recovered"] is False


def test_regular_new_catalog_reindex_rederives_and_keeps_published_context(publication):
    fixture = publication
    case_id = fixture.cases[0]["id"]
    coordinator.update_case_contexts(fixture.store.root, [case_id], apply=True)
    old = _active(fixture)
    old_profile = contexts.read_contexts(fixture.root, old)[case_id]
    old_qmd = _qmd(fixture)[qmd_path(fixture.cases[0])]
    source = _source_snapshot(fixture)
    addition = obj("publication-new-setting", "models", "New unrelated catalog revision")
    _put(fixture.store, [addition])
    with fixture.store.object_stream() as objects:
        built = fixture.search.build(objects, modes=["qmd_keyword"], generation=objects.generation)
    assert built["modes"]["qmd_keyword"]["ready"]
    active = _active(fixture)
    assert active["generation"] != old["generation"] and active["directory"] != old["directory"]
    assert active["case_context_count"] == 1 and active["case_contexts_required"] is True
    profile = contexts.read_contexts(fixture.root, active)[case_id]
    assert profile["content_hash"] == old_profile["content_hash"] and profile["qmd_hash"] == old_profile["qmd_hash"]
    assert _qmd(fixture)[qmd_path(fixture.cases[0])][4:6] == old_qmd[4:6]
    assert _qmd(fixture)[qmd_path(fixture.cases[0])][9] == old_qmd[9]
    assert _stats(fixture)[case_id]["case_context_included"] is True
    hits = fixture.search.search("reactive suspension", library_ids=["models"], limit=20)
    assert [hit["id"] for hit in hits] == [case_id]
    current_sources = _source_snapshot(fixture)
    assert current_sources[1] == source[1]
    assert [row for row in current_sources[0] if row[0] != addition["id"]] == source[0]
