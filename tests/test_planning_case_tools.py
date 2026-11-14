"""Case discovery/navigation cannot substitute for successful configuration reads."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_provider import LibraryKnowledgeProvider
from advisor.library_store import LibraryStore
from advisor.planning_service import PlanningService, tool_definitions
from advisor.sessions import SessionStore
from test_agent_sessions import payload
from test_compact_model import mph
from test_workflow import FakePlanner, GOAL


@pytest.fixture
def case_tools(tmp_path):
    nodes = ''.join('<MeshFeature tag="size' + str(index) + '" op="Size"><propertyValue name="p:hmax" value="' + str(index / 100) + '"/></MeshFeature>'
                    for index in range(2, 31))
    source = mph(tmp_path / 'sources/case.mph', '<Model><MeshSequence tag="mesh1">' + nodes + '</MeshSequence></Model>')
    store = LibraryStore(tmp_path / 'libraries')
    library = store.register('Case tool integration', 'user-models', [{'id': 'models', 'role': 'models', 'path': str(source.parent)}], library_id='cases')
    store.set_enabled(library['id'], True)
    built = store.build(library['id'], paths=[source], model_format='compact', deep=False, modes=[])
    assert built['status'] in {'completed', 'partial'}, built
    overview = next(item for item in store.objects() if item['type'] == 'model_case')
    provider = LibraryKnowledgeProvider(SimpleNamespace(get_document=lambda _identity: None), store.root)
    sessions = SessionStore(tmp_path / 'workspace', provider, FakePlanner())
    task = sessions.create(GOAL, None)
    tools = PlanningService(tmp_path / 'workspace', sessions, provider)
    return SimpleNamespace(store=store, provider=provider, sessions=sessions, task=task, tools=tools, overview=overview, sid=task['id'])


def configuration(fixture, *, bound=True, **options):
    args = {'case_id': fixture.overview['id'], **options}
    if bound:
        args['session_id'] = fixture.sid
    return fixture.tools.call_public('read_case_configuration', args)


def test_case_overview_hands_off_to_registered_offline_original_tool(case_tools):
    fixture = case_tools
    overview = fixture.tools.call_public('read_source', {
        'session_id': fixture.sid, 'id': fixture.overview['id'], 'length': 500})
    action = overview['case_reading']['original']
    assert action['tool'] == 'inspect_case_source'
    assert action['arguments']['case_id'] == fixture.overview['id']
    # Use a fresh session: a raw XML read must not invent parsed/API credit.
    sid = fixture.sessions.create('Only inspect original XML.', None)['id']
    result = fixture.tools.call_public(action['tool'], dict(action['arguments'], session_id=sid))
    assert result['basis'] == 'original_saved_xml' and result['raw_result']['items']
    assert result['is_read_evidence'] is False
    assert fixture.tools.read_sources(sid) == []
    page = fixture.tools.call_public('inspect_case_source', {
        'session_id': sid, 'case_id': fixture.overview['id'], 'view': 'xml', 'length': 400})
    assert page['is_read_evidence'] is True and page['raw_result']['text']
    assert page['evidence_refs'][0]['context'] == 'original_mph_xml'
    assert fixture.tools.read_sources(sid) == [fixture.overview['id']]
    assert page['evidence_refs'][0] in fixture.tools._entry(sid)['record']['read_contexts']
    assert not any(ref['context'] == 'case_configuration' for ref in page['evidence_refs'])
    import json
    assert len(json.dumps(page, ensure_ascii=False).encode('utf-8')) <= 7000
    fixture.store.set_enabled('cases', False)
    with pytest.raises(PermissionError):
        fixture.tools.call_public(action['tool'], dict(action['arguments'], session_id=sid))


def assert_no_citation_credit(fixture):
    assert fixture.tools.read_sources(fixture.sid) == []
    args = payload(fixture.task)
    for step in args['plan']['steps']:
        step['source_ids'] = [fixture.overview['id']]
    with pytest.raises(ValueError):
        fixture.tools.call_public('save_plan', dict(args, session_id=fixture.sid))
    assert fixture.sessions.get(fixture.sid)['revision'] == 1


def test_case_tools_schemas_allow_unbound_calls_without_changing_bound_scope():
    names = {'search_cases', 'get_case_guide', 'read_case_configuration'}
    public = {item['name']: item for item in tool_definitions()}
    bound = {item['name']: item for item in tool_definitions(bound=True)}
    assert names <= public.keys() and names <= bound.keys()
    for name in names:
        assert 'session_id' in public[name]['inputSchema']['properties']
        assert 'session_id' not in public[name]['inputSchema']['required']
        assert 'session_id' not in bound[name]['inputSchema']['properties']
    schema = public['search_knowledge']['inputSchema']
    assert {'case_id', 'stage'} <= schema['properties'].keys()
    assert not {'case_id', 'stage'} & set(schema['required'])


@pytest.mark.parametrize('bound', [False, True])
def test_search_cases_returns_candidates_and_never_authorizes_citations(case_tools, monkeypatch, bound):
    fixture = case_tools
    search = Mock(return_value=[copy.deepcopy(fixture.overview)])
    monkeypatch.setattr(fixture.provider, 'search_cases', search)
    args = {'query': 'maximum element size', 'library_ids': ['cases']}
    if bound:
        args['session_id'] = fixture.sid
    result = fixture.tools.call_public('search_cases', args)
    assert result['mode'] == 'qmd_keyword' and result['retrieval_view'] == 'case_overviews'
    assert result['results'][0]['id'] == fixture.overview['id']
    assert result['results'][0]['candidate_only'] and result['results'][0]['read_required']
    search.assert_called_once_with('maximum element size', library_ids=['cases'], limit=6)
    assert_no_citation_credit(fixture)
    assert not (fixture.tools.data_dir / 'agent-sessions').exists()


@pytest.mark.parametrize('prepared', [False, True])
@pytest.mark.parametrize('bound', [False, True])
def test_get_case_guide_never_records_hints_as_source_reads(case_tools, monkeypatch, prepared, bound):
    fixture = case_tools
    if prepared:
        import advisor.case_guides as guides
        monkeypatch.setattr(guides, 'get_case_guide', lambda store, case_id: {
            'state': 'ready', 'case_id': case_id, 'is_read_evidence': False,
            'stages': [{'id': 'mesh', 'title': '网格', 'observed': [{'hint': 'Saved element size', 'source_ids': [case_id]}]}]})
    args = {'case_id': fixture.overview['id']}
    if bound:
        args['session_id'] = fixture.sid
    result = fixture.tools.call_public('get_case_guide', args)
    assert result['state'] == ('ready' if prepared else 'not_prepared')
    assert result['is_read_evidence'] is False
    assert_no_citation_credit(fixture)


def test_nonempty_configuration_is_real_saved_data_and_records_case_once(case_tools):
    fixture = case_tools
    result = configuration(fixture, node_path='compact:node/3', category='mesh', limit=1)
    assert result['returned'] == 1
    assert result['nodes'][0]['path'] == 'compact:node/3'
    assert result['nodes'][0]['properties'][0]['name'] == 'hmax'
    assert result['nodes'][0]['properties'][0]['value'] == 0.03
    assert result['source'] == 'xml'
    assert result['physical_validation'] == 'not_run' and result['effective_activation'] == 'not_evaluated'
    assert result['case_id'] == fixture.overview['id']
    assert fixture.tools.read_sources(fixture.sid) == [fixture.overview['id']]
    configuration(fixture, node_path='compact:node/3', source='saved')
    assert fixture.tools.read_sources(fixture.sid) == [fixture.overview['id']]
    restored = PlanningService(fixture.tools.data_dir, fixture.sessions, fixture.provider)
    assert restored.read_sources(fixture.sid) == [fixture.overview['id']]


def test_exact_compact_node_three_never_includes_node_thirty(case_tools):
    fixture = case_tools
    # The lower storage API deliberately supports broad substring navigation.
    broad = fixture.store.configuration(fixture.overview['id'], node_path='compact:node/3', source='saved', limit=8)
    assert {node['path'] for node in broad['nodes']} == {'compact:node/3', 'compact:node/30'}
    result = configuration(fixture, node_path='compact:node/3', limit=8)
    assert [node['path'] for node in result['nodes']] == ['compact:node/3']
    assert result['total_matches'] == 1


@pytest.mark.parametrize('bound', [False, True])
def test_empty_configuration_does_not_authorize_any_case_citation(case_tools, bound):
    fixture = case_tools
    result = configuration(fixture, bound=bound, node_path='compact:node/999', limit=1)
    assert result['returned'] == 0 and result['nodes'] == []
    assert_no_citation_credit(fixture)


def test_unbound_successful_configuration_does_not_credit_an_unrelated_session(case_tools):
    fixture = case_tools
    result = configuration(fixture, bound=False, node_path='compact:node/3')
    assert result['returned'] == 1
    assert_no_citation_credit(fixture)


def test_case_id_and_stage_reach_the_existing_knowledge_search(case_tools, monkeypatch):
    fixture = case_tools
    search = Mock(return_value=[])
    monkeypatch.setattr(fixture.provider, 'search', search)
    result = fixture.tools.call_public('search_knowledge', {
        'session_id': fixture.sid, 'query': 'FreeTet hmax', 'kind': 'all', 'mode': 'qmd_keyword',
        'case_id': fixture.overview['id'], 'stage': 'mesh', 'library_ids': ['cases']})
    search.assert_called_once_with('FreeTet hmax', limit=6, kind='all', filters={
        'mode': 'qmd_keyword', 'case_id': fixture.overview['id'], 'stage': 'mesh', 'library_ids': ['cases']})
    assert result['results'] == [] and fixture.tools.read_sources(fixture.sid) == []


@pytest.mark.parametrize('name', ['get_case_guide', 'read_case_configuration'])
@pytest.mark.parametrize('gate', ['closed', 'heldout'])
def test_live_library_and_holdout_gates_deny_case_tools_before_any_credit(case_tools, monkeypatch, name, gate):
    fixture = case_tools
    if gate == 'closed':
        fixture.store.set_enabled('cases', False)
    else:
        policy = HoldoutPolicy(citation_ids=frozenset([fixture.overview['id']]), token='held-case')
        monkeypatch.setattr(fixture.provider.store, 'evaluation_policy', lambda: policy)
    with pytest.raises(PermissionError):
        fixture.tools.call_public(name, {'session_id': fixture.sid, 'case_id': fixture.overview['id']})
    assert fixture.tools.read_sources(fixture.sid) == []


@pytest.mark.parametrize('gate', ['closed', 'heldout', 'heldout_group'])
def test_permission_change_during_configuration_cannot_grant_citation_credit(case_tools, monkeypatch, gate):
    fixture = case_tools
    policy = [HoldoutPolicy(token='clear')]
    monkeypatch.setattr(fixture.provider.store, 'evaluation_policy', lambda: policy[0])
    read = fixture.provider.store.configuration
    def changing(*args, **kwargs):
        result = read(*args, **kwargs)
        if gate == 'closed':
            fixture.provider.store.set_enabled('cases', False)
        elif gate == 'heldout':
            policy[0] = HoldoutPolicy(citation_ids=frozenset([fixture.overview['id']]), token='held-case')
        else:
            policy[0] = HoldoutPolicy(case_groups=frozenset([fixture.overview['case_group']]), token='held-group')
        return result
    monkeypatch.setattr(fixture.provider.store, 'configuration', changing)
    with pytest.raises(PermissionError):
        configuration(fixture, node_path='compact:node/3')
    assert fixture.tools.read_sources(fixture.sid) == []


@pytest.mark.parametrize('name,args', [
    ('search_cases', {'query': ''}),
    ('get_case_guide', {'case_id': []}),
    ('read_case_configuration', {'case_id': 'case', 'limit': 0}),
    ('read_case_configuration', {'case_id': 'case', 'limit': 9}),
    ('read_case_configuration', {'case_id': 'case', 'limit': True}),
    ('read_case_configuration', {'case_id': 'case', 'limit': '1'}),
    ('read_case_configuration', {'case_id': 'case', 'limit': 1.5}),
    ('read_case_configuration', {'case_id': 'case', 'source': 'xml'}),
    ('read_case_configuration', {'case_id': 'case', 'node_path': []}),
])
def test_new_case_tools_reject_invalid_public_arguments_before_reading(case_tools, monkeypatch, name, args):
    fixture = case_tools
    monkeypatch.setattr(fixture.provider, 'get_document', lambda *_args: pytest.fail('Invalid arguments must stop before source reads'))
    with pytest.raises(ValueError):
        fixture.tools.call_public(name, args)
