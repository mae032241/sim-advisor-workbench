"""API batch publication changes only the selected immutable case entry points."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor import deep_enrichment
from advisor.build_control import BuildCancelled
from advisor.library_store import LibraryLifecycleConflict, LibraryStore
from test_library_compact_store import compact_fixture


def fixture(tmp_path, *, count=2):
    store, library, model = compact_fixture(tmp_path)
    from test_compact_model import mph
    for number in range(1, count):
        mph(model.parent / f'case-{number}.mph', '<Model><MeshSequence tag="mesh1"><MeshFeature tag="size" op="Size">'
            f'<propertyValue name="p:hmax" value="{number}"/></MeshFeature></MeshSequence></Model>')
    result = store.build(library['id'], model_format='compact', deep=False, modes=[])
    assert result['status'] in {'completed', 'partial'}, result
    store.set_enabled(library['id'], False)
    return store, library, [item for item in store.objects() if item['type'] == 'model_case']


def builder(authority, *, library_root, source_override, **kwargs):
    from advisor.compact_case import CompactCase
    from advisor.compact_library import enrich_case
    from test_compact_case import api_bundle
    reader = CompactCase(Path(library_root) / authority['data']['compact_case']['path'], source_override=source_override)
    case = reader.read_case()
    bundle = api_bundle(case, [{'api_node_id': 1, 'kind': 'property', 'name': 'additionalApiFact', 'value_type': 'String',
                               'status': 'observed', 'getter': 'getString', 'value': 'fixture observed', 'relation': 'api_observed'}])
    bundle['run']['status'] = 'observed'
    bundle['run']['read_contract'] = {'geometry_run': False, 'mesh_run': False, 'solved': False}
    bundle['nodes'][0]['compact_node_id'] = next(node['id'] for node in reader.iter_nodes() if node.get('feature_branch') == 'mesh')
    return enrich_case(authority, library_root=library_root, source_override=source_override,
                       artifact_dir=kwargs.get('artifact_dir') or Path(library_root) / 'artifacts' / ('api-' + authority['object_id']), bundle=bundle)


def metadata(store):
    with closing(store.connect()) as db:
        return dict(db.execute('SELECT id,body FROM libraries'))


def test_api_publication_refreshes_holdout_without_changing_selection(tmp_path):
    from advisor.evaluation_sets import compile_registry, write_registry, exclusion_snapshot
    store, library, authorities = fixture(tmp_path, count=2)
    held = authorities[0]
    selection = {'id': 'retained-test', 'name': 'Retained test', 'models': [
        {'library_id': library['id'], 'asset_key': held['asset_key'], 'title': held['title']}]}
    write_registry(store.root, compile_registry(store.root / 'catalog.sqlite3', [selection]))
    store.set_enabled(library['id'], True)
    batch = deep_enrichment.create_batch(store)
    result = deep_enrichment.run_batch(store, batch['checkpoint'], builder=builder, modes=[], clean_indexes=False)
    assert result['status'] in {'completed', 'partial'}
    policy = exclusion_snapshot(store.root)  # Must be fresh after API identities change.
    current = store.objects()
    held_items = [obj for obj in current if obj['asset_key'] == held['asset_key']]
    allowed_items = [obj for obj in current if obj['asset_key'] != held['asset_key']]
    assert any(obj['local_id'].startswith('deep-api-') for obj in held_items)
    assert all(policy.blocks_object(obj) and not store.source_available(obj['id']) for obj in held_items)
    assert allowed_items and all(store.source_available(obj['id']) for obj in allowed_items)
    assert store.evaluation_set_summaries()['sets'][0]['models'][0]['asset_key'] == held['asset_key']


def test_batch_discovery_does_not_hydrate_xml_objects_or_filter_disabled_libraries(tmp_path, monkeypatch):
    store, library, authorities = fixture(tmp_path)
    for name in ('objects', 'object_stream'):
        monkeypatch.setattr(store, name, lambda *args, **kwargs: pytest.fail('Planning must select only authority identities'))
    connect = store.connect
    def no_payloads():
        db = connect()
        db.create_function('sim_catalog_unpack', 1, lambda raw: pytest.fail('Planning must not decode revision payloads'))
        return db
    monkeypatch.setattr(store, 'connect', no_payloads)
    planned = deep_enrichment.create_batch(store)
    assert planned['total'] == len(authorities) and planned['case_counts']['pending'] == 2
    assert store.library(library['id'])['enabled'] is False


def test_publication_preserves_saved_xml_ids_groups_payloads_and_library_flags(tmp_path):
    store, library, authorities = fixture(tmp_path, count=1)
    before = store.objects()
    library_metadata = metadata(store)
    authority = authorities[0]
    _, record = deep_enrichment._source_record(store, authority)
    published = deep_enrichment.publish_case_overlay(store, authority,
        builder(authority, library_root=store.root, source_override=Path(record['path'])), batch_id='batch', job_id='job')
    assert published['api_objects'] == 1 and not published['recovered']
    active = store.objects()
    new = next(item for item in active if item['type'] == 'model_case')
    assert new['id'] != authority['id'] and new['object_id'] == authority['object_id']
    assert new['data']['base_case_id'] == authority['id'] and new['data']['base_case_group'] == authority['case_group']
    assert new['data']['compact_case'] == authority['data']['compact_case']
    assert new['case_group'] != authority['case_group'] and metadata(store) == library_metadata
    for old in before:
        assert store.get(old['id']) == old
        if old['type'] != 'model_case':
            assert next(item for item in active if item['id'] == old['id']) == old
    assert store.library(library['id'])['enabled'] is False


def test_new_overlay_replaces_only_prior_api_children_and_keeps_all_history(tmp_path):
    store, _, authorities = fixture(tmp_path, count=1)
    base = authorities[0]
    _, record = deep_enrichment._source_record(store, base)
    first = deep_enrichment.publish_case_overlay(store, base, builder(base, library_root=store.root,
        source_override=Path(record['path'])), batch_id='first', job_id='first')
    first_active = store.objects()
    first_api = next(item for item in first_active if item['local_id'].startswith('deep-api-'))
    authority = store.get(first['authority_id'])
    next_result = builder(authority, library_root=store.root, source_override=Path(record['path']))
    next_result['objects'][1]['local_id'] = 'deep-api-another'
    deep_enrichment.publish_case_overlay(store, authority, next_result, batch_id='next', job_id='next')
    active = store.objects()
    assert first_api['id'] not in {item['id'] for item in active}
    assert store.get(first_api['id']) == first_api
    assert {item['id'] for item in first_active if item['local_id'] not in {'model-case', first_api['local_id']}} <= {item['id'] for item in active}
    latest = next(item for item in active if item['type'] == 'model_case')
    assert latest['data']['base_case_id'] == base['id']


def test_staging_only_keeps_live_catalog_unchanged_and_resume_publishes_without_reextracting(tmp_path):
    store, _, authorities = fixture(tmp_path)
    before, flags = store.objects(), metadata(store)
    planned = deep_enrichment.create_batch(store)
    staged = deep_enrichment.run_batch(store, planned['checkpoint'], builder=builder, publish=False, modes=[])
    assert staged['status'] == 'completed' and staged['case_counts']['staged'] == len(authorities)
    assert staged['api_counts']['completed'] == len(authorities)
    assert store.objects() == before and metadata(store) == flags
    completed = deep_enrichment.run_batch(store, planned['checkpoint'], modes=[],
        builder=lambda *args, **kwargs: pytest.fail('Staged API reads must not execute again'))
    assert completed['status'] == 'completed' and completed['case_counts']['completed'] == len(authorities)
    assert metadata(store) == flags


def test_cancelled_serial_extraction_resumes_staged_cases_and_uses_a_new_job_marker(tmp_path):
    store, _, _ = fixture(tmp_path)
    before = store.objects()
    planned = deep_enrichment.create_batch(store)
    calls = []
    def interrupted(authority, **kwargs):
        calls.append(authority['id'])
        if len(calls) == 2:
            raise BuildCancelled()
        return builder(authority, **kwargs)
    stopped = deep_enrichment.run_batch(store, planned['checkpoint'], modes=[], builder=interrupted)
    assert stopped['status'] == 'cancelled' and stopped['case_counts']['staged'] == 1
    assert store.objects() == before
    resumed = deep_enrichment.run_batch(store, planned['checkpoint'], modes=[], builder=builder)
    assert resumed['status'] == 'completed' and resumed['case_counts']['completed'] == 2
    assert stopped['job_id'] != resumed['job_id'] and not (store.root / '.build.lock').exists()


def test_publication_crash_is_reconciled_without_second_api_read(tmp_path, monkeypatch):
    store, _, _ = fixture(tmp_path, count=1)
    planned = deep_enrichment.create_batch(store)
    original = deep_enrichment.publish_case_overlay
    interrupted = False
    def committed_then_cancelled(*args, **kwargs):
        nonlocal interrupted
        value = original(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise BuildCancelled()
        return value
    monkeypatch.setattr(deep_enrichment, 'publish_case_overlay', committed_then_cancelled)
    assert deep_enrichment.run_batch(store, planned['checkpoint'], builder=builder, modes=[])['status'] == 'cancelled'
    monkeypatch.setattr(deep_enrichment, 'publish_case_overlay', original)
    resumed = deep_enrichment.run_batch(store, planned['checkpoint'], modes=[],
        builder=lambda *args, **kwargs: pytest.fail('Committed overlay must reconcile without API reread'))
    assert resumed['status'] == 'completed' and resumed['cases_preview'][0]['report']['recovered'] is True


def test_batch_builds_indexes_once_after_all_publications_and_never_embeds(tmp_path, monkeypatch):
    store, _, _ = fixture(tmp_path)
    planned = deep_enrichment.create_batch(store)
    calls = []
    def indexed(self, objects, *, modes, generation, **kwargs):
        assert all(item['state'] == 'completed' for item in deep_enrichment.batch_status(store, planned['checkpoint'])['cases_preview'])
        calls.append(modes)
        assert len(list(objects)) > 2
        return {'generation': generation, 'modes': {mode: {'ready': True} for mode in modes}}
    monkeypatch.setattr('advisor.library_search.LibrarySearch.build', indexed)
    done = deep_enrichment.run_batch(store, planned['checkpoint'], builder=builder)
    assert done['status'] == 'completed' and calls == [['qmd_keyword']]
    deep_enrichment.run_batch(store, planned['checkpoint'], builder=builder)
    assert calls == [['qmd_keyword']]
    with pytest.raises(ValueError, match='embeddings'):
        deep_enrichment.run_batch(store, planned['checkpoint'], modes=['qmd_vector'])


def test_api_jobs_allow_scope_changes_but_keep_destructive_guards_and_summary_kind(tmp_path):
    store, library, _ = fixture(tmp_path, count=1)
    store._save_job({'id': 'a' * 32, 'kind': 'api_enrichment', 'library_ids': [library['id']], 'scope': 'selected',
                     'status': 'running', 'pid': os.getpid(), 'started_at': '2026-01-01T00:00:00+00:00', 'items': []})
    assert store.jobs_summary()[0]['kind'] == 'api_enrichment'
    assert store.set_enabled(library['id'], True)['enabled'] is True
    assert store.set_enabled_many([library['id']], False)['changed'] == 1
    with pytest.raises(LibraryLifecycleConflict):
        store.delete_library(library['id'])


def test_api_runner_is_injected_and_failed_case_does_not_replace_saved_authority(tmp_path):
    store, _, authorities = fixture(tmp_path, count=1)
    before = store.objects()
    planned = deep_enrichment.create_batch(store)
    marker = object()
    def failed(authority, **kwargs):
        assert kwargs['runner'] is marker
        return {'api_status': 'failed', 'coverage': {'deep_api': {'status': 'failed'}}, 'objects': []}
    done = deep_enrichment.run_batch(store, planned['checkpoint'], builder=failed, api_runner=marker, modes=[])
    assert done['status'] == 'partial' and done['case_counts']['failed'] == 1 and store.objects() == before
    assert store.job_summary(done['job_id'])['counts']['failed'] == 1
    assert done['cases_preview'][0]['authority_id'] == authorities[0]['id']


def test_checkpoint_compresses_staging_and_reclaims_cleared_payload_pages_once(tmp_path):
    store, _, _ = fixture(tmp_path, count=1)
    planned = deep_enrichment.create_batch(store)
    def padded(authority, **kwargs):
        value = builder(authority, **kwargs)
        value['staging_only_fixture'] = os.urandom(120000).hex()
        return value
    staged = deep_enrichment.run_batch(store, planned['checkpoint'], builder=padded, publish=False, modes=[])
    with closing(sqlite3.connect(planned['checkpoint'])) as db:
        assert db.execute('SELECT typeof(prepared_result) FROM cases').fetchone() == ('blob',)
    completed = deep_enrichment.run_batch(store, planned['checkpoint'], builder=builder, modes=[])
    assert completed['case_counts']['completed'] == 1
    assert completed['checkpoint_bytes'] + 50000 < staged['checkpoint_bytes']
    with closing(sqlite3.connect(planned['checkpoint'])) as db:
        assert db.execute('SELECT prepared_result FROM cases').fetchone() == (None,)
        assert db.execute('PRAGMA freelist_count').fetchone() == (0,)


def test_publication_respects_enabled_flag_changed_during_api_staging(tmp_path):
    store, library, _ = fixture(tmp_path, count=1)
    planned = deep_enrichment.create_batch(store)
    changed = False
    def user_scope_change(job):
        nonlocal changed
        if job['stage'] == 'deep_api' and not changed:
            changed = True
            store.set_enabled_many([library['id']], True)
    completed = deep_enrichment.run_batch(store, planned['checkpoint'], builder=builder, modes=[], on_progress=user_scope_change)
    assert completed['status'] == 'completed' and store.library(library['id'])['enabled'] is True


def test_index_tail_cleanup_waits_for_reader_grace_using_existing_retention_rules(tmp_path, monkeypatch):
    from advisor.library_search import LibrarySearch
    from test_library_search import obj
    search = LibrarySearch(tmp_path / 'libraries/search', preserve_history=True)
    old = search.build([obj('old')], modes=['native'], generation='old')
    current = search.build([obj('current')], modes=['native'], generation='current')
    store = LibraryStore(tmp_path / 'libraries')
    clock = [deep_enrichment.time.time()]
    os.utime(old['directory'], (clock[0] - 205, clock[0] - 205))
    monkeypatch.setattr(deep_enrichment.time, 'time', lambda: clock[0])
    monkeypatch.setattr(deep_enrichment.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(deep_enrichment.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    progress = []
    result = deep_enrichment.cleanup_retired_indexes(store, on_progress=progress.append)
    assert result['state'] == 'completed' and result['removed_generations'] == 1
    assert progress[0]['retained_recent_generations'] == 1
    assert not Path(old['directory']).exists() and Path(current['directory']).exists()


def test_partial_api_status_counts_remain_explicit_after_publication(tmp_path):
    store, _, _ = fixture(tmp_path)
    planned = deep_enrichment.create_batch(store)
    calls = 0
    def partly_observed(authority, **kwargs):
        nonlocal calls
        calls += 1
        value = builder(authority, **kwargs)
        if calls == 2:
            value['api_status'] = 'partial'
            value['coverage']['deep_api']['status'] = 'partial'
        return value
    completed = deep_enrichment.run_batch(store, planned['checkpoint'], builder=partly_observed, modes=[])
    assert completed['status'] == 'partial'
    assert completed['case_counts']['completed'] == completed['case_counts']['partial'] == 1


@pytest.mark.parametrize('size', [None, 0, 1, 3, 5])
def test_serial_batch_cli_uses_bounded_five_model_runner_unless_single_case_is_explicit(tmp_path, capsys, size):
    from unittest.mock import Mock, patch
    from scripts import enrich_model_api as cli
    store, runner = Mock(), Mock()
    argv = ['--data-dir', str(tmp_path), 'run', str(tmp_path / 'checkpoint.sqlite3')]
    if size is not None:
        argv += ['--batch-size', str(size)]
    with patch.object(cli, 'LibraryStore', return_value=store), patch.object(cli, 'run_batch', return_value={'status': 'completed'}) as run, \
         patch('advisor.compact_api_batch.BatchAPIRunner', return_value=runner) as batch:
        assert cli.main(argv) == 0
    if size in {0, 1}:
        batch.assert_not_called()
        assert run.call_args.kwargs['api_runner'] is None
    else:
        batch.assert_called_once_with(store, Path(argv[3]), batch_size=5 if size is None else size)
        assert run.call_args.kwargs['api_runner'] is runner
    capsys.readouterr()
