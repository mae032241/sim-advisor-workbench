from types import SimpleNamespace
from unittest.mock import patch

import pytest

from advisor.planning_service import PlanningService, tool_definitions
from advisor.sessions import SessionStore
from test_workflow import FakeKB, FakePlanner, GOAL


@pytest.fixture
def service(tmp_path):
    kb = FakeKB()
    kb.store = SimpleNamespace(root=tmp_path / 'libraries')
    sessions = SessionStore(tmp_path, kb, FakePlanner())
    task = sessions.create(GOAL, None)
    return PlanningService(tmp_path, sessions, kb), task['id'], kb.store


def test_navigation_schema_exposes_paging_and_contextual_views():
    definition = next(x for x in tool_definitions() if x['name'] == 'get_case_navigation')
    props = definition['inputSchema']['properties']
    assert props['limit']['maximum'] == 8
    assert props['view']['enum'] == ['children', 'dependencies', 'manual_links', 'implementations']
    assert props['node_path']['pattern'] == r'^compact:node/[0-9]+$'
    assert 'session_id' not in definition['inputSchema']['required']


@pytest.mark.parametrize('view', ['children', 'dependencies'])
def test_complete_navigation_dispatch_does_not_grant_read_credit(service, view):
    tools, sid, store = service
    expected = {'state': 'ready', 'items': [{'node_path': 'compact:node/4'}], 'is_read_evidence': True}
    with patch('advisor.case_tree.get_case_navigation', return_value=expected) as reader:
        result = tools.call_public('get_case_navigation', {'session_id': sid, 'case_id': 'case-id', 'view': view,
                                                         'node_path': 'compact:node/2', 'offset': 3, 'limit': 2})
    reader.assert_called_once_with(store, 'case-id', node_path='compact:node/2', view=view,
                                  query=None, direction='both', offset=3, limit=2)
    assert result['items'] == expected['items'] and result['is_read_evidence'] is False
    assert tools.read_sources(sid) == []


def test_manual_association_and_reverse_are_metadata_only(service):
    tools, sid, store = service
    with patch('advisor.case_manual_links.CaseManualLinks') as factory:
        links = factory.return_value
        links.links.return_value = {'items': [], 'state': 'ready'}
        links.reverse.return_value = {'items': [], 'state': 'ready'}
        assert tools.call_public('get_case_navigation', {'session_id': sid, 'case_id': 'case-id',
                                'view': 'manual_links', 'node_path': 'compact:node/4'})['is_read_evidence'] is False
        links.links.assert_called_once_with('case-id', node_path='compact:node/4', offset=0, limit=8)
        tools.call_public('get_case_navigation', {'view': 'implementations', 'manual_node_id': 'native-html-id'})
        links.reverse.assert_called_once_with('native-html-id', offset=0, limit=8)
    assert tools.read_sources(sid) == []


@pytest.mark.parametrize('arguments', [
    {}, {'view': 'implementations'},
    {'view': 'implementations', 'manual_node_id': 'manual', 'case_id': 'case'},
    {'case_id': 'case', 'manual_node_id': 'manual'},
    {'case_id': 'case', 'view': 'manual_links', 'query': 'field'},
    {'case_id': 'case', 'view': 'children', 'direction': 'incoming'},
    {'case_id': 'case', 'limit': True}, {'case_id': 'case', 'limit': 9},
    {'case_id': 'case', 'node_path': '../arbitrary-file'},
])
def test_navigation_rejects_inconsistent_or_out_of_contract_arguments(service, arguments):
    tools, _, _ = service
    with pytest.raises(ValueError):
        tools.call_public('get_case_navigation', arguments)
