"""Read STEP geometry as geometry, without inventing a simulation definition.

sim-cli supplies real STEP observations. A labelled native Part 21 metadata
reader provides a fallback/full declaration inventory. Optional isolated Gmsh /
OpenCASCADE imports supply topology and measures; no meshing or solve is run.
"""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time

from .model_reader import MAX_FILE_BYTES, MAX_ITEMS, ModelInspectionError, PROJECT_ROOT, _python_command, parser_status
from .process_runner import run_text


MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 200_000
MAX_RECORD_BYTES = 1024 * 1024
MAX_BODY_SUMMARIES = 200
PARSER_TIMEOUT = 25
_QUOTED = re.compile(r"'(?:[^']|'')*'", re.DOTALL)
_TOKENS = re.compile(r"(?P<quoted>'(?:[^']|'')*')|(?P<comment>/\*.*?\*/)|(?P<end>;)", re.DOTALL)
_RESULT_PREFIX = "SIM_ADVISOR_GEOMETRY_RESULT="

_KERNEL_SCRIPT = r'''
import json,sys,math,gmsh
gmsh.initialize([],readConfigFiles=False,run=False)
try:
    gmsh.option.setNumber('General.Terminal',0)
    gmsh.option.setNumber('General.NumThreads',1)
    gmsh.option.setString('Geometry.OCCTargetUnit','M')
    gmsh.model.add('geometry inspection')
    gmsh.model.occ.importShapes(sys.argv[1],highestDimOnly=False,format='step')
    gmsh.model.occ.synchronize()
    entities={d:gmsh.model.getEntities(d) for d in range(4)}
    total=sum(len(v) for v in entities.values())
    bounds=list(gmsh.model.getBoundingBox(-1,-1)) if total else None
    if bounds is not None and not all(math.isfinite(v) for v in bounds): bounds=None
    volume=sum(gmsh.model.occ.getMass(3,t) for _,t in entities[3]) if 0<len(entities[3])<=5000 else None
    if volume is not None and not math.isfinite(volume): volume=None
    bodies=[]
    for _,tag in entities[3][:int(sys.argv[2])]:
        boundary=gmsh.model.getBoundary([(3,tag)],combined=False,oriented=False,recursive=False)
        bodies.append({'tag':tag,'name':gmsh.model.getEntityName(3,tag)[:4096],
                       'bounding_box_values':list(gmsh.model.getBoundingBox(3,tag)),
                       'volume_value':gmsh.model.occ.getMass(3,tag),
                       'surface_count':len({t for d,t in boundary if d==2})})
    value={'engine':'gmsh/OpenCASCADE','version':gmsh.__version__,'status':'imported' if total else 'empty',
           'topology':{name:len(entities[d]) for d,name in enumerate(('vertices','edges','faces','solids'))},
           'bounding_box_values':bounds,'volume_value':volume,'target_unit':'m','validity':'not_checked',
           'bodies':bodies,'bodies_total':len(entities[3]),'bodies_truncated':len(entities[3])>len(bodies)}
    print('SIM_ADVISOR_GEOMETRY_RESULT='+json.dumps(value,allow_nan=False))
finally:
    gmsh.finalize()
'''


def _strings(text: str) -> list[str]:
    values = [match.group()[1:-1].replace("''", "'") for match in _QUOTED.finditer(text)]
    return [value if len(value) <= 4096 else value[:4096] + " [truncated]" for value in values]


def _records(text: str):
    """Split semicolon records while preserving quoted strings and comments."""
    parts, start = [], 0
    for token in _TOKENS.finditer(text):
        if token.lastgroup == "comment":
            parts.extend((text[start:token.start()], " "))
            start = token.end()
        elif token.lastgroup == "end":
            parts.append(text[start:token.start()])
            yield "".join(parts).strip()
            parts, start = [], token.end()


def _entity_types(rhs: str) -> list[str]:
    simple = re.match(r"\s*([A-Z][A-Z0-9_]*)\s*\(", rhs, re.I)
    if simple:
        return [simple.group(1).upper()]
    # Complex entities declare several types at the first parenthesis level;
    # nested value constructors are not separate STEP entity declarations.
    value = _QUOTED.sub("''", rhs)
    depth, previous, result = 0, None, []
    for token in re.finditer(r"[A-Z][A-Z0-9_]*|[()]", value, re.I):
        word = token.group().upper()
        if word == "(":
            if depth == 1 and previous:
                result.append(previous)
            depth += 1
            previous = None
        elif word == ")":
            depth -= 1
            previous = None
        else:
            previous = word
    return result


def _declared_unit(rhs: str) -> str | None:
    match = re.search(r"SI_UNIT\s*\(\s*(\$|\.[A-Z]+\.)\s*,\s*\.METRE\.\s*\)", rhs, re.I)
    if match:
        return {"$": "m", ".MILLI.": "mm", ".CENTI.": "cm", ".DECI.": "dm",
                ".MICRO.": "um", ".NANO.": "nm", ".KILO.": "km"}.get(match.group(1).upper())
    conversion = re.search(r"CONVERSION_BASED_UNIT\s*\(\s*'([^']+)'", rhs, re.I)
    if conversion:
        return {"INCH": "in", "INCHES": "in", "FOOT": "ft", "FEET": "ft"}.get(conversion.group(1).upper())
    return None


def _native_metadata(data: bytes, complete: bool) -> dict:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    if not re.match(r"\s*ISO-10303-21\s*;", text, re.I):
        raise ModelInspectionError("STEP file must contain an ISO-10303-21 exchange structure")
    schemas, products, units, counts = [], [], [], Counter()
    observed, skipped, ended, in_data, data_seen, limited = 0, 0, False, False, False, not complete
    for record in _records(text):
        upper = record.upper()
        if upper.startswith("FILE_SCHEMA"):
            schemas = _strings(record)[:MAX_ITEMS]
        elif upper == "DATA" or upper.startswith("DATA(") or upper.startswith("DATA ("):
            in_data = True
            data_seen = True
        elif upper == "ENDSEC":
            in_data = False
        elif upper == "END-ISO-10303-21":
            ended = True
        elif in_data:
            match = re.match(r"#(\d+)\s*=\s*(.*)\Z", record, re.DOTALL)
            if not match:
                skipped += 1
                continue
            observed += 1
            if observed > MAX_RECORDS:
                observed -= 1
                limited = True
                break
            if len(record) > MAX_RECORD_BYTES:
                skipped += 1
                limited = True
                continue
            ref, rhs = "#" + match.group(1), match.group(2)
            types = _entity_types(rhs)
            if not types:
                skipped += 1
            counts.update(types)
            if "PRODUCT" in types and len(products) < MAX_ITEMS:
                names = _strings(rhs)
                products.append({"native_ref": ref, "name": names[1] if len(names) > 1 else (names[0] if names else ""),
                                 "source": f"native STEP: DATA {ref}=PRODUCT"})
            if "LENGTH_UNIT" in types and len(units) < MAX_ITEMS:
                units.append({"native_ref": ref, "unit": _declared_unit(rhs), "expression": rhs[:4096],
                              "source": f"native STEP: DATA {ref}, length-unit declaration; context not resolved by metadata reader"})
    if complete and not limited and not ended:
        raise ModelInspectionError("STEP exchange structure is incomplete (missing END-ISO-10303-21)")
    if complete and not data_seen:
        raise ModelInspectionError("STEP exchange structure has no DATA section")
    type_limit = len(counts) > MAX_ITEMS
    return {"schemas": schemas, "products": products, "products_truncated": counts["PRODUCT"] > len(products),
            "length_unit_declarations": units, "length_units_truncated": counts["LENGTH_UNIT"] > len(units),
            "raw_entity_counts": dict(sorted(counts.items())[:MAX_ITEMS]), "raw_entity_types_total": len(counts),
            "raw_entity_types_truncated": type_limit, "records_observed": observed,
            "skipped_records": skipped, "metadata_status": "partial" if limited or skipped or type_limit else "parsed",
            "raw_entity_counts_source": "native STEP: observed DATA declaration types (not validated topology)",
            "raw_entity_counts_status": "partial" if limited or skipped or type_limit else "complete"}


def _scan_step(path: Path, status: dict) -> dict:
    result = run_text([*status["command"], "--json", "scan", str(path), "--full", "--no-recursive", "--limit", "1", "--format", "step"],
                      "", PROJECT_ROOT, PARSER_TIMEOUT)
    payload = json.loads(result.stdout)
    assets = payload.get("assets", [])
    if result.returncode or not payload.get("ok") or len(assets) != 1 or not assets[0].get("ok") or assets[0].get("format") != "step":
        raise RuntimeError("sim-cli did not return one successful STEP asset")
    return payload


@lru_cache(maxsize=4)
def _kernel_probe(python: str) -> dict:
    try:
        result = run_text([python, "-c", "import gmsh,json; print(json.dumps({'version':gmsh.__version__}))"], "", PROJECT_ROOT, 8, 65536)
        if result.returncode:
            raise RuntimeError("Gmsh/OpenCASCADE import unavailable")
        version = json.loads(result.stdout)["version"]
        return {"available": True, "engine": "gmsh/OpenCASCADE", "version": version,
                "detail": "独立 CAD 内核只读导入；不生成网格或运行求解。"}
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
        return {"available": False, "engine": "gmsh/OpenCASCADE", "version": None,
                "detail": "CAD 内核不可用；保留 STEP 元数据，拓扑/尺寸/体积未知。"}


def geometry_status() -> dict:
    """Actual optional kernel readiness; independent of COMSOL availability."""
    return dict(_kernel_probe(_python_command()[0]))


def _kernel_geometry(path: Path) -> dict:
    deadline = time.monotonic() + PARSER_TIMEOUT
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("CAD kernel inspection deadline exhausted")
        result = run_text([_python_command()[0], "-c", _KERNEL_SCRIPT, str(path), str(MAX_BODY_SUMMARIES)], "", PROJECT_ROOT, remaining)
        # Some native OCCT imports fail transiently on this Windows build.
        # One fresh-process retry shares the same total deadline; failures
        # remain isolated and are never converted into fabricated measures.
        native_fault = result.returncode in {3221225477, -1073741819, -11, -6} or "access violation" in result.stderr.lower()
        if attempt == 0 and result.returncode and native_fault:
            continue
        values = [line[len(_RESULT_PREFIX):] for line in result.stdout.splitlines() if line.startswith(_RESULT_PREFIX)]
        if result.returncode or len(values) != 1:
            raise RuntimeError("CAD kernel did not return a geometry observation")
        observation = json.loads(values[0])
        observation["import_retry_count"] = attempt
        return observation


def _bounds_record(values, source):
    if (not isinstance(values, list) or len(values) != 6
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)):
        return None
    return {"min": values[:3], "max": values[3:], "size": [values[i+3] - values[i] for i in range(3)],
            "unit": "m", "source": source + "; includes CAD bounding tolerances"}


def _inspection_capabilities(geometry):
    kernel = geometry["kernel"]
    bodies = geometry["bodies"]
    geometry["capabilities"] = {
        "overall_bounding_box": "observed" if kernel["bounding_box"] is not None else "unavailable",
        "topology_counts": "observed" if kernel["topology"] is not None else "unavailable",
        "solid_volume_sum": "observed" if kernel["volume"] is not None else "unavailable",
        "body_summaries": geometry["body_summary_status"],
        "snapshot_scoped_body_ids": "observed" if bodies else "unavailable",
        "per_body_bounds": "observed" if bodies and all(b["bounding_box"] is not None for b in bodies) else "unavailable",
        "per_body_volume": "observed" if bodies and all(b["volume"] is not None for b in bodies) else "unavailable",
        "per_body_surface_count": "observed" if bodies else "unavailable",
        "cross_edit_persistent_ids": "not_implemented",
        "per_face_inventory": "not_implemented",
        "per_face_surface_type_and_area": "not_implemented",
        "face_curvature": "not_implemented",
        "face_adjacency": "not_implemented",
        "minimum_gap": "not_implemented",
        "wall_thickness": "not_implemented",
        "assembly_tree_and_instances": "not_implemented",
        "geometry_views_and_highlights": "not_implemented",
        "feature_recognition_holes_fillets_threads": "not_implemented",
        "physical_relevance": "requires_goal_and_user_or_physics_evidence",
        "simplification_effect_on_target_outputs": "not_evaluated",
        "cad_modification": "not_enabled",
    }
    geometry["inspection_limits"] = [
        "逐体摘要描述导入 CAD 实体，不等同于装配零件、计算域或具有材料/边界条件的对象。",
        "body id 仅绑定当前文件指纹、内核版本和固定导入设置；跨编辑、重新导出或更换内核时不承诺同一 ID。",
        "当前应用没有逐面清单/类型/面积/曲率、面邻接、最小间隙或局部厚度数据；库存在 API 不代表已集成这些检查。",
        "当前没有装配实例树、真实接触关系、带标号视图、剖切或高亮截图，不能据此定位具体孔、圆角、螺纹或密封间隙。",
        "小体积、小包围盒或较少面数不证明与目标无关；简化须考虑目标量、载荷/传热/流动路径、物理尺度及用户确认。",
        "可给条件化的保留/省略/等效化建议；未计算简化前后目标量差异，不能声称误差已满足要求。",
    ]
    if geometry["body_summaries_truncated"]:
        geometry["inspection_limits"].append(f"逐体列表仅返回前 {MAX_BODY_SUMMARIES} 项；未列出的实体仍属于未检查部分。")


def inspect_geometry(path: str | Path) -> dict:
    model_path = Path(path).expanduser().resolve(strict=True)
    if not model_path.is_file() or model_path.suffix.lower() not in {".stp", ".step"}:
        raise ModelInspectionError("Geometry input must be a local .stp or .step file")
    before = model_path.stat()
    if before.st_size > MAX_FILE_BYTES:
        raise ModelInspectionError(f"STEP file exceeds {MAX_FILE_BYTES // 1024 // 1024} MiB inspection limit")
    digest, chunks, observed_bytes = hashlib.sha256(), [], 0
    with model_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            if observed_bytes < MAX_METADATA_BYTES:
                chunks.append(chunk[:MAX_METADATA_BYTES - observed_bytes])
            observed_bytes += len(chunk)
            if observed_bytes > MAX_FILE_BYTES:
                raise ModelInspectionError("STEP file grew beyond the inspection size limit")
    geometry = _native_metadata(b"".join(chunks), observed_bytes <= MAX_METADATA_BYTES)
    geometry["source_format"] = "step"
    geometry["sim_cli_metadata_status"] = "not_run"
    geometry.update(bodies=[], bodies_total=None, body_summaries_truncated=False, body_summary_status="unavailable",
                    body_id_scope="file fingerprint + gmsh version + OCCTargetUnit=M + imported dimension/tag; not persistent across edits")
    fingerprint = digest.hexdigest()
    snapshot = {"id": f"model-{fingerprint[:20]}", "file_name": model_path.name, "path": str(model_path),
                "format": "step", "version": None, "asset_kind": "cad_geometry", "simulation_state": "not_defined",
                "definition_status": {key: "not_defined" for key in ("components", "physics", "materials", "studies", "boundary_conditions", "mesh")},
                "parser": "native-step-metadata (fallback)", "facts": [], "components": [], "physics": [],
                "materials": [], "studies": [], "parameters": {}, "selections": [], "geometry": geometry,
                "unknowns": ["此输入是 CAD 几何；物理场、材料、边界/初始条件、网格和研究尚未定义，不是解析失败。",
                             "不能从文件名、零件名或几何外观确定材料、流体/固体区域、载荷、入口/出口或支承。",
                             "STEP 产品记录不是 COMSOL 组件或已确认的物理域；CAD 标签不等于 COMSOL 边界编号。",
                             "几何有效性、可网格化性、装配接触/间隙、重叠与仿真适用性未验证。"],
                "warnings": [], "fingerprint": fingerprint}

    def fact(key, label, value, source):
        snapshot["facts"].append({"key": key, "label": label, "value": value, "source": source})

    fact("file.size_bytes", "文件大小（字节）", before.st_size, "filesystem: stat().st_size")
    fact("asset.kind", "输入类型", "CAD geometry; simulation not defined", "STEP exchange structure, not a solver model")
    fact("step.schemas", "STEP 模式声明", geometry["schemas"], "native STEP: HEADER FILE_SCHEMA")
    fact("step.raw_entity_counts", "STEP 实体声明计数（非内核拓扑数）", geometry["raw_entity_counts"], geometry["raw_entity_counts_source"])
    fact("step.length_unit_declarations", "文件长度单位声明", geometry["length_unit_declarations"], "native STEP: DATA LENGTH_UNIT records")
    if geometry["metadata_status"] == "partial":
        snapshot["warnings"].append("原生 STEP 元数据读取达到限制或有未识别记录；声明计数是部分观察，不能当作完整拓扑。")
    status = parser_status()
    snapshot["parser_detail"] = {"sim_cli_available": status["available"], "sim_cli_version": status.get("version"),
                                 "scan_succeeded": False, "native_metadata_supplement": True}
    if status["available"]:
        try:
            payload = _scan_step(model_path, status)
            data = payload["assets"][0]["summary"]["data"]
            snapshot["parser"] = "sim-cli STEP scan + native metadata supplement"
            snapshot["parser_detail"].update(scan_succeeded=True, engine=payload.get("engine"), schema=payload.get("schema_version"))
            geometry["sim_cli_metadata_status"] = "partial" if data.get("truncated") or data.get("skipped_records") else "parsed"
            fact("scan.step_records_observed", "sim-cli 观察到的 STEP 记录数", data.get("records_observed"), "sim-cli scan: assets[0].summary.data.records_observed")
            if geometry["sim_cli_metadata_status"] == "partial":
                snapshot["warnings"].append("sim-cli STEP 元数据报告部分记录/截断；原生声明计数独立标注完整性。")
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, subprocess.TimeoutExpired):
            geometry["sim_cli_metadata_status"] = "failed"
            snapshot["warnings"].append("sim-cli STEP 扫描未完成；已使用明确标注的原生元数据回退。")
    else:
        snapshot["warnings"].append("sim-cli 不可用；STEP 使用标准库元数据回退，未伪装为 sim-cli 结果。")
    kernel_status = geometry_status()
    geometry["kernel"] = {**kernel_status, "status": "not_run", "unit": None, "bounding_box": None,
                          "topology": None, "volume": None, "validity": "not_checked"}
    if kernel_status["available"]:
        try:
            observation = _kernel_geometry(model_path)
            source = "gmsh " + str(observation["version"]) + "/OpenCASCADE: read-only STEP import, OCCTargetUnit=M"
            kernel = geometry["kernel"]
            kernel.update(status=observation["status"], topology=observation["topology"], source=source)
            kernel["import_retry_count"] = observation.get("import_retry_count", 0)
            if kernel["import_retry_count"]:
                snapshot["warnings"].append("CAD 内核首次导入发生原生异常，隔离重试后取得读数；几何有效性仍未验证。")
            fact("cad.topology", "导入后 CAD 内核拓扑数（非 STEP 声明数）", observation["topology"], source + "; gmsh.model.getEntities(dim)")
            unit_known = (geometry["metadata_status"] == "parsed" and bool(geometry["length_unit_declarations"])
                          and geometry["raw_entity_counts"].get("LENGTH_UNIT", 0) == len(geometry["length_unit_declarations"])
                          and all(x.get("unit") for x in geometry["length_unit_declarations"]))
            if unit_known:
                kernel["unit"] = "m"
                bounds = observation.get("bounding_box_values")
                kernel["bounding_box"] = _bounds_record(bounds, source)
                if kernel["bounding_box"] is not None:
                    fact("cad.bounding_box", "内核包围盒（米，含 CAD 容差）", kernel["bounding_box"], source + "; gmsh.model.getBoundingBox(-1,-1)")
                volume = observation.get("volume_value")
                if isinstance(volume, (int, float)) and math.isfinite(volume) and volume >= 0:
                    kernel["volume"] = {"value": volume, "unit": "m^3", "method": "sum of imported solid volumes; overlaps not subtracted", "source": source}
                    fact("cad.volume_sum", "导入实体体积之和（不扣除重叠）", kernel["volume"], source + "; sum(gmsh.model.occ.getMass(3,tag))")
            else:
                snapshot["unknowns"].append("文件长度单位声明缺失或未识别；不把内核默认缩放当作真实尺寸，包围盒/体积保持未知。")
            if isinstance(observation.get("bodies"), list):
                geometry["bodies_total"] = observation.get("bodies_total")
                geometry["body_summaries_truncated"] = bool(observation.get("bodies_truncated")) or len(observation["bodies"]) > MAX_BODY_SUMMARIES
                geometry["body_summary_status"] = "partial" if geometry["body_summaries_truncated"] else "observed"
                for item in observation["bodies"][:MAX_BODY_SUMMARIES]:
                    tag = item["tag"]
                    identity = f"{fingerprint}|gmsh={observation['version']}|OCCTargetUnit=M|3:{tag}"
                    body_source = source + f"; imported body (dim=3,tag={tag})"
                    volume = item.get("volume_value")
                    geometry["bodies"].append({
                        "id": "body-" + hashlib.sha256(identity.encode()).hexdigest()[:20],
                        "kernel_dim_tag": [3, tag], "imported_name": str(item.get("name", ""))[:4096],
                        "bounding_box": _bounds_record(item.get("bounding_box_values"), body_source) if unit_known else None,
                        "volume": {"value": volume, "unit": "m^3", "source": body_source}
                            if unit_known and isinstance(volume, (int, float)) and math.isfinite(volume) and volume >= 0 else None,
                        "surface_count": item.get("surface_count"), "physical_role": "unknown", "source": body_source})
                fact("cad.body_summaries", "逐体摘要（ID 仅在当前快照范围有效）",
                     {"returned": len(geometry["bodies"]), "total": geometry["bodies_total"],
                      "truncated": geometry["body_summaries_truncated"], "id_scope": geometry["body_id_scope"]},
                     source + "; getEntities(3), getBoundary, getBoundingBox and occ.getMass")
            snapshot["parser"] += " + gmsh/OpenCASCADE geometry"
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, subprocess.TimeoutExpired):
            geometry["kernel"]["status"] = "failed"
            snapshot["warnings"].append("CAD 内核导入失败或达到资源限制；保留 STEP 元数据，拓扑/尺寸/体积未知。")
    if geometry["kernel"]["topology"] is None:
        snapshot["unknowns"].append("未获得 CAD 内核拓扑；STEP MANIFOLD_SOLID_BREP/ADVANCED_FACE 声明数不能替代有效实体/面的数量。")
    if geometry["kernel"]["bounding_box"] is None:
        snapshot["unknowns"].append("具有已知物理单位的几何包围盒未知。")
    if geometry["kernel"]["volume"] is None:
        snapshot["unknowns"].append("实体体积未知；不从外包围盒推算实体体积。")
    _inspection_capabilities(geometry)
    after = model_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ModelInspectionError("Geometry changed during inspection; retry with a stable file")
    return snapshot
