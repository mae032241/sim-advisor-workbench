"""Generation-bound, disposable metadata for source-derived case summaries.

Profiles describe retrieved documentation. They never authorize a source read:
the search adapter checks their exact dependencies against live catalog policy.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from .catalog_storage import register_catalog_functions, revision_storage
from .index_manifest import _owned
from .query_scope_counts import database_signature

SCHEMA = "case-context/v1"
FILENAME = "case-contexts.sqlite3"
PENDING_FILENAME = "case-context-update.json"
BATCH_LIMIT = 64
MAX_BATCH_BYTES = 4 * 1024 * 1024
_BATCH_UNDO_SCHEMA = "case-context-batch-undo/v1"
_VALIDATED = OrderedDict()
_VALIDATED_LOCK = threading.Lock()
_SCHEMA_SQL = (
    "CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE TABLE contexts(case_id TEXT PRIMARY KEY,library_id TEXT NOT NULL,case_group TEXT NOT NULL,"
    "profile_hash TEXT NOT NULL,profile_json TEXT NOT NULL,qmd_path TEXT UNIQUE NOT NULL,qmd_hash TEXT NOT NULL)",
    "CREATE TABLE dependencies(case_id TEXT NOT NULL,source_id TEXT NOT NULL,library_id TEXT NOT NULL,"
    "object_id TEXT NOT NULL,PRIMARY KEY(case_id,source_id))",
    "CREATE TABLE capabilities(case_id TEXT PRIMARY KEY,classification TEXT NOT NULL,evidence_json TEXT NOT NULL)",
    "CREATE INDEX capabilities_classification ON capabilities(classification,case_id)",
)


class CaseContextStale(RuntimeError):
    """An enriched index has lost the metadata needed to authorize its text."""


def context_path(search_root):
    return Path(search_root).absolute() / "runtime" / FILENAME


def pending_path(search_root):
    return Path(search_root).absolute() / "runtime" / PENDING_FILENAME


def assert_no_pending(search_root):
    # lexists also catches dangling links and directories: any marker presence
    # means publication may have stopped between the QMD and metadata writes.
    if os.path.lexists(pending_path(search_root)):
        raise RuntimeError("Case context update is pending; finish or recover it before searching")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _connect(path):
    connection = register_catalog_functions(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True))
    connection.execute("PRAGMA query_only=ON")
    return connection


def _identity(search_root, active):
    root = Path(search_root).resolve()
    directory = Path(active.get("directory") or "")
    directory = directory.resolve() if directory.is_absolute() else (root / directory).resolve()
    if not directory.is_relative_to(root / "generations") or not (directory / "objects.sqlite3").is_file():
        raise CaseContextStale("Case context requires the current owned QMD generation")
    return {"generation": active.get("generation"), "corpus_hash": active.get("corpus_hash"),
            "objects_signature": database_signature(directory / "objects.sqlite3")}


def _read_meta(db):
    """Validate disposable storage shape, never live source authorization."""
    meta = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM meta")}
    for table, columns in (("contexts", {"case_id", "library_id", "case_group", "profile_hash", "profile_json", "qmd_path", "qmd_hash"}),
                           ("dependencies", {"case_id", "source_id", "library_id", "object_id"}),
                           ("capabilities", {"case_id", "classification", "evidence_json"})):
        if not columns <= {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}:
            raise ValueError("Context metadata schema is incomplete")
    for table, key in (("contexts", "context_count"), ("dependencies", "dependency_count"),
                       ("capabilities", "capability_count")):
        if type(meta.get(key)) is not int or db.execute("SELECT count(*) FROM " + table).fetchone()[0] != meta[key]:
            raise ValueError("Context metadata row binding is incomplete")
    if db.execute("SELECT 1 FROM contexts c WHERE NOT EXISTS(SELECT 1 FROM dependencies d WHERE d.case_id=c.case_id) LIMIT 1").fetchone():
        raise ValueError("Context metadata has lost its source dependencies")
    if meta.get("schema") != SCHEMA:
        raise ValueError("Unknown context metadata schema")
    return meta


def validated_contexts(search_root, active):
    """Return metadata binding, or None for an untouched historical index."""
    assert_no_pending(search_root)
    path = context_path(search_root)
    required = active.get("case_contexts_required") is True or active.get("case_context_schema") is not None
    if not os.path.lexists(path):
        if required:
            raise CaseContextStale("Required case context metadata is missing; rebuild or recover the context index")
        return None
    try:
        _owned(Path(search_root).absolute(), path)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError("Context metadata must be an unshared regular database")
        identity = _identity(search_root, active)
        signature = database_signature(path, change_counter=True)
        key = _json([os.path.normcase(str(path)), signature, identity])
        # Only stable schema/count validation is reused. Pending markers,
        # ownership, current generation identity and all live gates are checked
        # independently on every call.
        with _VALIDATED_LOCK:
            meta = _VALIDATED.get(key)
        if meta is None:
            with closing(_connect(path)) as db:
                meta = _read_meta(db)
        if any(meta.get(key) != value for key, value in identity.items()):
            raise ValueError("Context metadata belongs to a different generation")
        if required and active.get("case_context_schema", SCHEMA) != SCHEMA:
            raise ValueError("Unknown required context schema")
        if database_signature(path, change_counter=True) != signature or _identity(search_root, active) != identity:
            raise ValueError("Context metadata changed while validating its binding")
        with _VALIDATED_LOCK:
            _VALIDATED[key] = deepcopy(meta)
            _VALIDATED.move_to_end(key)
            while len(_VALIDATED) > 8:
                _VALIDATED.popitem(last=False)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise CaseContextStale("Case context metadata is stale or unreadable; prepare it for the current QMD generation") from exc
    return {"path": str(path), "meta": deepcopy(meta)}


def read_contexts(search_root, active=None):
    """Read all cached profiles, optionally requiring their current binding."""
    located = validated_contexts(search_root, active) if active is not None else None
    path = Path(located["path"]) if located else context_path(search_root)
    if not path.is_file():
        return {}
    _owned(Path(search_root).absolute(), path)
    with closing(_connect(path)) as db:
        result = {}
        for identity, digest, raw, qmd_path, qmd_hash in db.execute(
                "SELECT case_id,profile_hash,profile_json,qmd_path,qmd_hash FROM contexts ORDER BY case_id"):
            if hashlib.sha256(raw.encode("utf-8")).hexdigest() != digest:
                raise CaseContextStale("Case context profile hash is invalid")
            profile = json.loads(raw)
            if not isinstance(profile, dict) or profile.get("case_id") != identity:
                raise CaseContextStale("Case context profile identity is invalid")
            result[identity] = dict(profile, qmd_path=qmd_path, qmd_hash=qmd_hash)
    return result


def load_context(search_root, active, case_id):
    """Read one profile only; the caller still must authorize its dependencies."""
    return load_contexts(search_root, active, [case_id]).get(case_id)


def load_contexts(search_root, active, case_ids):
    """Read a bounded result batch with one generation/schema validation."""
    case_ids = list(dict.fromkeys(case_ids))
    if not case_ids:
        return {}
    if len(case_ids) > 100:
        raise ValueError("Case context reads require a bounded result batch")
    located = validated_contexts(search_root, active)
    if located is None:
        return {}
    with closing(_connect(located["path"])) as db:
        rows = db.execute("SELECT case_id,profile_hash,profile_json FROM contexts WHERE case_id IN (" +
                          ",".join("?" for _ in case_ids) + ")", case_ids).fetchall()
    result = {}
    for case_id, digest, raw in rows:
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != digest:
            raise CaseContextStale("Case context profile hash is invalid")
        profile = json.loads(raw)
        if not isinstance(profile, dict) or profile.get("case_id") != case_id:
            raise CaseContextStale("Case context profile identity is invalid")
        result[case_id] = profile
    return result


def _bounded(value, name, limit=1024):
    if not isinstance(value, str) or not value or len(value) > limit or "\0" in value:
        raise ValueError("Invalid case context " + name)
    return value


def _profile_rows(search_root, contexts):
    profiles = list(contexts.values()) if isinstance(contexts, dict) else list(contexts)
    rows, dependencies = [], []
    catalog_path = Path(search_root).parent / "catalog.sqlite3"
    with closing(_connect(catalog_path)) as catalog:
        revision_table = "_revision_payloads" if revision_storage(catalog) == "catalog-revisions/zlib-v1" else "revisions"
        seen = set()
        for entry in profiles:
            profile = dict(entry.get("profile", entry))
            case_id = _bounded(profile.get("case_id"), "case_id", 256)
            if case_id in seen or profile.get("schema", SCHEMA) != SCHEMA:
                raise ValueError("Duplicate or unsupported case context profile")
            seen.add(case_id)
            library_id = _bounded(profile.get("library_id"), "library_id", 256)
            case_group = _bounded(profile.get("case_group"), "case_group", 512)
            qmd_path = entry.get("qmd_path", profile.get("qmd_path"))
            qmd_hash = entry.get("qmd_hash", profile.get("qmd_hash"))
            if not isinstance(qmd_path, str) or not re.fullmatch(r"lib_[a-f0-9]{20}/[A-Za-z0-9][A-Za-z0-9._-]*\.md", qmd_path):
                raise ValueError("Case context requires its exact generated QMD path")
            if not isinstance(qmd_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", qmd_hash):
                raise ValueError("Case context requires its indexed QMD content hash")
            profile.pop("qmd_path", None)
            profile.pop("qmd_hash", None)
            raw = _json(profile)
            refs = profile.get("source_refs") or profile.get("excerpts") or []
            dep_rows = {}
            for ref in refs:
                if not isinstance(ref, dict):
                    raise ValueError("Case context source refs must contain exact source identities")
                source_id = _bounded(ref.get("source_id", ref.get("id")), "source_id", 256)
                identity = catalog.execute("SELECT library_id,object_id FROM " + revision_table + " WHERE id=?", (source_id,)).fetchone()
                if identity is None or identity[0] != ref.get("library_id") or ref.get("object_id", identity[1]) != identity[1]:
                    raise ValueError("Case context dependency differs from its immutable catalog identity")
                dep_rows[source_id] = (case_id, source_id, identity[0], identity[1])
            if not dep_rows:
                raise ValueError("Source-derived context text requires exact dependencies")
            rows.append((case_id, library_id, case_group, hashlib.sha256(raw.encode("utf-8")).hexdigest(), raw, qmd_path, qmd_hash))
            dependencies.extend(dep_rows.values())
    return rows, dependencies


def _capability_rows(capabilities):
    capability_items = list(capabilities.items()) if isinstance(capabilities, dict) else [
        (value["case_id"], value) for value in capabilities]
    capability_rows = []
    seen = set()
    for case_id, item in capability_items:
        _bounded(case_id, "capability case_id", 256)
        if case_id in seen:
            raise ValueError("Duplicate case capability")
        seen.add(case_id)
        item = {"classification": item} if isinstance(item, str) else dict(item)
        classification = item.get("classification", "unknown")
        if classification not in {"geometry_only", "configured", "unknown"}:
            raise ValueError("Unsupported model completeness classification")
        capability_rows.append((case_id, classification, _json(item)))
    return capability_rows


def _capability_hash(rows):
    return hashlib.sha256(_json(sorted(rows)).encode("utf-8")).hexdigest()


def write_contexts(search_root, active, contexts, capabilities, *, output=None):
    """Atomically replace the sidecar; QMD files and manifests are untouched.

    Contexts are full profile dictionaries plus qmd_path/qmd_hash. Capabilities
    accepts a case-id mapping to classification dictionaries (or strings), or an
    iterable of dictionaries containing case_id. All source refs are exact
    immutable citations and are checked through catalog scalar identity columns.
    """
    search_root = Path(search_root).absolute()
    target = context_path(search_root)
    if output is not None and Path(output).absolute() != target:
        raise ValueError("Case contexts must use the fixed owned runtime path")
    _owned(search_root, target)
    if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
        raise ValueError("Case contexts refuse shared or nonregular files")
    meta = {"schema": SCHEMA, **_identity(search_root, active), "source_text_is_read_evidence": False}
    rows, dependencies = _profile_rows(search_root, contexts)
    capability_rows = _capability_rows(capabilities)
    meta.update(context_count=len(rows), dependency_count=len(dependencies), capability_count=len(capability_rows),
                capability_hash=_capability_hash(capability_rows))
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with closing(sqlite3.connect(stage)) as db, db:
            for statement in _SCHEMA_SQL:
                db.execute(statement)
            db.executemany("INSERT INTO meta VALUES(?,?)", ((key, _json(value)) for key, value in meta.items()))
            db.executemany("INSERT INTO contexts VALUES(?,?,?,?,?,?,?)", rows)
            db.executemany("INSERT INTO dependencies VALUES(?,?,?,?)", dependencies)
            db.executemany("INSERT INTO capabilities VALUES(?,?,?)", capability_rows)
        os.replace(stage, target)
    finally:
        if stage.exists():
            stage.unlink()
    return {"path": str(target), "meta": meta, "context_count": len(rows), "capability_count": len(capability_rows)}


def _batch_target(search_root):
    root = Path(search_root).absolute()
    target = context_path(root)
    _owned(root, target)
    if os.path.lexists(target) and (not target.is_file() or target.stat().st_nlink != 1):
        raise ValueError("Case contexts refuse shared or nonregular files")
    return root, target


def _batch_rows(search_root, profiles, capabilities):
    profiles = list(profiles.values()) if isinstance(profiles, dict) else list(profiles)
    if len(profiles) > BATCH_LIMIT:
        raise ValueError("Case context batch exceeds the bounded profile limit")
    if len(_json(profiles).encode("utf-8")) > MAX_BATCH_BYTES:
        raise ValueError("Case context batch exceeds the bounded byte limit")
    rows, dependencies = _profile_rows(search_root, profiles)
    cap_rows = None if capabilities is None else _capability_rows(capabilities)
    if cap_rows is not None and len(_json(cap_rows).encode("utf-8")) > MAX_BATCH_BYTES:
        raise ValueError("Case capability batch exceeds the bounded byte limit")
    return rows, dependencies, cap_rows


def _capability_change(db, meta, rows):
    if rows is None:
        return False, meta.get("capability_hash")
    digest = _capability_hash(rows)
    previous = meta.get("capability_hash")
    if previous is None:
        # Existing v1 sidecars gain this scalar once; ordinary later batches
        # never rescan or rewrite the complete capability table.
        previous = _capability_hash(db.execute(
            "SELECT case_id,classification,evidence_json FROM capabilities").fetchall())
    return previous != digest, digest


def prepare_context_batch(search_root, active, profiles, *, capabilities=None):
    """Capture only the selected old rows for a durable publication journal.

    Call before changing QMD/object metadata or creating the pending marker.
    Passing capabilities replaces that complete classification set only when
    its canonical hash changes. No context profile outside this batch is read.
    """
    root, target = _batch_target(search_root)
    rows, _, cap_rows = _batch_rows(root, profiles, capabilities)
    located = validated_contexts(root, active)
    undo = {"schema": _BATCH_UNDO_SCHEMA, "existed": located is not None,
            "case_ids": [row[0] for row in rows], "contexts": [], "dependencies": [], "meta": [],
            "capability_override_applied": False}
    if located is not None:
        signature = database_signature(target, change_counter=True)
        with closing(_connect(target)) as db:
            undo["meta"] = db.execute("SELECT key,value FROM meta ORDER BY key").fetchall()
            for identity in undo["case_ids"]:
                undo["contexts"].extend(db.execute("SELECT * FROM contexts WHERE case_id=?", (identity,)).fetchall())
                undo["dependencies"].extend(db.execute("SELECT * FROM dependencies WHERE case_id=? ORDER BY source_id", (identity,)).fetchall())
            changed, _ = _capability_change(db, located["meta"], cap_rows)
            undo["capability_override_applied"] = changed
            if changed:
                undo["capabilities"] = db.execute("SELECT * FROM capabilities ORDER BY case_id").fetchall()
        if database_signature(target, change_counter=True) != signature:
            raise CaseContextStale("Case context metadata changed while preparing its undo journal")
        undo["signature"] = signature
    if len(_json(undo).encode("utf-8")) > MAX_BATCH_BYTES:
        raise ValueError("Case context undo exceeds the bounded byte limit")
    # Tuples must round-trip exactly through a JSON publication journal.
    return json.loads(_json(undo))


def _checkpoint(db):
    if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
        raise RuntimeError("Case context metadata checkpoint is busy")


def _write_meta(db, meta):
    db.execute("DELETE FROM meta")
    db.executemany("INSERT INTO meta VALUES(?,?)", ((key, _json(value)) for key, value in meta.items()))


def _batch_report(target, meta):
    return {"path": str(target), "meta": meta, "context_count": meta["context_count"],
            "capability_count": meta["capability_count"]}


def _remove_database(target):
    for suffix in ("-wal", "-shm", "-journal", ""):
        path = Path(str(target) + suffix)
        _owned(target.parent.parent, path)
        if os.path.lexists(path):
            if not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError("Case context cleanup refuses shared or nonregular files")
            path.unlink()


def upsert_context_batch(search_root, active, profiles, *, capabilities=None, expected=None):
    """Commit at most 64 profile replacements in one SQLite transaction.

    Existing unrelated profiles stay in place. The caller publishes QMD and
    object statistics first; the metadata binds to that current object file.
    A supplied prepare result guards against an intervening sidecar writer.
    """
    root, target = _batch_target(search_root)
    rows, dependencies, cap_rows = _batch_rows(root, profiles, capabilities)
    identity = _identity(root, active)
    existed = target.exists()
    if expected is not None:
        if (expected.get("existed") is not existed or
                existed and expected.get("signature") != database_signature(target, change_counter=True)):
            raise CaseContextStale("Case context metadata changed after preparing its undo journal")
        if expected.get("case_ids") != [row[0] for row in rows]:
            raise ValueError("Case context batch differs from its prepared undo journal")
    target.parent.mkdir(parents=True, exist_ok=True)
    committed = False
    try:
        with closing(sqlite3.connect(target)) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                if (expected is not None and existed and
                        expected.get("signature") != database_signature(target, change_counter=True)):
                    raise CaseContextStale("Case context metadata changed after preparing its undo journal")
                if existed:
                    meta = _read_meta(db)
                    if any(meta.get(key) != identity[key] for key in ("generation", "corpus_hash")):
                        raise CaseContextStale("Case context metadata belongs to a different generation")
                else:
                    for statement in _SCHEMA_SQL:
                        db.execute(statement)
                    meta = {"schema": SCHEMA, "source_text_is_read_evidence": False,
                            "context_count": 0, "dependency_count": 0, "capability_count": 0}
                changed, cap_hash = _capability_change(db, meta, cap_rows)
                if (expected is not None and existed and
                        changed != expected.get("capability_override_applied")):
                    raise ValueError("Case capability replacement differs from its prepared undo journal")
                for row in rows:
                    db.execute("DELETE FROM dependencies WHERE case_id=?", (row[0],))
                    db.execute("DELETE FROM contexts WHERE case_id=?", (row[0],))
                db.executemany("INSERT INTO contexts VALUES(?,?,?,?,?,?,?)", rows)
                db.executemany("INSERT INTO dependencies VALUES(?,?,?,?)", dependencies)
                if changed:
                    db.execute("DELETE FROM capabilities")
                    db.executemany("INSERT INTO capabilities VALUES(?,?,?)", cap_rows)
                meta.update(identity)
                for table, key in (("contexts", "context_count"), ("dependencies", "dependency_count"),
                                   ("capabilities", "capability_count")):
                    meta[key] = db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                if cap_hash is not None:
                    meta["capability_hash"] = cap_hash
                _write_meta(db, meta)
                db.commit()
                committed = True
            except BaseException:
                db.rollback()
                _checkpoint(db)
                raise
            _checkpoint(db)
    except BaseException:
        if not existed and not committed:
            _remove_database(target)
        raise
    return _batch_report(target, meta)


def _validate_undo(undo):
    if (not isinstance(undo, dict) or undo.get("schema") != _BATCH_UNDO_SCHEMA or
            type(undo.get("existed")) is not bool or type(undo.get("capability_override_applied")) is not bool or
            len(_json(undo).encode("utf-8")) > MAX_BATCH_BYTES):
        raise ValueError("Invalid bounded case context undo journal")
    identities = undo.get("case_ids")
    if (not isinstance(identities, list) or len(identities) > BATCH_LIMIT or
            any(not isinstance(identity, str) for identity in identities) or
            len(set(identities)) != len(identities)):
        raise ValueError("Invalid case context undo identities")
    for identity in identities:
        _bounded(identity, "undo case_id", 256)
    for table, width in (("contexts", 7), ("dependencies", 4), ("meta", 2)):
        rows = undo.get(table)
        if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != width for row in rows):
            raise ValueError("Invalid case context undo rows")
        if table != "meta" and any(row[0] not in identities for row in rows):
            raise ValueError("Case context undo contains an unrelated profile")
    if undo["capability_override_applied"]:
        rows = undo.get("capabilities")
        if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != 3 for row in rows):
            raise ValueError("Invalid case capability undo rows")


def restore_context_batch(search_root, active, undo):
    """Undo a batch while binding restored rows to current object metadata."""
    _validate_undo(undo)
    root, target = _batch_target(search_root)
    if not undo["existed"]:
        _remove_database(target)
        return None
    if not target.is_file():
        raise CaseContextStale("Case context metadata is missing during batch recovery")
    identity = _identity(root, active)
    meta = {key: json.loads(value) for key, value in undo["meta"]}
    if meta.get("schema") != SCHEMA or any(meta.get(key) != identity[key] for key in ("generation", "corpus_hash")):
        raise CaseContextStale("Case context undo belongs to a different generation")
    with closing(sqlite3.connect(target)) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            for case_id in undo["case_ids"]:
                db.execute("DELETE FROM dependencies WHERE case_id=?", (case_id,))
                db.execute("DELETE FROM contexts WHERE case_id=?", (case_id,))
            db.executemany("INSERT INTO contexts VALUES(?,?,?,?,?,?,?)", undo["contexts"])
            db.executemany("INSERT INTO dependencies VALUES(?,?,?,?)", undo["dependencies"])
            if undo["capability_override_applied"]:
                db.execute("DELETE FROM capabilities")
                db.executemany("INSERT INTO capabilities VALUES(?,?,?)", undo["capabilities"])
            meta.update(identity)
            _write_meta(db, meta)
            _read_meta(db)
            db.commit()
        except BaseException:
            db.rollback()
            _checkpoint(db)
            raise
        _checkpoint(db)
    return _batch_report(target, meta)


def dependency_predicate(*, alias="o", revision_table="_revision_payloads", holdout=False):
    """Same small scalar gate is evaluated in Python and QMD before TopK."""
    if revision_table not in {"_revision_payloads", "revisions"}:
        raise ValueError("Unsupported catalog identity table")
    blocked = ("NOT EXISTS(SELECT 1 FROM case_context_catalog.libraries l WHERE l.id=dep.library_id "
               "AND coalesce(json_extract(l.body,'$.enabled'),1)=1 AND json_extract(l.body,'$.deleted_at') IS NULL) "
               "OR NOT EXISTS(SELECT 1 FROM case_context_catalog." + revision_table + " r "
               "JOIN case_context_catalog.active a ON a.library_id=r.library_id AND a.object_id=r.object_id AND a.citation_id=r.id "
               "WHERE r.id=dep.source_id AND r.library_id=dep.library_id AND r.object_id=dep.object_id)")
    context_blocked = "0"
    if holdout:
        blocked += (" OR EXISTS(SELECT 1 FROM evaluation_holdout.identities h WHERE h.id=dep.source_id OR h.id=dep.object_id)"
                    " OR EXISTS(SELECT 1 FROM evaluation_holdout.case_groups h JOIN case_context_catalog.case_members m "
                    "ON m.group_id=h.case_group WHERE m.citation_id=dep.source_id)"
                    " OR EXISTS(SELECT 1 FROM evaluation_holdout.paths h JOIN authoritative_objects.objects x "
                    "ON x.qmd_path=h.path WHERE x.id=dep.source_id)")
        context_blocked = "EXISTS(SELECT 1 FROM evaluation_holdout.case_groups h WHERE h.case_group=c.case_group)"
    return (f"NOT EXISTS(SELECT 1 FROM case_contexts.contexts c WHERE c.case_id={alias}.id AND (" + context_blocked +
            " OR EXISTS(SELECT 1 FROM case_contexts.dependencies dep WHERE dep.case_id=c.case_id AND (" + blocked + "))))")


def capabilities_predicate(*, alias="o"):
    return f"NOT EXISTS(SELECT 1 FROM case_contexts.capabilities cap WHERE cap.case_id={alias}.id AND cap.classification='geometry_only')"
