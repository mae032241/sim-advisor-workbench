"""Irreversible cleanup of one registered library's retained search indexes.

The caller must authorize/soft-delete the library and retain its catalog row
until this function succeeds. A small retry journal covers filesystem/SQLite
boundaries; it is not an embedding checkpoint. Independent corpora and original
source files are outside this module's scope.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from .build_control import check_cancelled
from .index_paths import SHARED_KEYWORD_STORAGE, resolve_index_manifest, serialize_index_manifest
from .index_manifest_cache import read_manifest
from .index_manifest import compact_manifest, manifest_pointer
from . import native_index
from . import qmd_shared
from .library_search import LibrarySearch, _NODE_BRIDGE, _hash, _json, _write_json, _corpus_hash

JOURNAL_SCHEMA = "registered-library-index-purge/v1"
_GENERATION = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_LIBRARY_KEY = re.compile(r"lib_[a-f0-9]{20}\Z")
_MANIFEST_LOCATORS = ("directory", "libraries", "path_schema", "keyword_storage")
_OWNED_GENERATION = re.compile(r"[a-f0-9]{20}-[a-f0-9]{8}\Z")
_NATIVE_FILES = {"objects.sqlite3" + suffix for suffix in ("", "-wal", "-shm", "-journal")}
_NATIVE_TABLES = {"objects", "object_fts", "object_fts_data", "object_fts_idx", "object_fts_content", "object_fts_docsize", "object_fts_config"}
_NATIVE_SQL = {
    "objects": "CREATE TABLE objects(id TEXT PRIMARY KEY, library_id TEXT NOT NULL, title TEXT, text TEXT, object_json TEXT NOT NULL)",
    "object_fts": "CREATE VIRTUAL TABLE object_fts USING fts5(id UNINDEXED,title,text,cjk,tokenize='porter unicode61')",
}
_SHARED_FILES = {"qmd-keyword.sqlite3" + suffix for suffix in ("", "-wal", "-shm", "-journal")}
_SHARED_PROGRESS_FILES = {"qmd-keyword.progress.json", "qmd-keyword.sqlite3.progress.json"}
_EMPTY_GENERATION_FILES = {"manifest.json", *_NATIVE_FILES, *_SHARED_FILES, *_SHARED_PROGRESS_FILES}
_REUSE_DIRECTORIES = ("keyword_reused_from_directory", "vector_reused_from_directory")


def _owned(root, path):
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        raise ValueError("Index purge path escapes the selected search root")
    for part in path.relative_to(root).parts:
        if part in {".", ".."}:
            raise ValueError("Index purge path contains traversal")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("Index purge refuses symlink/junction index artifacts")
    return path


def _read_manifest(path, root, cache=None):
    if cache is not None and path in cache:
        return cache[path]
    if not path.exists():
        return None
    _owned(root, path)
    try:
        value = copy.deepcopy(read_manifest(path))
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot safely purge an unreadable index manifest") from exc
    if not isinstance(value, dict):
        raise ValueError("Index manifest must be an object")
    # Path conversion validates/copies only these owned locators. Representation
    # and mapping trees can contain tens of thousands of records per generation.
    value.update(resolve_index_manifest({key: value[key] for key in _MANIFEST_LOCATORS if key in value}, root))
    value.pop("path_schema", None)
    if cache is not None:
        cache[path] = value
    return value


def _manifest_paths(root):
    paths = [root / "active.json", root / "last-build.json"]
    generations = _owned(root, root / "generations")
    if generations.exists():
        for directory in sorted(generations.iterdir()):
            if not _GENERATION.fullmatch(directory.name) or not directory.is_dir():
                raise ValueError("Unexpected entry in search generations; purge refused")
            paths.append(_owned(root, directory) / "manifest.json")
    return paths


def _plan(root, library_id, generation, journal, cache=None):
    previous = {}
    if journal.exists():
        value = json.loads(journal.read_text(encoding="utf-8"))
        if value.get("schema") != JOURNAL_SCHEMA or value.get("library_id") != library_id:
            raise RuntimeError("Another incomplete index purge must be retried first")
        if not isinstance(value.get("entries"), dict):
            raise ValueError("Invalid index purge retry journal")
        previous = value["entries"]
    entries = copy.deepcopy(previous)
    for entry in entries.values():
        # Root pointers may have advanced since a failed attempt. Only attach
        # them to the generation they currently describe, never the old one.
        entry["manifests"] = [name for name in entry["manifests"] if name not in {"active.json", "last-build.json"}]
    active_directory = None
    for path in _manifest_paths(root):
        manifest = _read_manifest(path, root, cache)
        if not manifest or manifest.get("directory") is None:
            continue
        directory = _owned(root, manifest["directory"])
        if path == root / "active.json":
            active_directory = directory.relative_to(root).as_posix()
        entry = entries.setdefault(directory.relative_to(root).as_posix(), {"keys": [], "manifests": []})
        entry["manifests"] = sorted(set([*entry["manifests"], path.relative_to(root).as_posix()]))
        if manifest.get("keyword_storage") == SHARED_KEYWORD_STORAGE:
            entry["shared_storage"] = True
        libraries = manifest.get("libraries", {})
        target = libraries.get(library_id)
        if target:
            key = target["key"]
            if any(item.get("key") == key for lid, item in libraries.items() if lid != library_id):
                raise ValueError("Libraries share a physical QMD directory; purge refused")
            entry["keys"].append(key)
            if manifest.get("keyword_storage") == SHARED_KEYWORD_STORAGE:
                entry.setdefault("shared_collections", []).append(target["collection"])
    # Interrupted generations may have no manifest yet. Their library directory
    # still follows the exporter's deterministic key and their native rows retain
    # an explicit library_id. Do not infer ownership from content hashes.
    generations = root / "generations"
    if generations.exists():
        for directory in sorted(generations.iterdir()):
            relative = _owned(root, directory).relative_to(root).as_posix()
            entries.setdefault(relative, {"keys": [], "manifests": []})
    expected_key = "lib_" + _hash(library_id)[:20]
    for relative, entry in entries.items():
        # Old retry journals predate this flag and may follow a committed
        # DELETE whose VACUUM failed. They must compact before claiming success.
        entry.setdefault("native_pending", relative in previous)
        entry["keys"] = sorted(set([*entry["keys"], expected_key]))
        for key in entry["keys"]:
            if not _LIBRARY_KEY.fullmatch(key):
                raise ValueError("Invalid generated library directory in purge plan")
            _owned(root, root / relative / key)
        # An interrupted build may have persisted the shared QMD database
        # before publishing any manifest. Collection identities still use the
        # exporter's deterministic library key; the QMD helper validates its
        # complete schema before deleting any rows.
        collections = entry.setdefault("shared_collections", [])
        if not isinstance(collections, list):
            raise ValueError("Invalid shared keyword collections in purge plan")
        if ((root / relative / "qmd-keyword.sqlite3").exists()
                and (entry.get("shared_storage") is True or not entry["manifests"])):
            entry["shared_storage"] = True
            collections.append(expected_key)
        entry["shared_collections"] = sorted(set(collections))
        entry.setdefault("shared_pending", [])
        if (not isinstance(entry["shared_pending"], list)
                or any(not isinstance(key, str) or not _LIBRARY_KEY.fullmatch(key)
                       for key in [*entry["shared_collections"], *entry["shared_pending"]])
                or not set(entry["shared_collections"]).issubset(entry["keys"])
                or not set(entry["shared_pending"]).issubset(entry["shared_collections"])):
            raise ValueError("Invalid shared keyword collection in purge plan")
    value = {"schema": JOURNAL_SCHEMA, "library_id": library_id, "generation": generation,
             "active_directory": active_directory, "entries": entries}
    _write_json(journal, value)
    return value


def _clean_native(path, library_id, *, load_objects=True, force_compact=False, on_dirty=None, catalog_path=None):
    if not path.exists():
        return {"removed": 0, "objects": None, "ids": set()}
    if path.stat().st_nlink > 1:
        raise ValueError("Native index has shared hard links; purge refused")
    with closing(sqlite3.connect(path, timeout=5)) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "objects" not in names:
            if names:
                raise ValueError("Unrecognized native index schema; purge refused")
            return {"removed": 0, "objects": [], "ids": set(), "empty": True, "owned_schema": False}
        if "object_fts" not in names:
            raise ValueError("Native index FTS table is missing; purge refused")
        compact = native_index.is_compact(db)
        ids = {row[0] for row in db.execute("SELECT id FROM objects WHERE library_id=?", (library_id,))}
        if ids or force_compact:
            if on_dirty:
                on_dirty()
            db.execute("PRAGMA secure_delete=ON")
            with db:
                db.execute("BEGIN IMMEDIATE")
                if compact:
                    native_index.delete_library(db, library_id)
                else:
                    db.execute("DELETE FROM object_fts WHERE id IN (SELECT id FROM objects WHERE library_id=?)", (library_id,))
                    db.execute("DELETE FROM objects WHERE library_id=?", (library_id,))
                # Required for generations that actually contained this library:
                # stale FTS postings/free pages must not retain deleted payloads.
                db.execute("INSERT INTO object_fts(object_fts) VALUES('optimize')")
            db.execute("VACUUM")
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise RuntimeError("Native index WAL could not be cleared; retry the purge after readers finish")
        empty = db.execute("SELECT 1 FROM objects LIMIT 1").fetchone() is None
        # An empty payload does not establish ownership of unrelated SQLite
        # tables, views, or triggers. Such a database is never disposable.
        schema = {(row[0], row[1]) for row in db.execute("SELECT type,name FROM sqlite_master")}
        definitions = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name IN ('objects','object_fts')"))
        owned_schema = (names == _NATIVE_TABLES and schema == {
            *(("table", name) for name in _NATIVE_TABLES), ("index", "sqlite_autoindex_objects_1")}
            and all(" ".join(definitions.get(name, "").split()).casefold() == " ".join(sql.split()).casefold()
                    for name, sql in _NATIVE_SQL.items())
            and [row[1] for row in db.execute("PRAGMA table_info(objects)")] == ["id", "library_id", "title", "text", "object_json"]
            and [row[1] for row in db.execute("PRAGMA table_info(object_fts)")] == ["id", "title", "text", "cjk"])
        if compact:
            owned_schema = native_index.owned_schema(db)
        if not ids and not force_compact and not load_objects:
            return {"removed": 0, "objects": None, "ids": set(), "unchanged": True,
                    "empty": empty, "owned_schema": owned_schema}
        if compact:
            identities = [row[0] for row in db.execute("SELECT id FROM objects ORDER BY id")]
            source_catalog = native_index.catalog_connection(catalog_path) if catalog_path and Path(catalog_path).is_file() else None
            try:
                fingerprint = _corpus_hash(native_index.hydrate_object(db, identity, catalog_path=catalog_path, catalog_db=source_catalog) for identity in identities)
            finally:
                if source_catalog is not None:
                    source_catalog.close()
            indexed = truncated = 0
            for (raw,) in db.execute("SELECT representation_json FROM objects"):
                check_cancelled()
                stats = native_index.unpack(raw)
                indexed += stats.get("indexed_characters", 0)
                truncated += int(bool(stats.get("truncated")))
            # Manifest cleanup needs identities/counts, never retained full
            # source objects. Hash authoritative payloads one revision wide.
            return {"removed": len(ids), "objects": [{"id": identity} for identity in identities], "ids": ids,
                    "empty": empty, "owned_schema": owned_schema, "remaining_ids": set(identities),
                    "corpus_hash": fingerprint, "indexed_generation": _hash(_json(identities))[:24],
                    "representation_summary": {"indexed_objects": len(identities), "unindexed_objects": 0,
                                               "indexed_characters": indexed, "truncated_objects": truncated}}
        objects = [json.loads(row[0]) for row in db.execute("SELECT object_json FROM objects ORDER BY id")]
        if any(item.get("library_id") == library_id for item in objects):
            raise RuntimeError("Native index still contains the purged library")
    return {"removed": len(ids), "objects": objects, "ids": ids,
            "empty": empty, "owned_schema": owned_schema,
            "remaining_ids": {item["id"] for item in objects},
            "corpus_hash": _hash(_json(objects)),
            "indexed_generation": _hash(_json(sorted(item["id"] for item in objects)))[:24]}


def _clean_shared_keyword(root, directory, entry, plan, journal):
    if entry.get("shared_storage") is not True:
        return {"empty": False, "owned_schema": False, "reclaimed_bytes": 0}
    path = _owned(root, directory / "qmd-keyword.sqlite3")
    for name in _SHARED_FILES:
        artifact = _owned(root, directory / name)
        if artifact.exists():
            info = artifact.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise ValueError("Shared keyword index has unowned or shared files; purge refused")
    if not path.exists():
        # A previous, validated empty-generation removal may have unlinked
        # SQLite before a sidecar unlink failed. Its durable journal is the
        # authority for resuming that removal, never the sidecar's name alone.
        return {"empty": entry.get("shared_empty") is True,
                "owned_schema": entry.get("shared_empty") is True, "reclaimed_bytes": 0}
    result = {"empty": False, "owned_schema": False, "reclaimed_bytes": 0}
    for collection in entry["shared_collections"]:
        retry = collection in entry["shared_pending"]
        if not retry:
            entry["shared_pending"].append(collection)
            _write_json(journal, plan)
        cleaned = qmd_shared.purge_collection(path, collection, force_compact=retry)
        result.update(empty=cleaned.get("empty") is True, owned_schema=cleaned.get("owned_schema") is True)
        result["reclaimed_bytes"] += max(0, cleaned.get("reclaimed_bytes", 0))
        entry["shared_empty"] = result["empty"] and result["owned_schema"]
        entry["shared_pending"].remove(collection)
        _write_json(journal, plan)
    return result


def _affected(value, library_id, keys):
    if library_id in value.get("libraries", {}):
        return True
    if any(path.split("/", 1)[0] in keys for path in value.get("mapping", {})):
        return True
    return any(library_id in mode.get(field, {}) for mode in value.get("modes", {}).values()
               for field in ("embedding_records", "progress") if isinstance(mode.get(field), dict))


def _clean_manifest(value, library_id, keys, native, *, active, generation):
    # Each cached manifest is owned by this purge under the search OS lease.
    # Updating it in place avoids copying multi-MB representation maps again.
    removed_ids = set(native["ids"])
    mapping = value.get("mapping", {})
    remaining_ids = native.get("remaining_ids", set())
    if any(path.split("/", 1)[0] in keys and sid in remaining_ids for path, sid in mapping.items()):
        raise ValueError("Purged library mappings point to another library's retained objects")
    for path, sid in list(mapping.items()):
        if path.split("/", 1)[0] in keys or sid in removed_ids:
            removed_ids.add(sid)
            del mapping[path]
    value.get("libraries", {}).pop(library_id, None)
    representation = value.get("representation", {})
    for sid in removed_ids:
        representation.get("objects", {}).pop(sid, None)
    objects = native["objects"]
    if objects is not None:
        value["object_count"] = len(objects)
        value["corpus_hash"] = native.get("corpus_hash") or _hash(_json(objects))
        indexed_generation = native.get("indexed_generation") or _hash(_json(sorted(item["id"] for item in objects)))[:24]
        if active and indexed_generation == generation:
            value["generation"] = generation
        if representation:
            stats = representation.get("objects", {})
            representation.update(object_count=len(objects), indexed_objects=len(stats), unindexed_objects=max(0, len(objects) - len(stats)),
                                  indexed_characters=sum(item.get("indexed_characters", 0) for item in stats.values()),
                                  truncated_objects=sum(bool(item.get("truncated")) for item in stats.values()))
            if native.get("representation_summary") is not None:
                representation.update(native["representation_summary"])
    for mode in value.get("modes", {}).values():
        for field in ("embedding_records", "progress"):
            if isinstance(mode.get(field), dict):
                mode[field].pop(library_id, None)
        if objects is None:
            mode.update(ready=False, state="unavailable", error="Native source-object index is missing")
        elif not objects:
            mode.update(ready=False, state="empty")
    value["ready"] = any(mode.get("ready") for mode in value.get("modes", {}).values())
    return value


def _remove_library_directory(root, path, *, validate_only=False, validated_bytes=None):
    path = _owned(root, path)
    if not _LIBRARY_KEY.fullmatch(path.name) or path.parent.parent != root / "generations":
        raise ValueError("Refusing recursive removal outside a generated library directory")
    if not path.exists():
        return 0
    if not path.is_dir():
        raise ValueError("Generated library index path is not a directory")
    total = validated_bytes
    if total is None:
        total = 0
        pending = [path]
        while pending:
            with os.scandir(pending.pop()) as entries:
                for child in entries:
                    info = child.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        raise ValueError("Index purge refuses symlink/junction index artifacts")
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(child.path)
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
                    else:
                        raise ValueError("Unexpected special file in generated library index")
    if validate_only:
        return total
    # _owned verifies the final absolute target immediately before recursion.
    shutil.rmtree(_owned(root, path))
    return total


def _empty_manifest(value):
    representation = value.get("representation", {})
    return (value.get("object_count") == 0 and value.get("libraries") == {}
            and value.get("mapping", {}) == {} and isinstance(representation, dict)
            and representation.get("objects", {}) == {})


def _empty_inventory(root, directory):
    """Inventory only exporter-owned generation files; never recurse here."""
    _owned(root, directory)
    if not directory.exists():
        return {}
    if not directory.is_dir() or not _OWNED_GENERATION.fullmatch(directory.name):
        return None
    inventory = {}
    for path in directory.iterdir():
        _owned(root, path)
        info = path.lstat()
        if (path.name not in _EMPTY_GENERATION_FILES or not stat.S_ISREG(info.st_mode)
                or info.st_nlink > 1 or getattr(info, "st_file_attributes", 0) & 0x400):
            return None
        inventory[path.name] = info.st_size
    return inventory


def _reclaim_empty_generations(root, plan, journal, cache, native_results, summary):
    # Retained libraries can refer to a historical generation. Keep those
    # dependencies even if their native object payload happens to be empty.
    referenced = set()
    for value in cache.values():
        for library in value.get("libraries", {}).values():
            for field in _REUSE_DIRECTORIES:
                if library.get(field):
                    referenced.add(_owned(root, library[field]))
    for relative, entry in plan["entries"].items():
        check_cancelled()
        directory = _owned(root, root / relative)
        native = native_results[relative]
        retry = entry.get("empty_removal") is True
        if directory in referenced or not (native.get("empty") or (retry and not (directory / "objects.sqlite3").exists())):
            continue
        manifests = []
        for name in entry["manifests"]:
            path = _owned(root, root / name)
            value = _read_manifest(path, root, cache)
            if value is not None:
                if value.get("directory") != str(directory) or not _empty_manifest(value):
                    break
                manifests.append(path)
        else:
            inventory = _empty_inventory(root, directory)
            manifest = cache.get(directory / "manifest.json")
            if (inventory is None or ("objects.sqlite3" in inventory and not native.get("owned_schema"))
                    or (any(name in inventory for name in _SHARED_FILES | _SHARED_PROGRESS_FILES)
                        and not (native.get("shared_empty") and native.get("shared_owned_schema")))
                    or (not retry and (manifest is None or not _empty_manifest(manifest)))):
                summary["warnings"].append("Retained empty index with unrecognized ownership or contents: " + relative)
                continue
            # Commit ownership/emptiness before removing any metadata. A crash
            # may leave only sidecars or the directory, all retried under this
            # same OS lease. Never remove build.lock or its parent search root.
            entry["empty_removal"] = True
            _write_json(journal, plan)
            for path in manifests:
                if path.parent == root and path.exists():
                    size = path.stat().st_size
                    _owned(root, path).unlink()
                    summary["search_metadata_bytes_removed"] += size
                    cache.pop(path, None)
                    if path == root / "active.json":
                        summary["active_generation_retargeted"] = False
            # Unlink the database first. A Windows reader may refuse this; the
            # journal remains and prevents reporting a completed purge. All
            # stale readers of this empty generation belong to the purged
            # library; retained libraries/dependencies were excluded above.
            names = sorted(inventory, key=lambda name: (name not in {"objects.sqlite3", "qmd-keyword.sqlite3"}, name))
            for name in names:
                path = _owned(root, directory / name)
                path.unlink()
                summary["search_metadata_bytes_removed"] += inventory[name]
                cache.pop(path, None)
            if directory.exists():
                _owned(root, directory).rmdir()
                summary["empty_generations_removed"] += 1
    generations = _owned(root, root / "generations")
    if generations.exists() and not any(generations.iterdir()):
        generations.rmdir()
    # Runtime bridges are generated lazily and carry no user configuration.
    # Exact source/name verification retains custom scripts and other corpora.
    if generations.exists() or any((root / name).exists() for name in ("active.json", "last-build.json")):
        return
    runtime = _owned(root, root / "runtime")
    if runtime.is_dir():
        for prefix, source in (("qmd_bridge_", _NODE_BRIDGE), ("qmd_shared_", qmd_shared._WORKER)):
            script = _owned(root, runtime / (prefix + _hash(source)[:16] + ".mjs"))
            if script.is_file() and script.stat().st_nlink == 1 and script.read_text(encoding="utf-8") == source:
                size = script.stat().st_size
                script.unlink()
                summary["runtime_bytes_removed"] += size
        if not any(runtime.iterdir()):
            runtime.rmdir()


def purge_library_indexes(search_root, library_id, *, generation, on_progress=None):
    """Purge current/historical registered indexes; preserve other QMD bytes.

    ``generation`` is the caller's desired generation after excluding the library.
    A retained current index is retargeted only when its remaining immutable IDs
    exactly match that generation. Previously failed/stale modes are not upgraded.
    """
    if not isinstance(library_id, str) or not library_id or len(library_id) > 256:
        raise ValueError("library_id must be a nonempty bounded string")
    if not isinstance(generation, str) or not generation or len(generation) > 256:
        raise ValueError("generation must be a nonempty bounded string")
    root = Path(search_root).resolve()
    summary = {"scope": "registered-library-search-indexes-current-and-historical", "library_id": library_id,
               "generations_cleaned": 0, "native_objects_removed": 0, "library_index_bytes_removed": 0,
               "empty_generations_removed": 0, "search_metadata_bytes_removed": 0, "runtime_bytes_removed": 0, "warnings": [],
               "active_generation_retargeted": False, "embedding_invoked": False,
               "independent_corpora_untouched": True, "original_sources_untouched": True}
    if not root.exists():
        return summary
    search = LibrarySearch.for_maintenance(root)
    if not search.lock.acquire(blocking=False):
        raise RuntimeError("Another library index build or purge is active")
    try:
        handle, token = search._acquire_lease(generation)
    except Exception:
        search.lock.release()
        raise
    journal = root / "purge-pending.json"
    try:
        check_cancelled()
        cache = {}
        plan = _plan(root, library_id, generation, journal, cache)
        # A retry may follow a catalog change; use today's caller-supplied
        # generation for the readiness comparison, not the old journal value.
        total_generations = len(plan["entries"])
        native_results = {}
        for completed, (relative, entry) in enumerate(plan["entries"].items()):
            check_cancelled()
            if on_progress:
                on_progress({"stage": "indexes", "completed": completed, "total": total_generations, "unit": "generations"})
            directory = _owned(root, root / relative)
            if directory.parent != root / "generations" or not _GENERATION.fullmatch(directory.name):
                raise ValueError("Invalid generation in purge journal")
            keys = set(entry["keys"])
            if any(not _LIBRARY_KEY.fullmatch(key) for key in keys):
                raise ValueError("Invalid library key in purge journal")
            allowed_manifests = {root / "active.json", root / "last-build.json", directory / "manifest.json"}
            for name in entry["manifests"]:
                path = _owned(root, root / name)
                if path not in allowed_manifests:
                    raise ValueError("Invalid manifest path in purge journal")
                value = _read_manifest(path, root, cache)
                if value and value.get("directory") == str(directory):
                    if any(item.get("key") in keys for lid, item in value.get("libraries", {}).items() if lid != library_id):
                        raise ValueError("Purge key belongs to another retained library")
            inventory = {key: _remove_library_directory(root, directory / key, validate_only=True) for key in keys}
            affected = {name: _affected(cache.get(root / name) or {}, library_id, keys) for name in entry["manifests"]}
            def mark_native_pending():
                entry["native_pending"] = True
                _write_json(journal, plan)

            native = _clean_native(_owned(root, directory / "objects.sqlite3"), library_id,
                                   load_objects=any(affected.values()), force_compact=entry["native_pending"],
                                   on_dirty=mark_native_pending, catalog_path=root.parent / "catalog.sqlite3")
            native_results[relative] = {field: native.get(field) for field in ("empty", "owned_schema")}
            if entry["native_pending"]:
                entry["native_pending"] = False
                _write_json(journal, plan)
            summary["native_objects_removed"] += native["removed"]
            shared = _clean_shared_keyword(root, directory, entry, plan, journal)
            native_results[relative].update(shared_empty=shared["empty"], shared_owned_schema=shared["owned_schema"])
            summary["library_index_bytes_removed"] += shared["reclaimed_bytes"]
            for name in entry["manifests"]:
                path = _owned(root, root / name)
                value = _read_manifest(path, root, cache)
                if value is None:
                    continue
                if value.get("directory") != str(directory):
                    raise ValueError("Purge manifest changed generation while cleanup was pending")
                current = relative == plan.get("active_directory")
                if not native["removed"] and not affected[name]:
                    if current and path == root / "active.json":
                        summary["active_generation_retargeted"] = value.get("generation") == generation
                    continue
                value = _clean_manifest(value, library_id, keys, native, active=current, generation=generation)
                stored = dict(value)
                if native.get("representation_summary") is not None:
                    stored = compact_manifest(stored)
                stored.update(serialize_index_manifest({key: value[key] for key in _MANIFEST_LOCATORS if key in value}, root))
                if path in {root / "active.json", root / "last-build.json"} and stored.get("object_lookup"):
                    # Publish the authority before replacing its root pointer.
                    _write_json(directory / "manifest.json", stored)
                    stored = manifest_pointer(value, root)
                _write_json(path, stored)
                if current and path == root / "active.json":
                    summary["active_generation_retargeted"] = value.get("generation") == generation
            # Manifests no longer reference these paths, keeping other libraries
            # usable even if a locked target QMD file requires a retry.
            for key in keys:
                summary["library_index_bytes_removed"] += _remove_library_directory(root, directory / key, validated_bytes=inventory[key])
            summary["generations_cleaned"] += 1
        _reclaim_empty_generations(root, plan, journal, cache, native_results, summary)
        if on_progress:
            on_progress({"stage": "indexes", "completed": total_generations, "total": total_generations, "unit": "generations"})
        journal.unlink()
        return summary
    finally:
        try:
            search._release_lease(handle, token)
        finally:
            search.lock.release()
