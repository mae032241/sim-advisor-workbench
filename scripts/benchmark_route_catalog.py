"""Paired route-catalog pilot through the product CLI with bounded tool pages.

This runner opens public tasks only. Card construction is a separate source-only
process. Original business schemas and planning instructions are retained, with
the same paging/route-output guidance added in both arms. Delivery warnings make
a run invalid rather than a negative model-quality result.
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
import traceback
from types import SimpleNamespace

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from scripts import benchmark_route_selection as base
from scripts import benchmark_product_batch as batch
from scripts.benchmark_backend_policy import install_backend_policy, policy_description
from scripts.benchmark_product_setup import create_task
from scripts.build_case_methods import link_library
from advisor.tool_delivery_audit import audit_rollout, locate_rollout, response_sha256

MODEL, EFFORT = 'gpt-6-luna', 'high'
CASES, CONDITIONS = base.CASES, base.CONDITIONS
RUN_ID = 'luna6-high-route-catalog-pilot-20261113'
NATIVE_PROFILE = dict(batch.PROFILE, **{'features.code_mode': True, 'features.code_mode_host': True})
PAGING_INSTRUCTIONS = '''
本次工具回包为小页。目录只作发现，方法卡按section读取，原始证据按需读取。
若结果含handle/next_offset/next_cursor/has_more，请用read_tool_page或原工具的分页入口继续；第一页、局部属性和结构摘要不代表全文或所有属性已读。
每个exec单元只能调用一次业务工具，并只输出该次的小页结果。禁止Promise.all、循环调用工具或合并多个结果输出。工具可能返回JSON字符串，应先JSON.parse解码再访问字段；例如const raw=await tools.get_task_context({}); const value=typeof raw==='string'?JSON.parse(raw):raw; text(value)。不要批量请求整库，不要拼接分页全文。传输审计发现批量调用、超长输出或截断将判本次运行无效。
方法卡是带来源的解释导航，引用具体设置前仍应读取原文/配置。not_prepared表示没有这层预构造卡，原始资料仍可用search_cases/search_knowledge/read_source/read_case_configuration查询。
'''
MESSAGE = '''先用get_task_context完整阅读公开需求及几何/模型事实，必要时按read_tool_page的next_offset读完。此轮只形成COMSOL技术路线判断，不保存方案。
先查看list_route_methods短目录；按方法目标与当前已知条件选择少量相关卡，使用read_method_card按section展开。目录或卡不可用时继续检索原始资料。卡不保证适用，不强制复制案例，也不代替正文证据。
比较合理候选方法：它们各自保留哪些现象、依赖哪些假设和输入，当前满足、冲突或未知的条件是什么，为什么选择或排除。对首选路线明确核心未知量与几何解释、材料关系、激励/边界形式以及跨域/接口量的传递关系；允许条件路线，缺证据须指出。无需完整网格、求解器或菜单步骤，不声称已求解。
最终回复一个JSON对象，含chosen_route、alternatives、applicability_conditions、unresolved、sources。说明必要的路线关系，不为追求短回复省掉关键耦合。不要调用save_plan或修改步骤、设置及资料库范围。'''
CORE_PATHS = tuple(dict.fromkeys((*base.CORE_PATHS, 'advisor/route_cards.py',
    'advisor/bounded_knowledge.py', 'advisor/tool_delivery_audit.py',
    'scripts/build_route_cards.py', 'scripts/benchmark_route_catalog.py')))


def hashes(root):
    directory = Path(root) / 'route-method-cards'
    paths = sorted(directory.glob('*.json'))
    if not paths or not (directory / 'manifest.json').is_file():
        raise ValueError('A frozen route-method catalog is required')
    return {path.relative_to(root).as_posix(): batch.sha(path) for path in paths}


def visible_response_sha256(value):
    """CodexRuntime unwraps RPC contentItems before the model receives them."""
    if isinstance(value, dict) and 'success' in value and 'contentItems' in value:
        items = value['contentItems']
        if len(items) != 1 or items[0].get('type') != 'inputText':
            raise ValueError('A single JSON tool payload is required for delivery verification')
        value = json.loads(items[0]['text'])
    return response_sha256(value)


def install_tools(app, cards, extra_tools, conditions, scope, audit_paths):
    from advisor.agent_sessions import planning_tools
    from advisor.bounded_knowledge import BoundedKnowledge
    from advisor.plan_drafts import schema_errors
    from advisor.tool_contracts import read_contract
    definitions = planning_tools() + copy.deepcopy(extra_tools)
    by_name = {tool['name']: tool for tool in definitions}
    original, pagers, lock = app.agents.runtime_tool, {}, threading.RLock()
    allowed = (base.READ_TOOLS - {'get_case_method'}) | {'list_route_methods', 'read_method_card', 'read_tool_page'}
    scope = frozenset(scope)

    def authorize(name, arguments):
        # Cached pages cannot outlive a library scope change or source denial.
        if frozenset(app.kb.store.searchable_library_ids()) != scope:
            raise PermissionError('Frozen library scope changed; requery after restoring the scope')
        identity = arguments.get('id') if name == 'read_source' else arguments.get('case_id') if name == 'read_case_configuration' else None
        if identity:
            app.kb.store.require_not_heldout(identity)
            if not app.kb.store.source_available(identity):
                raise PermissionError('The source is disabled, deleted, unavailable or held out')

    def raw(sid, name, arguments):
        if name == 'get_tool_contract':
            return read_contract(definitions, arguments)
        if name == 'list_route_methods':
            return cards.list_methods(**arguments, enabled=conditions[sid] == 'case_methods')
        if name == 'read_method_card':
            return cards.read_method(**arguments, enabled=conditions[sid] == 'case_methods')
        # The guard has already checked the current runtime entry for this call.
        return original(sid, name, arguments, source_entry=None)

    def guarded(sid, name, arguments, *, source_entry=None):
        import time
        start = time.monotonic()
        row = {'recorded_at': base.now(), 'session_id': sid, 'condition': conditions.get(sid),
               'tool': name, 'arguments': arguments}
        result = None
        try:
            if sid not in conditions:
                raise PermissionError('This task is outside the experiment')
            app.sessions.get(sid)
            if source_entry is not None:
                app.agents._require_current_entry(sid, source_entry)
            if name not in by_name:
                raise PermissionError('This tool is not registered in the experiment')
            if name == 'set_libraries_enabled':
                errors = schema_errors(arguments, by_name[name]['inputSchema'])
                if errors:
                    raise ValueError('Library scope arguments differ from the published contract')
                if arguments.get('enabled') is True and set(arguments.get('library_ids', [])).issubset(scope):
                    result = {'enabled': True, 'changed': 0, 'scope_frozen': True, 'library_ids': arguments['library_ids']}
                else:
                    raise PermissionError('The common library scope is frozen')
            elif name not in allowed:
                raise PermissionError('This is a read-only route experiment')
            else:
                errors = schema_errors(arguments, by_name[name]['inputSchema'])
                if errors:
                    raise ValueError('Arguments differ from the published contract: ' + json.dumps(errors, ensure_ascii=False))
                with lock:
                    if sid not in pagers:
                        pagers[sid] = BoundedKnowledge(lambda n, a, task=sid: raw(task, n, a),
                                                       max_bytes=7000, authorize=authorize)
                    pager = pagers[sid]
                result = pager.call(name, arguments)
            row['success'] = not isinstance(result, dict) or result.get('success') is not False
            if isinstance(result, dict):
                row.update(result_keys=sorted(result), result_state=result.get('state'),
                           evidence_refs=result.get('evidence_refs'),
                           result_ids=[item.get('id', item.get('method_id')) for item in result.get('results', result.get('methods', []))
                                       if isinstance(item, dict)])
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
            row.update(result_json_bytes=len(encoded), result_sha256=hashlib.sha256(encoded).hexdigest())
            row['response_sha256'] = visible_response_sha256(result)
            if len(encoded) > 7000:
                raise ValueError('Bounded tool output exceeded its delivery budget')
            return result
        except (ValueError, PermissionError, FileNotFoundError) as error:
            row.update(success=False, error_type=type(error).__name__, error=str(error)[:700])
            result = base._feedback('route_tool_rejected', str(error)[:700])
            row['response_sha256'] = visible_response_sha256(result)
            return result
        finally:
            row['seconds'] = round(time.monotonic() - start, 4)
            if sid in audit_paths:
                with lock:
                    path = audit_paths[sid]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    app.agents.runtime_tool = guarded
    return definitions


def parser():
    result = base.parser()
    result.description = __doc__
    result.set_defaults(run_id=RUN_ID)
    result.add_argument('--condition', action='append', choices=CONDITIONS,
                        help='Run only the selected condition; repeat for both arms.')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    selected_conditions = args.condition or list(CONDITIONS)
    if len(set(selected_conditions)) != len(selected_conditions):
        raise ValueError('Each condition may be selected only once')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', args.run_id) or args.timeout <= 0:
        raise ValueError('A simple new run ID and positive timeout are required')
    candidates, candidate_hash = base.load_candidates(args.candidate_list)
    card_hashes = hashes(args.method_root)
    root = args.benchmark_root.resolve()
    here, runs = root / 'private/evaluations' / args.run_id, root / 'runs' / args.run_id
    for directory in (here, runs):
        if directory.exists() and any(directory.iterdir()):
            raise FileExistsError('Historical attempts are never overwritten')
    here.mkdir(parents=True, exist_ok=True)
    runtime = here / 'runtime'
    (runtime / 'tmp').mkdir(parents=True)
    os.environ.update(SIM_ADVISOR_NODE=str(args.offline_root / 'node/node.exe'),
                      SIM_ADVISOR_QMD_DIR=str(args.offline_root / 'qmd/node_modules/@tobilu/qmd'),
                      SIM_ADVISOR_SEARCH_CACHE=str(runtime / 'search-cache'),
                      TEMP=str(runtime / 'tmp'), TMP=str(runtime / 'tmp'), PYTHONIOENCODING='utf-8')
    from advisor.server import App, make_server
    from advisor.config import codex_command
    from advisor.agent_sessions import INSTRUCTIONS, planning_tools
    from advisor.route_cards import RouteCardStore, tool_definitions as card_definitions
    from advisor.bounded_knowledge import BoundedKnowledge
    from advisor.runtime_adapters import CodexRuntimeFactory, CodexDebugRuntime, runtime_command
    from advisor.library_store import LibraryStore
    from advisor.retrieval_settings import read as embedding_settings
    from scripts.benchmark_native_cli import project_mcp_disables
    extra = card_definitions() + BoundedKnowledge(lambda *_: {}).tool_definitions()

    class NativeFactory(CodexRuntimeFactory):
        def __call__(self, *, cwd, on_event, on_tool, model=args.model, reasoning_effort=EFFORT):
            inherited, _ = project_mcp_disables(Path(cwd))
            command = runtime_command(codex_command()) + batch.flags(dict(NATIVE_PROFILE, **inherited)) + ['--strict-config']
            underlying = CodexDebugRuntime(command=command, cwd=cwd, on_event=on_event,
                                          on_tool=on_tool, model=model, reasoning_effort=reasoning_effort)
            class NativeExtended(base.ExtendedRuntime):
                def start(self, thread_id=None, tools=None, instructions=''):
                    return super().start(thread_id=thread_id, tools=tools,
                                         instructions=instructions + PAGING_INSTRUCTIONS)
            return NativeExtended(underlying, extra)

    store = LibraryStore(args.data_root / 'libraries')
    if embedding_settings(args.data_root / 'libraries/search')['enabled'] or store.evaluation_set_summaries()['retrieval_enabled']:
        raise ValueError('Embeddings and held-out retrieval must already be disabled')
    before = {item['id']: item['enabled'] for item in store.libraries(include_deleted=True) if not item.get('deleted_at')}
    if set(args.scope_library_id) - set(before):
        raise ValueError('Unknown/deleted library in the common scope')
    batch.write(here / 'scope-before.json', before)
    app = server = None
    stop, lock = threading.Event(), threading.RLock()
    state = {'schema': 'route-catalog-progress/v1', 'status': 'preparing', 'model': args.model,
             'effort': EFFORT, 'expected_runs': len(CASES) * len(selected_conditions),
             'parallel_limit': args.parallel, 'active': [], 'completed': []}
    def progress():
        state['updated_at'] = base.now()
        batch.write(here / 'progress.json', state)
    try:
        additions = [key for key in args.scope_library_id if not before[key]]
        if additions:
            store.set_enabled_many(additions, True)
        scope = sorted(key for key, enabled in before.items() if enabled or key in additions)
        batch.write(here / 'scope-frozen.json', {'enabled_library_ids': scope, 'added': additions})
        data = runtime / 'product'
        data.mkdir()
        link_library(data / 'libraries', args.data_root / 'libraries')
        docs = runtime / 'empty-documents'
        docs.mkdir()
        app = App(data_dir=data, doc_root=docs, runtime_factory=NativeFactory())
        cards = RouteCardStore(args.method_root, app.kb)
        catalog = cards.list_methods(limit=3)
        if catalog.get('state') != 'ready' or not catalog.get('total'):
            raise ValueError('Current route-method catalog is unavailable')
        covered = set()
        for path in (args.method_root / 'route-method-cards').glob('method-*.json'):
            covered.update(json.loads(path.read_text(encoding='utf-8'))['card']['source_case_ids'])
        if not set(candidates['case_ids']).issubset(covered):
            raise ValueError('Every frozen source case requires a generated method card')
        conditions, audits = {}, {}
        def search_audit(row):
            with lock:
                with (here / 'backend-policy-audits.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        install_backend_policy(app.tools, 'qmd_keyword', on_search=search_audit)
        definitions = install_tools(app, cards, extra, conditions, scope, audits)
        server = make_server(app, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        batch.write(here / 'services.json', {'pid': os.getpid(), 'url': url})
        batch.write(here / 'runtime-tools.json', definitions)
        (here / 'product-instructions.txt').write_text(INSTRUCTIONS + PAGING_INSTRUCTIONS, encoding='utf-8')
        tasks, contexts, manifests = [], {}, {}
        for cid in CASES:
            for condition in selected_conditions:
                key = condition + '-' + cid
                prepared = create_task(url, root, cid)
                sid = prepared['session']['id']
                conditions[sid] = condition
                audits[sid] = here / 'audits' / (key + '.jsonl')
                context = app.tools.call(sid, 'get_task_context', {})
                contexts[key], manifests[key] = context, prepared['input_manifest']
                batch.write(here / 'prepared' / key / 'context-preflight.json', context)
                batch.write(here / 'prepared' / key / 'input-manifest.json', prepared['input_manifest'])
                tasks.append(SimpleNamespace(url=url, session_id=sid, case=cid, condition=condition,
                    destination=runs / key, model=args.model, timeout=args.timeout,
                    poll_interval=args.poll_interval, audit_path=audits[sid]))
        for cid in CASES:
            a, b = ('existing_navigation-' + cid), ('case_methods-' + cid)
            if a in contexts and b in contexts and (contexts[a]['goal'] != contexts[b]['goal'] or batch.normalize_context(contexts[a]['model']) != batch.normalize_context(contexts[b]['model']) or manifests[a] != manifests[b]):
                raise ValueError('Paired public task inputs differ')
        frozen_sources = {name: batch.sha(APP / name) for name in CORE_PATHS}
        verification_path = args.method_root / 'verification.json'
        construction_verification = None
        if verification_path.is_file():
            verified = json.loads(verification_path.read_text(encoding='utf-8'))
            if not verified.get('valid') or verified.get('card_contents_changed') is not False:
                raise ValueError('Construction reverification did not preserve the original card contents')
            construction_verification = {'path': str(verification_path), 'sha256': batch.sha(verification_path),
                'constructor_rerun': verified['constructor_rerun'],
                'card_contents_changed': verified['card_contents_changed'],
                'recovered_execution_errors': sum(len(row['delivery_audit'].get('execution_errors', []))
                                                   for row in verified['cases'])}
        protocol = {'schema': 'route-catalog-protocol/v1', 'cases': CASES, 'conditions': selected_conditions,
            'model': args.model, 'effort': EFFORT, 'parallel_limit': args.parallel,
            'runtime_profile': NATIVE_PROFILE, 'tool_transport': 'bounded_code_mode_single_call',
            'condition_difference': 'case_methods exposes the generated route catalog/cards; existing_navigation reports not_prepared. Both retain the same raw knowledge tools and bounded paging.',
            'production_instructions_base_unchanged': True, 'shared_paging_instructions': PAGING_INSTRUCTIONS,
            'shared_route_message': MESSAGE, 'message_sha256': base.digest(MESSAGE),
            'output_budget_bytes': 7000, 'actual_tool_output_audit_budget_bytes': 8192,
            'generated_card_sha256': card_hashes, 'source_cases': candidates, 'candidate_sha256': candidate_hash,
            'construction_delivery_verification': construction_verification,
            'source_sha256': frozen_sources, 'public_input_manifest': manifests,
            'enabled_libraries': scope, 'search_backend': policy_description('qmd_keyword'),
            'private_grading_read': False, 'test_answers_supplied': False, 'embedding_enabled': False,
            'index_rebuild_performed': False, 'solver_run': False, 'attempts_per_condition_case': 1,
            'automatic_retries': 0, 'execution_order': [task.condition + '-' + task.case for task in tasks],
            'delivery_failure_scope': 'Only the affected run is invalid; other turns finish naturally.',
            'limitations': ['Three tasks, one attempt each; not a significance test.',
                            ('Historical baseline comparison only; prompts/tool delivery may differ, and no baseline inference is rerun.'
                             if len(selected_conditions) == 1 else
                             'Bounded single-call paging guidance differs from historical trials; compare only the new paired arms.')]}
        batch.write(here / 'protocol.json', protocol)
        if args.prepare_only:
            state.update(status='preflight_complete')
            progress()
            return 0
        def monitor():
            while not stop.wait(0.5):
                if (here / 'stop.request').exists():
                    stop.set()
        threading.Thread(target=monitor, daemon=True).start()
        def execute(task):
            key = task.condition + '-' + task.case
            with lock:
                state['active'].append(key)
                state['status'] = 'running'
                progress()
            try:
                result = base.run_turn(task, MESSAGE, cancel_event=stop)
                if result.get('conversation_id'):
                    try:
                        path = locate_rollout(result['conversation_id'])
                        expected = [json.loads(line)['response_sha256'] for line in audits[task.session_id].read_text(encoding='utf-8').splitlines()]
                        delivery = audit_rollout(path, result['conversation_id'], expected_response_sha256=expected)
                    except Exception as error:
                        delivery = {'valid': False, 'issues': [{'code': type(error).__name__, 'detail': str(error)}]}
                else:
                    delivery = {'valid': False, 'issues': [{'code': 'thread_identity_missing'}]}
                batch.write(task.destination / 'delivery-audit.json', delivery)
                result['delivery_valid'] = delivery['valid']
                if not delivery['valid']:
                    result.update(status='invalid_delivery', failure_code='actual_tool_output_not_verified')
                batch.write(task.destination / 'run.json', result)
                row = {key: result.get(key) for key in ('case_id', 'condition', 'status', 'seconds',
                    'conversation_id', 'natural_termination', 'failure_code', 'delivery_valid')}
                with lock:
                    state['completed'].append(row)
                print(json.dumps({'event': 'completed', **row}), flush=True)
            finally:
                with lock:
                    state['active'].remove(key)
                    progress()
        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = [executor.submit(execute, task) for task in tasks]
            for future in as_completed(futures):
                future.result()
        if hashes(args.method_root) != card_hashes or batch.sha(args.candidate_list) != candidate_hash or any(batch.sha(APP / name) != value for name, value in frozen_sources.items()):
            raise ValueError('Frozen sources or cards changed during inference')
        failed = any(row['status'] != 'complete' for row in state['completed'])
        state.update(status='completed_with_failures' if failed else 'completed', frozen_hashes_unchanged=True)
        progress()
        return 2 if failed else 0
    except BaseException as error:
        stop.set()
        state.update(status='failed', error_type=type(error).__name__, detail=str(error))
        progress()
        batch.write(here / 'infrastructure-error.json', {'detail': str(error), 'traceback': traceback.format_exc()})
        raise
    finally:
        stop.set()
        cleanup = {'scope_restored': False, 'owned_services_stopped': False, 'errors': []}
        if app is not None:
            app.agents.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        cleanup['owned_services_stopped'] = True
        actual = {item['id']: item['enabled'] for item in store.libraries(include_deleted=True) if item['id'] in before}
        for enabled in (False, True):
            changed = [key for key, value in before.items() if value is enabled and actual.get(key) != value]
            if changed:
                store.set_enabled_many(changed, enabled)
        cleanup['scope_restored'] = {item['id']: item['enabled'] for item in store.libraries(include_deleted=True) if item['id'] in before} == before
        batch.write(here / 'cleanup.json', cleanup)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
