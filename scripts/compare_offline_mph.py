"""Compare saved MPH XML with hash-linked, previously acquired API observations.

No COMSOL, sim-cli, solver, embedding or subprocess is invoked. API observations
are never supplied to the offline reader. Run from any directory with explicit
--models-dir, --api-root and --output paths (relative paths are supported).
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

XML_CATEGORIES = {
    "ModelNode": "component", "GeomSequence": "geom", "GeomFeature": "feature",
    "MeshSequence": "mesh", "MeshFeature": "feature", "Physics": "physics",
    "PhysicsFeature": "feature", "PhysicsProp": "prop", "Material": "material",
    "MaterialModel": "propertyGroup", "MaterialPropertyGroup": "propertyGroup",
    "Study": "study", "StudyFeature": "feature", "SolverSequence": "sol", "SolverFeature": "feature",
    "SelectionFeature": "selection", "Cpl": "cpl", "FunctionFeature": "func",
    "MultiphysicsCoupling": "multiphysics", "Coordsys": "coordSystem", "Results": "result",
    "ResultFeature": "feature", "DatasetFeature": "dataset", "NumericalFeature": "numerical",
    "ExportFeature": "export", "TableFeature": "table",
}
XML_SEGMENT = re.compile(r'([\w]+)\[@tag=("(?:\\.|[^"\\])*")\]')
API_SEGMENT = re.compile(r'([^/()]+)\(([^()]*)\)')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_read(path):
    def reject(value):
        raise ValueError(f"Non-finite JSON number: {value}")
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject)


def raw_field(node, name):
    for item in node.get("properties", []):
        if item.get("element") == name and not item.get("truncated"):
            return str(item.get("text") or item.get("attributes", {}).get("value") or "").strip()
    return ""


def xml_descriptors(nodes):
    """Scope comes only from saved XML ancestry/storedParent/geom references."""
    geometry_components = defaultdict(set)
    for node in nodes:
        if node.get("node_kind") == "GeomSequence":
            match = re.search(r"/modelNode/([^/]+)", raw_field(node, "storedParent"))
            if match:
                geometry_components[node["tag"]].add(match.group(1))
    by_path = {node["path"]: node for node in nodes}
    output = []
    for index, node in enumerate(nodes):
        path = node.get("path", "")
        ancestors = [value for key, value in by_path.items() if path == key or path.startswith(key + "/")]
        ancestors.sort(key=lambda value: len(value["path"]))
        components, evidence = set(), []
        for ancestor in ancestors:
            if ancestor.get("node_kind") == "ModelNode":
                components.add(ancestor["tag"]); evidence.append("xml_component_ancestor")
            stored = re.search(r"/modelNode/([^/]+)", raw_field(ancestor, "storedParent"))
            if stored:
                components.add(stored.group(1)); evidence.append("xml_storedParent")
            geometry = re.search(r"(?:^|/)geom/([^/]+)", raw_field(ancestor, "geom"))
            if geometry and geometry_components[geometry.group(1)]:
                components.update(geometry_components[geometry.group(1)]); evidence.append("xml_geom_to_storedParent")
        trail = [(XML_CATEGORIES[kind], json.loads(tag)) for kind, tag in XML_SEGMENT.findall(path) if kind in XML_CATEGORIES]
        category = XML_CATEGORIES.get(node.get("node_kind"))
        if category == "result" and not trail:
            trail = [("result", node.get("tag", "result"))]
        global_scope = bool(trail and trail[0][0] in {"study", "sol", "result"})
        scope_known = bool(components) or global_scope
        if len(components) == 1 and (not trail or trail[0][0] != "component"):
            trail.insert(0, ("component", next(iter(components))))
        output.append({"index": index, "path": path, "category": category or "unsupported:" + str(node.get("node_kind")),
            "node_kind": node.get("node_kind"), "tag": node.get("tag"), "type": str(node.get("type") or ""),
            "trail": trail, "scope_known": scope_known, "scope_ambiguous": len(components) > 1,
            "scope_evidence": sorted(set(evidence)) or (["xml_global_study_solver_result"] if global_scope else [])})
    return output


def api_descriptors(nodes):
    output = []
    for index, node in enumerate(nodes):
        trail = API_SEGMENT.findall(node.get("path", ""))
        if node.get("category") == "result" and not trail:
            trail = [("result", node.get("tag", "result"))]
        output.append({"index": index, "path": node.get("path"), "category": node.get("category"), "tag": node.get("tag"),
                       "type": str(node.get("type") or ""), "trail": trail})
    return output


def pair_nodes(offline, api):
    left, right = xml_descriptors(offline), api_descriptors(api)
    candidates, reverse, conflicts = {}, defaultdict(list), []
    for item in left:
        matches = []
        if not item["scope_ambiguous"] and not item["category"].startswith("unsupported:"):
            for target in right:
                if (item["category"], item["tag"]) != (target["category"], target["tag"]):
                    continue
                trail = target["trail"]
                if not item["scope_known"]:
                    trail = [entry for entry in trail if entry[0] != "component"]
                if item["trail"] != trail:
                    continue
                if item["type"] and target["type"] and item["type"] != target["type"]:
                    conflicts.append({"offline": item["index"], "api": target["index"], "reason": "type_conflict"})
                    continue
                matches.append(target["index"]); reverse[target["index"]].append(item["index"])
        candidates[item["index"]] = matches
    pairs, ambiguous = [], []
    for item in left:
        found = candidates[item["index"]]
        if len(found) == 1 and len(reverse[found[0]]) == 1:
            target = right[found[0]]
            pairs.append({"offline": item, "api": target,
                "basis": "category_tag_ancestor_scope_and_type" if item["type"] and target["type"] else "unique_structure_type_missing_on_one_or_both_sides",
                "component_scope": "explicit_xml_evidence" if item["scope_known"] else "unique_candidate_component_scope_not_saved"})
        elif found:
            ambiguous.append({"offline": item, "api_candidates": [right[index] for index in found], "reason": "non_unique_in_one_or_both_inventories"})
    used_left = {item["offline"]["index"] for item in pairs} | {item["offline"]["index"] for item in ambiguous}
    used_right = {item["api"]["index"] for item in pairs} | {index for item in ambiguous for index in candidates[item["offline"]["index"]]}
    return {"pairs": pairs, "ambiguous": ambiguous, "offline_only": [item for item in left if item["index"] not in used_left],
            "api_only": [item for item in right if item["index"] not in used_right], "type_conflicts": conflicts}


def value_family(value):
    if value is None: return "null"
    if isinstance(value, bool): return "boolean"
    if isinstance(value, (int, float)): return "number"
    if isinstance(value, str): return "string"
    if isinstance(value, list): return "array:" + ",".join(sorted(set(value_family(item) for item in value)))
    return "object"


def compare_values(left, right):
    if value_family(left) != value_family(right): return "typed_value_difference"
    if isinstance(left, list):
        if len(left) != len(right): return "typed_value_difference"
        results = [compare_values(a, b) for a, b in zip(left, right)]
        if all(value == "equal" for value in results): return "equal"
        if all(value in {"equal", "numeric_within_tolerance"} for value in results): return "numeric_within_tolerance"
        return "expression_text_difference" if any(value == "expression_text_difference" for value in results) else "typed_value_difference"
    if left == right: return "equal"
    if isinstance(left, (int, float)) and not isinstance(left, bool) and math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-15):
        return "numeric_within_tolerance"
    return "expression_text_difference" if isinstance(left, str) else "typed_value_difference"


def value_receipt(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {"family": value_family(value), "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "preview": encoded[:240], "preview_truncated": len(encoded) > 240}


def raw_property_names(node):
    names = set()
    for raw in node.get("properties", []):
        attributes = raw.get("attributes", {})
        value = attributes.get("name") if raw.get("element") == "propertyValue" else attributes.get("param")
        if value: names.add(str(value).removeprefix("p:"))
        if raw.get("raw_xml") and not raw.get("truncated"):
            try:
                for element in ET.fromstring(raw["raw_xml"]).iter():
                    value = element.get("name") if element.tag == "propertyValue" else element.get("param") if element.tag == "param" else None
                    if value: names.add(value.removeprefix("p:"))
            except ET.ParseError:
                pass
    return names


def compare_properties(offline, api):
    normalized = offline.get("normalized_properties") or {}
    left, right = defaultdict(list), defaultdict(list)
    for item in normalized.get("properties", []): left[item.get("name")].append(item)
    for item in api.get("properties", []): right[item.get("name")].append(item)
    raw_names, results = raw_property_names(offline), []
    for name in sorted(set(left) | set(right), key=str):
        a, b = left.get(name, []), right.get(name, [])
        row = {"name": name, "presence": "both" if a and b else "offline_only" if a else "api_only"}
        reasons = []
        if len(a) > 1 or len(b) > 1:
            row.update(status="ambiguous_property_name", offline_count=len(a), api_count=len(b)); results.append(row); continue
        a, b = a[0] if a else None, b[0] if b else None
        if a:
            row["offline_status"], row["offline_value_type"] = a.get("status"), a.get("value_type")
            if a.get("status") != "decoded": reasons.append("offline_partial" if a.get("status") == "partial" else "offline_undecoded")
        if b:
            row["api_read_status"], row["api_value_type"] = b.get("read_status"), b.get("value_type")
            if b.get("read_status") != "observed": reasons.append("api_read_partial" if b.get("read_status") == "partial" else "api_read_failed")
        if not a:
            row["xml_observation"] = "present_in_raw_xml_not_structured" if name in raw_names else "xml_observation_truncated" if offline.get("properties_truncated") else "not_found_in_current_structured_output_other_saved_encodings_not_exhaustively_checked"
        if reasons: row.update(status="not_comparable", reasons=reasons)
        elif not a or not b: row["status"] = row["presence"]
        elif isinstance(a.get("value"), dict) and a["value"].get("layout") == "not_verified": row["status"] = "structured_matrix_layout_not_verified"
        else: row["status"] = compare_values(a.get("value"), b.get("value"))
        if row["status"] != "equal":
            if a: row["offline_value"] = value_receipt(a.get("value"))
            if b: row["api_value"] = value_receipt(b.get("value"))
        results.append(row)
    return {"normalized_interface_present": "normalized_properties" in offline, "summary": dict(Counter(item["status"] for item in results)),
            "noncomparison_reasons": dict(Counter(reason for item in results for reason in item.get("reasons", []))), "items": results}


def compare_selection(offline, api):
    normalized = offline.get("normalized_selections")
    observed = api.get("selection")
    entries = normalized.get("entries", []) if isinstance(normalized, dict) else []
    observed = observed if isinstance(observed, dict) else {}
    dim = observed.get("dim", {})
    getters = {key: observed.get(key, {}) for key in ("entities", "inputEntities")}
    def readable_getter(value):
        return isinstance(dim, dict) and dim.get("status") == "observed" and isinstance(value, dict) and value.get("status") == "observed"
    comparisons = []
    for entry_index, entry in enumerate(entries):
        for explicit_index, explicit in enumerate(entry.get("explicit", [])):
            for getter, observation in getters.items():
                item = {"entry": entry_index, "explicit": explicit_index, "api_getter": getter, "entity_dimension": explicit.get("entity_dimension"),
                        "highest_dimension": explicit.get("highest_dimension"), "dimension_relation": explicit.get("dimension_relation"), "api_dimension": dim.get("value") if isinstance(dim, dict) else None}
                if explicit.get("status") != "decoded_saved": item["status"] = "saved_selection_undecoded"
                elif explicit.get("dimension_relation") != "same": item["status"] = "dimension_relation_different_or_unknown_not_compared"
                elif not readable_getter(observation): item["status"] = "api_selection_getter_unavailable"
                elif explicit.get("entity_dimension") != dim["value"]: item["status"] = "api_and_saved_dimension_different_not_compared"
                else:
                    a, b = explicit.get("decoded_entities"), observation["value"]
                    if not isinstance(a, list) or not isinstance(b, list) or any(type(value) is not int for value in a + b):
                        item["status"] = "entity_list_not_comparable"
                    else:
                        a, b = sorted(set(a)), sorted(set(b))
                        item.update(status="same_dimension_entity_sets_equal" if a == b else "same_dimension_entity_sets_different",
                            offline_entities=value_receipt(a), api_entities=value_receipt(b), offline_entity_count=len(a), api_entity_count=len(b))
                comparisons.append(item)
    if not comparisons:
        comparisons.append({"status": "saved_indirection_not_resolved" if any(entry.get("named_references") or entry.get("remaining") or entry.get("objects") for entry in entries)
            else "api_getter_only_in_compared_output" if any(readable_getter(value) for value in getters.values()) else "no_comparable_selection_observation"})
    return {"offline_status": normalized.get("status", "not_reported") if isinstance(normalized, dict) else "normalization_not_available",
            "api_status": observed.get("status", "getter_observations") if observed else "not_reported", "entries": len(entries),
            "saved_indirections": {key: sum(len(entry.get(key, [])) for entry in entries) for key in ("named_references", "remaining", "objects", "base", "output_references")},
            "physical_membership": "not_validated", "api_geometry_built_by_extractor": observed.get("geometry_built_by_extractor"),
            "comparisons": comparisons, "summary": dict(Counter((item.get("api_getter", "no_direct_pair") + ":" + item["status"]) for item in comparisons))}


def api_catalog(root):
    catalog, errors = defaultdict(list), []
    for manifest in sorted(root.rglob("api-manifest.json")):
        try:
            value = json_read(manifest); state = manifest.with_name("api-state.json")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", str(value.get("source_sha256", ""))) or not state.is_file():
                raise ValueError("missing source hash or state")
            catalog[value["source_sha256"].lower()].append({"manifest": manifest, "manifest_sha256": sha256(manifest), "source_sha256": value["source_sha256"].lower(), "state": state, "state_sha256": sha256(state)})
        except (OSError, ValueError) as exc:
            errors.append({"manifest": manifest.relative_to(root).as_posix(), "error": str(exc)})
    return catalog, errors


def compare_case(model, models_root, api_root, catalog):
    before = sha256(model)
    row = {"model": model.relative_to(models_root).as_posix(), "source_sha256": before}
    with zipfile.ZipFile(model) as archive:
        names = set(archive.namelist()); preview = "preview" in names and "dmodel.xml" not in names
    # Imports and XML normalization execute while process creation is forbidden.
    with patch("subprocess.Popen", side_effect=AssertionError("Offline evaluation forbids all subprocesses")):
        from advisor.library_model import _xml_state
        offline, coverage, _info = _xml_state(model)
    row.update(preview_only=preview, offline_coverage=coverage, offline_node_count=len(offline))
    linked = catalog.get(before, [])
    if preview:
        row.update(status="preview_only_no_api_comparison", source_unchanged=sha256(model) == before); return row
    if not linked:
        row.update(status="api_snapshot_not_found", source_unchanged=sha256(model) == before); return row
    if len({item["state_sha256"] for item in linked}) > 1:
        row.update(status="api_snapshot_ambiguous", api_candidates=[item["state"].relative_to(api_root).as_posix() for item in linked], source_unchanged=sha256(model) == before); return row
    linked = linked[0]
    from advisor.library_model import _read_api_state
    api = _read_api_state(linked["state"])
    pairing = pair_nodes(offline, api["nodes"])
    detailed, samples = [], []
    for pair in pairing.pop("pairs"):
        a, b = offline[pair["offline"]["index"]], api["nodes"][pair["api"]["index"]]
        comparison = compare_properties(a, b)
        selection = compare_selection(a, b)
        detailed.append({**pair, "properties": comparison, "selection": selection})
        names = {item["name"] for item in comparison["items"]}
        if a.get("tag") in {"pot1", "hf1"} or "hmax" in names or "hmin" in names:
            chosen = {"hmax", "hmin", "HeatFluxType", "h", "Text", "V0", "q0", "q0_input"}
            properties = []
            for item in comparison["items"]:
                if item["name"] in chosen:
                    properties.append({**item,
                        "offline_values": [value_receipt(prop.get("value")) for prop in a.get("normalized_properties", {}).get("properties", []) if prop.get("name") == item["name"]],
                        "api_values": [value_receipt(prop.get("value")) for prop in b.get("properties", []) if prop.get("name") == item["name"]]})
            samples.append({"tag": a["tag"], "offline_path": a["path"], "api_path": b["path"],
                "properties": properties, "selection": selection})
    summary = Counter(); reasons = Counter(); selections = Counter()
    for item in detailed:
        summary.update(item["properties"]["summary"]); reasons.update(item["properties"]["noncomparison_reasons"]); selections.update(item["selection"]["summary"])
    unsupported = sum(item["category"].startswith("unsupported:") for item in pairing["offline_only"])
    row.update(status="compared", api_snapshot=linked["state"].relative_to(api_root).as_posix(), api_snapshot_sha256=linked["state_sha256"],
        api_manifest=linked["manifest"].relative_to(api_root).as_posix(), api_manifest_source_sha256=linked.get("source_sha256", before),
        api_read_contract=api.get("read_contract"), api_node_count=len(api["nodes"]), api_truncated=api.get("truncated"),
        api_runtime_version=api.get("runtime_version"), api_file_saved_version=api.get("file_saved_version"), api_export_status=api.get("export_status"), api_read_errors=api.get("read_errors"),
        api_property_read_status=dict(Counter(prop.get("read_status", "not_reported") for node in api["nodes"] for prop in node.get("properties", []))),
        api_execution_flags={key: api.get(key) for key in ("geometry_run", "mesh_run", "solver_run", "defaults_generated")},
        node_summary={"unique_matches": len(detailed), "ambiguous_offline": len(pairing["ambiguous"]), "api_only": len(pairing["api_only"]), "offline_only": len(pairing["offline_only"]),
            "offline_only_comparable_categories": len(pairing["offline_only"]) - unsupported, "offline_other_structural_records": unsupported,
            "matched_by_category": dict(Counter(item["api"]["category"] for item in detailed)), "api_only_by_category": dict(Counter(item["category"] for item in pairing["api_only"])), "type_conflicts": len(pairing["type_conflicts"])},
        property_summary=dict(summary), property_noncomparison_reasons=dict(reasons), selection_summary=dict(selections), node_pairing=pairing, matched_nodes=detailed, samples=samples,
        source_unchanged=sha256(model) == before, api_snapshot_unchanged=sha256(linked["state"]) == linked["state_sha256"],
        api_manifest_unchanged=sha256(linked["manifest"]) == linked.get("manifest_sha256", sha256(linked["manifest"])))
    return row


def readable(report):
    lines = ["# 离线 MPH 与既有 API 观察对照", "", "只读解析相同 SHA256 的文件；未启动 COMSOL、网格、求解或 embedding。节点和属性数量均不是完整率。", "",
        "| 模型 | 状态 | 唯一配对 | 歧义 | API 单侧输出 | 离线单侧（可比类别） | XML 其他结构记录 |", "|---|---|---:|---:|---:|---:|---:|"]
    for row in report["models"]:
        count = row.get("node_summary", {})
        lines.append(f"| {row['model']} | {row['status']} | {count.get('unique_matches', '—')} | {count.get('ambiguous_offline', '—')} | {count.get('api_only', '—')} | {count.get('offline_only_comparable_categories', '—')} | {count.get('offline_other_structural_records', '—')} |")
    lines += ["", "属性对照（仅唯一配对的节点）：", "", "| 模型 | 相等 | 数值容差内 | 表达式文本不同 | 类型/值不同 | 矩阵布局待核验 | 离线未解码/部分 | API 失败/部分 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["models"]:
        if row["status"] != "compared": continue
        counts, reasons = row["property_summary"], row["property_noncomparison_reasons"]
        lines.append(f"| {row['model']} | {counts.get('equal', 0)} | {counts.get('numeric_within_tolerance', 0)} | {counts.get('expression_text_difference', 0)} | {counts.get('typed_value_difference', 0)} | {counts.get('structured_matrix_layout_not_verified', 0)} | {reasons.get('offline_undecoded', 0) + reasons.get('offline_partial', 0)} | {reasons.get('api_read_failed', 0) + reasons.get('api_read_partial', 0)} |")
    lines += ["", "既有 API 快照来源：", "", "| 模型 | 保存版本 | API 运行时版本 | MPH SHA256 前缀 |", "|---|---|---|---|"]
    for row in report["models"]:
        if row["status"] == "compared": lines.append(f"| {row['model']} | {row.get('api_file_saved_version')} | {row.get('api_runtime_version')} | {row['source_sha256'][:16]} |")
    focus = next((row for row in report["models"] if {"pot1", "hf1"} <= {sample["tag"] for sample in row.get("samples", [])}), None)
    if focus:
        lines += ["", f"抽样（{focus['model']}；重复属性保持多条，矩阵不重排）：", "", "| 节点/属性 | 状态 | 离线值预览 | API 值预览 |", "|---|---|---|---|"]
        for sample in focus["samples"]:
            for prop in sample["properties"]:
                left = "; ".join(value["preview"][:60] for value in prop["offline_values"]).replace("|", "\\|")
                right = "; ".join(value["preview"][:60] for value in prop["api_values"]).replace("|", "\\|")
                lines.append(f"| {sample['tag']}/{prop['name']} | {prop['status']} | {left} | {right} |")
            if sample["tag"] in {"pot1", "hf1"}: lines.append(f"| {sample['tag']}/selection | {json.dumps(sample['selection']['summary'], ensure_ascii=False)} | 保存 ID，非最终成员 | getter 观察，非物理验证 |")
    lines += ["", "限制：API-only 仅指未在当前离线结构化结果找到，不等同 MPH 未保存或必须 API，也不自动称为默认值。JSON 单列可在当前节点原始 XML 观察中确认存在但未结构化的属性；其他编码层未穷尽。XML 其他记录含列表、包装及未建立可比类别的技术节点，不能用节点数相除得出完整率。运行时版本、加载时处理和默认处理可能影响差异，版本与 export_status 逐例记录。类型或作用域不明确的唯一结构配对仍单列依据；有歧义不强配。表达式仅比较文本，不证明数学等价；API getter 和保存的选择 ID 均不证明有效启用、最终成员或物理正确。", "", "hmax/hmin、pot1 和 hf1 的值摘要与同维选择比较见 JSON 的 samples；值散列基于完整值，预览可能截短。inputEntities 与 entities 是两组独立 getter 观察，分别比较；保存 ID 与输入 ID 相同但与 current entities 不同，不能自动归咎解码错误。选择统计按 getter 分组，非唯一节点数。维度不同或不明时不强比，不解析 named/remaining/objects 为最终成员。"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--api-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="JSON report; a sibling .md report is also written")
    args = parser.parse_args(argv)
    models_root, api_root = args.models_dir.resolve(), args.api_root.resolve()
    if not models_root.is_dir() or not api_root.is_dir(): parser.error("Input directories must exist")
    output = args.output.resolve(); output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() != ".json" or output.is_relative_to(api_root): parser.error("Output must be a .json report outside the immutable API snapshot directory")
    reader_files = [APP_ROOT / "advisor" / name for name in ("library_model.py", "mph_saved_values.py", "mph_saved_selections.py")]
    reader_hashes = {path.relative_to(APP_ROOT).as_posix(): sha256(path) for path in reader_files if path.is_file()}
    catalog, errors = api_catalog(api_root); rows = []; started = time.monotonic()
    for model in sorted(models_root.rglob("*.mph")):
        try: rows.append(compare_case(model, models_root, api_root, catalog))
        except Exception as exc: rows.append({"model": model.relative_to(models_root).as_posix(), "status": "evaluation_error", "error": str(exc)})
    report = {"schema": "offline-mph-comparison/v1", "inputs": {"models_dir": str(args.models_dir), "api_root": str(args.api_root)},
        "policy": {"offline_reader": "advisor.library_model._xml_state only", "api_never_inputs_offline_reader": True, "subprocesses_forbidden_during_offline_read": True,
            "matching": "mutually unique category/tag/type and semantic ancestor scopes; XML-only explicit component evidence; no forced ambiguous matches",
            "numeric_comparison_tolerance": {"relative": 1e-12, "absolute": 1e-15}, "no_semantic_completeness_percentage": True},
        "reader_source_sha256": reader_hashes, "reader_unchanged": all(path.is_file() and sha256(path) == reader_hashes.get(path.relative_to(APP_ROOT).as_posix()) for path in reader_files),
        "model_status_counts": dict(Counter(row["status"] for row in rows)), "api_catalog_errors": errors, "models": rows, "seconds": round(time.monotonic() - started, 3)}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(readable(report), encoding="utf-8")
    print(json.dumps({"json": str(output), "report": str(output.with_suffix('.md')), "status_counts": report["model_status_counts"], "seconds": report["seconds"]}, ensure_ascii=False))
    return 1 if errors or not rows or not report["reader_unchanged"] or any(row["status"] in {"evaluation_error", "api_snapshot_not_found", "api_snapshot_ambiguous"} or row.get("source_unchanged") is False or row.get("api_snapshot_unchanged") is False or row.get("api_manifest_unchanged") is False for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
