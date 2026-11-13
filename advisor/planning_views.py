"""Small Agent response views; stored sources and public reads remain authoritative.

Only metadata is summarized here. A source page's body and paging contract, and
the actual retrieval contract, are copied without truncation or reinterpretation.
Typed configuration responses must not be passed through these metadata views.
"""
from __future__ import annotations

import copy
import json

from .knowledge_views import source_context, source_context_view

_IDENTITY_FIELDS = ("id", "title", "kind", "type", "library_id", "revision_id", "asset_key", "asset_hash")
_STATUS_FIELDS = ("status", "state", "read_scope", "text_truncated", "partial", "unresolved",
                  "source_validity", "model_agreement", "execution_status", "effective_activation",
                  "physical_validation", "model_completeness", "availability")
_CONTEXT_FIELDS = ("role", "applicability", "case_identity", "case_name", "package", "feature_branch",
                   "feature_type", "section", *_STATUS_FIELDS)
_SCALAR_HINTS = ("version", "package", "feature_branch", "feature_type", "section", "scope_hint",
                 "language", "standalone", "summary_origin", "origin_trust", "analysis_scope")
_COVERAGE_BRANCHES = ("raw_xml", "deep_api", "tutorial", "saved_features", "source_container",
                      "normalization", "properties", "selections", "traversal", "property_statuses",
                      "selection_statuses", "read_coverage")
_COVERAGE_FIELDS = ("status", "state", "partial", "unresolved", "failed", "unknown", "truncated",
                    "read_contract", "read_errors", "properties_unresolved", "properties_partial",
                    "omitted_nodes", "omitted_properties", "omitted_selections", "saved_status",
                    "api_status", "tutorial_status", "physical_validation", "effective_activation")
_REFERENCE_FIELDS = ("id", "source_id", "target_id", "target_local_id", "library_id", "revision_id",
                     "title", "type", "kind", "relation", "relation_type", "relation_label", "status",
                     "state", "available", "alignment", "reason", "feature_branch", "category", "path", "description")
_EXACT_REFERENCE_FIELDS = {"id", "source_id", "target_id", "target_local_id", "library_id", "revision_id"}
_PAGE_FIELDS = ("text", "offset", "total_characters", "next_offset", "length", "returned_characters")
_METADATA_LIMIT = 1800


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _hint(value, maximum=100):
    if isinstance(value, str):
        return value if len(value) <= maximum else value[:maximum - 1] + "…"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return None


def _count(items):
    """Understand the existing candidate view's trailing omission marker too."""
    return sum(item.get("remaining_items", 0) if isinstance(item, dict) and set(item) == {"remaining_items"}
               else 1 for item in items)


def _status(value):
    if isinstance(value, dict):
        return {key: _hint(value[key], 80) for key in ("status", "state", "readable", "valid", "partial", "unresolved",
                "classification", "basis", "reason", "saved_status", "api_status", "tutorial_status",
                "physical_validation", "effective_activation")
                if key in value and (value[key] is None or isinstance(value[key], (str, int, float, bool)))}
    if isinstance(value, (list, tuple)):
        return {"count": _count(value), "details_omitted": True}
    return _hint(value, 80)


def _coverage_summary(value, depth=0):
    if not isinstance(value, dict):
        return _status(value)
    result = {}
    for key in _COVERAGE_FIELDS:
        if key in value:
            result[key] = _status(value[key])
    if depth < 2:
        for key in _COVERAGE_BRANCHES:
            if key in value and isinstance(value[key], dict):
                summary = _coverage_summary(value[key], depth + 1)
                if summary:
                    result[key] = summary
    omitted = len(set(value) - set(result))
    if omitted:
        result["omitted_fields"] = omitted
    budget = 650 if depth == 0 else 240
    if _size(result) > budget:
        issues = set()

        def find_issues(item):
            if not isinstance(item, dict):
                return
            for key, child in item.items():
                if isinstance(child, dict):
                    find_issues(child)
                elif key == "observed_issues" and isinstance(child, list):
                    issues.update(child)
                elif isinstance(child, str) and child in {"partial", "unresolved", "failed", "unavailable", "preview_only"}:
                    issues.add(child)
                elif key in {"partial", "unresolved", "truncated", "properties_unresolved", "properties_partial"} and child:
                    issues.add("partial" if key == "properties_partial" else "unresolved" if key == "properties_unresolved" else key)

        find_issues(result)
        trimmed = {"details_omitted": True}
        if issues:
            trimmed["observed_issues"] = sorted(issues)
        for key, child in result.items():
            if not _add_bounded(trimmed, key, child, budget) and isinstance(child, dict) and "status" in child:
                _add_bounded(trimmed, key, {"status": child["status"], "details_omitted": True}, budget)
        return trimmed
    return result


def _context(source):
    data = source.get("data") if isinstance(source.get("data"), dict) else {}
    original = source.get("source_context")
    original = original if isinstance(original, dict) else source_context(source)
    result = {}
    for key in _CONTEXT_FIELDS:
        if key in original:
            value = original[key]
        elif key in _STATUS_FIELDS and key in source:
            value = source[key]
        elif key in _STATUS_FIELDS and key in data:
            value = data[key]
        else:
            continue
        # Case identity is a navigation identity, not a display hint.
        result[key] = copy.deepcopy(value) if key == "case_identity" and isinstance(value, str) else _status(value)
    physics = original.get("physics")
    if isinstance(physics, list):
        result["physics"] = [_hint(item, 60) for item in physics[:3] if isinstance(item, str)]
        if len(physics) > 3:
            result["physics_omitted"] = len(physics) - 3
    return result


def _reference(item):
    if isinstance(item, str):
        return item  # Exact dependency IDs must remain usable as tool inputs.
    if not isinstance(item, dict):
        return None
    result = {}
    for key in _REFERENCE_FIELDS:
        if key not in item:
            continue
        value = item[key]
        if key in _EXACT_REFERENCE_FIELDS and isinstance(value, str):
            result[key] = value
        elif value is None or isinstance(value, (str, int, float, bool)):
            result[key] = _hint(value, 72)
    return result or None


def _add_bounded(result, key, value, limit):
    if _size(result) + _size({key: value}) <= limit:
        result[key] = value
        return True
    return False


def _navigation(result, key, items, *, limit, maximum=4):
    if not isinstance(items, (list, tuple)):
        return
    total = _count(items)
    shown = []
    for item in items:
        reference = _reference(item)
        if reference is None or isinstance(item, dict) and set(item) == {"remaining_items"}:
            continue
        if len(shown) >= maximum:
            break
        proposed = shown + [reference]
        # Reserve room for the count so a cut list never looks complete.
        if _size(result) + _size({key: proposed, key + "_omitted": total - len(proposed)}) > limit:
            break
        shown = proposed
    result[key] = shown
    if total > len(shown):
        result[key + "_omitted"] = total - len(shown)


def _metadata(source, *, limit):
    result = {key: copy.deepcopy(source[key]) for key in _IDENTITY_FIELDS if key in source}
    result["source_context"] = _context(source)
    if "retrieval" in source:
        # Backend, generation and all scope fields describe actual retrieval.
        result["retrieval"] = copy.deepcopy(source["retrieval"])
    result["metadata_scope"] = "bounded_summary"
    data = source.get("data") if isinstance(source.get("data"), dict) else {}

    # Preserve status/validity before optional descriptive hints/navigation.
    for key in _STATUS_FIELDS:
        if key in source:
            result[key] = _status(source[key])
    coverage = source.get("coverage", data.get("coverage"))
    if coverage is not None:
        summary = _coverage_summary(coverage)
        if not _add_bounded(result, "coverage", summary, limit):
            # Branch status must remain explicit even if all counters will not fit.
            result["coverage"] = {key: ({"status": value["status"]} if isinstance(value, dict) and "status" in value else value)
                                  for key, value in summary.items() if key in {"status", "state", "partial", "unresolved", "truncated", "raw_xml", "deep_api", "tutorial"}}
            result["coverage"]["details_omitted"] = True
    evidence = source.get("evidence")
    if isinstance(evidence, (list, tuple)):
        _add_bounded(result, "evidence_summary", {"count": _count(evidence), "details_omitted": bool(evidence)}, limit)

    # Dependency/navigation IDs outrank duplicate descriptive metadata.
    for key in ("related_sources", "dependency_ids", "source_ids", "case_context_source_ids", "relations",
                "linked_topics", "children"):
        if key in source:
            _navigation(result, key, source[key], limit=limit)
    if "parent" in source:
        parent = _reference(source["parent"])
        if parent is not None and not _add_bounded(result, "parent", parent, limit):
            result["parent_omitted"] = True
    for key in ("prerequisites", "references", "unresolved", "dependency_ids", "source_ids", "case_context_source_ids"):
        if isinstance(data.get(key), (list, tuple)) and key not in result:
            _navigation(result, key, data[key], limit=limit, maximum=3)
    for key in _SCALAR_HINTS:
        value = source.get(key, data.get(key))
        if value is not None and isinstance(value, (str, int, float, bool)):
            _add_bounded(result, key, _hint(value), limit)
    for key in ("related_sources_warning", "linked_topics_warning"):
        if key in source:
            _add_bounded(result, key, _hint(source[key], 160), limit)
    return result


def candidate_for_agent(source):
    """Bound search metadata and excerpts without treating a candidate as read."""
    availability = source.get("availability")
    if isinstance(availability, dict) and availability.get("readable") is False:
        # Historical citations retain identity only. Do not recreate content
        # fields (even an empty excerpt) after the live permission filter.
        return source_context_view(source, available=False)
    data = source.get("data") if isinstance(source.get("data"), dict) else {}
    if source.get("kind") == "code" or source.get("type") == "implementation_snippet" or data.get("code"):
        excerpt = data.get("summary") or source.get("summary") or "Implementation snippet; read the source to verify."
    else:
        excerpt = source.get("excerpt") or data.get("summary") or source.get("summary") or source.get("text") or ""
    result = _metadata(source, limit=1100)
    result["excerpt"] = _hint(excerpt, 400) if isinstance(excerpt, str) else ""
    result["candidate_only"] = True
    result["read_required"] = True
    return result


def source_page_for_agent(response):
    """Keep all requested body text and exact pagination before compact metadata.

    The size limit applies to optional metadata; exact source identities and the
    retrieval contract are never rewritten to meet it. Bodies/configuration
    values are not an appropriate place for a metadata serialization budget.
    """
    body = {key: copy.deepcopy(response[key]) for key in _PAGE_FIELDS if key in response}
    return body | _metadata(response, limit=_METADATA_LIMIT)


def library_scope_for_agent(scope):
    """Show the selectable catalog once; groups reference IDs instead of roots.

    Counts and dependency maps describe the original scope. A large catalog's
    display is explicitly limited; the existing query/type filters can narrow
    it without guessing library IDs or treating a partial listing as empty.
    """
    fields = ("schema", "library_count", "enabled_library_count", "global_library_count", "matched_library_count",
              "filter_applied", "query", "type", "query_match_mode", "next_action")
    result = {key: copy.deepcopy(scope[key]) for key in fields if key in scope}
    libraries = scope.get("libraries", [])
    libraries = libraries if isinstance(libraries, list) else []
    shown = []
    for library in libraries[:32]:
        if not isinstance(library, dict):
            continue
        item = {key: copy.deepcopy(library[key]) for key in ("id", "type", "enabled", "object_count") if key in library}
        for key in ("name", "version"):
            if key in library:
                item[key] = _hint(library[key], 160 if key == "name" else 80)
        roots = library.get("roots")
        if isinstance(roots, list):
            item["root_count"] = len(roots)
        # Capability counts are useful scope hints; capability trees are not.
        for key in ("capability_counts", "object_counts", "source_counts"):
            counts = library.get(key)
            if isinstance(counts, dict):
                item[key] = {name: value for name, value in list(counts.items())[:12]
                             if isinstance(value, (int, float, bool))}
                if len(counts) > len(item[key]):
                    item[key + "_omitted"] = len(counts) - len(item[key])
        shown.append(item)
    result["libraries"] = shown
    if len(libraries) > len(shown):
        result["libraries_omitted"] = len(libraries) - len(shown)
        result["view_next_action"] = "Narrow get_library_scope with query or type to inspect omitted libraries."
    shown_ids = {item.get("id") for item in shown}

    def group_view(group, depth=0):
        group_result = {key: _hint(group[key], 100) for key in ("id", "name") if key in group}
        members = group.get("libraries", group.get("library_ids"))
        if isinstance(members, list):
            identities = [item.get("id") if isinstance(item, dict) else item for item in members]
            selected = [identity for identity in identities if identity in shown_ids]
            group_result["library_ids"] = selected
            if len(identities) > len(selected):
                group_result["libraries_omitted"] = len(identities) - len(selected)
        groups = group.get("groups")
        if isinstance(groups, list):
            group_result["groups"] = [group_view(item, depth + 1) for item in groups[:8]
                                      if isinstance(item, dict)] if depth < 3 else []
            if len(groups) > len(group_result["groups"]):
                group_result["groups_omitted"] = len(groups) - len(group_result["groups"])
        return group_result

    groups = scope.get("groups")
    if isinstance(groups, list):
        result["groups"] = [group_view(item) for item in groups[:8] if isinstance(item, dict)]
        if len(groups) > len(result["groups"]):
            result["groups_omitted"] = len(groups) - len(result["groups"])
    enabled = scope.get("enabled_library_ids")
    if isinstance(enabled, list):
        result["enabled_library_ids"] = [identity for identity in enabled if identity in shown_ids]
        if len(enabled) > len(result["enabled_library_ids"]):
            result["enabled_library_ids_omitted"] = len(enabled) - len(result["enabled_library_ids"])
    for key in ("case_context_dependencies", "case_context_disabled_dependencies"):
        if key in scope:
            result[key] = copy.deepcopy(scope[key])
    result["metadata_scope"] = "bounded_summary"
    return result
