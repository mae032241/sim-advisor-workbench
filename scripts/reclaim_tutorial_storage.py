"""Offline physical reclamation after canonical tutorial normalization.

Model settings and active retrieval IDs stay exact. Old tutorial IDs become
explicit canonical aliases. No MPH parsing, QMD rebuilding or embedding runs.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import uuid

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from scripts.normalize_tutorials import ReadOnlyLibraryStore, _offline_build_lease, _write_lease, _release_file
from advisor.catalog_storage import register_catalog_functions
from advisor.compact_case import DATABASE
from advisor.compact_reclaim import ensure_storage_binding_schema, stage_compact_reclaim
from advisor.index_manifest import _owned
from advisor.index_manifest_cache import read_manifest
from advisor.index_paths import resolve_index_manifest
from advisor.native_reclaim import stage_native_reclaim
from advisor.tutorial_aliases import apply_relink
from advisor.tutorial_migration import TutorialMigrationPlan


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def connect(path, *, readonly=False):
    db = sqlite3.connect(Path(path).resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=30)
    register_catalog_functions(db)
    if readonly:
        db.execute('PRAGMA query_only=ON')
    return db


def backup(source, target):
    if target.exists():
        raise ValueError('Staging target already exists')
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(source, readonly=True)) as old, closing(sqlite3.connect(target)) as new:
        old.backup(new)


def active_fingerprint(db):
    digest, count = hashlib.sha256(), 0
    for cid,payload in db.execute('SELECT r.id,r.payload FROM active a JOIN revisions r ON r.id=a.citation_id ORDER BY r.id'):
        digest.update(cid.encode() + b'\0' + payload.encode() + b'\0')
        count += 1
    return {'count': count, 'sha256': digest.hexdigest()}


def model_references(store):
    references = {}
    with closing(store.connect()) as db:
        for (raw,) in db.execute("SELECT r.payload FROM case_members m JOIN revisions r ON r.id=m.citation_id WHERE m.local_id='model-case'"):
            obj = json.loads(raw)
            ref = obj.get('data', {}).get('compact_case')
            if not isinstance(ref, dict):
                continue
            relative = Path(ref['path'])
            path = _owned(store.root, store.root / relative)
            if relative.is_absolute() or not path.is_relative_to(store.root / 'artifacts') or path.name != DATABASE:
                raise ValueError('Invalid owned compact model reference')
            identity = relative.as_posix()
            prior = references.setdefault(identity, {'path': identity, 'sha256': ref['sha256'], 'case_ids': []})
            if prior['sha256'] != ref['sha256']:
                raise ValueError('Conflicting model storage references')
            prior['case_ids'].append(obj['id'])
    return references


def remove_stage(data, stage):
    stage = _owned(data, stage)
    if stage.parent != data or not re.fullmatch(r'\.tutorial-reclaim-[a-f0-9]{32}', stage.name):
        raise ValueError('Refusing to remove an unowned staging directory')
    for directory, dirs, files in os.walk(stage):
        for name in [*dirs, *files]:
            _owned(data, Path(directory) / name)
    shutil.rmtree(stage)


def reclaim(data_dir, plan_path, emit):
    store, plan = ReadOnlyLibraryStore(data_dir), TutorialMigrationPlan(plan_path)
    data, library = store.paths.root, store.root
    manifest = resolve_index_manifest(read_manifest(library / 'search/active.json'), library / 'search')
    if manifest.get('modes', {}).get('qmd_vector', {}).get('ready'):
        raise ValueError('This maintenance path requires embedding disabled')
    stage = data / ('.tutorial-reclaim-' + uuid.uuid4().hex)
    marker = library / '.catalog-migration.lock'
    moved, committed = [], False
    before_bytes = sum(p.stat().st_size for p in data.rglob('*') if p.is_file())
    with _offline_build_lease(store) as token:
        stage.mkdir()
        try:
            references = model_references(store)
            model_results, model_moves = [], []
            with closing(store.connect()) as db:
                expected_active = active_fingerprint(db)
            qmd_before = {key: file_hash(item['db_path']) for key,item in manifest['libraries'].items()}
            for number, ref in enumerate(references.values(), 1):
                source = _owned(library, library / ref['path'])
                if file_hash(source) != ref['sha256']:
                    raise ValueError('Model storage changed or was already rebound: ' + ref['path'])
                with closing(connect(source, readonly=True)) as db:
                    has_tutorial = any(db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone()
                                       for table in ('documents','passages','text_pool'))
                if has_tutorial:
                    target = stage / 'models' / ref['path']
                    target.parent.mkdir(parents=True, exist_ok=True)
                    result = stage_compact_reclaim(source, target)
                    result['relative_path'] = ref['path']
                    model_results.append(result)
                    model_moves.append((source, target))
                if number % 50 == 0 or number == len(references):
                    emit({'phase':'staging_models','completed':number,'total':len(references),'changed':len(model_results)})

            native = _owned(library, Path(manifest['directory']) / 'objects.sqlite3')
            native_stage = stage / 'native.sqlite3'
            emit({'phase':'reclaiming_native'})
            native_result = stage_native_reclaim(native, native_stage)
            catalog_stage = stage / 'catalog.sqlite3'
            backup(store.db_path, catalog_stage)
            emit({'phase':'relinking_catalog'})
            with closing(connect(catalog_stage)) as db:
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('BEGIN IMMEDIATE')
                alias_result = apply_relink(db, plan, source_catalog=store.db_path)
                ensure_storage_binding_schema(db)
                db.executemany('INSERT OR REPLACE INTO compact_storage_bindings VALUES(?,?,?)',
                               ((r['relative_path'],r['original_sha256'],r['current_sha256']) for r in model_results))
                if active_fingerprint(db) != expected_active:
                    raise ValueError('Active model/manual payloads changed during relinking')
                db.commit()
                db.execute('VACUUM')
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Reclaimed catalog failed integrity check')
            moves = [*model_moves, (native,native_stage), (store.db_path,catalog_stage)]
            _write_lease(marker, {'pid':os.getpid(),'token':token,'kind':'tutorial-physical-reclaim','stage':str(stage)})
            # One recovery manifest lists every original and backup path before
            # any replacement. A stopped process leaves this owned stage.
            journal = [{'original':str(old),'staged':str(new),
                        'backup':str(stage / 'backups' / old.relative_to(library))} for old,new in moves]
            (stage / 'recovery.json').write_text(json.dumps(journal),encoding='utf-8')
            # All actual writers must be stopped; refuse nonempty sidecars.
            for old,new in moves:
                for suffix in ('-wal','-journal'):
                    sidecar = old.with_name(old.name + suffix)
                    if sidecar.exists() and sidecar.stat().st_size:
                        raise RuntimeError('Live SQLite transaction sidecar: ' + str(sidecar))
            emit({'phase':'publishing','models':len(model_moves)})
            for entry in journal:
                old,new,previous = (Path(entry[key]) for key in ('original','staged','backup'))
                previous.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old,previous)
                moved.append((old,previous))
                os.replace(new,old)
            # Reopen the real catalog and use real public source/config APIs.
            with closing(store.connect()) as db:
                if active_fingerprint(db) != expected_active:
                    raise ValueError('Published active payload fingerprint changed')
            sample_alias = next(plan.iter_retirements())['id']
            if store.source_available(sample_alias):
                assert store.read(sample_alias).get('alias', {}).get('requested_id') == sample_alias
            else:
                try:
                    store.read(sample_alias)
                except PermissionError:
                    pass
                else:
                    raise ValueError('Closed tutorial owner allowed a new alias read')
            visible, samples = set(store.searchable_library_ids()), 0
            for ref in references.values():
                if store.get(ref['case_ids'][0])['library_id'] not in visible:
                    continue
                value = store.configuration(ref['case_ids'][0], source='xml', limit=1)
                assert value['source'] == 'xml'
                samples += 1
                if samples == 3:
                    break
            if qmd_before != {key:file_hash(item['db_path']) for key,item in manifest['libraries'].items()}:
                raise ValueError('QMD files changed during physical reclamation')
            committed = True
            _release_file(marker,token)
            model_savings = sum(r['reclaimed_bytes'] for r in model_results)
            result = {'status':'completed','model_databases':len(references),'rebound_models':len(model_results),
                      'model_reclaimed_bytes':model_savings,'native':native_result,'aliases':alias_result,
                      'active_payloads':expected_active,'qmd_files_unchanged':True,'embedding':False,
                      'model_semantic_proofs':model_results}
            remove_stage(data,stage)
            result.update(before_bytes=before_bytes,after_bytes=sum(p.stat().st_size for p in data.rglob('*') if p.is_file()))
            result['saved_bytes'] = result['before_bytes']-result['after_bytes']
            return result
        except BaseException:
            if not committed:
                for original,previous in reversed(moved):
                    if previous.exists():
                        os.replace(previous,original)
                _release_file(marker,token)
                if stage.exists():
                    remove_stage(data,stage)
            raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--apply',action='store_true',help='All workbench writers must be stopped')
    args=parser.parse_args()
    if not args.apply:
        parser.error('--apply is required for physical reclamation')
    def emit(value):
        print(json.dumps(value,ensure_ascii=False),flush=True)
    result=reclaim(args.data_dir,args.plan,emit)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    emit({key:value for key,value in result.items() if key!='model_semantic_proofs'})


if __name__=='__main__':
    main()
