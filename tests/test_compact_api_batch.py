"""A real fixture subprocess exercises the bounded disk queue and failure paths."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor import compact_api_batch as batch
from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.process_runner import run_text as actual_run_text


FIXTURE = r'''
import hashlib,json,os,pathlib,sys,time
command=json.loads(sys.argv[1]);start=command.index('ReadMphBatch')+1
timeout,manifest,*arguments=command[start:]
mode=sys.argv[2];cases=[]
if mode=='sleep':
    pathlib.Path(sys.argv[3]).write_text(str(os.getpid()))
    time.sleep(30)
for index in range(len(arguments)//4):
    source,state,error,export=map(pathlib.Path,arguments[index*4:index*4+4])
    if mode=='fail_middle' and index==1:
        error.write_text(json.dumps({'error_class':'FixtureReadError','reason':'deliberate per-case failure'}));continue
    data={'schema':'mph-api-state/v1','read_contract':'mph-api-read/v2','nodes':[],
        'geometry_run':False,'mesh_run':False,'solver_run':False,'defaults_generated':False,
        'read_errors':0,'read_coverage':{},'source_fingerprint':hashlib.sha256(source.read_bytes()).hexdigest()}
    if mode=='bad_contract' and index==0:data['mesh_run']=True
    state.write_text(json.dumps(data));export.write_text('temporary reconstruction')
    cases.append({'case_index':index,'status':'completed','remaining_model_tags':0})
    pathlib.Path(manifest).write_text(json.dumps({'schema':'mph-api-batch-runtime/v1','cases':cases}))
    if mode=='fatal':sys.exit(124)
'''


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path / 'libraries'
    source = tmp_path / 'sources'
    root.mkdir(); source.mkdir()
    authorities, paths = {}, []
    library = {'id': 'model-library', 'type': 'user-models', 'enabled': False, 'deleted_at': None,
               'roots': [{'id': 'main', 'path': str(source)}]}
    for index in range(3):
        path = source / f'case-{index}.mph'; path.write_bytes(('fixture-model-' + str(index)).encode())
        paths.append(path)
        authorities['authority-' + str(index)] = {'id': 'authority-' + str(index), 'object_id': 'object-' + str(index),
            'library_id': library['id'], 'asset_key': 'main:' + path.name, 'asset_hash': hashlib.sha256(path.read_bytes()).hexdigest()}
    store = SimpleNamespace(root=root, paths=SimpleNamespace(resolve=lambda value: Path(value).resolve()),
                            get=lambda cid: authorities.get(cid), library=lambda *args, **kwargs: library)
    directory = root / 'deep-api-batches'; directory.mkdir()
    checkpoint = directory / 'fixture.sqlite3'
    with closing(sqlite3.connect(checkpoint)) as db, db:
        db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE cases(object_id TEXT PRIMARY KEY,authority_id TEXT,library_id TEXT,state TEXT);')
        db.executemany('INSERT INTO meta VALUES(?,?)', [('schema', 'deep-api-batch/v1'), ('library_root', str(root))])
        db.executemany('INSERT INTO cases VALUES(?,?,?,?)', [('object-' + str(i), 'authority-' + str(i), library['id'], 'running' if i == 0 else 'pending') for i in range(3)])
    binary = tmp_path / 'runtime/java/win64/jre/bin'; binary.mkdir(parents=True)
    for name in ('java.exe', 'javac.exe'):
        (binary / name).write_bytes(b'fixture executable placeholder')
    script = tmp_path / 'fixture.py'; script.write_text(FIXTURE, encoding='utf-8')
    calls, mode = [], ['normal']
    marker = tmp_path / 'fixture-pid'
    def run(command, input_text, cwd, timeout, max_output):
        calls.append(command)
        if command[0].endswith('javac.exe'):
            return actual_run_text([sys.executable, '-c', 'print("fixture compile")'], input_text, cwd, timeout, max_output)
        engine = json.loads(command[3])
        return actual_run_text([sys.executable, str(script), json.dumps(engine), mode[0], str(marker)], input_text, cwd, timeout, max_output)
    monkeypatch.setattr(batch, '_comsol_root', lambda: tmp_path / 'runtime')
    monkeypatch.setattr(batch, 'run_text', run)
    runner = batch.BatchAPIRunner(store, checkpoint, batch_size=3, temp_root=tmp_path / 'temporary')
    def activate(index):
        with closing(sqlite3.connect(checkpoint)) as db, db:
            db.execute("UPDATE cases SET state='staged' WHERE state='running'")
            db.execute("UPDATE cases SET state='running' WHERE object_id=?", ('object-' + str(index),))
    def read(index):
        path = paths[index]
        return runner(path, tmp_path / 'caller-output', authorities['authority-' + str(index)]['asset_hash'])
    yield SimpleNamespace(runner=runner, paths=paths, calls=calls, mode=mode, activate=activate, read=read,
                          temporary=tmp_path / 'temporary', store=store, checkpoint=checkpoint, marker=marker,
                          authorities=authorities, library=library)
    runner.close()


def test_three_cases_use_one_subprocess_and_consume_distinct_disk_snapshots(fixture):
    first, assets, coverage = fixture.read(0)
    assert assets == [] and coverage['status'] == 'observed'
    assert first['source_fingerprint'] == hashlib.sha256(fixture.paths[0].read_bytes()).hexdigest()
    assert len(list(fixture.temporary.rglob('api-state.json'))) == 2
    assert not list(fixture.temporary.rglob('*.mph')) and not list(fixture.temporary.rglob('*.java'))
    for index in (1, 2):
        fixture.activate(index)
        value, _, _ = fixture.read(index)
        assert value['source_fingerprint'] == hashlib.sha256(fixture.paths[index].read_bytes()).hexdigest()
    assert fixture.runner.stats['jvm_processes'] == 1 and len(fixture.calls) == 2
    assert fixture.runner.stats['consumed'] == 3 and not list(fixture.temporary.iterdir())


def test_per_case_failure_does_not_hide_or_replace_the_next_case(fixture):
    fixture.mode[0] = 'fail_middle'
    assert fixture.read(0)[0] is not None
    fixture.activate(1)
    value, _, coverage = fixture.read(1)
    assert value is None and coverage['status'] == 'failed' and 'FixtureReadError' in coverage['reason']
    fixture.activate(2)
    assert fixture.read(2)[0]['source_fingerprint'] == fixture.authorities['authority-2']['asset_hash']
    assert fixture.runner.stats['jvm_processes'] == 1 and fixture.runner.stats['failed'] == 1


def test_jvm_failure_preserves_completed_case_and_reports_unattempted_cases(fixture):
    fixture.mode[0] = 'fatal'
    value, _, coverage = fixture.read(0)
    assert value is not None and coverage['batch_runtime']['returncode'] == 124
    for index in (1, 2):
        fixture.activate(index)
        value, _, coverage = fixture.read(index)
        assert value is None and coverage['status'] == 'failed' and 'did not complete' in coverage['reason']
    assert fixture.runner.stats['jvm_processes'] == 1


def test_changed_prefetched_source_is_rejected_without_stale_body(fixture):
    assert fixture.read(0)[0] is not None
    fixture.paths[1].write_bytes(b'changed after extraction')
    fixture.activate(1)
    value, _, coverage = fixture.read(1)
    assert value is None and 'Original MPH changed' in coverage['reason']
    assert len(list(fixture.temporary.rglob('api-state.json'))) == 1


def test_checkpoint_scope_rejects_foreign_source_before_launch(fixture, tmp_path):
    path = tmp_path / 'foreign.mph'; path.write_bytes(b'foreign')
    value, _, coverage = fixture.runner(path, tmp_path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert value is None and 'outside the current checkpoint' in coverage['reason']
    assert not fixture.calls and not fixture.temporary.exists()


def test_bad_contract_is_rejected_and_only_consumed_result_is_deleted(fixture):
    fixture.mode[0] = 'bad_contract'
    value, _, coverage = fixture.read(0)
    assert value is None and 'read contract' in coverage['reason']
    assert len(list(fixture.temporary.rglob('api-state.json'))) == 2
    fixture.runner.close()
    assert not list(fixture.temporary.iterdir())


def test_close_removes_unconsumed_results_and_refuses_reuse(fixture):
    fixture.read(0)
    fixture.runner.close()
    assert not list(fixture.temporary.iterdir())
    with pytest.raises(ValueError, match='closed'):
        fixture.read(1)


def test_cancellation_kills_fixture_process_and_discards_all_outputs(fixture, tmp_path):
    fixture.mode[0] = 'sleep'
    control = BuildControl(tmp_path / 'cancel')
    def cancel():
        for _ in range(200):
            if fixture.marker.exists():
                control.cancel_path.write_text('cancel');return
            time.sleep(.01)
    worker = threading.Thread(target=cancel); worker.start()
    started = time.monotonic()
    try:
        with build_control(control), pytest.raises(BuildCancelled):
            fixture.read(0)
    finally:
        worker.join(timeout=3)
    assert time.monotonic() - started < 8
    assert not list(fixture.temporary.iterdir())


def test_already_cancelled_next_call_discards_prefetched_snapshots(fixture, tmp_path):
    fixture.read(0)
    fixture.activate(1)
    control = BuildControl(tmp_path / 'cancel')
    control.cancel_path.write_text('cancel')
    with build_control(control), pytest.raises(BuildCancelled):
        fixture.read(1)
    assert not list(fixture.temporary.iterdir())


@pytest.mark.parametrize('batch_size', [0,1,2,4,6,True,'5'])
def test_only_bounded_three_or_five_case_sizes_are_accepted(fixture,batch_size):
    with pytest.raises(ValueError):
        batch.BatchAPIRunner(fixture.store, fixture.checkpoint, batch_size=batch_size)
