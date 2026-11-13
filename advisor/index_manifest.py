"""Small generation manifests and local, atomically published root pointers.

Per-object retrieval paths and representation statistics belong to the object
SQLite index. A generation has one JSON authority; active/last-build only name
that authority. Inline legacy manifests remain readable during migration.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .build_control import check_cancelled
from .index_paths import resolve_index_manifest, serialize_index_manifest

MANIFEST_SCHEMA = "sim-advisor-index/v2"
POINTER_SCHEMA = "sim-advisor-index-pointer/v1"
LOOKUP_SCHEMA = "native-object-lookup-v1"
_GENERATION = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_LOCATORS = ("directory", "libraries", "path_schema", "keyword_storage")


def is_manifest_pointer(value):
    return isinstance(value, dict) and value.get("manifest_schema") == POINTER_SCHEMA


def _owned(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Index manifest path escapes its search root")
    current = root
    for part in (None, *path.relative_to(root).parts):
        if part is not None:
            current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("Index manifest refuses symlink/junction paths")
    return path


def pointer_target(path, value):
    """Validate one root pointer; pointer chains and remote targets are forbidden."""
    path = Path(path).absolute()
    if not is_manifest_pointer(value) or path.name not in {"active.json", "last-build.json"}:
        raise ValueError("Index pointers must be active.json or last-build.json")
    locator = value.get("manifest")
    generation = value.get("generation")
    if not isinstance(generation, str) or not generation or len(generation) > 256:
        raise ValueError("Index pointer requires a bounded generation identity")
    parts = locator.split("/") if isinstance(locator, str) else []
    if (len(parts) != 3 or parts[0] != "generations" or parts[2] != "manifest.json"
            or not _GENERATION.fullmatch(parts[1])):
        raise ValueError("Index pointer must name generations/<directory>/manifest.json")
    _owned(path.parent, path)
    return _owned(path.parent, path.parent.joinpath(*parts))


def validate_pointer_authority(pointer_path, pointer, target, authority):
    """Validate the named generation and return its checked runtime locators."""
    if not isinstance(authority, dict) or is_manifest_pointer(authority):
        raise ValueError("Index manifest pointer chains are forbidden")
    if authority.get("generation") != pointer["generation"]:
        raise ValueError("Index pointer and generation manifest identities differ")
    resolved = resolve_index_manifest(
        {key: authority[key] for key in _LOCATORS if key in authority}, Path(pointer_path).parent)
    if not resolved.get("directory") or Path(resolved["directory"]) != Path(target).parent.resolve():
        raise ValueError("Index pointer target describes a different generation directory")
    return resolved


def normalize_modes(value):
    """Forget retired native readiness while retaining all saved QMD locators.

    This is a metadata-only normalization: bridge version, generation, reuse
    dependencies, QMD readiness and embedding records are preserved.
    """
    output = dict(value)
    modes = value.get("modes")
    if isinstance(modes, dict) and "native" in modes:
        output["modes"] = {mode: status for mode, status in modes.items() if mode != "native"}
        keyword = modes.get("qmd_keyword", {})
        if (modes["native"].get("ready") and not keyword.get("ready")
                and keyword.get("state") in {"not_built", "not_requested", None}):
            output["modes"]["qmd_keyword"] = dict(
                keyword, ready=False, state="not_built",
                error="A QMD keyword index is required; build qmd_keyword for this library generation")
        output["ready"] = any(status.get("ready") for status in output["modes"].values()
                              if isinstance(status, dict))
    if isinstance(value.get("requested_modes"), list):
        output["requested_modes"] = [mode for mode in value["requested_modes"] if mode != "native"]
    return output


def compact_manifest(value):
    """Copy only small metadata, omitting the SQLite-owned per-object trees."""
    if not isinstance(value, dict) or is_manifest_pointer(value):
        raise ValueError("A generation manifest must be an object, not a pointer")
    value = normalize_modes(value)
    output = {key: copy.deepcopy(item) for key, item in value.items()
              if key not in {"mapping", "representation"}}
    representation = value.get("representation")
    if representation is not None:
        if not isinstance(representation, dict):
            raise ValueError("Index representation metadata must be an object")
        output["representation"] = {key: copy.deepcopy(item) for key, item in representation.items()
                                    if key != "objects"}
    output["manifest_schema"] = MANIFEST_SCHEMA
    output["object_lookup"] = {"schema": LOOKUP_SCHEMA}
    return output


def stored_manifest(value, search_root):
    """Normalize owned paths without traversing either legacy bulk tree."""
    output = compact_manifest(value)
    output.update(serialize_index_manifest(
        {key: output[key] for key in _LOCATORS if key in output}, search_root))
    return output


def manifest_pointer(value, search_root):
    root = Path(search_root).absolute()
    locators = serialize_index_manifest({key: value[key] for key in _LOCATORS if key in value}, root)
    directory = locators.get("directory")
    if directory is None:
        raise ValueError("Index pointer requires a generation directory")
    pointer = {"manifest_schema": POINTER_SCHEMA, "manifest": directory + "/manifest.json",
               "generation": value.get("generation")}
    pointer_target(root / "active.json", pointer)
    return pointer


def _write_json(path, value, *, cancellable=True):
    if cancellable:
        check_cancelled()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        if cancellable:
            check_cancelled()
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def publish_manifest(directory, value, search_root, *, write_json=None, cancellable=True,
                     active=True, last_build=True):
    """Publish authority first, so every visible pointer has a complete target.

    The caller owns the build lock/lease and has committed the object lookup.
    Root pointer replacements are independently atomic; an interruption between
    them leaves each pointing at a complete generation.
    """
    root = Path(search_root).absolute()
    authority = stored_manifest(value, root)
    pointer = manifest_pointer(authority, root)
    target = pointer_target(root / "active.json", pointer)
    if Path(directory).absolute() != target.parent:
        raise ValueError("Published manifest directory differs from its authority")
    writer = write_json or _write_json
    writer(target, authority, cancellable=cancellable)
    for name, enabled in (("active.json", active), ("last-build.json", last_build)):
        if enabled:
            writer(root / name, pointer, cancellable=cancellable)
    return authority


def compact_existing_index(search_root, *, ensure_lookup, write_json=None):
    """Replace legacy JSON only after SQLite lookup validation succeeds.

    The caller excludes writers with the build lease. ``ensure_lookup`` receives
    (objects.sqlite3, legacy_manifest) and must verify all SQL paths/statistics;
    it may also perform a staged object migration. False means validation failed.
    No source parser, QMD update, embedding, or generation change is performed.
    """
    from .index_manifest_cache import read_manifest

    root = Path(search_root).absolute()
    _owned(root, root)
    generations = _owned(root, root / "generations")
    authorities = {}
    if generations.exists():
        for directory in sorted(generations.iterdir()):
            check_cancelled()
            _owned(root, directory)
            if not directory.is_dir() or not _GENERATION.fullmatch(directory.name):
                raise ValueError("Unexpected search generation; manifest compaction refused")
            path = _owned(root, directory / "manifest.json")
            value = read_manifest(path)
            if value is None:
                # Unpublished interrupted generations have no authority to shrink.
                if path.exists():
                    raise ValueError("Unreadable generation manifest")
                continue
            compact = stored_manifest(value, root)
            pointer = manifest_pointer(compact, root)
            if pointer_target(root / "active.json", pointer) != path:
                raise ValueError("Generation manifest describes a different directory")
            database = _owned(root, directory / "objects.sqlite3")
            if not database.is_file() or ensure_lookup(database, value) is False:
                raise ValueError("Object lookup validation failed")
            authorities[compact["directory"]] = (path, compact)
    pointers = []
    for name in ("active.json", "last-build.json"):
        path = _owned(root, root / name)
        if not path.exists():
            continue
        value = read_manifest(path)
        if value is None:
            raise ValueError("Unreadable root index manifest")
        if value.get("directory") is None:
            continue  # A small failed-build status has no generation authority.
        compact = stored_manifest(value, root)
        authority = authorities.get(compact["directory"])
        if authority is None or authority[1] != compact:
            raise ValueError("Root manifest differs from its generation authority")
        pointers.append((path, manifest_pointer(compact, root)))
    writer = write_json or _write_json
    # Complete all validation before replacing any JSON. Legacy root copies are
    # still usable if interrupted while publishing the authorities.
    for path, compact in authorities.values():
        writer(path, compact)
    for path, pointer in pointers:
        writer(path, pointer)
    return {"generations": len(authorities), "pointers": len(pointers)}
