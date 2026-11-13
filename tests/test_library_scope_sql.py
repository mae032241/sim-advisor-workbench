"""Bounded scope queries preserve the original scoring and policy boundaries."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from advisor import library_search as module, object_index as native_index
from advisor.case_metadata import object_feature_branch
from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_search import LibrarySearch
from test_case_branch_search import feature
from test_evaluation_search import policy_for
from test_library_search import FakeEngineSearch


def scope_index(root, objects, *, legacy=False, generation="scope"):
    """Build actual QMD once; optionally expose its unchanged historical lookup."""
    search = LibrarySearch(root)
    if not search.node or not (search.qmd_dir / 'dist/index.js').is_file() or search._qmd_version() != '2.8.3':
        pytest.skip('Supported local QMD/Node dependencies unavailable')
    built = search.build(objects, modes=['qmd_keyword'], generation=generation)
    assert built['modes']['qmd_keyword']['ready'], built
    path = Path(built['directory']) / 'objects.sqlite3'
    if legacy:
        path.unlink()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE objects(id TEXT PRIMARY KEY, library_id TEXT NOT NULL, title TEXT, text TEXT, object_json TEXT NOT NULL)')
            db.execute("CREATE VIRTUAL TABLE object_fts USING fts5(id UNINDEXED,title,text,cjk,tokenize='porter unicode61')")
            for item in sorted(objects, key=lambda value: value['id']):
                text, _ = module._representation(item)
                db.execute('INSERT INTO objects VALUES(?,?,?,?,?)', (item['id'], item['library_id'], item['title'], text, module._json(item)))
                # Keep old postings as an unrelated canary. Public retrieval must
                # resolve the original saved QMD rows, including native aliases.
                db.execute('INSERT INTO object_fts VALUES(?,?,?,?)', (item['id'], 'retired lexical canary', '', ''))
        built = dict(built)
        built.pop('object_lookup', None)
        for target in (search.active_path, search.last_build_path, path.parent / 'manifest.json'):
            target.write_text(module._json(module._serialize_manifest(built, root)), encoding='utf-8')
    return search, built, path


def normalized_matches(search, tmp_path, queries):
    script = tmp_path / 'normalize-terms.mjs'
    script.write_text("""import {normalizeCjkForFTS} from STORE;
      const queries=QUERIES;
      process.stdout.write(JSON.stringify(queries.map(terms=>terms.map(term=>'"'+normalizeCjkForFTS(term).replaceAll('"','""')+'"').join(' OR '))));
    """.replace('STORE', json.dumps((search.qmd_dir / 'dist/store.js').as_uri()))
        .replace('QUERIES', json.dumps([module._lex_terms(query) for query in queries])), encoding='utf-8')
    result = subprocess.run([search.node, str(script)], capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    return dict(zip(queries, json.loads(result.stdout)))


def keyword_reference(db, eligible, match, limit):
    db.execute('DELETE FROM expected_scope')
    db.executemany('INSERT INTO expected_scope VALUES(?,?)', [(native_index.qmd_path(item), item['id']) for item in eligible])
    rows = db.execute("""SELECT s.id,bm25(documents_fts,1.5,4.0,1.0) AS score
      FROM documents_fts CROSS JOIN documents d ON d.id=documents_fts.rowid
      CROSS JOIN expected_scope s ON s.path=d.collection || '/' || d.path
      WHERE documents_fts MATCH ? AND d.active=1 ORDER BY score LIMIT ?""", (match, limit)).fetchall()
    return [(identity, abs(score) / (1 + abs(score))) for identity, score in rows]


@pytest.mark.parametrize('options', [dict(kind='code'), dict(feature_branch='mesh'),
                                    dict(kind='code', feature_branch='mesh')])
def test_keyword_filters_count_in_sql_and_hydrate_only_final_hits(tmp_path, monkeypatch, options):
    objects = [feature(str(index), 'mesh' if index % 2 else 'physics',
                       kind='code' if index % 3 else 'case', library='one' if index % 5 else 'two',
                       text='valve mesh pressure ' * (1 + index % 7)) for index in range(1200)]
    search, built, _ = scope_index(tmp_path / 'search', objects)
    connect, statements, hydrated = sqlite3.connect, [], []

    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    original_hydrate = native_index.hydrate_object

    def hydrate(db, identity, **kwargs):
        hydrated.append(identity)
        return original_hydrate(db, identity, **kwargs)

    monkeypatch.setattr(module.sqlite3, 'connect', traced)
    monkeypatch.setattr(native_index, 'hydrate_object', hydrate)
    hits = search.search('valve pressure', library_ids=['one'], limit=6, **options)
    expected = [item for item in objects if item['library_id'] == 'one'
                and (options.get('kind', 'all') == 'all' or item['kind'] == options['kind'])
                and (not options.get('feature_branch') or object_feature_branch(item) == options['feature_branch'])]
    assert len(hits) == 6 and hydrated == [hit['id'] for hit in hits]
    assert search.last_search_scope['libraries'] == {'one': len(expected)}
    assert not any('query_scope_ids' in sql for sql in statements)
    assert not any(sql.startswith('SELECT id,library_id') or sql.startswith('SELECT o.id FROM objects') for sql in statements)
    assert module.query_scope_counts.BASE_SQL in statements
    # Compare with a separate explicit eligible-path table and BM25 call.
    with closing(connect(Path(built['directory']) / 'qmd-keyword.sqlite3')) as db:
        db.execute('CREATE TEMP TABLE expected_scope(path TEXT PRIMARY KEY,id TEXT)')
        reference = keyword_reference(db, expected, '"valve" OR "pressure"', 6)
    assert [(hit['id'], hit['retrieval']['score']) for hit in hits] == reference


@pytest.mark.parametrize('legacy', [False, True])
def test_keyword_rank_preserves_qmd_scores_and_tie_order_across_scopes(tmp_path, legacy):
    objects = [feature(str(index).zfill(3), 'mesh' if index % 3 else 'physics',
                       kind='code' if index % 4 else 'case', library='one' if index % 2 else 'two',
                       text='valve pressure mesh sizing 阀门压降 ' * (1 if index % 5 else 3)) for index in range(140)]
    for item in objects:
        item['title'] = 'mesh valve identical tied titles'
    search, built, _ = scope_index(tmp_path / 'search', objects, legacy=legacy, generation='rank-cursor')
    queries = ('valve pressure', 'mesh sizing', '阀门压降')
    matches = normalized_matches(search, tmp_path, queries)
    with closing(sqlite3.connect(Path(built['directory']) / 'qmd-keyword.sqlite3')) as db:
        db.execute('CREATE TEMP TABLE expected_scope(path TEXT PRIMARY KEY,id TEXT)')
        for options in ({}, {'kind': 'code'}, {'feature_branch': 'mesh'}, {'kind': 'case', 'feature_branch': 'physics'}):
            for libraries in (['one'], ['one', 'two']):
                eligible = [item for item in objects if item['library_id'] in libraries
                            and (not options.get('kind') or item['kind'] == options['kind'])
                            and (not options.get('feature_branch') or object_feature_branch(item) == options['feature_branch'])]
                for query in queries:
                    for limit in (1, 6, 40):
                        expected = keyword_reference(db, eligible, matches[query], limit)
                        hits = search.search(query, library_ids=libraries, limit=limit, **options)
                        assert [(hit['id'], hit['retrieval']['score']) for hit in hits] == expected, (legacy, query, options, libraries, limit)


@pytest.mark.parametrize('branch', ['geometry', 'mesh', 'solver', 'physics', 'misc'])
def test_legacy_sql_scope_matches_metadata_precedence_without_loading_payloads(tmp_path, monkeypatch, branch):
    objects = [feature('alias', 'geom', legacy=True), feature('explicit', 'mesh'),
               feature('invalid', 'invalid', legacy=True), feature('all', 'all'),
               feature('solver', 'sol', legacy=True), feature('physics', 'physics')]
    objects[2].update(type='method_step', title='mesh and physics')
    objects[2]['data']['feature_branch'] = []
    objects[3].update(type='method_step', feature_branch='solver')
    objects[3]['data']['section'] = 'mesh'
    search, _, _ = scope_index(tmp_path / 'search', objects, legacy=True)
    expected = {item['id'] for item in objects if object_feature_branch(item) == branch}
    loads, decoded = json.loads, []

    def tracked(raw, *args, **kwargs):
        result = loads(raw, *args, **kwargs)
        if isinstance(result, dict) and 'library_id' in result and 'id' in result:
            decoded.append(result['id'])
            assert result['id'] in expected, 'Legacy scope must project metadata, never decode the candidate corpus'
        return result

    monkeypatch.setattr(json, 'loads', tracked)
    hits = search.search('mesh valve', kind='code', feature_branch=branch, limit=100)
    assert {hit['id'] for hit in hits} == expected
    assert decoded == [hit['id'] for hit in hits]
    assert search.last_search_scope['eligible_objects'] == len(expected)


def test_shared_keyword_sends_bounded_scope_and_rejects_foreign_metadata_hit(tmp_path, monkeypatch):
    search = FakeEngineSearch(tmp_path / 'search')
    target = feature('target', 'mesh')
    other = feature('foreign', 'physics')
    search.build([target, other] + [feature(str(index), 'mesh') for index in range(500)],
                 modes=['qmd_keyword'], generation='shared')
    search.calls.clear()
    hits = search.search('valve', mode='qmd_keyword', kind='code', feature_branch='mesh', limit=6)
    assert len(hits) == 6 and all(object_feature_branch(item) == 'mesh' for item in hits)
    request, = search.calls
    assert request['authoritative_scope']['feature_branch'] == 'mesh'
    assert all('allowed_paths' not in item for item in request['collections'])
    assert len(json.dumps(request)) < 1800
    monkeypatch.setattr(search, '_engine_shared_keyword', lambda request: [
        dict(filepath='qmd://' + native_index.qmd_path(other), score=1, source='fts')])
    with pytest.raises(RuntimeError, match='authoritative retrieval scope'):
        search.search('valve', mode='qmd_keyword', kind='code', feature_branch='mesh')


@pytest.mark.parametrize('mode', ['qmd_keyword', 'qmd_vector'])
def test_qmd_candidate_pool_is_independent_of_payload_limit(tmp_path, monkeypatch, mode):
    search = FakeEngineSearch(tmp_path / 'search')
    search.build([feature(str(index), 'mesh', library='one' if index % 2 else 'two') for index in range(100)],
                 modes=[mode], generation='pool')
    original = search.search('mesh valve', mode=mode, kind='code', feature_branch='mesh', limit=40)
    hydrated = []
    hydrate = native_index.hydrate_object

    def tracked(db, identity, **kwargs):
        hydrated.append(identity)
        return hydrate(db, identity, **kwargs)

    search.calls.clear()
    monkeypatch.setattr(native_index, 'hydrate_object', tracked)
    hits = search.search('mesh valve', mode=mode, kind='code', feature_branch='mesh', limit=6, candidate_limit=40)
    assert [(hit['id'], hit['retrieval']['score']) for hit in hits] == [
        (hit['id'], hit['retrieval']['score']) for hit in original[:6]]
    assert hydrated == [hit['id'] for hit in hits]
    assert all(call['limit'] == 40 for call in search.calls)


@pytest.mark.parametrize('change', ['holdout', 'visibility'])
def test_filtered_query_rechecks_policies_after_hydration(tmp_path, monkeypatch, change):
    target = feature('target', 'mesh')
    search = LibrarySearch(tmp_path / 'search')
    search.build([target], modes=['qmd_keyword'], generation='policy')
    policy = [HoldoutPolicy(token='clear')]
    visible = [{'cases'}]
    monkeypatch.setattr(module, 'exclusion_snapshot', lambda root: policy[0])
    monkeypatch.setattr(search, '_searchable_library_ids', lambda ids: visible[0])
    original = native_index.hydrate_object

    def changing(db, identity, **kwargs):
        item = original(db, identity, **kwargs)
        if change == 'holdout':
            policy[0] = policy_for([target])
        else:
            visible[0] = set()
        return item

    monkeypatch.setattr(native_index, 'hydrate_object', changing)
    assert search.search('mesh valve', kind='code', feature_branch='mesh') == []


def test_keyword_scope_cancellation_stops_before_payload_reads(tmp_path, monkeypatch):
    search = LibrarySearch(tmp_path / 'search')
    search.build([feature('target', 'mesh')], modes=['qmd_keyword'], generation='cancel')
    marker = tmp_path / 'cancel-requested'
    query = search._engine_shared_keyword
    def cancelled_after_query(request):
        result = query(request)
        marker.touch()
        return result

    monkeypatch.setattr(search, '_engine_shared_keyword', cancelled_after_query)
    monkeypatch.setattr(native_index, 'hydrate_object', lambda *args, **kwargs: pytest.fail('Cancelled queries must not hydrate'))
    with build_control(BuildControl(marker)), pytest.raises(BuildCancelled):
        search.search('mesh', kind='code', feature_branch='mesh')


@pytest.mark.parametrize('legacy', [False, True])
def test_filtered_scope_applies_holdout_before_topk_and_restores_without_rebuild(tmp_path, monkeypatch, legacy):
    held = [feature(str(index), 'mesh', text='mesh sizing ' * 12) for index in range(120)]
    target = feature('target', 'mesh', text='mesh sizing controls')
    search, _, path = scope_index(tmp_path / 'search', held + [target], legacy=legacy, generation='held')
    before = path.read_bytes()
    policy = [policy_for(held)]
    monkeypatch.setattr(module, 'exclusion_snapshot', lambda root: policy[0])
    assert [hit['id'] for hit in search.search('mesh sizing', kind='code', feature_branch='mesh', limit=1)] == [target['id']]
    assert search.last_search_scope['eligible_objects'] == 1
    policy[0] = HoldoutPolicy(token='restored')
    assert search.search('mesh sizing', kind='code', feature_branch='mesh', limit=1)[0]['id'] != target['id']
    assert path.read_bytes() == before


@pytest.mark.parametrize('legacy', [False, True])
def test_filtered_counts_probe_excluded_keys_and_deduplicate_identity_path_rules(tmp_path, monkeypatch, legacy):
    objects = [feature(str(index), 'mesh' if index % 2 else 'physics',
                       kind='code' if index % 3 else 'case', library='one' if index % 5 else 'two') for index in range(800)]
    search, _, path = scope_index(tmp_path / 'search', objects, legacy=legacy, generation='excluded-count')
    identities = frozenset(item['id'] for index, item in enumerate(objects) if index % 7 == 0)
    paths = frozenset(native_index.qmd_path(item) for index, item in enumerate(objects) if index % 11 == 0)
    policy = HoldoutPolicy(citation_ids=identities, qmd_paths=paths, token='mixed-identities-and-paths')
    monkeypatch.setattr(module, 'exclusion_snapshot', lambda root: policy)
    connect, statements = sqlite3.connect, []

    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(module.sqlite3, 'connect', traced)
    hits = search.search('valve mesh', kind='code', feature_branch='mesh', library_ids=['one', 'two'], limit=6)
    expected = {library: sum(item['library_id'] == library and item['kind'] == 'code' and object_feature_branch(item) == 'mesh'
                             and item['id'] not in identities and native_index.qmd_path(item) not in paths for item in objects)
                for library in ('one', 'two')}
    assert search.last_search_scope['libraries'] == expected
    assert all(item['id'] not in identities and native_index.qmd_path(item) not in paths for item in hits)
    base = (next(sql for sql in statements if sql.startswith('SELECT o.library_id,count(*) FROM objects o WHERE'))
            if legacy else module.query_scope_counts.BASE_SQL)
    excluded = (next(sql for sql in statements if sql.startswith('SELECT o.library_id,count(*) FROM evaluation_holdout.identities'))
                if legacy else module.query_scope_counts.EXCLUDED_SQL)
    assert 'NOT EXISTS' not in base and 'CROSS JOIN objects' in excluded
    policy_path = next((search.data_dir / 'runtime').glob('evaluation-exclusions-*.sqlite3'))
    with closing(connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        native_index.register_scope_functions(db)
        db.execute('ATTACH DATABASE ? AS evaluation_holdout', (policy_path.as_uri() + '?mode=ro',))
        base_plan = ' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + base))
        excluded_plan = ' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + excluded))
    if not legacy:
        assert 'COVERING INDEX objects_scope' in base_plan
        assert 'UNION' in excluded_plan and 'UNION ALL' not in excluded
        assert 'SEARCH x USING COVERING INDEX sqlite_autoindex_objects_1' in excluded_plan
        assert 'SEARCH x USING COVERING INDEX sqlite_autoindex_objects_2' in excluded_plan
    else:
        assert 'SEARCH o USING INDEX sqlite_autoindex_objects_1' in excluded_plan
    assert 'SCAN h' in excluded_plan and 'SCAN o' not in excluded_plan and 'SCAN x' not in excluded_plan


def test_real_shared_scope_matches_legacy_allowed_paths_scores_with_holdout(tmp_path, monkeypatch):
    search = LibrarySearch(tmp_path / 'search', cache_dir=tmp_path / 'cache')
    if not search.node or not (search.qmd_dir / 'dist/index.js').is_file() or search._qmd_version() != '2.8.3':
        pytest.skip('Optional supported local QMD/Node dependencies unavailable')
    objects = [feature(str(index), 'mesh' if index % 3 else 'physics', library='one' if index % 2 else 'two',
                       text='mesh sizing valve pressure ' * (1 + index % 7)) for index in range(160)]
    held = [item for index, item in enumerate(objects) if index % 13 == 0]
    search.build(objects, modes=['qmd_keyword'], generation='sql-parity')
    monkeypatch.setattr(module, 'exclusion_snapshot', lambda root: policy_for(held))
    requests = []
    original = search._engine_shared_keyword

    def tracked(request):
        requests.append(request)
        return original(request)

    monkeypatch.setattr(search, '_engine_shared_keyword', tracked)
    hits = search.search('mesh sizing valve', mode='qmd_keyword', kind='code', feature_branch='mesh', limit=8)
    request, = requests
    reference = dict(request)
    reference.pop('authoritative_scope')
    reference['collections'] = [dict(scope, allowed_paths=sorted(
        native_index.qmd_path(item).split('/')[1] for item in objects
        if item['library_id'] == scope['library_key'] and object_feature_branch(item) == 'mesh'))
        for scope in request['collections']]
    expected = original(reference)
    assert [(native_index.qmd_path(hit), hit['retrieval']['score']) for hit in hits] == [
        (row['filepath'].removeprefix('qmd://'), row['score']) for row in expected]
    old_pool = search.search('mesh sizing valve', mode='qmd_keyword', kind='code', feature_branch='mesh', limit=40)
    hydrated = []
    hydrate = native_index.hydrate_object

    def tracked_hydrate(db, identity, **kwargs):
        hydrated.append(identity)
        return hydrate(db, identity, **kwargs)

    monkeypatch.setattr(native_index, 'hydrate_object', tracked_hydrate)
    bounded = search.search('mesh sizing valve', mode='qmd_keyword', kind='code', feature_branch='mesh', limit=6, candidate_limit=40)
    assert [(hit['id'], hit['retrieval']['score']) for hit in bounded] == [
        (hit['id'], hit['retrieval']['score']) for hit in old_pool[:6]]
    assert hydrated == [hit['id'] for hit in bounded]
    assert requests[-1]['limit'] == 40
