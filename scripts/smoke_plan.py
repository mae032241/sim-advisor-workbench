"""Real local integration smoke: model + manuals + persistence; --refine uses CLI."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.config import DATA, examples
from advisor.server import App
from advisor.sessions import export_markdown


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refine", action="store_true", help="Call the logged-in Codex CLI; may consume account usage")
    parser.add_argument("--example", help="Example ID from /api/status; defaults to the geometry-first example")
    parser.add_argument("--goal", help="Optional explicit goal for this demonstration, without changing the built-in example")
    args = parser.parse_args()
    app = App()
    example = next((item for item in examples() if item["id"] == args.example), None) if args.example else examples()[0]
    if not example:
        raise ValueError("Unknown example ID")
    model = app.inspect(example["path"])
    session = app.sessions.create("[演示验收] " + (args.goal or example["goal"]), model)
    print(json.dumps({"stage": "intake", "session": session["id"], "asset_kind": model.get("asset_kind"), "parser": model["parser"], "sources": len(session["sources"]), "steps": len(session["plan"]["steps"])}, ensure_ascii=False), flush=True)
    if args.refine:
        original_generate = app.planner.delegate.backend.generate
        def observe_stage(prompt, schema, **kwargs):
            started = time.monotonic()
            print(json.dumps({"stage": kwargs.get("stage"), "status": "running"}), flush=True)
            result = original_generate(prompt, schema, **kwargs)
            # Preserve completed structured outputs for this explicit demo test,
            # so a later-stage failure does not erase diagnostic evidence.
            artifacts = DATA / "exports" / "research-stages" / session["id"]
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / (kwargs["stage"] + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"stage": kwargs.get("stage"), "status": "completed", "seconds": round(time.monotonic() - started, 2)}), flush=True)
            return result
        app.planner.delegate.backend.generate = observe_stage
        session = app.sessions.refine(session["id"], session["revision"])
    output = DATA / "exports"
    output.mkdir(exist_ok=True)
    path = output / ("demo-step-design.md" if args.refine and model.get("asset_kind") == "cad_geometry" else "demo-research-plan.md" if args.refine else "demo-task-intake.md")
    path.write_text(export_markdown(session), encoding="utf-8")
    print(json.dumps({"stage": "complete", "session": session["id"], "revision": session["revision"], "engine": session["plan"]["engine"], "routes": len(session.get("design_brief",{}).get("candidate_routes",[])), "steps": len(session["plan"]["steps"]), "questions": len(session["questions"]), "artifact": str(path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
