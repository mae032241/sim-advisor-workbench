"""Paired raw-originals / current-library trial, through the product Agent CLI.

Only public tasks are imported. A file allowlist is shared by both arms; no
native disk/shell/browser tools, test references, rubrics or previous answers
are registered. Derived case navigation is prepared on its first requested
training case, and that construction cost stays inside the recorded turn.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid
from types import MethodType, SimpleNamespace
from urllib.parse import unquote, urlsplit

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from scripts import benchmark_product_batch as batch
from scripts import benchmark_route_selection as base
from scripts.benchmark_product_setup import create_task, _public_file
from advisor.bounded_knowledge import BoundedKnowledge
from advisor.plan_drafts import schema_errors
from advisor.tool_contracts import read_contract, describe_contract
from advisor.tool_delivery_audit import response_sha256

MODEL, EFFORT = 'gpt-6-luna', 'high'
CASES = ('case-01', 'case-02', 'case-09')
ARMS = ('raw_address', 'indexed')
COMMON = {'get_task_context', 'get_original_scope', 'get_tool_contract', 'read_tool_page'}
INDEXED = {'get_library_scope', 'search_knowledge', 'read_source', 'search_cases',
           'get_case_guide', 'get_case_navigation', 'read_case_configuration',
           'get_html_navigation', 'read_html_section', 'inspect_case_source'}
PROFILE = dict(batch.PROFILE)
LEGACY_INSTRUCTIONS = '''你是通用COMSOL仿真方案助手。依据用户需求和初始几何判断技术路线，核对适用条件，不预设某个物理接口或参考案例。只能使用本会话列出的工具，原始资料目录不保证每个文件都可访问。
先读get_task_context；可用get_original_scope获得批准的原始案例和手册目录，自主查找、读取与按需解析。若有工作台资料库的结构导航，优先定位相关父级章节或案例节点，再读原文与实际配置；关键词检索补充术语与缺口。没有这些工具时直接使用普通原件工具，不猜不存在的工具名。工具导航、文件名、案例接口存在都不证明适用、启用或求解成功；区分来源直接事实、工程推断与未知。
工具回包是小页，按next_offset/has_more等实际游标继续；一般大对象可用read_tool_page按JSON指针取字段。未返回的内容不算已读。不要求遍历所有资料。
每个exec单元仅调用一次业务工具，输出该次解码后的完整小页；不要Promise.all、循环调用或拼接全文输出。可在JavaScript中处理已经读到的数据。JSON字符串先JSON.parse再访问字段，避免截断。禁止试图读取未授权路径、扩大材料范围、调用未列出的工具或恢复历史会话。
此轮只交付技术路线判断，最终返回一个JSON对象，字段为chosen_route、alternatives、applicability_conditions、unresolved、sources。sources可引用实际读过的原始路径及页/节点/字符范围，或实际读过的资料库来源与定位。无需保存方案或修改工作台状态，不声称已运行仿真。'''
LEGACY_MESSAGE = '''先用get_task_context完整阅读公开需求及几何/模型事实，必要时按read_tool_page读完。本轮只形成COMSOL技术路线判断。
自主使用提供的原始资料与工具查证，比较合理候选方法：各自保留哪些现象、依赖哪些假设和输入，当前满足、冲突或未知的条件是什么，为什么选择或排除。对首选路线明确核心未知量与几何解释、材料关系、激励/边界形式以及跨域/接口量的传递关系；允许条件路线，缺证据须指出。无需完整网格、求解器或菜单步骤，不声称已求解。
最终回复一个JSON对象，含chosen_route、alternatives、applicability_conditions、unresolved、sources。说明必要的路线关系，不为追求短回复省掉关键耦合。不要保存或修改方案、步骤、设置与资料库范围。'''
SUFFIXES = {'.mph', '.pdf', '.html', '.htm', '.txt', '.md', '.csv', '.xml', '.m', '.java', '.json', '.stp', '.step'}


_EXECUTION_RULES = '''你是通用COMSOL仿真方案助手。以完整公开需求、输入和几何事实为任务依据，不预设物理接口或参考案例。规划深度由本次任务模式明确指定，不能自行把用户的实施需求缩成接口推荐。
只能使用本会话实际提供的工具及其schema。先读get_task_context完整理解公开需求、输入与几何事实，必要时按read_tool_page续读；用get_original_scope查看批准的原始资料范围。根据问题查找相关手册和案例，读取原文与实际配置以支持判断；工具导航、文件名和接口存在本身不证明适用、生效或求解成功。
工具回包是小页，按next_offset/has_more等实际游标继续，一般大对象用read_tool_page按JSON指针取字段。每个exec单元仅调用一次业务工具，完整输出该次小页，不循环调用或拼接全文。可用JavaScript处理已经读到的数据；JSON字符串先JSON.parse再访问字段，未返回内容不算已读。
禁止读取未授权路径、扩大资料范围、调用未列出的工具、恢复历史会话或修改原始模型。此轮不运行求解，也不修改工作台状态；最终交付保存在本轮可见JSON中，由控制器原样落盘。'''

_IMPLEMENTATION_MESSAGE = (APP / 'prompts/comsol_implementation_task.v1.txt').read_text(encoding='utf-8').strip()


def task_mode(*, legacy=False, mode=None):
    selected = mode or ('route' if legacy else 'implementation')
    if selected not in {'route', 'implementation'} or legacy and selected != 'route':
        raise ValueError('task mode must be route/implementation; legacy replay requires route')
    return selected


def effective_instructions(*, legacy=False, mode=None):
    """Keep research/transport rules shared; explicitly select the task depth."""
    selected = task_mode(legacy=legacy, mode=mode)
    if legacy:
        return LEGACY_INSTRUCTIONS
    from advisor.research_policy import research_instructions
    final_format = effective_message(mode=selected).split('最终', 1)[-1]
    return _EXECUTION_RULES + '\n\n' + research_instructions(selected) + '\n\n最终' + final_format


def effective_message(*, legacy=False, mode=None):
    return LEGACY_MESSAGE if task_mode(legacy=legacy, mode=mode) == 'route' else _IMPLEMENTATION_MESSAGE


INSTRUCTIONS = effective_instructions()
MESSAGE = effective_message()


def emit(event, **value):
    print(json.dumps({'event': event, **value}, ensure_ascii=False), flush=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def protected_companion_stems(policy):
    stems = {Path(asset.split(':', 1)[-1]).stem.casefold() for _, asset in policy.asset_keys
             if Path(asset.split(':', 1)[-1]).suffix.casefold() == '.mph'}
    for value in policy.source_paths:
        parsed = urlsplit(value)
        path = Path(unquote(parsed.path if parsed.scheme == 'file' else value))
        if path.suffix.casefold() == '.mph':
            stems.add(path.stem.casefold())
    return frozenset(stems)


def protected_companion(path, stems):
    """Exclude model + tutorial + exported parameter/variable/code companions."""
    return any(part.casefold() == stem or any(part.casefold().startswith(stem + separator) for separator in ('.', '_', '-'))
               for part in Path(path).parts for stem in stems)


def index_argument_identities(name, args):
    fields = {'read_source': ('id',), 'get_html_navigation': ('node_id', 'source_id'),
              'read_html_section': ('node_id',), 'get_case_navigation': ('case_id', 'manual_node_id')}.get(name, ('case_id',))
    return [args[field] for field in fields if isinstance(args.get(field), str) and args[field]]


def filter_index_navigation(value, allowed):
    """Drop nested discovery records outside the common physical originals."""
    def clean(item):
        if isinstance(item, list):
            return [kept for entry in item if (kept := clean(entry)) is not None]
        if not isinstance(item, dict):
            return item
        manual = item.get('manual')
        if isinstance(manual, dict) and isinstance(manual.get('node_id'), str) and not allowed(manual['node_id']):
            return None
        for key in ('id', 'source_id', 'case_id', 'node_id', 'manual_node_id'):
            identity = item.get(key)
            if isinstance(identity, str) and identity.startswith(('lo-', 'tree-', 'html-')) and not allowed(identity):
                return None
        return {key: clean(entry) for key, entry in item.items()}
    result = clean(value)
    if result is None:
        raise PermissionError('Navigation origin is outside the common material scope')
    for field in ('items', 'matches', 'results'):
        if isinstance(result.get(field), list):
            result['returned'] = len(result[field])
    if result != value:
        result['material_filter'] = 'common_approved_originals'
        result['pagination_scope'] = 'provider_cursor_before_common_material_filter'
    return result


def freeze_scope(store, scope):
    """Instance-local overlay only; callers must verify the disk-backed gate too."""
    allowed = frozenset(scope)
    library, libraries = store.library, store.libraries
    def one(self, identity, include_deleted=False):
        value = copy.deepcopy(library(identity, include_deleted=include_deleted))
        value['enabled'] = value['id'] in allowed and not value.get('deleted_at')
        return value
    def many(self, include_deleted=False):
        return [dict(value, enabled=value['id'] in allowed and not value.get('deleted_at'))
                for value in libraries(include_deleted=include_deleted)]
    def searchable(self, requested=None):
        if requested is not None and (not isinstance(requested, list) or any(not isinstance(value, str) for value in requested)):
            raise ValueError('Expected a list of library identities')
        live = {value['id'] for value in libraries(include_deleted=True)
                if value['id'] in allowed and not value.get('deleted_at')}
        return sorted(live) if requested is None else list(dict.fromkeys(value for value in requested if value in live))
    store.library, store.libraries = MethodType(one, store), MethodType(many, store)
    store.searchable_library_ids = MethodType(searchable, store)
    return libraries


def assert_search_scope_agreement(store, scope):
    """Never run an indexed treatment whose backend sees a different scope.

    Sharing a LibraryStore instance is insufficient: LibrarySearch and its SQL
    dependency gates intentionally read persisted lifecycle flags themselves.
    Do not bypass those gates to accommodate an experimental overlay.
    """
    from advisor.library_search import LibrarySearch
    expected = set(store.searchable_library_ids(list(scope)))
    backend = LibrarySearch.for_maintenance(store.root / 'search')._searchable_library_ids()
    if backend is None or set(backend) != expected:
        actual = set(backend or ())
        raise RuntimeError('Indexed benchmark scope disagrees with the persisted backend: '
                           f'expected={len(expected)}, backend={len(actual)}, '
                           f'missing={len(expected - actual)}, unexpected={len(actual - expected)}. '
                           'Use an isolated catalog with consistent lifecycle flags; '
                           'an instance-local overlay cannot enable indexed retrieval.')
    return {'expected_count': len(expected), 'backend_count': len(backend), 'agreement': True}


def resource_inventory(store, scope):
    """Security inventory only: no headings, abstracts, content index or ranking."""
    policy = store.evaluation_policy()
    if store.evaluation_set_summaries()['retrieval_enabled']:
        raise PermissionError('The administrator holdout gate must be disabled for retrieval')
    libraries = [store.library(identity) for identity in scope]
    roots = sorted({str(Path(root['path']).resolve(strict=True)) for lib in libraries for root in lib['roots']})
    forbidden, stems = set(), protected_companion_stems(policy)
    for library_id, asset in policy.asset_keys:
        if library_id not in scope or ':' not in asset:
            continue
        root_id, relative = asset.split(':', 1)
        lib = store.library(library_id)
        for root in lib['roots']:
            if root['id'] == root_id:
                path = Path(root['path']) / relative
                forbidden.add(str(path.absolute()).casefold())
    path_rules = set()
    for value in policy.source_paths | policy.document_relpaths:
        parsed = urlsplit(value)
        path_rules.add(unquote(parsed.path if parsed.scheme == 'file' else value).replace('\\', '/').strip('/').casefold())
    files, excluded, seen = [], 0, set()
    for root in roots:
        anchor = Path(root)
        if anchor == APP or anchor in APP.parents or APP in anchor.parents:
            raise PermissionError('A resource root overlaps private project/evaluation files')
        for current, directories, names in os.walk(anchor, followlinks=False):
            current = Path(current)
            directories[:] = sorted(name for name in directories if not (current / name).is_symlink()
                                    and not getattr(current / name, 'is_junction', lambda: False)())
            for name in sorted(names):
                path = current / name
                if path.suffix.casefold() not in SUFFIXES or path.is_symlink() or not path.is_file():
                    continue
                absolute, normalized = str(path.absolute()), path.absolute().as_posix().strip('/').casefold()
                if absolute.casefold() in seen:
                    continue
                seen.add(absolute.casefold())
                parts = normalized.split('/')
                blocked = (absolute.casefold() in forbidden or protected_companion(path, stems)
                           or any('/'.join(parts[index:]) in path_rules for index in range(len(parts)))
                           or name.casefold() == 'toc.xml' and any(part.casefold().startswith('com.comsol.help.models.') for part in path.parts))
                if blocked:
                    excluded += 1
                    continue
                # Do not pre-read all manuals merely to hand over addresses.
                # Unknown content copies are denied by hash on the actual raw
                # read; explicit heldout paths/companions were removed above.
                fingerprint = batch.sha(path) if path.suffix.casefold() == '.mph' else None
                if fingerprint is not None and fingerprint in policy.source_hashes:
                    excluded += 1
                    continue
                info = path.stat()
                files.append({'path': absolute, 'sha256': fingerprint, 'bytes': info.st_size,
                              'mtime_ns': info.st_mtime_ns})
        emit('resource_root_checked', roots_done=roots.index(root) + 1, roots_total=len(roots), approved_files=len(files))
    if not files or not any(Path(row['path']).suffix.casefold() == '.mph' for row in files):
        raise ValueError('No approved original model corpus')
    return {'schema': 'raw-address-resource-inventory/v1', 'roots': roots, 'files': files,
            'excluded_count': excluded, 'content_index_built': False,
            'policy_sha256': digest({key: sorted(getattr(policy, key)) for key in
                ('citation_ids', 'object_ids', 'case_groups', 'asset_keys', 'source_hashes', 'document_relpaths', 'source_paths')})}


def reuse_inventory(path, store, scope):
    """Reuse the controller-owned security scan; no second corpus-wide hash pass."""
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    policy = store.evaluation_policy()
    expected = digest({key: sorted(getattr(policy, key)) for key in
        ('citation_ids', 'object_ids', 'case_groups', 'asset_keys', 'source_hashes', 'document_relpaths', 'source_paths')})
    roots = sorted({str(Path(root['path']).resolve(strict=True)) for identity in scope for root in store.library(identity)['roots']})
    if (value.get('schema') != 'raw-address-resource-inventory/v1' or value.get('policy_sha256') != expected
            or value.get('roots') != roots or not value.get('files')):
        raise PermissionError('The resource inventory no longer matches the scope and exclusion policy')
    root_set = {Path(root) for root in roots}
    for item in value['files']:
        original = Path(item['path'])
        if (not original.is_absolute() or original.suffix.casefold() not in SUFFIXES
                or not root_set.intersection((original, *original.parents))
                or item.get('sha256') is not None and not re.fullmatch('[a-f0-9]{64}', item['sha256'])):
            raise PermissionError('Invalid original resource entry')
    stems = protected_companion_stems(policy)
    retained = [item for item in value['files'] if not protected_companion(item['path'], stems)]
    value['excluded_count'] += len(value['files']) - len(retained)
    value['files'] = retained
    value['companion_exclusion_version'] = 'protected-original-stem-prefix/v2'
    return value


def definitions(arm, raw):
    from advisor.agent_sessions import planning_tools
    tools = [copy.deepcopy(tool) for tool in planning_tools()
             if tool['name'] == 'get_task_context' or arm == 'indexed' and tool['name'] in INDEXED]
    tools.extend(raw.tool_definitions())
    tools.extend(BoundedKnowledge(lambda *_: {}).tool_definitions())
    tools.append({'name': 'get_original_scope', 'description': '批准的原始资料目录与本任务输入文件；只是原地址，不是内容索引。使用普通文件工具自主定位，未批准文件不可读。',
                  'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}})
    tools.append({'name': 'get_tool_contract', 'description': '查看本会话实际工具的输入契约，按offset/next_offset分页。',
                  'inputSchema': {'type': 'object', 'required': ['tool'], 'properties': {'tool': {'type': 'string'}, 'path': {'type': 'string'},
                     'offset': {'type': 'integer', 'minimum': 0}, 'length': {'type': 'integer', 'minimum': 1, 'maximum': 1200}},
                     'additionalProperties': False}})
    for tool in tools:
        if tool['name'] == 'read_source':
            tool['inputSchema']['properties']['length'].update(maximum=1200, default=1000)
        tool['description'] = tool['description'].split('\n输入契约', 1)[0]
        tool['description'] = describe_contract(tool)
    return sorted(tools, key=lambda item: item['name'])


def install_tools(app, tasks, resources, here, lock):
    from advisor.raw_resource_tools import RawResourceTools
    from advisor.case_tree import CaseTreeRepository, build_case_trees
    original = app.agents.runtime_tool
    raw, contracts, pagers = {}, {}, {}
    build_lock = threading.RLock()
    ordinary_paths = [entry['path'] for entry in resources['files']]
    approved = frozenset(os.path.normcase(str(Path(value).absolute())) for value in ordinary_paths)
    pinned_policy = app.libraries.evaluation_policy()
    def root_authority_stamp():
        with closing(app.libraries.connect()) as db:
            rows = db.execute('SELECT id,body FROM libraries ORDER BY id').fetchall()
        return digest([(row[0], json.loads(row[1])) for row in rows])
    pinned_roots = root_authority_stamp()
    def check_frozen_authority():
        policy = app.libraries.evaluation_policy()
        if policy != pinned_policy or root_authority_stamp() != pinned_roots:
            raise PermissionError('Frozen material roots or exclusion policy changed; cached results cannot be reused')
    def indexed_original_allowed(identity, *, navigation=False):
        from advisor.html_tree_tools import is_html_source_id
        if is_html_source_id(identity):
            record = app.tools.html_trees._record_for(node_id=identity)
            if not record:
                return False
            with app.tools.html_trees._open(record) as index:
                node = index.node(identity)
                app.tools.html_trees._authorize(index, node, body=not navigation)
                if not node.get('doc_path'):
                    _, root = app.tools.html_trees._binding(index.meta)
                    return navigation and str(root) in resources['roots']
                obj = {'asset_key': node['root_id'] + ':' + node['doc_path'], 'library_id': node['library_id']}
        else:
            obj = app.libraries.get(identity)
        if not obj:
            return False
        asset = obj.get('asset_key', '')
        if ':' not in asset:
            return False
        root_id, relative = asset.split(':', 1)
        library = app.libraries.library(obj['library_id'])
        for root in library['roots']:
            if root['id'] == root_id:
                candidate = (Path(root['path']) / relative).absolute()
                return os.path.normcase(str(candidate)) in approved
        return False
    for sid, task in tasks.items():
        public = [str(_public_file(batch.ROOT, batch.ROOT / entry['path'])) for entry in task.input_manifest['files']]
        # The context names the product's uploaded copy of the public geometry.
        # Grant this one identical file, never its parent or any other uploads.
        model = app.sessions.get(sid).get('model') or {}
        uploaded = Path(model['path']).resolve(strict=True)
        expected = next(entry['sha256'] for entry in task.input_manifest['files'] if entry['role'] == 'initial_geometry')
        if not uploaded.is_relative_to(app.data_dir / 'uploads') or batch.sha(uploaded) != expected:
            raise PermissionError('Uploaded geometry differs from the public input')
        task.original_input_files = public + [str(uploaded)]
        raw[sid] = RawResourceTools(files=ordinary_paths + task.original_input_files, roots=resources['roots'], strict_files=True,
                                   denied_hashes=app.libraries.evaluation_policy().source_hashes)
        contracts[sid] = definitions(task.condition, raw[sid])
    def native(sid, name, args):
        if name == 'get_original_scope':
            task = tasks[sid]
            return {'roots': resources['roots'], 'user_input_files': task.original_input_files,
                    'note': '原始地址；按文件名或正文自主查找。列表已排除不可访问文件，没有预选参考案例。'}
        if name == 'get_tool_contract':
            return read_contract(contracts[sid], args)
        if name in {tool['name'] for tool in raw[sid].tool_definitions()}:
            return raw[sid].call(name, args)
        for identity in index_argument_identities(name, args):
            if not indexed_original_allowed(identity, navigation=name == 'get_html_navigation'):
                raise PermissionError('Indexed source is outside the common approved original corpus')
        if name == 'get_case_navigation' and args.get('case_id'):
            # Same script construction for whichever authorized reference the
            # model chooses. No task/answer-based reference selection occurs.
            with build_lock:
                repo = CaseTreeRepository(app.libraries)
                if repo.case(args['case_id']).get('state') == 'not_prepared':
                    began = time.monotonic()
                    result = build_case_trees(app.libraries, [args['case_id']])
                    with (here / 'derived-builds.jsonl').open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps({'case_id': args['case_id'], 'seconds': time.monotonic() - began,
                                                 'result': result}, ensure_ascii=False) + '\n')
        result = original(sid, name, args, source_entry=None)
        if isinstance(result, dict) and result.get('success') is False and 'contentItems' in result:
            raise ValueError(result['contentItems'][0].get('text', 'Tool rejected the request'))
        if name in {'search_cases', 'search_knowledge'} and isinstance(result, dict):
            result['results'] = [item for item in result.get('results', [])
                                 if item.get('id') and indexed_original_allowed(item['id'])]
        if name in {'get_html_navigation', 'get_case_navigation'}:
            cache = {}
            def candidate_allowed(identity):
                if identity not in cache:
                    try:
                        cache[identity] = indexed_original_allowed(identity, navigation=True)
                    except (KeyError, ValueError, PermissionError, OSError):
                        cache[identity] = False
                return cache[identity]
            result = filter_index_navigation(result, candidate_allowed)
        return result
    def authorize(sid, name, args):
        check_frozen_authority()
        app.sessions.get(sid)
        if name in INDEXED and tasks[sid].condition != 'indexed':
            raise PermissionError('Indexed content is not available in the raw condition')
        for identity in index_argument_identities(name, args):
            if not indexed_original_allowed(identity, navigation=name == 'get_html_navigation'):
                raise PermissionError('Cached source is outside the approved original scope')
            from advisor.html_tree_tools import is_html_source_id
            if is_html_source_id(identity):
                if not app.tools.html_trees.get_document(identity, metadata_only=True):
                    raise PermissionError('The HTML source is not available')
            else:
                app.libraries.require_not_heldout(identity)
                if not app.libraries.source_available(identity):
                    raise PermissionError('The source is unavailable or held out')
    for sid in tasks:
        pagers[sid] = BoundedKnowledge(lambda name, args, sid=sid: native(sid, name, args), max_bytes=7000,
                                      authorize=lambda name, args, sid=sid: authorize(sid, name, args))
    def guarded(sid, name, arguments, *, source_entry=None):
        began, row = time.monotonic(), {'session_id': sid, 'tool': name, 'arguments': arguments}
        try:
            if sid not in tasks:
                raise PermissionError('Unknown evaluation task')
            check_frozen_authority()
            if source_entry is not None:
                app.agents._require_current_entry(sid, source_entry)
            by_name = {tool['name']: tool for tool in contracts[sid]}
            if name not in by_name:
                raise PermissionError('This tool is not available in this condition')
            errors = schema_errors(arguments, by_name[name]['inputSchema'])
            if errors:
                raise ValueError('Arguments differ from tool contract: ' + json.dumps(errors, ensure_ascii=False))
            args = copy.deepcopy(arguments)
            if name == 'read_source':
                args.setdefault('length', 1000)
            direct = {'get_html_navigation', 'read_html_section', 'get_case_navigation', 'inspect_case_source'} | {tool['name'] for tool in raw[sid].tool_definitions()}
            result = native(sid, name, args) if name in direct else pagers[sid].call(name, args)
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(encoded) > 7000:
                raise ValueError('Complete result exceeded 7000 bytes; use a smaller page')
            row.update(success=True, result_json_bytes=len(encoded), response_sha256=response_sha256(result))
            if isinstance(result, dict):
                row['result_state'] = result.get('state')
                row['returned'] = result.get('returned')
                row['first_parse_elapsed_ms'] = result.get('first_parse_elapsed_ms')
                row['cache_reused'] = result.get('cache_reused')
        except Exception as error:
            result = {'success': False, 'error_type': type(error).__name__, 'error': str(error)[:900],
                      'next_action': '按当前工具契约修正参数或选择允许的资料入口；最终自然完成回复。'}
            row.update(success=False, error_type=type(error).__name__, error=str(error)[:900],
                       response_sha256=response_sha256(result))
        finally:
            row['seconds'] = round(time.monotonic() - began, 4)
            if sid in tasks:
                with lock:
                    with tasks[sid].audit_path.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        return result
    app.agents.runtime_tool = guarded
    return contracts, guarded


def case_chain_preflight(app, url, resources, here, lock):
    """Probe real registered tools in a separate, never-inferred session.

    The generic training query is unrelated to task/rubric content. Its reads
    and page handles must not contaminate any of the three planning sessions.
    """
    probe = here / 'preflight'
    probe.mkdir()
    prepared = create_task(url, batch.ROOT, CASES[0])
    sid = prepared['session']['id']
    task = SimpleNamespace(condition='indexed', input_manifest=prepared['input_manifest'],
                           audit_path=probe / 'calls.jsonl')
    saved = app.agents.runtime_tool
    transcript = []
    def call(name, arguments):
        result = guarded(sid, name, arguments)
        transcript.append({'tool': name, 'arguments': arguments, 'response': result})
        if result.get('success') is False:
            raise RuntimeError('Case-chain preflight failed: ' + json.dumps(result, ensure_ascii=False))
        return result
    try:
        _, guarded = install_tools(app, {sid: task}, resources, probe, lock)
        # A fixed, previously parsed public training fixture, unrelated to any
        # of the three task subjects. Broad top hits may legitimately have no
        # retained configuration, which cannot test the configuration endpoint.
        hits = call('search_cases', {'query': 'rotating plate',
                                    'library_ids': ['cs63-models-molecular-flow-module']})
        if not hits.get('results'):
            raise RuntimeError('Case-chain preflight: actual search_cases returned no training case')
        identity = hits['results'][0]['id']
        overview = call('read_source', {'id': identity, 'length': 1000})
        if not overview.get('text'):
            raise RuntimeError('Case-chain preflight: case overview is not readable')
        original_action = overview.get('case_reading', {}).get('original')
        if not original_action or original_action.get('tool') != 'inspect_case_source':
            raise RuntimeError('Case-chain preflight: lightweight original-reading action is missing')
        original = call(original_action['tool'], original_action['arguments'])
        native = original.get('raw_result', {})
        if original.get('basis') != 'original_saved_xml' or not native.get('items'):
            raise RuntimeError('Case-chain preflight: offline original hierarchy is unavailable')
        original_xml = call('inspect_case_source', {'case_id': identity,
            'node_path': native['items'][0]['node_path'], 'view': 'xml', 'length': 400})
        if (not original_xml.get('raw_result', {}).get('text') or
                not original_xml.get('raw_result', {}).get('cache_reused') or
                not any(ref.get('context') == 'original_mph_xml' for ref in original_xml.get('evidence_refs', []))):
            raise RuntimeError('Case-chain preflight: cached original XML/read receipt is unavailable')
        children = call('get_case_navigation', {'case_id': identity, 'limit': 2})
        if children.get('state') != 'ready' or not children.get('items'):
            raise RuntimeError('Case-chain preflight: parsed hierarchy is not ready')
        dependencies = call('get_case_navigation', {'case_id': identity, 'view': 'dependencies', 'limit': 2})
        if dependencies.get('state') != 'ready' or not dependencies.get('items'):
            raise RuntimeError('Case-chain preflight: parsed dependency page is not ready')
        request = children['items'][0]['read_requests'][0]
        configuration = call('read_case_configuration', {**request, 'limit': 1})
        if not configuration.get('nodes'):
            raise RuntimeError('Case-chain preflight: exact configuration node was not returned')
        return {'success': True, 'models_started': 0, 'separate_session': sid,
                'offline_original_verified': True, 'original_xml_receipt_verified': True,
                'query_rule': 'Fixed rotating-plate molecular-flow training fixture; no task-based reference selection',
                'case_id': identity, 'tool_calls': transcript}
    finally:
        app.agents.runtime_tool = saved
        batch.write(probe / 'transcript.json', transcript)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', default='luna6-high-raw-vs-indexed-20261114')
    parser.add_argument('--data-root', type=Path, default=batch.DATA)
    parser.add_argument('--offline-root', type=Path, default=batch.OFFLINE)
    parser.add_argument('--parallel', type=int, choices=(1, 2), default=2)
    parser.add_argument('--timeout', type=float, default=7200)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--case-id', choices=CASES, action='append', help='Explicit infrastructure-only rerun subset; preserve original failed attempt')
    parser.add_argument('--arm', choices=ARMS, action='append')
    parser.add_argument('--legacy-prompt', action='store_true', help='Historical replay only; default uses the product research policy')
    parser.add_argument('--task-mode', choices=('route', 'implementation'),
                        help='Default: full implementation planning; route must be explicitly selected')
    parser.add_argument('--library-snapshot', type=Path, help='Independent prepared library snapshot; never a production alias')
    parser.add_argument('--resource-inventory', type=Path, help='Reuse a controller-owned preflight inventory with the exact same exclusion policy and roots')
    args = parser.parse_args(argv)
    args.task_mode = task_mode(legacy=args.legacy_prompt, mode=args.task_mode)
    message_text = effective_message(legacy=args.legacy_prompt, mode=args.task_mode)
    cases, arms = tuple(args.case_id or CASES), tuple(args.arm or ARMS)
    if len(set(cases)) != len(cases) or len(set(arms)) != len(arms):
        raise ValueError('Duplicate task selections are not allowed')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}', args.run_id):
        raise ValueError('Simple fresh run ID required')
    here, runs = batch.ROOT / 'private/evaluations' / args.run_id, batch.ROOT / 'runs' / args.run_id
    if here.exists() or runs.exists():
        raise FileExistsError('Never overwrite or repeat existing attempts')
    here.mkdir(parents=True)
    runs.mkdir(parents=True)
    tmp = here / 'runtime/tmp'
    tmp.mkdir(parents=True)
    os.environ.update({'SIM_ADVISOR_NODE': str(args.offline_root / 'node/node.exe'),
        'SIM_ADVISOR_QMD_DIR': str(args.offline_root / 'qmd/node_modules/@tobilu/qmd'),
        'SIM_ADVISOR_SEARCH_CACHE': str(here / 'runtime/search-cache'),
        'TEMP': str(tmp), 'TMP': str(tmp), 'PYTHONIOENCODING': 'utf-8'})
    os.environ.pop('SIM_ADVISOR_HTML_TREE_INDEX', None)
    from advisor.config import codex_command
    from advisor.runtime_adapters import CodexRuntimeFactory, CodexDebugRuntime, runtime_command
    from advisor.server import App, make_server
    from scripts.benchmark_native_cli import project_mcp_disables
    from scripts.build_case_methods import link_library
    from scripts.benchmark_library_snapshot import prepare_snapshot, require_snapshot, source_flags
    from scripts.benchmark_html_tree import inspect_delivery, verify_public_inputs
    state = {'status': 'preparing', 'expected_runs': len(cases) * len(arms), 'parallel_limit': args.parallel,
             'model': MODEL, 'effort': EFFORT, 'active': [], 'completed': []}
    stop, lock, task_map, all_contracts = threading.Event(), threading.RLock(), {}, {}
    def progress():
        state['updated_at'] = base.now()
        batch.write(here / 'progress.json', state)
    class Runtime:
        def __init__(self, inner):
            self.inner = inner
        def __getattr__(self, name):
            return getattr(self.inner, name)
        def start(self, thread_id=None, tools=None, instructions=''):
            # The factory's cwd is the session's dedicated workspace; its name
            # is an exact plan identity assigned by AgentSessions.
            sid = self.inner.cwd.name
            if sid not in all_contracts:
                raise PermissionError('Runtime workspace does not match a prepared task')
            return self.inner.start(thread_id=thread_id, tools=all_contracts[sid], instructions=effective_instructions(legacy=args.legacy_prompt, mode=args.task_mode))
    class Factory(CodexRuntimeFactory):
        def __call__(self, *, cwd, on_event, on_tool, model=MODEL, reasoning_effort=EFFORT):
            if (model, reasoning_effort) != (MODEL, EFFORT):
                raise ValueError('Fixed Luna6/high protocol')
            inherited, _ = project_mcp_disables(Path(cwd))
            command = runtime_command(codex_command()) + batch.flags(dict(PROFILE, **inherited)) + ['--strict-config']
            return Runtime(CodexDebugRuntime(command=command, cwd=cwd, on_event=on_event, on_tool=on_tool,
                                           model=model, reasoning_effort=reasoning_effort))
    app, server, before = None, None, None
    progress()
    try:
        data, docs = here / 'runtime/product', here / 'runtime/empty-documents'
        data.mkdir(parents=True)
        docs.mkdir(parents=True)
        source_root = args.data_root / 'libraries'
        snapshot_root = args.library_snapshot or APP.parents[1] / '.tmp' / ('lib-' + uuid.uuid4().hex[:10]) / 'libraries'
        if not snapshot_root.exists():
            emit('snapshot_started', destination=str(snapshot_root))
            prepare_snapshot(source_root, snapshot_root)
        snapshot = require_snapshot(source_root, snapshot_root)
        batch.write(here / 'library-snapshot.json', snapshot)
        emit('snapshot_ready', destination=str(snapshot_root))
        link_library(data / 'libraries', snapshot_root)
        app = App(data, docs, runtime_factory=Factory())
        before = {value['id']: value['enabled'] for value in app.libraries.libraries(include_deleted=True)}
        if app.libraries.evaluation_set_summaries()['retrieval_enabled']:
            raise PermissionError('Holdout retrieval is enabled')
        from advisor.retrieval_settings import read
        if read(args.data_root)['enabled']:
            raise ValueError('Embedding must remain disabled')
        scope = sorted(value['id'] for value in app.libraries.libraries() if value['type'] in {'official-models', 'official-manuals'})
        original_libraries = app.libraries.libraries
        # Every layer sees the independently persisted snapshot lifecycle flags.
        if app.kb.store is not app.libraries or app.tools.kb.store is not app.libraries:
            raise RuntimeError('The scope overlay is not shared by all indexed entry points')
        if 'indexed' in arms:
            assert_search_scope_agreement(app.libraries, scope)
        resources = reuse_inventory(args.resource_inventory, app.libraries, scope) if args.resource_inventory else resource_inventory(app.libraries, scope)
        batch.write(here / 'resource-inventory.json', resources)
        server = make_server(app, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        if 'indexed' in arms:
            positive = case_chain_preflight(app, url, resources, here, lock)
            batch.write(here / 'case-chain-preflight.json', positive)
            emit('case_chain_verified', case_id=positive['case_id'], models_started=0)
        manifests = {}
        # Alternating order is fixed before execution, one fresh turn per item.
        for case in cases:
            for arm in arms:
                prepared = create_task(url, batch.ROOT, case)
                sid, key = prepared['session']['id'], arm + '-' + case
                audit = here / 'audits' / (key + '.jsonl')
                audit.parent.mkdir(exist_ok=True)
                task = SimpleNamespace(url=url, session_id=sid, case=case, condition=arm,
                    destination=runs / key, model=MODEL, timeout=args.timeout, poll_interval=3,
                    audit_path=audit, input_manifest=prepared['input_manifest'], task_mode=args.task_mode)
                task_map[sid] = task
                manifests[key] = prepared['input_manifest']
        contracts, guarded = install_tools(app, task_map, resources, here, lock)
        all_contracts.update(contracts)
        # Never register indexed interfaces in the raw condition.
        isolation_checked_arms = set()
        for sid, task in task_map.items():
            names = {value['name'] for value in contracts[sid]}
            if task.condition == 'raw_address' and names & INDEXED:
                raise PermissionError('Raw arm received an indexed tool')
            blocked = guarded(sid, 'read_original_file', {'path': str(here / 'progress.json')})
            if blocked.get('success') is not False:
                raise PermissionError('Raw tools exposed a private controller file')
            if args.resource_inventory and task.condition not in isolation_checked_arms:
                prior = json.loads(args.resource_inventory.read_text(encoding='utf-8'))
                family_stems = protected_companion_stems(app.libraries.evaluation_policy())
                excluded = [entry['path'] for entry in prior['files']
                            if protected_companion(entry['path'], family_stems)]
                for source in excluded:
                    reader = 'inspect_original_mph' if Path(source).suffix.casefold() == '.mph' else 'read_original_file'
                    denied = guarded(sid, reader, {'path': source})
                    if denied.get('success') is not False:
                        raise PermissionError('Raw tools exposed a protected model companion')
            if task.condition == 'indexed' and task.condition not in isolation_checked_arms:
                nav = guarded(sid, 'get_html_navigation', {'limit': 1})
                if (nav.get('state') != 'ready' or nav.get('success') is False
                        or not (nav.get('items') or nav.get('matches') or nav.get('node'))):
                    raise RuntimeError('Approved native HTML navigation is unavailable')
                protected = sorted(app.libraries.evaluation_policy().citation_ids)
                if protected and guarded(sid, 'read_source', {'id': protected[0], 'length': 500}).get('success') is not False:
                    raise PermissionError('An indexed heldout source was exposed')
            isolation_checked_arms.add(task.condition)
            # Remove preflight calls: audits used to verify model-visible delivery only.
            task.audit_path.unlink(missing_ok=True)
        for case in cases:
            if set(arms) == set(ARMS) and manifests['raw_address-' + case] != manifests['indexed-' + case]:
                raise ValueError('The two arms received different original inputs')
        verify_public_inputs(batch.ROOT, manifests)
        protocol = {'schema': ('raw-vs-indexed-implementation-protocol/v1' if args.task_mode == 'implementation'
                               else 'raw-vs-indexed-route-protocol/v1'), 'model': MODEL, 'effort': EFFORT,
            'cases': cases, 'conditions': arms, 'parallel_limit': args.parallel, 'attempts_per_item': 1,
            'runtime_profile': PROFILE,
            'common_instructions': effective_instructions(legacy=args.legacy_prompt, mode=args.task_mode), 'common_message': message_text,
            'task_mode': args.task_mode,
            'research_prompt_source': 'legacy_replay' if args.legacy_prompt else 'product_research_policy_' + args.task_mode,
            'resource_inventory_sha256': digest(resources), 'enabled_scope': scope,
            'scope_mechanism': 'Independent copied catalog with real persisted lifecycle flags; no runtime overlay',
            'library_snapshot': str(snapshot_root),
            'production_switches_changed': False, 'indexed_tools': sorted(INDEXED),
            'originals_present_in_both_arms': True, 'private_grading_read': False,
            'historical_answers_supplied': False, 'reference_cases_preselected_for_tasks': False,
            'case_navigation_cache': 'Construct from existing parsed training archive on first Agent-selected case request; count in tool/turn latency',
            'manual_relation_cache': 'Production automatic metadata association on first selected training case request',
            'final_answer': 'Naturally completed visible JSON, captured locally; no database source-ID save requirement',
            'runtime_tools': contracts, 'public_inputs': manifests,
            'limitations': ['Three cases, one attempt per arm; no estimate of run-to-run variation.',
                           'Both arms use code mode for ordinary JavaScript processing; unrestricted shell/disk/web tools are disabled.',
                           'Default research instructions now share the product policy; historical raw-address answers used the earlier prompt. Comparisons with those answers are descriptive, not a prompt-controlled retrieval ablation.',
                           'Planning depth is recorded by task_mode; complete implementation plans are not solved simulations. Historical route-only outputs are not equal-depth controls.']}
        watched = ('scripts/benchmark_raw_address.py', 'scripts/benchmark_library_snapshot.py', 'advisor/raw_resource_tools.py',
                   'advisor/planning_service.py', 'advisor/case_tree.py', 'advisor/case_manual_links.py',
                   'advisor/html_tree_tools.py', 'advisor/html_tree.py', 'advisor/codex_runtime.py',
                   'advisor/case_original_tools.py', 'advisor/research_policy.py',
                   'advisor/bounded_knowledge.py', 'advisor/plan_settings.py', 'advisor/query_terms.py',
                   'prompts/comsol_implementation_task.v1.txt', 'prompts/comsol_planner.v1.txt')
        protocol['frozen_source_sha256'] = {name: batch.sha(APP / name) for name in watched}
        # Keep the exact inference-time sources after future development changes.
        for name in watched:
            original_bytes = (APP / name).read_bytes()
            frozen = here / 'frozen-source' / name
            frozen.parent.mkdir(parents=True, exist_ok=True)
            frozen.write_bytes(original_bytes)
        batch.write(here / 'protocol.json', protocol)
        batch.write(here / 'services.json', {'pid': os.getpid(), 'url': url})
        if args.prepare_only:
            state['status'] = 'preflight_complete'
            progress()
            emit('preflight_complete', tasks=len(task_map), models_started=0, resources=len(resources['files']))
            return 0
        def monitor():
            while not stop.wait(1):
                if (here / 'stop.request').exists():
                    stop.set()
        threading.Thread(target=monitor, daemon=True).start()
        def execute(task):
            key = task.condition + '-' + task.case
            with lock:
                state['active'].append(key)
                state['status'] = 'running'
                progress()
            emit('case_started', item=key)
            try:
                result = base.run_turn(task, message_text, cancel_event=stop, allowed_conditions=ARMS)
                result['retrieval_arm'] = task.condition
                delivery = inspect_delivery(task, result)
                batch.write(task.destination / 'delivery-audit.json', delivery)
                result['delivery_valid'] = delivery.get('valid') is True
                if result.get('status') == 'complete' and not result['delivery_valid']:
                    result.update(status='invalid_delivery', failure_code='actual_tool_output_not_verified')
                batch.write(task.destination / 'run.json', result)
                row = {key: result.get(key) for key in ('case_id', 'condition', 'status', 'seconds', 'failure_code', 'delivery_valid')}
                with lock:
                    state['completed'].append(row)
                emit('case_completed', **row)
                return result
            finally:
                with lock:
                    state['active'].remove(key)
                    progress()
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            results = list(pool.map(execute, task_map.values()))
        verify_public_inputs(batch.ROOT, manifests)
        if {name: batch.sha(APP / name) for name in watched} != protocol['frozen_source_sha256']:
            raise RuntimeError('Runtime code changed during inference')
        actual = {value['id']: value['enabled'] for value in original_libraries(include_deleted=True)}
        if actual != before:
            raise RuntimeError('Snapshot library switches changed during inference')
        if source_flags(source_root) != snapshot['source_flags']:
            raise RuntimeError('Production library switches changed during inference')
        state['status'] = 'completed' if all(result.get('status') == 'complete' for result in results) else 'completed_with_failures'
        progress()
        return 0 if state['status'] == 'completed' else 2
    except BaseException as error:
        state.update(status='failed', error_type=type(error).__name__, detail=str(error))
        progress()
        raise
    finally:
        stop.set()
        if app is not None:
            app.agents.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        batch.write(here / 'cleanup.json', {'owned_services_stopped': True, 'production_switches_modified_by_controller': False})


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
