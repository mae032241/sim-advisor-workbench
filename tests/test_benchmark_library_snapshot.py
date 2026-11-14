"""Independent persisted flags reach the real backend without modifying source."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from advisor.library_store import LibraryStore
from advisor.library_search import LibrarySearch
from scripts import benchmark_library_snapshot as snapshot


def test_snapshot_flags_are_persisted_and_catalog_is_independent(tmp_path, monkeypatch):
    source = LibraryStore(tmp_path / 'source/libraries')
    originals = tmp_path / 'originals'
    originals.mkdir()
    for identity, kind in [('official', 'official-models'), ('custom', 'user-models')]:
        source.register(identity, kind, [{'id': 'models', 'path': str(originals)}], library_id=identity)
    source.set_enabled('custom', True)
    index = source.root / 'search/generations/test'
    index.mkdir(parents=True)
    with closing(sqlite3.connect(index / 'objects.sqlite3')) as db:
        db.execute('CREATE TABLE objects(id TEXT)')
    (source.root / 'search/active.json').write_text('{"generation":"test"}')
    def manifest(path, **kwargs):
        return {'directory': str(Path(path).parent / 'generations/test'), 'generation': 'test', 'corpus_hash': 'corpus'}
    monkeypatch.setattr(snapshot, 'read_manifest', manifest)
    # Sidecar validation itself has its own integration tests. This fixture
    # isolates snapshot ownership and real persisted backend lifecycle reads.
    monkeypatch.setattr('advisor.case_context_store.validated_contexts', lambda *args: None)
    monkeypatch.setattr('advisor.case_navigation.validated_navigation', lambda *args: None)
    target = tmp_path / 'copy/libraries'
    report = snapshot.prepare_snapshot(source.root, target)
    assert report['production_flags_changed'] is False
    assert snapshot.source_flags(source.root) == {'official': False, 'custom': True}
    assert snapshot.source_flags(target) == {'official': True, 'custom': False}
    assert LibrarySearch.for_maintenance(target / 'search')._searchable_library_ids() == {'official'}
    assert not source.db_path.samefile(target / 'catalog.sqlite3')
    assert snapshot.require_snapshot(source.root, target) == report
    LibraryStore(target).set_enabled('official', False)
    assert LibrarySearch.for_maintenance(target / 'search')._searchable_library_ids() == set()
    assert source.library('custom')['enabled'] is True
    # Generation/policy changes invalidate a reused preparation.
    (source.root / 'search/active.json').write_text('{"generation":"new"}')
    with pytest.raises(RuntimeError, match='no longer matches'):
        snapshot.require_snapshot(source.root, target)


def test_snapshot_refuses_source_alias_and_existing_destination(tmp_path):
    source = LibraryStore(tmp_path / 'libraries')
    with pytest.raises(ValueError, match='independent'):
        snapshot.require_snapshot(source.root, source.root)
    with pytest.raises(FileExistsError):
        snapshot.prepare_snapshot(source.root, source.root)
