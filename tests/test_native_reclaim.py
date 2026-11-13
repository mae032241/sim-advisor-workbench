from contextlib import closing
import sqlite3

import pytest

from advisor.object_index import create_schema, insert_object, has_legacy_fts
from advisor.native_reclaim import stage_native_reclaim
from test_object_index import historical


@pytest.mark.parametrize('legacy', [False, True])
def test_reclaim_preserves_gapped_rowids_and_optional_historical_fts(tmp_path, legacy):
    source, destination = tmp_path / 'old.sqlite3', tmp_path / 'new.sqlite3'
    with closing(sqlite3.connect(source)) as db, db:
        if legacy:
            historical(db)
        else:
            create_schema(db)
            for rowid, name in [(2, 'mesh'), (19, 'solver'), (200, 'material')]:
                obj = {'id': name, 'library_id': 'models', 'title': name, 'type': 'saved_configuration'}
                insert_object(db, obj, 'mesh hmax ' + name, {}, '', rowid=rowid)
        db.execute('CREATE TABLE disposable(body BLOB)')
        db.execute('INSERT INTO disposable VALUES(zeroblob(300000))')
        db.execute('DROP TABLE disposable')
        expected = db.execute('SELECT rowid,* FROM objects ORDER BY rowid').fetchall()
        before = (db.execute("SELECT rowid,bm25(object_fts) FROM object_fts WHERE object_fts MATCH 'mesh' ORDER BY rowid").fetchall()
                  if legacy else None)
    result = stage_native_reclaim(source, destination)
    assert result['after_bytes'] < result['before_bytes']
    with closing(sqlite3.connect(destination)) as db:
        assert db.execute('SELECT rowid,* FROM objects ORDER BY rowid').fetchall() == expected
        assert has_legacy_fts(db) is legacy
        if legacy:
            assert db.execute("SELECT rowid,bm25(object_fts) FROM object_fts WHERE object_fts MATCH 'mesh' ORDER BY rowid").fetchall() == before


def test_refuses_unowned_native_schema_without_changes(tmp_path):
    source, destination = tmp_path / 'other.sqlite3', tmp_path / 'stage.sqlite3'
    with closing(sqlite3.connect(source)) as db:
        db.execute('CREATE TABLE data(value)')
    before = source.read_bytes()
    with pytest.raises(ValueError, match='owned'):
        stage_native_reclaim(source, destination)
    assert source.read_bytes() == before and not destination.exists()
