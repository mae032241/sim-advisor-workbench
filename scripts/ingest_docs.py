"""Build/inspect the local official COMSOL corpus; optional actual QMD index."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.knowledge import KnowledgeBase
from advisor.retrieval_backends import QMDAdapter
from advisor.config import DATA


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc-root", type=Path)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--qmd", action="store_true", help="Also export the corpus and build/verify actual QMD keyword retrieval")
    parser.add_argument("--query")
    args = parser.parse_args()
    kb = KnowledgeBase(args.data_dir, args.doc_root)
    result = kb.build()
    if args.qmd and result["ready"]:
        result["qmd_build"] = QMDAdapter(args.data_dir).prepare(kb)
        result["backends"] = kb.status()["backends"]
    if args.query:
        result["results"] = [{k: r[k] for k in ("id", "title", "url", "section_path", "kind", "score", "excerpt")} for r in kb.search(args.query)]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
