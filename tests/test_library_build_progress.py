import copy
import os
from pathlib import Path

from advisor.library_store import LibraryStore, now


def model_fixture(tmp_path, monkeypatch):
    sources = tmp_path / 'models'
    sources.mkdir()
    for name in ('good.mph', 'partial.mph', 'broken.mph'):
        (sources / name).write_text('Synthetic extraction fixture ' + name, encoding='utf-8')
    store = LibraryStore(tmp_path / 'catalog')
    library = store.register('fixture', 'official-models', [{'id': 'main', 'path': str(sources)}])
    library = store.set_enabled(library["id"], True)

    def extract(path, **kwargs):
        if path.name == 'broken.mph':
            raise ValueError('fixture cannot be parsed')
        coverage = {'deep_api': {'status': 'partial' if path.name == 'partial.mph' else 'observed',
                                  'read_errors': 3 if path.name == 'partial.mph' else 0, 'read_contract': 'fixture/v1'}}
        return {'objects': [{'local_id': 'case', 'type': 'model_case', 'title': path.name,
                             'coverage': coverage, 'evidence': [{'asset_path': str(path), 'method': 'synthetic-fixture'}]}],
                'coverage': coverage}

    monkeypatch.setattr('advisor.library_model.extract_case', extract)
    snapshots = []
    original = store._save_job

    def save(job):
        original(job)
        snapshots.append(copy.deepcopy(job))

    monkeypatch.setattr(store, '_save_job', save)
    return store, library, snapshots


def test_partial_reads_are_counted_and_live_file_progress_precedes_extraction(tmp_path, monkeypatch):
    store, library, snapshots = model_fixture(tmp_path, monkeypatch)
    result = store.build(library['id'], modes=[])
    assert result['status'] == 'partial'
    assert result['counts'] == {'succeeded': 1, 'partial': 1, 'failed': 1}
    assert result['partial_extractions'] == 1
    assert result['completed'] == result['total'] == 3
    assert result['elapsed_seconds'] >= 0
    partial = next(item for item in result['items'] if item['path'] == 'partial.mph')
    assert partial['status'] == 'partial' and partial['coverage']['deep_api']['read_errors'] == 3
    live = [item for item in snapshots if item['stage'] == 'extracting']
    assert any(item['progress']['current_file'] == 'partial.mph' and item['completed'] < 3 for item in live)
    assert any(item['counts']['partial'] == 1 for item in live)
    assert snapshots[0]['progress']['total'] is None
    assert any(item['stage'] == 'archiving' and item['progress']['unit'] == 'objects' for item in snapshots)


def test_index_progress_is_its_own_stage_not_completed_file_percentage(tmp_path, monkeypatch):
    store, library, snapshots = model_fixture(tmp_path, monkeypatch)
    from test_library_search import enable_test_embedding
    enable_test_embedding(store.root)

    class Indexer:
        def __init__(self, root):
            pass

        def build(self, objects, *, modes, generation, on_progress):
            assert objects and generation
            on_progress({'stage': 'indexing_native', 'progress': {'completed': 0, 'total': len(objects),
                         'unit': 'objects', 'current_file': None, 'detail': 'native fixture'}})
            on_progress({'stage': 'indexing_vector', 'progress': {'completed': None, 'total': None,
                         'unit': 'chunks', 'current_file': None, 'detail': 'waiting for real embedding progress'}})
            return {'modes': {mode: {'ready': True} for mode in modes}}

    monkeypatch.setattr('advisor.library_search.LibrarySearch', Indexer)
    result = store.build(library['id'], modes=['native', 'qmd_vector'])
    vector = next(item for item in snapshots if item['stage'] == 'indexing_vector')
    assert vector['completed'] == vector['total'] == 3
    assert vector['progress']['total'] is None and vector['progress']['completed'] is None
    assert result['status'] == 'partial'


def test_active_jobs_survive_history_limit_and_single_job_detects_dead_owner(tmp_path):
    store = LibraryStore(tmp_path / 'catalog')
    active = {'id': 'active', 'library_id': 'fixture', 'status': 'running', 'stage': 'extracting',
              'pid': os.getpid(), 'started_at': now(), 'completed': 0, 'total': 10, 'items': [], 'failed': []}
    store._save_job(active)
    for index in range(35):
        store._save_job({'id': f'old-{index}', 'library_id': 'fixture', 'status': 'failed', 'stage': 'finished'})
    assert any(item['id'] == 'active' for item in store.jobs())
    store._save_job({**active, 'id': 'dead', 'pid': 2147483647})
    result = store.job('dead')
    assert result['status'] == 'interrupted' and result['stage'] == 'finished'
    assert result['elapsed_seconds'] >= 0


def test_old_completed_job_with_unresolved_deep_reads_is_not_reported_as_complete(tmp_path):
    store = LibraryStore(tmp_path / 'catalog')
    store._save_job({'id': 'old-partial', 'library_id': 'fixture', 'status': 'completed', 'items': [
        {'status': 'completed', 'coverage': {'deep_api': {'status': 'partial', 'read_errors': 17}}}], 'failed': []})
    result = store.job('old-partial')
    assert result['status'] == 'partial'
    assert result['counts'] == {'succeeded': 0, 'partial': 1, 'failed': 0}
