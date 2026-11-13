"""Backend-neutral plan schemas and pure validation; no Agent runtime imports."""
from __future__ import annotations

import copy
import hashlib
import math
import re

STR = {"type": "string"}
STRINGS = {"type": "array", "items": STR}
SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
NORMALIZABLE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
ID_SCHEMA = {"type": "string", "pattern": SAFE_ID.pattern,
             "description": "稳定ID：小写英文字母开头，后续仅小写字母、数字、下划线或连字符，共1–64字符；同类对象内不得重复。"}
SIMPLIFICATION_ID_SCHEMA = {"type": "string", "pattern": NORMALIZABLE_ID.pattern,
    "description": "建议直接使用小写稳定ID；兼容ASCII大写并自动转小写，转换后必须符合 " + SAFE_ID.pattern + " 且不得重复。"}


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


QUERY_SCHEMA = _object({"query": STR, "kind": {"type": "string", "enum": ["all", "manual", "case", "code"]}, "reason": STR})
FORMULATION_SCHEMA = _object({
    "problem_summary": STR,
    "candidate_physics": {"type": "array", "items": _object({"name": STR, "reason": STR, "assumptions": STRINGS, "domain_hypothesis": STR})},
    "queries": {"type": "array", "items": QUERY_SCHEMA, "maxItems": 6},
    "geometry_unknowns": STRINGS,
})
QUESTION_SCHEMA = _object({
    "id": ID_SCHEMA, "label": STR, "kind": {"type": "string", "enum": ["text", "select"]},
    "options": {"type": "array", "items": _object({"value": STR, "label": STR})},
    "required": {"type": "boolean"}, "reason": STR,
})
QUESTION_SCHEMA["properties"]["id"] = dict(ID_SCHEMA, description="输入小写稳定ID；新增问题的最终ID由服务根据label生成，已有问题身份不可重定义。")
QUESTION_SCHEMA["properties"]["label"] = {"type": "string", "minLength": 1, "maxLength": 600, "pattern": r"\S"}
QUESTION_SCHEMA["properties"]["reason"] = {"type": "string", "minLength": 1, "maxLength": 1800, "pattern": r"\S"}
QUESTION_SCHEMA["properties"]["options"]["maxItems"] = 8
QUESTION_SCHEMA["properties"]["options"]["description"] = "kind=text时为空数组；kind=select时2至8项，value不得重复。"
QUESTION_SCHEMA["properties"]["options"]["items"]["properties"]["value"] = {
    "type": "string", "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}$", "maxLength": 64}
SIMPLIFICATION_SCHEMA = _object({
    "objective": STR, "protected_features": STRINGS,
    "candidates": {"type": "array", "maxItems": 8, "items": _object({
        "id": SIMPLIFICATION_ID_SCHEMA, "target": STR, "action": STR, "rationale": STR, "applicability": STR,
        "expected_benefit": STR, "lost_effects": STRINGS,
        "geometry_evidence": {**STRINGS, "description": "至少一条非空文本，说明几何依据或尚未检查/定位的限制；未知时明确写待检查范围。自由文字是候选解释，不能自证已观测几何；不可用空数组或纯空白占位。"},
        "geometry_fact_refs": STRINGS, "target_scope": {"type": "string", "enum": ["global", "body", "local_feature"]},
        "source_ids": {**STRINGS, "description": "仅引用实际读取的来源ID；几何检查后仍为conditional时必须至少一个。几何支持不足会先降为needs_inspection，此时可空；缺方法依据保留待检查，不得虚构来源。"},
        "verification": {**STRINGS, "description": "至少一条非空的待执行验证方法，例如对照目标量、守恒或简化前后差异；不是已通过验证的结论，不可用空数组或纯空白占位。"},
        "status": {"type": "string", "enum": ["conditional", "needs_inspection", "keep"]},
        "requires_confirmation": {"type": "boolean"},
    })},
    "unresolved_geometry": STRINGS,
})
SIMPLIFICATION_SCHEMA["properties"]["candidates"]["items"]["properties"]["status"]["description"] = (
    "conditional须有已读方法来源及当前快照中与范围匹配的已观察body数据；几何支持不足时自动降为needs_inspection。"
    "needs_inspection是待检查建议，不要求文档引用，但仍须说明依据/未知限制和验证计划；keep为保留。")
DESIGN_BRIEF_SCHEMA = _object({
    "objective": STR,
    "domain_strategy": STR,
    "candidate_routes": {"type": "array", "items": _object({
        "id": ID_SCHEMA, "name": STR, "physics": STRINGS, "rationale": STR,
        "assumptions": STRINGS, "required_inputs": STRINGS, "tradeoffs": STR,
        "status": {"type": "string", "enum": ["candidate", "recommended", "blocked"]},
    })},
    "decisions": {"type": "array", "items": _object({
        "id": ID_SCHEMA, "topic": STR, "recommendation": STR, "reason": STR,
        "alternatives": STRINGS, "source_ids": STRINGS, "requires_confirmation": {"type": "boolean"},
    })},
    "geometry_interpretation": {"type": "array", "items": _object({
        "statement": STR, "status": {"type": "string", "enum": ["observed", "hypothesis", "unknown"]}, "evidence": STR,
    })},
    "simplification": SIMPLIFICATION_SCHEMA,
})
DESIGN_BRIEF_SCHEMA["properties"]["candidate_routes"].update(minItems=1, maxItems=6)
for _field in ("objective", "domain_strategy"):
    DESIGN_BRIEF_SCHEMA["properties"][_field] = {"type": "string", "minLength": 1, "pattern": r"\S"}


def _check_shape(value, schema, where="result"):
    """Use the same shape validator as public and bound planning tools."""
    import json
    from .plan_drafts import schema_errors
    errors = schema_errors(value, schema)
    if errors:
        raise ValueError(where + " 字段不符合schema：" + json.dumps(errors, ensure_ascii=False))


def _label_key(label):
    return "".join(character for character in label.casefold() if character.isalnum())


def _safe_question_list(proposed, existing):
    """Return all existing definitions plus supplemental identity-stable fields."""
    existing = copy.deepcopy(existing or [])
    by_id = {item["id"]: item for item in existing if isinstance(item, dict) and isinstance(item.get("id"), str)}
    by_label = {_label_key(item.get("label", "")): item for item in existing if isinstance(item, dict) and item.get("label")}
    output = list(existing)
    output_ids = set(by_id)
    for index, item in enumerate(proposed):
        _check_shape(item, QUESTION_SCHEMA, f"questions[{index}]")
        if not SAFE_ID.fullmatch(item["id"]):
            raise ValueError(f"Agent 问题 ID 不安全或无效。questions[{index}].id 必须符合 {SAFE_ID.pattern}。")
        label, reason = item["label"].strip(), item["reason"].strip()
        if not label or len(label) > 600 or not reason or len(reason) > 1800:
            raise ValueError("Agent 问题缺少明确问题或理由。")
        old = by_id.get(item["id"]) or by_label.get(_label_key(label))
        if old:
            if item["label"] != old.get("label") or item["kind"] != old.get("kind") or item["options"] != old.get("options", []) or item["reason"] != old.get("reason"):
                raise ValueError("Agent 重新使用了已有问题身份，但改变了问题文字、类型、选项或量定义理由；已拒绝覆盖原会话。")
            # The compatible original definition is already in output, including
            # its required flag and reason. Saved answers retain their meaning.
            continue
        options = item["options"]
        if item["kind"] == "text" and options:
            raise ValueError("文本问题不能带预选或选项。")
        if item["kind"] == "select":
            if not 2 <= len(options) <= 8:
                raise ValueError("选择问题必须给出 2–8 个备选。")
            values = set()
            for option in options:
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}", option["value"]) or not option["label"].strip() or option["value"] in values:
                    raise ValueError("Agent 问题选项无效或重复。")
                values.add(option["value"])
        # New IDs depend on the question content, not the model's random ID or
        # call number. Reusing that question in subsequent rounds preserves it.
        qid = "rq_" + hashlib.sha256(_label_key(label).encode("utf-8")).hexdigest()[:16]
        if qid not in output_ids:
            output.append({"id": qid, "label": label, "kind": item["kind"], "options": copy.deepcopy(options), "required": item["required"], "reason": reason})
            output_ids.add(qid)
    return output


def _validated_retirements(proposed, questions, existing):
    by_id = {item.get("id"): item for item in existing or [] if isinstance(item, dict)}
    proposed_ids = {item["id"] for item in questions}
    proposed_labels = {_label_key(item["label"]) for item in questions}
    retired, seen = [], set()
    for item in proposed:
        qid, reason = item["id"], item["reason"].strip()
        if not SAFE_ID.fullmatch(qid) or qid.startswith("decision_") or qid not in by_id or qid in seen:
            raise ValueError("Agent 只能退役当前已有的非决策输入问题，且不能重复退役。")
        if not reason or len(reason) > 1800:
            raise ValueError("问题退役必须说明路线变化导致其不再适用的理由。")
        if qid in proposed_ids or _label_key(by_id[qid].get("label", "")) in proposed_labels:
            raise ValueError("Agent 不能在同一轮退役又重新提出同一个问题。")
        retired.append({"id": qid, "reason": reason})
        seen.add(qid)
    return retired


def geometry_reference_catalog(model):
    """Controller-only exact refs to actual snapshot data, never LLM prose.

    Current snapshots provide metadata, overall quantities and scoped bodies,
    not a face inventory. Facts do not gain finer scope because an explanation
    mentions a face, measurement API or STEP declaration number.
    """
    if not isinstance(model, dict):
        return []
    geometry = model.get("geometry") if isinstance(model.get("geometry"), dict) else {}
    caps = geometry.get("capabilities") if isinstance(geometry.get("capabilities"), dict) else {}
    identity = {"snapshot_id": model.get("id"), "fingerprint": model.get("fingerprint")}
    entries = []
    global_keys = {"cad.topology", "cad.bounding_box", "cad.volume_sum", "cad.body_summaries"}
    bad_statuses = {"not_implemented", "not_evaluated", "not_checked", "not_run", "unavailable", "unknown", "failed"}
    def unavailable(value):
        if isinstance(value, str):
            return value in bad_statuses
        if isinstance(value, dict):
            return any(unavailable(child) for child in value.values())
        if isinstance(value, list):
            return any(unavailable(child) for child in value)
        return False
    for fact in model.get("facts", []):
        if not isinstance(fact, dict) or not isinstance(fact.get("key"), str) or not fact["key"]:
            continue
        value = fact.get("value")
        available = value is not None and not unavailable(value)
        entries.append({"ref": "fact:" + fact["key"], "kind": "fact", "scope": "global" if fact["key"] in global_keys else "metadata",
                        **identity, "available": available, "observed_fields": [fact["key"]] if available else [],
                        "value": copy.deepcopy(value), "conditional_support": False})
    for body in geometry.get("bodies", []):
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not body["id"]:
            continue
        available = caps.get("snapshot_scoped_body_ids") == "observed" and bool(body.get("source"))
        bounds, volume = body.get("bounding_box"), body.get("volume")
        def finite_number(value):
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        bounds_observed = (isinstance(bounds, dict) and not unavailable(bounds) and bool(bounds.get("unit"))
                           and all(isinstance(bounds.get(key), list) and len(bounds[key]) == 3 and all(finite_number(value) for value in bounds[key]) for key in ("min", "max"))
                           and all(high >= low for low, high in zip(bounds["min"], bounds["max"]))
                           and caps.get("per_body_bounds") != "not_implemented")
        volume_observed = (isinstance(volume, dict) and not unavailable(volume) and bool(volume.get("unit"))
                           and finite_number(volume.get("value")) and volume["value"] >= 0
                           and caps.get("per_body_volume") != "not_implemented")
        observed_fields = [key for key in ("id", "kernel_dim_tag", "surface_count") if body.get(key) is not None] if available else []
        if available and bounds_observed:
            observed_fields.append("bounding_box")
        if available and volume_observed:
            observed_fields.append("volume")
        value = {"id": body["id"], "kernel_dim_tag": body.get("kernel_dim_tag"), "surface_count": body.get("surface_count")}
        for key in ("bounding_box", "volume"):
            item = body.get(key)
            value[key] = {name: copy.deepcopy(child) for name, child in item.items() if name != "source"} if isinstance(item, dict) else None
        entries.append({"ref": "body:" + body["id"], "kind": "body", "scope": "body", **identity,
                        "available": available, "observed_fields": observed_fields, "value": value,
                        "conditional_support": available and (bounds_observed or volume_observed)})
    # Duplicate fact keys are not an unambiguous API reference. Exclude all
    # collisions instead of selecting whichever value happened to occur last.
    counts = {}
    for entry in entries:
        counts[entry["ref"]] = counts.get(entry["ref"], 0) + 1
    return [entry for entry in entries if counts[entry["ref"]] == 1]


def validate_response(response, pool, context=None):
    brief = response["design_brief"]
    if not brief["objective"].strip() or not brief["domain_strategy"].strip() or not 1 <= len(brief["candidate_routes"]) <= 6:
        raise ValueError("Agent 设计需要目标、计算域策略和至少一条候选路线。")
    for key, label_key in (("candidate_routes", "name"), ("decisions", "topic")):
        seen = set()
        for index, item in enumerate(brief[key]):
            if not SAFE_ID.fullmatch(item["id"]) or item["id"] in seen or not item[label_key].strip():
                field = "id" if not SAFE_ID.fullmatch(item["id"]) or item["id"] in seen else label_key
                raise ValueError(f"Agent 设计路线/决定 ID 重复或无效。design_brief.{key}[{index}].{field}：ID必须符合 {SAFE_ID.pattern} 且同类不重复；{label_key}须为非空文本。")
            seen.add(item["id"])
    for interpretation in brief["geometry_interpretation"]:
        # Free-form model statements are never the authoritative geometry
        # observation channel. Exact catalog entries/model facts are.
        if interpretation["status"] == "observed":
            interpretation["status"] = "hypothesis"
        prefix = "Agent解释（非解析器观察）："
        if not interpretation["evidence"].startswith(prefix):
            interpretation["evidence"] = prefix + interpretation["evidence"]
    allowed = set(pool)
    citations = list(response["selected_source_ids"]) + [sid for item in brief["decisions"] for sid in item["source_ids"]]
    simplification = brief["simplification"]
    if not simplification["objective"].strip():
        raise ValueError("模型简化指导需要说明与本次目标量的关系。")
    if simplification["candidates"] and not any(item.strip() for item in simplification["protected_features"]):
        raise ValueError("提出简化候选前必须说明需要保护的目标敏感特征。")
    seen = set()
    catalog = {entry["ref"]: entry for entry in geometry_reference_catalog((context or {}).get("model"))}
    # Current source schema has no observed face/boundary/domain inventory.
    # Never accept numeric labels from prose, counts, raw STEP records or
    # substring matches (face99 cannot supply face9).
    entity_pattern = re.compile(r"(?<![a-z0-9_])(?:faces?|boundar(?:y|ies)|domains?|bod(?:y|ies))\s*(?:id|#|no\.?|number)?\s*[:=]?\s*\d+|(?:面|边界|邊界|域|实体|實體)\s*(?:编号|編號|#)?\s*[:：]?\s*\d+", re.I)
    body_pattern = re.compile(r"(?<![a-zA-Z0-9_-])body-[0-9a-f]{1,64}(?![a-zA-Z0-9_-])", re.I)
    for index, candidate in enumerate(simplification["candidates"]):
        where = f"design_brief.simplification.candidates[{index}]"
        if NORMALIZABLE_ID.fullmatch(candidate["id"]):
            candidate["id"] = candidate["id"].lower()
        if not SAFE_ID.fullmatch(candidate["id"]) or candidate["id"] in seen:
            raise ValueError(f"简化候选 ID 重复或无效。{where}.id：ASCII大小写输入归一为小写后须符合 {SAFE_ID.pattern}，且归一后不得重复。")
        seen.add(candidate["id"])
        if any(not candidate[key].strip() for key in ("target", "action", "rationale", "applicability", "expected_benefit")):
            raise ValueError("简化候选需要目标范围、措施、理由、前提及预期收益。")
        if not any(item.strip() for item in candidate["verification"]) or not any(item.strip() for item in candidate["geometry_evidence"]):
            empty_fields = [where + "." + key for key in ("geometry_evidence", "verification") if not any(item.strip() for item in candidate[key])]
            raise ValueError("简化候选需要几何事实/限制和待执行验证方法。以下字段至少需要一条非空文本：" + ", ".join(empty_fields) + "。")
        refs = list(dict.fromkeys(candidate["geometry_fact_refs"]))
        if not set(refs) <= set(catalog):
            raise ValueError("简化候选引用了当前快照白名单之外的几何事实/实体 ID。")
        candidate["geometry_fact_refs"] = refs
        target_text = candidate["target"] + " " + candidate["action"] + " " + " ".join(candidate["geometry_evidence"])
        if entity_pattern.search(target_text):
            raise ValueError("当前快照没有可核对的局部实体编号清单；简化候选必须使用精确 body ref 或待检查语义范围。")
        for body_id in body_pattern.findall(target_text):
            if "body:" + body_id not in refs or catalog["body:" + body_id]["scope"] != "body":
                raise ValueError("简化候选的 body ID 未以当前快照精确引用及对应范围提供。")
        body_refs = [catalog[ref] for ref in refs if catalog[ref]["scope"] == "body"]
        supported = candidate["target_scope"] == "body" and bool(body_refs) and all(item["conditional_support"] for item in body_refs)
        if candidate["status"] == "conditional" and not supported:
            candidate["status"] = "needs_inspection"
            candidate["requires_confirmation"] = True
            limitation = f"候选 {candidate['id']} 尚无与作用范围匹配的已观察几何数据；需检查定位/局部测量后再判断适用性。"
            if limitation not in simplification["unresolved_geometry"]:
                simplification["unresolved_geometry"].append(limitation)
        if candidate["status"] == "conditional" and not candidate["source_ids"]:
            raise ValueError(f"有条件采用的简化策略必须有已读来源支持。{where}.source_ids 在 status=conditional 时须至少包含一个实际已读来源ID；缺依据时保持needs_inspection，不得虚构引用。")
        prefix = "候选解释（非观察数据）："
        candidate["geometry_evidence"] = [text if text.startswith(prefix) else prefix + text for text in candidate["geometry_evidence"]]
        citations.extend(candidate["source_ids"])
    if not set(citations) <= allowed:
        raise ValueError("Agent 设计引用了控制器未实际读取的资料。")
    from .planner import validate_agent_plan
    validate_agent_plan(response["plan"], list(pool.values()))
