"""Purge releases persistent Windows readers and prevents late startup races."""
from contextlib import contextmanager
from pathlib import Path
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from advisor import library_search as search_module
from advisor import qmd_worker
from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.library_index_purge import purge_library_indexes
from advisor.library_search import _hash, _json
from advisor.process_runner import _ProcessTree
from advisor.server import make_server
from test_library_index_purge import only_library_fixture
from test_qmd_batch import sdk_fixture, query_request, events
from test_qmd_worker import observe_worker, wait_for, behavior, change_spec


@pytest.fixture(autouse=True)
def release_query_workers():
    yield
    qmd_worker.close_workers()


@pytest.mark.parametrize('active_query', [False, True])
def test_last_library_purge_stops_worker_and_blocks_queries_and_warmup_until_done(tmp_path, monkeypatch, active_query):
    root, _, _, _ = only_library_fixture(tmp_path, builds=1)
    search, trace = sdk_fixture(tmp_path)
    workers = observe_worker(monkeypatch)
    if active_query:
        behavior(search, 'if(spec.stall)await new Promise(()=>{})')
    startup_epoch = qmd_worker.worker_epoch(root)
    search.warmup_worker(startup_epoch=startup_epoch)
    worker = workers[-1]
    assert worker.pid and worker.cwd == root
    request = query_request(tmp_path, 'retained-reader')
    failures = []
    thread = None
    if active_query:
        change_spec(request, stall=True)
        def blocked_query():
            try:
                search._engine(request)
            except RuntimeError as exc:
                failures.append(str(exc))
        thread = threading.Thread(target=blocked_query)
        thread.start()
        wait_for(lambda: trace.exists() and '"keyword"' in trace.read_text(encoding='utf-8'))
    progress = []
    def while_purging(value):
        assert worker.pid is None
        existed = (root / 'runtime').exists()
        with pytest.raises(RuntimeError, match='maintenance'):
            search.warmup_worker(startup_epoch=startup_epoch)
        with pytest.raises(RuntimeError, match='maintenance'):
            search._engine(request)
        assert (root / 'runtime').exists() is existed
        progress.append(value)
    result = purge_library_indexes(root, 'deleted', generation='empty-library', on_progress=while_purging)
    if thread is not None:
        thread.join(timeout=3)
        assert not thread.is_alive() and failures
    assert result['empty_generations_removed'] == 1 and progress
    assert not (root / 'runtime').exists()
    assert not (root / 'purge-pending.json').exists()
    assert not (root / 'build-lease.json').exists()
    assert not any(key[0] == qmd_worker._root_key(root) for key in qmd_worker._WORKERS)
    # A startup job queued before purge stays invalid after the guard releases.
    with pytest.raises(RuntimeError, match='invalidated'):
        search.warmup_worker(startup_epoch=startup_epoch)
    assert not (root / 'runtime').exists()


def test_partial_purge_restarts_worker_and_keeps_real_retained_keyword_queries_usable(tmp_path, monkeypatch):
    from test_qmd_shared_query import public_fixture
    store, search, trace, objects = public_fixture(tmp_path)
    workers = observe_worker(monkeypatch)
    before = search.search('needle', mode='qmd_keyword', library_ids=['second'])
    assert before and all(hit['library_id'] == 'second' for hit in before)
    previous = workers[-1]
    previous_pid = previous.pid
    root = store.root / 'search'
    retained = sorted(item['id'] for item in objects if item['library_id'] != 'first')
    store.set_enabled('first', False)
    result = purge_library_indexes(root, 'first', generation=_hash(_json(retained))[:24])
    assert result['native_objects_removed'] == 2
    assert previous.pid is None and not (root / 'purge-pending.json').exists()
    after = search.search('needle', mode='qmd_keyword', library_ids=['first', 'second'])
    assert [hit['id'] for hit in after] == [hit['id'] for hit in before]
    assert workers[-1].pid != previous_pid
    assert search.search('needle', mode='qmd_keyword', library_ids=['second'])
    assert len({event['pid'] for event in events(trace)}) == 2


def test_cancelled_purge_releases_worker_maintenance_guard_and_query_can_restart(tmp_path, monkeypatch):
    from advisor import library_index_purge as purge_module
    root, _, _, _ = only_library_fixture(tmp_path, builds=1)
    search, _ = sdk_fixture(tmp_path)
    workers = observe_worker(monkeypatch)
    search.warmup_worker()
    previous = workers[-1]
    cancelled = threading.Event()
    suspend = purge_module.suspend_workers
    @contextmanager
    def cancelled_after_stopping(root):
        with suspend(root):
            cancelled.set()
            yield
    monkeypatch.setattr(purge_module, 'suspend_workers', cancelled_after_stopping)
    with build_control(BuildControl(tmp_path / 'cancel', cancelled)):
        with pytest.raises(BuildCancelled):
            purge_library_indexes(root, 'deleted', generation='cancelled')
    assert previous.pid is None
    assert not (root / 'build-lease.json').exists()
    assert not (root / 'purge-pending.json').exists()
    assert search._engine(query_request(tmp_path, 'retained-reader'))
    assert workers[-1] is not previous and workers[-1].pid


def test_delayed_workbench_startup_cannot_recreate_runtime_after_last_library_purge(tmp_path, monkeypatch):
    root, _, _, _ = only_library_fixture(tmp_path, builds=1)
    search, trace = sdk_fixture(tmp_path)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = search.warmup_worker
    def delayed_constructor(_root):
        started.set()
        assert release.wait(5)
        return search
    def observed_warmup(**kwargs):
        try:
            return original(**kwargs)
        finally:
            finished.set()
    search.warmup_worker = observed_warmup
    monkeypatch.setattr(search_module, 'LibrarySearch', delayed_constructor)
    server = make_server(SimpleNamespace(libraries=SimpleNamespace(root=root.parent)), 0)
    try:
        assert started.wait(3)
        purge_library_indexes(root, 'deleted', generation='empty-library')
        assert not (root / 'runtime').exists()
        release.set()
        assert finished.wait(3)
        assert not (root / 'runtime').exists() and not trace.exists()
    finally:
        release.set()
        server.server_close()


def test_other_cli_worker_does_not_pin_disposable_runtime_directory(tmp_path):
    root, _, _, _ = only_library_fixture(tmp_path, builds=1)
    search, trace = sdk_fixture(tmp_path)
    ready = tmp_path / 'cli-ready'
    script = tmp_path / 'other-cli.py'
    script.write_text("""import sys
from pathlib import Path
from advisor.library_search import LibrarySearch
search=LibrarySearch(Path(sys.argv[1]),qmd_dir=sys.argv[2],node=sys.argv[3],cache_dir=sys.argv[4])
environment=search._env
search._env=lambda:{**environment(),'QMD_BATCH_TRACE':sys.argv[5]}
search.warmup_worker()
Path(sys.argv[6]).write_text('ready',encoding='utf-8')
sys.stdin.read(1)
""", encoding='utf-8')
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    source_root = str(Path(search_module.__file__).resolve().parents[1])
    python_paths = [source_root, *([os.environ['PYTHONPATH']] if os.environ.get('PYTHONPATH') else [])]
    process = subprocess.Popen([sys.executable, str(script), str(root), str(search.qmd_dir), search.node,
                                str(search.cache_dir), str(trace), str(ready)], cwd=tmp_path,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', env=dict(os.environ, PYTHONPATH=os.pathsep.join(python_paths)), **kwargs)
    tree = _ProcessTree(process)
    try:
        wait_for(lambda: ready.exists() or process.poll() is not None)
        if process.poll() is not None:
            _, stderr = process.communicate(timeout=5)
            pytest.fail(f'Other CLI exited before worker startup ({process.returncode}): {stderr}')
        pid = events(trace)[0]['pid']
        assert search._pid_alive(pid)
        purge_library_indexes(root, 'deleted', generation='empty-library')
        assert not (root / 'runtime').exists() and not (root / 'purge-pending.json').exists()
        assert search._pid_alive(pid), 'The other CLI owns its process lifecycle'
        _, stderr = process.communicate(input='x', timeout=5)
        assert process.returncode == 0, stderr
        assert not search._pid_alive(pid), 'CLI atexit must reclaim its worker'
    finally:
        tree.close()
