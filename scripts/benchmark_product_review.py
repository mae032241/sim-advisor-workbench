"""Prepare immutable saved-plan evidence and verify human/model judgements.

This module never assigns a physical verdict. It excludes the user request,
model snapshot, retrieved source bodies, and chat from candidate evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from advisor import benchmark as core

ROOT = APP / 'benchmarks/comsol-plan-v3'
PRIMARY = 'luna6-high-product-cli-20261111'
EVAL = ROOT / 'private/evaluations' / PRIMARY
REVIEW = EVAL / 'reviews'
PACKETS = EVAL / 'review-packets'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def render_candidate(saved):
    """Lossless text of authored planning fields, with stable JSON paths."""
    lines = []

    def visit(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, path+'.'+key if path else key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f'{path}[{index}]')
        else:
            lines.extend([path, str(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False), ''])
    for key in ('plan', 'design_brief', 'questions', 'evidence_gaps', 'planning_incomplete', 'plan_stale'):
        if key in saved:
            visit(saved[key], key)
    return '\n'.join(lines)+'\n'


def prepare(run_dir, *, role='primary'):
    run_dir = Path(run_dir)
    run = core.load_json(run_dir/'run.json')
    if run.get('status') != 'complete' or not run.get('saved_plan'):
        return None
    assert digest(run_dir/'plan.md') == run['plan_sha256']
    case = run['case_id']
    ref_path = ROOT/'private/cases'/case/'reference.json'
    frozen = core.load_json(EVAL/'protocol.json')
    assert digest(ref_path) == frozen['reference_sha256'][case]
    saved = core.load_json(run_dir/'saved-plan.json')
    candidate = render_candidate(saved)
    key = run['arm']+'-'+case
    folder = PACKETS/key
    if (folder/'packet.json').exists():
        previous = core.load_json(folder/'packet.json')
        assert previous['run_directory'] == str(run_dir.resolve()), 'Archive the superseded trial packet before selecting a supplementary trial'
    folder.mkdir(parents=True, exist_ok=True)
    ref = core.load_json(ref_path)
    write(folder/'questions.json', {key: ref.get(key) for key in
          ('schema', 'case_id', 'title', 'questions', 'assessment_policy', 'unverified')})
    (folder/'candidate.txt').write_text(candidate, encoding='utf-8', newline='')
    meta = {'schema': 'saved-plan-review-packet/v1', 'run': key, 'case_id': case, 'arm': run['arm'],
            'trial_role': role, 'run_directory': str(run_dir.resolve()),
            'saved_plan_sha256': digest(run_dir/'saved-plan.json'),
            'export_sha256': digest(run_dir/'plan.md'), 'reference_sha256': digest(ref_path),
            'candidate_sha256': digest(folder/'candidate.txt'),
            'candidate_scope': 'Only saved authored plan/design/questions/gaps fields; no user input, source bodies, model snapshot or chat.',
            'numerical_simulation_performed': False}
    write(folder/'packet.json', meta)
    return meta


def validate(path):
    path = Path(path)
    review = core.load_json(path)
    packet = core.load_json(PACKETS/path.stem/'packet.json')
    candidate_path = PACKETS/path.stem/'candidate.txt'
    assert digest(candidate_path) == packet['candidate_sha256']
    assert review['candidate_sha256'] == packet['candidate_sha256']
    assert review['reference_sha256'] == packet['reference_sha256']
    run_dir = Path(packet['run_directory'])
    assert digest(run_dir/'saved-plan.json') == packet['saved_plan_sha256']
    ref = core.load_json(ROOT/'private/cases'/packet['case_id']/'reference.json')
    assert digest(ROOT/'private/cases'/packet['case_id']/'reference.json') == packet['reference_sha256']
    candidate = candidate_path.read_text(encoding='utf-8')
    judgement = {key: review[key] for key in ('case_id', 'check_results', 'task_defects', 'summary')}
    assert all(row['verdict'] in ('pass', 'fail') for row in judgement['check_results'])
    for row in judgement['check_results']:
        assert row.get('reason_code') in ({None} if row['verdict'] == 'pass' else
                                          {'missing', 'contradicted', 'unverified_alternative', 'input_conflict'})
        if row['verdict'] == 'fail' and row['reason_code'] != 'missing':
            assert row.get('plan_quotes'), row['id']
    assessed = core.assess_judgement(ref, judgement, candidate)
    return {'run': packet['run'], 'case_id': packet['case_id'], 'arm': packet['arm'],
            'trial_role': packet['trial_role'], 'counts': assessed['counts'],
            'category_counts': assessed['category_counts'], 'quote_verification': assessed['quote_verification'],
            'task_defects': review['task_defects'], 'review_sha256': digest(path)}


def main():
    REVIEW.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) > 1 and sys.argv[1] == 'validate':
        rows = [validate(path) for path in sorted(REVIEW.glob('*.json'))]
        write(EVAL/'review-validation.json', rows)
        print(json.dumps({'reviews': len(rows), 'checks': sum(sum(v['counts'].values()) for v in rows),
                          'verified': True}, ensure_ascii=False))
    else:
        rows = [prepare(path) for path in sorted((ROOT/'runs'/PRIMARY).iterdir()) if (path/'run.json').is_file()]
        rows = [row for row in rows if row]
        write(EVAL/'review-packet-manifest.json', rows)
        print(json.dumps({'prepared': len(rows), 'candidate_input_leakage': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
