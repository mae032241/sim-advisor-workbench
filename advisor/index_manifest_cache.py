"""Bounded, identity-checked reads of generated search manifests.

UI/native callers request only the small summary. Full QMD callers receive
independent metadata with shared ``mapping``/``representation`` trees: those
two trees are read-only by contract and must never be mutated by callers.
The cache stores disk locators; resolving them belongs to the current root.
No sidecars survive purge, relocation, or interrupted publication.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import json
import os
from pathlib import Path
import threading

from .build_control import check_cancelled
from .index_manifest import (MANIFEST_SCHEMA, POINTER_SCHEMA, is_manifest_pointer,
                             pointer_target, validate_pointer_authority)
from .ui_status import status_summary


_SUMMARY_LIMIT = 4
_FULL_MAX_BYTES = 256 * 1024 * 1024
_SHARED_FIELDS = frozenset({"mapping", "representation"})
_SUMMARIES = OrderedDict()
_POINTERS = OrderedDict()
_FULL = None
_LOCK = threading.RLock()


def _identity(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _discard(key):
    global _FULL
    _SUMMARIES.pop(key, None)
    _POINTERS.pop(key, None)
    if _FULL is not None and _FULL[0] == key:
        _FULL = None


def _remember_summary(key, identity, value):
    _SUMMARIES[key] = (identity, status_summary(value))
    _SUMMARIES.move_to_end(key)
    while len(_SUMMARIES) > _SUMMARY_LIMIT:
        _SUMMARIES.popitem(last=False)


def _copy_full(value):
    return {key: item if key in _SHARED_FIELDS else copy.deepcopy(item) for key, item in value.items()}


def _read_manifest_file(path, summary=False, *, cancellable=True):
    """Read a stable current manifest, returning None for missing/invalid JSON.

    Every call stats the live path, even on a cache hit. Atomic replacement,
    in-place updates, deletion and re-creation therefore invalidate old data.
    A concurrent publication is retried rather than caching mixed generations.
    Only one full manifest, up to 256 MiB on disk, is retained; summary-only
    reads never retain a full per-object tree.
    """
    global _FULL
    path = Path(path).absolute()
    key = os.path.normcase(str(path))
    def check():
        if cancellable:
            check_cancelled()

    check()
    # Serialize misses so concurrent UI polls do not parse the same huge file
    # several times or retain several copies while one publication completes.
    with _LOCK:
        for _attempt in range(3):
            check()
            try:
                identity = _identity(path.stat())
            except OSError:
                _discard(key)
                return None
            pointer = _POINTERS.get(key)
            if pointer is not None and pointer[0] == identity:
                _POINTERS.move_to_end(key)
                return copy.deepcopy(pointer[1])
            cached = _SUMMARIES.get(key)
            if summary and cached is not None and cached[0] == identity:
                _SUMMARIES.move_to_end(key)
                return copy.deepcopy(cached[1])
            if not summary and _FULL is not None and _FULL[:2] == (key, identity):
                return _copy_full(_FULL[2])
            if ((cached is not None and cached[0] != identity)
                    or (_FULL is not None and _FULL[0] == key and _FULL[1] != identity)):
                _discard(key)
            try:
                check()
                with path.open("r", encoding="utf-8") as source:
                    # Windows/Python can report creation time for path.stat()
                    # and change time for fstat(). Compare handle identity and
                    # content metadata here; the final path check includes both.
                    if _identity(os.fstat(source.fileno()))[:4] != identity[:4]:
                        continue
                    value = json.load(source)
                check()
                if _identity(path.stat()) != identity:
                    continue
            except (OSError, ValueError):
                _discard(key)
                return None
            if not isinstance(value, dict):
                raise ValueError("Index manifest must be an object")
            if value.get("manifest_schema") not in (None, MANIFEST_SCHEMA, POINTER_SCHEMA):
                _discard(key)
                raise ValueError("Unsupported index manifest schema")
            _remember_summary(key, identity, value)
            if is_manifest_pointer(value):
                _POINTERS[key] = (identity, value)
                _POINTERS.move_to_end(key)
                while len(_POINTERS) > _SUMMARY_LIMIT:
                    _POINTERS.popitem(last=False)
                return copy.deepcopy(value)
            if summary:
                return copy.deepcopy(_SUMMARIES[key][1])
            _FULL = (key, identity, value) if identity[2] <= _FULL_MAX_BYTES else None
            return _copy_full(value)
        _discard(key)
        return None


def read_manifest(path, summary=False, *, cancellable=True):
    """Read legacy JSON or one validated, local generation-manifest pointer.

    Both pointer and authority identities are checked on every read, including
    summary cache hits. A dangling pointer never falls back to an older cached
    generation, and a changed pointer is retried before returning metadata.
    """
    path = Path(path).absolute()
    key = os.path.normcase(str(path))
    with _LOCK:
        for _attempt in range(3):
            value = _read_manifest_file(path, summary, cancellable=cancellable)
            if not is_manifest_pointer(value):
                return value
            identity = _POINTERS[key][0]
            target = pointer_target(path, value)
            authority = _read_manifest_file(target, summary, cancellable=cancellable)
            if authority is None:
                return None
            validate_pointer_authority(path, value, target, authority)
            try:
                if _identity(path.stat()) == identity:
                    return authority
            except OSError:
                _discard(key)
                return None
            _discard(key)
        return None
