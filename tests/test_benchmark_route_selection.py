"""Route pilot contract and lifecycle checks; no inference or private inputs."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from advisor.case_methods import tool_definitions
from advisor.agent_sessions import INSTRUCTIONS, planning_tools
from scripts import benchmark_route_selection as experiment

SID_A, SID_B = 'a' * 32, 'b' * 32
CASE_ID = 'public-allowed-case'


def test_candidate_file_contains_only_common_ids_and_prompt_is_bounded(tmp_path):
    path = tmp_path / 'candidates.json'
    value = {'schema': experiment.CANDIDATE_SCHEMA, 'case_ids': [CASE_ID, 'another-allowed-case']}
    path.write_text(json.dumps(value), encoding='utf-8')
    loaded, sha = experiment.load_candidates(path)
    assert loaded == value and sha == hashlib.sha256(path.read_bytes()).hexdigest()
    message = experiment.route_message(value['case_ids'])
    assert all(case_id in message for case_id in value['case_ids'])
    assert len(message) <= 12000 and '无需调用 save_plan' in message
    assert 'existing_navigation' not in message and 'case_methods' not in message
    defaults = experiment.parser().parse_args(['--method-root', 'methods', '--candidate-list', str(path)])
    assert defaults.model == 'gpt-6-luna' and defaults.parallel == 2
    assert experiment.CASES == ('case-01', 'case-02', 'case-09')


@pytest.mark.parametrize('mutation', ['extra-prose', 'duplicate', 'path', 'whitespace', 'empty'])
def test_candidates_reject_arbitrary_payloads_and_noncanonical_ids(tmp_path, mutation):
    value = {'schema': experiment.CANDIDATE_SCHEMA, 'case_ids': [CASE_ID]}
    if mutation == 'extra-prose':
        value['answer'] = 'Never pass a private answer as a candidate payload.'
    elif mutation == 'duplicate':
        value['case_ids'] *= 2
    elif mutation == 'path':
        value['case_ids'] = ['../case.mph']
    elif mutation == 'whitespace':
        value['case_ids'] = ['\ncase']
    else:
        value['case_ids'] = []
    path = tmp_path / 'candidates.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError):
        experiment.load_candidates(path)


def test_profile_hashes_reject_aliases_and_detect_changes(tmp_path):
    folder = tmp_path / 'case-methods'
    folder.mkdir()
    path = folder / 'profile.json'
    path.write_text('{"profile":"frozen"}', encoding='utf-8')
    before = experiment.profile_files(tmp_path)
    assert list(before) == ['case-methods/profile.json']
    path.write_text('{"profile":"changed"}', encoding='utf-8')
    assert before != experiment.profile_files(tmp_path)
    (folder / 'unexpected.txt').write_text('not a profile', encoding='utf-8')
    with pytest.raises(ValueError, match='JSON'):
        experiment.profile_files(tmp_path)


def test_runtime_keeps_production_prompt_and_all_schemas_then_appends_method():
    calls = []
    underlying = SimpleNamespace(
        backend_id='codex-app-server',
        start=lambda **kwargs: calls.append(kwargs) or {'thread_id': 'new'},
        status=lambda: {'running': True},
    )
    tools = planning_tools()
    original = copy.deepcopy(tools)
    runtime = experiment.ExtendedRuntime(underlying, tool_definitions())
    runtime.start(tools=tools, instructions=INSTRUCTIONS)
    assert calls[0]['instructions'] == INSTRUCTIONS
    assert calls[0]['tools'][:-1] == original
    assert calls[0]['tools'][-1] == tool_definitions()[0]
    assert tools == original and runtime.backend_id == 'codex-app-server'
    assert runtime.status()['running'] is True
    with pytest.raises(ValueError, match='duplicates'):
        runtime.start(tools=tools + tool_definitions(), instructions=INSTRUCTIONS)


def guarded_app(tmp_path, *, method_failure=None):
    forwarded, method_calls, validated = [], [], []

    def original(sid, name, args, **kwargs):
        forwarded.append((sid, name, copy.deepcopy(args), kwargs))
        return {'success': True, 'evidence_refs': [{'source_id': 'source', 'context': 'source_text', 'locator': 'text:0:10'}]}

    def method(case_id, *, enabled):
        method_calls.append((case_id, enabled))
        if method_failure:
            raise method_failure('Source is disabled or held out')
        return {'state': 'ready' if enabled else 'not_prepared', 'case_id': case_id,
                'is_read_evidence': False, **({'profile': {'method': 'short navigation'}} if enabled else {})}

    app = SimpleNamespace(
        sessions=SimpleNamespace(get=lambda sid: validated.append(('session', sid)) or {'id': sid}),
        agents=SimpleNamespace(runtime_tool=original,
                              _require_current_entry=lambda sid, entry: validated.append(('lifecycle', sid))),
    )
    paths = {SID_A: tmp_path / 'a.jsonl', SID_B: tmp_path / 'b.jsonl'}
    definitions = experiment.install_route_tools(app, SimpleNamespace(get_case_method=method), tool_definitions(),
        {SID_A: 'existing_navigation', SID_B: 'case_methods'}, [CASE_ID], ['common-library'], paths)
    return app, forwarded, method_calls, validated, paths, definitions


def test_both_conditions_share_schema_but_method_availability_differs(tmp_path):
    app, forwarded, methods, validated, paths, definitions = guarded_app(tmp_path)
    entry = object()
    a = app.agents.runtime_tool(SID_A, 'get_case_method', {'case_id': CASE_ID}, source_entry=entry)
    b = app.agents.runtime_tool(SID_B, 'get_case_method', {'case_id': CASE_ID}, source_entry=entry)
    assert a['state'] == 'not_prepared' and 'profile' not in a
    assert b['state'] == 'ready' and b['is_read_evidence'] is False
    assert methods == [(CASE_ID, False), (CASE_ID, True)] and not forwarded
    assert validated == [('session', SID_A), ('lifecycle', SID_A), ('session', SID_B), ('lifecycle', SID_B)]
    # Profiles not in the frozen candidate set are unavailable in either arm.
    assert app.agents.runtime_tool(SID_B, 'get_case_method', {'case_id': 'outside-list'})['state'] == 'not_prepared'
    contract = app.agents.runtime_tool(SID_B, 'get_tool_contract', {'tool': 'get_case_method'})
    schema = tool_definitions()[0]['inputSchema']
    assert json.loads(contract['text']) == schema
    assert contract['schema_sha256'] == experiment.digest(experiment._canonical(schema))
    assert definitions[:-1] == planning_tools()
    rows = [json.loads(line) for line in paths[SID_B].read_text(encoding='utf-8').splitlines()]
    assert all(row['seconds'] >= 0 and row['result_sha256'] for row in rows)
    assert rows[0]['result_state'] == 'ready'


@pytest.mark.parametrize('error', [PermissionError, FileNotFoundError])
def test_method_permission_rejection_does_not_expose_a_profile(tmp_path, error):
    app, _, methods, _, _, _ = guarded_app(tmp_path, method_failure=error)
    for sid in (SID_A, SID_B):
        result = app.agents.runtime_tool(sid, 'get_case_method', {'case_id': CASE_ID})
        assert result['success'] is False and 'profile' not in json.dumps(result)
    assert methods == [(CASE_ID, False), (CASE_ID, True)]


def test_guard_rejects_writes_and_scope_changes_but_keeps_normal_reads(tmp_path):
    app, forwarded, _, _, paths, _ = guarded_app(tmp_path)
    for name in ('save_plan', 'update_steps', 'save_plan_settings', 'repair_plan_draft', 'future_unknown_write'):
        assert app.agents.runtime_tool(SID_A, name, {'private_payload': 'must-not-be-audited'})['success'] is False
    for args in ({'library_ids': ['common-library'], 'enabled': False},
                 {'library_ids': ['another-library'], 'enabled': True}):
        assert app.agents.runtime_tool(SID_A, 'set_libraries_enabled', args)['success'] is False
    repeated = app.agents.runtime_tool(SID_A, 'set_libraries_enabled', {'library_ids': ['common-library'], 'enabled': True})
    assert repeated['changed'] == 0 and repeated['scope_frozen']
    for name in ('read_source', 'search_cases', 'search_knowledge', 'get_task_context', 'read_case_configuration'):
        assert app.agents.runtime_tool(SID_A, name, {})['success'] is True
    assert [row[1] for row in forwarded] == ['read_source', 'search_cases', 'search_knowledge', 'get_task_context', 'read_case_configuration']
    assert 'must-not-be-audited' not in paths[SID_A].read_text(encoding='utf-8')
    assert app.agents.runtime_tool('f' * 32, 'read_source', {})['success'] is False
    assert len(forwarded) == 5


def test_bad_method_arguments_use_shared_schema_and_do_not_read(tmp_path):
    app, _, methods, _, _, _ = guarded_app(tmp_path)
    for arguments in ({}, {'case_id': CASE_ID, 'profile': 'forged'}, {'case_id': '../file'}):
        result = app.agents.runtime_tool(SID_B, 'get_case_method', arguments)
        assert result['success'] is False
        assert json.loads(result['contentItems'][0]['text'])['error_code'] == 'tool_arguments_invalid'
    assert not methods


class Product:
    def __init__(self, *, terminal='completed', reply=None, mutate=False, cancellation=None):
        self.terminal, self.reply = terminal, reply or '{"chosen_route":{},"alternatives":[],"applicability_conditions":[],"unresolved":[],"sources":[]}'
        self.mutate, self.cancellation = mutate, cancellation
        self.connected = self.active = False
        self.messages, self.calls = [], []
        self.last_turn, self.model, self.effort = None, 'default', 'medium'
        self.polls = 0
        self.session = {'id': SID_A, 'revision': 1, 'goal': 'Complete public requirement',
                        'model': {'facts': []}, 'plan': {'engine': 'intake-only', 'steps': []}, 'history': []}

    def state(self):
        return {'status': 'running' if self.active else 'idle' if self.connected else 'disconnected',
            'connected': self.connected, 'alive': self.connected, 'thread_id': 'independent-thread' if self.connected else None,
            'active_turn_id': 'turn-1' if self.active else None, 'turn_outcome_supported': True,
            'last_turn': self.last_turn, 'messages': copy.deepcopy(self.messages),
            'model': self.model, 'reasoning_effort': self.effort, 'managed_backend': 'codex',
            'connection_mode': 'managed', 'backend': 'codex-app-server',
            'warning': 'Transient native warning' if self.active else None,
            'error_metadata': {'will_retry': True, 'code': 'native-transient'} if self.active else None}

    def runner(self, command, **kwargs):
        action = command[2]
        self.calls.append(action)
        assert command[3] == SID_A and kwargs['timeout'] > 0
        if kwargs['cancel_event'] is not None and kwargs['cancel_event'].is_set():
            raise experiment._Cancelled()
        if action == 'settings':
            self.model, self.effort = command[command.index('--model') + 1], command[command.index('--effort') + 1]
        elif action == 'message':
            assert '--text-file' in command
            text = Path(command[command.index('--text-file') + 1]).read_text(encoding='utf-8')
            self.connected = self.active = True
            self.messages.append({'role': 'user', 'text': text})
        elif action == 'status' and self.active:
            self.polls += 1
            if self.cancellation is not None:
                self.cancellation.set()
            elif self.polls >= 2:
                self.active = False
                if self.terminal != 'missing':
                    self.last_turn = {'turn_id': 'turn-1', 'status': self.terminal}
                if self.terminal == 'completed':
                    self.messages.append({'role': 'assistant', 'text': self.reply})
                    if self.mutate:
                        self.session['revision'] += 1
                        self.session['plan'] = {'engine': 'product-agent', 'steps': [{'title': 'Unexpected write'}]}
        elif action == 'disconnect':
            self.connected = self.active = False
        elif action == 'interrupt':
            self.active = False
        return subprocess.CompletedProcess(command, 0, json.dumps(self.state()), 'PRIVATE_STDERR_MUST_NOT_LEAK')

    def read(self, url, *, timeout):
        assert timeout > 0
        return copy.deepcopy(self.state() if urlsplit(url).path.endswith('/agent') else self.session)


def arguments(destination, *, timeout=2, condition='existing_navigation'):
    return SimpleNamespace(destination=destination, condition=condition, case='case-09', session_id=SID_A,
        url='http://127.0.0.1:12345', model='gpt-6-luna', timeout=timeout, poll_interval=0.01)


def test_natural_completed_route_needs_no_saved_plan_and_submits_one_cli_message(tmp_path):
    product = Product()
    message = experiment.route_message([CASE_ID])
    run = experiment.run_turn(arguments(tmp_path), message, runner=product.runner, http_reader=product.read)
    assert run['status'] == 'complete' and run['natural_termination'] and run['visible_json_valid']
    assert run['saved_plan'] is False and run['save_plan_required'] is False and run['authored_state_unchanged']
    assert run['message_count'] == run['agent_execution_count'] == 1 and run['automatic_retry_count'] == 0
    assert product.calls.count('message') == 1 and product.calls.count('status') >= 3
    assert product.calls[-1] == 'disconnect' and 'interrupt' not in product.calls
    assert run['runtime_warning_observed'] and run['reported_model'] == 'gpt-6-luna'
    assert json.loads((tmp_path / 'visible-final.json').read_text(encoding='utf-8')) == json.loads(product.reply)
    assert (tmp_path / 'user-message.txt').read_text(encoding='utf-8') == message
    assert 'PRIVATE_STDERR_MUST_NOT_LEAK' not in (tmp_path / 'run.json').read_text(encoding='utf-8')


@pytest.mark.parametrize('terminal', ['failed', 'interrupted'])
def test_natural_terminal_failures_are_retained_without_retry_or_preemption(tmp_path, terminal):
    product = Product(terminal=terminal)
    run = experiment.run_turn(arguments(tmp_path), 'Route choice only', runner=product.runner, http_reader=product.read)
    assert run['status'] == 'incomplete' and run['failure_code'] == 'agent_turn_' + terminal
    assert run['natural_termination'] and product.calls.count('message') == 1
    assert 'interrupt' not in product.calls and product.calls[-1] == 'disconnect'
    assert (tmp_path / 'agent-final.json').exists()


def test_idle_without_reported_turn_outcome_waits_then_interrupts_owned_agent(tmp_path):
    product = Product(terminal='missing')
    run = experiment.run_turn(arguments(tmp_path, timeout=0.15), 'Route choice only', runner=product.runner, http_reader=product.read)
    assert run['status'] == 'timed_out' and not run['natural_termination']
    assert product.calls.count('message') == 1 and product.calls[-2:] == ['interrupt', 'disconnect']


def test_stop_request_event_cancels_owned_cli_turn_and_collects_partial_chat(tmp_path):
    stop = threading.Event()
    product = Product(cancellation=stop)
    run = experiment.run_turn(arguments(tmp_path), 'Route choice only', cancel_event=stop,
                              runner=product.runner, http_reader=product.read)
    assert run['status'] == 'cancelled' and product.calls.count('message') == 1
    assert product.calls[-2:] == ['interrupt', 'disconnect'] and (tmp_path / 'chat.json').exists()


def test_preexisting_conversation_is_never_disconnected_or_reused(tmp_path):
    product = Product()
    product.connected = True
    product.messages.append({'role': 'assistant', 'text': 'Previous run'})
    run = experiment.run_turn(arguments(tmp_path), 'Route choice only', runner=product.runner, http_reader=product.read)
    assert run['failure_code'] == 'task_not_fresh' and run['message_count'] == 0
    assert product.calls == ['status'] and product.connected


def test_invalid_json_keeps_raw_visible_reply_and_has_explicit_output_failure(tmp_path):
    product = Product(reply='A route decision that violated the JSON request.')
    run = experiment.run_turn(arguments(tmp_path), 'Route choice only', runner=product.runner, http_reader=product.read)
    assert run['natural_termination'] and run['status'] == 'incomplete'
    assert run['failure_code'] == 'visible_json_invalid'
    assert (tmp_path / 'visible-final.txt').read_text(encoding='utf-8') == product.reply


def test_read_only_state_change_is_a_failure_even_with_valid_visible_json(tmp_path):
    product = Product(mutate=True)
    run = experiment.run_turn(arguments(tmp_path), 'Route choice only', runner=product.runner, http_reader=product.read)
    assert run['status'] == 'incomplete' and run['failure_code'] == 'route_modified_plan'
    assert run['authored_state_unchanged'] is False


def test_existing_attempt_directory_cannot_be_overwritten(tmp_path):
    (tmp_path / 'run.json').write_text('historical result', encoding='utf-8')
    with pytest.raises(ValueError, match='empty'):
        experiment.run_turn(arguments(tmp_path), 'Route choice only')
    assert (tmp_path / 'run.json').read_text(encoding='utf-8') == 'historical result'
