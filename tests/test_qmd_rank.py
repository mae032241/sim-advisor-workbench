"""The FTS rank cursor preserves QMD BM25 rows, ties and scoped TopK."""
from contextlib import closing
import json
import sqlite3

import pytest

from advisor.qmd_worker import close_workers
from test_qmd_shared_query import collection, runtime, make_database, search_runtime, shared_request


@pytest.fixture(autouse=True)
def release_query_workers():
    yield
    close_workers()


def instrument_rank_parity(wrapper):
    path = wrapper / "dist/index.js"
    source = path.read_text(encoding="utf-8")
    source = source.replace("/FROM documents_fts CROSS JOIN/i", "/FROM (?:main\\.)?documents_fts CROSS JOIN/i")
    original = "statement.all=(...args)=>{trace('query',{sql,match:args[0]});return all(...args);};"
    replacement = r"""statement.all=(...args)=>{
      const actual=all(...args);
      const legacy=sql.replace('documents_fts.rank AS bm25_score','bm25(documents_fts,1.5,4.0,1.0) AS bm25_score')
        .replace(" AND documents_fts.rank MATCH 'bm25(1.5,4.0,1.0)'",'')
        .replace('ORDER BY documents_fts.rank ASC','ORDER BY bm25_score ASC');
      if(legacy===sql)throw new Error('Rank parity test did not reach the optimized SQL');
      const expected=prepare(legacy).all(...args);
      const plan=prepare('EXPLAIN QUERY PLAN '+sql).all(...args);
      const legacy_plan=prepare('EXPLAIN QUERY PLAN '+legacy).all(...args);
      trace('rank_parity',{actual,expected,plan,legacy_plan});
      if(JSON.stringify(actual)!==JSON.stringify(expected))throw new Error('Raw BM25 row, score or tie order changed');
      return actual;
    };"""
    assert original in source
    path.write_text(source.replace(original, replacement), encoding="utf-8")


def official_scores(record):
    return [{'filepath': hit['filepath'], 'score': abs(hit['bm25_score']) / (1 + abs(hit['bm25_score'])),
             'source': 'fts'} for hit in record['expected']]


@pytest.mark.parametrize("layout", ["shared", "legacy"])
def test_real_qmd_rank_preserves_bm25_scores_ties_and_scopes_without_sorting(tmp_path, layout):
    probe, wrapper, trace = runtime(tmp_path)
    instrument_rank_parity(wrapper)
    libraries = ("first", "second", "closed") if layout == "shared" else ("first",)
    rows = []
    for library in libraries:
        # Hundreds of exactly tied rows cross the 40-candidate boundary. Closed
        # collections and held-out rows deliberately contain stronger matches.
        for index in range(220):
            title = "needle " * 30 if library == "closed" or index < 60 else "Mesh"
            body = "needle mesh alpha 模型" if index % 7 else "needle mesh thermal 模型"
            rows.append({"collection": collection(library) if layout == "shared" else "objects",
                         "path": f"row-{index:03}.md", "title": title, "body": body, "library": library})
    database = make_database(tmp_path, probe, rows)
    policy = tmp_path / "policy.sqlite3"
    native = tmp_path / "scope.sqlite3"
    with closing(sqlite3.connect(policy)) as db, db:
        db.executescript('CREATE TABLE paths(path TEXT PRIMARY KEY); CREATE TABLE identities(id TEXT PRIMARY KEY);')
        db.executemany('INSERT INTO paths VALUES(?)', [(collection(row["library"]) + "/" + row["path"],)
                      for row in rows if int(row["path"][4:7]) < 70])
        db.execute('INSERT INTO identities VALUES(?)', ('first-row-100.md',))
    with closing(sqlite3.connect(native)) as db, db:
        db.execute('CREATE TABLE objects(id TEXT PRIMARY KEY,library_id TEXT,kind TEXT,feature_branch TEXT,qmd_path TEXT,source_json TEXT)')
        db.executemany('INSERT INTO objects VALUES(?,?,?,?,?,?)', [
            (row['library'] + '-' + row['path'], row['library'], 'case' if index % 2 else 'code',
             'mesh' if index % 3 else 'geometry', row['collection'] + '/' + row['path'], '{}')
            for index, row in enumerate(rows)])
    search = search_runtime(tmp_path, wrapper, trace, probe.node)
    queries = ['needle mesh', '"needle mesh"', 'alpha -thermal', '模型']
    for limit in (1, 7, 40):
        for scope in ("all", "paths", "authoritative"):
            if layout == "legacy" and scope == "authoritative":
                continue
            if layout == "shared":
                request = shared_request(database, ["first", "second"], queries[0], limit=limit)
                if scope == "paths":
                    for item in request['collections']:
                        item['allowed_paths'] = [f'row-{index:03}.md' for index in range(180, 220)]
                if scope == "authoritative":
                    request['authoritative_scope'] = {'db_path': str(native), 'kind': 'case', 'feature_branch': 'mesh'}
                if scope != "all":
                    request['holdout_db_path'] = str(policy)
            else:
                request = {'op': 'keyword', 'db_path': str(database), 'corpus': str(database.parent / 'corpus'),
                           'query': queries[0], 'limit': limit,
                           'holdout_db_path': str(policy), 'holdout_collection': collection('first')}
                if scope == 'paths':
                    request['allowed_paths'] = [f'row-{index:03}.md' for index in range(180, 220)]
            for query in queries:
                request['query'] = query
                previous = len(trace.read_text(encoding='utf-8').splitlines()) if trace.exists() else 0
                actual = search._engine(request)
                records = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()[previous:]]
                comparisons = [event for event in records if event['event'] == 'rank_parity']
                assert len(comparisons) == 1
                assert actual == official_scores(comparisons[0])
                for event in comparisons:
                    assert event['actual'] == event['expected']
                    assert not any('USE TEMP B-TREE FOR ORDER BY' in row['detail'] for row in event['plan'])
                    assert any('USE TEMP B-TREE FOR ORDER BY' in row['detail'] for row in event['legacy_plan'])
