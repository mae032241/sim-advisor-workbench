"""The offline coordinator never publishes a partial catalog/index pair."""
from contextlib import closing
from pathlib import Path

import pytest

from advisor.index_manifest import publish_manifest, stored_manifest
from advisor.tutorial_index_migration import _desired_fingerprints
from advisor.tutorial_migration import build_tutorial_migration_plan
from scripts import normalize_tutorials as coordinator
from test_tutorial_migration import fixture


def _old_indexes(store):
    root = store.root / "search"
    directory = root / "generations" / "old-generation"
    directory.mkdir(parents=True)
    manifest = {"ready": True, "generation": store.current_generation(), "directory": str(directory),
                "object_count": len(store.objects()), "libraries": {}, "modes": {"native": {"ready": True}}}
    publish_manifest(directory, manifest, root)
    return directory, {name: (root / name).read_bytes() for name in ("active.json", "last-build.json")}


def _fake_stage(store, plan, on_progress=None):
    root = store.root / "search"
    directory = root / "generations" / "new-generation"
    directory.mkdir(parents=True)
    corpus = directory / "manual" / "corpus"
    corpus.mkdir(parents=True)
    (corpus / "temporary.md").write_text("temporary manual export", encoding="utf-8")
    fingerprint, generation, count = _desired_fingerprints(store, plan, None)
    manifest = {"ready": True, "generation": generation, "directory": str(directory),
                "corpus_hash": fingerprint, "object_count": count, "libraries": {}, "modes": {"native": {"ready": True}}}
    return {"manifest": stored_manifest(manifest, root), "directory": str(directory), "corpus_directories": [str(corpus)],
            "counts": {"objects": count}, "previous_manifest": {"generation": plan.metadata()["source_generation"]}}


def test_plan_only_uses_no_store_or_workspace_constructor_writes(tmp_path, monkeypatch):
    store, *_ = fixture(tmp_path)
    before = store.current_generation()
    with closing(store.connect()) as db:
        tables = list(db.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
    monkeypatch.setattr(coordinator.LibraryStore, "__init__", lambda *a, **k: pytest.fail("plan-only must not initialize catalog schema"))
    monkeypatch.setattr(coordinator.WorkspacePaths, "__init__", lambda *a, **k: pytest.fail("plan-only must not update path anchors"))
    result = coordinator.run_normalization(store.root, tmp_path / "plan.sqlite3", plan_only=True)
    assert result["status"] == "planned" and store.current_generation() == before
    with closing(store.connect()) as db:
        assert list(db.execute("SELECT name,sql FROM sqlite_master ORDER BY name")) == tables
    assert not (store.root / ".build.lock").exists() and not (store.root / "search").exists()


def test_staging_failure_keeps_catalog_and_root_pointers(tmp_path, monkeypatch):
    store, *_ = fixture(tmp_path)
    old, snapshots = _old_indexes(store)
    plan = build_tutorial_migration_plan(store, tmp_path / "plan.sqlite3")
    before = store.current_generation()
    def fail(*args, **kwargs):
        raise RuntimeError("injected QMD staging failure")
    monkeypatch.setattr(coordinator, "stage_tutorial_indexes", fail)
    with pytest.raises(RuntimeError, match="staging failure"):
        coordinator.run_normalization(store.root, plan.path, apply_existing_plan=True)
    assert store.current_generation() == before and old.is_dir()
    assert all((store.root / "search" / name).read_bytes() == raw for name, raw in snapshots.items())
    assert not (store.root / ".build.lock").exists()
    assert not (store.root / ".catalog-migration.lock").exists()


def test_publication_failure_restores_exact_pointers_and_rolls_back_catalog(tmp_path, monkeypatch):
    store, *_ = fixture(tmp_path)
    old, snapshots = _old_indexes(store)
    plan = build_tutorial_migration_plan(store, tmp_path / "plan.sqlite3")
    before = store.current_generation()
    monkeypatch.setattr(coordinator, "stage_tutorial_indexes", _fake_stage)
    def fail_after_pointer_publication(*args, **kwargs):
        publish_manifest(*args, **kwargs)
        raise RuntimeError("injected publication failure")
    monkeypatch.setattr(coordinator, "publish_manifest", fail_after_pointer_publication)
    with pytest.raises(RuntimeError, match="publication failure"):
        coordinator.run_normalization(store.root, plan.path, apply_existing_plan=True)
    assert store.current_generation() == before and old.is_dir()
    assert all((store.root / "search" / name).read_bytes() == raw for name, raw in snapshots.items())
    assert not (store.root / "search" / "generations" / "new-generation").exists()
    assert not (store.root / ".tutorial-migration-recovery.json").exists()
    assert not (store.root / ".catalog-migration.lock").exists()


def test_success_retains_old_generation_and_removes_only_new_manual_corpus(tmp_path, monkeypatch):
    store, originals, _, copies, pdfs = fixture(tmp_path)
    old, _ = _old_indexes(store)
    plan = build_tutorial_migration_plan(store, tmp_path / "plan.sqlite3")
    monkeypatch.setattr(coordinator, "stage_tutorial_indexes", _fake_stage)
    result = coordinator.run_normalization(store.root, plan.path, apply_existing_plan=True)
    assert result["status"] == "applied" and result["generation"] == store.current_generation()
    assert old.is_dir()
    assert not (store.root / "search" / "generations" / "new-generation" / "manual" / "corpus").exists()
    assert (store.root / "search" / "generations" / "new-generation" / "manifest.json").is_file()
    assert all(store.get(obj["id"]) == obj for obj in [*originals, *copies, *pdfs])
    assert not (store.root / ".build.lock").exists() and not (store.root / ".catalog-migration.lock").exists()
