"""Lossless compressed catalog revisions and explicit, offline migration.

``revisions`` keeps its historical SQL interface. Only the authoritative TEXT
payload's physical representation changes; IDs and JSON are never regenerated.
Connections that read that view must call :func:`register_catalog_functions`.
Legacy catalogs stay readable until an operator explicitly migrates them.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import uuid
import zlib

from .build_control import BuildCancelled, check_cancelled


_TABLE = "_revision_payloads"
_SCHEMA = "catalog-revisions/zlib-v1"


def _pack(payload):
    if not isinstance(payload, str):
        raise ValueError("Catalog revision payload must be TEXT")
    raw = payload.encode("utf-8")
    compressed = zlib.compress(raw, 6)
    return b"\x01" + compressed if len(compressed) < len(raw) else b"\x00" + raw


def _unpack(payload):
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("Invalid compressed catalog payload")
    if payload[0] == 0:
        raw = payload[1:]
    elif payload[0] == 1:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(payload[1:]) + decoder.flush()
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("Invalid compressed catalog payload stream")
    else:
        raise ValueError("Unsupported compressed catalog payload codec")
    return raw.decode("utf-8")


def register_catalog_functions(connection):
    """Register the exact TEXT codec on any writable or read-only connection."""
    connection.create_function("sim_catalog_pack", 1, _pack, deterministic=True)
    connection.create_function("sim_catalog_unpack", 1, _unpack, deterministic=True)
    return connection


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def revision_storage(connection):
    """Return the representation without touching an existing catalog."""
    row = connection.execute("SELECT type FROM sqlite_master WHERE name='revisions'").fetchone()
    if not row:
        return None
    if row[0] == "table":
        return "json-text"
    if row[0] == "view" and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)).fetchone():
        definition = connection.execute("SELECT sql FROM sqlite_master WHERE name='revisions'").fetchone()[0]
        if "sim_catalog_unpack(payload)" in definition:
            return _SCHEMA
    raise ValueError("Unsupported catalog revisions schema")


def initialize_revisions(connection):
    """Create compact revisions for a new catalog; leave legacy tables alone."""
    register_catalog_functions(connection)
    if revision_storage(connection) is not None:
        return
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone():
        raise ValueError("Catalog contains an incomplete compressed revisions schema")
    # execute(), rather than executescript(), keeps the caller's transaction.
    statements = [
        f"CREATE TABLE {_TABLE}(id TEXT PRIMARY KEY, object_id TEXT NOT NULL, "
        "library_id TEXT NOT NULL, payload BLOB NOT NULL)",
        f"CREATE INDEX revision_payloads_library ON {_TABLE}(library_id)",
        f"CREATE VIEW revisions AS SELECT id,object_id,library_id,sim_catalog_unpack(payload) AS payload FROM {_TABLE}",
        f"CREATE TRIGGER revisions_insert INSTEAD OF INSERT ON revisions BEGIN "
        f"INSERT INTO {_TABLE} VALUES(NEW.id,NEW.object_id,NEW.library_id,sim_catalog_pack(NEW.payload)); END",
        f"CREATE TRIGGER revisions_update INSTEAD OF UPDATE ON revisions BEGIN "
        f"UPDATE {_TABLE} SET id=NEW.id,object_id=NEW.object_id,library_id=NEW.library_id,"
        "payload=sim_catalog_pack(NEW.payload) WHERE id=OLD.id; END",
        f"CREATE TRIGGER revisions_delete INSTEAD OF DELETE ON revisions BEGIN "
        f"DELETE FROM {_TABLE} WHERE id=OLD.id; END",
    ]
    connection.execute("SAVEPOINT initialize_catalog_revisions")
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute("RELEASE initialize_catalog_revisions")
    except BaseException:
        connection.execute("ROLLBACK TO initialize_catalog_revisions")
        connection.execute("RELEASE initialize_catalog_revisions")
        raise


def _hash_rows(rows):
    """Hash exact SQL values, including row order, TEXT, BLOB and NULL types."""
    hasher, count = hashlib.sha256(), 0
    for row in rows:
        check_cancelled()
        hasher.update(struct.pack(">Q", len(row)))
        for value in row:
            if value is None:
                kind, raw = b"n", b""
            elif isinstance(value, bytes):
                kind, raw = b"b", value
            elif isinstance(value, str):
                kind, raw = b"t", value.encode("utf-8")
            elif isinstance(value, int):
                kind, raw = b"i", str(value).encode("ascii")
            elif isinstance(value, float):
                kind, raw = b"f", struct.pack(">d", value)
            else:
                raise ValueError("Unsupported catalog SQL value")
            hasher.update(kind + struct.pack(">Q", len(raw)) + raw)
        count += 1
    return {"rows": count, "sha256": hasher.hexdigest()}


def _table_columns(db, name):
    # Hidden/generated columns cannot be inserted; their definitions are kept.
    return [row[1] for row in db.execute("PRAGMA table_xinfo(" + _quote(name) + ")") if row[6] == 0]


def _table_query(db, name):
    columns = _table_columns(db, name)
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0]
    without_rowid = "WITHOUT ROWID" in sql.upper()
    selected = ",".join(_quote(column) for column in columns)
    if not without_rowid:
        return "SELECT rowid," + selected + " FROM " + _quote(name) + " ORDER BY rowid", ["rowid"] + columns
    primary = [row[1] for row in sorted(db.execute("PRAGMA table_info(" + _quote(name) + ")"), key=lambda row: row[5]) if row[5]]
    return "SELECT " + selected + " FROM " + _quote(name) + " ORDER BY " + ",".join(map(_quote, primary)), columns


def _snapshot(db, tables, *, compressed):
    result = {}
    for name in tables:
        if name == "revisions":
            physical = _TABLE if compressed else "revisions"
            payload = "sim_catalog_unpack(payload)" if compressed else "payload"
            query = f"SELECT rowid,id,object_id,library_id,{payload} FROM {physical} ORDER BY rowid"
        else:
            query, _ = _table_query(db, name)
        result[name] = _hash_rows(db.execute(query))
    return result


def _catalog_connection(path, *, read_only=False):
    suffix = "?mode=ro" if read_only else "?mode=rw"
    return register_catalog_functions(sqlite3.connect(path.as_uri() + suffix, uri=True, timeout=1))


def _interruptible(connection):
    def interrupted():
        try:
            check_cancelled()
        except BuildCancelled:
            return 1
        return 0
    connection.set_progress_handler(interrupted, 10000)
    return connection


def _progress(callback, **update):
    check_cancelled()
    if callback is not None:
        callback(update)
    check_cancelled()


def compact_catalog(path, *, offline=False, on_progress=None):
    """Atomically replace one stopped catalog with a verified compressed copy.

    The caller must stop all application/CLI processes using this catalog and
    pass ``offline=True``. The cooperative migration lock rejects new Store
    connections; an SQLite exclusive lock also detects outstanding readers and
    writers before publication. No source files, compact-case databases, search
    indexes or revision identities are rewritten. The sole staging file is a
    sibling of the catalog and is removed on failure; no full backup is kept.
    A process killed before publication can leave a staging file/lock, which an
    operator may remove after verifying the recorded process has stopped.
    """
    if offline is not True:
        raise ValueError("Catalog compaction requires all application processes stopped and offline=True")
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Catalog must be an owned regular database")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("Catalog must be an owned regular database")
    lease = path.parent / ".build.lock"
    lock = path.parent / ".catalog-migration.lock"
    stage = path.with_name("." + path.name + ".compact-" + uuid.uuid4().hex + ".tmp")
    owned = False
    source = target = None
    published = False
    try:
        # Never recover another job's lease implicitly in an offline migration.
        if lease.exists():
            raise RuntimeError("A catalog build/maintenance lease exists; stop its owner before migration")
        with lock.open("x", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "catalog": str(path), "stage": str(stage)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        owned = True
        if lease.exists():
            raise RuntimeError("A catalog build/maintenance lease appeared; migration was not started")
        source = _interruptible(_catalog_connection(path))
        storage = revision_storage(source)
        if storage == _SCHEMA:
            return {"status": "already_compact", "storage": storage, "bytes": path.stat().st_size}
        if storage != "json-text":
            raise ValueError("Catalog has no legacy revisions table")
        # Checkpoint before acquiring the exclusive rollback-journal lock. A
        # still-open WAL reader prevents conversion and leaves source data safe.
        checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and checkpoint[0]:
            raise RuntimeError("Catalog is busy; stop all processes before migration")
        if source.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
            raise RuntimeError("Catalog is busy; stop all processes before migration")
        source.execute("BEGIN EXCLUSIVE")
        schema = source.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
        revision_extras = [row[1] for row in schema if row[2] == "revisions" and row[0] != "table"]
        if revision_extras:
            raise ValueError("Catalog revisions has custom indexes/triggers; explicit schema migration is required")
        tables = [row[1] for row in schema if row[0] == "table"]
        if _TABLE in tables or any(row[1] in {"revisions_insert", "revisions_update", "revisions_delete", "revision_payloads_library"} for row in schema):
            raise ValueError("Catalog contains reserved compression schema names")
        if any("CREATE VIRTUAL TABLE" in row[3].upper() for row in schema if row[0] == "table"):
            raise ValueError("Catalog contains virtual tables; explicit schema migration is required")
        before_bytes = path.stat().st_size
        target = _interruptible(register_catalog_functions(sqlite3.connect(stage)))
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("PRAGMA synchronous=FULL")
        target.execute("BEGIN")
        for pragma in ("application_id", "user_version", "auto_vacuum"):
            target.execute("PRAGMA " + pragma + "=" + str(source.execute("PRAGMA " + pragma).fetchone()[0]))
        for kind, name, _, sql in schema:
            if kind == "table" and name != "revisions":
                if name == "sqlite_sequence":
                    continue  # Created automatically by AUTOINCREMENT tables.
                target.execute(sql)
        initialize_revisions(target)
        counts = {}
        for name in tables:
            _progress(on_progress, phase="copy", table=name, completed=0)
            if name == "revisions":
                query = "SELECT rowid,id,object_id,library_id,payload FROM revisions ORDER BY rowid"
                insert = f"INSERT INTO {_TABLE}(rowid,id,object_id,library_id,payload) VALUES(?,?,?,?,sim_catalog_pack(?))"
            else:
                query, columns = _table_query(source, name)
                insert = "INSERT INTO " + _quote(name) + "(" + ",".join(map(_quote, columns)) + ") VALUES(" + ",".join("?" for _ in columns) + ")"
                if name == "sqlite_sequence":
                    target.execute("DELETE FROM sqlite_sequence")
            count = 0
            with closing(source.execute(query)) as cursor:
                while True:
                    check_cancelled()
                    batch = cursor.fetchmany(128)
                    if not batch:
                        break
                    target.executemany(insert, batch)
                    count += len(batch)
                    _progress(on_progress, phase="copy", table=name, completed=count)
            counts[name] = count
        for kind, _, _, sql in schema:
            if kind != "table":
                target.execute(sql)
        target.commit()
        _progress(on_progress, phase="validate", completed=0)
        expected = _snapshot(source, tables, compressed=False)
        actual = _snapshot(target, tables, compressed=True)
        if expected != actual:
            raise ValueError("Catalog migration changed logical rows or their order")
        if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Compressed catalog failed integrity_check")
        violations_before = source.execute("PRAGMA foreign_key_check").fetchall()
        if target.execute("PRAGMA foreign_key_check").fetchall() != violations_before:
            raise ValueError("Catalog migration changed foreign-key consistency")
        # fsync the complete, closed staging database before atomic replacement.
        target.close()
        target = None
        with stage.open("rb+") as stream:
            os.fsync(stream.fileno())
        after_bytes = stage.stat().st_size
        _progress(on_progress, phase="publish", completed=sum(counts.values()))
        source.rollback()
        source.close()
        source = None
        # Store connections are fenced by the cooperative lock until replace
        # finishes. On Windows any uncooperative remaining reader makes this
        # fail without altering the original pathname.
        check_cancelled()
        os.replace(stage, path)
        published = True
        return {"status": "compacted", "storage": _SCHEMA, "before_bytes": before_bytes,
                "after_bytes": after_bytes, "saved_bytes": before_bytes - after_bytes,
                "tables": expected, "payloads": "exact_text", "identities": "unchanged"}
    except sqlite3.Error:
        # A progress-handler interrupt must retain the public cancellation
        # exception rather than surfacing an SQLite "interrupted" error.
        check_cancelled()
        raise
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        if not published:
            for candidate in (stage, Path(str(stage) + "-journal")):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
        if owned:
            lock.unlink(missing_ok=True)
