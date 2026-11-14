"""One HTML-manual-tree route arm through the real product Agent CLI.

Only the three original public inputs are prepared. No method cards, source-case
candidate list, private answers, scoring criteria or historical replies are sent
to the model. Each task has one independent natural turn and no automatic retry.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from types import SimpleNamespace

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from scripts import benchmark_route_selection as base
from scripts import benchmark_product_batch as batch
from scripts.benchmark_backend_policy import install_backend_policy, policy_description
from scripts.benchmark_product_setup import create_task, _public_file
from scripts.build_case_methods import link_library
from advisor.tool_delivery_audit import audit_rollout, locate_rollout, response_sha256

MODEL, EFFORT = 'gpt-6-luna', 'high'
CASES = ('case-01', 'case-02', 'case-09')
CONDITION = 'html_tree'
HTML_TOOLS = frozenset({'get_html_navigation', 'read_html_section'})
READ_TOOLS = (base.READ_TOOLS - {'get_case_method'}) | HTML_TOOLS | {'read_tool_page'}
RUN_ID = 'luna6-high-html-tree-20261114'
PROFILE = dict(batch.PROFILE, **{'features.code_mode': True, 'features.code_mode_host': True})
PAGING_INSTRUCTIONS = '''
本次工具回包为小页。HTML功能手册树只作发现；get_html_navigation不授予正文阅读，read_html_section读取原始HTML章节。可用QMD返回的source_id反查树，再核对父接口、适用条件和相关章节。
next_offset只表示当前工具/当前章节的续读位置，null不表示整章上下文或整本手册已读。按实际需要查看children、siblings、父节点和跨页说明，选择性读相关原文，不强制遍历整本。原文和配置的实际引用范围保持原样；未读范围不能称已核对。
若一般工具结果含handle/next_offset/next_cursor/has_more，使用read_tool_page或原工具的分页入口继续。HTML两工具自身已返回完整有界语义页，不把其字段再拆成JSON字符碎片。read_source的有效length默认1000、最大1200字符，以当前工具契约为准。
每个exec单元只能调用一次业务工具，并只输出该次解码后的完整小页结果。禁止Promise.all、循环调用工具或合并多个结果输出。工具可能返回JSON字符串，应先JSON.parse解码再访问字段；例如const raw=await tools.get_task_context({}); const value=typeof raw==='string'?JSON.parse(raw):raw; text(value)。不要拼接分页全文或重复打印。传输审计发现批量调用、超长输出或截断将判本次运行无效。
资料库范围已固定，所有写操作与切库均禁止。not_prepared表示当前HTML树入口不可用，可继续用既有QMD与原文读取入口，不能据此推断没有相关方法。
'''
MESSAGE = '''先用get_task_context完整阅读公开需求及几何/模型事实，必要时按read_tool_page的next_offset读完。此轮只形成COMSOL技术路线判断，不保存方案。
先查看get_html_navigation的HTML功能手册树；结合QMD定位可选接口与相关章节，使用read_html_section读取适用条件和候选方法比较的原文。可用已发现来源的source_id返回原生树，按需查看父接口和相邻章节。树或章节不可用时继续检索原始资料。导航不保证适用，不算正文已读，也不要求遍历整本手册。
比较合理候选方法：它们各自保留哪些现象、依赖哪些假设和输入，当前满足、冲突或未知的条件是什么，为什么选择或排除。对首选路线明确核心未知量与几何解释、材料关系、激励/边界形式以及跨域/接口量的传递关系；允许条件路线，缺证据须指出。无需完整网格、求解器或菜单步骤，不声称已求解。
最终回复一个JSON对象，含chosen_route、alternatives、applicability_conditions、unresolved、sources。说明必要的路线关系，不为追求短回复省掉关键耦合。不要调用save_plan或修改步骤、设置及资料库范围。'''
CORE_PATHS = tuple(dict.fromkeys((*base.CORE_PATHS, 'advisor/html_tree.py', 'advisor/html_tree_tools.py',
    'advisor/bounded_knowledge.py', 'advisor/tool_delivery_audit.py', 'scripts/build_html_tree.py',
    'scripts/benchmark_html_tree.py')))


def emit(event, **values):
    print(json.dumps({'event': event, **values}, ensure_ascii=False), file=sys.stderr, flush=True)


def visible_response_sha256(value):
    if isinstance(value, dict) and 'success' in value and 'contentItems' in value:
        items = value['contentItems']
        if len(items) != 1 or items[0].get('type') != 'inputText':
            raise ValueError('One complete JSON tool payload is required')
        value = json.loads(items[0]['text'])
    return response_sha256(value)


def effective_definitions(definitions=None):
    """One effective contract for runtime registration, validation and queries."""
    from advisor.agent_sessions import planning_tools
    from advisor.bounded_knowledge import BoundedKnowledge
    from advisor.tool_contracts import describe_contract
    values = copy.deepcopy(planning_tools() if definitions is None else definitions)
    by_name = {tool['name']: tool for tool in values if tool['name'] in READ_TOOLS}
    by_name.update({tool['name']: tool for tool in BoundedKnowledge(lambda *_: {}).tool_definitions()})
    if not HTML_TOOLS.issubset(by_name):
        raise ValueError('The production HTML tree tools have not been registered')
    for name in ('read_source', 'get_tool_contract'):
        definition = by_name[name]
        definition['inputSchema']['properties']['length'].update(maximum=1200, default=1000 if name == 'read_source' else 1200)
        definition['description'] = definition['description'].split('\n输入契约', 1)[0]
        definition['description'] += '\n本实验的有效length上限为1200字符，分页与服务校验同源。'
        definition['description'] = describe_contract(definition)
    return [by_name[name] for name in sorted(by_name)]


class HtmlRuntime:
    """Replace the runtime seam with precisely the effective read-only schemas."""
    def __init__(self, runtime):
        self.runtime = runtime
    def __getattr__(self, name):
        return getattr(self.runtime, name)
    def start(self, thread_id=None, tools=None, instructions=''):
        return self.runtime.start(thread_id=thread_id, tools=effective_definitions(tools),
                                  instructions=instructions + PAGING_INSTRUCTIONS)


def install_tools(app, sessions, scope, audit_paths):
    from advisor.bounded_knowledge import BoundedKnowledge
    from advisor.plan_drafts import schema_errors
    from advisor.tool_contracts import read_contract
    definitions = effective_definitions()
    by_name = {tool['name']: tool for tool in definitions}
    original, pagers, lock = app.agents.runtime_tool, {}, threading.RLock()
    scope = frozenset(scope)

    def authorize(name, arguments):
        if frozenset(app.kb.store.searchable_library_ids()) != scope:
            raise PermissionError('The frozen library scope changed')
        identity = arguments.get('id') if name == 'read_source' else arguments.get('case_id') if name == 'read_case_configuration' else None
        if identity:
            app.kb.store.require_not_heldout(identity)
            if not app.kb.store.source_available(identity):
                raise PermissionError('The source is disabled, deleted, unavailable or held out')

    def raw(sid, name, arguments):
        if name == 'get_tool_contract':
            return read_contract(definitions, arguments)
        value = original(sid, name, arguments, source_entry=None)
        if isinstance(value, dict) and value.get('success') is False and 'contentItems' in value:
            # Do not let the source projection hide a transport feedback body.
            items = value['contentItems']
            message = items[0].get('text') if items and isinstance(items[0], dict) else 'The production tool rejected this read'
            raise ValueError(message)
        return value

    def guarded(sid, name, arguments, *, source_entry=None):
        began = time.monotonic()
        row = {'recorded_at': base.now(), 'session_id': sid, 'condition': CONDITION,
               'tool': name, 'arguments': arguments}
        try:
            if sid not in sessions:
                raise PermissionError('This task is outside the experiment')
            app.sessions.get(sid)
            if source_entry is not None:
                app.agents._require_current_entry(sid, source_entry)
            if name not in by_name:
                raise PermissionError('Only this experiment\'s read tools are permitted; writes and library changes are disabled')
            errors = schema_errors(arguments, by_name[name]['inputSchema'])
            if errors:
                raise ValueError('Arguments differ from the effective tool contract: ' + json.dumps(errors, ensure_ascii=False))
            authorize(name, arguments)
            arguments = copy.deepcopy(arguments)
            if name in {'read_source', 'get_tool_contract'}:
                arguments.setdefault('length', 1000 if name == 'read_source' else 1200)
            row['effective_arguments'] = arguments
            if name in HTML_TOOLS:
                # Production HTML tools do live source/root/hash/holdout checks
                # and already build semantic pages within the common byte cap.
                result = raw(sid, name, arguments)
            else:
                with lock:
                    if sid not in pagers:
                        pagers[sid] = BoundedKnowledge(lambda n, a, task=sid: raw(task, n, a),
                                                       max_bytes=7000, authorize=authorize)
                    pager = pagers[sid]
                result = pager.call(name, arguments)
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(encoded) > 7000:
                raise ValueError('Tool result exceeded the 7000-byte effective delivery contract')
            row.update(success=not isinstance(result, dict) or result.get('success') is not False,
                       result_json_bytes=len(encoded), response_sha256=visible_response_sha256(result))
            if isinstance(result, dict):
                entries = result.get('results', result.get('items', result.get('matches', [])))
                row.update(result_keys=sorted(result), result_state=result.get('state'),
                           evidence_refs=result.get('evidence_refs'),
                           next_offset=result.get('next_offset'),
                           has_more=result.get('has_more'), returned=result.get('returned'),
                           result_ids=[item.get('id', item.get('node_id')) for item in entries if isinstance(item, dict)]
                                      if isinstance(entries, list) else [])
            return result
        except (ValueError, PermissionError, FileNotFoundError, RuntimeError) as error:
            result = base._feedback('html_tree_tool_rejected', str(error)[:700])
            row.update(success=False, error_type=type(error).__name__, error=str(error)[:700],
                       response_sha256=visible_response_sha256(result))
            return result
        finally:
            row['seconds'] = round(time.monotonic() - began, 4)
            if sid in audit_paths:
                with lock:
                    path = audit_paths[sid]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    app.agents.runtime_tool = guarded
    return definitions


def tree_snapshot(tree_root):
    supplied = Path(tree_root).expanduser()
    if supplied.is_symlink() or getattr(supplied, 'is_junction', lambda: False)():
        raise ValueError('The frozen HTML tree must not be a filesystem alias')
    supplied = supplied.resolve(strict=True)
    directory = supplied if supplied.is_dir() else supplied.parent
    index, manifest = directory / 'index.sqlite3', directory / 'manifest.json'
    if supplied.is_file() and supplied != index:
        raise ValueError('--tree-root must be the index directory or its index.sqlite3')
    for path in (index, manifest):
        if (not path.is_file() or path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
                or path.stat().st_nlink != 1):
            raise ValueError('The HTML tree requires regular unaliased index.sqlite3 and manifest.json')
    value = json.loads(manifest.read_text(encoding='utf-8'))
    if (value.get('schema') != 'html-manual-tree/v1' or value.get('ready') is not True
            or value.get('index_file') != 'index.sqlite3' or value.get('index_sha256') != batch.sha(index)
            or not value.get('library_id') or not value.get('root_id') or not value.get('node_count')):
        raise ValueError('HTML tree manifest is incomplete or its index hash differs')
    return {'directory': str(directory), 'index_path': str(index), 'manifest': value,
            'files': {'index.sqlite3': batch.sha(index), 'manifest.json': batch.sha(manifest)}}


def historical_baselines(paths):
    """Only opaque hashes and paths; historical replies are never model input."""
    return [{'path': str(Path(path).resolve(strict=True)), 'sha256': batch.sha(path)} for path in paths]


def source_hashes():
    return {name: batch.sha(APP / name) for name in CORE_PATHS}


def verify_public_inputs(root, manifests):
    for manifest in manifests.values():
        for item in manifest['files']:
            path = _public_file(root, root / item['path'])
            if batch.sha(path) != item['sha256']:
                raise ValueError('An original public input changed during the run')


def inspect_delivery(task, result):
    if not result.get('conversation_id'):
        return {'valid': False, 'issues': [{'code': 'thread_identity_missing'}]}
    try:
        expected = [json.loads(line)['response_sha256'] for line in task.audit_path.read_text(encoding='utf-8').splitlines()]
        path = locate_rollout(result['conversation_id'])
        return audit_rollout(path, result['conversation_id'], expected_response_sha256=expected)
    except Exception as error:
        return {'valid': False, 'issues': [{'code': type(error).__name__, 'detail': str(error)}]}


def run_cli_turn(task, message, *, cancel_event=None, runner=None, http_reader=None):
    """Explicitly opt the new arm into the shared real-CLI lifecycle guard."""
    return base.run_turn(task, message, cancel_event=cancel_event, runner=runner,
                         http_reader=http_reader, allowed_conditions=(CONDITION,))


def run_tasks(tasks, stop, state, progress, lock, *, run_turn=None, delivery_check=None):
    """An unsuccessful task never cancels its siblings or selects a new attempt."""
    run_turn, delivery_check = run_turn or run_cli_turn, delivery_check or inspect_delivery
    def execute(task):
        key = task.condition + '-' + task.case
        with lock:
            state['active'].append(key)
            state['status'] = 'running'
            progress()
        emit('case_started', case_id=task.case, condition=task.condition)
        try:
            try:
                result = run_turn(task, MESSAGE, cancel_event=stop)
            except Exception as error:
                result = {'case_id': task.case, 'condition': task.condition, 'status': 'incomplete',
                          'failure_code': 'case_execution_exception', 'error_type': type(error).__name__,
                          'natural_termination': False}
            delivery = delivery_check(task, result)
            task.destination.mkdir(parents=True, exist_ok=True)
            batch.write(task.destination / 'delivery-audit.json', delivery)
            result['delivery_valid'] = delivery.get('valid') is True
            if result.get('status') == 'complete' and not result['delivery_valid']:
                result.update(status='invalid_delivery', failure_code='actual_tool_output_not_verified')
            batch.write(task.destination / 'run.json', result)
            row = {name: result.get(name) for name in ('case_id', 'condition', 'status', 'seconds',
                   'conversation_id', 'natural_termination', 'failure_code', 'delivery_valid')}
            with lock:
                state['completed'].append(row)
            emit('case_completed', **row)
            return result
        finally:
            with lock:
                state['active'].remove(key)
                progress()
    with ThreadPoolExecutor(max_workers=state['parallel_limit']) as executor:
        futures = [executor.submit(execute, task) for task in tasks]
        results = []
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except BaseException:
                # Failure to persist/audit progress is shared infrastructure,
                # unlike an ordinary model/tool failure returned by run_turn.
                stop.set()
                raise
    return results


def open_store(data_root):
    from advisor.library_store import LibraryStore
    return LibraryStore(data_root / 'libraries')


def embedding_enabled(data_root):
    from advisor.retrieval_settings import read
    return read(data_root / 'libraries/search')['enabled']


def create_application(args, here):
    from advisor.server import App
    from advisor.config import codex_command
    from advisor.runtime_adapters import CodexRuntimeFactory, CodexDebugRuntime, runtime_command
    from scripts.benchmark_native_cli import project_mcp_disables
    class Factory(CodexRuntimeFactory):
        def __call__(self, *, cwd, on_event, on_tool, model=MODEL, reasoning_effort=EFFORT):
            if model != MODEL or reasoning_effort != EFFORT:
                raise ValueError('This protocol fixes gpt-6-luna/high')
            inherited, _ = project_mcp_disables(Path(cwd))
            command = runtime_command(codex_command()) + batch.flags(dict(PROFILE, **inherited)) + ['--strict-config']
            return HtmlRuntime(CodexDebugRuntime(command=command, cwd=cwd, on_event=on_event,
                                                on_tool=on_tool, model=model, reasoning_effort=reasoning_effort))
    data, docs = here / 'runtime/product', here / 'runtime/empty-documents'
    data.mkdir(parents=True)
    docs.mkdir(parents=True)
    link_library(data / 'libraries', args.data_root / 'libraries')
    return App(data_dir=data, doc_root=docs, runtime_factory=Factory())


def start_service(app):
    from advisor.server import make_server
    server = make_server(app, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_port}'


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--tree-root', type=Path, required=True)
    result.add_argument('--run-id', default=RUN_ID)
    result.add_argument('--benchmark-root', type=Path, default=batch.ROOT)
    result.add_argument('--data-root', type=Path, default=batch.DATA)
    result.add_argument('--offline-root', type=Path, default=batch.OFFLINE)
    result.add_argument('--scope-library-id', action='append', default=[])
    result.add_argument('--historical-baseline', type=Path, action='append', default=[])
    result.add_argument('--parallel', type=int, choices=(1, 2), default=2)
    result.add_argument('--timeout', type=float, default=7200)
    result.add_argument('--poll-interval', type=float, default=1.0)
    result.add_argument('--model', choices=(MODEL,), default=MODEL)
    result.add_argument('--prepare-only', action='store_true')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', args.run_id)
            or args.timeout <= 0 or args.poll_interval <= 0):
        raise ValueError('A simple fresh run ID and positive timeout/poll interval are required')
    if len(args.scope_library_id) != len(set(args.scope_library_id)):
        raise ValueError('Frozen library IDs must be unique')
    tree = tree_snapshot(args.tree_root)
    baselines = historical_baselines(args.historical_baseline)
    root = args.benchmark_root.resolve()
    here, runs = root / 'private/evaluations' / args.run_id, root / 'runs' / args.run_id
    for directory in (here, runs):
        if directory.exists() and any(directory.iterdir()):
            raise FileExistsError('Historical attempts are never overwritten or resumed')
    here.mkdir(parents=True, exist_ok=True)
    env = {'SIM_ADVISOR_NODE': str(args.offline_root / 'node/node.exe'),
           'SIM_ADVISOR_QMD_DIR': str(args.offline_root / 'qmd/node_modules/@tobilu/qmd'),
           'SIM_ADVISOR_SEARCH_CACHE': str(here / 'runtime/search-cache'),
           'SIM_ADVISOR_HTML_TREE_INDEX': tree['index_path'],
           'TEMP': str(here / 'runtime/tmp'), 'TMP': str(here / 'runtime/tmp'), 'PYTHONIOENCODING': 'utf-8'}
    previous_env = {key: os.environ.get(key) for key in env}
    (here / 'runtime/tmp').mkdir(parents=True)
    os.environ.update(env)
    stop, lock = threading.Event(), threading.RLock()
    state = {'schema': 'html-tree-route-progress/v1', 'status': 'preparing', 'model': MODEL, 'effort': EFFORT,
             'expected_runs': 3, 'parallel_limit': args.parallel, 'active': [], 'completed': []}
    def progress():
        state['updated_at'] = base.now()
        batch.write(here / 'progress.json', state)
    app = server = store = before = None
    progress()
    try:
        store = open_store(args.data_root)
        if embedding_enabled(args.data_root) or store.evaluation_set_summaries()['retrieval_enabled']:
            raise ValueError('Embedding and held-out retrieval must already be disabled')
        before = {item['id']: item['enabled'] for item in store.libraries(include_deleted=True) if not item.get('deleted_at')}
        batch.write(here / 'scope-before.json', before)
        if set(args.scope_library_id) - set(before):
            raise ValueError('Unknown/deleted library in the shared scope')
        additions = [key for key in args.scope_library_id if not before[key]]
        if additions:
            store.set_enabled_many(additions, True)
        scope = sorted(key for key, enabled in before.items() if enabled or key in additions)
        if tree['manifest']['library_id'] not in scope:
            raise PermissionError('The HTML manual library is outside the frozen live scope')
        batch.write(here / 'scope-frozen.json', {'enabled_library_ids': scope, 'added': additions})
        app = create_application(args, here)
        audits, sessions = {}, set()
        def search_audit(row):
            with lock:
                with (here / 'backend-policy-audits.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        install_backend_policy(app.tools, 'qmd_keyword', on_search=search_audit)
        definitions = install_tools(app, sessions, scope, audits)
        server, url = start_service(app)
        batch.write(here / 'services.json', {'pid': os.getpid(), 'url': url})
        batch.write(here / 'runtime-tools.json', definitions)
        from advisor.agent_sessions import INSTRUCTIONS
        (here / 'product-instructions.txt').write_text(INSTRUCTIONS + PAGING_INSTRUCTIONS, encoding='utf-8')
        tasks, manifests = [], {}
        for cid in CASES:
            key = CONDITION + '-' + cid
            prepared = create_task(url, root, cid)
            sid = prepared['session']['id']
            sessions.add(sid)
            audits[sid] = here / 'audits' / (key + '.jsonl')
            context = app.tools.call(sid, 'get_task_context', {})
            manifests[key] = prepared['input_manifest']
            batch.write(here / 'prepared' / key / 'context-preflight.json', context)
            batch.write(here / 'prepared' / key / 'input-manifest.json', prepared['input_manifest'])
            tasks.append(SimpleNamespace(url=url, session_id=sid, case=cid, condition=CONDITION,
                         destination=runs / key, model=MODEL, timeout=args.timeout,
                         poll_interval=args.poll_interval, audit_path=audits[sid]))
        navigation = app.tools.call(tasks[0].session_id, 'get_html_navigation', {'limit': 1})
        batch.write(here / 'html-tree-preflight.json', navigation)
        if navigation.get('state') != 'ready':
            raise ValueError('The production HTML tree interface is not ready in the frozen scope')
        frozen_sources = source_hashes()
        verify_public_inputs(root, manifests)
        protocol = {'schema': 'html-tree-route-protocol/v1', 'cases': CASES, 'conditions': [CONDITION],
            'model': MODEL, 'effort': EFFORT, 'parallel_limit': args.parallel, 'timeout_seconds': args.timeout,
            'runtime_profile': PROFILE, 'tool_transport': 'bounded_code_mode_single_call',
            'production_instructions_base_unchanged': True, 'paging_instructions': PAGING_INSTRUCTIONS,
            'route_message': MESSAGE, 'message_sha256': base.digest(MESSAGE), 'tree_snapshot': tree,
            'source_sha256': frozen_sources, 'public_input_manifest': manifests, 'enabled_libraries': scope,
            'search_backend': policy_description('qmd_keyword'), 'historical_baselines': baselines,
            'historical_baselines_supplied_to_model': False, 'private_grading_read': False,
            'test_answers_supplied': False, 'original_route_case_tutorials_supplied': False,
            'method_cards_exposed': False, 'embedding_enabled': False, 'index_rebuild_performed': False,
            'solver_run': False, 'attempts_per_case': 1, 'automatic_retries': 0,
            'output_budget_bytes': 7000, 'actual_tool_output_audit_budget_bytes': 8192,
            'execution_order': [CONDITION + '-' + cid for cid in CASES],
            'failure_scope': 'A task failure does not stop siblings. Only user stop.request or fatal shared infrastructure can stop remaining work.',
            'limitations': ['One new HTML-tree arm only; historical arms are not rerun.',
                            'Three tasks, one attempt each; differences do not establish statistical significance.']}
        batch.write(here / 'protocol.json', protocol)
        if args.prepare_only:
            state.update(status='preflight_complete')
            progress()
            emit('preflight_complete', run_id=args.run_id, tasks=3, model_launched=False)
            return 0
        def monitor():
            last_notice = time.monotonic()
            while not stop.wait(0.5):
                if (here / 'stop.request').exists():
                    emit('user_stop_requested', run_id=args.run_id)
                    stop.set()
                if time.monotonic() - last_notice >= 30:
                    with lock:
                        emit('running', active=list(state['active']), completed=len(state['completed']))
                    last_notice = time.monotonic()
        if (here / 'stop.request').exists():
            stop.set()
        threading.Thread(target=monitor, daemon=True).start()
        results = run_tasks(tasks, stop, state, progress, lock)
        if tree_snapshot(args.tree_root)['files'] != tree['files'] or source_hashes() != frozen_sources:
            raise ValueError('Frozen HTML index/manifest or source code changed during inference')
        verify_public_inputs(root, manifests)
        failed = len(results) != 3 or any(row.get('status') != 'complete' or not row.get('delivery_valid') for row in results)
        state.update(status='completed_with_failures' if failed else 'completed', frozen_hashes_unchanged=True)
        progress()
        return 2 if failed else 0
    except BaseException as error:
        stop.set()
        state.update(status='failed', error_type=type(error).__name__, detail=str(error))
        progress()
        batch.write(here / 'infrastructure-error.json', {'error_type': type(error).__name__, 'detail': str(error)})
        emit('infrastructure_failed', error_type=type(error).__name__, detail=str(error))
        raise
    finally:
        stop.set()
        cleanup = {'scope_restored': before is None, 'owned_services_stopped': False, 'environment_restored': False, 'errors': []}
        for action, target in (('agents_close', app.agents.close if app is not None else None),
                               ('server_shutdown', server.shutdown if server is not None else None),
                               ('server_close', server.server_close if server is not None else None)):
            if target is not None:
                try:
                    target()
                except Exception as error:
                    cleanup['errors'].append({'action': action, 'error_type': type(error).__name__})
        cleanup['owned_services_stopped'] = not cleanup['errors']
        if store is not None and before is not None:
            try:
                actual = {item['id']: item['enabled'] for item in store.libraries(include_deleted=True) if item['id'] in before}
                for enabled in (False, True):
                    changed = [key for key, value in before.items() if value is enabled and actual.get(key) != value]
                    if changed:
                        store.set_enabled_many(changed, enabled)
                cleanup['scope_restored'] = {item['id']: item['enabled'] for item in store.libraries(include_deleted=True) if item['id'] in before} == before
            except Exception as error:
                cleanup['errors'].append({'action': 'scope_restore', 'error_type': type(error).__name__})
        for key, previous in previous_env.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        cleanup['environment_restored'] = all(os.environ.get(key) == value for key, value in previous_env.items())
        batch.write(here / 'cleanup.json', cleanup)
        emit('cleanup_completed', **cleanup)
        if not cleanup['scope_restored'] or not cleanup['owned_services_stopped'] or not cleanup['environment_restored']:
            raise RuntimeError('Cleanup could not restore the experiment scope, services or environment')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
