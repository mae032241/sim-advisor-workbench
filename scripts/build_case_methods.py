"""Build bounded, evidence-linked case methods in independent model sessions.

This is an experimental curator, not the product planner. It consumes explicit
non-heldout case IDs only, never opens benchmark tasks, never builds an index,
and writes sessions, receipts, audit and profiles under its own output folder.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
import tomllib

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

# Match the restricted product batch profile without importing benchmark data
# loaders, prompts or output paths. The curator adds only its own dynamic tools.
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
READ_TOOLS = frozenset({'read_source', 'read_case_configuration', 'get_case_guide',
                        'search_knowledge', 'get_task_context'})
LOCAL_TOOLS = frozenset({'submit_case_method', 'get_tool_contract'})
DEFAULT_LIBRARY_ROOT = Path('D:/software/sim-offline/data/libraries')
DEFAULT_MODEL = 'gpt-6-luna'

INSTRUCTIONS = '''你是资料整理员，为单个已允许案例构造简洁、通用的方法说明。此任务不是用户仿真方案规划。
本会话仅允许整理输入中给出的唯一case_id及其现有原文、保存配置和直接关联来源。不得探索其他案例、测试题、标准答案、历史方案、评分、外部网页、文件、记忆或其他会话。
先read_source读取本案例概要的现有正文，再用get_case_guide看本案例导航并按需展开stage；核查研究、接口、耦合与设置依赖所需的原文及必要分类配置。read_source和read_case_configuration才是已读证据。所有search_knowledge必须限定此case_id，使用qmd_keyword；找不到资料就说明未知，不扩展搜索范围。
目标是让后续读者理解本案例做什么、输出什么、如何实现以及何时可以迁移。覆盖：目标和输出；方法与假设；成立与失效条件；域、物理接口和耦合变量；传递关系与设置依赖；迁移边界；证据覆盖与未知。
区分source_text、saved_configuration、api_configuration的直接观察与llm_inference解释。凡是因果关系、方法成立条件、迁移判断等超出来源原句的解释都必须标llm_inference，提供支持来源并写局限。缺证据标unknown，不编造设置值、公式、研究、求解结果或验证结论。
节点或接口存在不等于在研究中启用。保存值/API观察不证明生效、成功求解或物理有效；只有实际读到的激活/研究关系才能支持相应主张，仍保留返回的未评估状态。不得用文件名、目录、导航摘要推出方法已启用。
每个明确主张使用实际读回的evidence_refs，原样保留source_id/context/locator；只引用已返回正文或配置范围。导航不授予引用权限。不复制大段原文，不为了填满字段而重复主张。
每节优先1到3个原子主张；每项explanation尽量不超过240字，limitations简短。未知可以明确写unknown且不带假证据。资料不足时保留缺口，本轮不补全公式或所有材料。
使用submit_case_method提交。输入结构与枚举必须先通过get_tool_contract(tool="submit_case_method")发现；深层签名显示unknown时查准确path，按next_offset分页，不猜结构。只修实际校验错误。
提交成功后给出很短的完成说明并自然结束本轮。不得写用户方案、启用资料库、修改生产资料或索引。
'''

JOINT_SOURCE_VERSION = 'joint-sources/v1'
JOINT_SOURCE_INSTRUCTIONS = '''
本轮启用教程与实际配置联合解释：像MooseAgent联读教程和模型配置，先找实际读回的物理接口、研究及耦合对象，再用本案例直接关联教程的正文解释其方法、前提和设置依赖。不能仅把教程摘要与配置清单并排。
逐项说明教程描述与实际读回配置的对应、冲突或尚未核实之处；接口、研究或耦合对象未读到时明确未知，不从教程反推保存配置中已有或生效。保留partial、effective_activation和physical_validation等读取限制。
至少在method_and_assumptions或transfer_and_setting_dependencies中提供一条origin=llm_inference的对应解释；该同一主张的evidence_refs必须同时复制真实read_source读回的关联教程source_text引用，以及本会话指定case_id真实read_case_configuration读回的case_configuration引用。允许解释两者不一致或配置尚不能证明教程方法生效，不要求声称一致。
教程须是关联教程正文（真实来源元数据中的help:com.comsol.help.models.*，或case_context/method_step教程来源）；本案例model_case概要即使含方法摘要也不算教程。导航、搜索摘要和来源名称不授予引用；只可用本会话现有read_source和read_case_configuration读取已允许范围，不手写引用或伪造字段。
不得为满足联合要求编造条件、公式、对象、设置值、启用关系或物理验证。如果缺少任一类真实读取或无法形成有依据的对应解释，保留缺口并结束为未提交，不提交虚构的联合说明。提交成功仍不证明物理正确或方法适用。
'''
JOINT_SOURCE_RULE = (
    'Joint-source mode: method_and_assumptions or transfer_and_setting_dependencies must contain '
    'one origin=llm_inference claim whose evidence_refs jointly copy real read_source source_text '
    'from an associated tutorial and real read_case_configuration case_configuration from the assigned '
    'case_id. Tutorial identity comes from bound source metadata: a non-model_case '
    'help:com.comsol.help.models.* source or a case_context/method_step tutorial source; '
    'the model_case overview is not a tutorial. Read only IDs discovered by this case navigation '
    'or case-scoped search, using the published read_source/read_case_configuration tools. '
    'Explain correspondence, conflict or unverified activation, preserve read limitations, and do '
    'not invent evidence to pass. If evidence is insufficient, end without submission. '
    'This is an additional read/reference gate, not a semantic or physical-validity check.'
)


def run_instructions(args):
    return INSTRUCTIONS + (JOINT_SOURCE_INSTRUCTIONS if getattr(args, 'joint_sources', False) else '')


def instruction_manifest(args):
    return {'joint_sources': bool(getattr(args, 'joint_sources', False)),
            'instructions_version': JOINT_SOURCE_VERSION if getattr(args, 'joint_sources', False) else 'case-method-curator/v1',
            'instructions_sha256': hashlib.sha256(run_instructions(args).encode('utf-8')).hexdigest()}


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def write_json(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        return
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def flags(settings):
    def literal(value):
        if isinstance(value, dict):
            return '{' + ', '.join(json.dumps(k) + '=' + literal(v) for k, v in value.items()) + '}'
        return json.dumps(value, ensure_ascii=False)
    return [part for key, value in settings.items() for part in ('-c', key + '=' + literal(value))]


def inherited_mcp_disables(directory):
    """Disable inherited servers without copying their commands or secrets."""
    names = set()
    user_config = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'config.toml'
    for path in [user_config, *(p / '.codex/config.toml' for p in (directory, *directory.parents))]:
        if path.is_file():
            servers = tomllib.loads(path.read_text(encoding='utf-8')).get('mcp_servers', {})
            if not isinstance(servers, dict):
                raise ValueError('Inherited MCP configuration must be an object')
            names.update(servers)
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', name) for name in names):
        raise ValueError('Inherited MCP names cannot be safely disabled')
    return {'mcp_servers.' + name: {'command': sys.executable, 'args': ['-c', 'pass'], 'enabled': False}
            for name in sorted(names)}


def case_key(case_id):
    return hashlib.sha256(case_id.encode('utf-8')).hexdigest()[:24]


def validate_options(args):
    if args.url is not None:
        raise ValueError('--url is not supported: this experiment requires the same local provider for live authority checks')
    if not args.cases or len(args.cases) != len(set(args.cases)):
        raise ValueError('--case must contain one or more unique, explicit case IDs')
    if any(not isinstance(case, str) or not case.strip() or len(case) > 256 for case in args.cases):
        raise ValueError('Each case ID must be nonempty and no longer than 256 characters')
    if type(args.parallel) is not int or not 1 <= args.parallel <= 2:
        raise ValueError('--parallel must be 1 or 2')
    if args.timeout <= 0:
        raise ValueError('--timeout must be positive')
    if not args.model.strip() or not args.effort.strip():
        raise ValueError('--model and --effort must be nonempty')
    args.library_root = args.library_root.expanduser().resolve(strict=True)
    if not (args.library_root / 'catalog.sqlite3').is_file():
        raise ValueError('--library-root must contain an existing production catalog')
    args.output = args.output.expanduser().resolve()
    if (args.output == args.library_root or args.output.is_relative_to(args.library_root)
            or args.library_root.is_relative_to(args.output)):
        raise ValueError('Output must be separate from the production library')
    if (args.output / 'manifest.json').exists() or (args.output / 'case-methods').exists():
        raise FileExistsError('This output is already reserved; existing profiles are never overwritten')
    if len(args.scope_library_ids) != len(set(args.scope_library_ids)):
        raise ValueError('--scope-library-id values must be unique')


def link_library(destination, source):
    destination, source = Path(destination), Path(source).resolve(strict=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('The experimental library link already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        # Native PowerShell owns this one reversible operation end to end.
        link_text, source_text = str(destination).replace("'", "''"), str(source).replace("'", "''")
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                 f"New-Item -ItemType Junction -Path '{link_text}' -Target '{source_text}' | Out-Null"],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        if result.returncode:
            raise RuntimeError('Could not create the experimental library junction')
    else:
        destination.symlink_to(source, target_is_directory=True)
    if destination.resolve() != source:
        raise RuntimeError('Experimental library link points at a different source')


def create_local_app(output, library_root):
    from advisor.server import App
    from advisor.runtime_adapters import DisabledRuntimeFactory
    data = Path(output) / 'runtime' / 'service'
    data.mkdir(parents=True, exist_ok=True)
    link_library(data / 'libraries', library_root)
    documents = Path(output) / 'runtime' / 'empty-documents'
    documents.mkdir(parents=True, exist_ok=True)
    return App(data_dir=data, doc_root=documents, runtime_factory=DisabledRuntimeFactory())


def library_flags(app):
    return {item['id']: {'enabled': item['enabled'], 'deleted_at': item.get('deleted_at')}
            for item in app.kb.store.libraries(include_deleted=True)}


class Audit:
    def __init__(self, path):
        self.path, self.lock = Path(path), threading.RLock()

    def append(self, row):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(canonical(dict(row, recorded_at=now())) + '\n')


def source_references(value):
    """Only explicit source references, never arbitrary node/step IDs."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {'source_refs', 'tutorial_links', 'tutorial_refs', 'related_sources'} and isinstance(child, list):
                for item in child:
                    identity = item if isinstance(item, str) else item.get('source_id') or item.get('id') if isinstance(item, dict) else None
                    if isinstance(identity, str) and identity:
                        yield identity
            elif isinstance(child, (list, dict)):
                yield from source_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from source_references(child)


class CaseReader:
    """One case's service calls and actual read receipts; no cross-case search."""
    def __init__(self, app, case_id, session_id, audit, scope_library_ids=()):
        self.app, self.case_id, self.session_id, self.audit = app, case_id, session_id, audit
        self.scope_library_ids = tuple(scope_library_ids)
        self.allowed_ids = {case_id}
        self.receipts, self.reads, self.lock = [], [], threading.RLock()

    def __call__(self, name, arguments):
        if name not in READ_TOOLS or not isinstance(arguments, dict):
            raise PermissionError('Only the published case read tools are allowed')
        arguments = dict(arguments)
        if 'session_id' in arguments:
            raise PermissionError('The service session is bound by the curator')
        if name in {'get_case_guide', 'read_case_configuration', 'search_knowledge'}:
            if arguments.get('case_id') != self.case_id:
                raise PermissionError('This session may only read its assigned case_id')
        if name == 'search_knowledge' and arguments.get('mode', 'qmd_keyword') != 'qmd_keyword':
            raise PermissionError('The curator only uses the existing keyword index')
        if name == 'search_knowledge' and self.scope_library_ids:
            requested = arguments.get('library_ids', self.scope_library_ids)
            if not isinstance(requested, (list, tuple)) or not set(requested).issubset(self.scope_library_ids):
                raise PermissionError('Search libraries must remain within the frozen experiment scope')
            arguments['library_ids'] = list(requested)
        if name == 'read_source' and arguments.get('id') not in self.allowed_ids:
            raise PermissionError('Read IDs must be discovered through this case navigation or case-scoped search')
        started = time.monotonic()
        try:
            result = self.app.tools.call_public(name, dict(arguments, session_id=self.session_id))
            with self.lock:
                if name == 'search_knowledge':
                    self.allowed_ids.update(item['id'] for item in result.get('results', [])
                                            if isinstance(item, dict) and isinstance(item.get('id'), str))
                if name in {'get_case_guide', 'read_source'}:
                    self.allowed_ids.update(source_references(result))
                refs = result.get('evidence_refs', []) if name in {'read_source', 'read_case_configuration'} else []
                if name == 'read_case_configuration':
                    refs = list(refs)
                    for node in result.get('nodes', []):
                        for prop in node.get('properties', []):
                            if isinstance(prop.get('evidence_locator'), str):
                                refs.append({'source_id': self.case_id, 'context': 'case_configuration',
                                             'locator': prop['evidence_locator']})
                for ref in refs:
                    if ref not in self.receipts:
                        self.receipts.append(ref)
                if refs:
                    self.reads.append({'tool': name, 'arguments': arguments, 'evidence_refs': refs,
                                       'result_sha256': digest(result),
                                       'source_id': result.get('id') or result.get('case_id'),
                                       'source': result.get('source'),
                                       'read_scope': result.get('read_scope'),
                                       'partial': result.get('partial'),
                                       'extraction_coverage': result.get('extraction_coverage')})
            self.audit.append({'kind': 'tool', 'tool': name, 'arguments': arguments, 'success': True,
                               'seconds': round(time.monotonic() - started, 4), 'result_sha256': digest(result),
                               'result_bytes': len(canonical(result).encode('utf-8')), 'evidence_refs': refs})
            return result
        except Exception as exc:
            self.audit.append({'kind': 'tool', 'tool': name, 'arguments': arguments,
                               'success': False, 'error_type': type(exc).__name__})
            raise


def require_joint_sources(builder, reader, profile):
    """An extra joint-read gate; the builder still validates every issued ref.

    Bindings originate in CaseMethodStore._authority during successful builder
    reads. Do not infer tutorial identity from model-supplied refs or read bodies.
    Intersect with CaseReader's actual reads rather than granting new receipts.
    """
    ledger = builder._receipts

    def ref_key(ref):
        fields = ('source_id', 'context', 'locator')
        if isinstance(ref, dict) and all(isinstance(ref.get(key), str) for key in fields):
            return tuple(ref[key] for key in fields)
        return None

    tutorial_refs, configuration_refs = set(), set()
    for read in reader.reads:
        for ref in read['evidence_refs']:
            key = ref_key(ref)
            if key is None:
                continue
            identity, context, _ = key
            binding = ledger.bindings.get(identity, {})
            tutorial = (binding.get('type') != 'model_case' and identity != reader.case_id
                        and (str(binding.get('asset_key') or '').startswith('help:com.comsol.help.models.')
                             or binding.get('type') in {'case_context', 'method_step'}))
            if (read['tool'] == 'read_source' and tutorial and context == 'source_text'
                    and ledger.references.get(key) == 'source_text'):
                tutorial_refs.add(key)
            if (read['tool'] == 'read_case_configuration' and identity == reader.case_id
                    and read['arguments'].get('case_id') == reader.case_id and context == 'case_configuration'
                    and ledger.references.get(key) in {'saved_configuration', 'api_configuration'}):
                configuration_refs.add(key)
    sections = profile.get('sections', {}) if isinstance(profile, dict) else {}
    if isinstance(sections, dict):
        for section in ('method_and_assumptions', 'transfer_and_setting_dependencies'):
            claims = sections.get(section, [])
            for claim in claims if isinstance(claims, list) else []:
                if not isinstance(claim, dict) or claim.get('origin') != 'llm_inference':
                    continue
                refs = claim.get('evidence_refs', [])
                keys = {ref_key(ref) for ref in refs} if isinstance(refs, list) else set()
                if keys & tutorial_refs and keys & configuration_refs:
                    return
    raise ValueError('Joint-source mode requires one llm_inference claim in method_and_assumptions or '
                     'transfer_and_setting_dependencies that cites both a real associated tutorial '
                     'source_text read and an assigned-case case_configuration read. An overview '
                     'does not count as a tutorial. Preserve evidence gaps; do not invent references.')


def curator_definitions(builder, *, joint_sources=False):
    from advisor.planning_service import tool_definitions
    definitions = {item['name']: item for item in tool_definitions(bound=True)}
    own = {item['name']: item for item in builder.tool_definitions()}
    missing = (READ_TOOLS | LOCAL_TOOLS) - (set(definitions) | set(own))
    if missing:
        raise ValueError('Case method module has not supplied the required tools')
    selected = {name: definitions[name] for name in READ_TOOLS}
    selected['get_tool_contract'] = definitions['get_tool_contract']
    selected['submit_case_method'] = own['submit_case_method']
    if joint_sources:
        selected['submit_case_method'] = dict(selected['submit_case_method'])
        selected['submit_case_method']['description'] += '\n' + JOINT_SOURCE_RULE
    return [selected[name] for name in sorted(selected)]


def runtime_command(cwd):
    from advisor.config import codex_command
    from advisor.runtime_adapters import runtime_command as base_command
    command = codex_command()
    if not command:
        raise RuntimeError('Codex CLI was not found')
    settings = dict(PROFILE, **inherited_mcp_disables(Path(cwd)))
    return base_command(command) + flags(settings) + ['--strict-config']


def build_one(app, method_store, args, case_id, stop, *, runtime_factory=None):
    from advisor.case_methods import CaseMethodBuilder
    from advisor.codex_runtime import CodexRuntime
    from advisor.tool_contracts import read_contract
    if stop.is_set() or (args.output / 'stop.request').exists():
        stop.set()
        return {'case_id': case_id, 'status': 'stopped', 'profile_submitted': False}
    key = case_key(case_id)
    case_dir = args.output / 'cases' / key
    case_dir.mkdir(parents=True, exist_ok=False)
    audit = Audit(case_dir / 'audit.jsonl')
    # No model attachment and no benchmark task text are used in this session.
    session = app.sessions.create('分析已允许案例的现有实现方法。仅整理既有资料与证据范围。', None)
    reader = CaseReader(app, case_id, session['id'], audit, args.scope_library_ids)
    builder = CaseMethodBuilder(method_store, reader, case_id=case_id)
    joint_sources = bool(getattr(args, 'joint_sources', False))
    definitions = curator_definitions(builder, joint_sources=joint_sources)
    write_json(case_dir / 'tools.json', definitions, exclusive=True)
    completed, submitted, active = threading.Event(), threading.Event(), threading.Event()
    active.set()
    result = {'case_id': case_id, 'session_id': session['id'], 'status': 'running', 'started_at': now()}
    lock, tool_lock = threading.RLock(), threading.RLock()

    def on_event(event):
        # Bound model text; no raw app-server protocol or source bodies.
        row = {k: v for k, v in event.items() if k != 'text'}
        if isinstance(event.get('text'), str) and event.get('type') == 'message':
            row['text'] = event['text'][:12000]
        audit.append(dict(row, kind='runtime_event'))
        if event.get('type') == 'turn' and event.get('event') == 'completed':
            with lock:
                result['turn_status'] = event.get('status')
            completed.set()

    def invoke_tool(name, arguments):
        try:
            if stop.is_set() or (args.output / 'stop.request').exists():
                raise InterruptedError('The experiment has been stopped')
            if name == 'get_tool_contract':
                contract = read_contract(definitions, arguments)
                if joint_sources and arguments.get('tool') == 'submit_case_method':
                    contract['rules'].append(JOINT_SOURCE_RULE)
                return contract
            if name == 'submit_case_method':
                if joint_sources:
                    require_joint_sources(builder, reader, arguments.get('profile') if isinstance(arguments, dict) else None)
                value = builder.call(name, arguments)
                audit.append({'kind': 'submit', 'success': value.get('saved') is True,
                              'result': value, 'input_sha256': digest(arguments)})
                if value.get('saved') is True:
                    submitted.set()
                return value
            if name in {'read_source', 'read_case_configuration'}:
                return builder.call(name, arguments)
            return reader(name, arguments)
        except (ValueError, PermissionError, FileNotFoundError, RuntimeError, InterruptedError) as exc:
            detail = getattr(exc, 'to_dict', None)
            error = detail() if callable(detail) else {'error': str(exc)[:1000], 'error_type': type(exc).__name__}
            audit.append({'kind': 'tool_feedback', 'tool': name, 'success': False, 'error': error})
            return {'success': False, 'contentItems': [{'type': 'inputText', 'text': canonical(error)}]}

    def on_tool(name, arguments):
        # Drain any in-flight service read before freezing its manifest. Calls
        # arriving after a timeout/stop cannot publish a late profile or receipt.
        with tool_lock:
            if not active.is_set():
                return {'success': False, 'contentItems': [{'type': 'inputText', 'text': 'This case session is closed.'}]}
            return invoke_tool(name, arguments)

    workspace = args.output / 'runtime' / 'workspaces' / key
    workspace.mkdir(parents=True, exist_ok=False)
    runtime = None
    started = time.monotonic()
    try:
        if stop.is_set() or (args.output / 'stop.request').exists():
            raise InterruptedError('The experiment has been stopped before model launch')
        command = runtime_command(workspace) if runtime_factory is None else ['test-runtime']
        runtime = (runtime_factory or CodexRuntime)(command=command, cwd=workspace, on_event=on_event,
                                                   on_tool=on_tool, model=args.model, reasoning_effort=args.effort)
        connection = runtime.start(tools=definitions, instructions=run_instructions(args))
        result['thread_id'] = connection['thread_id']
        runtime.send('本会话唯一允许的case_id：' + case_id + '\n请整理此案例的通用方法档案并提交。')
        while not completed.wait(0.25):
            if stop.is_set() or (args.output / 'stop.request').exists():
                stop.set()
                result['status'] = 'stopped'
                runtime.interrupt()
                break
            if time.monotonic() - started >= args.timeout:
                result['status'] = 'timeout'
                runtime.interrupt()
                break
            state = runtime.status()
            if not state.get('running') or state.get('state') == 'error':
                result['status'] = 'runtime_error'
                result['error_metadata'] = state.get('error_metadata')
                break
        if completed.is_set():
            result['status'] = ('completed' if submitted.is_set() and result.get('turn_status') == 'completed'
                                else 'failed' if result.get('turn_status') != 'completed' else 'no_submission')
        result['profile_submitted'] = submitted.is_set()
        result['runtime_status'] = runtime.status()
    except Exception as exc:
        result.update(status='failed', error_type=type(exc).__name__, error=str(exc)[:1000])
    finally:
        active.clear()
        if runtime is not None:
            runtime.close()
        with tool_lock, reader.lock:
            write_json(case_dir / 'source-manifest.json', {'schema': 'case-method-read-manifest/v1',
                       'case_id': case_id, 'session_id': session['id'], 'receipts': reader.receipts,
                       'reads': reader.reads, **instruction_manifest(args)}, exclusive=True)
        result['profile_submitted'] = submitted.is_set()
        result.update(finished_at=now(), seconds=round(time.monotonic() - started, 3))
        write_json(case_dir / 'result.json', result, exclusive=True)
    return result


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--url', help='Reserved; explicitly rejected until live HTTP authority is supported')
    cli.add_argument('--case', action='append', dest='cases', required=True, help='Exact allowed model_case citation ID; repeat per case')
    cli.add_argument('--output', type=Path, required=True)
    cli.add_argument('--library-root', type=Path, default=DEFAULT_LIBRARY_ROOT)
    cli.add_argument('--scope-library-id', action='append', dest='scope_library_ids', default=[],
                     help='Explicit allowed library scope; temporarily enable only these IDs, then restore their original flags')
    cli.add_argument('--model', default=DEFAULT_MODEL)
    cli.add_argument('--effort', default='high')
    cli.add_argument('--joint-sources', action='store_true',
                     help='Require a joint tutorial/configuration inference; keep the legacy curator instructions by default')
    cli.add_argument('--parallel', type=int, default=2)
    cli.add_argument('--timeout', type=float, default=1800, help='Maximum seconds per case, including source reads')
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    validate_options(args)
    args.output.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    manifest = {'schema': 'case-method-build/v1', 'created_at': now(), 'case_ids': args.cases,
                'model': args.model, 'effort': args.effort, 'parallel_limit': args.parallel,
                'timeout_seconds': args.timeout, 'library_root': str(args.library_root),
                'scope_library_ids': args.scope_library_ids,
                'profile': PROFILE, **instruction_manifest(args),
                'builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'method_module_sha256': hashlib.sha256((APP / 'advisor' / 'case_methods.py').read_bytes()).hexdigest(),
                'source_boundary': 'one assigned case per independent session; no benchmark inputs',
                'stop_request': str(args.output / 'stop.request')}
    write_json(args.output / 'manifest.json', manifest, exclusive=True)
    app, before, results, changed = None, None, [], []
    try:
        app = create_local_app(args.output, args.library_root)
        before = library_flags(app)
        write_json(args.output / 'library-flags-before.json', before, exclusive=True)
        if set(args.scope_library_ids) - set(before):
            raise ValueError('Experiment scope contains unknown library IDs')
        if any(before[identity]['deleted_at'] is not None for identity in args.scope_library_ids):
            raise ValueError('Experiment scope contains deleted libraries')
        changed = [identity for identity in args.scope_library_ids if not before[identity]['enabled']]
        if changed:
            app.tools.call_public('set_libraries_enabled', {'library_ids': changed, 'enabled': True})
        write_json(args.output / 'library-flags-active.json', library_flags(app), exclusive=True)
        # Freeze immutable source authority before any model is started.
        authorities = []
        for case_id in args.cases:
            authority = app.kb.get_case_authority(case_id)
            authorities.append({key: authority[key] for key in ('id', 'library_id', 'object_id', 'revision_id',
                                'asset_key', 'asset_hash', 'case_group', 'type') if key in authority}
                               | {'authority_sha256': digest(authority)})
        write_json(args.output / 'source-manifest.json', {'schema': 'case-method-source-manifest/v1',
                   'cases': authorities, **instruction_manifest(args)}, exclusive=True)
        from advisor.case_methods import CaseMethodStore
        store = CaseMethodStore(args.output, app.kb)
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            pending = {pool.submit(build_one, app, store, args, case_id, stop): case_id for case_id in args.cases}
            for future in as_completed(pending):
                try:
                    result = future.result()
                except Exception as exc:
                    result = {'case_id': pending[future], 'status': 'failed', 'error_type': type(exc).__name__, 'error': str(exc)[:1000]}
                results.append(result)
                write_json(args.output / 'progress.json', {'schema': 'case-method-build-progress/v1',
                           'updated_at': now(), 'completed': results, 'expected': len(args.cases)})
                print(canonical({'case_id': result['case_id'], 'status': result['status']}), flush=True)
        profiles = sorted(store.directory.glob('*.json'))
        write_json(args.output / 'profile-manifest.json', {'schema': 'case-method-profile-manifest/v1',
                   'created_at': now(), 'profiles': [{'path': str(path.relative_to(args.output)),
                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in profiles],
                   'case_results': [{'case_id': result['case_id'], 'status': result['status']} for result in results],
                   'read_audits': [{'path': str(path.relative_to(args.output)),
                                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                                  for path in sorted((args.output / 'cases').glob('*/*')) if path.is_file()]}, exclusive=True)
        return 0 if len(results) == len(args.cases) and all(r['status'] == 'completed' for r in results) else 1
    finally:
        if app is not None:
            if changed:
                app.tools.call_public('set_libraries_enabled', {'library_ids': changed, 'enabled': False})
            after = library_flags(app)
            write_json(args.output / 'library-flags-after.json', after, exclusive=True)
            app.agents.close()
            if before != after:
                raise RuntimeError('Library scope changed during the curator run; inspect frozen before/after flags')


if __name__ == '__main__':
    raise SystemExit(main())
