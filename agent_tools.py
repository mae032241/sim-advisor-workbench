"""JSON tool surface for Pi TUI/SDK or any coding agent with subprocess tools."""
import argparse
import json
import sys
from pathlib import Path

from advisor.config import DATA
from advisor.knowledge import KnowledgeBase
from advisor.library_provider import LibraryKnowledgeProvider
from advisor.query_terms import normalize_mode
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
    search.add_argument("--backend", type=normalize_mode, choices=["qmd_keyword", "qmd_vector"], help=argparse.SUPPRESS)
    search.add_argument("--mode", type=normalize_mode, choices=["qmd_keyword", "qmd_vector"])
    search.add_argument("--library", action="append", dest="library_ids")
    search.add_argument("--branch")
    search.add_argument("--feature-branch")
    search.add_argument("--case-id", help="Exact case overview ID; search its prepared settings/tutorial scope")
    search.add_argument("--stage", help="Implementation stage within --case-id")
    cases = sub.add_parser("cases", help="QMD case discovery over model overviews only")
    cases.add_argument("query")
    cases.add_argument("--library", action="append", dest="library_ids")
    cases.add_argument("--limit", type=int, default=6)
    cases.add_argument("--include-geometry-only", action="store_true")
    guide = sub.add_parser("case-guide", help="Prepared implementation navigation; does not count as a source read")
    guide.add_argument("id")
    guide.add_argument("--stage", help="Expand one implementation stage; default is a small flow overview")
    source = sub.add_parser("source")
    source.add_argument("id")
    sub.add_parser("list", help="List current public tool definitions without opening a library or plan")
    contract = sub.add_parser("contract", help="Read the actual tool input contract without opening a library or plan")
    contract.add_argument("tool")
    contract.add_argument("--path", help="JSON pointer into input data, e.g. /design_brief/simplification/candidates/0/status")
    contract.add_argument("--offset", type=int)
    contract.add_argument("--length", type=int)
    scope = sub.add_parser("scope", help="List lightweight library groups and current enabled state")
    scope.add_argument("--query")
    scope.add_argument("--type", choices=["official-manuals", "official-models", "user-manuals", "user-models"])
    enable = sub.add_parser("enable-many", help="Enable or disable explicit libraries without building indexes")
    enable.add_argument("--library", action="append", dest="library_ids", required=True)
    enable.add_argument("--enabled", choices=["true", "false"], required=True)
    sub.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command in {"list", "contract"}:
            from advisor.planning_service import tool_definitions
            from advisor.chat_bridge_tools import external_tool_definitions
            definitions = tool_definitions() + external_tool_definitions()
            if args.command == "list":
                result = {"tools": definitions}
            else:
                from advisor.tool_contracts import read_contract
                arguments = {key: getattr(args, key) for key in ("tool", "path", "offset", "length") if getattr(args, key) is not None}
                result = read_contract(definitions, arguments)
        elif args.command == "inspect":
            result = inspect_model(args.path)
        elif args.command in {"scope", "enable-many"}:
            store = LibraryStore(args.data_dir / "libraries")
            result = store.library_scope(query=args.query, type=args.type) if args.command == "scope" else store.set_enabled_many(args.library_ids, args.enabled == "true")
        else:
            kb = LibraryKnowledgeProvider(KnowledgeBase(args.data_dir), args.data_dir / "libraries")
            if args.command == "search":
                mode = args.mode or args.backend or "qmd_keyword"
                result = {"results": kb.search(args.query, args.limit, args.kind, {"mode": mode, "branch": args.branch, "feature_branch": args.feature_branch, "library_ids": args.library_ids, "case_id": args.case_id, "stage": args.stage})}
            elif args.command == "cases":
                result = {"results": kb.search_cases(args.query, library_ids=args.library_ids, limit=args.limit, include_geometry_only=args.include_geometry_only)}
            elif args.command == "case-guide":
                from advisor.case_guides import get_case_guide
                from advisor.planning_service import _guide_view
                from advisor.case_metadata import normalize_feature_branch
                result = _guide_view(get_case_guide(kb.store, args.id), normalize_feature_branch(args.stage))
            elif args.command == "source":
                result = kb.get_document(args.id)
                if result is None:
                    raise ValueError("Source ID not found")
            else:
                result = kb.status()
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, RuntimeError) as exc:
        payload = exc.to_dict() if callable(getattr(exc, "to_dict", None)) else {"error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
