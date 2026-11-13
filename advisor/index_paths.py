"""Portable search-manifest locators; source and model provenance stay untouched.

Legacy absolute paths are interpreted only through the known generated layout.
They are never used as a fallback, even when the old directory still exists.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

PATH_SCHEMA = "search-root-relative-v1"
SHARED_KEYWORD_STORAGE = "shared-collections-v1"
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_LIBRARY = re.compile(r"lib_[a-f0-9]{20}\Z")
_LIBRARY_PATHS = {"corpus": "corpus", "db_path": "qmd-keyword.sqlite3", "vector_db_path": "qmd-vector.sqlite3"}
_REUSE_DIRECTORIES = ("keyword_reused_from_directory", "vector_reused_from_directory")


def _parts(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("Index locator must be a nonempty path string")
    parts = value.replace("\\", "/").split("/")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("Index locator contains path traversal")
    return tuple(part for part in parts if part)


def _absolute(value):
    return PureWindowsPath(value).is_absolute() or PurePosixPath(value.replace("\\", "/")).is_absolute()


def _legacy_root_and_directory(value):
    parts = _parts(value)
    if not _absolute(value) or len(parts) < 3 or parts[-2] != "generations" or not _SEGMENT.fullmatch(parts[-1]):
        raise ValueError("Legacy index directory must end with generations/<generation-directory>")
    return parts[:-2], parts[-2:]


def _same_parts(left, right, windows):
    return tuple(x.casefold() for x in left) == tuple(x.casefold() for x in right) if windows else left == right


def _inside(root, parts):
    # resolve also rejects existing symlinks/junctions escaping the new root.
    candidate = root.joinpath(*parts)
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("Index locator escapes the current search root")
    return str(candidate)


def resolve_index_manifest(manifest, search_root):
    """Return an absolute runtime copy, without rewriting the stored manifest."""
    if not isinstance(manifest, dict):
        raise ValueError("Index manifest must be an object")
    value = copy.deepcopy(manifest)
    schema = value.get("path_schema")
    if schema not in (None, PATH_SCHEMA):
        raise ValueError("Unsupported index path_schema")
    storage = value.get("keyword_storage")
    if storage not in (None, SHARED_KEYWORD_STORAGE):
        raise ValueError("Unsupported index keyword_storage")
    root = Path(search_root).resolve()
    # A shared keyword database is named by every collection. Check each
    # physical locator once within this conversion, without retaining filesystem
    # decisions between calls (a junction may change before the next query).
    checked = {}

    def inside(parts):
        parts = tuple(parts)
        if parts not in checked:
            checked[parts] = _inside(root, parts)
        return checked[parts]

    directory = value.get("directory")
    libraries = value.get("libraries", {})
    if not isinstance(libraries, dict):
        raise ValueError("Index libraries must be an object")
    if directory is None:
        if libraries:
            raise ValueError("Index libraries require a generation directory")
        return value
    legacy_root = None
    _parts(directory)
    windows = bool(PureWindowsPath(directory).drive)
    if schema is None:
        legacy_root, directory_parts = _legacy_root_and_directory(directory)
    else:
        directory_parts = _parts(directory)
        if _absolute(directory) or len(directory_parts) != 2 or directory_parts[0] != "generations" or not _SEGMENT.fullmatch(directory_parts[1]):
            raise ValueError("Relative index directory must be generations/<generation-directory>")
    value["directory"] = inside(directory_parts)
    for item in libraries.values():
        if not isinstance(item, dict):
            raise ValueError("Each index library must be an object")
        key = item.get("key")
        if not isinstance(key, str) or not _LIBRARY.fullmatch(key):
            raise ValueError("Invalid generated library directory key")
        if any(field not in item for field in ("corpus", "db_path")):
            raise ValueError("Index library requires corpus and db_path locators")
        if storage == SHARED_KEYWORD_STORAGE and item.get("collection") != key:
            raise ValueError("Shared keyword collection must match its generated library key")
        for field, filename in _LIBRARY_PATHS.items():
            if field not in item:
                continue
            expected = ((*directory_parts, filename) if field == "db_path" and storage == SHARED_KEYWORD_STORAGE
                        else (*directory_parts, key, filename))
            actual = _parts(item[field])
            if schema is None:
                if not _absolute(item[field]) or not _same_parts(actual, (*legacy_root, *expected), windows):
                    raise ValueError(f"Legacy {field} does not match its generation/library layout")
            elif _absolute(item[field]) or actual != expected:
                raise ValueError(f"Relative {field} does not match its generation/library layout")
            item[field] = inside(expected)
        for field in _REUSE_DIRECTORIES:
            if field not in item:
                continue
            actual = _parts(item[field])
            if schema is None:
                if not _absolute(item[field]) or not _same_parts(actual[:-2], legacy_root, windows):
                    raise ValueError(f"Legacy {field} has a different search root")
                actual = actual[-2:]
            elif _absolute(item[field]):
                raise ValueError(f"Relative {field} must stay inside the search root")
            if len(actual) != 2 or actual[0] != "generations" or not _SEGMENT.fullmatch(actual[1]):
                raise ValueError(f"Invalid {field} generation directory")
            item[field] = inside(actual)
    # This is a runtime copy. Its absolute locators must not be misinterpreted
    # as serialized relative locators on a second conversion.
    value.pop("path_schema", None)
    return value


def serialize_index_manifest(manifest, search_root):
    """Return the disk form; only explicitly owned index locator fields change."""
    root = Path(search_root).resolve()
    value = resolve_index_manifest(manifest, root)
    if value.get("directory") is not None:
        value["directory"] = Path(value["directory"]).relative_to(root).as_posix()
    for item in value.get("libraries", {}).values():
        for field in (*_LIBRARY_PATHS, *_REUSE_DIRECTORIES):
            if field in item:
                item[field] = Path(item[field]).relative_to(root).as_posix()
    value["path_schema"] = PATH_SCHEMA
    return value
