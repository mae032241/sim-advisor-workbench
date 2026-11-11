"""Stage physical tutorial-body reclamation without parsing an MPH file.

Model/API rows and shared pool entries remain equal as SQL values. Tutorial
rows and their exclusively referenced metadata are removed. Publication and
the operational catalog binding are owned by the offline coordinator.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import re
import sqlite3
import struct

from .build_control import BuildCancelled, check_cancelled
from .catalog_storage import register_catalog_functions
from .compact_case import FORMAT

TUTORIAL_TABLES = frozenset({"documents", "passages", "text_pool"})
BINDING_TABLE = "compact_storage_bindings"
_SHA = re.compile(r"[a-f0-9]{64}\Z")


def ensure_storage_binding_schema(db):
    """Create only an operational table inside the coordinator's transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS compact_storage_bindings(
      relative_path TEXT NOT NULL,original_sha256 TEXT NOT NULL,current_sha256 TEXT NOT NULL,
      PRIMARY KEY(relative_path,original_sha256))""")


def storage_binding_matches(catalog_path, relative_path, original_sha256, current_sha256):
    """Authorize one exact old-hash/current-hash binding; never infer a rebind."""
    if not all(isinstance(value, str) and _SHA.fullmatch(value) for value in (original_sha256, current_sha256)):
        return False
    relative = Path(relative_path)
    catalog = Path(catalog_path)
    if relative.is_absolute() or ".." in relative.parts or not catalog.is_file():
        return False
    try:
        with closing(sqlite3.connect(catalog.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
            register_catalog_functions(db)
            db.execute("PRAGMA query_only=ON")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (BINDING_TABLE,)).fetchone():
                return False
            return db.execute("""SELECT 1 FROM compact_storage_bindings
              WHERE relative_path=? AND original_sha256=? AND current_sha256=?""",
                              (relative.as_posix(), original_sha256, current_sha256)).fetchone() is not None
    except (OSError, sqlite3.Error):
        return False


def _connect(path, *, read_only=False):
    path = Path(path)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30) if read_only else sqlite3.connect(path, timeout=30)
    if read_only:
        db.execute("PRAGMA query_only=ON")
    def interrupted():
        try:
            check_cancelled()
        except BuildCancelled:
            return 1
        return 0
    db.set_progress_handler(interrupted, 10000)
    return db


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            check_cancelled()
            digest.update(chunk)
    return digest.hexdigest()


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _hash_rows(rows):
    digest, count = hashlib.sha256(), 0
    for row in rows:
        check_cancelled()
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
                raise ValueError("Unsupported compact SQL value")
            digest.update(kind + struct.pack(">Q", len(raw)) + raw)
        digest.update(b"\xff")
        count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def _retained_metadata_sql(db, tables):
    references = []
    for name in sorted(set(tables) - TUTORIAL_TABLES - {"metadata_pool"}):
        for foreign in db.execute("PRAGMA foreign_key_list(" + _quote(name) + ")"):
            if foreign[2] == "metadata_pool":
                if foreign[4] not in {None, "id"}:
                    raise ValueError("Unsupported shared metadata foreign key")
                column = _quote(foreign[3])
                references.append("SELECT " + column + " FROM " + _quote(name) + " WHERE " + column + " IS NOT NULL")
    return " UNION ".join(references) if references else "SELECT NULL WHERE 0"


def _semantic_digest(db):
    tables = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    if not {"model", "nodes", "properties", "selections", "symbols", "value_pool", "metadata_pool", *TUTORIAL_TABLES} <= tables.keys():
        raise ValueError("Unsupported compact database schema")
    row = db.execute("SELECT format FROM model WHERE id=1").fetchone()
    if row != (FORMAT,):
        raise ValueError("Unsupported compact model format")
    result, digest = {}, hashlib.sha256()
    metadata_sql = _retained_metadata_sql(db, tables)
    for name in sorted(tables.keys() - TUTORIAL_TABLES):
        columns = [row[1] for row in db.execute("PRAGMA table_xinfo(" + _quote(name) + ")") if row[6] == 0]
        selected = ",".join(map(_quote, columns))
        if "WITHOUT ROWID" in (tables[name] or "").upper():
            primary = [row[1] for row in sorted(db.execute("PRAGMA table_info(" + _quote(name) + ")"), key=lambda row: row[5]) if row[5]]
            query = "SELECT " + selected + " FROM " + _quote(name) + " ORDER BY " + ",".join(map(_quote, primary))
        else:
            where = " WHERE id IN (" + metadata_sql + ")" if name == "metadata_pool" else ""
            query = "SELECT rowid," + selected + " FROM " + _quote(name) + where + " ORDER BY rowid"
        result[name] = _hash_rows(db.execute(query))
        digest.update(name.encode("utf-8") + b"\0" + str(result[name]["rows"]).encode("ascii") + b"\0" + bytes.fromhex(result[name]["sha256"]))
    schema = _hash_rows(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
    return {"sha256": digest.hexdigest(), "tables": result, "schema_sha256": schema["sha256"]}


def compact_semantic_digest(path):
    """Hash retained SQL values, explicit row IDs, schema and optional API/FTS."""
    with closing(_connect(Path(path).expanduser().resolve(strict=True), read_only=True)) as db:
        return _semantic_digest(db)


def stage_compact_reclaim(path, output):
    """Write a new staged database and prove its model semantics unchanged.

    This function never replaces the input, writes a catalog binding, opens an
    MPH, or updates citation payloads. All symbols/value IDs remain intact.
    Tutorial metadata is removed only when no retained foreign key uses it.
    """
    source = Path(path).expanduser().resolve(strict=True)
    target = Path(output).expanduser().resolve()
    if not source.is_file() or source == target or target.exists():
        raise ValueError("Reclaim requires an existing source and a new staged output")
    for suffix in ("-wal", "-journal"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("Compact source has a live transaction sidecar")
    before = source.stat()
    original_sha = _hash_file(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with target.open("xb"):
            created = True
        with closing(_connect(source, read_only=True)) as old, closing(_connect(target)) as staged:
            old.execute("BEGIN")
            semantic_before = _semantic_digest(old)
            counts = {name: old.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in sorted(TUTORIAL_TABLES)}
            text_bytes = old.execute("SELECT COALESCE(sum(length(CAST(text AS BLOB))),0) FROM text_pool").fetchone()[0]
            old.backup(staged, pages=128, progress=lambda *_: check_cancelled())
            if _semantic_digest(staged) != semantic_before:
                raise ValueError("Staged backup changed compact model semantics")
            staged.execute("PRAGMA foreign_keys=ON")
            staged.execute("PRAGMA journal_mode=DELETE")
            staged.execute("PRAGMA secure_delete=ON")
            with staged:
                staged.execute("CREATE TEMP TABLE tutorial_metadata_candidates(id INTEGER PRIMARY KEY)")
                staged.execute("""INSERT OR IGNORE INTO temp.tutorial_metadata_candidates
                  SELECT metadata_id FROM documents WHERE metadata_id IS NOT NULL UNION
                  SELECT metadata_id FROM passages WHERE metadata_id IS NOT NULL""")
                staged.execute("DELETE FROM passages")
                staged.execute("DELETE FROM documents")
                staged.execute("DELETE FROM text_pool")
                tables = [row[0] for row in staged.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                metadata_sql = _retained_metadata_sql(staged, tables)
                removed_metadata = staged.execute("""DELETE FROM metadata_pool WHERE
                  id IN (SELECT id FROM temp.tutorial_metadata_candidates) AND id NOT IN (""" + metadata_sql + ")").rowcount
            staged.execute("VACUUM")
            if staged.execute("PRAGMA integrity_check").fetchone() != ("ok",) or staged.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Reclaimed compact database failed integrity checks")
            semantic_after = _semantic_digest(staged)
            if semantic_after != semantic_before:
                raise ValueError("Tutorial reclamation changed retained model/API data")
            if any(staged.execute("SELECT 1 FROM " + name + " LIMIT 1").fetchone() for name in TUTORIAL_TABLES):
                raise ValueError("Tutorial body rows remain after reclamation")
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or _hash_file(source) != original_sha:
            raise ValueError("Original compact database changed during reclamation")
        check_cancelled()
        current_sha = _hash_file(target)
        return {"source_path": str(source), "staged_path": str(target),
                "original_sha256": original_sha, "current_sha256": current_sha,
                "before_bytes": before.st_size, "after_bytes": target.stat().st_size,
                "reclaimed_bytes": before.st_size - target.stat().st_size,
                "removed_rows": counts, "removed_text_bytes": text_bytes,
                "removed_tutorial_metadata_rows": removed_metadata,
                "semantic_unchanged": True, "semantic_sha256_before": semantic_before["sha256"],
                "semantic_sha256_after": semantic_after["sha256"], "semantic_proof": semantic_after,
                "shared_pool_policy": "retain_all_symbol_value_ids_and_model_referenced_metadata"}
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                target.with_name(target.name + suffix).unlink(missing_ok=True)
        check_cancelled()
        raise
