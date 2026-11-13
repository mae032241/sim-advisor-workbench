"""Replay a frozen query manifest through native and QMD keyword retrieval.

No planner, embeddings, index construction, query expansion or fallback lives
here. Candidate cards retain each provider's returned order and scores.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

APP = Path(__file__).resolve().parents[1]
WORKSPACE = APP.parents[1]
sys.path.insert(0, str(APP))
from advisor.knowledge_views import candidate_view
from advisor.retrieval_settings import read as embedding_settings

DEFAULT_DATA = Path('D:/software/sim-offline/data')
DEFAULT_OFFLINE = Path('D:/software/sim-offline/sim-advisor-offline-win-x64-0.2.2')
TOP_K = 6
MODES = ('native', 'qmd_keyword')


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    temporary = path.with_suffix(path.suffix + '.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def append(path, value):
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def load_manifest(path):
    payload = Path(path).read_bytes()
    value = json.loads(payload.decode('utf-8-sig'))
    if not isinstance(value, dict) or not isinstance(value.get('queries'), list) or not value['queries']:
        raise ValueError('Manifest must contain a nonempty queries array')
    keys = set()
    for item in value['queries']:
        if not isinstance(item, dict) or not isinstance(item.get('key'), str) or not item['key'] or item['key'] in keys:
            raise ValueError('Every query needs a unique nonempty key; queries are never deduplicated')
        keys.add(item['key'])
        args = item.get('arguments')
        if (not isinstance(args, dict) or set(args) - {'query', 'kind', 'library_ids', 'feature_branch'}
                or not isinstance(args.get('query'), str) or not args['query'].strip()
                or args.get('kind', 'all') not in {'all', 'manual', 'case', 'code'}
                or not isinstance(args.get('library_ids'), list) or not args['library_ids']
                or any(not isinstance(identity, str) or not identity for identity in args['library_ids'])):
            raise ValueError('Query arguments require original query, kind and explicit library_ids; no backend/limit overrides')
        if 'feature_branch' in args and not isinstance(args['feature_branch'], str):
            raise ValueError('feature_branch must preserve its original string value')
    return value, payload


@contextmanager
def runtime_environment(out, offline):
    runtime = out / 'runtime'
    (runtime / 'tmp').mkdir(parents=True)
    settings = {'SIM_ADVISOR_NODE': str(offline / 'node/node.exe'),
                'SIM_ADVISOR_QMD_DIR': str(offline / 'qmd/node_modules/@tobilu/qmd'),
                'SIM_ADVISOR_SEARCH_CACHE': str(runtime / 'query-cache'),
                'TEMP': str(runtime / 'tmp'), 'TMP': str(runtime / 'tmp'),
                'QMD_EMBED_MODEL': None, 'SIM_ADVISOR_EMBED_MODEL': None}
    previous = {key: os.environ.get(key) for key in settings}
    try:
        for key, value in settings.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def default_provider(data, out):
    from advisor.knowledge import KnowledgeBase
    from advisor.library_provider import LibraryKnowledgeProvider
    empty = out / 'empty-documents'
    empty.mkdir()
    return LibraryKnowledgeProvider(KnowledgeBase(out / 'legacy', empty), data / 'libraries')


def default_engine(search_root):
    from advisor.library_search import LibrarySearch
    return LibrarySearch(search_root)


def assert_isolation(store):
    settings = embedding_settings(store.root / 'search')
    if settings['enabled'] or settings.get('error'):
        raise RuntimeError('Embedding must already be disabled with valid settings; this runner never changes it')
    if store.evaluation_set_summaries()['retrieval_enabled']:
        raise RuntimeError('Held-out retrieval must remain disabled; this runner never changes it')


def flags(store):
    return {item['id']: item['enabled'] for item in store.libraries(include_deleted=True)}


def set_many(store, identities, enabled):
    for offset in range(0, len(identities), 1000):
        store.set_enabled_many(identities[offset:offset + 1000], enabled)


def one_query(provider, spec, mode):
    args = copy.deepcopy(spec['arguments'])
    filters = {key: copy.deepcopy(args[key]) for key in ('library_ids', 'feature_branch') if key in args}
    filters['mode'] = mode
    row = {'query_key': spec['key'], 'group': spec.get('group'), 'mode': mode,
           'arguments': copy.deepcopy(args), 'top_k': TOP_K, 'started_at': now(),
           'results': [], 'ids': [], 'observed_backends': [], 'error': None,
           'backend_validation': 'failed'}
    started, elapsed = time.perf_counter(), None
    try:
        hits = provider.search(args['query'], kind=args.get('kind', 'all'), limit=TOP_K, filters=filters)
        elapsed = time.perf_counter() - started
        if not isinstance(hits, list) or len(hits) > TOP_K:
            raise RuntimeError('Provider returned an invalid TopK result list')
        for hit in hits:
            retrieval = hit.get('retrieval') if isinstance(hit, dict) else None
            actual = retrieval.get('backend') if isinstance(retrieval, dict) else None
            row['observed_backends'].append(actual)
            if actual != mode:
                raise RuntimeError(f'Backend mismatch: expected {mode}, observed {actual!r}; no fallback permitted')
            if (not isinstance(hit.get('id'), str) or not hit['id']
                    or hit.get('library_id') not in args['library_ids']):
                raise RuntimeError('Provider returned an unidentified or out-of-scope source')
        row['results'] = [candidate_view(hit) for hit in hits]
        row['ids'] = [hit['id'] for hit in hits]
        row['backend_validation'] = 'verified_hits' if hits else 'empty_result_no_hit_evidence'
    except Exception as exc:
        row['error'] = {'type': type(exc).__name__, 'detail': str(exc)[:1000]}
    row['seconds'] = round(time.perf_counter() - started if elapsed is None else elapsed, 6)
    row['observed_backends'] = sorted(set(row['observed_backends']), key=str)
    return row


def agreement(rows):
    left, right = (rows[mode] for mode in MODES)
    if left['error'] or right['error']:
        return {'comparable': False}
    native, qmd = left['ids'], right['ids']
    overlap = set(native) & set(qmd)
    union = set(native) | set(qmd)
    return {'comparable': True, 'same_ordered_ids': native == qmd,
            'native_first_id': native[0] if native else None,
            'qmd_first_id': qmd[0] if qmd else None,
            'same_first_id': (native[0] if native else None) == (qmd[0] if qmd else None),
            'native_count': len(native), 'qmd_count': len(qmd),
            'overlap_ids': [identity for identity in native if identity in overlap],
            'overlap_count': len(overlap), 'union_count': len(union),
            'jaccard': len(overlap) / len(union) if union else 1.0,
            'interpretation': 'ID agreement only; not relevance, precision or recall'}


def run_comparison(manifest_path, out, *, data_dir=DEFAULT_DATA, offline=DEFAULT_OFFLINE,
                   provider_factory=None, engine_factory=None):
    if provider_factory is None:
        from advisor.library_search import MODES as active_modes
        if 'native' not in active_modes:
            raise RuntimeError('Independent native retrieval is retired. Historical results remain readable; use the recorded source revision to reproduce this comparison.')
    manifest_path, out = Path(manifest_path).resolve(), Path(out).resolve()
    data, offline = Path(data_dir).resolve(), Path(offline).resolve()
    manifest, original = load_manifest(manifest_path)
    if not out.is_relative_to(WORKSPACE.resolve()):
        raise ValueError('Output and temporary cache must stay inside the workspace')
    if out.exists() and any(out.iterdir()):
        raise ValueError('Output directory must be new or empty; previous results are never overwritten')
    out.mkdir(parents=True, exist_ok=True)
    (out / 'manifest.json').write_bytes(original)
    sources = ['scripts/compare_keyword_backends.py', 'advisor/library_provider.py', 'advisor/library_search.py',
               'advisor/native_index.py', 'advisor/qmd_worker.py', 'advisor/knowledge.py',
               'advisor/knowledge_views.py', 'advisor/library_store.py', 'advisor/evaluation_sets.py',
               'advisor/query_scope_counts.py', 'advisor/index_manifest_cache.py']
    protocol = {'schema': 'keyword-backend-comparison/v1', 'started_at': now(),
                'manifest_path': str(manifest_path), 'manifest_sha256': hashlib.sha256(original).hexdigest(),
                'source_sha256': {name: sha(APP / name) for name in sources},
                'data_dir': str(data), 'offline': str(offline), 'top_k': TOP_K,
                'query_count': len(manifest['queries']), 'expected_calls': 2 * len(manifest['queries']),
                'modes': list(MODES), 'order': 'native first on odd queries; qmd_keyword first on even queries',
                'provider': 'LibraryKnowledgeProvider.search', 'explicit_library_scope': True,
                'query_rewritten': False, 'candidate_order_modified': False, 'automatic_fallback': False,
                'model_inference': False, 'embedding': False, 'index_rebuild': False,
                'qmd_dir': str(offline / 'qmd/node_modules/@tobilu/qmd'), 'node': str(offline / 'node/node.exe')}
    sdk = offline / 'qmd/node_modules/@tobilu/qmd'
    protocol['sdk_sha256'] = {name: sha(sdk / name) if (sdk / name).is_file() else None
                              for name in ('package.json', 'dist/index.js', 'dist/store.js')}
    state = {'status': 'preparing', 'completed_calls': 0, 'completed_queries': 0, 'errors': 0,
             'expected_calls': protocol['expected_calls'], 'query_count': protocol['query_count']}
    write(out / 'protocol.json', protocol)
    write(out / 'progress.json', state)
    provider, engine, before, available = None, None, None, {}
    cleanup = {'scope_restored': False, 'owned_worker_closed': False, 'errors': []}
    with runtime_environment(out, offline):
        try:
            provider = (provider_factory or default_provider)(data, out)
            store = provider.store
            libraries = store.libraries(include_deleted=True)
            before = {item['id']: item['enabled'] for item in libraries}
            available = {item['id']: item for item in libraries if not item.get('deleted_at')}
            write(out / 'scope-before.json', before)
            needed = sorted({identity for item in manifest['queries'] for identity in item['arguments']['library_ids']})
            if set(needed) - set(available):
                raise ValueError('Manifest references unknown/deleted libraries: ' + ', '.join(sorted(set(needed) - set(available))))
            assert_isolation(store)
            generation = store.current_generation()
            protocol.update(generation=generation, activated_library_ids=needed)
            write(out / 'protocol.json', protocol)
            set_many(store, needed, True)
            engine = (engine_factory or default_engine)(store.root / 'search')
            if not engine.warmup_worker().get('ready'):
                raise RuntimeError('The local QMD keyword SDK could not be warmed')
            state['status'] = 'running'
            for ordinal, spec in enumerate(manifest['queries']):
                pair = {}
                order = MODES if ordinal % 2 == 0 else tuple(reversed(MODES))
                for mode in order:
                    assert_isolation(store)
                    row = one_query(provider, spec, mode)
                    row.update(ordinal=ordinal + 1, generation=generation)
                    append(out / 'results.jsonl', row)
                    pair[mode] = row
                    state['completed_calls'] += 1
                    state['errors'] += bool(row['error'])
                    state.update(updated_at=now(), last_query_key=spec['key'], last_mode=mode)
                    write(out / 'progress.json', state)
                    print(json.dumps({'event': 'query', 'completed': state['completed_calls'],
                                      'total': state['expected_calls'], 'query_key': spec['key'],
                                      'mode': mode, 'seconds': row['seconds'], 'error': row['error']}, ensure_ascii=True), flush=True)
                if store.current_generation() != generation:
                    raise RuntimeError('Library generation changed during the paired comparison')
                append(out / 'pairs.jsonl', {'ordinal': ordinal + 1, 'query_key': spec['key'], 'group': spec.get('group'),
                                            'execution_order': list(order), **agreement(pair)})
                state['completed_queries'] += 1
            if manifest_path.read_bytes() != original:
                raise RuntimeError('Original query manifest changed during execution')
            state.update(status='completed_with_errors' if state['errors'] else 'completed', completed_at=now())
        except BaseException as exc:
            state.update(status='failed', error_type=type(exc).__name__, error=str(exc)[:1000])
            raise
        finally:
            if engine is not None:
                try:
                    engine.close_worker()
                    cleanup['owned_worker_closed'] = True
                except Exception as exc:
                    cleanup['errors'].append('close worker: ' + str(exc)[:500])
            if provider is not None and before is not None:
                try:
                    current = flags(provider.store)
                    for enabled in (False, True):
                        restore = [identity for identity in available if before[identity] is enabled and current.get(identity) is not enabled]
                        set_many(provider.store, restore, enabled)
                    cleanup['scope_after'] = flags(provider.store)
                    cleanup['scope_restored'] = cleanup['scope_after'] == before
                    cleanup['generation_after'] = provider.store.current_generation()
                    cleanup['heldout_retrieval_enabled'] = provider.store.evaluation_set_summaries()['retrieval_enabled']
                    assert_isolation(provider.store)
                    if not cleanup['scope_restored']:
                        raise RuntimeError('Library enabled flags were not restored exactly')
                except Exception as exc:
                    cleanup['errors'].append('restore scope: ' + str(exc)[:500])
            cleanup['completed_at'] = now()
            if cleanup['errors']:
                state['status'] = 'cleanup_failed'
            write(out / 'cleanup.json', cleanup)
            write(out / 'progress.json', state)
            write(out / 'summary.json', {**state, 'generation': protocol.get('generation'),
                                        'manifest_sha256': protocol['manifest_sha256'], 'cleanup': cleanup})
            if cleanup['errors']:
                raise RuntimeError('Comparison cleanup failed: ' + '; '.join(cleanup['errors']))
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--data', default=DEFAULT_DATA, type=Path)
    parser.add_argument('--offline', default=DEFAULT_OFFLINE, type=Path)
    args = parser.parse_args()
    run_comparison(args.manifest, args.out, data_dir=args.data, offline=args.offline)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
