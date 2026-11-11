"""Bounded, read-only MPH inspection. No COMSOL process or vendor API is used.

sim-cli's solver-free ``scan`` is the primary engine. The small native XML
reader supplies scoped labels/global parameter expressions and is an explicitly
labelled fallback when sim-cli is unavailable. Neither reader proves effective
activation, selection membership, solution freshness, or physical validity.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import html
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib

from .build_control import check_cancelled
from .process_runner import _ProcessTree


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_CONTAINER_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_METADATA_TOTAL = 32 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_ITEMS = 200
MAX_TEXT = 4096
CLI_TIMEOUT_SECONDS = 25
CLI_OUTPUT_BYTES = 4 * 1024 * 1024


class ModelInspectionError(ValueError):
    """Invalid, unsupported, or resource-limit-exceeding model input."""


class _CLIError(RuntimeError):
    pass


def _run_bounded(command: list[str], *, timeout: float = CLI_TIMEOUT_SECONDS,
                 max_output: int = CLI_OUTPUT_BYTES) -> tuple[int, bytes, bytes]:
    """Drain both pipes concurrently, kill on deadline/combined byte limit."""
    check_cancelled()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    isolation = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == "nt" else {"start_new_session": True})
    check_cancelled()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, cwd=PROJECT_ROOT, env=env,
                               **isolation)
    tree = _ProcessTree(process)
    outputs = [bytearray(), bytearray()]
    count = 0
    lock = threading.Lock()
    overflow = threading.Event()

    def drain(stream, index):
        nonlocal count
        try:
            while chunk := stream.read(65536):
                with lock:
                    remaining = max(0, max_output - count)
                    outputs[index].extend(chunk[:remaining])
                    count += len(chunk)
                    if count > max_output:
                        overflow.set()
                        try:
                            process.kill()
                        except OSError:
                            pass
        finally:
            stream.close()

    workers = [threading.Thread(target=drain, args=(stream, index), daemon=True)
               for index, stream in enumerate((process.stdout, process.stderr))]
    for worker in workers:
        worker.start()
    completed = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                process.wait(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        check_cancelled()
        completed = True
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise _CLIError(f"sim-cli exceeded the {timeout:g} second inspection deadline") from None
    finally:
        tree.close()
        for worker in workers:
            worker.join(timeout=2)
        if os.name == "nt" and (not completed or overflow.is_set()):
            # Match run_text's bounded Windows cwd-handle settling on aborts.
            time.sleep(0.05)
    if overflow.is_set():
        raise _CLIError(f"sim-cli output exceeded {max_output} bytes")
    return process.returncode, bytes(outputs[0]), bytes(outputs[1])


def _python_command() -> tuple[str, ...]:
    override = os.environ.get("SIM_ADVISOR_SIM_PYTHON")
    if override:
        return (str(Path(override).expanduser().resolve()), "-m", "sim")
    candidate = PROJECT_ROOT / ".model-env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # A local isolated environment always wins; never alter the server's env.
    return (str(candidate if candidate.is_file() else Path(sys.executable)), "-m", "sim")


@lru_cache(maxsize=4)
def _probe(command: tuple[str, ...]) -> dict:
    try:
        probe = [command[0], "-c", "import importlib.metadata as m; import sim,simparse,json; "
                 "print(json.dumps({'version':m.version('sim-cli-core'),'engine_version':m.version('simparse')}))"]
        code, out, _ = _run_bounded(probe, timeout=8, max_output=65536)
        if code:
            raise _CLIError("sim-cli-core or its native simparse dependency cannot be imported")
        versions = json.loads(out)
        # Check the real command surface too; importing a package alone is insufficient.
        code, _, _ = _run_bounded([*command, "--version"], timeout=8, max_output=65536)
        if code:
            raise _CLIError("sim-cli --version failed")
        return {"available": True, "mode": "sim-cli scan", **versions,
                "engine": "simparse", "command": list(command),
                "detail": "真实 sim-cli 只读 scan 可用；不启动 COMSOL，不验证求解结果。"}
    except (OSError, ValueError, _CLIError) as exc:
        return {"available": False, "mode": "native-mph-container (fallback)",
                "version": None, "engine": None, "command": list(command),
                "detail": f"sim-cli 不可用：{str(exc)[:240]}；使用明确标注的标准库 MPH XML 回退。"}


def parser_status() -> dict:
    """Report the actual local import/CLI probe, cached for this server process."""
    result = dict(_probe(_python_command()))
    result["limitations"] = ["仅保存文件的浅层观察；有效启用状态与物理正确性未知。",
                             "不解析二进制网格质量、完整选择集或解场；不进行求解。"]
    return result


def _safe_container(path: Path) -> tuple[dict[str, bytes], list[str]]:
    """Validate limits before sim-cli sees an uploaded container; never extract."""
    metadata = {}
    try:
        with zipfile.ZipFile(path, "r") as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise ModelInspectionError(f"MPH container exceeds {MAX_ENTRIES} entries")
            if sum(item.file_size for item in entries) > MAX_CONTAINER_BYTES:
                raise ModelInspectionError("MPH expanded size exceeds the inspection limit")
            names = [item.filename for item in entries]
            if len(names) != len(set(names)):
                raise ModelInspectionError("MPH container has duplicate member names")
            if not any(name in names for name in ("modelinfo.xml", "dmodel.xml", "model.xml")):
                raise ModelInspectionError("ZIP does not contain recognizable COMSOL model metadata")
            total = 0
            for item in entries:
                if item.flag_bits & 1:
                    raise ModelInspectionError("Encrypted MPH members cannot be inspected")
                if item.filename.lower().endswith((".xml", ".json", ".txt")) or item.filename == "fileversion":
                    total += item.file_size
                    if item.file_size > MAX_METADATA_BYTES or total > MAX_METADATA_TOTAL:
                        raise ModelInspectionError("MPH metadata exceeds the inspection size limit")
                    if item.file_size > max(1, item.compress_size) * 1000:
                        raise ModelInspectionError("MPH metadata compression ratio exceeds the inspection limit")
                    with archive.open(item) as member:
                        data = member.read(MAX_METADATA_BYTES + 1)
                    if len(data) > MAX_METADATA_BYTES:
                        raise ModelInspectionError("MPH metadata exceeds the inspection size limit")
                    if item.filename.lower().endswith(".xml"):
                        declarations = data.replace(b"\x00", b"").upper()
                        if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
                            raise ModelInspectionError("XML entity/DOCTYPE declarations are not accepted")
                    if item.filename in ("modelinfo.xml", "dmodel.xml"):
                        metadata[item.filename] = data
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error) as exc:
        raise ModelInspectionError(f"Cannot read MPH ZIP container: {str(exc)[:180]}") from exc
    return metadata, names


def _text(value) -> str:
    value = str(value or "")
    return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + " [truncated]"


def _fact(snapshot: dict, key: str, label: str, value, source: str):
    snapshot["facts"].append({"key": key, "label": label, "value": value, "source": source})


def _native_metadata(snapshot: dict, metadata: dict[str, bytes]) -> None:
    roots = {}
    for name, data in metadata.items():
        try:
            roots[name] = ET.fromstring(data)
        except ET.ParseError:
            snapshot["warnings"].append(f"{name} XML 无法解析；相应信息保持未知。")
    info = roots.get("modelinfo.xml")
    if info is not None:
        if not snapshot["version"]:
            snapshot["version"] = info.get("comsolVersion")
        for key, label in (("comsolVersion", "保存版本"), ("title", "模型标题"),
                           ("description", "模型说明"), ("nodeType", "保存节点类型"),
                           ("isRunnable", "文件记录的可运行标记"),
                           ("lastComputationVersion", "文件记录的上次计算版本")):
            if info.get(key):
                _fact(snapshot, f"metadata.{key}", label, _text(info.get(key)),
                      f"native XML: modelinfo.xml:/modelInfo/@{key}")
        geometries = [{"tag": x.get("tag"), "dimension": x.get("dimension"),
                       "source": "native XML: modelinfo.xml:/modelInfo/geometryInfo/geom"}
                      for x in info.findall("./geometryInfo/geom")][:MAX_ITEMS]
        if geometries:
            _fact(snapshot, "geometry", "几何记录", geometries, "native XML: modelinfo.xml:/modelInfo/geometryInfo")
    model = roots.get("dmodel.xml")
    if model is None:
        snapshot["unknowns"].append("dmodel.xml 缺失或不可读；组件、全局参数及节点作用域可能未知。")
        return

    def node_record(element, xml_tag, scope=""):
        record = {"tag": _text(element.get("tag")), "label": _text(element.get("name") or element.get("tag")),
                  "type": _text(element.get("op")) or None,
                  "source": f"native XML: dmodel.xml:{scope}//{xml_tag}[@tag='{element.get('tag', '')}']"}
        parent = element.findtext("storedParent")
        if parent:
            record["stored_parent"] = _text(parent)
        flags = element.findtext("entityFlags") or ""
        record["activation"] = "saved inactive flag" if "INACTIVE" in flags.split("|") else "unknown"
        return record

    for field, tag in (("components", "ModelNode"), ("physics", "Physics"),
                       ("materials", "Material"), ("studies", "Study"),
                       ("selections", "SelectionFeature")):
        all_nodes = list(model.iter(tag))
        records = []
        for element in all_nodes[:MAX_ITEMS]:
            record = node_record(element, tag)
            if tag == "Study":
                steps = list(element.iter("StudyFeature"))
                record["steps"] = [node_record(x, "StudyFeature", f"//Study[@tag='{element.get('tag', '')}']")
                                   for x in steps[:MAX_ITEMS]]
                record["steps_total"] = len(steps)
                record["steps_truncated"] = len(steps) > MAX_ITEMS
            if tag == "Physics":
                features = list(element.iter("PhysicsFeature"))
                record["features"] = [node_record(x, "PhysicsFeature", f"//Physics[@tag='{element.get('tag', '')}']")
                                      for x in features[:MAX_ITEMS]]
                record["features_total"] = len(features)
                record["features_truncated"] = len(features) > MAX_ITEMS
            explicit = element.find("./selection/explicit")
            if explicit is None:
                explicit = element.find("./outputSelection/explicit")
            if explicit is not None:
                record["selection_record"] = {k: _text(v) for k, v in explicit.attrib.items()}
                record["selection_status"] = "raw saved encoding; effective membership unknown"
            records.append(record)
        if records:
            snapshot[field] = records
        if len(all_nodes) > MAX_ITEMS:
            snapshot["warnings"].append(f"{tag} 共 {len(all_nodes)} 项，仅返回前 {MAX_ITEMS} 项。")

    # Only expressions within ModelParam are global parameters. simparse 0.4.0
    # also exposes hundreds of unscoped material/physics defaults as parameters.
    count = 0
    for group in model.iter("ModelParam"):
        for expression in group.iter("expressions"):
            name, value = expression.get("name"), expression.get("expr")
            if not name or value is None:
                continue
            count += 1
            if count <= MAX_ITEMS:
                snapshot["parameters"][_text(name)] = _text(value)
                _fact(snapshot, f"parameter.{_text(name)}", f"全局参数 {_text(name)}", _text(value),
                      f"native XML: dmodel.xml://ModelParam//expressions[@name='{_text(name)}']/@expr")
    if count > MAX_ITEMS:
        snapshot["warnings"].append(f"全局参数共 {count} 项，仅返回前 {MAX_ITEMS} 项。")
    unsupported = sorted({element.tag for element in model if element.get("tag")}
                         - {"ModelParam", "PhysicsList", "MaterialList", "ModelNodeList", "StudyList", "SelectionList"})
    if unsupported:
        snapshot["unknowns"].append("未语义解析的 dmodel.xml 顶层节点类别：" + ", ".join(unsupported[:MAX_ITEMS]))
    if info is None and model.findtext("comsolVersion"):
        _fact(snapshot, "metadata.comsolVersion", "保存版本原始记录", _text(model.findtext("comsolVersion")),
              "native XML: dmodel.xml:/Model/comsolVersion")


def _scan(path: Path, status: dict) -> dict:
    command = [*status["command"], "--json", "scan", str(path), "--full", "--no-recursive",
               "--limit", "1", "--format", "comsol-mph"]
    code, out, _ = _run_bounded(command)
    try:
        payload = json.loads(out)
    except (ValueError, UnicodeError):
        raise _CLIError("sim-cli returned invalid JSON") from None
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if code or not payload.get("ok") or not isinstance(assets, list) or len(assets) != 1 or not assets[0].get("ok"):
        reason = payload.get("error_code") or payload.get("status") or f"exit {code}"
        raise _CLIError(f"sim-cli scan did not return a successful single asset ({str(reason)[:100]})")
    if payload.get("truncated") or assets[0].get("format") != "comsol-mph":
        raise _CLIError("sim-cli returned truncated output or an unexpected format")
    return payload


def inspect_model(path: str | Path) -> dict:
    """Return a provenance-bearing snapshot, or raise ValueError/OSError.

    Limits are checked before launching the parser. All extracted values are
    saved-file observations, never inferred engineer decisions or solve results.
    """
    if not str(path).strip():
        raise ModelInspectionError("A local .mph, .stp, or .step file path is required")
    if Path(path).suffix.lower() in {".stp", ".step"}:
        from .geometry_reader import inspect_geometry
        return inspect_geometry(path)
    model_path = Path(path).expanduser().resolve(strict=True)
    if not model_path.is_file():
        raise ModelInspectionError("Model path must be a regular file")
    if model_path.suffix.lower() != ".mph":
        raise ModelInspectionError("Supported inputs are COMSOL .mph and STEP .stp/.step files")
    before = model_path.stat()
    if before.st_size > MAX_FILE_BYTES:
        raise ModelInspectionError(f"MPH file exceeds {MAX_FILE_BYTES // 1024 // 1024} MiB inspection limit")
    metadata, entries = _safe_container(model_path)
    digest = hashlib.sha256()
    with model_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    fingerprint = digest.hexdigest()
    snapshot = {"id": f"model-{fingerprint[:20]}", "file_name": model_path.name,
                "path": str(model_path), "format": "comsol-mph", "version": None,
                "parser": "native-mph-container (fallback)", "facts": [], "components": [],
                "physics": [], "materials": [], "studies": [], "parameters": {}, "selections": [],
                "unknowns": ["文件观察不能确认物理场/研究的有效启用状态、模型适用性或边界条件是否满足目标。",
                             "二进制几何拓扑、完整选择集、网格质量、解场及结果时效性未验证。",
                             "材料/物理场的存储默认值不等于用户确认的工程输入。"],
                "warnings": [], "fingerprint": fingerprint}
    _fact(snapshot, "file.size_bytes", "文件大小（字节）", before.st_size, "filesystem: stat().st_size")
    _fact(snapshot, "file.container_entries", "MPH 容器成员数", len(entries), "native ZIP: central directory")
    binary = [name for name in entries if name.lower().endswith(".mphbin")]
    if binary:
        _fact(snapshot, "file.binary_resources", "二进制资源文件名（不证明有效解）", binary[:MAX_ITEMS], "native ZIP: central directory")
    status = parser_status()
    snapshot["parser_detail"] = {"sim_cli_available": status["available"], "sim_cli_version": status.get("version"),
                                 "scan_succeeded": False, "native_xml_supplement": True}
    if status["available"]:
        try:
            payload = _scan(model_path, status)
            asset = payload["assets"][0]
            data = asset.get("summary", {}).get("data", {})
            if not isinstance(data, dict):
                raise _CLIError("sim-cli returned an unexpected MPH summary schema")
            snapshot["parser"] = "sim-cli scan + native XML supplement"
            snapshot["parser_detail"].update(scan_succeeded=True, engine=payload.get("engine"), schema=payload.get("schema_version"))
            snapshot["version"] = data.get("comsol_version")
            for key, label in (("comsol_version", "sim-cli 保存版本"), ("title", "sim-cli 模型标题"),
                               ("node_type", "sim-cli 保存节点类型"), ("is_runnable", "sim-cli 文件可运行标记"),
                               ("used_licenses", "文件记录的使用产品"), ("size_breakdown", "容器存储分布")):
                if key in data:
                    value = html.unescape(data[key]) if isinstance(data[key], str) else data[key]
                    _fact(snapshot, f"scan.{key}", label, value, f"sim-cli scan: assets[0].summary.data.{key}")
            for field, key in (("physics", "physics_tags"), ("materials", "material_tags"), ("studies", "study_tags")):
                snapshot[field] = [{"tag": _text(tag), "label": _text(tag), "type": None,
                                    "source": f"sim-cli scan: assets[0].summary.data.{key}", "activation": "unknown"}
                                   for tag in data.get(key, [])[:MAX_ITEMS]]
            unscoped = len(data.get("parameters", []))
            _fact(snapshot, "scan.unscoped_values", "sim-cli 未定作用域值数量（不作为全局参数）", unscoped,
                  "sim-cli scan: len(assets[0].summary.data.parameters)")
            for message in asset.get("warnings", [])[:20]:
                snapshot["warnings"].append(f"sim-cli: {_text(message)}")
            if unscoped:
                snapshot["warnings"].append("sim-cli 的 parameters 包含未定作用域的材料/物理场值；全局参数仅从 ModelParam/expressions 读取。")
        except (OSError, ValueError, TypeError, AttributeError, _CLIError) as exc:
            snapshot["warnings"].append(f"sim-cli 扫描失败，已使用原生容器回退：{str(exc)[:240]}")
    else:
        snapshot["warnings"].append(status["detail"])
    _native_metadata(snapshot, metadata)
    if not snapshot["version"]:
        snapshot["unknowns"].append("精确 COMSOL 保存版本未知。")
    for field, label in (("components", "组件"), ("physics", "物理场"), ("materials", "材料"),
                         ("studies", "研究"), ("parameters", "全局参数"), ("selections", "选择集")):
        if not snapshot[field]:
            snapshot["unknowns"].append(f"未提取到{label}记录；空列表不证明模型中不存在。")
    after = model_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ModelInspectionError("Model changed during inspection; retry with a stable file")
    return snapshot
