"""Small, text-free dependency manifests for retained QMD generations.

The live runtime database owns profiles. Retained generations need only exact
case/source identities and library ownership to erase derived text on purge.
Immutable sidecars also keep an older manifest usable after publication fails.
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

from .index_manifest import _owned

SCHEMA = "case-context-dependencies/v1"
MANIFEST_KEY = "case_context_dependency_index"
_GENERATION = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_QMD_PATH = re.compile(r"lib_[a-f0-9]{20}/[A-Za-z0-9][A-Za-z0-9._-]*\.md\Z")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bounded(value, name, limit=256):
    if not isinstance(value, str) or not value or len(value) > limit or "\0" in value:
        raise ValueError("Invalid generation context " + name)
    return value


def _binding(search_root, active):
    root = Path(search_root).absolute()
    raw = active.get("directory")
    if not isinstance(raw, str) or not raw:
        raise ValueError("Generation dependencies require an owned generation")
    directory = Path(raw)
    directory = directory if directory.is_absolute() else root / directory
    directory = _owned(root, directory)
    parts = directory.relative_to(root).parts
    if len(parts) != 2 or parts[0] != "generations" or not _GENERATION.fullmatch(parts[1]):
        raise ValueError("Generation dependencies require generations/<directory>")
    generation = _bounded(active.get("generation"), "generation")
    corpus_hash = active.get("corpus_hash")
    if corpus_hash is not None:
        _bounded(corpus_hash, "corpus hash")
    return root, directory, {"schema": SCHEMA, "directory": Path(*parts).as_posix(),
                             "generation": generation, "corpus_hash": corpus_hash}


def _readonly(root, path):
    path = _owned(root, path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("Generation dependencies require an unshared regular database")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    return db


def _create(db):
    db.executescript("""CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE contexts(case_id TEXT PRIMARY KEY,library_id TEXT NOT NULL,qmd_path TEXT UNIQUE NOT NULL);
      CREATE TABLE dependencies(case_id TEXT NOT NULL,source_id TEXT NOT NULL,library_id TEXT NOT NULL,
        PRIMARY KEY(case_id,source_id));
      CREATE INDEX contexts_library ON contexts(library_id,case_id);
      CREATE INDEX dependencies_library ON dependencies(library_id,case_id);""")


def _insert_context(db, case_id, library_id, qmd_path):
    _bounded(case_id, "case ID")
    _bounded(library_id, "owner library ID")
    if not isinstance(qmd_path, str) or len(qmd_path) > 512 or not _QMD_PATH.fullmatch(qmd_path):
        raise ValueError("Generation context requires its exact QMD path")
    db.execute("INSERT INTO contexts VALUES(?,?,?)", (case_id, library_id, qmd_path))


def _insert_dependency(db, case_id, source_id, library_id):
    row = (_bounded(case_id, "dependent case ID"), _bounded(source_id, "source ID"),
           _bounded(library_id, "source library ID"))
    previous = db.execute("SELECT library_id FROM dependencies WHERE case_id=? AND source_id=?", row[:2]).fetchone()
    if previous is not None and previous[0] != library_id:
        raise ValueError("Source dependency has conflicting library ownership")
    db.execute("INSERT OR IGNORE INTO dependencies VALUES(?,?,?)", row)


def _from_profiles(db, profiles):
    items = profiles.values() if isinstance(profiles, dict) else profiles
    for entry in items:
        profile = entry.get("profile", entry)
        case_id = profile.get("case_id")
        _insert_context(db, case_id, profile.get("library_id"), entry.get("qmd_path", profile.get("qmd_path")))
        for ref in profile.get("source_refs") or profile.get("excerpts") or ():
            _insert_dependency(db, case_id, ref.get("id", ref.get("source_id")), ref.get("library_id"))


def _from_runtime(db, root, binding):
    from .case_context_store import context_path
    with closing(_readonly(root, context_path(root))) as source:
        meta = {key: json.loads(raw) for key, raw in source.execute(
            "SELECT key,value FROM meta WHERE key IN ('generation','corpus_hash')")}
        if any(meta.get(key) != binding[key] for key in ("generation", "corpus_hash")):
            raise ValueError("Runtime context dependencies belong to another generation")
        # Never select profile_json: corpus size must not materialize profiles.
        for row in source.execute("SELECT case_id,library_id,qmd_path FROM contexts ORDER BY case_id"):
            _insert_context(db, *row)
        for row in source.execute("SELECT case_id,source_id,library_id FROM dependencies ORDER BY case_id,source_id"):
            _insert_dependency(db, *row)


def _counts(db):
    if db.execute("SELECT 1 FROM contexts c WHERE NOT EXISTS(SELECT 1 FROM dependencies d WHERE d.case_id=c.case_id) LIMIT 1").fetchone():
        raise ValueError("Generation contexts require exact source dependencies")
    if db.execute("SELECT 1 FROM dependencies d WHERE NOT EXISTS(SELECT 1 FROM contexts c WHERE c.case_id=d.case_id) LIMIT 1").fetchone():
        raise ValueError("Generation dependency refers to an unknown case")
    return {"context_count": db.execute("SELECT count(*) FROM contexts").fetchone()[0],
            "dependency_count": db.execute("SELECT count(*) FROM dependencies").fetchone()[0]}


def _digest(db, binding):
    digest = hashlib.sha256((_json(binding) + "\n").encode("utf-8"))
    for table, columns, order in (("contexts", "case_id,library_id,qmd_path", "case_id"),
                                   ("dependencies", "case_id,source_id,library_id", "case_id,source_id")):
        digest.update((table + "\n").encode("ascii"))
        for row in db.execute("SELECT " + columns + " FROM " + table + " ORDER BY " + order):
            digest.update((_json(row) + "\n").encode("utf-8"))
    return digest.hexdigest()


def _finish(root, directory, binding, db):
    counts = _counts(db)
    digest = _digest(db, binding)
    meta = dict(binding, **counts, sha256=digest)
    db.executemany("INSERT INTO meta VALUES(?,?)", ((key, _json(value)) for key, value in meta.items()))
    db.commit()
    path = _owned(root, directory / ("case-context-dependencies-" + digest + ".sqlite3"))
    return path, {"schema": SCHEMA, "path": path.relative_to(root).as_posix(), "sha256": digest, **counts}


def _write(search_root, active, populate):
    root, directory, binding = _binding(search_root, active)
    if not directory.is_dir():
        raise ValueError("Generation dependency directory does not exist")
    stage = directory / ("case-context-dependencies-" + uuid.uuid4().hex + ".tmp")
    try:
        with closing(sqlite3.connect(stage)) as db:
            _create(db)
            populate(db, root, binding)
            target, descriptor = _finish(root, directory, binding, db)
        if os.path.lexists(target):
            # Content-addressed metadata is immutable, including on rollback.
            _validated(search_root, dict(active, **{MANIFEST_KEY: descriptor}))
        else:
            os.replace(stage, target)
        return descriptor
    finally:
        stage.unlink(missing_ok=True)


def write_generation_dependencies(search_root, active, profiles=None):
    """Write text-free generation metadata, streaming runtime SQL by default."""
    return _write(search_root, active, lambda db, root, binding:
                  _from_runtime(db, root, binding) if profiles is None else _from_profiles(db, profiles))


def mark_generation_dependencies(search_root, active, profiles=None):
    """Return a compact manifest; the legacy per-case JSON map is dropped."""
    descriptor = write_generation_dependencies(search_root, active, profiles)
    value = dict(active)
    value.pop("case_context_dependencies", None)
    value.update(case_contexts_required=True, case_context_count=descriptor["context_count"])
    value[MANIFEST_KEY] = descriptor
    return value


def _validated(search_root, active):
    root, directory, binding = _binding(search_root, active)
    descriptor = active.get(MANIFEST_KEY)
    if not isinstance(descriptor, dict) or descriptor.get("schema") != SCHEMA:
        raise ValueError("Unsupported generation context dependency index")
    digest = descriptor.get("sha256")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ValueError("Invalid generation dependency digest")
    expected = (directory / ("case-context-dependencies-" + digest + ".sqlite3")).relative_to(root).as_posix()
    if descriptor.get("path") != expected:
        raise ValueError("Generation dependency locator differs from its owned generation")
    path = root / expected
    with closing(_readonly(root, path)) as db:
        meta = {key: json.loads(raw) for key, raw in db.execute("SELECT key,value FROM meta")}
        counts = _counts(db)
        if (any(meta.get(key) != value for key, value in binding.items()) or meta.get("sha256") != digest
                or any(type(descriptor.get(key)) is not int or descriptor[key] != value
                       or type(meta.get(key)) is not int or meta[key] != value
                       for key, value in counts.items()) or _digest(db, binding) != digest):
            raise ValueError("Generation context dependency binding is stale or invalid")
    return path, descriptor


def _legacy(active):
    values = active.get("case_context_dependencies", {})
    if not isinstance(values, dict):
        raise ValueError("Invalid legacy generation context dependencies")
    for identity, owners in values.items():
        _bounded(identity, "legacy case ID")
        if not isinstance(owners, list) or not owners:
            raise ValueError("Invalid legacy context library ownership")
        for owner in owners:
            _bounded(owner, "legacy library ID")
    return values


def dependent_case_ids(search_root, active, library_id):
    """Return exact case IDs for purge, including historical inline manifests."""
    _bounded(library_id, "purged library ID")
    if MANIFEST_KEY not in active:
        return {identity for identity, owners in _legacy(active).items() if library_id in owners}
    path, _ = _validated(search_root, active)
    with closing(_readonly(Path(search_root).absolute(), path)) as db:
        return {row[0] for row in db.execute("SELECT case_id FROM contexts WHERE library_id=? "
                "UNION SELECT case_id FROM dependencies WHERE library_id=?", (library_id, library_id))}


def remove_generation_dependencies(search_root, active, case_ids):
    """Return a new immutable metadata locator after overviews are reverted.

    Call only after the bounded QMD and statistics patches succeed. The old
    sidecar remains available to any still-published older manifest.
    """
    identities = set(case_ids)
    for identity in identities:
        _bounded(identity, "removed case ID")
    value = dict(active)
    if MANIFEST_KEY not in active:
        value["case_context_dependencies"] = {identity: owners for identity, owners in _legacy(active).items()
                                               if identity not in identities}
        value["case_context_count"] = len(value["case_context_dependencies"])
        return value
    path, _ = _validated(search_root, active)
    if not identities:
        return value

    def populate(db, root, binding):
        with closing(_readonly(root, path)) as source:
            for row in source.execute("SELECT case_id,library_id,qmd_path FROM contexts ORDER BY case_id"):
                if row[0] not in identities:
                    _insert_context(db, *row)
            for row in source.execute("SELECT case_id,source_id,library_id FROM dependencies ORDER BY case_id,source_id"):
                if row[0] not in identities:
                    _insert_dependency(db, *row)

    value[MANIFEST_KEY] = _write(search_root, active, populate)
    value.pop("case_context_dependencies", None)
    value["case_context_count"] = value[MANIFEST_KEY]["context_count"]
    return value


def prune_generation_dependencies(search_root, active):
    """Delete unreferenced metadata snapshots only after publication commits."""
    from .case_context_store import assert_no_pending
    from .index_manifest_cache import read_manifest
    assert_no_pending(search_root)
    root, directory, _ = _binding(search_root, active)
    keep = _validated(search_root, active)[0] if MANIFEST_KEY in active else None
    published = read_manifest(directory / "manifest.json", summary=True)
    if (published is None or published.get("generation") != active.get("generation")
            or published.get(MANIFEST_KEY) != active.get(MANIFEST_KEY)
            or (keep is None and _legacy(published) != _legacy(active))):
        raise ValueError("Dependency pruning requires the published generation manifest")
    strict = re.compile(r"case-context-dependencies-[a-f0-9]{64}\.sqlite3\Z")
    obsolete = []
    for candidate in directory.iterdir():
        if candidate == keep or not strict.fullmatch(candidate.name):
            continue
        candidate = _owned(root, candidate)
        if not candidate.is_file() or candidate.stat().st_nlink != 1:
            raise ValueError("Dependency pruning refuses shared or nonregular files")
        obsolete.append(candidate)
    for candidate in obsolete:
        candidate.unlink()
    return {"removed_snapshots": len(obsolete)}
