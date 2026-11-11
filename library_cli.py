"""Local library constructor; stdout is JSON and failures use a nonzero exit."""
import argparse
import json
from pathlib import Path
import sys
from advisor.config import DATA
from advisor.library_store import LibraryStore
from advisor.case_metadata import FEATURE_BRANCHES, normalize_feature_branch
from advisor.knowledge_views import related_sources_view


def main():
    parser = argparse.ArgumentParser(description="Build and query versioned simulation libraries")
    parser.add_argument("--data-dir", type=Path, default=DATA / "libraries")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("name")
    add.add_argument("--id")
    add.add_argument("--type", choices=["official-manuals", "official-models", "user-manuals", "user-models"], required=True,
                     help="Declared source category; official/custom identity is not inferred from the source path")
    add.add_argument("--path", type=Path, required=True)
    add.add_argument("--docs-root", type=Path)
    add.add_argument("--version", default="")
    cmd = commands.add_parser("list")
    cmd.add_argument("--include-deleted", action="store_true")
    cmd = commands.add_parser("scope", help="Read lightweight library groups and enabled state, including disabled libraries")
    cmd.add_argument("--query")
    cmd.add_argument("--type", choices=["official-manuals", "official-models", "user-manuals", "user-models"])
    cmd = commands.add_parser("enable-many", help="Enable or disable explicit library IDs atomically without indexing")
    cmd.add_argument("--library", action="append", dest="library_ids", required=True)
    cmd.add_argument("--enabled", choices=["true", "false"], required=True)
    commands.add_parser("runtime", help="Read dependency paths/provenance for this data directory without building an index")
    cmd = commands.add_parser("cancel", help="Request cancellation of one build job without stopping the workbench")
    cmd.add_argument("job_id")
    for action in ("enable", "disable", "delete", "restore", "purge", "models"):
        cmd = commands.add_parser(action, help="Irreversibly remove a deleted library's derived data" if action == "purge" else "Library visibility; sources and historical revisions are retained")
        cmd.add_argument("library_id")
        if action == "purge":
            cmd.add_argument("--confirm", action="store_true", help="Irreversibly remove this deleted library's derived data; keep original sources")
        if action == "models":
            cmd.add_argument("--limit", type=int, default=None, help="Optional explicit sample limit; default lists all registered models without hashing")
    for action in ["scan", "build", "status", "rebind"]:
        cmd = commands.add_parser(action)
        cmd.add_argument("library_id")
        if action in {"scan", "build"}:
            cmd.add_argument("--path", action="append", dest="paths")
            cmd.add_argument("--limit", type=int)
        if action == "build":
            cmd.add_argument("--shallow", action="store_true")
            cmd.add_argument("--model-format", choices=["compact", "legacy"], default="compact",
                             help="Model storage format; compact keeps selected settings once with source evidence links")
            cmd.add_argument("--enrich", action="store_true")
            cmd.add_argument("--modes", default="native")
        if action == "rebind":
            cmd.add_argument("--root-id", default="main")
            cmd.add_argument("--path", required=True)
    cmd = commands.add_parser("index")
    cmd.add_argument("--modes", default="native,qmd_keyword", help="Default: native,qmd_keyword; qmd_vector requires an explicit selection")
    cmd = commands.add_parser("search")
    cmd.add_argument("query")
    cmd.add_argument("--mode", default="native", choices=["native", "qmd_keyword", "qmd_vector", "qmd_hybrid"])
    cmd.add_argument("--kind", default="all", choices=["all", "manual", "case", "code"])
    cmd.add_argument("--feature-branch", type=normalize_feature_branch, metavar="BRANCH", help="Optional category: " + ",".join(FEATURE_BRANCHES))
    cmd.add_argument("--library", action="append", dest="library_ids")
    cmd.add_argument("--limit", type=int, default=8)
    cmd = commands.add_parser("read")
    cmd.add_argument("id")
    cmd = commands.add_parser("configuration")
    cmd.add_argument("id")
    cmd.add_argument("--category")
    cmd.add_argument("--node-path")
    cmd.add_argument("--limit", type=int, default=20)
    cmd.add_argument("--source", choices=["api", "saved"], default="api")
    cmd = commands.add_parser("evidence")
    cmd.add_argument("id")
    cmd.add_argument("--index", type=int, default=0)
    cmd.add_argument("--line", type=int)
    cmd.add_argument("--limit", type=int, default=120)
    args = parser.parse_args()
    try:
        if args.command == "runtime":
            from advisor.library_search import LibrarySearch
            print(json.dumps(LibrarySearch(args.data_dir / "search").dependency_status(), ensure_ascii=False, indent=2))
            return 0
        store = LibraryStore(args.data_dir)
        if args.command == "add":
            roots = [{"id": "main", "path": str(args.path), "role": "manuals" if args.type in {"official-manuals", "user-manuals"} else "models"}]
            if args.docs_root:
                roots.append({"id": "docs", "path": str(args.docs_root), "role": "docs"})
            result = store.register(args.name, args.type, roots, args.version, args.id)
        elif args.command == "list":
            result = store.libraries(include_deleted=True) if args.include_deleted else store.libraries()
        elif args.command == "scope":
            result = store.library_scope(query=args.query, type=args.type)
        elif args.command == "enable-many":
            result = store.set_enabled_many(args.library_ids, args.enabled == "true")
        elif args.command in {"enable", "disable"}:
            result = store.set_enabled(args.library_id, args.command == "enable")
        elif args.command == "delete":
            result = store.delete_library(args.library_id)
        elif args.command == "restore":
            result = store.restore_library(args.library_id)
        elif args.command == "purge":
            result = store.purge_library(args.library_id, confirmed=args.confirm)
        elif args.command == "models":
            result = store.model_candidates(args.library_id, limit=args.limit)
        elif args.command == "scan":
            result = store.scan(args.library_id, args.paths, args.limit)
        elif args.command == "build":
            result = store.build(args.library_id, paths=args.paths, limit=args.limit,
                                 deep=not args.shallow, enrich=args.enrich, modes=args.modes.split(","),
                                 model_format=args.model_format)
        elif args.command == "index":
            result = store.reindex(args.modes.split(","))
        elif args.command == "cancel":
            result = store.cancel_build(args.job_id)
        elif args.command == "status":
            result = {"library": store.library(args.library_id), "jobs": [j for j in store.jobs_summary() if j["library_id"] == args.library_id or args.library_id in j.get("library_ids", [])], "indexes": store.index_status_summary()}
        elif args.command == "rebind":
            result = store.rebind(args.library_id, args.root_id, args.path)
        elif args.command == "read":
            result = store.read(args.id)
            if result is None:
                raise ValueError("Object not found")
            result = dict(result)
            try:
                related = getattr(store, "related_sources", None)
                result["related_sources"] = related_sources_view(related(args.id, limit=8)) if callable(related) else []
            except Exception:
                result["related_sources"] = []
                result["related_sources_warning"] = "关联来源摘要暂不可读取；当前正文仍可查看。"
        elif args.command == "configuration":
            result = store.configuration(args.id, category=args.category, node_path=args.node_path, limit=args.limit, source=args.source)
        elif args.command == "evidence":
            result = store.read_evidence(args.id, index=args.index, line=args.line, limit=args.limit)
        else:
            from advisor.library_search import LibrarySearch
            if not store.index_status_summary().get("modes", {}).get(args.mode, {}).get("ready"):
                raise ValueError("Requested index is unavailable or stale; run index first")
            result = LibrarySearch(store.root / "search").search(args.query, mode=args.mode, library_ids=args.library_ids, limit=args.limit, kind=args.kind, feature_branch=args.feature_branch)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get("status") == "cancelled":
            return 0 if args.command == "cancel" else 130
        if args.command == "index" and isinstance(result, dict):
            return 0 if all(result.get("modes", {}).get(mode, {}).get("ready") for mode in args.modes.split(",")) else 1
        return 1 if isinstance(result, dict) and result.get("status") == "failed" else 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
