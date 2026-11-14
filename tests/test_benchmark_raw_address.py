from pathlib import Path
from types import SimpleNamespace
import json
import threading

import pytest

from scripts import benchmark_raw_address as trial
from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_store import LibraryStore


def test_scope_overlay_does_not_change_persisted_switches(tmp_path):
    store = LibraryStore(tmp_path / 'libraries')
    originals = tmp_path / 'originals'
    originals.mkdir()
    for identity in ('first', 'second'):
        store.register(identity, 'official-models', [{'id': 'models', 'role': 'models', 'path': str(originals)}], library_id=identity)
        store.set_enabled(identity, False)
    original = trial.freeze_scope(store, ['first'])
    assert store.searchable_library_ids() == ['first']
    assert store.library('first')['enabled'] is True
    assert store.library('second')['enabled'] is False
    store.require_readable_library('first')
    with pytest.raises(PermissionError):
        store.require_readable_library('second')
    assert all(item['enabled'] is False for item in original())
    fresh = LibraryStore(store.root)
    assert fresh.searchable_library_ids() == []
    # The actual backend reopens the catalog; the overlay must not pass preflight.
    with pytest.raises(RuntimeError, match='scope disagrees.*expected=1, backend=0'):
        trial.assert_search_scope_agreement(store, ['first'])


def test_scope_preflight_accepts_only_matching_persisted_lifecycle(tmp_path):
    store = LibraryStore(tmp_path / 'libraries')
    originals = tmp_path / 'originals'
    originals.mkdir()
    store.register('First', 'official-models', [{'id': 'models', 'path': str(originals)}], library_id='first')
    store.set_enabled('first', True)
    trial.freeze_scope(store, ['first'])
    assert trial.assert_search_scope_agreement(store, ['first']) == {
        'expected_count': 1, 'backend_count': 1, 'agreement': True}
    # A lifecycle change at the independent backend also wins over the overlay.
    LibraryStore(store.root).set_enabled('first', False)
    with pytest.raises(RuntimeError, match='scope disagrees'):
        trial.assert_search_scope_agreement(store, ['first'])


def test_inventory_excludes_protected_models_companions_and_hash_duplicates(tmp_path, monkeypatch):
    originals = tmp_path / 'originals'
    originals.mkdir()
    (originals / 'training.mph').write_bytes(b'training-model')
    (originals / 'training.pdf').write_bytes(b'training-pdf')
    (originals / 'reserved.mph').write_bytes(b'reserved-model')
    (originals / 'reserved.pdf').write_bytes(b'reserved-tutorial')
    (originals / 'reserved_variables.txt').write_text('heldout auxiliary definitions')
    (originals / 'reserved_parameters.txt').write_text('heldout auxiliary parameters')
    (originals / 'renamed.mph').write_bytes(b'reserved-model')
    (originals / 'private.html').write_text('reserved tutorial')
    (originals / 'manual.html').write_text('functional manual')
    policy = SimpleNamespace(asset_keys=frozenset({('library', 'models:reserved.mph')}),
        source_paths=frozenset(), document_relpaths=frozenset({'private.html'}),
        source_hashes=frozenset({trial.batch.sha(originals / 'reserved.mph')}),
        citation_ids=frozenset(), object_ids=frozenset(), case_groups=frozenset())
    library = {'id': 'library', 'roots': [{'id': 'models', 'path': str(originals)}]}
    store = SimpleNamespace(evaluation_policy=lambda: policy, library=lambda *_: library,
                           evaluation_set_summaries=lambda: {'retrieval_enabled': False})
    monkeypatch.setattr(trial, 'APP', tmp_path / 'program')
    result = trial.resource_inventory(store, ['library'])
    assert {Path(item['path']).name for item in result['files']} == {'training.mph', 'training.pdf', 'manual.html'}
    assert result['content_index_built'] is False
    assert result['excluded_count'] == 6


def test_definitions_keep_raw_and_indexed_arms_distinct_with_common_file_tools():
    raw = SimpleNamespace(tool_definitions=lambda: [{'name': 'read_original_file',
        'description': 'read a file', 'inputSchema': {'type': 'object', 'properties': {}}}])
    left, right = trial.definitions('raw_address', raw), trial.definitions('indexed', raw)
    a, b = {item['name'] for item in left}, {item['name'] for item in right}
    assert not (a & trial.INDEXED)
    assert b - a == trial.INDEXED
    assert {'get_task_context', 'get_original_scope', 'read_original_file', 'read_tool_page'} <= a
    assert not {'save_plan', 'update_steps', 'set_libraries_enabled', 'get_case_method'} & b
    source = next(item for item in right if item['name'] == 'read_source')
    assert '"maximum":1200' in source['description']
    assert '"maximum":16000' not in source['description']
    for tool in left:
        assert '输入契约' in tool['description']
    assert trial.MODEL == 'gpt-6-luna' and trial.EFFORT == 'high'


def test_default_task_preserves_full_implementation_request_without_case_specific_hints():
    assert '{candidates}' not in trial.MESSAGE
    assert '先查看get_html_navigation' not in trial.MESSAGE
    assert 'chosen_route' in trial.MESSAGE
    for required in ('implementation_plan', '工程师', '几何准备', '网格', '研究/求解', '结果提取', '验收',
                     '关键赋值', '耦合方向', '只引用实际读取范围'):
        assert required in trial.MESSAGE
    for narrowed in ('本轮只形成COMSOL技术路线判断', '无需完整网格', '此轮只交付技术路线判断'):
        assert narrowed not in trial.MESSAGE
        assert narrowed not in trial.INSTRUCTIONS


def test_default_implementation_prompt_uses_shared_policy_and_route_requires_explicit_mode():
    from advisor.research_policy import research_instructions, RESEARCH_POLICY
    current = trial.effective_instructions()
    assert research_instructions('implementation') in current
    assert current.count(RESEARCH_POLICY) == 1
    assert trial.INSTRUCTIONS == current
    assert trial.MESSAGE == trial.effective_message()
    assert trial.effective_instructions(legacy=True) == trial.LEGACY_INSTRUCTIONS
    assert trial.effective_message(legacy=True) == trial.LEGACY_MESSAGE
    assert research_instructions('route') in trial.effective_instructions(mode='route')
    assert trial.task_mode() == 'implementation'
    assert trial.task_mode(legacy=True) == 'route'
    with pytest.raises(ValueError):
        trial.effective_instructions(legacy=True, mode='implementation')


@pytest.mark.parametrize('name', ['reserved.mph', 'RESERVED.PDF', 'reserved_variables.txt',
                                 'reserved_parameters.txt', 'reserved_model.java', 'reserved-v2.mph', 'reserved.notes.html'])
def test_holdout_companion_prefix_excludes_all_auxiliary_kinds(name):
    assert trial.protected_companion(Path(name), {'reserved'})
    assert not trial.protected_companion(Path('unrelated_reserved_example.mph'), {'reserved'})


def test_named_holdout_directory_also_blocks_generic_companion_names():
    assert trial.protected_companion(Path('examples/reserved/parameters.txt'), {'reserved'})
    assert trial.protected_companion(Path('examples/reserved-export/geometry.mph'), {'reserved'})


def test_navigation_arguments_include_manual_reverse_and_source_bridge_entries():
    assert trial.index_argument_identities('get_html_navigation', {'node_id': 'tree-x', 'source_id': 'lo-y'}) == ['tree-x', 'lo-y']
    assert trial.index_argument_identities('get_case_navigation', {'manual_node_id': 'tree-x', 'view': 'implementations'}) == ['tree-x']


def test_navigation_filters_both_reverse_endpoints_and_nested_context():
    result = {'items': [{'case_id': 'lo-allowed', 'manual': {'node_id': 'tree-denied'}},
                        {'case_id': 'lo-denied'}, {'case_id': 'lo-allowed', 'manual': {'node_id': 'tree-allowed'}}],
              'parent': {'node_id': 'tree-denied'}, 'next_offset': 3, 'total': 8}
    filtered = trial.filter_index_navigation(result, lambda value: value.endswith('allowed'))
    # A relation with either blocked endpoint is removed as a whole.
    assert filtered['items'][0]['manual']['node_id'] == 'tree-allowed'
    assert len(filtered['items']) == 1
    assert filtered['parent'] is None
    assert filtered['next_offset'] == 3
    assert filtered['pagination_scope'] == 'provider_cursor_before_common_material_filter'


def test_cached_page_rejected_after_frozen_library_authority_changes(tmp_path, monkeypatch):
    public = tmp_path / 'benchmark/public/cases/case-01/inputs'
    public.mkdir(parents=True)
    geometry = public / 'geometry_only.mph'
    geometry.write_bytes(b'public geometry only')
    data = tmp_path / 'runtime'
    uploaded = data / 'uploads/one/geometry_only.mph'
    uploaded.parent.mkdir(parents=True)
    uploaded.write_bytes(geometry.read_bytes())
    originals = tmp_path / 'originals'
    originals.mkdir()
    original = originals / 'training.html'
    original.write_text('permitted original')
    store = LibraryStore(data / 'libraries')
    store.register('Training', 'official-manuals', [{'id': 'help', 'role': 'manuals', 'path': str(originals)}], library_id='training')
    store.set_enabled('training', True)
    task = SimpleNamespace(condition='raw_address', audit_path=tmp_path / 'audit.jsonl',
        input_manifest={'files': [{'path': 'public/cases/case-01/inputs/geometry_only.mph',
                                  'role': 'initial_geometry', 'sha256': trial.batch.sha(geometry)}]})
    app = SimpleNamespace(libraries=store, data_dir=data,
        sessions=SimpleNamespace(get=lambda *_: {'model': {'path': str(uploaded)}}),
        agents=SimpleNamespace(runtime_tool=lambda *a, **kw: {'text': 'permitted facts ' * 2000}))
    monkeypatch.setattr(trial.batch, 'ROOT', tmp_path / 'benchmark')
    _, call = trial.install_tools(app, {'session': task}, {'roots': [str(originals)], 'files': [{'path': str(original)}]}, tmp_path, threading.RLock())
    page = call('session', 'get_task_context', {})
    assert page.get('handle')
    store.set_enabled('training', False)
    denied = call('session', 'read_tool_page', {'handle': page['handle']})
    assert denied['success'] is False
    assert 'Frozen material' in denied['error'] and 'text' not in denied
