import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from advisor.agent_backend import AgentBackendError
from scripts import benchmark_codex_cli as rag
from scripts import benchmark_no_library_cli as cli


def make_case(root):
    public, private = cli.core.case_paths(root, "case-01")
    (public / "inputs").mkdir(parents=True)
    private.mkdir(parents=True)
    (public / "problem.md").write_text("Anonymous public task. " * 30, encoding="utf-8")
    (public / "inputs/observations.csv").write_text("time,value\n0,1\n1,2\n", encoding="utf-8")
    cli.core.dump_json(public / "inputs/topology.json", {"domains": [1, 2], "adjacent": [[1, 2]]})
    asset = public / "inputs/geometry_only.mph"
    asset.write_bytes(b"geometry-only-fixture")
    cli.core.dump_json(public / "inputs/geometry.json", {
        "preparation_status": "ready", "status": "geometry_only", "mesh_run": False,
        "solver_run": False, "materials_defined": False, "physics_defined": False,
        "geometries": [{"dimension": 2}], "assets": {asset.name: {"sha256": cli.core.sha256(asset)}}})
    cli.core.dump_json(private / "reference.json", {
        "case_id": "case-01", "sources": [{"id": "private-source"}], "secret": "PRIVATE_REFERENCE_CANARY",
        "assessment": {"schema": "plan-checklist/v1", "checks": [
            {"id": name, "category": name, "question": "Does the plan address this?",
             "applicability": {"type": "always"}, "pass_condition": "Explicit implementation",
             "fail_condition": "Missing implementation", "severity": "major",
             "evidence": [{"source_id": "private-source", "locator": "section"}]}
            for name in cli.core.CATEGORIES]}})


def args_for(destination):
    return SimpleNamespace(case="case-01", destination=destination,
                           model="gpt-5.6-luna", effort="medium", timeout=600)


def events(item="agent_message"):
    return "\n".join(json.dumps(event) for event in [
        {"type": "thread.started"}, {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "THOUGHT_CHAIN_CANARY"}},
        {"type": "item.completed", "item": {"type": item, "text": "RAW_CLI_CANARY"}},
        {"type": "turn.completed"}])


def test_one_generation_has_identical_public_question_and_zero_library_access(tmp_path, monkeypatch):
    root, destination = tmp_path / "benchmark", tmp_path / "no-library"
    make_case(root)
    expected_question = rag.public_question(root, "case-01", tmp_path / "rag-public-inputs")
    calls = []

    def forbidden_access(*args, **kwargs):
        pytest.fail("The no-library runner must never request a knowledge gate or workbench")

    monkeypatch.setattr(rag.benchmark, "KnowledgeGate", forbidden_access)
    monkeypatch.setattr(rag.benchmark, "request_json", forbidden_access)
    monkeypatch.setattr(rag.benchmark, "require_isolation", forbidden_access)

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        running = cli.core.load_json(destination / "run.json")
        assert running["status"] == "running" and running["knowledge_call_count"] == 0
        assert command[command.index("--model") + 1] == "gpt-5.6-luna"
        assert command[command.index("-c") + 1] == 'model_reasoning_effort="medium"'
        assert kwargs["timeout"] == 600
        assert "--ignore-user-config" in command and "--ephemeral" in command
        assert kwargs["cwd"] != destination and not list(kwargs["cwd"].glob("*.mph"))
        schema = cli.core.load_json(command[command.index("--output-schema") + 1])
        assert schema == cli.PLAN_SCHEMA
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["plan_markdown"]
        envelope = json.loads(kwargs["input_text"])
        assert set(envelope) == {"instructions", "question"}
        assert envelope["question"] == expected_question
        Path(command[command.index("--output-last-message") + 1]).write_text(
            json.dumps({"plan_markdown": "完整中文实施方案；列出待确认材料参数、边界、网格、求解与验证。"}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, events(), "STDERR_CANARY")

    result = cli.plan(root, args_for(destination), command="mock-codex", runner=runner)
    assert len(calls) == result["model_call_count"] == 1
    assert result["status"] == "complete" and result["model_execution_audit"][0]["accepted"] is True
    assert cli.core.load_json(destination / "run.json") == result
    assert result["plan_sha256"] == cli.core.sha256(destination / "plan.md")
    assert result["model_input_sha256"] == cli.core.sha256(destination / "model-input.json")
    assert result["prepared_question_sha256"] == cli.core.sha256(destination / "question.json")
    assert result["input_packet_sha256"] == cli.text_sha256(json.dumps(expected_question, ensure_ascii=False))
    assert result["instructions_sha256"] == cli.text_sha256(cli.INSTRUCTIONS)
    assert result["common_planning_policy_sha256"] == cli.text_sha256(cli.COMMON_PLANNING_POLICY)
    assert result["planning_policy_version"] == cli.PLANNING_POLICY_VERSION
    assert result["knowledge_library_enabled"] is False
    assert result["library_ids"] == result["knowledge_calls"] == result["read_source_ids"] == []
    assert result["knowledge_call_count"] == result["retry_count"] == 0
    assert all(result[key] is False for key in ("private_reference_supplied", "solver_run",
               "scoring_run", "thought_chain_stored", "raw_cli_logs_stored", "physical_feedback_supplied"))
    assert result["isolation_policy"]["workbench_requested"] is False
    assert result["seconds"] >= 0 and result["model_execution_audit"][0]["seconds"] >= 0
    prompt = calls[0][1]["input_text"]
    saved = "\n".join(path.read_text(encoding="utf-8") for path in destination.rglob("*")
                      if path.is_file() and path.suffix != ".mph")
    for withheld in ("PRIVATE_REFERENCE_CANARY", "private-source", "THOUGHT_CHAIN_CANARY",
                     "RAW_CLI_CANARY", "STDERR_CANARY", str(root), str(destination)):
        assert withheld not in prompt
    for withheld in ("PRIVATE_REFERENCE_CANARY", "private-source", "THOUGHT_CHAIN_CANARY",
                     "RAW_CLI_CANARY", "STDERR_CANARY"):
        assert withheld not in saved


@pytest.mark.parametrize("item", ["command_execution", "web_search", "mcp_tool_call"])
def test_native_tool_activity_fails_closed_without_retry_or_plan(tmp_path, item):
    root, destination = tmp_path / "benchmark", tmp_path / "run"
    make_case(root)
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        Path(command[command.index("--output-last-message") + 1]).write_text(
            json.dumps({"plan_markdown": "Otherwise valid plan"}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, events(item), "")

    with pytest.raises(AgentBackendError, match="audit"):
        cli.plan(root, args_for(destination), command="mock-codex", runner=runner)
    report = cli.core.load_json(destination / "run.json")
    assert len(calls) == report["model_call_count"] == 1
    assert report["status"] == "incomplete" and report["failure_code"] == "native_tool_activity"
    assert report["model_execution_audit"][0]["tool_activity"] == [item]
    assert report["retry_count"] == report["knowledge_call_count"] == 0
    assert not (destination / "plan.md").exists()


@pytest.mark.parametrize("output", [{"plan_markdown": "  "}, {"plan_markdown": "plan", "tool_requests": []}])
def test_invalid_final_has_no_repair_or_feedback(tmp_path, output):
    root, destination = tmp_path / "benchmark", tmp_path / "run"
    make_case(root)
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(output), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, events(), "")

    with pytest.raises(cli.cli.PlanningValidationError, match="single nonempty"):
        cli.plan(root, args_for(destination), command="mock-codex", runner=runner)
    report = cli.core.load_json(destination / "run.json")
    assert len(calls) == 1 and report["status"] == "incomplete"
    assert report["failure_code"] == "invalid_final_plan"
    assert report["retry_count"] == 0 and report["physical_feedback_supplied"] is False
    assert not (destination / "plan.md").exists()


@pytest.mark.parametrize("tree", ["public", "private"])
def test_run_cannot_be_placed_under_benchmark_source_or_private_tree(tmp_path, tree):
    root = tmp_path / "benchmark"
    make_case(root)

    def forbidden_model_call(*args, **kwargs):
        pytest.fail("Invalid output paths must fail before model execution")

    with pytest.raises(ValueError, match="separate"):
        cli.plan(root, args_for(root / tree / "new-run"), command="mock-codex", runner=forbidden_model_call)


def test_cli_defaults_are_the_exact_ablation_configuration():
    args = cli.parser().parse_args(["--case", "case-01", "--destination", "unused"])
    assert (args.model, args.effort, args.timeout) == ("gpt-5.6-luna", "medium", 600)
    assert not hasattr(args, "workbench_url") and not hasattr(args, "max_rounds")
