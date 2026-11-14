/* Local-only UI. All persisted facts, answers, plans and sources come from the API. */
"use strict";

const $ = (id) => document.getElementById(id);
const state = { status: null, session: null, model: null, modelPath: "", operation: null, tab: "intent", planView: "flow", workspaceView: "plans", returnSessionId: null, draftAnswers: {}, dirty: false, indexBusy: false, indexResult: null, sourceRequest: 0, statusRequest: 0, sessionRequest: 0, questionGroups: [], overviewNodes: null, agent: null, showDeletedSessions: false, deletedSessions: [], deletedSessionIds: new Set(), restoredSessionIds: new Set(), trashRequest: 0, trashLoading: false, trashError: "", sessionDeletion: null };
Object.assign(state, { managedSessions: [], managementLoaded: false, managementLoading: false, managementError: "", managementRequest: 0, managementRead: null, managementTotal: 0, managementNextOffset: null, sessionPurge: null });
let agentPanel = null;
let statusRead = null;
const apiControllers = new Set();
let sourceRead = null, sourceSearch = null, searchRequest = 0, sourceTechnicalData = null;
const caseGuide = globalThis.SimAdvisorCaseGuide?.mount(document.getElementById("source-case-guide"), {
  call: async (name, args, signal) => (await api("/api/tools/call", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name, arguments: args}), signal})).result,
  openSource: id => showSource(id),
});
let versionsState = null;
let examplesRenderKey = null, sessionsRenderKey = null, systemRenderKey = null, corpusRenderKey = null;
let managementRenderKey = null;
const statusLabels = { intake: "任务已建立 · 待规划", needs_input: "待补充意图", needs_replan: "待 Agent 规划", draft: "方案草案 · 待确认", confirmed: "当前版本已确认" };
const kindLabels = { manual: "手册资料", case: "模型案例", code: "API / 实现片段" };
function setIfChanged(node, property, value) { if (node[property] !== value) node[property] = value; }
function textIfChanged(node, value) { setIfChanged(node, "textContent", String(value ?? "")); }

function el(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
function lazyContent(details, render) { let rendered = false; const update = () => { if (details.open && !rendered) { rendered = true; render(); } }; details.addEventListener("toggle", update); update(); }
function list(value) { return Array.isArray(value) ? value : []; }
function valueText(value) {
  if (value === undefined || value === null || value === "") return "未知";
  if (Array.isArray(value)) return value.map(valueText).join("、") || "未发现";
  if (typeof value === "object") return value.label || value.title || value.name || value.tag || value.message || JSON.stringify(value);
  return String(value);
}
function isNonBlank(value) { return value !== undefined && value !== null && String(value).trim() !== ""; }
function isAnswered(value) { return isNonBlank(value) && !/(?:待定|待确认|待確認|未知|不确定|不確定)|\b(?:undecided|unknown|tbd)\b/i.test(String(value)); }
function isGeometry(model) { return model?.asset_kind === "cad_geometry" || /^(?:step|stp)$/i.test(model?.geometry?.source_format || ""); }
function hasAgentPlan(session = state.session) { return Boolean(session?.plan && !["intake-only", "reference-baseline"].includes(session.plan.engine) && list(session.plan.steps).length); }
function isStale(session = state.session) { return Boolean(session?.plan_stale || session?.planning_incomplete || session?.status === "needs_replan"); }
function agentActionLabel() { return !hasAgentPlan() ? "Agent 规划方案" : isStale() ? "Agent 重新规划" : "Agent 深化方案"; }
function isQuestionResolved(question, value) { return isAnswered(value) && (!question.id.startsWith("decision_") || value === "accept"); }
function unresolvedQuestions(decisions = false) { return list(state.session?.questions).filter((question) => Boolean(question.id.startsWith("decision_")) === decisions && question.required && !isQuestionResolved(question, questionValue(question))); }
function hasMethodEvidenceGaps() { return [...list(state.session?.evidence_gaps), ...list(state.session?.plan?.evidence_gaps), ...list(state.session?.research_trace).flatMap((entry) => list(entry.evidence_gaps))].length > 0; }
function compactSentence(value, limit = 150) { const text = String(value || "").trim(); const first = text.match(/^[\s\S]*?[。！？\n]/)?.[0]?.trim() || text; return first.length > limit ? first.slice(0, limit) + "…" : first; }
function disclosure(title, className = "") { const details = el("details", `disclosure-section${className ? " " + className : ""}`); const summary = el("summary", "", title), content = el("div", "disclosure-content"); details.append(summary, content); return { details, summary, content }; }
function revealQuestion(questionId) {
  const index = list(state.session?.questions).findIndex((question) => question.id === questionId), input = $("answer-" + index);
  selectTab("intent");
  if (!input) { $("tab-intent").focus(); return; }
  for (let parent = input.parentElement; parent && parent !== $("view-intent"); parent = parent.parentElement) if (parent.tagName === "DETAILS") parent.open = true;
  input.closest(".question-field")?.scrollIntoView({ behavior: "smooth", block: "center" }); input.focus({ preventScroll: true });
}
function boundingBoxSummary(box, fallbackUnit) {
  const finiteTriple = (value) => Array.isArray(value) && value.length === 3 && value.every((item) => typeof item === "number" && Number.isFinite(item));
  let size = finiteTriple(box?.size) ? box.size : null;
  if (!size && finiteTriple(box?.min) && finiteTriple(box?.max)) size = box.max.map((value, index) => value - box.min[index]);
  if (!size || size.some((value) => value < 0)) return "已读取记录 · 详细数值见技术证据";
  const formatter = new Intl.NumberFormat("zh-CN", { maximumSignificantDigits: 4 });
  const unit = box.unit || fallbackUnit;
  return `约 ${size.map((value) => formatter.format(value)).join(" × ")} ${unit ? valueText(unit) : "（单位未确定）"}（X × Y × Z）`;
}
function safeURL(value) {
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? url.href : null; }
  catch { return null; }
}
function dateText(value) {
  if (!value) return "时间未记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !message;
}
function errorMessage(error) { return error instanceof Error ? error.message : String(error); }
async function api(path, options = {}) {
  const {signal, timeoutMs, ...settings} = options, controller = new AbortController();
  const limit = timeoutMs ?? (path === "/api/index/build" ? 600000 : /^\/api\/(models\/|sessions$)/.test(path) && settings.method === "POST" ? 120000 : settings.method === "POST" ? 30000 : path.startsWith("/api/search?") ? 60000 : 15000);
  const abort = () => controller.abort(); signal?.addEventListener("abort", abort, {once: true}); if (signal?.aborted) abort();
  apiControllers.add(controller); let deadline, onAbort;
  const operation = (async () => {
  const response = await fetch(path, { credentials: "same-origin", ...settings, signal: controller.signal });
  let body;
  try { body = await response.json(); }
  catch (error) { if (error.name === "AbortError") throw error; throw new Error(response.ok ? "本地服务返回了无法读取的内容。" : `本地服务请求失败（HTTP ${response.status}）。请检查服务日志。`); }
  if (!response.ok) {
    const detail = body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : "";
    const error = new Error(`${body.error || `请求失败（HTTP ${response.status}）`}${detail ? `\n${detail}` : ""}`);
    error.status = response.status;
    throw error;
  }
  return body;
  })();
  const timeout = new Promise((_resolve, reject) => { deadline = setTimeout(() => { reject(new Error(settings.method === "POST" ? "请求超时，结果待核对；本地服务可能仍在处理。请先核对状态，不会自动重发。" : "读取本地服务超时，请稍后重试。")); controller.abort(); }, limit); });
  const cancelled = new Promise((_resolve, reject) => { onAbort = () => reject(Object.assign(new Error("请求已取消"), {name: "AbortError"})); controller.signal.addEventListener("abort", onAbort, {once: true}); if (controller.signal.aborted) onAbort(); });
  try { return await Promise.race([operation, timeout, cancelled]); }
  finally { clearTimeout(deadline); signal?.removeEventListener("abort", abort); controller.signal.removeEventListener("abort", onAbort); apiControllers.delete(controller); }
}
function post(path, body) { return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }

function setBusy(operation, title, detail) {
  state.operation = operation;
  $("busy-overlay").hidden = !operation;
  if (operation) { $("busy-title").textContent = title; $("busy-detail").textContent = detail; }
  $("intake-form").setAttribute("aria-busy", String(Boolean(operation)));
  ["create-session", "inspect-model", "model-file", "new-session"].forEach((id) => { $(id).disabled = Boolean(operation); });
  syncConfirmation();
  syncRefine();
  agentPanel?.bindSession();
  $("build-index").disabled = Boolean(operation) || state.indexBusy || Boolean(state.status?.corpus?.building);
  renderSessions();
  syncVersionControls();
}
async function runOperation(operation, title, detail, action) {
  if (state.operation) return;
  notice("");
  setBusy(operation, title, detail);
  try { await action(); }
  catch (error) {
    if (error.status === 409 && state.session && /版本已变化/.test(errorMessage(error))) { notice("方案已在别处更新（Agent、工具或另一个窗口）。页面上未保存的回答仍保留；请先「载入更新的方案」再操作，不要刷新浏览器。", true); agentPanel?.refresh?.(); }
    else if (error.name !== "AbortError") notice(errorMessage(error), true);
  }
  finally { setBusy(null); }
}

async function refreshStatus(quiet = false) {
  if (statusRead?.request === state.statusRequest) { if (!quiet) statusRead.quiet = false; return statusRead.promise; }
  statusRead?.controller.abort();
  const request = ++state.statusRequest, read = {request, quiet, controller: new AbortController(), promise: null}; statusRead = read;
  read.promise = (async () => {
  try {
    const result = await api("/api/status?view=summary", {signal: read.controller.signal});
    if (request !== state.statusRequest) return;
    state.status = result;
    textIfChanged($("server-status"), result.ready ? "本地服务已连接" : "本地服务已连接 · 准备中");
    setIfChanged($("server-dot"), "className", `status-dot ${result.ready ? "available" : ""}`); if ($("topbar-dot")) setIfChanged($("topbar-dot"), "className", `status-dot ${result.ready ? "available" : ""}`);
    renderExamples(); renderSessions(); renderSystem(); syncRefine();
  } catch (error) {
    if (request !== state.statusRequest) return;
    textIfChanged($("server-status"), "本地服务连接失败");
    setIfChanged($("server-dot"), "className", "status-dot failed"); if ($("topbar-dot")) setIfChanged($("topbar-dot"), "className", "status-dot failed");
    if (!state.status) {
      examplesRenderKey = sessionsRenderKey = systemRenderKey = null;
      $("example-list").replaceChildren(el("span", "muted", "服务连接后可读取案例"));
      $("session-list").replaceChildren(el("p", "side-empty", "暂时无法读取历史"));
      $("service-list").replaceChildren(el("p", "source-error", errorMessage(error)));
    }
    if (!read.quiet && error.name !== "AbortError") notice(`无法连接本地服务：${errorMessage(error)}`, true);
  }
  finally { if (statusRead === read) statusRead = null; }
  })();
  return read.promise;
}
function renderExamples() {
  const examples = list(state.status?.examples);
  const key = JSON.stringify(examples.map(({id, title, path, goal}) => [id, title, path, goal]));
  if (key === examplesRenderKey) return; examplesRenderKey = key;
  const container = $("example-list"); container.replaceChildren();
  if (!examples.length) { container.append(el("span", "muted", "暂无可用案例")); return; }
  examples.forEach((example) => {
    const button = el("button", "example-button", example.title || example.id); button.type = "button";
    button.title = example.path || example.goal || "载入此案例";
    button.addEventListener("click", () => {
      if (state.operation) return;
      $("goal").value = example.goal || ""; $("model-path").value = example.path || "";
      state.model = null; state.modelPath = ""; $("file-name").textContent = "或填写本机文件路径";
      renderModel(); $("goal").focus(); notice("案例目标与路径已填入。可先读取文件，再建立仿真任务。");
    });
    container.append(button);
  });
}
function renderSessions() {
  renderPlanManagement();
  const sessions = list(state.status?.sessions).filter(session => !state.deletedSessionIds.has(session.id) && !session.deleted_at).slice(0, 5);
  const key = JSON.stringify([Boolean(state.operation), Boolean(state.sessionDeletion), Boolean(state.sessionPurge), state.session?.id, sessions.map(({id, title, revision, status, updated_at}) => [id, title, revision, status, updated_at])]);
  if (key === sessionsRenderKey) return; sessionsRenderKey = key;
  const container = $("session-list"); container.replaceChildren();
  $("delete-current-session").disabled = !state.session || Boolean(state.operation) || Boolean(state.sessionDeletion) || Boolean(state.sessionPurge);
  if (!sessions.length) { container.append(el("p", "side-empty", "生成第一份方案后，可在方案管理中查看。")); return; }
  sessions.forEach((session) => {
    const row = el("div", "session-history-row"); row.dataset.sessionId = session.id;
    const primary = el("button", `session-item${session.id === state.session?.id ? " current" : ""}`);
    primary.type = "button"; primary.disabled = Boolean(state.operation) || Boolean(state.sessionDeletion) || Boolean(state.sessionPurge); primary.addEventListener("click", () => restoreSession(session.id));
    if (session.id === state.session?.id) primary.setAttribute("aria-current", "true");
    primary.append(el("span", "session-item-title", session.title || session.id));
    const meta = el("span", "session-item-meta"); meta.append(el("span", "", statusLabels[session.status] || session.status), el("span", "", dateText(session.updated_at))); primary.append(meta);
    primary.title = session.title || session.id;
    row.append(primary); container.append(row);
  });
}
function renderPlanManagement() {
  if (state.workspaceView !== "sessions") return;
  const deleted = state.showDeletedSessions, loading = deleted ? state.trashLoading : state.managementLoading, error = deleted ? state.trashError : state.managementError;
  const source = deleted ? state.deletedSessions.filter(session => !state.restoredSessionIds.has(session.id)) : state.managedSessions.filter(session => !state.deletedSessionIds.has(session.id));
  const query = $("managed-session-search").value.trim().toLocaleLowerCase(), sessions = source.filter(session => `${session.title || ""} ${session.id}`.toLocaleLowerCase().includes(query));
  const blocked = Boolean(state.operation || state.sessionDeletion || state.sessionPurge);
  const key = JSON.stringify([deleted, loading, error, query, blocked, source.length, state.managementLoaded, state.managementTotal, state.managementNextOffset, state.session?.id, sessions.map(({id, title, revision, status, updated_at, deleted_at}) => [id, title, revision, status, updated_at, deleted_at])]);
  if (key === managementRenderKey) return; managementRenderKey = key;
  [["sessions-active-tab", !deleted], ["toggle-deleted-sessions", deleted]].forEach(([id, selected]) => { const button = $(id); button.classList.toggle("active", selected); button.setAttribute("aria-selected", String(selected)); button.tabIndex = selected ? 0 : -1; button.disabled = blocked; });
  $("managed-sessions-panel").setAttribute("aria-labelledby", deleted ? "toggle-deleted-sessions" : "sessions-active-tab");
  textIfChanged($("managed-session-search-hint"), deleted ? "按名称或编号搜索已删除方案。" : "搜索已载入的方案；可继续载入更多。");
  textIfChanged($("managed-sessions-status"), loading ? "正在读取方案列表…" : deleted ? `已删除 ${source.length} 个方案${query ? ` · 匹配 ${sessions.length} 个` : ""}` : `已载入 ${source.length} / ${state.managementTotal} 个方案${query ? ` · 匹配 ${sessions.length} 个` : ""}`);
  textIfChanged($("managed-sessions-error"), error); setIfChanged($("managed-sessions-error"), "hidden", !error);
  $("refresh-managed-sessions").disabled = blocked || loading; $("management-new-session").disabled = blocked;
  $("load-more-sessions").hidden = deleted || state.managementNextOffset === null; $("load-more-sessions").disabled = blocked || loading;
  const container = $("managed-session-list"); container.replaceChildren();
  if (!sessions.length && !loading) container.append(el("p", "managed-sessions-empty", query ? "没有匹配的方案。" : deleted ? "暂无已删除方案。" : "暂无已保存方案。建立第一个仿真任务后，会保存在这里。"));
  sessions.forEach(session => {
    const row = el("article", "managed-session-row"), summary = el("div", "managed-session-summary"), title = el("h2", "", session.title || session.id), actions = el("div", "managed-session-actions"); row.dataset.sessionId = session.id;
    summary.append(title, el("p", "managed-session-meta", [deleted ? "已删除 · 可恢复" : statusLabels[session.status] || session.status, Number.isInteger(session.revision) ? `r${session.revision}` : "", `${deleted ? "删除于" : "更新于"} ${dateText(deleted ? session.deleted_at || session.updated_at : session.updated_at)}`, session.id === state.session?.id ? "当前工作方案" : ""].filter(Boolean).join(" · ")));
    const identifier = el("p", "managed-session-id", session.id); summary.append(identifier);
    const action = (label, className, handler) => { const button = el("button", `button ${className}`, label); button.type = "button"; button.disabled = blocked; button.setAttribute("aria-label", `${label}方案：${session.title || session.id}`); button.addEventListener("click", handler); actions.append(button); };
    if (deleted) { action("恢复", "button-subtle", () => restoreDeletedSession(session.id)); action("彻底删除", "session-current-delete", () => openSessionPurge(session.id)); }
    else { action("打开", "button-subtle", () => restoreSession(session.id)); action("删除", "button-ghost session-current-delete", () => openSessionDeletion(session.id)); }
    row.append(summary, actions); container.append(row);
  });
}
function invalidateManagedSessions() {
  state.managementRequest++; state.managementRead?.abort(); state.managementRead = null; state.managementLoading = false; state.managementLoaded = false;
}
async function refreshManagedSessions(append = false) {
  if (append && (state.managementLoading || state.managementNextOffset === null)) return;
  state.managementRead?.abort(); const controller = new AbortController(), request = ++state.managementRequest, offset = append ? state.managementNextOffset : 0;
  state.managementRead = controller; state.managementLoading = true; state.managementError = ""; renderPlanManagement();
  try {
    const result = await api(`/api/sessions?offset=${offset}&limit=100`, {signal: controller.signal}); if (request !== state.managementRequest) return;
    if (!Array.isArray(result.sessions) || !Number.isInteger(result.total) || !(result.next_offset === null || Number.isInteger(result.next_offset) && result.next_offset > offset)) throw new Error("服务返回的方案列表格式不完整，请刷新后重试。");
    const summaries = result.sessions.filter(session => !state.deletedSessionIds.has(session.id) && !session.deleted_at), rows = append ? [...state.managedSessions, ...summaries] : summaries;
    state.managedSessions = [...new Map(rows.map(session => [session.id, session])).values()]; state.managementTotal = result.total; state.managementNextOffset = result.next_offset; state.managementLoaded = true;
  } catch (error) { if (request === state.managementRequest && error.name !== "AbortError") state.managementError = `方案列表读取失败：${errorMessage(error)}`; }
  finally { if (request === state.managementRequest) { state.managementLoading = false; state.managementRead = null; renderPlanManagement(); } }
}
async function openPlanManagement() {
  if (state.operation || state.sessionDeletion || state.sessionPurge) return;
  setWorkspaceView("sessions", {focus: true});
  if (state.showDeletedSessions) await refreshDeletedSessions(); else await refreshManagedSessions();
}
async function selectSessionManagementTab(deleted) {
  if (state.operation || state.sessionDeletion || state.sessionPurge) return;
  state.showDeletedSessions = deleted; renderPlanManagement();
  if (deleted) await refreshDeletedSessions(); else if (!state.managementLoaded) await refreshManagedSessions();
}
async function refreshDeletedSessions() {
  const request = ++state.trashRequest; state.trashLoading = true; state.trashError = ""; renderSessions();
  try {
    const result = await api("/api/sessions/deleted"); if (request !== state.trashRequest) return;
    state.deletedSessions = list(result.sessions).filter(session => !state.restoredSessionIds.has(session.id));
    state.deletedSessions.forEach(session => state.deletedSessionIds.add(session.id));
  } catch (error) { if (request === state.trashRequest) state.trashError = `已删除方案读取失败：${errorMessage(error)}`; }
  finally { if (request === state.trashRequest) { state.trashLoading = false; renderSessions(); } }
}
async function toggleDeletedSessions() {
  await selectSessionManagementTab(true);
}
function sessionDeletionControls() {
  const pending = state.sessionDeletion;
  $("confirm-session-delete").disabled = !pending || pending.loading || pending.submitting || !Number.isInteger(pending.revision);
  $("cancel-session-delete").disabled = Boolean(pending?.submitting);
  $("confirm-session-delete").textContent = pending?.submitting ? "正在删除…" : "确认删除";
}
async function readSessionDeletionTarget(pending, preserveError = false) {
  pending.read?.abort(); const controller = new AbortController(); pending.read = controller; pending.loading = true; pending.revision = null;
  sessionDeletionControls();
  if (!preserveError) $("session-delete-error").textContent = "";
  try {
    const session = await api(`/api/sessions/${encodeURIComponent(pending.id)}`, {signal: controller.signal});
    if (state.sessionDeletion !== pending || controller.signal.aborted) return;
    if (session.id !== pending.id || !Number.isInteger(session.revision) || session.deleted_at) throw new Error("无法确认方案的当前版本，请关闭后重试。");
    pending.revision = session.revision; pending.title = session.plan?.title || session.title || session.id;
    $("session-delete-name").textContent = `${pending.title} · r${pending.revision}`;
    $("session-delete-unsaved").textContent = state.session?.id === pending.id && state.dirty ? "当前方案有未保存修改，确认删除将丢弃这些修改。" : "若删除的是当前方案，未保存修改将丢弃。";
  } catch (error) {
    if (state.sessionDeletion === pending && error.name !== "AbortError") $("session-delete-error").textContent += `${preserveError ? "\n" : ""}读取方案失败：${errorMessage(error)}`;
  } finally { if (state.sessionDeletion === pending) { pending.loading = false; pending.read = null; sessionDeletionControls(); } }
}
async function openSessionDeletion(id) {
  if (!id || state.operation || state.sessionPurge || state.deletedSessionIds.has(id)) return;
  if (state.sessionDeletion) return;
  for (const dialog of document.querySelectorAll("dialog")) if (dialog.open) dialog.close();
  state.sourceRequest++; state.sessionRequest++;
  const pending = {id, revision: null, title: "", loading: true, submitting: false, read: null}; state.sessionDeletion = pending;
  $("session-delete-name").textContent = "正在读取方案与最新版本…"; $("session-delete-error").textContent = "";
  $("session-delete-unsaved").textContent = "若删除的是当前方案，未保存修改将丢弃。";
  sessionDeletionControls(); openDialog("session-delete-dialog"); $("cancel-session-delete").focus(); renderSessions();
  await readSessionDeletionTarget(pending);
}
function cancelSessionDeletion() {
  const pending = state.sessionDeletion; if (!pending || pending.submitting) return;
  pending.read?.abort(); state.sessionDeletion = null;
  if ($("session-delete-dialog").open) $("session-delete-dialog").close(); renderSessions();
}
async function confirmSessionDeletion() {
  const pending = state.sessionDeletion;
  if (!pending || pending.loading || pending.submitting || !Number.isInteger(pending.revision) || state.operation) return;
  pending.submitting = true; sessionDeletionControls();
  await runOperation("session-delete", "正在删除方案", "将方案移入可恢复的已删除列表，原始模型与资料库保持原样。", async () => {
    try {
      const result = await post(`/api/sessions/${encodeURIComponent(pending.id)}/delete`, {revision: pending.revision, confirmed: true});
      if (result.id !== pending.id || result.status !== "deleted") throw new Error("服务未确认方案已删除，请刷新历史后核对。");
      state.deletedSessionIds.add(pending.id); state.restoredSessionIds.delete(pending.id); state.statusRequest++; state.sessionRequest++; state.trashRequest++; state.trashLoading = false;
      invalidateManagedSessions(); state.managedSessions = state.managedSessions.filter(session => session.id !== pending.id);
      if (state.status) state.status = {...state.status, sessions: list(state.status.sessions).filter(session => session.id !== pending.id)};
      state.deletedSessions = [{...result, title: pending.title}, ...state.deletedSessions.filter(session => session.id !== pending.id)];
      if (state.session?.id === pending.id || !state.session && state.returnSessionId === pending.id) clearSessionContext();
      else if (state.returnSessionId === pending.id) { state.returnSessionId = null; syncWorkspaceURL(); }
      state.sessionDeletion = null; $("session-delete-dialog").close(); renderSessions();
      await refreshStatus(true); if (state.showDeletedSessions) await refreshDeletedSessions(); else if (state.workspaceView === "sessions") await refreshManagedSessions();
      notice(`方案「${pending.title}」已移入已删除方案，可恢复。原始模型与资料库未更改。`);
    } catch (error) {
      if (state.sessionDeletion === pending) { $("session-delete-error").textContent = errorMessage(error); if (error.status === 409) await readSessionDeletionTarget(pending, true); }
      throw error;
    }
  });
  if (state.sessionDeletion === pending) { pending.submitting = false; sessionDeletionControls(); if (!$("session-delete-dialog").open) cancelSessionDeletion(); }
}
async function restoreDeletedSession(id) {
  if (!id || state.operation || state.sessionDeletion || state.sessionPurge) return;
  await runOperation("session-restore", "正在恢复方案", "恢复保存在已删除列表中的方案，不切换当前工作。", async () => {
    const restored = await post(`/api/sessions/${encodeURIComponent(id)}/restore`, {confirmed: true});
    if (restored.id !== id || restored.deleted_at) throw new Error("服务未确认方案已恢复，请刷新历史后核对。");
    state.deletedSessionIds.delete(id); state.restoredSessionIds.add(id); state.statusRequest++; state.trashRequest++; state.trashLoading = false;
    invalidateManagedSessions(); state.deletedSessions = state.deletedSessions.filter(session => session.id !== id);
    await refreshStatus(true); renderSessions(); notice("方案已恢复至已保存方案列表，当前工作未切换。");
  });
}
function sessionPurgeControls() {
  const pending = state.sessionPurge;
  $("confirm-session-purge").disabled = !pending || pending.loading || pending.submitting || !Number.isInteger(pending.revision);
  $("cancel-session-purge").disabled = Boolean(pending?.submitting);
  textIfChanged($("confirm-session-purge"), pending?.submitting ? "正在彻底删除…" : "确认彻底删除");
}
async function readSessionPurgeTarget(pending, preserveError = false) {
  pending.read?.abort(); const controller = new AbortController(); pending.read = controller; pending.loading = true; pending.revision = null; sessionPurgeControls();
  if (!preserveError) $("session-purge-error").textContent = "";
  try {
    const result = await api("/api/sessions/deleted", {signal: controller.signal}); if (state.sessionPurge !== pending || controller.signal.aborted) return;
    const session = list(result.sessions).find(session => session.id === pending.id);
    if (!session?.deleted_at || !Number.isInteger(session.revision)) throw new Error("该方案已不在已删除列表，或其版本无法核对。请取消后刷新列表。");
    pending.revision = session.revision; pending.title = session.title || session.id;
    $("session-purge-name").textContent = `${pending.title} · r${pending.revision}`;
  } catch (error) { if (state.sessionPurge === pending && error.name !== "AbortError") $("session-purge-error").textContent += `${preserveError ? "\n" : ""}核对已删除方案失败：${errorMessage(error)}`; }
  finally { if (state.sessionPurge === pending) { pending.loading = false; pending.read = null; sessionPurgeControls(); } }
}
async function openSessionPurge(id) {
  if (!id || state.operation || state.sessionDeletion || state.sessionPurge || !state.deletedSessions.some(session => session.id === id && session.deleted_at)) return;
  for (const dialog of document.querySelectorAll("dialog")) if (dialog.open) dialog.close();
  const pending = {id, revision: null, title: "", loading: true, submitting: false, read: null}; state.sessionPurge = pending;
  $("session-purge-name").textContent = "正在核对已删除方案与最新版本…"; $("session-purge-error").textContent = "";
  sessionPurgeControls(); openDialog("session-purge-dialog"); $("cancel-session-purge").focus(); renderSessions(); await readSessionPurgeTarget(pending);
}
function cancelSessionPurge() {
  const pending = state.sessionPurge; if (!pending || pending.submitting) return;
  pending.read?.abort(); state.sessionPurge = null; if ($("session-purge-dialog").open) $("session-purge-dialog").close(); renderSessions();
}
async function confirmSessionPurge() {
  const pending = state.sessionPurge;
  if (!pending || pending.loading || pending.submitting || !Number.isInteger(pending.revision) || state.operation) return;
  pending.submitting = true; sessionPurgeControls();
  await runOperation("session-purge", "正在彻底删除方案", "永久删除已删除方案与方案版本快照。", async () => {
    try {
      const result = await post(`/api/sessions/${encodeURIComponent(pending.id)}/purge`, {revision: pending.revision, confirmed: true});
      if (result.id !== pending.id || result.status !== "purged") throw new Error("服务未确认已彻底删除，列表暂时保留；请刷新后核对。");
      state.deletedSessionIds.add(pending.id); state.restoredSessionIds.delete(pending.id); state.statusRequest++; state.sessionRequest++; state.trashRequest++; state.trashLoading = false;
      invalidateManagedSessions(); state.deletedSessions = state.deletedSessions.filter(session => session.id !== pending.id); state.managedSessions = state.managedSessions.filter(session => session.id !== pending.id);
      if (state.status) state.status = {...state.status, sessions: list(state.status.sessions).filter(session => session.id !== pending.id)};
      if (state.session?.id === pending.id || !state.session && state.returnSessionId === pending.id) clearSessionContext();
      else if (state.returnSessionId === pending.id) { state.returnSessionId = null; syncWorkspaceURL(); }
      state.sessionPurge = null; $("session-purge-dialog").close(); renderSessions(); await refreshStatus(true);
      notice(`方案「${pending.title}」及其方案版本已彻底删除，无法恢复。`);
    } catch (error) {
      if (state.sessionPurge === pending) { $("session-purge-error").textContent = errorMessage(error); if (error.status === 409) await readSessionPurgeTarget(pending, true); }
      throw error;
    }
  });
  if (state.sessionPurge === pending) { pending.submitting = false; sessionPurgeControls(); if (!$("session-purge-dialog").open) cancelSessionPurge(); }
}
function systemStatusCards(status, session, agent) {
  const services = status?.services || {}, cards = [];
  const sim = services.sim_cli || {};
  cards.push({ id: "sim_cli", title: "模型解析 · sim-cli", label: sim.status_label || (sim.available === true ? "可用" : sim.available === false ? "不可用" : "状态未报告"), tone: sim.available === true ? "good" : "warn", mode: sim.mode, detail: sim.detail || "读取几何与已有模型；解析成功不代表已完成物理验证。" });
  const page = services.pageindex || {}, installed = page.installed ?? (page.available === true ? true : page.mode === "not-installed" ? false : undefined);
  let pageLabel = "状态未报告", pageDetail = "可选 PDF 推理组件；普通 HTML / PDF 资料读取不依赖它。";
  if (page.available === true) pageLabel = "可用";
  else if (installed === false) { pageLabel = "可选 · 未安装"; pageDetail = "未安装可选 PageIndex 扩展；仍可使用普通 HTML / PDF 读取与资料库检索。"; }
  else if (installed === true) {
    pageLabel = page.model_configured !== true ? "已安装 · 待模型配置" : "已安装 · 待验证";
    pageDetail = page.model_configured !== true ? "SDK 已安装；PDF 推理实验仍需显式配置模型与凭据。" : "SDK 与模型配置已报告；是否可执行以实际验证状态为准。";
  }
  cards.push({ id: "pageindex", title: "PDF 推理 · PageIndex（可选）", label: installed === undefined ? page.status_label || pageLabel : pageLabel, tone: page.available === true ? "good" : "neutral", detail: page.status_label && page.detail ? page.detail : pageDetail });
  const qmd = services.qmd || {}, indexes = qmd.library_indexes || status?.corpus?.library_indexes;
  const component = qmd.component_available ?? qmd.installed ?? (qmd.available === true ? true : undefined);
  const modeStates = { queued: "等待构建", running: "构建中", building: "构建中", indexing: "构建中", interrupted: "已中断", cancelled: "已中断", cancel_requested: "正在中断", not_requested: "未构建（按需）", not_built: "未构建", unavailable: "未就绪", missing: "未构建", stale: "已失效", error: "失败", failed: "失败", partial: "部分就绪", ready: "就绪标记待核验" };
  const modes = [["qmd_keyword", "QMD 关键词 · qmd_keyword"], ["qmd_vector", "QMD 向量 · qmd_vector"]].map(([id, title]) => {
    const mode = indexes?.modes?.[id];
    const rawError = mode?.error ? valueText(mode.error) : "";
    const translated = /^Build owner exited before this mode completed/.test(rawError) ? "构建进程已退出，该模式未完成；已完成的模式仍可使用。" : /^This request did not resume the unfinished mode/.test(rawError) ? "本次操作未续建先前未完成的索引；不会自动重新开始。" : null;
    return { id, title, label: mode?.ready === true ? "索引就绪" : modeStates[mode?.state] || mode?.state || "状态未报告", tone: mode?.ready === true ? "good" : ["interrupted", "cancelled", "error", "failed", "stale"].includes(mode?.state) ? "warn" : "neutral", error: translated || rawError, technical: translated ? rawError : null };
  });
  const keywordReady = indexes?.modes?.qmd_keyword?.ready === true, vectorReady = indexes?.modes?.qmd_vector?.ready === true;
  let qmdLabel = "组件状态未报告";
  if (component === false) qmdLabel = "组件未配置";
  else if (keywordReady || vectorReady) qmdLabel = keywordReady && vectorReady ? "关键词与向量可用" : keywordReady ? "关键词可用" : "向量可用";
  else if (component === true) qmdLabel = "已安装 · 索引未就绪";
  const visibility = status?.corpus?.library_visibility;
  const noEnabledLibraries = visibility?.enabled_library_count === 0 && ((visibility.disabled_library_count || 0) + (visibility.deleted_library_count || 0)) > 0;
  if (noEnabledLibraries && (keywordReady || vectorReady)) qmdLabel = "索引保留 · 无启用资料库";
  // Modern registered-library modes take precedence over the legacy full-manual
  // verification flag, including when speaking to an older server.
  const visibilityDetail = visibility ? `\n资料库：启用 ${visibility.enabled_library_count ?? "未报告"} · 关闭 ${visibility.disabled_library_count ?? "未报告"} · 已删除 ${visibility.deleted_library_count ?? "未报告"}。启停不改变已保存的索引。` : "";
  cards.push({ id: "qmd", title: "资料库检索 · QMD", label: indexes ? qmdLabel : qmd.status_label || qmdLabel, tone: !noEnabledLibraries && component !== false && (keywordReady || vectorReady) ? "good" : "neutral", mode: `组件：${component === true ? "已安装 / 已配置" : component === false ? "未配置" : "未报告"}`, detail: (qmd.status_label && qmd.detail ? qmd.detail : "以下是注册资料库的 QMD 关键词和可选向量模式；向量默认关闭，不影响已就绪的关键词检索。") + visibilityDetail, modes });
  const planner = services.planner || {};
  const disabled = ["none", "disabled", "not-configured"].includes(planner.mode);
  cards.push({ id: "planner", title: "工作台托管 Agent", label: planner.status_label || (disabled ? "托管后端已关闭" : planner.available === true ? "可用" : planner.available === false ? "不可用" : "状态未报告"), tone: planner.available === true ? "good" : "neutral", mode: planner.mode ? `托管调试后端：${planner.mode}` : "托管调试后端：未报告", detail: `${planner.detail || "此处仅报告工作台可启动的托管后端。"}\n当前方案的外部会话连接单独显示，不由此状态推断。` });
  let current = { id: "session_agent", title: "当前方案 Agent", label: "未选择方案", tone: "neutral", detail: "选择方案后显示它的连接状态；资料库可独立使用。" };
  if (session) {
    current = { ...current, label: "连接状态未读取", detail: "正在读取所选方案的 Agent 连接；不代表已绑定 DSH。" };
    if (agent?.session_id === session.id) {
      const external = agent.connection_mode === "external", binding = agent.external || {};
      const connected = external ? agent.connected === true && binding.registered === true && binding.alive === true : agent.connected === true && agent.alive === true;
      const activity = agent.status === "running" ? "运行中" : agent.status === "starting" ? "正在连接" : "已连接";
      const backend = agent.managed_backend === "dsh" ? "DSH" : agent.managed_backend === "codex" ? "Codex" : agent.managed_backend || "托管 Agent";
      const offline = external && binding.registered === true && !connected && binding.offline_reason;
      const offlineDetail = offline ? ({heartbeat_timeout: "外部 Agent 心跳已超时，请检查进程并重新连接。", process_exited: "外部 Agent 进程已退出，需要重新接入当前方案。"}[offline] || (/[^\x00-\x7F]/.test(offline) ? offline : "外部 Agent 已失联，请检查进程与连接。")) : null;
      const connectionError = (!external || binding.registered === true) && agent.error ? valueText(agent.error) : null;
      current = { ...current, label: connected ? `${external ? "外部" : "托管"}${activity}` : external ? binding.registered ? "外部连接已失联" : "外部 Agent 未绑定" : agent.status === "starting" ? "托管正在连接" : agent.status === "error" ? "连接错误" : "未连接", tone: connected ? "good" : offline || !external && agent.status === "error" ? "warn" : "neutral", mode: external ? `外部会话：${binding.name || "名称未报告"}` : `当前方案托管后端：${backend}`, detail: offlineDetail || connectionError || (connected ? `${agent.model || "模型未报告"}${agent.reasoning_effort ? " · " + agent.reasoning_effort : ""}。连接不代表方案已完成或已求解。` : external ? "请复制本方案的 DSH 绑定链接，在 DSH 当前会话重新绑定；工作台不会自动启动或注册 Agent。" : "可在当前方案的 Agent 对话中连接。") };
      if (!connected && !external && disabled) current.detail = "当前服务的托管后端已关闭；可将当前方案切换为外部模式后绑定 DSH 等 Agent。";
    }
  }
  cards.push(current);
  return cards;
}
function renderSystem() {
  const cards = systemStatusCards(state.status, state.session, state.agent), key = JSON.stringify(cards);
  if (key !== systemRenderKey) {
  systemRenderKey = key;
  const container = $("service-list"); container.replaceChildren();
  cards.forEach((service) => {
    const card = el("section", "service-card"), heading = el("div", "service-card-heading"); card.dataset.service = service.id;
    heading.append(el("h3", "", service.title), el("span", `badge badge-${service.tone}`, service.label));
    card.append(heading);
    if (service.mode) card.append(el("p", "service-mode", service.mode));
    card.append(el("p", "service-detail", service.detail));
    if (service.modes) {
      const rows = el("div", "system-index-modes");
      service.modes.forEach((mode) => {
        const row = el("div", "system-index-mode"); row.dataset.mode = mode.id;
        row.append(el("span", "", mode.title), el("span", `badge badge-${mode.tone}`, mode.label));
        if (mode.error) row.append(el("p", "source-error", valueText(mode.error)));
        if (mode.technical) { const details = el("details", "system-mode-details"); details.append(el("summary", "", "技术错误详情"), el("p", "service-detail", mode.technical)); row.append(details); }
        rows.append(row);
      });
      card.append(rows);
    }
    container.append(card);
  });
  }
  renderCorpus(state.status?.corpus);
}
function renderCorpus(corpus) {
  const labels = { ready: "索引就绪", status: "索引状态", state: "索引状态", document_count: "文档数量", documents: "文档数量", source_count: "来源数量", sources: "来源数量", chunk_count: "文本段落", chunks: "文本段落", sections: "章节数量", counts: "索引统计", by_kind: "来源分类", packages: "文档包", version: "文档版本", doc_root_available: "文档目录可读", indexed: "已索引", total: "总计", processed: "已处理", progress: "处理进度", path: "索引位置", doc_root: "文档目录", root: "文档目录", backend: "检索模式", mode: "索引模式", detail: "状态说明", error: "索引错误", errors: "索引错误", warning: "提示", limitations: "已知限制", last_built: "最近构建", built_at: "最近构建", last_build_at: "最近构建", last_build: "本次构建", message: "状态说明", indexing: "正在构建", building: "正在构建" };
  const visible = corpus && typeof corpus === "object" ? Object.entries(corpus).filter(([key, value]) => key in labels && value !== undefined && value !== null && value !== "") : null;
  const key = JSON.stringify([visible?.length ? visible : corpus ?? null, state.indexResult?.last_build, Boolean(state.operation), state.indexBusy]);
  if (key === corpusRenderKey) return; corpusRenderKey = key;
  const container = $("corpus-status"); container.replaceChildren();
  if (!corpus || typeof corpus !== "object") { container.append(el("p", "inline-empty", "尚未收到知识库状态。")); return; }
  const rows = el("div", "corpus-lines");
  let shown = 0;
  Object.entries(corpus).forEach(([key, value]) => {
    if (!(key in labels) || value === undefined || value === null || value === "") return;
    const row = el("div", "corpus-line"); let text = value;
    if (typeof value === "boolean") text = value ? "是" : "否";
    else if (typeof value === "object") text = Array.isArray(value) ? value.map(valueText).join("；") : Object.entries(value).map(([k, v]) => `${k}: ${valueText(v)}`).join(" · ");
    row.append(el("span", "", labels[key]), el("span", "", text)); rows.append(row); shown++;
  });
  if (!shown) rows.append(el("p", "inline-empty", JSON.stringify(corpus, null, 2)));
  if (state.indexResult?.last_build && !corpus.last_build) { const row = el("div", "corpus-line"); const build = state.indexResult.last_build; row.append(el("span", "", "本次构建"), el("span", "", `新增 ${build.indexed ?? "未知"} · 未变 ${build.unchanged ?? "未知"} · 耗时 ${build.seconds ?? "未知"} 秒${list(build.errors).length ? `\n错误：${build.errors.map(valueText).join("；")}` : ""}`)); rows.append(row); }
  container.append(rows);
  const externalIndexing = corpus.building === true || corpus.indexing === true || ["building", "indexing", "running"].includes(corpus.status) || ["building", "indexing", "running"].includes(corpus.state);
  setIfChanged($("index-progress"), "hidden", !(state.indexBusy || externalIndexing));
  const progress = corpus.progress || corpus.message || corpus.detail;
  textIfChanged($("index-progress-text"), typeof progress === "string" ? progress : "正在更新已登记资料库的 QMD 关键词索引，请稍候…");
  setIfChanged($("build-index"), "disabled", Boolean(state.operation) || state.indexBusy || externalIndexing);
}

function emptyModel() {
  const wrap = el("div", "model-empty"), art = el("div", "mesh-art"); art.setAttribute("aria-hidden", "true");
  // This static SVG is the only retained markup; all model data uses text nodes.
  const initialArt = document.querySelector("#model-content .mesh-art svg");
  if (initialArt) state.meshArt = initialArt.cloneNode(true);
  if (state.meshArt) art.append(state.meshArt.cloneNode(true));
  wrap.append(art, el("h3", "", "从你的几何开始"));
  const text = el("p", "", "读取 STEP 后展示几何事实，再由 Agent 研究需要建立的物理模型。");
  wrap.append(text, el("span", "empty-caption", ".stp / .step 几何 · 也支持已有 .mph 模型")); return wrap;
}
function renderModel(container = $("model-content"), model = state.model, intake = true) {
  if (!container) return;
  if (!state.meshArt) { const svg = container.querySelector("svg"); if (svg) state.meshArt = svg.cloneNode(true); }
  const key = JSON.stringify(model ?? null);
  if (container.renderKey === key && container.childElementCount) return; container.renderKey = key;
  container.replaceChildren();
  if (!model) { (intake ? $("model-badge") : {}).textContent = "等待读取"; (intake ? $("model-badge") : {}).className = "badge badge-neutral"; container.append(emptyModel()); return; }
  const cad = isGeometry(model);
  (intake ? $("model-badge") : {}).textContent = cad ? "几何已读取" : "已只读解析"; (intake ? $("model-badge") : {}).className = "badge badge-good";
  const fileHeading = el("div", "model-file-heading"), title = el("div");
  title.append(el("h3", "", model.file_name || "输入快照"), el("p", "", `${cad ? "STEP 几何" : `COMSOL ${model.version || "版本未知"}`} · ${model.parser || "解析方式未报告"}`));
  fileHeading.append(el("span", "file-icon", cad ? "STEP" : "MPH"), title); container.append(fileHeading);
  const summary = el("dl", "fact-list model-summary");
  function summaryRow(label, value) { const row = el("div", "fact-row"); row.append(el("dt", "", label), el("dd", "", value)); summary.append(row); }
  function recordSummary(records) { const values = list(records); if (!values.length) return "未提取 · 仍需核对"; const preview = values.slice(0, 3).map(valueText).join("、"); return `${preview}${values.length > 3 ? ` 等 ${values.length} 项` : ""}`; }
  if (cad) {
    const geometry = model.geometry || {}, kernel = geometry.kernel || {};
    summaryRow("输入类型", "CAD 几何 · 仿真物理模型尚未定义");
    if (list(geometry.products).length) summaryRow("几何名称", recordSummary(geometry.products));
    if (list(geometry.schemas).length) summaryRow("STEP 结构", geometry.schemas.join("、"));
    const units = list(geometry.length_unit_declarations);
    summaryRow("长度单位", kernel.unit ? valueText(kernel.unit) : units.length ? units.map(valueText).join("、") : "尚未确定 · 需核对导入单位");
    if (kernel.status) summaryRow("几何内核", `${kernel.engine || "本地内核"} · ${kernel.status}`);
    if (kernel.bounding_box) summaryRow("包围盒尺寸", boundingBoxSummary(kernel.bounding_box, kernel.unit));
    if (kernel.available && kernel.topology && Object.keys(kernel.topology).length) summaryRow("内核拓扑", Object.entries(kernel.topology).map(([key, value]) => `${({ solids: "实体", faces: "面", shells: "壳", edges: "边", vertices: "顶点" })[key] || key} ${valueText(value)}`).join(" · "));
  } else {
    [["components", "组件"], ["physics", "物理场"], ["materials", "材料"], ["studies", "研究"]].forEach(([key, label]) => summaryRow(label, recordSummary(model[key])));
    const params = Object.entries(model.parameters || {});
    summaryRow("全局参数", params.length ? `${params.length} 项 · ${params.slice(0, 3).map(([key, value]) => `${key} = ${valueText(value)}`).join("；")}${params.length > 3 ? "…" : ""}` : "未提取 · 仍需核对");
  }
  container.append(summary);
  if (cad && model.geometry?.kernel?.bounding_box) container.append(el("p", "geometry-overview-note", "外形尺寸仅作概览，数值已适度舍入。包围盒包含 CAD 边界容差，不代表制造或测量精度；原值与来源见技术证据。"));
  if (cad) { const definitions = el("div", "cad-definition-state"); definitions.append(el("h4", "", "下一步需要设计"), el("p", "", "物理场与耦合 · 材料 · 边界与初始条件 · 研究类型"), el("span", "", "STEP 文件提供几何；这些仿真设置将在 Agent 规划与意图确认中建立。")); container.append(definitions); }
  const allUnknowns = list(model.unknowns), keyUnknowns = allUnknowns.filter((item) => !/dmodel\.xml|顶层节点类别|未提取到|精确 COMSOL 保存版本/.test(valueText(item))).slice(0, 2);
  if (!keyUnknowns.length) keyUnknowns.push(...allUnknowns.slice(0, 2));
  if (keyUnknowns.length) { const unknowns = el("div", "model-unknowns model-key-unknowns"), ul = el("ul"); unknowns.append(el("h4", "", "关键未知信息")); keyUnknowns.forEach((item) => ul.append(el("li", "", valueText(item)))); unknowns.append(ul); container.append(unknowns); }
  const details = el("details", "model-evidence-details"); details.append(el("summary", "", "解析证据与技术细节"));
  const evidence = el("div", "model-evidence-body");
  evidence.append(el("p", "read-only-note", cad ? "以下为几何解析证据。STEP 原始实体记录计数不能直接作为经几何内核验证的实体或面数量。" : "以下为解析器返回的原始事实与出处。存储记录不能证明设置已启用或适用于当前目标。"));
  if (list(model.facts).length) { const rawFacts = el("dl", "fact-list raw-facts"); list(model.facts).forEach((fact) => { const row = el("div", "fact-row"), value = el("dd"); value.append(el("pre", "fact-value", typeof fact.value === "object" ? JSON.stringify(fact.value, null, 2) : valueText(fact.value))); if (fact.source) value.append(el("span", "fact-source", `来源：${valueText(fact.source)}`)); row.append(el("dt", "", fact.label || fact.key || "模型信息"), value); rawFacts.append(row); }); evidence.append(rawFacts); }
  if (allUnknowns.length) { const unknowns = el("div", "model-unknowns"), ul = el("ul"); unknowns.append(el("h4", "", "全部未知信息")); allUnknowns.forEach((item) => ul.append(el("li", "", valueText(item)))); unknowns.append(ul); evidence.append(unknowns); }
  list(model.warnings).forEach((warning) => evidence.append(el("p", "model-warning", valueText(warning))));
  const raw = el("details", "raw-snapshot-details"); raw.append(el("summary", "", "完整模型快照（JSON，含节点与参数出处）")); lazyContent(raw, () => raw.append(el("pre", "raw-snapshot", JSON.stringify(model, null, 2)))); evidence.append(raw); details.append(evidence); container.append(details);
  container.append(el("p", "read-only-note", cad ? "只读几何快照 · 物理意义与导入设置需结合目标核对。" : "只读快照 · 节点记录与有效设置仍需在 COMSOL 中复核。"));
}
async function inspectPath() {
  const path = $("model-path").value.trim();
  if (!path) { notice("请先填写 .stp、.step 或 .mph 文件路径，或选择文件上传。", true); $("model-path").focus(); return; }
  await runOperation("inspect", "正在读取输入文件", "提取文件中可确认的几何或模型结构，保留未知信息。", async () => {
    $("model-panel").setAttribute("aria-busy", "true");
    try { state.model = await post("/api/models/inspect", { path }); state.modelPath = path; renderModel(); notice("输入快照已读取。可核对几何与已知信息后建立仿真任务。"); }
    finally { $("model-panel").setAttribute("aria-busy", "false"); }
  });
}
async function uploadModel(file) {
  if (!file) return;
  if (!/\.(?:stp|step|mph)$/i.test(file.name)) { notice("请选择 .stp、.step 几何或 .mph 仿真模型文件。", true); $("model-file").value = ""; return; }
  await runOperation("upload", "正在上传并读取文件", `${file.name} · 文件较大时，请等待本地服务完成处理。`, async () => {
    $("model-panel").setAttribute("aria-busy", "true");
    try {
      state.model = await api("/api/models/upload", { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-File-Name": encodeURIComponent(file.name) }, body: file });
      state.modelPath = state.model.path || ""; $("model-path").value = state.modelPath; $("file-name").textContent = file.name;
      renderModel(); notice("文件已上传至本地服务并完成只读解析。");
    } finally { $("model-panel").setAttribute("aria-busy", "false"); $("model-file").value = ""; }
  });
}

const sideTabs = ["intent", "agent", "sources", "history"];
function storedPreference(key, fallback) { try { return window.localStorage?.getItem(key) ?? fallback; } catch { return fallback; } }
function storePreference(key, value) { try { window.localStorage?.setItem(key, value); } catch { /* preference only */ } }
state.sidePanelOpen = storedPreference("simAdvisor.sidePanel", "open") !== "closed";
state.navPreference = storedPreference("simAdvisor.nav", "");
function setSidePanel(open) {
  state.sidePanelOpen = Boolean(open); storePreference("simAdvisor.sidePanel", open ? "open" : "closed");
  $("workbench")?.classList.toggle("side-collapsed", !state.sidePanelOpen);
  const toggle = $("side-toggle"); if (!toggle) return;
  const label = state.sidePanelOpen ? "收起右侧面板" : "展开右侧面板";
  toggle.setAttribute("aria-expanded", String(state.sidePanelOpen)); toggle.setAttribute("aria-label", label); toggle.title = label;
  const mark = toggle.querySelector?.("[aria-hidden]"); if (mark) mark.textContent = state.sidePanelOpen ? "»" : "«";
}
// Left navigation: the user's explicit choice wins; until they make one it folds while a plan is open.
function navCompact() { return state.navPreference ? state.navPreference === "compact" : Boolean(state.session); }
function syncNav() {
  const compact = navCompact(); document.body.classList.toggle("nav-compact", compact);
  const toggle = $("nav-toggle"); if (!toggle) return;
  const label = compact ? "展开导航" : "收起导航";
  toggle.setAttribute("aria-expanded", String(!compact)); toggle.title = label; toggle.setAttribute("aria-label", label);
  const text = toggle.querySelector?.(".nav-toggle-label"); if (text) text.textContent = label;
  const icon = toggle.querySelector?.("[aria-hidden]"); if (icon) icon.textContent = compact ? "»" : "«";
}
function toggleNav() { state.navPreference = navCompact() ? "expanded" : "compact"; storePreference("simAdvisor.nav", state.navPreference); syncNav(); }
function selectTab(name, focus = false, open = true) {
  if (!sideTabs.includes(name)) name = "intent";
  state.tab = name;
  sideTabs.forEach((tab) => {
    const selected = tab === name; $("tab-" + tab).classList.toggle("active", selected); $("tab-" + tab).setAttribute("aria-selected", String(selected)); $("tab-" + tab).tabIndex = selected ? 0 : -1; $("view-" + tab).hidden = !selected;
  });
  if (open) setSidePanel(true); else setSidePanel(state.sidePanelOpen);
  if (focus) $("tab-" + name).focus();
  if (name === "history" && state.session) void loadVersions();
}
function normalizedAnswer(question, value) {
  if (!isNonBlank(value)) return "";
  if (question.kind === "number") { const number = Number(value); return Number.isFinite(number) ? number : ""; }
  return String(value);
}
function syncWorkspaceURL() {
  const url = new URL(window.location.href), returnId = state.session?.id || state.returnSessionId || null;
  if (["libraries", "sessions"].includes(state.workspaceView)) { url.searchParams.set("view", state.workspaceView); url.searchParams.delete("session"); }
  else { url.searchParams.delete("view"); if (returnId) url.searchParams.set("session", returnId); else url.searchParams.delete("session"); }
  window.history.replaceState({ ...window.history.state, returnSessionId: returnId }, "", url);
}
function setWorkspaceView(name, { focus = false, updateURL = true } = {}) {
  const libraries = name === "libraries", management = name === "sessions"; state.workspaceView = libraries ? "libraries" : management ? "sessions" : "plans";
  $("plan-workspace").hidden = libraries || management; $("library-management").hidden = !libraries; $("plan-management").hidden = !management;
  document.body.classList.toggle("viewing-libraries", libraries);
  document.body.classList.toggle("viewing-plan-management", management);
  $("workspace-name").textContent = libraries ? "资料库管理" : management ? "方案管理" : "方案工作区";
  [["open-workspace", !libraries && !management], ["open-workspace-mobile", !libraries && !management], ["open-plan-management", management], ["open-plan-management-mobile", management], ["open-libraries", libraries], ["open-libraries-mobile", libraries]].forEach(([id, selected]) => {
    const button = $(id); if (!button) return; button.classList.toggle("active", selected);
    if (selected) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  if (management) renderPlanManagement();
  if (updateURL) syncWorkspaceURL();
  if (focus) { $("main").focus({ preventScroll: true }); $("main").scrollIntoView({ behavior: "smooth", block: "start" }); }
}
async function openWorkspace() {
  setWorkspaceView("plans", { focus: true });
  if (!state.session && state.returnSessionId) await restoreSession(state.returnSessionId);
}
function adoptSession(session, resetTab = false) {
  if (!session || session.deleted_at || state.deletedSessionIds.has(session.id)) return false;
  if (state.session?.id !== session.id) resetVersions();
  if (state.session?.id !== session.id) state.agent = null;
  state.session = session; state.model = session.model || null; state.modelPath = session.model?.path || ""; state.dirty = false;
  state.draftAnswers = {};
  list(session.questions).forEach((question) => { state.draftAnswers[question.id] = normalizedAnswer(question, session.answers?.[question.id]); });
  $("goal").value = session.goal || ""; $("model-path").value = state.modelPath; $("file-name").textContent = "或填写本机文件路径";
  $("workbench").hidden = false; $("how-it-works").hidden = true;
  if ($("task-inputs")) $("task-inputs").open = false;
  document.body.classList.add("has-session"); syncNav();
  renderModel(); renderSession(); if (resetTab) selectTab("intent", false, false);
  agentPanel?.bindSession();
  if ($("system-dialog").open) renderSystem();
  state.returnSessionId = session.id; syncWorkspaceURL();
  return true;
}
async function createSession(event) {
  event.preventDefault(); const goal = $("goal").value.trim(); if (!goal) return;
  if (state.dirty && !window.confirm("当前回答尚未保存。放弃这些修改并建立新任务？")) return;
  const path = $("model-path").value.trim();
  await runOperation("create", "正在建立仿真任务", "保存目标与输入快照。建立后可调用 Agent 研究并设计物理方案。", async () => {
    const payload = { goal };
    if (path) payload.model_path = path;
    if (state.model?.id && path === state.modelPath) payload.model_id = state.model.id;
    const session = await post("/api/sessions", payload); adoptSession(session, true); setWorkspaceView("plans");
    notice("仿真任务已建立。请点击「Agent 规划方案」，根据目标与几何设计建模路线。");
    $("workbench").scrollIntoView({ behavior: "smooth", block: "start" }); await refreshStatus(true);
  });
}
async function restoreSession(id) {
  if (state.deletedSessionIds.has(id) || state.sessionDeletion || state.sessionPurge || state.operation) return;
  if (state.session?.id === id) { setWorkspaceView("plans", {focus: true}); return; }
  if (state.dirty && !window.confirm("当前回答尚未保存。放弃这些修改并恢复所选方案？")) return;
  await runOperation("restore", "正在恢复方案", "读取服务中保存的方案、回答与版本历史。", async () => { const request = ++state.sessionRequest; const session = await api(`/api/sessions/${encodeURIComponent(id)}`); if (request !== state.sessionRequest || !adoptSession(session, true)) return; setWorkspaceView("plans"); notice(""); $("workbench").scrollIntoView({ behavior: "smooth", block: "start" }); renderSessions(); });
}
function clearSessionContext() {
  resetVersions();
  state.sessionRequest++; state.session = null; state.agent = null; state.returnSessionId = null; state.model = null; state.modelPath = ""; state.draftAnswers = {}; state.dirty = false;
  agentPanel?.bindSession();
  if ($("system-dialog").open) renderSystem();
  state.overviewNodes = null; state.questionGroups = []; document.body.classList.remove("has-session"); syncNav();
  if ($("task-inputs")) $("task-inputs").open = true;
  $("intake-form").reset(); $("file-name").textContent = "或填写本机文件路径"; $("workbench").hidden = true; $("how-it-works").hidden = false;
  $("plan-heading").textContent = "仿真任务"; $("revision-label").textContent = ""; $("export-markdown").href = "#"; $("export-json").href = "#";
  syncWorkspaceURL(); renderModel(); renderSessions();
}
function newSession() {
  if (state.operation || state.sessionDeletion || state.sessionPurge) return;
  if (state.dirty && !window.confirm("当前回答尚未保存。放弃这些修改并新建方案？")) return;
  clearSessionContext();
  notice(""); setWorkspaceView("plans"); $("intake").scrollIntoView({ behavior: "smooth", block: "start" }); $("goal").focus();
}
function renderSession() {
  const session = state.session; if (!session) return;
  $("plan-heading").textContent = session.plan?.title || session.title || "仿真任务";
  $("session-status").textContent = !hasAgentPlan() ? "任务已建立 · 待 Agent 规划" : session.planning_incomplete ? "研究尚未完成 · 待继续规划" : statusLabels[session.status] || session.status;
  $("session-status").className = `badge ${session.status === "confirmed" ? "badge-good" : ["needs_input", "needs_replan"].includes(session.status) ? "badge-warn" : "badge-neutral"}`;
  $("revision-label").textContent = `版本 r${session.revision}`;
  const engine = session.plan?.engine;
  $("plan-engine").textContent = engine === "intake-only" ? "尚未调用 Agent" : engine === "reference-baseline" ? "旧版规则参考 · 待重新规划" : `规划引擎：${engine || "未报告"}`;
  $("plan-engine").title = engine || "未报告";
  $("export-markdown").href = `/api/sessions/${encodeURIComponent(session.id)}/export?format=markdown`;
  $("export-json").href = `/api/sessions/${encodeURIComponent(session.id)}/export?format=json`;
  renderPlanningState(); renderPlan(); renderSources(); renderQuestions(); renderHistory(); syncRefine(); renderSessions(); selectTab(state.tab, false, false);
}
let planningStateKey = null;
function renderPlanningState() {
  const container = $("planning-state"), planned = hasAgentPlan();
  const key = JSON.stringify([state.session?.id, planned, state.session?.plan_stale, state.session?.planning_incomplete, unresolvedQuestions(false).length > 0, hasMethodEvidenceGaps()]);
  if (key === planningStateKey) return; planningStateKey = key;
  container.replaceChildren(); container.hidden = planned && !state.session?.plan_stale;
  if (container.hidden) return;
  const content = el("div");
  content.append(el("h3", "", !planned ? "任务已建立 · 待规划" : state.session?.plan_stale ? "输入已更新 · 方案待更新" : "研究尚未完成"));
  const gaps = [];
  if (unresolvedQuestions(false).length) gaps.push("用户资料待明确");
  if (state.session?.planning_incomplete) gaps.push(hasMethodEvidenceGaps() ? "方法依据待补查" : "研究未完成，原因见研究记录");
  if (state.session?.plan_stale && planned) gaps.push("流程需按新输入更新");
  content.append(el("p", "", !planned ? "输入已保存，下一步由 Agent 形成建模路线。" : `${gaps.join("；") || "流程仍待核对"}。当前仅保留草稿。`));
  container.append(content);
}
function appendList(container, heading, items, className = "") {
  if (!list(items).length) return;
  const block = el("div", className); block.append(el("h3", "plan-block-title", heading)); const ul = el("ul", "simple-list"); items.forEach((item) => ul.append(el("li", "", valueText(item)))); block.append(ul); container.append(block);
}
function sourceNumber(id, session = state.session) { const index = list(session?.sources).findIndex((source) => source.id === id); return index < 0 ? "来源" : String(index + 1); }
function citationButton(id, session = state.session) { const button = el("button", "citation", `[${sourceNumber(id, session)}]`); button.type = "button"; const source = list(session?.sources).find((item) => item.id === id); button.setAttribute("aria-label", `查看来源：${source?.title || id}`); button.disabled = source?.availability?.readable === false; button.title = button.disabled ? "资料库已关闭或删除，引用记录保留" : source?.title || id; if (!button.disabled) button.addEventListener("click", () => showSource(id)); return button; }
function appendCitations(container, ids, session = state.session) { const valid = new Set(list(session?.sources).map((source) => source.id)); const row = el("div", "citation-row"); list(ids).filter((id) => valid.has(id)).forEach((id) => row.append(citationButton(id, session))); if (row.childElementCount) container.append(row); }
function renderGeometryFactReferences(container, candidate, session = state.session) {
  const section = el("div", "simplification-detail geometry-fact-references"); section.append(el("h5", "route-detail-label", "可核对的几何事实"));
  if (!Object.prototype.hasOwnProperty.call(candidate, "geometry_fact_refs")) { section.append(el("p", "simplification-evidence-gap", "旧版建议尚无结构化几何引用，可补充简化分析。")); container.append(section); return; }
  const references = list(candidate.geometry_fact_refs), model = session?.model;
  if (!session?.geometry_reference_catalog) section.append(el("p", "geometry-reference-note", "此记录尚无结构化引用目录审查；以下仅按当前模型快照精确核对键与原始值。"));
  if (!references.length) section.append(el("p", "simplification-evidence-gap", "此项尚未提供结构化几何引用，采用前需补充可核对的事实。"));
  references.forEach((reference) => {
    const card = el("div", "geometry-fact-reference"), ref = typeof reference === "string" ? reference : "";
    const fact = ref.startsWith("fact:") ? list(model?.facts).find((item) => item.key === ref.slice(5)) : null;
    const body = ref.startsWith("body:") ? list(model?.geometry?.bodies).find((item) => item.id === ref.slice(5)) : null;
    if (fact) {
      card.append(el("h6", "geometry-reference-title", fact.label || fact.key), el("code", "geometry-reference-id", ref));
      card.append(el("pre", "fact-value", typeof fact.value === "object" ? JSON.stringify(fact.value, null, 2) : valueText(fact.value)), el("p", "fact-source", `来源：${fact.source ? valueText(fact.source) : "当前快照未记录来源"}`));
    } else if (body) {
      card.append(el("h6", "geometry-reference-title", body.imported_name || "当前快照中的实体"), el("code", "geometry-reference-id", ref));
      const values = el("dl", "body-reference-values");
      const row = (label, value) => { const item = el("div"); item.append(el("dt", "", label), el("dd", "", value)); values.append(item); };
      row("实体 ID", body.id);
      row("包围盒尺寸", body.bounding_box ? boundingBoxSummary(body.bounding_box, model?.geometry?.kernel?.unit) : "未知");
      const volume = body.volume;
      row("体积", volume && typeof volume.value === "number" && Number.isFinite(volume.value) ? `约 ${new Intl.NumberFormat("zh-CN", { maximumSignificantDigits: 4 }).format(volume.value)} ${volume.unit || "（单位未记录）"}` : "未知");
      row("表面数", typeof body.surface_count === "number" && Number.isFinite(body.surface_count) ? String(body.surface_count) : "未知");
      card.append(values, el("p", "geometry-reference-note", "实体 ID 仅在当前快照范围有效，物理角色仍需确认。显示尺寸经过舍入，包围盒包含 CAD 边界容差。"), el("p", "fact-source", `来源：${body.source ? valueText(body.source) : "当前快照未记录来源"}`));
      const raw = el("details", "body-reference-raw"); raw.append(el("summary", "", "查看此实体原始记录"), el("pre", "raw-snapshot", JSON.stringify(body, null, 2))); card.append(raw);
    } else {
      card.classList.add("unresolved-reference"); card.append(el("h6", "geometry-reference-title", "不可核对的引用"), el("code", "geometry-reference-id", valueText(reference)), el("p", "simplification-evidence-gap", "当前快照中没有与此引用精确对应的事实键或实体 ID。请补充简化分析以绑定当前快照。"));
    }
    section.append(card);
  });
  container.append(section);
}
function renderSimplification(container, simplification, session = state.session, readOnly = false) {
  if (!simplification || typeof simplification !== "object" || !Object.keys(simplification).length) return;
  const section = el("section", "simplification-section");
  const heading = el("div", "simplification-section-heading"); heading.append(el("h4", "design-label", "模型简化指导"));
  if (!readOnly) { const supplement = el("button", "button button-subtle simplification-action", "补充简化分析"); supplement.type = "button"; supplement.addEventListener("click", supplementSimplification); heading.append(supplement); }
  const candidateCount = list(simplification.candidates).length;
  section.append(heading);
  if (candidateCount) section.append(el("p", "simplification-intro", "以下为待核查的建模建议，尚未修改 CAD。"));
  if (simplification.objective) section.append(el("p", "design-copy", simplification.objective));
  if (!candidateCount) {
    const facts = el("dl", "basis-facts");
    const row = (label, items) => { if (!list(items).length) return; facts.append(el("dt", "", label), el("dd", "", list(items).map(valueText).join("；"))); };
    row("应保留", simplification.protected_features); row("待检查几何", simplification.unresolved_geometry);
    section.append(el("p", "basis-note", "Agent 尚未提出具体的简化候选。")); if (facts.childElementCount) section.append(facts);
    container.append(section); return;
  }
  if (list(simplification.protected_features).length) {
    const protectedFeatures = el("div", "protected-features"); protectedFeatures.append(el("h5", "route-detail-label", "应保留的物理与几何特征"));
    const ul = el("ul", "route-list-text"); simplification.protected_features.forEach((item) => ul.append(el("li", "", item))); protectedFeatures.append(ul); section.append(protectedFeatures);
  }
  const statuses = { conditional: "有条件建议", needs_inspection: "待检查几何", keep: "建议保留" };
  const candidates = list(simplification.candidates), cards = el("div", "simplification-candidates");
  candidates.forEach((candidate, index) => {
    const card = el("details", "simplification-card"), summary = el("summary", "simplification-summary");
    const heading = el("span", "simplification-summary-heading"); heading.append(el("span", "simplification-target", candidate.target || `简化评估 ${index + 1}`), el("span", `badge ${candidate.status === "keep" ? "badge-neutral" : "badge-warn"}`, statuses[candidate.status] || candidate.status || "待核查"));
    summary.append(heading);
    if (candidate.action) summary.append(el("span", "simplification-summary-action", candidate.action));
    card.append(summary); const body = el("div", "simplification-body");
    function detail(label, value) { const row = el("div", "simplification-detail"); row.append(el("h5", "route-detail-label", label), el("p", "route-detail-copy", isNonBlank(value) ? valueText(value) : "尚未明确，需进一步核查。")); body.append(row); }
    function detailList(label, items, emptyText = "尚未明确，需进一步核查。") { const row = el("div", "simplification-detail"); row.append(el("h5", "route-detail-label", label)); if (list(items).length) { const ul = el("ul", "route-list-text"); items.forEach((item) => ul.append(el("li", "", valueText(item)))); row.append(ul); } else row.append(el("p", "simplification-evidence-gap", emptyText)); body.append(row); }
    detail("对象", candidate.target); detail("目标范围", ({ global: "整体", body: "实体", local_feature: "局部特征" })[candidate.target_scope] || "旧版建议未标注范围，可补充简化分析"); detail("建议处理", candidate.action); detail("为什么这样建议", candidate.rationale);
    renderGeometryFactReferences(body, candidate, session);
    detailList("解释与待检查假设", candidate.geometry_evidence, "尚未提供解释或待检查假设。");
    const references = el("div", "simplification-detail"); references.append(el("h5", "route-detail-label", "案例与文档依据")); appendCitations(references, candidate.source_ids, session);
    if (!references.querySelector(".citation")) references.append(el("p", "simplification-evidence-gap", "此项尚未关联可核查的案例或文档来源，需进一步核查适用性。")); body.append(references);
    detail("成立条件", candidate.applicability); detail("预期收益", candidate.expected_benefit);
    detailList("可能损失的物理效应或几何细节", candidate.lost_effects, "可能损失尚未明确，需在验证时评估。"); detailList("如何验证简化是否可接受", candidate.verification, "验证方式尚未明确，采用前需补充。");
    if (candidate.status === "needs_inspection") body.append(el("p", "simplification-pending", "待检查：先核对上述对象与几何证据，再决定是否采用此建议。"));
    if (candidate.requires_confirmation) body.append(el("p", "simplification-pending", "此项需要你的明确选择，才能作为后续几何处理的依据。"));
    card.append(body); cards.append(card);
  });
  if (cards.childElementCount) section.append(cards);
  else section.append(el("p", "inline-empty", "当前研究未列出具体简化候选，请先核对需要保留的特征与尚未明确的几何信息。"));
  if (list(simplification.unresolved_geometry).length) {
    const pending = el("div", "simplification-unresolved"); pending.append(el("h5", "route-detail-label", "仍需检查的几何信息")); const ul = el("ul", "route-list-text"); simplification.unresolved_geometry.forEach((item) => ul.append(el("li", "", item))); pending.append(ul); section.append(pending);
  }
  container.append(section);
}
function basisSection(container, title, className = "") { const section = el("section", `basis-section ${className}`.trim()); section.append(el("h3", "basis-title", title)); container.append(section); return section; }
function renderDesignBrief(container, session = state.session, readOnly = false) {
  const brief = session?.design_brief || session?.plan?.design_brief || {}, plan = session?.plan || {};
  if (plan.evidence_status && hasAgentPlan(session)) container.append(el("p", "basis-evidence", `依据状态：${plan.evidence_status}`));
  const routes = list(brief.candidate_routes);
  if (routes.length) {
    const section = basisSection(container, routes.length > 1 ? `路线对比（${routes.length} 条）` : "建模路线");
    const statuses = { candidate: ["备选", "badge-neutral"], recommended: ["推荐", "badge-good"], blocked: ["条件不满足", "badge-warn"] };
    const wrap = el("div", "basis-table-wrap"), table = el("table", "basis-table"), head = el("tr");
    head.append(el("th", "", "")); routes.forEach((route) => { const th = el("th"); th.scope = "col"; const [label, tone] = statuses[route.status] || [route.status || "待研究", "badge-neutral"]; th.append(el("span", "basis-route-name", route.name || route.id || "建模路线"), el("span", `badge ${tone}`, label)); head.append(th); });
    const thead = el("thead"); thead.append(head); const tbody = el("tbody");
    const rows = [["物理场", (route) => list(route.physics).join("、")], ["理由", (route) => route.rationale], ["取舍与代价", (route) => valueText(route.tradeoffs)], ["适用前提", (route) => list(route.assumptions)], ["还需要的输入", (route) => list(route.required_inputs)]];
    rows.forEach(([label, pick]) => {
      const values = routes.map(pick); if (!values.some((value) => Array.isArray(value) ? value.length : isNonBlank(value))) return;
      const tr = el("tr"), th = el("th", "", label); th.scope = "row"; tr.append(th);
      values.forEach((value) => { const td = el("td"); if (Array.isArray(value)) { if (value.length) { const ul = el("ul"); value.forEach((item) => ul.append(el("li", "", valueText(item)))); td.append(ul); } else td.append(el("span", "basis-empty", "—")); } else td.textContent = isNonBlank(value) ? value : "—"; tr.append(td); });
      tbody.append(tr);
    });
    table.append(thead, tbody); wrap.append(table); section.append(wrap);
  }
  const interpretations = list(brief.geometry_interpretation), assumptions = list(plan.assumptions);
  if (brief.domain_strategy || interpretations.length || assumptions.length) {
    const section = basisSection(container, "建模前提与假设");
    if (brief.domain_strategy) section.append(el("p", "basis-lead", brief.domain_strategy));
    const ul = el("ul", "basis-list");
    assumptions.forEach((item) => ul.append(el("li", "", valueText(item))));
    const labels = { observed: "Agent 引述 · 待核对", hypothesis: "几何假设 · 待验证", unknown: "几何 · 未知" };
    interpretations.forEach((item) => { const li = el("li", "basis-geometry"); li.append(el("span", `badge ${item.status === "observed" ? "badge-neutral" : "badge-warn"}`, labels[item.status] || "几何 · 待核对"), el("span", "", ` ${item.statement || ""}`)); if (item.evidence) li.append(el("span", "basis-evidence-note", `依据：${item.evidence}`)); ul.append(li); });
    if (ul.childElementCount) section.append(ul);
  }
  const decisions = list(brief.decisions);
  if (decisions.length) {
    const section = basisSection(container, "关键决策"), ul = el("ul", "basis-list basis-decisions");
    decisions.forEach((decision) => {
      const decisionQuestion = list(session?.questions).find((question) => question.id.startsWith("decision_") && question.label === `是否采用此建议：${decision.topic || "建模决策"}` && question.reason === `${decision.recommendation || ""}\n依据与理由：${decision.reason || ""}`);
      const accepted = decisionQuestion && session?.answers?.[decisionQuestion.id] === "accept";
      const li = el("li"), line = el("p", "basis-decision-line");
      line.append(el("strong", "", `${decision.topic || decision.id || "建模决策"}：`), el("span", "", decision.recommendation || "未给出建议"));
      if (decision.requires_confirmation) line.append(el("span", `badge ${accepted ? "badge-good" : "badge-warn"}`, accepted ? "已接受" : "待你确认"));
      li.append(line);
      const why = [decision.reason, list(decision.alternatives).length ? `其他选择：${decision.alternatives.join("；")}` : ""].filter(isNonBlank).join(" · ");
      if (why) li.append(el("p", "basis-evidence-note", why));
      appendCitations(li, decision.source_ids, session);
      if (decision.requires_confirmation && !readOnly) { const button = el("button", "text-button decision-intent-link", accepted ? "查看你的选择" : "去确认"); button.type = "button"; button.addEventListener("click", () => decisionQuestion ? revealQuestion(decisionQuestion.id) : selectTab("intent", true)); li.append(button); }
      ul.append(li);
    }); section.append(ul);
  }
  if (list(plan.difficulties).length) { const section = basisSection(container, "风险与难点"), ul = el("ul", "basis-list"); plan.difficulties.forEach((item) => ul.append(el("li", "", valueText(item)))); section.append(ul); }
  renderSimplification(container, brief.simplification, session, readOnly);
  if (!container.childElementCount) container.append(el("p", "inline-empty", hasAgentPlan(session) ? "此方案没有记录路线与依据。" : "Agent 规划后，这里会列出候选路线、建模前提和关键决策。"));
}
function showPlanView(name, focus = false) {
  state.planView = name === "design" ? "design" : "flow";
  ["flow", "design"].forEach((view) => { const tab = $(`plan-tab-${view}`), panel = $(`plan-view-${view}`); if (!tab || !panel) return; const selected = view === state.planView; tab.setAttribute("aria-selected", String(selected)); tab.tabIndex = selected ? 0 : -1; tab.classList.toggle("active", selected); panel.hidden = !selected; });
  if (focus) { $(`plan-tab-${state.planView}`)?.focus(); $("plan-view-tabs")?.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
}
function renderOverview(container) {
  const session = state.session, plan = session?.plan || {}, brief = session?.design_brief || plan.design_brief || {}, routes = list(brief.candidate_routes), route = routes.find((item) => item.status === "recommended") || routes[0];
  const section = el("section", "overview-section"), routeLine = el("div", "overview-route"); section.id = "plan-overview";
  if (route) {
    routeLine.append(el("h3", "", route.name || "待确认的建模路线"), el("span", `badge ${route.status === "blocked" ? "badge-warn" : "badge-neutral"}`, route.status === "blocked" ? "受阻 · 待补充" : route.status === "recommended" ? "Agent 推荐 · 待核对" : "候选 · 待确认"));
    const others = routes.filter((item) => item !== route);
    if (others.length) { const compare = el("button", "text-button", `另有 ${others.length} 条备选路线`); compare.type = "button"; compare.addEventListener("click", () => showPlanView("design", true)); routeLine.append(compare); }
  } else routeLine.append(el("h3", "", hasAgentPlan() ? "建模路线未注明" : "尚未形成建模路线"));
  section.append(routeLine);
  if (isNonBlank(plan.summary) && hasAgentPlan()) section.append(el("p", "plan-summary", plan.summary));
  else if (!hasAgentPlan()) section.append(el("p", "plan-summary", "目标与输入已保存。点击「Agent 规划方案」，Agent 会比较建模路线、查阅资料，并列出需要你确认的问题。"));
  const facts = el("dl", "overview-facts");
  const fact = (label, value) => { if (!isNonBlank(value)) return; const item = el("div"); item.append(el("dt", "", label), el("dd", "", valueText(value))); facts.append(item); };
  fact("目标", brief.objective); fact("物理场", list(route?.physics).join(" · "));
  fact("几何", session?.model ? (session.model.name || session.model.path || "").split(/[\\/]/).pop() : "");
  if (hasAgentPlan()) fact("依据", list(session?.sources).length ? `${list(session.sources).length} 个来源` : "暂无引用来源");
  if (facts.childElementCount) section.append(facts);
  section.append(renderSessionInputs(session));
  container.append(section); state.overviewNodes = {}; syncOverview();
}
function renderSessionInputs(session) {
  const inputs = el("details", "session-inputs"), summary = el("summary"), model = session?.model;
  summary.append(el("span", "session-inputs-title", "任务输入"), el("span", "session-inputs-hint", [model ? (model.file_name || "已读取模型") : "未附几何", "你的原始描述"].join(" · ")));
  const body = el("div", "session-inputs-body"), goal = el("section", "session-inputs-goal");
  goal.append(el("h4", "", "你的原始描述"), el("p", "", session?.goal || "未记录"));
  const geometry = el("section", "session-inputs-model"); geometry.append(el("h4", "", "几何与模型"));
  const modelBox = el("div", "model-content"); geometry.append(modelBox);
  if (model) renderModel(modelBox, model, false); else modelBox.append(el("p", "inline-empty", "建立任务时没有附几何或模型文件。"));
  const actions = el("div", "session-inputs-actions"), fork = el("button", "button button-subtle", "以此为基础新建任务"); fork.type = "button";
  fork.addEventListener("click", () => { const goalText = session?.goal || "", path = session?.model?.path || ""; newSession(); if (state.session) return; $("goal").value = goalText; $("model-path").value = path; $("goal").focus(); });
  actions.append(fork, el("p", "", "已建立的任务不能直接修改目标或几何。需要改动时，复制这些输入新建一个任务，原方案保留不变。"));
  body.append(goal, geometry, actions); inputs.append(summary, body);
  return inputs;
}
function syncOverview() { syncWorkflow(); }
function workflowState(session = state.session) {
  const planned = hasAgentPlan(session), required = list(session?.questions).filter((question) => question.required);
  const unresolved = required.filter((question) => !isQuestionResolved(question, questionValue(question)));
  const agentBusy = ["starting", "running"].includes(state.agent?.status) || Boolean(state.agent?.commandPending);
  const confirmed = session?.status === "confirmed" && !state.dirty;
  return { planned, required, unresolved, agentBusy, confirmed, researching: planned && isStale(session) };
}
function syncWorkflow() {
  const session = state.session, bar = $("stage-bar"), next = $("next-action"); if (!session || !bar || !next) return;
  const flow = workflowState(session);
  const stages = [
    { label: "输入", done: true },
    { label: "Agent 规划", done: flow.planned && !flow.researching, note: !flow.planned ? "" : flow.researching ? (session.plan_stale ? "待更新" : "未完成") : "" },
    { label: "回答问题", done: flow.planned && !flow.unresolved.length && !state.dirty, note: flow.required.length ? `${flow.required.length - flow.unresolved.length}/${flow.required.length}` : "" },
    { label: "确认方案", done: flow.confirmed },
    { label: "导出", done: false }];
  const refine = $("refine-plan");
  let label, run, disabled = Boolean(state.operation), hint = "";
  if (state.agent?.updateAvailable || state.agent?.refreshingPlan) { label = state.agent?.refreshingPlan ? "正在载入更新…" : "载入更新的方案"; run = () => agentPanel?.loadUpdate?.(); disabled = Boolean(state.agent?.refreshingPlan) || Boolean(state.operation); hint = state.dirty ? "未保存的回答会保留并与新版本合并" : "方案已在别处更新，载入后再核对与确认"; }
  else if (flow.agentBusy) { label = "Agent 处理中…"; disabled = true; run = null; hint = "可在 Agent 标签中补充说明"; }
  else if (!flow.planned) { label = agentActionLabel(); run = () => refine.click(); disabled = refine.disabled; hint = refine.title; }
  else if (state.dirty) { label = "保存回答"; run = () => $("intent-form").requestSubmit(); disabled = $("save-answers").disabled; }
  else if (flow.unresolved.length) { label = `回答 ${flow.unresolved.length} 个必填问题`; run = () => revealQuestion(flow.unresolved[0].id); disabled = false; }
  else if (flow.researching) { label = agentActionLabel(); run = () => refine.click(); disabled = refine.disabled; hint = refine.title; }
  else if (!flow.confirmed) { label = "确认方案"; run = () => $("confirm-session").click(); disabled = $("confirm-session").disabled; hint = $("confirmation-detail").textContent; }
  else { label = "导出 Markdown"; run = () => $("export-markdown").click(); disabled = false; }
  const current = flow.agentBusy || !flow.planned || (flow.researching && !state.dirty && !flow.unresolved.length) ? 1 : state.dirty || flow.unresolved.length ? 2 : !flow.confirmed ? 3 : 4;
  const key = JSON.stringify([stages, current]);
  if (state.stageKey !== key) {
    state.stageKey = key; bar.replaceChildren();
    stages.forEach((stage, index) => { const item = el("li", stage.done ? "done" : index === current ? "current" : ""), mark = el("span", "stage-mark", stage.done ? "✓" : String(index + 1)); mark.ariaHidden = "true"; item.append(mark, el("span", "", stage.label)); if (stage.note) item.append(el("span", "stage-note", stage.note)); if (index === current) item.ariaCurrent = "step"; item.append(el("span", "visually-hidden", stage.done ? "（已完成）" : index === current ? "（当前）" : "")); bar.append(item); });
  }
  textIfChanged(next, label); setIfChanged(next, "disabled", Boolean(disabled)); setIfChanged(next, "title", hint || ""); next.onclick = run;
  // The refine button stays available as a secondary action unless it is already the primary one.
  refine.hidden = run !== null && label === agentActionLabel();
}
function renderStepKeyPoints(container, step) {
  container.append(el("h4", "", "关键点与输入"));
  const points = el("dl", "flow-key-points");
  [["设置位置", step.location], ["作用区域", step.selection], ["所需输入", list(step.inputs).join("\n")]].forEach(([label, value]) => { if (!isNonBlank(value)) return; const row = el("div", "step-detail"); row.append(el("dt", "step-detail-label", label), el("dd", "step-detail-value", value)); points.append(row); });
  if (points.childElementCount) container.append(points); else container.lastChild?.remove();
}
function renderMeshGuidance(container, step) {
  const guidance = el("div", "mesh-guidance"), parts = [...String(step.action || "").split(/(?<=[。；;\n])\s*/u), ...list(step.inputs), ...list(step.checks)].map((value) => String(value).trim()).filter(Boolean);
  const groups = [["网格类型与分区", /扫掠|映射|四面体|六面体|三角形|网格类型|单元类型|三维实体参考网格|\b(?:swept|mapped|tetrahed\w*|hexahed\w*|mesh type)\b/i], ["尺度依据", /尺度|尺寸|厚度|间隙|圆角|足迹|扩散|\bsize\b/i], ["局部加密", /加密|细化|梯度|承压|窄截面|边缘|\brefin\w*\b/i], ["质量检查", /网格质量|单元质量|雅可比|畸变|扭曲|偏斜|纵横比|单元翻转|负体积|\b(?:quality|jacobian|skew\w*|aspect)\b/i], ["收敛与目标量比较", /收敛|趋于稳定|加密对照|误差|\bconverg\w*\b/i]];
  const buckets = groups.map(() => []);
  [...new Set(parts)].forEach((part) => { const index = [3, 4, 0, 2, 1].find((candidate) => groups[candidate][1].test(part)); if (index !== undefined) buckets[index].push(part); });
  if (!buckets.some((matches) => matches.length)) return false;
  const missing = groups.filter((_, index) => !buckets[index].length).map(([label]) => label);
  groups.forEach(([label], index) => { const matches = buckets[index]; if (!matches.length) return; const section = el("section", "mesh-guidance-section"); section.append(el("h4", "mesh-guidance-title", label)); const ul = el("ul", "simple-list"); matches.forEach((text) => ul.append(el("li", "", text))); section.append(ul); guidance.append(section); });
  if (missing.length) guidance.append(el("p", "mesh-guidance-empty", `未提及：${missing.join("、")}`));
  container.append(guidance); return true;
}
function renderSettingsDisclosure(container, settings, session, title, className, note = "") {
  if (!settings.length) return;
  const section = disclosure(`${title}（${settings.length} 项）`, className);
  container.append(section.details);
  lazyContent(section.details, () => {
    if (note) section.content.append(el("p", "flow-reading-note", note));
    section.content.append(el("p", "flow-reading-note", "以下为 Agent 整理的方案设置与来源记录；采用状态不代表已实施或已完成物理验证。"));
    const statuses = { adopted: "方案采用", adapted: "调整采用", alternative: "备选", unresolved: "待明确", not_applicable: "不适用" };
    const contexts = { source_text: "文档原文", case_configuration: "案例配置", navigation: "导航线索" };
    settings.forEach((setting, index) => {
      const row = el("section", "flow-section plan-setting"), heading = el("h4", "plan-setting-heading");
      heading.append(el("span", "", isNonBlank(setting.setting) ? valueText(setting.setting) : `设置 ${index + 1}`), el("span", "badge badge-neutral", statuses[setting.status] || valueText(setting.status)));
      row.append(heading);
      const values = el("dl", "flow-key-points plan-setting-values");
      const field = (label, value, required = false) => { if (!required && !isNonBlank(value)) return; const item = el("div", "step-detail"); item.append(el("dt", "step-detail-label", label), el("dd", "step-detail-value", isNonBlank(value) ? valueText(value) : "尚需补充")); values.append(item); };
      field("对应要求", setting.requirement, true); field("建议值", setting.proposed_value, true);
      field("迁移依据", setting.transfer_rationale); field("原因", setting.reason); field("缺口", setting.gap);
      row.append(values);
      const references = el("div", "plan-setting-sources"); references.append(el("h4", "", "来源记录"));
      const refs = list(setting.source_refs);
      if (!refs.length) references.append(el("p", "inline-empty", "未关联来源记录。"));
      refs.forEach((reference) => {
        if (!reference || typeof reference !== "object") return;
        const record = el("div", "plan-setting-source"), source = list(session?.sources).find((item) => item.id === reference.source_id);
        if (source && source.availability?.readable !== false) record.append(citationButton(reference.source_id, session));
        record.append(el("span", "", `${source?.title ? valueText(source.title) + " · " : ""}${valueText(reference.source_id)}`));
        if (source?.availability?.readable === false) record.append(el("span", "muted", " · 来源已关闭或删除，引用记录保留"));
        const evidence = el("dl", "flow-key-points");
        [["依据类型", contexts[reference.context] || valueText(reference.context)], ["定位", reference.locator], ["来源记载值", reference.stated_value]].forEach(([label, value]) => { const item = el("div", "step-detail"); item.append(el("dt", "step-detail-label", label), el("dd", "step-detail-value", valueText(value))); evidence.append(item); });
        record.append(evidence); references.append(record);
      });
      row.append(references); section.content.append(row);
    });
  });
}
function renderStepSettings(container, step, session = state.session) {
  if (!isNonBlank(step.id)) return;
  const settings = list(session?.plan_settings).filter((row) => row && list(row.step_ids).includes(step.id));
  renderSettingsDisclosure(container, settings, session, "设置与依据", "flow-settings");
}
function renderUnmappedSettings(container, steps, session = state.session) {
  const stepIds = new Set(steps.map((step) => step.id).filter(isNonBlank));
  const settings = list(session?.plan_settings).filter((row) => row && !list(row.step_ids).some((id) => stepIds.has(id)));
  renderSettingsDisclosure(container, settings, session, "未采用与待处理设置", "flow-unmapped-settings", "以下记录尚未对应到当前流程环节，保留原记录与状态供核对。");
}
function renderSimulationFlow(container, steps, session = state.session) {
  if (!steps.length) { container.append(el("p", "inline-empty", "尚未形成实施流程。先完成 Agent 规划，再逐环节核对。")); renderUnmappedSettings(container, steps, session); return; }
  container.append(el("h3", "plan-block-title", `建模流程 · ${steps.length} 步`));
  const flow = el("ol", "simulation-flow"), reuse = session === state.session, previous = reuse ? new Map(flowNodeCache) : null;
  if (reuse) flowNodeCache.clear();
  const sourceState = JSON.stringify(list(session?.sources).map((source) => [source.id, source.availability?.readable])), stepTitles = JSON.stringify(steps.map((item) => [item.id, item.title]));
  steps.forEach((step, index) => {
    const nodeKey = JSON.stringify([step, index, session?.plan_stale, sourceState, stepTitles, list(session?.plan_settings).filter((row) => list(row?.step_ids).includes(step.id))]);
    if (reuse && previous.has(nodeKey)) { const kept = previous.get(nodeKey); flowNodeCache.set(nodeKey, kept); flow.append(kept); return; }
    const mesh = /网格|\bmesh\b/i.test([step.title, step.location, step.selection].filter(Boolean).join(" "));
    const item = el("li", "flow-node"); item.dataset.stepId = step.id || String(index + 1); if (mesh) item.dataset.stage = "mesh";
    const details = el("details", "flow-node-details"), summary = el("summary", "flow-node-summary"), heading = el("span", "flow-node-heading");
    heading.append(el("strong", "flow-node-title", step.title || `步骤 ${index + 1}`)); if (step.purpose) heading.append(el("span", "flow-node-purpose", compactSentence(step.purpose, 38)));
    summary.append(el("span", "flow-node-number", index + 1), heading, el("span", "badge badge-neutral flow-node-status", session?.plan_stale ? "待更新" : step.difficulty ? "需核查" : "待实施"));
    const body = el("div", "flow-node-body"); if (step.purpose && compactSentence(step.purpose, 38) !== step.purpose) body.append(el("p", "step-purpose", step.purpose));
    let original = body;
    if (mesh && renderMeshGuidance(body, step)) { const full = disclosure("完整实施指南与输入", "flow-original"); body.append(full.details); original = full.content; }
    const action = el("section", "flow-section"); action.append(el("h4", "", "实施指南"), el("p", "step-action", step.action || "Agent 未给出操作说明。")); original.append(action); renderStepKeyPoints(original, step);
    if (isNonBlank(step.difficulty)) { const difficulty = el("section", "flow-section flow-difficulty"); difficulty.append(el("h4", "", "难点"), el("p", "flow-difficulty-copy", step.difficulty)); body.append(difficulty); }
    if (list(step.checks).length) { const checks = el("section", "flow-section"); checks.append(el("h4", "", "验收与检查")); const ul = el("ul", "step-checks"); step.checks.forEach((check) => ul.append(el("li", "", check))); checks.append(ul); (original === body ? body : original).append(checks); }
    renderStepSettings(body, step, session);
    const dependencies = el("div", "flow-dependencies"); dependencies.append(el("span", "", "前置步骤："));
    list(step.depends_on).forEach((id) => { const dependency = steps.find((candidate) => candidate.id === id), link = el("button", "citation flow-dependency", dependency ? `${id} · ${dependency.title}` : `${id}（未找到对应节点）`); link.type = "button"; link.addEventListener("click", () => { const target = Array.from((item.parentElement || flow).children).find((node) => node.dataset.stepId === id); if (!target) return; target.querySelector("details").open = true; target.scrollIntoView({ behavior: "smooth", block: "center" }); target.querySelector("summary")?.focus({ preventScroll: true }); }); dependencies.append(link); }); if (list(step.depends_on).length) body.append(dependencies); appendCitations(body, step.source_ids, session);
    details.append(summary, body); item.append(details); flow.append(item); if (reuse) flowNodeCache.set(nodeKey, item);
  }); container.append(flow); renderUnmappedSettings(container, steps, session);
}
// Agent saves can arrive every few seconds; rebuilding the whole plan replaced the element under the
// pointer (hover flicker) and collapsed whatever the user had opened. Skip unchanged plans, reuse
// unchanged step nodes and carry open disclosures across a rebuild.
let planRenderKey = null;
const flowNodeCache = new Map();
function planContentKey(session) {
  const answers = Object.fromEntries(list(session?.questions).filter((question) => question.id.startsWith("decision_")).map((question) => [question.id, session.answers?.[question.id]]));
  return JSON.stringify([session?.id, session?.plan, session?.design_brief, session?.model, session?.plan_settings, session?.research_trace, session?.plan_stale, session?.planning_incomplete, session?.status,
    list(session?.sources).map((source) => [source.id, source.title, source.availability?.readable]), list(session?.questions).map((question) => [question.id, question.label, question.reason]), answers]);
}
function openDisclosureKeys(container) {
  return new Set(Array.from(container.querySelectorAll("details[open]")).filter((details) => !details.closest(".flow-node")).map((details) => details.id || details.querySelector("summary")?.textContent?.trim()).filter(Boolean));
}
function renderPlan() {
  const container = $("plan-content"), key = planContentKey(state.session);
  if (key === planRenderKey && container.childElementCount) { renderPlanGaps(); renderResearchTrace(); return; }
  planRenderKey = key;
  const reopen = openDisclosureKeys(container);
  container.replaceChildren(); const plan = state.session?.plan || {}; state.overviewNodes = null;
  renderOverview(container);
  const tabs = el("div", "plan-view-tabs"); tabs.id = "plan-view-tabs"; tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "方案内容");
  const views = [["flow", `建模流程${list(plan.steps).length ? ` · ${list(plan.steps).length} 步` : ""}`], ["design", "路线与依据"]];
  views.forEach(([name, label], index) => { const tab = el("button", "plan-view-tab", label); tab.type = "button"; tab.id = `plan-tab-${name}`; tab.setAttribute("role", "tab"); tab.setAttribute("aria-controls", `plan-view-${name}`); tab.addEventListener("click", () => showPlanView(name)); tab.addEventListener("keydown", (event) => { if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return; event.preventDefault(); showPlanView(views[(index + 1) % 2][0], true); }); tabs.append(tab); });
  const flow = el("section", "plan-view"), design = el("section", "plan-view");
  flow.id = "plan-view-flow"; design.id = "plan-view-design"; [flow, design].forEach((panel, index) => { panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", `plan-tab-${views[index][0]}`); });
  renderSimulationFlow(flow, list(plan.steps)); renderDesignBrief(design);
  container.append(tabs, flow, design); showPlanView(state.planView);
  container.querySelectorAll("details").forEach((details) => { if (!details.closest(".flow-node") && reopen.has(details.id || details.querySelector("summary")?.textContent?.trim())) details.open = true; });
  renderPlanGaps(); renderResearchTrace();
}
function renderPlanGaps() {
  const container = $("plan-gaps"); if (!container) return; container.replaceChildren();
  const items = list(state.session?.plan?.missing_information).filter(isNonBlank); container.hidden = !items.length; if (!items.length) return;
  container.append(el("h3", "", `Agent 标注的待补充信息（${items.length}）`), el("p", "side-intro", "规划时 Agent 记下的缺口。其中一部分已经变成上面的问题；缺少资料依据的，可以直接检索。"));
  const ul = el("ul", "gap-list");
  items.forEach((item) => { const li = el("li"); li.append(el("span", "", valueText(item))); if (/依据|手册|文献|资料|来源|案例/.test(valueText(item))) { const search = el("button", "text-button", "检索资料"); search.type = "button"; search.addEventListener("click", () => { openSearch(); $("search-query").value = valueText(item).replace(/^缺少|的?(手册|文献|资料)?依据$/g, ""); }); li.append(search); } ul.append(li); });
  container.append(ul);
}
const traceStages = { conversation: "Agent 规划", interactive_edit: "修改步骤", search: "检索资料", read: "阅读来源", retrieve: "读取来源", formulation: "形成检索问题", questions: "整理问题", validation: "校验方案", readiness: "就绪检查", evidence_feedback: "依据反馈", research_budget: "研究预算", source_budget: "来源预算" };
const traceStatuses = { completed: "完成", blocked_evidence: "依据不足", draft_validated: "草稿校验通过", steps_updated: "已更新步骤", budget_exhausted: "预算用尽", source_budget_exhausted: "来源数已达上限", failed: "失败", blocked: "受阻", unavailable: "来源不可用", rejected_invalid_provenance: "来源出处无效", rejected_empty_context: "上下文为空", partial_draft_retained: "保留部分草稿", research_requested: "请求补充研究", hypotheses: "形成假设", needs_replan: "需要重新规划", retired: "已撤回" };
let researchTraceKey = null;
function renderResearchTrace() {
  const container = $("research-trace"); if (!container) return;
  const raw = state.session?.research_trace ?? state.session?.plan?.research_trace, key = JSON.stringify([state.session?.id, raw ?? null]);
  if (key === researchTraceKey && container.childElementCount) return; researchTraceKey = key;
  const rawOpen = Boolean(container.querySelector?.("details.trace-raw[open]")); container.replaceChildren();
  const entries = Array.isArray(raw) ? raw.filter((item) => item && typeof item === "object") : [];
  container.append(el("h3", "", "研究轨迹"));
  if (!entries.length) { container.append(el("p", "inline-empty", typeof raw === "string" && raw.trim() ? raw : "这一版还没有研究记录。")); return; }
  const ol = el("ol", "trace-list");
  entries.forEach((entry) => {
    const li = el("li", `trace-item${/blocked|failed|exhausted|rejected|unavailable/.test(entry.status || "") ? " warn" : ""}`), head = el("p", "trace-head");
    head.append(el("strong", "", traceStages[entry.stage] || entry.stage || "记录"), el("span", "", traceStatuses[entry.status] || entry.status || ""));
    li.append(head);
    const facts = [];
    if (isNonBlank(entry.query)) facts.push(`检索“${entry.query}”${entry.kind ? `（${({ manual: "手册", case: "案例", code: "代码", all: "全部" })[entry.kind] || entry.kind}）` : ""}`);
    if (Array.isArray(entry.result_ids)) facts.push(`命中 ${entry.result_ids.length} 条`);
    if (isNonBlank(entry.source_id)) facts.push(`来源 ${entry.source_id}`);
    if (Number.isFinite(entry.source_count)) facts.push(`引用 ${entry.source_count} 个来源`);
    if (entry.physical_validation === "not_run") facts.push("未做物理验证");
    if (facts.length) li.append(el("p", "trace-facts", facts.join(" · ")));
    if (isNonBlank(entry.reason)) li.append(el("p", "trace-facts", entry.reason));
    if (list(entry.evidence_gaps).length) li.append(el("p", "trace-gaps", `缺口：${entry.evidence_gaps.map(valueText).join("；")}`));
    ol.append(li);
  });
  container.append(ol);
  const details = disclosure("原始记录（排查用）", "trace-raw"); details.content.append(el("pre", "raw-snapshot", JSON.stringify(raw, null, 2))); details.details.open = rawOpen; container.append(details.details);
}
function renderSources() {
  const sources = list(state.session?.sources), container = $("source-list"); container.replaceChildren(); $("source-count").textContent = String(sources.length);
  if (!sources.length) { container.append(el("p", "inline-empty", hasAgentPlan() ? "当前方案未关联可核查文档。请核对 Agent 标注的推断与待研究项。" : "Agent 规划时会根据建模问题查阅依据，并将实际引用的文档显示在这里。")); return; }
  sources.forEach((source, index) => {
    const button = el("button", "source-card"); button.type = "button"; const top = el("span", "source-topline"); top.append(el("span", "source-index", index + 1), el("span", "", kindLabels[source.kind] || source.kind || "文档"));
    button.append(top, el("h4", "", source.title || source.id));
    button.disabled = source.availability?.readable === false;
    button.append(el("span", "source-card-foot", button.disabled ? "资料库已关闭或删除，引用记录保留" : source.version ? `版本 ${source.version}` : "版本未报告 · 打开查看依据"));
    if (button.disabled) button.title = "资料库已关闭或删除，引用记录保留";
    else button.addEventListener("click", () => showSource(source.id)); container.append(button);
  });
}
function questionValue(question) { return normalizedAnswer(question, state.draftAnswers[question.id]); }
function updateDirty() {
  state.dirty = list(state.session?.questions).some((question) => questionValue(question) !== normalizedAnswer(question, state.session?.answers?.[question.id]));
  agentPanel?.bindSession();
}
let questionsRenderKey = null;
function renderQuestions() {
  const container = $("question-list"), session = state.session;
  const key = JSON.stringify([session?.id, session?.questions ?? null, session?.answers ?? null, state.draftAnswers, hasAgentPlan()]);
  if (key === questionsRenderKey && container.childElementCount) { syncConfirmation(); return; }
  questionsRenderKey = key;
  const reopen = new Set(Array.from(container.querySelectorAll?.("details[open]") || []).map((details) => details.dataset?.group).filter(Boolean));
  container.replaceChildren(); state.questionGroups = []; $("intent-form").noValidate = true;
  const questions = list(state.session?.questions), entries = questions.map((question, index) => ({ question, index }));
  if (!questions.length) container.append(el("p", "inline-empty", hasAgentPlan() ? "当前方案未提出额外意图问题。请核对建模路线、前提与来源。" : "Agent 尚未提出建模问题。请先启动规划，让 Agent 结合你的几何与目标识别需要明确的选择。"));
  const resolved = (entry) => isQuestionResolved(entry.question, questionValue(entry.question));
  const answered = entries.filter(resolved), pending = entries.filter((entry) => !resolved(entry)), information = pending.filter((entry) => !entry.question.id.startsWith("decision_")), decisions = pending.filter((entry) => entry.question.id.startsWith("decision_"));
  const priority = information.filter((entry) => entry.question.required).slice(0, 3), priorityIds = new Set(priority.map((entry) => entry.question.id)), others = information.filter((entry) => !priorityIds.has(entry.question.id));
  function addGroup(groupEntries, title, primary = false) {
    if (!groupEntries.length) return;
    const wrapper = primary ? el("section", "question-priority") : el("details", "disclosure-section question-group"), summary = el(primary ? "h3" : "summary", "", title), content = el("div", "question-group-content");
    wrapper.append(summary);
    if (!primary) { if (wrapper.dataset) wrapper.dataset.group = title; if (reopen.has(title)) wrapper.open = true; }
    if (primary) wrapper.append(el("p", "question-group-note", "先展示这几项，完整问题仍保留在下方；填写它们不代表已满足全部确认条件。"));
    wrapper.append(content); container.append(wrapper); state.questionGroups.push({ entries: groupEntries, title, summary, primary });
    groupEntries.forEach((entry) => content.append(renderField(entry.question, entry.index)));
  }
  function renderField(question, index) {
    const field = el("div", "question-field"), label = el("label", "", question.label || question.id), id = `answer-${index}`;
    field.dataset.questionId = question.id;
    label.htmlFor = id;
    if (question.required) label.append(el("span", "required-mark", "必填")); else label.append(el("span", "required-mark", "可选"));
    const reason = el("p", "question-reason", question.reason || "请根据你的实际建模意图填写。"); reason.id = `${id}-reason`;
    let input;
    if (question.kind === "select") {
      input = el("select"); const empty = el("option", "", "请选择，尚未回答"); empty.value = ""; input.append(empty);
      list(question.options).forEach((option) => { const item = el("option", "", option.label || option.value); item.value = String(option.value); input.append(item); });
    } else if (question.kind === "number") { input = el("input"); input.type = "number"; input.step = "any"; input.placeholder = "尚未回答"; }
    else { input = el("textarea"); input.rows = 3; input.placeholder = "尚未回答 · 请说明数值、单位、区域与数据来源"; }
    input.id = id; input.name = question.id; input.dataset.questionId = question.id; input.setAttribute("aria-required", String(Boolean(question.required))); input.setAttribute("aria-describedby", `${id}-reason ${id}-status`);
    input.value = questionValue(question); input.disabled = Boolean(state.operation);
    const status = el("span", "question-answer-status"); status.id = `${id}-status`;
    input.addEventListener("input", () => { state.draftAnswers[question.id] = input.value; updateDirty(); updateQuestionStatus(question, field, status); syncConfirmation(); syncRefine(); });
    field.append(label, reason, input, status); updateQuestionStatus(question, field, status); return field;
  }
  addGroup(priority, "先补充的资料", true); addGroup(others, "其他资料问题"); addGroup(decisions, "方案与简化确认"); addGroup(answered, "已回答的问题");
  syncConfirmation();
}
function syncQuestionGroupCounts() {
  state.questionGroups.forEach((group) => { const unresolved = group.entries.filter(({ question }) => question.required && !isQuestionResolved(question, questionValue(question))).length; textIfChanged(group.summary, `${group.title}（${group.entries.length} 项${unresolved ? ` · ${unresolved} 项必填待明确` : ""}）`); });
}
function updateQuestionStatus(question, field, status) {
  const answered = isQuestionResolved(question, questionValue(question)); field.classList.toggle("unanswered", !answered);
  const changed = questionValue(question) !== normalizedAnswer(question, state.session?.answers?.[question.id]);
  const pending = isNonBlank(questionValue(question)) && !answered;
  status.textContent = pending ? (question.id.startsWith("decision_") && questionValue(question) === "revise" ? changed ? "已选择需要调整 · 请保存后让 Agent 重新分析" : "已保存调整意图 · 等待 Agent 重新分析" : changed ? "已填写待定 · 尚未保存，仍需明确" : "已保存待定回答 · 仍需明确后才能确认") : !answered ? "未回答 · 仍保留为未知" : changed ? "已填写 · 尚未保存" : "已回答 · 已保存在当前版本";
}
function syncConfirmation() {
  const session = state.session, questions = list(session?.questions), required = questions.filter((question) => question.required), unresolved = required.filter((question) => !isQuestionResolved(question, questionValue(question)));
  const agentBusy = ["starting", "running"].includes(state.agent?.status) || state.agent?.commandPending;
  $("question-count").textContent = String(unresolved.length); $("question-count").hidden = unresolved.length === 0;
  $("intent-progress").textContent = `必填已回答 ${required.length - unresolved.length} / ${required.length}`;
  $("save-answers").disabled = !session || Boolean(state.operation) || !state.dirty;
  $("confirm-session").disabled = !session || Boolean(state.operation) || agentBusy || state.agent?.updateAvailable || state.agent?.refreshingPlan || state.dirty || unresolved.length > 0 || !hasAgentPlan(session) || isStale(session) || session.status === "confirmed";
  $("confirm-session").textContent = session?.status === "confirmed" ? "当前版本已确认 ✓" : "确认意图与当前方案 ✓";
  let title = "等待确认", detail = "请先回答必填问题并保存。";
  if (state.dirty) { title = "回答尚未保存"; detail = "请先保存明确选择；涉及工况或建模路线的变化需要 Agent 更新方案。"; }
  else if (agentBusy) { title = "Agent 正在处理当前任务"; detail = "可在对话中补充说明；等方案更新后，再核对并确认当前版本。"; }
  else if (state.agent?.updateAvailable || state.agent?.refreshingPlan) { title = "方案有更新 · 待刷新核对"; detail = "读取更新后的流程并核对关键选择，再确认当前方案。"; }
  else if (session?.status === "confirmed") { title = `r${session.revision} 已明确确认`; detail = "后续回答或方案深化会创建新版本，并需要重新确认。"; }
  else if (session && !hasAgentPlan(session)) { title = "等待 Agent 设计建模方案"; detail = "任务与输入已保存。先规划物理模型与路线，再明确关键问题并确认方案。"; }
  else if (session?.planning_incomplete) { title = "研究尚未完成 · 请继续规划"; detail = "当前分析与研究轨迹已保留。请让 Agent 继续规划，完成后再核对并确认。"; }
  else if (isStale(session)) { title = "意图已保存 · 方案待重新规划"; detail = "上一版方案供比较。请点击「Agent 重新规划」，完成后再核对并确认。"; }
  else if (unresolved.length) { title = `还有 ${unresolved.length} 个必填问题尚未明确`; detail = "未回答或选择待定的信息仍为未知；明确并保存后可确认意图与方案。"; }
  else if (session) { title = `r${session.revision} 等待你的确认`; detail = "请核对步骤、来源与回答，再明确确认当前版本。"; }
  $("confirmation-title").textContent = title; $("confirmation-detail").textContent = detail;
  document.querySelectorAll("#question-list input, #question-list select, #question-list textarea").forEach((input) => { input.disabled = Boolean(state.operation); });
  syncQuestionGroupCounts(); syncOverview(); if (session) renderPlanningState();
}
function syncRefine() {
  const available = Boolean(agentPanel), sending = Boolean(state.agent?.commandPending);
  document.querySelectorAll(".agent-plan-action").forEach((button) => {
    setIfChanged(button, "disabled", !state.session || Boolean(state.operation) || sending || state.dirty || !available);
    textIfChanged(button, `${agentActionLabel()} ✧`);
    setIfChanged(button, "title", state.dirty ? "请先保存回答，再让 Agent 规划" : available ? "将规划任务发送到当前 Agent 对话；运行中可继续补充说明，流程更新后需重新核对" : "Agent 对话面板尚未加载，请刷新页面");
  });
  document.querySelectorAll(".simplification-action").forEach((button) => {
    setIfChanged(button, "disabled", !canSupplementSimplification());
    setIfChanged(button, "title", !hasAgentPlan() ? "先由 Agent 完成主要建模方案，再补充简化分析" : !hasRouteDesign() ? "旧版方案缺少建模路线设计，请先整体重新规划" : state.session?.plan_stale ? "建模输入已变化，请先整体重新规划" : state.dirty ? "请先保存回答，再补充简化分析" : available ? "向当前 Agent 对话发送模型简化任务；完成后需要核对新版本" : "Agent 对话面板尚未加载，请刷新页面");
  });
  syncWorkflow();
}
function hasRouteDesign() { return isNonBlank(state.session?.design_brief?.objective) && list(state.session?.design_brief?.candidate_routes).length > 0; }
function canSupplementSimplification() { return Boolean(state.session && hasAgentPlan() && hasRouteDesign() && !state.session.plan_stale && !state.operation && !state.dirty && agentPanel && !state.agent?.commandPending); }
async function saveAnswers(event) {
  event.preventDefault(); if (!state.session || !state.dirty) return;
  for (const input of document.querySelectorAll("#question-list input[type=number]")) { if (!input.validity.valid) { revealQuestion(input.dataset.questionId); input.reportValidity(); return; } }
  await runOperation("answers", "正在保存回答", "保存明确选择与新版本，核对当前方案的确认条件。", async () => {
    const answers = {}; list(state.session.questions).forEach((question) => { answers[question.id] = String(questionValue(question)); });
    const session = await post(`/api/sessions/${encodeURIComponent(state.session.id)}/answers`, { revision: state.session.revision, answers }); adoptSession(session);
    notice(isStale(session) ? `回答已保存 · r${session.revision}。请让 Agent 根据新意图重新规划，再确认更新后的方案。` : `回答已保存 · r${session.revision}。请核对建模路线与其余关键问题，再确认当前方案。`); await refreshStatus(true);
  });
}
async function confirmSession() {
  if (!state.session || $("confirm-session").disabled) return;
  await runOperation("confirm", "正在确认当前方案", "核验必填回答并记录明确确认。", async () => {
    const session = await post(`/api/sessions/${encodeURIComponent(state.session.id)}/confirm`, { revision: state.session.revision }); adoptSession(session);
    notice(`方案 r${session.revision} 已确认并保存在本地。求解与物理验证仍需在 COMSOL 中执行。`); await refreshStatus(true);
  });
}
async function refinePlan() {
  if (!state.session || $("refine-plan").disabled) return;
  const id = state.session.id;
  const text = `${hasAgentPlan() ? "请根据当前已保存的输入与回答，重新核对并更新仿真建模方案。" : "请根据当前已保存的目标与几何解析，设计仿真建模方案。"}请研究真实资料并比较物理路线，更新会话中的结构化方案与流程，说明各环节关键点、难点、网格构建与验证方式，保留未确定条件和需要我回答的问题。本次只做建模规划，不执行求解。`;
  if (await agentPanel?.sendTask(text)) { if (state.session?.id !== id) return; selectTab("agent"); notice("规划任务已发送到当前 Agent 对话。回复和流程更新会自动显示，可继续补充说明。"); }
}
async function supplementSimplification() {
  if (!canSupplementSimplification()) return;
  const id = state.session.id;
  if (await agentPanel?.sendTask("请基于当前已保存的模型解析、目标与建模路线，结合真实相近案例补充模型简化分析，并更新会话中的结构化简化指导。逐项说明对象、可核对几何事实、成立条件、收益、可能损失与验证方式；未定位特征保持待检查，不把建议说成已修改CAD。本次只分析和规划，不修改模型或求解。")) { if (state.session?.id !== id) return; selectTab("plan"); notice("简化分析任务已发送到当前 Agent 对话。请等待回复和方案更新，再核对新建议。"); }
}
let historyRenderKey = null;
function renderHistory() {
  const container = $("history-list"), view = versionsState?.sessionId === state.session?.id ? versionsState : null;
  const key = JSON.stringify([state.session?.id, state.session?.revision, state.session?.history ?? null, view?.listBusy, view?.listError, view?.loadedRevision, view?.loadedRevision === state.session?.revision ? view?.versions : null]);
  if (key === historyRenderKey && $("history-operations").childElementCount) return; historyRenderKey = key;
  container.replaceChildren();
  textIfChanged($("history-status"), view?.listBusy ? "正在读取版本列表…" : view?.listError || (view?.loadedRevision === state.session?.revision ? "点击有快照的版本，只读查看完整方案。" : "打开「方案版本」或点击刷新，读取可用历史快照。"));
  if (view?.loadedRevision === state.session?.revision) renderVersionItems(container, view.versions);
  const operations = $("history-operations"); operations.replaceChildren();
  const entries = [...list(state.session?.history)].reverse();
  if (!entries.length) operations.append(el("li", "inline-empty", "暂无操作记录。"));
  entries.forEach((entry) => { const item = el("li", "history-item"), heading = el("div", "history-item-head"); heading.append(el("strong", "", `r${entry.revision}`), el("span", "", entry.event === "simplification_refined" ? "更新简化指导" : entry.event || "更新")); const time = el("time", "", dateText(entry.at)); if (entry.at) time.dateTime = entry.at; heading.append(time); item.append(heading, el("p", "", entry.summary || "方案已保存")); operations.append(item); });
}
function resetVersions() {
  const old = versionsState; versionsState = null;
  old?.listController?.abort(); old?.previewController?.abort();
  if ($("versions-dialog")?.open) $("versions-dialog").close();
  $("version-preview")?.replaceChildren?.(el("p", "inline-empty", "选择一个有快照的版本查看完整方案。")); $("versions-list")?.replaceChildren?.();
  if ($("version-restore-confirmation")) setIfChanged($("version-restore-confirmation"), "hidden", true);
}
function versionContext() {
  if (!state.session) return null;
  if (versionsState?.sessionId !== state.session.id) { resetVersions(); versionsState = {sessionId: state.session.id, versions: [], loadedRevision: null, currentRevision: null, listError: "", listBusy: false, previewSequence: 0, snapshot: null, selected: null, error: "", confirming: false, restoring: false}; }
  return versionsState;
}
function currentVersionContext(view) { return Boolean(view && view === versionsState && view.sessionId === state.session?.id && !state.deletedSessionIds.has(view.sessionId)); }
function renderVersionItems(container, versions) {
  if (!versions.length) { container.append(el("li", "inline-empty", "暂无版本记录。")); return; }
  versions.forEach((version) => {
    const item = el("li", `version-item${version.current ? " current" : ""}`), button = el("button", "version-preview-action", `r${version.revision}${version.current ? " · 当前" : ""}`); button.type = "button"; button.disabled = version.available !== true;
    if (!button.disabled) button.addEventListener("click", () => previewVersion(version.revision));
    item.append(button, el("p", "", version.summary || version.event || "方案已保存"), el("time", "", dateText(version.at)), el("p", "muted", version.available === true ? "可查看完整快照" : "仅操作记录，未保存快照")); container.append(item);
  });
}
function renderVersionList() {
  const view = versionsState; if (!currentVersionContext(view)) return;
  textIfChanged($("versions-status"), view.listBusy ? "正在读取版本列表…" : view.listError || `当前方案 r${view.currentRevision ?? state.session.revision} · 历史预览只读，不改变当前回答或 Agent 绑定。`);
  $("versions-list").replaceChildren(); renderVersionItems($("versions-list"), view.versions); renderHistory(); syncVersionControls();
}
async function loadVersions(force = false) {
  const view = versionContext(); if (!view) return;
  if (view.listPromise) return view.listPromise;
  if (!force && view.loadedRevision === state.session.revision) { renderVersionList(); return; }
  const revision = state.session.revision, controller = new AbortController(); view.listController = controller; view.listBusy = true; view.listError = ""; renderVersionList();
  view.listPromise = (async () => {
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(view.sessionId)}/versions`, {signal: controller.signal});
      if (!currentVersionContext(view)) return;
      if (data.session_id !== view.sessionId || !Number.isInteger(data.current_revision)) throw new Error("版本列表与当前方案不一致，请刷新后重试。");
      view.versions = list(data.versions).filter(item => Number.isInteger(item.revision) && item.revision > 0).sort((a, b) => b.revision - a.revision);
      view.loadedRevision = revision; view.currentRevision = data.current_revision;
    } catch (error) { if (currentVersionContext(view) && error.name !== "AbortError") view.listError = errorMessage(error); }
    finally { if (currentVersionContext(view)) { view.listBusy = false; view.listController = null; view.listPromise = null; renderVersionList(); } }
  })();
  return view.listPromise;
}
async function openVersions() { if (!state.session) return; versionContext(); openDialog("versions-dialog"); await loadVersions(); }
function closeVersionPreview() {
  const view = versionsState; if (!view) return;
  view.listController?.abort(); view.previewController?.abort(); view.previewSequence++; view.snapshot = null; view.selected = null; view.previewBusy = false; view.confirming = false; view.error = "";
  $("version-preview").replaceChildren(el("p", "inline-empty", "选择一个有快照的版本查看完整方案。")); syncVersionControls();
}
async function previewVersion(revision) {
  const view = versionContext(); if (!view || !view.versions.some(item => item.revision === revision && item.available === true)) return;
  openDialog("versions-dialog");
  if (view.snapshot?.revision === revision) { renderVersionPreview(view.snapshot); syncVersionControls(); return; }
  view.previewController?.abort(); const controller = new AbortController(), sequence = ++view.previewSequence; view.previewController = controller;
  view.selected = revision; view.snapshot = null; view.previewBusy = true; view.confirming = false; view.error = "";
  $("version-preview").replaceChildren(el("p", "inline-empty", `正在读取 r${revision} 的只读快照…`)); syncVersionControls();
  try {
    const snapshot = await api(`/api/sessions/${encodeURIComponent(view.sessionId)}/versions/${revision}`, {signal: controller.signal});
    if (!currentVersionContext(view) || sequence !== view.previewSequence) return;
    if (snapshot.id !== view.sessionId || snapshot.revision !== revision) throw new Error("服务返回的快照与所选版本不一致。");
    view.snapshot = snapshot; renderVersionPreview(snapshot);
  } catch (error) { if (currentVersionContext(view) && sequence === view.previewSequence && error.name !== "AbortError") view.error = errorMessage(error); }
  finally { if (currentVersionContext(view) && sequence === view.previewSequence) { view.previewBusy = false; view.previewController = null; syncVersionControls(); } }
}
function renderVersionPreview(snapshot) {
  const container = $("version-preview"), plan = snapshot.plan || {}; container.replaceChildren();
  container.append(el("h3", "", `r${snapshot.revision} · ${plan.title || snapshot.title || "仿真方案"}`), el("p", "version-readonly-note", `历史快照 · 只读。${snapshot.planning_incomplete ? "该版本研究尚未完成。" : ""}方案不代表已执行建模、求解或物理验证。`));
  if (snapshot.goal) container.append(el("h4", "", "该版本目标"), el("p", "design-copy", snapshot.goal));
  if (plan.summary) container.append(el("p", "plan-summary", plan.summary));
  renderSimulationFlow(container, list(plan.steps), snapshot);
  const design = disclosure("路线与依据"); renderDesignBrief(design.content, snapshot, true); container.append(design.details);
  if (list(plan.missing_information).length) { const gaps = disclosure(`待补充信息（${plan.missing_information.length} 项）`); appendList(gaps.content, "仍缺少的信息", plan.missing_information); container.append(gaps.details); }
  const answers = disclosure(`该版本问题与回答（${list(snapshot.questions).length} 项）`), records = el("dl", "version-answer-list");
  list(snapshot.questions).forEach(question => { records.append(el("dt", "", question.label || question.id), el("dd", "", isNonBlank(snapshot.answers?.[question.id]) ? valueText(snapshot.answers[question.id]) : "未回答 · 保留为未知")); }); answers.content.append(records); container.append(answers.details);
  const sources = disclosure(`该版本引用（${list(snapshot.sources).length} 项）`);
  list(snapshot.sources).forEach(source => { const row = el("p"); row.append(citationButton(source.id, snapshot), el("span", "", ` ${source.title || source.id}${source.availability?.readable === false ? " · 资料库已关闭或删除，引用记录保留" : ""}`)); sources.content.append(row); }); container.append(sources.details);
}
function versionRestoreReason(view = versionsState) {
  if (!currentVersionContext(view) || !view.snapshot) return "先选择一个有快照的历史版本。";
  if (view.previewBusy || view.listBusy) return "正在读取版本，请稍候。";
  if (view.restoring || state.operation) return "当前操作尚未结束，请稍候。";
  if (view.listError || view.loadedRevision !== state.session.revision || view.currentRevision !== state.session.revision) return "当前方案或版本列表已更新，请重新打开当前方案并刷新版本列表。";
  if (view.snapshot.revision >= state.session.revision) return "当前版本无需恢复；请选择较早的可用快照。";
  if (state.dirty) return "当前回答尚未保存，请先保存或处理未保存修改。";
  const agent = state.agent;
  if (!agent || agent.session_id !== state.session.id) return "正在核对当前方案的 Agent 连接状态，请稍候。";
  if (agent.connected || agent.alive || agent.external?.registered || ["starting", "running"].includes(agent.status) || agent.commandPending) return "请先在 Agent 对话中断开连接或外部绑定，再创建新修订。";
  return "";
}
function syncVersionControls() {
  if ($("open-versions")) setIfChanged($("open-versions"), "disabled", !state.session || Boolean(state.sessionDeletion));
  const view = versionsState, reason = versionRestoreReason(view);
  if (!$("restore-version")) return;
  if (view?.confirming && reason && !view.restoring) view.confirming = false;
  setIfChanged($("restore-version"), "disabled", Boolean(reason)); textIfChanged($("version-restore-hint"), reason || "以历史内容创建新修订；当前版本号不回退，原确认不会沿用。");
  setIfChanged($("version-restore-confirmation"), "hidden", !view?.confirming);
  setIfChanged($("confirm-version-restore"), "disabled", Boolean(reason)); setIfChanged($("cancel-version-restore"), "disabled", Boolean(view?.restoring));
  textIfChanged($("confirm-version-restore"), view?.restoring ? "正在创建…" : "确认创建新修订"); textIfChanged($("version-error"), view?.error || "");
}
function beginVersionRestore() {
  const view = versionsState; if (versionRestoreReason(view)) { syncVersionControls(); return; }
  view.confirming = true; view.error = "";
  textIfChanged($("version-restore-detail"), `将以 r${view.snapshot.revision} 的内容创建新的当前修订（当前为 r${state.session.revision}）。已有版本继续保留，版本号不会回退；确认状态清除，需要重新审阅。`); syncVersionControls();
}
async function confirmVersionRestore() {
  const view = versionsState; if (!view?.confirming || versionRestoreReason(view)) { syncVersionControls(); return; }
  const revision = state.session.revision, target = view.snapshot.revision; view.restoring = true; view.error = ""; syncVersionControls();
  await runOperation("version-restore", "正在创建新修订", "保留历史版本，以所选快照创建新的当前方案；不会运行 Agent 或求解。", async () => {
    try {
      const session = await post(`/api/sessions/${encodeURIComponent(view.sessionId)}/versions/${target}/restore`, {revision, confirmed: true});
      if (!currentVersionContext(view)) return;
      if (session.id !== view.sessionId || session.revision !== revision + 1 || session.status === "confirmed") throw new Error("服务未返回预期的新修订，请刷新当前方案后核对。");
      resetVersions(); adoptSession(session, true); notice(`已以 r${target} 创建新修订 r${session.revision}。请重新审阅方案并确认。`); await refreshStatus(true);
    } catch (error) { if (currentVersionContext(view) && error.name !== "AbortError") { view.error = errorMessage(error); view.confirming = false; } }
  });
  if (currentVersionContext(view)) { view.restoring = false; syncVersionControls(); }
}

function openDialog(id) { const dialog = $(id); if (!dialog.open) dialog.showModal(); }
function openSystem() { renderSystem(); openDialog("system-dialog"); refreshStatus(true); }
function updateAgentState(agent) { if (state.session && agent?.session_id && agent.session_id !== state.session.id) return; state.agent = state.session ? agent : null; syncConfirmation(); syncRefine(); syncWorkflow(); syncVersionControls(); if ($("system-dialog").open) renderSystem(); }
function openSearch() { openDialog("search-dialog"); if (!$("search-query").value && state.session) $("search-query").value = state.session.goal || ""; $("search-query").focus(); }
async function showSource(id) {
  caseGuide?.reset(); sourceRead?.abort(); const controller = new AbortController(); sourceRead = controller;
  const request = ++state.sourceRequest; openDialog("source-dialog"); $("source-title").textContent = "正在读取来源…"; $("source-meta").replaceChildren(); $("source-official-link").hidden = true; $("source-body").className = "document-text"; $("source-body").textContent = "正在读取索引中保存的文档内容…";
  $("source-related").replaceChildren(); $("source-related").hidden = true;
  sourceTechnicalData = null; $("source-technical").open = false; $("source-technical").hidden = true; $("source-technical-body").textContent = "";
  try {
    const source = await api(`/api/sources/${encodeURIComponent(id)}`, {signal: controller.signal}); if (request !== state.sourceRequest) return;
    $("source-title").textContent = source.title || "文档原文"; const meta = $("source-meta");
    const path = Array.isArray(source.section_path) ? source.section_path.join(" › ") : source.section_path;
    [kindLabels[source.kind] || source.kind, source.version ? `COMSOL ${source.version}` : null, source.origin || source.branch,
      source.library_id, source.revision_id ? `资料版本 ${source.revision_id.slice(0, 12)}` : null, path].filter(Boolean).forEach((text) => meta.append(el("span", "", text)));
    const url = safeURL(source.url); if (url) { $("source-official-link").href = url; $("source-official-link").hidden = false; }
    $("source-body").textContent = source.text || "该来源未提供可预览正文。";
    renderRelatedSources(source);
    if (source.type === "model_case") caseGuide?.show(source.id);
    sourceTechnicalData = source; $("source-technical").hidden = !source.evidence && !Object.keys(source.coverage || {}).length;
  } catch (error) { if (request !== state.sourceRequest || error.name === "AbortError") return; $("source-title").textContent = "来源读取失败"; $("source-body").textContent = errorMessage(error); $("source-body").className = "document-text source-error"; }
  finally { if (sourceRead === controller) sourceRead = null; }
}
function renderSourceTechnical() {
  if (!$("source-technical").open || !sourceTechnicalData || $("source-technical-body").textContent) return;
  const source = sourceTechnicalData, technical = [];
  if (source.evidence) technical.push("来源定位（与所引资料版本绑定）：\n" + JSON.stringify(source.evidence, null, 2));
  if (source.coverage && Object.keys(source.coverage).length) technical.push("提取覆盖与缺口：\n" + JSON.stringify(source.coverage, null, 2));
  $("source-technical-body").textContent = technical.join("\n\n");
}
function renderRelatedSources(source) {
  const container = $("source-related"), related = list(source.related_sources); container.replaceChildren();
  container.hidden = !related.length && !source.related_sources_warning;
  if (container.hidden) return;
  container.append(el("h3", "", "关联来源 · 待逐项核实"));
  container.append(el("p", "muted", "这些是依赖与同源资料线索，未证明已阅读、可执行或与教程逐步对齐。"));
  const relations = {requires: "依赖", same_case: "同案例", context: "教程背景与定义", documented_by: "教程候选", follows_document_order: "文档顺序，非执行依赖"};
  const statuses = {resolved: "已定位，待核实", source_relation: "来源关联，待核实", same_source_asset: "同源资料", documented_not_model_validated: "文档原文，未核验模型一致性", candidate_same_branch: "同类候选，未核对对齐", unresolved: "未解析，待查证", not_verified: "尚未核实", observed_source: "源码记录，未执行"};
  related.forEach((item) => {
    const row = el("div", "related-source-row");
    if (typeof item.id === "string" && item.id.trim() && item.status !== "unresolved") {
      const button = el("button", "text-button related-source-button", item.title || item.id); button.type = "button";
      button.addEventListener("click", () => showSource(item.id)); row.append(button);
    } else row.append(el("span", "related-source-unresolved", item.title || "未定位的依赖"));
    const candidate = item.status === "candidate_same_branch" || item.relation === "documented_by" && item.alignment === "not_verified";
    row.append(el("p", "related-source-labels", [relations[item.relation] || "关联资料", candidate ? "同类候选，未核对对齐" : statuses[item.status] || "待核实", item.feature_branch ? `分支：${item.feature_branch}` : null].filter(Boolean).join(" · ")));
    if (item.status === "unresolved") row.append(el("p", "muted", "该引用尚未定位到当前案例版本，不生成来源链接。"));
    container.append(row);
  });
  if (source.related_sources_warning) container.append(el("p", "source-error", source.related_sources_warning));
}
async function searchSources(event) {
  event.preventDefault(); const query = $("search-query").value.trim(); if (!query) return;
  sourceSearch?.abort(); const controller = new AbortController(); sourceSearch = controller; const request = ++searchRequest;
  $("search-submit").disabled = true; $("search-status").textContent = "正在查询本地文档索引…"; $("search-results").replaceChildren();
  try {
    const kind = $("search-kind").value;
    const params = new URLSearchParams({ q: query, kind, limit: "8" });
    const branch = $("search-feature-branch").value; if (branch && kind !== "case") params.set("feature_branch", branch);
    const result = kind === "case"
      ? (await api("/api/tools/call", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name: "search_cases", arguments: {query}}), signal: controller.signal})).result
      : await api(`/api/search?${params}`, {signal: controller.signal}); if (request !== searchRequest) return; const results = list(result.results);
    $("search-status").textContent = results.length ? `找到 ${results.length} 条相关结果。请核对版本、章节和适用条件。` : "未找到匹配内容。可调整关键词，或在引擎与知识库状态中更新索引。";
    results.forEach((source) => { const card = el("article", "search-result"), button = el("button", "search-result-title", source.title || source.id); button.type = "button"; button.addEventListener("click", () => showSource(source.id)); card.append(button, el("p", "", source.excerpt || "打开查看来源内容")); const path = Array.isArray(source.section_path) ? source.section_path.join(" › ") : source.section_path; card.append(el("div", "source-topline", [kindLabels[source.kind] || source.kind, source.version ? `COMSOL ${source.version}` : null, path].filter(Boolean).join(" · "))); $("search-results").append(card); });
  } catch (error) { if (request === searchRequest && error.name !== "AbortError") $("search-status").textContent = `检索失败：${errorMessage(error)}`; }
  finally { if (request === searchRequest) { sourceSearch = null; $("search-submit").disabled = false; } }
}
async function buildIndex() {
  if (state.operation || state.indexBusy) return; state.indexBusy = true; $("index-progress").hidden = false; $("index-progress-text").textContent = "正在更新已登记资料库的 QMD 关键词索引，完成前请保持页面打开…"; $("build-index").disabled = true;
  try { const result = await post("/api/index/build", {}); state.indexResult = result; renderCorpus(result); const failures = list(result.last_build?.errors); const hasError = Boolean(result.error) || failures.length > 0; notice(hasError ? `索引构建已返回，但存在错误：${result.error || failures.map(valueText).join("；")}` : result.ready ? `QMD 关键词索引已就绪 · ${result.object_count ?? result.document_count ?? result.count ?? "未知"} 个文档。` : "索引操作已返回，但 QMD 关键词索引尚未就绪。请检查运行组件和资料库索引状态。", hasError || !result.ready); await refreshStatus(true); }
  catch (error) { notice(`索引构建失败：${errorMessage(error)}`, true); $("corpus-status").append(el("p", "source-error", errorMessage(error))); }
  finally { state.indexBusy = false; renderCorpus(state.status?.corpus || state.indexResult); }
}

$("intake-form").addEventListener("submit", createSession);
$("inspect-model").addEventListener("click", inspectPath);
$("model-file").addEventListener("change", (event) => uploadModel(event.target.files[0]));
$("model-path").addEventListener("change", () => { if ($("model-path").value.trim() !== state.modelPath) { state.model = null; state.modelPath = ""; $("file-name").textContent = "或填写本机文件路径"; renderModel(); } });
$("new-session").addEventListener("click", newSession);
$("open-versions").addEventListener("click", openVersions);
$("refresh-versions").addEventListener("click", () => loadVersions(true));
$("reload-version-list").addEventListener("click", () => loadVersions(true));
$("restore-version").addEventListener("click", beginVersionRestore);
$("cancel-version-restore").addEventListener("click", () => { if (versionsState && !versionsState.restoring) { versionsState.confirming = false; syncVersionControls(); } });
$("confirm-version-restore").addEventListener("click", confirmVersionRestore);
$("versions-dialog").addEventListener("close", closeVersionPreview);
$("refresh-history").addEventListener("click", () => refreshStatus());
$("toggle-deleted-sessions").addEventListener("click", toggleDeletedSessions);
$("sessions-active-tab").addEventListener("click", () => selectSessionManagementTab(false));
[["sessions-active-tab", true], ["toggle-deleted-sessions", false]].forEach(([id, deleted]) => $(id).addEventListener("keydown", (event) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); const next = event.key === "Home" ? false : event.key === "End" ? true : deleted; void selectSessionManagementTab(next); $(next ? "toggle-deleted-sessions" : "sessions-active-tab").focus(); } }));
$("managed-session-search").addEventListener("input", renderPlanManagement);
$("refresh-managed-sessions").addEventListener("click", () => state.showDeletedSessions ? refreshDeletedSessions() : refreshManagedSessions());
$("load-more-sessions").addEventListener("click", () => refreshManagedSessions(true));
$("management-new-session").addEventListener("click", newSession);
$("delete-current-session").addEventListener("click", () => openSessionDeletion(state.session?.id));
$("cancel-session-delete").addEventListener("click", cancelSessionDeletion);
$("confirm-session-delete").addEventListener("click", confirmSessionDeletion);
$("session-delete-dialog").addEventListener("cancel", (event) => { event.preventDefault(); cancelSessionDeletion(); });
$("session-delete-dialog").addEventListener("close", cancelSessionDeletion);
$("cancel-session-purge").addEventListener("click", cancelSessionPurge);
$("confirm-session-purge").addEventListener("click", confirmSessionPurge);
$("session-purge-dialog").addEventListener("cancel", (event) => { event.preventDefault(); cancelSessionPurge(); });
$("session-purge-dialog").addEventListener("close", cancelSessionPurge);
$("refresh-status").addEventListener("click", () => refreshStatus());
["open-system", "system-shortcut"].forEach((id) => $(id).addEventListener("click", openSystem));
["open-search", "search-more"].forEach((id) => $(id).addEventListener("click", openSearch));
$("intent-form").addEventListener("submit", saveAnswers);
$("confirm-session").addEventListener("click", confirmSession);
$("refine-plan").addEventListener("click", refinePlan);
$("analyze-simplification").addEventListener("click", supplementSimplification);
agentPanel = window.SimAdvisorAgentPanel?.mount($("agent-panel"), {
  getSession: () => state.session,
  isDirty: () => state.dirty,
  isBlocked: () => Boolean(state.operation),
  onState: updateAgentState,
  onSession: (session, { preserveDrafts = false } = {}) => {
    if (state.session?.id !== session.id || state.operation || session.revision < state.session.revision || (state.dirty && !preserveDrafts)) return;
    const drafts = {};
    if (state.dirty) {
      const definition = (question) => JSON.stringify({ id: question.id, label: question.label, kind: question.kind || question.type || "text", options: list(question.options).map((option) => typeof option === "object" && option !== null ? { value: option.value, label: option.label } : option) });
      const changed = list(state.session.questions).filter((question) => questionValue(question) !== normalizedAnswer(question, state.session.answers?.[question.id]));
      const incompatible = changed.filter((question) => { const replacement = list(session.questions).find((candidate) => candidate.id === question.id); return !replacement || definition(question) !== definition(replacement); });
      if (incompatible.length) throw new Error(`未刷新，当前回答均已保留。以下问题已删除或含义变化，请先复制对应回答，再手动处理：${incompatible.map((question) => question.label || question.id).join("；")}`);
      changed.forEach((question) => { drafts[question.id] = questionValue(question); });
    }
    adoptSession(session);
    if (Object.keys(drafts).length) { Object.assign(state.draftAnswers, drafts); updateDirty(); renderQuestions(); syncRefine(); }
    notice(`Agent 更新了流程 · r${session.revision}。${Object.keys(drafts).length ? "未保存回答已保留，请核对后保存。" : "请核对关键选择与依据，再确认当前版本。"}`); refreshStatus(true);
  }
}) || null;
$("search-form").addEventListener("submit", searchSources);
$("search-kind").addEventListener("change", () => {
  const overview = $("search-kind").value === "case";
  $("search-feature-branch").disabled = overview;
  $("search-feature-branch").title = overview ? "案例发现只检索概要；打开案例后按实现环节查设置。" : "";
});
$("source-dialog").addEventListener("close", () => { caseGuide?.reset(); state.sourceRequest++; sourceRead?.abort(); sourceRead = null; });
$("source-technical").addEventListener("toggle", renderSourceTechnical);
$("search-dialog").addEventListener("close", () => { searchRequest++; sourceSearch?.abort(); sourceSearch = null; $("search-submit").disabled = false; });
$("build-index").addEventListener("click", buildIndex);
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(button.dataset.close).close()));
// Native closedby="any" light-dismiss routes through each dialog's cancel handler; this is the fallback for older browsers.
if (typeof HTMLDialogElement === "undefined" || !("closedBy" in HTMLDialogElement.prototype)) document.querySelectorAll(".modal").forEach((dialog) => { dialog.addEventListener("click", (event) => { if (event.target !== dialog || dialog.id === "session-delete-dialog" && state.sessionDeletion?.submitting || dialog.id === "session-purge-dialog" && state.sessionPurge?.submitting) return; const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); }); });
const tabs = sideTabs;
$("nav-toggle")?.addEventListener("click", toggleNav);
$("side-toggle")?.addEventListener("click", () => setSidePanel(!state.sidePanelOpen));
syncNav();
const sessionMenu = $("session-menu"), sessionMenuButton = $("session-menu-button");
if (sessionMenu?.showPopover) {
  sessionMenu.addEventListener("toggle", (event) => { if (event.newState !== "open") return; const rect = sessionMenuButton.getBoundingClientRect(); sessionMenu.style.top = `${rect.bottom + 6}px`; sessionMenu.style.left = `${Math.max(8, Math.min(rect.right - sessionMenu.offsetWidth, window.innerWidth - sessionMenu.offsetWidth - 8))}px`; });
  sessionMenu.addEventListener("click", (event) => { if (event.target.closest(".menu-item")) sessionMenu.hidePopover(); });
  window.addEventListener("scroll", () => { if (sessionMenu.matches(":popover-open")) sessionMenu.hidePopover(); }, { passive: true });
}
if (window.ResizeObserver && $("workbench-header")) new ResizeObserver(([entry]) => $("workbench").style.setProperty("--workbench-header", `${Math.ceil(entry.target.getBoundingClientRect().height)}px`)).observe($("workbench-header"));
tabs.forEach((tab, index) => { const button = $("tab-" + tab); button.addEventListener("click", () => selectTab(tab)); button.addEventListener("keydown", (event) => { let next; if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length]; if (event.key === "ArrowLeft") next = tabs[(index + tabs.length - 1) % tabs.length]; if (event.key === "Home") next = tabs[0]; if (event.key === "End") next = tabs[tabs.length - 1]; if (next) { event.preventDefault(); selectTab(next, true); } }); });
window.addEventListener("beforeunload", (event) => { if (state.dirty || ["create", "refine", "answers", "upload"].includes(state.operation) || state.indexBusy) { event.preventDefault(); event.returnValue = ""; } });
window.addEventListener("pagehide", () => { apiControllers.forEach((controller) => controller.abort()); });
const libraryManagement = $("library-management");
let libraryPanel = null;
function ensureLibraryPanel() { if (!libraryPanel && libraryManagement) libraryPanel = window.SimAdvisorLibraryPanel?.mount(libraryManagement) || null; return libraryPanel; }
function openLibraries(refresh = true) {
  if (!libraryManagement) return;
  setWorkspaceView("libraries", { focus: true });
  const existing = Boolean(libraryPanel), panel = ensureLibraryPanel();
  if (refresh && existing) panel?.refresh();
}
["open-libraries", "open-libraries-mobile"].forEach((id) => $(id)?.addEventListener("click", () => openLibraries()));
["open-workspace", "open-workspace-mobile"].forEach((id) => $(id)?.addEventListener("click", openWorkspace));
["open-plan-management", "open-plan-management-mobile"].forEach((id) => $(id)?.addEventListener("click", openPlanManagement));
const initialParams = new URL(window.location.href).searchParams;
state.returnSessionId = initialParams.get("session") || window.history.state?.returnSessionId || null;
setWorkspaceView(["libraries", "sessions"].includes(initialParams.get("view")) ? initialParams.get("view") : "plans");
async function init() {
  renderModel(); if (state.workspaceView === "libraries") ensureLibraryPanel();
  const managementReady = state.workspaceView === "sessions" ? refreshManagedSessions() : null;
  await refreshStatus();
  if (state.workspaceView === "plans" && state.returnSessionId && state.session?.id !== state.returnSessionId) await restoreSession(state.returnSessionId);
  if (state.workspaceView === "libraries") await ensureLibraryPanel()?.ready;
  if (managementReady) await managementReady;
}
init();
setInterval(() => { if (state.indexBusy || $("system-dialog").open) refreshStatus(true); }, 15000);
