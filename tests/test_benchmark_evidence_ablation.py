"""Frozen evidence and CLI lifecycle checks, with no model or private inputs."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from scripts import benchmark_evidence_ablation as experiment


SID = 'a' * 32


def pack_value(text='Public allowed manual text. ' * 25):
    item = {'source_id': 'src-allowed', 'library_id': 'lib-manual', 'title': 'Allowed manual',
            'offset': 17, 'length': max(500, len(text)), 'text': text, 'sha256': experiment.digest(text)}
    item['evidence_refs'] = [{'source_id': item['source_id'], 'context': 'source_text',
                              'locator': f"text:{item['offset']}:{item['offset'] + len(text)}"}]
    return {'schema': experiment.PACK_SCHEMA, 'items': [item], 'text': experiment.evidence_text([item])}


def save_pack(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return path


def test_pack_is_bounded_exact_verbatim_and_messages_fit_product_limit(tmp_path):
    value = pack_value()
    path = save_pack(tmp_path / 'pack.json', value)
    loaded, sha = experiment.load_pack(path)
    assert loaded == value and sha == experiment.digest(path.read_bytes())
    assert len(experiment.CURATED_PREFIX + loaded['text']) <= 12000
    assert len(experiment.REVIEW_PREFIX + loaded['text']) <= 12000
    assert len(experiment.REVISION_PREFIX) + experiment.MAX_REVIEW_CHARS <= 12000


@pytest.mark.parametrize('mutation', ['hash', 'compiled', 'receipt', 'short-read', 'oversized', 'duplicate'])
def test_pack_rejects_changed_prose_hash_receipts_and_bounds(tmp_path, mutation):
    value = pack_value()
    if mutation == 'hash':
        value['items'][0]['sha256'] = '0' * 64
    elif mutation == 'compiled':
        value['text'] += '\nHidden answer payload outside the allowed source pages.'
    elif mutation == 'receipt':
        value['items'][0]['evidence_refs'][0]['locator'] = 'text:0:999999'
    elif mutation == 'short-read':
        value['items'][0]['length'] = 499
    elif mutation == 'duplicate':
        value['items'] *= 2
        value['text'] = experiment.evidence_text(value['items'])
    else:
        value = pack_value('x' * 9000)
    with pytest.raises(ValueError):
        experiment.load_pack(save_pack(tmp_path / 'pack.json', value))


def test_preload_requires_real_identical_product_reads_and_receipts():
    pack = pack_value()
    item = pack['items'][0]
    calls = []

    def call(sid, name, arguments):
        calls.append((sid, name, copy.deepcopy(arguments)))
        if name == 'set_libraries_enabled':
            return {'success': True}
        return {'id': item['source_id'], 'library_id': item['library_id'], 'title': item['title'],
                'offset': item['offset'], 'text': item['text'], 'evidence_refs': item['evidence_refs']}

    app = SimpleNamespace(tools=SimpleNamespace(call=call))
    receipts = experiment.preload_pack(app, SID, pack)
    assert [row[1] for row in calls] == ['set_libraries_enabled', 'read_source']
    assert calls[-1][2] == {'id': item['source_id'], 'offset': item['offset'], 'length': item['length']}
    assert receipts[0]['sha256'] == item['sha256']
    changed = copy.deepcopy(pack)
    changed['items'][0]['text'] += 'changed'
    with pytest.raises(ValueError, match='live allowed'):
        experiment.preload_pack(app, SID, changed)


def test_reviewer_guard_keeps_schema_and_rejects_all_candidate_writes(tmp_path):
    forwarded = []

    def original(sid, name, arguments, **kwargs):
        forwarded.append((sid, name))
        return {'success': True}

    app = SimpleNamespace(agents=SimpleNamespace(runtime_tool=original))
    audit = tmp_path / 'blocks.jsonl'
    experiment.readonly_reviewer(app, {SID}, audit)
    for tool in ('save_plan', 'update_steps', 'save_plan_settings', 'repair_plan_draft', 'unknown_future_write'):
        assert app.agents.runtime_tool(SID, tool, {'secret_payload': 'must-not-appear'})['success'] is False
    assert app.agents.runtime_tool(SID, 'set_libraries_enabled', {'enabled': False})['success'] is False
    for tool in ('get_task_context', 'search_knowledge', 'read_source', 'set_libraries_enabled', 'get_mechanism_checks'):
        assert app.agents.runtime_tool(SID, tool, {'enabled': True} if tool == 'set_libraries_enabled' else {})['success'] is True
    assert app.agents.runtime_tool('b' * 32, 'save_plan', {})['success'] is True
    assert len(forwarded) == 6
    rows = [json.loads(line) for line in audit.read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 6 and all(row['reviewer_write_blocked'] for row in rows)
    assert 'must-not-appear' not in audit.read_text(encoding='utf-8')


class Product:
    def __init__(self, terminal='completed', *, save=True):
        self.terminal, self.save = terminal, save
        self.connected = self.active = False
        self.messages, self.calls = [], []
        self.turn_count, self.polls = 0, 0
        self.last_turn, self.model, self.effort = None, 'default', 'medium'
        self.session = {'id': SID, 'revision': 1, 'goal': 'Unmodified public goal', 'model': {'facts': []},
                        'plan': {'engine': 'intake-only', 'steps': [{'title': 'Placeholder'}]}, 'history': []}

    def state(self):
        return {'status': 'running' if self.active else 'idle' if self.connected else 'disconnected',
                'connected': self.connected, 'alive': self.connected, 'thread_id': 'paired-thread' if self.connected else None,
                'active_turn_id': 'turn-' + str(self.turn_count) if self.active else None,
                'turn_outcome_supported': True, 'last_turn': self.last_turn, 'messages': copy.deepcopy(self.messages),
                'model': self.model, 'reasoning_effort': self.effort, 'managed_backend': 'codex',
                'connection_mode': 'managed', 'backend': 'codex-app-server',
                'warning': 'Native transient warning' if self.active else None,
                'error_metadata': {'will_retry': True, 'code': 'native-transient'} if self.active else None}

    def runner(self, command, **kwargs):
        action = command[2]
        self.calls.append(action)
        assert command[3] == SID and kwargs['timeout'] > 0
        if kwargs['cancel_event'] is not None and kwargs['cancel_event'].is_set():
            raise experiment._Cancelled()
        if action == 'settings':
            self.model, self.effort = command[command.index('--model') + 1], command[command.index('--effort') + 1]
        elif action == 'message':
            assert '--text-file' in command and experiment.MESSAGE not in command
            text = Path(command[command.index('--text-file') + 1]).read_text(encoding='utf-8')
            self.connected = self.active = True
            self.turn_count += 1
            self.polls = 0
            self.messages.append({'role': 'user', 'text': text})
        elif action == 'status' and self.active:
            self.polls += 1
            if self.polls >= 2:
                self.active = False
                if self.terminal != 'missing':
                    self.last_turn = {'turn_id': 'turn-' + str(self.turn_count), 'status': self.terminal}
                if self.terminal == 'completed':
                    self.messages.append({'role': 'assistant', 'text': '{"findings":[],"unresolved":[]}'})
                    if self.save:
                        self.session['revision'] += 1
                        self.session['plan'] = {'engine': 'product-agent', 'steps': [{'title': 'Revision ' + str(self.turn_count)}]}
                        self.session['history'].append({'event': 'agent_plan_updated', 'revision': self.session['revision']})
        elif action == 'disconnect':
            self.connected = self.active = False
        elif action == 'interrupt':
            self.active = False
        return subprocess.CompletedProcess(command, 0, json.dumps(self.state()), 'PRIVATE_STDERR_MUST_NOT_LEAK')

    def read(self, url, *, timeout):
        path = urlsplit(url).path
        if path.endswith('/agent'):
            return copy.deepcopy(self.state())
        if path.endswith('/export'):
            return '# Exact product export\r\nSaved plan.  \r\n'
        return copy.deepcopy(self.session)


def arguments(destination, condition='autonomous', **kwargs):
    return SimpleNamespace(destination=destination, condition=condition, case='case-09', session_id=SID,
                           url='http://127.0.0.1:12345', timeout=kwargs.pop('timeout', 2), poll_interval=0.01, **kwargs)


def test_natural_completion_waits_through_native_warning_and_never_retries(tmp_path):
    product = Product()
    output = tmp_path / 'autonomous-case-09'
    run = experiment.run_turn(arguments(output), experiment.MESSAGE, runner=product.runner, http_reader=product.read)
    assert run['status'] == 'complete' and run['saved_plan'] and run['runtime_warning_observed']
    assert run['message_count'] == run['agent_execution_count'] == 1 and run['automatic_retry_count'] == 0
    assert product.calls.count('message') == 1 and product.calls.count('status') >= 3
    assert 'interrupt' not in product.calls and product.calls[-1] == 'disconnect'
    assert run['arm'] == 'autonomous' and run['retrieval_arm'] == 'rag'
    assert (output / 'plan.md').read_bytes() == b'# Exact product export\r\nSaved plan.  \r\n'
    assert 'PRIVATE_STDERR_MUST_NOT_LEAK' not in (output / 'run.json').read_text(encoding='utf-8')
    assert not run['private_reference_supplied'] and not run['evaluation_performed']


@pytest.mark.parametrize('terminal', ['failed', 'interrupted'])
def test_failed_natural_turn_is_recorded_once_without_replacement(tmp_path, terminal):
    product = Product(terminal=terminal)
    run = experiment.run_turn(arguments(tmp_path / 'run'), experiment.MESSAGE, runner=product.runner, http_reader=product.read)
    assert run['status'] == 'incomplete' and run['failure_code'] == 'agent_turn_' + terminal
    assert product.calls.count('message') == 1 and 'interrupt' not in product.calls


def test_native_idle_without_outcome_waits_until_failsafe_and_cleans_up(tmp_path):
    product = Product(terminal='missing')
    run = experiment.run_turn(arguments(tmp_path / 'run', timeout=0.12), experiment.MESSAGE,
                              runner=product.runner, http_reader=product.read)
    assert run['status'] == 'timed_out'
    assert product.calls.count('message') == 1 and product.calls[-2:] == ['interrupt', 'disconnect']


def test_reviewed_condition_resumes_exact_curated_snapshot_and_retains_b_artifacts(tmp_path):
    product = Product()
    curated = tmp_path / 'curated-case-09'
    b = experiment.run_turn(arguments(curated, 'curated'), experiment.MESSAGE, keep_connected=True,
                            runner=product.runner, http_reader=product.read)
    frozen = {path.name: path.read_bytes() for path in curated.iterdir()}
    assert b['status'] == 'complete' and product.connected and 'disconnect' not in product.calls
    c_args = arguments(tmp_path / 'reviewed-case-09', 'reviewed', expected_thread_id=b['conversation_id'],
                       expected_session_sha256=b['final_session_sha256'])
    c = experiment.run_turn(c_args, experiment.REVISION_PREFIX + '{"findings":[]}', fresh=False,
                            runner=product.runner, http_reader=product.read)
    assert c['status'] == 'complete' and c['initial_revision'] == b['final_revision']
    assert c['conversation_id'] == b['conversation_id'] and c['paired_continuation_of'] == 'curated'
    assert product.calls.count('settings') == 1 and product.calls.count('message') == 2
    assert frozen == {path.name: path.read_bytes() for path in curated.iterdir()}
    assert json.loads((c_args.destination / 'session-initial.json').read_text(encoding='utf-8'))['revision'] == 2


def test_changed_curated_snapshot_prevents_second_message_without_preempting_thread(tmp_path):
    product = Product()
    b = experiment.run_turn(arguments(tmp_path / 'curated', 'curated'), experiment.MESSAGE, keep_connected=True,
                            runner=product.runner, http_reader=product.read)
    product.session['goal'] = 'Unexpected mutation'
    c_args = arguments(tmp_path / 'reviewed', 'reviewed', expected_thread_id=b['conversation_id'],
                       expected_session_sha256=b['final_session_sha256'])
    c = experiment.run_turn(c_args, 'Review feedback', fresh=False, runner=product.runner, http_reader=product.read)
    assert c['failure_code'] == 'continuation_snapshot_mismatch' and c['message_count'] == 0
    assert product.calls.count('message') == 1 and product.connected


@pytest.mark.parametrize('change', ['availability', 'plan', 'source_id', 'source_provenance'])
def test_only_live_source_availability_is_ignored_by_paired_guards(tmp_path, change):
    product = Product()
    product.session['sources'] = [{'id': 'allowed-source', 'text': 'Authored source body retained',
                                   'provenance': {'revision_id': 'immutable-revision'}}]
    availability = {'readable': True}

    def read(url, *, timeout):
        result = product.read(url, timeout=timeout)
        if isinstance(result, dict) and isinstance(result.get('sources'), list):
            for source in result['sources']:
                source['availability'] = copy.deepcopy(availability)
        return result

    app = SimpleNamespace(sessions=SimpleNamespace(get=lambda sid: copy.deepcopy(product.session)))
    b = experiment.run_turn(arguments(tmp_path / 'curated', 'curated'), experiment.MESSAGE, keep_connected=True,
                            runner=product.runner, http_reader=read)
    raw_hash = experiment.digest(experiment._canonical(experiment.candidate_fields(product.session)))
    if change == 'availability':
        availability.update(readable=False, state='unavailable')
        assert experiment.require_candidate_unchanged(app, SID, raw_hash) == raw_hash
    elif change == 'plan':
        product.session['plan']['steps'][0]['title'] = 'Unexpected authored edit'
    elif change == 'source_id':
        product.session['sources'][0]['id'] = 'different-source'
    else:
        product.session['sources'][0]['provenance']['revision_id'] = 'different-revision'
    if change != 'availability':
        with pytest.raises(experiment.ProductRunError, match='Raw curated candidate changed'):
            experiment.require_candidate_unchanged(app, SID, raw_hash)
    c_args = arguments(tmp_path / 'reviewed', 'reviewed', expected_thread_id=b['conversation_id'],
                       expected_session_sha256=b['final_continuation_snapshot_sha256'])
    c = experiment.run_turn(c_args, 'Independent review feedback', fresh=False,
                            runner=product.runner, http_reader=read)
    if change == 'availability':
        assert c['status'] == 'complete' and product.calls.count('message') == 2
    else:
        assert c['failure_code'] == 'continuation_snapshot_mismatch' and c['message_count'] == 0
        assert product.calls.count('message') == 1


def test_transport_normalization_removes_only_top_level_source_availability():
    original = {'availability': {'task': 'preserved'}, 'sources': [{
        'id': 'exact-source-id', 'text': 'complete source content', 'availability': {'readable': False},
        'provenance': {'availability': 'not a transport field', 'revision_id': 'exact-revision'}}]}
    expected = copy.deepcopy(original)
    expected['sources'][0].pop('availability')
    assert experiment.continuation_snapshot(original) == expected
    assert original['sources'][0]['availability'] == {'readable': False}


def test_reviewer_collects_only_last_visible_reply_without_saving(tmp_path):
    product = Product(save=False)
    output = tmp_path / 'reviewer-case-09'
    run = experiment.run_turn(arguments(output, 'reviewer'), 'Read-only review', reviewer=True,
                              seeded_candidate=True, runner=product.runner, http_reader=product.read)
    assert run['status'] == 'complete' and run['reviewer_read_only'] and not run['saved_plan']
    assert run['arm'] == 'technical-review' and not (output / 'saved-plan.json').exists()
    assert (output / 'review.txt').read_text(encoding='utf-8') == '{"findings":[],"unresolved":[]}'
    assert experiment.final_visible_message([{'role': 'reasoning', 'text': 'hidden'},
        {'role': 'assistant', 'text': 'earlier'}, {'role': 'assistant', 'text': 'final'}]) == 'final'
    with pytest.raises(experiment.ProductRunError):
        experiment.final_visible_message([{'role': 'assistant', 'text': 'x' * 11}], maximum=10)


def test_fresh_condition_rejects_existing_thread_without_any_mutation(tmp_path):
    product = Product()
    product.connected = True
    run = experiment.run_turn(arguments(tmp_path / 'run'), experiment.MESSAGE,
                              runner=product.runner, http_reader=product.read)
    assert run['failure_code'] == 'task_not_fresh'
    assert product.calls == ['status']


@pytest.fixture
def short_session_root():
    # SessionStore's UUID archive filenames exceed classic Windows MAX_PATH
    # under the app's deeply nested default temporary directory.
    base = experiment.APP / '.test-temp'
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ablation-seed-', dir=base) as folder:
        yield Path(folder)


def test_reviewer_seed_uses_real_session_store_and_preserves_public_model_questions_and_candidate(short_session_root):
    from advisor.sessions import SessionStore

    store = SessionStore(short_session_root, SimpleNamespace(), None)
    model = {'id': 'actual-upload-id', 'fingerprint': 'same-geometry-fingerprint',
             'file_name': 'geometry_only.mph', 'facts': [{'key': 'observed.volume', 'value': 1}]}
    b = store.create('Public goal with observed geometry.', model)
    r = store.create('Public goal with observed geometry.', model)
    app = SimpleNamespace(sessions=store)
    original_r = copy.deepcopy(r)
    q = {'id': 'required_data', 'kind': 'text', 'label': 'Provide measured data', 'required': True}
    brief = {'decisions': [{'topic': 'Study regime', 'recommendation': 'Time dependent',
                            'alternatives': ['Stationary'], 'reason': 'Public transient requirement',
                            'requires_confirmation': True}]}
    plan = copy.deepcopy(b['plan'])
    plan['engine'], plan['title'] = 'product-agent', 'Authored B candidate'
    bundle = {'plan': plan, 'plan_settings': [], 'questions': [q], 'design_brief': brief,
              'sources': [{'id': 'allowed-source', 'title': 'Allowed source'}],
              'geometry_reference_catalog': {'fact:observed.volume': {'value': 1}},
              'evidence_gaps': ['Unmeasured material parameter'], 'planning_incomplete': False,
              'research_trace': [{'backend': 'codex-app-server', 'model': experiment.MODEL}]}
    candidate = store.apply_agent_bundle(b['id'], b['revision'], bundle)
    # A prior intake question must retire cleanly when absent from B's saved draft.
    store.apply_agent_bundle(r['id'], r['revision'], {'questions': [dict(q, id='old_intake_question')],
                                                    'plan': original_r['plan']})
    receipt = experiment.seed_reviewer(app, r['id'], candidate)
    seeded = store.get(r['id'])
    assert seeded['goal'] == original_r['goal'] and seeded['model'] == original_r['model']
    assert experiment.candidate_fields(seeded) == experiment.candidate_fields(candidate)
    assert len(seeded['questions']) == 2 and seeded['questions'][-1]['id'].startswith('decision_')
    assert receipt['candidate_sha256'] == experiment.digest(experiment._canonical(experiment.candidate_fields(candidate)))
    assert store.get(b['id']) == candidate


def test_experiment_does_not_import_private_reference_loader_or_grade_candidates():
    source = Path(experiment.__file__).read_text(encoding='utf-8')
    assert 'core.validate_case' not in source and 'from scripts.benchmark_cli import core' not in source
    assert "root / 'private/cases'" not in source and 'reference.json' not in source
    assert len(experiment.CORE_PATHS) == 20
