import json

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
