"""Extracted setting identities cannot disappear between staged decisions."""
import copy

import pytest

from advisor.plan_drafts import PlanDraftError
from advisor.sessions import export_markdown
from test_agent_sessions import payload
from test_plan_drafts import workbench, read
from test_plan_settings_service import row


def test_unhandled_extracted_item_is_visible_and_prevents_silent_final_loss(workbench):
    _, _, sessions, task, tools = workbench
    args = payload(task); read(tools, task['id'])
    first = dict(row(args['plan']['steps'][0]['id']), id='first', status='unresolved', proposed_value='')
    other = dict(first, id='other', setting='Another independently extracted setting')
    tools.call(task['id'], 'save_plan_settings', {'revision': 1, 'settings': [first, other]})
    selected = dict(first, status='adopted', proposed_value='current chosen value')
    result = tools.call(task['id'], 'save_plan_settings', {'revision': 1, 'settings': [selected]})
    assert result['unhandled_count'] == 1
    missing = tools.call(task['id'], 'get_plan_settings', {'view': 'unhandled'})
    assert missing['rows'] == [{'id': 'other', 'setting': other['setting']}]
    assert tools.call(task['id'], 'get_task_context', {})['plan_settings_summary']['unhandled_count'] == 1
    with pytest.raises(PlanDraftError) as error:
        tools.call(task['id'], 'save_plan', args)
    assert error.value.errors[0]['path'] == '/plan_settings'
    assert sessions.get(task['id'])['revision'] == 1
    irrelevant = dict(other, status='not_applicable', reason='Different domain assumption; retain the decision', step_ids=[])
    tools.call(task['id'], 'save_plan_settings', {'revision': 1, 'settings': [selected, irrelevant]})
    tools.call(task['id'], 'save_plan', args)
    saved = sessions.get(task['id'])
    assert len(saved['plan_settings']) == 2
    markdown = export_markdown(saved)
    assert '未采用与待处理设置' in markdown and irrelevant['reason'] in markdown
    assert irrelevant['setting'] in markdown


def test_partial_edit_cannot_commit_when_an_extracted_item_was_dropped(workbench):
    _, _, sessions, task, tools = workbench
    args = payload(task); read(tools, task['id'])
    tools.call(task['id'], 'save_plan', args)
    current = sessions.get(task['id'])
    a = dict(row(current['plan']['steps'][0]['id']), id='a')
    b = dict(a, id='b')
    tools.call(task['id'], 'save_plan_settings', {'revision': 2, 'settings': [a, b]})
    tools.call(task['id'], 'save_plan_settings', {'revision': 2, 'settings': [a]})
    step = copy.deepcopy(current['plan']['steps'][0]); step['action'] += ' Updated.'
    with pytest.raises(ValueError):
        tools.call(task['id'], 'update_steps', {'revision': 2, 'steps': [step]})
    assert sessions.get(task['id']) == current


def test_explicit_whole_table_clear_remains_available_and_unhandled_filter_is_unambiguous(workbench):
    _, _, sessions, task, tools = workbench
    args = payload(task); read(tools, task['id'])
    a = dict(row(args['plan']['steps'][0]['id']), id='a')
    tools.call(task['id'], 'save_plan_settings', {'revision': 1, 'settings': [a]})
    tools.call(task['id'], 'save_plan_settings', {'revision': 1, 'settings': []})
    with pytest.raises(ValueError):
        tools.call(task['id'], 'get_plan_settings', {'view': 'unhandled', 'step_id': a['step_ids'][0]})
    tools.call(task['id'], 'save_plan', {**args, 'plan_settings': []})
    assert sessions.get(task['id'])['plan_settings'] == []
