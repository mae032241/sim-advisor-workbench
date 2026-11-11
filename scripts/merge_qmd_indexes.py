"""Merge saved QMD keyword collections during explicit offline maintenance.

Native indexes, model/document sources and per-library vector databases stay
in place. A recovery journal separates publication from old-keyword cleanup.
Stop application readers before invoking this command.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from advisor.index_manifest import _owned, _write_json, publish_manifest
from advisor.index_manifest_cache import read_manifest
from advisor.index_paths import resolve_index_manifest
from advisor.library_search import LibrarySearch
from advisor.qmd_shared import KEYWORD_STORAGE, merge_keyword_indexes
from scripts.normalize_tutorials import ReadOnlyLibraryStore, _atomic_bytes, _offline_build_lease


def _active(root):
    value = read_manifest(root / "active.json", summary=True)
    if value is None:
        raise ValueError("Active search manifest is unavailable")
    return resolve_index_manifest(value, root)


def _remove_legacy_databases(root, directory, paths):
    removed = removed_bytes = 0
    for value in paths:
        path = _owned(root, Path(value))
        if path.parent.parent != directory or not path.parent.name.startswith("lib_") or path.name != "qmd-keyword.sqlite3":
            raise ValueError("Legacy keyword cleanup escaped its generation/library boundary")
        for suffix in ("", "-wal", "-shm", "-journal"):
            target = _owned(root, path.with_name(path.name + suffix))
            if target.exists():
                if target.stat().st_nlink > 1:
                    raise ValueError("Legacy keyword cleanup refuses shared hard links")
                removed_bytes += target.stat().st_size
                target.unlink()
                removed += int(not suffix)
        try:
            path.parent.rmdir()
        except OSError:
            pass  # Vector databases and any other retained files keep it alive.
    return {"removed_databases": removed, "removed_bytes": removed_bytes}


def _resume_cleanup(root, journal_path):
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    current = _active(root)
    directory = _owned(root, Path(journal["directory"]))
    shared = _owned(root, directory / "qmd-keyword.sqlite3")
    if (journal.get("schema") != "qmd-shared-migration/v1" or journal.get("phase") not in {"publication_pending", "published_cleanup_pending"}
            or current.get("directory") != str(directory)
            or current.get("generation") != journal["generation"]):
        raise ValueError("Shared-keyword recovery journal does not match the active generation")
    if journal["phase"] == "publication_pending" and current.get("keyword_storage") != KEYWORD_STORAGE:
        allowed = {root / "active.json", root / "last-build.json", directory / "manifest.json"}
        for value, encoded in journal["previous_files"].items():
            path = _owned(root, Path(value))
            if path not in allowed:
                raise ValueError("Shared-keyword recovery path is not an owned manifest")
            if encoded is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_bytes(path, base64.b64decode(encoded, validate=True))
        for suffix in ("", "-wal", "-shm", "-journal"):
            shared.with_name(shared.name + suffix).unlink(missing_ok=True)
        journal_path.unlink()
        return None
    if (current.get("keyword_storage") != KEYWORD_STORAGE
            or not shared.is_file() or current.get("generation") != journal["generation"]):
        raise ValueError("Shared-keyword recovery journal does not match the published generation")
    cleanup = _remove_legacy_databases(root, directory, journal["legacy_databases"])
    journal_path.unlink()
    return {"status": "resumed_cleanup", "generation": current["generation"], **cleanup,
            "shared_database": str(shared), "verification": journal["verification"]}


def merge_indexes(data_dir, *, qmd_dir=None, node=None, on_progress=None):
    """Publish one verified shared DB in the unchanged active generation."""
    store = ReadOnlyLibraryStore(data_dir)
    root = store.root / "search"
    journal_path = store.root / ".qmd-shared-migration.json"
    with _offline_build_lease(store):
        if journal_path.exists():
            resumed = _resume_cleanup(root, journal_path)
            if resumed is not None:
                return resumed
        old = _active(root)
        if not old.get("modes", {}).get("qmd_keyword", {}).get("ready"):
            raise ValueError("A ready keyword generation is required for saved-row migration")
        directory = _owned(root, Path(old["directory"]))
        shared = _owned(root, directory / "qmd-keyword.sqlite3")
        if old.get("keyword_storage") == KEYWORD_STORAGE:
            if not shared.is_file():
                raise FileNotFoundError("Published shared keyword database is missing")
            return {"status": "unchanged", "generation": old["generation"], "shared_database": str(shared)}
        if shared.exists():
            raise FileExistsError("Unpublished shared keyword output exists; inspect it before retrying")
        engine = LibrarySearch(root, qmd_dir=qmd_dir, node=node)
        snapshots = {root / name: ((root / name).read_bytes() if (root / name).is_file() else None)
                     for name in ("active.json", "last-build.json")}
        authority = _owned(root, directory / "manifest.json")
        snapshots[authority] = authority.read_bytes() if authority.is_file() else None
        published = False
        try:
            report = merge_keyword_indexes(old["libraries"], shared, qmd_dir=engine.qmd_dir, node=engine.node,
                                           env=engine._env(), on_progress=on_progress)
            with closing(sqlite3.connect((directory / "objects.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                native_counts = dict(db.execute("SELECT library_id,count(*) FROM objects GROUP BY library_id"))
            actual_counts = {library: item["documents"] for library, item in report["collections"].items()}
            if report["documents"] != old["object_count"] or native_counts != actual_counts:
                raise ValueError("Saved QMD collection counts differ from the unchanged native generation")
            updated = dict(old, keyword_storage=KEYWORD_STORAGE, libraries={})
            legacy = set()
            for library, item in old["libraries"].items():
                legacy.add(str(_owned(root, Path(item["db_path"]))))
                updated["libraries"][library] = dict(item, db_path=str(shared), collection=item["key"])
            updated["modes"] = {mode: dict(value) for mode, value in old["modes"].items()}
            updated["modes"]["qmd_keyword"].update(backend="qmd-shared-fts", qmd_version="2.8.3")
            updated["keyword_migration"] = {"strategy": "saved-qmd-collections", "embedding_invoked": False,
                                             "source_databases": report["source_databases"], "content_rows": report["content_rows"]}
            journal = {"schema": "qmd-shared-migration/v1", "phase": "publication_pending", "generation": old["generation"],
                       "directory": str(directory), "legacy_databases": sorted(legacy), "verification": report,
                       "previous_files": {str(path): base64.b64encode(value).decode("ascii") if value is not None else None
                                          for path, value in snapshots.items()}}
            _write_json(journal_path, journal)
            publish_manifest(directory, updated, root)
            published = True
            journal["phase"] = "published_cleanup_pending"
            _write_json(journal_path, journal, cancellable=False)
            cleanup = _remove_legacy_databases(root, directory, sorted(legacy))
            journal_path.unlink()
            return {"status": "merged", "generation": old["generation"], "shared_database": str(shared),
                    "verification": report, **cleanup}
        except BaseException:
            if not published:
                try:
                    for path, value in snapshots.items():
                        if value is None:
                            path.unlink(missing_ok=True)
                        else:
                            _atomic_bytes(path, value)
                except BaseException as restore_error:
                    raise RuntimeError("Manifest restoration failed; shared output and recovery journal retained") from restore_error
                for suffix in ("", "-wal", "-shm", "-journal"):
                    shared.with_name(shared.name + suffix).unlink(missing_ok=True)
                journal_path.unlink(missing_ok=True)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--qmd-dir", type=Path)
    parser.add_argument("--node", type=Path)
    parser.add_argument("--output", "--report", dest="output", type=Path, help="Optional small verification report")
    parser.add_argument("--progress-log", type=Path)
    args = parser.parse_args(argv)
    if args.progress_log:
        args.progress_log.parent.mkdir(parents=True, exist_ok=True)
    log = args.progress_log.open("a", encoding="utf-8") if args.progress_log else None
    def progress(value):
        line = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **value}, ensure_ascii=False)
        print(line, flush=True)
        if log:
            log.write(line + "\n")
            log.flush()
    try:
        result = merge_indexes(args.data_dir, qmd_dir=args.qmd_dir, node=args.node, on_progress=progress)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        progress(result)
        return 0
    except Exception as exc:
        progress({"status": "failed", "error": str(exc)})
        return 1
    finally:
        if log:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
