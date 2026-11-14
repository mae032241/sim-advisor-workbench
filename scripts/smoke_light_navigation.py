"""Exercise original-location and on-demand MPH tools, without an LLM/COMSOL.

Use an independent writable data snapshot. This script neither selects
references using a benchmark rubric nor changes source files or library flags.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case-query', required=True)
    parser.add_argument('--case-library', required=True)
    parser.add_argument('--task-mode', choices=('route', 'implementation'), default='implementation')
    parser.add_argument('--replay-audits', type=Path)
    parser.add_argument('--offline-root', type=Path)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    if args.offline_root:
        os.environ['SIM_ADVISOR_NODE'] = str(args.offline_root / 'node/node.exe')
        os.environ['SIM_ADVISOR_QMD_DIR'] = str(args.offline_root / 'qmd/node_modules/@tobilu/qmd')
        os.environ['SIM_ADVISOR_SEARCH_CACHE'] = str(args.output.resolve() / 'search-cache')
    os.environ.pop('SIM_ADVISOR_HTML_TREE_INDEX', None)
    from advisor.server import App
    from advisor.qmd_worker import close_workers
    from advisor.research_policy import RESEARCH_POLICY, research_instructions
    from scripts.benchmark_raw_address import effective_instructions
    app = App(args.data_dir, runtime_factory='none')
    calls = []
    report = {'models_started': 0, 'comsol_started': False, 'embedding_invoked': False,
              'chapter_queries': [], 'case_chain': {}, 'shared_research_policy': False}
    def call(name, arguments):
        began = time.perf_counter()
        result = app.tools.call_public(name, arguments)
        row = {'tool': name, 'arguments': arguments, 'seconds': round(time.perf_counter()-began, 4),
               'response_bytes': len(json.dumps(result, ensure_ascii=False).encode('utf-8')), 'response': result}
        calls.append(row)
        return result
    try:
        if args.replay_audits:
            queries = []
            for path in sorted(args.replay_audits.glob('*.jsonl')):
                for line in path.read_text(encoding='utf-8').splitlines():
                    row = json.loads(line)
                    if row['tool'] == 'get_html_navigation' and row['arguments'].get('query'):
                        query = row['arguments']['query']
                        if query not in queries:
                            queries.append(query)
            for query in queries:
                result = call('get_html_navigation', {'query': query, 'limit': 2})
                entry = {'query': query, 'matches': result.get('total'),
                         'query_match_mode': result.get('query_match_mode'),
                         'empty_feedback_present': bool(result.get('refinement')),
                         'seconds': calls[-1]['seconds']}
                # Generic syntax-only demonstration, never a route evaluator:
                # shorten a zero-hit title query until a prefix has candidates.
                if result.get('total') == 0:
                    words = query.split()
                    for count in range(len(words)-1, 0, -1):
                        prefix = ' '.join(words[:count])
                        candidate = call('get_html_navigation', {'query': prefix, 'limit': 2})
                        if candidate.get('total'):
                            entry['prefix_refinement'] = {'query': prefix, 'matches': candidate['total'],
                                                          'applicability': 'not_evaluated'}
                            break
                report['chapter_queries'].append(entry)
        hits = call('search_cases', {'query': args.case_query, 'library_ids': [args.case_library]})
        assert hits.get('results'), 'No case found in the requested enabled training library'
        identity = hits['results'][0]['id']
        overview = call('read_source', {'id': identity, 'length': 600})
        original_action = overview['case_reading']['original']
        original = call(original_action['tool'], original_action['arguments'])
        native = original['raw_result']
        assert original['basis'] == 'original_saved_xml' and native.get('items')
        child = native['items'][0]['node_path']
        raw_page = call('inspect_case_source', {'case_id': identity, 'node_path': child, 'view': 'xml', 'length': 400})
        assert raw_page['raw_result'].get('text')
        assert original['raw_result']['sha256'] == raw_page['raw_result']['sha256']
        assert raw_page['raw_result']['cache_reused'] is True
        assert calls[-1]['response_bytes'] <= 7000 and calls[-2]['response_bytes'] <= 7000
        report['case_chain'] = {'case_id': identity, 'hit_count': len(hits['results']),
                                'child_path': child, 'offline_cache_reused': True,
                                'calls': [row['tool'] for row in calls[-4:]],
                                'original_read_seconds': [row['seconds'] for row in calls[-2:]]}
        report['shared_research_policy'] = (effective_instructions(mode=args.task_mode).count(RESEARCH_POLICY) == 1
            and research_instructions(args.task_mode) in effective_instructions(mode=args.task_mode))
        assert report['shared_research_policy']
        report['status'] = 'passed'
    finally:
        app.agents.close()
        close_workers()
        (args.output / 'calls.json').write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding='utf-8')
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    raise SystemExit(main())
