"""Disposable metadata counts for immutable object-directory generations.

The one fixed sidecar is display metadata, never a visibility/holdout permit.
QMD postings and live lifecycle predicates remain authoritative for hits.
"""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time

from .build_control import check_cancelled

SCHEMA = 'query-scope-counts/v1'
CACHE_FILE = 'query-scope-counts.json'
TEMP_FILE = CACHE_FILE + '.tmp'
_MAX_BYTES = 8 * 1024 * 1024
_MEMORY_LIMIT = 8
_MEMORY = OrderedDict()
_LOCK = threading.Lock()

BASE_SQL = ('SELECT library_id,kind,feature_branch,count(*) FROM objects '
            'GROUP BY library_id,kind,feature_branch')
EXCLUDED_SQL = '''SELECT o.library_id,o.kind,o.feature_branch,count(*) FROM (
    SELECT x.rowid AS rid FROM evaluation_holdout.identities h CROSS JOIN objects x ON x.id=h.id
    UNION
    SELECT x.rowid AS rid FROM evaluation_holdout.paths h CROSS JOIN objects x ON x.qmd_path=h.path
) excluded CROSS JOIN objects o ON o.rowid=excluded.rid
GROUP BY o.library_id,o.kind,o.feature_branch'''


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def database_signature(path, *, change_counter=False):
    """Include uncheckpointed writes and atomic replacement, never reader SHM."""
    result = []
    for suffix in ('', '-wal', '-journal'):
        try:
            info = Path(str(path) + suffix).stat()
            value = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
            if change_counter and not suffix:
                # Windows may coalesce timestamps for successive small commits.
                # SQLite's four-byte change counter still distinguishes them.
                with Path(path).open('rb') as source:
                    source.seek(24)
                    value.append(source.read(4).hex())
            if suffix == '-wal' and info.st_size == 0:
                value = None  # Opening a WAL reader can create an empty file.
        except OSError:
            value = None
        result.append(value)
    return result


def _matrix(rows):
    result, seen = [], set()
    if not isinstance(rows, list):
        raise ValueError('Scope counts must be a matrix')
    for row in rows:
        if (not isinstance(row, (list, tuple)) or len(row) != 4 or not isinstance(row[0], str)
                or not row[0] or any(value is not None and (not isinstance(value, str) or len(value) > 4096) for value in row[:3])
                or type(row[3]) is not int or not 0 < row[3] <= 2 ** 63 - 1 or tuple(row[:3]) in seen):
            raise ValueError('Invalid scope count metadata')
        seen.add(tuple(row[:3]))
        result.append(list(row))
    return result


def _block(identity, rows):
    rows = _matrix(rows)
    return {'identity': identity, 'rows': rows, 'sha256': hashlib.sha256(_encoded(rows).encode('utf-8')).hexdigest()}


def _validated_block(value, identity):
    if not isinstance(value, dict) or set(value) != {'identity', 'rows', 'sha256'} or value['identity'] != identity:
        return None
    try:
        block = _block(identity, value['rows'])
        return block if block['sha256'] == value['sha256'] else None
    except (ValueError, TypeError):
        return None


def _regular(path):
    try:
        info = path.lstat()
        return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and not getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))
    except OSError:
        return False


def _read(path, identity):
    try:
        if path.parent.resolve() != path.parent or not _regular(path) or path.stat().st_size > _MAX_BYTES:
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('schema') != SCHEMA or set(value) != {'schema', 'base', 'excluded'}:
            return None
        base = _validated_block(value['base'], identity)
        return dict(value, base=base) if base is not None else None
    except (OSError, ValueError, TypeError):
        return None


def _write(path, value):
    """One fixed temporary slot bounds debris even after an interrupted write."""
    temporary = path.with_name(TEMP_FILE)
    owned_temporary = False
    try:
        if path.parent.resolve() != path.parent or path.exists() and not _regular(path):
            return
        if _regular(temporary) and time.time() - temporary.stat().st_mtime > 60:
            temporary.unlink()  # Recover the bounded slot after a crashed writer.
        body = _encoded(value)
        if len(body.encode('utf-8')) > _MAX_BYTES:
            return
        with temporary.open('x', encoding='utf-8') as output:
            owned_temporary = True
            output.write(body)
        os.replace(temporary, path)
    except OSError:
        pass  # A read-only generation or another writer only loses reuse.
    finally:
        if owned_temporary:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def counts(db, database_path, active, *, policy_token='', holdout_path=None, library_ids, kind='all', feature_branch=None):
    """Aggregate a small persisted matrix; scan source metadata only on change."""
    database_path = Path(database_path)
    signature = database_signature(database_path)
    identity = {'file': database_path.name, 'generation': active.get('generation'),
                'corpus_hash': active.get('corpus_hash'), 'database': signature}
    key = _encoded([os.path.normcase(str(database_path.absolute())), identity])
    cache_path = database_path.with_name(CACHE_FILE)
    excluded_identity = ({'policy': policy_token, 'database': database_signature(holdout_path, change_counter=True)}
                         if holdout_path is not None else None)
    with _LOCK:
        check_cancelled()
        record = _MEMORY.get(key) or _read(cache_path, identity)
        dirty = record is None
        if record is None:
            record = {'schema': SCHEMA, 'base': _block(identity, db.execute(BASE_SQL).fetchall()), 'excluded': None}
        excluded = []
        if excluded_identity is not None:
            block = _validated_block(record.get('excluded'), excluded_identity)
            if block is None:
                block = _block(excluded_identity, db.execute(EXCLUDED_SQL).fetchall())
                record['excluded'] = block
                dirty = True
            excluded = block['rows']
        if database_signature(database_path) != signature:
            raise RuntimeError('Native index changed while preparing scope statistics; retry the query')
        check_cancelled()
        if dirty:
            _write(cache_path, record)
        _MEMORY[key] = record
        _MEMORY.move_to_end(key)
        while len(_MEMORY) > _MEMORY_LIMIT:
            _MEMORY.popitem(last=False)
        libraries = set(library_ids)
        totals = {library: 0 for library in libraries}
        for rows, sign in ((record['base']['rows'], 1), (excluded, -1)):
            for library, row_kind, branch, count in rows:
                if library in libraries and (kind == 'all' or row_kind == kind) and (feature_branch is None or branch == feature_branch):
                    totals[library] += sign * count
        if any(count < 0 for count in totals.values()):
            _MEMORY.pop(key, None)
            raise ValueError('Excluded scope counts exceed indexed metadata')
        return totals
