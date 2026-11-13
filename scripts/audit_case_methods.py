"""Audit frozen case methods and render their explanations without a model call.

The audit checks recorded provenance, not the truth of generated interpretations.
It reads only an existing experiment folder; no catalog, index or library flags
are changed. Tutorial excerpts are not copied into the resulting report.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def reference_key(ref):
    return tuple(ref.get(key) for key in ("source_id", "context", "locator"))


def tutorial_source(dependency):
    # COMSOL application-library tutorials have this recorded asset namespace.
    # A model overview and a general manual page must not satisfy this check.
    return str(dependency.get("asset_key", "")).startswith("help:com.comsol.help.models.")


def inspect_pack(path, expected_sha):
    payload = path.read_bytes()
    if sha(payload) != expected_sha:
        raise ValueError(f"Frozen pack changed: {path.name}")
    pack = json.loads(payload)
    profile = pack["profile"]
    if sha(canonical(profile)) != pack["profile_sha256"]:
        raise ValueError(f"Profile hash mismatch: {path.name}")
    case_id = profile["case_id"]
    dependencies = pack["dependencies"]
    if dependencies[case_id]["type"] != "model_case":
        raise ValueError("The assigned source is not a model case")
    tutorials = {key for key, dep in dependencies.items() if tutorial_source(dep)}
    receipts = {reference_key(ref): ref["origin"] for ref in pack["reads"]}
    claims = []
    for section, values in profile["sections"].items():
        for index, value in enumerate(values):
            refs = value["evidence_refs"]
            for ref in refs:
                key = reference_key(ref)
                if key not in receipts or ref["source_id"] not in dependencies:
                    raise ValueError("A claim lacks a recorded bound read reference")
                if value["origin"] not in ("llm_inference", "unknown") and receipts[key] != value["origin"]:
                    raise ValueError("Claim origin differs from the recorded read origin")
            text = any(ref["source_id"] in tutorials and ref["context"] == "source_text" for ref in refs)
            config = any(ref["source_id"] == case_id and ref["context"] == "case_configuration" for ref in refs)
            claims.append({"section": section, "index": index, **value,
                           "joint_tutorial_configuration": text and config})
    core_joint = [claim for claim in claims if claim["joint_tutorial_configuration"]
                  and claim["origin"] == "llm_inference"
                  and claim["section"] in ("method_and_assumptions", "transfer_and_setting_dependencies")]
    tutorial_reads = [ref for ref in pack["reads"]
                      if ref["source_id"] in tutorials and ref["context"] == "source_text"]
    config_reads = [ref for ref in pack["reads"]
                    if ref["source_id"] == case_id and ref["context"] == "case_configuration"]
    return {"case_id": case_id, "model_asset": dependencies[case_id]["asset_key"],
            "pack": str(path), "pack_sha256": expected_sha, "pack_bytes": len(payload),
            "tutorial_assets": sorted({dependencies[key]["asset_key"] for key in tutorials}),
            "tutorial_read_ranges": len(tutorial_reads), "configuration_read_ranges": len(config_reads),
            "claim_count": len(claims), "origin_counts": dict(Counter(claim["origin"] for claim in claims)),
            "joint_core_claim_count": len(core_joint),
            "joint_construction_recorded": bool(tutorial_reads and config_reads and core_joint),
            "claims": claims}


def audit(root):
    root = Path(root).resolve(strict=True)
    manifest = read_json(root / "profile-manifest.json")
    rows = []
    for entry in manifest["profiles"]:
        path = (root / entry["path"]).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("Profile manifest points outside the experiment")
        rows.append(inspect_pack(path, entry["sha256"]))
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate case profiles")
    build = read_json(root / "manifest.json")
    if set(row["case_id"] for row in rows) != set(build["case_ids"]):
        raise ValueError("Not all requested cases have frozen profiles")
    return {"schema": "case-method-joint-audit/v1", "created_at": datetime.now(timezone.utc).isoformat(),
            "method_root": str(root), "model": build["model"], "effort": build["effort"],
            "profile_count": len(rows),
            "joint_construction_count": sum(row["joint_construction_recorded"] for row in rows),
            "total_pack_bytes": sum(row["pack_bytes"] for row in rows),
            "boundary": "Recorded source provenance only; no claim of semantic agreement, activation, solver success or physical validity.",
            "cases": rows}


def markdown(report):
    lines = ["# 案例方法说明与联合证据审计", "",
             "此文件由脚本从冻结产物生成；审计证明来源记录完整，不证明解释或物理路线正确。", "",
             f"构造模型：{report['model']} / {report['effort']}。案例数：{report['profile_count']}。",
             f"方法或依赖章节包含双方证据的案例：{report['joint_construction_count']}。", "",
             "| 案例 | 教程读取范围 | 配置读取范围 | 核心联合解释 | 文件字节 |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for row in report["cases"]:
        lines.append(f"| {row['model_asset']} | {row['tutorial_read_ranges']} | {row['configuration_read_ranges']} | {row['joint_core_claim_count']} | {row['pack_bytes']} |")
    for row in report["cases"]:
        lines.extend(["", f"## {row['model_asset']}", "", f"案例 ID：`{row['case_id']}`", ""])
        previous = None
        for claim in row["claims"]:
            if claim["section"] != previous:
                previous = claim["section"]
                lines.extend([f"### {previous}", ""])
            lines.append(f"- [{claim['origin']}] {claim['explanation']}")
            for limitation in claim["limitations"]:
                lines.append(f"  - 局限：{limitation}")
            for ref in claim["evidence_refs"]:
                lines.append(f"  - 证据：`{ref['source_id']}` / `{ref['context']}` / `{ref['locator']}`")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--require-joint", action="store_true")
    args = parser.parse_args(argv)
    report = audit(args.method_root)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, text in (("joint-source-audit.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n"),
                       ("method-notes.md", markdown(report))):
        with (args.output / name).open("x", encoding="utf-8") as stream:
            stream.write(text)
    print(json.dumps({key: report[key] for key in ("profile_count", "joint_construction_count", "total_pack_bytes")}, ensure_ascii=False))
    return int(args.require_joint and report["joint_construction_count"] != report["profile_count"])


if __name__ == "__main__":
    raise SystemExit(main())
