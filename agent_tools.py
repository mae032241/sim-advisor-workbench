"""JSON tool surface for Pi TUI/SDK or any coding agent with subprocess tools."""
import argparse
import json
import sys
from pathlib import Path

from advisor.config import DATA
from advisor.knowledge import KnowledgeBase
from advisor.library_provider import LibraryKnowledgeProvider
from advisor.library_store import LibraryStore
from advisor.model_reader import inspect_model


def main():
    parser = argparse.ArgumentParser(description="COMSOL local knowledge/model tools; stdout is JSON")
    parser.add_argument("--data-dir", type=Path, default=DATA)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("path")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--kind", choices=["all", "manual", "case", "code"], default="all")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--backend", choices=["native", "qmd"], help="Compatibility alias: qmd selects qmd_keyword")
    search.add_argument("--mode", choices=["native", "qmd_keyword", "qmd_vector"])
    search.add_argument("--library", action="append", dest="library_ids")
    search.add_argument("--branch")
    search.add_argument("--feature-branch")
    source = sub.add_parser("source")
    source.add_argument("id")
    scope = sub.add_parser("scope", help="List lightweight library groups and current enabled state")
    scope.add_argument("--query")
    scope.add_argument("--type", choices=["official-manuals", "official-models", "user-manuals", "user-models"])
    enable = sub.add_parser("enable-many", help="Enable or disable explicit libraries without building indexes")
    enable.add_argument("--library", action="append", dest="library_ids", required=True)
    enable.add_argument("--enabled", choices=["true", "false"], required=True)
    sub.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect_model(args.path)
        elif args.command in {"scope", "enable-many"}:
            store = LibraryStore(args.data_dir / "libraries")
            result = store.library_scope(query=args.query, type=args.type) if args.command == "scope" else store.set_enabled_many(args.library_ids, args.enabled == "true")
        else:
            kb = LibraryKnowledgeProvider(KnowledgeBase(args.data_dir), args.data_dir / "libraries")
            if args.command == "search":
                mode = args.mode or ("qmd_keyword" if args.backend == "qmd" else "native")
                result = {"results": kb.search(args.query, args.limit, args.kind, {"mode": mode, "branch": args.branch, "feature_branch": args.feature_branch, "library_ids": args.library_ids})}
            elif args.command == "source":
                result = kb.get_document(args.id)
                if result is None:
                    raise ValueError("Source ID not found")
            else:
                result = kb.status()
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
