"""Bounded decoding of explicitly stored COMSOL XML property representations.

No expressions are evaluated. A decoded saved value is not an API getter type,
an effective setting, or evidence that the user explicitly supplied the value.
Unknown structures, references and ambiguous sentinels retain their raw data.
"""
from __future__ import annotations

from collections import Counter
import json
from itertools import islice
import math
import re
from xml.sax.saxutils import escape

NORMALIZER_VERSION = "mph-saved-values/v1"
_NAMED = {"param", "propertyValue", "expressions"}
_RAW = _NAMED | {"NodeDefaultValues", "active", "entityFlags", "storedParent", "base", "geom", "identifier", "study", "solType"}
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_ROWS = re.compile(r"(\d{1,12})\|(.*)\Z", re.S)
_COUNTED = re.compile(r"(\d{1,12})(?:,(.*))?\Z", re.S)


def _kind(element):
    return str(element.tag).rsplit("}", 1)[-1]


def _clip(value, budget):
    value = str(value or "")
    return value[:budget], len(value) > budget


def _raw_xml(element, budget):
    """An XML-equivalent bounded prefix, without serializing a huge subtree."""
    output, used, truncated = [], 0, False
    stack = [("node", element)]
    while stack and used < budget:
        action, value = stack.pop()
        if action == "node":
            tag = str(value.tag)
            pieces = ["<" + tag]
            for name, attr in value.attrib.items():
                pieces.extend([" " + str(name) + '="', escape(str(attr)[:budget], {'"': "&quot;"}), '"'])
                if sum(len(piece) for piece in pieces) > budget:
                    truncated = True
                    break
            pieces.append(">")
            stack.append(("text", "</" + tag + ">"))
            # Do not allocate a list proportional to a hostile child count.
            children = list(islice(value, budget))
            if len(value) > len(children):
                truncated = True
            for child in reversed(children):
                if child.tail:
                    stack.append(("escaped", child.tail))
                stack.append(("node", child))
            if value.text:
                stack.append(("escaped", value.text))
            stack.append(("text", "".join(pieces)))
            continue
        chunk = escape(str(value)[:budget]) if action == "escaped" else str(value)
        remaining = budget - used
        output.append(chunk[:remaining])
        used += min(len(chunk), remaining)
        truncated = truncated or len(chunk) > remaining or len(str(value)) > budget
    return "".join(output), truncated or bool(stack)


def _raw(element, max_text, max_items):
    attributes, truncated = {}, False
    for index, (name, value) in enumerate(element.attrib.items()):
        if index >= max_items:
            truncated = True
            break
        name, cut_name = _clip(name, max_text)
        value, cut_value = _clip(value, max_text)
        attributes[name] = value
        truncated = truncated or cut_name or cut_value
    text, cut = _clip(element.text, max_text)
    xml, cut_xml = _raw_xml(element, max_text)
    return {"element": str(element.tag)[:max_text], "attributes": attributes, "text": text,
            "raw_xml": xml, "truncated": truncated or cut or cut_xml}


def _tree(element, max_items, max_text, depth=0, budget=None):
    if budget is None:
        budget = {"nodes": max_items, "fields": max_items, "characters": max_text}
    budget["nodes"] -= 1
    def take(value):
        value = str(value or "")
        result = value[:max(0, budget["characters"])]
        budget["characters"] -= len(result)
        return result, len(result) < len(value)
    attributes, truncated = {}, False
    for name, value in element.attrib.items():
        if budget["fields"] <= 0:
            truncated = True
            break
        budget["fields"] -= 1
        name, cut_name = take(name)
        value, cut_value = take(value)
        attributes[name] = value
        truncated = truncated or cut_name or cut_value
    text, cut = take(element.text)
    children = []
    if depth < 8:
        for index, child in enumerate(element):
            if index >= max_items or budget["nodes"] <= 0:
                truncated = True
                break
            children.append(_tree(child, max_items, max_text, depth+1, budget))
    elif len(element):
        truncated = True
    return {"element": str(element.tag)[:max_text], "attributes": attributes, "text": text,
            "children": children, "children_total": len(element),
            "truncated": truncated or cut or any(child["truncated"] for child in children)}


def _cells(text, limit):
    """Read quoted cells, unescaping ONLY reserved serialization delimiters."""
    values, issues, cursor = [], [], 0
    while cursor < len(text):
        if len(values) >= limit:
            return values, [*issues, "cell_limit"], True
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor == len(text):
            break
        if text[cursor] != "'":
            return values, [*issues, "expected_quoted_cell"], False
        cursor += 1
        cell, closed = [], False
        while cursor < len(text):
            ch = text[cursor]
            if ch == "\\" and cursor+1 < len(text) and text[cursor+1] in {"\\", "'", ",", "|"}:
                cell.append(text[cursor+1])
                cursor += 2
            elif ch == "'":
                cursor += 1
                closed = True
                break
            else:
                cell.append(ch)
                cursor += 1
        if not closed:
            return values, [*issues, "unterminated_quoted_cell"], False
        values.append("".join(cell))
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor < len(text):
            if text[cursor] != ",":
                return values, [*issues, "unexpected_data_after_cell"], False
            cursor += 1
            if cursor == len(text):
                return values, [*issues, "trailing_delimiter"], False
    return values, issues, False


def _encoded(raw, max_cells, *, matrix):
    if matrix:
        return _matrix(raw, max_cells)
    match = _COUNTED.fullmatch(raw)
    if not match:
        return {"value": None, "status": "unresolved", "value_type": "unknown", "issues": ["unsupported_serialized_shape"]}
    count, body = int(match[1]), match[2]
    values, issues, limited = _cells(body or "", max_cells)
    if not limited and len(values) != count:
        issues.append("declared_cell_count_mismatch")
    status = "partial" if issues or limited else "decoded"
    result = {"value": values,
              "status": status, "value_type": "array", "declared_entries": count,
              "entries_observed": len(values), "entries_complete": not issues and not limited, "issues": issues,
              "encoding": "counted_quoted_cells", "truncated": limited}
    return result


def _matrix(raw, max_cells):
    """R|C1,'row1'|C2,'row2': each row has its own explicit count.

    Ragged row lengths occur in saved files. Only the serialized row grouping
    is claimed, never a numerical/API column-major or row-major interpretation.
    """
    match = _ROWS.fullmatch(raw)
    if not match:
        return {"value": None, "status": "unresolved", "value_type": "unknown", "issues": ["unsupported_serialized_shape"]}
    declared_rows, body = int(match[1]), match[2]
    segments, beginning, cursor, quoted, limited = [], 0, 0, False, False
    while cursor < len(body):
        ch = body[cursor]
        if ch == "\\" and cursor+1 < len(body) and body[cursor+1] in {"\\", "'", ",", "|"}:
            cursor += 2
            continue
        if ch == "'":
            quoted = not quoted
        elif ch == "|" and not quoted:
            segments.append(body[beginning:cursor])
            beginning = cursor + 1
            if len(segments) >= max_cells:
                limited = True
                break
        cursor += 1
    if not limited:
        segments.append(body[beginning:])
    rows, widths, issues, cells = [], [], [], []
    for segment in segments:
        row = _COUNTED.fullmatch(segment)
        if not row:
            issues.append("unsupported_row_encoding")
            break
        width = int(row[1])
        values, row_issues, cut = _cells(row[2] or "", max(0, max_cells-len(cells)))
        if not cut and len(values) != width:
            row_issues.append("declared_cell_count_mismatch")
        rows.append(values)
        widths.append(width)
        cells.extend(values)
        issues.extend(row_issues)
        if cut:
            limited = True
            break
    if not limited and len(rows) != declared_rows:
        issues.append("declared_row_count_mismatch")
    rectangular = bool(widths) and len(set(widths)) == 1 and len(rows) == declared_rows
    return {"value": {"shape": [declared_rows, widths[0] if rectangular else None], "rows": rows,
                      "row_lengths": widths, "entries": cells, "entry_order": "serialized", "layout": "not_verified"},
            "status": "partial" if issues or limited else "decoded", "value_type": "matrix",
            "declared_entries": sum(widths) if len(rows) == declared_rows else None,
            "declared_rows": declared_rows, "rows_observed": len(rows), "entries_observed": len(cells),
            "entries_complete": not issues and not limited, "issues": issues,
            "encoding": "counted_rows_of_quoted_cells", "truncated": limited}


def _scalar(raw):
    if raw in {"true", "false"}:
        return {"value": raw == "true", "value_type": "boolean", "status": "decoded", "issues": []}
    if raw in {"NaN", "Inf", "+Inf", "-Inf", "Infinity", "+Infinity", "-Infinity"}:
        return {"value": {"numeric_status": "non_finite", "representation": raw}, "value_type": "tagged_number", "status": "decoded", "issues": []}
    if _NUMBER.fullmatch(raw):
        try:
            if not re.search(r"[.eE]", raw):
                if len(raw.lstrip("+-")) > 128:
                    raise ValueError("Integer size limit")
                return {"value": int(raw), "value_type": "integer", "status": "decoded", "issues": []}
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("Numeric range limit")
            return {"value": value, "value_type": "number", "status": "decoded", "issues": []}
        except (ValueError, OverflowError):
            return {"value": {"numeric_status": "out_of_range", "representation": raw}, "value_type": "tagged_number", "status": "partial", "issues": ["numeric_range_not_representable"]}
    return {"value": raw, "value_type": "string", "status": "decoded", "issues": []}


def _decode(element, max_items, max_text):
    kind = _kind(element)
    fields = {name: str(element.get(name))[:max_text] for name in ("value", "valueMatrix", "ValueRows", "Reference", "ReferenceArray", "ReferenceContainerList", "expr") if element.get(name) is not None}
    cut = any(len(str(element.get(name))) > max_text for name in fields)
    raw, selected = None, None
    if kind == "expressions" and "expr" in fields:
        raw, selected = fields["expr"], "expr"
        decoded = {"value": raw, "value_type": "expression", "status": "decoded", "issues": []}
    elif "valueMatrix" in fields:
        raw, selected = fields["valueMatrix"], "valueMatrix"
        decoded = _encoded(raw, max_items, matrix=True)
    elif "value" in fields:
        raw, selected = fields["value"], "value"
        if kind == "param":
            if raw == "0":
                decoded = {"value": raw, "value_type": "ambiguous_zero_marker", "status": "unresolved", "issues": ["zero_may_encode_an_empty_parameter_array_not_numeric_zero"]}
            else:
                decoded = _encoded(raw, max_items, matrix=True)
                if decoded["status"] == "unresolved":
                    decoded.update(value=raw, value_type="string")
                if decoded["status"] == "decoded" and decoded.get("declared_entries") == 1:
                    decoded["encoded_shape"] = decoded["value"]["shape"]
                    decoded["value"], decoded["value_type"] = decoded["value"]["entries"][0], "string"
        else:
            decoded = _scalar(raw)
    elif "ValueRows" in fields or "ReferenceArray" in fields:
        selected = "ValueRows" if "ValueRows" in fields else "ReferenceArray"
        raw = fields[selected]
        decoded = _encoded(raw, max_items, matrix=False)
        decoded["value_type"] = "row_values" if selected == "ValueRows" else "reference_array"
    elif len(element):
        first = element[0]
        if len(element) == 1 and _kind(first).lower() == "null" and not first.attrib and not len(first) and not (first.text or "").strip():
            decoded = {"value": None, "value_type": "null", "status": "decoded", "issues": []}
        else:
            tree = _tree(element, max_items, max_text)
            selection = len(element) == 1 and _kind(first) == "Selection"
            decoded = {"value": tree, "value_type": "selection_structure" if selection else "xml_structure",
                       "status": "partial" if selection else "unresolved", "truncated": tree["truncated"],
                       "issues": ["selection_topology_and_effective_binding_not_resolved" if selection else "unsupported_child_structure_retained"]}
        selected = "child_xml"
    elif (element.text or "").strip() == "null":
        raw, selected = "null", "text"
        decoded = {"value": None, "value_type": "null", "status": "decoded", "issues": []}
    elif "Reference" in fields:
        raw, selected = fields["Reference"], "Reference"
        decoded = {"value": {"reference": raw, "container": fields.get("ReferenceContainerList")}, "value_type": "reference",
                   "status": "partial", "issues": ["reference_target_and_getter_value_not_resolved"]}
    else:
        raw, selected = (element.text or "")[:max_text], "text"
        decoded = {"value": raw or None, "value_type": "unknown", "status": "unresolved", "issues": ["no_supported_value_payload"]}
    decoded.update(raw_value=raw, raw_fields=fields, selected_representation=selected,
                   type_basis="saved_syntax_not_API_getter_type", representation_consistency="not_validated" if len(set(fields) & {"value", "valueMatrix", "ValueRows"}) > 1 else "single_representation")
    if cut:
        decoded.update(status="partial", truncated=True, issues=[*decoded["issues"], "value_text_limit"])
    return decoded


def normalize_properties(element, *, max_items=500, max_text=16000):
    """Normalize direct saved properties, preserving bounded legacy raw fields.

    Matrix entries remain in serialization order with the declared shape. No
    unverified row/column layout, units, references or expressions are evaluated.
    ``status`` concerns decoding only, never simulation validity or activation.
    """
    if type(max_items) is not int or not 0 <= max_items <= 10000 or type(max_text) is not int or not 1 <= max_text <= 1024 * 1024:
        raise ValueError("Property limits require max_items 0..10000 and max_text 1..1048576")
    properties, raw_properties, total, raw_total = [], [], 0, 0
    unsupported = Counter()
    for position, child in enumerate(element, 1):
        kind = _kind(child)
        if kind in _RAW:
            raw_total += 1
            if len(raw_properties) < max_items:
                raw_properties.append(_raw(child, max_text, max_items))
        if kind not in _NAMED:
            continue
        total += 1
        if len(properties) >= max_items:
            continue
        raw = _raw(child, max_text, max_items)
        original = child.get("param") if kind == "param" else child.get("name")
        original, cut_name = _clip(original, max_text)
        name = original[2:] if kind == "propertyValue" and original.startswith("p:") else original
        decoded = _decode(child, max_items, max_text)
        if not name:
            decoded.update(status="unresolved", issues=[*decoded["issues"], "missing_property_name"])
        if raw["truncated"] or cut_name:
            decoded["truncated"] = True
            if decoded["status"] == "decoded":
                decoded["status"] = "partial"
        for nested in child:
            if _kind(nested) not in {"Selection", "null", "Null"}:
                unsupported[_kind(nested)] += 1
        attribute = "param" if kind == "param" else "name"
        item = {**raw, **decoded, "name": name or None, "original_name": original or None, "name_prefix_removed": kind == "propertyValue" and original.startswith("p:"),
                "source_kind": kind, "locator": f"child[{position}]/{kind}[@{attribute}={json.dumps(original, ensure_ascii=False)}]",
                "value_origin": "saved_value_not_proven_effective_or_user_explicit", "effective_activation": "not_evaluated"}
        item["truncated"] = bool(raw["truncated"] or decoded.get("truncated") or cut_name)
        properties.append(item)
    return {"properties": properties, "total": total, "truncated": total > len(properties) or raw_total > len(raw_properties) or any(p["truncated"] for p in properties) or any(p["truncated"] for p in raw_properties),
            "unresolved_count": sum(p["status"] == "unresolved" for p in properties), "partial_count": sum(p["status"] == "partial" for p in properties),
            "unread_count": total - len(properties), "raw_properties": raw_properties, "raw_total": raw_total,
            "unsupported_child_kinds": dict(unsupported), "normalizer_version": NORMALIZER_VERSION,
            "limits": {"max_items": max_items, "max_text": max_text, "max_cells": max_items, "max_structure_depth": 8},
            "coverage": "bounded_saved_encoding_only", "API_types_validated": False, "expressions_evaluated": False}
