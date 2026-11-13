"""Logical holdouts exclude candidates before TopK without rebuilding indexes."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from advisor import library_search as module
from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_search import LibrarySearch
from advisor.object_index import qmd_path
from test_library_search import FakeEngineSearch, enable_test_embedding, obj


def policy_for(items, *, paths=False):
    identities = frozenset(item['id'] for item in items)
    return HoldoutPolicy(citation_ids=frozenset() if paths else identities,
                         object_ids=frozenset(item['object_id'] for item in items) if not paths else frozenset(),
                         qmd_paths=frozenset(qmd_path(item) for item in items) if paths else frozenset(),
                         token=hashlib.sha256(json.dumps(sorted(identities)).encode()).hexdigest())


def snapshot(paths):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def test_opening_and_restoring_web_gate_reuses_existing_keyword_index(tmp_path, monkeypatch):
    search = LibrarySearch(tmp_path / 'search')
    held = obj('held', text='exclusive holdout needle')
    legal = obj('legal', text='ordinary material reference')
    search.build([held, legal], modes=['qmd_keyword'], generation='gate-existing')
    policy = [policy_for([held])]
    monkeypatch.setattr(module, 'exclusion_snapshot', lambda root: policy[0])
    directory = Path(search._load_active(summary=True)['directory'])
    files = [directory / 'objects.sqlite3', search.active_path]
    before = snapshot(files)
    assert search.search('exclusive needle', library_ids=['cases']) == []
    policy[0] = HoldoutPolicy(token='web-open:' + policy[0].token)
    assert [hit['id'] for hit in search.search('exclusive needle', library_ids=['cases'])] == [held['id']]
    policy[0] = policy_for([held])
    assert search.search('exclusive needle', library_ids=['cases']) == []
    assert snapshot(files) == before


def test_unfiltered_counts_use_covering_index_and_cached_exclusion_totals(tmp_path, monkeypatch):
    search = LibrarySearch(tmp_path / 'search')
    held, legal = obj('held'), obj('legal')
    search.build([held, legal], modes=['qmd_keyword'], generation='counts')
    monkeypatch.setattr(module, 'exclusion_snapshot', lambda root: policy_for([held]))
    connect, statements = sqlite3.connect, []

    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(module.sqlite3, 'connect', traced)
    assert [hit['id'] for hit in search.search('valve')] == [legal['id']]
    count_sql = next(sql for sql in statements if sql == module.query_scope_counts.BASE_SQL)
    with closing(connect((Path(search._load_active(summary=True)['directory']) / 'objects.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        plan = ' '.join(str(row) for row in db.execute('EXPLAIN QUERY PLAN ' + count_sql))
    assert 'COVERING INDEX objects_scope' in plan and 'CORRELATED' not in plan
    cache = next((search.data_dir / 'runtime').glob('evaluation-exclusions-*.sqlite3'))
    with closing(connect(cache)) as db:
        assert db.execute('SELECT library_id,object_count FROM excluded_counts').fetchall() == [('cases', 1)]
    assert search.last_search_scope['eligible_objects'] == 1


@pytest.mark.parametrize('path_rule', [False, True])
@pytest.mark.parametrize('legacy', [False, True])
def test_keyword_excludes_strong_hits_before_topk_even_with_explicit_library(tmp_path, monkeypatch, path_rule, legacy):
    search = LibrarySearch(tmp_path / 'search')
    held = [obj('strong-' + str(i), text='mesh sizing ' * 12) for i in range(180)]
    target = obj('weak', text='mesh sizing controls tetrahedral elements')
    built = search.build(held + [target], modes=['qmd_keyword'], generation='existing')
    if legacy:
        # A historical, unchanged generation has source JSON and keeps QMD
        # locators in its manifest rather than the objects table.
        old = tmp_path / 'legacy.sqlite3'
        with closing(sqlite3.connect(old)) as db, db:
            db.execute('CREATE TABLE objects(id TEXT PRIMARY KEY,library_id TEXT,title TEXT,text TEXT,object_json TEXT)')
            db.execute("CREATE VIRTUAL TABLE object_fts USING fts5(id UNINDEXED,title,text,cjk,tokenize='porter unicode61')")
            for item in held + [target]:
                text = module._representation(item)[0]
                db.execute('INSERT INTO objects VALUES(?,?,?,?,?)', (item['id'], item['library_id'], item['title'], text, json.dumps(item)))
                db.execute('INSERT INTO object_fts VALUES(?,?,?,?)', (item['id'], item['title'], text, ''))
        old.replace(Path(built['directory']) / 'objects.sqlite3')
        legacy_manifest = dict(built)
        legacy_manifest.pop('object_lookup', None)
        search.active_path.write_text(json.dumps(module._serialize_manifest(legacy_manifest, search.data_dir)), encoding='utf-8')
    assert search.search('mesh sizing', limit=1)[0]['id'] != target['id']
    files = [search.active_path, Path(built['directory']) / 'objects.sqlite3']
    before = snapshot(files)
    policy = policy_for(held, paths=path_rule)
    monkeypatch.setattr(module, 'exclusion_snapshot', lambda root: policy)
    for selection in (None, ['cases'], 'cases'):
        hits = search.search('mesh sizing', library_ids=selection, limit=1)
        assert [hit['id'] for hit in hits] == [target['id']]
        assert hits[0]['retrieval']['generation'] == 'existing'
        assert hits[0]['retrieval']['search_scope']['eligible_objects'] == 1
    cache = list((search.data_dir / 'runtime').glob('evaluation-exclusions-*.sqlite3'))
    assert len(cache) == 1
    cached = snapshot(cache)
    assert search.search('mesh sizing', library_ids=['cases'], limit=1)[0]['id'] == target['id']
    assert snapshot(cache) == cached and snapshot(files) == before


class ExclusionEngine(FakeEngineSearch):
    """Independent bridge boundary fixture applying SQL exclusion before LIMIT."""
    def _engine(self, request, *, timeout=120):
        if request['op'] not in {'shared_keyword', 'keyword', 'vector'} or 'holdout_db_path' not in request:
            return super()._engine(request, timeout=timeout)
        self.calls.append(dict(request))
        with closing(sqlite3.connect(request['db_path'], uri=True)) as db:
            db.execute('ATTACH DATABASE ? AS policy', (Path(request['holdout_db_path']).as_uri() + '?mode=ro',))
            if request['op'] == 'shared_keyword':
                collections = [entry['collection'] for entry in request['collections']]
                rows = db.execute('SELECT collection,path FROM documents d WHERE collection IN (' + ','.join('?' for _ in collections) + ') '
                                  "AND NOT EXISTS(SELECT 1 FROM policy.paths h WHERE h.path=d.collection || '/' || d.path) ORDER BY path LIMIT ?",
                                  [*collections, request['limit']]).fetchall()
                return [{'filepath': 'qmd://' + collection + '/' + path, 'score': .1, 'source': 'fts'} for collection, path in rows]
            rows = db.execute("SELECT value FROM fixture_paths WHERE NOT EXISTS(SELECT 1 FROM policy.paths h WHERE h.path=? || '/' || value) ORDER BY value LIMIT ?",
                              (request['holdout_collection'], request['limit'])).fetchall()
            return [{'filepath': 'qmd://objects/' + path, 'score': .1, 'source': 'vec' if request['op'] == 'vector' else 'fts'} for path, in rows]


@pytest.mark.parametrize('mode', ['qmd_keyword', 'qmd_vector'])
def test_qmd_orchestration_passes_policy_by_filename_and_rejects_illegal_bridge_hits(tmp_path, monkeypatch, mode):
    search = ExclusionEngine(tmp_path / 'search')
    held, target = obj('held'), obj('target')
    built = search.build([held, target], modes=['qmd_keyword', 'qmd_vector'], generation='existing')
    before = snapshot([search.active_path, Path(built['directory']) / 'objects.sqlite3'])
    monkeypatch.setattr(module, 'exclusion_snapshot', lambda root: policy_for([held]))
    search.calls.clear()
    assert [hit['id'] for hit in search.search('valve', mode=mode, library_ids=['cases'], limit=1)] == [target['id']]
    assert search.calls and all('holdout_db_path' in call and 'excluded_ids' not in call for call in search.calls)
    real_engine = search._engine

    def forbidden(request, **kwargs):
        if request['op'] == 'batch':
            return [{'library_key': item['library_key'], 'results': forbidden(item)} for item in request['requests']]
        if request['op'] == 'shared_keyword':
            return [{'filepath': 'qmd://' + qmd_path(held), 'score': 1, 'source': 'fts'}]
        if request['op'] == 'vector':
            return [{'filepath': 'qmd://objects/' + qmd_path(held).split('/')[1], 'score': 1, 'source': 'vec'}]
        return real_engine(request, **kwargs)

    monkeypatch.setattr(search, '_engine', forbidden)
    monkeypatch.setattr(module.object_index, 'hydrate_object', lambda *args, **kwargs: pytest.fail('Excluded payload must not hydrate'))
    with pytest.raises(RuntimeError, match='evaluation-excluded'):
        search.search('valve', mode=mode, library_ids=['cases'], limit=1)
    assert snapshot([search.active_path, Path(built['directory']) / 'objects.sqlite3']) == before


def supported_qmd(tmp_path):
    search = LibrarySearch(tmp_path / 'search', cache_dir=tmp_path / 'cache')
    if not search.node or not (search.qmd_dir / 'dist/index.js').is_file() or search._qmd_version() != '2.8.3':
        pytest.skip('Optional supported local QMD/Node dependencies unavailable')
    return search


def test_real_shared_keyword_excludes_high_scores_before_topk_without_reindex(tmp_path, monkeypatch):
    search = supported_qmd(tmp_path)
    held = [obj('strong-' + str(i), text='mesh sizing ' * 12) for i in range(120)]
    target = obj('weak', text='mesh sizing controls tetrahedral elements')
    built = search.build(held + [target], modes=['qmd_keyword'], generation='existing')
    assert built['modes']['qmd_keyword']['ready']
    assert search.search('mesh sizing', mode='qmd_keyword', limit=1)[0]['id'] != target['id']
    files = [search.active_path, Path(built['directory']) / 'objects.sqlite3', Path(built['directory']) / 'qmd-keyword.sqlite3']
    before = snapshot(files)
    monkeypatch.setattr(module, 'exclusion_snapshot', lambda root: policy_for(held))
    for feature in (None, 'mesh'):
        # No feature is needed to install the holdout predicate. A separate
        # empty feature scope must remain empty and must not relax exclusions.
        hits = search.search('mesh sizing', mode='qmd_keyword', library_ids=['cases'], limit=1, feature_branch=feature)
        assert [hit['id'] for hit in hits] == ([target['id']] if feature is None else [])
    assert snapshot(files) == before


def test_real_legacy_keyword_and_vectors_exclude_before_scoring_and_topk(tmp_path):
    probe = supported_qmd(tmp_path)
    enable_test_embedding(tmp_path / 'query')
    wrapper, database = tmp_path / 'SDK wrapper', tmp_path / 'library/index.sqlite3'
    (wrapper / 'dist').mkdir(parents=True)
    database.parent.mkdir()
    (database.parent / 'corpus').mkdir()
    (wrapper / 'package.json').write_text('{"type":"module","version":"2.8.3"}', encoding='utf-8')
    index_url, store_url, llm_url = [(probe.qmd_dir / ('dist/' + name + '.js')).as_uri() for name in ('index', 'store', 'llm')]
    model = 'hf:fixture/embeddinggemma.gguf'
    setup = """import {createStore} from INDEX;import {getEmbeddingFingerprint} from STORE;
      const model=MODEL,now='2026-11-11T00:00:00Z';
      const s=await createStore({dbPath:DB,config:{collections:{objects:{path:'corpus',pattern:'**/*.md'}},models:{embed:model}}});
      try{
        s.internal.ensureVecTable(2);
        for(const [name,body,vector] of [['held.md','mesh sizing '.repeat(100),[1,0]],['target.md','mesh sizing controls tetrahedral elements',[0,1]]]){
          s.internal.insertContent(name,body,now);s.internal.insertDocument('objects',name,name,name,now,now);
          s.internal.insertEmbedding(name,0,0,new Float32Array(vector),model,now,1,getEmbeddingFingerprint(model));
        }
      }finally{await s.close();}
    """.replace('INDEX', json.dumps(index_url)).replace('STORE', json.dumps(store_url)).replace('MODEL', json.dumps(model)).replace('DB', json.dumps(str(database)))
    result = subprocess.run([probe.node, '--input-type=module', '-e', setup], cwd=database.parent,
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    source = """import {createStore as original} from INDEX;
      globalThis.fetch=()=>{throw new Error('Network forbidden');};
      export async function createStore(options){
        const s=await original(options);
        s.update=s.embed=()=>{throw new Error('Index mutation forbidden');};
        s.searchLex=s.internal.searchVec=()=>{throw new Error('Unfiltered SDK retrieval forbidden');};
        s.internal.llm.embed=async()=>({embedding:[1,0]});return s;
      }""".replace('INDEX', json.dumps(index_url))
    (wrapper / 'dist/index.js').write_text(source, encoding='utf-8')
    (wrapper / 'dist/store.js').write_text('export {getEmbeddingFingerprint,normalizeCjkForFTS} from ' + json.dumps(store_url) + ';', encoding='utf-8')
    (wrapper / 'dist/llm.js').write_text('export {formatQueryForEmbedding} from ' + json.dumps(llm_url) + ';', encoding='utf-8')
    search = LibrarySearch(tmp_path / 'query', qmd_dir=wrapper, node=probe.node, cache_dir=tmp_path / 'cache')
    collection = qmd_path(obj('held')).split('/')[0]
    excluded = tmp_path / 'policy.sqlite3'
    with closing(sqlite3.connect(excluded)) as db, db:
        db.execute('CREATE TABLE paths(path TEXT PRIMARY KEY)')
        db.execute('INSERT INTO paths VALUES(?)', (collection + '/held.md',))
    before = snapshot([database])
    for mode in ('keyword', 'vector'):
        request = {'op': mode, 'db_path': str(database), 'corpus': str(database.parent / 'corpus'), 'query': 'mesh sizing',
                   'index_model_uri': model, 'limit': 1, 'holdout_db_path': str(excluded), 'holdout_collection': collection}
        for allowed in (None, ['held.md', 'target.md']):
            if allowed is not None:
                request['allowed_paths'] = allowed
            assert [hit['filepath'] for hit in search._engine(request)] == ['qmd://objects/target.md']
    assert snapshot([database]) == before
