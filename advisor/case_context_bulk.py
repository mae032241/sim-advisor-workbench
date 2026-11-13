"""Resumable offline derivation of context for every active case overview.

Only the existing canonical-text constructor supplies profile text. The job
database retains identities, outcomes and small provenance counts; complete
profiles exist there only while waiting for an incremental publication batch.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .case_context import build_case_context, classify_case_metadata
from .case_navigation import _OVERVIEWS_SQL
from .evaluation_sets import EvaluationPolicyStale, _load, _watermark
from .index_manifest import _owned


SCHEMA = "case-context-bulk/v1"
MAX_BATCH_SIZE = 64
_TERMINAL = {"completed", "partial"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _closed_policy(store, watermark):
    """Keep administrator holdouts closed even when their web read gate is open."""
    registry, policy, _ = _load(store.root)
    if registry is not None and registry.get("catalog_revision_watermark") != watermark:
        raise EvaluationPolicyStale("The evaluation holdout registry does not match the catalog watermark")
    return policy


def _make_store(data_dir):
    from scripts.normalize_tutorials import ReadOnlyLibraryStore

    class ProtectedStore(ReadOnlyLibraryStore):
        def evaluation_policy(self):
            return self.bulk_policy

        def require_not_heldout(self, citation_id, source=None):
            super().require_not_heldout(citation_id, source)
            if source is not None and _blocked(self.bulk_policy, source):
                raise PermissionError("The source belongs to an administrator holdout")

    return ProtectedStore(data_dir)


def _snapshot(store):
    from .case_context_index import _active, _keyword_path
    with closing(store.connect()) as db:
        db.execute("BEGIN")
        watermark = _watermark(db)
        rows = [tuple(row) for row in db.execute(_OVERVIEWS_SQL)]
    active = _active(store.root / "search")
    if not _keyword_path(active).is_file():
        raise FileNotFoundError("The existing QMD keyword index is missing")
    policy = _closed_policy(store, watermark)
    directory = Path(active["directory"]).resolve()
    identity = {"catalog_watermark": watermark, "generation": active.get("generation"),
                "corpus_hash": active.get("corpus_hash"),
                "directory": directory.relative_to(store.root / "search").as_posix(),
                "overview_count": len(rows), "overview_ids_sha256": hashlib.sha256(_json(rows).encode()).hexdigest(),
                "holdout_policy_token": policy.token}
    return identity, rows, active, policy


def _verify_snapshot(store, expected):
    current, _, _, _ = _snapshot(store)
    if current != expected:
        raise RuntimeError("The active corpus, QMD generation or holdout registry changed; this checkpoint cannot resume")


@contextmanager
def _runner_lock(path):
    """An OS-held lock survives metadata rewrites and is released after a crash."""
    with _native_path(path).open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another bulk case-context runner owns this data directory") from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another bulk case-context runner owns this data directory") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _set_meta(db, **values):
    db.executemany("INSERT OR REPLACE INTO meta VALUES(?,?)", ((key, _json(value)) for key, value in values.items()))


def _native_path(path, *, suffix_budget=0):
    native = str(Path(path).absolute())
    if os.name == "nt" and len(native) + suffix_budget >= 240 and not native.startswith("\\\\?\\"):
        # SQLite's VFS and atomic file creation need this prefix in deep
        # workspace paths, including the desktop application's temp folder.
        native = "\\\\?\\UNC\\" + native[2:] if native.startswith("\\\\") else "\\\\?\\" + native
    return Path(native)


def _checkpoint_connection(path):
    return sqlite3.connect(_native_path(path), timeout=30)


def _open_job(path, snapshot, rows, apply, resume):
    native = _native_path(path)
    existed = native.exists()
    if resume and not existed:
        raise FileNotFoundError("No bulk checkpoint matches this exact corpus, holdout policy and execution mode")
    if existed and (not native.is_file() or native.stat().st_nlink != 1
                    or native.is_symlink() or getattr(native, "is_junction", lambda: False)()):
        raise ValueError("Bulk checkpoints must be unshared regular files")
    db = _checkpoint_connection(path)
    db.row_factory = sqlite3.Row
    # Erase excerpt bytes as soon as a committed publication clears its stage.
    db.execute("PRAGMA secure_delete=ON")
    db.execute("PRAGMA journal_mode=DELETE")
    if not existed:
        with db:
            db.executescript("""
              CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE cases(sequence INTEGER PRIMARY KEY,case_id TEXT UNIQUE NOT NULL,
                library_id TEXT NOT NULL,case_group TEXT NOT NULL,state TEXT NOT NULL,
                reason TEXT,detail TEXT,capability_json TEXT,profile_json TEXT,content_hash TEXT,
                context_characters INTEGER NOT NULL DEFAULT 0,source_references INTEGER NOT NULL DEFAULT 0);
              CREATE INDEX cases_state ON cases(state,sequence);
              CREATE TABLE sources(source_id TEXT PRIMARY KEY,library_id TEXT NOT NULL,document_sha256 TEXT);
            """)
            _set_meta(db, schema=SCHEMA, snapshot=snapshot, apply=apply, created_at=_now(),
                      state="running", stage="classification", published_batches=0, navigation_refreshed=False)
            db.executemany("INSERT INTO cases(sequence,case_id,library_id,case_group,state) VALUES(?,?,?,?,?)",
                           ((index, *row, "unclassified") for index, row in enumerate(rows)))
    elif (_meta(db, "schema") != SCHEMA or _meta(db, "snapshot") != snapshot or _meta(db, "apply") != apply):
        db.close()
        raise ValueError("Bulk checkpoint identity or schema does not match this run")
    return db


def _counts(db):
    states = Counter({row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM cases GROUP BY state")})
    total = sum(states.values())
    return {"active_overviews": total, "eligible": total - states["unclassified"] - states["heldout"],
            "heldout": states["heldout"], "classified": total - states["unclassified"],
            "pending": states["unclassified"] + states["pending"], "staged": states["staged"],
            "published": states["published"], "committed": states["published"],
            "noop": _meta(db, "unchanged_documents", 0), "derived": states["derived"],
            "degraded": states["degraded"], "failed": states["failed"],
            "processed": total - states["unclassified"] - states["pending"],
            "ready": states["published"] + states["derived"] + states["staged"]}


def _progress(db, path, job_id, *, state="running", stage, current_case=None, error=None):
    from scripts.normalize_tutorials import _atomic_bytes
    with db:
        _set_meta(db, state=state, stage=stage, updated_at=_now(), error=error)
    value = {"schema": SCHEMA, "job_id": job_id, "pid": os.getpid(), "state": state,
             "stage": stage, "current_case": current_case, "updated_at": _now(),
             "dry_run": not _meta(db, "apply"), "counts": _counts(db),
             "published_batches": _meta(db, "published_batches", 0),
             "checkpoint": str(path.parent / "jobs" / (job_id + ".sqlite3")),
             "stop_file": _meta(db, "stop_file"), "resume_supported": True,
             "source_parsing": False, "embedding_invoked": False, "full_qmd_rebuild": False,
             "query_input_used": False, "handwritten_case_text": False}
    if error:
        value["error"] = str(error)[:500]
    _atomic_bytes(_native_path(path, suffix_budget=40), (_json(value) + "\n").encode("utf-8"))


def _outcome(db, case_id, state, reason=None, detail=None, **values):
    assignments = {"state": state, "reason": reason, "detail": detail, **values}
    with db:
        db.execute("UPDATE cases SET " + ",".join(key + "=?" for key in assignments) + " WHERE case_id=?",
                   [*assignments.values(), case_id])


def _blocked(policy, raw):
    from .object_index import qmd_path
    return policy.blocks_object(raw) or qmd_path(raw) in policy.qmd_paths


def _classify(store, db, objects, case_id):
    raw = store.get(case_id)
    if raw is None or raw.get("id") != case_id or raw.get("type") != "model_case":
        raise ValueError("The exact active model_case overview is unavailable")
    if _blocked(store.bulk_policy, raw):
        _outcome(db, case_id, "heldout", "administrator_holdout")
        return
    capability = _json(classify_case_metadata(raw))
    if objects.execute("SELECT 1 FROM objects WHERE id=?", (case_id,)).fetchone() is None:
        _outcome(db, case_id, "failed", "case_not_in_qmd_generation", capability_json=capability)
        return
    _outcome(db, case_id, "pending", capability_json=capability)


def _derive(store, db, case_id, apply):
    profile = build_case_context(store, case_id)
    if profile.get("state") != "ready" or not profile.get("text"):
        codes = sorted({str(gap.get("code", "unknown_gap"))[:100] for gap in profile.get("gaps", [])
                        if isinstance(gap, dict)})[:16]
        _outcome(db, case_id, "degraded", "canonical_context_unavailable", _json(codes))
        return
    if profile.get("case_id") != case_id:
        raise ValueError("The context constructor returned a different case identity")
    refs = profile.get("source_refs", [])
    with db:
        db.executemany("INSERT OR IGNORE INTO sources VALUES(?,?,?)",
                       ((ref.get("id", ref.get("source_id")), ref["library_id"], ref.get("source_sha256")) for ref in refs))
        db.execute("""UPDATE cases SET state=?,reason=NULL,detail=NULL,profile_json=?,content_hash=?,
          context_characters=?,source_references=? WHERE case_id=?""",
                   ("staged" if apply else "derived", _json(profile) if apply else None,
                    profile.get("content_hash"), len(profile["text"]), len(refs), case_id))


def _publish_batch(data_dir, profiles, capabilities):
    from .case_context_index import publish_context_batch
    return publish_context_batch(data_dir, profiles, capabilities=capabilities, refresh_navigation=False)


def _refresh_navigation(data_dir):
    from .case_context_index import refresh_bulk_navigation
    return refresh_bulk_navigation(data_dir)


def _flush(store, data_dir, db, snapshot, batch_size, capabilities, progress_path, job_id):
    rows = db.execute("SELECT case_id,profile_json FROM cases WHERE state='staged' ORDER BY sequence LIMIT ?", (batch_size,)).fetchall()
    if not rows:
        return
    _verify_snapshot(store, snapshot)
    profiles = {row[0]: json.loads(row[1]) for row in rows}
    _progress(db, progress_path, job_id, stage="publishing", current_case=rows[0][0])
    # A crash after publication but before this transaction replays the same
    # exact profiles. The incremental publisher is deliberately idempotent.
    published = _publish_batch(data_dir, profiles, capabilities)
    with db:
        db.executemany("UPDATE cases SET state='published',profile_json=NULL WHERE case_id=?", ((identity,) for identity in profiles))
        _set_meta(db, published_batches=_meta(db, "published_batches", 0) + 1,
                  changed_documents=_meta(db, "changed_documents", 0) + published.get("changed_documents", 0),
                  unchanged_documents=_meta(db, "unchanged_documents", 0) + published.get("unchanged_documents", 0))
    _progress(db, progress_path, job_id, stage="deriving")


def _summary(db, checkpoint, progress_path, job_id):
    counts = _counts(db)
    reasons = {row[0]: row[1] for row in db.execute("SELECT reason,count(*) FROM cases WHERE reason IS NOT NULL GROUP BY reason")}
    classifications = Counter(json.loads(row[0]).get("classification", "unknown")
                              for row in db.execute("SELECT capability_json FROM cases WHERE capability_json IS NOT NULL"))
    errors = [{"case_id": row[0], "reason": row[1], "detail": row[2]}
              for row in db.execute("SELECT case_id,reason,detail FROM cases WHERE state='failed' ORDER BY sequence LIMIT 12")]
    sources = {row[0]: row[1] for row in db.execute("SELECT library_id,count(*) FROM sources GROUP BY library_id")}
    chars, refs = db.execute("SELECT coalesce(sum(context_characters),0),coalesce(sum(source_references),0) FROM cases").fetchone()
    sidecar = _native_path(progress_path.parent.parent / "case-contexts.sqlite3")
    staged_chars = db.execute("SELECT coalesce(sum(length(profile_json)),0) FROM cases WHERE profile_json IS NOT NULL").fetchone()[0]
    result = {"schema": SCHEMA, "job_id": job_id, "state": _meta(db, "state"), "stage": _meta(db, "stage"),
              "dry_run": not _meta(db, "apply"), "counts": counts, "reason_counts": reasons,
              "capability_counts": dict(classifications), "source_counts": {"unique_sections": sum(sources.values()),
              "unique_documents": db.execute("SELECT count(DISTINCT document_sha256) FROM sources").fetchone()[0],
              "sections_by_library": sources, "context_references": refs, "context_characters": chars},
              "published_batches": _meta(db, "published_batches", 0),
              "changed_documents": _meta(db, "changed_documents", 0),
              "unchanged_documents": _meta(db, "unchanged_documents", 0),
              "navigation_refreshed": _meta(db, "navigation_refreshed", False),
              "navigation": _meta(db, "navigation_result"),
              "storage": {"checkpoint_bytes": _native_path(checkpoint).stat().st_size,
                          "checkpoint_compacted": _meta(db, "checkpoint_compacted", False),
                          "case_context_sidecar_bytes": sidecar.stat().st_size if sidecar.is_file() else 0,
                          "pending_profile_characters": staged_chars, "maximum_batch_profiles": MAX_BATCH_SIZE,
                          "published_profile_text_retained_in_checkpoint": False},
              "checkpoint": str(checkpoint), "progress": str(progress_path), "failure_sample": errors,
              "stop_file": _meta(db, "stop_file"), "resume_supported": True,
              "source_parsing": False, "embedding_invoked": False, "full_qmd_rebuild": False,
              "handwritten_case_text": False, "query_input_used": False}
    if _meta(db, "error"):
        result["error"] = _meta(db, "error")
    return result


def run_bulk_case_contexts(data_dir, *, apply=False, batch_size=MAX_BATCH_SIZE, resume=False,
                           stop_file=None, report=None):
    """Automatically resume the exact corpus job; never reparse models or embed.

    Dry runs write only bounded job metadata and per-case outcomes in the
    runtime job directory. They discard profile text immediately. ``resume``
    additionally requires an existing matching checkpoint instead of starting
    a new job. A stop file is read between cases and publication batches.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError("batch_size must be between 1 and 64")
    store = _make_store(data_dir)
    search = store.root / "search"
    runtime = _owned(search, search / "runtime" / "case-context-bulk")
    runtime.mkdir(parents=True, exist_ok=True)
    jobs = _owned(search, runtime / "jobs")
    jobs.mkdir(exist_ok=True)
    lock = _owned(search, runtime / "runner.lock")
    progress_path = _owned(search, runtime / "progress.json")
    stop = _native_path(Path(stop_file).expanduser().resolve() if stop_file is not None else runtime / "stop")
    with _runner_lock(lock):
        snapshot, rows, active, store.bulk_policy = _snapshot(store)
        fingerprint = hashlib.sha256(_json(snapshot).encode()).hexdigest()[:24]
        job_id = ("apply-" if apply else "dry-run-") + fingerprint
        checkpoint = _owned(search, jobs / (job_id + ".sqlite3"))
        with closing(_open_job(checkpoint, snapshot, rows, apply, resume)) as db:
            with db:
                _set_meta(db, stop_file=str(stop))
            if _meta(db, "state") in _TERMINAL:
                _progress(db, progress_path, job_id, state=_meta(db, "state"), stage="finished")
                result = _summary(db, checkpoint, progress_path, job_id)
            else:
                try:
                    _progress(db, progress_path, job_id, stage="classification")
                    with closing(sqlite3.connect((Path(active["directory"]) / "objects.sqlite3").as_uri() + "?mode=ro", uri=True)) as objects:
                        for row in db.execute("SELECT case_id FROM cases WHERE state='unclassified' ORDER BY sequence").fetchall():
                            if stop.exists():
                                break
                            identity = row[0]
                            try:
                                _classify(store, db, objects, identity)
                            except Exception as exc:
                                _outcome(db, identity, "failed", "overview_read_failed", f"{type(exc).__name__}: {str(exc)[:400]}")
                            _progress(db, progress_path, job_id, stage="classification", current_case=identity)
                    _verify_snapshot(store, snapshot)
                    capabilities = {row[0]: json.loads(row[1]) for row in db.execute(
                        "SELECT case_id,capability_json FROM cases WHERE capability_json IS NOT NULL")}
                    if apply and not stop.exists():
                        # Interrupted jobs publish their already-derived batch
                        # before constructing another profile.
                        while _counts(db)["staged"] >= batch_size:
                            _flush(store, data_dir, db, snapshot, batch_size, capabilities, progress_path, job_id)
                            if stop.exists():
                                break
                    for row in db.execute("SELECT case_id FROM cases WHERE state='pending' ORDER BY sequence").fetchall():
                        if stop.exists():
                            break
                        identity = row[0]
                        try:
                            _derive(store, db, identity, apply)
                        except Exception as exc:
                            _outcome(db, identity, "failed", "context_derivation_failed", f"{type(exc).__name__}: {str(exc)[:400]}")
                        _progress(db, progress_path, job_id, stage="deriving", current_case=identity)
                        if apply and _counts(db)["staged"] >= batch_size and not stop.exists():
                            _flush(store, data_dir, db, snapshot, batch_size, capabilities, progress_path, job_id)
                    if stop.exists():
                        _progress(db, progress_path, job_id, state="cancelled", stage="stopped")
                    else:
                        _verify_snapshot(store, snapshot)
                        if apply:
                            while _counts(db)["staged"]:
                                _flush(store, data_dir, db, snapshot, batch_size, capabilities, progress_path, job_id)
                            if _counts(db)["published"] and not _meta(db, "navigation_refreshed") and not stop.exists():
                                _verify_snapshot(store, snapshot)
                                _progress(db, progress_path, job_id, stage="navigation")
                                navigation_result = _refresh_navigation(data_dir)
                                with db:
                                    _set_meta(db, navigation_refreshed=True, navigation_result=navigation_result)
                        counts = _counts(db)
                        if stop.exists():
                            _progress(db, progress_path, job_id, state="cancelled", stage="stopped")
                        else:
                            navigation_result = _meta(db, "navigation_result") or {}
                            guide_failures = (navigation_result.get('guides') or {}).get('failed_count', 0)
                            _progress(db, progress_path, job_id,
                                      state="partial" if counts["degraded"] or counts["failed"] or guide_failures else "completed", stage="finished")
                except (KeyboardInterrupt, SystemExit):
                    _progress(db, progress_path, job_id, state="cancelled", stage="stopped")
                    raise
                except Exception as exc:
                    _progress(db, progress_path, job_id, state="failed", stage="stopped", error=f"{type(exc).__name__}: {str(exc)[:400]}")
                result = _summary(db, checkpoint, progress_path, job_id)
            if _meta(db, "state") in _TERMINAL and not _meta(db, "checkpoint_compacted", False):
                # This is the small job checkpoint, never the shared QMD DB.
                # Source excerpts have already been securely cleared.
                try:
                    db.commit()
                    db.execute("VACUUM")
                    with db:
                        _set_meta(db, checkpoint_compacted=True)
                except sqlite3.Error as exc:
                    with db:
                        _set_meta(db, checkpoint_cleanup_warning=str(exc)[:200])
                result = _summary(db, checkpoint, progress_path, job_id)
    if report is not None:
        from scripts.normalize_tutorials import _atomic_bytes
        target = Path(report).expanduser().resolve()
        if target.is_relative_to(store.root) and not target.is_relative_to(runtime / "reports"):
            raise ValueError("The report must not overwrite corpus, index or bulk checkpoint files")
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_bytes(_native_path(target, suffix_budget=40), (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return result
