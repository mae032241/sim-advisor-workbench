"""Plan or publish offline canonical tutorial normalization.

Stop every application/CLI process using the data directory before publication.
--plan-only is strictly read-only with respect to the supplied data directory;
it creates only the explicitly named plan (and optional progress log).
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import uuid

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from advisor.build_control import check_cancelled
from advisor.catalog_storage import register_catalog_functions
from advisor.index_manifest import _owned, _write_json, publish_manifest
from advisor.library_search import LibrarySearch
from advisor.library_store import LibraryStore
from advisor.tutorial_catalog import refresh_links
from advisor.tutorial_index_migration import stage_tutorial_indexes
from advisor.tutorial_migration import (
    TutorialMigrationPlan, _generation, apply_plan_to_catalog,
    build_tutorial_migration_plan, validate_tutorial_plan_sources,
)
from advisor.workspace_paths import SCHEMA as PATH_SCHEMA, WorkspacePaths


def _library_root(path):
    path = Path(path).expanduser().resolve(strict=True)
    if (path / "catalog.sqlite3").is_file():
        return path
    if (path / "libraries" / "catalog.sqlite3").is_file():
        return (path / "libraries").resolve()
    raise FileNotFoundError("--data-dir must contain catalog.sqlite3 or libraries/catalog.sqlite3")


class ReadOnlyLibraryStore(LibraryStore):
    """Use Store readers without constructor migrations, WAL changes or anchors."""
    def __init__(self, data_dir):
        self.root = _library_root(data_dir)
        self.db_path = self.root / "catalog.sqlite3"
        if self.db_path.is_symlink():
            raise ValueError("Catalog must be a regular owned database")
        self._build_lock = threading.Lock()
        workspace = self.root.parent if self.root.name == "libraries" else self.root
        self.paths = object.__new__(WorkspacePaths)
        self.paths.root = self.paths.current_root = workspace
        self.paths.anchor_path = workspace / "workspace-location.json"
        self.paths.previous_roots = ()
        if self.paths.anchor_path.is_file():
            anchor = json.loads(self.paths.anchor_path.read_text(encoding="utf-8"))
            roots = [anchor.get("current_root"), *anchor.get("previous_roots", [])]
            if (anchor.get("schema") != PATH_SCHEMA or any(
                    not isinstance(item, str) or not Path(item).is_absolute() or Path(item).resolve().parent == Path(item).resolve()
                    for item in roots)):
                raise ValueError("Invalid existing workspace location anchor")
            self.paths.previous_roots = tuple(dict.fromkeys(Path(item).resolve() for item in roots if Path(item).resolve() != workspace))

    def connect(self):
        db = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=30)
        register_catalog_functions(db)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db


def _event(callback, phase, **details):
    if callback:
        callback({"phase": phase, **details})


def _write_lease(path, body):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(body, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def _release_file(path, token):
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("token") == token:
            path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def _offline_build_lease(store):
    token = uuid.uuid4().hex
    lease = store.root / ".build.lock"
    maintenance = store.root / ".catalog-migration.lock"
    recovery = store.root / ".tutorial-migration-recovery.json"
    if maintenance.exists() or recovery.exists():
        raise RuntimeError("Offline maintenance/recovery marker exists; inspect its owner and recovery journal before retrying")
    try:
        _write_lease(lease, {"pid": os.getpid(), "token": token, "kind": "tutorial-normalization",
                             "workspace_root": str(store.paths.root)})
    except FileExistsError:
        raise RuntimeError("A catalog build lease exists; stop its owner before tutorial normalization") from None
    search = LibrarySearch.for_maintenance(store.root / "search")
    acquired = search.lock.acquire(blocking=False)
    handle = search_token = None
    try:
        if not acquired:
            raise RuntimeError("Another task owns the search directory")
        handle, search_token = search._acquire_lease("tutorial-normalization")
        yield token
    finally:
        if handle is not None:
            search._release_lease(handle, search_token)
        if acquired:
            search.lock.release()
        _release_file(lease, token)


def _atomic_bytes(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_pointers(root, snapshots):
    for name, value in snapshots.items():
        path = _owned(root, root / name)
        if value is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_bytes(path, value)


def _remove_stage(root, directory):
    directory = _owned(root, Path(directory))
    generations = (root / "generations").resolve()
    if directory.resolve().parent != generations or directory.resolve() == generations:
        raise ValueError("Refusing to remove an index directory outside the owned generation boundary")
    if directory.exists():
        shutil.rmtree(directory)


def _cleanup_corpus(root, staged):
    directory = _owned(root, Path(staged["directory"]))
    warnings = []
    for value in staged.get("corpus_directories", []):
        try:
            corpus = _owned(root, Path(value))
            if corpus.name != "corpus" or not corpus.resolve().is_relative_to(directory.resolve()) or corpus.resolve() == directory.resolve():
                raise ValueError("Manual corpus cleanup escaped the staged generation")
            if corpus.exists():
                shutil.rmtree(corpus)
        except (OSError, ValueError) as exc:
            warnings.append("Manual corpus retained: " + str(exc))
    return warnings


def _publish(store, plan, staged, token, callback):
    root = store.root / "search"
    directory = _owned(root, Path(staged["directory"]))
    snapshots = {name: (_owned(root, root / name).read_bytes() if (root / name).is_file() else None)
                 for name in ("active.json", "last-build.json")}
    recovery = store.root / ".tutorial-migration-recovery.json"
    maintenance = store.root / ".catalog-migration.lock"
    body = {"schema": "tutorial-publication-recovery/v1", "pid": os.getpid(), "token": token,
            "plan": str(plan.path), "migration_id": plan.metadata()["migration_id"],
            "source_generation": plan.metadata()["source_generation"],
            "target_generation": staged["manifest"]["generation"], "staged_directory": str(directory),
            "previous_pointers": {name: base64.b64encode(value).decode("ascii") if value is not None else None
                                  for name, value in snapshots.items()}, "phase": "publication_pending"}
    _write_lease(maintenance, {"pid": os.getpid(), "token": token, "kind": "tutorial-normalization", "plan": str(plan.path)})
    committed = False
    attempted_publish = False
    safe_release = True
    db = None
    try:
        _write_json(recovery, body, cancellable=False)
        db = register_catalog_functions(sqlite3.connect(store.db_path.as_uri() + "?mode=rw", uri=True, timeout=30))
        db.execute("BEGIN IMMEDIATE")
        summary = apply_plan_to_catalog(db, plan, verify_sources=True)
        refresh_links(db)
        if _generation(db) != staged["manifest"]["generation"]:
            raise ValueError("Staged index generation differs from the catalog transaction")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Catalog integrity check failed before tutorial publication")
        check_cancelled()
        _event(callback, "publishing_catalog_and_indexes")
        attempted_publish = True
        # Direct publication retains every old generation. LibrarySearch._publish
        # applies ordinary retention and must not be used for this migration.
        publish_manifest(directory, staged["manifest"], root, cancellable=True)
        db.commit()
        committed = True
        try:
            _event(callback, "catalog_and_indexes_committed", generation=staged["manifest"]["generation"])
        except Exception as exc:
            summary.setdefault("coordinator_warnings", []).append("Post-commit progress logging failed: " + str(exc))
        return summary
    except BaseException:
        if not committed:
            if db is not None:
                db.rollback()
            try:
                if attempted_publish:
                    _restore_pointers(root, snapshots)
            except BaseException as restore_error:
                safe_release = False
                body["phase"] = "pointer_restore_failed"
                body["restore_error"] = str(restore_error)
                _write_json(recovery, body, cancellable=False)
                raise RuntimeError("Catalog rolled back but pointer restoration failed; maintenance lock and recovery journal retained") from restore_error
        raise
    finally:
        if db is not None:
            db.close()
        if safe_release:
            try:
                recovery.unlink(missing_ok=True)
                _release_file(maintenance, token)
            except OSError as exc:
                if committed:
                    summary.setdefault("coordinator_warnings", []).append("Committed migration marker cleanup failed: " + str(exc))
                else:
                    raise


def run_normalization(data_dir, plan_path, *, plan_only=False, apply_existing_plan=False,
                      canonical_library_id="cs63-documents", on_progress=None):
    """Coordinate one offline migration; old generations/revisions are retained."""
    if plan_only and apply_existing_plan:
        raise ValueError("--plan-only and --apply-existing-plan are mutually exclusive")
    store = ReadOnlyLibraryStore(data_dir)
    if (store.root / ".catalog-migration.lock").exists():
        raise RuntimeError("Catalog is already undergoing offline maintenance")
    if plan_only:
        plan = build_tutorial_migration_plan(store, plan_path, canonical_library_id=canonical_library_id, on_progress=on_progress)
        return {**plan.summary(), "status": "planned", "plan": str(plan.path)}
    staged = None
    committed = False
    with _offline_build_lease(store) as token:
        plan = TutorialMigrationPlan(plan_path) if apply_existing_plan else build_tutorial_migration_plan(
            store, plan_path, canonical_library_id=canonical_library_id, on_progress=on_progress)
        validate_tutorial_plan_sources(plan)
        _event(on_progress, "staging_tutorial_indexes")
        try:
            staged = stage_tutorial_indexes(store, plan, on_progress=on_progress)
            summary = _publish(store, plan, staged, token, on_progress)
            committed = True
        finally:
            # Never remove a generation while an unresolved publication could
            # still reference it. Its recovery journal is the operator's proof.
            if staged and not committed and not (store.root / ".tutorial-migration-recovery.json").exists():
                with closing(store.connect()) as current:
                    published = _generation(current) == staged["manifest"]["generation"]
                if not published:
                    _remove_stage(store.root / "search", staged["directory"])
        warnings = _cleanup_corpus(store.root / "search", staged)
        warnings.extend(summary.get("coordinator_warnings", []))
        return {"status": "applied", "plan": str(plan.path), "generation": staged["manifest"]["generation"],
                "counts": summary["counts"], "indexes": staged["counts"], "warnings": warnings,
                "archive_policy": "immutable_revisions_and_previous_index_generations_retained",
                "previous_manifest": staged["previous_manifest"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="Library catalog directory or its parent data directory")
    parser.add_argument("--plan", type=Path, required=True, help="Explicit SQLite plan path; existing plans are never overwritten")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan-only", action="store_true", help="Read data without modifying catalog/indexes or acquiring write leases")
    modes.add_argument("--apply-existing-plan", action="store_true", help="Publish an already reviewed plan with all app processes stopped")
    parser.add_argument("--canonical-library-id", default="cs63-documents")
    parser.add_argument("--progress-log", type=Path, help="Optional additional JSON-lines log")
    args = parser.parse_args(argv)
    if args.progress_log:
        args.progress_log.parent.mkdir(parents=True, exist_ok=True)
    log = args.progress_log.open("a", encoding="utf-8") if args.progress_log else None
    def emit(value):
        line = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **value}, ensure_ascii=False)
        print(line, flush=True)
        if log:
            log.write(line + "\n")
            log.flush()
    try:
        result = run_normalization(args.data_dir, args.plan, plan_only=args.plan_only,
                                   apply_existing_plan=args.apply_existing_plan,
                                   canonical_library_id=args.canonical_library_id,
                                   on_progress=lambda event: emit({"event": "progress", **event}))
        emit({"event": "result", **result})
        return 0
    except Exception as exc:
        emit({"event": "error", "error": str(exc), "error_type": type(exc).__name__})
        return 1
    finally:
        if log:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
