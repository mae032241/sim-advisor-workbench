"""Register official HTML sources and build the workbench's QMD index."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.config import DATA, DOC_ROOT
from advisor.knowledge import KnowledgeBase
from advisor.library_provider import LibraryKnowledgeProvider
from advisor.library_store import LibraryStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc-root", type=Path, default=DOC_ROOT)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--library-id", default="official-html-manuals")
    parser.add_argument("--qmd", action="store_true", help="Compatibility flag; QMD keyword is always used")
    parser.add_argument("--query")
    parser.add_argument("--enable", action="store_true", help="Explicitly enable this library before building/searching")
    args = parser.parse_args()
    store = LibraryStore(args.data_dir / "libraries")
    existing = {x["id"]: x for x in store.libraries(include_deleted=True)}
    if args.library_id not in existing:
        store.register(name="Official COMSOL HTML manuals", type="official-manuals",
                       roots=[{"id": "help", "role": "documents", "path": str(args.doc_root)}],
                       library_id=args.library_id)
    else:
        library = existing[args.library_id]
        if library.get("deleted_at"):
            raise ValueError("资料库已删除；请恢复它或使用新的 --library-id。")
        if not any(Path(r["path"]).resolve() == args.doc_root.resolve() for r in library["roots"]):
            raise ValueError("已登记的资料库使用不同目录；请在资料库管理中重新关联目录。")
    if args.enable:
        store.set_enabled(args.library_id, True)
    if not store.library(args.library_id)["enabled"]:
        raise ValueError("资料库已登记，默认关闭；请在资料库管理中启用，或显式传入 --enable 后构建。")
    result = store.build(args.library_id, modes=["qmd_keyword"])
    if args.query:
        if not store.library(args.library_id)["enabled"]:
            raise ValueError("资料已构建；新库默认关闭，请先启用再检索。")
        kb = LibraryKnowledgeProvider(KnowledgeBase(args.data_dir, args.doc_root), store.root)
        result["results"] = kb.search(args.query, filters={"library_ids": [args.library_id]})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
