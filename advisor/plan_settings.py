"""Bounded, authored requirement-to-setting transfers and advisory review.

The table records an author's choices; it is not an evidence ledger. Only the
service-supplied ledger can establish that an exact source context was read.
Neither a read receipt nor a matching value establishes physical applicability.
"""
from __future__ import annotations

import copy
import json
from .mechanism_checks import known_check_ids


MAX_SETTINGS = 64
MAX_SOURCE_REFS = 8
MAX_SETTING_BYTES = 12 * 1024
MAX_TABLE_BYTES = 2 * 1024 * 1024
SETTING_STATUSES = ("adopted", "adapted", "alternative", "unresolved", "not_applicable")
SOURCE_CONTEXTS = ("source_text", "case_configuration", "html_section", "original_mph_xml", "navigation")
ACTIVE_SETTING_STATUSES = {"adopted", "adapted", "alternative"}
IMPLEMENTATION_FIELDS = ("action", "inputs", "checks")


def _text(limit=6000, *, empty=False):
    spec = {"type": "string", "maxLength": limit}
    if not empty:
        spec.update(minLength=1, pattern=r"\S")
    return spec


SOURCE_REF_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "source_id": _text(30000),
        "context": {"type": "string", "enum": list(SOURCE_CONTEXTS)},
        "locator": _text(2000),
        "stated_value": _text(empty=True),
    },
    "required": ["source_id", "context", "locator", "stated_value"],
}
PLAN_SETTING_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "description": "每行完整JSON以UTF-8紧凑编码计量不得超过12 KiB（12288字节）；保留原值、迁移理由与缺口，精简重复文字。",
    "properties": {
        "id": _text(200), "requirement_id": _text(500), "route_id": _text(500),
        "requirement": _text(), "setting": _text(),
        "proposed_value": _text(empty=True),
        "status": {"type": "string", "enum": list(SETTING_STATUSES)},
        "reason": _text(), "transfer_rationale": _text(), "gap": _text(empty=True),
        "step_ids": {"type": "array", "maxItems": 64, "uniqueItems": True, "items": _text(30000)},
        "check_ids": {"type": "array", "maxItems": 16, "uniqueItems": True,
                      "items": dict(_text(200), enum=sorted(known_check_ids())), "description": "可选机制清单ID；新规划按get_mechanism_checks逐项登记，不是来源或物理正确性证明。"},
        "implementation": {"type": "object", "additionalProperties": False,
                           "properties": {"step_id": _text(30000),
                                          "field": {"type": "string", "enum": list(IMPLEMENTATION_FIELDS)}},
                           "required": ["step_id", "field"],
                           "description": "已选原子设置的落点；proposed_value原文须出现在该步骤action或inputs/checks的单条文本中。只校验文本保留。"},
        "source_refs": {"type": "array", "maxItems": MAX_SOURCE_REFS, "items": SOURCE_REF_SCHEMA},
    },
    "required": ["id", "requirement", "setting", "proposed_value", "status", "reason",
                 "transfer_rationale", "gap", "step_ids", "source_refs"],
}
PLAN_SETTINGS_SCHEMA = {
    "type": "array", "maxItems": MAX_SETTINGS, "items": PLAN_SETTING_SCHEMA,
    "description": "最多64行；每行完整JSON不超过12 KiB，整表完整JSON不超过2 MiB（2097152字节），均按UTF-8紧凑编码计量。",
}


def _string(value, spec, path):
    if not isinstance(value, str) or len(value) > spec["maxLength"]:
        raise ValueError(path + " must be bounded text")
    if spec.get("minLength") and not value.strip():
        raise ValueError(path + " must be nonempty text")


def _fields(value, schema, path):
    if (not isinstance(value, dict) or not set(schema["required"]) <= set(value)
            or set(value) - set(schema["properties"])):
        raise ValueError(path + " fields do not match the plan-setting schema")


def _json_bytes(value, path):
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError(path + " must be valid UTF-8 JSON") from error


def validate_plan_settings(rows):
    """Validate authored shape and bounds; return a copy without granting trust.

    Missing citations and dangling step links are advisory review findings.
    Unresolved and inapplicable rows may leave the proposed value empty; their
    explicit reason and transfer rationale are still required. Byte limits use
    compact JSON with literal Unicode encoded as UTF-8, including JSON syntax.
    """
    if not isinstance(rows, list) or len(rows) > MAX_SETTINGS:
        raise ValueError("plan_settings must be an array of at most 64 rows")
    seen = set()
    for index, row in enumerate(rows):
        path = f"plan_settings[{index}]"
        _fields(row, PLAN_SETTING_SCHEMA, path)
        for name, value in row.items():
            spec = PLAN_SETTING_SCHEMA["properties"][name]
            if spec["type"] == "string":
                if "enum" in spec:
                    if not isinstance(value, str) or value not in spec["enum"]:
                        raise ValueError(path + "." + name + " has an unknown value")
                else:
                    _string(value, spec, path + "." + name)
        if row["id"] in seen:
            raise ValueError(path + ".id must be unique")
        seen.add(row["id"])
        if row["status"] in {"adopted", "adapted", "alternative"} and not row["proposed_value"].strip():
            raise ValueError(path + ".proposed_value is required for this authored status")
        steps = row["step_ids"]
        if not isinstance(steps, list) or len(steps) > 64:
            raise ValueError(path + ".step_ids must be an array of at most 64 IDs")
        for step in steps:
            _string(step, PLAN_SETTING_SCHEMA["properties"]["step_ids"]["items"], path + ".step_ids")
        if len(set(steps)) != len(steps):
            raise ValueError(path + ".step_ids cannot repeat an ID")
        if "check_ids" in row:
            from .mechanism_checks import CHECK_IDS
            identities = row["check_ids"]
            if (not isinstance(identities, list) or len(identities) > 16
                    or any(not isinstance(item, str) or item not in CHECK_IDS for item in identities)
                    or len(set(identities)) != len(identities)):
                raise ValueError(path + ".check_ids must contain at most 16 distinct known mechanism check IDs")
        if "implementation" in row:
            binding = row["implementation"]
            spec = PLAN_SETTING_SCHEMA["properties"]["implementation"]
            _fields(binding, spec, path + ".implementation")
            _string(binding["step_id"], spec["properties"]["step_id"], path + ".implementation.step_id")
            if binding["field"] not in IMPLEMENTATION_FIELDS:
                raise ValueError(path + ".implementation.field must be action, inputs or checks")
        refs = row["source_refs"]
        if not isinstance(refs, list) or len(refs) > MAX_SOURCE_REFS:
            raise ValueError(path + ".source_refs must be an array of at most 8 references")
        ref_keys = set()
        for ref_index, ref in enumerate(refs):
            ref_path = path + f".source_refs[{ref_index}]"
            _fields(ref, SOURCE_REF_SCHEMA, ref_path)
            for name in ("source_id", "locator", "stated_value"):
                _string(ref[name], SOURCE_REF_SCHEMA["properties"][name], ref_path + "." + name)
            if not isinstance(ref["context"], str) or ref["context"] not in SOURCE_CONTEXTS:
                raise ValueError(ref_path + ".context has an unknown value")
            key = (ref["source_id"], ref["context"], ref["locator"])
            if key in ref_keys:
                raise ValueError(ref_path + " repeats an exact source context")
            ref_keys.add(key)
        if _json_bytes(row, path) > MAX_SETTING_BYTES:
            raise ValueError(path + f" exceeds {MAX_SETTING_BYTES} canonical UTF-8 JSON bytes")
    if _json_bytes(rows, "plan_settings") > MAX_TABLE_BYTES:
        raise ValueError(f"plan_settings exceeds {MAX_TABLE_BYTES} canonical UTF-8 JSON bytes")
    return copy.deepcopy(rows)


def setting_implementation_findings(rows, plan=None):
    """Literal-retention findings only, never semantic/physical validation.

    A new mechanism-mapped active setting needs its entire atomic proposed value
    in an explicitly selected actionable field. A summary, citation or another
    step does not satisfy that contract. Old tables remain editable.
    """
    steps_known = isinstance(plan, dict) and isinstance(plan.get("steps"), list)
    steps = {s["id"]: s for s in plan.get("steps", [])
             if isinstance(s, dict) and isinstance(s.get("id"), str)} if steps_known else {}
    findings = []
    for row in rows:
        if row["status"] not in ACTIVE_SETTING_STATUSES:
            continue
        binding = row.get("implementation")
        base = {"category": "implementation_check", "setting_id": row["id"]}
        if not binding:
            if row.get("check_ids"):
                findings.append({**base, "code": "implementation_binding_missing",
                    "message": "已选机制设置没有实施落点；填implementation并将proposed_value原文放入对应步骤。"})
            continue
        identity, field = binding["step_id"], binding["field"]
        where = {**base, "step_id": identity, "field": field}
        if identity not in row["step_ids"]:
            findings.append({**where, "code": "implementation_step_unmapped",
                             "message": "实施落点不在此设置的step_ids中。"})
            continue
        if not steps_known:
            continue  # Setting extraction precedes authoring a plan.
        if identity not in steps:
            findings.append({**where, "code": "implementation_step_missing", "message": "实施落点指向不存在的步骤。"})
            continue
        value = steps[identity].get(field)
        texts = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        if not any(isinstance(text, str) and row["proposed_value"] in text for text in texts):
            findings.append({**where, "code": "implementation_value_missing",
                "message": "拟用值未原样保留在绑定字段；将简短原子proposed_value放入该字段，实际操作另写action。"})
    return findings


def require_setting_implementations(rows, plan):
    findings = setting_implementation_findings(rows, plan)
    if findings:
        first = findings[0]
        raise ValueError("plan_settings implementation: " + first["setting_id"] + ": " + first["message"])


def _ledger_ids(evidence, name):
    values = evidence.get(name)
    if values is None:
        return None
    if (not isinstance(values, (list, tuple, set, frozenset))
            or any(not isinstance(value, str) or not value.strip() for value in values)):
        raise ValueError(name + " must contain nonempty source IDs")
    return set(values)


def _ledger_contexts(evidence):
    values = evidence.get("read_contexts")
    if values is None:
        return None
    if not isinstance(values, (list, tuple)):
        raise ValueError("read_contexts must be an array of exact source contexts")
    result = set()
    for entry in values:
        if not isinstance(entry, dict) or not {"source_id", "context", "locator"} <= set(entry):
            raise ValueError("read_contexts entries need source_id, context and locator")
        for name in ("source_id", "locator"):
            _string(entry[name], SOURCE_REF_SCHEMA["properties"][name], "read_contexts." + name)
        if not isinstance(entry["context"], str) or entry["context"] not in SOURCE_CONTEXTS:
            raise ValueError("read_contexts has an unknown context")
        result.add((entry["source_id"], entry["context"], entry["locator"]))
    return result


def review_plan_settings(rows=None, *, plan=None, evidence=None):
    """Review exact links without judging physics, values or transfer reasoning.

    ``evidence`` must be supplied by the service, not copied from authored rows.
    Absent ledger keys remain unknown. Exact read contexts do not expand to a
    sibling node, a different page, or another evidence mode of the same source.
    """
    rows = validate_plan_settings([] if rows is None else rows)
    if plan is not None and not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    if evidence is not None and not isinstance(evidence, dict):
        raise ValueError("evidence must be a trusted ledger object")
    evidence = evidence or {}
    read_ids = _ledger_ids(evidence, "read_source_ids")
    available_ids = _ledger_ids(evidence, "available_source_ids")
    contexts = _ledger_contexts(evidence)
    steps_known = plan is not None and isinstance(plan.get("steps"), list)
    step_ids = {step["id"] for step in plan["steps"]
                if isinstance(step, dict) and isinstance(step.get("id"), str)} if steps_known else set()
    warnings, reviewed = [], []

    def warn(code, category, message, row, **where):
        warnings.append({"code": code, "category": category, "message": message,
                         "setting_id": row["id"], **where})

    for row in rows:
        if steps_known:
            unknown = sorted(set(row["step_ids"]) - step_ids)
            if unknown:
                warn("unknown_setting_step", "implementation_check", "设置映射指向不存在的实施步骤。", row, step_ids=unknown)
        if row["status"] in {"adopted", "adapted"} and not row["step_ids"]:
            warn("setting_has_no_implementation_step", "implementation_check", "已采用或调整的设置尚未对应具体步骤。", row)
        if row["status"] == "unresolved":
            warn("authored_setting_unresolved", "authored_gap", "作者将此设置标为未解决；保留理由与影响。", row, reason=row["reason"])
        if row["gap"].strip():
            warn("authored_setting_gap", "authored_gap", row["gap"], row)
        if not row["source_refs"]:
            warn("setting_has_no_source", "evidence_gap", "此设置未附来源；保留用户输入或推导理由，外部依据尚未记录。", row)
        source_reviews = []
        for ref_index, ref in enumerate(row["source_refs"]):
            identity = ref["source_id"]
            key = (identity, ref["context"], ref["locator"])
            where = {"source_ref_index": ref_index, "source_id": identity,
                     "context": ref["context"], "locator": ref["locator"]}
            checks = {
                "read_status": "unknown" if read_ids is None else "read" if identity in read_ids else "not_in_read_ledger",
                "availability": "unknown" if available_ids is None else "available" if identity in available_ids else "unavailable",
                "context_status": "unknown" if contexts is None else "recorded" if key in contexts else "not_in_read_ledger",
            }
            if read_ids is not None and identity not in read_ids:
                warn("source_not_in_read_ledger", "evidence_gap", "来源不在服务提供的已读账本中。", row, **where)
            if available_ids is not None and identity not in available_ids:
                warn("source_unavailable", "evidence_gap", "来源当前不可用，不能据此新增已核实主张。", row, **where)
            if ref["context"] == "navigation":
                warn("navigation_is_not_setting_evidence", "evidence_gap", "导航只用于定位，不能作为已读取正文或配置的设置依据。", row, **where)
            elif contexts is None:
                warn("source_context_unknown", "evidence_gap", "服务未提供精确阅读范围；此来源定位尚未核对。", row, **where)
            elif key not in contexts:
                warn("source_context_not_in_read_ledger", "evidence_gap", "此正文范围或配置节点不在精确阅读账本中；其他范围的阅读不能替代。", row, **where)
            if ref["context"] != "navigation" and not ref["stated_value"].strip():
                warn("source_stated_value_missing", "evidence_gap", "尚未记录来源实际说明的值或方法，不能与拟用设置混同。", row, **where)
            confirmed = (checks == {"read_status": "read", "availability": "available", "context_status": "recorded"}
                         and ref["context"] != "navigation")
            source_reviews.append({**where, **checks, "evidence_status": "read_context_recorded" if confirmed else "review_required",
                                   "source_value_verification": "not_evaluated"})
        reviewed.append({"setting_id": row["id"], "status": row["status"], "source_reviews": source_reviews,
                         "transfer_review": "agent_review_required"})

    warnings.extend(setting_implementation_findings(rows, plan))

    return {"schema": "plan-settings-review/v1", "status": "advisory", "blocking": False,
            "physical_correctness": "not_evaluated", "source_value_verification": "not_evaluated",
            "source_applicability": "agent_review_required", "plan_settings": rows, "per_setting": reviewed,
            "warnings": warnings, "gaps": [item for item in warnings if item["category"] in {"evidence_gap", "authored_gap"}],
            "implementation_checks": [item for item in warnings if item["category"] == "implementation_check"],
            "evidence_ledger_scope": {"read_source_ids_known": read_ids is not None,
                                      "available_source_ids_known": available_ids is not None,
                                      "read_contexts_known": contexts is not None},
            "plan_step_ids_known": steps_known}
