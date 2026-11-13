"""The advisory tool reviews public inputs and trusted reads without saving."""
import copy

import pytest

from advisor.plan_drafts import PlanDraftError
from test_plan_drafts import workbench, read
from test_agent_sessions import payload
from test_workflow import SOURCE


def test_review_retained_draft_does_not_change_plan_or_citation_ledger(workbench):
    _, _, sessions, task, tools = workbench
    value = payload(task)
    value['plan']['steps'][0]['extra_note'] = 'Must remain available while repairing format'
    with pytest.raises(PlanDraftError) as error:
        tools.call_public('save_plan', {**value, 'session_id': task['id']})
    before = copy.deepcopy(sessions.get(task['id']))
    result = tools.call_public('review_plan', {'session_id': task['id'], 'draft_id': error.value.to_dict()['draft_id']})
    assert result['blocking'] is False and result['physical_correctness'] == 'not_evaluated'
    assert result['requirement_inventory'] and result['per_stage_coverage']
    assert sessions.get(task['id']) == before
    assert tools.read_sources(task['id']) == []
    assert any(item['code'] == 'source_not_in_read_ledger' for item in result['warnings'])


def test_review_rechecks_current_source_availability(workbench, monkeypatch):
    _, kb, _, task, tools = workbench
    read(tools, task['id'])
    monkeypatch.setattr(kb, 'source_available', lambda _: False, raising=False)
    result = tools.call_public('review_plan', {'session_id': task['id'], 'plan': payload(task)['plan']})
    assert any(item['code'] == 'source_unavailable' for item in result['warnings'])
    assert tools.read_sources(task['id']) == [SOURCE['id']]


def test_review_accepts_optional_checklist_fields_without_rewriting_candidate(workbench):
    _, _, _, task, tools = workbench
    candidate = payload(task)['plan']
    result = tools.call_public('review_plan', {'session_id': task['id'], 'plan': candidate,
        'checklist': [{'requirement_id': 'goal', 'step_ids': [candidate['steps'][0]['id']],
                       'status': 'conditional', 'reason': 'Implementation still needs source verification.'}]})
    assert result['blocking'] is False
    assert result['requirement_inventory'][0]['mappings']


def test_review_requires_explicit_task_and_rejects_ambiguous_candidate(workbench):
    _, _, _, task, tools = workbench
    with pytest.raises(ValueError):
        tools.call_public('review_plan', {'plan': {}})
    with pytest.raises(ValueError):
        tools.call_public('review_plan', {'session_id': task['id'], 'plan': {}, 'draft_id': 'a' * 32})
