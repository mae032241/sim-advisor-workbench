"""Opt-in, lossless tool-result navigation for the route-card experiment.

This adapter does not retrieve, index, or interpret knowledge. It calls the
supplied reader and keeps an object-local copy of each result. Projections are
labelled, and every omitted field remains accessible by JSON pointer. A JSON
fragment never grants a whole source/property reference.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from threading import Lock


MAX_PAGE_CHARACTERS = 1200
_IDENTITY = ("id", "source_id", "case_id", "title", "type", "kind", "library_id", "revision_id")
_SOURCE_FIELDS = _IDENTITY + ("text", "offset", "length", "returned_characters", "total_characters",
    "next_offset", "evidence_refs", "source_context", "read_scope", "text_truncated", "configuration_hint")
_CONFIG_FIELDS = ("case_id", "source", "source_context", "partial", "truncated", "read_scope",
    "extraction_coverage", "coverage_read_scope", "physical_validation", "effective_activation",
    "total", "total_nodes", "matched", "available", "note")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _bytes(value):
    # Match the runtime's actual inputText serialization, including spaces.
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _pointer(value, path):
    if not isinstance(path, str) or path and not path.startswith("/"):
        raise ValueError("path must be an exact JSON pointer")
    for token in path.split("/")[1:] if path else []:
        if re.search(r"~(?![01])", token):
            raise ValueError("Invalid JSON pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if token not in value:
                raise ValueError("Unknown JSON pointer field")
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            if int(token) >= len(value):
                raise ValueError("JSON pointer index is out of range")
            value = value[int(token)]
        else:
            raise ValueError("JSON pointer does not select a value")
    return value


def _decode(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as error:
            raise ValueError("Tool returned invalid JSON") from error
    # Some read-only tools return arrays. Preserve their exact contents under an
    # explicit envelope rather than silently coercing them into an empty object.
    if isinstance(value, list):
        value = {"result": value, "result_type": "array"}
    if not isinstance(value, dict):
        raise ValueError("Tool result must be a JSON object or array")
    try:
        return json.loads(_json(value))
    except (TypeError, ValueError) as error:
        raise ValueError("Tool returned a non-JSON result") from error


class BoundedKnowledge:
    """Bound existing read calls without changing their provider or source data.

    ``observer(original_tool, effective_arguments, delivered_result)`` is a
    trusted integration hook, including for cached pages. It receives only the
    result actually delivered to the model. ``authorize(tool, arguments)`` is
    called before every cached read and should recheck the current source gate.
    Neither hook is provided by model arguments. ``audit`` contains delivery
    sizes/scopes, not an authoritative CaseMethodBuilder receipt ledger.
    """
    def __init__(self, read_call, max_bytes=7000, *, observer=None, authorize=None):
        if not callable(read_call):
            raise TypeError("read_call must be callable")
        if type(max_bytes) is not int or max_bytes < 1024:
            raise ValueError("max_bytes must be an integer of at least 1024")
        self.read_call = read_call
        self.max_bytes = max_bytes
        self.observer = observer
        self.authorize = authorize
        self.audit = []
        self._results = {}
        self._result_lock = Lock()
        self._result_counter = 0

    def tool_definitions(self):
        return [{"name": "read_tool_page", "description":
            "Read a retained tool result by its handle and exact JSON pointer. Default pages contain original serialized JSON, not a summary. Follow next_offset until null to reconstruct the subtree. For a configuration /nodes/N/properties array only, offset counts properties and a structured page grants references only for complete displayed properties. JSON fragments grant no whole-property/source evidence. Cached pages recheck current authorization.",
            "inputSchema": {"type": "object", "additionalProperties": False,
                "required": ["handle"], "properties": {
                    "handle": {"type": "string", "minLength": 1},
                    "path": {"type": "string", "default": ""},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "length": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_CHARACTERS,
                               "default": MAX_PAGE_CHARACTERS}}}}]

    def call(self, name, arguments):
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise ValueError("A tool call needs a name and an argument object")
        if name == "read_tool_page":
            return self._read_page(arguments)
        args = deepcopy(arguments)
        # Constructor contracts accept only tool/path; do not invent a request
        # field that their original strict validator rejects. Large complete
        # schemas still use our bounded JSON result pages below.
        if name in {"read_source", "get_plan_draft"} or name == "get_tool_contract" and "length" in args:
            length = args.get("length", MAX_PAGE_CHARACTERS)
            if type(length) is not int or length <= 0:
                raise ValueError("length must be a positive integer")
            args["length"] = min(length, MAX_PAGE_CHARACTERS)
        if name == "read_case_configuration":
            args.setdefault("limit", 1)
        result = _decode(self.read_call(name, args))
        with self._result_lock:
            self._result_counter += 1
            handle = "tool-page-" + str(self._result_counter)
            self._results[handle] = {"tool": name, "arguments": args, "raw": result}
        if name == "read_source":
            view = self._source(handle)
        elif name in {"search_cases", "search_knowledge"}:
            view = self._search(handle)
        elif name == "get_case_guide":
            view = self._guide(handle)
        elif name == "read_case_configuration" and isinstance(result.get("nodes"), list):
            view = self._configuration(handle)
        else:
            view = deepcopy(result) if _bytes(result) <= self.max_bytes else self._fragment(handle, "", 0, MAX_PAGE_CHARACTERS)
        if _bytes(view) > self.max_bytes:
            view = self._fragment(handle, "", 0, MAX_PAGE_CHARACTERS)
        return self._deliver(handle, view)

    def _entry(self, handle):
        if not isinstance(handle, str) or handle not in self._results:
            raise ValueError("Unknown tool-page handle")
        return self._results[handle]

    def _origin(self, handle):
        entry = self._entry(handle)
        # The actual complete arguments stay private for authorization/observer.
        # These fields identify read scope without echoing a large write payload.
        return {"origin_tool": entry["tool"], "origin_arguments": {
            key: deepcopy(value) for key, value in entry["arguments"].items()
            if key in {"id", "case_id", "source", "node_path", "category", "stage", "offset", "length", "limit", "tool", "path"}}}

    def _link(self, handle, path="", offset=0):
        return {"tool": "read_tool_page", "arguments": {"handle": handle, "path": path, "offset": offset}}

    def _projection(self, view, handle, omitted):
        if omitted:
            view["metadata_view"] = {"kind": "projection", "omitted_fields": omitted,
                                     "original": self._link(handle)}
        return view

    def _source(self, handle):
        raw = self._entry(handle)["raw"]
        view = {key: deepcopy(raw[key]) for key in _SOURCE_FIELDS if key in raw}
        text, offset = raw.get("text"), raw.get("offset")
        if isinstance(text, str) and type(offset) is int:
            expected = {"source_id": raw.get("id", raw.get("source_id")), "context": "source_text",
                        "locator": f"text:{offset}:{offset + len(text)}"}
            view["evidence_refs"] = [expected] if expected in raw.get("evidence_refs", []) else []
        else:
            view["evidence_refs"] = []
        omitted = [key for key in raw if key not in view]
        if view["evidence_refs"] != raw.get("evidence_refs", []):
            omitted.append("evidence_refs (outside displayed text range)")
        self._projection(view, handle, omitted)
        # Never shorten a real body and leave its next_offset/ref unchanged.
        # If unusually large identity/coverage metadata does not fit, page the
        # entire original result instead, granting no body read from a fragment.
        if _bytes(view) <= self.max_bytes:
            return view
        core = {key: deepcopy(raw[key]) for key in _SOURCE_FIELDS
                if key in raw and key not in {"source_context", "configuration_hint"}}
        core["evidence_refs"] = deepcopy(view["evidence_refs"])
        self._projection(core, handle, [key for key in raw if key not in core])
        return core if _bytes(core) <= self.max_bytes else self._fragment(handle, "", 0, MAX_PAGE_CHARACTERS)

    def _search(self, handle):
        raw = self._entry(handle)["raw"]
        if not isinstance(raw.get("results"), list):
            return deepcopy(raw)
        view = {key: deepcopy(value) for key, value in raw.items() if key != "results"}
        candidates = []
        for index, candidate in enumerate(raw["results"][:6]):
            if not isinstance(candidate, dict):
                candidates.append(deepcopy(candidate))
                continue
            keep = _IDENTITY + ("excerpt", "source_context", "read_scope", "text_truncated", "candidate_only", "read_required", "score")
            item = {key: deepcopy(candidate[key]) for key in keep if key in candidate}
            excerpt = item.get("excerpt")
            if isinstance(excerpt, str) and len(excerpt) > 300:
                item["excerpt"] = excerpt[:300]
                item["summary_view"] = {"kind": "excerpt_preview", "is_source_text": False,
                                        "original": self._link(handle, f"/results/{index}")}
            elif any(key not in keep for key in candidate):
                item["metadata_view"] = {"kind": "projection", "original": self._link(handle, f"/results/{index}")}
            item["detail"] = {"tool": "read_source", "arguments": {"id": candidate["id"]}} if candidate.get("id") else self._link(handle, f"/results/{index}")
            candidates.append(item)
        view["results"] = candidates
        while len(candidates) > 3 and _bytes(view) > self.max_bytes - 500:
            candidates.pop()
        omitted = len(raw["results"]) - len(candidates)
        if omitted:
            view["has_more"] = True
            view["remaining_result_count"] = omitted
            view["remaining_results"] = self._link(handle, "/results")
        else:
            view.setdefault("has_more", False)
        if view != raw:
            view["metadata_view"] = {"kind": "candidate_projection", "is_source_text": False,
                                     "original": self._link(handle)}
        return view

    def _guide(self, handle):
        entry = self._entry(handle)
        raw = entry["raw"]
        if not isinstance(raw.get("stages"), list):
            return deepcopy(raw)
        view = {key: deepcopy(raw[key]) for key in ("case_id", "state", "overview", "is_read_evidence", "detail_scope", "detail") if key in raw}
        view["is_read_evidence"] = False
        view["stages"] = []
        for index, stage in enumerate(raw["stages"]):
            if not isinstance(stage, dict):
                continue
            head = {key: deepcopy(stage[key]) for key in ("id", "title", "source_count", "setting_count", "total_node_count", "selected_node_count", "summary_truncated") if key in stage}
            head["detail"] = self._link(handle, f"/stages/{index}")
            view["stages"].append(head)
        return self._projection(view, handle, [key for key in raw if raw.get(key) != view.get(key)])

    def _node_identity(self, node, handle, index):
        result = {key: deepcopy(value) for key, value in node.items() if key != "properties"}
        if _bytes(result) > min(1500, self.max_bytes // 3):
            result = {key: deepcopy(node[key]) for key in ("id", "path", "name", "tag", "type", "label", "category") if key in node}
            result["metadata_view"] = {"kind": "node_identity", "original": self._link(handle, f"/nodes/{index}")}
        return result

    def _property_page(self, handle, node_index, offset, length):
        entry = self._entry(handle)
        raw = entry["raw"]
        node = raw["nodes"][node_index]
        properties = node.get("properties", [])
        if not isinstance(properties, list):
            raise ValueError("Configuration properties must be an array")
        if offset > len(properties):
            raise ValueError("Property offset is out of range")
        view = {key: deepcopy(raw[key]) for key in _CONFIG_FIELDS if key in raw}
        small_node = self._node_identity(node, handle, node_index)
        small_node["properties"] = []
        view.update(self._origin(handle))
        view.update({"nodes": [small_node], "returned": 1, "handle": handle,
                     "path": f"/nodes/{node_index}/properties", "offset": offset,
                     "offset_unit": "property_index", "property_count": len(properties),
                     "evidence_refs": [], "delivered_evidence_refs": []})
        end, used = offset, 0
        deferred = []
        while end < len(properties):
            prop = properties[end]
            size = len(_json(prop))
            if size > length:
                deferred.append({"property_index": end, "reason": "property_requires_json_pages",
                                 "original": self._link(handle, f"/nodes/{node_index}/properties/{end}")})
                end += 1
                break
            if used + size > length:
                break
            small_node["properties"].append(deepcopy(prop))
            used += size
            end += 1
            if _bytes(view) > self.max_bytes - 1000:
                small_node["properties"].pop()
                end -= 1
                break
        # A tiny caller budget may not fit even the first otherwise normal
        # property. It remains reachable by its exact original JSON pointer.
        if end == offset and end < len(properties):
            deferred.append({"property_index": end, "reason": "property_requires_json_pages",
                             "original": self._link(handle, f"/nodes/{node_index}/properties/{end}")})
            end += 1
        if deferred:
            view["deferred_properties"] = deferred
        view["next_offset"] = end if end < len(properties) else None
        view["partial"] = bool(raw.get("partial") or offset or end < len(properties) or deferred)
        view["delivery_partial"] = bool(offset or end < len(properties) or deferred)
        view["property_range"] = {"start": offset, "end": end, "complete_displayed": len(small_node["properties"])}
        if end < len(properties):
            view["next_page"] = self._link(handle, view["path"], end)
        refs = self._configuration_refs(entry, small_node)
        view["evidence_refs"] = refs
        view["delivered_evidence_refs"] = deepcopy(refs)
        omitted = [key for key in raw if key not in view and key != "nodes"]
        self._projection(view, handle, omitted + (["nodes/properties (see property_range)"] if view["delivery_partial"] else []))
        return view

    @staticmethod
    def _configuration_refs(entry, node):
        raw = entry["raw"]
        identity, path = raw.get("case_id"), node.get("path")
        mode = entry["arguments"].get("source", "saved")
        if not isinstance(identity, str) or not isinstance(path, str) or not path:
            return []
        base = {"source_id": identity, "context": "case_configuration", "locator": mode + ":" + path}
        if base not in raw.get("evidence_refs", []):
            # Property locators without the service-issued node ref grant no
            # credit, matching the existing private constructor ledger.
            for prop in node.get("properties", []):
                if isinstance(prop, dict):
                    prop.pop("evidence_locator", None)
            return []
        refs = [base]
        for prop in node.get("properties", []):
            locator = prop.get("evidence_locator") if isinstance(prop, dict) else None
            if isinstance(locator, str) and locator.startswith(base["locator"] + "::property/"):
                refs.append(dict(base, locator=locator))
        return refs

    def _configuration(self, handle):
        raw = self._entry(handle)["raw"]
        if not raw["nodes"]:
            return deepcopy(raw)
        if not isinstance(raw["nodes"][0], dict):
            return self._fragment(handle, "", 0, MAX_PAGE_CHARACTERS)
        view = self._property_page(handle, 0, 0, MAX_PAGE_CHARACTERS)
        if len(raw["nodes"]) > 1:
            view["remaining_nodes"] = [self._link(handle, f"/nodes/{index}/properties")
                                       for index in range(1, len(raw["nodes"]))]
            view["delivery_partial"] = True
        return view

    def _fragment(self, handle, path, offset, length):
        entry = self._entry(handle)
        text = _json(_pointer(entry["raw"], path))
        if offset > len(text):
            raise ValueError("JSON page offset is out of range")
        end = min(len(text), offset + length)
        view = {"handle": handle, "path": path, "format": "serialized_json", "offset": offset,
                "offset_unit": "unicode_character", "total_characters": len(text),
                "text": text[offset:end], "next_offset": end if end < len(text) else None,
                "partial": bool(offset or end < len(text)), "delivery_partial": bool(offset or end < len(text)),
                "is_read_evidence": False, "delivered_evidence_refs": [],
                "source_range": {"json_pointer": path, "start": offset, "end": end},
                "metadata_view": {"kind": "original_json_page", "whole_reference_credit": False},
                **self._origin(handle)}
        # Unicode and JSON escaping both affect bytes. Shrink a *declared page*
        # with a truthful next_offset, never silently truncate a source body.
        while _bytes(view) > self.max_bytes and end > offset:
            end = offset + (end - offset) // 2
            view.update(text=text[offset:end], next_offset=end if end < len(text) else None,
                        partial=bool(offset or end < len(text)), delivery_partial=bool(offset or end < len(text)))
            view["source_range"]["end"] = end
        if _bytes(view) > self.max_bytes:
            # An unusually long source identity in arguments is still available
            # in the cached original result, and is passed intact to both hooks.
            view.pop("origin_arguments", None)
        if _bytes(view) > self.max_bytes or end == offset and offset < len(text):
            raise ValueError("Tool page metadata cannot fit max_bytes; increase the budget or use a shorter pointer")
        return view

    def _read_page(self, arguments):
        if set(arguments) - {"handle", "path", "offset", "length"} or "handle" not in arguments:
            raise ValueError("Invalid read_tool_page arguments")
        handle = arguments["handle"]
        entry = self._entry(handle)
        path, offset, length = arguments.get("path", ""), arguments.get("offset", 0), arguments.get("length", MAX_PAGE_CHARACTERS)
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= MAX_PAGE_CHARACTERS:
            raise ValueError("Invalid tool page range")
        _pointer(entry["raw"], path)  # Validate before dispatching special views.
        if self.authorize is not None:
            self.authorize(entry["tool"], deepcopy(entry["arguments"]))
        match = re.fullmatch(r"/nodes/(0|[1-9][0-9]*)/properties", path)
        if entry["tool"] == "read_case_configuration" and match:
            view = self._property_page(handle, int(match[1]), offset, length)
            if _bytes(view) > self.max_bytes:
                raise ValueError("Configuration identity exceeds page budget; use a specific property JSON pointer")
        else:
            view = self._fragment(handle, path, offset, length)
        return self._deliver(handle, view)

    def _deliver(self, handle, view):
        entry = self._entry(handle)
        if _bytes(view) > self.max_bytes:
            raise ValueError("Tool result exceeds delivery budget")
        refs = view.get("delivered_evidence_refs", view.get("evidence_refs", []))
        self.audit.append({"tool": entry["tool"], "arguments": deepcopy(entry["arguments"]),
            "handle": handle, "bytes": _bytes(view), "max_bytes": self.max_bytes,
            "delivered_evidence_refs": deepcopy(refs), "source_range": deepcopy(view.get("source_range")),
            "delivery_partial": bool(view.get("delivery_partial") or view.get("metadata_view")),
            "source_text_delivery_complete": (view.get("text") == entry["raw"].get("text")
                and view.get("format") != "serialized_json") if entry["tool"] == "read_source" else None,
            "source_text_truncated": entry["raw"].get("text_truncated"),
            "source_read_scope": deepcopy(entry["raw"].get("read_scope"))})
        if self.observer is not None:
            self.observer(entry["tool"], deepcopy(entry["arguments"]), deepcopy(view))
        return deepcopy(view)
