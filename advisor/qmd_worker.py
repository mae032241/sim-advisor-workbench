"""One local JSONL QMD process per data root and runtime configuration.

Only SDK code is retained between requests. The bridge opens/closes databases
inside each request, so permissions, exclusions and generation swaps stay fresh.
"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

from .build_control import check_cancelled
from .process_runner import _ProcessTree, ProcessOutputLimitError, ProcessTimeoutError

_MARKER = b"LIBRARY_SEARCH_RESPONSE="
_MAX_OUTPUT = 4 * 1024 * 1024
_STDERR_TAIL = 32 * 1024
_WORKERS = {}
_WORKERS_LOCK = threading.Lock()
_ROOT_GATES = {}
_MAINTENANCE = set()
_EPOCHS = {}
_CLOSED_WORKBENCHES = set()


def _root_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _root_gate(root):
    with _WORKERS_LOCK:
        return _ROOT_GATES.setdefault(root, threading.RLock())


def worker_epoch(data_root):
    """Capture startup's identity before dispatching its optional warmup."""
    with _WORKERS_LOCK:
        return _EPOCHS.get(_root_key(data_root), 0)


@contextmanager
def worker_preparation(data_root, *, stop_event=None, expected_epoch=None):
    """Protect script creation/registration, without holding a lock during SQL."""
    root = _root_key(data_root)
    with _root_gate(root):
        with _WORKERS_LOCK:
            if root in _CLOSED_WORKBENCHES:
                raise RuntimeError("QMD workbench has been closed")
            if root in _MAINTENANCE:
                raise RuntimeError("QMD index maintenance is active; retry the query")
            if expected_epoch is not None and expected_epoch != _EPOCHS.get(root, 0):
                raise RuntimeError("QMD startup warmup was invalidated by index maintenance")
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("QMD warmup has been stopped")
        check_cancelled()
        yield


def start_workbench_workers(data_root):
    """An explicit new server reopens its root after an earlier shutdown."""
    root = _root_key(data_root)
    with _root_gate(root):
        with _WORKERS_LOCK:
            _CLOSED_WORKBENCHES.discard(root)
            return _EPOCHS.get(root, 0)


def stop_workbench_workers(data_root):
    """Close the root before joining handlers, including handlers not joined."""
    root = _root_key(data_root)
    with _root_gate(root):
        with _WORKERS_LOCK:
            _CLOSED_WORKBENCHES.add(root)
            _EPOCHS[root] = _EPOCHS.get(root, 0) + 1
        close_workers(data_root)


@contextmanager
def suspend_workers(data_root):
    """Under the caller's purge lease, stop readers and block late startups."""
    root = _root_key(data_root)
    gate = _root_gate(root)
    with gate:
        with _WORKERS_LOCK:
            if root in _MAINTENANCE:
                raise RuntimeError("QMD index maintenance is already active")
            _MAINTENANCE.add(root)
            _EPOCHS[root] = _EPOCHS.get(root, 0) + 1
        try:
            close_workers(data_root)
        except BaseException:
            with _WORKERS_LOCK:
                _MAINTENANCE.remove(root)
            raise
    try:
        yield
    finally:
        with gate:
            with _WORKERS_LOCK:
                _MAINTENANCE.remove(root)


def _detail(raw):
    value = raw.decode("utf-8", errors="replace")[-1800:]
    return re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token)[=:]\s*)\S+", r"\1[redacted]", value)


class _Run:
    def __init__(self, process):
        self.process = process
        self.tree = _ProcessTree(process)
        self.responses = queue.Queue(maxsize=2)
        self.wake = threading.Event()
        self.failed = None
        self.stderr = bytearray()
        self.stderr_lock = threading.Lock()
        self.threads = []

    def read_stdout(self):
        pending = bytearray()
        try:
            while chunk := self.process.stdout.read(65536):
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    if len(line) > _MAX_OUTPUT:
                        raise ProcessOutputLimitError(_MAX_OUTPUT)
                    if line.startswith(_MARKER):
                        response = json.loads(line[len(_MARKER):])
                        self.responses.put_nowait(response)
                        self.wake.set()
                if len(pending) > _MAX_OUTPUT:
                    raise ProcessOutputLimitError(_MAX_OUTPUT)
        except (OSError, ValueError, queue.Full, ProcessOutputLimitError) as exc:
            self.failed = exc
        finally:
            self.process.stdout.close()
            self.wake.set()

    def read_stderr(self):
        try:
            while chunk := self.process.stderr.read(65536):
                with self.stderr_lock:
                    self.stderr.extend(chunk)
                    del self.stderr[:-_STDERR_TAIL]
        except OSError:
            pass
        finally:
            self.process.stderr.close()

    def close(self):
        self.tree.close()
        try:
            self.process.stdin.close()
        except OSError:
            pass
        self.wake.set()
        for thread in self.threads:
            if thread.ident is not None and thread is not threading.current_thread():
                thread.join(timeout=0.5)


class QmdWorker:
    """Serialized request/response transport; a failed process restarts lazily."""

    def __init__(self, node, script, cwd):
        self.command = [str(node), str(script), "--worker"]
        self.cwd = Path(cwd)
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._run = None
        self._closed = False
        self._sequence = 0

    @property
    def pid(self):
        with self._state_lock:
            run = self._run
            return run.process.pid if run is not None and run.process.poll() is None else None

    def _start(self):
        with self._state_lock:
            if self._closed:
                raise RuntimeError("QMD worker has been closed")
            if self._run is None:
                check_cancelled()
                kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
                process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, cwd=self.cwd, shell=False, bufsize=0,
                                           env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"), **kwargs)
                run = self._run = _Run(process)
                run.threads = [threading.Thread(target=reader, daemon=True, name="qmd-worker-pipe")
                               for reader in (run.read_stdout, run.read_stderr)]
                for thread in run.threads:
                    thread.start()
            return self._run

    def _discard(self, run):
        with self._state_lock:
            if self._run is run:
                self._run = None
            else:
                return
        run.close()

    def request(self, payload, *, timeout=120):
        check_cancelled()
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
            raise ValueError("timeout must be a finite positive number")
        deadline = time.monotonic() + timeout
        # Waiting for another query is cancellable and shares this deadline.
        while not self._request_lock.acquire(timeout=min(0.05, max(0, deadline - time.monotonic()))):
            check_cancelled()
            if time.monotonic() >= deadline:
                raise ProcessTimeoutError(self.command, timeout)
        run = None
        try:
            check_cancelled()
            with self._state_lock:
                previous = self._run
            if previous is not None and (previous.process.poll() is not None or previous.failed is not None):
                self._discard(previous)
            run = self._start()
            self._sequence += 1
            request_id = self._sequence
            encoded = (json.dumps({"id": request_id, "request": payload}, ensure_ascii=False, allow_nan=False,
                                  separators=(",", ":")) + "\n").encode("utf-8")
            with run.stderr_lock:
                run.stderr.clear()
            write_error = []

            def write():
                try:
                    remaining = memoryview(encoded)
                    while remaining:
                        count = run.process.stdin.write(remaining[:65536])
                        if not count:
                            raise BrokenPipeError("QMD worker input closed")
                        remaining = remaining[count:]
                except (OSError, ValueError) as exc:
                    write_error.append(exc)
                finally:
                    run.wake.set()

            writer = threading.Thread(target=write, daemon=True, name="qmd-worker-input")
            writer.start()
            while True:
                check_cancelled()
                if self._closed:
                    raise RuntimeError("QMD worker has been closed")
                if run.failed is not None:
                    raise RuntimeError("QMD worker returned an invalid or oversized response") from run.failed
                try:
                    response = run.responses.get_nowait()
                except queue.Empty:
                    response = None
                if response is not None:
                    if not isinstance(response, dict) or response.get("id") != request_id or not isinstance(response.get("ok"), bool):
                        raise RuntimeError("QMD worker response identity mismatch")
                    if not response["ok"]:
                        error = _detail(str(response.get("error", "QMD request failed")).encode("utf-8"))
                        # A normal tool error leaves the transport reusable.
                        return_error = RuntimeError(f"QMD {payload.get('op', 'query')} failed: {error}")
                        break
                    if "result" not in response:
                        raise RuntimeError("QMD worker returned no structured result")
                    return response["result"]
                if run.process.poll() is not None or write_error:
                    with run.stderr_lock:
                        detail = _detail(run.stderr)
                    raise RuntimeError(f"QMD worker exited during {payload.get('op', 'query')}: {detail}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProcessTimeoutError(self.command, timeout)
                run.wake.wait(min(0.05, remaining))
                run.wake.clear()
        except BaseException:
            # Cancellation/timeout cannot leave a reply queued for a later query.
            if run is not None:
                self._discard(run)
            raise
        finally:
            self._request_lock.release()
        raise return_error

    def close(self):
        with self._state_lock:
            self._closed = True
            run, self._run = self._run, None
        if run is not None:
            run.close()


def get_worker(data_root, node, script, runtime, payload, *, stop_event=None):
    """Fresh LibrarySearch instances share only the same resolved runtime."""
    identity = {key: payload.get(key) for key in
                ("qmd_dir", "env", "model_uri", "embedding_enabled", "model_cache")}
    sdk = Path(payload["qmd_dir"])
    sources = [Path(node), sdk / "package.json", *(sdk / "dist" / name for name in ("index.js", "store.js", "llm.js"))]
    identity["files"] = []
    for path in sources:
        try:
            stat = path.stat()
            identity["files"].append([str(path), stat.st_size, stat.st_mtime_ns, stat.st_ino])
        except OSError:
            identity["files"].append([str(path), None])
    identity = json.dumps(identity, sort_keys=True, ensure_ascii=False)
    signature = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    key = (_root_key(data_root), _root_key(node), _root_key(script), signature)
    with _WORKERS_LOCK:
        if key[0] in _CLOSED_WORKBENCHES:
            raise RuntimeError("QMD workbench has been closed")
        if key[0] in _MAINTENANCE:
            raise RuntimeError("QMD index maintenance is active; retry the query")
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("QMD warmup has been stopped")
        # Settings, bridge updates and SDK replacements retire the old process
        # for this root instead of accumulating dormant runtimes indefinitely.
        retired = [_WORKERS.pop(old) for old in list(_WORKERS) if old[0] == key[0] and old != key]
        worker = _WORKERS.get(key)
        if worker is None:
            # Purge retains the search root/build.lock, but removes runtime.
            # A long-lived Windows cwd must never pin that disposable folder.
            worker = _WORKERS[key] = QmdWorker(node, script, data_root)
    for previous in retired:
        previous.close()
    return worker


def close_workers(data_root=None):
    """Release this workbench's processes; CLI users are collected at exit."""
    root = _root_key(data_root) if data_root is not None else None
    with _WORKERS_LOCK:
        keys = [key for key in _WORKERS if root is None or key[0] == root]
        workers = [_WORKERS.pop(key) for key in keys]
    for worker in workers:
        worker.close()


atexit.register(close_workers)
