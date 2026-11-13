"""Navigation scopes constrain the same QMD BM25 query before global TopK."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from advisor import library_search as module, library_provider as provider_module
from advisor.case_navigation import build_navigation, NavigationNotPrepared, NavigationStale
from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_provider import LibraryKnowledgeProvider
from advisor.library_search import LibrarySearch
from advisor.library_store import LibraryStore, encoded
from advisor.qmd_worker import close_workers, QmdWorker
from test_library_search import obj
from test_qmd_shared_query import runtime, search_runtime


@pytest.fixture(autouse=True)
def release_workers():
    yield
    close_workers()


@pytest.fixture
def navigation(tmp_path):
    probe, wrapper, trace = runtime(tmp_path)
    sources = tmp_path / 'sources'
    sources.mkdir()
    store = LibraryStore(tmp_path / 'libraries')
    for library, kind in (('models', 'official-models'), ('manuals', 'official-manuals'), ('closed', 'official-models')):
        store.register(library, kind, [{'id': 'root', 'path': str(sources)}], library_id=library)
        store.set_enabled(library, True)
    overviews = []
    for index in range(8):
        item = obj('case-' + str(index), 'closed' if index == 7 else 'models', 'needle model overview')
        item.update(type='model_case', local_id='model-case', case_group='group-' + str(index))
        overviews.append(item)
    mesh = obj('mesh-setting', 'models', 'needle mesh implementation')
    mesh.update(type='saved_configuration', local_id='mesh-setting', case_group='group-0')
    mesh['data']['feature_branch'] = 'mesh'
    geometry = obj('geometry-setting', 'models', 'needle ' * 40)
    geometry.update(type='saved_configuration', local_id='geometry-setting', case_group='group-0')
    geometry['data']['feature_branch'] = 'geometry'
    support = []
    for index in range(45):
        item = obj('frame-' + str(index), 'models', 'needle ' * 50)
        item.update(type='saved_configuration', local_id='frame-' + str(index), case_group='group-0', title='needle ' * 40)
        item['data'].update(feature_branch='mesh', feature_type='Frame')
        support.append(item)
    tutorial = obj('tutorial-mesh', 'manuals', 'needle mesh documented instructions')
    tutorial.update(type='method_step', kind='manual')
    tutorial['data']['feature_branch'] = 'mesh'
    objects = [*overviews, mesh, geometry, *support, tutorial]
    with closing(store.connect()) as db, db:
        for item in objects:
            db.execute('INSERT INTO revisions VALUES(?,?,?,?)', (item['id'], item['object_id'], item['library_id'], encoded(item)))
            db.execute('INSERT INTO active VALUES(?,?,?)', (item['library_id'], item['object_id'], item['id']))
            if item.get('case_group'):
                db.execute('INSERT INTO case_members VALUES(?,?,?)', (item['case_group'], item['local_id'], item['id']))
    builder = LibrarySearch(store.root / 'search', qmd_dir=probe.qmd_dir, node=probe.node, cache_dir=tmp_path / 'cache')
    built = builder.build(objects, modes=['qmd_keyword'], generation=store.current_generation())
    assert built['modes']['qmd_keyword']['ready']
    report = build_navigation(store, case_ids=[item['id'] for item in overviews[:5]])
    nav_path = Path(report['path'])
    with closing(sqlite3.connect(nav_path)) as db, db:
        # Classification is independently tested by the navigation compiler.
        db.executemany("UPDATE members SET role='support',global_visible=0 WHERE object_id=?", [(item['id'],) for item in support])
        db.execute('INSERT INTO members VALUES(?,?,?,?,?)', (overviews[0]['id'], tutorial['id'], 'reference', 'mesh', 1))
    store.set_enabled('closed', False)
    search = search_runtime(tmp_path, wrapper, trace, probe.node, store.root / 'search')
    provider = LibraryKnowledgeProvider(SimpleNamespace(get_document=lambda _identity: None), store.root)
    return SimpleNamespace(store=store, search=search, provider=provider, overviews=overviews, mesh=mesh,
                           geometry=geometry, support=support, tutorial=tutorial, report=report, path=nav_path, trace=trace)


def test_overviews_cover_all_active_cases_beyond_five_pilots_and_scope_before_topk(navigation):
    fixture = navigation
    hits = fixture.search.search('needle', retrieval_view='case_overviews', limit=100)
    assert {item['id'] for item in hits} == {item['id'] for item in fixture.overviews[:7]}
    assert all(item['type'] == 'model_case' for item in hits)
    assert fixture.report['overview_count'] == 8 and len(fixture.report['prepared_case_ids']) == 5
    assert fixture.search.last_search_scope['eligible_objects'] == 7
    assert fixture.search.search('needle', retrieval_view='case_overviews', limit=1)[0]['type'] == 'model_case'
    # The existing broad knowledge entry point retains its original scope.
    assert fixture.search.search('needle', limit=1)[0]['type'] == 'saved_configuration'


def test_selected_case_stage_includes_settings_and_useful_cross_library_tutorial_in_one_query(navigation, monkeypatch):
    fixture = navigation
    calls = []
    request = QmdWorker.request
    def observe(worker, payload, **kwargs):
        calls.append(payload)
        return request(worker, payload, **kwargs)
    monkeypatch.setattr(QmdWorker, 'request', observe)
    hits = fixture.search.search('needle', case_id=fixture.overviews[0]['id'], stage='mesh', limit=20)
    assert {item['id'] for item in hits} == {fixture.mesh['id'], fixture.tutorial['id']}
    assert fixture.search.last_search_scope['eligible_objects'] == 2
    assert len(calls) == 1 and calls[0]['op'] == 'shared_keyword'
    assert 'allowed_paths' not in json.dumps(calls[0]) and len(json.dumps(calls[0])) < 5000
    assert calls[0]['authoritative_scope']['navigation'] == {
        'db_path': str(fixture.path), 'view': 'knowledge', 'case_id': fixture.overviews[0]['id'], 'stage': 'mesh'}
    statements = [json.loads(line) for line in fixture.trace.read_text(encoding='utf-8').splitlines()]
    sql = next(row['sql'] for row in statements if row['event'] == 'query')
    assert 'case_navigation.members' in sql and 'n.object_id=o.id' in sql
    assert "n.role IN ('implementation','reference')" in sql and 'n.global_visible=1' in sql
    assert sql.index('case_navigation.members') < sql.index('ORDER BY documents_fts.rank')
    assert "bm25(1.5,4.0,1.0)" in sql
    hits = fixture.search.search('needle', kind='case', case_id=fixture.overviews[0]['id'], stage='mesh', limit=20)
    assert [item['id'] for item in hits] == [fixture.mesh['id']], 'kind keeps its existing meaning for tutorial manual objects'


def test_navigation_counts_start_from_small_id_tables_and_empty_stage_does_not_start_qmd(navigation, monkeypatch):
    fixture = navigation
    connect, statements = sqlite3.connect, []
    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(module.sqlite3, 'connect', traced)
    assert fixture.search.search('needle', case_id=fixture.overviews[0]['id'], stage='mesh')
    count = next(sql for sql in statements if sql.startswith('SELECT o.library_id,count(*) FROM case_navigation.members'))
    with closing(connect(Path(fixture.search._load_active(summary=True)['directory']) / 'objects.sqlite3')) as db:
        db.execute('ATTACH DATABASE ? AS case_navigation', (str(fixture.path),))
        plan = ' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + count))
    assert 'SEARCH n USING' in plan and 'SEARCH o USING' in plan and 'SCAN o' not in plan
    monkeypatch.setattr(fixture.search, '_engine_shared_keyword', lambda *_args: pytest.fail('Empty navigation stage must stop before QMD'))
    assert fixture.search.search('needle', case_id=fixture.overviews[0]['id'], stage='solver') == []


def test_unprepared_missing_and_stale_navigation_never_fall_back_to_global_results(navigation, monkeypatch):
    fixture = navigation
    monkeypatch.setattr(fixture.search, '_engine_shared_keyword', lambda *_args: pytest.fail('Invalid navigation must stop before QMD'))
    with pytest.raises(NavigationNotPrepared, match='not_prepared'):
        fixture.search.search('needle', case_id=fixture.overviews[5]['id'])
    backup = fixture.path.with_suffix('.saved')
    fixture.path.rename(backup)
    try:
        with pytest.raises(NavigationNotPrepared, match='not_prepared'):
            fixture.search.search('needle', retrieval_view='case_overviews')
    finally:
        backup.rename(fixture.path)
    with closing(sqlite3.connect(fixture.path)) as db, db:
        db.execute("UPDATE meta SET value=? WHERE key='corpus_hash'", (json.dumps('stale'),))
    with pytest.raises(NavigationStale, match='stale'):
        fixture.search.search('needle', retrieval_view='case_overviews')


def test_selected_case_owner_and_holdout_are_checked_independently_of_linked_manual(navigation, monkeypatch):
    fixture = navigation
    policy = HoldoutPolicy(citation_ids=frozenset([fixture.overviews[0]['id']]), token='held-overview')
    monkeypatch.setattr(module, 'exclusion_snapshot', lambda _root: policy)
    with pytest.raises(PermissionError, match='evaluation-excluded'):
        fixture.search.search('needle', library_ids=['manuals'], case_id=fixture.overviews[0]['id'])
    monkeypatch.setattr(module, 'exclusion_snapshot', lambda _root: HoldoutPolicy(token='clear'))
    query = fixture.search._engine_shared_keyword
    def close_model_after_query(request):
        result = query(request)
        fixture.store.set_enabled('models', False)
        return result
    monkeypatch.setattr(fixture.search, '_engine_shared_keyword', close_model_after_query)
    with pytest.raises(PermissionError, match='became unavailable'):
        fixture.search.search('needle', library_ids=['manuals'], case_id=fixture.overviews[0]['id'], stage='mesh')


def test_provider_search_cases_and_case_scoped_knowledge_keep_read_permissions(navigation, monkeypatch):
    fixture = navigation
    monkeypatch.setattr(provider_module, 'LibrarySearch', lambda *_args: fixture.search)
    assert all(item['type'] == 'model_case' for item in fixture.provider.search_cases('needle', limit=20))
    hits = fixture.provider.search('needle', kind='all', filters={'case_id': fixture.overviews[0]['id'], 'stage': 'mesh'})
    assert {item['id'] for item in hits} == {fixture.mesh['id'], fixture.tutorial['id']}
    fixture.store.set_enabled('models', False)
    monkeypatch.setattr(fixture.search, '_engine_shared_keyword', lambda *_args: pytest.fail('Closed case must be rejected before retrieval'))
    with pytest.raises(PermissionError):
        fixture.provider.search('needle', filters={'case_id': fixture.overviews[0]['id'], 'library_ids': ['manuals']})


@pytest.mark.parametrize('options', [{'retrieval_view': 'settings'}, {'stage': 'mesh'}, {'case_id': []},
                                    {'case_id': 'case', 'stage': 'mesh_typo'}, {'retrieval_view': 'case_overviews', 'case_id': 'case'},
                                    {'retrieval_view': 'case_overviews', 'mode': 'qmd_vector'}])
def test_invalid_or_embedding_navigation_does_not_load_a_model_or_query(tmp_path, monkeypatch, options):
    search = LibrarySearch(tmp_path / 'search')
    monkeypatch.setattr(search, '_require_embedding', lambda: pytest.fail('Navigation must not load embedding'))
    monkeypatch.setattr(search, '_engine', lambda *_args, **_kwargs: pytest.fail('Invalid navigation must not retrieve'))
    with pytest.raises(ValueError):
        search.search('needle', **options)


def test_bridge_hits_outside_visible_case_members_are_rejected(navigation, monkeypatch):
    from advisor.object_index import qmd_path
    fixture = navigation
    monkeypatch.setattr(fixture.search, '_engine_shared_keyword', lambda _request: [
        {'filepath': 'qmd://' + qmd_path(fixture.support[0]), 'score': 1.0, 'source': 'fts'}])
    with pytest.raises(RuntimeError, match='authoritative retrieval scope'):
        fixture.search.search('needle', case_id=fixture.overviews[0]['id'], stage='mesh', limit=1)


def test_navigation_replacement_during_query_requires_retry(navigation, monkeypatch):
    fixture = navigation
    query = fixture.search._engine_shared_keyword
    def replace_after_query(request):
        rows = query(request)
        with closing(sqlite3.connect(fixture.path)) as db, db:
            db.execute("UPDATE meta SET value=? WHERE key='compiled_policy_token'", (json.dumps('changed'),))
        return rows
    monkeypatch.setattr(fixture.search, '_engine_shared_keyword', replace_after_query)
    with pytest.raises(RuntimeError, match='navigation changed'):
        fixture.search.search('needle', retrieval_view='case_overviews')
