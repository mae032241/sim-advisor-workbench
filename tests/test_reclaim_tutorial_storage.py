from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import sqlite3

import pytest

from advisor.index_manifest import publish_manifest
from advisor.tutorial_catalog import refresh_links
from advisor.tutorial_index_migration import stage_tutorial_indexes
from advisor.tutorial_migration import apply_plan_to_catalog
from scripts.reclaim_tutorial_storage import reclaim, file_hash
from test_tutorial_index_migration import fixture, FakeEngine


def prepared(tmp_path):
    store, plan, old, *_ = fixture(tmp_path)
    staged = stage_tutorial_indexes(store, plan, engine_factory=FakeEngine, manual_strategy='sdk')
    with closing(store.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        apply_plan_to_catalog(db, plan)
        refresh_links(db)
    publish_manifest(Path(staged['directory']),staged['manifest'],store.root/'search')
    return store,plan


def test_full_reclaim_relinks_and_removes_physical_bodies(tmp_path):
    store,plan = prepared(tmp_path)
    before = store.objects()
    result = reclaim(store.root.parent, plan.path, lambda event: None)
    assert result['status']=='completed' and result['qmd_files_unchanged']
    assert store.objects()==before
    assert result['aliases']['payloads_removed'] == plan.summary()['counts']['retirements']
    for old in plan.iter_retirements():
        assert store.read(old['id'])['alias']['requested_id']==old['id']
    assert not list(store.root.parent.glob('.tutorial-reclaim-*'))
    assert not (store.root/'.build.lock').exists()
    assert not (store.root/'.catalog-migration.lock').exists()


def test_post_publish_failure_restores_original_files(tmp_path):
    store,plan = prepared(tmp_path)
    catalog_before=file_hash(store.db_path)
    models={p:file_hash(p) for p in (store.root/'artifacts').rglob('compactcase.sqlite3')}
    with patch('scripts.reclaim_tutorial_storage.ReadOnlyLibraryStore.configuration',side_effect=RuntimeError('Injected read failure')):
        with pytest.raises(RuntimeError,match='Injected'):
            reclaim(store.root.parent, plan.path, lambda event: None)
    assert file_hash(store.db_path)==catalog_before
    assert all(file_hash(p)==digest for p,digest in models.items())
    assert not list(store.root.parent.glob('.tutorial-reclaim-*'))
    assert not (store.root/'.catalog-migration.lock').exists()


def test_reclaim_preserves_closed_libraries_without_requiring_public_reads(tmp_path):
    store,plan = prepared(tmp_path)
    store.set_enabled('cs63-documents',False)
    store.set_enabled('models',False)
    result=reclaim(store.root.parent,plan.path,lambda event:None)
    assert result['status']=='completed'
    assert not store.library('cs63-documents')['enabled']
    assert not store.library('models')['enabled']
    old=next(plan.iter_retirements())['id']
    assert not store.source_available(old)
    with pytest.raises(PermissionError):
        store.read(old)
