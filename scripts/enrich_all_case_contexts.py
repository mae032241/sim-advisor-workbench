"""Derive canonical context for every existing active case without model parsing."""
from pathlib import Path
import argparse
import json
import sqlite3
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from advisor.case_context_bulk import run_bulk_case_contexts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--all", action="store_true", help="Process all active exact overview IDs (the default scope)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Incrementally publish ready profiles in batches")
    mode.add_argument("--dry-run", action="store_true", help="Derive and report without changing the index (default)")
    parser.add_argument("--resume", action="store_true", help="Require the matching existing checkpoint; matching jobs otherwise resume automatically")
    parser.add_argument("--batch-size", type=int, default=64, choices=range(1, 65), metavar="1..64")
    parser.add_argument("--stop-file", type=Path, help="Stop between cases/batches while this file exists; default: libraries/search/runtime/case-context-bulk/stop. Remove it and rerun to resume")
    parser.add_argument("--report", type=Path, help="Write a small outcome/provenance-count report without source text")
    args = parser.parse_args(argv)
    try:
        result = run_bulk_case_contexts(args.data_dir, apply=args.apply, batch_size=args.batch_size,
                                        resume=args.resume, stop_file=args.stop_file, report=args.report)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["state"] == "failed" else 2 if result["state"] == "cancelled" else 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"schema": "case-context-bulk/v1", "state": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
