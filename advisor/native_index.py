"""Compact native retrieval storage, independent of QMD/model/source parsing.

FTS stores postings only. A compressed bounded representation supports exact
FTS deletion on older SQLite releases; managed full payloads live in the
catalog once. Standalone indexes retain their own compressed source payload.
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

SCHEMA = "native-objects/v2"
LOOKUP_SCHEMA = "native-object-lookup-v1"
OBJECT_SQL = ("CREATE TABLE objects(id TEXT PRIMARY KEY, library_id TEXT NOT NULL, "
              "kind TEXT, feature_branch TEXT, qmd_path TEXT UNIQUE NOT NULL, "
              "representation_json BLOB NOT NULL, search_input BLOB NOT NULL, source_json BLOB)")
FTS_SQL = "CREATE VIRTUAL TABLE object_fts USING fts5(id UNINDEXED,title,text,cjk,content='',tokenize='porter unicode61')"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def pack(value):
    return zlib.compress(_json(value).encode("utf-8"), 6)


def unpack(value):
    return json.loads(zlib.decompress(value))


def is_compact(db):
    return "source_json" in {row[1] for row in db.execute("PRAGMA table_info(objects)")}


def create_schema(db):
    db.execute(OBJECT_SQL)
    db.execute(FTS_SQL)
    db.execute("CREATE TABLE native_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.execute("INSERT INTO native_metadata VALUES('schema',?)", (SCHEMA,))
    db.execute("CREATE INDEX objects_scope ON objects(library_id,kind,feature_branch)")


def owned_schema(db):
    tables = {"objects", "native_metadata", "object_fts", "object_fts_data", "object_fts_idx", "object_fts_docsize", "object_fts_config"}
    indexes = {"sqlite_autoindex_objects_1", "sqlite_autoindex_objects_2", "sqlite_autoindex_native_metadata_1", "objects_scope"}
    schema = {(row[0], row[1]) for row in db.execute("SELECT type,name FROM sqlite_master")}
    if schema != {*(("table", name) for name in tables), *(("index", name) for name in indexes)}:
        return False
    definitions = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name IN ('objects','object_fts')"))
    normalize = lambda sql: " ".join(sql.split()).casefold()
    return (all(normalize(definitions.get(name, "")) == normalize(sql) for name, sql in (("objects", OBJECT_SQL), ("object_fts", FTS_SQL)))
            and db.execute("SELECT value FROM native_metadata WHERE key='schema'").fetchone() == (SCHEMA,))


def qmd_path(item):
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    return "lib_" + digest(item["library_id"])[:20] + "/o-" + digest(item["id"])[:32] + ".md"


def insert_object(db, item, text, stats, cjk, *, standalone=True, rowid=None, path=None):
    columns = "id,library_id,kind,feature_branch,qmd_path,representation_json,search_input,source_json"
    values = [item["id"], item["library_id"], item.get("kind"), object_feature_branch(item),
              path or qmd_path(item), pack(stats), pack([item["title"], text, cjk]), pack(item) if standalone else None]
    if rowid is not None:
        columns, values = "rowid," + columns, [rowid, *values]
    cursor = db.execute("INSERT INTO objects(" + columns + ") VALUES(" + ",".join("?" for _ in values) + ")", values)
    db.execute("INSERT INTO object_fts(rowid,id,title,text,cjk) VALUES(?,?,?,?,?)",
               (rowid if rowid is not None else cursor.lastrowid, item["id"], item["title"], text, cjk))


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
            raise RuntimeError("Authoritative catalog required by the compact native index is missing")
        if catalog_db is not None:
            payload = catalog_db.execute("SELECT payload FROM revisions WHERE id=? AND library_id=?", (identity, row[0])).fetchone()
        else:
            with closing(catalog_connection(catalog_path)) as source:
                payload = source.execute("SELECT payload FROM revisions WHERE id=? AND library_id=?", (identity, row[0])).fetchone()
        if payload is None:
            raise RuntimeError("Exact catalog revision required by the compact native index is missing")
        item = json.loads(payload[0])
    if not isinstance(item, dict) or item.get("id") != identity or item.get("library_id") != row[0]:
        raise ValueError("Native source identity does not match its catalog revision")
    return item


def representation_stats(db, identity):
    row = db.execute("SELECT representation_json FROM objects WHERE id=?", (identity,)).fetchone()
    return unpack(row[0]) if row else None


def lookup_id(db, path):
    row = db.execute("SELECT id FROM objects WHERE qmd_path=?", (path,)).fetchone()
    return row[0] if row else None


def delete_library(db, library_id):
    """Delete postings with their original tokens, including on old FTS5."""
    for rowid, identity, raw in db.execute("SELECT rowid,id,search_input FROM objects WHERE library_id=?", (library_id,)):
        check_cancelled()
        title, text, cjk = unpack(raw)
        db.execute("INSERT INTO object_fts(object_fts,rowid,id,title,text,cjk) VALUES('delete',?,?,?,?,?)",
                   (rowid, identity, title, text, cjk))
    db.execute("DELETE FROM objects WHERE library_id=?", (library_id,))


def validate_manifest_lookup(path, manifest):
    """Prove bulk JSON can be dropped without losing any object locator."""
    with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as db:
        if not is_compact(db):
            raise ValueError("Compact native lookup is not available")
        count = db.execute("SELECT count(*) FROM objects").fetchone()[0]
        if count != manifest.get("object_count"):
            raise ValueError("Native lookup count does not match the generation manifest")
        if "mapping" in manifest and len(manifest["mapping"]) != count:
            raise ValueError("Retained QMD mapping does not cover every native object")
        legacy_stats = manifest.get("representation", {}).get("objects")
        if legacy_stats is not None and len(legacy_stats) != count:
            raise ValueError("Retained representation metadata does not cover every native object")
        for locator, raw in db.execute("SELECT qmd_path,representation_json FROM objects"):
            check_cancelled()
            if not isinstance(locator, str) or not re.fullmatch(r"lib_[a-f0-9]{20}/o-[a-f0-9]{32}\.md", locator):
                raise ValueError("Native object lookup contains an invalid generated QMD path")
            if not isinstance(unpack(raw), dict):
                raise ValueError("Native representation metadata is invalid")
        for path, identity in manifest.get("mapping", {}).items():
            check_cancelled()
            if lookup_id(db, path) != identity:
                raise ValueError("Native object locator differs from the retained QMD mapping")
        for identity, stats in manifest.get("representation", {}).get("objects", {}).items():
            check_cancelled()
            if representation_stats(db, identity) != stats:
                raise ValueError("Native representation metadata differs from the generation manifest")
    return True


def compact_native_index(path, *, manifest=None, catalog_path=None, offline=False, on_progress=None):
    """Migrate existing representations in isolation, then atomically publish.

    The caller owns the offline maintenance/build lease. The original DB stays
    byte-for-byte intact until all rows, payload hashes and FTS integrity pass.
    QMD databases and source files are never opened here.
    """
    if not offline:
        raise ValueError("Native index compaction requires the offline maintenance lease")
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
            if is_compact(old):
                return {"changed": False, "before_bytes": before.st_size, "after_bytes": before.st_size}
            columns = [row[1] for row in old.execute("PRAGMA table_info(objects)")]
            if columns != ["id", "library_id", "title", "text", "object_json"]:
                raise ValueError("Unrecognized native index schema; compaction refused")
            tables = {"objects", "object_fts", "object_fts_data", "object_fts_idx", "object_fts_content", "object_fts_docsize", "object_fts_config"}
            schema = {(row[0], row[1]) for row in old.execute("SELECT type,name FROM sqlite_master")}
            if schema != {*(("table", name) for name in tables), ("index", "sqlite_autoindex_objects_1")}:
                raise ValueError("Native compaction refuses unowned tables, views, indexes, or triggers")
            total = old.execute("SELECT count(*) FROM objects").fetchone()[0]
            if "object_count" in manifest and total != manifest["object_count"]:
                raise ValueError("Native index count differs from its retained manifest")
            paths = {identity: locator for locator, identity in manifest.get("mapping", {}).items()}
            from .library_search import _representation
            count = managed = 0
            with closing(sqlite3.connect(stage)) as new, new:
                create_schema(new)
                for rowid, identity, library, title, text, raw in old.execute("SELECT rowid,id,library_id,title,text,object_json FROM objects ORDER BY rowid"):
                    check_cancelled()
                    item = json.loads(raw)
                    if item.get("id") != identity or item.get("library_id") != library or item.get("title") != title:
                        raise ValueError("Native source payload identity is inconsistent")
                    match = source_catalog.execute("SELECT payload FROM revisions WHERE id=? AND library_id=?", (identity, library)).fetchone() if source_catalog else None
                    shared = bool(match and json.loads(match[0]) == item)
                    stats = manifest.get("representation", {}).get("objects", {}).get(identity)
                    if stats is None:
                        stats = _representation(item)[1]
                    # Existing FTS content is authoritative for exact tokens and
                    # rowids, including historical representation policies.
                    fts = old.execute("SELECT rowid,id,title,text,cjk FROM object_fts WHERE rowid=?", (rowid,)).fetchone()
                    if fts is None or fts[1:4] != (identity, title, text):
                        raise ValueError("Native FTS representation is inconsistent")
                    insert_object(new, item, text, stats, fts[4], standalone=not shared, rowid=fts[0], path=paths.get(identity))
                    count += 1
                    managed += int(shared)
                    if on_progress is not None and (count % 1000 == 0 or count == total):
                        on_progress({"completed": count, "total": total, "unit": "objects"})
                if count != total:
                    raise ValueError("Native migration did not preserve every object")
                new.execute("INSERT INTO object_fts(object_fts) VALUES('integrity-check')")
                check_cancelled()
            validate_manifest_lookup(stage, dict(manifest, object_count=total))
            with closing(sqlite3.connect(stage)) as new:
                if new.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError("Compact native SQLite integrity check failed")
        check_cancelled()
        now = path.stat()
        if (now.st_size, now.st_mtime_ns, now.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino):
            raise RuntimeError("Native source index changed during compaction")
        if any(path.with_name(path.name + suffix).exists() and path.with_name(path.name + suffix).stat().st_size for suffix in ("-wal", "-journal")):
            raise RuntimeError("Native index has a live transaction sidecar; compaction refused")
        after_bytes = stage.stat().st_size
        os.replace(stage, path)
        return {"changed": True, "objects": count, "catalog_references": managed,
                "before_bytes": before.st_size, "after_bytes": after_bytes}
    finally:
        if source_catalog is not None:
            source_catalog.close()
        if stage.exists():
            stage.unlink()
