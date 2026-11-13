"""The comparison harness preserves queries, ranking and library lifecycle."""
import copy
import json
import os

import pytest

from scripts import compare_keyword_backends as runner


class Store:
    def __init__(self, root):
        self.root = root
        self.enabled = {'a': False, 'b': True, 'unrelated': True, 'deleted': False}
        self.changes = []
    def libraries(self, include_deleted=False):
        return [{'id': identity, 'enabled': enabled, 'deleted_at': 'old' if identity == 'deleted' else None}
                for identity, enabled in self.enabled.items() if include_deleted or identity != 'deleted']
    def set_enabled_many(self, identities, enabled):
        assert 'deleted' not in identities
        self.changes.append((list(identities), enabled))
        self.enabled.update({identity: enabled for identity in identities})
    def current_generation(self):
        return 'frozen-generation'
    def evaluation_set_summaries(self):
        return {'retrieval_enabled': False}


class Provider:
    def __init__(self, root, failure=None):
        self.store = Store(root)
        self.calls, self.failure = [], failure
    def search(self, query, *, kind, limit, filters):
        self.calls.append(copy.deepcopy({'query': query, 'kind': kind, 'limit': limit, 'filters': filters}))
        if self.failure == 'interrupt':
            raise KeyboardInterrupt('fixture interrupted query')
        mode = filters['mode']
        if self.failure == 'exception' and mode == 'qmd_keyword':
            raise RuntimeError('fixture QMD unavailable')
        ids = ['two', 'one'] if mode == 'native' else ['one', 'two']
        actual = 'native' if self.failure == 'fallback' and mode == 'qmd_keyword' else mode
        result = [{'id': identity, 'library_id': filters['library_ids'][0], 'title': identity,
                   'text': 'private body ' * 2000, 'kind': kind,
                   'retrieval': {'backend': actual, 'score': index / 10, 'rank': index + 1}}
                  for index, identity in enumerate(ids)]
        filters['library_ids'].append('MUTATION_MUST_NOT_REACH_MANIFEST')
        return result


class Engine:
    def __init__(self, fail=False):
        self.fail, self.warmed, self.closed = fail, False, False
    def warmup_worker(self):
        self.warmed = True
        if self.fail:
            raise RuntimeError('fixture warmup failed')
        return {'ready': True}
    def close_worker(self):
        self.closed = True


def setup(tmp_path, monkeypatch, *, failure=None, warmup_failure=False):
    monkeypatch.setattr(runner, 'WORKSPACE', tmp_path)
    manifest = {'note': 'Frozen raw inputs', 'queries': [
        {'key': 'q1', 'group': 'recorded', 'arguments': {'query': '  原 query: alpha-beta  ', 'kind': 'manual',
         'library_ids': ['a', 'a', 'b'], 'feature_branch': 'mesh'}, 'origins': [{'line': 3}]},
        {'key': 'q2', 'group': 'probe', 'arguments': {'query': 'exact complete phrase', 'kind': 'all', 'library_ids': ['b']},
         'expected': {'source_id': 'one', 'title': 'Original expected title'}}]}
    path = tmp_path / 'frozen-manifest.json'
    original = (json.dumps(manifest, ensure_ascii=False, indent=3) + '\n').encode('utf-8')
    path.write_bytes(original)
    provider, engine = Provider(tmp_path / 'data/libraries', failure), Engine(warmup_failure)
    before = dict(provider.store.enabled)
    out = tmp_path / 'new-results'
    def run():
        return runner.run_comparison(path, out, data_dir=tmp_path / 'data', offline=tmp_path / 'offline',
                                     provider_factory=lambda *_args: provider, engine_factory=lambda *_args: engine)
    return path, original, manifest, out, provider, engine, before, run


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_raw_arguments_order_scores_bounded_cards_and_scope_are_preserved(tmp_path, monkeypatch):
    path, original, manifest, out, provider, engine, before, run = setup(tmp_path, monkeypatch)
    monkeypatch.setenv('SIM_ADVISOR_SEARCH_CACHE', 'original-cache')
    monkeypatch.setenv('QMD_EMBED_MODEL', 'original-inherited-value')
    result = run()
    assert result['status'] == 'completed' and result['completed_calls'] == 4
    assert engine.warmed and engine.closed and provider.store.enabled == before
    assert provider.store.changes == [(['a', 'b'], True), (['a'], False)]
    assert [call['filters']['mode'] for call in provider.calls] == ['native', 'qmd_keyword', 'qmd_keyword', 'native']
    for index, call in enumerate(provider.calls):
        spec = manifest['queries'][index // 2]['arguments']
        assert call['query'] == spec['query'] and call['kind'] == spec['kind'] and call['limit'] == 6
        assert call['filters']['library_ids'] == spec['library_ids']
        assert call['filters'].get('feature_branch') == spec.get('feature_branch')
    rows = lines(out / 'results.jsonl')
    assert rows[0]['ids'] == ['two', 'one'] and rows[1]['ids'] == ['one', 'two']
    assert [card['retrieval']['score'] for card in rows[0]['results']] == [0, .1]
    assert all('text' not in card and len(card['excerpt']) <= 700 for row in rows for card in row['results'])
    assert all(row['backend_validation'] == 'verified_hits' for row in rows)
    assert all(pair['overlap_count'] == 2 and not pair['same_first_id'] for pair in lines(out / 'pairs.jsonl'))
    assert path.read_bytes() == original == (out / 'manifest.json').read_bytes()
    protocol = json.loads((out / 'protocol.json').read_text(encoding='utf-8'))
    assert protocol['generation'] == 'frozen-generation' and protocol['manifest_sha256'] == runner.sha(path)
    assert protocol['model_inference'] is False and protocol['embedding'] is False and protocol['index_rebuild'] is False
    cleanup = json.loads((out / 'cleanup.json').read_text(encoding='utf-8'))
    assert cleanup['scope_restored'] and cleanup['owned_worker_closed'] and not cleanup['heldout_retrieval_enabled']
    assert os.environ['SIM_ADVISOR_SEARCH_CACHE'] == 'original-cache'
    assert os.environ['QMD_EMBED_MODEL'] == 'original-inherited-value'


@pytest.mark.parametrize('failure', ['fallback', 'exception'])
def test_backend_failure_is_recorded_without_retry_or_fallback_and_scope_restores(tmp_path, monkeypatch, failure):
    _, _, _, out, provider, engine, before, run = setup(tmp_path, monkeypatch, failure=failure)
    result = run()
    assert result['status'] == 'completed_with_errors' and result['errors'] == 2
    assert len(provider.calls) == 4 and engine.closed and provider.store.enabled == before
    rows = lines(out / 'results.jsonl')
    qmd = [row for row in rows if row['mode'] == 'qmd_keyword']
    assert all(row['error'] and row['results'] == [] for row in qmd)
    assert all(not row['comparable'] for row in lines(out / 'pairs.jsonl'))


@pytest.mark.parametrize('warmup_failure,failure', [(True, None), (False, 'interrupt')])
def test_warmup_failure_and_interruption_restore_flags_and_close_owned_worker(tmp_path, monkeypatch, warmup_failure, failure):
    _, _, _, out, provider, engine, before, run = setup(tmp_path, monkeypatch, failure=failure, warmup_failure=warmup_failure)
    with pytest.raises(RuntimeError if warmup_failure else KeyboardInterrupt):
        run()
    assert provider.store.enabled == before and engine.closed
    cleanup = json.loads((out / 'cleanup.json').read_text(encoding='utf-8'))
    assert cleanup['scope_restored'] and not cleanup['errors']
    assert json.loads((out / 'progress.json').read_text(encoding='utf-8'))['status'] == 'failed'


def test_existing_results_are_never_overwritten(tmp_path, monkeypatch):
    _, _, _, out, provider, engine, before, run = setup(tmp_path, monkeypatch)
    out.mkdir()
    complete = out / 'summary.json'
    complete.write_text('{"status":"completed"}', encoding='utf-8')
    with pytest.raises(ValueError, match='never overwritten'):
        run()
    assert complete.read_text(encoding='utf-8') == '{"status":"completed"}'
    assert not provider.calls and not provider.store.changes and not engine.warmed
    assert provider.store.enabled == before
