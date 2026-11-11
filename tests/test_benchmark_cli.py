import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest


APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("benchmark_cli", APP / "scripts/benchmark_cli.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
core = cli.core


def case(root, case_id="case-01"):
    public, private = core.case_paths(root, case_id)
    (public / "inputs").mkdir(parents=True, exist_ok=True)
    private.mkdir(parents=True, exist_ok=True)
    (public / "problem.md").write_text("Anonymous task input. " * 30, encoding="utf-8")
    asset = public / "inputs/geometry_only.mph"
    asset.write_bytes(b"anonymous-geometry")
    core.dump_json(public / "inputs/geometry.json", {
        "preparation_status": "ready", "status": "geometry_only", "mesh_run": False,
        "solver_run": False, "materials_defined": False, "physics_defined": False,
        "geometries": [{"dimension": 2}], "assets": {asset.name: {"sha256": core.sha256(asset)}}})
    reference = {"case_id": case_id, "sources": [{"id": "source"}],
        "implementation": {"secret": "PRIVATE_IMPLEMENTATION_CANARY"},
        "assessment": {"schema": "plan-checklist/v1", "checks": [
            {"id": name, "category": name, "question": "Does the plan address this requirement?",
             "applicability": {"type": "always"}, "pass_condition": "Explicit statement", "fail_condition": "Missing",
             "severity": "critical", "evidence": [{"source_id": "source", "locator": "chapter"}]}
            for name in core.CATEGORIES]}}
    core.dump_json(private / "reference.json", reference)
    return reference


PLAN = "Plan specifies concrete implementation."


def judgement(reference, review=False):
    results = [{"id": key, "verdict": "pass", "reason": "Supported", "evidence_status": "quoted",
                "plan_quotes": [PLAN], "applicability_reason": "Always applies"} for key in core.checks(reference)]
    if review:
        results[0].update(verdict="uncertain", evidence_status="ambiguous", plan_quotes=[])
    return {"case_id": reference["case_id"], "check_results": results,
            "task_defects": ["Missing required data"] if review else [], "summary": "Evidence-based assessment"}


def test_validate_requires_complete_eighteen_case_set(tmp_path, capsys):
    case(tmp_path)
    assert cli.main(["--root", str(tmp_path), "validate"]) == 2
    assert "missing=" in capsys.readouterr().err
    for i in range(2, 19):
        case(tmp_path, f"case-{i:02}")
    result = cli.validate(tmp_path)
    assert result["ready"] == 18
    assert result["status"] == "ready"
    (tmp_path / "public/cases/case-18/inputs/geometry_only.mph").write_bytes(b"changed")
    result = cli.validate(tmp_path)
    assert result["ready"] == 17
    assert result["errors"][0]["case_id"] == "case-18"


def test_prepare_and_judge_have_separate_visibility(tmp_path):
    root = tmp_path / "benchmark"
    case(root)
    run = tmp_path / "weak-run"
    cli.prepare(root, "case-01", run)
    content = "".join(p.read_text(encoding="utf-8") for p in run.rglob("*") if p.is_file())
    assert "PRIVATE_IMPLEMENTATION_CANARY" not in content
    assert not (run / "reference.json").exists()
    assert (run / "question.json").is_file()
    with pytest.raises(ValueError, match="private directory"):
        cli.private_output(root, run / "judge.json")
    with pytest.raises(ValueError, match="separate"):
        cli.prepare(root, "case-01", root / "private/weak-run")
    with pytest.raises(ValueError, match="empty"):
        cli.prepare(root, "case-01", run)


def test_assessment_summary_recomputes_verdict_counts(tmp_path, capsys):
    reference = case(tmp_path)
    value = judgement(reference)
    value['check_results'][0].update(verdict='fail', evidence_status='missing', plan_quotes=[])
    inp, plan = tmp_path / 'judge.json', tmp_path / 'plan.md'
    plan.write_text(PLAN)
    core.dump_json(inp, value)
    assert cli.main(['--root', str(tmp_path), 'assess', '--case', 'case-01', '--judgement', str(inp), '--plan', str(plan)]) == 0
    capsys.readouterr()
    output = tmp_path / 'private/evaluations/case-01/assessment.json'
    result = core.load_json(output)
    assert result['counts']['fail'] == 1 and result['conclusion'] == 'critical_failure'
    result['counts']['pass'] = 1000
    core.dump_json(output, result)
    summary = cli.summarize(tmp_path, [output])
    assert summary['check_counts']['pass'] == 5 and summary['check_counts']['fail'] == 1
    assert 'mean_score' not in summary
    with pytest.raises(ValueError): cli.summarize(tmp_path, [output, output])


def test_uncertainty_is_kept_separate_from_failure(tmp_path):
    reference = case(tmp_path)
    report = core.assess_judgement(reference, judgement(reference, review=True), PLAN)
    output = tmp_path / 'private/evaluations/case-01/assessment.json'
    core.dump_json(output, report)
    summary = cli.summarize(tmp_path, [output])
    assert summary['review_cases'] == ['case-01']
    assert summary['check_counts']['uncertain'] == 1 and summary['check_counts']['fail'] == 0


def test_bundle_contains_only_validated_public_files(tmp_path, capsys):
    for i in range(1, 19):
        case(tmp_path, f"case-{i:02}")
    archive = tmp_path / "question-set.zip"
    assert cli.main(["--root", str(tmp_path), "bundle", "--output", str(archive)]) == 0
    capsys.readouterr()
    with zipfile.ZipFile(archive) as z:
        assert len([p for p in z.namelist() if p.endswith("problem.md")]) == 18
        assert not any("private" in p or p.endswith("reference.json") for p in z.namelist())
        assert all(b"PRIVATE_IMPLEMENTATION_CANARY" not in z.read(p) for p in z.namelist())
    assert cli.main(["--root", str(tmp_path), "bundle", "--output", str(tmp_path / "public/unsafe.zip")]) == 2


def test_knowledge_gate_blocks_scope_mutation_and_rechecks_isolation(monkeypatch):
    enabled = {"value": False}
    calls = []

    def request(url, payload=None, **kwargs):
        if url.endswith("evaluation-sets"):
            return {"retrieval_enabled": enabled["value"], "access_token": "MANAGEMENT_TOKEN", "title": "WITHHELD_TITLE"}
        calls.append(payload)
        if payload["name"] == "get_library_scope":
            return {"result": {"libraries": [{"id": "manual", "name": "Manual", "enabled": True},
                                              {"id": "disabled", "enabled": False}]}}
        if payload["name"] == "search_knowledge":
            return {"result": {"results": [{"id": "evidence-1", "library_id": "manual"}]}}
        return {"result": {"id": "evidence-1", "library_id": "manual", "text": "Source page"}}

    monkeypatch.setattr(cli, "request_json", request)
    gate = cli.KnowledgeGate("http://workbench")
    scope = gate.call("get_library_scope", {})
    assert "MANAGEMENT_TOKEN" not in json.dumps(scope)
    assert "WITHHELD_TITLE" not in json.dumps(scope)
    for name, args in [("set_libraries_enabled", {}), ("search_knowledge", {"query": "x", "mode": "qmd_vector"}),
                       ("search_knowledge", {"query": "x", "library_ids": ["disabled"]}),
                       ("read_source", {"id": "unseen"})]:
        with pytest.raises(ValueError):
            gate.call(name, args)
    gate.call("search_knowledge", {"query": "Darcy conductivity", "mode": "native"})
    gate.call("read_source", {"id": "evidence-1"})
    assert gate.trace[-1]["evidence_ids"] == ["evidence-1"]
    enabled["value"] = True
    before = len(calls)
    with pytest.raises(ValueError, match="disabled"):
        gate.call("search_knowledge", {"query": "must not run"})
    assert len(calls) == before


def test_plan_is_explicit_mocked_and_never_sends_private_data(tmp_path, monkeypatch):
    root = tmp_path / "benchmark"
    case(root)
    request_log = []
    rounds = {"value": 0}

    def request(url, payload=None, **kwargs):
        if url.endswith("evaluation-sets"):
            return {"retrieval_enabled": False, "access_token": "MANAGEMENT_TOKEN", "title": "WITHHELD_TITLE"}
        if url.endswith("/api/tools/call"):
            if payload["name"] == "get_library_scope":
                return {"result": {"libraries": [{"id": "manual", "name": "Manual", "enabled": True}]}}
            return {"result": {"results": [{"id": "source-1", "library_id": "manual"}]}}
        request_log.append(json.loads(json.dumps(payload)))
        assert kwargs["key"] == "ENVIRONMENT_ONLY_KEY"
        rounds["value"] += 1
        if rounds["value"] == 1:
            return {"model": "actual-model-version", "choices": [{"finish_reason": "tool_calls", "message": {
                "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "search_knowledge", "arguments": json.dumps({"query": "mesh convergence", "kind": "manual", "mode": "native"})}}]}}]}
        return {"model": "actual-model-version", "choices": [{"finish_reason": "stop", "message": {"content": "Final concrete plan."}}]}

    monkeypatch.setattr(cli, "request_json", request)
    monkeypatch.setenv("SIM_BENCHMARK_API_KEY", "ENVIRONMENT_ONLY_KEY")
    args = SimpleNamespace(case="case-01", destination=tmp_path / "run", workbench_url="http://workbench",
                           llm_base_url="https://provider/v1", model="explicit-model", max_rounds=3,
                           max_output_tokens=1000, timeout=10)
    result = cli.plan(root, args)
    assert result["status"] == "complete"
    assert result["reported_models"] == ["actual-model-version", "actual-model-version"]
    assert result["knowledge_calls"][0]["arguments"]["library_ids"] == ["manual"]
    sent = json.dumps(request_log)
    assert all(canary not in sent for canary in ["PRIVATE_IMPLEMENTATION_CANARY", "MANAGEMENT_TOKEN", "WITHHELD_TITLE", "ENVIRONMENT_ONLY_KEY"])
    assert {t["function"]["name"] for t in request_log[0]["tools"]} == cli.ALLOWED_TOOLS
    saved = (args.destination / "run.json").read_text(encoding="utf-8")
    assert "ENVIRONMENT_ONLY_KEY" not in saved
    assert result["thought_chain_stored"] is False
