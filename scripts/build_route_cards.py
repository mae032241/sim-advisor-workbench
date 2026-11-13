"""Construct small source-only route cards in independent, bounded sessions.

Explicit immutable case identities are the only task inputs. This script never
loads evaluation questions, answers or reference plans and never builds an index.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from scripts.build_case_methods import (  # Runtime/lifecycle helpers only.
    Audit, DEFAULT_LIBRARY_ROOT, canonical, case_key, create_local_app, digest,
    flags, inherited_mcp_disables, library_flags, now, source_references, write_json,
)

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
READ_TOOLS = frozenset({'read_source', 'read_case_configuration', 'get_case_guide', 'search_knowledge'})
LOCAL_TOOLS = frozenset({'get_case_evidence_directory', 'get_tool_contract', 'submit_method_card', 'read_tool_page'})
MAX_TOOL_BYTES = 7000
INSTRUCTIONS_VERSION = 'source-only-route-card-curator/v2'
SOURCE_INSTRUCTIONS = '''你是方法资料整理员。本会话只整理唯一指定case_id的已有来源，生成可供方法选择的简洁方法卡；不是为用户写仿真方案。
只能读取指定案例、它直接关联的教程，以及当前允许范围中的通用功能手册。不得探索其他案例、测试问题、标准答案、历史方案、评分、文件、外部网页、记忆或其他会话。不得提交save_plan或改变任何资料库。
先read_source读指定案例概要，再get_case_evidence_directory。目录按保存配置所有节点的标题、类型、层级列出完整索引；依next_offset读完nodes页，并读stages页，逐个get_case_guide(stage=实际id)检查实现导航。导航仅提供入口，不是证据。不可只读某类配置最前几项。根据真实节点名称、接口、教程提到的机制与依赖，选择关键节点用准确node_path读取；优先与方法选择有关的物理接口、耦合、研究和变量关系，再按实际需要核查输入、边界、材料和求解设置。目录完整不意味着属性全部已读。
先读直接关联教程正文，把教程的目标、机制、条件和依赖与保存配置对照。教程链接或接口名称不能证明模型已启用该方法。可用search_knowledge(case_id=指定id)搜索本案例来源；需要通用定义、适用条件、接口选择依据或方法比较时，才用kind=manual且省略case_id搜索已允许手册。查询词必须来自本轮真实来源中出现的接口名称或方法术语，并附通用的假设/接口选择/方法比较词，不依据未来任务预设答案。不另搜模型案例。搜索命中不是证据，需read_source读正文。
方法单位应概括一种可复用机制或路线，不按原文件名写标题或summary，也不要只复述案例。至少提交一张卡，确有独立方法时可多张；同类方法使用简洁一致的family。
每张卡九节goals,mechanism,applicability,exclusions,alternatives,required_inputs,coupling_chain,implementation,gaps，每节1到3条短主张。重点解释方法目标与机制、来源支持的选用/排除条件、尚需查明的事实、替代方法的依据、耦合输入输出与依赖、少量可执行入口。无依据就unknown，不为填满字段强编规则、公式、替代路线或参数。
source_text、saved_configuration、api_configuration是不同观察范围；因果解释、教程与配置的对应、迁移判断标llm_inference并写具体局限。unknown不带伪引用。每条非unknown主张复制当前实际返回小页的精确evidence_refs；后端读到但未送达当前页的内容不能引用。不得根据标题、导航、摘要或隐藏属性制造引用。
节点或接口存在不证明在研究中生效或成功求解。保留读取返回的partial、effective_activation与physical_validation限制。在gaps统一写资料覆盖与重要未核实事实；不要在每行堆积重复提醒。只写来源支持的范围。
所有回包均为小页；按next_offset或read_tool_page给出的handle继续。大JSON片页本身不授予完整配置引用，必须使用实际交付的精确引用，不能猜locator。get_tool_contract读取submit_method_card的准确字段；长schema用path和offset分页，不猜结构。只修实际校验错误，不重启或重试会话挑选结果。
通过submit_method_card提交card。提交成功后给很短的完成说明，自然结束本轮。
'''
TRANSPORT_INSTRUCTIONS = '''本轮使用bounded_code_mode工具传输。每个exec单元只能调用一个业务工具一次；不得使用Promise.all、循环、并行调用、批量调用或跨页累积完整结果。不得输出缓存、会话、历史结果数组或多个回包，不得做批量全量dump。
业务工具回包是JSON字符串。先用typeof raw === "string" ? JSON.parse(raw) : raw解码，再读取字段或做投影；不能把未解码的raw当对象取字段，不能text(raw)打印原JSON字符串。每单元只能调用一次text(result)，完整输出当前这一个解码后的有界响应；投影不能替代该响应的完整交付。不得重复打印、拼接多页或展开全量配置。保持实际返回的证据引用、覆盖限制和分页标记。
需要后续内容时另开exec单元，仅调用一次read_tool_page，复制handle、path和offset读取下一小页，只输出该页。每个单元的一次业务工具调用与一次有界解码回包交付完成后，再决定下一次调用。
'''
INSTRUCTIONS = SOURCE_INSTRUCTIONS + TRANSPORT_INSTRUCTIONS


def instruction_manifest():
    return {'instructions_version': INSTRUCTIONS_VERSION,
            'source_instructions_sha256': hashlib.sha256(SOURCE_INSTRUCTIONS.encode('utf-8')).hexdigest(),
            'transport_instructions_sha256': hashlib.sha256(TRANSPORT_INSTRUCTIONS.encode('utf-8')).hexdigest(),
            'instructions_sha256': hashlib.sha256(INSTRUCTIONS.encode('utf-8')).hexdigest()}


def validate_options(args):
    if not 1 <= len(args.cases) <= 6 or len(args.cases) != len(set(args.cases)):
        raise ValueError('--case requires 1 to 6 unique explicit case IDs')
    if any(not isinstance(identity, str) or not identity.strip() or len(identity) > 256 for identity in args.cases):
        raise ValueError('Each case ID must be nonempty and no longer than 256 characters')
    if type(args.parallel) is not int or not 1 <= args.parallel <= 2:
        raise ValueError('--parallel must be 1 or 2')
    if args.timeout <= 0 or not args.model.strip() or not args.effort.strip():
        raise ValueError('Timeout must be positive; model and effort must be nonempty')
    if len(args.scope_library_ids) != len(set(args.scope_library_ids)):
        raise ValueError('--scope-library-id values must be unique')
    args.library_root = args.library_root.expanduser().resolve(strict=True)
    if not (args.library_root / 'catalog.sqlite3').is_file():
        raise ValueError('--library-root must contain an existing production catalog')
    args.output = args.output.expanduser().resolve()
    if (args.output == args.library_root or args.output.is_relative_to(args.library_root)
            or args.library_root.is_relative_to(args.output)):
        raise ValueError('Output must be separate from the production library')
    if (args.output / 'manifest.json').exists() or (args.output / 'route-method-cards').exists():
        raise FileExistsError('This output is already reserved; frozen runs are never overwritten')


def compact_node_headers(provider, case_id):
    """All headers from one hash-verified compact artifact, no property hydration.

    The established compact reader validates the artifact and any API overlay.
    Its metadata connection then reads only node identity/symbol columns. There
    is no model enumeration, catalog scan or new knowledge provider here.
    """
    from advisor.compact_library import _reader
    authority = provider.get_case_authority(case_id)
    context = provider.store._compact_context(authority)
    if context is None:
        return {'state': 'unavailable', 'nodes': [], 'reason': 'No supported compact node metadata'}
    pinned, source_override = context
    reader = _reader(pinned, provider.store.root, source_override)
    with closing(reader._connect()) as database:
        reader._model(database)
        sql = '''SELECT n.id,n.parent_id,t.value,k.value,l.value,y.value,b.value
          FROM nodes n LEFT JOIN symbols t ON t.id=n.tag_id
          LEFT JOIN symbols k ON k.id=n.kind_id LEFT JOIN symbols l ON l.id=n.label_id
          LEFT JOIN symbols y ON y.id=n.type_id LEFT JOIN symbols b ON b.id=n.branch_id ORDER BY n.id'''
        rows = [{'node_path': 'compact:node/' + str(row[0]), 'parent_id': row[1],
                 'tag': row[2], 'kind': row[3], 'title': row[4] or row[2] or row[3],
                 'type': row[5], 'stage': row[6] or 'misc'} for row in database.execute(sql)]
    after = provider.get_case_authority(case_id)
    if digest(authority) != digest(after):
        raise RuntimeError('Case identity changed while reading compact node metadata')
    return {'state': 'complete', 'nodes': rows, 'node_count': len(rows),
            'artifact_sha256': authority.get('data', {}).get('compact_case', {}).get('sha256'),
            'headers_sha256': digest(rows), 'read_scope': 'all_saved_node_headers_only'}


class SourceReader:
    """Assigned-case reads plus genuine general manuals in a frozen live scope."""
    def __init__(self, app, case_id, session_id, audit, scope_library_ids=()):
        self.app, self.case_id, self.session_id, self.audit = app, case_id, session_id, audit
        self.scope_library_ids = tuple(scope_library_ids)
        self.allowed_ids, self.associated_ids, self.backend_reads = {case_id}, {case_id}, []
        self.lock, self.directory = threading.RLock(), None
        self.authority = app.kb.get_case_authority(case_id)

    def _metadata(self, identity):
        if not isinstance(identity, str) or not self.app.kb.source_available(identity):
            raise PermissionError('Source is unavailable under the current library/holdout policy')
        source = self.app.kb.store.get(identity)
        if not source:
            raise FileNotFoundError('Source metadata is missing')
        if self.scope_library_ids and source.get('library_id') not in self.scope_library_ids:
            raise PermissionError('Source lies outside the frozen library scope')
        self.app.kb.store.require_not_heldout(identity, source)
        return source

    def _general_manual(self, source):
        if source.get('type') not in {'manual_topic', 'api_reference'}:
            return False
        library = self.app.kb.store.library(source.get('library_id'), include_deleted=True)
        if not library or library.get('type') not in {'official-manuals', 'user-manuals'}:
            return False
        # Tutorial packages remain restricted to the assigned case association.
        namespace = str(source.get('asset_key') or '').replace('\\', '/').casefold()
        return 'com.comsol.help.models.' not in namespace

    def _permitted_source(self, identity, *, general=False, associated=False):
        source = self._metadata(identity)
        if source.get('type') == 'model_case' and identity != self.case_id:
            raise PermissionError('Other model case overviews cannot enter this session')
        manual = self._general_manual(source)
        if general:
            if not manual:
                raise PermissionError('Unscoped search may return genuine general manuals only')
            return source
        same_case = (source.get('case_group') is not None
                     and source.get('case_group') == self.authority.get('case_group'))
        if identity == self.case_id or same_case or manual:
            return source
        if ((associated or identity in self.associated_ids)
                and source.get('type') in {'case_context', 'method_step', 'manual_topic'}):
            return source
        raise PermissionError('Only assigned-case sources, directly associated tutorials and general manuals are allowed')

    def prepare_directory(self):
        if self.directory is not None:
            return self.directory
        index = compact_node_headers(self.app.kb, self.case_id)
        overview = self('get_case_guide', {'case_id': self.case_id})
        stages = []
        for summary in overview.get('stages', []):
            stage_id = summary.get('id')
            if not isinstance(stage_id, str):
                continue
            detail = self('get_case_guide', {'case_id': self.case_id, 'stage': stage_id})
            stage = next((stage for stage in detail.get('stages', []) if stage.get('id') == stage_id), summary)
            stages.append({'id': stage_id, 'title': stage.get('title') or stage_id,
                           'source_refs': stage.get('source_refs', []),
                           'configuration_requests': stage.get('configuration_requests', []),
                           **{key: stage[key] for key in ('total_node_count', 'selected_node_count', 'summary_truncated') if key in stage}})
        self.directory = dict(index, schema='route-case-evidence-directory/v1', case_id=self.case_id,
                              stages=stages, stage_count=len(stages), guide_state=overview.get('state'),
                              is_read_evidence=False, evidence_refs=[])
        return self.directory

    def directory_page(self, arguments):
        if set(arguments) - {'case_id', 'section', 'offset', 'limit'} or arguments.get('case_id') != self.case_id:
            raise PermissionError('Evidence directory is bound to the assigned case')
        section, offset, limit = arguments.get('section', 'nodes'), arguments.get('offset', 0), arguments.get('limit', 20)
        if section not in {'nodes', 'stages'} or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('Directory pages require nodes/stages, offset>=0, and limit 1..20')
        directory = self.prepare_directory()
        items = directory[section]
        if section == 'stages':
            items = [{key: value for key, value in item.items()
                      if key not in {'source_refs', 'configuration_requests'}}
                     | {'source_count': len(item.get('source_refs', [])),
                        'source_refs': [{key: ref[key] for key in ('id', 'title', 'type', 'role') if key in ref}
                                        for ref in item.get('source_refs', []) if isinstance(ref, dict)],
                        'configuration_request_count': len(item.get('configuration_requests', [])),
                        'detail': {'tool': 'get_case_guide', 'arguments': {'case_id': self.case_id, 'stage': item['id']}}}
                     for item in items]
        page = {key: value for key, value in directory.items() if key not in {'nodes', 'stages'}}
        selected = items[offset:offset + limit]
        while True:
            result = dict(page, section=section, items=selected, offset=offset, returned=len(selected),
                          total=len(items), next_offset=offset + len(selected) if offset + len(selected) < len(items) else None)
            if len(selected) <= 1 or len(json.dumps(result, ensure_ascii=False).encode('utf-8')) <= MAX_TOOL_BYTES - 250:
                return result
            selected.pop()

    def authorize(self, name, arguments):
        """Recheck identities for cached as well as fresh bounded delivery."""
        if name == 'read_source':
            identity = arguments.get('id')
            if identity not in self.allowed_ids:
                raise PermissionError('Read IDs must come from this case or an allowed manual search')
            return self._permitted_source(identity)
        if name in {'get_case_guide', 'read_case_configuration', 'get_case_evidence_directory'}:
            if arguments.get('case_id') != self.case_id:
                raise PermissionError('This session may only read its assigned case_id')
            return self.app.kb.get_case_authority(self.case_id)

    def __call__(self, name, arguments):
        if name not in READ_TOOLS or not isinstance(arguments, dict) or 'session_id' in arguments:
            raise PermissionError('Only published source-only reads are allowed; the session is fixed')
        arguments = deepcopy(arguments)
        self.authorize(name, arguments)
        general = False
        if name == 'search_knowledge':
            if arguments.get('mode', 'qmd_keyword') != 'qmd_keyword':
                raise PermissionError('Only the existing keyword index is allowed')
            case_id = arguments.get('case_id')
            general = case_id is None
            if general and (arguments.get('kind') != 'manual' or 'stage' in arguments):
                raise PermissionError('Unscoped search is limited to general manuals')
            if not general and case_id != self.case_id:
                raise PermissionError('Case search is bound to the assigned identity')
            requested = arguments.get('library_ids', list(self.scope_library_ids))
            if self.scope_library_ids:
                if not isinstance(requested, list) or not set(requested).issubset(self.scope_library_ids):
                    raise PermissionError('Search libraries must remain inside the frozen scope')
                arguments['library_ids'] = requested
        started = time.monotonic()
        try:
            result = self.app.tools.call_public(name, dict(arguments, session_id=self.session_id))
            with self.lock:
                if name == 'search_knowledge':
                    visible = []
                    for item in result.get('results', []):
                        try:
                            self._permitted_source(item.get('id'), general=general, associated=not general)
                        except (PermissionError, FileNotFoundError):
                            continue
                        visible.append(item)
                        self.allowed_ids.add(item['id'])
                        if not general:
                            self.associated_ids.add(item['id'])
                    result = dict(result, results=visible)
                if name in {'get_case_guide', 'read_source'}:
                    associated = name == 'get_case_guide' or arguments.get('id') == self.case_id
                    for identity in source_references(result):
                        try:
                            self._permitted_source(identity, associated=associated)
                        except (PermissionError, FileNotFoundError):
                            continue
                        self.allowed_ids.add(identity)
                        if associated:
                            self.associated_ids.add(identity)
                    # read_source links are genuine navigation, not evidence.
                    for item in result.get('linked_topics', []):
                        identity = item.get('id')
                        try:
                            source = self._permitted_source(identity)
                            if identity in self.allowed_ids or self._general_manual(source):
                                self.allowed_ids.add(identity)
                        except (PermissionError, FileNotFoundError):
                            continue
                row = {'tool': name, 'arguments': arguments, 'result_sha256': digest(result),
                       'source_id': result.get('id') or result.get('case_id'),
                       'source': result.get('source'), 'read_scope': result.get('read_scope'),
                       'partial': result.get('partial'), 'extraction_coverage': result.get('extraction_coverage')}
                self.backend_reads.append(row)
            self.audit.append(dict(row, kind='source_read', success=True,
                                   seconds=round(time.monotonic() - started, 4)))
            return result
        except Exception as exc:
            self.audit.append({'kind': 'source_read', 'tool': name, 'arguments': arguments,
                               'success': False, 'error_type': type(exc).__name__})
            raise


def curator_definitions(builder, bounded):
    from advisor.planning_service import tool_definitions
    definitions = {item['name']: deepcopy(item) for item in tool_definitions(bound=True)}
    own = {item['name']: deepcopy(item) for item in builder.tool_definitions()}
    selected = {name: definitions[name] for name in READ_TOOLS}
    for name in {'read_source', 'read_case_configuration', 'submit_method_card'}:
        selected[name] = own[name]
    selected['read_source']['inputSchema']['properties']['length'].update(maximum=1200, default=1000)
    selected['read_case_configuration']['inputSchema']['properties']['limit'].update(maximum=1, default=1)
    contract = definitions['get_tool_contract']
    contract['inputSchema']['properties']['length'].update(maximum=1200, default=1200)
    selected['get_tool_contract'] = contract
    selected['get_case_evidence_directory'] = {
        'name': 'get_case_evidence_directory',
        'description': 'Page all assigned-case saved node headers or every guide stage. Navigation only; use exact node_path to read real configuration. Read every nodes page before selecting method evidence.',
        'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['case_id'],
                        'properties': {'case_id': {'type': 'string'},
                                       'section': {'type': 'string', 'enum': ['nodes', 'stages'], 'default': 'nodes'},
                                       'offset': {'type': 'integer', 'minimum': 0, 'default': 0},
                                       'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 20}}}}
    selected.update({item['name']: deepcopy(item) for item in bounded.tool_definitions()})
    # Replace any original read definitions supplied by the bounded adapter.
    selected = {name: item for name, item in selected.items() if name in READ_TOOLS | LOCAL_TOOLS}
    if set(selected) != READ_TOOLS | LOCAL_TOOLS:
        raise ValueError('Route construction tool allowlist is incomplete')
    return [selected[name] for name in sorted(selected)]


def runtime_command(workspace):
    from advisor.config import codex_command
    from advisor.runtime_adapters import runtime_command as base_command
    command = codex_command()
    if not command:
        raise RuntimeError('Codex CLI was not found')
    return base_command(command) + flags(dict(PROFILE, **inherited_mcp_disables(Path(workspace)))) + ['--strict-config']


def inspect_runtime_delivery(runtime, thread_id):
    """Inspect actual persisted tool outputs, never private reasoning text."""
    from advisor.tool_delivery_audit import locate_rollout, audit_rollout
    hint = None
    request = getattr(runtime, '_request', None)
    if callable(request):
        try:
            thread = request('thread/read', {'threadId': thread_id, 'includeTurns': False}).get('thread', {})
            hint = thread.get('path')
        except (RuntimeError, ValueError, KeyError):
            pass
    path = locate_rollout(thread_id, hint=hint)
    return audit_rollout(path, expected_thread_id=thread_id, max_output_bytes=8192, require_outputs=True)


def build_one(app, store, args, case_id, stop, *, runtime_factory=None, delivery_inspector=None):
    from advisor.route_cards import RouteCardBuilder
    from advisor.bounded_knowledge import BoundedKnowledge
    from advisor.codex_runtime import CodexRuntime
    from advisor.tool_contracts import read_contract
    if stop.is_set() or (args.output / 'stop.request').exists():
        stop.set()
        return {'case_id': case_id, 'status': 'stopped', 'card_submitted': False}
    key = case_key(case_id)
    case_dir = args.output / 'cases' / key
    case_dir.mkdir(parents=True, exist_ok=False)
    audit, raw_audit = Audit(case_dir / 'audit.jsonl'), Audit(case_dir / 'raw-audit.jsonl')
    session = app.sessions.create('仅根据已允许案例与关联来源整理通用方法卡。', None)
    reader = SourceReader(app, case_id, session['id'], audit, args.scope_library_ids)
    completed, submitted, active = threading.Event(), threading.Event(), threading.Event()
    active.set()
    result = {'case_id': case_id, 'session_id': session['id'], 'status': 'running', 'started_at': now()}
    visible, cards, tool_deliveries, directory_reads = [], [], [], []
    directory_seen = {'nodes': set(), 'stages': set()}
    lock, tool_lock = threading.RLock(), threading.RLock()
    definitions, builder = [], None

    def raw_call(name, arguments):
        if name == 'get_tool_contract':
            arguments = dict(arguments)
            arguments['length'] = min(arguments.get('length', 1200), 1200)
            return read_contract(definitions, arguments)
        if name == 'get_case_evidence_directory':
            return reader.directory_page(arguments)
        if name == 'submit_method_card':
            directory = reader.prepare_directory()
            if directory.get('state') != 'complete':
                raise ValueError('A complete saved-node header index is required before card submission')
            missing_nodes = {node['node_path'] for node in directory['nodes']} - directory_seen['nodes']
            missing_stages = {stage['id'] for stage in directory['stages']} - directory_seen['stages']
            if missing_nodes or missing_stages:
                raise ValueError('Read all evidence-directory nodes/stages pages before submission: '
                                 + str(len(missing_nodes)) + ' node headers and '
                                 + str(len(missing_stages)) + ' stage headers remain undelivered')
            value = builder.call(name, arguments)
            audit.append({'kind': 'submit', 'success': value.get('saved') is True,
                          'result': value, 'input_sha256': digest(arguments)})
            if value.get('saved') is True:
                submitted.set()
                cards.append({'method_id': value.get('method_id'), 'card_sha256': digest(arguments['card'])})
            return value
        return reader(name, arguments)

    def authorize(name, arguments):
        reader.authorize(name, arguments)
        builder.authorize_read(name, arguments)

    def observe(name, arguments, value):
        builder.record_delivery(name, arguments, value)
        if name == 'get_case_evidence_directory' and isinstance(value.get('items'), list):
            section = value.get('section')
            if section in directory_seen:
                identity_key = 'node_path' if section == 'nodes' else 'id'
                directory_seen[section].update(item[identity_key] for item in value['items']
                                               if isinstance(item, dict) and isinstance(item.get(identity_key), str))
                directory_reads.append({'section': section, 'offset': value.get('offset'),
                                        'returned': len(value['items']), 'next_offset': value.get('next_offset'),
                                        'delivered_sha256': digest(value)})
        refs = value.get('evidence_refs', []) if isinstance(value, dict) else []
        if name in {'read_source', 'read_case_configuration'}:
            row = {'tool': name, 'arguments': deepcopy(arguments), 'delivered_sha256': digest(value),
                   'delivered_bytes': len(json.dumps(value, ensure_ascii=False).encode('utf-8')),
                   'evidence_refs': refs,
                   'partial': value.get('partial'), 'read_scope': value.get('read_scope')}
            visible.append(row)
            audit.append(dict(row, kind='visible_delivery'))

    bounded = BoundedKnowledge(raw_call, max_bytes=MAX_TOOL_BYTES, observer=observe, authorize=authorize)
    builder = RouteCardBuilder(store, bounded.call, case_id=case_id)
    definitions = curator_definitions(builder, bounded)
    write_json(case_dir / 'tools.json', definitions, exclusive=True)

    def on_event(event):
        audit.append(dict(event, kind='runtime_event'))
        if event.get('type') == 'turn' and event.get('event') == 'completed':
            with lock:
                result['turn_status'] = event.get('status')
            completed.set()

    def on_tool(name, arguments):
        with tool_lock:
            try:
                if not active.is_set() or stop.is_set() or (args.output / 'stop.request').exists():
                    raise InterruptedError('The case session is closed or stopped')
                if name not in READ_TOOLS | LOCAL_TOOLS:
                    raise PermissionError('Tool is outside the source-only constructor allowlist')
                value = bounded.call(name, arguments)
            except (ValueError, PermissionError, FileNotFoundError, RuntimeError, InterruptedError) as exc:
                detail = getattr(exc, 'to_dict', None)
                error = detail() if callable(detail) else {'error_type': type(exc).__name__, 'error': str(exc)}
                # Even feedback passes the bounded adapter; never slice tool text.
                feedback = BoundedKnowledge(lambda _name, _args: {'success': False, 'error': error}, max_bytes=MAX_TOOL_BYTES)
                value = feedback.call('tool_feedback', {})
                audit.append({'kind': 'tool_feedback', 'tool': name, 'success': False, 'error_type': type(exc).__name__})
            output_bytes = len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
            if output_bytes > MAX_TOOL_BYTES:
                raise RuntimeError('Bounded delivery exceeded the UTF-8 byte budget')
            tool_deliveries.append({'tool': name, 'bytes': output_bytes, 'sha256': digest(value)})
            raw_audit.append({'kind': 'callback_delivery', 'tool': name, 'arguments': arguments,
                              'result': value, 'bytes': output_bytes})
            return value

    workspace = args.output / 'runtime' / 'workspaces' / key
    workspace.mkdir(parents=True, exist_ok=False)
    runtime, started = None, time.monotonic()
    try:
        directory = reader.prepare_directory()
        write_json(case_dir / 'evidence-directory.json', directory, exclusive=True)
        if stop.is_set() or (args.output / 'stop.request').exists():
            raise InterruptedError('Stopped before model launch')
        command = runtime_command(workspace) if runtime_factory is None else ['test-runtime']
        runtime = (runtime_factory or CodexRuntime)(command=command, cwd=workspace, on_event=on_event,
                                                   on_tool=on_tool, model=args.model, reasoning_effort=args.effort)
        connection = runtime.start(tools=definitions, instructions=INSTRUCTIONS)
        result['thread_id'] = connection['thread_id']
        runtime.send('本会话唯一允许case_id：' + case_id + '\n请按来源目录、真实正文及配置，构造并提交通用方法卡。')
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
                result.update(status='runtime_error', error_metadata=state.get('error_metadata'))
                break
        if completed.is_set():
            result['status'] = ('completed' if submitted.is_set() and result.get('turn_status') == 'completed'
                                else 'failed' if result.get('turn_status') != 'completed' else 'no_submission')
        result['runtime_status'] = runtime.status()
    except Exception as exc:
        result.update(status='failed', error_type=type(exc).__name__, error=str(exc))
    finally:
        active.clear()
        if runtime is not None:
            # Resolve only path metadata while connected; audit after close/flush.
            inspector = delivery_inspector or inspect_runtime_delivery
            try:
                if delivery_inspector is None:
                    from advisor.tool_delivery_audit import locate_rollout, audit_rollout
                    hint = None
                    request = getattr(runtime, '_request', None)
                    if callable(request) and result.get('thread_id'):
                        try:
                            metadata = request('thread/read', {'threadId': result['thread_id'], 'includeTurns': False})
                            hint = metadata.get('thread', {}).get('path')
                        except (RuntimeError, ValueError, KeyError):
                            pass
                    runtime.close()
                    path = locate_rollout(result.get('thread_id'), hint=hint)
                    delivery = audit_rollout(path, expected_thread_id=result.get('thread_id'),
                                             max_output_bytes=8192, require_outputs=True,
                                             expected_response_sha256=[row['sha256'] for row in tool_deliveries])
                else:
                    runtime.close()
                    delivery = inspector(runtime, result.get('thread_id'))
                result['delivery_audit'] = delivery
                if delivery.get('tool_output_count', 0) < len(tool_deliveries):
                    delivery['valid'] = False
                    delivery.setdefault('issues', []).append('Actual rollout has fewer tool outputs than callback deliveries')
                if not delivery.get('valid'):
                    result['status'] = 'invalid_delivery'
            except Exception as exc:
                runtime.close()
                result.update(status='invalid_delivery', delivery_audit={'valid': False,
                              'issues': ['Actual rollout delivery could not be verified'], 'error_type': type(exc).__name__})
        with tool_lock, reader.lock:
            write_json(case_dir / 'source-manifest.json', {
                'schema': 'route-card-source-read-manifest/v1', 'case_id': case_id, 'session_id': session['id'],
                'source_only': True, 'no_questions_access': True, 'bounded_code_mode': True,
                'backend_reads': reader.backend_reads, 'visible_deliveries': visible,
                'callback_deliveries': tool_deliveries, 'bounded_audit': getattr(bounded, 'audit', []),
                'source_bindings': deepcopy(builder._receipts.bindings),
                'directory_coverage': {section: sorted(identities) for section, identities in directory_seen.items()},
                'directory_page_calls': directory_reads,
                **instruction_manifest()}, exclusive=True)
            write_json(case_dir / 'cards.json', {'schema': 'route-card-case-submissions/v1',
                       'case_id': case_id, 'cards': cards}, exclusive=True)
        result.update(card_submitted=submitted.is_set(), card_count=len(cards),
                      finished_at=now(), seconds=round(time.monotonic() - started, 3))
        write_json(case_dir / 'result.json', result, exclusive=True)
    return result


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--case', action='append', dest='cases', required=True)
    cli.add_argument('--scope-library-id', action='append', dest='scope_library_ids', default=[])
    cli.add_argument('--library-root', type=Path, default=DEFAULT_LIBRARY_ROOT)
    cli.add_argument('--output', type=Path, required=True)
    cli.add_argument('--model', default='gpt-6-luna')
    cli.add_argument('--effort', default='high')
    cli.add_argument('--parallel', type=int, default=2)
    cli.add_argument('--timeout', type=float, default=1800)
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    validate_options(args)
    args.output.mkdir(parents=True, exist_ok=True)
    from advisor.route_cards import RouteCardStore, SCHEMA, card_schema
    manifest = {'schema': 'route-card-build/v1', 'created_at': now(), 'case_ids': args.cases,
                'source_only': True, 'no_questions_access': True, 'bounded_code_mode': True,
                'model': args.model, 'effort': args.effort, 'parallel_limit': args.parallel,
                'timeout_seconds': args.timeout, 'library_root': str(args.library_root),
                'scope_library_ids': args.scope_library_ids, 'profile': PROFILE,
                'card_schema': SCHEMA, 'card_schema_sha256': digest(card_schema()),
                'builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'card_module_sha256': hashlib.sha256((APP / 'advisor/route_cards.py').read_bytes()).hexdigest(),
                'bounded_module_sha256': hashlib.sha256((APP / 'advisor/bounded_knowledge.py').read_bytes()).hexdigest(),
                'max_callback_utf8_bytes': MAX_TOOL_BYTES, 'silent_retries': False,
                'stop_request': str(args.output / 'stop.request'), **instruction_manifest()}
    write_json(args.output / 'manifest.json', manifest, exclusive=True)
    app, before, changed, results = None, None, [], []
    stop = threading.Event()
    try:
        app = create_local_app(args.output, args.library_root)
        before = library_flags(app)
        write_json(args.output / 'library-flags-before.json', before, exclusive=True)
        if set(args.scope_library_ids) - set(before):
            raise ValueError('Experiment scope contains unknown libraries')
        if any(before[identity]['deleted_at'] is not None for identity in args.scope_library_ids):
            raise ValueError('Experiment scope contains deleted libraries')
        if not args.scope_library_ids:
            args.scope_library_ids = [identity for identity, state in before.items() if state['enabled'] and state['deleted_at'] is None]
        changed = [identity for identity in args.scope_library_ids if not before[identity]['enabled']]
        if changed:
            app.tools.call_public('set_libraries_enabled', {'library_ids': changed, 'enabled': True})
        write_json(args.output / 'library-flags-active.json', library_flags(app), exclusive=True)
        authorities = []
        for case_id in args.cases:
            authority = app.kb.get_case_authority(case_id)
            if authority['library_id'] not in args.scope_library_ids:
                raise PermissionError('Assigned case is outside the frozen library scope')
            authorities.append({key: authority[key] for key in ('id', 'library_id', 'object_id', 'revision_id',
                                'asset_key', 'asset_hash', 'case_group', 'type') if key in authority}
                               | {'authority_sha256': digest(authority)})
        write_json(args.output / 'source-manifest.json', {'schema': 'route-card-frozen-sources/v1',
                   'cases': authorities, 'scope_library_ids': args.scope_library_ids,
                   'source_only': True, 'no_questions_access': True, **instruction_manifest()}, exclusive=True)
        store = RouteCardStore(args.output, app.kb)
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            pending = {pool.submit(build_one, app, store, args, case_id, stop): case_id for case_id in args.cases}
            for future in as_completed(pending):
                try:
                    result = future.result()
                except Exception as exc:
                    result = {'case_id': pending[future], 'status': 'failed', 'error_type': type(exc).__name__, 'error': str(exc)}
                results.append(result)
                write_json(args.output / 'progress.json', {'schema': 'route-card-build-progress/v1',
                           'updated_at': now(), 'completed': results, 'expected': len(args.cases)})
                print(canonical({'case_id': result['case_id'], 'status': result['status']}), flush=True)
        valid = len(results) == len(args.cases) and all(row['status'] == 'completed' for row in results)
        frozen = store.freeze_catalog() if valid else {'frozen': False, 'reason': 'Every case must complete with verified actual tool delivery'}
        write_json(args.output / 'cards-manifest.json', {'schema': 'route-card-build-output/v1',
                   'created_at': now(), 'valid': valid, 'catalog': frozen, 'case_results': results,
                   'artifacts': [{'path': str(path.relative_to(args.output)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                                 for path in sorted((args.output / 'cases').glob('*/*')) if path.is_file()]}, exclusive=True)
        return 0 if valid else 1
    finally:
        if app is not None:
            if changed:
                app.tools.call_public('set_libraries_enabled', {'library_ids': changed, 'enabled': False})
            after = library_flags(app)
            write_json(args.output / 'library-flags-after.json', after, exclusive=True)
            app.agents.close()
            if before != after:
                raise RuntimeError('Library scope changed during the run; inspect frozen before/after flags')


if __name__ == '__main__':
    raise SystemExit(main())
