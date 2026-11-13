"""Bounded, task-owned save drafts and exact, atomic JSON-pointer repairs.

Drafts are inert JSON. Reading or patching one never grants citation authority;
the planning service must run its normal authorization and commit checks again.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import threading
import uuid

from .tool_contracts import expectations

from .sessions import ConflictError

MAX_DRAFT_BYTES = 2 * 1024 * 1024
MAX_READ_CHARACTERS = 16000
MAX_PATCHES = 64
_LOCK = threading.RLock()


def pointer(parts):
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def schema_errors(value, schema, path=""):
    """Collect every shape error for the JSON-schema subset used by planners.

    The default bounds match the existing strict validator. Unlike that
    validator, errors in sibling branches do not hide one another.
    """
    errors = []

    def issue(code, message, at=path, spec=None):
        expected = expectations(schema if spec is None else spec)
        errors.append({"path": at, "code": code, "message": message,
                       **({"expected": expected} if expected else {})})

    expected = schema.get("type")
    types = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
             "string": lambda v: isinstance(v, str), "boolean": lambda v: type(v) is bool,
             "integer": lambda v: type(v) is int,
             "number": lambda v: type(v) is int or (type(v) is float and math.isfinite(v)),
             "null": lambda v: v is None}
    if expected in types and not types[expected](value):
        issue("type", f"Expected {expected}.")
        return errors
    if "enum" in schema and value not in schema["enum"]:
        issue("enum", "Expected one of " + json.dumps(schema["enum"], ensure_ascii=False) + ".")
    if "const" in schema and value != schema["const"]:
        issue("const", "Value does not match the required constant.")
    if expected == "object":
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                issue("required", "Required field is missing.", path + pointer([name]), properties.get(name, {}))
        for name, child in value.items():
            child_path = path + pointer([name])
            if name in properties:
                errors.extend(schema_errors(child, properties[name], child_path))
            elif schema.get("additionalProperties") is False:
                issue("additionalProperties", "Unexpected field; preserve it until explicitly repaired. Allowed fields: " + ", ".join(properties), child_path)
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(schema_errors(child, schema["additionalProperties"], child_path))
    elif expected == "array":
        if schema.get("uniqueItems"):
            for index, child in enumerate(value):
                if child in value[:index]:
                    issue("uniqueItems", "Array items must be distinct.", path + pointer([index]))
        for key, valid in (("minItems", len(value) >= schema.get("minItems", 0)),
                           ("maxItems", len(value) <= schema.get("maxItems", 200))):
            if not valid:
                issue(key, f"Array length violates {key}={schema.get(key, 200)}.")
        for index, child in enumerate(value):
            errors.extend(schema_errors(child, schema.get("items", {}), path + pointer([index])))
    elif expected == "string":
        if len(value) < schema.get("minLength", 0):
            issue("minLength", f"Text is shorter than {schema['minLength']} characters.")
        if len(value) > schema.get("maxLength", 30000):
            issue("maxLength", f"Text exceeds {schema.get('maxLength', 30000)} characters.")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            issue("pattern", "Text does not match the required pattern.")
    elif expected in {"integer", "number"}:
        for key, valid in (("minimum", value >= schema.get("minimum", -math.inf)),
                           ("maximum", value <= schema.get("maximum", math.inf)),
                           ("exclusiveMinimum", value > schema.get("exclusiveMinimum", -math.inf)),
                           ("exclusiveMaximum", value < schema.get("exclusiveMaximum", math.inf))):
            if not valid:
                issue(key, f"Number violates {key}={schema[key]}.")
    return errors


def semantic_error(error):
    """Retain an exact existing validator path when its message supplies one."""
    match = re.search(r"(?:design_brief|questions|retired_questions|evidence_gaps|plan_settings|plan)(?:\.[a-zA-Z_][a-zA-Z_0-9]*|\[\d+\])+", str(error))
    path = pointer(re.findall(r"[a-zA-Z_][a-zA-Z_0-9]*|\d+", match[0])) if match else ""
    if not path and str(error).startswith("plan_settings "):
        path = "/plan_settings"
    code = "permission" if isinstance(error, PermissionError) else "revision_conflict" if isinstance(error, ConflictError) else "semantic"
    if code == "revision_conflict":
        path = "/revision"
    return {"path": path, "code": code, "message": str(error)}


class _DraftFailure:
    def __init__(self, message, *, draft=None, errors=None, code="plan_draft_invalid", next_action=None,
                 contract_tool="save_plan"):
        super().__init__(message)
        self.draft_id = draft.get("draft_id") if draft else None
        self.draft_revision = draft.get("draft_revision") if draft else None
        self.errors = errors or [{"path": "", "code": "validation", "message": message}]
        self.code = code
        self.contract_tool = contract_tool
        self.next_action = next_action or {
            "tool": "repair_plan_draft", "draft_id": self.draft_id,
            "draft_revision": self.draft_revision,
            "instruction": "Read only affected paths with get_plan_draft; repair these fields with exact add/replace/remove patches. Unrelated content is retained. A valid repair reruns all save checks before committing."}

    def to_dict(self):
        return {"error": str(self), "error_code": self.code, "message": str(self), "draft_id": self.draft_id,
                "draft_revision": self.draft_revision, "errors": copy.deepcopy(self.errors),
                "contract": {"tool": "get_tool_contract", "arguments": {"tool": self.contract_tool},
                             "note": "Use each error path to read the exact input-field schema; do not guess enum values or types."},
                "next_action": copy.deepcopy(self.next_action), "saved": False}


class PlanDraftError(_DraftFailure, ValueError):
    pass


class PlanDraftPermissionError(_DraftFailure, PermissionError):
    pass


class PlanDraftConflictError(_DraftFailure, ConflictError):
    pass


def _json_text(value):
    def require_json(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings.")
            for child in item.values():
                require_json(child)
        elif isinstance(item, list):
            for child in item:
                require_json(child)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise ValueError("Only JSON values can be retained without coercion.")
    try:
        require_json(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise PlanDraftError("Save draft must contain finite JSON values.", errors=[{
            "path": "", "code": "json", "message": "Payload is not serializable as finite JSON."}],
            next_action={"tool": "save_plan", "instruction": "Submit bounded, finite JSON values."}) from error


def _bounded_payload(value):
    text = _json_text(value)
    if len(text.encode("utf-8")) > MAX_DRAFT_BYTES:
        raise PlanDraftError("Save draft exceeds the 2 MiB payload limit; the previous draft was retained.",
            errors=[{"path": "", "code": "maxBytes", "message": "Maximum draft payload is 2097152 UTF-8 bytes."}],
            next_action={"tool": "save_plan", "instruction": "Reduce the payload below 2 MiB; the previous draft is unchanged."})


def _parts(path):
    if not isinstance(path, str) or len(path) > 2000 or (path and not path.startswith("/")) or re.search(r"~(?![01])", path):
        raise ValueError("Path must be a bounded JSON pointer, with ~0 and ~1 escaping.")
    return [] if path == "" else [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _index(part, size, *, add=False):
    if part == "-" and add:
        return size
    if not re.fullmatch(r"0|[1-9][0-9]*", part) or len(part) > 10:
        raise ValueError("Array paths require a canonical index; '-' is available only for add.")
    index = int(part)
    if (index > size if add else index >= size):
        raise ValueError("Array index does not exist.")
    return index


def _lookup(value, parts):
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list):
            value = value[_index(part, len(value))]
        else:
            raise ValueError("Exact draft path does not exist.")
    return value


class PlanDraftStore:
    """One latest draft per validated task; identities prevent stale overwrites."""
    lock = _LOCK

    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.directory = self.root / "plan-drafts"

    def _owned(self, path):
        if not path.is_relative_to(self.root) or not path.resolve().is_relative_to(self.root):
            raise PermissionError("Draft path escapes the workspace.")
        current = self.root
        for part in path.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise PermissionError("Draft directories and files cannot be symlinks or junctions.")
        return path

    def _path(self, sid):
        if not isinstance(sid, str) or re.fullmatch(r"[a-f0-9]{32}", sid) is None:
            raise FileNotFoundError("Draft task does not exist.")
        return self._owned(self.directory / (sid + ".json"))

    def _write(self, sid, record):
        path = self._path(sid)
        _bounded_payload(record["payload"])
        self._owned(self.directory).mkdir(parents=True, exist_ok=True)
        temp = self._owned(path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp"))
        try:
            temp.write_text(_json_text(record), encoding="utf-8")
            os.replace(temp, self._path(sid))
        finally:
            if temp.exists():
                temp.unlink()

    def capture(self, sid, payload):
        with self.lock:
            _bounded_payload(payload)
            record = {"schema": "sim-advisor-plan-draft/v1", "session_id": sid,
                      "draft_id": uuid.uuid4().hex, "draft_revision": 1,
                      "status": "draft", "payload": copy.deepcopy(payload), "last_error": None}
            self._write(sid, record)
            return record

    def load(self, sid, draft_id):
        with self.lock:
            if not isinstance(draft_id, str) or not re.fullmatch(r"[a-f0-9]{32}", draft_id):
                raise PlanDraftError("Invalid draft identity.", code="plan_draft_not_found")
            path = self._path(sid)
            if not path.is_file():
                raise PlanDraftError("No saved draft exists for this task.", code="plan_draft_not_found")
            # The envelope is small; payloads are bounded before every write.
            if path.stat().st_size > MAX_DRAFT_BYTES + 65536:
                raise PlanDraftError("Stored draft exceeds the read limit.", code="plan_draft_too_large")
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("schema") != "sim-advisor-plan-draft/v1" or record.get("session_id") != sid:
                raise PlanDraftError("Stored draft ownership is invalid.", code="plan_draft_not_found")
            if record.get("draft_id") != draft_id:
                raise PlanDraftConflictError("A newer save attempt replaced this draft; reload the latest draft.",
                    draft=record, code="plan_draft_conflict", errors=[{"path": "/draft_id", "code": "draft_conflict", "message": "Draft identity is no longer current."}],
                    next_action={"tool": "get_plan_draft", "draft_id": record.get("draft_id")})
            return record

    def note_error(self, sid, record, errors):
        # Shape errors are recomputed, so no unbounded duplicate error history is
        # persisted. A semantic failure remains visible on subsequent reads.
        record["last_error"] = errors[0] if errors and errors[0]["code"] in {"semantic", "permission", "revision_conflict"} else None
        self._write(sid, record)

    def mark_saved(self, sid, record, revision):
        record.update(status="saved", saved_revision=revision, last_error=None)
        self._write(sid, record)

    def read(self, sid, draft_id, schema, *, path="", offset=0, length=MAX_READ_CHARACTERS):
        with self.lock:
            record = self.load(sid, draft_id)
            try:
                if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= MAX_READ_CHARACTERS:
                    raise ValueError("Read offset must be nonnegative and length must be between 1 and 16000.")
                selected = _lookup(record["payload"], _parts(path))
            except ValueError as error:
                raise PlanDraftError(str(error), draft=record, errors=[{"path": path if isinstance(path, str) else "", "code": "draft_read", "message": str(error)}]) from error
            text = json.dumps(selected, ensure_ascii=False, indent=2, allow_nan=False)
            errors = schema_errors(record["payload"], schema)
            if not errors and record.get("last_error"):
                errors = [record["last_error"]]
            return {"draft_id": draft_id, "draft_revision": record["draft_revision"], "status": record["status"],
                    "path": path, "format": "json", "text": text[offset:offset + length],
                    "offset": offset, "total_characters": len(text),
                    "next_offset": offset + length if offset + length < len(text) else None,
                    "errors": errors, "citation_authority": "unchanged",
                    "contract": {"tool": "get_tool_contract", "arguments": {"tool": "save_plan", "path": path},
                                 "note": "Draft text is the current value, not the allowed schema."}}

    def patch(self, sid, draft_id, draft_revision, patches):
        with self.lock:
            record = self.load(sid, draft_id)
            if type(draft_revision) is not int or draft_revision != record["draft_revision"]:
                raise PlanDraftConflictError("Draft changed; read its current revision before patching.", draft=record,
                    code="plan_draft_conflict", errors=[{"path": "/draft_revision", "code": "draft_conflict", "message": "Expected the current draft revision."}],
                    next_action={"tool": "get_plan_draft", "draft_id": draft_id})
            if record["status"] != "draft":
                raise PlanDraftConflictError("This draft was already saved; read the task context before editing.", draft=record,
                    code="plan_draft_already_saved", next_action={"tool": "get_task_context"})
            if not isinstance(patches, list) or not 1 <= len(patches) <= MAX_PATCHES:
                raise PlanDraftError("Repair requires 1 to 64 exact patches.", draft=record,
                    errors=[{"path": "/patches", "code": "patch", "message": "Expected 1 to 64 patches."}])
            payload = copy.deepcopy(record["payload"])
            for index, patch in enumerate(patches):
                try:
                    if not isinstance(patch, dict) or patch.get("op") not in {"add", "replace", "remove"}:
                        raise ValueError("Patch op must be add, replace or remove.")
                    required = {"op", "path"} | ({"value"} if patch["op"] != "remove" else set())
                    if set(patch) != required:
                        raise ValueError("Patch fields must exactly match op/path and value for add or replace.")
                    parts = _parts(patch["path"])
                    if not parts:
                        raise ValueError("Patch an exact field or section; replacing the entire draft root is not supported.")
                    parent = _lookup(payload, parts[:-1])
                    key, op = parts[-1], patch["op"]
                    if isinstance(parent, dict):
                        if op == "add" and key in parent:
                            raise ValueError("Add requires a missing field; use replace for an existing field.")
                        if op != "add" and key not in parent:
                            raise ValueError("Exact patch field does not exist.")
                        if op == "remove":
                            del parent[key]
                        else:
                            parent[key] = copy.deepcopy(patch["value"])
                    elif isinstance(parent, list):
                        position = _index(key, len(parent), add=op == "add")
                        if op == "add":
                            parent.insert(position, copy.deepcopy(patch["value"]))
                        elif op == "replace":
                            parent[position] = copy.deepcopy(patch["value"])
                        else:
                            parent.pop(position)
                    else:
                        raise ValueError("Patch parent must be an existing object or array.")
                except (KeyError, TypeError, ValueError) as error:
                    raise PlanDraftError(str(error), draft=record, errors=[{"path": f"/patches/{index}", "code": "patch", "message": str(error)}]) from error
            try:
                _bounded_payload(payload)
            except PlanDraftError as error:
                raise PlanDraftError(str(error), draft=record, errors=error.errors,
                    next_action={"tool": "get_plan_draft", "draft_id": draft_id,
                                 "instruction": "The patch batch was rejected without changing the draft; inspect its current contents and submit a bounded JSON repair."}) from error
            record.update(payload=payload, draft_revision=record["draft_revision"] + 1, last_error=None)
            self._write(sid, record)
            return record
