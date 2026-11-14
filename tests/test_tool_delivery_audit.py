import json

import pytest

from advisor.tool_delivery_audit import audit_rollout, locate_rollout, response_sha256


def make_log(tmp_path, output, *, kind="function_call_output"):
    path = tmp_path / "rollout-synthetic-thread-123.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "synthetic-thread-123"}},
            {"type": "response_item", "payload": {"type": "reasoning", "text": "Warning: truncated output"}},
            {"type": "response_item", "payload": {"type": kind, "call_id": "call-1", "output": output}}]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_native_output_keeps_source_partial_flag_separate(tmp_path):
    path = make_log(tmp_path, '{"text_truncated":true,"next_offset":1000}')
    result = audit_rollout(path, "synthetic-thread-123")
    assert result["valid"] and result["tool_output_count"] == 1
    assert not result["reasoning_inspected"]
    assert locate_rollout("synthetic-thread-123", hint=path) == path.resolve()


def test_real_output_truncation_is_invalid(tmp_path):
    path = make_log(tmp_path, [{"type": "input_text", "text": "Warning: truncated output (original token count: 20000)"}], kind="custom_tool_call_output")
    result = audit_rollout(path, "synthetic-thread-123")
    assert not result["valid"]
    assert result["issues"][0]["code"] == "delivery_warning"


def test_oversized_or_wrong_thread_outputs_are_not_accepted(tmp_path):
    path = make_log(tmp_path, "x" * 9000)
    result = audit_rollout(path, "another-thread")
    assert {item["code"] for item in result["issues"]} == {"output_budget_exceeded", "wrong_thread"}


def test_disabled_host_is_transport_failure(tmp_path):
    result = audit_rollout(make_log(tmp_path, "code-mode host is disabled"), "synthetic-thread-123")
    assert not result["valid"] and result["issues"][0]["code"] == "delivery_warning"


def test_batched_code_cell_is_rejected_even_when_output_is_small(tmp_path):
    path = make_log(tmp_path, "small")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call",
            "name": "exec", "input": "const a=await tools.a({}); const b=await tools.b({}); text(a);", "call_id": "c"}}) + "\n")
    result = audit_rollout(path, "synthetic-thread-123")
    assert not result["valid"] and result["issues"][0]["code"] == "batched_tool_cell"


def test_callback_must_be_fully_visible_not_just_a_projection(tmp_path):
    value = {"text": "关键来源", "next_offset": 1200, "evidence_refs": [{"locator": "x"}]}
    path = make_log(tmp_path, 'Script completed\nOutput:\n' + json.dumps(value, ensure_ascii=False, indent=2))
    assert audit_rollout(path, "synthetic-thread-123", expected_response_sha256=[response_sha256(value)])["valid"]
    path = make_log(tmp_path, json.dumps({"text": value["text"]}))
    result = audit_rollout(path, "synthetic-thread-123", expected_response_sha256=[response_sha256(value)])
    assert not result["valid"]
    assert result["issues"][0]["code"] == "callback_response_not_fully_delivered"


def test_tool_metadata_inventory_has_separate_budget_but_not_business_reads(tmp_path):
    path = make_log(tmp_path, 'x' * 11000)
    rows = path.read_text(encoding='utf-8').splitlines()
    rows.insert(1, json.dumps({'type': 'response_item', 'payload': {'type': 'custom_tool_call',
        'name': 'exec', 'call_id': 'call-1', 'input': 'text(ALL_TOOLS.map(x=>x.name))'}}))
    path.write_text('\n'.join(rows), encoding='utf-8')
    assert audit_rollout(path, 'synthetic-thread-123')['valid']
    rows[1] = rows[1].replace('text(ALL_TOOLS.map(x=>x.name))', 'text(await tools.read_source({}))')
    path.write_text('\n'.join(rows), encoding='utf-8')
    assert not audit_rollout(path, 'synthetic-thread-123')['valid']


def exec_failure_log(tmp_path, error, *, delivered=None):
    path = make_log(tmp_path, error, kind="custom_tool_call_output")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows.insert(1, {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
        "call_id": "call-1", "input": "const card = { explanation: 'synthetic' }; text(card);"}})
    if delivered is not None:
        rows.append({"type": "response_item", "payload": {"type": "custom_tool_call_output",
            "call_id": "call-recovered", "output": "Script completed\nOutput:\n" + json.dumps(delivered)}})
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_explicit_exec_syntax_error_is_diagnostic_when_all_callbacks_are_visible(tmp_path):
    value = {"source": "original callback", "next_offset": None}
    error = "Script failed\nScript error:\nSyntaxError: Unexpected token ':'"
    path = exec_failure_log(tmp_path, error, delivered=value)
    original = path.read_bytes()
    result = audit_rollout(path, "synthetic-thread-123", expected_response_sha256=[response_sha256(value)])
    assert result["schema"] == "tool-delivery-audit/v2"
    assert result["valid"] and result["issues"] == []
    assert len(result["execution_errors"]) == 1
    assert result["execution_errors"][0]["code"] == "recoverable_syntax_error"
    assert result["execution_errors"][0]["error"] == error
    assert path.read_bytes() == original


def test_syntax_error_after_unprinted_callback_remains_invalid(tmp_path):
    value = {"source": "callback received but never printed"}
    path = exec_failure_log(tmp_path, "Script failed\nScript error:\nSyntaxError: Unexpected token in JSON.parse")
    result = audit_rollout(path, "synthetic-thread-123", expected_response_sha256=[response_sha256(value)])
    assert not result["valid"] and result["execution_errors"] == []
    assert {issue["code"] for issue in result["issues"]} == {"delivery_warning", "callback_response_not_fully_delivered"}


def test_exec_syntax_error_in_separate_content_block_is_recoverable(tmp_path):
    value = {"source": "complete callback"}
    blocks = [{"type": "text", "text": "Script failed\nWall time 0.0 seconds\nOutput:\n"},
              {"type": "text", "text": "Script error:\nSyntaxError: Unexpected token ')'"}]
    path = exec_failure_log(tmp_path, blocks, delivered=value)
    result = audit_rollout(path, "synthetic-thread-123", expected_response_sha256=[response_sha256(value)])
    assert result["valid"] and result["issues"] == []
    assert result["execution_errors"][0]["code"] == "recoverable_syntax_error"
    assert result["execution_errors"][0]["error"] == "\n".join(block["text"] for block in blocks)


@pytest.mark.parametrize("expected", [None, []])
def test_syntax_error_without_expected_callback_hashes_is_not_exempt(tmp_path, expected):
    path = exec_failure_log(tmp_path, "Script failed\nScript error:\nSyntaxError: Unexpected token ':'")
    result = audit_rollout(path, "synthetic-thread-123", expected_response_sha256=expected)
    assert not result["valid"] and result["execution_errors"] == []
    assert result["issues"][0]["code"] == "delivery_warning"


@pytest.mark.parametrize("error", ["Script failed\nScript error:\nRuntimeError: failed execution",
    "Script failed\nScript error:\nSyntaxError: unexpected token\nRuntimeError: failed host"])
def test_runtime_errors_stay_invalid_even_when_callbacks_match(tmp_path, error):
    value = {"source": "fully visible"}
    path = exec_failure_log(tmp_path, error, delivered=value)
    result = audit_rollout(path, "synthetic-thread-123", expected_response_sha256=[response_sha256(value)])
    assert not result["valid"] and result["execution_errors"] == []
    assert result["issues"][0]["code"] == "delivery_warning"


def test_syntax_diagnostic_cannot_hide_real_truncation_in_same_exec_output(tmp_path):
    value = {"source": "fully visible"}
    error = "Script failed\nScript error:\nSyntaxError: unexpected token\nWarning: truncated output"
    path = exec_failure_log(tmp_path, error, delivered=value)
    result = audit_rollout(path, "synthetic-thread-123", expected_response_sha256=[response_sha256(value)])
    assert not result["valid"]
    assert any(issue.get("warning") == "Warning: truncated output" for issue in result["issues"])


def test_syntaxerror_text_outside_an_exec_call_is_not_exempt(tmp_path):
    value = {"source": "fully visible"}
    path = make_log(tmp_path, "Script failed\nScript error:\nSyntaxError: unexpected token\n" + json.dumps(value))
    result = audit_rollout(path, "synthetic-thread-123", expected_response_sha256=[response_sha256(value)])
    assert not result["valid"] and result["execution_errors"] == []
