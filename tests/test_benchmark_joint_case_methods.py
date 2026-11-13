import json

from scripts import benchmark_joint_case_methods as pilot


def prepare(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot.benchmark_route_selection.batch, "ROOT", tmp_path / "benchmark")
    for key in ("SIM_ADVISOR_NODE", "SIM_ADVISOR_QMD_DIR", "SIM_ADVISOR_SEARCH_CACHE", "PYTHONIOENCODING",
                "QMD_EMBED_MODEL", "SIM_ADVISOR_EMBED_MODEL"):
        monkeypatch.setenv(key, "synthetic-original")
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({"schema": "case-method-candidates/v1", "case_ids": ["synthetic-case"]}), encoding="utf-8")
    output = tmp_path / "output"
    return ["--candidate-list", str(candidates), "--output", str(output), "--run-id", "synthetic-run"], output


def test_no_model_comparison_after_joint_audit_failure(tmp_path, monkeypatch):
    args, output = prepare(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(pilot.build_case_methods, "main", lambda args: calls.append(("build", args)) or 0)
    monkeypatch.setattr(pilot.audit_case_methods, "main", lambda args: calls.append(("audit", args)) or 1)
    monkeypatch.setattr(pilot.benchmark_route_selection, "main", lambda args: calls.append(("compare", args)) or 0)
    assert pilot.main(args) == 1
    assert [name for name, _ in calls] == ["build", "audit"]
    assert "--joint-sources" in calls[0][1]
    assert "--require-joint" in calls[1][1]
    assert json.loads((output / "pipeline-state.json").read_text(encoding="utf-8"))["stage"] == "joint_source_audit_failed"


def test_successful_pipeline_keeps_two_agent_limit_and_explicit_luna(tmp_path, monkeypatch):
    args, output = prepare(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(pilot.build_case_methods, "main", lambda args: calls.append(("build", args)) or 0)
    monkeypatch.setattr(pilot.audit_case_methods, "main", lambda args: calls.append(("audit", args)) or 0)
    monkeypatch.setattr(pilot.benchmark_route_selection, "main", lambda args: calls.append(("compare", args)) or 0)
    assert pilot.main(args) == 0
    assert [name for name, _ in calls] == ["build", "audit", "compare"]
    for _, arguments in (calls[0], calls[2]):
        assert arguments[arguments.index("--parallel") + 1] == "2"
        assert arguments[arguments.index("--model") + 1] == "gpt-6-luna"
    freeze = json.loads((output / "freeze.json").read_text(encoding="utf-8"))
    assert freeze["joint_sources"] and not freeze["private_grading_visible"]
    assert not freeze["predecessor_results_reused"]
