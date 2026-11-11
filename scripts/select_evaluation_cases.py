"""Select a diverse, reproducible evaluation split from existing parsed cases.

Reads existing catalog/compact SQLite files only. Does not parse MPH, run COMSOL,
embed, enable libraries, or rebuild indexes. The output is an administrator's
selection manifest, not an Agent tool response.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import sqlite3
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from advisor.catalog_storage import register_catalog_functions


# One case per task family, reviewed after comparing the full candidate pool.
CHOICES = (
    ("acdc-module", "superconducting_wire", "超导瞬态磁场", "磁场 H 公式与超导非线性瞬态"),
    ("acoustics-module", "submarine_asymptotic_scattering", "高频声学散射", "渐近散射接口，区别于常规压力声学体网格"),
    ("microfluidics-module", "slip_flow_benchmark", "稀薄气体滑移流", "滑移流边界和尺度条件，区别于普通无滑移流动"),
    ("battery-design-module", "pb_acid_battery_1d", "铅酸电池", "铅酸电化学及放电过程，区别于锂离子电池模板"),
    ("chemical-reaction-engineering-module", "liquid_liquid_extraction", "液液萃取", "连续相、分散相传质和混合物多相流"),
    ("wave-optics-module", "optical_yagi_uda_antenna", "光学边界元", "电磁波边界元与波长研究"),
    ("plasma-module", "icp_ccp_argon_chlorine", "射频偏压等离子体", "时间周期等离子体与感应电流、传热及流动耦合"),
    ("fuel-cell-and-electrolyzer-module", "soec_thermodynamics", "固体氧化物电解", "热力学、浓物质传递、多孔流动与电流分布耦合"),
    ("heat-transfer-module", "freeze_drying", "冻干与移动边界", "变形几何、多孔介质传热和渗流的瞬态耦合"),
    ("nonlinear-structural-materials-module", "biventricular_cardiac_model", "心肌非线性力学", "曲线坐标、PDE 辅助场与心肌大变形"),
    ("structural-mechanics-module", "vacancy_electromigration", "互连电迁移", "电流、温度、应力与固体传输共同驱动"),
    ("semiconductor-module", "gaas_pin_photodiode", "光电半导体", "频域电磁场与半导体载流子耦合"),
    ("electrodeposition-module", "cu_trench_deposition_pf", "电沉积移动界面", "相场与 Nernst–Planck 电流分布耦合"),
    ("subsurface-flow-module", "aquifer_characterization", "地下水参数反演", "Darcy 流、观测量和优化研究"),
    ("optimization-module", "beam_eigenfrequency_topology_optimization", "特征频率拓扑优化", "特征频率、稳态与拓扑优化研究的组合"),
    ("particle-tracing-module", "dielectrophoretic_separation", "介电泳粒子分离", "电场、蠕动流和瞬态粒子轨迹分步耦合"),
    ("rf-module", "tunable_cavity_filter", "机电调谐射频器件", "静电、固体变形与自适应频率扫描"),
    ("heat-transfer-module", "spacecraft_thermal_analysis", "航天器轨道热载荷", "轨道载荷、事件与温度研究的专用流程"),
)


def candidates(libraries_root: Path):
    with sqlite3.connect((libraries_root / "catalog.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        register_catalog_functions(db)
        rows = db.execute("""SELECT r.payload FROM revisions r
          JOIN case_members m ON m.citation_id=r.id AND m.local_id='model-case'
          JOIN active a ON a.library_id=r.library_id AND a.object_id=r.object_id AND a.citation_id=r.id""")
        result = []
        for (raw,) in rows:
            obj = json.loads(raw)
            if obj.get("coverage", {}).get("source_container", {}).get("status") == "preview_only":
                continue
            compact = obj.get("data", {}).get("compact_case", {}).get("path")
            if compact:
                result.append({k: obj[k] for k in ("id", "object_id", "library_id", "asset_key", "asset_hash", "title")}
                              | {"compact": compact})
    return result


def features(libraries_root: Path, case):
    path = (libraries_root / case["compact"]).resolve()
    if not path.is_relative_to(libraries_root):
        raise ValueError("Compact case escapes the library directory")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        rows = list(db.execute("""SELECT k.value,t.value,b.value FROM nodes n
          LEFT JOIN symbols k ON n.kind_id=k.id LEFT JOIN symbols t ON n.type_id=t.id
          LEFT JOIN symbols b ON n.branch_id=b.id"""))
    return dict(case,
        physics_types=sorted({t for k, t, b in rows if k == "Physics" and t}),
        study_types=sorted({t for k, t, b in rows if b == "study" and t}),
        coupling_types=sorted({t for k, t, b in rows if b == "multiphysics" and t}),
        physics_features=sorted({t for k, t, b in rows if k == "PhysicsFeature" and t}))


def analyze(cases):
    tokens = [{("p", v) for v in c["physics_types"]} | {("s", v) for v in c["study_types"]}
              | {("c", v) for v in c["coupling_types"]} | {("f", v) for v in c["physics_features"]}
              for c in cases]
    frequency = Counter(t for ts in tokens for t in ts)
    weights = {t: (1 + math.log((len(cases) + 1) / (n + 1))) * {"p": 4, "s": 2, "c": 3, "f": 1}[t[0]]
               for t, n in frequency.items()}
    norms = [math.sqrt(sum(weights[t] ** 2 for t in ts)) for ts in tokens]
    postings = defaultdict(list)
    for i, ts in enumerate(tokens):
        for token in ts:
            postings[token].append(i)
    for i, case in enumerate(cases):
        scores = defaultdict(float)
        for token in tokens[i]:
            for j in postings[token]:
                if j != i:
                    scores[j] += weights[token] ** 2
        nearest = sorted(((score / (norms[i] * norms[j]), j) for j, score in scores.items()), reverse=True)[:3]
        case["novelty"] = round(1 - nearest[0][0], 4) if nearest else 1.0
        case["nearest"] = [{"similarity": round(s, 4), "library_id": cases[j]["library_id"],
                            "asset_key": cases[j]["asset_key"]} for s, j in nearest]
    return cases


def select(cases):
    models = []
    for module, stem, direction, rationale in CHOICES:
        found = [c for c in cases if c["library_id"] == "cs63-models-" + module
                 and c["asset_key"].split("/")[-1] == stem + ".mph"]
        if len(found) != 1:
            raise ValueError(f"Expected one fully parsed candidate for {module}/{stem}, found {len(found)}")
        item = found[0]
        models.append({k: item[k] for k in ("library_id", "asset_key", "asset_hash", "object_id", "title",
                                           "physics_types", "study_types", "coupling_types", "novelty", "nearest")}
                      | {"direction": direction, "selection_reason": rationale})
    return {"schema": "evaluation-selection/v1", "id": "comsol-diverse-v1", "name": "COMSOL 差异案例测试集 v1",
            "description": "18 个方向的现有完整模型；模型设置和对应案例教程隔离，通用功能手册保留。",
            "selection_method": {"candidate_count": len(cases), "metric": "1 - maximum weighted feature cosine similarity",
                "features": ["physics interface", "study type", "multiphysics coupling", "physics feature"],
                "weights": {"physics": 4, "study": 2, "coupling": 3, "feature": 1},
                "note": "结构差异筛选与方向人工复核；不证明物理正确性或语义上的完全独立。"},
            "models": models}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = (args.data_dir / "libraries").resolve()
    raw = candidates(root)
    with ThreadPoolExecutor(max_workers=4) as pool:
        cases = list(pool.map(lambda c: features(root, c), raw))
    cases = [c for c in cases if c["physics_types"] and "livelink" not in c["library_id"]
             and not any(s in c["asset_key"].lower() for s in
                         ("geom_sequence", "geom_subsequence", "_geom.mph", "_geometry.mph", "_material.mph"))]
    result = select(analyze(cases))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"selected": len(result["models"]), "candidates": len(cases), "output": str(args.output),
                      "catalog_modified": False, "indexes_rebuilt": False}))


if __name__ == "__main__":
    main()
