"""Existing catalogs must support exact source-to-case policy checks by index."""
from contextlib import closing
import sqlite3

from advisor.library_store import LibraryStore


def test_old_catalog_gains_covering_citation_lookup_without_changing_members(tmp_path):
    root = tmp_path / 'libraries'
    root.mkdir()
    path = root / 'catalog.sqlite3'
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE case_members(group_id TEXT NOT NULL,local_id TEXT NOT NULL,'
                   'citation_id TEXT NOT NULL,PRIMARY KEY(group_id,local_id))')
        db.executemany('INSERT INTO case_members VALUES(?,?,?)',
                       [('group-'+str(i % 100), 'member-'+str(i), 'citation-'+str(i)) for i in range(20000)])

    store = LibraryStore(root)
    with closing(store.connect()) as db:
        db.execute('CREATE TEMP TABLE heldout_groups(case_group TEXT PRIMARY KEY)')
        db.executemany('INSERT INTO heldout_groups VALUES(?)', [('group-1',), ('group-2',)])
        statement = ('SELECT 1 FROM heldout_groups h JOIN case_members m ON m.group_id=h.case_group '
                     'WHERE m.citation_id=?')
        for identity, expected in [('citation-1', True), ('citation-3', False), ('missing', False)]:
            assert bool(db.execute(statement, (identity,)).fetchone()) is expected
        plan = [row[3] for row in db.execute('EXPLAIN QUERY PLAN '+statement, ('missing',))]
        assert any('case_members_citation' in detail and 'SEARCH m' in detail for detail in plan), plan
        assert not any('SCAN m' in detail for detail in plan), plan
        assert db.execute('SELECT count(*) FROM case_members').fetchone()[0] == 20000

    # Reopening an upgraded catalog is idempotent.
    reopened = LibraryStore(root)
    with closing(reopened.connect()) as db:
        assert db.execute('SELECT count(*) FROM case_members').fetchone()[0] == 20000
