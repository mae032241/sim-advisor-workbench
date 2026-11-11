"""Bounded saved-selection syntax normalization, without COMSOL/API execution.

Encoding evidence is a static observation of the installed COMSOL 6.3 classes:
SelectionPrim$SelectionPrimXmlReader calls
ModelCustomDataTypeConverter.createCsBitSetFromString for explicit/@entities.
That method's bytecode offsets 11..54 select one header token for a nonnegative
first integer, or two for a negative first integer; offsets 55..79 copy the rest
to CsBitSet.a(int[]). CsBitSet stores those values shifted by its offset.
No JAR is read at runtime, and this is not a guarantee for every file version.

Decoded values are saved CsBitSet payloads. Named/object/remaining selections
are never evaluated into geometry membership. In particular dim != hDim must
not be compared as if the payload were already mapped to the requested dim.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

SCHEMA = "mph-saved-selections/v1"
ENCODING = "comsol63-csbitset-string-v1"
FORMAT_EVIDENCE = {
    "scope": "Static COMSOL 6.3 saved-format observation; other versions not guaranteed",
    "reader": "com.comsol.model.data.primitive.SelectionPrim$SelectionPrimXmlReader",
    "converter": "com.comsol.model.util.ModelCustomDataTypeConverter.createCsBitSetFromString",
    "converter_class_sha256": "3abde297dae4a2f7db311800b9c9dfd83cb8dac488aad89464da9f5e6f7f655c",
    "bitset_class_sha256": "07e64280a070a7ae1400dcd97c1d53a058a81c0c027af989377668acdc42137c",
}
_INTEGER = re.compile(r"[+-]?\d+\Z")
_CSV = re.compile(r"\s*[+-]?\d+(?:\s*,\s*[+-]?\d+)*\s*\Z")


def _tag(element):
    return element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""


class _Budget:
    def __init__(self, items, text):
        self.items, self.text = items, text
        self.truncated = False

    def take(self, count=1):
        taken = min(count, self.items)
        self.items -= taken
        self.truncated |= taken < count
        return taken

    def capture(self, text):
        text = str(text or "")
        result = text[:self.text]
        cut = len(result) != len(text)
        self.text -= len(result)
        self.truncated |= cut
        return result, cut


def _attributes(element, budget):
    result = {}
    for index, (key, value) in enumerate(element.attrib.items()):
        if index >= 32:
            budget.truncated = True
            break
        name, _ = budget.capture(str(key)[:128])
        result[name], _ = budget.capture(value)
    return result


def _xml_prefix(element, maximum):
    """Do not materialize an unbounded ElementTree serialization just to clip it."""
    parts, size, cut, visited = [], 0, False, 0

    def emit(value):
        nonlocal size, cut
        available = maximum - size
        if len(value) > available:
            cut = True
        value = value[:max(0, available)]
        parts.append(value)
        size += len(value)

    def visit(node, depth=0):
        nonlocal visited, cut
        if size >= maximum or depth > 12 or visited >= 1000:
            cut = True
            return
        visited += 1
        emit("<" + str(node.tag)[:128])
        for index, (key, value) in enumerate(node.attrib.items()):
            if index >= 32 or size >= maximum:
                cut = True
                break
            emit(" " + str(key)[:128] + "=" + quoteattr(str(value)[:maximum]))
        emit(">")
        if node.text:
            emit(escape(node.text[:maximum]))
            cut |= len(node.text) > maximum
        for child in node:
            if size >= maximum:
                cut = True
                break
            visit(child, depth + 1)
            if child.tail:
                emit(escape(child.tail[:maximum]))
                cut |= len(child.tail) > maximum
        emit("</" + str(node.tag)[:128] + ">")

    visit(element)
    return "".join(parts), cut


def _dimension(value):
    if isinstance(value, str) and len(value) <= 16 and _INTEGER.fullmatch(value.strip()):
        number = int(value)
        if -1 <= number <= 3:
            return number
    return None


def _integers(text, *, empty=False):
    if empty and not text.strip():
        return []
    if not _CSV.fullmatch(text):
        raise ValueError("not a comma-separated integer sequence")
    result = [int(value.strip()) for value in text.split(",")]
    if any(value < -2147483648 or value > 2147483647 for value in result):
        raise ValueError("integer outside the saved Java int range")
    return result


def _decode_explicit(element, selection, budget):
    raw, cut = budget.capture(element.get("entities", ""))
    geom, geom_cut = budget.capture(element.get("geom", ""))
    dim, high = _dimension(element.get("dim")), _dimension(element.get("hDim"))
    result = {"status": "unresolved", "raw_encoded": raw, "entity_dimension": dim, "highest_dimension": high,
              "encoded_entity_dimension": high if high is not None else dim,
              "dimension_relation": "unknown" if dim is None or high is None else "same" if dim == high else "different",
              "geometry_ref": geom or None, "membership_status": "not_evaluated", "truncated": cut or geom_cut}
    if "entities" not in element.attrib:
        result["reason"] = "No saved entities attribute; absence does not prove empty/all selection"
        return result
    if cut:
        result["reason"] = "Encoded entities exceeded the text budget; no partial stream is decoded"
        return result
    if selection.get("T") != "32" or selection.get("selType") != "GEOMDIM":
        result["reason"] = "Selection context does not match the observed 6.3 CsBitSet format"
        return result
    try:
        tokens = _integers(raw)
        if tokens[0] >= 0:
            span, offset, header = tokens[0], 0, 1
        else:
            if len(tokens) < 2 or tokens[0] == -2147483648:
                raise ValueError("negative-span encoding needs a valid offset header")
            span, offset, header = -tokens[0], tokens[1], 2
        lower, upper = -offset, span - offset
        if lower < -2147483648 or upper > 2147483647 or upper < lower:
            raise ValueError("encoding header exceeds supported Java int bounds")
        payload = tokens[header:]
        if any(value < lower or value >= upper for value in payload):
            raise ValueError("serialized member is outside the header's bitset bounds")
        values = sorted(set(payload))
    except (ValueError, IndexError) as exc:
        result["reason"] = str(exc)
        return result
    count = budget.take(len(values))
    result.update(status="decoded_saved" if count == len(values) and not geom_cut else "partial", encoding=ENCODING,
                  decoded_entities=values[:count], decoded_count=len(values), serialized_value_count=len(payload),
                  encoding_header={"header_tokens": tokens[:header], "span": span, "offset": offset,
                                   "lower_bound": lower, "upper_bound_exclusive": upper},
                  truncated=count != len(values) or geom_cut)
    return result


def _remaining(element, budget):
    raw, cut = budget.capture(element.text or "")
    result = {"raw_encoded": raw, "groups": [], "status": "unresolved", "truncated": cut}
    if cut or not re.fullmatch(r"\s*\{[^{}]*\}(?:\s*,\s*\{[^{}]*\})*\s*", raw):
        result["reason"] = "Remaining groups are truncated or do not match saved brace-group syntax"
        return result
    try:
        groups = [(match.group(0), _integers(match.group(1), empty=True)) for match in re.finditer(r"\{([^{}]*)\}", raw)]
    except ValueError as exc:
        result["reason"] = str(exc)
        return result
    for index, (encoded, values) in enumerate(groups):
        if not budget.take():
            result["truncated"] = True
            break
        count = budget.take(len(values))
        group_raw, raw_cut = budget.capture(encoded)
        result["groups"].append({"group_index": index, "raw_encoded": group_raw, "values": values[:count],
            "saved_count": len(values), "entity_dimension": None, "status": "saved_group_not_final_membership",
            "truncated": count != len(values) or raw_cut})
        result["truncated"] |= count != len(values) or raw_cut
    result["status"] = "partial" if result["truncated"] else "observed_structure"
    return result


def _entry(selection, budget):
    result = {"element": _tag(selection), "attributes": {}, "status": "observed_structure", "explicit": [],
              "named_references": [], "output_references": [], "remaining": [], "objects": [], "base": [],
              "unresolved_reasons": [], "truncated": False, "membership_status": "not_evaluated"}
    if _tag(selection) == "outputSelection":
        value, cut = budget.capture((selection.text or "").strip())
        if value:
            result["output_references"].append(value)
        else:
            result["unresolved_reasons"].append("Output selection reference is absent")
        result["truncated"] |= cut
    else:
        for child in selection:
            if not budget.take():
                result["truncated"] = True
                break
            kind = _tag(child)
            if kind == "explicit":
                result["explicit"].append(_decode_explicit(child, selection, budget))
            elif kind == "named":
                value, cut = budget.capture((child.text or "").strip())
                if value:
                    result["named_references"].append(value)
                else:
                    result["unresolved_reasons"].append("Named reference is absent")
                result["truncated"] |= cut
            elif kind == "remaining":
                remaining = _remaining(child, budget)
                result["remaining"].extend(remaining.pop("groups"))
                result["remaining_raw_encoded"] = remaining["raw_encoded"]
                if remaining["status"] == "unresolved":
                    result["unresolved_reasons"].append(remaining["reason"])
                result["truncated"] |= remaining["truncated"]
            elif kind in {"object", "base"}:
                geometry_ref, geom_cut = budget.capture(child.get("geom", ""))
                feature_ref, feature_cut = budget.capture(child.get("feature", ""))
                record = {"attributes": _attributes(child, budget), "geometry_ref": geometry_ref or None,
                          "feature_ref": feature_ref or None, "entity_dimension": _dimension(child.get("dim")),
                          "status": "observed_object_reference" if kind == "object" else "observed_base"}
                result["truncated"] |= geom_cut or feature_cut
                if child.get("assocId") is not None:
                    raw, cut = budget.capture(child.get("assocId"))
                    association = {"raw_encoded": raw, "status": "unresolved", "truncated": cut}
                    try:
                        if cut or not raw.startswith("{") or not raw.endswith("}"):
                            raise ValueError("Unrecognized association tuple")
                        values = _integers(raw[1:-1], empty=True)
                        count = budget.take(len(values))
                        association.update(values=values[:count], status="observed_identifier_tuple", truncated=count != len(values))
                    except ValueError:
                        pass
                    record["association_id"] = association
                    result["truncated"] |= association["truncated"]
                result["objects" if kind == "object" else "base"].append(record)
            else:
                result["unresolved_reasons"].append("Unrecognized direct selection child: " + kind[:80])
    result["attributes"] = _attributes(selection, budget)
    raw, cut = _xml_prefix(selection, budget.text)
    result["raw_xml"], _ = budget.capture(raw)
    result["truncated"] |= cut or any(item.get("truncated") for item in result["explicit"])
    reasons = [item.get("reason") for item in result["explicit"] if item.get("status") == "unresolved"]
    result["unresolved_reasons"].extend(reason for reason in reasons if reason)
    if not any(result[key] for key in ("explicit", "named_references", "output_references", "remaining", "objects", "base")):
        result["unresolved_reasons"].append("No recognized saved selection representation")
    if result["truncated"]:
        result["status"] = "partial"
    elif result["unresolved_reasons"]:
        result["status"] = "unresolved"
    elif result["explicit"] and all(item["status"] == "decoded_saved" for item in result["explicit"]):
        result["status"] = "decoded_saved"
    return result


def normalize_selections(element, *, max_items=500, max_text=16000):
    """Normalize this selection, or only a feature's direct selection children."""
    if not isinstance(element, ET.Element):
        raise ValueError("element must be an ElementTree Element")
    if type(max_items) is not int or not 1 <= max_items <= 10000 or type(max_text) is not int or not 1 <= max_text <= 1000000:
        raise ValueError("Selection limits must be bounded positive integers")
    budget = _Budget(max_items, max_text)
    entries = []
    candidates = (element,) if _tag(element) in {"selection", "outputSelection"} else element
    for candidate in candidates:
        if _tag(candidate) not in {"selection", "outputSelection"}:
            continue
        if not budget.take():
            break
        entries.append(_entry(candidate, budget))
    statuses = {entry["status"] for entry in entries}
    truncated = budget.truncated or any(entry["truncated"] for entry in entries)
    status = ("not_present" if not entries else "partial" if truncated or ("unresolved" in statuses and len(statuses) > 1)
              else "unresolved" if statuses == {"unresolved"} else "decoded_saved" if statuses == {"decoded_saved"} else "observed_structure")
    return {"schema": SCHEMA, "status": status, "entries": entries, "truncated": truncated,
            "limits": {"max_items": max_items, "max_text": max_text}, "membership_evaluated": False,
            "geometry_executed": False, "format_evidence": dict(FORMAT_EVIDENCE)}
