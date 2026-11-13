/* On-demand case navigation. Hints never imply a source/configuration read. */
(function () {
  "use strict";
  const list = value => Array.isArray(value) ? value : [];
  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  }
  function mount(container, { call, openSource }) {
    let revision = 0, active = null;
    const reset = () => { revision++; active?.abort(); active = null; container.replaceChildren(); container.hidden = true; };
    async function show(caseId) {
      reset(); container.hidden = false;
      const ticket = revision;
      container.append(node("h3", "", "案例实现流程"), node("p", "muted", "展开环节查看配置线索、缺口和原文。导航提示不等于已核实设置。"));
      const button = node("button", "button button-subtle", "加载实现流程"); button.type = "button";
      container.append(button);
      button.addEventListener("click", async () => {
        button.disabled = true; button.textContent = "正在读取导航…";
        active = new AbortController();
        try {
          const guide = await call("get_case_guide", { case_id: caseId }, active.signal);
          if (ticket !== revision) return;
          button.remove();
          if (guide.state !== "ready") { container.append(node("p", "muted", guide.detail || "此案例尚未准备完整导航，可先阅读概要与关联来源。")); return; }
          const flow = node("ol", "case-guide-flow");
          const primary = ["misc", "geometry", "selection", "material", "physics", "multiphysics", "mesh", "study", "solver", "result"];
          const stages = list(guide.stages);
          for (const id of primary) {
            const stage = stages.find(item => item.id === id);
            if (stage) flow.append(renderStage(stage, ticket, caseId));
          }
          container.append(flow);
          const extra = stages.filter(stage => !primary.includes(stage.id) && (stage.source_count || list(stage.observed).length || list(stage.source_refs).length));
          if (extra.length) {
            const details = node("details", "case-guide-extra"); details.append(node("summary", "", "参数、函数与变量依赖"));
            const items = node("ol", "case-guide-flow"); extra.forEach(stage => items.append(renderStage(stage, ticket, caseId))); details.append(items); container.append(details);
          }
          const gaps = node("details", "case-guide-gaps"); gaps.append(node("summary", "", "提取缺口与适用性限制"));
          list(guide.gaps).forEach(gap => gaps.append(node("p", "muted", gap.detail || gap.code)));
          container.append(gaps);
        } catch (error) {
          if (ticket !== revision || error.name === "AbortError") return;
          button.disabled = false; button.textContent = "重试加载实现流程";
          container.append(node("p", "source-error", error.message || "导航读取失败"));
        }
      });
    }
    function renderStage(stage, ticket, caseId) {
      const item = node("li", "case-guide-step"), details = node("details", "case-guide-node" + (stage.id === "mesh" ? " case-guide-mesh" : ""));
      const summary = node("summary"); summary.append(node("strong", "", stage.title), node("span", "small-label", `${stage.source_count ?? list(stage.source_refs).length} 个证据入口`));
      details.append(summary);
      const body = node("div", "case-guide-step-body");
      if (stage.detail_available) {
        if (stage.key_hint) body.append(node("p", "muted", stage.key_hint));
        let loading = false, done = false;
        details.addEventListener("toggle", async () => {
          if (!details.open || loading || done || ticket !== revision) return;
          loading = true;
          const status = node("p", "muted", "正在读取环节导航…"); body.append(status);
          try {
            const guide = await call("get_case_guide", {case_id: caseId, stage: stage.id}, active?.signal);
            if (ticket !== revision) return;
            const expanded = list(guide.stages).find(item => item.id === stage.id);
            if (guide.state !== "ready" || !expanded) throw new Error(guide.detail || "此环节导航已失效，请重新加载案例。");
            body.replaceChildren(); fillStage(body, expanded, ticket); done = true;
          } catch (error) { if (ticket === revision && error.name !== "AbortError") status.textContent = error.message || "读取失败，收起后可重试。"; }
          finally { loading = false; }
        });
      } else fillStage(body, stage, ticket);
      details.append(body); item.append(details); return item;
    }
    function fillStage(body, stage, ticket) {
      list(stage.observed).forEach(fact => {
        const row = node("div", "case-guide-fact"); row.append(node("p", "", fact.hint));
        list(fact.property_hints).forEach(prop => row.append(node("p", "muted", `${prop.name}: ${prop.hint}${prop.complete ? "" : "（不完整）"}`)));
        row.append(node("small", "muted", fact.basis === "linked_tutorial_excerpt" ? "教程线索 · 与保存配置的一致性未核实" : "配置线索 · 实际生效状态未评估")); body.append(row);
      });
      list(stage.guidance).forEach(hint => body.append(node("p", "case-guide-check", `实施检查：${hint.text}`)));
      list(stage.gaps).forEach(gap => body.append(node("p", "muted", gap.detail || gap.code)));
      const refs = node("div", "case-guide-links");
      const seen = new Set();
      list(stage.source_refs).forEach(ref => {
        if (seen.has(ref.id)) return; seen.add(ref.id);
        const link = node("button", "text-button", ref.title || ref.id); link.type = "button";
        link.addEventListener("click", () => openSource(ref.id)); refs.append(link);
      }); body.append(refs);
      list(stage.configuration_requests).forEach(request => {
        const block = node("details", "case-guide-configuration"), summary = node("summary", "", `读取保存配置 · ${request.node_path}`);
        block.append(summary); let loading = false, done = false;
        block.addEventListener("toggle", async () => {
          if (!block.open || loading || done || ticket !== revision) return;
          loading = true; const status = node("p", "muted", "正在读取配置…"); block.append(status);
          try {
            const result = await call("read_case_configuration", request);
            if (ticket !== revision) return;
            status.remove(); done = true;
            block.append(node("p", "muted", "保存值不证明当前生效或已通过求解验证。"), node("pre", "case-guide-config-data", JSON.stringify(result, null, 2)));
          } catch (error) { if (ticket === revision) status.textContent = error.message || "配置读取失败；收起后可重试。"; }
          finally { loading = false; }
        }); body.append(block);
      });
    }
    return { show, reset };
  }
  window.SimAdvisorCaseGuide = { mount };
}());
