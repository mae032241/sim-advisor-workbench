"""Build tutorial/configuration method notes, audit them, then run the Luna pilot.

Uses explicit non-test candidate IDs and the existing three-task Agent CLI
experiment. No private grading reference is opened by this process. Embeddings,
index rebuilding, answer repair and automatic retries are not performed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import traceback

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from scripts import audit_case_methods, benchmark_route_selection, build_case_methods


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--candidate-list", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scope-library-id", action="append", default=[])
    parser.add_argument("--library-root", type=Path, default=build_case_methods.DEFAULT_LIBRARY_ROOT)
    parser.add_argument("--offline-root", type=Path, default=benchmark_route_selection.batch.OFFLINE)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--parallel", type=int, choices=(1, 2), default=2)
    parser.add_argument("--timeout", type=float, default=7200)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", args.run_id) or args.timeout <= 0:
        raise ValueError("A simple new run ID and positive timeout are required")
    benchmark_root = benchmark_route_selection.batch.ROOT
    for directory in (benchmark_root / "private/evaluations" / args.run_id, benchmark_root / "runs" / args.run_id):
        if directory.exists() and any(directory.iterdir()):
            raise FileExistsError("Historical route attempts are never overwritten")
    candidates, candidate_hash = benchmark_route_selection.load_candidates(args.candidate_list)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.update(SIM_ADVISOR_NODE=str(args.offline_root / "node/node.exe"),
                      SIM_ADVISOR_QMD_DIR=str(args.offline_root / "qmd/node_modules/@tobilu/qmd"),
                      SIM_ADVISOR_SEARCH_CACHE=str(args.output / "search-cache"), PYTHONIOENCODING="utf-8")
    os.environ.pop("QMD_EMBED_MODEL", None)
    os.environ.pop("SIM_ADVISOR_EMBED_MODEL", None)
    state_path = args.output / "pipeline-state.json"
    method_root = args.output / "method-build"

    def update(stage, **values):
        value = {"schema": "joint-case-method-pilot/v1", "stage": stage,
                 "updated_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
                 "model": args.model, "effort": "high", "parallel_limit": args.parallel,
                 "run_id": args.run_id, "method_root": str(method_root), **values}
        build_case_methods.write_json(state_path, value)
        print(json.dumps({"stage": stage, **values}, ensure_ascii=False), flush=True)

    sources = ["advisor/case_methods.py", "scripts/build_case_methods.py",
               "scripts/audit_case_methods.py", "scripts/benchmark_route_selection.py",
               "scripts/benchmark_joint_case_methods.py"]
    hashes = {name: hashlib.sha256((APP / name).read_bytes()).hexdigest() for name in sources}
    freeze = {"schema": "joint-case-method-pilot-freeze/v1", "source_sha256": hashes,
              "candidate_list_sha256": candidate_hash, "candidate_ids": candidates["case_ids"],
              "model": args.model, "effort": "high", "joint_sources": True,
              "scope_library_ids": args.scope_library_id,
              "maximum_active_model_processes": args.parallel,
              "test_tasks_visible_to_constructor": False, "private_grading_visible": False,
              "predecessor_results_reused": False, "automatic_retry_count": 0,
              "construction_inspiration": "MooseAgent: explain native input configuration using associated documentation; independently implemented for COMSOL."}
    build_case_methods.write_json(args.output / "freeze.json", freeze, exclusive=True)
    try:
        build_args = ["--output", str(method_root), "--library-root", str(args.library_root),
                      "--model", args.model, "--effort", "high", "--parallel", str(args.parallel),
                      "--timeout", str(args.timeout), "--joint-sources"]
        for identity in candidates["case_ids"]:
            build_args.extend(["--case", identity])
        for identity in args.scope_library_id:
            build_args.extend(["--scope-library-id", identity])
        update("building_joint_methods", expected_profiles=len(candidates["case_ids"]),
               stop_file=str(method_root / "stop.request"))
        code = build_case_methods.main(build_args)
        if code:
            update("method_build_incomplete", returncode=code, comparison_started=False)
            return code
        update("auditing_joint_sources")
        code = audit_case_methods.main(["--method-root", str(method_root), "--output",
                                       str(args.output / "audit"), "--require-joint"])
        if code:
            update("joint_source_audit_failed", comparison_started=False)
            return code
        if (method_root / "stop.request").exists():
            update("stopped_before_comparison")
            return 2
        if any(hashlib.sha256((APP / name).read_bytes()).hexdigest() != value for name, value in hashes.items()):
            raise RuntimeError("Frozen pilot source changed during construction")
        run_args = ["--run-id", args.run_id, "--method-root", str(method_root),
                    "--candidate-list", str(args.candidate_list.resolve()), "--model", args.model,
                    "--parallel", str(args.parallel), "--timeout", str(args.timeout), "--poll-interval", "2",
                    "--offline-root", str(args.offline_root)]
        for identity in args.scope_library_id:
            run_args.extend(["--scope-library-id", identity])
        update("running_route_comparison", expected_runs=6,
               stop_file=str(APP / "benchmarks/comsol-plan-v3/private/evaluations" / args.run_id / "stop.request"))
        code = benchmark_route_selection.main(run_args)
        update("inference_complete" if code == 0 else "comparison_incomplete", returncode=code,
               grading="Separate offline evaluation against frozen questions; not fed back to any Agent.")
        return code
    except BaseException as exc:
        update("pipeline_error", error_type=type(exc).__name__, detail=str(exc), traceback=traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
