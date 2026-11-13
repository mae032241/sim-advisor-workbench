"""Source-only method-card construction followed by the paired route pilot.

No benchmark question, reference, prior answer or grade is opened during card
construction. The inference runner opens the same three public tasks only after
all cards have been frozen and actual constructor delivery has passed its gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import traceback

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from scripts import build_route_cards as builder
from scripts import benchmark_route_catalog as experiment
from scripts import benchmark_route_selection as old
from scripts.build_case_methods import write_json, now


def render_cards(root, output):
    cards = [json.loads(path.read_text(encoding='utf-8')) for path in
             sorted((root / 'route-method-cards').glob('method-*.json'))]
    lines = ['# 路线目录与方法卡', '', '由来源独立构造；未输入测试题或评分答案。方法卡提供解释与阅读入口，不证明新任务适用性。', '',
             '| 方法 | 方法族 | 摘要 |', '| --- | --- | --- |']
    for pack in cards:
        card = pack['card']
        lines.append('| ' + ' | '.join(str(card[key]).replace('|', '\\|').replace('\n', ' ')
                                       for key in ('title', 'family', 'summary')) + ' |')
    for pack in cards:
        card = pack['card']
        lines.extend(['', '## ' + card['title'], '', '`' + pack['method_id'] + '`', ''])
        for section, claims in card['sections'].items():
            lines.extend(['### ' + section, ''])
            for claim in claims:
                lines.append('- [' + claim['origin'] + '] ' + claim['explanation'])
                for limitation in claim['limitations']:
                    lines.append('  - 局限：' + limitation)
                for ref in claim['evidence_refs']:
                    lines.append(f"  - 来源：`{ref['source_id']}` / `{ref['context']}` / `{ref['locator']}`")
            lines.append('')
    output.write_text('\n'.join(lines), encoding='utf-8')
    return {'method_count': len(cards), 'source_cases': sorted({identity for pack in cards
                for identity in pack['card']['source_case_ids']}),
            'pack_bytes': sum(path.stat().st_size for path in (root / 'route-method-cards').glob('method-*.json'))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--candidate-list', required=True, type=Path)
    parser.add_argument('--run-id', default=experiment.RUN_ID)
    parser.add_argument('--scope-library-id', action='append', default=[])
    parser.add_argument('--library-root', type=Path, default=builder.DEFAULT_LIBRARY_ROOT)
    parser.add_argument('--offline-root', type=Path, default=experiment.batch.OFFLINE)
    parser.add_argument('--model', default='gpt-6-luna')
    parser.add_argument('--parallel', type=int, choices=(1, 2), default=2)
    parser.add_argument('--timeout', type=float, default=7200)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', args.run_id) or args.timeout <= 0:
        raise ValueError('A new simple run ID and positive timeout are required')
    candidates, candidate_hash = old.load_candidates(args.candidate_list)
    if len(candidates['case_ids']) != 6:
        raise ValueError('This paired pilot uses exactly the six previously selected non-test sources')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.update(SIM_ADVISOR_NODE=str(args.offline_root / 'node/node.exe'),
                      SIM_ADVISOR_QMD_DIR=str(args.offline_root / 'qmd/node_modules/@tobilu/qmd'),
                      SIM_ADVISOR_SEARCH_CACHE=str(args.output / 'search-cache'), PYTHONIOENCODING='utf-8')
    os.environ.pop('QMD_EMBED_MODEL', None)
    os.environ.pop('SIM_ADVISOR_EMBED_MODEL', None)
    root = args.output / 'card-build'
    sources = ['advisor/route_cards.py', 'advisor/bounded_knowledge.py', 'advisor/tool_delivery_audit.py',
               'scripts/build_route_cards.py', 'scripts/benchmark_route_catalog.py',
               'scripts/benchmark_route_card_pipeline.py']
    source_hashes = {name: experiment.batch.sha(APP / name) for name in sources}
    write_json(args.output / 'freeze.json', {'schema': 'route-card-pipeline-freeze/v1',
        'source_sha256': source_hashes, 'candidate_list_sha256': candidate_hash,
        'source_case_ids': candidates['case_ids'], 'model': args.model, 'effort': 'high',
        'construction_inputs': 'Assigned cases, linked tutorials and source-derived general manuals only.',
        'test_task_input_to_constructor': False, 'private_grading_input': False,
        'maximum_active_model_processes': args.parallel, 'tool_transport': 'bounded_code_mode_single_call',
        'source_of_manual_queries': 'Names and mechanisms from the assigned source materials; never target tasks.',
        'automatic_retry_count': 0}, exclusive=True)
    def update(stage, **values):
        state = {'schema': 'route-card-pipeline/v1', 'stage': stage, 'updated_at': now(),
                 'pid': os.getpid(), 'model': args.model, 'effort': 'high',
                 'parallel_limit': args.parallel, 'run_id': args.run_id, 'card_root': str(root), **values}
        write_json(args.output / 'pipeline-state.json', state)
        print(json.dumps({'stage': stage, **values}, ensure_ascii=False), flush=True)
    try:
        build_args = ['--output', str(root), '--library-root', str(args.library_root),
                      '--model', args.model, '--effort', 'high', '--parallel', str(args.parallel),
                      '--timeout', str(args.timeout)]
        for identity in candidates['case_ids']:
            build_args.extend(['--case', identity])
        for identity in args.scope_library_id:
            build_args.extend(['--scope-library-id', identity])
        update('building_route_cards', source_case_count=6, stop_file=str(root / 'stop.request'))
        code = builder.main(build_args)
        if code:
            update('card_construction_invalid_or_incomplete', returncode=code, inference_started=False)
            return code
        summary = render_cards(root, args.output / 'method-cards.md')
        write_json(args.output / 'card-summary.json', summary, exclusive=True)
        if set(summary['source_cases']) != set(candidates['case_ids']):
            raise ValueError('Source-only construction did not cover every frozen candidate')
        if any(experiment.batch.sha(APP / name) != value for name, value in source_hashes.items()):
            raise ValueError('Frozen experiment code changed during construction')
        if (root / 'stop.request').exists() or (args.output / 'stop.request').exists():
            update('stopped_before_inference')
            return 2
        run_args = ['--run-id', args.run_id, '--method-root', str(root),
                    '--candidate-list', str(args.candidate_list.resolve()), '--model', args.model,
                    '--parallel', str(args.parallel), '--timeout', str(args.timeout), '--poll-interval', '2',
                    '--offline-root', str(args.offline_root)]
        for identity in args.scope_library_id:
            run_args.extend(['--scope-library-id', identity])
        update('running_paired_route_inference', **summary, expected_runs=6,
               stop_file=str(experiment.batch.ROOT / 'private/evaluations' / args.run_id / 'stop.request'))
        code = experiment.main(run_args)
        update('inference_complete' if code == 0 else 'inference_invalid_or_incomplete', returncode=code,
               grading='Offline frozen route questions; not fed back to any constructor/planner.')
        return code
    except BaseException as error:
        update('pipeline_error', error_type=type(error).__name__, detail=str(error), traceback=traceback.format_exc())
        raise


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
