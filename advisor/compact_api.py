"""Selective, source-labelled API observations for one compact MPH case.

The full getter snapshot, copied MPH and Java reconstruction are temporary.
XML values remain authoritative saved facts; API observations never replace
them or establish study-effective activation, membership or physical validity.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET

from .build_control import check_cancelled
from .compact_model import MAX_TEXT, MAX_VALUE_CELLS, MAX_VALUE_CHARACTERS, _array_descriptor, source_only_property_reason
from .library_model import API_READ_CONTRACT, JAVA_SOURCE, _comsol_root, _deep_state
from .model_reader import PROJECT_ROOT

API_SELECTION_VERSION = "compact-mph-api/v1"
MAX_OBSERVATIONS = 5000
MAX_OBSERVATIONS_PER_NODE = 64
_ROOT_ACCESSORS = {"ModelNode": "component", "GeomSequence": "geom", "MeshSequence": "mesh", "Physics": "physics",
                   "Material": "material", "MultiphysicsCoupling": "multiphysics", "SelectionFeature": "selection",
                   "Expr": "variable", "Variable": "variable", "FunctionFeature": "func", "Cpl": "cpl", "Pair": "pair",
                   "Coordsys": "coordSystem", "ProbeFeature": "probe", "Study": "study", "SolverSequence": "sol"}
_CHILD_ACCESSORS = {"GeomFeature": "feature", "MeshFeature": "feature", "PhysicsFeature": "feature",
                    "StudyFeature": "feature", "SolverFeature": "feature", "PhysicsProp": "prop",
                    "MaterialModel": "propertyGroup", "MaterialPropertyGroup": "propertyGroup",
                    "GeomObjectSelectionFeature": "selection", "MeshSelectionFeature": "selection"}
_RESULT_ACCESSORS = {"DatasetFeature": "dataset", "NumericalFeature": "numerical", "TableFeature": "table",
                    "EvaluationGroupFeature": "evaluationGroup"}
_COMPONENT_REQUIRED = {"GeomSequence", "MeshSequence", "Physics", "MultiphysicsCoupling", "Coordsys"}
_MISSING_KEYS = {
    "mesh": {"hmax", "hmin", "hgrad", "hcurve", "hnarrow", "hauto", "custom", "numelem", "hmaxactive", "hminactive", "hgradactive", "hcurveactive", "hnarrowactive"},
    "study": {"plist", "plistarr", "pname", "punit", "tlist", "tunit", "freq", "frequnit", "useparam"},
    "solver": {"rtol", "atol", "atolglobal", "maxiter", "maxnliter", "maxstep", "initialstep"},
    "geometry": {"filename", "unit", "lengthunit", "repairtol", "repairtoltype"},
    "selection": {"input", "entitydim", "geom", "named"},
    "func": {"funcname", "expr", "args", "argunit", "fununit", "filename"},
    "result": {"expr", "unit", "data", "solution"},
}


class CompactAPIReadError(ValueError):
    """The requested single-case getter read did not meet its read-only contract."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            check_cancelled()
            digest.update(block)
    return digest.hexdigest()


def parser_signature():
    digest = hashlib.sha256(API_SELECTION_VERSION.encode("ascii"))
    for path in (Path(__file__), Path(__file__).with_name("compact_model.py"),
                 Path(__file__).with_name("library_model.py"), JAVA_SOURCE):
        digest.update(path.name.encode("utf-8"))
        digest.update(bytes.fromhex(_hash(path)))
    return digest.hexdigest()


def current_runtime_policy():
    """Pre-JVM cache identity; inspect installed file metadata without loading it."""
    root = _comsol_root()

    def binary(relative):
        path = root / relative
        try:
            stat = path.stat()
            return {"path": relative, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        except OSError:
            return {"path": relative, "status": "unavailable"}

    def directory_stamp(relative, suffix=None):
        directory = root / relative
        records = []
        try:
            for path in sorted(directory.iterdir(), key=lambda value: value.name):
                if path.is_file() and (suffix is None or path.suffix.lower() == suffix):
                    stat = path.stat()
                    records.append((path.name, stat.st_size, stat.st_mtime_ns))
        except OSError:
            return {"status": "unavailable"}
        return {"file_count": len(records), "stat_sha256": hashlib.sha256(_json(records).encode("utf-8")).hexdigest()}

    return {"schema": "compact-api-runtime-policy/v1", "read_contract": API_READ_CONTRACT,
            "selector_signature": parser_signature(), "comsol_root": str(root),
            "java": binary("java/win64/jre/bin/java.exe"), "javac": binary("java/win64/jre/bin/javac.exe"),
            "plugins": directory_stamp("plugins", ".jar"), "native_runtime": directory_stamp("bin/win64")}


def _require_saved_model(case):
    coverage = case.get("coverage", {})
    container = coverage.get("source_container", {})
    if container.get("status") == "preview_only" or str(container.get("node_type", "")).lower() == "preview":
        raise CompactAPIReadError("Preview-only MPH contains no saved model configuration; obtain the actual compact/full MPH before an API read")
    if coverage.get("raw_xml", {}).get("status") in {"missing", "malformed"}:
        raise CompactAPIReadError("Saved XML is unavailable; selective API matching requires original model configuration")


def _saved_bundle(reader):
    if isinstance(reader, dict):
        _require_saved_model(reader["case"])
        return reader
    case = reader.read_case()
    _require_saved_model(case)
    nodes = list(reader.iter_nodes())
    return {"case": case, "nodes": nodes, "properties": [item for node in nodes for item in node.get("properties", [])],
            "selections": [item for node in nodes for item in node.get("selections", [])], "raw_xml": reader.read_raw_xml()}


def _kind(element):
    return str(element.tag).rsplit("}", 1)[-1]


def compact_node_identities(saved):
    """Resolve exact API paths using XML ancestry, storedParent and geom refs.

    Missing/ambiguous component ownership is never guessed from matching tags.
    Unsupported source kinds simply have no API identity in this contract.
    """
    root = ET.fromstring(saved["raw_xml"])
    parents = {child: parent for parent in root.iter() for child in parent}
    components, geometries = defaultdict(list), defaultdict(list)
    for element in root.iter():
        if _kind(element) == "ModelNode":
            components[element.get("tag")].append(element)
        elif _kind(element) == "GeomSequence":
            geometries[element.get("tag")].append(element)
    by_id = {node["id"]: node for node in saved["nodes"]}
    elements = {}
    for node in saved["nodes"]:
        element = elements[node["parent_id"]] if node.get("parent_id") is not None else root
        for index in node["xml_route"]:
            element = element[index]
        if element.get("tag") != node["tag"] or _kind(element) != node["kind"]:
            raise CompactAPIReadError("Compact node pointer does not match original XML")
        elements[node["id"]] = element

    def component(element, seen=None):
        seen = set() if seen is None else seen
        if element in seen:
            return False
        seen.add(element)
        current = element
        while current is not None:
            if _kind(current) == "ModelNode":
                tag = current.get("tag")
                return tag if len(components[tag]) == 1 else False
            refs = [(child.text or "").strip() for child in current if _kind(child) == "storedParent"]
            if refs:
                if len(refs) != 1:
                    return False
                match = re.fullmatch(r"/modelNode/([^/]+)", refs[0])
                return match[1] if match and len(components[match[1]]) == 1 else False
            refs = [(child.text or "").strip() for child in current if _kind(child) == "geom"]
            if refs:
                if len(refs) != 1:
                    return False
                match = re.fullmatch(r"/geom/([^/]+)", refs[0])
                if not match or len(geometries[match[1]]) != 1:
                    return False
                return component(geometries[match[1]][0], seen)
            current = parents.get(current)
        return None

    identities, reasons = {}, Counter()

    def identity(node_id):
        if node_id in identities:
            return identities[node_id]
        node = by_id[node_id]
        kind, tag = node["kind"], node["tag"]
        if not isinstance(tag, str) or not tag or any(char in tag for char in "/()"):
            reasons["unsupported_tag_syntax"] += 1
            identities[node_id] = None
            return None
        parent_id = node.get("parent_id")
        parent_path = identity(parent_id) if parent_id is not None else None
        accessor = _CHILD_ACCESSORS.get(kind)
        if accessor:
            path = parent_path + "/" + accessor + "(" + tag + ")" if parent_path else None
        elif kind in _RESULT_ACCESSORS:
            path = "model/result/" + _RESULT_ACCESSORS[kind] + "(" + tag + ")"
        elif kind in _ROOT_ACCESSORS:
            accessor = _ROOT_ACCESSORS[kind]
            if kind == "ModelNode":
                path = "model/component(" + tag + ")"
            elif kind in {"Study", "SolverSequence"}:
                path = "model/" + accessor + "(" + tag + ")"
            elif kind == "FunctionFeature" and parent_path:
                path = parent_path + ("/feature(" if by_id[parent_id]["kind"] == "FunctionFeature" else "/func(") + tag + ")"
            else:
                owner = component(elements[node_id])
                if owner is False or kind in _COMPONENT_REQUIRED and owner is None:
                    path = None
                    reasons["component_not_proven"] += 1
                else:
                    path = "model" + ("/component(" + owner + ")" if owner else "") + "/" + accessor + "(" + tag + ")"
        else:
            path = None
        if path is None:
            reasons["unsupported_or_unresolved_identity"] += 1
        identities[node_id] = path
        return path

    for node_id in by_id:
        identity(node_id)
    return identities, dict(reasons)


def _bounded_value(value):
    """Small typed values once; large arrays become explicit descriptors."""
    if isinstance(value, list):
        cells, pending = [], list(reversed(value))
        while pending:
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(reversed(item))
            else:
                cells.append(item)
        if len(cells) > MAX_VALUE_CELLS or len(_json(value)) > MAX_VALUE_CHARACTERS:
            shape = [len(value)]
            if value and all(isinstance(item, list) for item in value):
                widths = {len(item) for item in value}
                shape.append(next(iter(widths)) if len(widths) == 1 else None)
            return _array_descriptor(cells, shape=shape), True
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT], True
    _json(value)  # Reject non-finite/non-JSON observations, never coerce them.
    return value, False


def _selection_value(selection):
    if not isinstance(selection, dict) or selection.get("status") == "not_applicable":
        return None, [], False
    value, issues, omitted = {}, [], False
    for key in ("named", "dim", "dimension", "geom", "isGlobal", "isGeom", "isInheriting", "inputEntities", "entities", "objects"):
        field = selection.get(key)
        if not isinstance(field, dict):
            continue
        if field.get("status") == "observed" and "value" in field:
            original = field["value"]
            if original is None or original == "" or original == []:
                continue
            value[key], cut = _bounded_value(original)
            omitted |= cut
        else:
            issues.append(field.get("read_error", {}).get("reason", "selection_getter_unresolved"))
    if selection.get("object_entities"):
        entries = []
        for item in selection["object_entities"][:32]:
            if "entities" in item:
                entities, cut = _bounded_value(item["entities"])
                entries.append({"object": item.get("object"), "entities": entities})
                omitted |= cut
            else:
                issues.append(item.get("read_error", {}).get("reason", "selection_object_getter_unresolved"))
        if entries:
            value["object_entities"] = entries
        omitted |= len(selection["object_entities"]) > 32
    if not any(key in value for key in ("named", "geom", "entities", "objects", "inputEntities", "object_entities")) and not any(type(value.get(key)) is int and value[key] >= 0 for key in ("dim", "dimension")):
        return None, issues, omitted
    return value, issues, omitted


def _same_saved_value(saved, value):
    if saved.get("status") != "decoded" or saved.get("truncated") or saved.get("value_omitted"):
        return False
    original = saved.get("value")
    if saved.get("value_type") == "matrix" and isinstance(original, dict):
        original = original.get("rows")
    return _json(original) == _json(value)


def select_compact_api(saved, api, *, coverage=None):
    """Pure projection for tests/inspection; returns no full API snapshot."""
    _require_saved_model(saved["case"])
    if (not isinstance(api, dict) or api.get("schema") != "mph-api-state/v1" or api.get("read_contract") != API_READ_CONTRACT
            or not isinstance(api.get("nodes"), list)
            or any(api.get(key) is not False for key in ("geometry_run", "mesh_run", "solver_run", "defaults_generated"))):
        raise CompactAPIReadError("API snapshot violates the no-execution read contract")
    identities, identity_reasons = compact_node_identities(saved)
    source_paths = defaultdict(list)
    by_id = {node["id"]: node for node in saved["nodes"]}
    for node_id, path in identities.items():
        if path:
            source_paths[path].append(node_id)
    api_path_counts = Counter(node.get("path") for node in api["nodes"])
    properties = defaultdict(lambda: defaultdict(list))
    for item in saved["properties"]:
        properties[item["node_id"]][item["name"]].append(item)
    nodes, observations, counts, selected_errors = [], [], Counter(), Counter()
    counts.update(api_nodes_total=len(api["nodes"]), offline_nodes_total=len(saved["nodes"]))
    for node in api["nodes"]:
        check_cancelled()
        path = node.get("path")
        matches = source_paths.get(path, [])
        if len(matches) != 1 or api_path_counts[path] != 1:
            counts["ambiguous_paths" if matches else "unmatched_api_nodes"] += 1
            continue
        node_id = matches[0]
        source_node = by_id[node_id]
        if source_node.get("type") and node.get("type") and source_node["type"] != node["type"]:
            counts["type_mismatches"] += 1
            continue
        counts["matched_api_nodes"] += 1
        saved_names = properties[node_id]
        controls = {name: items[0]["value"] for name, items in saved_names.items()
                    if len(items) == 1 and items[0].get("status") == "decoded" and not items[0].get("truncated") and not items[0].get("value_omitted")}
        candidates = []
        names = Counter(item.get("name") for item in node.get("properties", []))
        for item in node.get("properties", []):
            counts["api_properties_seen_on_matched_nodes"] += 1
            name = item.get("name")
            if not isinstance(name, str) or names[name] != 1 or len(saved_names.get(name, [])) > 1:
                counts["ambiguous_properties"] += 1
                continue
            if source_only_property_reason(name, controls, feature_branch=source_node.get("feature_branch"), feature_type=source_node.get("type")):
                counts["selector_controlled_values_excluded"] += 1
                continue
            existing = saved_names.get(name, [])
            if not existing and name not in _MISSING_KEYS.get(source_node.get("feature_branch"), set()):
                counts["unselected_api_properties"] += 1
                continue
            status = item.get("read_status", "unresolved")
            if not existing and ("value" not in item or item["value"] is None or item["value"] == "" or item["value"] == []):
                counts["empty_missing_defaults_excluded"] += 1
                continue
            row = {"kind": "property", "name": name, "value_type": item.get("value_type", "unknown"),
                   "status": status, "getter": item.get("getter", "unknown")}
            gap = bool(existing and (existing[0].get("status") != "decoded" or existing[0].get("value_omitted") or existing[0].get("truncated")))
            if existing:
                row["xml_property_ordinal"] = existing[0]["ordinal"]
            if status == "observed" and "value" in item and existing and _same_saved_value(existing[0], item["value"]):
                row["relation"] = "corroborates_saved_xml"
                priority = 4
            else:
                row["relation"] = "fills_saved_gap" if gap else "differs_from_saved_xml" if existing else "missing_saved_property"
                if "value" in item:
                    if str(item.get("value_type", "")).lower() == "selection":
                        selected_value, selection_issues, omitted = _selection_value(item["value"])
                        if selected_value is not None:
                            row["value"] = selected_value
                        else:
                            selection_issues = selection_issues or ["no_meaningful_selection_target"]
                        if selection_issues:
                            row["issues"] = selection_issues
                            selected_errors.update(selection_issues)
                            if row["status"] == "observed":
                                row["status"] = "partial"
                    else:
                        row["value"], omitted = _bounded_value(item["value"])
                    if omitted:
                        row["value_omitted"] = True
                error = item.get("read_error", {})
                if error or status not in {"observed", "partial"}:
                    reason = error.get("reason", "api_getter_unresolved")
                    row["issues"] = [reason]
                    selected_errors[reason] += 1
                priority = 0 if gap else 2 if not existing else 3
            candidates.append((priority, row))
        value, issues, omitted = _selection_value(node.get("selection"))
        if value is not None:
            row = {"kind": "selection", "name": "@selection", "value": value, "value_type": "Selection",
                   "status": "observed" if node["selection"].get("read_complete") and not issues else "partial",
                   "getter": "selection()", "relation": "api_observed_selection"}
            if issues:
                row["issues"] = issues
                selected_errors.update(issues)
            if omitted:
                row["value_omitted"] = True
            candidates.append((1, row))
        if type(node.get("local_active")) is bool:
            candidates.append((1, {"kind": "activation", "name": "@local_active", "value": node["local_active"],
                                   "value_type": "Boolean", "status": "observed", "getter": "isActive()",
                                   "relation": "local_flag_not_effective_activation"}))
        candidates.sort(key=lambda pair: pair[0])
        selected = candidates[:min(MAX_OBSERVATIONS_PER_NODE, max(0, MAX_OBSERVATIONS - len(observations)))]
        counts["observations_omitted_by_limit"] += len(candidates) - len(selected)
        if selected:
            api_node_id = len(nodes) + 1
            nodes.append({"id": api_node_id, "compact_node_id": node_id, "api_path": path})
            for _, row in selected:
                row["api_node_id"] = api_node_id
                observations.append(row)
                counts[row["relation"]] += 1
    reads = api.get("read_coverage", {})
    run = {"schema": API_SELECTION_VERSION, "status": "partial" if api.get("read_errors") or api.get("truncated") or selected_errors else "observed",
           "runtime_version": api.get("runtime_version"), "file_saved_version": api.get("file_saved_version"),
           "read_contract": api["read_contract"], "getter_source_sha256": _hash(JAVA_SOURCE), "selector_signature": parser_signature(),
           "runtime_policy": current_runtime_policy(),
           "scope": "Selected saved/current getter observations; no effective activation, membership, default/explicit origin or physical-validity proof",
           "contract": {"identity": "Exact component/branch/owner/feature ancestry; storedParent and unique geom references prove component ownership; ambiguities skipped",
                        "values": "Saved XML is not overwritten. Exact typed matches reference XML ordinals without storing values again; differences and missing values remain api_observed",
                        "selection": "Named references, target dimensions and observed entities only; no geometry execution or membership validation",
                        "activation": "isActive() is a local observed flag, never study-effective activation",
                        "retention": "Full API snapshot, Java reconstruction and copied MPH exist only inside a cleaned temporary directory",
                        "execution": {key: api[key] for key in ("geometry_run", "mesh_run", "solver_run", "defaults_generated")},
                        "limits": {"max_observations": MAX_OBSERVATIONS, "max_observations_per_node": MAX_OBSERVATIONS_PER_NODE,
                                   "max_value_cells": MAX_VALUE_CELLS, "max_value_characters": MAX_VALUE_CHARACTERS}},
           "counts": {**dict(counts), "retained_nodes": len(nodes), "retained_observations": len(observations)},
           "errors": {"all_getter_read_errors": api.get("read_errors", 0), "all_failure_reasons": reads.get("failure_reasons", {}),
                      "selected_failure_reasons": dict(selected_errors), "identity_exclusions": identity_reasons},
           "truncation_reasons": api.get("truncation_reasons", {})}
    if coverage and coverage.get("cleanup_warning"):
        run["cleanup_warning"] = str(coverage["cleanup_warning"])[:300]
    return {"source_sha256": saved["case"]["source_sha256"], "run": run, "nodes": nodes, "observations": observations}


def extract_compact_api(reader, *, temp_root=None, runner=None):
    """Read one source copy in a disposable workspace, then retain only facts."""
    saved = _saved_bundle(reader)
    source = Path(saved["case"]["source_path"]).resolve(strict=True)
    fingerprint = saved["case"]["source_sha256"]
    before = source.stat()
    if _hash(source) != fingerprint:
        raise CompactAPIReadError("Original MPH hash does not match this compact case")
    workspace = Path(temp_root).resolve() if temp_root is not None else PROJECT_ROOT
    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="compact-api-", dir=workspace) as temporary:
        check_cancelled()
        api, _, coverage = (runner or _deep_state)(source, Path(temporary), fingerprint)
        check_cancelled()
        if api is None:
            raise CompactAPIReadError("Standalone API extraction " + str(coverage.get("status", "failed")) + ": " + str(coverage.get("reason", "no snapshot"))[:300])
        result = select_compact_api(saved, api, coverage=coverage)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or _hash(source) != fingerprint:
        raise CompactAPIReadError("Original model changed during compact API extraction")
    return result
