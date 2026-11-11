"""Local defaults; SIM_ADVISOR_DATA separates persistent data from program files."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("SIM_ADVISOR_DATA", ROOT / "data")).resolve()
DOC_ROOT = Path(os.environ.get("COMSOL_DOC_ROOT", "D:/comsol/COMSOL63/Multiphysics/doc/help/wtpwebapps/ROOT/doc"))
APPLICATIONS = Path(os.environ.get("COMSOL_APPLICATIONS", "D:/comsol/COMSOL63/Multiphysics/applications"))


def codex_command() -> str | None:
    configured = os.environ.get("SIM_ADVISOR_CODEX")
    if configured:
        return str(Path(configured)) if Path(configured).is_file() else shutil.which(configured)
    found = shutil.which("codex")
    if found:
        return found
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
    candidates = list(base.glob("*/codex.exe")) if base.is_dir() else []
    return str(max(candidates, key=lambda p: p.stat().st_mtime)) if candidates else None


def examples() -> list[dict]:
    candidates = [
        ("bracket-step", "从纯 STEP 几何设计仿真路线", "Design_Module/Tutorial_Examples/bracket_import_optimization_geom.step",
         "我只有这个支架的 STEP 几何，没有物理场、材料和边界条件。想判断它在局部加热后是否会变形过大。请分析需求，比较可行建模路线，给出逐步实施指南、各环节的缺口与风险，以及需要我确认的信息。不要照搬教程工况。"),
        ("bracket-basic", "从结构模型规划热应力分析", "Structural_Mechanics_Module/Tutorials/bracket_basic.mph",
         "我有一个支架结构模型，想分析非均匀加热下的温度分布、热变形和最大等效应力。请检查已有模型，给出需要补充的物理场、边界条件、耦合方法和验证步骤。"),
        ("bracket-thermal", "审查已有热膨胀模型", "Structural_Mechanics_Module/Tutorials/bracket_thermal.mph",
         "检查这个支架热膨胀模型，整理现有设置，并规划温度载荷变化时的位移和应力参数化研究。说明哪些输入还需要我确认。"),
    ]
    return [{"id": key, "title": title, "path": str(APPLICATIONS / path), "goal": goal}
            for key, title, path, goal in candidates if (APPLICATIONS / path).is_file()]
