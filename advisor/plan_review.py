"""Advisory coverage review of public task inputs and an authored plan.

This module does not select physics or judge equations. It only inspects explicit
fields, an optional agent-authored requirement map, and a trusted evidence
ledger. Missing mappings are questions for the author, not failed requirements.
"""
from __future__ import annotations

import json


_TEXT = {"type": "string", "maxLength": 6000}
_IDS = {"type": "array", "items": {"type": "string", "maxLength": 30000}, "maxItems": 64}
CHECKLIST_SCHEMA = {"type": "array", "maxItems": 64, "items": {
    "type": "object", "additionalProperties": False,
    "properties": {"requirement_id": {"type": "string", "maxLength": 500},
                   "statement": _TEXT, "step_ids": _IDS,
                   "status": {"type": "string", "enum": ["implemented", "conditional", "unresolved", "not_applicable"]},
                   "reason": _TEXT, "source_ids": _IDS},
    "required": ["requirement_id", "step_ids", "status", "reason"]}}

# These are questions for the author. They are deliberately not keyword tests:
# mentioning an interface or a unit cannot establish a complete implementation.
REVIEW_QUESTIONS = [
    "每个公开目标量和已给输入是否对应到具体步骤，并保留定义、表达式、单位与成立条件？",
    "选定路线是否交代计算域、实体定位、接口/本构、材料及边界/初始条件的实际赋值？",
    "存在耦合时，来源、接收量、变量定义、单位和传递顺序是否明确；不适用时是否说明理由？",
    "网格与研究/求解设置是否联系实际尺度、变量依赖和所选方法的适用条件？",
    "结果提取的位置、表达式、单位和验收方式是否与公开目标逐项一致？",
    "关键设置是否有实际已读且可用的依据；用户输入缺口、方法证据缺口和实施前检查是否分开？",
]


def _text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _bounded(value, limit=6000):
    text = _text(value)
    return text[:limit], len(text) > limit


def _nonempty(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value) and all(isinstance(item, str) and item.strip() for item in value)
    return False


def _inventory(context, design_brief):
    items = []

    def add(identity, origin, value, kind="requirement", label=None, limit=3000):
        text, truncated = _bounded(value, limit)
        items.append({"id": identity, "origin": origin, "kind": kind, "text": text,
                      "text_truncated": truncated, **({"label": label} if label else {}),
                      "mapping_status": "review_required", "mappings": []})

    if _nonempty(context.get("goal")):
        add("goal", "goal", context["goal"], limit=6000)
    questions = context.get("questions") if isinstance(context.get("questions"), list) else []
    question_by_id = {item["id"]: item for item in questions
                      if isinstance(item, dict) and isinstance(item.get("id"), str)}
    answers = context.get("answers") if isinstance(context.get("answers"), dict) else {}
    for identity, answer in list(answers.items())[:64]:
        if answer is not None and (not isinstance(answer, str) or answer.strip()):
            question = question_by_id.get(identity, {})
            add("answer:" + str(identity), "answers", answer, label=question.get("label"))
    for identity, question in list(question_by_id.items())[:64]:
        answer = answers.get(identity)
        if question.get("required") and (answer is None or isinstance(answer, str) and not answer.strip()):
            add("question:" + identity, "questions", question.get("label", identity), "unanswered_question")
    model = context.get("model")
    if isinstance(model, dict):
        # Public model facts are context, not inferred requirements or evidence
        # that a physical feature exists. Do not return local paths/parser data.
        for name, value in list(model.items())[:64]:
            if name not in {"path", "parser_detail"} and value is not None:
                add("model:" + str(name), "model", value, "model_fact", limit=1000)
    routes = design_brief.get("candidate_routes")
    if isinstance(routes, list):
        for index, route in enumerate(routes[:24]):
            if isinstance(route, dict) and route.get("status") in {"recommended", "conditional", "blocked", "selected"}:
                add("route:" + str(route.get("id", index)), "design_brief", route, "authored_route")
    return items


def _id_set(evidence, key):
    values = evidence.get(key)
    if values is None:
        return None
    if not isinstance(values, (list, tuple, set, frozenset)) or any(not isinstance(item, str) for item in values):
        raise ValueError(key + " must contain source IDs")
    return set(values)


def _validate_checklist(checklist):
    if checklist is None:
        return []
    if not isinstance(checklist, list) or len(checklist) > 64:
        raise ValueError("checklist must be an array of at most 64 authored mappings")
    properties = CHECKLIST_SCHEMA["items"]["properties"]
    required = set(CHECKLIST_SCHEMA["items"]["required"])
    for item in checklist:
        if not isinstance(item, dict) or not required <= set(item) or set(item) - set(properties):
            raise ValueError("checklist fields do not match the advisory mapping schema")
        for name, value in item.items():
            spec = properties[name]
            if spec["type"] == "array":
                if (not isinstance(value, list) or len(value) > 64
                        or any(not isinstance(identity, str) or not identity.strip() or len(identity) > 30000 for identity in value)):
                    raise ValueError("checklist ID lists must contain bounded nonempty strings")
            elif not isinstance(value, str) or not value.strip() or len(value) > spec.get("maxLength", 6000):
                raise ValueError("checklist text fields must be bounded nonempty strings")
        if item["status"] not in properties["status"]["enum"]:
            raise ValueError("unknown authored checklist status")
    return checklist


def review_plan(plan, *, context=None, design_brief=None, evidence=None, checklist=None):
    """Return non-blocking structural findings without mutating the draft.

    ``context`` must be the service's current public task context; ``evidence``
    must come from its trusted ledger, never a claim in the submitted plan.
    Omitted ledger keys mean unknown, while empty lists mean known-empty.
    The optional checklist refines inventory entries into authored requirements.
    It does not grant evidence credit or establish physical correctness.
    """
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    context = context if isinstance(context, dict) else {}
    design_brief = design_brief if isinstance(design_brief, dict) else context.get("design_brief", {})
    design_brief = design_brief if isinstance(design_brief, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    checklist = _validate_checklist(checklist)
    read_ids = _id_set(evidence, "read_source_ids")
    available_ids = _id_set(evidence, "available_source_ids")
    warnings, stages = [], []

    def warn(code, category, message, **where):
        warnings.append({"code": code, "category": category, "message": message, **where})

    def check_sources(identities, **where):
        if not isinstance(identities, list) or any(not isinstance(identity, str) or not identity.strip() for identity in identities):
            warn("invalid_source_list", "method_gap", "引用字段需要来源ID列表；本次审查不授予阅读记录。", **where)
            return
        unread = sorted(set(identities) - read_ids) if read_ids is not None else []
        unavailable = sorted(set(identities) - available_ids) if available_ids is not None else []
        if unread:
            warn("source_not_in_read_ledger", "method_gap", "引用不在提供的已读证据账本中，需实际读取后核对适用性。", source_ids=unread, **where)
        if unavailable:
            warn("source_unavailable", "method_gap", "引用来源当前不可用，不能据此新增已核实主张。", source_ids=unavailable, **where)

    raw_steps = plan.get("steps")
    steps = raw_steps if isinstance(raw_steps, list) else []
    if not steps:
        warn("no_implementation_steps", "implementation_check", "草稿尚无可审查的实施步骤。")
    seen = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            warn("invalid_step", "implementation_check", "实施步骤需要明确字段。", step_index=index)
            continue
        identity = step.get("id")
        fields = {name: _nonempty(step.get(name)) for name in ("purpose", "action", "location", "selection", "inputs", "checks")}
        missing = [name for name, present in fields.items() if not present]
        if missing:
            warn("implementation_fields_empty", "implementation_check", "这些实施字段尚未显式填写；请补充适用设置或说明不适用的理由。",
                 step_id=identity, fields=missing)
        dependencies = step.get("depends_on")
        if (not isinstance(identity, str) or not identity.strip() or identity in seen
                or not isinstance(dependencies, list) or any(not isinstance(dep, str) or dep not in seen for dep in dependencies)):
            warn("step_dependency_unresolved", "implementation_check", "步骤身份或先后依赖尚不明确。", step_id=identity)
        if isinstance(identity, str):
            seen.add(identity)
        check_sources(step.get("source_ids", []), step_id=identity)
        stages.append({"step_id": identity, "title": step.get("title", ""), "explicit_fields": fields,
                       "missing_fields": missing, "content_review": "agent_review_required",
                       "note": "字段非空只说明已填写，不证明域、表达式、单位、耦合、求解或结果定义正确。"})

    inventory = _inventory(context, design_brief)
    by_id = {item["id"]: item for item in inventory}
    for item in inventory:
        if item["kind"] == "unanswered_question":
            warn("public_question_unanswered", "unmet_public_input", "公开任务中的原有必答问题尚无回答；是否阻塞所选路线仍需判断。", requirement_id=item["id"])
    for index, item in enumerate(checklist):
        identity = item["requirement_id"]
        if identity not in by_id:
            warn("unknown_requirement_mapping", "implementation_check", "自检项没有对应的公开需求/模型事实/选定路线身份，请核对映射。",
                 requirement_id=identity, checklist_index=index)
            continue
        entry = dict(item)
        by_id[identity]["mappings"].append(entry)
        unknown_steps = sorted(set(item["step_ids"]) - seen)
        if unknown_steps:
            warn("unknown_step_mapping", "implementation_check", "需求映射指向不存在的实施步骤。", requirement_id=identity, step_ids=unknown_steps)
        if item["status"] in {"implemented", "conditional"} and not item["step_ids"]:
            warn("requirement_has_no_implementation_step", "implementation_check", "作者标为已实现或有条件实现，但尚未对应具体步骤。", requirement_id=identity)
        if item["status"] == "unresolved":
            warn("authored_requirement_unresolved", "implementation_check", "作者将此要求标为未解决；请说明是输入、方法依据还是实施检查，并保留其影响。", requirement_id=identity)
        check_sources(item.get("source_ids", []), requirement_id=identity, checklist_index=index)
    for item in inventory:
        mappings = item["mappings"]
        if mappings:
            statuses = {mapping["status"] for mapping in mappings}
            item["mapping_status"] = next(iter(statuses)) if len(statuses) == 1 else "mixed_authored_status"
    unreviewed = [item["id"] for item in inventory if not item["mappings"] and item["kind"] != "model_fact"]
    if unreviewed:
        warn("requirement_mapping_review_needed", "implementation_check", "这些公开要求尚无作者提供的逐项映射；不能据此判定方案未满足，请对照目标和输入自检。", requirement_ids=unreviewed)
    for decision in design_brief.get("decisions", []) if isinstance(design_brief.get("decisions"), list) else []:
        if isinstance(decision, dict):
            check_sources(decision.get("source_ids", []), decision_id=decision.get("id"))
    simplification = design_brief.get("simplification")
    if isinstance(simplification, dict) and isinstance(simplification.get("candidates"), list):
        for candidate in simplification["candidates"]:
            if isinstance(candidate, dict):
                check_sources(candidate.get("source_ids", []), simplification_id=candidate.get("id"))
    declared_gaps = context.get("evidence_gaps")
    if isinstance(declared_gaps, list):
        for gap in declared_gaps[:64]:
            if isinstance(gap, str) and gap.strip():
                text, truncated = _bounded(gap)
                warn("declared_method_gap", "method_gap", text, text_truncated=truncated)

    return {"schema": "plan-review/v1", "status": "advisory", "blocking": False,
            "physical_correctness": "not_evaluated", "source_applicability": "agent_review_required",
            "coverage_scope": "explicit_fields_authored_mappings_and_trusted_ledger",
            "requirement_inventory": inventory, "per_stage_coverage": stages, "warnings": warnings,
            "evidence_gaps": [item for item in warnings if item["category"] == "method_gap"],
            "input_gaps": [item for item in warnings if item["category"] == "unmet_public_input"],
            "implementation_checks": [item for item in warnings if item["category"] == "implementation_check"],
            "evidence_ledger_scope": {"read_source_ids_known": read_ids is not None, "available_source_ids_known": available_ids is not None},
            "review_questions": list(REVIEW_QUESTIONS),
            "next_action": "按公开目标、已给输入和选定路线逐项核对；有缺口时修订具体步骤或保留成立条件与证据缺口，再保存。审查提示不会阻止保存。"}
