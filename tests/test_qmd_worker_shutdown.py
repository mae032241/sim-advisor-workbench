"""Shutdown gates late handlers; only starting a new workbench reopens QMD."""
import threading
from types import SimpleNamespace

import pytest

from advisor import library_search as search_module
from advisor import qmd_worker, server as server_module
from test_qmd_batch import sdk_fixture, query_request, events
from test_qmd_worker import observe_worker, wait_for


@pytest.fixture(autouse=True)
def release_query_workers():
    yield
    qmd_worker.close_workers()


@pytest.mark.parametrize('join_handler', [False, True])
def test_server_close_blocks_late_handlers_and_warmup_until_new_server(tmp_path, monkeypatch, join_handler):
    search, trace = sdk_fixture(tmp_path)
    workers = observe_worker(monkeypatch)
    monkeypatch.setattr(search_module, 'LibrarySearch', lambda root: search)
    release = threading.Event()
    failures = []
    pending = None
    class ServerWithoutSockets:
        def __init__(self, *_args):
            pass
        def server_close(self):
            # Exercise both ThreadingHTTPServer's join window and daemon
            # handlers continuing after server_close has already returned.
            if join_handler and pending is not None:
                release.set()
                pending.join(timeout=3)
    monkeypatch.setattr(server_module, 'ThreadingHTTPServer', ServerWithoutSockets)
    app = SimpleNamespace(libraries=SimpleNamespace(root=search.data_dir.parent))
    server = server_module.make_server(app, 0)
    wait_for(lambda: trace.exists() and events(trace))
    first = workers[-1]
    first_pid = first.pid
    old_epoch = server._qmd_epoch
    request = query_request(tmp_path, 'first')
    def late_handler():
        assert release.wait(5)
        try:
            search._engine(request)
        except RuntimeError as exc:
            failures.append(str(exc))
    pending = threading.Thread(target=late_handler)
    pending.start()
    try:
        server.server_close()
        release.set()
        pending.join(timeout=3)
        assert not pending.is_alive() and failures and 'closed' in failures[0]
        assert first.pid is None
        with pytest.raises(RuntimeError, match='closed'):
            search.warmup_worker(startup_epoch=old_epoch)
        with pytest.raises(RuntimeError, match='closed'):
            search._engine(request)
        assert not any(key[0] == qmd_worker._root_key(search.data_dir) for key in qmd_worker._WORKERS)
        replacement = server_module.make_server(app, 0)
        try:
            assert search._engine(request)
            assert workers[-1].pid and workers[-1].pid != first_pid
            # Repeating close on the old server cannot disable its successor.
            server.server_close()
            assert search._engine(request)
        finally:
            replacement.server_close()
        assert workers[-1].pid is None
    finally:
        release.set()
        pending.join(timeout=3)
        server.server_close()


def test_standalone_cli_root_stays_lazy_without_a_workbench_lifecycle(tmp_path):
    search, trace = sdk_fixture(tmp_path)
    assert not trace.exists()
    assert search._engine(query_request(tmp_path, 'first'))
    qmd_worker.close_workers(search.data_dir)
    assert search._engine(query_request(tmp_path, 'second'))
    assert len({event['pid'] for event in events(trace)}) == 2
