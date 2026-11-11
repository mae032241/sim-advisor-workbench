"""Shared official/user MPH case extraction with explicit evidence and coverage.

XML is saved-state evidence. Optional standalone COMSOL getters read a copied
model, without building geometry, meshing, generating solvers, or solving.
Tutorial instructions and exported Java are separate, locatable sources.
"""
from __future__ import annotations

from collections import Counter
import hashlib
from html.parser import HTMLParser
import json
from itertools import zip_longest
import math
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import zipfile
import xml.etree.ElementTree as ET

from .model_reader import ModelInspectionError, PROJECT_ROOT, _safe_container, inspect_model
from .config import DATA, DOC_ROOT
from .process_runner import run_text
from .case_structure import MAX_JAVA_BYTES, STRUCTURE_VERSION, java_objects
from .case_metadata import object_feature_branch
from .mph_saved_values import normalize_properties
from .mph_saved_selections import normalize_selections

EXTRACTOR_VERSION = "mph-case/1.4.0"
# Structural source chunking changes must not invalidate unchanged JVM getter
# artifacts. Keep the legacy signature value for those independently cached reads.
API_EXTRACTOR_VERSION = "mph-case/1.1.0"
API_READ_CONTRACT = "mph-api-read/v2"
MAX_NODES = 5000
MAX_PROPERTIES = 500
MAX_TEXT = 16000
MAX_TUTORIAL_STEPS = 25
DEFAULT_DOC_ROOT = DOC_ROOT
JAVA_SOURCE = PROJECT_ROOT / "scripts" / "mph_extract" / "ReadMph.java"
_DEEP_LOCK = threading.Lock()
_PROPERTY_TAGS = {"param", "propertyValue", "expressions", "NodeDefaultValues", "active", "entityFlags", "storedParent", "base", "geom", "identifier", "study", "solType"}
_SECTION_TAGS = {"components": {"ModelNode"}, "physics": {"Physics", "PhysicsFeature", "PhysicsProp"},
                 "materials": {"Material", "MaterialPropertyGroup", "MaterialModel"}, "selections": {"SelectionFeature", "GeomSelection"},
                 "couplings": {"MultiphysicsCoupling", "Cpl", "Pair"}, "studies": {"Study", "StudyFeature"},
                 "solvers": {"SolverSequence", "SolverFeature"}, "geometry": {"GeomSequence", "GeomFeature"},
                 "mesh": {"MeshSequence", "MeshFeature"}, "parameters": {"ModelParam", "ModelParamGroup"}}
_SAVED_BRANCHES = {kind: {"components": "misc", "materials": "material", "selections": "selection",
                         "couplings": "multiphysics", "studies": "study", "solvers": "solver",
                         "parameters": "param"}.get(section, section)
                   for section, kinds in _SECTION_TAGS.items() for kind in kinds}
_SAVED_BRANCHES.update({"FunctionFeature": "func", "Expr": "variable", "Variable": "variable",
                        "ResultFeature": "result", "DatasetFeature": "result", "NumericalFeature": "result",
                        "TableFeature": "result", "ExportFeature": "result", "GeomObjectSelectionFeature": "selection",
                        "CumulativeSelection": "selection"})
SAVED_FEATURE_LIMITS = {"max_objects": 1200, "max_parts_per_node": 8, "max_items_per_object": 8,
                        "max_part_characters": 600, "max_node_path_characters": 1024,
                        "max_property_preview_characters": 80, "max_selection_preview_characters": 400}
_MESH_PROPERTY_PRIORITY = {name: index for index, name in enumerate(("hmax", "hauto", "hmin", "custom", "hmaxactive",
                                                                  "hminactive", "hgrad", "hcurve", "hnarrow"))}


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _short(value):
    value = str(value or "")
    return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + " [truncated]"


def _evidence(path, locator, method, status="observed"):
    return {"asset_path": str(path), "locator": locator, "method": method, "status": status}


def _object(local_id, type_, title, text, data, evidence, *, kind="case", relations=None):
    return {"local_id": local_id, "type": type_, "title": title, "text": text, "data": data,
            "evidence": evidence, "relations": relations or [], "kind": kind}


def _asset(path: Path, role: str, *, derived=False):
    path = path.resolve()
    return {"path": str(path), "asset_path": str(path), "role": role, "status": "observed",
            "sha256": _hash(path), "size_bytes": path.stat().st_size, "derived": derived}


def _xml_state(path: Path):
    metadata, _ = _safe_container(path)
    info = {}
    if "modelinfo.xml" in metadata:
        try:
            info = dict(ET.fromstring(metadata["modelinfo.xml"]).attrib)
        except ET.ParseError:
            pass
    if "dmodel.xml" not in metadata:
        return [], {"status": "missing", "node_count": 0, "truncated": False}, info
    try:
        root = ET.fromstring(metadata["dmodel.xml"])
    except ET.ParseError as exc:
        return [], {"status": "malformed", "node_count": 0, "truncated": False}, {}
    records, total, property_truncation = [], 0, False

    def element_data(element):
        nonlocal property_truncation
        raw = ET.tostring(element, encoding="unicode")
        truncated = len(raw) > MAX_TEXT or len(element.text or "") > MAX_TEXT or any(len(v) > MAX_TEXT for v in element.attrib.values())
        property_truncation = property_truncation or truncated
        return {"element": element.tag, "attributes": {k: _short(v) for k, v in element.attrib.items()},
                "text": _short(element.text), "raw_xml": _short(raw), "truncated": truncated}

    def visit(element, parent_path, inherited_branch="misc"):
        nonlocal total, property_truncation
        branch = _SAVED_BRANCHES.get(element.tag, inherited_branch)
        segment = element.tag + ("[@tag=" + json.dumps(element.get("tag"), ensure_ascii=False) + "]" if element.get("tag") else "")
        locator = parent_path + "/" + segment
        if element.get("tag"):
            total += 1
            if len(records) < MAX_NODES:
                properties = [child for child in element if child.tag in _PROPERTY_TAGS]
                selections = [child for child in element if child.tag in {"selection", "outputSelection"}]
                if len(properties) > MAX_PROPERTIES:
                    property_truncation = True
                normalized = normalize_properties(element, max_items=MAX_PROPERTIES, max_text=MAX_TEXT)
                normalized_selections = normalize_selections(element, max_items=MAX_PROPERTIES, max_text=MAX_TEXT)
                if normalized.get("truncated") or normalized_selections.get("truncated"):
                    property_truncation = True
                records.append({"path": locator, "node_kind": element.tag, "tag": element.get("tag"), "feature_branch": branch,
                    "label": element.get("name", element.get("tag")), "type": element.get("op"),
                    "attributes": dict(element.attrib), "properties": [element_data(x) for x in properties[:MAX_PROPERTIES]],
                    "properties_total": len(properties), "properties_truncated": len(properties) > MAX_PROPERTIES,
                    "selection_records": [element_data(x) for x in selections],
                    "selection_resolution": "raw_encoding_not_resolved", "effective_activation": "not_evaluated",
                    "normalized_properties": normalized, "normalized_selections": normalized_selections,
                    "value_origin": "saved_value_not_proven_effective_or_user_explicit"})
        for index, child in enumerate(element):
            # Untagged wrappers receive a sibling position, avoiding ambiguous paths.
            visit(child, locator if child.get("tag") else locator + f"/child[{index + 1}]", branch)

    visit(root, "")
    coverage = {"status": "bounded_saved_state", "node_count": len(records), "nodes_observed_total": total,
                "truncated": total > MAX_NODES or property_truncation,
                "property_value_semantics": "bounded_saved_value_decoding_not_effective_configuration",
                "normalization": {"schema": "mph-saved-normalization/v1",
                    "properties_total": sum(n["normalized_properties"].get("total", 0) for n in records),
                    "properties_unresolved": sum(n["normalized_properties"].get("unresolved_count", 0) for n in records),
                    "properties_partial": sum(n["normalized_properties"].get("partial_count", 0) for n in records),
                    "selection_statuses": dict(Counter(n["normalized_selections"].get("status", "unknown") for n in records)),
                    "default_vs_explicit": "not_evaluated", "effective_membership": "not_validated"}}
    return records, coverage, info


def _comsol_root():
    return Path(os.environ.get("SIM_ADVISOR_COMSOL_HOME", "D:/comsol/COMSOL63/Multiphysics")).resolve()


def _read_api_state(path):
    def reject_constant(value):
        raise ValueError(f"Non-standard JSON numeric literal {value}; rebuild the API snapshot")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("API numeric literal exceeds finite JSON representation")
        return number
    if Path(path).stat().st_size > 24 * 1024 * 1024:
        raise ValueError("API snapshot exceeded 24 MiB")
    data = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant, parse_float=finite_float)
    if (not isinstance(data, dict) or data.get("schema") != "mph-api-state/v1"
            or data.get("read_contract") != API_READ_CONTRACT or not isinstance(data.get("nodes"), list)
            or any(data.get(key) is not False for key in ("geometry_run", "mesh_run", "solver_run", "defaults_generated"))):
        raise ValueError("Unexpected extractor state, read contract, or prohibited execution flags")
    if (type(data.get("read_errors")) is not int or data["read_errors"] < 0 or not isinstance(data.get("read_coverage", {}), dict)
            or any(not isinstance(node, dict) or not isinstance(node.get("properties", []), list)
                   or any(not isinstance(item, dict) for item in node.get("properties", [])) for node in data["nodes"])):
        raise ValueError("Unexpected API coverage or node schema")
    return data


def _api_coverage(data, *, cached):
    """Explain attempted getter coverage without treating reads as effectiveness."""
    properties = [item for node in data.get("nodes", []) for item in node.get("properties", [])]
    observed = sum(item.get("read_status") == "observed" for item in properties)
    partial = sum(item.get("read_status") == "partial" for item in properties)
    unresolved = sum(item.get("read_status") not in {"observed", "partial"} for item in properties)
    reads = data.get("read_coverage", {})
    selections = reads.get("selections", {})
    traversal = reads.get("traversal", {})
    errors = max(data.get("read_errors", 0), unresolved + selections.get("unresolved", 0) + traversal.get("unresolved", 0))
    examples = [{"node_path": node.get("path"), "property": item.get("name"), "value_type": item.get("value_type"),
                 "read_status": item.get("read_status"), "reason": item.get("read_error", {}).get("reason", "selection_property_incomplete"),
                 "origin": item.get("read_error", {}).get("origin"), "exception": item.get("read_error", {}).get("exception")}
                for node in data.get("nodes", []) for item in node.get("properties", []) if item.get("read_status") != "observed"]
    return {"status": "partial" if errors or partial or data.get("truncated") else "observed", "cached": cached,
            "node_count": len(data.get("nodes", [])), "read_errors": errors,
            "properties": {"total": len(properties), "declared": reads.get("properties", {}).get("declared", len(properties)), "observed": observed, "partial": partial, "unresolved": unresolved},
            "selections": selections, "traversal": traversal, "failure_reasons": reads.get("failure_reasons", {}),
            "failure_examples": examples[:12], "failure_examples_truncated": len(examples) > 12,
            "non_finite_values": reads.get("non_finite_values", 0), "truncated": bool(data.get("truncated")),
            "limits": data.get("limits", {}), "truncation_reasons": data.get("truncation_reasons", {}),
            "read_contract": data.get("read_contract"),
            "scope": "Saved/current API getter observations; default-vs-explicit origin, effective activation and physical validity are not evaluated",
            "selection_scope": "Named references/object names/entity indices observed only when supported; no geometry build, membership validity or persistent identity proof"}


def _deep_state(path: Path, output: Path, fingerprint: str):
    """Return actual API state and derived assets, or an honest failed status."""
    root = _comsol_root()
    output = output.resolve()
    binary = root / "java" / "win64" / "jre" / "bin"
    java, compiler = binary / "java.exe", binary / "javac.exe"
    if not java.is_file() or not compiler.is_file():
        return None, [], {"status": "unavailable", "reason": "Standalone COMSOL Java/compiler not found"}
    output.mkdir(parents=True, exist_ok=True)
    result, exported, cache = output / "api-state.json", output / "model_export.java", output / "api-manifest.json"
    signature = {"source_sha256": fingerprint, "extractor_version": API_EXTRACTOR_VERSION,
                 "read_contract": API_READ_CONTRACT,
                 "java_extractor_sha256": _hash(JAVA_SOURCE), "comsol_root": str(root),
                 "java_export_name": exported.name,
                 "runtime_binary_size": java.stat().st_size, "runtime_binary_mtime_ns": java.stat().st_mtime_ns}
    if cache.is_file() and result.is_file():
        try:
            if json.loads(cache.read_text(encoding="utf-8")) == signature:
                data = _read_api_state(result)
                assets = [_asset(result, "api_snapshot", derived=True)]
                if exported.is_file():
                    assets.append(_asset(exported, "exported_reconstruction_java", derived=True))
                return data, assets, _api_coverage(data, cached=True)
        except (OSError, ValueError):
            pass
    work = output / "runtime"
    copied = work / "input.mph"
    coverage = None
    owns_copy = False
    wrapper = "import os,sys,json,subprocess; c=json.loads(sys.argv[1]); e=dict(os.environ,TEMP=sys.argv[2],TMP=sys.argv[2],COMSOL_NUM_THREADS='1'); p=subprocess.run(c,env=e,stdin=subprocess.DEVNULL,stdout=sys.stdout,stderr=sys.stderr); sys.exit(p.returncode)"
    with _DEEP_LOCK:
        try:
            for directory in (work, work / "home", work / "tmp", work / "classes"):
                directory.mkdir(parents=True, exist_ok=True)
            if copied.resolve() == path.resolve() or (copied.exists() and copied.samefile(path)):
                raise ValueError("Extraction runtime input must differ from the original model")
            owns_copy = True
            if copied.exists():
                copied.chmod(0o644)
            shutil.copyfile(path, copied)
            copied.chmod(0o444)
            compiled = run_text([str(compiler), "-encoding", "UTF-8", "-d", str(work / "classes"), str(JAVA_SOURCE)], "", work, 35, 512 * 1024)
            if compiled.returncode:
                raise RuntimeError("Standalone extractor compilation failed")
            command = [str(java), "-Djava.library.path=" + str(root / "bin" / "win64"),
                       "-Duser.home=" + str(work / "home"), "-Djava.io.tmpdir=" + str(work / "tmp"),
                       "-cp", str(work / "classes") + os.pathsep + str(root / "plugins" / "*"),
                       "ReadMph", str(copied), str(result), str(exported)]
            timeout = max(15, min(240, int(os.environ.get("SIM_ADVISOR_MPH_TIMEOUT", "120"))))
            run = run_text([sys.executable, "-c", wrapper, json.dumps(command), str(work / "tmp")], "", work, timeout, 1024 * 1024)
            if run.returncode or not result.is_file():
                raise RuntimeError(f"Standalone COMSOL extraction failed (exit {run.returncode}); saved XML retained")
            if result.stat().st_size > 24 * 1024 * 1024:
                raise RuntimeError("API snapshot exceeded 24 MiB")
            data = _read_api_state(result)
            cache.write_text(json.dumps(signature, ensure_ascii=False, indent=2), encoding="utf-8")
            assets = [_asset(result, "api_snapshot", derived=True)]
            if exported.is_file():
                assets.append(_asset(exported, "exported_reconstruction_java", derived=True))
            coverage = _api_coverage(data, cached=False)
            return data, assets, coverage
        except (OSError, ValueError, RuntimeError, __import__("subprocess").TimeoutExpired) as exc:
            coverage = {"status": "failed", "reason": _short(str(exc))[:300], "node_count": 0}
            return None, [], coverage
        finally:
            # run_text has reaped its process tree before returning or raising.
            # Only the temporary model input is disposable; outputs are evidence.
            pending = sys.exc_info()[1]
            for temporary in (copied, work / "classes" / "ReadMph.class"):
                try:
                    if owns_copy and temporary.exists():
                        if temporary == copied:
                            temporary.chmod(0o644)
                        temporary.unlink()
                except OSError as exc:
                    label = "MPH input" if temporary == copied else "extractor class"
                    warning = "Temporary " + label + " could not be removed: " + _short(str(exc))[:300]
                    if coverage is not None:
                        coverage["cleanup_warning"] = "; ".join(filter(None, [coverage.get("cleanup_warning"), warning]))
                    elif pending is not None and hasattr(pending, "add_note"):
                        pending.add_note(warning)


class _TutorialParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.links, self.frames = [], [], []
        self.title, self.in_title, self.skip = "", False, 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "style"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag in {"a", "img"}:
            value = attributes.get("href") or attributes.get("src")
            if value:
                self.links.append(value)
            if self.frames and (attributes.get("name") or attributes.get("id")):
                self.frames[-1]["anchor"] = attributes.get("name") or attributes.get("id")
        if tag in {"div", "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre"}:
            self.frames.append({"tag": tag, "class": attributes.get("class", ""), "line": self.getpos()[0],
                                "anchor": attributes.get("id", ""), "parts": []})

    def handle_data(self, text):
        if self.skip:
            return
        if self.in_title:
            self.title += text
        for frame in self.frames:
            frame["parts"].append(text)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1
        if tag == "title":
            self.in_title = False
        if self.frames and self.frames[-1]["tag"] == tag:
            frame = self.frames.pop()
            text = re.sub(r"\s+", " ", "".join(frame.pop("parts"))).strip()
            # Ignore structural wrappers and table-cell bullet markers.
            if text and "outer" not in frame["class"].lower() and (frame["class"] or frame["tag"] != "div") and len(text) <= MAX_TEXT:
                self.blocks.append({**frame, "text": text})


class _TutorialReferenceParser(_TutorialParser):
    """Keep headings and association metadata, without retaining tutorial prose.

    Small transient frame buffers also recognize an Application Library path
    inside the source HTML. Only headings and that association line survive.
    The full parser above remains available to the document-owned extractor.
    """
    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        self.links.clear()

    def handle_data(self, text):
        if self.skip:
            return
        if self.in_title:
            self.title = (self.title + text)[:512]
        for frame in self.frames:
            remaining = 2048 - sum(len(part) for part in frame["parts"])
            if remaining > 0:
                frame["parts"].append(text[:remaining])

    def handle_endtag(self, tag):
        previous = len(self.blocks)
        super().handle_endtag(tag)
        if len(self.blocks) == previous:
            return
        block = self.blocks[-1]
        if not (block["class"].lower().startswith("head") or re.fullmatch(r"h[1-6]", block["tag"]) or
                re.search(r"Application\s+Library\s+path\s*:", block["text"], re.I)):
            self.blocks.pop()


def _tutorial(path: Path, docs_root: Path | None, info: dict, *, metadata_only=False):
    root = Path(docs_root) if docs_root is not None else DEFAULT_DOC_ROOT
    expected = re.fullmatch(r"modeldescr_([^/]+)/(.+)", info.get("modelTitle", ""))
    expected_package = f"com.comsol.help.models.{expected[1]}.{expected[2]}" if expected else None
    if root.is_file():
        candidates, method = [root], "explicit_document_reference_candidate"
        root = root.parent
    elif root.is_dir():
        stems = {path.stem} | ({expected[2]} if expected else set())
        candidates = list({p for stem in stems for p in root.glob(f"*/{stem}.html")})
        method = "modelTitle_identifier_candidate" if expected else "unique_filename_candidate"
    else:
        candidates, method = [], "not_found"
    parsed = []
    for html in candidates:
        if html.stat().st_size > 8 * 1024 * 1024:
            continue
        parser = _TutorialReferenceParser() if metadata_only else _TutorialParser()
        parser.feed(html.read_text(encoding="utf-8", errors="replace"))
        parser.blocks.sort(key=lambda item: item["line"])
        library_path = next((re.split(r"Application\s+Library\s+path\s*:\s*", block["text"], maxsplit=1, flags=re.I)[1].strip()
                             for block in parser.blocks if re.search(r"Application\s+Library\s+path\s*:", block["text"], re.I)), None)
        parsed.append((html.resolve(), parser, library_path))
    applications = Path(os.environ.get("COMSOL_APPLICATIONS", _comsol_root() / "applications")).resolve()
    actual_relative = path.relative_to(applications).with_suffix("").as_posix() if path.is_relative_to(applications) else None
    if actual_relative:
        exact = [item for item in parsed if item[2] and item[2].replace("\\", "/").strip("/").casefold() == actual_relative.casefold()]
        if len(exact) == 1:
            parsed, method = exact, "application_library_path_exact"
        else:
            return None, None, {"status": "application_path_mismatch" if parsed else "missing", "method": "application_library_path_required",
                               "model_relative_path": actual_relative, "candidates": [str(item[0]) for item in parsed]}, []
    elif expected_package:
        preferred = [item for item in parsed if item[0].parent.name == expected_package]
        if preferred:
            parsed = preferred
    candidates = [item[0] for item in parsed]
    if len(candidates) != 1:
        return None, None, {"status": "ambiguous" if candidates else "missing", "candidates": [str(p) for p in candidates[:20]]}, []
    html, parser, library_path = parsed[0]
    return html, parser, {"status": "application_path_matched" if actual_relative else "candidate_matched",
                           "method": method, "application_library_path": library_path,
                           "model_relative_path": actual_relative, "settings_agreement": "not_verified", "path": str(html)}, [root.resolve()]


def _tutorial_reference(html, parser):
    """Build an external document relation; no tutorial body belongs to MPH."""
    from .tutorial_sources import tutorial_reference
    chapters = []
    for block in parser.blocks:
        if not (block["class"].lower().startswith("head") or re.fullmatch(r"h[1-6]", block["tag"])):
            continue
        title = block["text"][:512]
        anchor = block.get("anchor") or ""
        chapter = {"title": title, "anchor": anchor,
                   "locator": "html:#" + anchor if anchor else f"html:line={block['line']}",
                   "category": object_feature_branch({"type": "method_step", "title": title}) or "misc"}
        chapters.append(chapter)
        if len(chapters) >= MAX_PROPERTIES:
            break
    return tutorial_reference(html, source_sha256=_hash(html), title=parser.title.strip()[:512], chapters=chapters)


def _tutorial_objects(html: Path, parser: _TutorialParser):
    groups, in_instructions, section, subsection = [], False, "", ""
    for block in parser.blocks:
        text, style = block["text"], block["class"].lower()
        if re.search(r"modeling instructions|modelling instructions|建模说明|建模步骤", text, re.I) and len(text) < 120:
            in_instructions = True
            section = text
            continue
        heading = style.startswith("head") or block["tag"].startswith("h")
        if heading:
            if style.startswith(("head2", "head3")) or block["tag"] in {"h2", "h3"}:
                section, subsection = text, ""
            else:
                subsection = text
        if not in_instructions:
            continue
        if heading or not re.search(r"num|body|bullet|step", style) and block["tag"] not in {"p", "li"}:
            continue
        if text in {"•", "·"}:
            continue
        if text.isdigit() and "num" in style and not block["anchor"]:
            continue
        locator = "html:#" + block["anchor"] if block["anchor"] else f"html:line={block['line']}"
        if not groups or groups[-1]["section"] != section:
            groups.append({"section": section, "instructions": []})
        groups[-1]["instructions"].append({"text": text, "subsection": subsection, "locator": locator, "line": block["line"]})
    coarsened = len(groups) > MAX_TUTORIAL_STEPS
    if coarsened:
        remaining = groups[MAX_TUTORIAL_STEPS - 1:]
        groups = groups[:MAX_TUTORIAL_STEPS - 1] + [{"section": "Additional documented GUI sections", "instructions":
            [item for group in remaining for item in group["instructions"]], "merged_sections": [g["section"] for g in remaining]}]
    objects, previous = [], None
    for count, group in enumerate(groups, 1):
        instructions = group["instructions"]
        local_id = "step-" + hashlib.sha256((group["section"] + instructions[0]["locator"]).encode()).hexdigest()[:20]
        text = "\n".join((item["subsection"] + ": " if item["subsection"] else "") + item["text"] for item in instructions)
        objects.append(_object(local_id, "method_step", group["section"], _short(text),
            {"instructions": instructions, "section": group["section"], "merged_sections": group.get("merged_sections", []),
             "tutorial_order": count, "instruction_count": len(instructions), "index_text_truncated": len(text) > MAX_TEXT,
             "step_status": "documented_not_executed", "model_agreement": "not_verified"},
            [_evidence(html, item["locator"], "tutorial_html_block", "documented") for item in instructions],
            relations=([{"type": "follows_document_order", "target_local_id": previous}] if previous else [])))
        previous = local_id
    return objects, coarsened


def _java_objects(path: Path, *, exported: bool):
    return java_objects(path, exported=exported)


def _api_summary(api):
    if api is None:
        return ""
    lines = ["Read-only COMSOL API getter observations. These are saved/current configured values, not proven effective settings or explicit user inputs."]
    nodes = api.get("nodes", [])
    selected = [n for n in nodes if n.get("category") in {"multiphysics", "study", "sol"} or n.get("path", "").startswith("model/study(")]
    boundaries = [n for n in nodes if "/physics(" in n.get("path", "") and (n.get("selection") or {}).get("dim", {}).get("value") in {0, 1, 2}]
    boundaries.sort(key=lambda n: 0 if re.search(r"HeatFlux|Temperature|Inlet|Outlet|Fixed|Roller|Spring|BoundaryLoad|Force|Potential|Ground|Terminal", n.get("type", ""), re.I) else 1)
    selected.extend(boundaries[:8])
    for node in selected[:20]:
        properties = node.get("properties", [])
        priority = {"Heat_physics", "Solid_physics", "HeatFluxType", "h", "q0_input", "Text", "Tref", "T0", "LoadType", "Ftot", "p0", "study"}
        chosen = [p for p in properties if p.get("name") in priority] or properties[:5]
        values = "; ".join(p["name"] + "=" + json.dumps(p.get("value", "[unresolved]"), ensure_ascii=False)[:160] for p in chosen[:8])
        attachment = " study=" + str(node["study"]) if "study" in node else ""
        lines.append(f"{node['path']} type={node.get('type')}{attachment}; {values}")
    coverage = _api_coverage(api, cached=False)
    lines.append(f"Getter coverage: properties={coverage['properties']}; unresolved property/selection/traversal reads={coverage['read_errors']}; geometry/mesh/solver execution: not run.")
    return "\n".join(lines)[:6500]


def _saved_preview(value, maximum):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    cut = len(text) > maximum
    return (text[:maximum - 12] + " [truncated]" if cut else text), cut


def _saved_selection_preview(entry):
    """Describe saved references/indices without resolving geometry membership."""
    result = {"status": entry.get("status"), "selection_type": entry.get("attributes", {}).get("selType")}
    truncated = bool(entry.get("truncated"))
    for key in ("named_references", "output_references", "objects", "base", "unresolved_reasons"):
        if entry.get(key):
            result[key] = entry[key][:4]
            truncated |= len(entry[key]) > 4
    if entry.get("explicit"):
        keys = ("status", "geometry_ref", "entity_dimension", "encoded_entity_dimension", "decoded_entities", "raw_encoded", "reason")
        result["explicit"] = [{key: value[:24] if isinstance(value, list) else value for key, value in item.items() if key in keys}
                              for item in entry["explicit"][:4]]
        truncated |= len(entry["explicit"]) > 4 or any(len(item.get("decoded_entities", [])) > 24 for item in entry["explicit"][:4])
    if entry.get("remaining"):
        result["remaining"] = [{key: value[:16] if isinstance(value, list) else value for key, value in item.items()
                                if key in {"status", "entity_dimension", "saved_count", "values"}}
                               for item in entry["remaining"][:4]]
        truncated |= len(entry["remaining"]) > 4 or any(len(item.get("values", [])) > 16 for item in entry["remaining"][:4])
    text, cut = _saved_preview(result, SAVED_FEATURE_LIMITS["max_selection_preview_characters"])
    return text, truncated or cut


def _saved_feature_objects(records, saved_state):
    """Bounded feature/property batches for offline retrieval, never GUI steps.

    Only small value previews enter the knowledge objects. Exact normalized and
    raw values remain in the hash-pinned snapshot at each cited JSON pointer.
    Properties are grouped by source feature and character budget, rather than
    making one object per property or copying the entire saved model into each.
    """
    limits = SAVED_FEATURE_LIMITS
    objects, represented_nodes, represented_properties, represented_selections = [], set(), 0, 0
    eligible_nodes, total_properties, total_selections, preview_truncations = 0, 0, 0, 0

    def groups(items):
        batch, characters, count = [], 0, 0
        for item in items:
            size = len(item["text"]) + 1
            if batch and (len(batch) >= limits["max_items_per_object"] or characters + size > limits["max_part_characters"]):
                yield batch
                count += 1
                if count >= limits["max_parts_per_node"]:
                    return
                batch, characters = [], 0
            batch.append(item)
            characters += size
        if batch:
            yield batch

    for node_index, node in enumerate(records):
        properties = node.get("normalized_properties", {}).get("properties", [])
        selections = node.get("normalized_selections", {}).get("entries", [])
        if not properties and not selections and node["node_kind"] not in _SAVED_BRANCHES:
            continue
        eligible_nodes += 1
        total_properties += len(properties)
        total_selections += len(selections)
        if len(objects) >= limits["max_objects"]:
            continue
        node_pointer = f"json:/nodes/{node_index}"

        def property_items():
            indexed = list(enumerate(properties))
            if node.get("feature_branch") == "mesh":
                # Keep the principal saved size controls discoverable even if
                # this node's many other properties hit the per-node limit.
                indexed.sort(key=lambda pair: _MESH_PROPERTY_PRIORITY.get(pair[1].get("name"), len(_MESH_PROPERTY_PRIORITY)))
            for index, item in indexed:
                value, cut = _saved_preview(item.get("raw_value") if item.get("status") == "unresolved" else item.get("value"),
                                           limits["max_property_preview_characters"])
                name = str(item.get("name") or "[unnamed]")
                locator = node_pointer + f"/normalized_properties/properties/{index}"
                status = str(item.get("status") or "unresolved")[:20]
                yield {"kind": "properties", "text": name[:64] + "=" + value + (" [" + status + "]" if status != "decoded" else ""),
                       "data": {"name": name[:64], "value_preview": value, "value_type": str(item.get("value_type", "unknown"))[:64],
                                "status": status, "snapshot_locator": locator,
                                "preview_truncated": cut or len(name) > 64 or bool(item.get("truncated"))}}

        def selection_items():
            for index, item in enumerate(selections):
                value, cut = _saved_selection_preview(item)
                yield {"kind": "selections", "text": "selection=" + value,
                       "data": {"value_preview": value, "status": str(item.get("status", "unresolved"))[:20],
                                "snapshot_locator": node_pointer + f"/normalized_selections/entries/{index}",
                                "preview_truncated": cut or bool(item.get("truncated"))}}

        property_groups, selection_groups = list(groups(property_items())), list(groups(selection_items()))
        # Alternate kinds so a property-heavy node cannot hide its selections.
        batches = [batch for pair in zip_longest(property_groups, selection_groups) for batch in pair if batch]
        if not batches:
            batches = [[]]
        for part, batch in enumerate(batches[:limits["max_parts_per_node"]], 1):
            if len(objects) >= limits["max_objects"]:
                break
            represented_nodes.add(node_index)
            included_properties = [item["data"] for item in batch if item["kind"] == "properties"]
            included_selections = [item["data"] for item in batch if item["kind"] == "selections"]
            represented_properties += len(included_properties)
            represented_selections += len(included_selections)
            preview_truncations += sum(item["data"]["preview_truncated"] for item in batch)
            node_path = node["path"]
            branch, feature_type = node.get("feature_branch", "misc"), node.get("type") or node["node_kind"]
            label = str(node.get("label") or node.get("tag") or node["node_kind"])
            title = f"Saved {branch} · {str(feature_type)[:64]} · {label[:64]}" + (f" · part {part}" if len(batches) > 1 else "")
            text = "Saved XML values; not proven effective or user explicit. Selection membership unvalidated; geometry, mesh and solver not run.\n"
            text += "\n".join(item["text"] for item in batch) or "Saved feature presence; configured values not observed."
            key = hashlib.sha256((node_path + ":" + str(node_index) + ":" + str(part)).encode()).hexdigest()[:24]
            evidence = [_evidence(saved_state, node_pointer, "native_mph_xml_snapshot", "saved_observed")]
            evidence.extend(_evidence(saved_state, item["data"]["snapshot_locator"], "native_mph_xml_snapshot", "saved_observed") for item in batch)
            objects.append(_object("saved-feature-" + key, "saved_configuration", title, text,
                {"schema": "mph-saved-feature/v1", "feature_branch": branch, "feature_branch_origin": "saved_xml_node_kind_or_ancestor",
                 "feature_type": str(feature_type)[:128], "source_order": node_index + 1,
                 "node_kind": node["node_kind"][:128], "tag": str(node.get("tag") or "")[:128],
                 "node_path": node_path[:limits["max_node_path_characters"]], "node_path_truncated": len(node_path) > limits["max_node_path_characters"],
                 "snapshot_locator": node_pointer, "part": part, "saved_properties": included_properties, "saved_selections": included_selections,
                 "value_origin": "saved_value_not_proven_effective_or_user_explicit", "effective_activation": "not_evaluated",
                 "selection_membership": "not_validated", "execution_status": "not_run",
                 "analysis_scope": "Bounded saved-state previews; exact normalized/raw node values at snapshot locators"}, evidence))
    coverage = {"schema": "mph-saved-feature/v1", "status": "bounded_saved_state", "object_count": len(objects),
                "eligible_nodes": eligible_nodes, "represented_nodes": len(represented_nodes),
                "properties_available": total_properties, "properties_represented": represented_properties,
                "selections_available": total_selections, "selections_represented": represented_selections,
                "omitted_nodes": eligible_nodes - len(represented_nodes), "omitted_properties": total_properties - represented_properties,
                "omitted_selections": total_selections - represented_selections, "preview_truncations": preview_truncations,
                "truncated": len(represented_nodes) < eligible_nodes or represented_properties < total_properties or represented_selections < total_selections or bool(preview_truncations),
                "scope": "Saved XML observations only; effectiveness, explicit origin and selection membership not evaluated",
                "limits": dict(limits)}
    return objects, coverage


def extract_case(model_path: Path, *, docs_root: Path | None = None, artifact_dir: Path | None = None, deep: bool = True) -> dict:
    """Extract one MPH case using the same rules for official and user inputs."""
    path = Path(model_path).expanduser().resolve(strict=True)
    if path.suffix.lower() != ".mph" or not path.is_file():
        raise ModelInspectionError("Case extraction requires a local .mph file")
    before = path.stat()
    snapshot = inspect_model(path)
    fingerprint = snapshot["fingerprint"]
    records, raw_coverage, info = _xml_state(path)
    coverage = {"extraction_level": "saved_xml_inventory", "raw_xml": raw_coverage,
                "deep_api": {"status": "not_requested", "node_count": 0},
                "validation": {"source_unchanged": False, "loaded_in_comsol": False, "geometry_built": False,
                               "mesh_built": False, "solver_generated": False, "solved": False,
                               "results_validated": False, "physical_validity": "not_evaluated"}, "sections": {}}
    preview_only = info.get("nodeType", "").lower() == "preview"
    coverage["source_container"] = {"node_type": info.get("nodeType"), "status": "preview_only" if preview_only else "saved_model",
        "compact_file_size_declared": info.get("compactFileSize"), "solved_file_size_declared": info.get("solvedFileSize")}
    warnings = list(snapshot.get("warnings", []))
    assets = [_asset(path, "original_mph")]
    output = ((Path(artifact_dir) if artifact_dir is not None else DATA / "library-extract") / ("mph-" + fingerprint[:20])).resolve()
    output.mkdir(parents=True, exist_ok=True)
    saved_state = output / "saved-state.json"
    saved_state.write_text(json.dumps({"schema": "mph-saved-state/v1", "source_sha256": fingerprint,
        "extractor_version": EXTRACTOR_VERSION, "nodes": records, "coverage": raw_coverage}, ensure_ascii=False, indent=2), encoding="utf-8")
    assets.append(_asset(saved_state, "saved_xml_snapshot", derived=True))
    with zipfile.ZipFile(path) as container:
        if "dmodel.xml" in container.namelist():
            raw_xml = output / "dmodel.xml"
            raw_xml.write_bytes(container.read("dmodel.xml"))
            assets.append(_asset(raw_xml, "raw_dmodel_xml", derived=True))
    api = None
    if deep and preview_only:
        coverage["deep_api"] = {"status": "preview_only", "node_count": 0,
            "reason": "Application Libraries preview contains no model settings; obtain the actual compact/full model as a new asset"}
        warnings.append("这是 Application Libraries 预览文件，缺少实际模型配置；需要取得同一案例真正的 compact/full MPH。教程说明不代表已读取该模型设置。")
    elif deep:
        api, derived, coverage["deep_api"] = _deep_state(path, output, fingerprint)
        assets.extend(derived)
        if api is not None:
            coverage["extraction_level"] = "api_state_with_saved_xml"
            coverage["validation"]["loaded_in_comsol"] = True
            warnings.extend(str(item) for item in api.get("warnings", []))
            if coverage["deep_api"]["status"] == "partial":
                detail = coverage["deep_api"]
                warnings.append(f"深度 API 仅部分读取：属性 {detail.get('properties', {})}；未解析 getter/选择/遍历读取 {detail.get('read_errors', 0)} 项。读取缺口不等于模型物理错误；有效启用状态、选择有效性与物理适用性仍未验证。")
        else:
            warnings.append("深度 COMSOL 读取未完成；保留原始 XML 节点/属性，未把保存值视为有效设置。")
    for section, tags in _SECTION_TAGS.items():
        matching = [item for item in records if item["node_kind"] in tags]
        coverage["sections"][section] = {"saved_nodes": len(matching),
            "status": "saved_observed_effectiveness_unknown" if matching else "not_observed_not_proof_of_absence"}
    html, tutorial, tutorial_coverage, _ = _tutorial(path, docs_root, info, metadata_only=True)
    coverage["tutorial"] = tutorial_coverage
    title = next((str(f["value"]) for f in snapshot["facts"] if f["key"] == "metadata.title"), path.stem)
    saved_objects, coverage["saved_features"] = _saved_feature_objects(records, saved_state)
    objects = []
    tutorial_refs = [_tutorial_reference(html, tutorial)] if html is not None else []
    coverage["tutorial"].update(body_storage="external_document_owner", reference_count=len(tutorial_refs), passage_count=0)
    if html is None:
        warnings.append("未唯一关联教程；未从保存模型树编造建模步骤。")
    sidecar = path.with_suffix(".java")
    if sidecar.is_file():
        assets.append(_asset(sidecar, "provided_java_source"))
        objects.extend(_java_objects(sidecar, exported=False))
    exported = next((Path(a["path"]) for a in assets if a["role"] == "exported_reconstruction_java"), None)
    if exported is not None and not sidecar.is_file():
        objects.extend(_java_objects(exported, exported=True))
    objects.extend(saved_objects)
    snippets = [item for item in objects if item["type"] == "implementation_snippet"]
    coverage["implementation"] = {"status": "observed_source_not_executed" if snippets else "missing",
                                  "snippet_count": len(snippets), "original_edit_history": "not_reconstructed",
                                  "structure_version": STRUCTURE_VERSION, "code_body": "complete_within_read_limit" if snippets else "not_available",
                                  "source_read_limit_bytes": MAX_JAVA_BYTES, "execution_order": "not_proven",
                                  "syntax_read_status": "partial" if any(item["data"]["parse_warnings"] for item in snippets) else "supported_lexical_subset",
                                  "unresolved_references": sum(r.get("status") == "unresolved" for item in snippets for r in item["data"]["references"])}
    java_path = sidecar if sidecar.is_file() else exported
    if java_path is not None and java_path.stat().st_size > MAX_JAVA_BYTES:
        coverage["implementation"].update(status="source_read_limit", code_body="retained_as_raw_asset_not_structured")
    coverage["dependencies"] = {"collected": "external_tutorial_references_and_adjacent_java",
                                 "external_model_references": "not_fully_resolved",
                                 "reproduction_dependency_closure": "not_validated"}
    if not snippets:
        warnings.append("Java 源文件已作为原始资产保留，但超过结构化读取限制；不能把缺少片段当作没有代码。" if java_path is not None and java_path.stat().st_size > MAX_JAVA_BYTES else
                        "没有可定位的 Java 实现代码；保留配置与外部文档引用，不生成伪代码冒充案例代码。")
    coverage["limitations"] = ["保存状态/API getter 返回值不等于研究中的有效设置或用户显式输入。",
        "选择、耦合及 solver 已观察配置不等于运行适用性验证；未运行几何、网格或求解。",
        "教程顺序是文档顺序；模型树是保存状态；导出 Java 不是原作者完整编辑历史。",
        "代码片段未证明可独立执行；依赖、教程与保存模型的一致性仍需复核。"]
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or _hash(path) != fingerprint:
        raise ModelInspectionError("Original model changed during case extraction")
    coverage["validation"]["source_unchanged"] = True
    summary = f"{title}\nSaved COMSOL version: {snapshot.get('version')}.\n" + "\n".join(f"{key}: {value['saved_nodes']} saved nodes; effectiveness unknown" for key, value in coverage["sections"].items())
    if preview_only:
        summary += "\nPREVIEW ONLY: actual model configuration is unavailable. Tutorial instructions are a separate documented reference."
    summary += "\n" + _api_summary(api)
    objects.insert(0, _object("model-case", "model_case", title, summary,
        {"model_snapshot": snapshot, "coverage": coverage,
         "configuration_assets": [{"asset_path": a["path"], "role": a["role"]} for a in assets if a["role"] in {"saved_xml_snapshot", "raw_dmodel_xml", "api_snapshot"}],
         "tutorial_refs": tutorial_refs,
         "extractor_version": EXTRACTOR_VERSION, "origin_trust": "not_inferred_from_file_location"},
        [_evidence(path, "MPH saved state and fingerprint " + fingerprint, "shared_mph_case_extractor"),
         _evidence(saved_state, "json:/nodes", "native_mph_xml_snapshot")]
        + [_evidence(a["path"], "json:/nodes", "comsol_standalone_api_snapshot") for a in assets if a["role"] == "api_snapshot"]
        + [_evidence(a["path"], "xml:/Model", "verbatim_mph_xml_member") for a in assets if a["role"] == "raw_dmodel_xml"]))
    return {"title": title, "version": snapshot.get("version"), "model_snapshot": snapshot,
            "objects": objects, "assets": assets, "coverage": coverage, "warnings": warnings,
            "extractor_version": EXTRACTOR_VERSION,
            "extractor_dependencies": [{"path": str(dependency), "sha256": _hash(dependency)} for dependency in
                                       (JAVA_SOURCE, Path(__file__).with_name("case_structure.py"), Path(__file__).with_name("case_metadata.py"),
                                        Path(__file__).with_name("mph_saved_values.py"), Path(__file__).with_name("mph_saved_selections.py"),
                                        Path(__file__).with_name("tutorial_sources.py"))]}
