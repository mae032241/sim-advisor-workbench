"""Read-only before/after audit of an offline tutorial normalization.

Only the explicitly named JSON report is written. Catalog, compact cases and
native/QMD databases are opened with mode=ro. The baseline contains hashes,
counts and a few bounded samples, never the complete retrieval corpus.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from advisor.catalog_storage import register_catalog_functions
from advisor.index_manifest_cache import read_manifest
from advisor.index_paths import resolve_index_manifest
from advisor.native_index import unpack
from advisor.retrieval_settings import read as read_embedding_settings
from advisor.tutorial_migration import TutorialMigrationPlan
from normalize_tutorials import ReadOnlyLibraryStore

SCHEMA = "tutorial-normalization-audit/v1"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def file_hash(path):
    path = Path(path)
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("Audit source changed while hashing: " + str(path))
    return digest.hexdigest()


def connect(path, *, catalog=False):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    if catalog:
        register_catalog_functions(db)
    db.execute("PRAGMA query_only=ON")
    db.create_function("audit_filename", 1, lambda value: "o-" + sha(value)[:32] + ".md", deterministic=True)
    return db


def attach(db, path, name):
    if name not in {"audit_plan", "audit_native"}:
        raise ValueError("Unsupported audit database alias")
    db.execute("ATTACH DATABASE ? AS " + name, (Path(path).resolve().as_uri() + "?mode=ro",))


def identities(rows):
    digest, count = hashlib.sha256(b"["), 0
    for row in rows:
        if count:
            digest.update(b",")
        digest.update(encoded(row[0]).encode("utf-8"))
        count += 1
    digest.update(b"]")
    return {"count": count, "sha256": digest.hexdigest(), "generation": digest.hexdigest()[:24]}


def rows_hash(rows):
    digest, count = hashlib.sha256(), 0
    for row in rows:
        digest.update(encoded(list(row)).encode("utf-8") + b"\n")
        count += 1
    return {"count": count, "sha256": digest.hexdigest()}


def _active_manifest(root):
    value = read_manifest(root / "search/active.json", summary=True)
    if value is None:
        raise ValueError("Active index manifest is unavailable")
    return resolve_index_manifest(value, root / "search")


def _model_databases(store, plan_db, catalog):
    databases, cases, candidates = {}, {}, []
    for identity, raw in plan_db.execute("SELECT id,payload FROM cases ORDER BY id"):
        obj = json.loads(raw)
        reference = obj.get("data", {}).get("compact_case")
        if not isinstance(reference, dict):
            continue
        relative = Path(reference["path"])
        path = (store.root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(store.root / "artifacts"):
            raise ValueError("Compact case path escapes owned artifacts")
        key = relative.as_posix()
        if key not in databases:
            digest = file_hash(path)
            if digest != reference["sha256"]:
                raise ValueError("Compact case hash differs from its catalog citation")
            databases[key] = {"sha256": digest, "bytes": path.stat().st_size}
        current = catalog.execute("SELECT payload FROM revisions WHERE id=?", (identity,)).fetchone()
        if current is None or json.loads(current[0]) != obj:
            raise ValueError("Immutable model case payload changed or disappeared")
        cases[identity] = sha(encoded(obj))
        title = str(obj.get("title", ""))
        candidates.append((0 if "head" in title.casefold() and "torso" in title.casefold() else 1, identity, key, title))
    samples = []
    for _, identity, key, title in sorted(candidates)[:3]:
        with closing(connect(store.root / key)) as db:
            values = db.execute("""SELECT p.node_id,n.tag_id,p.name_id,p.value_id,p.status_id,
              names.value,v.value_json FROM properties p JOIN symbols names ON names.id=p.name_id
              JOIN nodes n ON n.id=p.node_id JOIN value_pool v ON v.id=p.value_id
              WHERE names.value IN ('hmax','hauto','custom') ORDER BY names.value,p.node_id LIMIT 8""").fetchall()
            if not values:
                values = db.execute("""SELECT p.node_id,n.tag_id,p.name_id,p.value_id,p.status_id,
                  names.value,v.value_json FROM properties p JOIN symbols names ON names.id=p.name_id
                  JOIN nodes n ON n.id=p.node_id JOIN value_pool v ON v.id=p.value_id ORDER BY p.id LIMIT 5""").fetchall()
            samples.append({"case_id": identity, "title": title, "database": key,
                            "node_count": db.execute("SELECT count(*) FROM nodes").fetchone()[0],
                            "property_count": db.execute("SELECT count(*) FROM properties").fetchone()[0],
                            "settings": [list(row[:5]) + [row[5], json.loads(row[6])] for row in values]})
    return {"databases": databases, "case_payload_hashes": cases, "configuration_samples": samples}


def _model_libraries(plan_db):
    result = []
    for identity, raw in plan_db.execute("SELECT library_id,body FROM owners ORDER BY library_id"):
        if json.loads(raw).get("type") in {"official-models", "user-models"}:
            result.append(identity)
    return result


def _native_retained(native, library):
    def rows():
        for rowid, identity, kind, branch, locator, representation, search_input in native.execute("""
          SELECT o.rowid,o.id,o.kind,o.feature_branch,o.qmd_path,o.representation_json,o.search_input
          FROM objects o LEFT JOIN audit_plan.retirements r ON r.id=o.id
          WHERE o.library_id=? AND r.id IS NULL ORDER BY o.id""", (library,)):
            yield rowid, identity, kind, branch, locator, sha(representation), sha(search_input)
    result = rows_hash(rows())
    result["samples"] = []
    for rowid, identity, locator, raw in native.execute("""SELECT o.rowid,o.id,o.qmd_path,o.search_input FROM objects o
      LEFT JOIN audit_plan.retirements r ON r.id=o.id WHERE o.library_id=? AND r.id IS NULL ORDER BY o.id LIMIT 2""", (library,)):
        title, text, _ = unpack(raw)
        result["samples"].append({"rowid": rowid, "id": identity, "qmd_path": locator,
                                  "title": title[:160], "text": text[:240], "text_sha256": sha(text)})
    return result


def _qmd_retained(path, native_path, plan_path, library, *, after):
    key = "lib_" + sha(library)[:20]
    with closing(connect(path)) as db:
        attach(db, native_path, "audit_native")
        attach(db, plan_path, "audit_plan")
        if after and db.execute("""SELECT 1 FROM audit_plan.retirements r JOIN documents d
          ON d.path=audit_filename(r.id) AND d.collection='objects' WHERE r.library_id=? LIMIT 1""", (library,)).fetchone():
            raise ValueError("Retired tutorial citation remains in QMD: " + library)
        samples = []
        def rows():
            for identity, collection, path_, title, content_hash, active, body in db.execute("""
              SELECT d.id,d.collection,d.path,d.title,d.hash,d.active,c.doc FROM documents d
              JOIN content c ON c.hash=d.hash JOIN audit_native.objects o ON o.qmd_path=? || '/' || d.path
              LEFT JOIN audit_plan.retirements r ON r.id=o.id
              WHERE d.collection='objects' AND d.active=1 AND o.library_id=? AND r.id IS NULL ORDER BY d.path""", (key, library)):
                if len(samples) < 2:
                    samples.append({"document_id": identity, "path": path_, "title": title[:160],
                                    "content_hash": content_hash, "text": body[:240], "text_sha256": sha(body)})
                yield identity, collection, path_, title, content_hash, active, sha(body)
        result = rows_hash(rows())
        result["samples"] = samples
        if db.execute("SELECT 1 FROM content_vectors LIMIT 1").fetchone():
            raise ValueError("Keyword QMD unexpectedly contains embeddings: " + library)
        return result


def audit(data_dir, plan_path, *, baseline=None):
    store, plan = ReadOnlyLibraryStore(data_dir), TutorialMigrationPlan(plan_path)
    metadata = plan.metadata()
    if Path(metadata["source_catalog"]).resolve() != store.db_path.resolve():
        raise ValueError("Audit plan belongs to another catalog")
    after = baseline is not None
    manifest = _active_manifest(store.root)
    native_path = Path(manifest["directory"]) / "objects.sqlite3"
    embedding = read_embedding_settings(store.root)
    if embedding.get("enabled") or embedding.get("error") or manifest.get("modes", {}).get("qmd_vector", {}).get("ready"):
        raise ValueError("Audit requires embeddings to remain disabled")
    with closing(connect(store.db_path, catalog=True)) as catalog, closing(connect(plan.path)) as staged, closing(connect(native_path)) as native:
        attach(catalog, plan.path, "audit_plan")
        attach(native, plan.path, "audit_plan")
        active = identities(catalog.execute("SELECT citation_id FROM active ORDER BY citation_id"))
        native_ids = identities(native.execute("SELECT id FROM objects ORDER BY id"))
        if active != native_ids or active["generation"] != manifest["generation"] or active["count"] != manifest["object_count"]:
            raise ValueError("Catalog/native/manifest active identities or generation differ")
        if not after and active["generation"] != metadata["source_generation"]:
            raise ValueError("Before audit requires the plan's original catalog generation")
        expected = baseline["expected_active"] if after else identities(catalog.execute("""
          SELECT citation_id FROM active WHERE citation_id NOT IN (SELECT id FROM audit_plan.retirements)
          UNION SELECT id FROM audit_plan.canonical_objects ORDER BY 1"""))
        if after and active != expected:
            raise ValueError("Active catalog IDs do not match source minus retired plus canonical IDs")
        if after and (catalog.execute("SELECT 1 FROM active WHERE citation_id IN (SELECT id FROM audit_plan.retirements) LIMIT 1").fetchone() or
                      native.execute("SELECT 1 FROM objects WHERE id IN (SELECT id FROM audit_plan.retirements) LIMIT 1").fetchone()):
            raise ValueError("Retired tutorial ID remains active in catalog/native")
        model_state = _model_databases(store, staged, catalog)
        models = {}
        model_libraries = _model_libraries(staged)
        for library in model_libraries:
            if library not in manifest["libraries"]:
                if native.execute("SELECT 1 FROM objects WHERE library_id=? LIMIT 1", (library,)).fetchone():
                    raise ValueError("Model library has no QMD keyword database")
                continue
            models[library] = {"native": _native_retained(native, library),
                               "qmd": _qmd_retained(manifest["libraries"][library]["db_path"], native_path, plan.path, library, after=after)}
            if models[library]["native"]["count"] != models[library]["qmd"]["count"]:
                raise ValueError("Retained model native/QMD counts differ: " + library)
        # Every QMD library, including the canonical owner, must drop old paths.
        if after:
            for library, item in manifest["libraries"].items():
                if library not in models:
                    _qmd_retained(item["db_path"], native_path, plan.path, library, after=True)
        canonical_sources = dict(staged.execute("SELECT source_sha256,count(*) FROM canonical_objects GROUP BY source_sha256 ORDER BY source_sha256"))
        canonical_count = sum(canonical_sources.values())
        if after:
            checked = 0
            for identity, source_sha, expected_raw, raw, owner in catalog.execute("""
              SELECT c.id,c.source_sha256,c.payload,r.payload,a.library_id FROM audit_plan.canonical_objects c
              LEFT JOIN active a ON a.citation_id=c.id LEFT JOIN revisions r ON r.id=c.id ORDER BY c.id"""):
                if raw is None or owner != metadata["canonical_library_id"]:
                    raise ValueError("Canonical manual citation is not active under its owner")
                obj = json.loads(raw)
                if obj != json.loads(expected_raw) or obj.get("asset_hash") != source_sha:
                    raise ValueError("Canonical manual payload/source SHA differs from the reviewed plan")
                checked += 1
            if checked != canonical_count:
                raise ValueError("Canonical manual count differs from plan")
        result = {"schema": SCHEMA, "phase": "after" if after else "before", "status": "passed",
                  "checked_at": datetime.now(timezone.utc).isoformat(), "data_dir": str(store.root.parent),
                  "plan": str(plan.path), "plan_sha256": file_hash(plan.path), "migration_id": metadata["migration_id"],
                  "active": active, "expected_active": expected, "model_state": model_state,
                  "retained_model_indexes": models, "canonical_manual": {"count": canonical_count, "sources": canonical_sources},
                  "embedding_disabled": True, "active_directory": str(manifest["directory"])}
        if after:
            if baseline.get("schema") != SCHEMA or baseline.get("phase") != "before" or baseline.get("migration_id") != result["migration_id"] or baseline.get("plan_sha256") != result["plan_sha256"]:
                raise ValueError("Before audit belongs to another normalization plan")
            for key in ("model_state", "retained_model_indexes", "canonical_manual"):
                if result[key] != baseline[key]:
                    raise ValueError("Before/after invariant changed: " + key)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True, help="Before baseline, or combined before/after report")
    parser.add_argument("--phase", choices=("before", "after", "auto"), default="auto")
    args = parser.parse_args(argv)
    output = Path(args.output).expanduser().resolve()
    phase = args.phase if args.phase != "auto" else "after" if output.exists() else "before"
    try:
        baseline = None
        if phase == "after":
            saved = json.loads(output.read_text(encoding="utf-8"))
            baseline = saved.get("before", saved)
        elif output.exists():
            raise FileExistsError("Before audit refuses to overwrite an existing baseline")
        result = audit(args.data_dir, args.plan, baseline=baseline)
        report = {"before": baseline, "after": result} if baseline is not None else result
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(encoded({"status": "passed", "phase": phase, "report": str(output), "active": result["active"],
                       "model_databases": len(result["model_state"]["databases"]),
                       "canonical_manual_chunks": result["canonical_manual"]["count"]}))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(encoded({"status": "failed", "phase": phase, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
