"""Explicit retrieval scopes default off and change atomically without indexing."""
from contextlib import closing
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.library_store import LibraryLifecycleConflict, LibraryStore


def fixture(tmp_path):
    source = tmp_path / 'sources'
    source.mkdir()
    store = LibraryStore(tmp_path / 'data/libraries')
    for lid, kind, name in [('manual', 'official-manuals', 'Acoustics manual'),
                            ('model', 'official-models', 'Acoustics model cases'),
                            ('custom', 'user-models', 'My configured models')]:
        store.register(name, kind, [{'id': 'root', 'role': 'models', 'path': str(source)}], version='6.3', library_id=lid)
    return store


def bodies(store):
    with closing(store.connect()) as db:
        return dict(db.execute('SELECT id,body FROM libraries ORDER BY id'))


def test_new_library_is_disabled_until_explicit_enable_and_saved_choices_survive_reopen(tmp_path):
    store = fixture(tmp_path)
    assert all(item['enabled'] is False for item in store.libraries())
    assert store.searchable_library_ids() == []
    assert store.jobs() == [] and not (store.root / 'search').exists()
    result = store.set_enabled_many(['manual', 'model', 'manual'], True)
    assert result['library_ids'] == ['manual', 'model'] and result['changed'] == 2
    assert result['index_rebuilt'] is False
    reopened = LibraryStore(store.root)
    assert reopened.searchable_library_ids() == ['manual', 'model']
    assert reopened.library('custom')['enabled'] is False
    assert reopened.jobs() == [] and not (store.root / 'search').exists()
    before = bodies(store)
    assert reopened.set_enabled_many(['manual', 'model'], True)['changed'] == 0
    assert bodies(store) == before


def test_scope_is_a_lightweight_tree_and_filters_do_not_read_jobs_objects_or_indexes(tmp_path, monkeypatch):
    store = fixture(tmp_path)
    store.set_enabled('model', True)
    store.delete_library('custom')
    for method in ('jobs', 'job', 'objects', 'object_stream', 'index_status'):
        monkeypatch.setattr(store, method, lambda *args, **kwargs: pytest.fail('Scope must only read library metadata'))
    statements = []
    connect = store.connect
    def traced():
        db = connect()
        db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(store, 'connect', traced)
    result = store.library_scope()
    assert result['library_count'] == 2 and result['enabled_library_ids'] == ['model']
    assert result['enabled_library_count'] == 1
    official, custom = result['groups']
    assert [group['id'] for group in official['groups']] == ['official-manuals', 'official-models']
    assert official['groups'][0]['libraries'][0]['id'] == 'manual'
    assert official['groups'][1]['libraries'][0]['id'] == 'model' and custom['libraries'] == []
    assert set(result['libraries'][0]) == {'id', 'name', 'type', 'version', 'enabled', 'object_count', 'roots'}
    assert set(result['libraries'][0]['roots'][0]) == {'id', 'role', 'path'}
    assert [item['id'] for item in store.library_scope(query='ACOUSTICS', type='official-models')['libraries']] == ['model']
    assert store.library_scope(query='missing')['libraries'] == []
    assert not any('FROM builds' in sql or 'FROM revisions' in sql or 'FROM objects' in sql for sql in statements)


@pytest.mark.parametrize('ids,enabled', [(None, True), ([], True), ('manual', True), ([1], True), ([''], True),
                                       (['manual'] * 1001, True), (['manual'], 1), (['manual'], None)])
def test_bad_bulk_scope_inputs_do_not_change_any_library(tmp_path, ids, enabled):
    store = fixture(tmp_path)
    before = bodies(store)
    with pytest.raises(ValueError):
        store.set_enabled_many(ids, enabled)
    assert bodies(store) == before


@pytest.mark.parametrize('invalid', ['unknown', 'deleted'])
def test_bulk_scope_rejects_unknown_or_deleted_library_before_any_metadata_change(tmp_path, invalid):
    store = fixture(tmp_path)
    if invalid == 'deleted':
        store.delete_library('custom')
    before = bodies(store)
    with pytest.raises(FileNotFoundError):
        store.set_enabled_many(['manual', 'custom' if invalid == 'deleted' else 'missing'], True)
    assert bodies(store) == before


@pytest.mark.parametrize('owner', [{'library_id': 'model'}, {'library_ids': ['model'], 'scope': 'selected', 'kind': 'reindex'},
                                  {'scope': 'all_libraries', 'kind': 'reindex'}])
def test_bulk_scope_checks_active_ownership_atomically_without_hydrating_full_jobs(tmp_path, monkeypatch, owner):
    store = fixture(tmp_path)
    store._save_job({'id': 'active', 'status': 'running', 'pid': os.getpid(), 'workspace_root': str(store.paths.root),
                     'items': [{'diagnostic': 'retained large history ' * 5000}], **owner})
    monkeypatch.setattr(store, 'jobs', lambda: pytest.fail('No full jobs history'))
    monkeypatch.setattr(store, 'job', lambda *args, **kwargs: pytest.fail('Live ownership headers suffice'))
    before = bodies(store)
    with pytest.raises(LibraryLifecycleConflict):
        store.set_enabled_many(['manual', 'model'], True)
    assert bodies(store) == before


def test_bulk_scope_keeps_old_indexes_and_legacy_missing_enabled_default(tmp_path, monkeypatch):
    store = fixture(tmp_path)
    with closing(store.connect()) as db, db:
        body = json.loads(db.execute("SELECT body FROM libraries WHERE id='manual'").fetchone()[0])
        body.pop('enabled')
        db.execute("UPDATE libraries SET body=? WHERE id='manual'", (json.dumps(body),))
    assert store.library('manual')['enabled'] is True
    marker = store.root / 'search/generations/retained/qmd-vector.sqlite3'
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b'Existing vectors and index data remain byte-identical')
    for method in ('build', 'start_build', 'reindex'):
        monkeypatch.setattr(store, method, lambda *args, **kwargs: pytest.fail('Scope changes never construct indexes'))
    before = marker.read_bytes(), marker.stat().st_mtime_ns, store.current_generation()
    assert store.set_enabled_many(['manual', 'model', 'custom'], False)['changed'] == 1
    assert (marker.read_bytes(), marker.stat().st_mtime_ns, store.current_generation()) == before
    assert store.searchable_library_ids() == [] and store.jobs() == []


@pytest.mark.parametrize('kwargs', [{'query': 4}, {'query': 'x' * 2001}, {'type': 'missing'}, {'type': []}])
def test_scope_filters_validate_before_loading_metadata(tmp_path, kwargs):
    with pytest.raises(ValueError):
        fixture(tmp_path).library_scope(**kwargs)
