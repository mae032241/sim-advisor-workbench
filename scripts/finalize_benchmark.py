"""Publish anonymous benchmark inputs and private human-readable references."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sys
import zipfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from advisor.benchmark import (CATEGORIES, bundle_public, checks, dump_json, load_json,
                               sha256, validate_case)

LABELS = dict(zip(CATEGORIES, ("目标与范围", "几何与域", "物理、材料与边界", "网格与离散", "研究与求解", "验证与风险")))


def details(value):
    if isinstance(value, list):
        return "\n".join(details(v) for v in value)
    if isinstance(value, str):
        return "- " + value + "\n"
    if not isinstance(value, dict):
        return "- " + str(value) + "\n"
    description = value.get("description") or value.get("statement") or value.get("note")
    if not description:
        return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```\n"
    evidence = "; ".join(str(e.get("source_id", "")) + ": " + str(e.get("locator", "")) for e in value.get("evidence", []))
    return "- " + str(description) + ("\n  - 依据：" + evidence if evidence else "") + "\n"


def reference_markdown(reference):
    case = reference["case_id"]
    out = [f"# {case} 实现参考与判断清单（仅评审）", "", "不得提供给被测模型。原 MPH 的保存设置与教程说明分别保留证据；未执行求解。", ""]
    logic = reference["implementation"].get("logic_steps", [])
    if logic:
        out += ["```mermaid", "flowchart TD"]
        for i, item in enumerate(logic):
            label = item.get("description", str(item)) if isinstance(item, dict) else str(item)
            label = html.escape(label.replace("\n", " ")[:52], quote=True)
            out.append(f'  S{i}["{label}"]')
            if i:
                out.append(f"  S{i-1} --> S{i}")
        out += ["```", "", details(logic)]
    for key, label in (("physics", "物理逻辑"), ("materials_boundaries", "材料与边界"), ("mesh", "网格关键点与难点"),
                       ("study_solver", "研究顺序与求解"), ("validation", "验收与交叉检查"), ("alternatives", "可接受的替代路线")):
        out += [f"<details><summary>{label}</summary>", "", details(reference["implementation"].get(key, [])), "", "</details>", ""]
    out += ["## 原子判断题", "", "必需项未交代判不满足；题面不足、参考未核实或真实歧义才待复核。方法专属项仅在未采用对应方法时不适用。", ""]
    for category in CATEGORIES:
        out += [f"### {LABELS[category]}", ""]
        for check in checks(reference).values():
            if check["category"] != category:
                continue
            condition = check["applicability"].get("condition") or "始终适用"
            out += [f"- **{check['id']}**：{check['question']}",
                    f"  - 适用：{condition}；严重性：{check['severity']}",
                    f"  - 满足：{check['pass_condition']}", f"  - 不满足：{check['fail_condition']}"]
    out += ["", "## 参考缺口与矛盾", "", details(reference.get("unverified", [])), "", "## 证据来源", ""]
    for source in reference["sources"]:
        out.append(f"- `{source['id']}`：{source.get('kind', '')}；{source.get('path', '')}；SHA256 `{source.get('sha256', 'not recorded')}`")
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=APP / "benchmarks/comsol-plan-v1")
    args = parser.parse_args()
    root = args.root.resolve()
    reports, public_cases = [], []
    for i in range(1, 19):
        cid = f"case-{i:02d}"
        report = validate_case(root, cid)
        reports.append(report)
        public = root / "public/cases" / cid
        private = root / "private/cases" / cid
        reference = load_json(private / "reference.json")
        (private / "reference.md").write_text(reference_markdown(reference), encoding="utf-8")
        title = (public / "problem.md").read_text(encoding="utf-8").splitlines()[0].lstrip("# ")
        public_cases.append({"case_id": cid, "title": title, "problem": f"cases/{cid}/problem.md",
                             "initial_geometry": f"cases/{cid}/inputs/geometry_only.mph",
                             "geometry_facts": f"cases/{cid}/inputs/geometry.json",
                             "geometry_dimensions": report["geometry_dimensions"]})
    total_checks = sum(r["judgement_checks"] for r in reports)
    validation = {"schema": "planning-benchmark-validation/v1", "case_count": 18,
                  "status": "ready", "judgement_checks": total_checks, "cases": reports,
                  "source_models_unchanged": True, "mesh_run": False, "solver_run": False,
                  "weak_model_run": False, "model_judgement_run": False}
    dump_json(root / "private/validation.json", validation)
    dump_json(root / "public/manifest.json", {"schema": "simulation-planning-benchmark/v1", "case_count": 18,
        "task_type": "implementation_plan", "comsol_version": "6.3", "reference_visibility": "withheld",
        "cases": public_cases, "solver_execution_required": False})
    public_readme = """# COMSOL 实施方案测试题 v1

本目录只包含匿名任务、初始几何和必要物性/观测数据。每题要求提交实施方案，不要求求解。

`manifest.json` 列出18题。先读各题 `problem.md` 和数据文件；`inputs/geometry_only.mph` 不含物理场、材料、网格、研究或解。所有题有原生几何，三维题还提供 STEP。`geometry.json` 与 `topology.json` 给出单位、维度、域/边界包围盒及度量，纯文本模型也可据此规划。拓扑编号仅对应本题初始几何，不是参考模型的边界条件编号。

方案应说明几何处理、材料与方程、边界和初值、耦合、网格、研究/求解顺序、验证及风险，并引用实际读取的知识来源。不得声称已经完成网格或求解验证。对于不能从输入确定的内容，明确列出需要确认的事实。

可以使用工作台中的通用手册与其它案例，评测期间须保持“测试集检索”关闭。不要向被测模型提供评审目录或完整参考 MPH。
"""
    (root / "public/README.md").write_text(public_readme, encoding="utf-8")
    links = "\n".join(f"- [{row['case_id']}：{row['title']}](cases/{row['case_id']}/reference.md)" for row in public_cases)
    (root / "private/README.md").write_text("# 评审资料（禁止发送给被测模型）\n\n"
        f"18题，共{total_checks}条原子判断题；没有主观分值、权重或总分。\n\n"
        "先读实现流程，再逐条判断。每项记录满足、不满足、待复核或不适用，并引用方案原文。必需内容未提及是不满足；题面缺条件、参考不明确或真实歧义才待复核。\n\n"
        "程序核对引文确实出现在候选方案、所有题均有判定、always项不能跳过；按类别汇总结果并突出critical失败。引文核对不能代替物理判断，争议项仍需人工复核。\n\n" + links + "\n", encoding="utf-8")
    # Remove only known, generated construction intermediates, preserving any
    # evidence file explicitly referenced by the final private references.
    referenced = set()
    for p in (root / "private/cases").glob("*/reference.json"):
        for s in load_json(p)["sources"]:
            path = Path(s.get("path", ""))
            if path.is_absolute():
                referenced.add(path.resolve())
    temporary = list((root / "private/source-notes").rglob("*"))
    temporary += [root / "private/write_cases_c.py", root / "private/extract_cases_c.py"]
    for pattern in ("*/evidence-inventory.json", "*/settings-lines.txt"):
        temporary += list((root / "private/cases").glob(pattern))
    removed = 0
    for p in temporary:
        if p.is_file() and p.resolve().is_relative_to(root / "private") and p.resolve() not in referenced:
            removed += p.stat().st_size
            p.unlink()
    packages = root / "packages"
    packages.mkdir(exist_ok=True)
    public_package = bundle_public(root, packages / "planning-inputs-v1.zip")
    private_zip = packages / "evaluator-reference-v1.zip"
    with zipfile.ZipFile(private_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for p in sorted((root / "private").rglob("*")):
            if p.is_file():
                archive.write(p, p.relative_to(root / "private").as_posix())
    package_manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "public": public_package,
        "private": {"archive": str(private_zip), "bytes": private_zip.stat().st_size, "sha256": sha256(private_zip)},
        "removed_intermediate_bytes": removed, "validation": validation}
    dump_json(packages / "manifest.json", package_manifest)
    print(json.dumps({"cases": 18, "judgement_checks": total_checks, "public_bytes": public_package["bytes"],
                      "private_bytes": private_zip.stat().st_size, "removed_intermediate_bytes": removed,
                      "weak_model_run": False, "solver_run": False}))


if __name__ == "__main__":
    main()
