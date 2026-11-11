"""Small sequential getter batches with a disposable on-disk result queue.

The checkpoint owns case selection/state. This runner only reads its current
running case and up to four later pending cases; it never publishes or indexes.
Each call loads one raw snapshot, removes its file, and returns the usual
``_deep_state`` tuple. No collection of full snapshots is retained in Python.
"""
from __future__ import annotations

from contextlib import closing
import copy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

from .build_control import BuildCancelled, check_cancelled
from .compact_api import _hash
from .deep_enrichment import SCHEMA, _batch_path, _source_record
from .library_model import JAVA_SOURCE, _api_coverage, _comsol_root, _read_api_state
from .process_runner import run_text

BATCH_SOURCE = JAVA_SOURCE.with_name("ReadMphBatch.java")


class BatchAPIRunner:
    def __init__(self, store, checkpoint, *, batch_size=5, temp_root=None):
        if type(batch_size) is not int or batch_size not in {3, 5}:
            raise ValueError("API batch_size must be 3 or 5")
        self.store, self.checkpoint = store, _batch_path(store, checkpoint)
        self.batch_size = batch_size
        self.temp_root = Path(temp_root).resolve() if temp_root is not None else store.root / ".api-runtime"
        self._cache, self._temporary, self._closed = {}, None, False
        self._lock = threading.RLock()
        self._stats = {"batches": 0, "jvm_processes": 0, "prefetched": 0, "consumed": 0, "failed": 0, "discarded": 0}
        with closing(self._checkpoint()) as db:
            meta = dict(db.execute("SELECT key,value FROM meta"))
            if meta.get("schema") != SCHEMA or store.paths.resolve(meta.get("library_root", "")) != store.root:
                raise ValueError("API runner checkpoint belongs to another catalog or schema")

    def _checkpoint(self):
        db = sqlite3.connect(self.checkpoint.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    @property
    def stats(self):
        return copy.deepcopy(self._stats)

    def _record(self, row):
        authority = self.store.get(row["authority_id"])
        if not authority or authority.get("object_id") != row["object_id"] or authority.get("library_id") != row["library_id"]:
            raise ValueError("Checkpoint model authority is missing or changed")
        _, record = _source_record(self.store, authority)
        return {"object_id": row["object_id"], "authority_id": row["authority_id"],
                "source": Path(record["path"]).resolve(), "fingerprint": record["content_hash"],
                "preview": (authority.get("coverage", {}).get("source_container", {}).get("status") == "preview_only")}

    def _current(self, source, fingerprint):
        with closing(self._checkpoint()) as db:
            rows = db.execute("SELECT object_id,authority_id,library_id FROM cases WHERE state='running' ORDER BY object_id LIMIT 2").fetchall()
        if len(rows) != 1:
            raise ValueError("API runner requires exactly one current running checkpoint case")
        record = self._record(rows[0])
        if record["source"] != source or record["fingerprint"] != fingerprint:
            raise ValueError("API runner source/hash is outside the current checkpoint case")
        if record["preview"]:
            raise ValueError("Preview-only MPH has no saved model configuration for API matching")
        return record

    def _discard(self):
        self._stats["discarded"] += len(self._cache)
        self._cache.clear()
        if self._temporary is not None:
            temporary, self._temporary = self._temporary, None
            for path in Path(temporary.name).rglob("*.mph"):
                path.chmod(0o644)
            temporary.cleanup()

    def close(self):
        with self._lock:
            self._discard()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _fill(self, current):
        self._discard()
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(prefix="getter-batch-", dir=self.temp_root)
        work = Path(self._temporary.name)
        for name in ("classes", "home", "tmp"):
            (work / name).mkdir()
        records = [current]
        skipped = []
        with closing(self._checkpoint()) as db:
            following = db.execute("SELECT object_id,authority_id,library_id FROM cases WHERE state='pending' AND object_id>? ORDER BY object_id LIMIT ?",
                                   (current["object_id"], self.batch_size - 1)).fetchall()
        for row in following:
            check_cancelled()
            try:
                record = self._record(row)
                if record["preview"]:
                    skipped.append({"object_id": row["object_id"], "reason": "preview_only"})
                else:
                    records.append(record)
            except (ValueError, OSError) as exc:
                skipped.append({"object_id": row["object_id"], "reason": str(exc)[:300]})
        arguments = []
        for index, record in enumerate(records):
            check_cancelled()
            case = work / str(index)
            case.mkdir()
            record = dict(record, snapshot=case / "api-state.json", error=case / "error.json")
            self._cache[record["object_id"]] = record
            try:
                before = record["source"].stat()
                if _hash(record["source"]) != record["fingerprint"]:
                    raise ValueError("Original MPH changed before batched API extraction")
                record["source_stat"] = (before.st_size, before.st_mtime_ns)
                copied = case / "input.mph"
                shutil.copyfile(record["source"], copied)
                if _hash(copied) != record["fingerprint"]:
                    raise ValueError("Batched MPH copy differs from its expected source")
                copied.chmod(0o444)
                arguments.extend(map(str, (copied, record["snapshot"], record["error"], case / "model_export.java")))
            except (ValueError, OSError) as exc:
                record["failure"] = str(exc)[:500]
        self._stats["batches"] += 1
        self._stats["prefetched"] += len(self._cache)
        measurement = {"selected": len(self._cache), "skipped": skipped, "batch_source_sha256": _hash(BATCH_SOURCE),
                       "getter_source_sha256": _hash(JAVA_SOURCE)}
        self._stats["last_batch"] = measurement
        try:
            if arguments:
                binary = _comsol_root() / "java/win64/jre/bin"
                java, javac = binary / "java.exe", binary / "javac.exe"
                if not java.is_file() or not javac.is_file():
                    raise RuntimeError("Standalone COMSOL Java/compiler not found")
                clock = time.monotonic()
                compiled = run_text([str(javac), "-encoding", "UTF-8", "-d", str(work / "classes"), str(JAVA_SOURCE), str(BATCH_SOURCE)], "", work, 35, 512 * 1024)
                measurement["javac_seconds"] = time.monotonic() - clock
                if compiled.returncode:
                    raise RuntimeError("Standalone batch extractor compilation failed: " + compiled.stderr[-600:])
                timeout = max(15, min(240, int(os.environ.get("SIM_ADVISOR_MPH_TIMEOUT", "120"))))
                manifest = work / "batch.json"
                command = [str(java), "-Djava.library.path=" + str(_comsol_root() / "bin/win64"),
                           "-Duser.home=" + str(work / "home"), "-Djava.io.tmpdir=" + str(work / "tmp"),
                           "-cp", str(work / "classes") + os.pathsep + str(_comsol_root() / "plugins/*"),
                           "ReadMphBatch", str(timeout * 1000), str(manifest), *arguments]
                wrapper = "import os,sys,json,subprocess; e=dict(os.environ,TEMP=sys.argv[2],TMP=sys.argv[2],COMSOL_NUM_THREADS='1'); p=subprocess.run(json.loads(sys.argv[1]),env=e,stdin=subprocess.DEVNULL);sys.exit(p.returncode)"
                clock = time.monotonic()
                self._stats["jvm_processes"] += 1
                result = run_text([sys.executable, "-c", wrapper, json.dumps(command), str(work / "tmp")], "", work,
                                  timeout * (len(arguments) // 4 + 1) + 15, 1024 * 1024)
                measurement.update(jvm_seconds=time.monotonic() - clock, returncode=result.returncode)
                if manifest.is_file() and manifest.stat().st_size <= 128 * 1024:
                    measurement["runtime"] = json.loads(manifest.read_text(encoding="utf-8"))
                if result.returncode:
                    measurement["process_error"] = (result.stderr or result.stdout)[-1200:]
            for record in self._cache.values():
                if not record["snapshot"].is_file() and not record.get("failure"):
                    reason = "Batch JVM did not complete this case"
                    if record["error"].is_file() and record["error"].stat().st_size <= 32 * 1024:
                        error = json.loads(record["error"].read_text(encoding="utf-8"))
                        reason = str(error.get("error_class", "")) + ": " + str(error.get("reason", reason))
                    record["failure"] = reason[:1000]
        except BuildCancelled:
            self._discard()
            raise
        except (OSError, ValueError, RuntimeError, __import__("subprocess").TimeoutExpired) as exc:
            measurement["process_error"] = str(exc)[:1200]
            for record in self._cache.values():
                if not record["snapshot"].is_file():
                    record["failure"] = "Batch JVM failed: " + str(exc)[:700]
        finally:
            # Keep only small metadata and raw JSON paths until consumption.
            for path in work.rglob("*"):
                if path.is_file() and path.suffix.lower() in {".mph", ".java", ".class"}:
                    if path.suffix.lower() == ".mph":
                        path.chmod(0o644)
                    path.unlink()

    def __call__(self, source, output, fingerprint):
        del output  # The queue owns its lifetime; callers receive no derived assets.
        with self._lock:
            if self._closed:
                raise ValueError("API batch runner is closed")
            cached = None
            try:
                check_cancelled()
                current = self._current(Path(source).resolve(strict=True), fingerprint)
                cached = self._cache.get(current["object_id"])
                if cached is None or any(cached[key] != current[key] for key in ("source", "fingerprint", "authority_id")):
                    self._fill(current)
                cached = self._cache.pop(current["object_id"])
                self._stats["consumed"] += 1
                if cached.get("failure"):
                    raise ValueError(cached["failure"])
                stat = current["source"].stat()
                if ((stat.st_size, stat.st_mtime_ns) != cached["source_stat"] or _hash(current["source"]) != fingerprint):
                    raise ValueError("Original MPH changed during batched API extraction")
                data = _read_api_state(cached["snapshot"])
                cached["snapshot"].unlink()
                coverage = _api_coverage(data, cached=False)
                coverage["batch_runtime"] = copy.deepcopy(self._stats["last_batch"])
                return data, [], coverage
            except BuildCancelled:
                self._discard()
                raise
            except (ValueError, OSError, sqlite3.Error) as exc:
                self._stats["failed"] += 1
                return None, [], {"status": "failed", "reason": str(exc)[:1200], "batch_runtime": copy.deepcopy(self._stats.get("last_batch", {}))}
            finally:
                if cached is not None:
                    cached["snapshot"].unlink(missing_ok=True)
                if not self._cache:
                    self._discard()
