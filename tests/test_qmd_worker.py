"""Persistent QMD queries reuse code while releasing every database handle."""
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from advisor import library_search as module
from advisor import qmd_worker
from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.library_search import LibrarySearch
from advisor.process_runner import ProcessTimeoutError
from advisor.server import make_server
from test_qmd_batch import sdk_fixture, query_request, events


@pytest.fixture(autouse=True)
def release_query_workers():
    yield
    qmd_worker.close_workers()


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("QMD worker did not reach the expected state")


def observe_worker(monkeypatch):
    workers = []
    original = module.get_worker
    def observe(*args, **kwargs):
        worker = original(*args, **kwargs)
        workers.append(worker)
        return worker
    monkeypatch.setattr(module, "get_worker", observe)
    return workers


def fresh(search):
    other = LibrarySearch(search.data_dir, qmd_dir=search.qmd_dir, node=search.node,
                          cache_dir=search.cache_dir)
    other._env = search._env
    return other


def behavior(search, source):
    # SQL statement callbacks are synchronous; block the fixture worker there.
    source = source.replace("await new Promise(()=>{})", "Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0)")
    sdk = search.qmd_dir / "dist/index.js"
    sdk.write_text(sdk.read_text(encoding="utf-8").replace(
        "if(spec.fail)throw new Error('injected library failure');", source +
        ";if(spec.fail)throw new Error('injected library failure');"), encoding="utf-8")


def change_spec(request, **values):
    path = Path(request["db_path"])
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec.update(values)
    path.write_text(json.dumps(spec), encoding="utf-8")


def test_warmup_and_fresh_searches_reuse_one_pid_but_reopen_each_database(tmp_path):
    search, trace = sdk_fixture(tmp_path)
    assert search.warmup_worker() == {"ready": True}
    assert [event["event"] for event in events(trace)] == ["import"]
    requests = [query_request(tmp_path, name) for name in ("first", "second")]
    assert search._engine(requests[0])[0]["filepath"].endswith("first.md")
    assert fresh(search)._engine(requests[1])[0]["filepath"].endswith("second.md")
    # Replacing the database changes the next response without changing PID.
    replacement = Path(requests[0]["db_path"]).with_suffix(".replacement")
    replacement.write_text(json.dumps({"hits": [{"filepath": "qmd://objects/replaced.md", "score": 1}]}), encoding="utf-8")
    replacement.replace(requests[0]["db_path"])
    assert fresh(search)._engine(requests[0])[0]["filepath"].endswith("replaced.md")
    Path(requests[0]["db_path"]).unlink()
    recorded = events(trace)
    assert len({event["pid"] for event in recorded}) == 1
    assert sum(event["event"] == "import" for event in recorded) == 1
    assert sum(event["event"] == "open" for event in recorded) == 3
    assert sum(event["event"] == "close" for event in recorded) == 3


def test_tool_error_keeps_worker_usable_and_crash_restarts_on_next_request(tmp_path):
    search, trace = sdk_fixture(tmp_path)
    behavior(search, "if(spec.crash)process.exit(41)")
    request = query_request(tmp_path, "first", fail=True)
    with pytest.raises(RuntimeError, match="injected library failure"):
        search._engine(request)
    first_pid = events(trace)[0]["pid"]
    change_spec(request, fail=False)
    assert search._engine(request)
    assert {event["pid"] for event in events(trace)} == {first_pid}
    change_spec(request, crash=True)
    with pytest.raises(RuntimeError, match="exited"):
        search._engine(request)
    change_spec(request, crash=False)
    assert search._engine(request)
    assert len({event["pid"] for event in events(trace)}) == 2


def test_cancelling_an_inflight_query_kills_only_worker_and_next_query_restarts(tmp_path, monkeypatch):
    search, trace = sdk_fixture(tmp_path)
    behavior(search, "if(spec.stall)await new Promise(()=>{})")
    request = query_request(tmp_path, "first")
    change_spec(request, stall=True)
    workers = observe_worker(monkeypatch)
    cancelled = threading.Event()
    monitor = threading.Thread(target=lambda: (wait_for(lambda: trace.exists() and '"keyword"' in trace.read_text(encoding="utf-8")), cancelled.set()), daemon=True)
    monitor.start()
    with build_control(BuildControl(tmp_path / "cancel-marker", cancelled)):
        with pytest.raises(BuildCancelled):
            search._engine(request)
    monitor.join(timeout=2)
    assert workers[0].pid is None
    change_spec(request, stall=False)
    assert search._engine(request)
    assert len({event["pid"] for event in events(trace)}) == 2


def test_timeout_has_no_stale_reply_and_shutdown_interrupts_active_request(tmp_path, monkeypatch):
    search, trace = sdk_fixture(tmp_path)
    behavior(search, "if(spec.stall)await new Promise(()=>{})")
    workers = observe_worker(monkeypatch)
    request = query_request(tmp_path, "first")
    search._engine(request)
    worker = workers[0]
    payload = {"qmd_dir": str(search.qmd_dir), "env": search._env(), "embedding_enabled": False, **request}
    change_spec(request, stall=True)
    with pytest.raises(ProcessTimeoutError):
        worker.request(payload, timeout=0.1)
    assert worker.pid is None
    failures = []
    def blocked():
        try:
            worker.request(payload, timeout=10)
        except RuntimeError as exc:
            failures.append(str(exc))
    thread = threading.Thread(target=blocked)
    thread.start()
    wait_for(lambda: len([event for event in events(trace) if event["event"] == "keyword"]) >= 3)
    qmd_worker.close_workers(search.data_dir)
    thread.join(timeout=3)
    assert not thread.is_alive() and failures
    assert worker.pid is None
    change_spec(request, stall=False)
    assert fresh(search)._engine(request)
    assert workers[-1] is not worker


def test_different_runtime_configuration_retires_previous_worker(tmp_path, monkeypatch):
    search, trace = sdk_fixture(tmp_path)
    workers = observe_worker(monkeypatch)
    request = query_request(tmp_path, "first")
    search._engine(request)
    other = fresh(search)
    second_trace = tmp_path / "other-trace.jsonl"
    other._env = lambda: {**search._env(), "QMD_BATCH_TRACE": str(second_trace)}
    assert other._engine(request)
    assert events(trace)[0]["pid"] != events(second_trace)[0]["pid"]
    assert workers[0].pid is None
    # Reverting configuration starts a fresh process with its original runtime.
    assert search._engine(request)
    assert len({event["pid"] for event in events(trace)}) == 2
    assert workers[1].pid is None


def test_stderr_is_bounded_without_breaking_protocol(tmp_path, monkeypatch):
    search, _ = sdk_fixture(tmp_path)
    behavior(search, "process.stderr.write('diagnostic '.repeat(10000))")
    workers = observe_worker(monkeypatch)
    assert search._engine(query_request(tmp_path, "first"))
    run = workers[0]._run
    wait_for(lambda: len(run.stderr) == qmd_worker._STDERR_TAIL)
    assert len(run.stderr) == qmd_worker._STDERR_TAIL


def test_workbench_starts_keyword_worker_and_server_close_releases_it(tmp_path, monkeypatch):
    search, trace = sdk_fixture(tmp_path)
    workers = observe_worker(monkeypatch)
    monkeypatch.setattr(module, "LibrarySearch", lambda root: search)
    server = make_server(SimpleNamespace(libraries=SimpleNamespace(root=search.data_dir.parent)), 0)
    try:
        wait_for(lambda: trace.exists() and events(trace))
        assert [event["event"] for event in events(trace)] == ["import"]
        assert search._engine(query_request(tmp_path, "first"))
        assert len({event["pid"] for event in events(trace)}) == 1
        worker = workers[-1]
        assert worker.pid
    finally:
        server.server_close()
    assert worker.pid is None


def test_stopped_startup_warmup_cannot_create_a_late_worker(tmp_path):
    search, trace = sdk_fixture(tmp_path)
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(RuntimeError, match="stopped"):
        search.warmup_worker(stop_event=stopped)
    assert not trace.exists()


def test_disabled_embedding_warmup_cannot_restore_model_uri_or_open_store(tmp_path):
    search, trace = sdk_fixture(tmp_path)
    sdk = search.qmd_dir / 'dist/index.js'
    sdk.write_text("globalThis.fetch=()=>{throw new Error('Network forbidden');};\n"
                   "if(process.env.QMD_EMBED_MODEL)throw new Error('Disabled embedding URI leaked into warmup');\n"
                   + sdk.read_text(encoding='utf-8'), encoding='utf-8')
    search.model_uri = 'hf:fixture/must-never-load.gguf'
    assert search.warmup_worker() == {'ready': True}
    assert [event['event'] for event in events(trace)] == ['import']


def test_real_sdk_reuses_process_but_reopens_replaced_index_and_current_library_scopes(tmp_path):
    from test_qmd_shared_query import runtime, make_database, search_runtime, shared_request, collection, public_fixture
    probe, wrapper, trace = runtime(tmp_path)
    rows = [{"collection": collection("first"), "path": "original.md", "title": "One", "body": "needle"}]
    database = make_database(tmp_path, probe, rows)
    search = search_runtime(tmp_path, wrapper, trace, probe.node)
    assert search._engine_shared_keyword(shared_request(database, ["first"], "needle"))[0]["filepath"].endswith("original.md")
    replacement = make_database(tmp_path, probe, [{**rows[0], "path": "replacement.md"}], tmp_path / "replacement.sqlite3")
    replacement.replace(database)
    assert search._engine_shared_keyword(shared_request(database, ["first"], "needle"))[0]["filepath"].endswith("replacement.md")
    database.unlink()  # No worker-held SQLite handles prevent deletion on Windows.
    assert len({event["pid"] for event in events(trace)}) == 1

    scoped = tmp_path / "scoped"
    scoped.mkdir()
    store, search, trace, _ = public_fixture(scoped)
    assert {hit["library_id"] for hit in search.search("needle", mode="qmd_keyword", library_ids=["first", "second"])} == {"first", "second"}
    store.set_enabled("first", False)
    assert {hit["library_id"] for hit in search.search("needle", mode="qmd_keyword", library_ids=["first", "second"])} == {"second"}
    store.set_enabled("first", True)
    assert {hit["library_id"] for hit in search.search("needle", mode="qmd_keyword", library_ids=["first"])} == {"first"}
    assert len({event["pid"] for event in events(trace)}) == 1
