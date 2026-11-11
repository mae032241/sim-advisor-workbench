"""Prepare blinded geometry in private workspaces; publish only neutral assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET

APP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP))
from advisor.geometry_reader import _python_command
from advisor.model_reader import inspect_model
from advisor.process_runner import run_text

COMSOL = Path(os.environ.get("SIM_ADVISOR_COMSOL_HOME", "D:/comsol/COMSOL63/Multiphysics"))
DEFAULT_ROOT = APP / "data" / "geometry-challenge"
JAVA_SOURCE = Path(__file__).with_name("PrepareGeometry.java")
WRAPPER = "import json,os,subprocess,sys;c=json.loads(sys.argv[1]);e=dict(os.environ,TEMP=sys.argv[2],TMP=sys.argv[2],COMSOL_NUM_THREADS='1');p=subprocess.run(c,env=e,stdin=subprocess.DEVNULL,stdout=sys.stdout,stderr=sys.stderr);sys.exit(p.returncode)"
FORBIDDEN_MODEL_TAGS = {"Physics", "PhysicsFeature", "Material", "Study", "StudyFeature", "SolverSequence", "SolverFeature", "MeshSequence", "MeshFeature", "MultiphysicsCoupling"}
DEFAULT_FORBIDDEN_TERMS = ("thermal", "solidmechanics", "heattransfer", "spring", "contact")

NORMALIZE = r'''
import gmsh,sys,json,math
gmsh.initialize([],readConfigFiles=False,run=False)
def observe():
    bodies=gmsh.model.getEntities(3)
    bounds=list(gmsh.model.getBoundingBox(-1,-1))
    return {'topology':{name:len(gmsh.model.getEntities(d)) for d,name in enumerate(('vertices','edges','faces','solids'))},
            'bbox_m':[x/1000 for x in bounds],
            'volume_sum_m3':sum(gmsh.model.occ.getMass(3,t) for _,t in bodies)/1e9}
try:
    gmsh.option.setNumber('General.Terminal',0)
    gmsh.option.setNumber('General.NumThreads',1)
    gmsh.option.setNumber('Geometry.OCCImportLabels',0)
    gmsh.option.setString('Geometry.OCCTargetUnit','MM')
    gmsh.model.add('part_01')
    gmsh.model.occ.importShapes(sys.argv[1],highestDimOnly=False,format='step')
    gmsh.model.occ.synchronize()
    before=observe()
    for i,(_,tag) in enumerate(gmsh.model.getEntities(3),1):gmsh.model.setEntityName(3,tag,'body_%03d'%i)
    gmsh.write(sys.argv[2])
    gmsh.clear();gmsh.model.add('part_01')
    gmsh.model.occ.importShapes(sys.argv[2],highestDimOnly=False,format='step');gmsh.model.occ.synchronize()
    after=observe();bodies=gmsh.model.getEntities(3);pairs=[]
    if len(bodies)<=24:
        for i,(_,a) in enumerate(bodies):
            for j,(_,b) in enumerate(bodies[i+1:],i+1):
                result=gmsh.model.occ.getDistance(3,a,3,b)
                pairs.append({'a':'body_%03d'%(i+1),'b':'body_%03d'%(j+1),'distance_m':result[0]/1000 if result[0]>=0 else None})
    print('NEUTRAL_GEOMETRY='+json.dumps({'before':before,'after':after,'pair_distances':pairs,'kernel_version':gmsh.__version__}))
finally:gmsh.finalize()
'''


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def neutral_header(text):
    """Replace only generated header metadata, preserving all CAD declarations."""
    text = re.sub(r"FILE_NAME\s*\(.*?\)\s*;", "FILE_NAME('part_01.step','',(''),(''),'geometry preparation','OpenCASCADE','');", text, count=1, flags=re.S)
    return re.sub(r"FILE_DESCRIPTION\s*\(.*?\)\s*;", "FILE_DESCRIPTION(('Neutral CAD geometry'),'2;1');", text, count=1, flags=re.S)


def contains_forbidden_term(text, forbidden_terms=DEFAULT_FORBIDDEN_TERMS):
    """Literal metadata checks; caller-supplied terms never become regex code."""
    folded = text.casefold()
    return any(str(term).casefold() in folded for term in forbidden_terms if str(term).strip())


def verify_geometry_only_mph(path, forbidden_terms=DEFAULT_FORBIDDEN_TERMS):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        root = ET.fromstring(archive.read("dmodel.xml"))
        counts = {tag: len(list(root.iter(tag))) for tag in sorted(FORBIDDEN_MODEL_TAGS)}
        data_members = [n for n in names if re.match(r"(?:mesh|solution|solutionstatic)\d+\.mphbin$", n, re.I)]
        text_members = [n for n in names if n.endswith((".xml", ".json", ".txt"))]
        text = "\n".join(archive.read(n).decode("utf-8", errors="replace") for n in text_members)
    if any(counts.values()) or data_members:
        raise ValueError("Fresh geometry MPH contains forbidden simulation nodes/data")
    if contains_forbidden_term(text, forbidden_terms):
        raise ValueError("Fresh geometry MPH leaks a source case name")
    return {"forbidden_simulation_node_counts": counts, "mesh_solution_members": data_members,
            "source_case_name_scan": "passed", "state": "geometry_only"}


def write_public_report(root, summary, provenance, proof):
    comparison = provenance.get("neutralization_comparison")
    checks = None
    if comparison:
        a, b = comparison["before"], comparison["after"]
        checks = {"scope": "CAD exchange/re-export only; not physical-output validation",
                  "solid_count_preserved": a["topology"]["solids"] == b["topology"]["solids"],
                  "maximum_bbox_difference_m": max(abs(x-y) for x,y in zip(a["bbox_m"], b["bbox_m"])),
                  "volume_sum_difference_m3": abs(a["volume_sum_m3"] - b["volume_sum_m3"]),
                  "bbox_check_tolerance_m": 5e-7, "volume_relative_check_tolerance": 1e-6}
    report = {"schema": "geometry-challenge-preparation/v1", "status": "complete",
              "input_files": summary["assets"], "neutral_geometry": summary,
              "created_from_empty_model": provenance["fresh_model"]["created_from_empty_model"],
              "fresh_phase_loaded_source_model": provenance["fresh_model"]["source_model_loaded"],
              "geometry_build_authorized_and_run": True, "mesh_run": False, "solver_run": False,
              "original_hash_mtime_size_verified_unchanged": bool(provenance.get("original_unchanged")),
              "original_identity_and_reference_material": "withheld; private audit only",
              "neutral_step_metadata_scan": "passed" if summary["format"] == "step" else "not_applicable",
              "actual_sim_cli_and_gmsh_read": summary["format"] == "step",
              "fresh_model_proof": proof, "exchange_geometry_checks": checks,
              "geometry_validity": "not_certified", "physical_output_validation": "not_run"}
    (Path(root) / "preparation-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def run_java(mode, source, private, destination, report):
    runtime = private / "runtime"
    for sub in ("classes", "home", "tmp"):
        (runtime / sub).mkdir(parents=True, exist_ok=True)
    java = COMSOL / "java" / "win64" / "jre" / "bin" / "java.exe"
    compiler = java.with_name("javac.exe")
    compiled = run_text([str(compiler), "-encoding", "UTF-8", "-cp", str(COMSOL / "plugins" / "*"), "-d", str(runtime / "classes"), str(JAVA_SOURCE)], "", runtime, 35)
    if compiled.returncode:
        raise RuntimeError("Geometry preparation compilation failed: " + compiled.stderr[:1200])
    command = [str(java), "-Dfile.encoding=UTF-8", "-Djava.library.path=" + str(COMSOL / "bin" / "win64"),
               "-Duser.home=" + str(runtime / "home"), "-Djava.io.tmpdir=" + str(runtime / "tmp"),
               "-cp", str(runtime / "classes") + os.pathsep + str(COMSOL / "plugins" / "*"),
               "PrepareGeometry", mode, str(source), str(destination), str(report)]
    result = run_text([sys.executable, "-c", WRAPPER, json.dumps(command), str(runtime / "tmp")], "", runtime, 180, 2 * 1024 * 1024)
    if result.returncode or not report.is_file():
        (private / "preparation-error.txt").write_text(result.stderr[:20000], encoding="utf-8")
        raise RuntimeError(f"Geometry-only COMSOL {mode} failed (exit {result.returncode}); see private error artifact")
    return json.loads(report.read_text(encoding="utf-8"))


def prepare(source, root=DEFAULT_ROOT, *, forbidden_terms=()):
    source, root = Path(source).resolve(strict=True), Path(root).resolve()
    private, inputs = root / "private" / "preparation-01", root / "inputs"
    private.mkdir(parents=True, exist_ok=True)
    inputs.mkdir(parents=True, exist_ok=True)
    source_stat, source_sha = source.stat(), file_hash(source)
    with zipfile.ZipFile(source) as archive:
        metadata = ET.fromstring(archive.read("modelinfo.xml"))
        info = metadata.attrib
        if info.get("nodeType") == "preview" or "dmodel.xml" not in archive.namelist():
            raise ValueError("Selected source is preview-only or lacks model geometry configuration")
        dimensions = [node.get("dimension") for node in metadata.findall("./geometryInfo/geom")]
        if not dimensions or any(dimension != "3" for dimension in dimensions):
            raise ValueError("Selected source must declare real 3D geometry")
    forbidden_terms = tuple(dict.fromkeys((*DEFAULT_FORBIDDEN_TERMS, source.stem, *forbidden_terms)))
    copied = private / "source.mph"
    shutil.copyfile(source, copied)
    provenance = {"original_path": str(source), "original_sha256": source_sha, "original_size": source_stat.st_size,
                  "original_mtime_ns": source_stat.st_mtime_ns, "model_metadata": info,
                  "private_source_copy": str(copied), "geometry_dimensions": dimensions,
                  "metadata_forbidden_terms": forbidden_terms,
                  "extraction_scope": "geometry_only", "mesh_run": False, "solver_run": False}
    (private / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    exported = run_java("export", copied, private, private, private / "export-report.json")
    provenance["comsol_export"] = exported
    if exported.get("step_export"):
        candidate = private / "part_01.step"
        result = run_text([_python_command()[0], "-c", NORMALIZE, str(private / "intermediate.step"), str(candidate)], "", private, 100)
        records = [line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("NEUTRAL_GEOMETRY=")]
        if result.returncode or len(records) != 1:
            raise RuntimeError("Neutral STEP re-export/inspection failed")
        comparison = json.loads(records[0])
        a, b = comparison["before"], comparison["after"]
        if a["topology"]["solids"] != b["topology"]["solids"] or max(abs(x-y) for x,y in zip(a["bbox_m"], b["bbox_m"])) > 5e-7:
            raise ValueError("Neutral re-export failed solid-count/bounding-box checks")
        if abs(a["volume_sum_m3"]-b["volume_sum_m3"]) > max(1e-12, abs(a["volume_sum_m3"]) * 1e-6):
            raise ValueError("Neutral re-export changed aggregate geometry volume")
        content = neutral_header(candidate.read_text(encoding="utf-8"))
        if contains_forbidden_term(content, forbidden_terms):
            raise ValueError("Neutral STEP contains source-case/physics metadata")
        final_input = inputs / "part_01.step"
        final_input.write_text(content, encoding="utf-8")
        inspection = inspect_model(final_input)
        if not inspection["parser_detail"]["scan_succeeded"] or inspection["geometry"]["kernel"]["status"] != "imported":
            raise ValueError("Neutral STEP did not pass actual sim-cli/Gmsh inspection")
        provenance["neutralization_comparison"] = comparison
        pairs = comparison["pair_distances"]
        known_distances = [p["distance_m"] for p in pairs if p["distance_m"] is not None]
        summary = {"input_file": "part_01.step", "format": "step", "unit": "m", "step_length_unit": "mm",
                   "cad_dimension": 3, "asset_kind": "cad_geometry",
                   "simulation_state": "not_defined", "topology": b["topology"],
                   "bounding_box_m": {"min": b["bbox_m"][:3], "max": b["bbox_m"][3:]},
                   "volume_sum_m3": b["volume_sum_m3"], "multiple_solids": b["topology"]["solids"] > 1,
                   "minimum_body_pair_distance_m": min(known_distances) if known_distances else None,
                   "pairwise_separation_status": "some_pairs_touch_or_overlap_within_tolerance" if known_distances and min(known_distances) <= 1e-7 else "separated_by_positive_distance" if known_distances else "not_checked",
                   "pairwise_check_tolerance_m": 1e-7,
                   "limitations": ["CAD body count is not an assembly-role or simulation-domain classification.", "Zero pair distance does not establish physical contact, sealing or absence of overlap.", "Volume is a per-solid sum; overlaps are not subtracted."]}
        (root / "preparation-neutral-inspection.json").write_text(json.dumps(inspection, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        final_input = inputs / "part_01.mphbin"
        shutil.copyfile(private / "native_geometry.mphbin", final_input)
        summary = {"input_file": final_input.name, "format": "native_geometry", "step_status": "unavailable",
                   "asset_kind": "cad_geometry", "simulation_state": "not_defined", "limitations": ["STEP export unavailable; no STEP claim is made."]}
    fresh = run_java("fresh", final_input, private, inputs, private / "fresh-model-report.json")
    if any(fresh.get(key) for key in ("physics_tags", "material_tags", "mesh_tags", "study_tags", "solver_tags", "multiphysics_tags", "pair_tags")):
        raise ValueError("Fresh model contains non-geometry configuration")
    proof = verify_geometry_only_mph(inputs / "geometry_only.mph", forbidden_terms)
    provenance["fresh_model"] = fresh
    provenance["geometry_only_zip_check"] = proof
    if file_hash(source) != source_sha or (source.stat().st_size, source.stat().st_mtime_ns) != (source_stat.st_size, source_stat.st_mtime_ns):
        raise ValueError("Original held-out model changed")
    provenance["original_unchanged"] = True
    provenance["public_assets"] = {p.name: {"sha256": file_hash(p), "size_bytes": p.stat().st_size} for p in (final_input, inputs / "geometry_only.mph")}
    (private / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["assets"] = provenance["public_assets"]
    summary["fresh_model_geometry_only_verified"] = True
    (root / "preparation-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_public_report(root, summary, provenance, proof)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--forbidden-term", action="append", default=[], help="Additional private source/physics metadata term to reject in published CAD")
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.output_root, forbidden_terms=args.forbidden_term), ensure_ascii=False, indent=2))
