"""Small derived search/navigation views; authoritative source objects stay intact."""
from __future__ import annotations

import copy

_BODY_FIELDS = {"text", "code", "instructions", "model_snapshot", "raw_model_snapshot", "parser_detail", "raw", "raw_xml", "xml", "nodes"}
_DATA_FIELDS = {"feature_branch", "feature_branch_origin", "feature_type", "scope_hint", "language", "function", "function_calls",
    "objects_created", "properties_set", "operations", "api_object_paths", "references", "prerequisites", "unresolved",
    "summary", "summary_origin", "section", "tutorial_order", "instruction_count", "index_text_truncated", "source_order", "source_spans",
    "code_origin", "execution_status", "execution_order", "standalone", "workflow_history", "structure_version", "analysis_scope",
    "coverage", "parse_warnings", "extractor_version", "origin_trust"}
_CANDIDATE_FIELDS = {"id", "object_id", "local_id", "revision_id", "case_group", "title", "kind", "type", "version", "branch", "feature_branch",
    "package", "physics", "origin", "url", "source_uri", "library_id", "asset_key", "asset_hash", "relative_path", "section_path", "parent", "children",
    "score", "status", "quality", "read_scope", "text_truncated", "evidence", "coverage", "relations"}
_RELATED_FIELDS = {"id", "title", "kind", "type", "feature_branch", "category", "status", "state", "relation", "relation_type", "relation_label",
    "reason", "available", "library_id", "revision_id", "alignment"}


def _compact(value, depth=0):
    if isinstance(value, str):
        return value if len(value) <= 320 else value[:320] + "…"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if depth >= 4:
        return {"summary_omitted": True}
    if isinstance(value, list):
        return [_compact(item, depth + 1) for item in value[:12]] + ([{"remaining_items": len(value) - 12}] if len(value) > 12 else [])
    if isinstance(value, dict):
        eligible = [(key, item) for key, item in value.items() if key not in _BODY_FIELDS]
        result = {key: _compact(item, depth + 1) for key, item in eligible[:24]}
        if len(eligible) > 24:
            result["remaining_fields"] = len(eligible) - 24
        return result
    return str(value)[:320]


def candidate_view(source):
    """Search candidates carry bounded hints, never full code or model trees."""
    result = {key: _compact(value) for key, value in source.items() if key in _CANDIDATE_FIELDS}
    data = source.get("data") if isinstance(source.get("data"), dict) else {}
    if data:
        result["data"] = {key: _compact(value) for key, value in data.items() if key in _DATA_FIELDS}
    if source.get("kind") == "code" or source.get("type") == "implementation_snippet" or data.get("code"):
        summary = data.get("summary") or source.get("summary") or f"实现片段：{source.get('title', source.get('id', '未命名'))}；需读取正文核对。"
    else:
        summary = source.get("excerpt") or data.get("summary") or source.get("summary") or source.get("text") or ""
    result["excerpt"] = str(summary)[:700]
    # Scope is an actual retrieval contract, not a generated explanation. Keep
    # its exact fields, backend and generation while detaching mutable objects.
    if "retrieval" in source:
        result["retrieval"] = copy.deepcopy(source["retrieval"])
    result["candidate_only"] = True
    result["read_required"] = True
    return result


def related_sources_view(items):
    """Keep unresolved dependency hints as well as pinned IDs; no body reads."""
    return [{key: copy.deepcopy(value) for key, value in item.items() if key in _RELATED_FIELDS}
            for item in items if isinstance(item, dict) and (item.get("id") or item.get("status") == "unresolved")]


def source_context_view(source, *, available=True):
    """Pinned source identity, with no historical-body bypass for closed roots."""
    if not available:
        return {key: copy.deepcopy(source[key]) for key in ("id", "title", "revision_id", "library_id", "kind", "type", "version", "url", "source_uri") if key in source} | {
            "availability": {"readable": False, "state": "unavailable"}}
    result = candidate_view(source)
    result.pop("candidate_only", None)
    result["availability"] = {"readable": True}
    return result
