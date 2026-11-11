"""Run real focused simplification research on a previously generated plan."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.config import DATA
from advisor.server import App
from advisor.sessions import export_markdown


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", help="Existing session ID; this calls the logged-in model and consumes usage")
    args = parser.parse_args()
    app = App()
    session = app.sessions.get(args.session)
    original = app.planner.delegate.backend.generate

    def observe(prompt, schema, **kwargs):
        started = time.monotonic()
        print(json.dumps({"stage": kwargs.get("stage"), "status": "running", "prompt_characters": len(prompt)}), flush=True)
        result = original(prompt, schema, **kwargs)
        print(json.dumps({"stage": kwargs.get("stage"), "status": "completed", "seconds": round(time.monotonic() - started, 2)}), flush=True)
        return result

    app.planner.delegate.backend.generate = observe
    session = app.sessions.refine(session["id"], session["revision"], focus="simplification")
    path = DATA / "exports" / "demo-step-simplification.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(export_markdown(session), encoding="utf-8")
    design = session.get("design_brief", {}).get("simplification", {})
    print(json.dumps({"session": session["id"], "revision": session["revision"], "status": session["status"],
                      "candidates": len(design.get("candidates", [])), "artifact": str(path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
