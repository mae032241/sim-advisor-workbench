"""Small paired route-selection pilot through the actual product Agent CLI.

The production instructions and every existing business-tool schema are retained.
An experiment-owned runtime wrapper adds one method-navigation tool. Its schema,
the public inputs, candidate list, user message and library scope are identical
in both conditions; only availability of frozen non-test method profiles differs.
This runner never loads private case references, grades answers, repairs plans,
or writes production indexes. A naturally completed visible JSON reply is the
result; saving a plan is neither requested nor required.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
from types import SimpleNamespace
from urllib.parse import urlsplit

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from scripts import benchmark_product_batch as batch
from scripts.benchmark_backend_policy import install_backend_policy, policy_description
from scripts.benchmark_product_cli import (
    ProductRunError, _Cancelled, _canonical, _http_read, _run_process, product_contract,
)
from scripts.benchmark_product_setup import create_task, public_goal

MODEL, EFFORT = 'gpt-6-luna', 'high'
CASES = ('case-01', 'case-02', 'case-09')
CONDITIONS = ('existing_navigation', 'case_methods')
CANDIDATE_SCHEMA = 'case-method-candidates/v1'
RUN_ID = 'luna6-high-route-selection-pilot-20261113'
READ_TOOLS = frozenset({
    'get_plan', 'get_task_context', 'get_plan_settings', 'get_library_scope',
    'search_knowledge', 'read_source', 'search_cases', 'get_case_guide',
    'read_case_configuration', 'get_mechanism_checks', 'review_plan',
    'get_plan_draft', 'get_tool_contract', 'get_case_method',
})
CORE_PATHS = (
    'agent_cli.py', 'tools_cli.py', 'advisor/agent_sessions.py',
    'advisor/codex_runtime.py', 'advisor/runtime_adapters.py',
    'advisor/planning_service.py', 'advisor/research_policy.py',
    'advisor/tool_contracts.py', 'advisor/case_methods.py',
    'advisor/case_guides.py', 'advisor/case_navigation.py',
    'advisor/library_store.py', 'prompts/comsol_planner.v1.txt',
    'scripts/benchmark_product_setup.py', 'scripts/benchmark_product_cli.py',
    'scripts/benchmark_product_batch.py', 'scripts/benchmark_route_selection.py',
    'scripts/benchmark_backend_policy.py',
)
ROUTE_MESSAGE = '''请先用 get_task_context 阅读当前任务的完整公开需求、输入数据与已导入几何/模型事实。本次仅判断 COMSOL 建模路线与适用条件：提出首选路线，说明物理接口、主要耦合关系和关键简化；比较合理替代路线，解释采用或排除的条件，以及哪些未知事实可能改变选择。不要制定完整实施方案，也不要保存或修改方案、步骤或设置表。

可使用当前全部读取、检索与导航工具，包括 get_case_method；方法说明和案例导航只帮助判断适用性，不能代替正文或配置证据，不证明当前任务的物理正确性。以下是两组共同提供的非测试案例候选精确 ID，可按需查阅；它们不保证适用，也不是必须采用的路线。仍可在共同已启用资料库范围内检索其他资料，保留证据缺口。当前资料库范围已固定，不能关闭或新增启用资料库。

候选案例 ID：
{candidates}

最终可见回复只返回一个紧凑 JSON 对象，包含 chosen_route、alternatives、applicability_conditions、unresolved、sources。理由基于当前任务事实及实际读取的证据；不把参考案例参数当作当前任务事实，不声称已经运行求解。无需调用 save_plan。'''


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def write(path, value):
    batch.write(path, value)


def load_candidates(path):
    path = Path(path)
    if path.stat().st_size > 100000:
        raise ValueError('Candidate list exceeds its byte bound')
    value = json.loads(path.read_bytes().decode('utf-8'))
    if not isinstance(value, dict) or value.get('schema') != CANDIDATE_SCHEMA:
        raise ValueError('Unsupported candidate-list schema')
    ids = value.get('case_ids')
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 32
            or any(not isinstance(item, str) or not item.strip() or len(item) > 256
                   or any(char in item for char in ('/', '\\', ':', '\r', '\n')) for item in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('Candidate case_ids must be 1..32 unique exact citation IDs')
    # Do not accept a prose/answer payload hidden alongside the common IDs.
    if set(value) != {'schema', 'case_ids'}:
        raise ValueError('Candidate list contains fields other than schema and case_ids')
    return value, digest(path.read_bytes())


def route_message(candidate_ids):
    message = ROUTE_MESSAGE.format(candidates=json.dumps(candidate_ids, ensure_ascii=False))
    if len(message) > 12000:
        raise ValueError('Common route-selection message exceeds the product limit')
    return message


def profile_files(root):
    """Hash the frozen method store, without accepting filesystem aliases."""
    root = Path(root).resolve()
    directory = root / 'case-methods'
    if not directory.is_dir() or directory.is_symlink() or getattr(directory, 'is_junction', lambda: False)():
        raise ValueError('Expected an owned case-methods directory')
    paths = []
    for path in directory.rglob('*'):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('Method profiles cannot be filesystem aliases')
        if path.is_file():
            if path.suffix.lower() != '.json' or not path.resolve().is_relative_to(root):
                raise ValueError('Method profile store must contain regular JSON files only')
            paths.append(path)
    if not paths or len(paths) > 100 or sum(path.stat().st_size for path in paths) > 32 * 1024 * 1024:
        raise ValueError('Method profile store is empty or exceeds the pilot bound')
    return {path.relative_to(root).as_posix(): batch.sha(path) for path in sorted(paths)}


class ExtendedRuntime:
    """Append schemas at the runtime seam; keep production instructions intact."""
    def __init__(self, runtime, extra_tools):
        self.runtime, self.extra_tools = runtime, copy.deepcopy(extra_tools)

    def __getattr__(self, name):
        return getattr(self.runtime, name)

    def start(self, thread_id=None, tools=None, instructions=''):
        existing = copy.deepcopy(tools or [])
        names = {item['name'] for item in existing}
        if names.intersection(item['name'] for item in self.extra_tools):
            raise ValueError('Experiment tool duplicates an existing production tool')
        return self.runtime.start(thread_id=thread_id, tools=existing + copy.deepcopy(self.extra_tools),
                                  instructions=instructions)


def _feedback(code, message, **extra):
    return {'success': False, 'contentItems': [{'type': 'inputText', 'text': json.dumps({
        'error_code': code, 'error': message, 'saved': False,
        'next_action': '继续读取与比较适用条件；最终返回路线选择 JSON，无需保存方案。',
        **extra}, ensure_ascii=False)}]}


def install_route_tools(app, method_store, extra_tools, session_conditions, candidate_ids,
                        enabled_library_ids, audit_paths):
    """Read-only runtime dispatcher, including a contract from the exact schema.

    Session identity and lifecycle validation precede the experiment tool. The
    method store performs live production-provider library/holdout checks.
    Existing read calls use the original dispatcher and reading ledger.
    """
    from advisor.agent_sessions import planning_tools
    from advisor.plan_drafts import schema_errors
    from advisor.tool_contracts import read_contract

    definitions = planning_tools() + copy.deepcopy(extra_tools)
    by_name = {item['name']: item for item in definitions}
    if len(by_name) != len(definitions) or set(item['name'] for item in extra_tools) != {'get_case_method'}:
        raise ValueError('Expected exactly one new get_case_method schema')
    original = app.agents.runtime_tool
    allowed_scope, candidates = frozenset(enabled_library_ids), frozenset(candidate_ids)
    audit_lock = threading.Lock()

    def guarded(sid, name, arguments, *, source_entry=None):
        started = time.monotonic()
        row = {'recorded_at': now(), 'session_id': sid, 'condition': session_conditions.get(sid), 'tool': name}
        if isinstance(arguments, dict):
            row['arguments'] = {key: value for key, value in arguments.items() if key in {
                'id', 'case_id', 'query', 'kind', 'mode', 'library_ids', 'enabled',
                'stage', 'category', 'node_path', 'source', 'offset', 'length', 'tool', 'path', 'limit',
            }}
        result = None
        try:
            if sid not in session_conditions:
                raise PermissionError('Task is not owned by this experiment')
            app.sessions.get(sid)
            if source_entry is not None:
                app.agents._require_current_entry(sid, source_entry)
            if name == 'set_libraries_enabled':
                errors = schema_errors(arguments, by_name[name]['inputSchema'])
                if errors:
                    result = _feedback('tool_arguments_invalid', '资料库工具参数不符合契约。', errors=errors)
                elif arguments['enabled'] is True and set(arguments['library_ids']).issubset(allowed_scope):
                    # No global write is needed for an already-enabled scope.
                    result = {'library_ids': list(dict.fromkeys(arguments['library_ids'])),
                              'enabled': True, 'changed': 0, 'index_rebuilt': False,
                              'scope_frozen': True, 'enabled_library_ids': sorted(allowed_scope)}
                else:
                    row['scope_write_blocked'] = True
                    result = _feedback('experiment_scope_frozen', '两组共同资料库范围已冻结，本次未改变任何库开关。',
                                       enabled_library_ids=sorted(allowed_scope))
            elif name not in READ_TOOLS:
                row['route_write_blocked'] = True
                result = _feedback('route_selection_read_only', '本次只做路线选择，未保存或修改方案、步骤或设置。')
            elif name == 'get_tool_contract':
                result = read_contract(definitions, arguments)
            elif name == 'get_case_method':
                errors = schema_errors(arguments, by_name[name]['inputSchema'])
                if errors:
                    result = _feedback('tool_arguments_invalid', '方法说明工具参数不符合契约。', errors=errors)
                else:
                    case_id = arguments['case_id']
                    result = method_store.get_case_method(case_id, enabled=(
                        session_conditions[sid] == 'case_methods' and case_id in candidates))
            else:
                result = original(sid, name, arguments, source_entry=source_entry)
            row['success'] = not isinstance(result, dict) or result.get('success') is not False
            if isinstance(result, dict):
                row['result_keys'] = sorted(result)
                row['result_state'] = result.get('state')
                row['result_count'] = len(result['results']) if isinstance(result.get('results'), list) else None
                row['result_ids'] = [item.get('id') for item in result.get('results', []) if isinstance(item, dict)]
                row['evidence_refs'] = result.get('evidence_refs')
            serialized = _canonical(result)
            row.update(result_sha256=digest(serialized), result_json_bytes=len(serialized.encode('utf-8')))
            return result
        except (ValueError, FileNotFoundError, PermissionError) as exc:
            row.update(success=False, error_type=type(exc).__name__)
            details = getattr(exc, 'to_dict', None)
            if callable(details):
                return {'success': False, 'contentItems': [{'type': 'inputText', 'text': json.dumps(details(), ensure_ascii=False)}]}
            return _feedback('route_tool_rejected', str(exc)[:500])
        except BaseException as exc:
            row.update(success=False, error_type=type(exc).__name__)
            raise
        finally:
            row['seconds'] = round(time.monotonic() - started, 4)
            path = audit_paths.get(sid)
            if path:
                with audit_lock:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(row, ensure_ascii=False) + '\n')

    app.agents.runtime_tool = guarded
    return definitions


def final_visible_json(messages, *, after=0):
    visible = [item['text'] for item in messages[after:] if isinstance(item, dict)
               and item.get('role') == 'assistant' and isinstance(item.get('text'), str) and item['text'].strip()]
    if not visible:
        raise ProductRunError('visible_reply_missing', 'No final visible route-selection reply was returned')
    text = visible[-1]
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ProductRunError('visible_json_invalid', 'Final visible route-selection reply is not JSON') from exc
    if not isinstance(value, dict):
        raise ProductRunError('visible_json_invalid', 'Final visible route-selection JSON must be an object')
    return text, value


def _authored_snapshot(session):
    return {key: session.get(key) for key in (
        'revision', 'plan', 'plan_settings', 'design_brief', 'questions', 'answers',
    )}


def run_turn(args, message, *, cancel_event=None, runner=None, http_reader=None, allowed_conditions=None):
    """One actual CLI message, natural completion, collection and owned cleanup."""
    parsed = urlsplit(args.url)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.path not in {'', '/'} or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('A loopback product service is required')
    if not re.fullmatch(r'[a-f0-9]{32}', args.session_id) or args.timeout <= 0:
        raise ValueError('A prepared task and positive timeout are required')
    accepted_conditions = CONDITIONS if allowed_conditions is None else tuple(allowed_conditions)
    if (not accepted_conditions or any(not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', item)
                                      for item in accepted_conditions)
            or args.condition not in accepted_conditions or not isinstance(message, str)
            or not 1 <= len(message.strip()) <= 12000):
        raise ValueError('A known condition and bounded common message are required')
    destination = Path(args.destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Turn output directory must be empty')
    destination.mkdir(parents=True, exist_ok=True)
    message_file = destination / 'user-message.txt'
    message_file.write_text(message, encoding='utf-8', newline='')
    runner, read = runner or _run_process, http_reader or _http_read
    started, initial, message_attempted, terminal, phase = time.monotonic(), None, False, False, 'preflight'
    initial_chat_count = 0
    deadline = started + args.timeout
    model = getattr(args, 'model', MODEL)
    report = {'schema': 'route-selection-run/v1', 'engine': 'product-agent-cli', 'case_id': args.case,
        'condition': args.condition, 'arm': args.condition, 'retrieval_arm': 'rag', 'status': 'running',
        'session_id': args.session_id, 'workbench_url': args.url, 'requested_model': model,
        'requested_effort': EFFORT, 'timeout_seconds': args.timeout,
        'message_sha256': digest(message), 'message_chars': len(message), 'message_count': 0,
        'agent_execution_count': 0, 'model_call_count': None, 'automatic_retry_count': 0, 'retry_count': 0,
        'evaluation_performed': False, 'scoring_run': False, 'private_reference_supplied': False,
        'execution_feedback_supplied': False, 'physical_feedback_supplied': False,
        'solver_run': False, 'saved_plan': False, 'save_plan_required': False,
        'route_selection_read_only': True, 'natural_termination': False,
        'tool_audit_path': str(getattr(args, 'audit_path', '') or '') or None,
        'cli_calls': [], 'cleanup_errors': [], 'started_at': now()}
    if getattr(args, 'task_mode', 'route') == 'implementation':
        report.update(schema='implementation-planning-run/v1', task_mode='implementation',
                      route_selection_read_only=False, implementation_planning_read_only=True)

    def checkpoint():
        report['seconds'] = round(time.monotonic() - started, 3)
        try:
            write(destination / 'run.json', report)
        except OSError as error:
            # A progress observer/antivirus can momentarily lock run.json on
            # Windows. Do not interrupt a healthy turn over a checkpoint.
            warnings = report.setdefault('checkpoint_warnings', [])
            if len(warnings) < 8:
                warnings.append({'type': type(error).__name__, 'errno': error.errno,
                                 'winerror': getattr(error, 'winerror', None)})

    def remaining():
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled()
        value = deadline - time.monotonic()
        if value <= 0:
            raise subprocess.TimeoutExpired('product-agent-turn', args.timeout)
        return value

    def call(action, *, cleanup=False, extra=()):
        command = [sys.executable, str(APP / 'agent_cli.py'), action, args.session_id, *extra, '--url', args.url]
        row = {'client': 'agent_cli.py', 'action': action, 'cleanup': cleanup}
        report['cli_calls'].append(row)
        began = time.monotonic()
        try:
            response = runner(command, cwd=APP, timeout=125 if cleanup else min(125, remaining()),
                              cancel_event=None if cleanup else cancel_event)
            row['returncode'] = response.returncode
            if response.returncode:
                raise ProductRunError('product_cli_failed', 'Product CLI action failed: ' + action)
            value = json.loads(response.stdout)
            if not isinstance(value, dict):
                raise ProductRunError('invalid_cli_response', 'Product CLI response must be an object')
            return value
        finally:
            row['seconds'] = round(time.monotonic() - began, 3)
            checkpoint()

    def verify(state):
        if (state.get('model') != model or state.get('reasoning_effort') != EFFORT
                or state.get('managed_backend') != 'codex' or state.get('connection_mode') != 'managed'
                or state.get('backend') != 'codex-app-server'):
            raise ProductRunError('runtime_identity_mismatch', 'Requested product runtime/model/effort differs')
        report.update(reported_model=state['model'], reported_effort=state['reasoning_effort'],
                      backend=state['backend'], conversation_id=state.get('thread_id'), pid=state.get('pid'))

    checkpoint()
    try:
        remaining()
        report['product_contract'] = product_contract()
        initial = read(args.url + f'/api/sessions/{args.session_id}', timeout=min(30, remaining()))
        write(destination / 'session-initial.json', initial)
        report['initial_revision'] = initial['revision']
        state = call('status')
        if (state.get('connected') or state.get('alive') or state.get('thread_id') or state.get('messages')
                or any(item.get('event') in {'agent_plan_updated', 'agent_steps_updated'} for item in initial.get('history', []))):
            raise ProductRunError('task_not_fresh', 'A unique unused task/runtime is required')
        phase = 'settings'
        state = call('settings', extra=('--managed-backend', 'codex', '--connection-mode', 'managed',
                     '--model', model, '--effort', EFFORT))
        verify(state)
        initial_chat_count = len(state.get('messages', []))
        phase, message_attempted = 'message', True
        report['message_count'] = report['agent_execution_count'] = 1
        state = call('message', extra=('--text-file', str(message_file.resolve())))
        expected = state.get('active_turn_id') or (state.get('last_turn') or {}).get('turn_id')
        phase = 'waiting_for_natural_completion'
        while True:
            remaining()
            verify(state)
            report['last_agent_status'] = state.get('status')
            active, last = state.get('active_turn_id'), state.get('last_turn')
            if active:
                if expected and active != expected:
                    raise ProductRunError('agent_turn_changed', 'Product turn changed without a new message')
                expected = active
            if isinstance(last, dict) and last.get('turn_id') == expected:
                report['last_turn'] = {key: last.get(key) for key in ('turn_id', 'status')}
                if last.get('status') in {'failed', 'interrupted'}:
                    terminal = True
                    report['natural_termination'] = True
                    raise ProductRunError('agent_turn_' + last['status'], 'Product turn ended without completing')
            if state.get('warning') or state.get('error') or state.get('error_metadata'):
                report['runtime_warning_observed'] = True
                if isinstance(state.get('error_metadata'), dict):
                    report['last_error_metadata'] = {key: state['error_metadata'][key] for key in
                        ('code', 'category', 'will_retry', 'turn_id', 'http_status') if key in state['error_metadata']}
            if state.get('status') in {'error', 'disconnected'} or state.get('alive') is False:
                raise ProductRunError('agent_disconnected', 'Product Agent disconnected before completing')
            if state.get('status') == 'idle' and not active:
                completed = (isinstance(last, dict) and last.get('turn_id') == expected and last.get('status') == 'completed'
                             if state.get('turn_outcome_supported') else not state.get('error'))
                if completed:
                    terminal = True
                    report.update(status='turn_completed', natural_termination=True)
                    break
                if not state.get('turn_outcome_supported') and state.get('error'):
                    terminal = True
                    raise ProductRunError('agent_turn_error', 'Product Agent reported a turn error')
            delay = min(max(0.01, getattr(args, 'poll_interval', 1.0)), remaining())
            (cancel_event or threading.Event()).wait(delay)
            state = call('status')
    except _Cancelled:
        report.update(status='cancelled', failure_code='cancelled', failure_phase=phase)
    except subprocess.TimeoutExpired:
        report.update(status='timed_out', failure_code='turn_timeout', failure_phase=phase)
    except Exception as exc:
        report.update(status='incomplete', failure_code=getattr(exc, 'code', 'product_runtime_failure'),
                      failure_type=type(exc).__name__, failure_phase=phase)
    finally:
        if message_attempted:
            for action in (['disconnect'] if terminal else ['interrupt', 'disconnect']):
                try:
                    call(action, cleanup=True)
                except Exception as exc:
                    report['cleanup_errors'].append({'action': action, 'error_type': type(exc).__name__})
            try:
                final = read(args.url + f'/api/sessions/{args.session_id}', timeout=30)
                chat = read(args.url + f'/api/sessions/{args.session_id}/agent', timeout=30)
                write(destination / 'session-final.json', final)
                write(destination / 'agent-final.json', chat)
                write(destination / 'chat.json', chat.get('messages', []))
                report['final_revision'] = final.get('revision')
                report['authored_state_unchanged'] = _authored_snapshot(initial) == _authored_snapshot(final)
                if not report['authored_state_unchanged']:
                    raise ProductRunError('route_modified_plan', 'Read-only route task modified authored plan state')
                if report['status'] == 'turn_completed':
                    visible = [item['text'] for item in chat.get('messages', [])[initial_chat_count:]
                               if isinstance(item, dict) and item.get('role') == 'assistant'
                               and isinstance(item.get('text'), str) and item['text'].strip()]
                    if visible:
                        (destination / 'visible-final.txt').write_text(visible[-1], encoding='utf-8', newline='')
                    text, value = final_visible_json(chat.get('messages', []), after=initial_chat_count)
                    write(destination / 'visible-final.json', value)
                    report.update(status='complete', visible_reply_sha256=digest(text), visible_json_valid=True,
                                  visible_reply_chars=len(text), final_session_sha256=digest(_canonical(final)))
            except Exception as exc:
                report['collection_error'] = {'code': getattr(exc, 'code', 'result_collection_failed'), 'error_type': type(exc).__name__}
                if report['status'] == 'turn_completed':
                    report.update(status='incomplete', failure_code=getattr(exc, 'code', 'result_collection_failed'))
            if report['cleanup_errors'] and report['status'] == 'complete':
                report.update(status='incomplete', failure_code='cleanup_failed')
        report['completed_at'] = now()
        checkpoint()
    return report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--run-id', default=RUN_ID)
    result.add_argument('--method-root', type=Path, required=True, help='Frozen generated store containing case-methods/*.json')
    result.add_argument('--candidate-list', type=Path, required=True)
    result.add_argument('--benchmark-root', type=Path, default=batch.ROOT)
    result.add_argument('--data-root', type=Path, default=batch.DATA)
    result.add_argument('--offline-root', type=Path, default=batch.OFFLINE)
    result.add_argument('--scope-library-id', action='append', default=[], help='Common additional enabled libraries, restored at cleanup')
    result.add_argument('--model', default=MODEL, help='Explicit model; recorded in the frozen protocol')
    result.add_argument('--parallel', type=int, choices=(1, 2), default=2)
    result.add_argument('--timeout', type=float, default=3600)
    result.add_argument('--poll-interval', type=float, default=1.0)
    result.add_argument('--prepare-only', action='store_true', help='Validate inputs and tools without model inference')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', args.run_id):
        raise ValueError('run-id must be a simple new directory name')
    if args.timeout <= 0 or args.poll_interval <= 0 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', args.model):
        raise ValueError('A valid model and positive timeout/poll interval are required')
    candidates, candidate_sha = load_candidates(args.candidate_list)
    method_hashes = profile_files(args.method_root)
    message = route_message(candidates['case_ids'])
    root = args.benchmark_root.resolve()
    here, runs = root / 'private/evaluations' / args.run_id, root / 'runs' / args.run_id
    for directory in (here, runs):
        if directory.exists() and any(directory.iterdir()):
            raise RuntimeError('Attempt directory is not empty; historical runs are never overwritten or resumed')
    for cid in CASES:
        public_goal(root, cid)
    here.mkdir(parents=True, exist_ok=True)
    runtime = here / 'runtime'
    (runtime / 'tmp').mkdir(parents=True, exist_ok=True)
    os.environ.update(SIM_ADVISOR_NODE=str(args.offline_root / 'node/node.exe'),
        SIM_ADVISOR_QMD_DIR=str(args.offline_root / 'qmd/node_modules/@tobilu/qmd'),
        SIM_ADVISOR_SEARCH_CACHE=str(runtime / 'search-cache'), TEMP=str(runtime / 'tmp'),
        TMP=str(runtime / 'tmp'), PYTHONIOENCODING='utf-8')
    os.environ.pop('QMD_EMBED_MODEL', None)
    os.environ.pop('SIM_ADVISOR_EMBED_MODEL', None)
    from advisor.server import App, make_server
    from advisor.config import codex_command
    from advisor.agent_sessions import INSTRUCTIONS, planning_tools
    from advisor.case_methods import CaseMethodStore, tool_definitions as method_tool_definitions
    from advisor.runtime_adapters import CodexRuntimeFactory, CodexDebugRuntime, runtime_command
    from advisor.library_store import LibraryStore
    from advisor.retrieval_settings import read as embedding_settings
    from advisor.planning_service import tool_definitions
    from scripts.benchmark_native_cli import project_mcp_disables

    extra_tools = method_tool_definitions()

    class RouteProfileFactory(CodexRuntimeFactory):
        def __call__(self, *, cwd, on_event, on_tool, model=args.model, reasoning_effort=EFFORT):
            settings = dict(batch.PROFILE)
            inherited, _ = project_mcp_disables(Path(cwd))
            settings.update(inherited)
            command = runtime_command(codex_command()) + batch.flags(settings) + ['--strict-config']
            underlying = CodexDebugRuntime(command=command, cwd=cwd, on_event=on_event,
                on_tool=on_tool, model=model, reasoning_effort=reasoning_effort)
            return ExtendedRuntime(underlying, extra_tools)

    store = LibraryStore(args.data_root / 'libraries')
    if embedding_settings(args.data_root / 'libraries/search')['enabled']:
        raise ValueError('Embeddings must already be disabled; this runner will not change them')
    if store.evaluation_set_summaries()['retrieval_enabled']:
        raise ValueError('Held-out retrieval must already be disabled')
    store.evaluation_policy()
    before = {lib['id']: lib['enabled'] for lib in store.libraries(include_deleted=True) if not lib.get('deleted_at')}
    if set(args.scope_library_id) - set(before):
        raise ValueError('Additional common scope names an unknown/deleted library')
    write(here / 'scope-before.json', before)
    app, server, stop, lock = None, None, threading.Event(), threading.RLock()
    state = {'schema': 'route-selection-progress/v1', 'status': 'preparing', 'model': args.model,
             'effort': EFFORT, 'expected_runs': 6, 'parallel_limit': args.parallel, 'active': [], 'completed': []}

    def progress():
        state['updated_at'] = now()
        write(here / 'progress.json', state)

    try:
        additions = [lid for lid in dict.fromkeys(args.scope_library_id) if not before[lid]]
        if additions:
            store.set_enabled_many(additions, True)
        common_scope = sorted(lid for lid, enabled in before.items() if enabled or lid in additions)
        if not common_scope:
            raise ValueError('A nonempty common enabled-library scope is required')
        write(here / 'scope-frozen.json', {'enabled_library_ids': common_scope, 'controller_added_library_ids': additions})
        data = runtime / 'product'
        data.mkdir(parents=True)
        link, target = data / 'libraries', args.data_root / 'libraries'
        escaped_link, escaped_target = str(link).replace("'", "''"), str(target).replace("'", "''")
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            f"New-Item -ItemType Junction -Path '{escaped_link}' -Target '{escaped_target}' | Out-Null"],
            check=True, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if link.resolve() != target.resolve():
            raise ValueError('Experiment library junction points to a different store')
        docs = runtime / 'empty-documents'
        docs.mkdir()
        app = App(data_dir=data, doc_root=docs, runtime_factory=RouteProfileFactory())
        method_store = CaseMethodStore(args.method_root, app.kb)
        # Check both conditions against live provider authority before inference.
        prepared_methods = {}
        for cid in candidates['case_ids']:
            baseline = method_store.get_case_method(cid, enabled=False)
            prepared = method_store.get_case_method(cid, enabled=True)
            if baseline.get('state') != 'not_prepared' or prepared.get('state') != 'ready':
                raise ValueError('Each frozen candidate requires a current ready method profile and not_prepared baseline')
            prepared_methods[cid] = {'baseline_state': baseline['state'], 'method_state': prepared['state'],
                                     'method_response_sha256': digest(_canonical(prepared))}
        session_conditions, audit_paths = {}, {}
        backend_audit_lock = threading.Lock()

        def backend_audit(row):
            with backend_audit_lock:
                path = here / 'backend-policy-audits.jsonl'
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(dict(row, recorded_at=now()), ensure_ascii=False) + '\n')

        install_backend_policy(app.tools, 'qmd_keyword', on_search=backend_audit)
        extended = install_route_tools(app, method_store, extra_tools, session_conditions,
                                      candidates['case_ids'], common_scope, audit_paths)
        server = make_server(app, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        write(here / 'services.json', {'pid': os.getpid(), 'urls': {'product': url}, 'data_roots': {'product': str(data)}})
        public_tools = batch.request(url, '/api/tools')
        remote = {item['name']: item for item in public_tools.get('tools', [])}
        if any(remote.get(item['name']) != item for item in tool_definitions()):
            raise ValueError('Remote production tool schemas differ')
        write(here / 'product-public-tools.json', public_tools)
        write(here / 'runtime-tools.json', extended)
        (here / 'product-instructions.txt').write_text(INSTRUCTIONS, encoding='utf-8')
        tasks, contexts, manifests = [], {}, {}
        for cid in CASES:
            for condition in CONDITIONS:
                key = condition + '-' + cid
                prepared = create_task(url, root, cid)
                sid = prepared['session']['id']
                if sid in session_conditions:
                    raise ValueError('Task sessions must be independent')
                session_conditions[sid] = condition
                audit_paths[sid] = here / 'audits' / (key + '.jsonl')
                context = app.tools.call(sid, 'get_task_context', {})
                contexts[key], manifests[key] = context, prepared['input_manifest']
                write(here / 'prepared' / key / 'input-manifest.json', prepared['input_manifest'])
                write(here / 'prepared' / key / 'context-preflight.json', context)
                write(here / 'prepared' / key / 'prepared-task.json', {'session_id': sid, 'goal_chars': prepared['goal_chars']})
                tasks.append(SimpleNamespace(url=url, session_id=sid, case=cid, condition=condition,
                    destination=runs / key, model=args.model, timeout=args.timeout,
                    poll_interval=args.poll_interval, audit_path=audit_paths[sid]))
        for cid in CASES:
            a, b = ('existing_navigation-' + cid), ('case_methods-' + cid)
            if (contexts[a]['goal'] != contexts[b]['goal']
                    or batch.normalize_context(contexts[a]['model']) != batch.normalize_context(contexts[b]['model'])
                    or manifests[a] != manifests[b]):
                raise ValueError('Public text/model/geometry inputs differ across paired tasks')
        source_hashes = {path: batch.sha(APP / path) for path in CORE_PATHS}
        protocol = {'schema': 'route-selection-protocol/v1', 'frozen_at': now(),
            'cases': list(CASES), 'conditions': list(CONDITIONS), 'expected_runs': 6,
            'requested_model': args.model, 'requested_effort': EFFORT,
            'maximum_parallel_agent_processes': 2, 'actual_active_turn_limit': args.parallel,
            'attempts_per_condition_case': 1, 'automatic_retry_count': 0, 'best_of_selection': False,
            'entrypoint': 'Actual agent_cli.py settings/message --text-file/status/interrupt/disconnect',
            'runtime_profile': batch.PROFILE, 'production_instructions_unchanged': True,
            'production_tool_schemas_unchanged': True, 'runtime_tool_extension': ['get_case_method'],
            'runtime_contract_queries_use_same_extended_schema': True,
            'condition_difference': 'get_case_method returns not_prepared in existing_navigation; frozen profiles in case_methods.',
            'candidate_list': candidates, 'candidate_list_sha256': candidate_sha,
            'method_root': str(args.method_root.resolve()), 'method_profile_sha256': method_hashes,
            'method_preflight': prepared_methods, 'instructions_sha256': digest(INSTRUCTIONS),
            'production_tool_schema_sha256': digest(_canonical(planning_tools())),
            'runtime_tool_schema_sha256': digest(_canonical(extended)),
            'public_tool_schema_sha256': digest(_canonical(public_tools)),
            'message_sha256': digest(message), 'same_message_for_all_runs': True,
            'enabled_library_ids': common_scope, 'controller_added_library_ids': additions,
            'agent_scope_changes': 'Only already-enabled true requests are acknowledged without writes; all additions/disables are rejected.',
            'read_only_dispatch_guard': 'All existing schemas retained; plan/steps/settings/draft writes rejected before dispatch.',
            'embedding_enabled': False, 'heldout_retrieval_enabled': False,
            'search_mode': 'qmd_keyword', 'retrieval_backend_policy': policy_description('qmd_keyword'),
            'private_reference_read': False, 'test_answers_supplied': False,
            'evaluation_performed': False, 'solver_run': False, 'index_rebuild_performed': False,
            'save_plan_required': False, 'completion': 'Natural completed turn plus final visible JSON object; no save_plan requirement.',
            'source_sha256': source_hashes, 'public_input_manifest': manifests,
            'execution_order': [task.condition + '-' + task.case for task in tasks],
            'limitations': ['Three public tasks and one attempt per condition; exploratory pilot only.',
                           'This evaluates route selection; source completeness and complete implementation plans are outside scope.']}
        write(here / 'protocol.json', protocol)
        write(here / 'preflight.json', {'status': 'ready', 'no_model_inference': True,
            'same_public_inputs': True, 'same_runtime_instructions_tools_message_scope': True,
            'method_profiles_ready': True, 'private_reference_read': False})
        if args.prepare_only:
            state.update(status='preflight_complete', no_model_inference=True)
            progress()
            return 0

        def monitor_stop():
            while not stop.wait(0.5):
                if (here / 'stop.request').exists():
                    stop.set()
        threading.Thread(target=monitor_stop, daemon=True).start()

        def execute(task):
            key = task.condition + '-' + task.case
            with lock:
                state['status'] = 'running'
                state['active'].append({'run': key, 'session_id': task.session_id})
                progress()
            try:
                result = run_turn(task, message, cancel_event=stop)
                row = {field: result.get(field) for field in ('status', 'condition', 'case_id', 'session_id',
                    'seconds', 'natural_termination', 'visible_json_valid', 'visible_reply_sha256',
                    'failure_code', 'conversation_id', 'saved_plan')}
                row['run'] = key
                with lock:
                    state['completed'].append(row)
                print(json.dumps({'event': 'completed', **row}, ensure_ascii=False), flush=True)
                return result
            finally:
                with lock:
                    state['active'] = [item for item in state['active'] if item['run'] != key]
                    progress()

        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = [executor.submit(execute, task) for task in tasks]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                # Set before executor shutdown waits for our active CLI clients.
                stop.set()
                raise
        if (batch.sha(args.candidate_list) != candidate_sha or profile_files(args.method_root) != method_hashes
                or any(batch.sha(APP / path) != value for path, value in source_hashes.items())):
            raise ValueError('Frozen candidates, methods or product sources changed during experiment')
        for manifest in manifests.values():
            for item in manifest['files']:
                if batch.sha(root / item['path']) != item['sha256']:
                    raise ValueError('Frozen public input changed during experiment')
        actual_scope = sorted(lib['id'] for lib in store.libraries() if lib['enabled'])
        if actual_scope != common_scope:
            raise ValueError('Common enabled-library scope changed during the experiment')
        failed = any(row['status'] != 'complete' for row in state['completed'])
        state.update(status='cancelled' if stop.is_set() else 'completed_with_failures' if failed else 'completed',
                     completed_at=now(), frozen_hashes_unchanged=True)
        progress()
        return 2 if failed else 0
    except BaseException as exc:
        stop.set()
        state.update(status='cancelled' if isinstance(exc, (_Cancelled, KeyboardInterrupt)) else 'failed',
                     failure_type=type(exc).__name__)
        progress()
        write(here / 'infrastructure-error.json', {'type': type(exc).__name__, 'detail': str(exc), 'traceback': traceback.format_exc()})
        raise
    finally:
        stop.set()
        cleanup, errors = {'timestamp': now(), 'scope_restored': False, 'owned_services_stopped': False}, []
        if app is not None:
            try:
                app.agents.close()
            except Exception as exc:
                errors.append({'action': 'close_agents', 'type': type(exc).__name__})
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception as exc:
                errors.append({'action': 'close_server', 'type': type(exc).__name__})
        cleanup['owned_services_stopped'] = not errors
        try:
            actual = {lib['id']: lib['enabled'] for lib in store.libraries(include_deleted=True) if lib['id'] in before}
            for enabled in (False, True):
                ids = [key for key, value in before.items() if value is enabled and actual.get(key) != value]
                if ids:
                    store.set_enabled_many(ids, enabled)
            actual = {lib['id']: lib['enabled'] for lib in store.libraries(include_deleted=True) if lib['id'] in before}
            cleanup['scope_restored'] = actual == before
            cleanup['heldout_retrieval_enabled'] = store.evaluation_set_summaries()['retrieval_enabled']
        except Exception as exc:
            errors.append({'action': 'restore_scope', 'type': type(exc).__name__})
        cleanup['errors'] = errors
        write(here / 'cleanup.json', cleanup)
        print(json.dumps({'event': 'cleanup', **cleanup}), flush=True)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
