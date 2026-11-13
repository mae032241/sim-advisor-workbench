"""Shared object metadata and exact revision lookup for QMD retrieval.

New databases contain no search postings. Legacy native FTS is recognized only
for migration and deletion; QMD owns all keyword and vector retrieval.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
import zlib

from .build_control import check_cancelled
from .case_metadata import object_feature_branch

SCHEMA = "object-index/v2"
V1_SCHEMA = "object-index/v1"
LEGACY_SCHEMA = "native-objects/v2"
LOOKUP_SCHEMA = "native-object-lookup-v1"
OBJECT_SQL = ("CREATE TABLE objects(id TEXT PRIMARY KEY, library_id TEXT NOT NULL, "
              "kind TEXT, feature_branch TEXT, qmd_path TEXT UNIQUE NOT NULL, "
              "representation_json BLOB NOT NULL, source_json BLOB)")
V1_OBJECT_SQL = ("CREATE TABLE objects(id TEXT PRIMARY KEY, library_id TEXT NOT NULL, "
                 "kind TEXT, feature_branch TEXT, qmd_path TEXT UNIQUE NOT NULL, "
                 "representation_json BLOB NOT NULL, search_input BLOB NOT NULL, source_json BLOB)")
LEGACY_FTS_SQL = "CREATE VIRTUAL TABLE object_fts USING fts5(id UNINDEXED,title,text,cjk,content='',tokenize='porter unicode61')"
_REPRESENTATION_MAGIC = b"RS1"
_REPRESENTATION_DEFAULTS = {
    "policy": "bounded-object-with-labelled-inferred-annotation-v1",
    "original_text_characters": 0, "original_data_characters": 0,
    "indexed_characters": 0, "body_characters_retained": 0,
    "truncated": False, "annotation_included": False,
    "annotation_is_observed": False, "annotation_fields": [],
    "feature_structure_included": False, "character_budget": 1400,
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def pack(value):
    return zlib.compress(_json(value).encode("utf-8"), 6)


def unpack(value):
    if isinstance(value, bytes) and value.startswith(_REPRESENTATION_MAGIC):
        return decode_representation(value)
    return json.loads(zlib.decompress(value))


def encode_representation(stats):
    """Store real statistics as a versioned short array, omitting defaults.

    Presence is separate from overrides so absent fields and explicit defaults
    round-trip exactly. Unknown extension fields retain their original names.
    """
    if not isinstance(stats, dict):
        raise ValueError("Object representation statistics must be an object")
    presence = overrides = 0
    values = []
    for index, (key, default) in enumerate(_REPRESENTATION_DEFAULTS.items()):
        if key in stats:
            presence |= 1 << index
            if type(stats[key]) is not type(default) or stats[key] != default:
                overrides |= 1 << index
                values.append(stats[key])
    body = [presence, overrides, values]
    extra = {key: value for key, value in stats.items() if key not in _REPRESENTATION_DEFAULTS}
    if extra:
        body.append(extra)
    return _REPRESENTATION_MAGIC + _json(body).encode("utf-8")


def decode_representation(raw):
    """Read current short arrays or historical compressed JSON dictionaries."""
    if not isinstance(raw, bytes) or not raw.startswith(_REPRESENTATION_MAGIC):
        result = unpack(raw)
        if not isinstance(result, dict):
            raise ValueError("Object representation statistics must be an object")
        return result
    body = json.loads(raw[len(_REPRESENTATION_MAGIC):])
    bits = (1 << len(_REPRESENTATION_DEFAULTS)) - 1
    if (not isinstance(body, list) or len(body) not in (3, 4)
            or type(body[0]) is not int or type(body[1]) is not int
            or body[0] < 0 or body[1] < 0 or body[0] & ~bits or body[1] & ~body[0]
            or not isinstance(body[2], list) or len(body[2]) != body[1].bit_count()
            or (len(body) == 4 and (not isinstance(body[3], dict)
                                   or set(body[3]) & _REPRESENTATION_DEFAULTS.keys()))):
        raise ValueError("Invalid compact object representation statistics")
    values = iter(body[2])
    result = {}
    for index, (key, default) in enumerate(_REPRESENTATION_DEFAULTS.items()):
        if body[0] & (1 << index):
            result[key] = next(values) if body[1] & (1 << index) else ([] if isinstance(default, list) else default)
    if len(body) == 4:
        result.update(body[3])
    return result


def is_compact(db):
    return "source_json" in {row[1] for row in db.execute("PRAGMA table_info(objects)")}


def is_slim(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(objects)")}
    return "source_json" in columns and "search_input" not in columns


def _legacy_feature_branch(explicit, top_level, hint, object_type, section, title):
    # Only projected scalar metadata crosses SQLite's boundary. Historical
    # indexes need neither whole payload decoding nor a migration for scopes.
    return object_feature_branch({"feature_branch": top_level, "type": object_type,
                                  "title": title, "data": {"feature_branch": explicit,
                                  "scope_hint": hint, "section": section}})


def register_scope_functions(db):
    db.create_function("legacy_feature_branch", 6, _legacy_feature_branch, deterministic=True)


def scope_predicate(*, compact, library_ids, kind="all", feature_branch=None, alias="o"):
    """Build a parameterized authoritative scope without materializing IDs."""
    libraries = list(library_ids)
    clauses = [f"{alias}.library_id IN ({','.join('?' for _ in libraries)})"]
    params = libraries[:]
    if kind != "all":
        value = f"{alias}.kind" if compact else f"json_extract({alias}.object_json,'$.kind')"
        clauses.append(value + "=?")
        params.append(kind)
    if feature_branch is not None:
        value = (f"{alias}.feature_branch" if compact else "legacy_feature_branch(" + ",".join(
            f"json_extract({alias}.object_json,'{path}')" for path in
            ("$.data.feature_branch", "$.feature_branch", "$.data.scope_hint", "$.type", "$.data.section", "$.title")) + ")")
        clauses.append(value + "=?")
        params.append(feature_branch)
    return " AND ".join(clauses), params


def create_schema(db):
    """Create shared metadata only; keyword postings belong to QMD."""
    db.execute(OBJECT_SQL)
    db.execute("CREATE TABLE object_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.execute("INSERT INTO object_metadata VALUES('schema',?)", (SCHEMA,))
    db.execute("CREATE INDEX objects_scope ON objects(library_id,kind,feature_branch)")


LEGACY_OBJECT_SQL = "CREATE TABLE objects(id TEXT PRIMARY KEY, library_id TEXT NOT NULL, title TEXT, text TEXT, object_json TEXT NOT NULL)"
LEGACY_CONTENT_FTS_SQL = "CREATE VIRTUAL TABLE object_fts USING fts5(id UNINDEXED,title,text,cjk,tokenize='porter unicode61')"
_FTS_SHADOWS = {"object_fts", "object_fts_data", "object_fts_idx", "object_fts_docsize", "object_fts_config"}


def has_legacy_fts(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='object_fts'").fetchone() is not None


def owned_schema(db):
    """Recognize exactly our metadata schemas, with optional historical FTS."""
    definitions = dict(db.execute("SELECT name,sql FROM sqlite_master"))
    actual = {(row[0], row[1]) for row in db.execute("SELECT type,name FROM sqlite_master")}
    normalize = lambda sql: " ".join((sql or "").split()).casefold()
    compact = is_compact(db)
    tables, indexes = {"objects"}, {"sqlite_autoindex_objects_1"}
    slim = is_slim(db) if compact else False
    expected_objects = (OBJECT_SQL if slim else V1_OBJECT_SQL) if compact else LEGACY_OBJECT_SQL
    if normalize(definitions.get("objects")) != normalize(expected_objects):
        return False
    if compact:
        candidates = [("object_metadata", SCHEMA)] if slim else [("object_metadata", V1_SCHEMA), ("native_metadata", LEGACY_SCHEMA)]
        metadata = next(((name, version) for name, version in candidates if name in definitions), None)
        if metadata is None:
            return False
        name, version = metadata
        if normalize(definitions[name]) != normalize("CREATE TABLE " + name + "(key TEXT PRIMARY KEY,value TEXT NOT NULL)"):
            return False
        if db.execute("SELECT value FROM " + name + " WHERE key='schema'").fetchone() != (version,):
            return False
        tables.add(name)
        indexes.update({"sqlite_autoindex_objects_2", "sqlite_autoindex_" + name + "_1", "objects_scope"})
        if normalize(definitions.get("objects_scope")) != normalize("CREATE INDEX objects_scope ON objects(library_id,kind,feature_branch)"):
            return False
    if has_legacy_fts(db):
        if slim:
            return False  # Current metadata has no token copies for legacy FTS.
        tables.update(_FTS_SHADOWS)
        if not compact:
            tables.add("object_fts_content")
        expected_fts = LEGACY_FTS_SQL if compact else LEGACY_CONTENT_FTS_SQL
        if normalize(definitions.get("object_fts")) != normalize(expected_fts):
            return False
    return actual == {*(("table", name) for name in tables), *(("index", name) for name in indexes)}


def qmd_path(item):
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    return "lib_" + digest(item["library_id"])[:20] + "/o-" + digest(item["id"])[:32] + ".md"


def insert_object(db, item, text, stats, cjk, *, standalone=True, rowid=None, path=None):
    """Persist metadata only; callers keep build text in a temporary QMD spool."""
    columns = "id,library_id,kind,feature_branch,qmd_path,representation_json,source_json"
    values = [item["id"], item["library_id"], item.get("kind"), object_feature_branch(item),
              path or qmd_path(item), encode_representation(stats), pack(item) if standalone else None]
    if rowid is not None:
        columns, values = "rowid," + columns, [rowid, *values]
    db.execute("INSERT INTO objects(" + columns + ") VALUES(" + ",".join("?" for _ in values) + ")", values)


def catalog_connection(path):
    db = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        from .catalog_storage import register_catalog_functions
        register_catalog_functions(db)
    except ImportError:
        pass  # A legacy catalog needs no decompression UDF.
    db.execute("PRAGMA query_only=ON")
    return db


def hydrate_object(db, identity, *, catalog_path=None, catalog_db=None):
    """Return the exact revision payload, never an active/latest replacement."""
    if not is_compact(db):
        row = db.execute("SELECT object_json FROM objects WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None
    row = db.execute("SELECT library_id,source_json FROM objects WHERE id=?", (identity,)).fetchone()
    if row is None:
        return None
    if row[1] is not None:
        item = unpack(row[1])
    else:
        if catalog_db is None and (catalog_path is None or not Path(catalog_path).is_file()):
            raise RuntimeError("Authoritative catalog required by the compact object index is missing")
        if catalog_db is not None:
            payload = catalog_db.execute("SELECT payload FROM revisions WHERE id=? AND library_id=?", (identity, row[0])).fetchone()
        else:
            with closing(catalog_connection(catalog_path)) as source:
                payload = source.execute("SELECT payload FROM revisions WHERE id=? AND library_id=?", (identity, row[0])).fetchone()
        if payload is None:
            raise RuntimeError("Exact catalog revision required by the compact object index is missing")
        item = json.loads(payload[0])
    if not isinstance(item, dict) or item.get("id") != identity or item.get("library_id") != row[0]:
        raise ValueError("Object source identity does not match its catalog revision")
    return item


def representation_stats(db, identity):
    row = db.execute("SELECT representation_json FROM objects WHERE id=?", (identity,)).fetchone()
    return decode_representation(row[0]) if row else None


def iter_representation_stats(db, *, library_id=None):
    """Stream per-object statistics in identity order without hydrating sources."""
    query = "SELECT id,representation_json FROM objects"
    parameters = ()
    if library_id is not None:
        query += " WHERE library_id=?"
        parameters = (library_id,)
    for identity, raw in db.execute(query + " ORDER BY id", parameters):
        check_cancelled()
        yield identity, decode_representation(raw)


def lookup_id(db, path):
    row = db.execute("SELECT id FROM objects WHERE qmd_path=?", (path,)).fetchone()
    return row[0] if row else None


def delete_library(db, library_id):
    """Delete metadata, removing any legacy postings with original tokens."""
    if has_legacy_fts(db):
        if is_compact(db):
            for rowid, identity, raw in db.execute("SELECT rowid,id,search_input FROM objects WHERE library_id=?", (library_id,)):
                check_cancelled()
                title, text, cjk = unpack(raw)
                db.execute("INSERT INTO object_fts(object_fts,rowid,id,title,text,cjk) VALUES('delete',?,?,?,?,?)",
                           (rowid, identity, title, text, cjk))
        else:
            db.execute("DELETE FROM object_fts WHERE id IN (SELECT id FROM objects WHERE library_id=?)", (library_id,))
    db.execute("DELETE FROM objects WHERE library_id=?", (library_id,))


def validate_manifest_lookup(path, manifest):
    """Prove bulk JSON can be dropped without losing any object locator."""
    with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as db:
        if not is_compact(db):
            raise ValueError("Compact object lookup is not available")
        count = db.execute("SELECT count(*) FROM objects").fetchone()[0]
        if count != manifest.get("object_count"):
            raise ValueError("Native lookup count does not match the generation manifest")
        if "mapping" in manifest and len(manifest["mapping"]) != count:
            raise ValueError("Retained QMD mapping does not cover every object")
        legacy_stats = manifest.get("representation", {}).get("objects")
        if legacy_stats is not None and len(legacy_stats) != count:
            raise ValueError("Retained representation metadata does not cover every object")
        for locator, raw in db.execute("SELECT qmd_path,representation_json FROM objects"):
            check_cancelled()
            if not isinstance(locator, str) or not re.fullmatch(r"lib_[a-f0-9]{20}/o-[a-f0-9]{32}\.md", locator):
                raise ValueError("Object lookup contains an invalid generated QMD path")
            if not isinstance(decode_representation(raw), dict):
                raise ValueError("Native representation metadata is invalid")
        for path, identity in manifest.get("mapping", {}).items():
            check_cancelled()
            if lookup_id(db, path) != identity:
                raise ValueError("Object locator differs from the retained QMD mapping")
        for identity, stats in manifest.get("representation", {}).get("objects", {}).items():
            check_cancelled()
            if representation_stats(db, identity) != stats:
                raise ValueError("Native representation metadata differs from the generation manifest")
    return True


def compact_object_index(path, *, manifest=None, catalog_path=None, offline=False, on_progress=None):
    """Migrate existing representations in isolation, then atomically publish.

    The caller owns the offline maintenance/build lease. The original DB stays
    byte-for-byte intact until object metadata and SQLite integrity pass.
    QMD databases and source files are never opened here.
    """
    if not offline:
        raise ValueError("Object index compaction requires the offline maintenance lease")
    path = Path(path).absolute()
    if (any(parent.is_symlink() or getattr(parent, "is_junction", lambda: False)() for parent in (path, *path.parents))
            or path.stat().st_nlink != 1):
        raise ValueError("Native compaction requires an unshared regular database")
    path = path.resolve()
    manifest = manifest or {}
    stage = path.with_name(path.name + ".compact-" + uuid.uuid4().hex + ".tmp")
    before = path.stat()
    # Managed readers resolve this sibling after relocation. A caller passing
    # an unrelated catalog must retain standalone payloads in this index.
    expected_catalog = path.parent.parent.parent.parent / "catalog.sqlite3"
    managed_catalog = (catalog_path is not None and Path(catalog_path).resolve() == expected_catalog.resolve()
                       and Path(catalog_path).is_file())
    source_catalog = catalog_connection(catalog_path) if managed_catalog else None
    try:
        check_cancelled()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as old:
            if not owned_schema(old):
                raise ValueError("Object compaction refuses unowned tables, views, indexes, or triggers")
            if is_compact(old):
                return {"changed": False, "before_bytes": before.st_size, "after_bytes": before.st_size}
            total = old.execute("SELECT count(*) FROM objects").fetchone()[0]
            if "object_count" in manifest and total != manifest["object_count"]:
                raise ValueError("Object index count differs from its retained manifest")
            paths = {identity: locator for locator, identity in manifest.get("mapping", {}).items()}
            from .library_search import _representation
            count = managed = 0
            with closing(sqlite3.connect(stage)) as new, new:
                create_schema(new)
                for rowid, identity, library, title, text, raw in old.execute("SELECT rowid,id,library_id,title,text,object_json FROM objects ORDER BY rowid"):
                    check_cancelled()
                    item = json.loads(raw)
                    if item.get("id") != identity or item.get("library_id") != library or item.get("title") != title:
                        raise ValueError("Object source payload identity is inconsistent")
                    match = source_catalog.execute("SELECT payload FROM revisions WHERE id=? AND library_id=?", (identity, library)).fetchone() if source_catalog else None
                    shared = bool(match and json.loads(match[0]) == item)
                    stats = manifest.get("representation", {}).get("objects", {}).get(identity)
                    if stats is None:
                        stats = _representation(item)[1]
                    # Preserve the retained QMD representation and original rowid.
                    cjk = ""
                    if has_legacy_fts(old):
                        fts = old.execute("SELECT rowid,id,title,text,cjk FROM object_fts WHERE rowid=?", (rowid,)).fetchone()
                        if fts is None or fts[1:4] != (identity, title, text):
                            raise ValueError("Legacy FTS representation is inconsistent")
                        cjk = fts[4]
                    insert_object(new, item, text, stats, cjk, standalone=not shared, rowid=rowid, path=paths.get(identity))
                    count += 1
                    managed += int(shared)
                    if on_progress is not None and (count % 1000 == 0 or count == total):
                        on_progress({"completed": count, "total": total, "unit": "objects"})
                if count != total:
                    raise ValueError("Native migration did not preserve every object")
                check_cancelled()
            validate_manifest_lookup(stage, dict(manifest, object_count=total))
            with closing(sqlite3.connect(stage)) as new:
                if new.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError("Compact object SQLite integrity check failed")
        check_cancelled()
        now = path.stat()
        if (now.st_size, now.st_mtime_ns, now.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino):
            raise RuntimeError("Object source index changed during compaction")
        if any(path.with_name(path.name + suffix).exists() and path.with_name(path.name + suffix).stat().st_size for suffix in ("-wal", "-journal")):
            raise RuntimeError("Object index has a live transaction sidecar; compaction refused")
        after_bytes = stage.stat().st_size
        os.replace(stage, path)
        return {"changed": True, "objects": count, "catalog_references": managed,
                "before_bytes": before.st_size, "after_bytes": after_bytes}
    finally:
        if source_catalog is not None:
            source_catalog.close()
        if stage.exists():
            stage.unlink()


# Historical maintenance entry point; this never creates search postings.
compact_native_index = compact_object_index


def _object_fingerprint(db):
    """Hash raw metadata and rowids without decoding or regenerating payloads."""
    digest, count = hashlib.sha256(), 0
    for row in db.execute("SELECT rowid,* FROM objects ORDER BY rowid"):
        check_cancelled()
        for value in row:
            if value is None:
                data = b"n"
            elif isinstance(value, bytes):
                data = b"b" + value
            else:
                data = type(value).__name__.encode("ascii") + b":" + str(value).encode("utf-8")
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        count += 1
    return {"objects": count, "objects_sha256": digest.hexdigest()}


def _storage_pages(db):
    size = db.execute("PRAGMA page_size").fetchone()[0]
    return {"page_size": size, "pages": db.execute("PRAGMA page_count").fetchone()[0],
            "free_pages": db.execute("PRAGMA freelist_count").fetchone()[0]}


def remove_legacy_fts(path, *, dry_run=True, offline=False, reclaim=True):
    """Remove only historical object_fts, preserving every raw object and rowid.

    Dry runs open the owned database read only. Applying requires the caller's
    offline maintenance/build lease. The schema drop and preservation check
    share a transaction; optional space reclamation follows the committed drop.
    Neither QMD databases nor catalogs, source files or embeddings are opened.
    Repeating the operation also finishes reclamation after an interrupted run.
    """
    if not dry_run and not offline:
        raise ValueError("Legacy FTS removal requires the offline maintenance lease")
    path = Path(path).expanduser().absolute()
    if (any(parent.is_symlink() or getattr(parent, "is_junction", lambda: False)()
            for parent in (path, *path.parents)) or not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("Legacy FTS removal requires an unshared regular database")
    path = path.resolve(strict=True)
    for suffix in ("-wal", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise RuntimeError("Object database has a live transaction sidecar; stop readers/writers first")
    before_bytes = path.stat().st_size
    removed_tables = []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        db.execute("PRAGMA query_only=ON")
        if not owned_schema(db):
            raise ValueError("Legacy FTS removal refuses an unowned object schema")
        expected = _object_fingerprint(db)
        before_pages = _storage_pages(db)
        removed_tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND (name='object_fts' OR name GLOB 'object_fts_*') ORDER BY name")]
    report = {"path": str(path), "dry_run": bool(dry_run), "owned_schema": True,
              "legacy_fts_present": bool(removed_tables), "changed": False,
              "fts_tables": removed_tables, "removed_tables": 0, **expected,
              "before_bytes": before_bytes, "after_bytes": before_bytes, "reclaimed_bytes": 0,
              "before_pages": before_pages, "after_pages": before_pages,
              "metadata_unchanged": True, "qmd_opened": False, "embedding_invoked": False,
              "compaction": "not_requested" if not reclaim else "not_needed"}
    if dry_run:
        return report
    check_cancelled()
    with closing(sqlite3.connect(path, timeout=30)) as db:
        with db:
            db.execute("BEGIN EXCLUSIVE")
            if not owned_schema(db) or _object_fingerprint(db) != expected:
                raise RuntimeError("Object database changed before legacy FTS removal")
            if has_legacy_fts(db):
                db.execute("DROP TABLE object_fts")
                if has_legacy_fts(db) or not owned_schema(db) or _object_fingerprint(db) != expected:
                    raise ValueError("Legacy FTS removal did not preserve the owned object metadata")
            check_cancelled()
        report.update(changed=bool(removed_tables), removed_tables=len(removed_tables))
        if reclaim and db.execute("PRAGMA freelist_count").fetchone()[0]:
            check_cancelled()
            if db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2:
                db.execute("PRAGMA incremental_vacuum").fetchall()
                report["compaction"] = "incremental_vacuum"
            else:
                db.execute("VACUUM")
                report["compaction"] = "vacuum"
        if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
            raise RuntimeError("Object database checkpoint is busy; retry space reclamation after readers finish")
        if not owned_schema(db) or _object_fingerprint(db) != expected:
            raise ValueError("Object metadata or rowids changed during space reclamation")
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("Object metadata SQLite integrity check failed")
        report["after_pages"] = _storage_pages(db)
    report["after_bytes"] = path.stat().st_size
    report["reclaimed_bytes"] = before_bytes - report["after_bytes"]
    return report


def slim_object_index(path, *, dry_run=True, offline=False, on_progress=None):
    """Remove retained build text and compact statistics without reindexing.

    Existing compact v1/native-v2 files remain untouched until a complete v2
    sibling passes rowid/retained-column comparison and SQLite integrity checks.
    Statistics are verified during their single conversion pass. Source payloads
    are copied as raw bytes; no catalog, QMD file or embedding is opened.
    """
    if not dry_run and not offline:
        raise ValueError("Object metadata slimming requires the offline maintenance lease")
    path = Path(path).expanduser().absolute()
    if (any(parent.is_symlink() or getattr(parent, "is_junction", lambda: False)()
            for parent in (path, *path.parents)) or not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("Object metadata slimming requires an unshared regular database")
    path = path.resolve(strict=True)

    def check_sidecars():
        for suffix in ("-wal", "-journal"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise RuntimeError("Object database has a live transaction sidecar; stop readers/writers first")

    check_sidecars()
    before = path.stat()
    stage = path.with_name(path.name + ".slim-" + uuid.uuid4().hex + ".tmp")
    retained = ("id", "library_id", "kind", "feature_branch", "qmd_path", "source_json")
    try:
        check_cancelled()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as old:
            old.execute("PRAGMA query_only=ON")
            old.execute("BEGIN")
            if not owned_schema(old) or not is_compact(old):
                raise ValueError("Object metadata slimming requires an owned compact object schema")
            slim = is_slim(old)
            metadata = "object_metadata" if old.execute(
                "SELECT 1 FROM sqlite_master WHERE name='object_metadata'").fetchone() else "native_metadata"
            schema_before = old.execute("SELECT value FROM " + metadata + " WHERE key='schema'").fetchone()[0]
            count, representation_before, source_bytes = old.execute(
                "SELECT count(*),coalesce(sum(length(representation_json)),0),coalesce(sum(length(source_json)),0) FROM objects").fetchone()
            search_bytes = 0 if slim else old.execute("SELECT coalesce(sum(length(search_input)),0) FROM objects").fetchone()[0]
            fts_tables = [row[0] for row in old.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND (name='object_fts' OR name GLOB 'object_fts_*') ORDER BY name")]
            report = {"path": str(path), "dry_run": bool(dry_run), "changed": False,
                      "schema_before": schema_before, "schema_after": SCHEMA,
                      "owned_schema": True, "objects": count, "statistics_verified": 0,
                      "search_input_bytes_removed": search_bytes,
                      "representation_before_bytes": representation_before,
                      "representation_after_bytes": representation_before,
                      "standalone_source_bytes": source_bytes, "legacy_fts_tables": fts_tables,
                      "before_bytes": before.st_size, "after_bytes": before.st_size, "reclaimed_bytes": 0,
                      "rowids_preserved": True, "metadata_columns_preserved": True,
                      "statistics_preserved": True, "catalog_opened": False,
                      "qmd_opened": False, "embedding_invoked": False}
            if slim:
                return report
            completed = representation_after = 0

            def converted_rows():
                nonlocal completed, representation_after
                for row in old.execute("SELECT rowid,id,library_id,kind,feature_branch,qmd_path,representation_json,source_json FROM objects ORDER BY rowid"):
                    check_cancelled()
                    stats = decode_representation(row[6])
                    encoded = encode_representation(stats)
                    if decode_representation(encoded) != stats:
                        raise ValueError("Compact representation statistics changed during conversion")
                    representation_after += len(encoded)
                    completed += 1
                    if on_progress is not None and (completed % 10000 == 0 or completed == count):
                        on_progress({"completed": completed, "total": count, "unit": "objects"})
                    yield (*row[:6], encoded, row[7])

            if dry_run:
                # This pass only reads the statistics column; retained source
                # blobs and discarded text are not decoded or hashed.
                for (raw,) in old.execute("SELECT representation_json FROM objects"):
                    check_cancelled()
                    stats = decode_representation(raw)
                    encoded = encode_representation(stats)
                    if decode_representation(encoded) != stats:
                        raise ValueError("Compact representation statistics changed during conversion")
                    representation_after += len(encoded)
                    completed += 1
                    if on_progress is not None and (completed % 10000 == 0 or completed == count):
                        on_progress({"completed": completed, "total": count, "unit": "objects"})
            else:
                with closing(sqlite3.connect(stage.as_uri(), uri=True, timeout=30)) as new:
                    new.execute("PRAGMA journal_mode=DELETE")
                    new.execute("PRAGMA temp_store=MEMORY")
                    with new:
                        create_schema(new)
                        new.executemany("INSERT INTO object_metadata(key,value) VALUES(?,?)",
                                        old.execute("SELECT key,value FROM " + metadata + " WHERE key<>'schema'"))
                        new.executemany("INSERT INTO objects(rowid,id,library_id,kind,feature_branch,qmd_path,representation_json,source_json) VALUES(?,?,?,?,?,?,?,?)",
                                        converted_rows())
                    actual = new.execute("SELECT count(*),coalesce(sum(length(representation_json)),0) FROM objects").fetchone()
                    if actual != (count, representation_after) or completed != count:
                        raise ValueError("Object metadata slimming did not preserve every object")
                    # Compare retained SQL values directly instead of hashing
                    # large source blobs in repeated before/after passes.
                    new.execute("ATTACH DATABASE ? AS original", (path.as_uri() + "?mode=ro",))
                    mismatch = new.execute(
                        "SELECT 1 FROM objects n LEFT JOIN original.objects o ON o.rowid=n.rowid WHERE o.rowid IS NULL OR "
                        + " OR ".join("o." + column + " IS NOT n." + column for column in retained) + " LIMIT 1").fetchone()
                    new.execute("DETACH DATABASE original")
                    if mismatch or not owned_schema(new) or not is_slim(new):
                        raise ValueError("Object metadata slimming changed retained columns or rowids")
                    for pragma in ("user_version", "application_id"):
                        value = old.execute("PRAGMA " + pragma).fetchone()[0]
                        new.execute("PRAGMA " + pragma + "=" + str(value))
                    if new.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        raise ValueError("Slim object SQLite integrity check failed")
            report.update(statistics_verified=completed, representation_after_bytes=representation_after)
        if not dry_run:
            check_cancelled()
            now = path.stat()
            if (now.st_size, now.st_mtime_ns, now.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino):
                raise RuntimeError("Object database changed during metadata slimming")
            check_sidecars()
            report["after_bytes"] = stage.stat().st_size
            os.replace(stage, path)
            report.update(changed=True, reclaimed_bytes=before.st_size - report["after_bytes"])
        return report
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            stage.with_name(stage.name + suffix).unlink(missing_ok=True)
