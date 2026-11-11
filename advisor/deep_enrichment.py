"""Resumable serial API enrichment of retained compact model cases.

Saved XML revisions and databases remain immutable. Each case publication is a
small catalog transaction; one optional index build follows the complete batch.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import copy
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
import zlib

from .build_control import BuildCancelled, BuildControl, build_control, check_cancelled
from .library_store import encoded, now, process_alive

SCHEMA = "deep-api-batch/v1"
OVERLAY_SCHEMA = "deep-api-overlay/v1"
_DONE = {"completed", "partial", "failed", "skipped"}
_MODES = {"native", "qmd_keyword"}


def _batch_path(store, path):
    path = Path(path).absolute()
    directory = store.root / "deep-api-batches"
    if (path.parent != directory or path.suffix != ".sqlite3"
            or not path.resolve().is_relative_to(directory.resolve())):
        raise ValueError("Deep API checkpoints must be direct .sqlite3 files in libraries/deep-api-batches")
    for item in (directory, path):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise ValueError("Deep API checkpoints refuse symlink/junction paths")
    return path


def _checkpoint(store, path):
    path = _batch_path(store, path)
    if not path.is_file():
        raise FileNotFoundError("Deep API batch checkpoint not found")
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        meta = dict(db.execute("SELECT key,value FROM meta"))
        if meta.get("schema") != SCHEMA or store.paths.resolve(meta.get("library_root", "")) != store.root:
            raise ValueError("Deep API checkpoint belongs to another catalog or schema")
        return db, meta
    except BaseException:
        db.close()
        raise


def create_batch(store, *, library_ids=None, limit=None, checkpoint=None):
    """Snapshot current model-case identities, without reading XML children."""
    if library_ids is not None and (not isinstance(library_ids, list) or not library_ids
                                    or any(not isinstance(lid, str) or not lid for lid in library_ids)):
        raise ValueError("library_ids must be a nonempty list of library IDs")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer")
    selected = None if library_ids is None else set(library_ids)
    libraries = {item["id"]: item for item in store.libraries(include_deleted=True)}
    if selected is not None and selected - libraries.keys():
        raise FileNotFoundError("Unknown selected library")
    if selected is not None and any(libraries[lid]["deleted_at"] is not None for lid in selected):
        raise FileNotFoundError("Deleted libraries cannot receive API enrichment")
    allowed = {lid for lid, item in libraries.items() if item["deleted_at"] is None
               and item["type"] in {"official-models", "user-models"} and (selected is None or lid in selected)}
    batch_id = uuid.uuid4().hex
    path = _batch_path(store, checkpoint or store.root / "deep-api-batches" / (batch_id + ".sqlite3"))
    if path.exists():
        raise FileExistsError("Deep API checkpoint already exists; resume it explicitly")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(path)) as target, target, closing(store.connect()) as source:
            target.executescript("""CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE cases(object_id TEXT PRIMARY KEY,authority_id TEXT NOT NULL,library_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
                published_id TEXT,report TEXT,prepared_result BLOB,updated_at TEXT);
              CREATE INDEX case_state ON cases(state,object_id);""")
            target.executemany("INSERT INTO meta VALUES(?,?)", [("schema", SCHEMA), ("batch_id", batch_id),
                ("library_root", store.paths.encode(store.root)), ("created_at", now())])
            # case_members locates authorities before revisions.payload is
            # decoded. Active's composite primary key then excludes history.
            query = """SELECT r.object_id,r.id,r.library_id FROM case_members m
              JOIN revisions r ON r.id=m.citation_id
              JOIN active a ON a.library_id=r.library_id AND a.object_id=r.object_id AND a.citation_id=r.id
              WHERE m.local_id='model-case' ORDER BY r.library_id,r.object_id"""
            count = 0
            for object_id, identity, library in source.execute(query):
                check_cancelled()
                if library not in allowed:
                    continue
                target.execute("INSERT OR IGNORE INTO cases(object_id,authority_id,library_id) VALUES(?,?,?)",
                               (object_id, identity, library))
                count += 1
                if limit is not None and count >= limit:
                    break
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return batch_status(store, path)


def batch_status(store, checkpoint):
    db, meta = _checkpoint(store, checkpoint)
    with closing(db):
        counts = {state: 0 for state in ("pending", "running", "staged", "completed", "partial", "failed", "skipped")}
        counts.update(dict(db.execute("SELECT state,COUNT(*) FROM cases GROUP BY state")))
        api_counts = {state: 0 for state in ("completed", "partial", "failed", "skipped", "not_extracted")}
        for state, api_status, count in db.execute("SELECT state,json_extract(report,'$.api_status'),COUNT(*) FROM cases GROUP BY 1,2"):
            category = "not_extracted" if state in {"pending", "running"} else _case_state(api_status)
            api_counts[category] += count
        preview = [dict(row) for row in db.execute(
            "SELECT object_id,authority_id,library_id,state,attempts,published_id,report FROM cases ORDER BY updated_at DESC,object_id LIMIT 8")]
        for item in preview:
            item["report"] = json.loads(item["report"]) if item["report"] else None
        return {"schema": SCHEMA, "batch_id": meta["batch_id"], "checkpoint": str(Path(checkpoint).absolute()),
                "case_counts": counts, "api_counts": api_counts, "total": sum(counts.values()), "completed": sum(counts[state] for state in _DONE),
                "extracted": sum(counts[state] for state in _DONE | {"staged"}),
                "last_job_id": meta.get("last_job_id"), "indexed_generation": meta.get("indexed_generation"),
                "cases_preview": preview, "checkpoint_bytes": Path(checkpoint).stat().st_size,
                "embedding_invoked": False, "library_states_changed": False}


def _active_authority(db, object_id, library_id):
    row = db.execute("SELECT r.payload FROM active a JOIN revisions r ON r.id=a.citation_id "
                     "WHERE a.library_id=? AND a.object_id=?", (library_id, object_id)).fetchone()
    return json.loads(row[0]) if row else None


def _published_for(authority, expected_id, batch_id):
    meta = (authority or {}).get("data", {}).get("deep_enrichment", {})
    return (meta.get("schema") == OVERLAY_SCHEMA and meta.get("batch_id") == batch_id
            and meta.get("source_authority_id") == expected_id)


def _source_record(store, authority):
    library = store.library(authority["library_id"], include_deleted=True)
    if library["deleted_at"] is not None or library["type"] not in {"official-models", "user-models"}:
        raise ValueError("API enrichment requires a retained model library")
    asset = authority.get("asset_key", "")
    root = next((item for item in sorted(library["roots"], key=lambda item: len(item["id"]), reverse=True)
                 if asset.startswith(item["id"] + ":")), None)
    if root is None:
        raise ValueError("Model source root is no longer registered")
    relative = asset[len(root["id"]) + 1:]
    path = (Path(root["path"]) / relative).resolve()
    if not path.is_relative_to(Path(root["path"]).resolve()) or not path.is_file():
        raise ValueError("Model source is unavailable or escapes its registered root")
    if path.suffix.lower() != ".mph":
        raise ValueError("API enrichment only reads MPH model sources")
    return library, {"root_id": root["id"], "relative_path": relative, "path": str(path),
                     "content_hash": authority["asset_hash"]}


def publish_case_overlay(store, authority, result, *, batch_id, job_id):
    """CAS-publish an API-only group; old XML groups and revisions stay intact."""
    if authority.get("type") != "model_case" or authority.get("local_id") != "model-case":
        raise ValueError("Expected one existing model-case authority")
    with closing(store.connect()) as db:
        current = _active_authority(db, authority["object_id"], authority["library_id"])
    if _published_for(current, authority["id"], batch_id):
        return {"authority_id": current["id"], "api_objects": current["data"]["deep_enrichment"]["api_objects"], "recovered": True}
    if not current or current["id"] != authority["id"]:
        raise ValueError("Model authority changed since this batch was planned")
    library, record = _source_record(store, authority)
    hashes = {}
    if store._evidence_hash(Path(record["path"]), hashes) != authority["asset_hash"]:
        raise ValueError("Original MPH changed; API overlay cannot attach to another saved snapshot")
    sources = copy.deepcopy(result.get("objects"))
    if not isinstance(sources, list) or not sources:
        raise ValueError("API enrichment returned no publication objects")
    primary = [item for item in sources if item.get("type") == "model_case" and item.get("local_id") == "model-case"]
    if len(primary) != 1 or any(item is not primary[0] and not str(item.get("local_id", "")).startswith("deep-api-") for item in sources):
        raise ValueError("API publication must contain one model-case and only deep-api-* children")
    data = primary[0].setdefault("data", {})
    if data.get("compact_case") != authority.get("data", {}).get("compact_case"):
        raise ValueError("API overlay must retain the exact saved compact-case reference")
    from .compact_library import _reader
    _reader(primary[0], store.root, Path(record["path"]))  # Verify the immutable staged overlay before publication.
    base_id = authority.get("data", {}).get("base_case_id", authority["id"])
    base_group = authority.get("data", {}).get("base_case_group", authority.get("case_group"))
    data.update(base_case_id=base_id, base_case_group=base_group)
    data["deep_enrichment"] = dict(data.get("deep_enrichment") or {}, schema=OVERLAY_SCHEMA,
        base_case_id=base_id, base_case_group=base_group, source_authority_id=authority["id"],
        batch_id=batch_id, job_id=job_id, api_status=result.get("api_status", "partial"), api_objects=len(sources) - 1)
    primary[0]["coverage"] = result.get("coverage", primary[0].get("coverage", {}))
    prepared = store._pin_case_group([store._prepare(library, record, item, evidence_hashes=hashes) for item in sources])
    published = next(item for item in prepared if item["local_id"] == "model-case")
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        check_cancelled()
        row = db.execute("SELECT body FROM libraries WHERE id=?", (library["id"],)).fetchone()
        if not row or store._library(json.loads(row[0]))["deleted_at"] is not None:
            raise FileNotFoundError("Library was deleted before API publication")
        current = _active_authority(db, authority["object_id"], library["id"])
        if not current or current["id"] != authority["id"]:
            raise ValueError("Model authority changed before API publication")
        if current.get("data", {}).get("deep_enrichment", {}).get("schema") == OVERLAY_SCHEMA:
            # Only prior API-group children are replaced. Saved XML children
            # still refer to their original immutable authority and group.
            db.execute("DELETE FROM active WHERE library_id=? AND citation_id IN "
                "(SELECT citation_id FROM case_members WHERE group_id=? AND local_id GLOB 'deep-api-*')",
                (library["id"], current.get("case_group")))
        for item in prepared:
            check_cancelled()
            db.execute("INSERT OR IGNORE INTO revisions VALUES(?,?,?,?)", (item["id"], item["object_id"], library["id"], encoded(item)))
            db.execute("INSERT OR REPLACE INTO case_members VALUES(?,?,?)", (item["case_group"], item["local_id"], item["id"]))
            db.execute("INSERT OR REPLACE INTO active VALUES(?,?,?)", (library["id"], item["object_id"], item["id"]))
        check_cancelled()
    return {"authority_id": published["id"], "api_objects": len(prepared) - 1, "recovered": False}


@contextmanager
def _lease(store, job_id):
    if not store._build_lock.acquire(blocking=False):
        raise RuntimeError("Another library constructor is active")
    path, token, owned = store.root / ".build.lock", uuid.uuid4().hex, False
    try:
        if path.is_file():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if store._owner_moved(previous) or not process_alive(previous.get("pid")):
                path.unlink()
        with path.open("x", encoding="utf-8") as stream:
            stream.write(encoded({"pid": os.getpid(), "job_id": job_id, "token": token, "workspace_root": str(store.paths.root)}))
        owned = True
        yield
    finally:
        try:
            if owned and path.exists() and json.loads(path.read_text(encoding="utf-8")).get("token") == token:
                path.unlink()
        finally:
            store._build_lock.release()


def _case_state(status):
    return "completed" if status in {"observed", "completed"} else "partial" if status == "partial" else "skipped" if status == "preview_only" else "failed"


def cleanup_retired_indexes(store, *, on_progress=None, timeout=240):
    """Honor the existing reader grace and retention ownership checks."""
    from .index_manifest_cache import read_manifest
    from .index_paths import resolve_index_manifest
    from .index_retention import prune_index_generations
    from .library_search import LibrarySearch

    search = LibrarySearch.for_maintenance(store.root / "search")
    started = time.monotonic()
    summary = {"state": "completed", "removed_generations": 0, "removed_bytes": 0, "warnings": []}
    while True:
        check_cancelled()
        active = read_manifest(search.data_dir / "active.json", summary=True)
        if not active or not active.get("directory"):
            return dict(summary, state="skipped", warnings=["No published index is available for tail cleanup"])
        active = resolve_index_manifest(active, search.data_dir)
        if not search.lock.acquire(blocking=False):
            return dict(summary, state="partial", warnings=["Search maintenance is busy; retired indexes retained"])
        handle = None
        try:
            handle, token = search._acquire_lease(active.get("generation") or "deep-api-tail-cleanup")
            result = prune_index_generations(search.data_dir, current_directory=active["directory"])
        except (OSError, RuntimeError, ValueError) as exc:
            return dict(summary, state="partial", warnings=[*summary["warnings"], "Retired index cleanup deferred: " + str(exc)[:300]])
        finally:
            try:
                if handle is not None:
                    search._release_lease(handle, token)
            finally:
                search.lock.release()
        summary["removed_generations"] += result.get("removed_generations", 0)
        summary["removed_bytes"] += result.get("removed_bytes", 0)
        summary["warnings"] = list(dict.fromkeys([*summary["warnings"], *result.get("warnings", [])]))
        summary["state"] = result.get("state", "partial")
        recent = result.get("retained_recent_generations", 0)
        if on_progress:
            on_progress(dict(summary, retained_recent_generations=recent))
        if not recent or result.get("state") == "skipped":
            return summary
        if time.monotonic() - started >= timeout:
            return dict(summary, state="partial", warnings=[*summary["warnings"], "Reader grace cleanup timeout; recent indexes retained"])
        # Cancellation is observed within one second; progress refreshes at
        # most every ten seconds during the existing reader grace period.
        for _ in range(10):
            check_cancelled()
            time.sleep(1)


def run_batch(store, checkpoint, *, modes=("native", "qmd_keyword"), publish=True, retry_failed=False,
              builder=None, api_runner=None, clean_indexes=True, on_progress=None):
    """Stage serial API reads, then publish cases and build indexes once."""
    if not isinstance(modes, (list, tuple)) or any(mode not in _MODES for mode in modes):
        raise ValueError("Deep API batches only support native/qmd_keyword index modes; embeddings are excluded")
    if any(type(value) is not bool for value in (retry_failed, publish, clean_indexes)):
        raise ValueError("retry_failed, publish and clean_indexes must be booleans")
    db, meta = _checkpoint(store, checkpoint)
    job_id = uuid.uuid4().hex
    job = {"id": job_id, "kind": "api_enrichment", "scope": "selected", "library_id": None,
           "library_ids": [row[0] for row in db.execute("SELECT DISTINCT library_id FROM cases ORDER BY library_id")],
           "status": "running", "stage": "deep_api", "pid": os.getpid(), "cancel_supported": True,
           "started_at": now(), "completed": 0, "items": [], "failed": [], "batch_id": meta["batch_id"],
           "checkpoint": str(Path(checkpoint).absolute()), "embedding_invoked": False, "library_states_changed": False}
    def progress(stage, current=None):
        status = batch_status(store, checkpoint)
        completed = status["extracted"] if stage == "deep_api" else status["completed"]
        job.update(stage=stage, completed=completed, total=status["total"], case_counts=status["case_counts"], api_counts=status["api_counts"],
                   progress={"completed": completed, "total": status["total"], "unit": "cases", "current_file": current,
                             "detail": "Serial read-only API enrichment; saved XML and library enabled states stay unchanged"})
        store._save_job(job)
        if on_progress:
            on_progress(dict(job))
    if builder is None:
        from .compact_library import enrich_case
        builder = enrich_case
    try:
        with _lease(store, job_id), build_control(BuildControl(store._cancel_path(job_id))):
            with db:
                db.execute("UPDATE cases SET state='pending' WHERE state='running'")
                if retry_failed:
                    db.execute("UPDATE cases SET state='pending' WHERE state='failed'")
                db.execute("INSERT OR REPLACE INTO meta VALUES('last_job_id',?)", (job_id,))
            progress("deep_api")
            while True:
                check_cancelled()
                row = db.execute("SELECT * FROM cases WHERE state='pending' ORDER BY object_id LIMIT 1").fetchone()
                if row is None:
                    break
                with db:
                    db.execute("UPDATE cases SET state='running',attempts=attempts+1,updated_at=? WHERE object_id=?", (now(), row["object_id"]))
                progress("deep_api", row["authority_id"])
                try:
                    with closing(store.connect()) as catalog:
                        current = _active_authority(catalog, row["object_id"], row["library_id"])
                    if _published_for(current, row["authority_id"], meta["batch_id"]):
                        report = {"authority_id": current["id"], "api_status": current["data"]["deep_enrichment"]["api_status"], "recovered": True}
                        state = _case_state(report["api_status"])
                        result = None
                    else:
                        authority = store.get(row["authority_id"])
                        if authority is None or not current or current["id"] != authority["id"]:
                            raise ValueError("Planned model authority is no longer active")
                        library, record = _source_record(store, authority)
                        artifact = store.root / "artifacts" / ("deep-api-" + authority["object_id"][3:])
                        result = builder(authority, library_root=store.root, artifact_dir=artifact,
                                         source_override=Path(record["path"]), **({"runner": api_runner} if api_runner else {}))
                        check_cancelled()
                        state = _case_state(result.get("api_status"))
                        report = {"api_status": result.get("api_status"), "cache_hit": bool(result.get("cache_hit")),
                                  "coverage": result.get("coverage", {}), "warnings": result.get("warnings", [])}
                        if state in {"completed", "partial"}:
                            state = "staged"
                    with db:
                        db.execute("UPDATE cases SET state=?,published_id=?,report=?,prepared_result=?,updated_at=? WHERE object_id=?",
                            (state, report.get("authority_id"), encoded(report),
                             zlib.compress(encoded(result).encode("utf-8"), 6) if state == "staged" else None, now(), row["object_id"]))
                        if state == "staged":
                            db.execute("INSERT OR REPLACE INTO meta VALUES('checkpoint_dirty','1')")
                except BuildCancelled:
                    raise
                except Exception as exc:
                    with db:
                        db.execute("UPDATE cases SET state='failed',report=?,updated_at=? WHERE object_id=?",
                            (encoded({"error": f"{type(exc).__name__}: {str(exc)[:1200]}"}), now(), row["object_id"]))
                progress("deep_api")
            # All long model reads are complete before the live catalog's
            # active generation changes. Staging-only pilots leave it intact.
            while publish:
                check_cancelled()
                row = db.execute("SELECT * FROM cases WHERE state='staged' ORDER BY object_id LIMIT 1").fetchone()
                if row is None:
                    break
                progress("publishing", row["authority_id"])
                try:
                    authority = store.get(row["authority_id"])
                    raw = row["prepared_result"]
                    result = json.loads(zlib.decompress(raw).decode("utf-8") if isinstance(raw, bytes) else raw)
                    report = json.loads(row["report"])
                    report.update(publish_case_overlay(store, authority, result, batch_id=meta["batch_id"], job_id=job_id))
                    with db:
                        db.execute("UPDATE cases SET state=?,published_id=?,report=?,prepared_result=NULL,updated_at=? WHERE object_id=?",
                            (_case_state(report.get("api_status")), report["authority_id"], encoded(report), now(), row["object_id"]))
                except BuildCancelled:
                    raise
                except Exception as exc:
                    with db:
                        db.execute("UPDATE cases SET state='failed',report=?,prepared_result=NULL,updated_at=? WHERE object_id=?",
                            (encoded({"error": f"{type(exc).__name__}: {str(exc)[:1200]}"}), now(), row["object_id"]))
                progress("publishing")
            published = db.execute("SELECT 1 FROM cases WHERE published_id IS NOT NULL LIMIT 1").fetchone()
            if publish and published:
                # Keep operator-owned holdouts aligned with newly published API
                # revisions. Refresh only the small policy, never its selection
                # or an extra copy of the search index.
                from .evaluation_sets import refresh_registry
                refresh_registry(store.root)
            generation = store.current_generation()
            indexed = dict(db.execute("SELECT key,value FROM meta")).get("indexed_generation")
            if publish and published and modes and indexed != generation:
                check_cancelled()
                progress("indexing")
                from .library_search import LibrarySearch
                with store.object_stream() as objects:
                    generation = objects.generation
                    def index_progress(update):
                        check_cancelled()
                        job.update(stage=update.get("stage", "indexing"), progress=update.get("progress", {}))
                        store._save_job(job)
                        if on_progress:
                            on_progress(dict(job))
                    job["indexes"] = LibrarySearch(store.root / "search").build(objects, modes=list(modes), generation=generation,
                        on_progress=index_progress)
                if all(job["indexes"].get("modes", {}).get(mode, {}).get("ready") for mode in modes):
                    with db:
                        db.execute("INSERT OR REPLACE INTO meta VALUES('indexed_generation',?)", (generation,))
                else:
                    job["failed_indexes"] = [mode for mode in modes if not job["indexes"].get("modes", {}).get(mode, {}).get("ready")]
            if publish and published and modes and clean_indexes and not job.get("failed_indexes"):
                progress("cleaning_indexes")
                job["index_cleanup"] = cleanup_retired_indexes(store,
                    on_progress=lambda report: (job.update(index_cleanup=report), progress("cleaning_indexes")))
            if (publish and dict(db.execute("SELECT key,value FROM meta")).get("checkpoint_dirty") == "1"
                    and not db.execute("SELECT 1 FROM cases WHERE state='staged' LIMIT 1").fetchone()):
                # Thin staging records are compressed while pending and are
                # cleared after commit. Reclaim their freed checkpoint pages
                # once, rather than retaining a second permanent payload copy.
                check_cancelled()
                progress("compacting_checkpoint")
                db.execute("VACUUM")
                with db:
                    db.execute("INSERT OR REPLACE INTO meta VALUES('checkpoint_dirty','0')")
            check_cancelled()
            counts = batch_status(store, checkpoint)["case_counts"]
            job["status"] = "partial" if counts["partial"] or counts["failed"] or job.get("failed_indexes") else "completed"
            job["publication_deferred"] = not publish
    except BuildCancelled:
        job.update(status="cancelled", cancelled_stage=job["stage"])
    except Exception as exc:
        job.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:1200]}")
    finally:
        if api_runner is not None:
            stats = getattr(api_runner, "stats", None)
            if isinstance(stats, dict):
                job["api_runner_stats"] = copy.deepcopy(stats)
            close = getattr(api_runner, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    job.setdefault("warnings", []).append("API runner temporary cleanup failed: " + str(exc)[:300])
        db.close()
        job["finished_at"] = now()
        progress("finished")
    return dict(batch_status(store, checkpoint), job_id=job_id, status=job["status"], publication_deferred=not publish,
                **({"index_cleanup": job["index_cleanup"]} if job.get("index_cleanup") else {}),
                **({"api_runner_stats": job["api_runner_stats"]} if job.get("api_runner_stats") else {}),
                **({"error": job["error"]} if job.get("error") else {}))
