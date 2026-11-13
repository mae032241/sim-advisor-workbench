"""Source-only route-card orchestration; all cases and runtimes are synthetic."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from advisor.bounded_knowledge import BoundedKnowledge
from advisor.route_cards import RouteCardBuilder, RouteCardStore, SECTIONS, card_schema
from advisor.tool_contracts import explicit_schema
from scripts import build_route_cards as build
from test_planning_case_tools import case_tools  # noqa: F401; synthetic service fixture


def app_for(fixture):
    return SimpleNamespace(kb=fixture.provider, tools=fixture.tools, sessions=fixture.sessions,
                           agents=SimpleNamespace(close=Mock()))


def options(tmp_path, case_id='synthetic-case'):
    return SimpleNamespace(output=tmp_path / 'curator', scope_library_ids=['cases'],
                           model='gpt-6-luna', effort='high', timeout=5, parallel=2,
                           cases=[case_id], library_root=tmp_path / 'libraries')


def card(case_id, ref):
    value = {'schema': 'route-method-card/v1', 'title': 'Synthetic size control',
             'family': 'Synthetic discretization',
             'summary': 'A synthetic saved size setting; no physical selection rule is established.',
             'source_case_ids': [case_id], 'sections': {
                 name: [{'explanation': 'No documented condition is established by this synthetic fixture.',
                         'origin': 'unknown', 'evidence_refs': [], 'limitations': ['Synthetic data only.']}]
                 for name in SECTIONS}}
    value['sections']['implementation'] = [{'explanation': 'The returned saved mesh size property exists.',
                                           'origin': 'saved_configuration', 'evidence_refs': [ref],
                                           'limitations': ['Saved presence does not establish activation.']}]
    return value


def read_directory(callback, case_id):
    pages = []
    for section in ('nodes', 'stages'):
        offset = 0
        while True:
            page = callback('get_case_evidence_directory', {'case_id': case_id, 'section': section, 'offset': offset})
            assert 'items' in page, page
            pages.append(page)
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
    return pages


def valid_delivery(_runtime, _thread_id):
    return {'valid': True, 'issues': [], 'tool_output_count': 1000,
            'synthetic_test_only': True, 'max_output_bytes_observed': 6999}


def test_bounded_code_mode_profile_keeps_host_and_blocks_planner_write_tools(case_tools, tmp_path):
    fixture = case_tools
    reader = build.SourceReader(app_for(fixture), fixture.overview['id'], fixture.sid,
                                build.Audit(tmp_path / 'audit.jsonl'), ['cases'])
    store = RouteCardStore(tmp_path / 'cards', fixture.provider)
    bounded = BoundedKnowledge(reader)
    builder = RouteCardBuilder(store, bounded.call, case_id=fixture.overview['id'])
    definitions = {item['name']: item for item in build.curator_definitions(builder, bounded)}
    assert set(definitions) == build.READ_TOOLS | build.LOCAL_TOOLS
    assert not {'save_plan', 'get_task_context', 'search_cases', 'record_delivery', 'authorize_read'} & set(definitions)
    assert build.PROFILE['features.code_mode'] is True and build.PROFILE['features.code_mode_host'] is True
    assert build.PROFILE['features.shell_tool'] is False and build.PROFILE['features.apps'] is False
    assert build.parser().parse_args(['--case', 'fixture', '--output', str(tmp_path)]).model == 'gpt-6-luna'
    assert definitions['get_tool_contract']['inputSchema']['properties']['length']['maximum'] == 1200
    assert definitions['read_source']['inputSchema']['properties']['length']['maximum'] == 1200
    assert definitions['read_case_configuration']['inputSchema']['properties']['limit']['maximum'] == 1


def test_transport_contract_requires_one_call_and_one_decoded_response_per_cell():
    instructions = build.TRANSPORT_INSTRUCTIONS
    assert '每个exec单元只能调用一个业务工具一次' in instructions
    assert '不得使用Promise.all、循环、并行调用、批量调用' in instructions
    assert 'JSON.parse(raw)' in instructions and '再读取字段或做投影' in instructions
    assert '每单元只能调用一次text(result)' in instructions
    assert '投影不能替代该响应的完整交付' in instructions
    assert '另开exec单元，仅调用一次read_tool_page' in instructions
    manifest = build.instruction_manifest()
    assert manifest['source_instructions_sha256'] == hashlib.sha256(build.SOURCE_INSTRUCTIONS.encode('utf-8')).hexdigest()
    assert manifest['transport_instructions_sha256'] == hashlib.sha256(instructions.encode('utf-8')).hexdigest()
    assert manifest['instructions_sha256'] == hashlib.sha256(build.INSTRUCTIONS.encode('utf-8')).hexdigest()


def test_directory_indexes_every_saved_header_without_hydrating_nodes(case_tools, tmp_path, monkeypatch):
    from advisor.compact_case import CompactCase
    fixture = case_tools
    monkeypatch.setattr(CompactCase, 'read_node', Mock(side_effect=AssertionError('Headers must not hydrate properties')))
    index = build.compact_node_headers(fixture.provider, fixture.overview['id'])
    assert index['state'] == 'complete' and len(index['nodes']) > 8
    assert 'compact:node/30' in {item['node_path'] for item in index['nodes']}
    assert all('properties' not in item for item in index['nodes'])
    assert index['headers_sha256'] == build.digest(index['nodes'])
    assert fixture.tools.read_sources(fixture.sid) == []


def test_directory_expands_every_guide_stage_and_pages_headers(case_tools, tmp_path, monkeypatch):
    import advisor.case_guides as guides
    fixture = case_tools
    calls = []
    original = fixture.tools.call_public

    def service(name, arguments):
        calls.append((name, deepcopy(arguments)))
        return original(name, arguments)

    monkeypatch.setattr(fixture.tools, 'call_public', service)
    monkeypatch.setattr(guides, 'get_case_guide', lambda _store, identity: {
        'state': 'ready', 'case_id': identity, 'is_read_evidence': False,
        'stages': [{'id': name, 'title': name, 'source_refs': [], 'configuration_requests': []}
                   for name in ('geometry', 'mesh', 'physics', 'study', 'variable')]})
    reader = build.SourceReader(app_for(fixture), fixture.overview['id'], fixture.sid,
                                build.Audit(tmp_path / 'audit.jsonl'), ['cases'])
    directory = reader.prepare_directory()
    assert {arguments['stage'] for name, arguments in calls if name == 'get_case_guide' and 'stage' in arguments} == {
        'geometry', 'mesh', 'physics', 'study', 'variable'}
    pages = read_directory(lambda name, arguments: reader.directory_page(arguments), fixture.overview['id'])
    paths = [item['node_path'] for page in pages if page['section'] == 'nodes' for item in page['items']]
    assert len(paths) == directory['node_count'] == len(set(paths))
    assert all(page['is_read_evidence'] is False and page['evidence_refs'] == [] for page in pages)


def test_reader_rejects_cross_case_search_unissued_ids_and_writes(case_tools, tmp_path):
    fixture = case_tools
    reader = build.SourceReader(app_for(fixture), fixture.overview['id'], fixture.sid,
                                build.Audit(tmp_path / 'audit.jsonl'), ['cases'])
    forbidden = [
        ('get_case_guide', {'case_id': 'other'}),
        ('read_case_configuration', {'case_id': 'other'}),
        ('search_knowledge', {'case_id': 'other', 'query': 'x'}),
        ('search_knowledge', {'query': 'x', 'kind': 'all'}),
        ('search_knowledge', {'query': 'x', 'kind': 'manual', 'mode': 'qmd_vector'}),
        ('search_knowledge', {'query': 'x', 'kind': 'manual', 'library_ids': ['outside']}),
        ('read_source', {'id': 'unissued'}),
        ('read_source', {'id': fixture.overview['id'], 'session_id': 'other-session'}),
        ('save_plan', {}), ('set_libraries_enabled', {}), ('get_task_context', {}),
    ]
    for name, arguments in forbidden:
        with pytest.raises(PermissionError):
            reader(name, arguments)
    assert not reader.backend_reads


def test_general_manual_search_checks_real_metadata_and_excludes_other_models(tmp_path):
    sources = {
        'assigned': {'id': 'assigned', 'type': 'model_case', 'kind': 'case', 'library_id': 'cases', 'case_group': 'one'},
        'other': {'id': 'other', 'type': 'model_case', 'kind': 'case', 'library_id': 'cases', 'case_group': 'two'},
        'manual': {'id': 'manual', 'type': 'manual_topic', 'kind': 'manual', 'library_id': 'manuals',
                   'asset_key': 'help:com.comsol.help.heat/heat_ug.html'},
        'tutorial': {'id': 'tutorial', 'type': 'manual_topic', 'kind': 'manual', 'library_id': 'manuals',
                     'asset_key': 'help:com.comsol.help.models.unrelated/model.html'},
    }
    service = Mock(return_value={'results': [{'id': identity, 'title': identity} for identity in ('manual', 'tutorial', 'other')]})
    store = SimpleNamespace(get=sources.get, require_not_heldout=Mock(),
                            library=lambda identity, **_kw: {'type': 'user-manuals' if identity == 'manuals' else 'user-models'})
    provider = SimpleNamespace(store=store, source_available=lambda identity: identity in sources,
                               get_case_authority=lambda identity: sources[identity])
    app = SimpleNamespace(kb=provider, tools=SimpleNamespace(call_public=service))
    reader = build.SourceReader(app, 'assigned', 'session', build.Audit(tmp_path / 'audit.jsonl'), ['cases', 'manuals'])
    result = reader('search_knowledge', {'kind': 'manual', 'query': 'source interface assumptions'})
    assert [item['id'] for item in result['results']] == ['manual']
    assert reader.allowed_ids == {'assigned', 'manual'}
    assert service.call_args.args[1]['library_ids'] == ['cases', 'manuals']
    reader('read_source', {'id': 'manual'})
    with pytest.raises(PermissionError):
        reader('read_source', {'id': 'other'})


def test_constructor_waits_for_natural_completion_and_records_visible_pages(case_tools, tmp_path):
    fixture = case_tools
    app, args = app_for(fixture), options(tmp_path, fixture.overview['id'])
    store, runtimes = RouteCardStore(args.output, fixture.provider), []

    class Runtime:
        def __init__(self, **kwargs):
            self.callback, self.event = kwargs['on_tool'], kwargs['on_event']
            self.done, self.interrupted = False, False
            runtimes.append(self)
        def start(self, **kwargs):
            self.definitions, self.instructions = kwargs['tools'], kwargs['instructions']
            return {'thread_id': 'synthetic-route-thread'}
        def send(self, text):
            assert fixture.overview['id'] in text
            read_directory(self.callback, fixture.overview['id'])
            contract = self.callback('get_tool_contract', {'tool': 'submit_method_card', 'path': '/card/sections/implementation/0/origin'})
            assert 'saved_configuration' in json.loads(contract['text'])['enum']
            offset, schema_pages = 0, []
            while True:
                full_contract = self.callback('get_tool_contract', {'tool': 'submit_method_card', 'offset': offset})
                assert full_contract['returned_characters'] <= 1200
                schema_pages.append(full_contract['text'])
                if full_contract['next_offset'] is None:
                    break
                offset = full_contract['next_offset']
            assert len(schema_pages) > 1
            assert json.loads(''.join(schema_pages))['properties']['card'] == explicit_schema(card_schema())
            page = self.callback('read_case_configuration', {'case_id': fixture.overview['id'], 'node_path': 'compact:node/3'})
            ref = {'source_id': fixture.overview['id'], 'context': 'case_configuration',
                   'locator': page['nodes'][0]['properties'][0]['evidence_locator']}
            saved = self.callback('submit_method_card', {'card': card(fixture.overview['id'], ref)})
            assert saved['saved'], saved
            self.timer = threading.Timer(0.15, self.finish)
            self.timer.start()
        def finish(self):
            self.done = True
            self.event({'type': 'turn', 'event': 'completed', 'status': 'completed'})
        def status(self):
            return {'running': True, 'state': 'idle' if self.done else 'busy'}
        def interrupt(self):
            self.interrupted = True
        def close(self):
            assert self.done, 'Submission must not terminate an active turn'
            self.timer.join()

    result = build.build_one(app, store, args, fixture.overview['id'], threading.Event(),
                             runtime_factory=Runtime, delivery_inspector=valid_delivery)
    assert result['status'] == 'completed' and result['card_submitted'] and result['card_count'] == 1
    assert not runtimes[0].interrupted
    directory = args.output / 'cases' / build.case_key(fixture.overview['id'])
    sources = json.loads((directory / 'source-manifest.json').read_text(encoding='utf-8'))
    headers = json.loads((directory / 'evidence-directory.json').read_text(encoding='utf-8'))
    assert len(sources['directory_coverage']['nodes']) == headers['node_count']
    assert sources['visible_deliveries'][0]['evidence_refs']
    assert sources['source_bindings'] and sources['source_only'] and sources['no_questions_access']
    assert sources['bounded_code_mode'] is True and 'direct_tool_mode' not in sources
    assert sources['instructions_sha256'] == hashlib.sha256(build.INSTRUCTIONS.encode('utf-8')).hexdigest()
    assert max(row['bytes'] for row in sources['callback_deliveries']) <= 7000
    assert app.sessions.get(result['session_id'])['revision'] == 1
    frozen = store.freeze_catalog()
    assert frozen['method_count'] == 1


def test_raw_backend_property_cannot_be_cited_until_visible_delivery(case_tools, tmp_path, monkeypatch):
    fixture = case_tools
    app, args = app_for(fixture), options(tmp_path, fixture.overview['id'])
    original = fixture.tools.call_public
    hidden_locator = 'saved:compact:node/3::property/hidden-synthetic'

    def service(name, arguments):
        value = original(name, arguments)
        if name == 'read_case_configuration':
            value['nodes'][0]['properties'].append({'id': 'hidden-synthetic', 'name': 'synthetic-large',
                'value': '文' * 10000, 'evidence_locator': hidden_locator})
        return value

    monkeypatch.setattr(fixture.tools, 'call_public', service)
    store = RouteCardStore(args.output, fixture.provider)

    class Runtime:
        def __init__(self, **kwargs):
            self.callback, self.event = kwargs['on_tool'], kwargs['on_event']
        def start(self, **_kwargs):
            return {'thread_id': 'synthetic-hidden-property'}
        def send(self, _text):
            read_directory(self.callback, fixture.overview['id'])
            page = self.callback('read_case_configuration', {'case_id': fixture.overview['id'], 'node_path': 'compact:node/3'})
            assert hidden_locator not in json.dumps(page, ensure_ascii=False)
            ref = {'source_id': fixture.overview['id'], 'context': 'case_configuration', 'locator': hidden_locator}
            rejected = self.callback('submit_method_card', {'card': card(fixture.overview['id'], ref)})
            assert rejected.get('success') is False and 'Unissued' in str(rejected)
            self.event({'type': 'turn', 'event': 'completed', 'status': 'completed'})
        def status(self):
            return {'running': True, 'state': 'idle'}
        def close(self):
            pass

    result = build.build_one(app, store, args, fixture.overview['id'], threading.Event(),
                             runtime_factory=Runtime, delivery_inspector=valid_delivery)
    assert result['status'] == 'no_submission' and not result['card_submitted']
    assert not store.directory.exists()


def test_unread_directory_and_invalid_actual_delivery_block_success(case_tools, tmp_path):
    fixture = case_tools
    app, args = app_for(fixture), options(tmp_path, fixture.overview['id'])
    store = RouteCardStore(args.output, fixture.provider)

    class Runtime:
        def __init__(self, **kwargs):
            self.callback, self.event = kwargs['on_tool'], kwargs['on_event']
        def start(self, **_kwargs):
            return {'thread_id': 'synthetic-invalid-delivery'}
        def send(self, _text):
            page = self.callback('read_case_configuration', {'case_id': fixture.overview['id'], 'node_path': 'compact:node/3'})
            rejected = self.callback('submit_method_card', {'card': card(fixture.overview['id'], page['evidence_refs'][0])})
            assert rejected.get('success') is False and 'undelivered' in str(rejected)
            self.event({'type': 'turn', 'event': 'completed', 'status': 'completed'})
        def status(self):
            return {'running': True, 'state': 'idle'}
        def close(self):
            pass

    result = build.build_one(app, store, args, fixture.overview['id'], threading.Event(), runtime_factory=Runtime,
                             delivery_inspector=lambda *_: {'valid': False, 'tool_output_count': 2, 'issues': ['Output truncated']})
    assert result['status'] == 'invalid_delivery' and not result['card_submitted']
    assert result['delivery_audit']['issues'] == ['Output truncated']


def test_real_audit_receives_canonical_hashes_for_complete_callback_responses(case_tools, tmp_path, monkeypatch):
    import advisor.tool_delivery_audit as delivery
    fixture = case_tools
    app, args = app_for(fixture), options(tmp_path, fixture.overview['id'])
    store, observed, expected = RouteCardStore(args.output, fixture.provider), [], []
    closed = []

    class Runtime:
        def __init__(self, **kwargs):
            self.callback, self.event = kwargs['on_tool'], kwargs['on_event']
        def start(self, **_kwargs):
            return {'thread_id': 'synthetic-canonical-delivery'}
        def send(self, _text):
            response = self.callback('get_case_evidence_directory', {'case_id': fixture.overview['id']})
            expected.append(hashlib.sha256(json.dumps(response, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest())
            self.event({'type': 'turn', 'event': 'completed', 'status': 'completed'})
        def status(self):
            return {'running': True, 'state': 'idle'}
        def close(self):
            closed.append(True)

    def audit(_path, **arguments):
        assert closed, 'Inspect persisted output only after runtime close/flush'
        observed.append(arguments)
        return {'valid': True, 'issues': [], 'tool_output_count': len(arguments['expected_response_sha256'])}

    monkeypatch.setattr(delivery, 'locate_rollout', lambda *_args, **_kwargs: tmp_path / 'synthetic-rollout.jsonl')
    monkeypatch.setattr(delivery, 'audit_rollout', audit)
    result = build.build_one(app, store, args, fixture.overview['id'], threading.Event(), runtime_factory=Runtime)
    assert result['status'] == 'no_submission' and result['delivery_audit']['valid'] is True
    assert observed[0]['expected_response_sha256'] == expected
    assert observed[0]['require_outputs'] is True and observed[0]['max_output_bytes'] == 8192


def test_timeout_interrupts_and_rejects_late_submission(case_tools, tmp_path):
    fixture = case_tools
    app, args = app_for(fixture), options(tmp_path, fixture.overview['id'])
    args.timeout = 0.01
    store = RouteCardStore(args.output, fixture.provider)

    class Runtime:
        def __init__(self, **kwargs):
            self.callback, self.interrupted = kwargs['on_tool'], False
        def start(self, **_kwargs):
            return {'thread_id': 'synthetic-timeout'}
        def send(self, _text):
            read_directory(self.callback, fixture.overview['id'])
        def status(self):
            return {'running': True, 'state': 'busy'}
        def interrupt(self):
            self.interrupted = True
        def close(self):
            assert self.interrupted
            late = self.callback('submit_method_card', {'card': {}})
            assert late.get('success') is False

    result = build.build_one(app, store, args, fixture.overview['id'], threading.Event(),
                             runtime_factory=Runtime, delivery_inspector=valid_delivery)
    assert result['status'] == 'timeout' and not result['card_submitted']


def test_stop_file_prevents_session_or_launch(tmp_path):
    args = options(tmp_path)
    args.output.mkdir()
    (args.output / 'stop.request').write_text('stop', encoding='utf-8')
    app, factory = SimpleNamespace(sessions=Mock()), Mock()
    result = build.build_one(app, None, args, 'synthetic', threading.Event(), runtime_factory=factory)
    assert result['status'] == 'stopped'
    app.sessions.create.assert_not_called()
    factory.assert_not_called()


def test_scope_restores_after_failure_and_invalid_run_never_freezes(case_tools, tmp_path, monkeypatch):
    fixture = case_tools
    fixture.store.set_enabled('cases', False)
    app = app_for(fixture)
    monkeypatch.setattr(build, 'create_local_app', lambda *_: app)

    def fail(*_):
        assert fixture.store.library('cases')['enabled']
        raise RuntimeError('Synthetic failure before starting a model')

    monkeypatch.setattr(build, 'build_one', fail)
    output = tmp_path / 'scope-output'
    assert build.main(['--case', fixture.overview['id'], '--output', str(output),
                       '--library-root', str(fixture.store.root), '--scope-library-id', 'cases']) == 1
    assert not fixture.store.library('cases')['enabled']
    assert json.loads((output / 'library-flags-before.json').read_text(encoding='utf-8')) == json.loads(
        (output / 'library-flags-after.json').read_text(encoding='utf-8'))
    manifest = json.loads((output / 'cards-manifest.json').read_text(encoding='utf-8'))
    assert manifest['valid'] is False and manifest['catalog']['frozen'] is False
    assert not (output / 'route-method-cards/catalog.json').exists()
    run_manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    assert run_manifest['bounded_code_mode'] is True and 'direct_tool_mode' not in run_manifest
    assert run_manifest['profile']['features.code_mode'] is True
    assert run_manifest['profile']['features.code_mode_host'] is True
    app.agents.close.assert_called_once()
    with pytest.raises(FileExistsError, match='never overwritten'):
        build.main(['--case', fixture.overview['id'], '--output', str(output), '--library-root', str(fixture.store.root)])


@pytest.mark.parametrize('changes', [{'parallel': 3}, {'cases': ['same', 'same']}, {'cases': []},
                                     {'cases': [str(index) for index in range(7)]}])
def test_bad_options_fail_before_source_or_runtime_preparation(tmp_path, changes):
    args = options(tmp_path)
    for name, value in changes.items():
        setattr(args, name, value)
    with pytest.raises(ValueError):
        build.validate_options(args)
    assert not args.output.exists()
