"""Build metadata-only navigation from existing compact case configurations."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sys

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from advisor.case_tree import SCHEMA, _authority, build_case_trees
from advisor.library_store import LibraryStore


def select_cases(store, *, case_ids=None, libraries=None, limit=3):
    """Catalog-only, deterministic selection; never consult benchmark material."""
    if case_ids:
        if libraries:
            raise ValueError("Explicit case IDs cannot be combined with library sampling")
        selected = list(dict.fromkeys(case_ids))
        for identity in selected:
            _authority(store, identity, load_reader=False)
        return selected
    if type(limit) is not int or limit < 1:
        raise ValueError("Sample limit must be positive")
    visible = store.searchable_library_ids(libraries)
    if not visible:
        return []
    with closing(store.connect()) as db:
        rows = db.execute("SELECT r.id FROM active a JOIN revisions r ON r.id=a.citation_id "
                          "WHERE a.library_id IN (" + ",".join("?" for _ in visible) + ") "
                          "AND json_extract(r.payload,'$.type')='model_case' "
                          "AND json_extract(r.payload,'$.data.compact_case.format') IS NOT NULL ORDER BY r.id", visible).fetchall()
    result = []
    for (identity,) in rows:
        try:
            # Exclusion applies at construction as well as later navigation.
            store.require_not_heldout(identity)
            obj = store.get(identity)
            store.require_not_heldout(identity, obj)
        except PermissionError:
            continue
        result.append(identity)
        if len(result) == limit:
            break
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="Existing LibraryStore root")
    parser.add_argument("--case-id", action="append", default=None)
    parser.add_argument("--library", action="append", default=None)
    parser.add_argument("--limit", type=int, default=None, help="Deterministic sample size (default 3)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--no-manual-links", action="store_true", help="Build only the case tree for diagnostics")
    args = parser.parse_args(argv)
    if args.case_id and (args.library or args.limit is not None):
        parser.error("--case-id cannot be combined with --library or --limit")
    if not (args.data_dir / "catalog.sqlite3").is_file():
        parser.error("--data-dir must contain an existing catalog.sqlite3")
    store = LibraryStore(args.data_dir)
    identities = select_cases(store, case_ids=args.case_id, libraries=args.library, limit=3 if args.limit is None else args.limit)
    selection = {"schema": SCHEMA, "selection_basis": "explicit_case_ids" if args.case_id else "sorted_public_active_compact_catalog",
                 "case_ids": identities, "selected_count": len(identities), "libraries": args.library or [],
                 "dry_run": args.dry_run, "configuration_values_copied": False, "models_executed": False}
    result = {"selection": selection}
    if not args.dry_run:
        if not identities:
            parser.error("No eligible current compact cases selected")
        result["tree"] = build_case_trees(store, identities)
        if not args.no_manual_links:
            from advisor.case_manual_links import CaseManualLinks
            matcher = CaseManualLinks(store)
            result["manual_links"] = [matcher.build(identity) for identity in identities]
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
