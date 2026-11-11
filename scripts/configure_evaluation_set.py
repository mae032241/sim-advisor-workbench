"""Administrator-only logical evaluation grouping; never rebuilds indexes.

Agent tools deliberately do not expose this command. Selections and exclusions
are portable metadata under data-dir/libraries; original sources remain intact.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from advisor.evaluation_sets import compile_registry, write_registry, refresh_registry, REGISTRY_FILENAME


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--selection", type=Path, help="Add/replace this named selection; retain all other sets")
    parser.add_argument("--refresh", action="store_true", help="Refresh exclusions for existing selections")
    parser.add_argument("--dry-run", action="store_true", help="Compile and report without publishing the registry")
    args = parser.parse_args()
    if bool(args.selection) == bool(args.refresh):
        parser.error("Choose exactly one of --selection or --refresh")
    root = (args.data_dir / "libraries").resolve()
    registry_path = root / REGISTRY_FILENAME
    previous = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.is_file() else None
    sets = previous["sets"] if previous else []
    if args.selection:
        selection = json.loads(args.selection.read_text(encoding="utf-8"))
        if not isinstance(selection, dict) or not selection.get("id"):
            raise ValueError("Selection must identify a named evaluation set")
        sets = [s for s in sets if s["id"] != selection["id"]] + [selection]
    if not sets:
        raise ValueError("No evaluation selection has been configured")
    def progress(update):
        if update.get("scanned_revisions", 0) % 100000 == 0:
            print(json.dumps({"stage": "compiling_exclusions", **update}), flush=True)
    if args.refresh and not args.dry_run:
        registry = refresh_registry(root, on_progress=progress)
    else:
        registry = compile_registry(root / "catalog.sqlite3", sets, on_progress=progress)
        if not args.dry_run:
            write_registry(root, registry)
    exclusions = registry["exclusions"]
    print(json.dumps({"sets": len(registry["sets"]), "selected_models": sum(len(s["models"]) for s in registry["sets"]),
                      "excluded_citations": len(exclusions["citation_ids"]), "excluded_objects": len(exclusions["object_ids"]),
                      "source_hashes": len(exclusions["source_hashes"]), "registry": str(registry_path),
                      "dry_run": args.dry_run, "catalog_modified": False, "indexes_rebuilt": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
