"""Build metadata navigation from existing generic-manual native toc.xml files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from advisor.html_tree import build_html_tree


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--library-id", required=True)
    parser.add_argument("--root-id", default="help")
    parser.add_argument("--package", action="append", default=None)
    args = parser.parse_args(argv)
    result = build_html_tree(args.html_root, args.output, library_id=args.library_id,
                             root_id=args.root_id, packages=args.package)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
