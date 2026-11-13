"""Bounded, identity-checked reads of generated search manifests.

UI callers request only the small summary. Full QMD callers receive
independent metadata with shared ``mapping``/``representation`` trees: those
two trees are read-only by contract and must never be mutated by callers.
Disk manifests and version-bound resolved locators have separate bounded caches.
The latter is bound to the current root and physical directory identities.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import json
import os
from pathlib import Path
import stat
import threading

from .build_control import check_cancelled
from .index_manifest import (MANIFEST_SCHEMA, POINTER_SCHEMA, is_manifest_pointer,
                             normalize_modes, pointer_target, validate_pointer_authority)
from .index_paths import resolve_index_manifest
from .ui_status import status_summary


_SUMMARY_LIMIT = 4
_FULL_MAX_BYTES = 256 * 1024 * 1024
_SHARED_FIELDS = frozenset({"mapping", "representation"})
_SUMMARIES = OrderedDict()
_POINTERS = OrderedDict()
_FULL = None
_LOCK = threading.RLock()
_RESOLVED = OrderedDict()
_RESOLVED_LIMIT = 8
_RESOLVED_LOCK = threading.Lock()
_RESOLVE_MISS_LOCK = threading.Lock()
_LOCATORS = ("directory", "libraries", "path_schema", "keyword_storage")


def _layout_paths(root, authority, resolved):
    """Capture physical path identities once, without directory timestamps."""
    paths = {Path(root).absolute(), Path(authority).absolute()}
    if resolved.get("directory"):
        paths.add(Path(resolved["directory"]))
    for item in resolved.get("libraries", {}).values():
        paths.update(Path(item[name]) for name in ("corpus", "db_path", "vector_db_path",
                     "keyword_reused_from_directory", "vector_reused_from_directory") if item.get(name))
    for path in tuple(paths):
        paths.update(path.parents)
    return tuple(sorted(str(path) for path in paths))


def _layout_signature(paths):
    # A replaced directory/link invalidates cached path resolution. Ordinary
    # directory contents, WAL writes and reader-created files do not.
    result = []
    for path in paths:
        try:
            info = os.lstat(path)
            attributes = getattr(info, "st_file_attributes", 0)
            linked = stat.S_ISLNK(info.st_mode) or bool(attributes & 0x400)
            result.append((info.st_dev, info.st_ino, info.st_mode, attributes & 0x400,
                           *((info.st_size, info.st_mtime_ns, info.st_ctime_ns) if linked else ())))
        except OSError:
            result.append(None)
    return tuple(result)


def _runtime_locators(root, authority_path, identity, value, resolve):
    """Reuse resolved metadata for one authority version at one data root."""
    key = (os.path.normcase(str(Path(root).absolute())),
           os.path.normcase(str(Path(authority_path).absolute())), identity, value.get("generation"))

    def cached():
        with _RESOLVED_LOCK:
            entry = _RESOLVED.get(key)
        if entry is not None and _layout_signature(entry[1]) == entry[2]:
            with _RESOLVED_LOCK:
                if key in _RESOLVED:
                    _RESOLVED.move_to_end(key)
            return copy.deepcopy(entry[0])
        return None

    result = cached()
    if result is not None:
        return result
    # Only version/layout misses are serialized. Warm requests do no full
    # locator conversion and never hold the JSON-cache lock during path I/O.
    with _RESOLVE_MISS_LOCK:
        result = cached()
        if result is not None:
            return result
        result = resolve()
        paths = _layout_paths(root, authority_path, result)
        signature = _layout_signature(paths)
        if any(info is not None and (stat.S_ISLNK(info[2]) or info[3]) for info in signature):
            # Link-bearing layouts retain fresh boundary validation rather
            # than caching a decision sampled during a link replacement.
            return resolve()
        with _RESOLVED_LOCK:
            _RESOLVED[key] = (copy.deepcopy(result), paths, signature)
            _RESOLVED.move_to_end(key)
            while len(_RESOLVED) > _RESOLVED_LIMIT:
                _RESOLVED.popitem(last=False)
        return result


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


def _read_manifest_file(path, summary=False, *, cancellable=True, receipt=None):
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
            if receipt is not None:
                receipt[str(path)] = identity
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
            value = normalize_modes(value)
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


def read_manifest(path, summary=False, *, cancellable=True, resolve_paths=False, receipt=None):
    """Read legacy JSON or one validated, local generation-manifest pointer.

    Pointer/authority identities and lightweight layout stamps are checked on
    reads. Complete locator conversion is cached by authority version and data
    root; publication, migration, configuration or layout changes invalidate it.
    A dangling pointer never falls back to an older cached generation.
    ``receipt`` accumulates the file identities belonging to returned content.
    Conflicting reads of one authority are marked None rather than overwritten.
    """
    path = Path(path).absolute()
    key = os.path.normcase(str(path))

    def finish(value, observed):
        if receipt is not None:
            for filename, identity in observed.items():
                receipt[filename] = identity if filename not in receipt or receipt[filename] == identity else None
        return value

    for _attempt in range(3):
        observed = {}
        value = _read_manifest_file(path, summary, cancellable=cancellable, receipt=observed)
        if not is_manifest_pointer(value):
            if value is not None and resolve_paths:
                value.update(_runtime_locators(path.parent, path, observed[str(path)], value,
                    lambda: resolve_index_manifest({key: value[key] for key in _LOCATORS if key in value}, path.parent)))
                value.pop("path_schema", None)
                # A publication during cold conversion cannot bless old data.
                try:
                    if _identity(path.stat()) != observed[str(path)]:
                        continue
                except OSError:
                    return None
            return finish(value, observed)
        identity = observed[str(path)]
        target = pointer_target(path, value)
        authority = _read_manifest_file(target, summary, cancellable=cancellable, receipt=observed)
        if authority is None:
            return finish(None, observed)
        if is_manifest_pointer(authority):
            raise ValueError("Index manifest pointer chains are forbidden")
        if authority.get("generation") != value.get("generation"):
            raise ValueError("Index pointer and generation manifest identities differ")
        resolved = _runtime_locators(path.parent, target, observed[str(target)], authority,
            lambda: validate_pointer_authority(path, value, target, authority))
        try:
            if (_identity(path.stat()) == identity and
                    _identity(target.stat()) == observed[str(target)]):
                if resolve_paths:
                    authority.update(resolved)
                    authority.pop("path_schema", None)
                return finish(authority, observed)
        except OSError:
            with _LOCK:
                _discard(key)
            return None
        with _LOCK:
            _discard(key)
    return None
