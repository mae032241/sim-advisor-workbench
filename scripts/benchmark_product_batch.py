"""Exercise the product's plan-bound CLI, with one independent service per arm.

No model prompt or planning loop lives here. Product AgentSessions supplies its
own instructions, tools, task context, save validation and native runtime.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
from types import SimpleNamespace
from urllib.request import Request, ProxyHandler, build_opener

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from scripts.benchmark_backend_policy import BACKENDS, install_backend_policy, policy_description
ROOT = APP / 'benchmarks/comsol-plan-v3'
RUN_ID = 'luna6-high-product-cli-20261111'
HERE = ROOT / 'private/evaluations' / RUN_ID
RUNS = ROOT / 'runs' / RUN_ID
DATA = Path('D:/software/sim-offline/data')
OFFLINE = Path('D:/software/sim-offline/sim-advisor-offline-win-x64-0.2.2')
MODEL, EFFORT = 'gpt-6-luna', 'high'
LOCK = threading.RLock()
STATE = {'schema': 'product-cli-paired-progress/v1', 'status': 'preparing',
         'model': MODEL, 'effort': EFFORT, 'parallel_limit': 2,
         'expected_runs': 30, 'active': [], 'completed': []}
STOP = threading.Event()
AUDITS = {}

# The same restricted planning profile applies to both arms. All actual
# workbench business tools remain present, including context, scope and saves.
PROFILE = {
    'features.shell_tool': False, 'features.view_image': False,
    'features.multi_agent': False, 'agents.enabled': False,
    'features.apps': False, 'features.plugins': False, 'features.memories': False,
    'features.hooks': False, 'features.remote_plugin': False,
    'features.skill_search': False, 'features.skip_host_skill_discovery': True,
    'features.code_mode': True, 'features.code_mode_host': True,
    'features.browser_use': False, 'features.computer_use': False,
    'features.in_app_browser': False, 'web_search': 'disabled',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    # A short-lived reader on Windows may deny replacement of its open file.
    # This is bookkeeping contention, never an Agent/model execution failure.
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(min(.02 * 2 ** attempt, .2))


def progress():
    STATE['updated_at'] = now()
    write(HERE / 'progress.json', STATE)


def flags(settings):
    def literal(value):
        if isinstance(value, dict):
            return '{'+', '.join(json.dumps(k)+'='+literal(v) for k, v in value.items())+'}'
        return json.dumps(value, ensure_ascii=False)
    return [part for key, value in settings.items() for part in ('-c', key+'='+literal(value))]


def request(url, path, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    with build_opener(ProxyHandler({})).open(
            Request(url+path, data=data, headers={'Content-Type': 'application/json'}), timeout=120) as response:
        return json.load(response)


def normalize_context(value):
    """Compare public semantic input, omitting generated task identity/path."""
    if isinstance(value, list):
        return [normalize_context(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_context(item) for key, item in value.items()
                if key not in {'id', 'path', 'source_path', 'created_at', 'updated_at'}}
    return value


def audit_tools(app, arm, retrieval_backend='agent'):
    def policy_audit(row):
        path = HERE / 'backend-policy-audits' / (arm + '.jsonl')
        row = dict(row, recorded_at=now(), arm=arm)
        with LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    description = install_backend_policy(app.tools, retrieval_backend, on_search=policy_audit)
    original = app.agents.runtime_tool

    def audited(sid, name, arguments, *, source_entry=None):
        started = time.monotonic()
        row = {'tool': name, 'started_at': now()}
        if isinstance(arguments, dict):
            row['arguments'] = {key: value for key, value in arguments.items()
                                if key in {'id', 'query', 'kind', 'mode', 'library_ids', 'enabled',
                                           'feature_branch', 'offset', 'length', 'revision', 'case_id', 'stage',
                                           'category', 'node_path', 'source', 'draft_id', 'draft_revision', 'path'}}
            if name == 'repair_plan_draft':
                row['patch_paths'] = [item.get('path') for item in arguments.get('patches', []) if isinstance(item, dict)]
            if name == 'search_knowledge':
                row['requested_mode'] = arguments.get('mode', 'qmd_keyword')
                row['effective_mode'] = row['requested_mode'] if retrieval_backend == 'agent' else retrieval_backend
                row['retrieval_backend_policy'] = retrieval_backend
        try:
            result = original(sid, name, arguments, source_entry=source_entry)
            row['success'] = not isinstance(result, dict) or result.get('success') is not False
            if isinstance(result, dict):
                row['result_keys'] = sorted(result)
                row['result_count'] = len(result.get('results', [])) if isinstance(result.get('results'), list) else None
                row['source_id'] = result.get('id') if name == 'read_source' else None
                row['result_json_bytes'] = len(json.dumps(result, ensure_ascii=False).encode('utf-8'))
                if result.get('success') is False and result.get('contentItems'):
                    try:
                        feedback = json.loads(result['contentItems'][0]['text'])
                    except (ValueError, KeyError, IndexError, TypeError):
                        feedback = {}
                    row['feedback_code'] = feedback.get('error_code')
                    row['feedback_draft_id'] = feedback.get('draft_id')
                    row['validation_paths'] = [item.get('path') for item in feedback.get('errors', []) if isinstance(item, dict)]
            return result
        except BaseException as exc:
            row.update(success=False, error_type=type(exc).__name__)
            raise
        finally:
            row['seconds'] = round(time.monotonic()-started, 4)
            with LOCK:
                path = AUDITS.get((arm, sid))
                if path:
                    with path.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(row, ensure_ascii=False)+'\n')
    app.agents.runtime_tool = audited
    return description


def main():
    global RUN_ID, HERE, RUNS
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-only', action='store_true', help='Real product input/CLI checks without model inference')
    parser.add_argument('--case', action='append', dest='cases', help='Explicit public case subset; all cases by default')
    parser.add_argument('--arm', action='append', choices=('rag', 'no-library'), dest='arms')
    parser.add_argument('--compare-context-root', type=Path, help='Prepared input folder of a prior paused complete-input trial')
    parser.add_argument('--run-id', default=RUN_ID)
    parser.add_argument('--supplements-protocol', type=Path)
    parser.add_argument('--trial-role', default='primary')
    parser.add_argument('--retrieval-backend', choices=BACKENDS, default='agent',
                        help='Experiment execution policy; fixes only search mode without changing prompts or tools')
    args = parser.parse_args()
    import re
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', args.run_id):
        parser.error('run-id must be a simple directory name')
    RUN_ID = args.run_id
    HERE = ROOT/'private/evaluations'/RUN_ID
    RUNS = ROOT/'runs'/RUN_ID
    HERE.mkdir(parents=True, exist_ok=True)
    if (HERE / 'protocol.json').exists():
        raise RuntimeError('This attempt is already frozen; never overwrite or resume completed model tasks silently')
    runtime = HERE / 'runtime'
    (runtime / 'tmp').mkdir(parents=True, exist_ok=True)
    os.environ.update(SIM_ADVISOR_NODE=str(OFFLINE/'node/node.exe'),
                      SIM_ADVISOR_QMD_DIR=str(OFFLINE/'qmd/node_modules/@tobilu/qmd'),
                      SIM_ADVISOR_SEARCH_CACHE=str(runtime/'search-cache'),
                      TEMP=str(runtime/'tmp'), TMP=str(runtime/'tmp'), PYTHONIOENCODING='utf-8')
    os.environ.pop('QMD_EMBED_MODEL', None)
    os.environ.pop('SIM_ADVISOR_EMBED_MODEL', None)
    from advisor.server import App, make_server
    from advisor.config import codex_command
    from advisor.agent_sessions import INSTRUCTIONS, planning_tools
    from advisor.research_policy import text_sha256
    from advisor.runtime_adapters import CodexRuntimeFactory, CodexDebugRuntime, runtime_command
    from advisor.library_store import LibraryStore
    from advisor.retrieval_settings import read as embedding_settings
    from scripts.benchmark_product_setup import create_task, public_goal
    from scripts.benchmark_product_cli import plan
    from scripts.benchmark_cli import core
    from scripts.benchmark_native_cli import project_mcp_disables

    class PlanningProfileFactory(CodexRuntimeFactory):
        def __call__(self, *, cwd, on_event, on_tool, model=MODEL, reasoning_effort=EFFORT):
            settings = dict(PROFILE)
            inherited, names = project_mcp_disables(Path(cwd))
            settings.update(inherited)
            command = runtime_command(codex_command())+flags(settings)+['--strict-config']
            return CodexDebugRuntime(command=command, cwd=cwd, on_event=on_event, on_tool=on_tool,
                                     model=model, reasoning_effort=reasoning_effort)

    manifest = core.load_json(ROOT/'public/manifest.json')
    cases = [item['case_id'] for item in manifest['cases']]
    assert len(cases) == 15
    if args.cases:
        if len(set(args.cases)) != len(args.cases) or not set(args.cases).issubset(cases):
            parser.error('case subset must be unique selected public cases')
        cases = [case for case in cases if case in args.cases]
    arms = args.arms or ['rag', 'no-library']
    if len(set(arms)) != len(arms):
        parser.error('arms must be unique')
    expected_runs = len(arms)*len(cases)
    STATE['expected_runs'] = expected_runs
    for cid in cases:
        core.validate_case(ROOT, cid)
        public_goal(ROOT, cid)
    assert not embedding_settings(DATA/'libraries/search')['enabled']
    store = LibraryStore(DATA/'libraries')
    assert not store.evaluation_set_summaries()['retrieval_enabled']
    store.evaluation_policy()
    before = {lib['id']: lib['enabled'] for lib in store.libraries(include_deleted=True) if not lib.get('deleted_at')}
    write(HERE/'scope-before.json', before)
    command = [part for part in runtime_command(codex_command()) if part != '--stdio']+flags(PROFILE)+['--strict-config', '--print-startup-config']
    probe = subprocess.run(command, capture_output=True, timeout=45,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    write(HERE/'runtime-profile-check.json', {'success': probe.returncode == 0,
        'returncode': probe.returncode, 'model_inference': False,
        'stdout_sha256': hashlib.sha256(probe.stdout).hexdigest(),
        'stderr_sha256': hashlib.sha256(probe.stderr).hexdigest()})
    if probe.returncode:
        raise RuntimeError('The installed Codex rejected the planning runtime configuration')
    empty_docs = runtime/'empty-documents'
    empty_docs.mkdir(exist_ok=True)
    apps, servers, urls = {}, {}, {}
    protocol = None
    try:
        for arm in arms:
            data = runtime/arm
            data.mkdir(parents=True, exist_ok=True)
            if arm == 'rag':
                link = data/'libraries'
                if not link.exists():
                    # Native PowerShell creates a directory junction without copying GBs of indexes.
                    escaped_link = str(link).replace("'", "''")
                    escaped_target = str(DATA/'libraries').replace("'", "''")
                    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                    f"New-Item -ItemType Junction -Path '{escaped_link}' -Target '{escaped_target}' | Out-Null"],
                                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                assert link.resolve() == (DATA/'libraries').resolve()
            app = App(data_dir=data, doc_root=empty_docs, runtime_factory=PlanningProfileFactory())
            apps[arm] = app
            audit_tools(app, arm, args.retrieval_backend)
            server = make_server(app, 0)
            servers[arm] = server
            threading.Thread(target=server.serve_forever, daemon=True).start()
            urls[arm] = f'http://127.0.0.1:{server.server_port}'
        write(HERE/'services.json', {'pid': os.getpid(), 'urls': urls, 'data_roots': {arm: str(app.data_dir) for arm, app in apps.items()}})
        if 'no-library' in apps:
            assert apps['no-library'].libraries.libraries() == []
            empty_result = request(urls['no-library'], '/api/tools/call', {'name': 'search_knowledge',
                'arguments': {'query': 'Faraday', 'kind': 'all', 'mode': 'qmd_keyword'}})
            assert isinstance(empty_result.get('result'), dict) and not empty_result['result'].get('results'), 'No-library service unexpectedly returned knowledge'
        schemas = {arm: request(url, '/api/tools') for arm, url in urls.items()}
        first_schema = next(iter(schemas.values()))
        assert all(schema == first_schema for schema in schemas.values()), 'Arms must expose identical product tools'
        write(HERE/'product-tools.json', first_schema)
        (HERE/'product-instructions.txt').write_text(INSTRUCTIONS, encoding='utf-8')
        if args.supplements_protocol:
            previous = core.load_json(args.supplements_protocol)
            assert text_sha256(INSTRUCTIONS) == previous['instructions_sha256']
            assert text_sha256(json.dumps(planning_tools(), ensure_ascii=False, sort_keys=True)) == previous['tool_schema_sha256']

        tasks = {arm: [] for arm in apps}
        contexts = {}
        for cid in cases:
            for arm, url in urls.items():
                prepared = create_task(url, ROOT, cid)
                sid = prepared['session']['id']
                destination = RUNS/(arm+'-'+cid)
                preparation = HERE/'prepared'/(arm+'-'+cid)
                preparation.mkdir(parents=True, exist_ok=True)
                write(preparation/'input-manifest.json', prepared['input_manifest'])
                write(preparation/'prepared-task.json', {'session_id': sid, 'goal_chars': prepared['goal_chars'],
                                                       'input_manifest': prepared['input_manifest']})
                context = request(url, '/api/tools/call', {'name': 'get_task_context', 'arguments': {'session_id': sid}})['result']
                write(preparation/'context-preflight.json', context)
                if args.compare_context_root:
                    previous_folder = args.compare_context_root/(arm+'-'+cid)
                    previous_context = core.load_json(previous_folder/'context-preflight.json')
                    previous_manifest = core.load_json(previous_folder/'input-manifest.json')
                    assert prepared['input_manifest'] == previous_manifest, 'Resume input files changed'
                    assert context['goal'] == previous_context['goal'], 'Resume user goal changed'
                    assert normalize_context(context['model']) == normalize_context(previous_context['model']), 'Resume observed geometry changed'
                contexts[(arm, cid)] = context
                audit_path = HERE/'audits'/(arm+'-'+cid+'.jsonl')
                audit_path.parent.mkdir(parents=True, exist_ok=True)
                with LOCK:
                    AUDITS[(arm, sid)] = audit_path
                tasks[arm].append(SimpleNamespace(case=cid, arm=arm, session_id=sid, url=url,
                    destination=destination, model=MODEL, effort=EFFORT, timeout=7200,
                    audit_path=audit_path, poll_interval=5.0))
            # Generated IDs and upload paths differ, actual goal and observed model facts must agree.
            if len(arms) == 2:
                left, right = (contexts[(arm, cid)] for arm in ('rag', 'no-library'))
                assert left['goal'] == right['goal']
                assert normalize_context(left['model']) == normalize_context(right['model']), f'Model input differs: {cid}'

        source_paths = ['agent_cli.py', 'tools_cli.py', 'advisor/agent_sessions.py', 'advisor/codex_runtime.py',
                        'advisor/runtime_adapters.py', 'advisor/planning_service.py', 'advisor/research_policy.py',
                        'prompts/comsol_planner.v1.txt', 'scripts/benchmark_product_cli.py', 'scripts/benchmark_product_setup.py',
                        'scripts/benchmark_product_batch.py']
        source_paths.append('scripts/benchmark_backend_policy.py')
        source_paths.extend(['advisor/plan_drafts.py', 'advisor/plan_review.py', 'advisor/planning_views.py', 'advisor/plan_settings.py',
                             'advisor/mechanism_checks.py', 'advisor/case_guides.py', 'advisor/case_navigation.py', 'advisor/library_store.py'])
        protocol = {'schema': 'product-cli-paired-protocol/v1', 'frozen_at': now(), 'requested_model': MODEL,
                    'requested_effort': EFFORT, 'maximum_parallel_agent_processes': 2,
                    'attempts_per_case_arm': 1, 'automatic_retry_count': 0, 'best_of_selection': False,
                    'trial_role': args.trial_role,
                    'cases': cases, 'arms': list(apps),
                    'entrypoint': 'Actual agent_cli.py settings/message/status/interrupt/disconnect; production AgentSessions and Codex app-server',
                    'instructions_sha256': text_sha256(INSTRUCTIONS),
                    'tool_schema_sha256': text_sha256(json.dumps(planning_tools(), ensure_ascii=False, sort_keys=True)),
                    'prompt_rewritten_for_benchmark': False, 'business_tools_filtered': False,
                    'initial_scope': 'All RAG libraries disabled per task; Agent can discover and explicitly enable selected libraries. No-library service has an empty independent catalog.',
                    'heldout_retrieval_enabled': False, 'embedding_enabled': False,
                    'retrieval_backend_policy': policy_description(args.retrieval_backend),
                    'COMSOL_solve_performed': False, 'index_rebuild_performed': False,
                    'runtime_profile': PROFILE,
                    'input_context': 'Actual model upload/inspection and task creation API, original public problem plus complete recursive user text attachments in goal; geometry/derived inputs remain separate; no invented model snapshot.',
                    'public_topology_json_injected': False,
                    'public_input_sha256': {p.relative_to(ROOT/'public').as_posix(): sha(p) for p in (ROOT/'public').rglob('*') if p.is_file()},
                    'reference_sha256': {cid: sha(ROOT/'private/cases'/cid/'reference.json') for cid in cases},
                    'source_sha256': {p: sha(APP/p) for p in source_paths},
                    'evaluation': 'Offline frozen official-route questions after natural completion; saved structured plan is primary output, chat-only outputs reported separately.',
                    'limitations': ['One run per arm/case.', 'Full workbench business contract, planning-only native runtime profile; builtin shell/file/web/plugin access disabled equally in both arms.',
                                    'Uses product geometry inspection instead of injecting precomputed benchmark topology; differences from prior native-exec runs are not a model-only comparison.']}
        if args.supplements_protocol:
            protocol['supplements_protocol'] = str(args.supplements_protocol.resolve())
            protocol['supplements_protocol_sha256'] = sha(args.supplements_protocol)
            protocol['original_attempts_preserved'] = True
        if args.compare_context_root:
            protocol['resume_context_matches'] = str(args.compare_context_root.resolve())
            protocol['reason'] = 'Resume only the task explicitly cancelled by the user; preserve all prior completed trials.'
        write(HERE/'preflight.json', {'status': 'ready', 'tasks_prepared': expected_runs, 'same_business_tools': True,
                                     'same_instructions': True, 'paired_model_contexts_equal': True,
                                     'no_library_search_empty': True, 'no_model_inference': True,
                                     'retrieval_backend_policy': policy_description(args.retrieval_backend)})
        if args.prepare_only:
            with LOCK:
                STATE.update(status='preflight_complete', tasks_prepared=expected_runs)
                progress()
            print(json.dumps({'event': 'preflight_complete', 'tasks': expected_runs, 'no_model_inference': True}), flush=True)
            return
        write(HERE/'protocol.json', protocol)
        with LOCK:
            STATE.update(status='running', started_at=now(), tasks_prepared=expected_runs)
            progress()
        print(json.dumps({'event': 'started', 'model': MODEL, 'effort': EFFORT, 'parallel': 2, 'runs': expected_runs}), flush=True)

        def monitor_stop():
            while not STOP.wait(0.5):
                if (HERE/'stop.request').exists():
                    STOP.set()
        threading.Thread(target=monitor_stop, daemon=True).start()

        def arm_worker(arm):
            rows = []
            for task in tasks[arm]:
                if STOP.is_set():
                    break
                if arm == 'rag':
                    # Only one RAG agent at a time, so global enable/disable cannot race another test.
                    live = [lib['id'] for lib in store.libraries() if lib['enabled']]
                    if live:
                        store.set_enabled_many(live, False)
                    assert not store.evaluation_set_summaries()['retrieval_enabled']
                key = arm+'-'+task.case
                with LOCK:
                    STATE['active'].append({'run': key, 'session_id': task.session_id, 'started_at': now()})
                    progress()
                try:
                    result = plan(ROOT, task, cancel_event=STOP)
                    row = {key: result.get(key) for key in ('status', 'case_id', 'arm', 'seconds', 'failure_type',
                           'failure_code', 'saved_plan', 'save_plan_count', 'plan_sha256')}
                    row['run'] = arm+'-'+task.case
                    row['retrieval_backend_policy'] = args.retrieval_backend
                except Exception as exc:
                    row = {'run': key, 'case_id': task.case, 'arm': arm,
                           'status': 'launch_error', 'failure_type': type(exc).__name__}
                with LOCK:
                    STATE['active'] = [item for item in STATE['active'] if item['run'] != key]
                    STATE['completed'].append(row)
                    progress()
                rows.append(row)
                print(json.dumps({'event': 'completed', **row}, ensure_ascii=False), flush=True)
            return rows

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='product-cli-arm') as pool:
            futures = [pool.submit(arm_worker, arm) for arm in apps]
            for future in as_completed(futures):
                future.result()
        for relative, digest in protocol['public_input_sha256'].items():
            assert sha(ROOT/'public'/relative) == digest
        for cid, digest in protocol['reference_sha256'].items():
            assert sha(ROOT/'private/cases'/cid/'reference.json') == digest
        with LOCK:
            STATE.update(status='paused_by_user' if STOP.is_set() else 'completed', completed_at=now(),
                         input_and_reference_hashes_unchanged=True)
            progress()
    except BaseException as exc:
        STOP.set()
        with LOCK:
            STATE.update(status='failed', failure_type=type(exc).__name__)
            progress()
        write(HERE/'infrastructure-error.json', {'type': type(exc).__name__, 'detail': str(exc),
                                               'traceback': traceback.format_exc()})
        raise
    finally:
        cleanup = {'scope_restored': False, 'owned_services_stopped': False, 'timestamp': now()}
        for app in apps.values():
            app.agents.close()
        for server in servers.values():
            server.shutdown()
            server.server_close()
        cleanup['owned_services_stopped'] = True
        for enabled in (False, True):
            ids = [key for key, value in before.items() if value is enabled]
            if ids:
                store.set_enabled_many(ids, enabled)
        actual = {lib['id']: lib['enabled'] for lib in store.libraries(include_deleted=True) if lib['id'] in before}
        cleanup['scope_restored'] = actual == before
        cleanup['heldout_retrieval_enabled'] = store.evaluation_set_summaries()['retrieval_enabled']
        write(HERE/'cleanup.json', cleanup)
        print(json.dumps({'event': 'cleanup', **cleanup}), flush=True)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
