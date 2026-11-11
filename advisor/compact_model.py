"""Read one MPH into compact relational facts; never persist expanded state.

The complete saved XML is returned once as bytes. Typed facts retain decoding
gaps and exact XML routes, without repeated raw trees, previews or provenance.
No COMSOL process, expression evaluation or physical validation is performed.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from .build_control import check_cancelled
from .library_model import _SAVED_BRANCHES, _SECTION_TAGS, _tutorial, _tutorial_reference
from .model_reader import ModelInspectionError, _safe_container
from .mph_saved_selections import FORMAT_EVIDENCE, SCHEMA as SELECTION_SCHEMA, normalize_selections
from .mph_saved_values import NORMALIZER_VERSION, normalize_properties

PARSER_VERSION = "compact-mph-parser/v1"
# Solved MPH files can contain large binary solution payloads. This reader
# streams their hash and reads only the independently bounded ZIP metadata;
# it never opens those solution members or feeds the whole file to simparse.
MAX_CONTAINER_BYTES = 1024 * 1024 * 1024
MAX_NODES = 5000
MAX_PROPERTIES = 500
MAX_TEXT = 16000
MAX_VALUE_CELLS = 64
MAX_VALUE_CHARACTERS = 2048
_NAMED_PROPERTIES = {"param", "propertyValue", "expressions"}
_SELECTIONS = {"selection", "outputSelection"}
# These exact kinds belong to saved view/display configuration. Uncertain
# numerical/result kinds are retained, including their unknown properties.
_GUI_KINDS = {"ViewList", "View", "ViewFeatureList", "ViewFeature",
              "HideDrawList", "HideGeomList", "HideMeshList", "ResultFeature", "ResultFeatureList",
              "ColorTableFeature", "ColorTableFeatureList"}
_GUI_SUBTREES = _GUI_KINDS - {"ResultFeature", "ResultFeatureList"}
_NODE_COLUMNS = {"tag", "name", "op"}
_ATTRIBUTE_METADATA = {"created", "modified", "expanded", "collapsed", "selected", "windowid"}
_FUNCTIONAL_KINDS = set(_SAVED_BRANCHES) - {"ResultFeature", "ExportFeature"}
_FUNCTIONAL_KINDS.update({"PhysicsField", "Frame", "FrameFeature", "Coordsys", "MeshSelectionFeature",
                          "ProbeFeature", "EvaluationGroupFeature", "ResultParam", "TableFeature"})
_FUNCTIONAL_BRANCHES = {"Frame": "geometry", "FrameFeature": "geometry", "Coordsys": "geometry",
                        "ProbeFeature": "result", "EvaluationGroupFeature": "result", "ResultParam": "result"}
_PRESENTATION_PROPERTIES = {"showPhysicsSymbols", "showphysicssymbols"}
_DATASET_PRESENTATION = {"defaultPlotIDs", "showgeom", "showmesh", "showsolutionparams", "showlegend",
                         "legendpos", "titletype", "titlenumberformat", "colortable", "linewidth", "linestyle"}


def _kind(element):
    return str(element.tag).rsplit("}", 1)[-1]


def _hash(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check_cancelled()
            hasher.update(chunk)
    return hasher.hexdigest()


def parser_signature():
    """Content identity of this reader and all reused decoding/source rules."""
    hasher = hashlib.sha256(PARSER_VERSION.encode("ascii"))
    for name in ("compact_model.py", "model_reader.py", "library_model.py", "case_metadata.py",
                 "mph_saved_values.py", "mph_saved_selections.py", "tutorial_sources.py"):
        hasher.update(name.encode("ascii"))
        hasher.update(bytes.fromhex(_hash(Path(__file__).with_name(name))))
    return hasher.hexdigest()


def _bounded_attributes(element, *, excluded=()):
    attributes, truncated = {}, False
    for name, value in element.attrib.items():
        if name in excluded:
            continue
        if len(attributes) >= MAX_PROPERTIES:
            truncated = True
            break
        attributes[name[:MAX_TEXT]] = value[:MAX_TEXT]
        truncated |= len(name) > MAX_TEXT or len(value) > MAX_TEXT
    return attributes, truncated


def _scaffolding(element):
    """Skip only collection wrappers with no directly stored configuration."""
    if not _kind(element).endswith("List") or (element.text or "").strip():
        return False
    if any(name not in {"tag", "name", "op", "T"} | _ATTRIBUTE_METADATA for name in element.attrib):
        return False
    return not any(_kind(child) in _NAMED_PROPERTIES | _SELECTIONS |
                   {"active", "entityFlags", "NodeDefaultValues"} for child in element)


def _array_descriptor(cells, *, shape=None, declared=None):
    descriptor = {"source_only": True, "entries_observed": len(cells)}
    if shape is not None:
        descriptor["shape"] = shape
    if declared is not None:
        descriptor["entries_declared"] = declared
    sample = cells[:2] + cells[-2:] if len(cells) > 4 else list(cells)
    descriptor["sample"] = [value[:128] if isinstance(value, str) else value for value in sample]
    if any(isinstance(value, str) and len(value) > 128 for value in sample):
        descriptor["sample_truncated"] = True
    if cells and all(type(value) in {int, float} and math.isfinite(value) for value in cells):
        descriptor["numeric_range"] = {"min": min(cells), "max": max(cells)}
    return descriptor


def _property_value(item):
    if item["value_type"] in {"xml_structure", "selection_structure"}:
        return None, True
    value = item.get("value")
    if item["value_type"] == "matrix" and isinstance(value, dict):
        # The normalizer also exposes a flattened entries list containing every
        # cell a second time. Rows plus declared shape/widths are sufficient.
        cells = value.get("entries", [])
        value = {key: value[key] for key in ("shape", "rows", "row_lengths") if key in value}
        if len(cells) > MAX_VALUE_CELLS or len(json.dumps(value, ensure_ascii=False)) > MAX_VALUE_CHARACTERS:
            return _array_descriptor(cells, shape=value.get("shape"), declared=item.get("declared_entries")), True
    elif isinstance(value, list) and (len(value) > MAX_VALUE_CELLS or len(json.dumps(value, ensure_ascii=False)) > MAX_VALUE_CHARACTERS):
        return _array_descriptor(value, declared=item.get("declared_entries")), True
    return value, False


def _selection_value(entry):
    """Retain meaningful saved reference/decoding facts, never raw XML copies."""
    value = {}
    attributes = entry.get("attributes", {})
    if attributes.get("selType"):
        value["selection_type"] = attributes["selType"]
    extra = {key: item for key, item in attributes.items() if key not in {"T", "selection", "selType"}}
    if extra:
        value["attributes"] = extra
    for key in ("named_references", "output_references"):
        if entry.get(key):
            value[key] = entry[key]
    explicit = []
    for item in entry.get("explicit", []):
        kept = {key: item[key] for key in ("status", "geometry_ref", "entity_dimension", "encoded_entity_dimension",
                                          "decoded_entities", "reason") if key in item}
        if len(kept.get("decoded_entities", [])) > MAX_VALUE_CELLS:
            kept["entities_descriptor"] = _array_descriptor(kept.pop("decoded_entities"))
            kept["entities_omitted"] = True
        if item.get("truncated"):
            kept["truncated"] = True
        explicit.append(kept)
    if explicit:
        value["explicit"] = explicit
    remaining = []
    for item in entry.get("remaining", []):
        kept = {key: item[key] for key in ("values", "status") if key in item}
        if len(kept.get("values", [])) > MAX_VALUE_CELLS:
            kept["values_descriptor"] = _array_descriptor(kept.pop("values"))
            kept["values_omitted"] = True
        if item.get("truncated"):
            kept.update(truncated=True, saved_count=item.get("saved_count"))
        remaining.append(kept)
    if remaining:
        value["remaining"] = remaining
    for key in ("objects", "base"):
        references = []
        for item in entry.get(key, []):
            kept = {name: item[name] for name in ("geometry_ref", "feature_ref", "entity_dimension") if item.get(name) is not None}
            extras = {name: attr for name, attr in item.get("attributes", {}).items()
                      if name not in {"geom", "feature", "dim", "assocId"}}
            if extras:
                kept["attributes"] = extras
            if item.get("association_id"):
                kept["association_id"] = {name: attr for name, attr in item["association_id"].items()
                                          if name in {"values", "status", "truncated"}}
            references.append(kept)
        if references:
            value[key] = references
    if entry.get("unresolved_reasons"):
        value["issues"] = entry["unresolved_reasons"]
    return value


def _explicitly_disabled(element):
    if any(element.get(key, "").strip().lower() == "false" for key in ("active", "enabled")):
        return True
    return any(_kind(child) == "active" and (child.get("value") or child.text or "").strip().lower() == "false"
               for child in element)


def source_only_property_reason(name, decoded_values, *, feature_branch=None, feature_type=None):
    """Known selector-controlled saved values; numeric zero is never a flag.

    The mapping must contain only unambiguous, completely decoded controls.
    This describes the saved selector syntax, not study-effective activation.
    """
    if feature_branch not in {"geometry", "mesh", "physics", "material", "multiphysics"}:
        return None
    if feature_branch == "mesh" and feature_type in {"Size", "MeshSizeDefault"}:
        custom = decoded_values.get("custom")
        if name == "hauto" and (custom is True or isinstance(custom, str) and custom.lower() == "on"):
            return "manual_mesh_size_automatic_preset_unused"
        if name in {"hmax", "hmin", "hgrad", "hcurve", "hnarrow"} and (custom is False or isinstance(custom, str) and custom.lower() == "off"):
            return "automatic_mesh_preset_manual_size_unused"
    material = decoded_values.get(str(name) + "_mat")
    if isinstance(material, str) and material in {"from_mat", "from_material"}:
        return "material_selected_base_value"
    active = decoded_values.get(str(name) + "active")
    if active is False or isinstance(active, str) and active.lower() in {"off", "false"}:
        return "explicit_inactive_base_value"
    return None


def _document_rows(path, docs_root, info):
    """Retain a relation to the external tutorial, never a second body copy."""
    html, tutorial, coverage, _ = _tutorial(path, docs_root, info, metadata_only=True)
    references = [_tutorial_reference(html, tutorial)] if html is not None else []
    coverage = {key: value for key, value in coverage.items() if key != "path"}
    coverage.update(body_storage="external_document_owner", reference_count=len(references), document_count=0, passage_count=0)
    return references, coverage


def parse_compact_model(path, docs_root=None):
    """Return compact typed rows and one raw XML byte string, without writes.

    IDs/ordinals are one-based. Routes and child_index are zero-based XML child
    indices; a node route is relative to its nearest retained parent, or the
    document root when parent_id is None. Raw child evidence is recovered by
    following parent routes and then the property's/selection's child_index.
    """
    path = Path(path).expanduser().resolve(strict=True)
    if path.suffix.lower() != ".mph" or not path.is_file():
        raise ModelInspectionError("Compact parsing requires a local .mph file")
    before = path.stat()
    if before.st_size > MAX_CONTAINER_BYTES:
        raise ModelInspectionError("MPH file exceeds the inspection size limit")
    fingerprint = _hash(path)
    metadata, _ = _safe_container(path)
    info = {}
    if metadata.get("modelinfo.xml"):
        try:
            info = dict(ET.fromstring(metadata["modelinfo.xml"]).attrib)
        except ET.ParseError:
            pass
    raw_xml = metadata.get("dmodel.xml", b"")
    root, xml_status = None, "missing"
    if raw_xml:
        try:
            root, xml_status = ET.fromstring(raw_xml), "bounded_saved_state"
        except ET.ParseError:
            xml_status = "malformed"
    nodes, properties, selections = [], [], []
    excluded, exclusion_examples, excluded_properties = Counter(), {}, Counter()
    sections, property_statuses, selection_statuses = Counter(), Counter(), Counter()
    excluded_attributes = Counter()
    total_nodes = total_properties = total_selections = omitted_trees = summarized_arrays = 0
    property_unread = selection_unread = empty_selections = attribute_truncations = 0
    normalization_truncated = False
    raw_only_kinds = Counter()
    if root is not None:
        total_nodes = sum(bool(element.get("tag")) for element in root.iter())
        stack = [(root, None, [], "misc", False, False)]
        while stack:
            check_cancelled()
            element, parent_id, route, branch, gui_parent, disabled_parent = stack.pop()
            kind = _kind(element)
            branch = _FUNCTIONAL_BRANCHES.get(kind, _SAVED_BRANCHES.get(kind, branch))
            gui = gui_parent or kind in _GUI_SUBTREES
            disabled = disabled_parent or _explicitly_disabled(element)
            tagged = bool(element.get("tag"))
            reason = ("display_or_plot_presentation" if gui or kind in _GUI_KINDS else "explicit_saved_false_node_or_ancestor" if disabled else
                      "collection_scaffolding" if _scaffolding(element) else
                      "outside_functional_allowlist" if kind not in _FUNCTIONAL_KINDS else None)
            if tagged and reason:
                excluded[(reason, kind)] += 1
                examples = exclusion_examples.setdefault((reason, kind), [])
                if len(examples) < 3 and len(route) <= 64:
                    examples.append({"parent_id": parent_id, "xml_route": route})
            elif tagged:
                if len(nodes) >= MAX_NODES:
                    break
                attributes, cut = _bounded_attributes(element, excluded=_NODE_COLUMNS | _ATTRIBUTE_METADATA)
                excluded_attributes.update(name for name in element.attrib if name in _ATTRIBUTE_METADATA)
                node_id = len(nodes) + 1
                node = {"id": node_id, "parent_id": parent_id, "xml_route": route, "kind": kind,
                        "tag": element.get("tag")[:MAX_TEXT], "label": element.get("name", "")[:MAX_TEXT],
                        "type": element.get("op", "")[:MAX_TEXT], "feature_branch": branch, "attributes": attributes}
                attributes_cut = cut or any(len(element.get(key, "")) > MAX_TEXT for key in _NODE_COLUMNS)
                if attributes_cut:
                    node["truncated"] = True
                    attribute_truncations += 1
                nodes.append(node)
                parent_id, route = node_id, []
                for section, kinds in _SECTION_TAGS.items():
                    sections[section] += kind in kinds
                normalized = normalize_properties(element, max_items=MAX_PROPERTIES, max_text=MAX_TEXT)
                normalized_selections = normalize_selections(element, max_items=MAX_PROPERTIES, max_text=MAX_TEXT)
                total_properties += normalized["total"]
                property_unread += normalized["unread_count"]
                normalization_truncated |= normalized["truncated"] or normalized_selections["truncated"]
                raw_only_kinds.update(item["element"] for item in normalized["raw_properties"] if _kind_name(item["element"]) not in _NAMED_PROPERTIES)
                named_counts = Counter(item["name"] for item in normalized["properties"])
                decoded_values = {item["name"]: item["value"] for item in normalized["properties"]
                                  if item["status"] == "decoded" and not item["truncated"] and named_counts[item["name"]] == 1}
                indices = [index for index, child in enumerate(element) if _kind(child) in _NAMED_PROPERTIES]
                for ordinal, (index, item) in enumerate(zip(indices, normalized["properties"]), 1):
                    name = item["name"]
                    source_only_reason = source_only_property_reason(name, decoded_values, feature_branch=branch, feature_type=element.get("op"))
                    if name in _PRESENTATION_PROPERTIES or kind == "DatasetFeature" and name in _DATASET_PRESENTATION or kind in {"MeshFeature", "GeomFeature"} and name in {"buildtime", "buildinfo", "buildinfox"}:
                        source_only_reason = "presentation_or_build_metadata"
                    if source_only_reason:
                        excluded_properties[(kind, name, source_only_reason)] += 1
                        continue
                    value, omitted = _property_value(item)
                    row = {"node_id": node_id, "ordinal": ordinal, "child_index": index, "name": item["name"],
                           "value": value, "value_type": item["value_type"], "status": item["status"],
                           "issues": item["issues"], "truncated": item["truncated"]}
                    if omitted:
                        row["value_omitted"] = True
                        if item["value_type"] in {"xml_structure", "selection_structure"}:
                            omitted_trees += 1
                        else:
                            summarized_arrays += 1
                    properties.append(row)
                    property_statuses[row["status"]] += 1
                indices = [index for index, child in enumerate(element) if _kind(child) in _SELECTIONS]
                total_selections += len(indices)
                selection_unread += len(indices) - len(normalized_selections["entries"])
                for ordinal, (index, item) in enumerate(zip(indices, normalized_selections["entries"]), 1):
                    unsupported = any(reason.startswith("Unrecognized direct selection child:") for reason in item.get("unresolved_reasons", []))
                    if not unsupported and not any(item.get(key) for key in ("explicit", "named_references", "output_references", "remaining", "objects", "base")):
                        empty_selections += 1
                        continue
                    selections.append({"node_id": node_id, "ordinal": ordinal, "child_index": index,
                                       "kind": item["element"], "status": item["status"],
                                       "value": _selection_value(item), "truncated": bool(item["truncated"])})
                    selection_statuses[item["status"]] += 1
            for index in range(len(element) - 1, -1, -1):
                stack.append((element[index], parent_id, route + [index], branch, gui, disabled))
    excluded_total = sum(excluded.values())
    omitted_nodes = total_nodes - len(nodes) - excluded_total
    tutorial_refs, tutorial_coverage = _document_rows(path, docs_root, info)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or _hash(path) != fingerprint:
        raise ModelInspectionError("Original model changed during compact parsing")
    contract = {"schema": PARSER_VERSION, "property_normalizer": NORMALIZER_VERSION, "selection_normalizer": SELECTION_SCHEMA,
                "limits": {"max_nodes": MAX_NODES, "max_properties_per_node": MAX_PROPERTIES, "max_text": MAX_TEXT,
                           "max_value_cells": MAX_VALUE_CELLS, "max_value_characters": MAX_VALUE_CHARACTERS},
                "semantics": "Saved syntax only; default/explicit origin, effective activation, selection membership and physical validity not evaluated",
                "execution": "No COMSOL, geometry build, meshing, solver generation, solve or expression evaluation",
                "source_pointer": "Node parent routes and property/selection child_index are zero-based XML child indices; IDs and ordinals are one-based",
                "matrix": "Rows retain serialized grouping and declared shape/widths; layout and numeric expressions are not evaluated",
                "omitted_values": "Unsupported XML/selection trees and large arrays remain source-only; large arrays keep shape/count/sample descriptors. Resolve exact data in the original hash-bound MPH member by node route and child_index",
                "persistence": "raw_xml is transient only; the original source MPH is authoritative and no raw XML snapshot is required",
                "functional_selection": "Only known simulation-definition kinds are normalized; GUI/plot presentation, scaffolding, explicitly false branches and other kinds remain source-only with coverage counts/route examples. Unknown named properties within selected nodes are retained unless an exact known presentation key",
                "functional_kinds": sorted(_FUNCTIONAL_KINDS),
                "remaining_selections": "Saved groups do not establish final geometry membership or an assigned entity dimension",
                "selection_format_evidence": dict(FORMAT_EVIDENCE)}
    coverage = {"source_container": {"node_type": info.get("nodeType"), "status": "preview_only" if info.get("nodeType", "").lower() == "preview" else "saved_model"},
                "raw_xml": {"status": xml_status, "bytes": len(raw_xml), "nodes_observed_total": total_nodes, "nodes_retained": len(nodes),
                            "nodes_omitted_by_limit": omitted_nodes, "excluded_nodes": excluded_total,
                            "exclusions": [{"reason": reason, "kind": kind, "count": count,
                                            "examples": exclusion_examples[(reason, kind)]} for (reason, kind), count in sorted(excluded.items())],
                            "properties_total": total_properties, "properties_retained": len(properties), "properties_unread": property_unread,
                            "properties_source_only": sum(excluded_properties.values()),
                            "property_exclusions": [{"node_kind": kind, "name": name, "count": count, "reason": reason}
                                                    for (kind, name, reason), count in sorted(excluded_properties.items())],
                            "property_statuses": dict(property_statuses), "raw_tree_values_omitted": omitted_trees,
                            "large_array_values_summarized": summarized_arrays, "metadata_attributes_source_only": dict(excluded_attributes),
                            "raw_only_child_kinds": dict(raw_only_kinds), "selections_total": total_selections,
                            "selections_retained": len(selections), "selections_unread": selection_unread,
                            "empty_selections_source_only": empty_selections,
                            "selection_statuses": dict(selection_statuses), "node_attributes_truncated": attribute_truncations,
                            "truncated": bool(omitted_nodes or property_unread or selection_unread or normalization_truncated or attribute_truncations)},
                "sections": dict(sections), "tutorial": tutorial_coverage, "validation": {"source_unchanged": True, "physical_validity": "not_evaluated"}}
    case = {"source_path": str(path), "source_sha256": fingerprint, "source_size_bytes": before.st_size,
            "source_member": "dmodel.xml",
            "version": info.get("comsolVersion"), "title": info.get("title") or info.get("modelTitle") or path.stem,
            "tutorial_refs": tutorial_refs,
            "coverage": coverage, "parser_contract": contract, "parser_signature": parser_signature()}
    return {"case": case, "nodes": nodes, "properties": properties, "selections": selections,
            "documents": [], "passages": [], "raw_xml": raw_xml}


def _kind_name(value):
    return str(value).rsplit("}", 1)[-1]
