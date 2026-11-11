"""Shared feature categories; a library's origin branch is a separate field."""
from __future__ import annotations

import re
import hashlib
from pathlib import Path

FEATURE_BRANCHES = ("geometry", "mesh", "physics", "material", "multiphysics", "selection",
                    "study", "solver", "result", "param", "func", "variable", "misc")
_ALIASES = {"geom": "geometry", "sol": "solver", "globals": "misc", "initialization": "misc",
            "parameters": "param", "materials": "material", "results": "result", "studies": "study"}


def normalize_feature_branch(value):
    if value is None or value == "all":
        return None
    if not isinstance(value, str):
        raise ValueError("feature_branch must name a supported feature category")
    value = _ALIASES.get(value.strip().lower(), value.strip().lower())
    if value not in FEATURE_BRANCHES:
        raise ValueError("Unsupported feature_branch")
    return value


def object_feature_branch(item):
    """Recognize explicit metadata, legacy scope hints, then tutorial headings.

    Headings are category hints, not evidence of code/step correspondence.
    """
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    for candidate in (data.get("feature_branch"), item.get("feature_branch"), data.get("scope_hint")):
        if candidate is not None:
            try:
                return normalize_feature_branch(candidate)
            except ValueError:
                pass
    if item.get("type") != "method_step":
        return None
    heading = str(data.get("section") or item.get("title") or "")
    for pattern, branch in ((r"\bmesh\b|网格|網格", "mesh"), (r"\bgeometry\b|几何|幾何", "geometry"),
                            (r"\bmultiphysics\b|多物理", "multiphysics"),
                            (r"\bmaterials?\b|材料", "material"), (r"\bsolvers?\b|求解器", "solver"),
                            (r"\bstud(?:y|ies)\b|研究", "study"), (r"\bresults?\b|结果|結果", "result"),
                            (r"\bselections?\b|选择集|選擇集", "selection"),
                            (r"\bparameters?\b|参数|參數", "param"),
                            (r"\b(?:physics|heat transfer|solid mechanics|laminar flow|electrostatics)\b|物理场|物理場", "physics")):
        if re.search(pattern, heading, re.I):
            return branch
    return None


def link_case_objects(objects):
    """Link source objects without claiming a step-to-code alignment."""
    result = [dict(item, data=dict(item.get("data", {})), relations=list(item.get("relations", []))) for item in objects]
    case = next((item for item in result if item.get("type") == "model_case"), None)
    steps = [item for item in result if item.get("type") == "method_step"]
    contexts = [item for item in result if item.get("type") == "case_context"]
    for item in result:
        branch = object_feature_branch(item)
        if branch:
            if not item["data"].get("feature_branch"):
                item["data"]["feature_branch"] = branch
                item["data"]["feature_branch_origin"] = "tutorial_heading_rule" if item.get("type") == "method_step" else "legacy_scope_hint"
        if case and item is not case:
            item["relations"].append({"type": "same_case", "target_local_id": case["local_id"], "status": "same_source_asset"})
        if item.get("type") in {"model_case", "implementation_snippet", "method_step"}:
            item["relations"].extend({"type": "context", "target_local_id": context["local_id"], "status": "documented_not_model_validated"} for context in contexts[:4])
        if item.get("type") == "implementation_snippet" and branch:
            for step in [step for step in steps if object_feature_branch(step) == branch][:3]:
                item["relations"].append({"type": "documented_by", "target_local_id": step["local_id"],
                                          "status": "candidate_same_branch", "alignment": "not_verified"})
    return result


def tutorial_context_objects(path, blocks):
    """Preserve explanatory tutorial sections alongside the GUI instructions."""
    sections = []
    active = None
    names = ((r"^(?:Introduction|概述|介绍|簡介)$", "introduction"),
             (r"^(?:Model Definition|模型定义|模型定義)$", "model_definition"),
             (r"^(?:Results(?: and Discussion)?|结果(?:与讨论)?|結果(?:與討論)?)$", "results_discussion"),
             (r"^References?$|^参考文献$|^參考文獻$", "references"))
    for block in blocks:
        text = str(block.get("text", "")).strip()
        style = str(block.get("class", "")).lower()
        heading = style.startswith("head") or re.fullmatch(r"h[1-6]", str(block.get("tag", "")))
        if heading and re.search(r"model(?:l)?ing instructions|model instructions|建模说明|建模步[骤驟]", text, re.I):
            break
        name = next((name for pattern, name in names if heading and re.fullmatch(pattern, text, re.I)), None)
        if name:
            active = {"name": name, "title": text, "blocks": []}
            sections.append(active)
        elif active is not None and text:
            active["blocks"].append(block)
    result = []
    for section in sections:
        if not section["blocks"]:
            continue
        text = "\n".join(str(block["text"]) for block in section["blocks"])
        locators = ["html:#" + block["anchor"] if block.get("anchor") else f"html:line={block['line']}" for block in section["blocks"]]
        key = hashlib.sha256((section["name"] + ":" + locators[0]).encode()).hexdigest()[:20]
        result.append({"local_id": "context-" + key, "type": "case_context", "title": section["title"], "text": text,
                       "data": {"section": section["name"], "status": "documented", "model_agreement": "not_verified",
                                "scope": "Extracted tutorial text; figures and image-only equations are not interpreted"},
                       "evidence": [{"asset_path": str(Path(path).resolve()), "locator": locator,
                                     "method": "tutorial_html_block", "status": "documented"} for locator in locators],
                       "relations": [], "kind": "case"})
    return result
