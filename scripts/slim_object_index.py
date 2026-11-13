"""Inspect or slim retained object metadata without rebuilding QMD indexes.

Defaults to a read-only preview. Stop application/CLI consumers before --apply.
An apply holds the offline build lease and atomically replaces each owned object
database after validation. Catalogs, QMD databases and embeddings are untouched.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from advisor.index_manifest import _owned
from advisor.object_index import slim_object_index
from advisor.qmd_worker import suspend_workers
from scripts.normalize_tutorials import ReadOnlyLibraryStore, _offline_build_lease

_GENERATION = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def slim_indexes(data_dir, *, apply=False, on_progress=None):
    """Migrate retained generations under the existing offline maintenance lease."""
    store = ReadOnlyLibraryStore(data_dir)
    root = store.root / "search"

    def run():
        generations = _owned(root, root / "generations")
        databases = []
        if generations.exists():
            for directory in sorted(generations.iterdir()):
                _owned(root, directory)
                if not directory.is_dir() or not _GENERATION.fullmatch(directory.name):
                    raise ValueError("Unexpected search generation; object metadata slimming refused")
                database = _owned(root, directory / "objects.sqlite3")
                if database.exists():
                    databases.append(database)
        results = []
        for path in databases:
            def progress(value):
                if on_progress:
                    on_progress({"database": str(path), **value})
            results.append(slim_object_index(path, dry_run=not apply, offline=apply, on_progress=progress))
        return {"schema": "slim-object-index/v1", "dry_run": not apply,
                "search_root": str(root), "databases_checked": len(results),
                "databases_changed": sum(item["changed"] for item in results),
                "objects": sum(item["objects"] for item in results),
                "search_input_bytes_removed": sum(item["search_input_bytes_removed"] for item in results),
                "representation_before_bytes": sum(item["representation_before_bytes"] for item in results),
                "representation_after_bytes": sum(item["representation_after_bytes"] for item in results),
                "before_bytes": sum(item["before_bytes"] for item in results),
                "after_bytes": sum(item["after_bytes"] for item in results),
                "reclaimed_bytes": sum(item["reclaimed_bytes"] for item in results),
                "metadata_columns_preserved": all(item["metadata_columns_preserved"] for item in results),
                "statistics_preserved": all(item["statistics_preserved"] for item in results),
                "rowids_preserved": all(item["rowids_preserved"] for item in results),
                "catalog_opened": False, "qmd_opened": False, "embedding_invoked": False,
                "databases": results}

    if not apply:
        return run()
    with _offline_build_lease(store), suspend_workers(root):
        return run()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, help="Data or library directory containing the catalog")
    parser.add_argument("--apply", action="store_true", help="Apply under the offline maintenance lease; default is read-only inspection")
    parser.add_argument("--report", type=Path, help="Also write the JSON verification report to this explicit file")
    args = parser.parse_args(argv)

    def progress(value):
        print(json.dumps({"progress": value}, ensure_ascii=False), file=sys.stderr, flush=True)

    result = slim_indexes(args.data_dir, apply=args.apply, on_progress=progress)
    body = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(body + "\n", encoding="utf-8", newline="\n")
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
