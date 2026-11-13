"""Reclaim object metadata pages without changing slim or historical schemas."""
from contextlib import closing
from pathlib import Path
import sqlite3

from .build_control import check_cancelled
from .catalog_storage import _hash_rows, _table_query
from .object_index import has_legacy_fts, owned_schema


def _snapshot(db):
    return {name: _hash_rows(db.execute(_table_query(db, name)[0]))
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}


def stage_native_reclaim(source, destination):
    """Stage VACUUM only when every rowid, row value and posting stays exact."""
    source, destination = Path(source).resolve(strict=True), Path(destination).resolve()
    if source == destination or destination.exists():
        raise ValueError('Native reclaim needs a new staging file')
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as old:
            if not owned_schema(old):
                raise ValueError('Only an owned object metadata schema can be reclaimed')
            expected = _snapshot(old)
            with closing(sqlite3.connect(destination)) as new:
                old.backup(new, pages=256, progress=lambda *_: check_cancelled())
                new.execute('PRAGMA journal_mode=DELETE')
                new.execute('VACUUM')
                if not owned_schema(new) or _snapshot(new) != expected:
                    raise ValueError('Native VACUUM changed rowids or FTS records; publication refused')
                if has_legacy_fts(new):
                    new.execute("INSERT INTO object_fts(object_fts) VALUES('integrity-check')")
                new.commit()
                if new.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Native reclaimed file failed integrity check')
        return {'before_bytes': source.stat().st_size, 'after_bytes': destination.stat().st_size,
                'objects': expected['objects']['rows'], 'rows_and_postings_unchanged': True}
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
