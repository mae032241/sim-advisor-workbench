"""One frozen public-case experiment: autonomous, curated, then reviewed revision.

All inference uses the shipped agent_cli and production AgentSessions contract.
Only public benchmark inputs and a pre-frozen allowed-library evidence pack are
read. The reviewed condition deliberately shares the curated first draft and
adds an independent review plus a continuation; this is not equal compute.
"""
from __future__ import annotations

import argparse
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
from scripts.benchmark_backend_policy import policy_description
from scripts.benchmark_product_cli import (MESSAGE, ProductRunError, _Cancelled,
    _canonical, _http_read, _run_process, product_contract, saved_plan_state)
from scripts.benchmark_product_setup import create_task, public_goal

MODEL, EFFORT, CASE = 'gpt-6-luna', 'high', 'case-09'
PACK_SCHEMA = 'allowed-evidence-pack/v1'
MAX_PACK_BYTES, MAX_EVIDENCE_CHARS, MAX_MESSAGE_CHARS = 200000, 8500, 12000
MAX_REVIEW_CHARS = 10500
CONDITIONS = ('autonomous', 'curated', 'reviewed')
CORE_PATHS = (
    'agent_cli.py', 'tools_cli.py', 'advisor/agent_sessions.py', 'advisor/codex_runtime.py',
    'advisor/runtime_adapters.py', 'advisor/planning_service.py', 'advisor/research_policy.py',
    'prompts/comsol_planner.v1.txt', 'scripts/benchmark_product_cli.py',
    'scripts/benchmark_product_setup.py', 'scripts/benchmark_product_batch.py',
    'scripts/benchmark_backend_policy.py', 'advisor/plan_drafts.py', 'advisor/plan_review.py',
    'advisor/planning_views.py', 'advisor/plan_settings.py', 'advisor/mechanism_checks.py',
    'advisor/case_guides.py', 'advisor/case_navigation.py', 'advisor/library_store.py')

CURATED_PREFIX = MESSAGE + '''

以下是事先从当前允许检索的资料库选取并逐字核对的有界来源摘录，是供你判断适用性的用户上下文，不是参考答案或强制建模路线。资料已通过当前任务的 read_source 读取；可按精确 source_id、locator 引用，仍须核对与本任务的适用范围及缺口。请自行检索补充，并保留真实不确定性。

'''
REVIEW_PREFIX = '''你是独立技术审阅者。当前任务上下文中的 plan、plan_settings、design_brief 是另一位规划者已保存的候选方案，不是正确答案。先 get_task_context 阅读原始公开需求、实际导入模型事实和完整候选方案，按需 get_plan_settings 及检索读取原文。只审阅，不保存、更新或修复方案，不生成替代完整方案。

逐项核查需求覆盖、方程与闭合关系、物理接口及耦合方向、域与边界作用域、初始/边界条件、单位和参数来源、研究/求解器顺序、网格与收敛验证。区分已证实错误、条件性风险和待核实缺口；不要因缺少数值而编造。只报告可定位且可行动的发现，说明影响、依据、建议的局部修改与验证方法；保留候选方案中有效的约束。不要声称求解已运行或物理已验证。最终可见回复用紧凑 JSON，含 findings（每项 location、severity、issue、basis、local_change、verification）、retained_constraints、unresolved；总计不超过10000字符。无发现可明确返回空列表。

以下与规划者完全相同的事先选定逐字摘录供核对；仅为允许资料证据，不是正确答案：

'''
REVISION_PREFIX = '''请继续当前已保存方案。以下是独立技术审阅者的最终可见意见；这些意见可能有误，不能视为权威或用户新增的物理事实。请针对原始公开需求、导入模型事实及已读原文逐项核实，说明采纳或拒绝的依据，对确有必要的地方作局部修订，保留原方案中有效的约束、设计和可追溯来源，然后保存修订后的完整方案。不要另起无关路线，也不要声称已经运行求解或验证物理结果。若核实后无需修改，仍请保存经核查的方案并说明理由。

独立审阅意见：
'''


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def write(path, value):
    batch.write(path, value)


def evidence_text(items):
    sections = []
    for item in items:
        locator = f"text:{item['offset']}:{item['offset'] + len(item['text'])}"
        sections.append(f"[source_id={item['source_id']}; title={item.get('title', '')}; locator={locator}]\n{item['text']}")
    return '\n\n---\n\n'.join(sections)


def load_pack(path):
    """Validate bounded verbatim pages, without accepting arbitrary prose payloads."""
    path = Path(path)
    if path.stat().st_size > MAX_PACK_BYTES:
        raise ValueError('Evidence pack exceeds the frozen byte bound')
    blob = path.read_bytes()
    pack = json.loads(blob.decode('utf-8'))
    if not isinstance(pack, dict) or pack.get('schema') != PACK_SCHEMA:
        raise ValueError('Unsupported evidence pack schema')
    items = pack.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 16:
        raise ValueError('Evidence pack requires 1..16 source pages')
    identities = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Evidence page must be an object')
        for key in ('source_id', 'library_id', 'text', 'sha256'):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError('Evidence page is missing a required string')
        if (type(item.get('offset')) is not int or item['offset'] < 0
                or type(item.get('length')) is not int or not 500 <= item['length'] <= 16000
                or len(item['text']) > item['length'] or not item['text'].strip()):
            raise ValueError('Evidence page bounds differ from read_source bounds')
        if item['sha256'] != digest(item['text']):
            raise ValueError('Evidence page hash mismatch')
        if any(len(item[key]) > 500 for key in ('source_id', 'library_id')):
            raise ValueError('Evidence page identity exceeds bound')
        title = item.get('title', '')
        if not isinstance(title, str) or len(title) > 500 or '\n' in title or '\r' in title:
            raise ValueError('Evidence title must be a bounded single line')
        identity = (item['source_id'], item['offset'], item['length'])
        if identity in identities:
            raise ValueError('Duplicate evidence page')
        identities.add(identity)
        expected = [{'source_id': item['source_id'], 'context': 'source_text',
                     'locator': f"text:{item['offset']}:{item['offset'] + len(item['text'])}"}]
        if 'evidence_refs' in item and item['evidence_refs'] != expected:
            raise ValueError('Evidence receipt does not match page bounds')
    compiled = evidence_text(items)
    if len(compiled) > MAX_EVIDENCE_CHARS or pack.get('text') != compiled:
        raise ValueError('Compiled evidence differs from bounded verbatim pages')
    for prefix in (CURATED_PREFIX, REVIEW_PREFIX):
        if len(prefix + compiled) > MAX_MESSAGE_CHARS:
            raise ValueError('Evidence message exceeds the product message limit')
    return pack, digest(blob)


def preload_pack(app, sid, pack):
    """Real product reads grant only the pages actually returned and verified."""
    ids = sorted({item['library_id'] for item in pack['items']})
    app.tools.call(sid, 'set_libraries_enabled', {'library_ids': ids, 'enabled': True})
    receipts = []
    for item in pack['items']:
        page = app.tools.call(sid, 'read_source', {key: item[key] for key in ('offset', 'length')}
                              | {'id': item['source_id']})
        if (not isinstance(page, dict) or page.get('id') != item['source_id']
                or page.get('library_id') != item['library_id']
                or page.get('offset') != item['offset'] or page.get('text') != item['text']
                or page.get('title', '') != item.get('title', '')
                or digest(page.get('text', '')) != item['sha256']):
            raise ValueError('Frozen evidence differs from a live allowed read_source page')
        expected = [{'source_id': item['source_id'], 'context': 'source_text',
                     'locator': f"text:{item['offset']}:{item['offset'] + len(item['text'])}"}]
        if page.get('evidence_refs') != expected:
            raise ValueError('Live evidence receipt differs from the frozen page')
        receipts.append({'source_id': item['source_id'], 'offset': item['offset'],
            'length': item['length'], 'returned_chars': len(page['text']), 'sha256': item['sha256'],
            'evidence_refs': page['evidence_refs'], 'read_via': 'PlanningService.call/read_source'})
    return receipts


def readonly_reviewer(app, reviewer_ids, audit_path):
    """Keep the full product schema; reject reviewer writes at runtime dispatch."""
    allowed = {'get_plan', 'get_task_context', 'get_plan_settings', 'get_library_scope',
               'set_libraries_enabled', 'search_knowledge', 'read_source', 'search_cases',
               'get_case_guide', 'read_case_configuration', 'get_mechanism_checks', 'review_plan',
               'get_plan_draft'}
    original = app.agents.runtime_tool

    def guarded(sid, name, arguments, *, source_entry=None):
        disabling_scope = name == 'set_libraries_enabled' and isinstance(arguments, dict) and arguments.get('enabled') is not True
        if sid in reviewer_ids and (name not in allowed or disabling_scope):
            row = {'recorded_at': now(), 'session_id': sid, 'tool': name,
                   'success': False, 'reviewer_write_blocked': True}
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            with audit_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            return {'success': False, 'contentItems': [{'type': 'inputText', 'text': json.dumps({
                'error': 'Independent review is read-only; no plan or settings write was performed.',
                'next_action': 'Inspect current candidate and return actionable findings in the final visible reply.'})}]}
        return original(sid, name, arguments, source_entry=source_entry)
    app.agents.runtime_tool = guarded


def final_visible_message(messages, *, after=0, maximum=MAX_REVIEW_CHARS):
    visible = [item['text'] for item in messages[after:] if isinstance(item, dict)
               and item.get('role') == 'assistant' and isinstance(item.get('text'), str)
               and item['text'].strip()]
    if not visible:
        raise ProductRunError('review_reply_missing', 'No final visible independent review was returned')
    result = visible[-1]
    if len(result) > maximum:
        raise ProductRunError('review_reply_over_bound', 'Independent review exceeds the frozen feedback bound')
    return result


def continuation_snapshot(session):
    """Ignore only the transport's live source availability decoration."""
    value = copy.deepcopy(session)
    if isinstance(value, dict) and isinstance(value.get('sources'), list):
        for source in value['sources']:
            if isinstance(source, dict):
                source.pop('availability', None)
    return value


def continuation_snapshot_sha256(session):
    return digest(_canonical(continuation_snapshot(session)))


def run_turn(args, message, *, fresh=True, keep_connected=False, reviewer=False,
             seeded_candidate=False, cancel_event=None, runner=None, http_reader=None):
    """Exactly one CLI message, including a fresh or explicitly paired continuation."""
    parsed = urlsplit(args.url)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.path not in {'', '/'} or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('A loopback product service is required')
    if not re.fullmatch(r'[a-f0-9]{32}', args.session_id) or args.timeout <= 0:
        raise ValueError('A prepared task and positive timeout are required')
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= MAX_MESSAGE_CHARS:
        raise ValueError('Message must fit the unchanged product message bound')
    destination = Path(args.destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Turn output directory must be empty')
    destination.mkdir(parents=True, exist_ok=True)
    message_file = destination / 'user-message.txt'
    message_file.write_text(message, encoding='utf-8', newline='')
    runner, read = runner or _run_process, http_reader or _http_read
    started, initial, message_attempted, terminal, phase = time.monotonic(), None, False, False, 'preflight'
    deadline = started + args.timeout
    report = {'schema': 'simulation-plan-run/v1', 'engine': 'product-agent-cli', 'case_id': args.case,
        'arm': 'technical-review' if reviewer else args.condition, 'retrieval_arm': 'rag',
        'condition': args.condition, 'status': 'running', 'session_id': args.session_id,
        'workbench_url': args.url, 'requested_model': MODEL, 'requested_effort': EFFORT,
        'timeout_seconds': args.timeout, 'message_sha256': digest(message), 'message_chars': len(message),
        'message_count': 0, 'agent_execution_count': 0, 'automatic_retry_count': 0, 'retry_count': 0,
        'model_call_count': None, 'evaluation_performed': False, 'scoring_run': False,
        'private_reference_supplied': False, 'execution_feedback_supplied': False,
        'physical_feedback_supplied': False, 'solver_run': False, 'saved_plan': False,
        'fresh_runtime': fresh, 'paired_continuation_of': 'curated' if not fresh else None,
        'reviewer_read_only': reviewer, 'reviewer_context_is_curated_draft': seeded_candidate,
        'tool_audit_path': str(getattr(args, 'audit_path', '') or '') or None,
        'cli_calls': [], 'cleanup_errors': [], 'started_at': now()}

    def checkpoint():
        report['seconds'] = round(time.monotonic() - started, 3)
        write(destination / 'run.json', report)

    def remaining():
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled()
        value = deadline - time.monotonic()
        if value <= 0:
            raise subprocess.TimeoutExpired('product-agent-turn', args.timeout)
        return value

    def call(action, *, cleanup=False, extra=()):
        command = [sys.executable, str(APP / 'agent_cli.py'), action, args.session_id,
                   *extra, '--url', args.url]
        row = {'client': 'agent_cli.py', 'action': action, 'cleanup': cleanup}
        report['cli_calls'].append(row)
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
            checkpoint()

    def verify(state):
        if (state.get('model') != MODEL or state.get('reasoning_effort') != EFFORT
                or state.get('managed_backend') != 'codex' or state.get('connection_mode') != 'managed'
                or state.get('backend') != 'codex-app-server'):
            raise ProductRunError('runtime_identity_mismatch', 'Requested product runtime/model/effort differs')
        report.update(reported_model=state['model'], reported_effort=state['reasoning_effort'],
                      backend=state['backend'], conversation_id=state.get('thread_id'), pid=state.get('pid'))

    checkpoint()
    try:
        report['product_contract'] = product_contract()
        initial = read(args.url + f'/api/sessions/{args.session_id}', timeout=min(30, remaining()))
        write(destination / 'session-initial.json', initial)
        report['initial_revision'] = initial['revision']
        state = call('status')
        report['initial_visible_message_count'] = len(state.get('messages', []))
        if fresh:
            if (state.get('connected') or state.get('alive') or state.get('thread_id') or state.get('messages')
                    or (not seeded_candidate and any(item.get('event') in {'agent_plan_updated', 'agent_steps_updated'}
                        for item in initial.get('history', [])))):
                raise ProductRunError('task_not_fresh', 'Fresh planning requires a unique unused task/runtime')
            phase = 'settings'
            state = call('settings', extra=('--managed-backend', 'codex', '--connection-mode', 'managed',
                         '--model', MODEL, '--effort', EFFORT))
        elif (not state.get('connected') or state.get('status') != 'idle' or state.get('active_turn_id')
                or state.get('thread_id') != args.expected_thread_id):
            raise ProductRunError('continuation_identity_mismatch', 'Paired revision must continue the idle curated thread')
        verify(state)
        initial_chat_count = len(state.get('messages', []))
        if not fresh:
            if continuation_snapshot_sha256(initial) != args.expected_session_sha256:
                raise ProductRunError('continuation_snapshot_mismatch', 'Curated draft changed before paired revision')
        phase, message_attempted = 'message', True
        report['message_count'] = report['agent_execution_count'] = 1
        state = call('message', extra=('--text-file', str(message_file.resolve())))
        expected = state.get('active_turn_id') or (state.get('last_turn') or {}).get('turn_id')
        phase = 'waiting_for_natural_completion'
        while True:
            remaining()
            verify(state)
            report['last_agent_status'] = state.get('status')
            active = state.get('active_turn_id')
            if active:
                if expected and active != expected:
                    raise ProductRunError('agent_turn_changed', 'Product turn changed without a new message')
                expected = active
            last = state.get('last_turn')
            if isinstance(last, dict) and last.get('turn_id') == expected:
                report['last_turn'] = {key: last.get(key) for key in ('turn_id', 'status')}
                if last.get('status') in {'failed', 'interrupted'}:
                    terminal = True
                    raise ProductRunError('agent_turn_' + last['status'], 'Product turn ended unsuccessfully')
            if state.get('error') or state.get('warning') or state.get('error_metadata'):
                report['runtime_warning_observed'] = True
                if isinstance(state.get('error_metadata'), dict):
                    report['last_error_metadata'] = {key: state['error_metadata'][key] for key in
                        ('code', 'category', 'will_retry', 'turn_id', 'http_status') if key in state['error_metadata']}
            if state.get('status') in {'error', 'disconnected'} or state.get('alive') is False:
                raise ProductRunError('agent_disconnected', 'Product runtime disconnected before completion')
            if state.get('status') == 'idle' and not active:
                completed = (isinstance(last, dict) and last.get('turn_id') == expected and last.get('status') == 'completed'
                             if state.get('turn_outcome_supported') else not state.get('error'))
                if completed:
                    terminal, report['status'] = True, 'turn_completed'
                    break
            delay = min(max(0.01, getattr(args, 'poll_interval', 5)), remaining())
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
            # B alone is deliberately left idle for its paired continuation.
            actions = [] if terminal and keep_connected and report['status'] == 'turn_completed' else (
                ['disconnect'] if terminal else ['interrupt', 'disconnect'])
            for action in actions:
                try:
                    call(action, cleanup=True)
                except Exception as exc:
                    report['cleanup_errors'].append({'action': action, 'error_type': type(exc).__name__})
            try:
                final = read(args.url + f'/api/sessions/{args.session_id}', timeout=30)
                write(destination / 'session-final.json', final)
                report['final_session_sha256'] = digest(_canonical(final))
                report['final_continuation_snapshot_sha256'] = continuation_snapshot_sha256(final)
                report.update(saved_plan_state(initial, final))
                chat = read(args.url + f'/api/sessions/{args.session_id}/agent', timeout=30)
                write(destination / 'chat.json', chat.get('messages', []))
                write(destination / 'agent-final.json', chat)
                if reviewer and report['status'] == 'turn_completed':
                    review = final_visible_message(chat.get('messages', []), after=initial_chat_count)
                    (destination / 'review.txt').write_text(review, encoding='utf-8', newline='')
                    report['review_sha256'], report['review_chars'] = digest(review), len(review)
                    report['status'] = 'complete'
                elif report['saved_plan']:
                    write(destination / 'saved-plan.json', {key: final.get(key) for key in
                        ('id', 'revision', 'plan', 'plan_settings', 'design_brief', 'questions', 'answers', 'sources',
                         'evidence_gaps', 'planning_incomplete', 'plan_stale', 'research_trace')})
                    markdown = read(args.url + f'/api/sessions/{args.session_id}/export?format=markdown', timeout=30)
                    if not isinstance(markdown, str):
                        raise ProductRunError('invalid_product_export', 'Product plan export was not text')
                    (destination / 'plan.md').write_text(markdown, encoding='utf-8', newline='')
                    report['plan_sha256'] = digest((destination / 'plan.md').read_bytes())
                if report['status'] == 'turn_completed':
                    report['status'] = 'complete' if report['saved_plan'] else 'no_saved_plan'
            except Exception as exc:
                report['collection_error'] = {'code': getattr(exc, 'code', 'result_collection_failed'),
                                              'error_type': type(exc).__name__}
                if report['status'] == 'turn_completed':
                    report['status'] = 'incomplete'
            if report['cleanup_errors'] and report['status'] in {'complete', 'no_saved_plan'}:
                report.update(status='incomplete', failure_code='cleanup_failed')
        report['completed_at'] = now()
        checkpoint()
    return report


def candidate_fields(session):
    return {key: copy.deepcopy(session[key]) for key in
            ('plan', 'plan_settings', 'sources', 'design_brief', 'questions', 'research_trace',
             'geometry_reference_catalog', 'evidence_gaps', 'planning_incomplete') if key in session}


def require_candidate_unchanged(app, sid, expected_sha256):
    """Check raw authored state independently of the HTTP source decoration."""
    actual = digest(_canonical(candidate_fields(app.sessions.get(sid))))
    if actual != expected_sha256:
        raise ProductRunError('curated_candidate_changed', 'Raw curated candidate changed during independent review')
    return actual


def seed_reviewer(app, sid, candidate):
    initial = app.sessions.get(sid)
    original = {key: copy.deepcopy(initial.get(key)) for key in ('goal', 'model')}
    bundle = candidate_fields(candidate)
    # The ordinary bundle merge keeps old questions unless explicitly retired.
    keep = {q['id'] for q in bundle.get('questions', [])}
    retired = [{'id': q['id'], 'reason': 'Independent review context copies the saved candidate question scope.'}
               for q in initial.get('questions', []) if q['id'] not in keep and not q['id'].startswith('decision_')]
    if retired:
        bundle['retired_questions'] = retired
    seeded = app.sessions.apply_agent_bundle(sid, initial['revision'], bundle)
    if any(seeded.get(key) != value for key, value in original.items()):
        raise ValueError('Reviewer candidate injection changed public task facts')
    authored = candidate_fields(candidate)
    if candidate_fields(seeded) != authored:
        raise ValueError('Reviewer candidate differs from the curated saved draft')
    return {'reviewer_context_is_curated_draft': True,
            'candidate_sha256': digest(_canonical(authored)),
            'public_goal_sha256': digest(original['goal']),
            'before_session_sha256': digest(_canonical(initial)),
            'after_session_sha256': digest(_canonical(seeded)),
            'controller_method': 'sessions.apply_agent_bundle', 'model_inference': False}


def disable_all(store):
    enabled = [lib['id'] for lib in store.libraries() if lib['enabled']]
    if enabled:
        store.set_enabled_many(enabled, False)
    if store.evaluation_set_summaries()['retrieval_enabled']:
        raise ValueError('Held-out retrieval must remain disabled')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=batch.ROOT)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--evidence-pack', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--timeout', type=float, default=7200)
    parser.add_argument('--poll-interval', type=float, default=5)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', args.run_id):
        parser.error('run-id must be a simple fresh directory name')
    if args.timeout <= 0 or args.poll_interval <= 0:
        parser.error('timeout and poll interval must be positive')
    root = args.root.resolve()
    # public_goal performs the public path validation; no private loader is used.
    public_goal(root, CASE)
    pack, pack_sha = load_pack(args.evidence_pack)
    here, runs = root / 'private/evaluations' / args.run_id, root / 'runs' / args.run_id
    if here.exists() and any(here.iterdir()):
        raise RuntimeError('Attempt directory already exists; attempts are never resumed or overwritten')
    if runs.exists() and any(runs.iterdir()):
        raise RuntimeError('Output directory already exists; attempts are never overwritten')
    here.mkdir(parents=True, exist_ok=True)
    runtime = here / 'runtime'
    (runtime / 'tmp').mkdir(parents=True, exist_ok=True)
    os.environ.update(SIM_ADVISOR_NODE=str(batch.OFFLINE / 'node/node.exe'),
        SIM_ADVISOR_QMD_DIR=str(batch.OFFLINE / 'qmd/node_modules/@tobilu/qmd'),
        SIM_ADVISOR_SEARCH_CACHE=str(runtime / 'search-cache'), TEMP=str(runtime / 'tmp'),
        TMP=str(runtime / 'tmp'), PYTHONIOENCODING='utf-8')
    os.environ.pop('QMD_EMBED_MODEL', None)
    os.environ.pop('SIM_ADVISOR_EMBED_MODEL', None)
    from advisor.server import App, make_server
    from advisor.config import codex_command
    from advisor.agent_sessions import INSTRUCTIONS, planning_tools
    from advisor.runtime_adapters import CodexRuntimeFactory, CodexDebugRuntime, runtime_command
    from advisor.library_store import LibraryStore
    from advisor.retrieval_settings import read as embedding_settings
    from advisor.planning_service import tool_definitions
    from scripts.benchmark_native_cli import project_mcp_disables

    class PlanningProfileFactory(CodexRuntimeFactory):
        def __call__(self, *, cwd, on_event, on_tool, model=MODEL, reasoning_effort=EFFORT):
            settings = dict(batch.PROFILE)
            inherited, _ = project_mcp_disables(Path(cwd))
            settings.update(inherited)
            command = runtime_command(codex_command()) + batch.flags(settings) + ['--strict-config']
            return CodexDebugRuntime(command=command, cwd=cwd, on_event=on_event,
                on_tool=on_tool, model=model, reasoning_effort=reasoning_effort)

    store = LibraryStore(batch.DATA / 'libraries')
    if embedding_settings(batch.DATA / 'libraries/search')['enabled']:
        raise ValueError('Embeddings must already be disabled; experiment will not change embedding settings')
    if store.evaluation_set_summaries()['retrieval_enabled']:
        raise ValueError('Held-out retrieval must already be disabled')
    store.evaluation_policy()
    before = {lib['id']: lib['enabled'] for lib in store.libraries(include_deleted=True) if not lib.get('deleted_at')}
    write(here / 'scope-before.json', before)
    app, server, stop = None, None, threading.Event()
    state = {'schema': 'evidence-ablation-progress/v1', 'status': 'preparing',
             'model': MODEL, 'effort': EFFORT, 'expected_runs': 4, 'active': [], 'completed': []}

    def progress():
        state['updated_at'] = now()
        write(here / 'progress.json', state)

    try:
        command = [part for part in runtime_command(codex_command()) if part != '--stdio'] + batch.flags(batch.PROFILE) + [
            '--strict-config', '--print-startup-config']
        probe = subprocess.run(command, capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
        write(here / 'runtime-profile-check.json', {'success': probe.returncode == 0,
            'returncode': probe.returncode, 'model_inference': False,
            'stdout_sha256': digest(probe.stdout), 'stderr_sha256': digest(probe.stderr)})
        if probe.returncode:
            raise ValueError('Installed product runtime rejected the common planning profile')
        data = runtime / 'product'
        data.mkdir(parents=True, exist_ok=True)
        link = data / 'libraries'
        escaped_link, escaped_target = str(link).replace("'", "''"), str(batch.DATA / 'libraries').replace("'", "''")
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            f"New-Item -ItemType Junction -Path '{escaped_link}' -Target '{escaped_target}' | Out-Null"],
            check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if link.resolve() != (batch.DATA / 'libraries').resolve():
            raise ValueError('Product library junction target differs')
        docs = runtime / 'empty-documents'
        docs.mkdir()
        app = App(data_dir=data, doc_root=docs, runtime_factory=PlanningProfileFactory())
        batch.HERE = here
        batch.AUDITS.clear()
        batch.audit_tools(app, 'evidence-ablation', 'qmd_keyword')
        reviewer_ids = set()
        readonly_reviewer(app, reviewer_ids, here / 'reviewer-write-blocks.jsonl')
        server = make_server(app, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        write(here / 'services.json', {'pid': os.getpid(), 'urls': {'product': url}, 'data_roots': {'product': str(data)}})
        tools = batch.request(url, '/api/tools')
        remote = {item['name']: item for item in tools.get('tools', [])}
        if any(remote.get(item['name']) != item for item in tool_definitions()):
            raise ValueError('Remote tool schemas differ from the production contract')
        write(here / 'product-tools.json', tools)
        (here / 'product-instructions.txt').write_text(INSTRUCTIONS, encoding='utf-8')
        tasks, contexts, manifests = {}, {}, {}
        for key in ('autonomous', 'curated', 'reviewer'):
            prepared = create_task(url, root, CASE)
            sid = prepared['session']['id']
            tasks[key] = SimpleNamespace(url=url, session_id=sid, case=CASE, condition=key,
                destination=runs / (key + '-' + CASE), model=MODEL, effort=EFFORT,
                timeout=args.timeout, poll_interval=args.poll_interval)
            context = batch.request(url, '/api/tools/call', {'name': 'get_task_context',
                'arguments': {'session_id': sid}})['result']
            preparation = here / 'prepared' / (key + '-' + CASE)
            write(preparation / 'input-manifest.json', prepared['input_manifest'])
            write(preparation / 'context-preflight.json', context)
            write(preparation / 'prepared-task.json', {'session_id': sid, 'goal_chars': prepared['goal_chars']})
            contexts[key], manifests[key] = context, prepared['input_manifest']
            audit = here / 'audits' / (key + '-' + CASE + '.jsonl')
            audit.parent.mkdir(parents=True, exist_ok=True)
            batch.AUDITS[('evidence-ablation', sid)] = audit
            tasks[key].audit_path = audit
        if len({task.session_id for task in tasks.values()}) != 3:
            raise ValueError('Public tasks must have unique session identities')
        first = contexts['autonomous']
        if not all(c['goal'] == first['goal'] and batch.normalize_context(c['model']) == batch.normalize_context(first['model'])
                   and manifests[key] == manifests['autonomous'] for key, c in contexts.items()):
            raise ValueError('Public inputs differ across fresh conditions')
        reviewer_ids.add(tasks['reviewer'].session_id)
        source_hashes = {path: batch.sha(APP / path) for path in CORE_PATHS}
        protocol = {'schema': 'product-evidence-ablation-protocol/v1', 'frozen_at': now(),
            'case_id': CASE, 'cases': [CASE], 'conditions': list(CONDITIONS), 'arms': list(CONDITIONS),
            'retrieval_arm': 'rag',
            'requested_model': MODEL, 'requested_effort': EFFORT,
            'maximum_parallel_agent_processes': 2, 'execution_order': ['A', 'B', 'R', 'C'],
            'actual_active_turn_limit': 1, 'attempts_per_condition': 1, 'automatic_retry_count': 0,
            'best_of_selection': False, 'equal_compute_claimed': False,
            'paired_design': 'B first draft is the exact initial state of C; C adds independent R and one author continuation.',
            'runtime_profile': batch.PROFILE, 'entrypoint': 'Actual agent_cli.py settings/message --text-file/status/interrupt/disconnect',
            'instructions_sha256': digest(INSTRUCTIONS), 'tool_schema_sha256': digest(_canonical(planning_tools())),
            'public_tool_schema_sha256': digest(_canonical(tools)), 'business_tools_filtered': False,
            'reviewer_write_guard': 'All schemas retained; reviewer runtime dispatch rejects plan/settings/draft writes.',
            'reviewer_context_is_curated_draft': True, 'reviewer_candidate_seed': 'Controller copies authored B candidate into R task; public goal/model unchanged.',
            'reviewer_initial_library_scope': 'Inherits B final enabled libraries; may enable additional libraries but may not disable any. C inherits review-added scope.',
            'prompt_rewritten_for_benchmark': False, 'experiment_injection_role': 'user message and declared reviewer candidate context only',
            'embedding_enabled': False, 'heldout_retrieval_enabled': False,
            'retrieval_backend_policy': policy_description('qmd_keyword'),
            'initial_scope': 'All libraries disabled before A and B preloading. B enables pack libraries through real read_source preparation and may enable others. R inherits B final enabled scope, re-reads the same pack, and may only add enabled libraries. C inherits review-added scope.',
            'continuation_snapshot_guard': 'Exact HTTP session hash excluding only sources[*].availability, plus exact raw SessionStore authored candidate hash; source identities/content/provenance remain checked.',
            'evidence_pack_sha256': pack_sha, 'evidence_pack_schema': PACK_SCHEMA,
            'evidence_pack_chars': len(pack['text']), 'evidence_pages': len(pack['items']),
            'source_sha256': source_hashes, 'experiment_script_sha256': batch.sha(Path(__file__)),
            'prompt_sha256': {'autonomous': digest(MESSAGE), 'curated': digest(CURATED_PREFIX + pack['text']),
                              'reviewer': digest(REVIEW_PREFIX + pack['text']), 'revision_prefix': digest(REVISION_PREFIX)},
            'public_input_manifest': manifests['autonomous'], 'private_reference_read': False,
            'reference_hash_owner': 'Separate blinded grader after inference freeze; this runner never reads references.',
            'COMSOL_solve_performed': False, 'index_rebuild_performed': False,
            'limitations': ['One case, one attempt each; no statistical generalization.',
                'C shares B first draft and consumes extra review plus revision compute.',
                'Independent reviewer findings are fallible and must be verified by the author.']}
        write(here / 'protocol.json', protocol)
        write(here / 'frozen-evidence-pack.json', pack)
        disable_all(store)
        write(here / 'preflight-evidence-receipts.json', {
            key: preload_pack(app, tasks[key].session_id, pack) for key in ('curated', 'reviewer')})
        disable_all(store)
        write(here / 'preflight.json', {'status': 'ready', 'no_model_inference': True,
            'same_public_inputs': True, 'same_product_instructions_and_tools': True,
            'evidence_hash_validated': True, 'private_reference_read': False})
        if args.prepare_only:
            state['status'] = 'preflight_complete'
            progress()
            return 0

        def monitor_stop():
            while not stop.wait(0.5):
                if (here / 'stop.request').exists():
                    stop.set()
        threading.Thread(target=monitor_stop, daemon=True).start()

        def execute(key, task, message, **options):
            if stop.is_set():
                raise _Cancelled()
            state.update(status='running', active=[{'run': key + '-' + CASE, 'session_id': task.session_id}])
            progress()
            result = run_turn(task, message, cancel_event=stop, **options)
            row = {field: result.get(field) for field in ('status', 'condition', 'session_id', 'seconds',
                'saved_plan', 'save_plan_count', 'plan_sha256', 'failure_code', 'conversation_id', 'review_sha256')}
            row['run'] = key + '-' + CASE
            state['completed'].append(row)
            state['active'] = []
            progress()
            print(json.dumps({'event': 'completed', **row}, ensure_ascii=False), flush=True)
            return result

        def skipped(key, dependency):
            row = {'schema': 'simulation-plan-run/v1', 'engine': 'product-agent-cli', 'case_id': CASE,
                'arm': 'technical-review' if key == 'reviewer' else key, 'retrieval_arm': 'rag',
                'condition': key, 'status': 'not_executed_dependency', 'dependency': dependency,
                'saved_plan': False, 'message_count': 0, 'agent_execution_count': 0,
                'automatic_retry_count': 0, 'private_reference_supplied': False, 'evaluation_performed': False,
                'requested_model': MODEL, 'requested_effort': EFFORT, 'completed_at': now(), 'run': key + '-' + CASE}
            write(runs / (key + '-' + CASE) / 'run.json', row)
            state['completed'].append(row)
            progress()

        def finish():
            if batch.sha(args.evidence_pack) != pack_sha or any(batch.sha(APP / path) != value for path, value in source_hashes.items()):
                raise ValueError('Frozen evidence or product core changed during experiment')
            if batch.sha(Path(__file__)) != protocol['experiment_script_sha256']:
                raise ValueError('Frozen experiment script changed during experiment')
            for item in manifests['autonomous']['files']:
                if batch.sha(root / item['path']) != item['sha256']:
                    raise ValueError('Frozen public user input changed during experiment')
            failed = any(row['status'] != 'complete' for row in state['completed'])
            state.update(status='completed_with_failures' if failed else 'completed',
                         completed_at=now(), frozen_hashes_unchanged=True)
            progress()
            return 2 if failed else 0

        a = execute('autonomous', tasks['autonomous'], MESSAGE)
        # A is independent: a genuine failed/unsaved attempt remains a result.
        # Check service health, then continue without retrying or replacing A.
        batch.request(url, '/api/tools')
        disable_all(store)
        write(here / 'curated-preload-receipts.json', preload_pack(app, tasks['curated'].session_id, pack))
        b = execute('curated', tasks['curated'], CURATED_PREFIX + pack['text'], keep_connected=True)
        if b['status'] != 'complete':
            skipped('reviewer', 'Curated condition did not produce a complete saved draft.')
            skipped('reviewed', 'Curated condition did not produce a complete saved draft.')
            return finish()
        candidate = app.sessions.get(tasks['curated'].session_id)
        curated_candidate_sha256 = digest(_canonical(candidate_fields(candidate)))
        write(here / 'reviewer-candidate-seed.json', seed_reviewer(app, tasks['reviewer'].session_id, candidate))
        # The reviewer receives exactly the same frozen pages and real read credits.
        write(here / 'reviewer-inherited-scope.json', {
            'enabled_library_ids': sorted(lib['id'] for lib in store.libraries() if lib['enabled']),
            'scope_origin': 'Exact B enabled scope; reviewer may add enabled libraries, cannot disable.'})
        write(here / 'reviewer-preload-receipts.json', preload_pack(app, tasks['reviewer'].session_id, pack))
        reviewer_initial = app.sessions.get(tasks['reviewer'].session_id)
        r = execute('reviewer', tasks['reviewer'], REVIEW_PREFIX + pack['text'], reviewer=True, seeded_candidate=True)
        if r['status'] != 'complete':
            skipped('reviewed', 'Independent review did not complete with bounded visible feedback.')
            return finish()
        reviewer_final = app.sessions.get(tasks['reviewer'].session_id)
        if candidate_fields(reviewer_initial) != candidate_fields(reviewer_final) or reviewer_initial['revision'] != reviewer_final['revision']:
            raise ProductRunError('reviewer_modified_candidate', 'Independent reviewer changed the candidate')
        feedback = (tasks['reviewer'].destination / 'review.txt').read_text(encoding='utf-8')
        continuation = SimpleNamespace(**vars(tasks['curated']))
        continuation.condition, continuation.destination = 'reviewed', runs / ('reviewed-' + CASE)
        continuation.audit_path = here / 'audits' / ('reviewed-' + CASE + '.jsonl')
        batch.AUDITS[('evidence-ablation', continuation.session_id)] = continuation.audit_path
        continuation.expected_thread_id = b['conversation_id']
        continuation.expected_session_sha256 = b['final_continuation_snapshot_sha256']
        require_candidate_unchanged(app, continuation.session_id, curated_candidate_sha256)
        write(here / 'revision-input-freeze.json', {'review_sha256': digest(feedback),
            'curated_session_sha256': b['final_session_sha256'], 'curated_plan_sha256': b['plan_sha256'],
            'curated_continuation_snapshot_sha256': b['final_continuation_snapshot_sha256'],
            'curated_raw_candidate_sha256': curated_candidate_sha256,
            'revision_message_sha256': digest(REVISION_PREFIX + feedback), 'frozen_at': now()})
        c = execute('reviewed', continuation, REVISION_PREFIX + feedback, fresh=False)
        return finish()
    except BaseException as exc:
        state.update(status='cancelled' if isinstance(exc, _Cancelled) else 'failed', failure_type=type(exc).__name__)
        progress()
        write(here / 'infrastructure-error.json', {'type': type(exc).__name__, 'detail': str(exc), 'traceback': traceback.format_exc()})
        raise
    finally:
        stop.set()
        cleanup = {'timestamp': now(), 'scope_restored': False, 'owned_services_stopped': False}
        errors = []
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
            for enabled in (False, True):
                ids = [key for key, value in before.items() if value is enabled]
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
