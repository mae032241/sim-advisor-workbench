import json
from pathlib import Path
import zipfile

import pytest

from advisor.benchmark import (CATEGORIES, bundle_public, checks, dump_json, judge_packet,
                               prepare_run, public_packet, assess_judgement, sha256, validate_case)


@pytest.fixture
def suite(tmp_path):
    public = tmp_path / 'public/cases/case-01'
    private = tmp_path / 'private/cases/case-01'
    (public / 'inputs').mkdir(parents=True)
    private.mkdir(parents=True)
    (public / 'problem.md').write_text('A fully specified planning question. ' * 20)
    asset = public / 'inputs/geometry_01.mphbin'
    asset.write_bytes(b'anonymous geometry fixture')
    dump_json(public / 'inputs/geometry.json', {'status': 'geometry_only', 'preparation_status': 'ready',
        'geometries': [{'dimension': 2}], 'mesh_run': False, 'solver_run': False,
        'materials_defined': False, 'physics_defined': False,
        'assets': {asset.name: {'sha256': sha256(asset), 'bytes': asset.stat().st_size}}})
    reference = {'case_id': 'case-01', 'sources': [{'id': 'saved'}], 'implementation': {'secret': 'PRIVATE GOLD ANSWER'},
        'assessment': {'schema': 'plan-checklist/v1', 'checks': [
            {'id': d, 'category': d, 'question': 'Is the requirement stated?',
             'applicability': {'type': 'always'}, 'pass_condition': 'Explicit statement',
             'fail_condition': 'Missing or contradicted', 'severity': 'critical',
             'evidence': [{'source_id': 'saved', 'locator': 'node1'}]} for d in CATEGORIES]}}
    dump_json(private / 'reference.json', reference)
    return tmp_path, public, reference


PLAN = 'Candidate section 1 clearly states the implementation.'


def judgement(reference):
    return {'case_id': reference['case_id'], 'check_results': [
        {'id': key, 'verdict': 'pass', 'reason': 'Explicit statement', 'evidence_status': 'quoted',
         'plan_quotes': [PLAN], 'applicability_reason': 'Always applies'} for key in checks(reference)],
        'task_defects': [], 'summary': 'Assessment'}


def test_public_run_and_zip_never_include_private_answer(suite, tmp_path):
    root, public, _ = suite
    packet = public_packet(root, 'case-01')
    assert 'PRIVATE GOLD' not in json.dumps(packet)
    run = tmp_path / 'runs/case-01'
    prepare_run(root, 'case-01', run)
    assert (run / 'question.json').is_file()
    assert not (run / 'reference.json').exists()
    output = root / 'public-inputs.zip'
    bundle_public(root, output)
    with zipfile.ZipFile(output) as archive:
        assert all('private' not in n and 'reference' not in n for n in archive.namelist())
        assert not any(b'PRIVATE GOLD' in archive.read(n) for n in archive.namelist())
    with pytest.raises(ValueError):
        prepare_run(root, 'case-01', root / 'private/run')
    with pytest.raises(ValueError):
        prepare_run(root, 'case-01', public / 'run')


def test_critical_failure_is_reported_without_subjective_points(suite):
    _, _, reference = suite
    value = judgement(reference)
    value['check_results'][0].update(verdict='fail', evidence_status='missing', plan_quotes=[])
    result = assess_judgement(reference, value, PLAN)
    assert result['counts'] == {'pass': 5, 'fail': 1, 'uncertain': 0, 'not_applicable': 0}
    assert result['conclusion'] == 'critical_failure' and result['critical_failures']
    assert 'score' not in result


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'bad_verdict', 'forged_quote', 'no_quote', 'always_na'])
def test_incomplete_or_unsupported_judgements_are_rejected(suite, fault):
    _, _, reference = suite
    value = judgement(reference)
    if fault == 'missing': value['check_results'].pop()
    elif fault == 'duplicate': value['check_results'].append(value['check_results'][0])
    elif fault == 'bad_verdict': value['check_results'][0]['verdict'] = 'half_credit'
    elif fault == 'forged_quote': value['check_results'][0]['plan_quotes'] = ['Never written in candidate']
    elif fault == 'no_quote': value['check_results'][0]['plan_quotes'] = []
    elif fault == 'always_na': value['check_results'][0]['verdict'] = 'not_applicable'
    with pytest.raises(ValueError): assess_judgement(reference, value, PLAN)


def test_conditional_alternatives_are_not_wrongly_failed(suite):
    _, _, reference = suite
    reference['assessment']['checks'][0]['applicability'] = {'type': 'conditional', 'condition': 'Uses method A'}
    value = judgement(reference)
    value['check_results'][0].update(verdict='not_applicable', applicability_reason='The candidate uses an alternative method')
    result = assess_judgement(reference, value, PLAN)
    assert result['counts']['not_applicable'] == 1 and result['conclusion'] == 'checks_satisfied'


def test_question_defects_require_review(suite):
    _, _, reference = suite
    value = judgement(reference)
    value['check_results'][0].update(verdict='uncertain', evidence_status='ambiguous', plan_quotes=[])
    value['task_defects'] = ['Required material data missing from public question']
    result = assess_judgement(reference, value, PLAN)
    assert result['conclusion'] == 'needs_review' and result['review_required']


def test_modified_geometry_or_defined_physics_reject_the_question(suite):
    root, public, _ = suite
    (public / 'inputs/geometry_01.mphbin').write_bytes(b'changed')
    with pytest.raises(ValueError): validate_case(root, 'case-01')


def test_only_judge_packet_contains_gold(suite):
    root, _, _ = suite
    value = judge_packet(root, 'case-01', 'Candidate plan')
    assert value['visibility'] == 'private_evaluator_only'
    assert 'PRIVATE GOLD ANSWER' in json.dumps(value)


def test_an_accidentally_copied_reference_is_not_published(suite):
    root, public, _ = suite
    dump_json(public / 'reference.json', {'gold': 'PRIVATE GOLD ANSWER'})
    with pytest.raises(ValueError):
        public_packet(root, 'case-01')
    with pytest.raises(ValueError):
        bundle_public(root, root / 'invalid.zip')


def test_judge_disagreement_is_exposed_instead_of_averaged(suite):
    from advisor.benchmark import compare_assessments
    _, _, reference = suite
    first = judgement(reference)
    second = judgement(reference)
    second['check_results'][0].update(verdict='uncertain', evidence_status='ambiguous', plan_quotes=[])
    result = compare_assessments(reference, [assess_judgement(reference, first, PLAN),
                                              assess_judgement(reference, second, PLAN)])
    assert len(result['disputed_checks']) == 1
    assert result['resolution'] == 'manual_review_required'
    assert result['automatic_majority_vote'] is False
