"""Persist small scope matrices, invalidate source changes, and reclaim them."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

from advisor import library_search as search_module, query_scope_counts as module
from advisor.index_retention import prune_index_generations
from advisor.library_index_purge import purge_library_indexes
from advisor.library_search import LibrarySearch
from test_case_branch_search import feature
from test_evaluation_search import policy_for


@pytest.fixture(autouse=True)
def empty_memory():
    module._MEMORY.clear()
    yield
    module._MEMORY.clear()


@pytest.fixture
def index(tmp_path):
    objects = [feature(str(i), 'mesh' if i % 2 else 'physics',
                       kind='code' if i % 3 else 'case', library='one' if i % 5 else 'two') for i in range(120)]
    search = LibrarySearch(tmp_path / 'search', preserve_history=True)
    active = search.build(objects, modes=['native'], generation='counts')
    path = Path(active['directory']) / 'objects.sqlite3'
    return search, active, path, objects


def direct(path, active, *, statements=None, **kwargs):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        if statements is not None:
            db.set_trace_callback(statements.append)
        return module.counts(db, path, active, library_ids=['one', 'two'], **kwargs)


def test_all_scope_combinations_share_one_covering_aggregate_and_restart_reuses_sidecar(index):
    search, active, path, objects = index
    statements = []
    assert sum(direct(path, active, statements=statements).values()) == len(objects)
    assert statements.count(module.BASE_SQL) == 1
    with closing(sqlite3.connect(path)) as db:
        assert 'COVERING INDEX objects_scope' in ' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + module.BASE_SQL))
    statements.clear()
    for kind in ('all', 'code', 'case'):
        for branch in (None, 'mesh', 'physics', 'geometry'):
            expected = {library: sum(item['library_id'] == library and (kind == 'all' or item['kind'] == kind)
                                     and (branch is None or item['data']['feature_branch'] == branch) for item in objects)
                        for library in ('one', 'two')}
            assert direct(path, active, statements=statements, kind=kind, feature_branch=branch) == expected
    assert not statements, 'Warm scope totals must not issue any SQL'
    cache_path = path.with_name(module.CACHE_FILE)
    body = cache_path.read_text(encoding='utf-8')
    identity = json.loads(body)['base']['identity']
    assert 'path' not in identity and identity['file'] == path.name and len(body) < 8000
    assert not path.with_name(module.TEMP_FILE).exists()
    # A distinct interpreter has neither module memory nor a warmed engine.
    child = '''import json,sqlite3,sys
from pathlib import Path
from advisor.query_scope_counts import counts,BASE_SQL
path=Path(sys.argv[1]);statements=[]
db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);db.set_trace_callback(statements.append)
result=counts(db,path,{'generation':sys.argv[2],'corpus_hash':sys.argv[3]},library_ids=['one','two'])
print(json.dumps({'counts':result,'aggregates':statements.count(BASE_SQL)}));db.close()
'''
    result = subprocess.run([sys.executable, '-c', child, str(path), active['generation'], active['corpus_hash']],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'counts': {'one': 96, 'two': 24}, 'aggregates': 0}


def test_policy_changes_replace_one_excluded_slot_without_recounting_source(index, monkeypatch):
    search, active, path, objects = index
    search.search('mesh', kind='code', feature_branch='mesh')
    original = sqlite3.connect
    statements = []

    def traced(*args, **kwargs):
        db = original(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(search_module.sqlite3, 'connect', traced)
    policy = [policy_for(objects[:15])]
    monkeypatch.setattr(search_module, 'exclusion_snapshot', lambda root: policy[0])
    for number in range(4):
        policy[0] = policy_for(objects[number:number + 15])
        search.search('mesh', kind='code', feature_branch='mesh')
        expected = sum(item['kind'] == 'code' and item['data']['feature_branch'] == 'mesh'
                       and not policy[0].blocks_object(item) for item in objects)
        assert search.last_search_scope['eligible_objects'] == expected
    assert module.BASE_SQL not in statements and statements.count(module.EXCLUDED_SQL) == 4
    statements.clear()
    module._MEMORY.clear()
    search.search('mesh', kind='code', feature_branch='mesh')
    assert module.BASE_SQL not in statements and module.EXCLUDED_SQL not in statements
    assert [entry.name for entry in path.parent.glob('query-scope-counts*')] == [module.CACHE_FILE]
    payload = json.loads(path.with_name(module.CACHE_FILE).read_text(encoding='utf-8'))
    assert payload['excluded']['identity']['policy'] == policy[0].token


def test_modified_exclusion_database_invalidates_only_the_small_excluded_matrix(index, tmp_path):
    _, active, path, objects = index
    policy_path = tmp_path / 'exclusions.sqlite3'
    with closing(sqlite3.connect(policy_path)) as db, db:
        db.execute('CREATE TABLE identities(id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE paths(path TEXT PRIMARY KEY)')
        db.execute('INSERT INTO identities VALUES(?)', (objects[0]['id'],))

    def count():
        statements = []
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('ATTACH DATABASE ? AS evaluation_holdout', (policy_path.as_uri() + '?mode=ro',))
            db.set_trace_callback(statements.append)
            result = module.counts(db, path, active, policy_token='same-token', holdout_path=policy_path, library_ids=['one', 'two'])
        return result, statements

    first, _ = count()
    with closing(sqlite3.connect(policy_path)) as db, db:
        db.execute('INSERT INTO identities VALUES(?)', (objects[1]['id'],))
    second, statements = count()
    assert sum(second.values()) == sum(first.values()) - 1
    assert module.BASE_SQL not in statements and statements.count(module.EXCLUDED_SQL) == 1


@pytest.mark.parametrize('change', ['in_place', 'atomic_replace', 'wal'])
def test_database_changes_invalidate_counts_without_rebuilding(index, change):
    _, active, path, _ = index
    writer = sqlite3.connect(path)
    try:
        if change == 'wal':
            assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
            writer.execute('PRAGMA wal_autocheckpoint=0')
        initial = direct(path, active)
        before = module.database_signature(path)
        if change == 'atomic_replace':
            writer.close()
            replacement = path.with_suffix('.replacement')
            shutil.copyfile(path, replacement)
            with closing(sqlite3.connect(replacement)) as db, db:
                db.execute("UPDATE objects SET kind='manual' WHERE id='lo-1'")
            os.utime(replacement, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
            replacement.replace(path)
        else:
            writer.execute("UPDATE objects SET kind='manual' WHERE id='lo-1'")
            writer.commit()
        after = module.database_signature(path)
        assert before != after
        if change == 'wal':
            assert before[0] == after[0] and before[1] != after[1], 'An uncheckpointed change must invalidate through WAL'
        statements = []
        assert direct(path, active, statements=statements, kind='manual') == {'one': 1, 'two': 0}
        assert statements.count(module.BASE_SQL) == 1
        assert direct(path, active) == initial
    finally:
        writer.close()


@pytest.mark.parametrize('damage', ['schema', 'checksum', 'negative', 'duplicate', 'identity'])
def test_invalid_persisted_metadata_is_recomputed(index, damage):
    _, active, path, _ = index
    expected = direct(path, active)
    cache = path.with_name(module.CACHE_FILE)
    value = json.loads(cache.read_text(encoding='utf-8'))
    if damage == 'schema':
        value['schema'] = 'untrusted-schema'
    elif damage == 'checksum':
        value['base']['sha256'] = '0' * 64
    elif damage == 'negative':
        value['base']['rows'][0][3] = -1
    elif damage == 'duplicate':
        value['base']['rows'].append(value['base']['rows'][0])
    else:
        value['base']['identity']['database'][0][1] = -1
    cache.write_text(json.dumps(value), encoding='utf-8')
    module._MEMORY.clear()
    statements = []
    assert direct(path, active, statements=statements) == expected
    assert statements.count(module.BASE_SQL) == 1


def test_old_temporary_slot_recovers_and_memory_stays_bounded(index, tmp_path):
    _, active, path, _ = index
    temporary = path.with_name(module.TEMP_FILE)
    temporary.write_text('interrupted cache write', encoding='utf-8')
    os.utime(temporary, (time.time() - 120, time.time() - 120))
    direct(path, active)
    assert path.with_name(module.CACHE_FILE).is_file() and not temporary.exists()
    for index in range(module._MEMORY_LIMIT + 3):
        directory = tmp_path / ('copy-' + str(index))
        directory.mkdir()
        copied = directory / 'objects.sqlite3'
        shutil.copyfile(path, copied)
        direct(copied, dict(active, generation='copy-' + str(index)))
    assert len(module._MEMORY) == module._MEMORY_LIMIT


def test_cached_zero_is_not_a_retrieval_permission_and_live_holdout_still_applies(index, monkeypatch):
    search, active, path, objects = index
    direct(path, active)
    cache = path.with_name(module.CACHE_FILE)
    value = json.loads(cache.read_text(encoding='utf-8'))
    value['base'] = module._block(value['base']['identity'], [])
    cache.write_text(json.dumps(value), encoding='utf-8')
    module._MEMORY.clear()
    assert search.search('valve', kind='code', feature_branch='mesh', limit=1)
    monkeypatch.setattr(search_module, 'exclusion_snapshot', lambda root: policy_for(objects))
    # Restore accurate statistics before applying a complete live exclusion.
    cache.unlink()
    module._MEMORY.clear()
    assert search.search('valve', kind='code', feature_branch='mesh', limit=1) == []


def test_statistics_files_are_removed_with_retired_and_purged_generations(tmp_path):
    search = LibrarySearch(tmp_path / 'search', preserve_history=True)
    old = search.build([feature('old', 'mesh')], modes=['native'], generation='old')
    search.search('mesh')
    old_directory = Path(old['directory'])
    (old_directory / module.TEMP_FILE).write_text('interrupted writer', encoding='utf-8')
    current = search.build([feature('current', 'mesh')], modes=['native'], generation='current')
    os.utime(old_directory, (time.time() - 86400, time.time() - 86400))
    retired = prune_index_generations(search.data_dir, current_directory=current['directory'])
    assert retired['removed_generations'] == 1 and not old_directory.exists()
    search.search('mesh')
    directory = Path(current['directory'])
    assert (directory / module.CACHE_FILE).is_file()
    (directory / module.TEMP_FILE).write_text('interrupted writer', encoding='utf-8')
    purged = purge_library_indexes(search.data_dir, 'cases', generation='empty')
    assert purged['empty_generations_removed'] == 1 and not directory.exists()
