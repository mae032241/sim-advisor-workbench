"""Mechanism discovery is public guidance, not a plan change or read grant."""
import copy

import pytest

from advisor.planning_service import tool_definitions
from test_plan_drafts import workbench


def test_guidance_is_available_without_plan_and_never_grants_evidence(workbench):
    _, _, sessions, task, tools = workbench
    before = copy.deepcopy(sessions.get(task['id']))
    catalog = tools.call_public('get_mechanism_checks', {})
    assert catalog['catalog'] and catalog['physical_correctness'] == 'not_evaluated'
    selected = tools.call_public('get_mechanism_checks', {
        'session_id': task['id'], 'mechanisms': ['porous_transport', 'moving_interface']})
    assert set(selected['selected_profiles']) == {'porous_transport', 'moving_interface'}
    assert any(row['id'] == 'porous_transport.thermal_advection' for row in selected['checks'])
    assert tools.read_sources(task['id']) == []
    assert sessions.get(task['id']) == before
    definition = next(t for t in tool_definitions() if t['name'] == 'get_mechanism_checks')
    assert 'session_id' not in definition['inputSchema']['required']


@pytest.mark.parametrize('arguments', [
    {'mechanisms': ['made-up']}, {'mechanisms': 'porous_transport'},
    {'mechanisms': ['porous_transport'] * 7}, {'query': 'unrecognized'},
])
def test_guidance_rejects_unknown_or_unbounded_arguments(workbench, arguments):
    *_, tools = workbench
    with pytest.raises(ValueError):
        tools.call_public('get_mechanism_checks', arguments)
