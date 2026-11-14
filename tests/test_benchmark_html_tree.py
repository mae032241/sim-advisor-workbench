"""Synthetic contract/preflight/lifecycle tests; no models, corpora or indexes."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from advisor.agent_sessions import planning_tools
from advisor.tool_contracts import read_contract
from advisor.tool_delivery_audit import response_sha256
from scripts import benchmark_html_tree as pilot


def synthetic_definitions():
    values = deepcopy(planning_tools())
    names = {item['name'] for item in values}
    for name, properties in (
        ('get_html_navigation', {'node_id': {'type': 'string'}, 'source_id': {'type': 'string'},
            'query': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 8, 'default': 8}}),
        ('read_html_section', {'node_id': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
            'length': {'type': 'integer', 'minimum': 1, 'maximum': 3200, 'default': 1800}})):
        if name not in names:
            values.append({'name': name, 'description': 'Synthetic bounded semantic HTML read.',
                           'inputSchema': {'type': 'object', 'additionalProperties': False,
                                           'properties': properties,
                                           'required': ['node_id'] if name == 'read_html_section' else []}})
    return values


@pytest.fixture
def published(monkeypatch):
    values = synthetic_definitions()
    monkeypatch.setattr('advisor.agent_sessions.planning_tools', lambda: deepcopy(values))
    return values


class Store:
    def __init__(self):
        self.flags = {'manuals': False, 'unrelated': True}
        self.disabled_sources = set()
    def libraries(self, **_kwargs):
        return [{'id': key, 'enabled': value, 'deleted_at': None} for key, value in self.flags.items()]
    def searchable_library_ids(self):
        return [key for key, value in self.flags.items() if value]
    def evaluation_set_summaries(self):
        return {'retrieval_enabled': False}
    def set_enabled_many(self, identities, enabled):
        for identity in identities:
            self.flags[identity] = enabled
    def require_not_heldout(self, identity):
        if identity == 'heldout':
            raise PermissionError('Synthetic held-out source')
    def source_available(self, identity):
        return identity not in self.disabled_sources


def installed(tmp_path, published):
    calls, store = [], Store()
    store.flags['manuals'] = True
    def original(sid, name, arguments, **kwargs):
        calls.append((sid, name, deepcopy(arguments)))
        if name == 'read_html_section':
            return {'state': 'ready', 'node_id': arguments['node_id'], 'text': 'x' * 1800,
                    'children': [{'node_id': 'child'}], 'siblings': [{'node_id': 'sibling'}],
                    'next_offset': 1800, 'evidence_refs': [{'source_id': 'html', 'context': 'html_section', 'locator': 'section:0:1800'}]}
        if name == 'get_html_navigation':
            return {'state': 'ready', 'children': [{'node_id': 'node'}], 'next_offset': None, 'is_read_evidence': False}
        if name == 'read_source':
            text = 'a' * arguments['length']
            return {'id': arguments['id'], 'text': text, 'offset': 0, 'next_offset': None,
                    'evidence_refs': [{'source_id': arguments['id'], 'context': 'source_text', 'locator': 'text:0:' + str(len(text))}]}
        return {'state': 'ready'}
    app = SimpleNamespace(kb=SimpleNamespace(store=store), sessions=SimpleNamespace(get=lambda sid: {'id': sid}),
        agents=SimpleNamespace(runtime_tool=original, _require_current_entry=Mock()))
    audit = tmp_path / 'audit.jsonl'
    definitions = pilot.install_tools(app, {'session'}, store.searchable_library_ids(), {'session': audit})
    return app, store, calls, audit, definitions


def test_effective_contract_is_consistent_and_does_not_mutate_production(published):
    before = deepcopy(published)
    definitions = pilot.effective_definitions(published)
    by_name = {tool['name']: tool for tool in definitions}
    source = by_name['read_source']
    assert source['inputSchema']['properties']['length']['maximum'] == 1200
    assert source['inputSchema']['properties']['length']['default'] == 1000
    assert '16000' not in source['description'] and '12000' not in source['description']
    contract = read_contract(definitions, {'tool': 'read_source', 'path': '/length'})
    assert json.loads(contract['text'])['maximum'] == 1200
    assert json.loads(contract['text'])['default'] == 1000
    assert by_name['read_html_section']['inputSchema']['properties']['length']['maximum'] == 3200
    assert pilot.HTML_TOOLS.issubset(by_name)
    assert not {'save_plan', 'save_plan_settings', 'set_libraries_enabled', 'get_case_method',
                'list_route_methods', 'read_method_card'} & set(by_name)
    assert published == before


def test_html_semantic_page_bypasses_generic_fragmentation(tmp_path, published):
    app, _, calls, audit, _ = installed(tmp_path, published)
    result = app.agents.runtime_tool('session', 'read_html_section', {'node_id': 'node'})
    assert len(result['text']) == 1800
    assert result['children'][0]['node_id'] == 'child' and result['siblings'][0]['node_id'] == 'sibling'
    assert 'format' not in result and 'handle' not in result
    assert calls[-1][2] == {'node_id': 'node'}
    row = json.loads(audit.read_text(encoding='utf-8').splitlines()[-1])
    assert row['response_sha256'] == response_sha256(result) and row['result_json_bytes'] <= 7000


def test_actual_source_defaults_and_validation_use_same_effective_contract(tmp_path, published):
    app, _, calls, audit, _ = installed(tmp_path, published)
    call = app.agents.runtime_tool
    result = call('session', 'read_source', {'id': 'source'})
    assert len(result['text']) == 1000 and calls[-1][2]['length'] == 1000
    failed = call('session', 'read_source', {'id': 'source', 'length': 16000})
    assert failed['success'] is False
    queried = call('session', 'get_tool_contract', {'tool': 'read_source', 'path': '/length'})
    assert json.loads(queried['text'])['default'] == 1000
    last = json.loads(audit.read_text(encoding='utf-8').splitlines()[-2])
    assert last['response_sha256'] == pilot.visible_response_sha256(failed)


def test_read_only_live_scope_and_source_gates(tmp_path, published):
    app, store, calls, _, _ = installed(tmp_path, published)
    call = app.agents.runtime_tool
    for name in ('save_plan', 'set_libraries_enabled', 'read_method_card'):
        assert call('session', name, {})['success'] is False
    assert call('another-session', 'get_html_navigation', {})['success'] is False
    assert not calls
    assert call('session', 'read_source', {'id': 'heldout'})['success'] is False
    store.flags['manuals'] = False
    assert call('session', 'get_html_navigation', {})['success'] is False
    assert not calls


def test_runtime_registers_effective_schemas_once_and_preserves_route_tail(published):
    calls = []
    underlying = SimpleNamespace(start=lambda **kwargs: calls.append(kwargs) or {'thread_id': 'synthetic'},
                                 status=lambda: {'running': True})
    runtime = pilot.HtmlRuntime(underlying)
    runtime.start(tools=published, instructions='production')
    names = [item['name'] for item in calls[0]['tools']]
    assert len(names) == len(set(names)) and pilot.HTML_TOOLS.issubset(names)
    assert calls[0]['instructions'] == 'production' + pilot.PAGING_INSTRUCTIONS
    assert runtime.status()['running'] is True
    assert '每个exec单元只能调用一次业务工具' in pilot.PAGING_INSTRUCTIONS
    assert 'JSON.parse' in pilot.PAGING_INSTRUCTIONS and 'siblings' in pilot.PAGING_INSTRUCTIONS
    assert 'list_route_methods' not in pilot.MESSAGE and 'read_method_card' not in pilot.MESSAGE
    assert 'chosen_route、alternatives、applicability_conditions、unresolved、sources' in pilot.MESSAGE


def make_tree(tmp_path):
    folder = tmp_path / 'tree'
    folder.mkdir()
    index = folder / 'index.sqlite3'
    index.write_bytes(b'synthetic immutable index')
    manifest = {'schema': 'html-manual-tree/v1', 'ready': True, 'index_file': 'index.sqlite3',
                'index_sha256': hashlib.sha256(index.read_bytes()).hexdigest(),
                'library_id': 'manuals', 'root_id': 'manual-root', 'node_count': 1,
                'document_count': 1, 'package_count': 1, 'packages': []}
    (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return folder


def test_tree_snapshot_freezes_index_and_manifest_and_rejects_tampering(tmp_path):
    folder = make_tree(tmp_path)
    first = pilot.tree_snapshot(folder)
    assert set(first['files']) == {'index.sqlite3', 'manifest.json'}
    assert pilot.tree_snapshot(folder / 'index.sqlite3') == first
    (folder / 'index.sqlite3').write_bytes(b'changed synthetic index')
    with pytest.raises(ValueError, match='hash'):
        pilot.tree_snapshot(folder)


def test_parser_has_only_new_arm_and_fixed_model_limits(tmp_path):
    args = pilot.parser().parse_args(['--tree-root', str(tmp_path)])
    assert args.timeout == 7200 and args.model == 'gpt-6-luna' and args.parallel == 2
    assert pilot.EFFORT == 'high' and pilot.CASES == ('case-01', 'case-02', 'case-09')
    assert not hasattr(args, 'method_root') and not hasattr(args, 'candidate_list')
    for options in (['--parallel', '3'], ['--model', 'another-model']):
        with pytest.raises(SystemExit):
            pilot.parser().parse_args(['--tree-root', str(tmp_path), *options])


def test_one_failed_case_never_stops_other_two_or_retries(tmp_path):
    tasks = [SimpleNamespace(case=case, condition=pilot.CONDITION, destination=tmp_path / case) for case in pilot.CASES]
    stop, lock, called = threading.Event(), threading.RLock(), []
    state = {'active': [], 'completed': [], 'parallel_limit': 2}
    def run(task, message, cancel_event):
        assert not cancel_event.is_set()
        called.append(task.case)
        if task.case == 'case-01':
            raise RuntimeError('Synthetic per-case failure')
        return {'case_id': task.case, 'condition': task.condition, 'status': 'complete', 'natural_termination': True}
    results = pilot.run_tasks(tasks, stop, state, lambda: None, lock, run_turn=run,
                             delivery_check=lambda task, result: {'valid': result['status'] == 'complete', 'issues': []})
    assert sorted(called) == sorted(pilot.CASES) and len(called) == 3 and not stop.is_set()
    assert len(results) == 3 and len(state['completed']) == 3 and state['active'] == []
    assert sum(result['status'] == 'complete' for result in results) == 2


def test_delivery_check_uses_actual_rollout_and_unwrapped_payload_hashes(tmp_path, monkeypatch):
    payload = {'error': 'synthetic feedback'}
    envelope = {'success': False, 'contentItems': [{'type': 'inputText', 'text': json.dumps(payload)}]}
    assert pilot.visible_response_sha256(envelope) == response_sha256(payload)
    audit = tmp_path / 'audit.jsonl'
    audit.write_text(json.dumps({'response_sha256': response_sha256(payload)}) + '\n', encoding='utf-8')
    expected = []
    monkeypatch.setattr(pilot, 'locate_rollout', lambda identity: tmp_path / 'rollout.jsonl')
    monkeypatch.setattr(pilot, 'audit_rollout', lambda *args, **kwargs: expected.append(kwargs) or {'valid': True})
    assert pilot.inspect_delivery(SimpleNamespace(audit_path=audit), {'conversation_id': 'synthetic'})['valid']
    assert expected[0]['expected_response_sha256'] == [response_sha256(payload)]


def test_real_cli_lifecycle_accepts_explicit_html_arm_and_reaches_settings(tmp_path):
    # Reuse a protocol-only synthetic HTTP/CLI peer, not a model or service.
    from test_benchmark_route_selection import Product
    product = Product()
    task = SimpleNamespace(destination=tmp_path / 'turn', condition=pilot.CONDITION, case='case-01',
        session_id='a' * 32, url='http://127.0.0.1:12345', model=pilot.MODEL, timeout=2, poll_interval=0.01)
    result = pilot.run_cli_turn(task, pilot.MESSAGE, runner=product.runner, http_reader=product.read)
    assert result['condition'] == 'html_tree' and result['status'] == 'complete'
    assert result['natural_termination'] is True and not result.get('failure_code')
    assert product.calls.count('settings') == 1 and product.calls.count('message') == 1
    assert product.calls[-1] == 'disconnect' and 'interrupt' not in product.calls
    with pytest.raises(ValueError, match='known condition'):
        pilot.base.run_turn(task, pilot.MESSAGE, runner=product.runner, http_reader=product.read)


def preflight_app(tmp_path, monkeypatch, published, *, task_failure=False):
    store, prepared, values = Store(), [], {}
    def tool(sid, name, arguments):
        if name == 'get_html_navigation':
            return {'state': 'ready', 'children': [{'node_id': 'synthetic-node'}], 'next_offset': None}
        return {'id': sid, 'revision': 1, 'goal': 'Synthetic public input', 'model': None}
    app = SimpleNamespace(kb=SimpleNamespace(store=store), tools=SimpleNamespace(call=tool),
        sessions=SimpleNamespace(get=lambda sid: values[sid]),
        agents=SimpleNamespace(runtime_tool=lambda *_args, **_kwargs: {}, _require_current_entry=Mock(), close=Mock()))
    def create(_url, root, case):
        if task_failure:
            raise RuntimeError('Synthetic preflight preparation failure')
        sid = str(len(prepared) + 1) * 32
        prepared.append(case)
        values[sid] = {'id': sid}
        path = root / 'public/cases' / case / 'problem.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Synthetic public input ' + case, encoding='utf-8')
        return {'session': {'id': sid}, 'input_manifest': {'case_id': case, 'files': [{
            'path': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}}
    server = SimpleNamespace(shutdown=Mock(), server_close=Mock())
    monkeypatch.setattr(pilot, 'open_store', lambda _root: store)
    monkeypatch.setattr(pilot, 'embedding_enabled', lambda _root: False)
    monkeypatch.setattr(pilot, 'create_application', lambda *_args: app)
    monkeypatch.setattr(pilot, 'start_service', lambda _app: (server, 'http://127.0.0.1:1'))
    monkeypatch.setattr(pilot, 'install_backend_policy', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pilot, 'create_task', create)
    monkeypatch.setattr(pilot, 'source_hashes', lambda: {'synthetic-source.py': 'synthetic-hash'})
    monkeypatch.setattr(pilot, 'run_tasks', Mock(side_effect=AssertionError('Preflight must not launch any model')))
    return app, store, prepared, server


def test_prepare_only_creates_exactly_three_tasks_and_always_cleans_up(tmp_path, monkeypatch, published):
    folder = make_tree(tmp_path)
    app, store, prepared, server = preflight_app(tmp_path, monkeypatch, published)
    monkeypatch.setenv('SIM_ADVISOR_HTML_TREE_INDEX', 'previous-index')
    baseline = tmp_path / 'old-protocol.json'
    baseline.write_text('{"synthetic_history":true}', encoding='utf-8')
    root = tmp_path / 'benchmark'
    assert pilot.main(['--tree-root', str(folder), '--run-id', 'synthetic', '--benchmark-root', str(root),
        '--data-root', str(tmp_path / 'data'), '--scope-library-id', 'manuals',
        '--historical-baseline', str(baseline), '--prepare-only']) == 0
    here = root / 'private/evaluations/synthetic'
    protocol = json.loads((here / 'protocol.json').read_text(encoding='utf-8'))
    assert prepared == list(pilot.CASES)
    assert protocol['conditions'] == ['html_tree'] and protocol['attempts_per_case'] == 1
    assert protocol['historical_baselines'][0]['sha256'] == hashlib.sha256(baseline.read_bytes()).hexdigest()
    assert str(baseline) not in protocol['route_message']
    assert protocol['historical_baselines_supplied_to_model'] is False and protocol['method_cards_exposed'] is False
    assert store.flags == {'manuals': False, 'unrelated': True}
    cleanup = json.loads((here / 'cleanup.json').read_text(encoding='utf-8'))
    assert cleanup['scope_restored'] and cleanup['owned_services_stopped'] and cleanup['environment_restored']
    assert os.environ['SIM_ADVISOR_HTML_TREE_INDEX'] == 'previous-index'
    app.agents.close.assert_called_once()
    server.shutdown.assert_called_once()
    with pytest.raises(FileExistsError):
        pilot.main(['--tree-root', str(folder), '--run-id', 'synthetic', '--benchmark-root', str(root), '--prepare-only'])


def test_prepare_failure_still_writes_cleanup_and_restores_scope(tmp_path, monkeypatch, published):
    folder = make_tree(tmp_path)
    app, store, prepared, server = preflight_app(tmp_path, monkeypatch, published, task_failure=True)
    root = tmp_path / 'benchmark'
    with pytest.raises(RuntimeError, match='Synthetic preflight'):
        pilot.main(['--tree-root', str(folder), '--run-id', 'failed', '--benchmark-root', str(root),
                    '--data-root', str(tmp_path / 'data'), '--scope-library-id', 'manuals', '--prepare-only'])
    cleanup = json.loads((root / 'private/evaluations/failed/cleanup.json').read_text(encoding='utf-8'))
    assert cleanup['scope_restored'] and cleanup['owned_services_stopped'] and cleanup['environment_restored']
    assert store.flags == {'manuals': False, 'unrelated': True} and not prepared
    app.agents.close.assert_called_once()


def test_real_production_html_interface_with_only_fictional_manual(tmp_path, monkeypatch):
    from advisor.html_tree import build_html_tree
    from advisor.library_store import LibraryStore
    from advisor.planning_service import PlanningService
    root = tmp_path / 'original-html'
    package = root / 'com.comsol.help.synthetic'
    package.mkdir(parents=True)
    (package / 'toc.xml').write_text('''<toc label="Synthetic manual">
      <topic label="Synthetic interface" href="interface.html#intro">
        <topic label="Synthetic condition" href="interface.html#condition"/>
      </topic></toc>''', encoding='utf-8')
    (package / 'interface.html').write_text('''<html><body><h1 id="intro">Synthetic interface</h1>
      <p>Original fictional manual introduction.</p><h2 id="condition">Synthetic condition</h2>
      <p>Fictional condition retained as original source text.</p></body></html>''', encoding='utf-8')
    store = LibraryStore(tmp_path / 'libraries')
    store.register('Fictional manuals', 'user-manuals', [{'id': 'manual-root', 'path': str(root), 'role': 'manuals'}], library_id='manuals')
    store.set_enabled('manuals', True)
    built = build_html_tree(root, tmp_path / 'tree', library_id='manuals', root_id='manual-root')
    monkeypatch.setenv('SIM_ADVISOR_HTML_TREE_INDEX', built['index_path'])
    sessions = SimpleNamespace(get=lambda sid: {'id': sid, 'revision': 1})
    service = PlanningService(tmp_path / 'workspace', sessions, SimpleNamespace(store=store))
    app = SimpleNamespace(kb=SimpleNamespace(store=store), sessions=sessions,
        agents=SimpleNamespace(runtime_tool=lambda sid, name, arguments, **_kwargs:
            service.call_public(name, dict(arguments, session_id=sid)), _require_current_entry=Mock()))
    audit = tmp_path / 'real-interface-audit.jsonl'
    definitions = pilot.install_tools(app, {'task'}, store.searchable_library_ids(), {'task': audit})
    assert pilot.HTML_TOOLS.issubset(tool['name'] for tool in definitions)
    nav = app.agents.runtime_tool('task', 'get_html_navigation', {'query': 'Synthetic condition'})
    assert nav['state'] == 'ready' and nav['items']
    node = nav['items'][0]['node_id']
    page = app.agents.runtime_tool('task', 'read_html_section', {'node_id': node})
    assert 'Fictional condition retained' in page['text'] and page['next_offset'] is None
    assert page['evidence_refs'][0]['context'] == 'html_section'
    assert 'format' not in page and page['ancestor_titles']
    assert service.read_sources('task') == []
    assert pilot.tree_snapshot(Path(built['index_path']).parent)['manifest']['node_count'] == 3
    store.set_enabled('manuals', False)
    denied = app.agents.runtime_tool('task', 'read_html_section', {'node_id': node})
    assert denied['success'] is False
