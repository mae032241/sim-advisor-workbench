import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from advisor.agent_backend import AgentBackendError
from scripts import benchmark_codex_cli as cli


def make_case(root):
    public, private = cli.core.case_paths(root, "case-01")
    (public / "inputs").mkdir(parents=True)
    private.mkdir(parents=True)
    (public / "problem.md").write_text("Anonymous public task. " * 30, encoding="utf-8")
    (public / "inputs/observations.csv").write_text("time,value\n0,1\n1,2\n", encoding="utf-8")
    asset = public / "inputs/geometry_only.mph"
    asset.write_bytes(b"geometry-only-fixture")
    cli.core.dump_json(public / "inputs/geometry.json", {
        "preparation_status": "ready", "status": "geometry_only", "mesh_run": False,
        "solver_run": False, "materials_defined": False, "physics_defined": False,
        "geometries": [{"dimension": 2}], "assets": {asset.name: {"sha256": cli.core.sha256(asset)}}})
    cli.core.dump_json(private / "reference.json", {
        "case_id": "case-01", "sources": [{"id": "source"}], "secret": "PRIVATE_REFERENCE_CANARY",
        "assessment": {"schema": "plan-checklist/v1", "checks": [
            {"id": name, "category": name, "question": "Does the plan address this?",
             "applicability": {"type": "always"}, "pass_condition": "Explicit implementation",
             "fail_condition": "Missing implementation", "severity": "major",
             "evidence": [{"source_id": "source", "locator": "section"}]}
            for name in cli.core.CATEGORIES]}})


def tool_action(name, **arguments):
    return {"action": "tools", "tool_requests": [{"name": name,
            "arguments": {key: arguments.get(key) for key in cli.ARGUMENT_PROPERTIES}}],
            "plan_markdown": None, "cited_source_ids": None}


def final_action(source="evidence-1"):
    return {"action": "final", "tool_requests": None,
            "plan_markdown": "Concrete implementation and verification plan. [" + source + "]",
            "cited_source_ids": [source]}


def event_log(item="agent_message"):
    events = [{"type": "thread.started", "thread_id": "transient-id"}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"type": "reasoning", "text": "THOUGHT_CHAIN_CANARY"}},
              {"type": "item.completed", "item": {"type": item, "text": "RAW_CLI_CANARY"}},
              {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]
    return "\n".join(json.dumps(value) for value in events)


def test_backend_preserves_exact_model_and_effort_and_discards_raw_logs(tmp_path):
    invocations = []

    def runner(command, **kwargs):
        invocations.append((command, kwargs))
        assert "--ignore-user-config" in command and "--ephemeral" in command and "--json" in command
        assert command[command.index("--model") + 1] == "gpt-5.6-luna"
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert command[command.index("-c") + 1] == 'model_reasoning_effort="medium"'
        assert kwargs["timeout"] == 600
        assert kwargs["cwd"] != tmp_path
        schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text(encoding="utf-8"))
        assert schema == cli.ACTION_SCHEMA
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(final_action()), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, event_log(), "STDERR_CANARY")

    backend = cli.CodexActionBackend(command="mock-codex", runner=runner)
    assert backend.generate("public prompt", 1) == final_action()
    assert backend.model == "gpt-5.6-luna" and backend.effort == "medium"
    assert backend.records[0]["accepted"] is True
    assert backend.records[0]["item_types"] == {"reasoning": 1, "agent_message": 1}
    assert all(value not in json.dumps(backend.records) for value in
               ("THOUGHT_CHAIN_CANARY", "RAW_CLI_CANARY", "STDERR_CANARY", "transient-id"))
    assert not invocations[0][1]["cwd"].exists()


@pytest.mark.parametrize("item", ["command_execution", "mcp_tool_call", "web_search", "file_change", "collab_tool_call"])
def test_audit_rejects_native_tools_even_with_a_valid_final_result(tmp_path, item):
    def runner(command, **kwargs):
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(final_action()), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, event_log(item), "")

    backend = cli.CodexActionBackend(command="mock-codex", runner=runner)
    with pytest.raises(AgentBackendError, match="audit"):
        backend.generate("public prompt", 1)
    assert backend.records[0]["tool_activity"] == [item]
    assert backend.records[0]["accepted"] is False
    assert "RAW_CLI_CANARY" not in json.dumps(backend.records)


@pytest.mark.parametrize("stderr,category", [
    ("Authentication failed: SECRET_CANARY", "authentication"),
    ("The model gpt-5.6-luna is not available: SECRET_CANARY", "model_unavailable"),
    ("Unexpected runtime problem: SECRET_CANARY", "runtime_failure")])
def test_failure_categories_do_not_store_stderr(stderr, category):
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", stderr)

    backend = cli.CodexActionBackend(command="mock-codex", runner=runner)
    with pytest.raises(AgentBackendError):
        backend.generate("public prompt", 1)
    assert backend.records[0]["failure_type"] == category
    assert "SECRET_CANARY" not in json.dumps(backend.records)


def test_strict_schema_and_inactive_tool_arguments():
    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["properties"]) == set(value["required"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(cli.ACTION_SCHEMA)
    assert cli.validate_action(tool_action("search_knowledge", query="conductivity", kind="manual", mode="native")) == [
        {"name": "search_knowledge", "arguments": {"query": "conductivity", "kind": "manual", "mode": "native"}}]
    with pytest.raises(ValueError, match="Inactive"):
        cli.validate_action(tool_action("read_source", id="source", query="unrelated"))
    too_many = tool_action("get_library_scope")
    too_many["tool_requests"] *= 7
    with pytest.raises(ValueError, match="1 to 6"):
        cli.validate_action(too_many)


def test_real_gate_action_loop_sends_only_public_inputs_and_saves_bounded_audit(tmp_path, monkeypatch):
    root, destination = tmp_path / "benchmark", tmp_path / "run"
    make_case(root)
    isolated = {"enabled": False}
    requests, prompts, checkpoints = [], [], []

    def request(url, payload=None, **kwargs):
        if url.endswith("evaluation-sets"):
            return {"retrieval_enabled": isolated["enabled"], "access_token": "MANAGEMENT_SECRET_CANARY",
                    "title": "WITHHELD_TITLE_CANARY"}
        requests.append(payload)
        if payload["name"] == "get_library_scope":
            return {"result": {"libraries": [{"id": "manual", "name": "Manual", "enabled": True},
                                              {"id": "private-set", "name": "DISABLED_LIBRARY_CANARY", "enabled": False}]}}
        if payload["name"] == "search_knowledge":
            return {"result": {"results": [{"id": "evidence-1", "library_id": "manual", "kind": "case",
                "source_context": {"role": "saved_configuration", "case_identity": "example-fixture",
                                   "applicability": "not_verified"}, "excerpt": "source excerpt"}]}}
        return {"result": {"id": "evidence-1", "library_id": "manual", "text": "Real fixture knowledge page",
                           "offset": 0, "next_offset": None, "total_characters": 27}}

    actions = [tool_action("search_knowledge", query="mesh verification", kind="manual", mode="native"),
               tool_action("read_source", id="evidence-1", offset=0, length=12000), final_action()]

    def runner(command, **kwargs):
        prompts.append(kwargs["input_text"])
        checkpoints.append(cli.core.load_json(destination / "run.json"))
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(actions[len(prompts) - 1]), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, event_log(), "")

    monkeypatch.setattr(cli.benchmark, "request_json", request)
    args = SimpleNamespace(case="case-01", destination=destination, workbench_url="http://workbench",
                           model="gpt-5.6-luna", effort="medium", timeout=600, max_rounds=16)
    result = cli.plan(root, args, runner=runner, command="mock-codex")
    assert result["status"] == "complete" and result["model_call_count"] == 3
    assert result["knowledge_call_counts"] == {"search_knowledge": 1, "read_source": 1}
    assert checkpoints[1]["status"] == "running" and checkpoints[1]["knowledge_call_count"] == 1
    assert checkpoints[2]["read_source_ids"] == ["evidence-1"]
    assert cli.core.load_json(destination / "run.json") == result
    assert result["plan_sha256"] == cli.core.sha256(destination / "plan.md")
    assert result["evaluation_set_retrieval_enabled"] is False
    assert result["solver_run"] is False and result["thought_chain_stored"] is False
    assert result["knowledge_calls"][0]["source_kind_counts"] == {"case": 1}
    read_summary = result["knowledge_calls"][1]["source_summaries"][0]
    assert read_summary["kind"] == "case"
    assert read_summary["source_context"]["role"] == "saved_configuration"
    assert read_summary["source_context"]["case_identity"] == "example-fixture"
    assert read_summary["content_excerpt"] == "Real fixture knowledge page"
    assert result["knowledge_calls"][1]["read_range"]["offset"] == 0
    assert result["instructions_sha256"] == cli.text_sha256(cli.INSTRUCTIONS)
    assert result["model_input_sha256"] == cli.core.sha256(destination / "model-input.json")
    assert all(len(record["prompt_sha256"]) == 64 for record in result["model_execution_audit"])
    assert json.loads(prompts[0])["question"]["input_data"]["inputs/observations.csv"] == "time,value\n0,1\n1,2\n"
    assert next(request for request in requests if request["name"] == "read_source")["arguments"]["id"] == "evidence-1"
    sent = "\n".join(prompts)
    saved = "\n".join(path.read_text(encoding="utf-8") for path in destination.rglob("*")
                      if path.is_file() and path.suffix != ".mph")
    for canary in ("PRIVATE_REFERENCE_CANARY", "MANAGEMENT_SECRET_CANARY", "WITHHELD_TITLE_CANARY",
                   "DISABLED_LIBRARY_CANARY", "THOUGHT_CHAIN_CANARY", "RAW_CLI_CANARY"):
        assert canary not in sent and canary not in saved
    assert "Real fixture knowledge page" in prompts[-1]


def test_final_without_real_read_is_rejected_and_isolation_changes_stop_calls(tmp_path, monkeypatch):
    root, destination = tmp_path / "benchmark", tmp_path / "run"
    make_case(root)
    isolated = {"enabled": False}

    def request(url, payload=None, **kwargs):
        if url.endswith("evaluation-sets"):
            return {"retrieval_enabled": isolated["enabled"]}
        if payload["name"] == "get_library_scope":
            return {"result": {"libraries": [{"id": "manual", "enabled": True}]}}
        return {"result": {"results": [{"id": "evidence-1", "library_id": "manual"}]}}

    outputs = [tool_action("search_knowledge", query="mesh", kind="manual", mode="native"), final_action(), final_action()]
    calls = []

    def runner(command, **kwargs):
        action = outputs[len(calls)]
        calls.append(command)
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(action), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, event_log(), "")

    monkeypatch.setattr(cli.benchmark, "request_json", request)
    args = SimpleNamespace(case="case-01", destination=destination, workbench_url="http://workbench",
                           model="gpt-5.6-luna", effort="medium", timeout=600, max_rounds=16)
    with pytest.raises(ValueError, match="actual read_source"):
        cli.plan(root, args, runner=runner, command="mock-codex")
    assert not (destination / "plan.md").exists()
    rejected_report = cli.core.load_json(destination / "run.json")
    assert rejected_report["status"] == "incomplete"
    assert rejected_report["failure_code"] == "final_citations_unread"
    assert rejected_report["citation_repair_count"] == 1
    assert rejected_report["rejected_candidates"][0]["failure_code"] == "final_citations_unread"
    assert (destination / "rejected-plan-02.md").is_file()
    args.destination = tmp_path / "isolated-run"
    calls.clear()

    def changing_runner(command, **kwargs):
        isolated["enabled"] = True
        return runner(command, **kwargs)

    with pytest.raises(ValueError, match="disabled"):
        cli.plan(root, args, runner=changing_runner, command="mock-codex")
    assert len(calls) == 1
    assert cli.core.load_json(args.destination / "run.json")["knowledge_calls"][0]["status"] == "incomplete"
    assert not (args.destination / "plan.md").exists()


def scripted_case(tmp_path, monkeypatch, finals):
    root, destination = tmp_path / "benchmark", tmp_path / "run"
    make_case(root)
    prompts = []
    actions = [tool_action("search_knowledge", query="mesh", kind="manual", mode="native"),
               tool_action("read_source", id="evidence-1", offset=0, length=12000), *finals]

    def request(url, payload=None, **kwargs):
        if url.endswith("evaluation-sets"):
            return {"retrieval_enabled": False, "access_token": "MANAGEMENT_SECRET_CANARY"}
        if payload["name"] == "get_library_scope":
            return {"result": {"libraries": [{"id": "manual", "enabled": True}]}}
        if payload["name"] == "search_knowledge":
            return {"result": {"results": [{"id": "evidence-1", "library_id": "manual"},
                                              {"id": "evidence-2", "library_id": "manual"}]}}
        return {"result": {"id": payload["arguments"]["id"], "library_id": "manual", "text": "Real source page"}}

    def runner(command, **kwargs):
        prompts.append(json.loads(kwargs["input_text"]))
        action = actions[len(prompts) - 1]
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(action), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, event_log(), "")

    monkeypatch.setattr(cli.benchmark, "request_json", request)
    args = SimpleNamespace(case="case-01", destination=destination, workbench_url="http://workbench",
                           model="gpt-5.6-luna", effort="medium", timeout=600, max_rounds=16)
    return args, prompts, lambda: cli.plan(root, args, runner=runner, command="mock-codex")


@pytest.mark.parametrize("failure_code", ["final_citations_unread", "final_declared_citation_missing_from_text",
                                          "final_contains_unread_source"])
def test_rejected_final_and_one_citation_feedback_are_saved(tmp_path, monkeypatch, failure_code):
    bad = final_action("evidence-2") if failure_code == "final_citations_unread" else final_action()
    if failure_code == "final_declared_citation_missing_from_text":
        bad["plan_markdown"] = "Complete visible implementation plan with no citation token."
    elif failure_code == "final_contains_unread_source":
        bad["plan_markdown"] += " Unread extra citation [evidence-2]."
    old_attempt = tmp_path / "old-attempt/run.json"
    cli.core.dump_json(old_attempt, {"status": "incomplete", "prior_attempt": True})
    old_bytes = old_attempt.read_bytes()
    args, prompts, run = scripted_case(tmp_path, monkeypatch, [bad, final_action()])
    report = run()
    assert old_attempt.read_bytes() == old_bytes
    assert report["status"] == "complete" and "failure_code" not in report
    assert report["first_final_accepted"] is False and report["completion_path"] == "citation_repaired"
    assert report["citation_repair_count"] == 1 and report["model_call_count"] == 4
    assert report["knowledge_call_count"] == 2
    rejected = report["rejected_candidates"][0]
    assert rejected["failure_code"] == failure_code and rejected["status"] == "rejected"
    assert rejected["scored"] is False and rejected["read_source_ids"] == ["evidence-1"]
    assert rejected["sha256"] == cli.core.sha256(args.destination / rejected["path"])
    saved = (args.destination / rejected["path"]).read_text(encoding="utf-8")
    assert "status: rejected; unscored" in saved and bad["plan_markdown"] in saved
    repair = prompts[-1]["citation_repair"]
    assert repair["failure_codes"][0] == failure_code
    assert repair["read_source_ids"] == ["evidence-1"]
    assert repair["rejected_plan_markdown"] == bad["plan_markdown"]
    _, expected_diagnostics = cli.final_citation_diagnostics(bad, {"evidence-1"}, {"evidence-1", "evidence-2"})
    for key, ids in expected_diagnostics.items():
        assert repair[key] == ids
    assert repair["unread_but_discovered_source_ids"] == sorted(
        set(expected_diagnostics["unread_declared_citation_ids"])
        | set(expected_diagnostics["unread_source_ids_in_text"]))
    assert repair["unrecognized_declared_citation_count"] == 0
    assert "可在原剩余预算内自行read_source核实" in repair["instructions"]
    assert "允许正常action=tools" in repair["instructions"]
    feedback_path = args.destination / rejected["feedback_path"]
    assert cli.core.load_json(feedback_path) == repair
    assert report["citation_feedback_path"] == rejected["feedback_path"]
    assert prompts[-1]["tool_history"] == prompts[-2]["tool_history"]
    assert all(canary not in json.dumps(prompts) for canary in
               ["PRIVATE_REFERENCE_CANARY", "MANAGEMENT_SECRET_CANARY", "THOUGHT_CHAIN_CANARY", "RAW_CLI_CANARY"])
    assert all(canary not in (args.destination / "run.json").read_text(encoding="utf-8")
               for canary in ["THOUGHT_CHAIN_CANARY", "RAW_CLI_CANARY"])


def test_citation_repair_is_limited_to_one_and_failed_candidates_remain_unscored(tmp_path, monkeypatch):
    bad = final_action()
    bad["plan_markdown"] = "Visible plan lacking the declared citation."
    args, prompts, run = scripted_case(tmp_path, monkeypatch, [bad, bad])
    with pytest.raises(cli.PlanningValidationError) as error:
        run()
    assert error.value.code == "final_declared_citation_missing_from_text"
    report = cli.core.load_json(args.destination / "run.json")
    assert report["failure_code"] == "final_declared_citation_missing_from_text"
    assert report["status"] == "incomplete" and report["citation_repair_count"] == 1
    assert len(prompts) == 4 and len(report["rejected_candidates"]) == 2
    assert {item["path"] for item in report["rejected_candidates"]} == {"rejected-plan-03.md", "rejected-plan-04.md"}
    assert all(item["scored"] is False for item in report["rejected_candidates"])
    assert not (args.destination / "plan.md").exists()


def test_citation_feedback_allows_source_read_without_new_budget_or_sampling(tmp_path, monkeypatch):
    args, prompts, run = scripted_case(tmp_path, monkeypatch, [final_action("evidence-2"),
        tool_action("read_source", id="evidence-2", offset=0, length=12000), final_action("evidence-2")])
    report = run()
    assert report["status"] == "complete" and report["model_call_count"] == 5
    assert report["max_rounds"] == 16 and report["citation_repair_count"] == 1
    assert report["knowledge_call_counts"] == {"search_knowledge": 1, "read_source": 2}
    assert len(report["rejected_candidates"]) == 1
    assert prompts[-1]["remaining_read_calls"] == cli.MAX_READ_CALLS - 2
    assert prompts[-1]["citation_repair"] == prompts[-2]["citation_repair"]
    assert prompts[-1]["citation_repair"]["unread_but_discovered_source_ids"] == ["evidence-2"]
    assert report["cited_source_ids"] == ["evidence-2"]


def test_citation_feedback_cannot_reset_the_source_read_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "MAX_READ_CALLS", 1)
    args, prompts, run = scripted_case(tmp_path, monkeypatch, [final_action("evidence-2"),
        tool_action("read_source", id="evidence-2", offset=0, length=12000)])
    with pytest.raises(cli.PlanningValidationError) as error:
        run()
    assert error.value.code == "read_budget_exceeded"
    report = cli.core.load_json(args.destination / "run.json")
    assert report["knowledge_call_counts"] == {"search_knowledge": 1, "read_source": 1}
    assert report["model_call_count"] == 4 and report["citation_repair_count"] == 1
    assert prompts[-1]["remaining_read_calls"] == 0
    assert not (args.destination / "plan.md").exists()


def test_invalid_final_action_is_diagnosed_and_retained_without_retry(tmp_path, monkeypatch):
    bad = final_action()
    bad["cited_source_ids"] = []
    args, prompts, run = scripted_case(tmp_path, monkeypatch, [bad])
    with pytest.raises(cli.PlanningValidationError) as error:
        run()
    assert error.value.code == "invalid_action"
    report = cli.core.load_json(args.destination / "run.json")
    assert report["failure_code"] == "invalid_action" and report["citation_repair_count"] == 0
    assert report["rejected_candidates"][0]["failure_code"] == "invalid_action"
    assert (args.destination / "rejected-plan-03.md").is_file()
    assert not (args.destination / "plan.md").exists() and len(prompts) == 3


def test_source_id_prefix_is_not_treated_as_a_read_or_unread_citation():
    action = final_action("evidence-10")
    codes, diagnostics = cli.final_citation_diagnostics(action, {"evidence-10"}, {"evidence-1", "evidence-10"})
    assert codes == [] and diagnostics["unread_source_ids_in_text"] == []
    action["cited_source_ids"] = ["evidence-1"]
    codes, _ = cli.final_citation_diagnostics(action, {"evidence-1"}, {"evidence-1", "evidence-10"})
    assert "final_declared_citation_missing_from_text" in codes
