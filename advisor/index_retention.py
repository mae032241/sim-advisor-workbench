"""Reclaim unreferenced generated indexes after a fully successful build.

The caller holds LibrarySearch's thread lock and process lease. Source files,
catalog revisions, evidence blobs, and extraction artifacts are outside scope.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat
import time

from .index_paths import SHARED_KEYWORD_STORAGE, resolve_index_manifest
from .index_manifest_cache import read_manifest

POLICY = "unreferenced-search-generations/v1"
_GENERATION = re.compile(r"[a-f0-9]{20}-[a-f0-9]{8}\Z")
_CORPUS_FILE = re.compile(r"o-[a-f0-9]{32}\.md\Z")
_REUSE = ("keyword_reused_from_directory", "vector_reused_from_directory")
_SIDECARS = ("", "-wal", "-shm", "-journal")
_ROOT_FILES = {"manifest.json", *("objects.sqlite3" + suffix for suffix in _SIDECARS)}
_SHARED_FILES = {"qmd-keyword.sqlite3" + suffix for suffix in _SIDECARS} | {
    "qmd-keyword.progress.json", "qmd-keyword.sqlite3.progress.json"}
_LIBRARY_FILES = {name + suffix for name in ("qmd-keyword.sqlite3", "qmd-vector.sqlite3") for suffix in _SIDECARS}
_LIBRARY_FILES.update({"qmd-keyword.progress.json", "qmd-vector.progress.json"})
_RETIREMENT_FAILURES = set()


def _entry(path):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("Index retention refuses symlink/junction/reparse entries")
    return info


def _owned(root, path):
    path = Path(path)
    if not path.is_absolute() or not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError("Index retention path escapes or aliases the search root")
    current = root
    for part in ("", *path.relative_to(root).parts):
        current = current / part
        _entry(current)
    return path


def _manifest(root, path, directory=None):
    _owned(root, path)
    if not path.is_file():
        raise ValueError("Index retention requires a regular manifest")
    manifest = read_manifest(path, summary=True)
    if manifest is None:
        raise ValueError("Index retention requires a readable manifest authority")
    value = resolve_index_manifest(manifest, root)
    target = Path(value.get("directory") or "")
    _owned(root, target)
    if target.parent != root / "generations" or not _GENERATION.fullmatch(target.name):
        raise ValueError("Index manifest does not identify a generated directory")
    if directory is not None and target != directory:
        raise ValueError("Index manifest identifies a different generation directory")
    return value


def _references(root, manifest):
    for item in manifest.get("libraries", {}).values():
        for field in _REUSE:
            if field in item:
                path = _owned(root, Path(item[field]))
                if path.parent != root / "generations" or not _GENERATION.fullmatch(path.name):
                    raise ValueError("Index reuse dependency is not a generated directory")
                yield path


def _tree_bytes(root, directory, manifest):
    """Validate the complete disposable tree without following filesystem links."""
    _owned(root, directory)
    keys = {item["key"] for item in manifest.get("libraries", {}).values()}
    root_files = _ROOT_FILES | (_SHARED_FILES if manifest.get("keyword_storage") == SHARED_KEYWORD_STORAGE else set())
    total = 0
    pending = [directory]
    while pending:
        for path in pending.pop().iterdir():
            relative = path.relative_to(directory).parts
            # The walk validated its parent before descending. One lstat is
            # sufficient here; do not resolve every corpus file's ancestors.
            info = _entry(path)
            if stat.S_ISDIR(info.st_mode):
                valid = (len(relative) == 1 and relative[0] in keys) or (
                    len(relative) == 2 and relative[0] in keys and relative[1] == "corpus")
                if valid:
                    pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                valid = ((len(relative) == 1 and relative[0] in root_files)
                         or (len(relative) == 2 and relative[0] in keys and relative[1] in _LIBRARY_FILES)
                         or (len(relative) == 3 and relative[0] in keys and relative[1] == "corpus"
                             and _CORPUS_FILE.fullmatch(relative[2])))
                total += info.st_size
            else:
                valid = False
            if not valid:
                raise ValueError("Unrecognized entry in generated index: " + "/".join(relative))
    if not (directory / "objects.sqlite3").is_file():
        raise ValueError("Generated native index is missing")
    return total


def retire_generation(search_root, directory):
    """Start the reader grace period before switching away from an active index."""
    root = Path(search_root).absolute()
    try:
        path = _owned(root, Path(directory))
        if path.parent != root / "generations" or not _GENERATION.fullmatch(path.name):
            raise ValueError("Retired index is not a generated directory")
        os.utime(path, None)
        return None
    except (OSError, ValueError) as exc:
        # Without a reliable retirement time, this process must retain history.
        _RETIREMENT_FAILURES.add(root)
        return "Could not mark index retirement; history retained: " + str(exc)[:300]


def prune_index_generations(search_root, *, current_directory):
    """Return honest cleanup counts/warnings; never invalidate a completed build."""
    result = {"policy": POLICY, "state": "completed", "removed_generations": 0,
              "removed_bytes": 0, "retained_recent_generations": 0, "warnings": []}
    try:
        root = Path(search_root).absolute()
        if root in _RETIREMENT_FAILURES:
            raise ValueError("A retirement timestamp failed; this process is retaining index history")
        _owned(root, root)
        generations = _owned(root, root / "generations")
        current = _owned(root, Path(current_directory))
        pointers = [_manifest(root, root / name) for name in ("active.json", "last-build.json")]
        if Path(pointers[0]["directory"]) != current:
            raise ValueError("Published index pointers changed; retention skipped")
        manifests, sizes, protected = {}, {}, {current}
        for directory in sorted(generations.iterdir()):
            if not _GENERATION.fullmatch(directory.name):
                result["warnings"].append("Retained unknown generation entry: " + directory.name)
                continue
            _owned(root, directory)
            if not directory.is_dir():
                raise ValueError("Generated index entry is not a directory")
            manifests[directory] = _manifest(root, directory / "manifest.json", directory)
        # A retained generation's aliases are roots too, including aliases in
        # pointer manifests that differ from the generation's stored manifest.
        pending = [Path(item["directory"]) for item in pointers]
        for item in pointers:
            pending.extend(_references(root, item))
        for directory, manifest in manifests.items():
            if directory == current:
                continue
            # Shared keyword queries have one 180-second process deadline.
            # Legacy readers may still spend that budget on every library.
            grace = 180 * (1 if manifest.get("keyword_storage") == SHARED_KEYWORD_STORAGE
                           else max(1, len(manifest.get("libraries", {})))) + 30
            if time.time() - directory.stat().st_mtime < grace:
                protected.add(directory)
                pending.append(directory)
                result["retained_recent_generations"] += 1
                continue
            try:
                sizes[directory] = _tree_bytes(root, directory, manifest)
            except (OSError, ValueError) as exc:
                protected.add(directory)
                pending.append(directory)
                result["warnings"].append("Retained " + directory.name + ": " + str(exc)[:300])
        visited = set()
        while pending:
            directory = pending.pop()
            if directory in visited:
                continue
            visited.add(directory)
            protected.add(directory)
            if directory not in manifests:
                raise ValueError("Index reuse dependency has no readable generation manifest")
            pending.extend(_references(root, manifests[directory]))
        for directory in sorted(set(manifests) - protected):
            try:
                # App writers remain excluded by the caller's build lease.
                # Recheck the deletion root, without walking every file twice.
                shutil.rmtree(_owned(root, directory))
                result["removed_generations"] += 1
                result["removed_bytes"] += sizes[directory]
            except (OSError, ValueError) as exc:
                result["warnings"].append("Could not completely remove " + directory.name
                                          + "; files may remain or be partially removed: " + str(exc)[:300])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result["state"] = "skipped"
        result["warnings"].append("Index retention skipped: " + str(exc)[:300])
    if result["warnings"] and result["state"] == "completed":
        result["state"] = "partial"
    return result
