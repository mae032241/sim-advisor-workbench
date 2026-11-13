"""One query validates one generation and opens one payload catalog handle."""
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from advisor import object_index as native_index
from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_search import LibrarySearch
from advisor.retrieval_settings import update as update_embedding_settings
from test_evaluation_search import policy_for
from test_library_search import FakeEngineSearch, obj
from test_native_index_compaction import catalog


def test_direct_search_validates_active_and_last_build_only_once(tmp_path, monkeypatch):
    search = FakeEngineSearch(tmp_path / 'search')
    search.build([obj(str(index)) for index in range(10)], modes=['native'], generation='one')
    load = Mock(wraps=search._load_manifest)
    status = Mock(wraps=search.status)
    monkeypatch.setattr(search, '_load_manifest', load)
    monkeypatch.setattr(search, 'status', status)
    assert len(search.search('valve', limit=6)) == 6
    assert status.call_count == 1
    assert [call.args[0] for call in load.call_args_list] == [search.active_path, search.last_build_path]


def test_snapshot_shares_status_with_query_without_second_validation(tmp_path, monkeypatch):
    search = FakeEngineSearch(tmp_path / 'search')
    search.build([obj('one')], modes=['native'], generation='one')
    snapshot = search.query_snapshot()
    state = snapshot.status
    assert state['modes']['qmd_keyword']['ready'] and state['generation'] == 'one'
    state['modes']['qmd_keyword']['ready'] = False
    state['libraries'].clear()
    monkeypatch.setattr(search, 'status', Mock(side_effect=AssertionError('Already validated for this query')))
    monkeypatch.setattr(search, '_load_active', Mock(side_effect=AssertionError('No repeated manifest resolution')))
    assert search.search('valve', snapshot=snapshot)[0]['id'] == 'lo-one'
    with pytest.raises(ValueError, match='only once'):
        search.search('valve', snapshot=snapshot)


def test_snapshot_receipt_rejects_authority_changed_after_status_before_guard(tmp_path, monkeypatch):
    search = FakeEngineSearch(tmp_path / 'search')
    built = search.build([obj('one')], modes=['native'], generation='one')
    authority = Path(built['directory']) / 'manifest.json'
    original = search.status

    def changed_after_read(**kwargs):
        result = original(**kwargs)
        value = json.loads(authority.read_text(encoding='utf-8'))
        value['modes']['qmd_keyword'].update(ready=False, state='stale', error='changed during provider preparation')
        authority.write_text(json.dumps(value), encoding='utf-8')
        return result

    monkeypatch.setattr(search, 'status', changed_after_read)
    with pytest.raises(RuntimeError, match='changed during query preparation'):
        search.query_snapshot()


def test_snapshot_rejects_model_changed_after_status_before_guard_without_model_inference(tmp_path, monkeypatch):
    search = FakeEngineSearch(tmp_path / 'search')
    search.build([obj('one')], modes=['native', 'qmd_keyword', 'qmd_vector'], generation='one')
    model = tmp_path / 'snapshot-model.gguf'
    original_bytes = b'GGUFquery-model-fixture'
    model.write_bytes(original_bytes)

    def model_info():
        info = model.stat()
        return {'available': True, 'path': str(model), 'sha256': ('a' if model.read_bytes() == original_bytes else 'b') * 64,
                'size': info.st_size, 'mtime_ns': info.st_mtime_ns}

    monkeypatch.setattr(search, '_model_info', model_info)
    original = search.status

    def changed_after_read(**kwargs):
        state = original(**kwargs)
        assert state['modes']['qmd_vector']['ready']
        model.write_bytes(b'GGUFchanged-query-model-fixture')
        return state

    monkeypatch.setattr(search, 'status', changed_after_read)
    with pytest.raises(RuntimeError, match='model changed during query preparation'):
        search.query_snapshot()


def test_embedding_disabled_snapshot_does_not_inspect_any_model(tmp_path, monkeypatch):
    search = LibrarySearch(tmp_path / 'search')
    search.build([obj('one')], modes=['native'], generation='one')
    monkeypatch.setattr(search, '_model_info', Mock(side_effect=AssertionError('Embedding is disabled')))
    snapshot = search.query_snapshot()
    assert search.search('valve', snapshot=snapshot)[0]['id'] == 'lo-one'


def test_snapshot_is_bound_to_the_engine_that_validated_it(tmp_path):
    search = FakeEngineSearch(tmp_path / 'search')
    search.build([obj('one')], modes=['native'], generation='one')
    snapshot = search.query_snapshot()
    other = FakeEngineSearch(search.data_dir)
    with pytest.raises(ValueError, match='one engine'):
        other.search('valve', snapshot=snapshot)
    assert search.search('valve', snapshot=snapshot)[0]['id'] == 'lo-one'


@pytest.mark.parametrize('changed', ['active', 'last_build', 'authority', 'database', 'qmd', 'settings', 'model'])
def test_snapshot_rejects_changed_generation_artifacts_or_dependencies(tmp_path, changed):
    search = FakeEngineSearch(tmp_path / 'search', preserve_history=True)
    built = search.build([obj('old')], modes=['native'], generation='old')
    if changed == 'qmd':
        search.qmd_dir = tmp_path / 'private-sdk'
        search.qmd_dir.mkdir()
        (search.qmd_dir / 'package.json').write_text('{"version":"2.8.3"}', encoding='utf-8')
    if changed == 'model':
        model = tmp_path / 'model.gguf'
        model.write_bytes(b'GGUFquery-snapshot-fixture')
        update_embedding_settings(search.data_dir, enabled=True, model_path=str(model))
        # The production model-info implementation supplies its resolved path.
        search._model_info = lambda: {'available': True, 'path': str(model), 'sha256': 'a' * 64}
    snapshot = search.query_snapshot()
    if changed == 'active':
        search.build([obj('new')], modes=['native'], generation='new')
    elif changed == 'last_build':
        search.last_build_path.write_text(json.dumps(dict(built, state='stale', error='objects changed')), encoding='utf-8')
    elif changed == 'authority':
        authority = Path(built['directory']) / 'manifest.json'
        body = json.loads(authority.read_text(encoding='utf-8'))
        body['corpus_hash'] = 'changed-after-provider-generation-check'
        authority.write_text(json.dumps(body), encoding='utf-8')
    elif changed == 'database':
        database = Path(built['directory']) / 'objects.sqlite3'
        database.rename(database.with_suffix('.missing'))
    elif changed == 'qmd':
        package = search.qmd_dir / 'package.json'
        package.write_text('{"version":"changed"}', encoding='utf-8')
    elif changed == 'settings':
        update_embedding_settings(search.data_dir, enabled=False)
    else:
        model.write_bytes(b'GGUFchanged-query-snapshot-fixture')
    with pytest.raises(RuntimeError, match='changed during query preparation'):
        search.search('valve', snapshot=snapshot)
    if changed == 'active':
        assert search.search('valve')[0]['id'] == 'lo-new'
    elif changed == 'last_build':
        assert not search.query_snapshot().status['modes']['qmd_keyword']['ready']
        with pytest.raises(RuntimeError, match='not ready'):
            search.search('valve')


def test_snapshot_cannot_reuse_visibility_or_holdout_authorization(tmp_path, monkeypatch):
    import advisor.library_search as module
    target = obj('one')
    search = FakeEngineSearch(tmp_path / 'search')
    search.build([target], modes=['native'], generation='one')
    policy = [HoldoutPolicy(token='clear')]
    monkeypatch.setattr(module, 'exclusion_snapshot', lambda root: policy[0])
    snapshot = search.query_snapshot()
    policy[0] = policy_for([target])
    assert search.search('valve', snapshot=snapshot) == []
    policy[0] = HoldoutPolicy(token='clear-again')
    snapshot = search.query_snapshot()
    monkeypatch.setattr(search, '_searchable_library_ids', lambda ids=None: set())
    assert search.search('valve', snapshot=snapshot) == []


def test_snapshot_survives_generation_cache_initialization_and_runtime_children(tmp_path):
    store = catalog(tmp_path / 'libraries', [obj('one')])
    search = FakeEngineSearch(store.root / 'search')
    with store.object_stream() as source:
        search.build(source, modes=['native'], generation='one')
    snapshot = search.query_snapshot()
    assert store.current_generation()
    runtime = search.data_dir / 'runtime'
    runtime.mkdir(exist_ok=True)
    (runtime / 'new-policy-cache.sqlite3').touch()
    assert search.search('valve', snapshot=snapshot)[0]['id'] == 'lo-one'


@pytest.mark.parametrize('limit', [6, 40])
@pytest.mark.parametrize('failure', [False, True])
def test_managed_hits_share_one_readonly_catalog_handle_and_always_close(tmp_path, monkeypatch, limit, failure):
    objects = [obj(str(index).zfill(2)) for index in range(50)]
    store = catalog(tmp_path / 'libraries', objects)
    search = FakeEngineSearch(store.root / 'search')
    with store.object_stream() as source:
        search.build(source, modes=['native'], generation='managed')
    connections = []
    connect = native_index.catalog_connection

    class TrackedConnection:
        def __init__(self, db):
            self.db, self.closed, self.reads = db, False, []

        def execute(self, sql, params=()):
            if sql.startswith('SELECT payload FROM revisions'):
                self.reads.append(params[0])
                if failure and len(self.reads) == 2:
                    raise RuntimeError('injected payload read failure')
            return self.db.execute(sql, params)

        def close(self):
            self.closed = True
            self.db.close()

    def tracked(path):
        db = TrackedConnection(connect(path))
        assert db.execute('PRAGMA query_only').fetchone() == (1,)
        connections.append(db)
        return db

    monkeypatch.setattr(native_index, 'catalog_connection', tracked)
    if failure:
        with pytest.raises(RuntimeError, match='injected payload read failure'):
            search.search('valve', limit=limit)
    else:
        hits = search.search('valve', limit=limit)
        assert len(hits) == limit
        assert connections[0].reads == [item['id'] for item in hits]
    assert len(connections) == 1 and connections[0].closed
    with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
        connections[0].db.execute('SELECT 1')


def test_standalone_queries_never_open_a_payload_catalog(tmp_path, monkeypatch):
    search = LibrarySearch(tmp_path / 'search')
    search.build([obj('one')], modes=['native'], generation='one')
    monkeypatch.setattr(native_index, 'catalog_connection', Mock(side_effect=AssertionError('Standalone payload is in the index')))
    assert search.search('valve')[0]['id'] == 'lo-one'
