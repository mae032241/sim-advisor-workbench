"""Inspect or remove obsolete independent object FTS during offline maintenance.

Defaults to a read-only dry run. Stop application/CLI consumers before --apply.
Only owned objects.sqlite3 databases and native mode manifest metadata change;
saved QMD keyword and vector databases are checked byte for byte.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from advisor.index_manifest import _owned, _write_json, normalize_modes
from advisor.object_index import remove_legacy_fts
from advisor.qmd_worker import suspend_workers
from scripts.normalize_tutorials import ReadOnlyLibraryStore, _offline_build_lease

_GENERATION = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _inventory(root):
    databases, manifests, qmd = [], [], []
    generations = _owned(root, root / "generations")
    if generations.exists():
        for directory in sorted(generations.iterdir()):
            _owned(root, directory)
            if not directory.is_dir() or not _GENERATION.fullmatch(directory.name):
                raise ValueError("Unexpected search generation; FTS removal refused")
            database = _owned(root, directory / "objects.sqlite3")
            if database.exists():
                databases.append(database)
            manifest = _owned(root, directory / "manifest.json")
            if manifest.exists():
                manifests.append(manifest)
            # QMD payloads are verified as files, never opened or rebuilt.
            for path in sorted(directory.rglob("qmd-*.sqlite3")):
                _owned(root, path)
                if not path.is_file() or path.stat().st_nlink != 1:
                    raise ValueError("QMD verification requires unshared regular databases")
                qmd.append(path)
    for name in ("active.json", "last-build.json"):
        path = _owned(root, root / name)
        if path.exists():
            manifests.append(path)
    return databases, manifests, qmd


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def remove_indexes(data_dir, *, apply=False, reclaim=True):
    """Inspect every retained generation, then apply under the offline lease."""
    store = ReadOnlyLibraryStore(data_dir)
    root = store.root / "search"

    def run():
        databases, manifest_paths, qmd_paths = _inventory(root)
        previews = [remove_legacy_fts(path, dry_run=True, reclaim=reclaim) for path in databases]
        manifests = []
        for path in manifest_paths:
            raw = path.read_bytes()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Index manifest must be an object")
            normalized = normalize_modes(value)
            if normalized != value:
                manifests.append((path, raw, normalized))
        before = {path.relative_to(root).as_posix(): _hash_file(path) for path in qmd_paths}
        results = previews
        if apply:
            results = [remove_legacy_fts(path, dry_run=False, offline=True, reclaim=reclaim) for path in databases]
            for path, original, normalized in manifests:
                if path.read_bytes() != original:
                    raise RuntimeError("Index manifest changed during offline maintenance")
                _write_json(path, normalized)
        after = {path.relative_to(root).as_posix(): _hash_file(path) for path in qmd_paths} if apply else before
        if after != before:
            raise ValueError("Saved QMD database bytes changed during object FTS removal")
        return {"schema": "remove-legacy-object-fts/v1", "dry_run": not apply,
                "search_root": str(root), "databases_checked": len(results),
                "databases_with_legacy_fts": sum(item["legacy_fts_present"] for item in results),
                "removed_tables": sum(item["removed_tables"] for item in results),
                "reclaimed_bytes": sum(item["reclaimed_bytes"] for item in results),
                "manifests_to_normalize": len(manifests),
                "manifests_normalized": len(manifests) if apply else 0,
                "qmd_databases_checked": len(before), "qmd_bytes_unchanged": True,
                "qmd_files": before, "embedding_invoked": False, "databases": results}

    if not apply:
        return run()
    with _offline_build_lease(store), suspend_workers(root):
        return run()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, help="Data or library directory containing the catalog")
    parser.add_argument("--apply", action="store_true", help="Apply under an offline maintenance lease; default is read-only inspection")
    parser.add_argument("--no-reclaim", action="store_true", help="Drop obsolete FTS without reclaiming SQLite free pages")
    parser.add_argument("--report", type=Path, help="Also write the JSON verification report to this explicit file")
    args = parser.parse_args(argv)
    result = remove_indexes(args.data_dir, apply=args.apply, reclaim=not args.no_reclaim)
    body = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(body + "\n", encoding="utf-8")
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
