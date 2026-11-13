import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tomllib

import pytest

from scripts import benchmark_native_cli as cli
from scripts.benchmark_native_mcp import LIBRARY_IDS
from test_benchmark_codex_cli import make_case


def arguments(destination, arm):
    return SimpleNamespace(case="case-01", destination=destination, arm=arm,
        model="gpt-5.6-luna", effort="medium", timeout=1200,
        workbench_url="http://127.0.0.1:61679" if arm == "rag" else None)


@pytest.mark.parametrize("arm", ["rag", "no-library"])
def test_one_native_process_keeps_final_verbatim_and_only_audits_after_exit(tmp_path, arm):
    root, destination = tmp_path / "benchmark", tmp_path / "run"
    make_case(root)
    invocations = []
    final_bytes = b"# Native plan\r\nUnread citation [unread-or-mistyped-source].  \r\n"

    def runner(command, **kwargs):
        invocations.append(command)
        assert not (destination / "run.json").exists()
        assert "--output-schema" not in command and "--ignore-user-config" in command
        assert "--ephemeral" in command and "--json" in command
        assert command[command.index("--model") + 1] == "gpt-5.6-luna"
        assert kwargs["timeout"] == 1200
        settings = {entry.split("=", 1)[0]: tomllib.loads("value=" + entry.split("=", 1)[1])["value"]
                    for index, entry in enumerate(command) if index and command[index - 1] == "-c"}
        assert settings["model_reasoning_effort"] == "medium"
        assert settings["features.shell_tool"] is False
        assert settings["web_search"] == "disabled"
        assert settings["agents.enabled"] is False
        assert settings["features.code_mode"] is True and settings["features.code_mode_host"] is True
        if arm == "rag":
            assert settings["mcp_servers.knowledge.enabled_tools"] == list(cli.TOOLS)
            assert settings["mcp_servers.knowledge.default_tools_approval_mode"] == "approve"
            mcp_args = settings["mcp_servers.knowledge.args"]
            ledger = Path(mcp_args[mcp_args.index("--ledger") + 1])
            ledger.write_text(json.dumps({"tool": "read_source", "status": "error", "error_code": "workbench_tool_error", "evidence_ids": []}) + "\n"
                + json.dumps({"tool": "read_source", "status": "complete", "evidence_ids": ["real-source"]}) + "\n", encoding="utf-8")
        else:
            assert all(isinstance(value, dict) and value["enabled"] is False for key, value in settings.items()
                       if key.startswith("mcp_servers."))
        Path(command[command.index("--output-last-message") + 1]).write_bytes(final_bytes)
        events = [{"type": "item.completed", "item": {"type": "reasoning", "text": "THOUGHT_CHAIN_CANARY"}},
                  {"type": "item.completed", "item": {"type": "mcp_tool_call", "arguments": "RAW_ARGUMENTS_CANARY"}},
                  {"type": "item.completed", "item": {"type": "todo_list", "text": "RAW_PLAN_CANARY"}},
                  {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 5,
                      "cached_input_tokens": 3, "raw_prompt": "USAGE_SECRET_CANARY"}}]
        return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(event) for event in events), "STDERR_CANARY")

    def scope_reader(url):
        assert arm == "rag"
        return [{"id": identifier, "enabled": True} for identifier in LIBRARY_IDS]

    result = cli.plan(root, arguments(destination, arm), runner=runner, command="mock-codex", scope_reader=scope_reader)
    assert len(invocations) == result["cli_process_count"] == result["agent_execution_count"] == 1
    assert result["model_call_count"] is None
    assert result["status"] == "complete" and result["evaluation_performed"] is False
    assert result["automatic_retry_count"] == 0 and result["execution_feedback_supplied"] is False
    assert result["execution_audit"]["usage"] == {"input_tokens": 12, "output_tokens": 5, "cached_input_tokens": 3}
    assert (destination / "plan.md").read_bytes() == final_bytes
    assert result["read_source_ids"] == (["real-source"] if arm == "rag" else [])
    saved = json.dumps(result) + (destination / "model-input.json").read_text(encoding="utf-8")
    for canary in ("PRIVATE_REFERENCE_CANARY", "THOUGHT_CHAIN_CANARY", "RAW_ARGUMENTS_CANARY", "RAW_PLAN_CANARY", "STDERR_CANARY", "USAGE_SECRET_CANARY"):
        assert canary not in saved


def test_arms_use_the_same_common_policy_without_action_loop_or_quotas():
    for arm in ("rag", "no-library"):
        text = cli.instructions(arm)
        assert text.count(cli.COMMON_PLANNING_POLICY) == 1
        assert "12 次" not in text and "8 次" not in text and "action=tools" not in text
    assert cli.instructions("rag").count(cli.RESEARCH_POLICY) == 1
    assert cli.RESEARCH_POLICY not in cli.instructions("no-library")


def test_explicit_library_scope_is_passed_to_native_mcp_and_run_record(tmp_path):
    root, destination = tmp_path / "benchmark", tmp_path / "run"
    make_case(root)
    args = arguments(destination, "rag")
    args.library_ids = [LIBRARY_IDS[0], "cs63-models-wave-optics-module"]

    def runner(command, **kwargs):
        settings = {entry.split("=", 1)[0]: tomllib.loads("value=" + entry.split("=", 1)[1])["value"]
                    for index, entry in enumerate(command) if index and command[index - 1] == "-c"}
        native_args = settings["mcp_servers.knowledge.args"]
        passed = [native_args[i+1] for i, value in enumerate(native_args) if value == "--library-id"]
        assert passed == args.library_ids
        Path(command[command.index("--output-last-message") + 1]).write_text("Final plan.", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, '{"type":"turn.completed"}', "")

    result = cli.plan(root, args, runner=runner, command="mock-codex", scope_reader=lambda _: [])
    assert result["library_ids"] == sorted(args.library_ids)


def test_no_library_never_requests_the_workbench_and_missing_final_is_not_a_format_rejection(tmp_path):
    root, destination = tmp_path / "benchmark", tmp_path / "run"
    make_case(root)

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "not-json-but-process-finished", "")

    def forbidden_scope(url):
        pytest.fail("no-library must not contact the knowledge service")

    report = cli.plan(root, arguments(destination, "no-library"), runner=runner,
                      command="mock-codex", scope_reader=forbidden_scope)
    assert report["status"] == "complete" and report["final_message_present"] is False
    assert report["execution_audit"]["malformed_event_lines"] == 1
    assert (destination / "plan.md").read_bytes() == b""


def test_timeout_is_one_total_resource_limit_without_retry(tmp_path):
    root, destination = tmp_path / "benchmark", tmp_path / "run"
    make_case(root)
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    result = cli.plan(root, arguments(destination, "no-library"), runner=runner, command="mock-codex")
    assert len(calls) == 1 and result["status"] == "execution_error"
    assert result["failure_type"] == "TimeoutExpired" and result["automatic_retry_count"] == 0


def test_project_mcp_inheritance_is_disabled_without_copying_secrets(tmp_path):
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers.foreign]\ncommand="COMMAND_SECRET_CANARY"\n', encoding="utf-8")
    directory = tmp_path / "public-workspace"
    directory.mkdir()
    settings, names = cli.project_mcp_disables(directory)
    assert "foreign" in names
    assert settings['mcp_servers.foreign']["enabled"] is False
    assert "COMMAND_SECRET_CANARY" not in json.dumps(settings)


def test_observer_write_failure_marks_audit_incomplete_without_rejecting_final(tmp_path):
    root, destination = tmp_path / "benchmark", tmp_path / "run"
    make_case(root)

    def runner(command, **kwargs):
        Path(command[command.index("--output-last-message") + 1]).write_text("Agent final unchanged.", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, '{"type":"turn.completed"}', cli.AUDIT_WRITE_FAILURE_MARKER)

    result = cli.plan(root, arguments(destination, "rag"), runner=runner, command="mock-codex", scope_reader=lambda _: [])
    assert result["status"] == "complete" and result["knowledge_audit_complete"] is False
    assert (destination / "plan.md").read_text(encoding="utf-8") == "Agent final unchanged."
