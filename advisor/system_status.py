"""UI status joins component availability with the indexes it can actually use."""
from __future__ import annotations

import copy


def html_tree_status(source: dict) -> dict:
    """Small published-navigation status; never inspect source HTML here."""
    source = source if isinstance(source, dict) else {}
    counts = {key: max(0, value) if type(value) is int else 0 for key in (
        "tree_count", "enabled_tree_count", "available_tree_count", "node_count", "document_count", "package_count")
        for value in (source.get(key),)}
    ready = bool(source.get("ready") and counts["enabled_tree_count"])
    if ready:
        label, detail = "主导航就绪", "规划优先按章节定位接口与适用条件，再读取原文；QMD 补充术语、表达式和案例。读取时核对当前库权限和原文。"
    elif counts["tree_count"] and not counts["enabled_tree_count"]:
        label, detail = "已有目录 · 资料库未启用", "启用对应手册库后可参与规划，关闭的库及其旧来源 ID 不可新读。"
    elif source.get("state") in {"error", "failed", "unavailable"} or counts["tree_count"] and not counts["available_tree_count"]:
        label, detail = "目录待修复", "章节树当前不可用；可在资料库中补建目录，规划可继续用 QMD 补充检索。"
    else:
        label, detail = "尚未构造章节树", "支持的 HTML 手册建库时自动构造；已有库可补建章节树。没有原生目录的资料继续使用 QMD。"
    result = {"available": ready, "installed": True, "mode": "native-html-chapters", "state": source.get("state", "not_prepared"),
              "status_label": label, "detail": detail, **counts}
    if source.get("error"):
        result["error"] = str(source["error"])[:500]
    return result


def qmd_status(legacy: dict, indexes: dict) -> dict:
    dependencies = indexes.get("dependencies", {})
    installed = bool(legacy.get("installed") or legacy.get("available") or dependencies.get("qmd_available"))
    modes = {}
    for name in ("qmd_keyword", "qmd_vector"):
        source = indexes.get("modes", {}).get(name, {})
        modes[name] = {"ready": bool(source.get("ready")), "state": source.get("state", "not_built")}
        if source.get("error"):
            modes[name]["error"] = str(source["error"])[:800]
    keyword, vector = (modes[name]["ready"] for name in ("qmd_keyword", "qmd_vector"))
    modern_ready = installed and (keyword or vector)
    if modern_ready:
        label = "关键词与向量可用" if keyword and vector else "关键词可用" if keyword else "向量可用"
        detail = "按注册资料库的实际索引状态判断。各检索模式分别显示，不会自动构建或续跑向量索引。"
    elif installed:
        label, detail = "已安装 · 索引未就绪", "QMD 组件已配置；当前没有就绪的注册资料库索引。请构建 QMD 关键词索引并检查具体状态。"
    else:
        label, detail = "组件未配置", "未找到可用的 QMD 运行组件。已有索引保留；请先检查离线依赖配置。"
    return {**copy.deepcopy(legacy), "available": bool(modern_ready),
            "installed": installed, "component_available": installed,
            "semantic_available": bool(installed and vector),
            "mode": "qmd-registered-libraries" if modern_ready else "registered-library-index-required" if installed else "not-configured",
            "status_label": label, "detail": detail,
            "limitations": "关键词与向量索引分别核验；向量未就绪时不会用关键词结果冒充语义检索。",
            "library_indexes": {"ready": bool(modern_ready), "building": bool(indexes.get("building")), "modes": modes},
            "legacy": {"available": False, "searchable": False, "document_count": legacy.get("document_count", 0)}}


def services_status(corpus: dict, parser: dict, planner: dict) -> dict:
    """Report QMD readiness only from the registered-library engine."""
    backends = corpus.get("backends", {})
    pageindex = copy.deepcopy(backends.get("pageindex", {}))
    if pageindex.get("available"):
        pageindex.update(status_label="可用")
    elif not pageindex.get("installed"):
        pageindex.update(status_label="可选扩展未安装", detail="当前 Python 环境未找到 PageIndex SDK。普通 HTML 目录读取和已就绪的 QMD 关键词检索不依赖此扩展。")
    else:
        pageindex.update(status_label="待配置 / 核验", detail="PageIndex SDK 已安装；PDF 推理检索还需模型配置与实际核验。")
    planner = copy.deepcopy(planner)
    if planner.get("mode") == "disabled":
        planner.update(status_label="托管后端已关闭", detail="当前服务不启动工作台托管 Agent。外部 DSH 等 Agent 是否连接，取决于当前方案的会话绑定。")
    return {"sim_cli": parser, "html_tree": html_tree_status(corpus.get("html_tree", {})), "pageindex": pageindex,
            "qmd": qmd_status(backends.get("qmd", {}), corpus.get("library_indexes", {})), "planner": planner}
