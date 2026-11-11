"""Export anonymous initial geometry from selected MPH files, without solving.

Source MPH files are never overwritten or duplicated. Only freshly created
geometry-only models enter the public benchmark; provenance remains private.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import zipfile
import xml.etree.ElementTree as ET

APP = Path(__file__).resolve().parents[1]
FORBIDDEN = {"Physics", "PhysicsFeature", "Material", "Study", "StudyFeature", "SolverSequence",
             "SolverFeature", "MeshSequence", "MeshFeature", "MultiphysicsCoupling"}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while data := stream.read(1024 * 1024):
            h.update(data)
    return h.hexdigest()


def check_mph(path, source_stem):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("dmodel.xml"))
        counts = {tag: len(list(root.iter(tag))) for tag in sorted(FORBIDDEN)}
        data = [n for n in z.namelist() if re.match(r"(?:mesh|solution|solutionstatic)\d+\.mphbin$", n, re.I)]
        text = "\n".join(z.read(n).decode("utf-8", errors="replace") for n in z.namelist()
                         if n.endswith((".xml", ".txt", ".json")))
    if any(counts.values()) or data:
        raise ValueError("Public MPH contains simulation settings or mesh/solution data")
    if source_stem.casefold() in text.casefold():
        raise ValueError("Public MPH contains the original case identity")
    return {"forbidden_node_counts": counts, "mesh_solution_members": data, "source_name_scan": "passed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=APP / "benchmarks/comsol-plan-v1")
    parser.add_argument("--comsol", type=Path, default=Path(r"D:\comsol\COMSOL63\Multiphysics"))
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--skip-ready", action="store_true")
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    root, comsol = args.root.resolve(), args.comsol.resolve()
    records = json.loads((root / "private/source-manifest.json").read_text(encoding="utf-8"))
    runtime = APP / "data/benchmark-geometry-runtime"
    for name in ("classes", "home", "tmp"):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    binary = comsol / "java/win64/jre/bin"
    subprocess.run([str(binary / "javac.exe"), "-encoding", "UTF-8", "-cp", str(comsol / "plugins/*"),
                    "-d", str(runtime / "classes"), str(APP / "scripts/mph_extract/BenchmarkGeometry.java")], check=True)
    results = []
    for record in records:
        cid = record["case_id"]
        if args.cases and cid not in args.cases:
            continue
        source = Path(record["source"])
        before = source.stat()
        if digest(source) != record["source_sha256"]:
            raise ValueError("Original source no longer matches selection: " + cid)
        out = root / "public/cases" / cid / "inputs"
        private = root / "private/cases" / cid
        out.mkdir(parents=True, exist_ok=True)
        private.mkdir(parents=True, exist_ok=True)
        report = private / "geometry-provenance.json"
        if args.skip_ready and report.is_file() and (out / "geometry.json").is_file():
            previous = json.loads(report.read_text(encoding="utf-8"))
            if previous.get("preparation_status") == "ready" and previous.get("source_sha256") == record["source_sha256"]:
                check_mph(out / "geometry_only.mph", source.stem)
                result = {"case_id": cid, "status": "ready", "reused": True}
                results.append(result)
                print(json.dumps(result), flush=True)
                continue
        # comp2 is an optimized, body-fitted verification result in case-15.
        component_filter = "comp1" if cid == "case-15" else "all"
        command = [str(binary / "java.exe"), "-Dfile.encoding=UTF-8", "-Djava.library.path=" + str(comsol / "bin/win64"),
                   "-Duser.home=" + str(runtime / "home"), "-Djava.io.tmpdir=" + str(runtime / "tmp"),
                   "-cp", str(runtime / "classes") + os.pathsep + str(comsol / "plugins/*"),
                   "BenchmarkGeometry", str(source), str(out), str(report), component_filter]
        started = time.monotonic()
        print(json.dumps({"case_id": cid, "stage": "geometry_only_export"}), flush=True)
        try:
            case_runtime = runtime / cid
            case_runtime.mkdir(exist_ok=True)
            env = dict(os.environ, TEMP=str(case_runtime), TMP=str(case_runtime), COMSOL_NUM_THREADS="1")
            proc = subprocess.run(command, cwd=case_runtime, env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=args.timeout)
            if proc.returncode:
                (private / "geometry-error.log").write_bytes(proc.stderr[-30000:] + proc.stdout[-10000:])
                raise RuntimeError(f"COMSOL geometry export exited {proc.returncode}")
            proof = check_mph(out / "geometry_only.mph", source.stem)
            for step in out.glob("*.step"):
                text = step.read_text(encoding="utf-8", errors="strict")
                text = re.sub(r"FILE_NAME\s*\(.*?\)\s*;", "FILE_NAME('geometry.step','',(''),(''),'benchmark geometry','COMSOL','');", text, count=1, flags=re.S)
                text = re.sub(r"FILE_DESCRIPTION\s*\(.*?\)\s*;", "FILE_DESCRIPTION(('Initial geometry'),'2;1');", text, count=1, flags=re.S)
                if source.stem.casefold() in text.casefold():
                    raise ValueError("Original case identity in neutral STEP")
                step.write_text(text, encoding="utf-8", newline="\n")
            provenance = json.loads(report.read_text(encoding="utf-8"))
            build_errors = [item for item in provenance["geometries"] if item["build_error"]]
            status = "review_required" if build_errors else "ready"
            geometry = json.loads((out / "geometry.json").read_text(encoding="utf-8"))
            geometry["assets"] = {p.name: {"bytes": p.stat().st_size, "sha256": digest(p)}
                                  for p in sorted(out.iterdir()) if p.is_file() and p.name != "geometry.json"}
            geometry["geometry_only_verification"] = proof
            geometry["preparation_status"] = status
            (out / "geometry.json").write_text(json.dumps(geometry, ensure_ascii=False, indent=2), encoding="utf-8")
            unchanged = (source.stat().st_size, source.stat().st_mtime_ns) == (before.st_size, before.st_mtime_ns) and digest(source) == record["source_sha256"]
            if not unchanged:
                raise ValueError("Original source changed during preparation")
            provenance.update(source_sha256=record["source_sha256"], original_unchanged=True,
                              preparation_status=status, public_geometry_proof=proof)
            report.write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
            (private / "geometry-error.log").unlink(missing_ok=True)
            result = {"case_id": cid, "status": status, "geometries": len(geometry["geometries"])}
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            result = {"case_id": cid, "status": "failed", "error": str(exc)}
        result["seconds"] = round(time.monotonic() - started, 2)
        results.append(result)
        print(json.dumps(result), flush=True)
        (root / "private/geometry-preparation-progress.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return int(any(item["status"] == "failed" for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
