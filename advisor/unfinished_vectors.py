"""Preview and remove only unfinished, unpublished registered-library vectors.

There is deliberately no recursive deletion, QMD import, model inspection, or
job recovery here. Completed and ambiguous manifests are preservation fences.
"""
from __future__ import annotations

from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid

from .index_paths import resolve_index_manifest
from .index_manifest import (MANIFEST_SCHEMA, POINTER_SCHEMA, is_manifest_pointer,
                             pointer_target, validate_pointer_authority)
from .library_search import LibrarySearch, _write_json
from .library_store import LibraryLifecycleConflict, encoded, now, process_alive

_GENERATION = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_LIBRARY = re.compile(r"lib_[a-f0-9]{20}\Z")
_TOKEN = re.compile(r"[a-f0-9]{64}\Z")
_UNFINISHED = {"building", "cancelled", "interrupted", "failed"}
_FILES = ("qmd-vector.sqlite3", "qmd-vector.sqlite3-wal", "qmd-vector.sqlite3-shm", "qmd-vector.progress.json")
_BUSY = "存在进行中的构建、索引或清理任务，请先中断并等待结束后再清理。"


def _owned(root, path):
    """Check lexical ownership and every existing reparse point before use."""
    path = Path(path)
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        raise ValueError("向量清理路径超出当前资料库索引目录。")
    current = root
    for part in (None, *path.relative_to(root).parts):
        if part is not None:
            current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("向量清理拒绝符号链接或联接（symlink/junction）。")
    return path


def _identity(root, path):
    path = _owned(root, path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("向量清理仅接受当前索引目录独占的普通文件。")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _manifest(root, path):
    if not path.exists():
        _owned(root, path)  # Broken links must also be refused.
        return None
    identity = _identity(root, path)
    raw = path.read_bytes()
    value = json.loads(raw)
    if _identity(root, path) != identity:
        raise ValueError("向量清理的索引清单已变化，请重新预览。")
    stamp = [path.relative_to(root).as_posix(), identity, hashlib.sha256(raw).hexdigest()]
    if not isinstance(value, dict) or value.get("manifest_schema") not in (None, MANIFEST_SCHEMA, POINTER_SCHEMA):
        raise ValueError("无效的索引清单。")
    if is_manifest_pointer(value):
        target = pointer_target(path, value)
        authority = _manifest(root, target)
        if authority is None:
            raise ValueError("索引指针缺少代次清单。")
        validate_pointer_authority(path, value, target, authority["raw"])
        if _identity(root, path) != identity:
            raise ValueError("向量清理的索引指针已变化，请重新预览。")
        # Update only the authority. Keep both identities/hashes in the preview
        # token so a pointer switch or target edit invalidates cleanup consent.
        return dict(authority, stamp=[stamp, authority["stamp"]])
    resolved = resolve_index_manifest(value, root)
    return {"path": path, "raw": value, "value": resolved,
            "stamp": stamp}


def _vector(value):
    modes = value.get("modes")
    return modes.get("qmd_vector") if isinstance(modes, dict) else None


def _unfinished(value):
    vector = _vector(value)
    return (isinstance(vector, dict) and vector.get("ready") is False
            and isinstance(vector.get("state"), str) and vector["state"] in _UNFINISHED
            and all(isinstance(vector.get(field, {}), dict) for field in ("embedding_records", "progress")))


def _completed_progress(value):
    # A chunk counter reaching its total can be an intermediate callback: the
    # actual retained batch also has chunk==total while bytes are incomplete.
    if not isinstance(value, dict):
        return False
    if value.get("ready") is True or value.get("state") in ("ready", "completed"):
        return True
    counters = [value.get(key) for key in ("chunksEmbedded", "totalChunks", "bytesProcessed", "totalBytes")]
    return (all(type(number) is int and number >= 0 for number in counters)
            and counters[1] > 0 and counters[3] > 0
            and counters[0] >= counters[1] and counters[2] >= counters[3]
            and value.get("errors", 0) == 0)


def _scan(root):
    root = _owned(root.parent, root)
    plan = {"entries": [], "stamps": [], "warnings": [], "skipped_count": 0}
    aliases = []
    for name in ("active.json", "last-build.json"):
        item = _manifest(root, root / name)
        if item:
            aliases.append(item)
            plan["stamps"].append(item["stamp"])
    generations = _owned(root, root / "generations")
    if not generations.exists():
        return plan
    for directory in sorted(generations.iterdir()):
        if not _GENERATION.fullmatch(directory.name):
            plan["skipped_count"] += 1
            continue
        directory = _owned(root, directory)
        if not directory.is_dir():
            plan["skipped_count"] += 1
            continue
        try:
            own = _manifest(root, directory / "manifest.json")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            # Links/reparse points are an explicit refusal, never a partial scan.
            if "symlink/junction" in str(exc):
                raise
            own = None
        if not own or own["value"].get("directory") != str(directory):
            plan["skipped_count"] += 1
            continue
        plan["stamps"].append(own["stamp"])
        manifests = [own, *(item for item in aliases if item["value"].get("directory") == str(directory))]
        if any(not _unfinished(item["value"]) for item in manifests):
            plan["skipped_count"] += 1
            continue
        libraries = own["value"].get("libraries", {})
        keys = [item.get("key") for item in libraries.values()]
        if len(set(keys)) != len(keys) or any(item["value"].get("libraries") != libraries for item in manifests):
            plan["skipped_count"] += 1
            continue
        for library_id, library in sorted(libraries.items()):
            key = library.get("key", "")
            if not _LIBRARY.fullmatch(key):
                plan["skipped_count"] += 1
                continue
            # Any recorded library-level result is a preservation fence, even
            # if the generation failed later on another library.
            if any(library_id in (_vector(item["value"]).get("embedding_records") or {})
                   or _completed_progress((_vector(item["value"]).get("progress") or {}).get(library_id))
                   for item in manifests):
                plan["skipped_count"] += 1
                continue
            parent = _owned(root, directory / key)
            progress = _owned(root, parent / _FILES[-1])
            if progress.exists():
                _identity(root, progress)
                try:
                    progress_value = json.loads(progress.read_bytes())
                except (ValueError, UnicodeError):
                    plan["skipped_count"] += 1
                    continue
                if not isinstance(progress_value, dict):
                    plan["skipped_count"] += 1
                    continue
                if _completed_progress(progress_value):
                    plan["skipped_count"] += 1
                    continue
            files = []
            for name in _FILES:
                path = _owned(root, parent / name)
                if path.exists():
                    identity = _identity(root, path)
                    files.append({"path": path, "identity": identity})
            if files:
                plan["entries"].append({"directory": directory, "library_id": library_id,
                                         "files": files, "manifests": manifests})
    if plan["skipped_count"]:
        plan["warnings"].append("已保留完成、未请求向量或无法确定状态的索引；仅清理未完成且尚未发布为可用索引的向量文件。")
    return plan


def _summary(root, plan, *, building=False):
    files = [item for entry in plan["entries"] for item in entry["files"]]
    stamp = {"root": str(root), "manifests": plan["stamps"],
             "files": [[item["path"].relative_to(root).as_posix(), item["identity"]] for item in files]}
    return {"file_count": len(files), "bytes": sum(item["identity"][2] for item in files),
            "generation_count": len({entry["directory"] for entry in plan["entries"]}),
            "preview_token": hashlib.sha256(encoded(stamp).encode()).hexdigest(),
            "can_clean": bool(files) and not building, "building": building,
            "skipped_count": plan["skipped_count"], "warnings": plan["warnings"],
            "samples": [item["path"].relative_to(root).as_posix() for item in files[:8]]}


def _active_jobs(db):
    # Read stored facts directly. jobs() can recover dead jobs and is unsuitable
    # for a read-only preview or for preserving historical task receipts.
    return db.execute("SELECT id FROM builds WHERE json_extract(body,'$.status') IN ('queued','running','cancel_requested') LIMIT 1").fetchone() is not None


def _catalog_busy(store):
    """Read active jobs without SQLite creating WAL/SHM files in the workspace.

    Immutable mode is correct for the checkpointed main file. An existing WAL
    instead needs a temporary snapshot, so uncheckpointed queued jobs are seen.
    A changing snapshot is conservatively treated as busy.
    """
    source = _owned(store.root, store.db_path)
    wal = _owned(store.root, source.with_name(source.name + "-wal"))
    before = [_identity(store.root, source), _identity(store.root, wal) if wal.exists() else None]
    if before[1] and before[1][2]:
        with tempfile.TemporaryDirectory(prefix="sim-advisor-vector-preview-") as temporary:
            copied = Path(temporary) / source.name
            shutil.copyfile(source, copied)
            shutil.copyfile(wal, copied.with_name(copied.name + "-wal"))
            with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)) as db:
                busy = _active_jobs(db)
    else:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            busy = _active_jobs(db)
    after = [_identity(store.root, source), _identity(store.root, wal) if wal.exists() else None]
    return busy or before != after


def _live_lease(store, path, *, search=False):
    _owned(store.root, path)
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_bytes())
        return process_alive(value.get("pid")) and (search or not store._owner_moved(value))
    except (OSError, ValueError, TypeError, AttributeError):
        return True  # An unreadable owner cannot authorize deletion.


def preview(store):
    root = _owned(store.root, store.root / "search")
    search = LibrarySearch.for_maintenance(root)
    acquired = store._build_lock.acquire(blocking=False)
    search_acquired = search.lock.acquire(blocking=False) if acquired else False
    try:
        building = (not acquired or not search_acquired or _catalog_busy(store)
                    or _live_lease(store, store.root / ".build.lock")
                    or _live_lease(store, root / "build-lease.json", search=True))
        if building:
            return _summary(root, {"entries": [], "stamps": [], "warnings": [_BUSY], "skipped_count": 0}, building=True)
        return _summary(root, _scan(root))
    finally:
        if search_acquired:
            search.lock.release()
        if acquired:
            store._build_lock.release()


def clean(store, *, confirmed=False, token=None):
    if confirmed is not True:
        raise ValueError("请明确确认清理未完成的向量索引。")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ValueError("请先预览清理范围并提交有效的 preview_token。")
    root = _owned(store.root, store.root / "search")
    if not store._build_lock.acquire(blocking=False):
        raise LibraryLifecycleConflict(_BUSY)
    lease = store.root / ".build.lock"
    owned = False
    search = LibrarySearch.for_maintenance(root)
    search_acquired = False
    handle = None
    lease_token = uuid.uuid4().hex
    try:
        if _live_lease(store, lease):
            raise LibraryLifecycleConflict(_BUSY)
        if lease.exists():
            _identity(store.root, lease)
            lease.unlink()
        try:
            with lease.open("x", encoding="utf-8") as stream:
                stream.write(encoded({"pid": os.getpid(), "workspace_root": str(store.paths.root),
                                      "kind": "unfinished-vector-cleanup", "token": lease_token}))
            owned = True
        except FileExistsError:
            raise LibraryLifecycleConflict(_BUSY) from None
        with closing(store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")  # Also fences enqueueing in other processes.
            if _active_jobs(db):
                raise LibraryLifecycleConflict(_BUSY)
            search_acquired = search.lock.acquire(blocking=False)
            if not search_acquired or _live_lease(store, root / "build-lease.json", search=True):
                raise LibraryLifecycleConflict(_BUSY)
            # No candidates is an idempotent no-op, including a missing root.
            plan = _scan(root)
            result = _summary(root, plan)
            if result["preview_token"] != token:
                raise LibraryLifecycleConflict("清理范围已变化，请重新预览后再确认；未删除任何向量文件。")
            if plan["entries"]:
                _owned(root, root / "build.lock")
                _owned(root, root / "build-lease.json")
                try:
                    handle, search_token = search._acquire_lease("unfinished-vector-cleanup")
                except RuntimeError:
                    raise LibraryLifecycleConflict(_BUSY) from None
                # The OS lease closes the cross-process scan/build race.
                plan = _scan(root)
                if _summary(root, plan)["preview_token"] != token:
                    raise LibraryLifecycleConflict("清理范围已变化，请重新预览后再确认；未删除任何向量文件。")
                updates = {}
                for entry in plan["entries"]:
                    for item in entry["manifests"]:
                        updates.setdefault(item["path"], copy.deepcopy(item["raw"]))
                # Verify every target before deleting the first file.
                for entry in plan["entries"]:
                    for item in entry["files"]:
                        if _identity(root, item["path"]) != item["identity"]:
                            raise LibraryLifecycleConflict("向量文件已变化，请重新预览。")
                for entry in plan["entries"]:
                    for item in entry["files"]:
                        if _identity(root, item["path"]) != item["identity"]:
                            raise LibraryLifecycleConflict("向量文件已变化，请重新预览。")
                        item["path"].unlink()
                cleaned_at = now()
                for path, value in updates.items():
                    vector = value["modes"]["qmd_vector"]
                    vector.update(ready=False, state="cleaned", cleanup={"at": cleaned_at, "preview_token": token,
                        "scope": "unfinished-unpublished-vector-files", "embedding_invoked": False})
                    _owned(root, path)
                    _write_json(path, value)
            return dict(result, status="cleaned", cleaned=True, can_clean=False, embedding_invoked=False)
    finally:
        if handle is not None:
            search._release_lease(handle, search_token)
        if search_acquired:
            search.lock.release()
        if owned:
            try:
                if json.loads(lease.read_bytes()).get("token") == lease_token:
                    lease.unlink()
            except (OSError, ValueError):
                pass
        store._build_lock.release()
