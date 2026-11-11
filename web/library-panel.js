/* Independent local-library panel. Mount with SimAdvisorLibraryPanel.mount(node). */
(function () {
  "use strict";
  const instances = new WeakMap();
  const list = (value) => Array.isArray(value) ? value : [];
  const objectText = (value) => value === undefined || value === null ? "未报告" : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
  const typeLabels = { "official-manuals": "官方手册资料", "official-models": "官方模型案例", "user-manuals": "自定义手册 / 文档", "user-models": "自定义模型案例" };
  const isManual = library => ["official-manuals", "user-manuals"].includes(library?.type);
  const objectLabels = { model_case: "模型案例", saved_configuration: "保存态功能配置", method_step: "方法步骤", code_example: "代码示例", code_fragment: "代码片段", document: "资料章节", manual_section: "手册章节", manual_topic: "手册主题", api_reference: "API 参考", implementation_snippet: "实现代码片段" };
  const stageLabels = { queued: "等待构建", scanning: "扫描绑定目录", extracting: "逐文件提取 MPH / 文档", deep_api: "补充 Deep API 观测", publishing: "发布 API 增强版本", cleaning_indexes: "回收旧索引", archiving: "归档资料对象", indexing: "构建检索索引", indexing_native: "构建原生索引", indexing_keyword: "构建关键词索引", indexing_vector: "构建向量索引", cleaning_corpus: "清理临时检索语料", finished: "处理结束" };
  const statusLabels = { queued: "排队中", running: "处理中", cancel_requested: "正在中断", completed: "已完成", succeeded: "已完成", partial: "部分完成 · 存在资料缺口", failed: "失败", interrupted: "已中断", cancelled: "已中断" };
  const modeLabels = { native: "原生层级检索", qmd_keyword: "QMD 关键词索引", qmd_vector: "QMD 向量索引", pageindex: "PageIndex", keyword: "关键词索引", vector: "向量索引" };
  const vectorMode = mode => ["qmd_vector", "vector"].includes(mode);
  const terminal = new Set(["completed", "succeeded", "partial", "failed", "interrupted", "cancelled"]);
  const pending = (job) => ["queued", "running", "cancel_requested"].includes(job?.status);
  const canCancel = (job) => pending(job) && job.cancellable !== false && job.cancel_supported !== false;
  const jobApplies = (job, libraryId) => !libraryId || job.library_id === libraryId || list(job.library_ids).includes(libraryId);
  const count = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
  const countText = (value) => count(value) === null ? "未报告" : String(value);
  const units = { files: "个文件", objects: "个对象", chunks: "个文本块", indexes: "个索引", cases: "个模型案例" };
  function coverageHasGaps(value) {
    const raw = value?.raw_xml, normalized = raw?.normalization || {}, api = value?.deep_api, features = value?.saved_features;
    return ["partial", "missing", "failed", "unavailable"].includes(raw?.status) || raw?.truncated === true ||
      [normalized.properties_partial, normalized.properties_unresolved, normalized.selection_statuses?.unresolved].some(n => count(n) > 0) ||
      ["partial", "failed", "unavailable", "preview_only"].includes(api?.status) || count(api?.read_errors) > 0 || api?.truncated === true ||
      [features?.omitted_nodes, features?.omitted_properties, features?.omitted_selections].some(n => count(n) > 0);
  }
  function coverageText(value) {
    const parts = [], raw = value?.raw_xml, normalized = raw?.normalization || {}, api = value?.deep_api, features = value?.saved_features;
    if (raw) {
      parts.push(`保存态：已读 ${countText(raw.node_count)} 个节点，${countText(normalized.properties_total)} 项属性`);
      if (count(normalized.properties_partial) > 0) parts.push(`属性未完整解码 ${normalized.properties_partial} 项`);
      if (count(normalized.properties_unresolved) > 0) parts.push(`属性未解析 ${normalized.properties_unresolved} 项`);
      if (count(normalized.selection_statuses?.unresolved) > 0) parts.push(`选择编码未解析 ${normalized.selection_statuses.unresolved} 项`);
      if (["missing", "failed", "unavailable"].includes(raw.status)) parts.push("保存态读取缺失");
      if (raw.truncated) parts.push("保存态读取已截断");
    }
    if (features) {
      parts.push(`保存态检索：${countText(features.object_count)} 个条目`);
      if ([features.omitted_nodes, features.omitted_properties, features.omitted_selections].some(n => count(n) > 0)) parts.push(`未纳入检索：${countText(features.omitted_nodes)} 个节点、${countText(features.omitted_properties)} 项属性、${countText(features.omitted_selections)} 项选择编码`);
    }
    if (api?.status === "preview_only") parts.push("模型仅含预览，缺少实际配置");
    else if (api && api.status !== "not_requested") {
      parts.push(`API：已读 ${countText(api.node_count)} 个节点，${countText(api.read_errors)} 项读取缺口`);
      if (["failed", "unavailable"].includes(api.status)) parts.push("API 读取未完成");
      if (api.truncated) parts.push("API 读取已截断");
    }
    if (parts.length) parts.push("保存值未验证为有效设置，未运行几何、网格或求解");
    return parts.join(" · ");
  }
  function jobCounts(job) {
    const reported = job.counts || {}, items = Array.isArray(job.items) ? job.items : null;
    const partial = items?.filter((item) => item.status === "partial" || coverageHasGaps(item.coverage) || item.enrichment?.status === "failed").length;
    return { succeeded: count(reported.succeeded) ?? (items ? items.length - partial : null), partial: count(reported.partial) ?? partial ?? null, failed: count(reported.failed) ?? (Array.isArray(job.failed) ? job.failed.length : null) };
  }
  function stageProgress(job) {
    const progress = job.progress || {};
    // Legacy completed/total count file attempts, never indexing completion.
    const legacyFiles = !job.progress && ["extracting", "finished"].includes(job.stage);
    const completed = count(progress.completed) ?? (legacyFiles ? count(job.completed) : null);
    const total = count(progress.total) ?? (legacyFiles ? count(job.total) : null);
    return { completed, total, unit: progress.unit || (legacyFiles ? "files" : null), determinate: total > 0 && completed !== null && completed <= total,
      currentFile: typeof progress.current_file === "string" ? progress.current_file : typeof job.current_file === "string" ? job.current_file : "", detail: typeof progress.detail === "string" ? progress.detail : "" };
  }
  function newerJob(current, incoming) {
    if (!current) return incoming;
    if (terminal.has(current.status) && pending(incoming)) return current;
    if (current.status === "cancel_requested" && ["queued", "running"].includes(incoming.status)) return current;
    const previous = Date.parse(current.updated_at || ""), next = Date.parse(incoming.updated_at || "");
    return Number.isFinite(previous) && Number.isFinite(next) && next < previous ? current : incoming;
  }
  function elapsedText(seconds) {
    if (count(seconds) === null) return "未报告";
    const value = Math.floor(seconds), minutes = Math.floor(value / 60);
    return minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分` : minutes ? `${minutes} 分 ${value % 60} 秒` : `${value} 秒`;
  }
  function byteText(value) {
    if (count(value) === null) return "未报告大小";
    if (value < 1024) return `${value} B`;
    const units = ["KiB", "MiB", "GiB", "TiB"]; let size = value / 1024, unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; }
    return `${size.toFixed(size < 10 ? 2 : 1)} ${units[unit]}`;
  }
  function node(tag, cls, text) { const value = document.createElement(tag); if (cls) value.className = cls; if (text !== undefined) value.textContent = String(text); return value; }
  function button(text, cls = "lp-button") { const value = node("button", cls, text); value.type = "button"; return value; }
  function field(labelText, input) { const label = node("label", "lp-field"); label.append(node("span", "lp-field-label", labelText), input); return label; }
  function input(type, placeholder) { const value = node("input", "lp-input"); value.type = type; if (placeholder) value.placeholder = placeholder; return value; }
  function select(options) { const value = node("select", "lp-input"); options.forEach(([key, label]) => { const item = node("option", "", label); item.value = key; value.append(item); }); return value; }
  function date(value) { if (!value) return "未记录"; const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("zh-CN", { hour12: false }); }
  function indexEntries(indexes) {
    const values = indexes?.modes || indexes?.indexes || indexes?.backends || indexes || {};
    if (Array.isArray(values)) return values.map((item) => typeof item === "string" ? [item, {}] : [item.mode || item.id || item.name, item]).filter(([key]) => key);
    return Object.entries(values).filter(([, value]) => value && typeof value === "object" && !Array.isArray(value));
  }

  function mount(target, options = {}) {
    if (typeof target === "string") target = document.querySelector(target);
    if (!(target instanceof Element)) throw new Error("Library panel mount requires a DOM element");
    if (instances.has(target)) instances.get(target).destroy();
    const state = { libraries: [], evaluationSets: [], evaluationStatus: "idle", evaluationError: "", selectedEvaluation: null, jobs: [], indexes: {}, selected: null, libraryView: "current", libraryAction: null, modelSelections: new Map(), createBusy: false, buildBusy: false, searchBusy: false, destroyed: false, refreshSequence: 0, searchSequence: 0, sourceSequence: 0, configurationSequence: 0, configurationController: null, embedding: {enabled: false, configured: false, model_path: ""}, embeddingStatus: "loading", embeddingError: "", embeddingSequence: 0, embeddingController: null, embeddingSaving: false, pollTimer: null, elapsedTimer: null, polls: new Map(), pollErrors: new Map(), cancelRequests: new Map(), cancelErrors: new Map(), observations: new Map(), modeChoices: new Map() };
    state.evaluationRetrievalEnabled = false; state.evaluationAccessKnown = false; state.evaluationAccessToken = ""; state.evaluationAccessSaving = false; state.evaluationAccessError = "";
    const apiBase = (options.apiBase || "").replace(/\/$/, "");
    state.libraryQuery = ""; state.libraryStatus = "all"; state.expandedGroups = new Set(["official", "custom"]);
    const requests = new Set();
    async function request(path, payload, signal) {
      if (state.destroyed) throw Object.assign(new Error("请求已取消"), {name: "AbortError"});
      const controller = new AbortController(), abort = () => controller.abort(); requests.add(controller);
      signal?.addEventListener("abort", abort, {once: true}); if (signal?.aborted) abort();
      let deadline, onAbort;
      const operation = (async () => {
      const response = await fetch(apiBase + path, { credentials: "same-origin", signal: controller.signal, ...(payload === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }) });
      let data; try { data = await response.json(); } catch { throw new Error(`本地服务返回无法读取的内容（HTTP ${response.status}）。`); }
      if (!response.ok) throw new Error(`${data.error || `请求失败（HTTP ${response.status}）`}${data.detail ? `\n${objectText(data.detail)}` : ""}`);
      return data;
      })();
      const timeout = new Promise((_resolve, reject) => { deadline = setTimeout(() => { reject(new Error(payload === undefined ? "读取本地服务超时，请稍后重试。" : "请求超时，结果待核对；服务可能仍在处理。请先刷新状态，不会自动重发。")); controller.abort(); }, payload !== undefined && path.endsWith("/purge") ? 600000 : path.endsWith("/rebind") ? 180000 : path.startsWith("/api/search?") || path.endsWith("/models") ? 60000 : payload === undefined ? 15000 : 30000); });
      const cancelled = new Promise((_resolve, reject) => { onAbort = () => reject(Object.assign(new Error("请求已取消"), {name: "AbortError"})); controller.signal.addEventListener("abort", onAbort, {once: true}); if (controller.signal.aborted) onAbort(); });
      try { return await Promise.race([operation, timeout, cancelled]); }
      finally { clearTimeout(deadline); signal?.removeEventListener("abort", abort); controller.signal.removeEventListener("abort", onAbort); requests.delete(controller); }
    }
    function lazyContent(details, render) { let rendered = false; const update = () => { if (details.open && !rendered) { rendered = true; render(); } }; details.addEventListener("toggle", update); update(); }
    function cancelSearch() { state.searchSequence++; state.searchController?.abort(); state.searchController = null; state.searchBusy = false; }
    target.replaceChildren(); target.classList.add("library-panel");
    const header = node("div", "lp-heading"), headerText = node("div"); headerText.append(node("p", "lp-eyebrow", "LOCAL KNOWLEDGE LIBRARIES"), node("h2", "", "资料库管理"), node("p", "lp-description", "绑定真实目录，构建可追溯的案例、方法步骤与代码对象。"));
    const refreshButton = button("刷新状态 ↻", "lp-button lp-button-subtle"), libraryFilters = node("div", "lp-library-filters"), libraryTabs = node("div", "lp-library-tabs"), currentTab = button("当前资料库", "lp-library-tab lp-tab-current"), deletedTab = button("已删除资料库", "lp-library-tab lp-tab-deleted"), evaluationTab = button("测试集", "lp-library-tab lp-tab-evaluation");
    libraryTabs.setAttribute("role", "tablist"); libraryTabs.setAttribute("aria-label", "资料库分栏");
    const viewTabs = [[currentTab, "current"], [deletedTab, "deleted"], [evaluationTab, "evaluation"]];
    viewTabs.forEach(([tab]) => tab.setAttribute("role", "tab")); libraryTabs.append(...viewTabs.map(([tab]) => tab));
    const cleanupButton = button("清理未完成向量索引", "lp-button lp-cleanup-vectors"), cleanupEntry = node("div", "lp-cleanup-entry");
    cleanupEntry.append(cleanupButton, node("span", "lp-help", "全局 · 当前及历史索引代次"));
    libraryFilters.append(refreshButton); header.append(headerText, libraryFilters); target.append(header);
    const message = node("div", "lp-message"); message.hidden = true; message.setAttribute("role", "status"); message.setAttribute("aria-live", "polite"); target.append(message);
    function say(text, error = false) { message.textContent = text; message.hidden = !text; message.classList.toggle("lp-error", error); }
    const embeddingControl = node("section", "lp-embedding-control"), embeddingIdentity = node("div"), embeddingLabel = node("strong", "lp-embedding-status", "QMD embedding：未启用"), embeddingHint = node("p", "lp-help lp-embedding-hint", "默认关闭。原生与 QMD 关键词检索无需 embedding 权重。"), embeddingConfigure = button("配置 embedding", "lp-button lp-embedding-configure");
    embeddingIdentity.append(embeddingLabel, embeddingHint); embeddingControl.append(embeddingIdentity, embeddingConfigure);
    const globalSettings = node("details", "lp-global-settings"); globalSettings.append(node("summary", "", "索引设置"), embeddingControl, cleanupEntry);

    const create = node("details", "lp-create"), createSummary = node("summary", "", "＋ 新增资料库"); create.append(createSummary);
    const createForm = node("form", "lp-create-form"), createFields = node("div", "lp-form-grid");
    const nameInput = input("text", "例如：传热与流动案例库"); nameInput.required = true;
    const typeInput = select([["", "请选择资料库类型"], ...Object.entries(typeLabels)]); typeInput.required = true;
    const versionInput = input("text", "例如：COMSOL 6.3 / 项目文档版本");
    createFields.append(field("资料库名称", nameInput), field("资料库类型", typeInput), field("资料版本（可选）", versionInput)); createForm.append(createFields, node("p", "lp-help", "按资料实际来源选择官方或自定义；目录路径不决定归属。新增库默认关闭，启用后才参与检索。"));
    const rootsTitle = node("div", "lp-section-heading"); rootsTitle.append(node("h3", "", "绑定目录"), node("span", "lp-muted", "只读原文件；名称相同不代表内容相同")); createForm.append(rootsTitle);
    const rootRows = node("div", "lp-roots"); createForm.append(rootRows);
    function addRoot() {
      const row = node("div", "lp-root-row"), id = input("text", `root-${rootRows.children.length + 1}`), path = input("text", "D:\\comsol\\COMSOL63\\Multiphysics\\applications"), role = select([["", "请选择目录用途"], ["models", "模型目录"], ["docs", "教程 / 文档"], ["attachments", "关联附件"]]);
      path.required = true; role.required = true; path.spellcheck = false;
      const remove = button("移除", "lp-button lp-button-quiet"); remove.addEventListener("click", () => { if (rootRows.children.length > 1) row.remove(); else say("资料库至少需要一个有效目录。", true); });
      row.append(field("目录 ID", id), field("本机绝对路径", path), field("用途", role), remove); row.getValue = (index) => ({ id: id.value.trim() || `root-${index + 1}`, path: path.value.trim(), role: role.value }); rootRows.append(row);
    }
    addRoot(); const addRootButton = button("＋ 再绑定一个目录", "lp-button lp-button-quiet"); addRootButton.addEventListener("click", addRoot);
    const registerButton = node("button", "lp-button lp-button-primary", "保存资料库"); registerButton.type = "submit";
    const createActions = node("div", "lp-form-actions"); createActions.append(addRootButton, registerButton); createForm.append(createActions); create.append(createForm);
    const toolbar = node("div", "lp-global-toolbar"); toolbar.append(create, globalSettings); target.append(toolbar);
    const workspace = node("div", "lp-workspace"), browser = node("section", "lp-library-browser"), libraryList = node("nav", "lp-library-list"), content = node("div", "lp-content");
    const browserControls = node("div", "lp-browser-controls"), libraryQuery = input("search", "搜索名称、模块或目录…"), libraryStatus = select([["all", "全部状态"], ["enabled", "仅已启用"], ["disabled", "仅已关闭"]]), browserCount = node("p", "lp-browser-count"), treeActions = node("div", "lp-tree-actions"), expandTree = button("展开全部", "lp-button lp-button-quiet lp-tree-expand"), collapseTree = button("折叠全部", "lp-button lp-button-quiet lp-tree-collapse");
    libraryQuery.classList.add("lp-library-filter"); libraryStatus.classList.add("lp-library-status-filter"); libraryQuery.setAttribute("aria-label", "筛选资料库目录"); libraryStatus.setAttribute("aria-label", "按启停状态筛选资料库");
    const browserHeader = node("div", "lp-browser-heading"), directoryToggle = button("收起目录", "lp-button lp-button-quiet lp-directory-toggle"); directoryToggle.setAttribute("aria-expanded", "true"); browserHeader.append(node("strong", "", "资料库目录"), directoryToggle);
    function collapseDirectory(collapsed) { workspace.classList.toggle("lp-directory-hidden", collapsed); directoryToggle.setAttribute("aria-expanded", String(!collapsed)); directoryToggle.textContent = collapsed ? "选择资料库" : "收起目录"; }
    directoryToggle.addEventListener("click", () => collapseDirectory(!workspace.classList.contains("lp-directory-hidden")));
    treeActions.append(expandTree, collapseTree); browserControls.append(libraryQuery, libraryStatus, browserCount, treeActions); browser.append(browserHeader, browserControls, libraryList);
    libraryList.setAttribute("aria-label", "按层级选择资料库"); content.setAttribute("role", "region"); content.setAttribute("aria-label", "选中资料库详情"); content.tabIndex = -1;
    workspace.setAttribute("role", "tabpanel"); workspace.append(browser, content); target.append(libraryTabs, workspace);
    libraryQuery.addEventListener("input", () => { state.libraryQuery = libraryQuery.value; state.treeAutoExpand = true; renderLibraryTree(); });
    libraryStatus.addEventListener("change", () => { state.libraryStatus = libraryStatus.value; state.treeAutoExpand = true; renderLibraryTree(); });
    expandTree.addEventListener("click", () => { state.treeAutoExpand = false; state.expandedGroups = new Set(["official", "official-manuals", "official-models", "custom", "user-manuals", "user-models"]); renderLibraryTree(); });
    collapseTree.addEventListener("click", () => { state.treeAutoExpand = false; state.expandedGroups.clear(); renderLibraryTree(); });
    const selectedHeading = node("div", "lp-selected-heading"), evaluationContent = node("section", "lp-evaluation-summary"), bindings = node("div", "lp-bindings"), indexHeading = node("div", "lp-section-heading"); indexHeading.append(node("h3", "", "检索索引的实际状态"), node("span", "lp-muted", "解析完成与索引就绪分别报告"));
    evaluationContent.hidden = true; evaluationContent.setAttribute("aria-label", "测试集摘要与全局检索范围");
    const evaluationAccess = node("div", "lp-evaluation-access"), evaluationAccessToggle = button("开放测试集检索", "lp-button lp-evaluation-access-toggle"), evaluationAccessStatus = node("p", "lp-help lp-evaluation-access-status");
    evaluationAccessStatus.setAttribute("role", "status"); evaluationAccessStatus.setAttribute("aria-live", "polite"); evaluationAccess.append(evaluationAccessToggle, evaluationAccessStatus);
    evaluationAccessToggle.addEventListener("click", changeEvaluationAccess);
    const libraryActions = node("div", "lp-library-actions"), enableLibrary = button("临时关闭", "lp-button lp-library-enable"), deleteLibrary = button("删除资料库", "lp-button lp-library-delete"), restoreLibrary = button("恢复资料库", "lp-button lp-library-restore"), purgeLibrary = button("彻底删除", "lp-button lp-library-purge"), libraryActionHelp = node("p", "lp-help lp-library-action-help");
    libraryActions.append(enableLibrary, deleteLibrary, restoreLibrary, purgeLibrary, libraryActionHelp);
    libraryActionHelp.setAttribute("role", "status"); libraryActionHelp.setAttribute("aria-live", "polite");
    const liveProgress = node("section", "lp-live-progress"), progressHead = node("div", "lp-progress-head"), progressAnnouncement = node("div", "lp-progress-announcement"), progressStage = node("h3", "lp-progress-stage"), progressStatus = node("span", "lp-badge");
    const progressEyebrow = node("span", "lp-progress-eyebrow", "构建进度");
    liveProgress.setAttribute("aria-label", "资料库构建进度"); progressAnnouncement.setAttribute("role", "status"); progressAnnouncement.setAttribute("aria-live", "polite"); progressAnnouncement.setAttribute("aria-atomic", "true"); progressAnnouncement.append(progressStage, progressStatus); progressHead.append(progressEyebrow, progressAnnouncement);
    const progressBar = node("progress", "lp-progress lp-stage-progress"), progressAmount = node("p", "lp-progress-amount"), progressDetail = node("p", "lp-progress-detail"), progressFile = node("p", "lp-progress-file"), progressFilePath = node("code"), progressMetrics = node("div", "lp-progress-metrics"), progressFiles = node("span", "lp-progress-files"), progressCounts = node("span", "lp-progress-counts"), progressElapsed = node("span", "lp-progress-elapsed"), progressWarning = node("p", "lp-progress-warning"), progressIdentity = node("p", "lp-progress-identity");
    const cancelActions = node("div", "lp-progress-actions"), cancelButton = button("中断构建", "lp-button lp-cancel-build"), cancelHelp = node("p", "lp-progress-cancel-help"); cancelActions.hidden = true; cancelButton.hidden = true; cancelButton.title = "仅中断当前构建任务"; cancelButton.addEventListener("click", cancelBuild); cancelActions.append(cancelButton, cancelHelp);
    progressBar.setAttribute("aria-label", "当前阶段进度"); progressFile.append(node("span", "", "当前文件"), progressFilePath); progressMetrics.append(progressFiles, progressCounts, progressElapsed); liveProgress.append(progressHead, progressBar, progressAmount, progressDetail, progressFile, progressMetrics, progressWarning, cancelActions, progressIdentity);
    const indexCoverage = node("p", "lp-help"), indexes = node("div", "lp-indexes"); content.append(selectedHeading, evaluationContent, libraryActions, liveProgress, bindings, indexHeading, indexCoverage, indexes);
    const buildDetails = node("details", "lp-build"); buildDetails.append(node("summary", "", "构建 / 更新当前资料库"));
    const buildForm = node("form", "lp-build-form");
    const casesSection = node("section", "lp-case-selection"), caseSummary = node("p", "lp-case-summary"), caseRoots = node("p", "lp-help lp-case-roots"), caseDetails = node("details", "lp-case-files-details"), caseFiles = node("ul", "lp-case-files"), reloadModels = button("重新查找", "lp-button lp-button-quiet lp-find-models");
    casesSection.append(node("h4", "", "构建范围 · 当前资料库全部模型"), caseSummary, caseRoots);
    caseDetails.append(node("summary", "", "查看全部模型文件清单"), caseFiles);
    casesSection.append(reloadModels, caseDetails); buildForm.append(casesSection, node("p", "lp-help", "构建当前登记目录中的全部资料，需要其他范围请新增资料库。默认只构建原生检索；关键词与向量按需勾选。Agent 注释保持关闭。"));
    const extractionSection = node("section", "lp-extraction"), extractionMode = select([["offline", "离线解析（默认，无需 COMSOL）"], ["deep", "COMSOL 深度 API 增强"]]), extractionHelp = node("p", "lp-help lp-extraction-help");
    extractionMode.classList.add("lp-extraction-select"); extractionMode.value = "offline"; extractionMode.setAttribute("aria-label", "模型解析方式");
    extractionSection.append(field("模型解析方式", extractionMode), extractionHelp); buildForm.append(extractionSection);
    let extractionLibraryId = null;
    function syncExtractionMode() {
      const library = selectedLibrary();
      if (extractionLibraryId !== library?.id) { extractionLibraryId = library?.id; extractionMode.value = "offline"; }
      extractionSection.hidden = !library || isManual(library) || deleted(library);
      extractionMode.disabled = !activeLibrary(library) || Boolean(state.libraryAction) || state.buildBusy || libraryPending(library);
      extractionHelp.textContent = extractionMode.value === "deep" ? "先离线读取，再通过 COMSOL API 补充配置；需要本机 COMSOL 或可复用的深度快照。不运行几何、网格或求解。" : "无需 COMSOL。读取文件中保存的属性、网格参数和选择编码；不验证模型有效性。";
    }
    extractionMode.addEventListener("change", syncExtractionMode);
    const modesFieldset = node("fieldset", "lp-modes"); modesFieldset.append(node("legend", "", "本次构建的索引模式"));
    const modeChoices = node("div", "lp-mode-choices"); modesFieldset.append(modeChoices); buildForm.append(modesFieldset);
    const buildButton = node("button", "lp-button lp-button-primary lp-build-submit", "开始构建"); buildButton.type = "submit"; buildForm.append(buildButton); buildDetails.append(buildForm); content.append(buildDetails);
    const jobsHeading = node("div", "lp-section-heading"); jobsHeading.append(node("h3", "", "构建结果与解析覆盖")); const jobsList = node("div", "lp-jobs"); content.append(jobsHeading, jobsList);
    const jobViews = new Map(), jobsEmpty = node("p", "lp-empty", "此资料库尚无构建任务记录。"), jobHistory = node("details", "lp-history lp-job-items"), jobHistorySummary = node("summary"); jobHistory.append(jobHistorySummary);
    const searchHeading = node("div", "lp-section-heading"); searchHeading.append(node("h3", "", "查看资料库对象"), node("span", "lp-muted", "实际检索结果；不会以示例填充空库")); content.append(searchHeading);
    const searchForm = node("form", "lp-search-form"), searchInput = input("search", "搜索案例、方法步骤或代码，例如：heat transfer"); searchInput.required = true; searchInput.setAttribute("aria-label", "检索当前资料库对象");
    const searchMode = select([["native", modeLabels.native], ["qmd_keyword", modeLabels.qmd_keyword], ["qmd_vector", modeLabels.qmd_vector]]); searchMode.setAttribute("aria-label", "资料库检索模式");
    const searchButton = node("button", "lp-button lp-button-subtle", "检索对象"); searchButton.type = "submit"; searchForm.append(searchInput, searchMode, searchButton); const searchStatus = node("p", "lp-help lp-search-status", "选择资料库、检索模式并输入问题，查看真实对象。"), results = node("div", "lp-results"); searchStatus.setAttribute("role", "status"); content.append(searchForm, searchStatus, results);
    const sourceDialog = node("dialog", "lp-source-dialog"), sourceHead = node("div", "lp-source-heading"), sourceTitle = node("h3", "", "对象证据"), closeSource = button("×", "lp-close"), sourceContent = node("div", "lp-source-content"); closeSource.setAttribute("aria-label", "关闭对象证据"); closeSource.addEventListener("click", () => sourceDialog.close()); sourceHead.append(sourceTitle, closeSource); sourceDialog.append(sourceHead, sourceContent); target.append(sourceDialog);
    sourceDialog.addEventListener("close", () => { state.sourceSequence++; state.sourceController?.abort(); state.sourceController = null; state.configurationSequence++; state.configurationController?.abort(); state.configurationController = null; });
    const embeddingDialog = node("dialog", "lp-embedding-dialog"), embeddingForm = node("form", "lp-embedding-form"), embeddingToggle = input("checkbox"), embeddingToggleLabel = node("label", "lp-checkbox lp-embedding-toggle"), embeddingPath = input("text", "例如 D:\\models\\embedding.gguf"), embeddingStatus = node("p", "lp-help lp-embedding-dialog-status"), embeddingActions = node("div", "lp-delete-confirm-actions"), embeddingCancel = button("取消", "lp-button lp-embedding-cancel"), embeddingSave = node("button", "lp-button lp-button-primary lp-embedding-save", "保存设置");
    embeddingToggle.classList.add("lp-embedding-enable"); embeddingPath.classList.add("lp-embedding-path"); embeddingPath.spellcheck = false; embeddingPath.setAttribute("aria-label", "本地 embedding GGUF 绝对路径"); embeddingDialog.setAttribute("aria-label", "QMD embedding 全局配置"); embeddingStatus.setAttribute("role", "status"); embeddingSave.type = "submit";
    embeddingToggleLabel.append(embeddingToggle, node("span", "", "手动启用 QMD embedding")); embeddingActions.append(embeddingCancel, embeddingSave);
    embeddingForm.append(node("h3", "", "QMD embedding · 全局控制"), node("p", "lp-help", "默认关闭，离线基础包无需携带 embedding 权重。原生与 QMD 关键词检索可直接使用。"), embeddingToggleLabel, field("本地 embedding GGUF 绝对路径", embeddingPath), node("p", "lp-help", "请自行准备本地 GGUF 文件；不会自动下载模型。启用设置后仍需按需选择向量构建或检索。关闭功能保留已有向量索引与模型文件。"), embeddingStatus, embeddingActions); embeddingDialog.append(embeddingForm); target.append(embeddingDialog);
    const embeddingAvailable = () => state.embeddingStatus === "ready" && state.embedding.enabled === true && state.embedding.configured === true && !state.embeddingSaving;
    function renderEmbeddingControl() {
      textIfChanged(embeddingLabel, `QMD embedding：${state.embedding.enabled === true && state.embeddingStatus === "ready" ? "已启用" : "未启用"}${state.embeddingStatus === "error" ? "（状态未确认）" : ""}`);
      textIfChanged(embeddingHint, state.embeddingStatus === "loading" ? "正在读取全局设置；原生与 QMD 关键词检索无需 embedding 权重。" : state.embeddingStatus === "error" ? `设置读取或保存未确认：${state.embeddingError}。向量功能保持停用，请刷新后核对；原生与 QMD 关键词仍可使用。` : state.embedding.enabled ? state.embedding.configured ? "已配置本地 GGUF；向量构建与检索仍需按需选择，不会自动运行。" : "已启用但本地 GGUF 未配置或不可用，请配置模型后使用向量功能。" : "默认关闭。先在 QMD 控制中手动启用 embedding；原生与 QMD 关键词检索无需权重。关闭不会删除已有向量索引。");
      embeddingConfigure.disabled = state.embeddingSaving || state.embeddingStatus === "loading" || Boolean(state.libraryAction) || Boolean(cleanupSession) || globalBuildPending();
    }
    function updateEmbeddingDraft() {
      embeddingToggle.disabled = state.embeddingSaving; embeddingPath.disabled = state.embeddingSaving || !embeddingToggle.checked; embeddingCancel.disabled = state.embeddingSaving; embeddingSave.disabled = state.embeddingSaving;
      embeddingSave.textContent = state.embeddingSaving ? "正在保存…" : embeddingToggle.checked ? "确认启用并保存" : "确认关闭并保存"; embeddingForm.setAttribute("aria-busy", String(state.embeddingSaving));
    }
    function syncEmbeddingPolicy() {
      const available = embeddingAvailable();
      modeChoices.querySelectorAll(".lp-vector-choice").forEach(label => { const check = label.querySelector("input"); check.disabled = !available; if (!available) { check.checked = false; state.modeChoices.set(check.value, false); } textIfChanged(label.querySelector(".lp-vector-help"), available ? "已在 QMD 控制中启用 embedding；CPU 构建可能耗时较长，向量仍需按需勾选。" : "先在 QMD 控制中手动启用 embedding；原生与关键词索引仍可构建。"); });
      indexes.querySelectorAll(".lp-vector-index-policy").forEach(note => textIfChanged(note, available ? "embedding 已手动启用；可用性仍以实际索引状态为准。" : "先在 QMD 控制中手动启用 embedding 后使用向量功能；已有向量索引保留。"));
      if (!available && vectorMode(searchMode.value)) { cancelSearch(); results.replaceChildren(); }
      syncSearchMode(); renderEmbeddingControl();
    }
    function validEmbeddingSettings(value) { return value && typeof value.enabled === "boolean" && typeof value.configured === "boolean" && typeof value.model_path === "string"; }
    async function readEmbeddingSettings() {
      if (state.destroyed || state.embeddingSaving) return;
      state.embeddingController?.abort(); const controller = new AbortController(), sequence = ++state.embeddingSequence; state.embeddingController = controller; state.embeddingStatus = "loading"; renderEmbeddingControl();
      try {
        const settings = await request("/api/retrieval/embedding-settings", undefined, controller.signal);
        if (state.destroyed || sequence !== state.embeddingSequence) return;
        if (!validEmbeddingSettings(settings)) throw new Error("服务未返回完整的 embedding 全局设置");
        state.embedding = settings; state.embeddingStatus = "ready"; state.embeddingError = "";
      } catch (error) {
        if (!state.destroyed && sequence === state.embeddingSequence && error.name !== "AbortError") { state.embedding = {...state.embedding, enabled: false}; state.embeddingStatus = "error"; state.embeddingError = error.message; }
      } finally { if (!state.destroyed && sequence === state.embeddingSequence) { state.embeddingController = null; syncEmbeddingPolicy(); } }
    }
    function closeEmbedding() { if (state.embeddingSaving) return; if (embeddingDialog.open) embeddingDialog.close(); if (!state.destroyed) embeddingConfigure.focus(); }
    embeddingConfigure.addEventListener("click", () => {
      if (embeddingConfigure.disabled || state.destroyed) return;
      embeddingToggle.checked = state.embeddingStatus === "ready" && state.embedding.enabled === true; embeddingPath.value = state.embedding.model_path || "";
      embeddingStatus.classList.remove("lp-error"); embeddingStatus.textContent = "勾选和填写路径仅修改待保存选项；点击确认后才会更改全局设置。"; updateEmbeddingDraft(); embeddingDialog.showModal(); embeddingToggle.focus();
    });
    embeddingToggle.addEventListener("change", () => { updateEmbeddingDraft(); embeddingStatus.classList.remove("lp-error"); embeddingStatus.textContent = embeddingToggle.checked ? "请填写本机 GGUF 文件的绝对路径，再确认启用；不会下载模型或自动构建向量。" : "确认关闭后停止向量构建与检索，已有向量索引和模型文件保留。"; });
    embeddingCancel.addEventListener("click", closeEmbedding); embeddingDialog.addEventListener("cancel", event => { event.preventDefault(); closeEmbedding(); });
    embeddingForm.addEventListener("submit", async event => {
      event.preventDefault(); if (state.embeddingSaving || state.destroyed || !embeddingDialog.open) return;
      const enabled = embeddingToggle.checked === true, modelPath = embeddingPath.value.trim();
      const absolute = /^(?:[a-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+[\\/]|\/(?!\/)|file:\/\/\/)/i.test(modelPath);
      if (enabled && (!absolute || !/\.gguf$/i.test(modelPath) || /[\0\r\n]/.test(modelPath))) { embeddingStatus.textContent = "请填写本机 embedding GGUF 文件的绝对路径（.gguf）。"; embeddingStatus.classList.add("lp-error"); return; }
      const sequence = ++state.embeddingSequence; state.embeddingController?.abort(); const controller = new AbortController(); state.embeddingController = controller; state.embeddingSaving = true; updateEmbeddingDraft(); renderEmbeddingControl();
      embeddingStatus.classList.remove("lp-error"); embeddingStatus.textContent = "正在保存全局设置，不会下载模型或运行 embedding。";
      try {
        const settings = await request("/api/retrieval/embedding-settings", enabled ? {enabled: true, model_path: modelPath} : {enabled: false}, controller.signal);
        if (state.destroyed || sequence !== state.embeddingSequence) return;
        if (!validEmbeddingSettings(settings) || settings.enabled !== enabled || enabled && settings.configured !== true) throw new Error("服务未确认所选 embedding 设置，请刷新核对");
        state.embedding = settings; state.embeddingStatus = "ready"; state.embeddingError = ""; state.embeddingSaving = false; updateEmbeddingDraft(); closeEmbedding(); syncEmbeddingPolicy();
        say(enabled ? "QMD embedding 已手动启用，已配置本地 GGUF。尚未运行向量构建或检索。" : "QMD embedding 已关闭。原生与关键词检索可继续使用，已有向量索引及模型文件保留。");
      } catch (error) {
        if (!state.destroyed && sequence === state.embeddingSequence && error.name !== "AbortError") { state.embedding = {...state.embedding, enabled: false}; state.embeddingStatus = "error"; state.embeddingError = error.message; embeddingStatus.textContent = `embedding 设置结果未确认：${error.message}。请刷新核对，不会自动重发。`; embeddingStatus.classList.add("lp-error"); }
      } finally { if (!state.destroyed && sequence === state.embeddingSequence) { state.embeddingController = null; state.embeddingSaving = false; updateEmbeddingDraft(); syncEmbeddingPolicy(); } }
    });
    const rebindDialog = node("dialog", "lp-rebind-dialog"), rebindForm = node("form", "lp-rebind-form"), rebindIdentity = node("p", "lp-help lp-rebind-identity"), rebindOldPath = node("code", "lp-rebind-old-path"), rebindPath = input("text", "填写迁移后的本机绝对目录"), rebindStatus = node("p", "lp-help lp-rebind-status"), rebindActions = node("div", "lp-delete-confirm-actions"), rebindCancel = button("取消", "lp-button lp-rebind-cancel"), rebindSubmit = node("button", "lp-button lp-button-primary lp-rebind-submit", "校验并关联");
    rebindDialog.setAttribute("aria-label", "重新关联资料目录"); rebindPath.setAttribute("aria-label", "新的资料目录"); rebindPath.required = true; rebindPath.spellcheck = false; rebindSubmit.type = "submit"; rebindStatus.setAttribute("role", "status");
    rebindActions.append(rebindCancel, rebindSubmit); rebindForm.append(node("h3", "", "重新关联目录"), rebindIdentity, node("p", "lp-help", "当前目录"), rebindOldPath, field("新的资料目录", rebindPath), node("p", "lp-help", "保持内部相对路径及文件内容不变。校验通过后更新目录关联，保留资料、引用与索引，不重新 embedding；不复制或移动文件。"), rebindStatus, rebindActions); rebindDialog.append(rebindForm); target.append(rebindDialog);
    let rebindTarget = null;
    const rebindBusy = () => state.libraryAction?.action === "rebind";
    function closeRebind() { if (rebindBusy()) return; rebindTarget = null; if (rebindDialog.open) rebindDialog.close(); }
    rebindCancel.addEventListener("click", closeRebind);
    rebindDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeRebind(); });
    rebindDialog.addEventListener("close", () => { rebindTarget = null; });
    function openRebind(library, root) {
      if (state.destroyed || deleted(library) || state.libraryAction || cleanupSession || state.buildBusy || libraryPending(library)) return;
      rebindTarget = {libraryId: library.id, rootId: root.id, name: library.name};
      rebindIdentity.textContent = `资料库「${library.name}」 · 目录 ${root.id}`; rebindOldPath.textContent = root.path; rebindPath.value = root.path;
      rebindStatus.textContent = "填写新目录后校验已入库文件。资料库的启用状态保持不变。"; rebindStatus.classList.remove("lp-error");
      rebindDialog.showModal(); rebindPath.focus();
    }
    rebindForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!rebindTarget || state.destroyed || state.libraryAction || state.buildBusy) return;
      const selected = {...rebindTarget}, library = state.libraries.find(item => item.id === selected.libraryId), path = rebindPath.value.trim();
      if (!library || deleted(library) || libraryPending(library)) { rebindStatus.textContent = "资料库已删除或正在构建，请先恢复资料库或等待构建结束。"; return; }
      if (!path || /[\0\r\n]/.test(path)) { rebindStatus.textContent = "请填写有效的新资料目录。"; return; }
      const controller = new AbortController(); state.libraryAction = {id: library.id, action: "rebind", controller};
      rebindPath.disabled = true; rebindCancel.disabled = true; rebindSubmit.disabled = true; rebindSubmit.textContent = "正在校验…"; rebindForm.setAttribute("aria-busy", "true");
      rebindStatus.classList.remove("lp-error"); rebindStatus.textContent = "正在逐文件校验内容，耗时取决于资料大小；不会重新构建索引。"; setDisabled();
      try {
        const updated = await request(`/api/libraries/${encodeURIComponent(library.id)}/rebind`, {root_id: selected.rootId, path}, controller.signal);
        if (state.destroyed) return;
        if (updated.id !== selected.libraryId || !list(updated.roots).some(root => root.id === selected.rootId)) throw new Error("服务未确认指定资料库的目录关联，请刷新状态核对。");
        state.libraries = state.libraries.map(item => item.id === selected.libraryId ? {...item, ...updated} : item);
        const discovery = state.modelSelections.get(selected.libraryId);
        if (discovery) { discovery.sequence++; discovery.controller?.abort(); state.modelSelections.delete(selected.libraryId); }
        state.libraryAction = null; rebindTarget = null; rebindDialog.close(); renderLibraries();
        if (!activeLibrary(updated) && !isManual(updated)) void discoverModels(updated.id, {force: true});
        const checked = count(updated.verified_evidence);
        say(`资料库「${selected.name}」的目录 ${selected.rootId} 已重新关联。${checked === null ? "" : `已校验 ${checked} 个源文件。`}${checked === 0 ? "此目录尚无已入库源文件可供内容校验。" : ""}原资料、引用及索引保留，未重新 embedding。`);
      } catch (error) {
        if (!state.destroyed && error.name !== "AbortError") { rebindStatus.textContent = `重新关联未确认：${error.message}`; rebindStatus.classList.add("lp-error"); }
      } finally {
        if (state.libraryAction?.controller === controller) state.libraryAction = null;
        if (!state.destroyed) { rebindPath.disabled = false; rebindCancel.disabled = false; rebindSubmit.disabled = false; rebindSubmit.textContent = "校验并关联"; rebindForm.setAttribute("aria-busy", "false"); setDisabled(); }
      }
    });
    const deleteDialog = node("dialog", "lp-delete-dialog"), deleteTitle = node("h3", "", "删除资料库？"), deleteMessage = node("p", "lp-delete-message"), deleteConfirmActions = node("div", "lp-delete-confirm-actions"), cancelDelete = button("取消", "lp-button lp-delete-cancel"), confirmDelete = button("确认删除", "lp-button lp-library-delete lp-delete-confirm");
    deleteDialog.setAttribute("aria-label", "确认可恢复删除资料库"); cancelDelete.autofocus = true;
    deleteConfirmActions.append(cancelDelete, confirmDelete); deleteDialog.append(deleteTitle, deleteMessage, deleteConfirmActions); target.append(deleteDialog);
    let deleteConfirmation = null;
    function finishDeleteConfirmation(accepted, confirmation = deleteConfirmation) {
      if (!confirmation || confirmation !== deleteConfirmation) return;
      deleteConfirmation = null; if (deleteDialog.open) deleteDialog.close();
      confirmation.resolve(accepted === true && !state.destroyed); if (!state.destroyed) setDisabled();
    }
    function requestDeleteConfirmation(library, action = "delete") {
      if (deleteConfirmation) return deleteConfirmation.id === library.id && deleteConfirmation.action === action ? deleteConfirmation.promise : Promise.resolve(false);
      const text = action === "purge" ? `彻底删除资料库「${library.name}」？此操作不可恢复。将清理该库已登记的提取资料、可归属缓存及索引，历史引用将失效。原始 MPH 模型和手册文件保留，其他资料库中的同源副本不受影响。` : `删除资料库「${library.name}」？这是可恢复删除：原文件、索引与历史引用均保留，不做物理删除。可在“已删除资料库”分栏恢复；恢复后仍需手动启用。`;
      const confirmation = {id: library.id, action, usingDialog: typeof options.confirm !== "function"};
      confirmation.promise = new Promise(resolve => { confirmation.resolve = resolve; }); deleteConfirmation = confirmation; setDisabled();
      if (confirmation.usingDialog) {
        deleteMessage.textContent = text; deleteTitle.textContent = action === "purge" ? "彻底删除资料库？" : "删除资料库？"; confirmDelete.textContent = action === "purge" ? "确认彻底删除" : "确认删除";
        deleteDialog.setAttribute("aria-label", action === "purge" ? "确认不可恢复地彻底删除资料库" : "确认可恢复删除资料库");
        try { deleteDialog.showModal(); cancelDelete.focus(); }
        catch { finishDeleteConfirmation(false, confirmation); say("无法打开删除确认，请重试。", true); }
      } else Promise.resolve().then(() => options.confirm(text)).then(accepted => finishDeleteConfirmation(accepted, confirmation), () => finishDeleteConfirmation(false, confirmation));
      return confirmation.promise;
    }
    cancelDelete.addEventListener("click", () => finishDeleteConfirmation(false));
    confirmDelete.addEventListener("click", () => finishDeleteConfirmation(true));
    deleteDialog.addEventListener("cancel", event => { event.preventDefault(); finishDeleteConfirmation(false); });
    deleteDialog.addEventListener("close", () => { if (!deleteDialog.open && deleteConfirmation?.usingDialog) finishDeleteConfirmation(false); });

    const cleanupDialog = node("dialog", "lp-cleanup-dialog"), cleanupSummary = node("p", "lp-cleanup-summary"), cleanupStatus = node("p", "lp-help lp-cleanup-status"), cleanupActions = node("div", "lp-delete-confirm-actions"), cleanupCancel = button("取消", "lp-button lp-cleanup-cancel"), cleanupRetry = button("重新检查", "lp-button lp-cleanup-retry"), cleanupConfirm = button("确认清理", "lp-button lp-library-delete lp-cleanup-confirm");
    cleanupDialog.setAttribute("aria-label", "清理未完成向量索引（全局）"); cleanupStatus.setAttribute("role", "status"); cleanupStatus.setAttribute("aria-live", "polite");
    cleanupActions.append(cleanupCancel, cleanupRetry, cleanupConfirm);
    cleanupDialog.append(node("h3", "", "清理未完成向量索引"), node("p", "lp-cleanup-scope", "全局范围：所有资料库的当前及历史索引代次，不受所选资料库限制。"), node("p", "lp-help", "仅清理本次构建未完成、尚未发布为可用索引的向量文件，不会清理其他历史索引或解析产物。保留原生索引、关键词索引、已发布 / 已就绪向量索引、原始模型与手册、已入库内容和 GGUF 模型。"), cleanupSummary, cleanupStatus, cleanupActions); target.append(cleanupDialog);
    let cleanupSession = null;
    const globalBuildPending = () => state.buildBusy || state.jobs.some(pending) || state.indexes.building === true || indexEntries(state.indexes).some(([, index]) => [index.state, index.status].includes("building"));
    function syncCleanupControls() {
      const building = globalBuildPending(), session = cleanupSession, cleaning = session?.phase === "cleaning";
      cleanupButton.disabled = state.refreshBusy || Boolean(state.libraryAction) || Boolean(deleteConfirmation) || building || Boolean(session);
      cleanupButton.title = building ? "有资料库正在构建，请中断并等待全部构建任务结束后再清理。" : "预览全局当前及历史代次中的未完成向量文件后确认清理";
      if (building && session?.preview && !session.invalidated) { session.invalidated = true; cleanupStatus.textContent = "检测到构建任务，预览已失效。请等待全部构建结束后重新检查。"; }
      cleanupConfirm.disabled = !session || session.phase !== "ready" || session.invalidated || building || session.preview?.can_clean !== true || session.preview?.building === true;
      cleanupRetry.disabled = !session || ["preview", "cleaning"].includes(session.phase) || building;
      cleanupCancel.disabled = cleaning; cleanupConfirm.textContent = cleaning ? "正在清理…" : "确认清理"; cleanupDialog.setAttribute("aria-busy", String(session?.phase === "preview" || cleaning));
    }
    function closeCleanup(force = false) {
      if (cleanupSession?.phase === "cleaning" && !force) return;
      const session = cleanupSession; cleanupSession = null; session?.controller?.abort();
      if (cleanupDialog.open) cleanupDialog.close();
      if (!state.destroyed) { setDisabled(); cleanupButton.focus(); }
    }
    async function previewCleanup(session) {
      if (state.destroyed || cleanupSession !== session || session.phase === "cleaning" || globalBuildPending()) return;
      session.controller?.abort(); const controller = new AbortController(); session.controller = controller; session.preview = null; session.invalidated = false; session.phase = "preview";
      cleanupSummary.textContent = "正在只读检查未完成向量文件…"; cleanupStatus.textContent = "尚未删除任何文件；检查完成前无法确认清理。"; cleanupStatus.classList.remove("lp-error"); setDisabled();
      try {
        const preview = await request("/api/library-indexes/unfinished-vectors", undefined, controller.signal);
        if (state.destroyed || cleanupSession !== session || session.controller !== controller) return;
        if (!Number.isSafeInteger(preview.file_count) || preview.file_count < 0 || !Number.isSafeInteger(preview.generation_count) || preview.generation_count < 0 || count(preview.bytes) === null || typeof preview.can_clean !== "boolean" || typeof preview.building !== "boolean") throw new Error("服务未返回完整的清理预览，请重新检查。");
        if (preview.can_clean && (!preview.file_count || !preview.generation_count || typeof preview.preview_token !== "string" || !preview.preview_token.trim())) throw new Error("服务未返回有效的清理凭据，请重新检查。");
        session.preview = preview; session.phase = "ready";
        cleanupSummary.textContent = `可清理 ${preview.file_count} 个文件，${byteText(preview.bytes)}，涉及 ${preview.generation_count} 个索引代次。`;
        const notes = list(preview.warnings).filter(item => typeof item === "string" && item.trim());
        cleanupStatus.textContent = [preview.building ? "服务报告有构建正在进行，请等待结束后重新检查。" : !preview.file_count ? "没有可安全清理的未完成向量文件。" : preview.can_clean ? "确认后仅清理此预览中的未完成向量文件。" : "服务尚未允许清理，请刷新状态并重新检查。", count(preview.skipped_count) > 0 ? `另有 ${preview.skipped_count} 项保留，未纳入清理。` : "", ...notes].filter(Boolean).join("\n");
      } catch (error) {
        if (!state.destroyed && cleanupSession === session && session.controller === controller && error.name !== "AbortError") { session.phase = "error"; cleanupSummary.textContent = "清理预览未完成。"; cleanupStatus.textContent = `尚未清理：${error.message}`; cleanupStatus.classList.add("lp-error"); }
      } finally { if (!state.destroyed && cleanupSession === session && session.controller === controller) { session.controller = null; setDisabled(); } }
    }
    cleanupButton.addEventListener("click", async () => {
      if (cleanupButton.disabled || cleanupSession || state.destroyed) return;
      const session = {phase: "idle", preview: null, controller: null, invalidated: false}; cleanupSession = session;
      try { cleanupDialog.showModal(); cleanupCancel.focus(); }
      catch { closeCleanup(); say("无法打开清理确认，请重试。", true); return; }
      await previewCleanup(session);
    });
    cleanupCancel.addEventListener("click", () => closeCleanup());
    cleanupDialog.addEventListener("cancel", event => { event.preventDefault(); closeCleanup(); });
    cleanupDialog.addEventListener("close", () => { if (cleanupSession) closeCleanup(true); });
    cleanupRetry.addEventListener("click", () => { if (!cleanupRetry.disabled && cleanupSession) return previewCleanup(cleanupSession); });
    cleanupConfirm.addEventListener("click", async () => {
      const session = cleanupSession;
      if (!session || cleanupConfirm.disabled || state.destroyed || globalBuildPending()) return;
      const token = session.preview.preview_token, controller = new AbortController(); session.phase = "cleaning"; session.preview = null; session.controller = controller;
      state.libraryAction = {id: null, action: "cleanup-vectors", controller}; cleanupStatus.classList.remove("lp-error"); cleanupStatus.textContent = "正在清理已确认的未完成向量文件，请等待服务报告结果。"; setDisabled();
      try {
        const result = await request("/api/library-indexes/unfinished-vectors", {confirmed: true, token}, controller.signal);
        if (state.destroyed || cleanupSession !== session || session.controller !== controller) return;
        if (result.status !== "cleaned" || result.cleaned !== true) throw new Error(`服务尚未确认清理完成，请刷新状态并重新检查。${list(result.warnings).filter(item => typeof item === "string").join("；")}`);
        state.libraryAction = null; session.phase = "done"; closeCleanup(true);
        const refreshed = await refresh(true); if (state.destroyed) return;
        say(`未完成向量索引已清理${count(result.file_count) === null ? "" : `：${result.file_count} 个文件${count(result.bytes) === null ? "" : `，${byteText(result.bytes)}`}`}。原生、关键词及已发布 / 已就绪向量索引、原文件、入库内容与 GGUF 模型保留。${list(result.warnings).filter(item => typeof item === "string" && item.trim()).map(item => `\n${item}`).join("")}${refreshed ? "" : "\n索引状态刷新未确认，请手动刷新核对。"}`, !refreshed);
      } catch (error) {
        if (!state.destroyed && cleanupSession === session && error.name !== "AbortError") { session.phase = "error"; cleanupStatus.textContent = `清理结果未确认：${error.message}\n请刷新状态并重新检查后再决定是否清理；不会自动重发。`; cleanupStatus.classList.add("lp-error"); say(cleanupStatus.textContent, true); }
      } finally {
        if (state.libraryAction?.controller === controller) state.libraryAction = null;
        if (!state.destroyed && cleanupSession === session) { session.controller = null; setDisabled(); }
      }
    });

    function selectedLibrary() { return visibleLibraries().find((item) => item.id === state.selected); }
    function deleted(library) { return Boolean(library?.deleted_at); }
    function activeLibrary(library) { return Boolean(library) && !deleted(library) && library.enabled !== false; }
    function libraryPending(library) { return Boolean(library) && state.jobs.some((job) => jobApplies(job, library.id) && pending(job)); }
    function libraryEnablePending(library) { return Boolean(library) && state.jobs.some((job) => job.kind !== "api_enrichment" && jobApplies(job, library.id) && pending(job)); }
    function visibleLibraries() { return state.libraryView === "evaluation" ? [] : state.libraries.filter((library) => deleted(library) === (state.libraryView === "deleted")); }
    function modelSelection(id) {
      if (!state.modelSelections.has(id)) state.modelSelections.set(id, { sequence: 0, status: "idle", files: [], hasMore: false, missing: [], incomplete: false, error: "", controller: null });
      return state.modelSelections.get(id);
    }
    function renderLibraryActions() {
      const library = selectedLibrary(), purge = state.libraryAction?.action === "purge" ? state.libraryAction : null, busy = Boolean(state.libraryAction) || Boolean(deleteConfirmation) || Boolean(cleanupSession) || state.buildBusy || libraryPending(library);
      libraryActions.hidden = state.libraryView === "evaluation" || !library && !purge; enableLibrary.hidden = !library || deleted(library); deleteLibrary.hidden = !library || deleted(library); restoreLibrary.hidden = !library || !deleted(library); purgeLibrary.hidden = !library || !deleted(library);
      textIfChanged(enableLibrary, library?.enabled === false ? "启用资料库" : "临时关闭"); textIfChanged(purgeLibrary, purge && purge.id === library?.id ? "正在彻底删除…" : "彻底删除");
      [enableLibrary, deleteLibrary, restoreLibrary, purgeLibrary].forEach((action) => { action.disabled = !library || busy; action.title = libraryPending(library) ? "请先中断正在进行的构建，待任务结束后再操作。" : ""; });
      enableLibrary.disabled = !library || Boolean(state.libraryAction) || Boolean(deleteConfirmation) || Boolean(cleanupSession) || state.buildBusy || libraryEnablePending(library);
      if (!libraryEnablePending(library)) enableLibrary.title = "";
      textIfChanged(libraryActionHelp, purge ? purgeProgressText(purge) : libraryPending(library) ? "当前有构建任务，请先中断并等待收尾后再更改资料库状态。" : deleted(library) ? "已删除资料库不参与检索。原文件、索引和历史引用保留；恢复后保持关闭，再手动启用。" : library?.enabled === false ? "当前已关闭，不参与检索；历史资料与索引保留。启用后可再次检索与构建。" : "关闭或可恢复删除后不参与检索；原文件、索引与历史引用保留。");
      if (!purge && libraryPending(library) && !libraryEnablePending(library)) textIfChanged(libraryActionHelp, `正在补充 API 资料，可正常启停检索；删除或重建需等待任务结束。当前${library?.enabled === false ? "已关闭，不参与检索" : "已启用"}。`);
      libraryActions.classList.toggle("lp-purging", Boolean(purge));
      if (purge) renderPurgeProgress(purge);
    }
    const purgeStageLabels = {planning: "制定清理计划", indexes: "清理可归属索引", files: "清理可归属资料文件", catalog: "更新资料目录", compacting: "整理资料目录数据库", finished: "服务清理结束"};
    const purgePhaseLabels = {jobs: "核对构建任务", revisions: "核对资料版本", artifacts: "核对提取文件与缓存", evidence: "核对来源证据", validation: "校验清理范围"};
    const purgeUnits = {...units, generations: "个索引代次", directories: "个目录"};
    function purgePhase(progress) {
      if (!progress || progress.status === "idle") return "等待服务报告清理阶段";
      if (progress.status === "completed") return "服务报告清理结束，等待最终响应确认";
      if (progress.status === "failed") return "服务报告清理失败，结果待确认";
      const stage = purgeStageLabels[progress.stage] || "等待服务报告清理阶段", phase = purgePhaseLabels[progress.phase];
      return phase ? `${stage} · ${phase}` : stage;
    }
    function purgeProgressText(action) {
      const progress = action.progress; let phase = purgePhase(progress);
      if (progress?.status === "failed" && typeof progress.error === "string" && progress.error) phase += `：${progress.error}`;
      if (progress?.status === "running" && count(progress.completed) !== null) {
        const unit = purgeUnits[progress.unit] || "项";
        phase += progress.total > 0 && count(progress.total) !== null && progress.completed <= progress.total ? `，已处理 ${progress.completed} / ${progress.total} ${unit}` : `，已处理 ${progress.completed} ${unit}`;
      }
      return `正在彻底删除资料库「${action.name}」：${phase}。原始 MPH 与手册文件保留。${action.progressError ? ` 进度读取暂不可用：${action.progressError}；删除结果仍待确认。` : ""}不会重复提交删除请求。`;
    }
    function renderPurgeProgress(action) {
      if (state.libraryView === "evaluation") { liveProgress.hidden = true; return; }
      const progress = action.progress, completed = count(progress?.completed), total = count(progress?.total), unit = purgeUnits[progress?.unit] || "项", determinate = total > 0 && completed !== null && completed <= total;
      liveProgress.hidden = false; liveProgress.setAttribute("aria-label", "资料库彻底删除进度"); liveProgress.setAttribute("data-operation", "purge"); liveProgress.setAttribute("data-status", "running"); liveProgress.setAttribute("data-stage", progress?.stage || "waiting"); liveProgress.setAttribute("data-phase", progress?.phase || ""); liveProgress.setAttribute("data-job-id", ""); liveProgress.setAttribute("aria-busy", "true");
      textIfChanged(progressEyebrow, "彻底删除进度"); textIfChanged(progressStage, purgePhase(progress)); textIfChanged(progressStatus, "正在彻底删除"); progressStatus.className = "lp-badge lp-warn";
      progressBar.hidden = false;
      if (determinate) { if (progressBar.max !== total) progressBar.max = total; if (!progressBar.hasAttribute("value") || progressBar.value !== completed) progressBar.value = completed; }
      else { progressBar.removeAttribute("value"); progressBar.removeAttribute("max"); }
      const amount = determinate ? `本阶段已处理 ${completed} / ${total} ${unit}` : total !== null ? completed === null ? `本阶段完成数待报告 · 总计 ${total} ${unit}` : `本阶段报告 ${completed} / ${total} ${unit} · 计数以服务为准` : completed !== null ? `本阶段已处理 ${completed} ${unit} · 总量未报告` : "本阶段总量未报告 · 正在核对清理范围";
      textIfChanged(progressAmount, amount); progressBar.setAttribute("aria-valuetext", `${purgePhase(progress)}，${amount}`);
      const detail = typeof progress?.detail === "string" && progress.detail ? progress.detail : `正在清理资料库「${action.name}」，请等待服务确认结果。`;
      textIfChanged(progressDetail, detail); progressDetail.hidden = false; progressFile.hidden = true; progressMetrics.hidden = false; progressFiles.hidden = true; progressCounts.hidden = true; cancelActions.hidden = true; cancelButton.hidden = true;
      const warning = [action.progressError ? `进度读取暂不可用：${action.progressError}；删除结果仍待确认。` : "", typeof progress?.error === "string" ? progress.error : ""].filter(Boolean).join("\n");
      progressWarning.hidden = !warning; textIfChanged(progressWarning, warning); progressIdentity.hidden = false; textIfChanged(progressIdentity, "原始 MPH 与手册文件保留。不会重复提交删除请求；最终结果以删除响应为准。");
      syncElapsedClock(null); renderElapsed();
    }
    function stopPurgeProgress(action) {
      clearTimeout(action?.progressTimer); if (action) { action.progressStopped = true; action.progressTimer = null; action.progressController?.abort(); action.progressController = null; }
    }
    function schedulePurgeProgress(action) {
      if (state.destroyed || state.libraryAction !== action || action.action !== "purge" || action.progressStopped) return;
      action.progressTimer = setTimeout(() => { action.progressTimer = null; void pollPurgeProgress(action); }, 1500);
    }
    async function pollPurgeProgress(action) {
      if (state.destroyed || state.libraryAction !== action || action.progressController || action.progressStopped) return;
      const controller = new AbortController(); action.progressController = controller;
      try {
        const progress = await request(`/api/libraries/${encodeURIComponent(action.id)}/purge-progress`, undefined, controller.signal);
        if (state.destroyed || state.libraryAction !== action || action.progressController !== controller) return;
        if (progress.library_id !== action.id || !["idle", "running", "completed", "failed"].includes(progress.status)) throw new Error("服务返回的清理进度与请求不一致");
        const previous = Date.parse(action.progress?.updated_at || ""), incoming = Date.parse(progress.updated_at || "");
        if (!Number.isFinite(previous) || !Number.isFinite(incoming) || incoming >= previous) action.progress = progress;
        action.progressError = ""; renderLibraryActions();
      } catch (error) {
        if (!state.destroyed && state.libraryAction === action && error.name !== "AbortError") { action.progressError = error.message; renderLibraryActions(); }
      } finally {
        if (action.progressController === controller) action.progressController = null;
        if (!state.destroyed && state.libraryAction === action) schedulePurgeProgress(action);
      }
    }
    function renderCaseSelection() {
      const library = selectedLibrary(); casesSection.hidden = !library || isManual(library) || deleted(library);
      if (casesSection.hidden) return;
      const draft = modelSelection(library.id);
      caseSummary.textContent = draft.status === "loading" ? "正在只读列出当前库的全部模型…" : draft.status === "error" ? `模型列表读取失败：${draft.error}。请检查绑定目录后重新查找，或新增资料库。` : draft.missing.length || draft.incomplete ? `已发现 ${draft.files.length} 个模型，但有绑定目录不可用或尚未完整列出。请检查目录后重新查找，或新增资料库。` : draft.hasMore ? `服务只返回 ${draft.files.length} 个模型，清单尚不完整。请重新查找后再构建。` : draft.status === "ready" && !draft.files.length ? "未发现模型。请检查绑定目录；需要其他范围请新增资料库。" : `已发现 ${draft.files.length} 个模型，将构建全部。`;
      caseSummary.classList.toggle("lp-error", draft.status === "error");
      if (!activeLibrary(library)) caseSummary.textContent = "资料库已关闭；启用后再读取模型清单。";
      const roots = list(library.roots).filter((root) => (root.role || "models") === "models");
      caseRoots.textContent = `目录来源：${roots.map((root) => `${root.id}：${root.path}`).join("；") || "模型目录由服务报告"}${draft.missing.length ? `。不可用目录：${draft.missing.map(root => typeof root === "string" ? root : root?.path || root?.root_id || "目录未报告").join("；")}` : ""}`;
      caseFiles.replaceChildren(); draft.files.forEach((file) => caseFiles.append(node("li", "", `${file.root_id || "目录"} · ${file.relative_path || file.path}`)));
      reloadModels.disabled = !activeLibrary(library) || draft.status === "loading" || Boolean(state.libraryAction);
      if (draft.status === "idle" && activeLibrary(library)) void discoverModels(library.id);
    }
    async function discoverModels(id, {force = false} = {}) {
      const library = state.libraries.find((item) => item.id === id);
      if (!library || deleted(library) || !activeLibrary(library) && !force || isManual(library) || state.destroyed) return;
      const draft = modelSelection(id); if (!force && draft.status !== "idle") return;
      draft.controller?.abort(); const controller = new AbortController(), sequence = ++draft.sequence;
      draft.controller = controller; draft.status = "loading"; draft.error = ""; if (state.selected === id) { renderCaseSelection(); setDisabled(); }
      try {
        const data = await request(`/api/libraries/${encodeURIComponent(id)}/models`, undefined, controller.signal);
        if (state.destroyed || sequence !== draft.sequence) return;
        if (data.library_id && data.library_id !== id) throw new Error("返回的资料库 ID 与请求不一致");
        const seen = new Set(); draft.files = list(data.files).filter((file) => file && typeof file.path === "string" && file.path.trim() && !seen.has(file.path) && seen.add(file.path));
        draft.hasMore = data.has_more === true; draft.missing = list(data.missing_roots); draft.incomplete = ["partial", "offline"].includes(data.status); draft.status = "ready";
      } catch (error) { if (error.name !== "AbortError" && !state.destroyed && sequence === draft.sequence) { draft.status = "error"; draft.error = error.message; } }
      finally { if (sequence === draft.sequence) { draft.controller = null; if (!state.destroyed && state.selected === id) { renderCaseSelection(); setDisabled(); } } }
    }
    async function changeLibrary(action) {
      const library = selectedLibrary(); if (!library || state.libraryAction || cleanupSession || state.buildBusy || (action === "enable" ? libraryEnablePending(library) : libraryPending(library))) return;
      if ((action === "purge" || action === "restore") && !deleted(library) || (action === "enable" || action === "delete") && deleted(library)) return;
      if (action === "delete" || action === "purge") {
        if (!await requestDeleteConfirmation(library, action) || state.destroyed || state.selected !== library.id || state.libraryAction || cleanupSession || state.buildBusy || libraryPending(library)) return;
        if (action === "purge" && !deleted(selectedLibrary())) return;
      }
      const controller = new AbortController(), operation = {id: library.id, name: library.name, action, controller, startedAt: Date.now()}; state.libraryAction = operation; state.refreshSequence++; state.refreshBusy = false;
      cancelSearch(); results.replaceChildren();
      const discovery = state.modelSelections.get(library.id);
      if (discovery?.controller) { discovery.sequence++; discovery.controller.abort(); discovery.controller = null; discovery.status = "idle"; }
      setDisabled();
      if (action === "purge") { schedulePurgeProgress(operation); liveProgress.scrollIntoView?.({behavior: "smooth", block: "nearest"}); }
      try {
        const updated = await request(`/api/libraries/${encodeURIComponent(library.id)}/${action}`, action === "enable" ? {enabled: library.enabled === false} : action === "purge" ? {confirmed: true} : {}, controller.signal);
        stopPurgeProgress(operation);
        if (state.destroyed) return;
        if (action === "purge") {
          if (updated.purged === false || updated.status !== "purged" && updated.purged !== true) throw new Error("服务尚未确认彻底删除完成，请刷新状态核对。");
          state.libraries = state.libraries.filter((item) => item.id !== library.id); state.modelSelections.delete(library.id); if (state.selected === library.id) state.selected = null;
        } else { const value = updated.library || updated; if (value.id === library.id) state.libraries = state.libraries.map((item) => item.id === library.id ? {...item, ...value} : item); }
        if (action !== "enable") await refresh(true);
        const warnings = action === "purge" ? list(updated.warnings).filter(item => typeof item === "string" && item.trim()) : [];
        say((action === "purge" ? "资料库已彻底删除，不可恢复；该库已登记的提取资料、可归属缓存及索引已清理，历史引用将失效。原始 MPH 与手册文件保留。" : action === "delete" ? "资料库已可恢复删除；原文件、索引与历史引用保留。可在“已删除资料库”分栏恢复。" : action === "restore" ? "资料库已恢复并保持关闭；请在“当前资料库”分栏手动启用。" : "资料库启用状态已保存。") + (warnings.length ? ` 提示：${warnings.join("；")}` : ""));
      } catch (error) { if (!state.destroyed && error.name !== "AbortError") say(action === "purge" ? `彻底删除结果未确认：${error.message}\n服务可能已处理部分内容，请先刷新状态核对；不会自动重发删除请求。` : `资料库状态未更改：${error.message}`, true); }
      finally { stopPurgeProgress(operation); if (state.libraryAction === operation) state.libraryAction = null; if (!state.destroyed) { renderLibraries(); setDisabled(); } }
    }
    function modeState(mode) { return indexEntries(state.indexes).find(([key]) => key === mode)?.[1] || {}; }
    function modeReady(mode) { return (!vectorMode(mode) || embeddingAvailable()) && modeState(mode).ready === true; }
    function setDisabled() { const library = selectedLibrary(), draft = library && !isManual(library) ? modelSelection(library.id) : null; const incomplete = draft && (draft.status !== "ready" || !draft.files.length || draft.hasMore || draft.missing.length || draft.incomplete); buildButton.disabled = !activeLibrary(library) || Boolean(state.libraryAction) || Boolean(cleanupSession) || state.buildBusy || libraryPending(library) || Boolean(incomplete); searchButton.disabled = !activeLibrary(library) || Boolean(state.libraryAction) || state.searchBusy || !modeReady(searchMode.value); registerButton.disabled = state.createBusy; refreshButton.disabled = state.refreshBusy || state.libraryView === "evaluation" && state.evaluationAccessSaving; createForm.setAttribute("aria-busy", String(state.createBusy)); buildForm.setAttribute("aria-busy", String(state.buildBusy)); registerButton.textContent = state.createBusy ? "正在保存…" : "保存资料库"; buildButton.textContent = state.buildBusy ? "正在提交…" : "开始构建"; buildButton.title = library && !activeLibrary(library) ? "请先恢复并启用资料库。" : incomplete ? "请先确认模型目录可读且已列出完整清单。" : ""; bindings.querySelectorAll(".lp-rebind-open").forEach(control => { control.disabled = !library || deleted(library) || Boolean(state.libraryAction) || Boolean(cleanupSession) || state.buildBusy || libraryPending(library); }); syncExtractionMode(); renderLibraryActions(); syncCleanupControls(); renderEmbeddingControl(); syncEvaluationAccess(); }
    function syncSearchMode() {
      const library = selectedLibrary();
      if (library && !activeLibrary(library)) { state.lifecycleSearchNotice = deleted(library) ? "资料库已删除，不参与检索；历史资料保留。恢复后请手动启用。" : "资料库已关闭，不参与检索；历史资料保留。请先启用资料库。"; searchStatus.textContent = state.lifecycleSearchNotice; searchStatus.classList.add("lp-search-error"); setDisabled(); return; }
      if (state.lifecycleSearchNotice && searchStatus.textContent === state.lifecycleSearchNotice) searchStatus.textContent = `已选择「${modeLabels[searchMode.value] || searchMode.value}」。输入问题后查询此模式的真实结果。`;
      state.lifecycleSearchNotice = null;
      Array.from(searchMode.options).forEach((option) => { option.disabled = vectorMode(option.value) && !embeddingAvailable(); option.textContent = `${modeLabels[option.value] || option.value}${option.disabled ? " · 先手动启用 embedding" : modeReady(option.value) ? "" : " · 未就绪"}`; });
      const ready = modeReady(searchMode.value), index = modeState(searchMode.value);
      if (!ready) { searchStatus.textContent = vectorMode(searchMode.value) && !embeddingAvailable() ? "先在 QMD 控制中手动启用 embedding 并配置本地 GGUF，再使用向量检索。原生与 QMD 关键词检索无需 embedding 权重。" : `所选模式「${modeLabels[searchMode.value] || searchMode.value}」尚未就绪：${index.error || index.detail || ({ not_built: "尚未构建", not_requested: "本次构建未请求此模式", stale: "索引或模型指纹已失效", failed: "索引构建失败" })[index.state || index.status] || "服务尚未报告可用索引"}。请先构建或检查状态；不会自动改用其他检索模式。`; state.modeBlocked = true; }
      else if (state.modeBlocked) { searchStatus.textContent = `已选择「${modeLabels[searchMode.value] || searchMode.value}」。输入问题后查询此模式的真实结果。`; state.modeBlocked = false; }
      searchStatus.classList.toggle("lp-search-error", !ready); setDisabled();
    }
    function coverage(value, container) {
      const entries = value && typeof value === "object" ? Object.entries(value) : [];
      if (!entries.length) { container.append(node("p", "lp-help", "服务尚未报告读取覆盖范围。")); return; }
      const concise = coverageText(value);
      if (concise) container.append(node("p", coverageHasGaps(value) ? "lp-warning lp-coverage-summary" : "lp-help lp-coverage-summary", concise));
      const details = node("details", "lp-job-items"), api = value.deep_api;
      const summary = api?.status === "preview_only" ? "模型仅含预览 · 查看缺失信息" : api?.node_count ? `已读 ${api.node_count} 个 API 节点 · ${countText(api.read_errors)} 项读取缺口 · 查看详情` : "查看提取范围与缺口";
      details.append(node("summary", "", summary));
      const grid = node("dl", "lp-coverage"); entries.forEach(([key, status]) => { const item = node("div"); item.append(node("dt", "", key), node("dd", "", objectText(status))); grid.append(item); }); details.append(grid); container.append(details);
    }
    function renderModes() {
      const entries = indexEntries(state.indexes); const provided = list(options.modes).map((key) => [key, {}]); const choices = entries.length ? entries : provided.length ? provided : [["native", {}], ["qmd_keyword", {}], ["qmd_vector", {}]];
      indexes.replaceChildren(); modeChoices.replaceChildren();
      const indexedLibraries = state.indexes.libraries;
      indexCoverage.textContent = state.indexes.generation ? `当前索引代次：${state.indexes.generation}。${state.selected && indexedLibraries && typeof indexedLibraries === "object" ? Object.prototype.hasOwnProperty.call(indexedLibraries, state.selected) ? "已包含当前资料库。" : "当前活跃索引尚未包含所选资料库。" : "以下是服务报告的全局索引状态。"}` : "以下是服务报告的全局索引状态；尚未报告活跃索引代次。";
      choices.forEach(([mode, index]) => {
        const card = node("section", "lp-index-card"), heading = node("div", "lp-index-card-heading"); const indexState = index.state || index.status; const ready = index.ready === true; const error = index.error || indexState === "failed";
        heading.append(node("h4", "", index.label || modeLabels[mode] || mode), node("span", `lp-badge${ready ? " lp-good" : error ? " lp-warn" : ""}`, ready ? "索引就绪" : error ? indexState === "stale" ? "已失效" : "失败" : ({ not_built: "尚未构建", not_requested: "此次未请求", building: "构建中", stale: "已失效", cleaned: "已清理", disabled: "未启用" })[indexState] || indexState || "未报告就绪"));
        card.append(heading, node("p", "lp-help", index.detail || index.error || (ready ? "已建立索引，查询结果显示实际后端与资料版本。" : indexState === "building" ? "正在构建，完成后才能使用此模式。" : "尚无此模式的可用索引。")));
        if (vectorMode(mode)) card.append(node("p", "lp-help lp-vector-index-policy", embeddingAvailable() ? "embedding 已手动启用；可用性仍以实际索引状态为准。" : "先在 QMD 控制中手动启用 embedding 后使用向量功能；已有向量索引保留。"));
        const metadata = Object.entries(index).filter(([key]) => !["label", "detail", "error"].includes(key)); if (metadata.length) { const details = node("details", "lp-index-details"); details.append(node("summary", "", "原始索引状态"), node("pre", "lp-raw", JSON.stringify(Object.fromEntries(metadata), null, 2))); card.append(details); }
        indexes.append(card);
        const label = node("label", "lp-checkbox"), check = input("checkbox"); check.value = mode; check.checked = state.modeChoices.has(mode) ? state.modeChoices.get(mode) : mode === "native";
        check.disabled = vectorMode(mode) && !embeddingAvailable(); if (check.disabled) { check.checked = false; state.modeChoices.set(mode, false); }
        check.addEventListener("change", () => { if (vectorMode(mode) && !embeddingAvailable()) check.checked = false; state.modeChoices.set(mode, check.checked); }); label.append(check, node("span", "", index.label || modeLabels[mode] || mode));
        if (vectorMode(mode)) { label.classList.add("lp-vector-choice"); label.append(node("small", "lp-vector-help", embeddingAvailable() ? "已在 QMD 控制中启用 embedding；CPU 构建可能耗时较长，向量仍需按需勾选。" : "先在 QMD 控制中手动启用 embedding；原生与关键词索引仍可构建。")); }
        modeChoices.append(label);
      });
      if (!entries.length) indexes.append(node("p", "lp-help", "服务尚未返回完整索引状态；以上未报告项不表示构建成功。"));
      syncSearchMode();
    }
    function filteredLibraries() {
      const terms = state.libraryQuery.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
      return visibleLibraries().filter(library => (state.libraryView === "deleted" || state.libraryStatus === "all" || (library.enabled !== false) === (state.libraryStatus === "enabled")) && terms.every(term =>
        [library.name, library.id, library.version, typeLabels[library.type], ...list(library.roots).map(root => root.path)].filter(Boolean).join(" ").toLocaleLowerCase().includes(term)));
    }
    function libraryPath(library) { return `${library.type.startsWith("official-") ? "官方资料" : "自定义资料"} / ${isManual(library) ? "手册" : "模型案例"}`; }
    function applyEvaluationSummary(data) {
      state.evaluationSets = list(data.sets); state.evaluationStatus = "ready";
      state.evaluationAccessKnown = typeof data.retrieval_enabled === "boolean"; state.evaluationRetrievalEnabled = data.retrieval_enabled === true;
      state.evaluationAccessToken = typeof data.access_token === "string" && data.access_token.trim() ? data.access_token : ""; state.evaluationAccessError = "";
      if (!state.evaluationSets.some(item => item.id === state.selectedEvaluation)) state.selectedEvaluation = state.evaluationSets[0]?.id || null;
    }
    function evaluationAccessText() { return state.evaluationAccessKnown ? state.evaluationRetrievalEnabled ? "隔离已解除" : "已隔离" : state.evaluationStatus === "loading" ? "正在读取状态" : "检索状态待核对"; }
    function syncEvaluationAccess() {
      evaluationAccessToggle.textContent = !state.evaluationAccessKnown && (state.evaluationStatus === "error" || state.evaluationAccessError || state.evaluationStatus === "ready") ? "刷新后更改检索范围" : state.evaluationRetrievalEnabled ? "恢复测试集隔离" : "开放测试集检索";
      evaluationAccessToggle.disabled = state.libraryView !== "evaluation" || state.evaluationStatus !== "ready" || !state.evaluationAccessKnown || !state.evaluationAccessToken || state.evaluationAccessSaving || state.refreshBusy;
      evaluationAccessToggle.setAttribute("aria-pressed", state.evaluationAccessKnown ? String(state.evaluationRetrievalEnabled) : "mixed"); evaluationAccess.setAttribute("aria-busy", String(state.evaluationAccessSaving));
      evaluationAccessStatus.textContent = state.evaluationAccessSaving ? "正在更新全部测试集的检索范围…" : state.evaluationAccessError || (state.evaluationStatus === "loading" ? "正在核对测试集检索状态。" : state.evaluationStatus !== "ready" || !state.evaluationAccessKnown || !state.evaluationAccessToken ? "检索状态未确认，暂不能更改；请刷新状态核对。" : `全部测试集：${evaluationAccessText()}。`);
      evaluationAccessStatus.classList.toggle("lp-error", Boolean(state.evaluationAccessError));
    }
    async function changeEvaluationAccess() {
      if (state.destroyed || evaluationAccessToggle.disabled) return;
      const enabled = !state.evaluationRetrievalEnabled, access_token = state.evaluationAccessToken;
      state.evaluationAccessSaving = true; state.evaluationAccessError = ""; setDisabled();
      try {
        const data = await request("/api/libraries/evaluation-sets/access", {enabled, access_token});
        if (state.destroyed) return;
        if (data.retrieval_enabled !== enabled || typeof data.access_token !== "string" || !data.access_token.trim()) throw new Error("服务尚未确认测试集检索范围，请刷新状态核对。");
        applyEvaluationSummary(data);
        if (state.libraryView === "evaluation") say(enabled ? "已开放全部测试集检索；仍遵循原资料库启用范围。" : "已恢复全部测试集隔离。");
      } catch (error) {
        if (!state.destroyed && error.name !== "AbortError") { state.evaluationAccessKnown = false; state.evaluationAccessToken = ""; state.evaluationAccessError = `检索范围更新未确认：${error.message} 请手动刷新核对，不会自动重发。`; }
      } finally {
        state.evaluationAccessSaving = false;
        if (!state.destroyed) { renderLibraries(); setDisabled(); }
      }
    }
    function renderEvaluationTree() {
      const terms = state.libraryQuery.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean), sets = state.evaluationSets.filter(item => terms.every(term =>
        [item.name, item.description, ...list(item.models).flatMap(model => [model.title, model.direction])].filter(Boolean).join(" ").toLocaleLowerCase().includes(term)));
      libraryList.replaceChildren();
      textIfChanged(browserCount, `${terms.length ? `匹配 ${sets.length} / ` : "共 "}${state.evaluationSets.length} 个测试集 · 摘要只读`);
      sets.forEach(item => {
        const card = button("", `lp-library-card lp-evaluation-card${item.id === state.selectedEvaluation ? " lp-current" : ""}`);
        card.setAttribute("aria-pressed", String(item.id === state.selectedEvaluation)); card.setAttribute("data-evaluation-set-id", item.id);
        card.append(node("strong", "", item.name), node("span", "lp-library-state", `${evaluationAccessText()} · 摘要只读`), node("span", "lp-library-count", `${count(item.model_count) ?? list(item.models).length} 个案例`));
        card.addEventListener("click", () => { state.selectedEvaluation = item.id; renderLibraries(); content.scrollTop = 0; collapseDirectory(true); }); libraryList.append(card);
      });
      if (!sets.length) libraryList.append(node("p", "lp-empty", state.evaluationStatus === "loading" ? "正在读取测试集…" : state.evaluationStatus === "error" ? "测试集摘要暂不可用，请刷新重试。" : terms.length ? "没有匹配的测试集。" : "尚未划分测试集。"));
    }
    function renderEvaluationSummary() {
      selectedHeading.replaceChildren(); evaluationContent.replaceChildren();
      const selected = state.evaluationSets.find(item => item.id === state.selectedEvaluation);
      selectedHeading.append(node("p", "lp-library-breadcrumb", "测试集 / 全局检索范围"), node("h3", "", selected?.name || "测试集"));
      const accessNotice = !state.evaluationAccessKnown ? "测试集默认隔离，当前检索状态待核对。" : state.evaluationRetrievalEnabled ? "测试集隔离已解除，可参与 Agent 检索。" : "测试集默认隔离，不参与 Agent 检索。";
      evaluationContent.append(node("p", "lp-evaluation-notice", `${accessNotice}仅可通过此处更改，不能由 Agent 启用。开放后仍遵循原资料库启用范围。`), evaluationAccess); syncEvaluationAccess();
      if (state.evaluationStatus === "error") { const error = node("p", "lp-help lp-error", `摘要读取失败：${state.evaluationError} 请刷新重试。`); error.setAttribute("role", "status"); evaluationContent.append(error); }
      if (!selected) { evaluationContent.append(node("p", "lp-empty", state.evaluationStatus === "loading" ? "正在读取案例摘要…" : "此处仅显示测试集名称、方向与案例摘要。")); return; }
      if (selected.description) evaluationContent.append(node("p", "lp-description", selected.description));
      const models = list(selected.models), heading = node("div", "lp-section-heading"), summaries = node("ul", "lp-evaluation-cases");
      heading.append(node("h3", "", `案例摘要（${count(selected.model_count) ?? models.length}）`), node("span", "lp-muted", "案例摘要只读"));
      models.forEach(model => { const row = node("li", "lp-evaluation-case"); row.append(node("span", "lp-evaluation-direction", model.direction || "未标注方向"), node("strong", "lp-evaluation-title", model.title || "未命名案例")); summaries.append(row); });
      evaluationContent.append(heading, summaries);
      if (!models.length) evaluationContent.append(node("p", "lp-empty", "此测试集尚无案例摘要。"));
    }
    function renderLibraryTree() {
      const evaluation = state.libraryView === "evaluation";
      libraryStatus.hidden = state.libraryView !== "current"; treeActions.hidden = evaluation; browserControls.classList.toggle("lp-evaluation-browser", evaluation);
      libraryQuery.placeholder = evaluation ? "筛选测试集、方向或案例…" : "搜索名称、模块或目录…";
      libraryQuery.setAttribute("aria-label", evaluation ? "筛选测试集摘要" : "筛选资料库目录"); libraryList.setAttribute("aria-label", evaluation ? "选择隔离测试集" : "按层级选择资料库");
      if (evaluation) { renderEvaluationTree(); return; }
      const scroll = libraryList.scrollTop, libraries = filteredLibraries(), total = visibleLibraries(), searching = Boolean(state.libraryQuery.trim()), narrowed = searching || state.libraryView !== "deleted" && state.libraryStatus !== "all";
      libraryList.replaceChildren();
      textIfChanged(browserCount, `${narrowed ? `匹配 ${libraries.length} / ` : "共 "}${total.length} 个库 · 已启用 ${total.filter(activeLibrary).length}`);
      const card = library => {
        const item = button("", `lp-library-card${library.id === state.selected ? " lp-current" : ""}${deleted(library) ? " lp-library-deleted" : library.enabled === false ? " lp-library-disabled" : ""}`);
        item.setAttribute("aria-pressed", String(library.id === state.selected)); item.setAttribute("data-library-id", library.id); item.title = `${library.name}\n${library.id}`;
        const shortName = library.type.startsWith("official-") ? library.name.replace(/^COMSOL\s+[\d.]+\s*(?:示例|官方文档)\s*·\s*/i, "").replace(/_Module$/i, "").replace(/_/g, " ") : library.name;
        item.append(node("strong", "", shortName), node("span", "lp-library-state", deleted(library) ? "已删除" : library.enabled === false ? "已关闭" : "已启用"), node("span", "lp-library-count", `${library.object_count ?? "未报告"} 个对象`));
        item.addEventListener("click", () => selectLibrary(library.id)); return item;
      };
      const group = (id, title, members, children) => {
        const section = node("details", "lp-library-group"), heading = node("summary", "lp-group-heading"), body = node("div", "lp-group-body");
        section.setAttribute("data-library-group", id); section.open = narrowed && state.treeAutoExpand !== false || state.expandedGroups.has(id);
        heading.append(node("span", "lp-group-name", title), node("span", "lp-group-count", `${members.length} 库 · ${members.filter(activeLibrary).length} 启用`)); section.append(heading, body);
        section.addEventListener("toggle", () => { if (!narrowed && libraryList.contains(section)) { if (section.open) state.expandedGroups.add(id); else state.expandedGroups.delete(id); } });
        if (members.length && state.libraryView !== "deleted") {
          const actions = node("div", "lp-group-actions");
          for (const enabled of [true, false]) {
            const action = button(`${enabled ? "启用" : "关闭"}${narrowed ? "匹配库" : "本组"}`, `lp-button lp-button-quiet lp-group-switch lp-group-${enabled ? "enable" : "disable"}`);
            const affected = members.filter(library => (library.enabled !== false) !== enabled);
            action.title = `${enabled ? "启用" : "关闭"}${affected.length} 个资料库，复用已有索引`;
            action.disabled = !affected.length || Boolean(state.libraryAction) || Boolean(cleanupSession) || state.buildBusy || members.some(libraryEnablePending);
            action.addEventListener("click", () => changeLibraryGroup(members, enabled)); actions.append(action);
          }
          body.append(actions);
        }
        if (children) children.forEach(child => body.append(child)); else members.slice().sort((a, b) => a.name.localeCompare(b.name, "zh-CN")).forEach(library => body.append(card(library)));
        return section;
      };
      const official = libraries.filter(library => ["official-manuals", "official-models"].includes(library.type)), custom = libraries.filter(library => !["official-manuals", "official-models"].includes(library.type));
      if (official.length) libraryList.append(group("official", "官方资料", official, [["official-manuals", "手册"], ["official-models", "模型案例"]].map(([type, name]) => {
        const members = official.filter(library => library.type === type); return members.length ? group(type, name, members) : null;
      }).filter(Boolean)));
      if (custom.length) libraryList.append(group("custom", "自定义资料", custom, [["user-manuals", "手册"], ["user-models", "模型案例"]].map(([type, name]) => {
        const members = custom.filter(library => library.type === type); return members.length ? group(type, name, members) : null;
      }).filter(Boolean)));
      if (!libraries.length) libraryList.append(node("p", "lp-empty", narrowed ? "没有匹配的资料库，请修改筛选条件。" : state.libraryView === "deleted" ? "暂无已删除资料库。" : "暂无资料库，可绑定目录新增。"));
      libraryList.scrollTop = scroll;
    }
    async function changeLibraryGroup(members, enabled) {
      if (state.libraryAction || cleanupSession || state.buildBusy || members.some(libraryEnablePending)) return;
      const affected = members.filter(library => !deleted(library) && (library.enabled !== false) !== enabled), ids = affected.map(library => library.id);
      if (!ids.length) return;
      const controller = new AbortController(), operation = {action: "enable-many", controller}; state.libraryAction = operation; state.refreshSequence++; state.refreshBusy = false;
      cancelSearch(); results.replaceChildren();
      state.sourceSequence++; state.sourceController?.abort(); state.configurationSequence++; state.configurationController?.abort(); if (sourceDialog.open) sourceDialog.close();
      affected.forEach(library => { const draft = state.modelSelections.get(library.id); if (draft?.controller) { draft.sequence++; draft.controller.abort(); draft.controller = null; draft.status = "idle"; } });
      renderLibraryTree(); setDisabled();
      try {
        const updated = await request("/api/libraries/enable", {library_ids: ids, enabled}, controller.signal);
        if (state.destroyed) return;
        const changes = new Map(list(updated.libraries).map(library => [library.id, library]));
        if (ids.some(id => !changes.has(id) || changes.get(id).enabled !== enabled)) throw new Error("服务尚未确认全部资料库状态，请刷新核对。");
        state.libraries = state.libraries.map(library => changes.has(library.id) ? {...library, ...changes.get(library.id)} : library);
        searchStatus.textContent = "资料库范围已更新，请重新检索。"; say(`已${enabled ? "启用" : "关闭"} ${ids.length} 个资料库。已有索引直接复用。`);
      } catch (error) { if (!state.destroyed && error.name !== "AbortError") say(`资料库状态更新未确认：${error.message}`, true); }
      finally { if (state.libraryAction === operation) state.libraryAction = null; if (!state.destroyed) { renderLibraries(); setDisabled(); } }
    }
    function renderLibraries() {
      const trash = state.libraryView === "deleted", evaluation = state.libraryView === "evaluation";
      viewTabs.forEach(([tab, view]) => { const selected = state.libraryView === view; tab.setAttribute("aria-selected", String(selected)); tab.tabIndex = selected ? 0 : -1; });
      workspace.setAttribute("aria-label", evaluation ? "隔离测试集" : trash ? "已删除资料库" : "当前资料库"); content.setAttribute("aria-label", evaluation ? "选中测试集摘要" : "选中资料库详情");
      toolbar.hidden = evaluation; evaluationContent.hidden = !evaluation; bindings.hidden = evaluation;
      [indexHeading, indexCoverage, indexes, buildDetails, jobsHeading, jobsList, searchHeading, searchForm, searchStatus, results].forEach((section) => { section.hidden = trash || evaluation; });
      renderLibraryTree();
      if (evaluation) { renderEvaluationSummary(); renderCaseSelection(); renderJobs(); setDisabled(); return; }
      const library = selectedLibrary(); selectedHeading.replaceChildren(); bindings.replaceChildren();
      if (!library) { selectedHeading.append(node("h3", "", "请选择资料库")); renderCaseSelection(); renderModes(); renderJobs(); setDisabled(); return; }
      selectedHeading.append(node("p", "lp-library-breadcrumb", libraryPath(library)), node("h3", "", library.name), node("p", "lp-description", `${deleted(library) ? "已删除" : library.enabled === false ? "已关闭" : "已启用"} · ${typeLabels[library.type] || library.type} · ${library.object_count ?? "未报告"} 个对象 · ${library.version || "版本未填写"}`));
      list(library.roots).forEach((root) => { const row = node("div", "lp-binding"), relocate = button("重新关联", "lp-button lp-button-quiet lp-rebind-open"); relocate.setAttribute("aria-label", `重新关联目录 ${root.id}`); relocate.disabled = deleted(library) || Boolean(state.libraryAction) || state.buildBusy || libraryPending(library); relocate.title = deleted(library) ? "请先恢复资料库。" : libraryPending(library) ? "请先中断并等待构建结束。" : "迁移后校验并更新此目录，不重建索引"; relocate.addEventListener("click", () => openRebind(library, root)); row.append(node("span", "lp-badge", `${root.id} / ${{ models: "模型", docs: "文档", attachments: "附件" }[root.role] || root.role}`), node("code", "", root.path), relocate); bindings.append(row); });
      if (library.generation) bindings.append(node("p", "lp-help lp-generation", `当前代次：${library.generation}`));
      if (library.coverage) coverage(library.coverage, bindings);
      renderCaseSelection(); renderModes(); renderJobs(); setDisabled();
    }
    function selectedJobs() { return state.selected ? state.jobs.filter((job) => jobApplies(job, state.selected)) : []; }
    function visibleJob() { const jobs = selectedJobs(); return jobs.find(pending) || jobs[0] || null; }
    function textIfChanged(element, value) { const text = String(value); if (element.textContent !== text) element.textContent = text; }
    function observeJob(incoming) {
      const index = state.jobs.findIndex((job) => job.id === incoming.id), current = index < 0 ? null : state.jobs[index], job = newerJob(current, incoming);
      if (job === incoming) state.observations.set(job.id, Date.now());
      if (index < 0) state.jobs.unshift(job); else state.jobs[index] = job;
      if (job.status === "cancel_requested" || terminal.has(job.status)) state.cancelErrors.delete(job.id);
      return job;
    }
    function elapsedSeconds(job) {
      const seconds = count(job.elapsed_seconds);
      if (seconds !== null) return seconds + (pending(job) ? Math.max(0, Date.now() - (state.observations.get(job.id) ?? Date.now())) / 1000 : 0);
      const start = Date.parse(job.started_at || ""), end = pending(job) ? Date.now() : Date.parse(job.finished_at || job.updated_at || "");
      return Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, end - start) / 1000 : null;
    }
    function renderElapsed() { const purge = state.libraryAction?.action === "purge" ? state.libraryAction : null, job = visibleJob(); if (purge) textIfChanged(progressElapsed, `已用时 ${elapsedText(Math.max(0, Date.now() - purge.startedAt) / 1000)}`); else if (job) textIfChanged(progressElapsed, `已用时 ${elapsedText(elapsedSeconds(job))}`); }
    function syncElapsedClock(job) {
      const active = state.libraryAction?.action === "purge" || pending(job);
      if (active && state.elapsedTimer === null && !state.destroyed) state.elapsedTimer = setInterval(renderElapsed, 1000);
      else if (!active && state.elapsedTimer !== null) { clearInterval(state.elapsedTimer); state.elapsedTimer = null; }
    }
    function renderProgress() {
      if (state.libraryView === "evaluation") { liveProgress.hidden = true; syncElapsedClock(null); return; }
      if (state.libraryAction?.action === "purge") { renderPurgeProgress(state.libraryAction); return; }
      const job = visibleJob(); syncElapsedClock(job);
      liveProgress.hidden = state.libraryView === "deleted"; liveProgress.setAttribute("aria-label", "资料库构建进度"); liveProgress.setAttribute("data-operation", "build"); liveProgress.removeAttribute("data-phase"); textIfChanged(progressEyebrow, "构建进度");
      liveProgress.setAttribute("data-status", job?.status || "idle"); liveProgress.setAttribute("data-stage", job?.stage || ""); liveProgress.setAttribute("data-job-id", job?.id || ""); liveProgress.setAttribute("aria-busy", String(pending(job)));
      progressBar.hidden = !job; progressMetrics.hidden = !job; progressFile.hidden = !job; progressDetail.hidden = true; progressWarning.hidden = true; progressIdentity.hidden = !job;
      cancelActions.hidden = !pending(job); cancelButton.hidden = !pending(job); cancelButton.disabled = true;
      if (!job) { textIfChanged(progressStage, state.selected ? "尚无构建任务" : "请选择资料库"); textIfChanged(progressStatus, "未开始"); progressStatus.className = "lp-badge"; textIfChanged(progressAmount, "提交构建后，阶段、当前文件与实际计数会在这里持续显示。"); return; }
      const cancelling = state.cancelRequests.has(job.id) || job.status === "cancel_requested";
      const scopeNote = job.scope === "all_libraries" ? `全局索引涉及 ${list(job.library_ids).length} 个资料库。` : "";
      cancelButton.disabled = cancelling || !canCancel(job); cancelButton.setAttribute("data-job-id", job.id);
      textIfChanged(cancelButton, cancelling ? "正在中断…" : !canCancel(job) && pending(job) ? "中断不可用" : state.cancelErrors.has(job.id) ? "重试中断" : "中断构建");
      textIfChanged(cancelHelp, scopeNote + (!canCancel(job) && pending(job) && !cancelling ? "此任务不支持中断，可继续查看进度。" : job.status === "cancel_requested" ? "已请求中断，等待当前步骤收尾。已保存结果保留；未完成向量不保证续建。" : "已保存结果保留；未完成向量不保证续建。"));
      const stage = stageProgress(job), counts = jobCounts(job), label = stageLabels[job.stage] || job.stage || "构建任务";
      textIfChanged(progressStage, label); textIfChanged(progressStatus, statusLabels[job.status] || job.status || "状态未报告"); progressStatus.className = `lp-badge${["completed", "succeeded"].includes(job.status) ? " lp-good" : ["partial", "failed", "interrupted", "cancelled"].includes(job.status) ? " lp-warn" : ""}`;
      progressBar.removeAttribute("value"); progressBar.removeAttribute("max");
      if (stage.determinate) { progressBar.max = stage.total; progressBar.value = stage.completed; }
      else if (!pending(job)) progressBar.hidden = true;
      const unit = units[stage.unit] || "项";
      const amount = stage.determinate ? `本阶段 ${stage.completed} / ${stage.total} ${unit}` : !pending(job) ? "任务已结束 · 阶段计数以服务记录为准" : stage.total !== null ? stage.completed === null ? `本阶段完成数待报告 · 总计 ${stage.total} ${unit}` : `本阶段报告 ${stage.completed} / ${stage.total} ${unit} · 计数需核对` : stage.completed !== null ? `本阶段已处理 ${stage.completed} ${unit} · 总量未报告` : "本阶段总量未报告 · 正在等待实际进度";
      textIfChanged(progressAmount, amount); progressBar.setAttribute("aria-valuetext", `${label}，${amount}`);
      if (stage.detail) { textIfChanged(progressDetail, stage.detail); progressDetail.hidden = false; }
      textIfChanged(progressFilePath, stage.currentFile || (pending(job) ? "服务尚未报告当前文件" : "此任务已结束"));
      textIfChanged(progressFiles, `文件已尝试 ${countText(job.completed)} / ${count(job.total) === null ? "总量待扫描" : job.total}`);
      textIfChanged(progressCounts, `成功 ${countText(counts.succeeded)} · 部分 ${countText(counts.partial)} · 失败 ${countText(counts.failed)}`); progressFiles.hidden = job.kind === "reindex"; progressCounts.hidden = job.kind === "reindex"; renderElapsed();
      const warnings = [];
      if (state.cancelErrors.has(job.id)) warnings.push(`中断请求未确认：${state.cancelErrors.get(job.id)}。可重试；任务是否结束以服务状态为准。`);
      if (state.pollErrors.has(job.id)) warnings.push(`状态连接暂时中断，保留上次进度并重试读取：${state.pollErrors.get(job.id)}。不会自动重跑构建。`);
      if (job.error) warnings.push(job.error);
      if (list(job.failed_indexes).length) warnings.push(`未就绪索引：${job.failed_indexes.map((mode) => modeLabels[mode] || mode).join("、")}`);
      if (job.status === "partial" && !job.error) warnings.push("部分文件或索引存在缺口，请展开下方构建结果核对；此任务未被标为全部成功。");
      if (warnings.length) { textIfChanged(progressWarning, warnings.join("\n")); progressWarning.hidden = false; }
      textIfChanged(progressIdentity, `${job.kind === "reindex" ? "全局索引任务" : "任务"} ${job.id} · 开始于 ${date(job.started_at)}`);
    }
    async function cancelBuild() {
      const selected = visibleJob();
      if (!canCancel(selected) || selected.status === "cancel_requested" || state.cancelRequests.has(selected.id) || state.destroyed) return;
      const id = selected.id, controller = new AbortController(); state.cancelRequests.set(id, controller); state.cancelErrors.delete(id); renderProgress();
      try {
        const response = await request(`/api/library-jobs/${encodeURIComponent(id)}/cancel`, {}, controller.signal);
        if (state.destroyed) return;
        if (response.id !== id) throw new Error("返回的任务 ID 与中断请求不一致");
        const job = observeJob(response);
        if (job.status !== "cancel_requested" && !terminal.has(job.status)) throw new Error("服务尚未确认中断请求");
        if (terminal.has(job.status)) await refresh(true);
      } catch (error) {
        if (error.name !== "AbortError" && !state.destroyed && pending(state.jobs.find((job) => job.id === id))) state.cancelErrors.set(id, error.message);
      } finally {
        state.cancelRequests.delete(id);
        if (!state.destroyed) { renderJobs(); setDisabled(); schedulePolls(); }
      }
    }
    function reconcileChildren(parent, children) {
      for (const child of [...parent.children]) if (!children.includes(child)) child.remove();
      children.forEach((child, index) => { if (parent.children[index] !== child) parent.insertBefore(child, parent.children[index] || null); });
    }
    function jobView(job) {
      let view = jobViews.get(job.id);
      if (!view) {
        const card = node("article", "lp-job"), heading = node("div", "lp-job-heading"), stage = node("strong"), status = node("span", "lp-badge"), amount = node("p", "lp-help"), error = node("p", "lp-job-error"), generation = node("p", "lp-help lp-generation"), readCoverage = node("p", "lp-help lp-job-coverage");
        card.setAttribute("data-job-id", job.id); heading.append(stage, status); card.append(heading, amount, error, readCoverage);
        const failures = node("details", "lp-failures"), failureSummary = node("summary"), failureBody = node("div"); failures.open = true; failures.append(failureSummary, failureBody);
        const items = node("details", "lp-job-items"), itemSummary = node("summary"), itemBody = node("div"); items.append(itemSummary, itemBody);
        const indexes = node("details", "lp-job-items lp-job-index-details"); indexes.append(node("summary", "", "此任务的索引构建结果"));
        view = {card, stage, status, amount, error, generation, readCoverage, failures, failureSummary, failureBody, items, itemSummary, itemBody, indexes, indexText: "", indexPre: null};
        const renderIndex = () => { if (!indexes.open || !view.indexText) return; if (!view.indexPre) { view.indexPre = node("pre", "lp-raw"); indexes.append(view.indexPre); } textIfChanged(view.indexPre, view.indexText); };
        indexes.addEventListener("toggle", renderIndex); view.renderIndex = renderIndex;
        card.append(failures, items, indexes, generation); jobViews.set(job.id, view);
      }
      textIfChanged(view.stage, stageLabels[job.stage] || job.stage || "构建任务"); textIfChanged(view.status, statusLabels[job.status] || job.status || "状态未报告");
      const badge = `lp-badge${job.status === "completed" ? " lp-good" : ["failed", "partial", "interrupted"].includes(job.status) ? " lp-warn" : ""}`; if (view.status.className !== badge) view.status.className = badge;
      const total = Number.isFinite(job.total) ? job.total : null, completed = Number.isFinite(job.completed) ? job.completed : null;
      textIfChanged(view.amount, `已处理 ${completed ?? "未报告"} / ${total ?? "扫描中"} 个文件 · ${date(job.started_at)}`);
      textIfChanged(view.error, job.error || ""); if (view.error.hidden !== !job.error) view.error.hidden = !job.error;
      const failed = list(job.failed), failedKey = JSON.stringify(failed); if (view.failures.hidden !== !failed.length) view.failures.hidden = !failed.length;
      if (failedKey !== view.failedKey) {
        view.failedKey = failedKey; textIfChanged(view.failureSummary, `${failed.length} 个失败文件`);
        view.failureBody.replaceChildren(...failed.map((failure) => { const item = node("div", "lp-failure"); item.append(node("code", "", failure.path || "未报告路径"), node("p", "", failure.error || "未报告原因")); return item; }));
      }
      const items = list(job.items), itemsKey = JSON.stringify(items); if (view.items.hidden !== !items.length) view.items.hidden = !items.length;
      const coverageItems = items.filter(item => coverageText(item.coverage)), gaps = coverageItems.filter(item => coverageHasGaps(item.coverage));
      view.readCoverage.hidden = !coverageItems.length;
      textIfChanged(view.readCoverage, coverageItems.length === 1 && !job.items_truncated ? coverageText(coverageItems[0].coverage) : coverageItems.length ? `模型解析覆盖：${count(job.items_total) ?? coverageItems.length} 个文件，${count(job.coverage_summary?.partial_files) ?? gaps.length} 个存在读取缺口。展开文件明细查看属性与选择编码；建库处理结束不代表完整解析或仿真验证。` : "");
      if (itemsKey !== view.itemsKey) {
        view.itemsKey = itemsKey; textIfChanged(view.itemSummary, job.items_truncated ? `最近 ${items.length} / 共 ${countText(job.items_total)} 个文件的对象数与解析覆盖 · 统计涵盖全部文件` : `查看 ${items.length} 个文件的对象数与解析覆盖`);
        view.itemBody.replaceChildren(...items.map((item) => { const entry = node("div", "lp-job-item"); entry.append(node("strong", "", item.path), node("p", "lp-help", `${statusLabels[item.status] || item.status} · ${item.objects ?? "未报告"} 个知识对象`)); coverage(item.coverage, entry); list(item.warnings).forEach((warning) => entry.append(node("p", "lp-warning", objectText(warning)))); return entry; }));
      }
      if (view.indexes.hidden !== !job.indexes) view.indexes.hidden = !job.indexes;
      // Omit large mappings before visiting them, including while closed. An
      // already open details/pre remains the same node when results change.
      view.indexText = job.indexes ? JSON.stringify(job.indexes, (key, value) => key === "mapping" || key === "representation" ? undefined : value, 2) : ""; view.renderIndex();
      textIfChanged(view.generation, `任务 ${job.id}${job.generation ? ` · 代次 ${job.generation}` : ""}`);
      return view.card;
    }
    function renderJobs() {
      renderProgress();
      const focused = document.activeElement, selected = selectedJobs(), active = selected.find(pending), jobs = (active ? [active, ...selected.filter((job) => job.id !== active.id)] : selected).slice(0, 8);
      const cards = jobs.map(jobView);
      for (const id of jobViews.keys()) if (!state.jobs.some((job) => job.id === id)) jobViews.delete(id);
      textIfChanged(jobHistorySummary, `历史构建记录（${Math.max(jobs.length - 1, 0)}）`);
      if (focused && cards.slice(1).some((card) => card.contains(focused))) jobHistory.open = true;
      reconcileChildren(jobHistory, [jobHistorySummary, ...cards.slice(1)]);
      reconcileChildren(jobsList, !cards.length ? [jobsEmpty] : cards.length > 1 ? [cards[0], jobHistory] : [cards[0]]);
      // A new active job can move the previously visible card into history.
      // Preserve keyboard focus if the browser clears it while moving a node.
      if (focused && target.contains(focused) && document.activeElement !== focused) focused.focus?.({preventScroll: true});
    }
    async function pollJob(id) {
      if (state.destroyed || state.polls.has(id)) return;
      const controller = new AbortController(); state.polls.set(id, controller);
      try { const response = await request(`/api/library-jobs/${encodeURIComponent(id)}?view=summary`, undefined, controller.signal); if (state.destroyed) return; if (response.id !== id) throw new Error("任务 ID 与请求不一致"); const job = observeJob(response); state.pollErrors.delete(id); renderJobs(); setDisabled(); if (terminal.has(job.status) && state.libraryAction?.action !== "purge") await refresh(true); }
      catch (error) { if (error.name !== "AbortError" && !state.destroyed) { state.pollErrors.set(id, error.message); renderProgress(); } }
      finally { state.polls.delete(id); }
    }
    function schedulePolls() { clearTimeout(state.pollTimer); state.pollTimer = null; if (state.destroyed) return; const jobs = state.jobs.filter(pending); if (jobs.length) state.pollTimer = setTimeout(() => { state.pollTimer = null; jobs.forEach((job) => { void pollJob(job.id); }); schedulePolls(); }, 2500); }
    async function refresh(quiet = false) {
      if (state.libraryView === "evaluation" && state.evaluationAccessSaving) { state.refreshBusy = false; setDisabled(); return false; }
      const evaluation = state.libraryView === "evaluation", sequence = ++state.refreshSequence, settingsRead = quiet || evaluation ? null : readEmbeddingSettings(); state.refreshBusy = true; setDisabled();
      if (evaluation) { state.evaluationStatus = "loading"; state.evaluationError = ""; state.evaluationAccessError = ""; renderLibraries(); }
      try {
        const data = await request(evaluation ? "/api/libraries/evaluation-sets" : state.libraryView === "deleted" ? "/api/libraries?view=summary&include_deleted=1" : "/api/libraries?view=summary"); if (sequence !== state.refreshSequence || state.destroyed) return;
        if (evaluation) {
          applyEvaluationSummary(data);
          renderLibraries(); if (!quiet) say("已读取测试集摘要与检索状态。"); return true;
        }
        state.libraries = list(Array.isArray(data) ? data : data.libraries);
        const existing = state.jobs, received = list(data.jobs); received.forEach(observeJob);
        state.jobs = [...received.map((job) => state.jobs.find((current) => current.id === job.id)), ...existing.filter((job) => pending(job) && !received.some((incoming) => incoming.id === job.id))]; state.indexes = data.indexes || {};
        const libraries = visibleLibraries();
        if (!libraries.some((library) => library.id === state.selected)) {
          const active = state.jobs.find((job) => pending(job) && libraries.some((library) => jobApplies(job, library.id)));
          state.selected = [active?.library_id, ...list(active?.library_ids)].find((id) => libraries.some((library) => library.id === id)) || libraries[0]?.id || null;
        }
        renderLibraries(); schedulePolls(); if (!quiet) say("已读取本地资料库与构建状态。"); return true;
      }
      catch (error) { if (!state.destroyed && sequence === state.refreshSequence) { if (evaluation) { state.evaluationStatus = "error"; state.evaluationError = error.message; state.evaluationAccessKnown = false; state.evaluationAccessToken = ""; renderLibraries(); } say(`${evaluation ? "测试集摘要" : "资料库状态"}读取失败：${error.message}`, true); } return false; }
      finally { if (sequence === state.refreshSequence) { state.refreshBusy = false; setDisabled(); } if (settingsRead) await settingsRead; }
    }
    function selectLibrary(id) { const library = visibleLibraries().find((library) => library.id === id); if (!library) return; state.selected = id; state.expandedGroups.add(library.type.startsWith("official-") ? "official" : "custom"); state.expandedGroups.add(library.type); cancelSearch(); results.replaceChildren(); searchStatus.textContent = "输入问题后检索当前资料库对象。"; renderLibraries(); content.scrollTop = 0; collapseDirectory(true); }
    function switchLibraryView(view) {
      if (state.destroyed || state.libraryView === view) return;
      const evaluationTransition = view === "evaluation" || state.libraryView === "evaluation";
      state.libraryView = view; state.selected = null; state.refreshSequence++; finishDeleteConfirmation(false); closeRebind();
      if (evaluationTransition) { state.libraryQuery = ""; libraryQuery.value = ""; say(""); }
      cancelSearch(); searchInput.value = ""; results.replaceChildren(); searchStatus.textContent = "输入问题后检索当前资料库对象。"; state.lifecycleSearchNotice = null;
      state.sourceSequence++; state.sourceController?.abort(); state.sourceController = null; state.configurationSequence++; state.configurationController?.abort(); state.configurationController = null;
      if (sourceDialog.open) sourceDialog.close(); sourceContent.replaceChildren();
      state.modelSelections.forEach((entry) => { entry.sequence++; entry.controller?.abort(); }); state.modelSelections.clear(); caseFiles.replaceChildren(); caseDetails.open = false;
      renderLibraries(); return refresh(true);
    }
    function configurationControls(source, sourceSequence) {
      const section = node("section", "lp-configuration"), heading = node("h4", "", "模型实际配置节点");
      section.append(heading, node("p", "lp-help", "按需读取本引用版本的归档节点，每次最多 10 项。节点属性与选择记录供核对；读取配置未执行求解或物理验证。"));
      const form = node("form", "lp-configuration-form"), path = input("text", "可选，例如 hf1 / solver / selection"), archive = select([["api", "API 配置归档"], ["saved", "保存记录归档"]]); path.spellcheck = false;
      const read = node("button", "lp-button lp-button-subtle", "读取配置节点"); read.type = "submit"; form.append(field("节点路径过滤（可选）", path), field("读取来源", archive), read);
      const result = node("div", "lp-configuration-result"); section.append(form, result);
      form.addEventListener("submit", async (event) => {
        event.preventDefault(); if (read.disabled || state.destroyed || sourceSequence !== state.sourceSequence || state.libraryView === "deleted") return;
        state.configurationController?.abort(); const controller = new AbortController(); state.configurationController = controller; const sequence = ++state.configurationSequence;
        read.disabled = true; read.textContent = "正在读取…"; form.setAttribute("aria-busy", "true"); const loading = node("p", "lp-help", "正在读取所选归档的匹配节点…"); loading.setAttribute("role", "status"); result.replaceChildren(loading);
        try {
          const params = new URLSearchParams({ source: archive.value, limit: "10" }); if (path.value.trim()) params.set("node_path", path.value.trim());
          const configuration = await request(`/api/library-objects/${encodeURIComponent(source.id)}/configuration?${params}`, undefined, controller.signal);
          if (state.destroyed || sequence !== state.configurationSequence || sourceSequence !== state.sourceSequence) return;
          result.replaceChildren(); const truncated = configuration.truncated === true ? "是" : configuration.truncated === false ? "否" : "未报告";
          result.append(node("p", "lp-configuration-counts", `匹配 ${configuration.total_matches ?? "未报告"} 项 · 返回 ${configuration.returned ?? "未报告"} 项 · 已截断：${truncated} · 来源：${configuration.source || "未报告"}`));
          result.append(node("p", "lp-help", `数值验证：${configuration.physical_validation === "not_run" ? "未运行" : configuration.physical_validation || "未报告"} · 有效启用状态：${configuration.effective_activation === "not_evaluated" ? "未评估" : configuration.effective_activation || "未报告"}`));
          if (configuration.truncated) result.append(node("p", "lp-warning", "本次结果仅为部分匹配节点。可填写更精确的节点路径后再次读取。"));
          result.append(node("pre", "lp-raw lp-configuration-nodes", JSON.stringify(configuration.nodes ?? [], null, 2)));
          const evidence = node("details", "lp-job-items"); evidence.append(node("summary", "", "配置归档的引用版本与来源证据"), node("pre", "lp-raw", JSON.stringify({ id: configuration.id, revision_id: configuration.revision_id, source: configuration.source, evidence: configuration.evidence }, null, 2))); result.append(evidence);
        } catch (error) {
          if (error.name !== "AbortError" && !state.destroyed && sequence === state.configurationSequence && sourceSequence === state.sourceSequence) result.replaceChildren(node("p", "lp-job-error", error.message));
        } finally {
          if (sequence === state.configurationSequence) { state.configurationController = null; read.disabled = false; read.textContent = "读取配置节点"; form.setAttribute("aria-busy", "false"); }
        }
      });
      return section;
    }
    async function showSource(id) {
      if (state.destroyed || !selectedLibrary() || state.libraryView === "deleted") return;
      if (typeof options.onSource === "function") { options.onSource(id); return; }
      state.configurationSequence++; state.configurationController?.abort(); state.configurationController = null;
      state.sourceController?.abort(); const controller = new AbortController(); state.sourceController = controller;
      const sequence = ++state.sourceSequence; sourceTitle.textContent = "正在读取知识对象…"; sourceContent.replaceChildren(node("p", "lp-help", "正在读取对象与原始出处。")); if (!sourceDialog.open) sourceDialog.showModal();
      try { const source = await request(`/api/sources/${encodeURIComponent(id)}`, undefined, controller.signal); if (sequence !== state.sourceSequence || state.destroyed) return; sourceTitle.textContent = source.title || "知识对象"; sourceContent.replaceChildren(); sourceContent.append(node("p", "lp-description", `${objectLabels[source.type] || source.type || source.kind || "资料"} · ${source.origin || "来源未报告"} · ${source.version || "版本未报告"}`));
        const annotation = source.data?.annotation;
        if (annotation) {
          sourceContent.append(node("p", "lp-help", "Agent 解释 · 依据有界资料生成，需结合原文与模型核对"));
          [annotation.summary, annotation.objective, annotation.purpose].filter(Boolean).forEach((text) => sourceContent.append(node("p", "lp-source-note", text)));
          [["适用条件", annotation.applicability], ["需要核对的输入", annotation.required_inputs], ["建议检查", annotation.checks], ["复用限制", annotation.reuse_cautions], ["资料缺口", annotation.limitations]].forEach(([title, values]) => { if (list(values).length) { const section = node("details", "lp-job-items"); section.append(node("summary", "", title)); const items = node("ul"); values.forEach((value) => items.append(node("li", "lp-source-note", value))); section.append(items); sourceContent.append(section); } });
        }
        if (source.coverage) coverage(source.coverage, sourceContent); if (source.type === "model_case") sourceContent.append(configurationControls(source, sequence));
        const original = node("details", "lp-job-items"); original.open = source.type === "implementation_snippet"; original.append(node("summary", "", "提取原文与实现片段")); lazyContent(original, () => original.append(node("pre", "lp-source-text", source.text || "此对象未提供正文。"))); sourceContent.append(original);
        const raw = node("details", "lp-job-items"); raw.append(node("summary", "", "完整对象、依赖与来源证据")); lazyContent(raw, () => raw.append(node("pre", "lp-raw", JSON.stringify(source, null, 2)))); sourceContent.append(raw);
      } catch (error) { if (sequence === state.sourceSequence && !state.destroyed && error.name !== "AbortError") sourceContent.replaceChildren(node("p", "lp-job-error", error.message)); }
      finally { if (state.sourceController === controller) state.sourceController = null; }
    }
    createForm.addEventListener("submit", async (event) => {
      event.preventDefault(); if (state.createBusy) return; state.createBusy = true; setDisabled(); say("");
      try { const roots = Array.from(rootRows.children).map((row, index) => row.getValue(index)); const library = await request("/api/libraries", { name: nameInput.value.trim(), type: typeInput.value, roots, version: versionInput.value.trim() }); if (state.destroyed) return; state.selected = library.id; await refresh(true); if (state.destroyed) return; create.open = false; say(`资料库「${library.name}」已保存。尚未开始解析或构建。`); }
      catch (error) { if (!state.destroyed && error.name !== "AbortError") say(error.message, true); } finally { if (!state.destroyed) { state.createBusy = false; setDisabled(); } }
    });
    buildForm.addEventListener("submit", async (event) => {
      event.preventDefault(); if (buildButton.disabled) return; const library = selectedLibrary();
      if (!isManual(library) && !["offline", "deep"].includes(extractionMode.value)) { say("请选择有效的模型解析方式。", true); return; }
      const deep = !isManual(library) && extractionMode.value === "deep";
      const modes = Array.from(modeChoices.querySelectorAll("input:checked")).map((check) => check.value); if (!modes.length) { say("请至少明确选择一种索引模式。", true); return; }
      if (modes.some(vectorMode) && !embeddingAvailable()) { say("先在 QMD 控制中手动启用 embedding 并配置本地 GGUF，再构建向量索引。", true); return; }
      state.buildBusy = true; setDisabled(); say("");
      try { const job = await request(`/api/libraries/${encodeURIComponent(library.id)}/build`, { deep, enrich: false, modes }); if (state.destroyed) return; observeJob(job); renderJobs(); schedulePolls(); liveProgress.scrollIntoView?.({ behavior: "smooth", block: "nearest" }); say(`构建任务已提交（${job.id}），${isManual(library) ? "文档解析" : deep ? "COMSOL 深度 API 增强" : "离线解析"}，范围为当前资料库登记目录中的全部资料。请以实际阶段和失败记录判断结果。`); }
      catch (error) { if (!state.destroyed && error.name !== "AbortError") say(error.message, true); } finally { if (!state.destroyed) { state.buildBusy = false; setDisabled(); } }
    });
    searchForm.addEventListener("submit", async (event) => {
      event.preventDefault(); const library = selectedLibrary(), query = searchInput.value.trim(), mode = searchMode.value; if (!activeLibrary(library) || !query || state.searchBusy || state.libraryAction) return; if (!modeReady(mode)) { syncSearchMode(); return; } const sequence = ++state.searchSequence, controller = new AbortController(); state.searchController = controller; state.searchBusy = true; setDisabled(); searchStatus.classList.remove("lp-search-error"); searchStatus.textContent = `正在通过「${modeLabels[mode] || mode}」检索当前资料库…`; results.replaceChildren();
      try { const params = new URLSearchParams({ library: library.id, mode, q: query, limit: "20" }); const data = await request(`/api/search?${params}`, undefined, controller.signal); if (sequence !== state.searchSequence || state.destroyed) return; const objects = list(data.results); searchStatus.textContent = objects.length ? `「${modeLabels[mode] || mode}」找到 ${objects.length} 个相关对象。命中下方保留实际检索后端和代次。` : `「${modeLabels[mode] || mode}」未找到匹配对象。可调整问题，或检查本库的构建状态。`;
        objects.forEach((object) => { const card = node("article", "lp-object"), open = button(object.title || object.id, "lp-object-title"); open.addEventListener("click", () => showSource(object.id)); const note = object.data?.annotation?.summary || object.data?.annotation?.purpose; card.append(open, node("p", "lp-help", `${objectLabels[object.type] || object.type || object.kind || "知识对象"} · 引用版本 ${object.id}`), node("p", "lp-retrieval", `实际检索后端：${object.retrieval?.backend || "服务未报告"} · 索引代次：${object.retrieval?.generation || "服务未报告"}`), node("p", "lp-object-excerpt", note ? `Agent 解释：${note}` : object.excerpt || (object.text ? object.text.slice(0, 260) : "打开查看对象内容与证据"))); if (object.coverage) coverage(object.coverage, card); results.append(card); });
      } catch (error) { if (sequence === state.searchSequence && !state.destroyed && error.name !== "AbortError") { searchStatus.textContent = `「${modeLabels[mode] || mode}」检索失败：${error.message}。本次未切换其他检索模式。`; searchStatus.classList.add("lp-search-error"); } }
      finally { if (sequence === state.searchSequence && !state.destroyed) { state.searchController = null; state.searchBusy = false; setDisabled(); } }
    });
    searchMode.addEventListener("change", () => { cancelSearch(); results.replaceChildren(); state.modeBlocked = true; syncSearchMode(); });
    viewTabs.forEach(([tab, view], index) => {
      tab.addEventListener("click", () => switchLibraryView(view));
      tab.addEventListener("keydown", (event) => { if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return; event.preventDefault(); const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? viewTabs.length - 1 : (index + (event.key === "ArrowLeft" ? -1 : 1) + viewTabs.length) % viewTabs.length, [next, nextView] = viewTabs[nextIndex]; next.focus(); void switchLibraryView(nextView); });
    });
    enableLibrary.addEventListener("click", () => changeLibrary("enable"));
    deleteLibrary.addEventListener("click", () => changeLibrary("delete"));
    restoreLibrary.addEventListener("click", () => changeLibrary("restore"));
    purgeLibrary.addEventListener("click", () => changeLibrary("purge"));
    reloadModels.addEventListener("click", () => { const library = selectedLibrary(); if (!reloadModels.disabled && library) return discoverModels(library.id, {force: true}); });
    refreshButton.addEventListener("click", () => refresh());
    const controller = { refresh, selectLibrary, destroy() { state.destroyed = true; finishDeleteConfirmation(false); closeCleanup(true); cancelSearch(); stopPurgeProgress(state.libraryAction); state.embeddingSequence++; state.embeddingController?.abort(); requests.forEach((request) => request.abort()); state.sourceSequence++; state.refreshSequence++; state.configurationSequence++; state.configurationController?.abort(); state.configurationController = null; state.libraryAction?.controller.abort(); state.modelSelections.forEach((entry) => { entry.sequence++; entry.controller?.abort(); }); clearTimeout(state.pollTimer); clearInterval(state.elapsedTimer); state.elapsedTimer = null; state.polls.forEach((abort) => abort.abort()); state.cancelRequests.forEach((abort) => abort.abort()); if (sourceDialog.open) sourceDialog.close(); if (rebindDialog.open) rebindDialog.close(); if (embeddingDialog.open) embeddingDialog.close(); target.replaceChildren(); target.classList.remove("library-panel"); instances.delete(target); } };
    instances.set(target, controller); controller.ready = Promise.all([refresh(true), readEmbeddingSettings()]); return controller;
  }
  window.SimAdvisorLibraryPanel = { mount };
})();
