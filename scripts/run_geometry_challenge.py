"""Run a geometry-only planning test with held-out sources excluded.

Only a neutral STEP snapshot and the public goal reach the planner. Original
models, preparation provenance and the rubric remain on the evaluation side.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from advisor.agent_backend import AgentBackendError, CodexJSONBackend
from advisor.config import DATA
from advisor.knowledge import KnowledgeBase
from advisor.library_provider import LibraryKnowledgeProvider
from advisor.model_reader import inspect_model
from advisor.planner import AgentPlanner
from advisor.process_runner import run_text
from advisor.sessions import SessionStore, export_markdown, write_json


def fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def check_geometry_input(model, forbidden_terms):
    if model.get("asset_kind") != "cad_geometry" or model.get("simulation_state") != "not_defined":
        raise ValueError("Challenge input must be CAD geometry with no simulation definition")
    for key in ("physics", "materials", "studies", "parameters", "selections"):
        if model.get(key):
            raise ValueError(f"Input contains saved simulation information: {key}")
    serialized = json.dumps(model, ensure_ascii=False).casefold()
    for term in forbidden_terms:
        if term.casefold() in serialized:
            raise ValueError("Private case identity leaked into public geometry snapshot")
    return {"physics": 0, "materials": 0, "studies": 0, "parameters": 0,
            "asset_kind": model["asset_kind"], "simulation_state": model["simulation_state"],
            "format": model["format"], "fingerprint": model["fingerprint"]}


class AuditedBackend(CodexJSONBackend):
    """Use the real CLI and accept runs only when JSON events show no tools.

The event log records event/item types, not reasoning text or raw CLI logs.
Tools would invalidate a blind test even if the returned JSON looked correct.
"""
    def __init__(self, data_dir, *, forbidden_terms=()):
        super().__init__(data_dir)
        self.forbidden_terms = tuple(forbidden_terms)
        self.artifacts = Path(data_dir) / "stages"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.records = []
        self.active_stage = None
        # This remains the real backend. The wrapper adds inspection to run_text;
        # it neither injects model output nor replays a recorded plan.
        self.runner = self._run_audited

    def _run_audited(self, command, **kwargs):
        command = list(command)
        command.insert(command.index("exec") + 1, "--json")
        completed = run_text(command, **kwargs)
        event_types, item_types, forbidden = Counter(), Counter(), []
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            event_types[str(event.get("type"))] += 1
            item = event.get("item")
            if isinstance(item, dict):
                kind = str(item.get("type"))
                item_types[kind] += 1
                if kind not in {"reasoning", "agent_message", "todo_list"}:
                    forbidden.append(kind)
        record = {"stage": self.active_stage, "event_types": dict(event_types), "item_types": dict(item_types),
                  "tool_activity": sorted(set(forbidden)), "json_events_observed": bool(event_types),
                  "returncode": completed.returncode}
        self.records.append(record)
        write_json(self.artifacts / "execution-audit.json", self.records)
        if not event_types or forbidden:
            raise AgentBackendError("Blind-test CLI audit failed: missing events or additional tools used")
        return completed

    def generate(self, prompt, schema, *, stage, timeout=None):
        if any(term.casefold() in prompt.casefold() for term in self.forbidden_terms):
            raise AgentBackendError("Held-out identity or reference leaked into the model prompt")
        self.active_stage = stage
        write_json(self.artifacts / (stage + "-input.json"), {"prompt": prompt, "sha256": fingerprint(prompt), "schema": schema})
        print(json.dumps({"stage": stage, "status": "running", "prompt_characters": len(prompt)}, ensure_ascii=False), flush=True)
        started = time.monotonic()
        result = super().generate(prompt, schema, stage=stage, timeout=timeout)
        write_json(self.artifacts / (stage + "-output.json"), result)
        print(json.dumps({"stage": stage, "status": "completed", "seconds": round(time.monotonic()-started, 2)}, ensure_ascii=False), flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geometry", type=Path, help="Defaults to <output-root>/inputs/part_01.step")
    parser.add_argument("--scenario", type=Path, required=True, help="External challenge scenario JSON")
    parser.add_argument("--output-root", type=Path, default=DATA / "geometry-challenge",
                        help="Keep each question's input, runs and evaluation artifacts separate")
    parser.add_argument("--exclude", action="append", default=[], help="Held-out source path or case stem; repeat for additional sources")
    parser.add_argument("--library", action="append", help="Limit retrieval to this library ID; repeat for additional libraries")
    parser.add_argument("--retrieval-mode", choices=("qmd_keyword", "qmd_vector"), default="qmd_keyword")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--session-id", help="Repeat planning for the same public test input without user answers")
    args = parser.parse_args()
    if not args.exclude:
        parser.error("at least one --exclude held-out source path or case stem is required")
    challenge = args.output_root.resolve()
    challenge.mkdir(parents=True, exist_ok=True)
    scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
    goal = scenario["public_goal"]
    if not isinstance(goal, str) or any(term.casefold() in goal.casefold() for term in args.exclude):
        raise ValueError("Scenario public_goal is invalid or contains the private case identity")
    geometry = args.geometry or challenge / "inputs/part_01.step"
    model = inspect_model(geometry)
    clean = check_geometry_input(model, args.exclude)
    public_input = {"goal": goal, "model": model, "input_assertions": clean}
    write_json(challenge / "public-input.json", public_input)
    (challenge / "public-goal.txt").write_text(goal + "\n", encoding="utf-8")
    attempt_id = uuid.uuid4().hex[:16]
    run_dir = challenge / "runs" / attempt_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "public-input.json", public_input)
    base = LibraryKnowledgeProvider(KnowledgeBase(DATA), DATA / "libraries")
    from advisor.challenge_provider import ChallengeKnowledgeProvider
    provider = ChallengeKnowledgeProvider(base, excluded_paths=args.exclude, mode=args.retrieval_mode,
        library_ids=args.library)
    planner = AgentPlanner(run_dir, provider)
    backend = AuditedBackend(run_dir, forbidden_terms=args.exclude)
    planner.delegate.backend = backend
    sessions = SessionStore(DATA, provider, planner)
    if args.session_id:
        session = sessions.get(args.session_id)
        if session["model"]["fingerprint"] != model["fingerprint"] or session["answers"] or session["goal"] != "[纯几何测试] " + goal:
            raise ValueError("Challenge rerun requires the same geometry and no injected user answers")
    else:
        write_json(DATA / "models" / (model["id"] + ".json"), model)
        session = sessions.create("[纯几何测试] " + goal, model)
    report = {"schema": "geometry-planning-challenge/v1", "session_id": session["id"], "attempt_id": attempt_id,
              "run_directory": str(run_dir), "repeat_session": bool(args.session_id),
              "scenario_id": scenario.get("id", scenario.get("scenario_id")),
              "scenario_sha256": hashlib.sha256(args.scenario.read_bytes()).hexdigest(),
              "public_goal_sha256": fingerprint(goal),
              "input_assertions": clean, "public_input": str(challenge / "public-input.json"),
              "knowledge_policy": {"modern": args.retrieval_mode, "legacy_manuals": "disabled", "library_ids": args.library,
                                   "held_out_source_families": args.exclude},
              "physical_solution": "not_run", "user_answers_injected": False,
              "preview_url": f"http://127.0.0.1:49268/?session={session['id']}"}
    write_json(challenge / "run.json", report)
    print(json.dumps({"stage": "intake", "session": session["id"], "input": clean}, ensure_ascii=False), flush=True)
    if args.plan:
        started = time.monotonic()
        try:
            session = sessions.refine(session["id"], session["revision"])
            report.update(status="completed", seconds=round(time.monotonic()-started, 2),
                planning_incomplete=session.get("planning_incomplete"), steps=len(session["plan"]["steps"]),
                sources=len(session["sources"]), questions=len(session["questions"]), engine=session["plan"]["engine"])
            write_json(challenge / "session.json", session)
            write_json(run_dir / "session.json", session)
            (challenge / "plan.md").write_text(export_markdown(session), encoding="utf-8")
            (run_dir / "plan.md").write_text(export_markdown(session), encoding="utf-8")
        except Exception as exc:
            report.update(status="failed", seconds=round(time.monotonic()-started, 2), error=f"{type(exc).__name__}: {str(exc)}")
            raise
        finally:
            report["model_execution_audit"] = backend.records
            report["retrieval_policy_status"] = provider.status()
            audit = getattr(provider, "audit", getattr(provider, "trace", []))
            report["retrieval_audit"] = audit
            write_json(challenge / "retrieval-audit.json", audit)
            write_json(run_dir / "retrieval-audit.json", audit)
            write_json(run_dir / "run.json", report)
            write_json(challenge / "run.json", report)
            with (challenge / "attempts.jsonl").open("a", encoding="utf-8") as journal:
                journal.write(json.dumps({key: report.get(key) for key in ("attempt_id", "session_id", "status", "seconds", "error", "repeat_session")}, ensure_ascii=False) + "\n")
    # Keep the full retrieval/status audit on disk; it can contain hundreds of
    # records and index mappings that are unsuitable for a terminal summary.
    console_report = {key: report.get(key) for key in (
        "session_id", "attempt_id", "status", "seconds", "planning_incomplete",
        "steps", "sources", "questions", "engine", "error", "preview_url")
        if key in report}
    console_report["report_path"] = str(run_dir / "run.json")
    print(json.dumps(console_report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
