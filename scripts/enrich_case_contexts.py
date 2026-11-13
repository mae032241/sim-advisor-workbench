"""Derive bounded canonical tutorial context and update exact QMD overview rows."""
from pathlib import Path
import argparse
import json
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from advisor.case_context_index import update_case_contexts, recover_case_context_update, rebind_case_contexts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--case", action="append", dest="cases", help="Exact non-heldout case overview ID; repeat up to 64")
    parser.add_argument("--apply", action="store_true", help="Apply an incremental update; default only derives and reports")
    parser.add_argument("--recover", action="store_true", help="Roll back an interrupted context publication using its small undo journal")
    parser.add_argument("--rebind", action="store_true", help="Refresh metadata after moving the data directory; QMD text and embeddings stay unchanged")
    parser.add_argument("--report", type=Path, help="Write the full provenance report to this explicit file")
    args = parser.parse_args(argv)
    if (args.recover or args.rebind) and (args.cases or args.apply) or args.recover and args.rebind:
        parser.error("--recover/--rebind cannot be combined with --case, --apply, or each other")
    if not args.recover and not args.rebind and not args.cases:
        parser.error("Select explicit --case IDs")
    try:
        result = (recover_case_context_update(args.data_dir) if args.recover else rebind_case_contexts(args.data_dir) if args.rebind else
                  update_case_contexts(args.data_dir, args.cases, apply=args.apply))
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Console stays small; full exact source excerpts are opt-in report data.
        print(json.dumps({key: value for key, value in result.items() if key != "profiles"}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"state": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
