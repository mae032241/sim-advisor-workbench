"""Transfers persist with their plan, but never manufacture evidence reads."""
import copy
import json

import pytest

from advisor.planning_service import PlanningService
from advisor.sessions import ConflictError, export_markdown
from test_agent_sessions import payload
from test_plan_drafts import workbench, read
from test_planning_case_tools import case_tools, configuration
from test_workflow import SOURCE


def row(step_id, refs=None):
    return {'id': 'boundary-law', 'requirement': 'Resolve the prescribed boundary load',
            'setting': 'Boundary feature and expression', 'proposed_value': 'q_input [W/m²]',
            'status': 'adapted', 'reason': 'Apply the current supplied input',
            'transfer_rationale': 'Same equation form, current units and boundary scope',
            'gap': '', 'step_ids': [step_id], 'source_refs': refs or []}


def test_stage_does_not_change_plan_and_commits_into_version(workbench):
    root, kb, sessions, task, tools = workbench
    p = payload(task)
    read(tools, task['id'])
    receipt = tools.call(task['id'], 'read_source', {'id': SOURCE['id'], 'offset': 0, 'length': 500})['evidence_refs'][0]
    value = row(p['plan']['steps'][0]['id'], [{**receipt, 'stated_value': 'source method'}])
    staged = tools.call(task['id'], 'save_plan_settings', {'revision': 1, 'settings': [value]})
    assert staged['scope'] == 'pending_plan_settings'
    assert 'plan_settings' not in staged['review']
    assert sessions.get(task['id']) == task
    reconnect = PlanningService(root, sessions, kb)
    assert reconnect.call(task['id'], 'get_plan_settings', {})['rows'] == [value]
    tools.call(task['id'], 'save_plan', p)
    saved = sessions.get(task['id'])
    assert saved['revision'] == 2 and saved['plan_settings'] == [value]
    record = json.loads((tools.directory / (task['id'] + '.json')).read_text(encoding='utf-8'))
    assert 'plan_settings_stage' not in record
    assert 'source method' in export_markdown(saved)
    context = tools.call(task['id'], 'get_task_context', {})
    assert 'plan_settings' not in context and context['plan_settings_summary']['row_count'] == 1
    assert not context['plan_settings_summary']['pending']
    changed = copy.deepcopy(saved['plan']['steps'][0]); changed['difficulty'] += ' Updated.'
    tools.call(task['id'], 'update_steps', {'revision': 2, 'steps': [changed]})
    assert sessions.get(task['id'])['plan_settings'] == [value]
    assert sessions.version(task['id'], 2)['plan_settings'] == [value]


def test_stale_stage_never_attaches_to_new_inputs(workbench):
    _, _, sessions, task, tools = workbench
    p = payload(task)
    tools.call(task['id'], 'save_plan_settings', {'revision': 1, 'settings': [row(p['plan']['steps'][0]['id'])]})
    read(tools, task['id'])
    tools.call(task['id'], 'save_plan', {**p, 'plan_settings': []})
    assert tools.call(task['id'], 'get_plan_settings', {})['rows'] == []
    with pytest.raises(ConflictError):
        tools.call(task['id'], 'save_plan_settings', {'revision': 1, 'settings': []})


def test_table_never_grants_source_or_sibling_scope_credit(workbench):
    _, _, _, task, tools = workbench
    p = payload(task)
    forged = row(p['plan']['steps'][0]['id'], [{'source_id': SOURCE['id'],
        'context': 'case_configuration', 'locator': 'saved:compact:node/1', 'stated_value': 'claimed value'}])
    tools.call(task['id'], 'save_plan_settings', {'revision': 1, 'settings': [forged]})
    assert tools.read_sources(task['id']) == []
    read(tools, task['id'])
    reviewed = tools.call(task['id'], 'review_plan', {'plan': p['plan']})['settings_review']
    assert any(x['code'] == 'source_context_not_in_read_ledger' for x in reviewed['warnings'])
    assert reviewed['physical_correctness'] == 'not_evaluated'


def test_closed_reference_in_table_cannot_be_newly_committed(workbench, monkeypatch):
    _, kb, sessions, task, tools = workbench
    read(tools, task['id'])
    p = payload(task)
    p['plan_settings'] = [row(p['plan']['steps'][0]['id'], [{'source_id': SOURCE['id'],
        'context': 'source_text', 'locator': 'text:0:500', 'stated_value': 'earlier content'}])]
    monkeypatch.setattr(kb, 'source_available', lambda _: False, raising=False)
    with pytest.raises(PermissionError):
        tools.call(task['id'], 'save_plan', p)
    assert sessions.get(task['id'])['revision'] == 1


def test_configuration_receipts_are_exact_and_separate_from_text(case_tools):
    f = case_tools
    observed = configuration(f, category='mesh', limit=1)
    assert observed['evidence_refs']
    assert all(x['context'] == 'case_configuration' and x['locator'].startswith('saved:') for x in observed['evidence_refs'])
    properties = observed['nodes'][0]['properties']
    record = json.loads((f.tools.directory / (f.sid + '.json')).read_text(encoding='utf-8'))
    assert all(x['evidence_locator'] in {ref['locator'] for ref in record['read_contexts']} for x in properties)
    assert all(ref in record['read_contexts'] for ref in observed['evidence_refs'])
    assert len(observed['evidence_refs']) == len(observed['nodes'])
    assert not any('stated_value' in x or 'text' in x for x in record['read_contexts'])


def test_configuration_scaffold_text_does_not_masquerade_as_property_read(case_tools):
    f = case_tools
    source = next(item for item in f.store.objects() if item['type'] == 'saved_configuration')
    result = f.tools.call(f.sid, 'read_source', {'id': source['id'], 'offset': 0, 'length': 500})
    assert result['configuration_hint']['tool'] == 'read_case_configuration'
    assert result['configuration_hint']['scope'] == 'source_text_only'
    assert all(ref['context'] == 'source_text' for ref in result['evidence_refs'])


def test_pagination_and_revision_restore(workbench):
    _, _, sessions, task, tools = workbench
    p = payload(task); read(tools, task['id'])
    rows = [dict(row(p['plan']['steps'][0]['id']), id='r' + str(i)) for i in range(9)]
    p['plan_settings'] = rows
    tools.call(task['id'], 'save_plan', p)
    page = tools.call(task['id'], 'get_plan_settings', {'offset': 6, 'limit': 2})
    assert page['rows'] == rows[6:8] and page['next_offset'] == 8
    sessions.restore_version(task['id'], 1, 2, confirmed=True)
    assert tools.call(task['id'], 'get_plan_settings', {})['rows'] == []
