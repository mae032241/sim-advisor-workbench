"""Interrupt real owned work without stopping the catalog or unrelated work."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

import pytest

from advisor.build_control import BuildCancelled
from advisor.library_store import LibraryStore
from advisor.process_runner import run_text


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError('Timed out waiting for owned test worker')


def fixture(tmp_path):
    source = tmp_path / 'sources'
    source.mkdir()
    (source / 'a.txt').write_text('Original mesh quality reference.', encoding='utf-8')
    store = LibraryStore(tmp_path / 'libraries')
    library = store.register('cancel fixture', 'official-manuals', [{'id': 'main', 'path': str(source)}])
    library = store.set_enabled(library["id"], True)
    assert store.build(library['id'], modes=['native'])['status'] == 'completed'
    return store, library, source


def slow_extractor(monkeypatch, source):
    from advisor import library_text
    original = library_text.extract_text_objects
    ready = source.parent / 'child-ready'

    def extract(path, **kwargs):
        if Path(path).name == 'b.txt':
            run_text([sys.executable, '-c', 'import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text("ready");time.sleep(60)', str(ready)],
                     '', source, 70)
        return original(path, **kwargs)

    monkeypatch.setattr(library_text, 'extract_text_objects', extract)
    (source / 'a.txt').write_text('New source revision, not yet committed.', encoding='utf-8')
    (source / 'b.txt').write_text('Slow extraction fixture.', encoding='utf-8')
    return ready


def test_cancel_from_another_store_stops_child_and_preserves_previous_catalog(tmp_path, monkeypatch):
    store, library, source = fixture(tmp_path)
    old_objects = store.objects()
    active = store.root / 'search/active.json'
    old_index = active.read_bytes()
    ready = slow_extractor(monkeypatch, source)
    job = store.start_build(library['id'], modes=['native'])
    wait_for(ready.exists)
    another = LibraryStore(store.root)
    started = time.monotonic()
    requested = another.cancel_build(job['id'])
    assert requested['status'] == 'cancel_requested'
    ended = wait_for(lambda: (j if (j := store.job(job['id']))['status'] == 'cancelled' else None))
    assert time.monotonic() - started < 5
    assert ended['cancelled_stage'] == 'extracting'
    assert ended['counts']['succeeded'] == 1 and not ended['failed']
    assert not ended['cancellable']
    assert store.objects() == old_objects and active.read_bytes() == old_index
    assert not (store.root / '.build.lock').exists()
    assert another.cancel_build(job['id'])['status'] == 'cancelled'
    # A new task has a different marker and can immediately complete.
    (source / 'b.txt').unlink()
    assert store.build(library['id'], modes=['native'])['status'] == 'completed'


def test_queued_cancel_prevents_any_extraction_and_terminal_cancel_is_noop(tmp_path, monkeypatch):
    store, library, source = fixture(tmp_path)
    terminal = store.jobs()[0]
    assert store.cancel_build(terminal['id'])['status'] == 'completed'
    assert not store._cancel_path(terminal['id']).exists()
    job_id = uuid.uuid4().hex
    store._save_job({'id': job_id, 'library_id': library['id'], 'pid': os.getpid(), 'status': 'queued',
                    'cancel_supported': True, 'items': [], 'failed': []})
    store.cancel_build(job_id)
    monkeypatch.setattr(store, 'scan', lambda *a, **kw: pytest.fail('Cancelled queue must not start scanning'))
    result = store.build(library['id'], job_id=job_id, modes=[])
    assert result['status'] == 'cancelled'
    assert store._cancel_path(job_id).exists()
    with pytest.raises(ValueError):
        store.cancel_build('../foreign')
    with pytest.raises(FileNotFoundError):
        store.cancel_build('f' * 32)


def test_progress_and_completion_cannot_overwrite_acknowledged_cancel(tmp_path):
    store = LibraryStore(tmp_path / 'catalog')
    job = {'id': uuid.uuid4().hex, 'library_id': 'test', 'pid': os.getpid(), 'status': 'running',
           'stage': 'extracting', 'cancel_supported': True, 'items': [], 'failed': []}
    store._save_job(job)
    first = store.cancel_build(job['id'])
    store._save_job(dict(job, completed=1))
    assert store.job(job['id'])['status'] == 'cancel_requested'
    store._save_job(dict(job, status='completed', stage='finished'))
    result = store.job(job['id'])
    assert result['status'] == 'cancelled'
    assert result['cancel_requested_at'] == first['cancel_requested_at']


def test_reindex_is_visible_and_cancellable_across_real_cli_processes(tmp_path):
    store, library, source = fixture(tmp_path)
    script = tmp_path / 'index_worker.py'
    ready = tmp_path / 'index-child-ready'
    script.write_text('''import sys
from pathlib import Path
from advisor.library_store import LibraryStore
from advisor.library_search import LibrarySearch
from advisor.process_runner import run_text
def slow(self, *args, **kwargs):
    run_text([sys.executable, '-c', 'import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text("ready");time.sleep(60)', sys.argv[2]], '', Path(sys.argv[1]), 70)
LibrarySearch._engine=slow
LibrarySearch._build_keyword=slow
result=LibraryStore(Path(sys.argv[1])).reindex(['native','qmd_keyword'])
import json
print(json.dumps(result), flush=True)
''', encoding='utf-8')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONUTF8='1')
    process = subprocess.Popen([sys.executable, str(script), str(store.root), str(ready)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    try:
        wait_for(ready.exists)
        job = next(j for j in store.jobs() if j.get('kind') == 'reindex')
        assert job['scope'] == 'all_libraries' and job['library_ids'] == [library['id']]
        assert store.cancel_build(job['id'])['status'] == 'cancel_requested'
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, stderr
        result = json.loads(stdout)
        assert result['status'] == 'cancelled' and result['job_id'] == job['id']
        assert store.job(job['id'])['status'] == 'cancelled'
        assert store.index_status()['modes']['native']['ready']
        assert not (store.root / '.build.lock').exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_http_cancel_keeps_other_sessions_and_server_available(tmp_path, monkeypatch):
    from advisor.server import App, make_server
    app = App(tmp_path / 'data', doc_root=tmp_path, runtime_factory='none')
    source = tmp_path / 'sources'
    source.mkdir()
    (source / 'a.txt').write_text('Mesh reference.', encoding='utf-8')
    library = app.libraries.register('HTTP fixture', 'official-manuals', [{'path': str(source)}])
    library = app.libraries.set_enabled(library["id"], True)
    ready = slow_extractor(monkeypatch, source)
    session = app.sessions.create('Preserve unrelated simulation task during cancellation', None)
    before = app.sessions.path(session['id']).read_bytes()
    server = make_server(app, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    def request(path, body=None):
        req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={'Content-Type': 'application/json'})
        with urlopen(req, timeout=8) as response:
            return json.load(response)
    try:
        job = request(f'/api/libraries/{library["id"]}/build', {'modes': ['native']})
        wait_for(ready.exists)
        result = request(f'/api/library-jobs/{job["id"]}/cancel', {})
        assert result['status'] in {'cancel_requested', 'cancelled'}
        wait_for(lambda: request(f'/api/library-jobs/{job["id"]}')['status'] == 'cancelled')
        assert request(f'/api/sessions/{session["id"]}')['id'] == session['id']
        assert app.sessions.path(session['id']).read_bytes() == before
        assert request('/api/libraries')['libraries'][0]['id'] == library['id']
        assert request(f'/api/library-jobs/{job["id"]}/cancel', {})['status'] == 'cancelled'
        with pytest.raises(HTTPError) as caught:
            request('/api/library-jobs/' + 'f' * 32 + '/cancel', {})
        assert caught.value.code == 404
    finally:
        server.shutdown()
        app.agents.close()
        server.server_close()


def test_enrichment_never_converts_cancellation_into_a_failed_annotation(tmp_path):
    from advisor.library_enrichment import enrich_case
    class Backend:
        model = engine = 'fixture'
        def generate(self, *args, **kwargs):
            raise BuildCancelled()
    with pytest.raises(BuildCancelled):
        enrich_case({'objects': []}, tmp_path, Backend())
