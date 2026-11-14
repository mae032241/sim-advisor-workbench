"""Check actual tool output visibility for an isolated experiment thread.

Only session identity and tool call/output records are examined. Reasoning and
assistant messages are never read into the audit. Source extraction flags such
as text_truncated are different from transport/output truncation warnings.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from collections import Counter


_WARNINGS = re.compile(
    r"Warning:\s*truncated output|Output exceeded[^\n]*truncat|"
    r"Tool result exceeded the runtime limit|Too many concurrent tool calls|"
    r"code-mode host is disabled|Script failed(?:\r?\n|$)", re.IGNORECASE)
_SYNTAX_ERROR = re.compile(r"(?:^|\r?\n)(?:Script error:\s*)?SyntaxError\b")
_RUNTIME_ERROR = re.compile(r"\bRuntimeError\b")
_OUTPUT_TYPES = {"custom_tool_call_output", "function_call_output", "tool_call_output",
                 "dynamic_tool_call_output"}


def locate_rollout(thread_id, hint=None, codex_home=None):
    if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,100}", thread_id):
        raise ValueError("An exact experiment thread ID is required")
    home = Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    candidates = []
    if hint:
        path = Path(hint)
        if path.is_file() and thread_id in path.name:
            candidates.append(path)
    if not candidates:
        candidates = list((home / "sessions").glob("*/*/*/*" + thread_id + ".jsonl"))
    if len(candidates) != 1:
        raise FileNotFoundError("Could not uniquely locate this experiment's tool-output record")
    return candidates[0].resolve(strict=True)


def _texts(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item["text"] for item in value if isinstance(item, dict)
                and isinstance(item.get("text"), str)]
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return [value["text"]]
        for key in ("contentItems", "content", "output"):
            if key in value:
                return _texts(value[key])
    return []


def response_sha256(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _response_hashes(text):
    """Decode complete JSON values from a tool wrapper without accepting a prefix."""
    decoder, found, cursor = json.JSONDecoder(), [], 0
    for match in re.finditer(r'[\{\[\"]', text):
        if match.start() < cursor:
            continue
        try:
            value, end = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, (dict, list)):
            found.append(response_sha256(value))
            cursor = end
    return found


def audit_rollout(path, expected_thread_id, max_output_bytes=8192, require_outputs=True,
                  expected_response_sha256=None):
    path = Path(path)
    raw = path.read_bytes()
    issues, outputs, syntax_failures, execution_errors = [], [], [], []
    observed_thread, delivered, inventory_calls, exec_calls = None, Counter(), set(), set()
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            issues.append({"code": "incomplete_rollout_record", "line": number})
            continue
        payload = record.get("payload", {})
        if record.get("type") == "session_meta":
            observed_thread = payload.get("id")
            continue
        if record.get("type") == "response_item" and payload.get("type") == "custom_tool_call" and payload.get("name") == "exec":
            exec_calls.add(payload.get("call_id"))
            script = payload.get("input", "")
            if isinstance(script, str) and 'ALL_TOOLS' in script and not re.search(r"\btools\s*(?:\.|\[)", script):
                inventory_calls.add(payload.get('call_id'))
            if isinstance(script, str) and (len(re.findall(r"\btools\s*(?:\.|\[)", script)) > 1 or "Promise.all" in script):
                issues.append({"code": "batched_tool_cell", "line": number, "call_id": payload.get("call_id")})
        if record.get("type") != "response_item" or payload.get("type") not in _OUTPUT_TYPES:
            continue
        texts = _texts(payload.get("output", payload.get("content", payload.get("result"))))
        size = sum(len(text.encode("utf-8")) for text in texts)
        for text in texts:
            delivered.update(_response_hashes(text))
        inventory = payload.get('call_id') in inventory_calls
        budget = max(max_output_bytes, 16384) if inventory else max_output_bytes
        row = {"line": number, "call_id": payload.get("call_id"), "type": payload["type"],
               "text_bytes": size, 'metadata_inventory': inventory, 'byte_budget': budget}
        execution_text = "\n".join(texts)
        for text in texts:
            for match in _WARNINGS.finditer(text):
                warning = match.group(0)
                if (warning.lower().startswith("script failed") and payload.get("call_id") in exec_calls
                        and _SYNTAX_ERROR.search(execution_text) and not _RUNTIME_ERROR.search(execution_text)):
                    # Decide only after all callback hashes have been checked.
                    # A parse failure can happen after a callback was received
                    # but before it was printed, which is still lost delivery.
                    syntax_failures.append({"line": number, "call_id": payload.get("call_id"),
                                            "warning": warning, "error": execution_text})
                else:
                    issues.append({"code": "delivery_warning", "line": number, "warning": warning})
        if size > budget:
            issues.append({"code": "output_budget_exceeded", "line": number,
                           "bytes": size, "maximum": budget})
        if not texts:
            issues.append({"code": "unrecognized_tool_output", "line": number})
        outputs.append(row)
    if observed_thread != expected_thread_id:
        issues.append({"code": "wrong_thread", "expected": expected_thread_id, "observed": observed_thread})
    if require_outputs and not outputs:
        issues.append({"code": "tool_outputs_missing"})
    missing = Counter()
    if expected_response_sha256 is not None:
        missing = Counter(expected_response_sha256) - delivered
        if missing:
            issues.append({"code": "callback_response_not_fully_delivered", "missing_count": sum(missing.values()),
                           "response_sha256": list(missing)})
    for failure in syntax_failures:
        if expected_response_sha256 and not missing:
            execution_errors.append(dict(failure, code="recoverable_syntax_error"))
        else:
            issues.append(dict(failure, code="delivery_warning"))
    return {"schema": "tool-delivery-audit/v2", "valid": not issues, "issues": issues,
            "execution_errors": execution_errors,
            "tool_output_count": len(outputs), "max_output_bytes_observed": max(
                (row["text_bytes"] for row in outputs), default=0),
            "output_byte_budget": max_output_bytes, "rollout_path": str(path.resolve()),
            "expected_response_count": None if expected_response_sha256 is None else len(expected_response_sha256),
            "rollout_sha256": hashlib.sha256(raw).hexdigest(), "outputs": outputs,
            "reasoning_inspected": False, "assistant_messages_inspected": False}
