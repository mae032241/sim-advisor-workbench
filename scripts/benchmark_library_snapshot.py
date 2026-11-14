"""Independent, writable library snapshots; no extraction or reindexing.

Original MPH/manual sources stay external. Only the copied catalog's lifecycle
flags change; QMD and all authorization layers read the same persisted state.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from advisor.index_manifest_cache import read_manifest
from advisor.query_scope_counts import database_signature

SCHEMA = 'benchmark-library-snapshot/v1'
MARKER = 'benchmark-snapshot.json'


def source_flags(root):
    path = Path(root).resolve() / 'catalog.sqlite3'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT id,body FROM libraries ORDER BY id').fetchall()
    return {identity: json.loads(body).get('enabled', True) for identity, body in rows}


def _stamp(root):
    names = ('search/active.json', 'evaluation-sets.json', 'evaluation-access.json')
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            if (root / name).is_file() else None for name in names}


def _copy_tree(source, target):
    if not source.exists():
        return
    def ignored(directory, names):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise ValueError('Snapshot refuses linked entries: ' + str(path))
        return [name for name in names if name.endswith(('-wal', '-shm', '-journal'))]
    shutil.copytree(source, target, ignore=ignored)


def prepare_snapshot(source_root, destination):
    source, target = Path(source_root).resolve(strict=True), Path(destination).absolute()
    if target.exists():
        raise FileExistsError('Snapshot destination must be new: ' + str(target))
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('Snapshot and production roots must not overlap')
    flags, stamp = source_flags(source), _stamp(source)
    active = read_manifest(source / 'search/active.json', summary=True, resolve_paths=True)
    if not active:
        raise ValueError('Snapshot requires an existing active index')
    from advisor.case_context_store import validated_contexts
    from advisor.case_navigation import validated_navigation
    context = validated_contexts(source / 'search', active)
    navigation = validated_navigation(source / 'search', active)
    object_path = Path(active['directory']) / 'objects.sqlite3'
    object_signature = database_signature(object_path)
    target.mkdir(parents=True)
    with closing(sqlite3.connect((source / 'catalog.sqlite3').as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(target / 'catalog.sqlite3')) as dst:
            src.backup(dst)
    for name in ('artifacts', 'blobs', 'html-trees'):
        _copy_tree(source / name, target / name)
    (target / 'search').mkdir()
    _copy_tree(source / 'search/generations', target / 'search/generations')
    shutil.copy2(source / 'search/active.json', target / 'search/active.json')
    for name in ('evaluation-sets.json', 'evaluation-access.json'):
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)
    anchor = source.parent / 'workspace-location.json'
    if anchor.is_file():
        shutil.copy2(anchor, target.parent / anchor.name)
    with closing(sqlite3.connect(target / 'catalog.sqlite3')) as db, db:
        for identity, raw in db.execute('SELECT id,body FROM libraries').fetchall():
            value = json.loads(raw)
            value['enabled'] = value.get('type') in {'official-models', 'official-manuals'} and value.get('deleted_at') is None
            db.execute('UPDATE libraries SET body=? WHERE id=?',
                       (json.dumps(value, ensure_ascii=False, separators=(',', ':')), identity))
    copied_active = read_manifest(target / 'search/active.json', summary=True, resolve_paths=True)
    new_signature = database_signature(Path(copied_active['directory']) / 'objects.sqlite3')
    # Validated disposable sidecars keep all rows and their generation. Only
    # physical object-file identity changes when the exact index is copied.
    for located in (context, navigation):
        if located is None:
            continue
        src = Path(located['path'])
        dst = target / src.relative_to(source)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        with closing(sqlite3.connect(dst)) as db, db:
            db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(new_signature), 'objects_signature'))
    if (_stamp(source) != stamp or source_flags(source) != flags or
            database_signature(object_path) != object_signature):
        raise RuntimeError('Source generation/policy changed during snapshot; do not use this copy')
    validated_contexts(target / 'search', copied_active)
    validated_navigation(target / 'search', copied_active)
    report = {'schema': SCHEMA, 'source_root': str(source), 'destination': str(target),
              'source_stamp': stamp, 'source_flags': flags, 'generation': active['generation'],
              'corpus_hash': active['corpus_hash'], 'embedding_invoked': False,
              'model_extraction_invoked': False, 'production_flags_changed': False,
              'sidecar_change': 'physical objects_signature only; rows and generation unchanged'}
    (target / MARKER).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def require_snapshot(source_root, destination):
    source, target = Path(source_root).resolve(strict=True), Path(destination).resolve(strict=True)
    if source == target or (source / 'catalog.sqlite3').samefile(target / 'catalog.sqlite3'):
        raise ValueError('Benchmark catalog must be independent of production')
    report = json.loads((target / MARKER).read_text(encoding='utf-8'))
    if (report.get('schema') != SCHEMA or report.get('source_root') != str(source)
            or report.get('destination') != str(target) or report.get('source_stamp') != _stamp(source)
            or report.get('source_flags') != source_flags(source)):
        raise RuntimeError('Prepared snapshot no longer matches the source generation/policy/flags')
    return report
