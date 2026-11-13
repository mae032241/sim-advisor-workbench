"""Small no-model profile of the registered QMD indexes."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import time

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--mode', action='append', choices=('qmd_keyword',))
    parser.add_argument('--key', action='append')
    parser.add_argument('--warmup-worker', action='store_true')
    args = parser.parse_args()
    output = args.out.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    offline = Path('D:/software/sim-offline/sim-advisor-offline-win-x64-0.2.2')
    os.environ.update(SIM_ADVISOR_NODE=str(offline/'node/node.exe'),
                      SIM_ADVISOR_QMD_DIR=str(offline/'qmd/node_modules/@tobilu/qmd'),
                      SIM_ADVISOR_SEARCH_CACHE=str(output.parent/'query-cache'))
    import advisor
    if args.baseline:
        advisor.__path__.insert(0, str(args.baseline.resolve()))
    from advisor.knowledge import KnowledgeBase
    from advisor.library_provider import LibraryKnowledgeProvider
    from advisor import library_search, library_store
    from advisor import qmd_worker
    library_search.PROJECT_ROOT = APP
    data = Path('D:/software/sim-offline/data')
    empty = output.parent/'empty-documents'
    empty.mkdir(exist_ok=True)
    kb = LibraryKnowledgeProvider(KnowledgeBase(output.parent/'legacy', empty), data/'libraries')
    before = {lib['id']: lib['enabled'] for lib in kb.store.libraries(include_deleted=True) if not lib.get('deleted_at')}
    assert kb.store.evaluation_set_summaries()['retrieval_enabled'] is False
    rows = []
    result = {'schema': 'query-performance-comparison/v1', 'started_at': datetime.now(timezone.utc).isoformat(),
              'model_inference': False, 'index_rebuilt': False, 'embedding_enabled': False,
              'module_files': {name: str(sys.modules['advisor.'+name].__file__) for name in
                               ('library_provider', 'library_search', 'library_store', 'object_index')},
              'rows': rows}
    write = lambda: output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    write()
    queries = [
        ('manual-h', 'The H Formulation', 'manual', None, ['cs63-documents']),
        ('manual-interpolation', 'Interpolation function table data extrapolation', 'manual', None, ['cs63-documents']),
        ('case-magnetic', 'Magnetic Fields Coil', 'case', None, ['cs63-models-acdc-module']),
        ('mesh-branch', 'boundary layer mesh', 'all', 'mesh', ['cs63-documents', 'cs63-models-acdc-module']),
    ]
    if args.key:
        queries = [query for query in queries if query[0] in args.key]
        if not queries:
            raise ValueError('No selected query key')
    modes = args.mode or ('qmd_keyword',)
    try:
        needed = sorted({lib for _, _, _, _, libs in queries for lib in libs})
        kb.store.set_enabled_many(needed, True)
        if args.warmup_worker:
            start = time.perf_counter()
            library_search.LibrarySearch(kb.store.root/'search').warmup_worker()
            result['worker_warmup_seconds'] = round(time.perf_counter()-start, 6)
        for mode in modes:
            for key, query, kind, branch, libraries in queries:
                for repeat in range(args.repeats):
                    filters = {'mode': mode, 'library_ids': libraries}
                    if branch is not None:
                        filters['feature_branch'] = branch
                    start = time.perf_counter()
                    try:
                        hits = kb.search(query, limit=6, kind=kind, filters=filters)
                        row = {'key': key, 'mode': mode, 'repeat': repeat,
                               'seconds': round(time.perf_counter()-start, 6),
                               'ids': [hit['id'] for hit in hits],
                               'scores': [hit.get('retrieval', {}).get('score', hit.get('score')) for hit in hits],
                               'library_ids': [hit.get('library_id') for hit in hits], 'error': None}
                        if mode.startswith('qmd'):
                            with qmd_worker._WORKERS_LOCK:
                                row['worker_pids'] = sorted({worker.pid for worker in qmd_worker._WORKERS.values() if worker.pid})
                    except Exception as exc:
                        row = {'key': key, 'mode': mode, 'repeat': repeat,
                               'seconds': round(time.perf_counter()-start, 6), 'error': type(exc).__name__,
                               'detail': str(exc)[:300]}
                    rows.append(row)
                    write()
                    print(json.dumps({k: row[k] for k in ('key', 'mode', 'repeat', 'seconds', 'error')}), flush=True)
        result['summary'] = {mode: {'median_seconds': round(statistics.median(r['seconds'] for r in rows if r['mode'] == mode), 6),
                                   'errors': sum(r['error'] is not None for r in rows if r['mode'] == mode)}
                             for mode in modes}
    finally:
        with qmd_worker._WORKERS_LOCK:
            workers = list(qmd_worker._WORKERS.values())
        qmd_worker.close_workers(kb.store.root/'search')
        result['query_workers_closed'] = all(worker.pid is None for worker in workers)
        for enabled in (False, True):
            ids = [key for key, value in before.items() if value is enabled]
            if ids:
                kb.store.set_enabled_many(ids, enabled)
        actual = {lib['id']: lib['enabled'] for lib in kb.store.libraries(include_deleted=True) if lib['id'] in before}
        result['scope_restored'] = actual == before
        result['heldout_retrieval_enabled'] = kb.store.evaluation_set_summaries()['retrieval_enabled']
        result['completed_at'] = datetime.now(timezone.utc).isoformat()
        write()


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
