"""Library discovery/control stays global across Agent and subprocess adapters."""
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_tools
import library_cli
import tools_cli
from advisor.agent_sessions import AgentSessions, planning_tools
from advisor.library_store import LibraryStore
from advisor.planning_service import PlanningService, tool_definitions
from advisor.sessions import SessionStore
from test_agent_sessions import FakeRuntime
from test_planning_source_lifecycle import setup_tools
from test_workflow import FakeKB, FakePlanner, GOAL


@pytest.fixture
def controls(tmp_path):
    source = tmp_path / 'sources'
    source.mkdir()
    store = LibraryStore(tmp_path / 'data/libraries')
    for lid, kind in [('manual', 'official-manuals'), ('model', 'official-models'), ('custom', 'user-models')]:
        store.register('Acoustics ' + lid, kind, [{'id': 'main', 'path': str(source)}], '6.3', lid)
    kb = FakeKB()
    kb.store = store
    sessions = SessionStore(tmp_path / 'data', kb, FakePlanner())
    tools = PlanningService(tmp_path / 'data', sessions, kb)
    return store, kb, sessions, tools


def test_global_scope_finds_closed_libraries_and_batch_changes_only_explicit_selection(controls, monkeypatch):
    store, _kb, sessions, tools = controls
    monkeypatch.setattr(sessions, 'get', Mock(side_effect=AssertionError('Global tools must not open a plan')))
    for method in ('index_status', 'jobs', 'object_stream'):
        monkeypatch.setattr(store, method, Mock(side_effect=AssertionError('Metadata only')))
    scope = tools.call_public('get_library_scope', {})
    assert scope['schema'] == 'library-scope/v1'
    assert {item['id'] for item in scope['libraries']} == {'manual', 'model', 'custom'}
    assert scope['enabled_library_ids'] == []
    result = tools.call_public('set_libraries_enabled', {'library_ids': ['manual', 'model', 'manual'], 'enabled': True})
    assert result['library_ids'] == ['manual', 'model']
    assert result['changed'] == 2 and result['index_rebuilt'] is False
    assert store.library('custom')['enabled'] is False
    result = tools.call('unused-session', 'set_libraries_enabled', {'library_ids': ['model'], 'enabled': False})
    assert result['changed'] == 1
    scoped = tools.call_public('get_library_scope', {'query': 'acoustics', 'type': 'official-manuals'})
    assert [lib['id'] for lib in scoped['libraries']] == ['manual']
    assert scoped['enabled_library_ids'] == ['manual']
    assert not (tools.data_dir / 'search').exists()
    assert list(tools.directory.iterdir()) == []
    assert tools.entries == {}


def test_custom_manual_type_is_filtered_by_registration_not_source_path(controls):
    store, _kb, _sessions, tools = controls
    store.register('My notes', 'user-manuals', store.library('manual')['roots'], '6.3', 'notes')
    scope = tools.call_public('get_library_scope', {'type': 'user-manuals'})
    assert [lib['id'] for lib in scope['libraries']] == ['notes']
    assert scope['libraries'][0]['type'] == 'user-manuals'
    schema = next(item['inputSchema'] for item in tool_definitions() if item['name'] == 'get_library_scope')
    assert 'user-manuals' in schema['properties']['type']['enum']


@pytest.mark.parametrize('name,args', [
    ('set_libraries_enabled', {'library_ids': [], 'enabled': True}),
    ('set_libraries_enabled', {'library_ids': ['manual'], 'enabled': 1}),
    ('set_libraries_enabled', {'library_ids': ['manual', 'missing'], 'enabled': True}),
    ('set_libraries_enabled', {'library_ids': ['manual'] * 1001, 'enabled': True}),
    ('set_libraries_enabled', {'library_ids': ['manual'], 'enabled': True, 'confirmed': True}),
    ('get_library_scope', {'type': 'all'}),
    ('get_library_scope', {'query': None}),
])
def test_public_and_managed_library_calls_reject_invalid_inputs_without_partial_changes(controls, name, args):
    store, _kb, _sessions, tools = controls
    for call in (lambda: tools.call_public(name, args), lambda: tools.call(None, name, args)):
        with pytest.raises((ValueError, FileNotFoundError)):
            call()
        assert store.library_scope()['enabled_library_ids'] == []


def test_deleted_library_cannot_be_enabled_and_failed_batch_changes_nothing(controls):
    store, _kb, _sessions, tools = controls
    store.delete_library('model')
    assert 'model' not in {lib['id'] for lib in tools.call_public('get_library_scope', {})['libraries']}
    with pytest.raises(FileNotFoundError):
        tools.call_public('set_libraries_enabled', {'library_ids': ['manual', 'model'], 'enabled': True})
    assert not store.library('manual')['enabled']


def test_search_explicit_scope_preserves_kind_mode_branch_and_never_implicitly_enables(controls):
    store, kb, _sessions, tools = controls
    kb.search = Mock(return_value=[])
    args = {'query': 'pressure', 'kind': 'code', 'mode': 'qmd_keyword', 'feature_branch': 'physics', 'library_ids': ['manual', 'manual', 'model']}
    tools.call_public('search_knowledge', args)
    kb.search.assert_called_once_with('pressure', limit=6, kind='code', filters={
        'mode': 'qmd_keyword', 'feature_branch': 'physics', 'library_ids': ['manual', 'model']})
    assert store.library_scope()['enabled_library_ids'] == []
    for value in ([], 'manual', [None], [''], ['manual'] * 1001):
        with pytest.raises(ValueError):
            tools.call_public('search_knowledge', {**args, 'library_ids': value})


def test_disabling_via_agent_tool_blocks_search_and_old_source_reads_without_rebuilding(tmp_path):
    store, provider, _sessions, plan, tools, objects = setup_tools(tmp_path)
    before = (store.root / 'search/active.json').read_bytes()
    tools.call_public('set_libraries_enabled', {'library_ids': ['a'], 'enabled': False})
    assert provider.search('mesh', filters={'library_ids': ['a']}) == []
    assert not provider.source_available(objects[0])
    with pytest.raises(PermissionError):
        tools.call_public('read_source', {'session_id': plan['id'], 'id': objects[0]['id'], 'offset': 0, 'length': 500})
    assert tools.read_sources(plan['id']) == []
    tools.call_public('set_libraries_enabled', {'library_ids': ['a'], 'enabled': True})
    assert tools.call_public('read_source', {'id': objects[0]['id'], 'offset': 0, 'length': 500})['id'] == objects[0]['id']
    assert (store.root / 'search/active.json').read_bytes() == before


def test_subprocess_agent_source_uses_the_same_lifecycle_gates_for_existing_ids(tmp_path, monkeypatch):
    store, provider, _sessions, _plan, tools, objects = setup_tools(tmp_path)
    monkeypatch.setattr(agent_tools, 'KnowledgeBase', lambda _: provider.legacy)
    tools.call_public('set_libraries_enabled', {'library_ids': ['a'], 'enabled': False})
    output = io.StringIO()
    monkeypatch.setattr(sys, 'argv', ['agent_tools.py', '--data-dir', str(store.root.parent), 'source', objects[0]['id']])
    with redirect_stdout(output):
        assert agent_tools.main() == 1
    assert '已关闭' in json.loads(output.getvalue())['error']
    tools.call_public('set_libraries_enabled', {'library_ids': ['a'], 'enabled': True})
    with redirect_stdout(io.StringIO()) as opened:
        assert agent_tools.main() == 0
    assert json.loads(opened.getvalue())['id'] == objects[0]['id']


def test_managed_and_bound_external_agents_share_library_tools_and_binding_checks(controls):
    store, kb, sessions, tools = controls
    sid = sessions.create(GOAL, None)['id']
    agents = AgentSessions(tools.data_dir, sessions, kb, runtime_factory=FakeRuntime)
    try:
        names = {tool['name'] for tool in planning_tools()}
        assert {'get_library_scope', 'set_libraries_enabled'} <= names
        assert agents.call_tool(sid, 'get_library_scope', {})['library_count'] == 3
        binding = agents.register_external(sid, {'name': 'Fixture', 'pid': os.getpid()})
        revision = sessions.get(sid)['revision']
        def external(name, arguments, token=binding['binding_id']):
            return agents.external_tool(sid, {'binding_id': token, 'name': name, 'arguments': arguments})
        assert external('get_library_scope', {'type': 'official-models'})['libraries'][0]['id'] == 'model'
        result = external('set_libraries_enabled', {'library_ids': ['manual', 'model'], 'enabled': True})
        assert result['library_ids'] == ['manual', 'model']
        with pytest.raises(PermissionError):
            external('set_libraries_enabled', {'library_ids': ['custom'], 'enabled': True}, token='wrong')
        with pytest.raises(PermissionError):
            external('set_libraries_enabled', {'library_ids': ['custom'], 'enabled': True, 'session_id': 'b' * 32})
        assert not store.library('custom')['enabled']
        assert sessions.get(sid)['revision'] == revision
        assert agents.tools.read_sources(sid) == []
    finally:
        agents.close()


@pytest.mark.parametrize('module,subdir', [(library_cli, 'libraries'), (agent_tools, '')])
def test_local_clis_expose_metadata_and_explicit_batch_controls(controls, monkeypatch, module, subdir):
    store, _kb, _sessions, _tools = controls
    data_dir = store.root if subdir else store.root.parent
    def invoke(*args):
        output = io.StringIO()
        monkeypatch.setattr(sys, 'argv', [module.__name__ + '.py', '--data-dir', str(data_dir), *args])
        with redirect_stdout(output):
            code = module.main()
        assert code == 0, output.getvalue()
        return json.loads(output.getvalue())
    if module is library_cli:
        notes = invoke('add', 'My notes', '--id', 'notes', '--type', 'user-manuals', '--path', store.library('manual')['roots'][0]['path'])
        assert notes['type'] == 'user-manuals' and notes['roots'][0]['role'] == 'manuals'
    else:
        store.register('My notes', 'user-manuals', store.library('manual')['roots'], library_id='notes')
    assert invoke('scope', '--type', 'official-models')['libraries'][0]['id'] == 'model'
    assert invoke('scope', '--type', 'user-manuals')['libraries'][0]['id'] == 'notes'
    result = invoke('enable-many', '--library', 'manual', '--library', 'model', '--enabled', 'true')
    assert result['library_ids'] == ['manual', 'model'] and result['index_rebuilt'] is False
    assert invoke('enable-many', '--library', 'model', '--enabled', 'false')['changed'] == 1
    assert store.library_scope()['enabled_library_ids'] == ['manual']
    assert store.jobs() == []


@pytest.mark.parametrize('argv,name,args', [
    (['scope', '--query', 'acoustics', '--type', 'official-models'], 'get_library_scope', {'query': 'acoustics', 'type': 'official-models'}),
    (['scope', '--type', 'user-manuals'], 'get_library_scope', {'type': 'user-manuals'}),
    (['enable-many', '--library', 'a', '--library', 'b', '--enabled', 'false'], 'set_libraries_enabled', {'library_ids': ['a', 'b'], 'enabled': False}),
])
def test_http_tools_cli_forwards_global_library_tools_without_session_or_confirmation(monkeypatch, argv, name, args):
    requests = []
    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return io.StringIO(json.dumps({'result': {'ok': True}}))
    monkeypatch.setattr(tools_cli, 'build_opener', lambda *args: Opener())
    monkeypatch.setattr(sys, 'argv', ['tools_cli.py', '--url', 'http://127.0.0.1:8765', *argv])
    with redirect_stdout(io.StringIO()):
        assert tools_cli.main() == 0
    request, timeout = requests[0]
    assert request.full_url == 'http://127.0.0.1:8765/api/tools/call' and timeout == 120
    assert json.loads(request.data) == {'name': name, 'arguments': args}


def test_public_library_schemas_work_with_or_without_a_bound_plan():
    for bound in (False, True):
        definitions = {item['name']: item['inputSchema'] for item in tool_definitions(bound=bound)}
        for name in ('get_library_scope', 'set_libraries_enabled'):
            assert 'session_id' not in definitions[name]['required']
            assert ('session_id' in definitions[name]['properties']) is not bound
        assert set(definitions['set_libraries_enabled']['required']) == {'library_ids', 'enabled'}
