/* Local Agent conversation bridge. Rendering never treats model text as HTML. */
"use strict";

window.SimAdvisorAgentPanel = (() => {
  function setIfChanged(node, property, value) { if (node[property] !== value) node[property] = value; }
  function textIfChanged(node, value) { setIfChanged(node, "textContent", String(value ?? "")); }
  function classIfChanged(node, name, enabled) { if (node.classList.contains(name) !== enabled) node.classList.toggle(name, enabled); }
  function mount(host, options = {}) {
    const make = (tag, className, text) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text != null) textIfChanged(node, text);
      return node;
    };
    const button = (text, className = "button button-subtle") => { const node = make("button", className, text); node.type = "button"; return node; };
    const labels = { disconnected: "未连接", starting: "正在连接", idle: "已连接 · 空闲", running: "运行中", error: "连接异常" };
    let sessionId = null, epoch = 0, pollSerial = 0, timer = null, snapshot = null, commandPending = false, refreshingPlan = false, pendingRevision = null, localError = "", commandError = "", destroyed = false, notifiedState = "", savedSettings = null, copyingLink = false, backendData = null, backendChoicesKey = "", effortChoicesKey = "", fieldBackend = "codex", messageCursor = "new";
    const controllers = new Set(), drafts = new Map(), settingsDrafts = new Map(), backendDrafts = new Map();
    const messages = new Map(), messageRows = new Map(), messageSequences = new Map();
    const defaultBackends = [
      { id: "codex", label: "Codex（调试）", default_model: "gpt-6.1-sol", default_effort: "xhigh", effort_options: ["low", "medium", "high", "xhigh"] },
      { id: "dsh", label: "DSH（网页托管）", default_model: "deepseek-flash", default_effort: "high", effort_options: ["off", "low", "high", "max"] }
    ];
    const heading = make("div", "agent-panel-heading"), title = make("h3", "", "Agent 对话"), status = make("span", "agent-panel-status", "未连接");
    status.setAttribute("role", "status");
    const connect = button("连接"), stop = button("停止", "button button-subtle agent-stop");
    heading.append(title, status, connect, stop);
    const modelSummary = make("p", "agent-model-summary"), connectionNote = make("p", "agent-connection-note"); connectionNote.setAttribute("role", "status");
    const dshBinding = make("div", "agent-dsh-binding"), dshActions = make("div", "agent-dsh-actions"), copyDshLink = button("复制 DSH 绑定链接"), dshLinkDetails = make("details", "agent-dsh-link-details"), dshLinkSummary = make("summary", "", "手动复制链接"), dshLinkLabel = make("label", "", "当前方案链接"), dshLinkInput = make("input", "agent-dsh-link"), dshCopyStatus = make("p", "agent-dsh-copy-status");
    dshLinkInput.id = `${host.id || "sim-advisor-agent"}-dsh-link`; dshLinkLabel.htmlFor = dshLinkInput.id; dshLinkInput.type = "url"; dshLinkInput.readOnly = true; dshLinkInput.spellcheck = false;
    dshLinkDetails.append(dshLinkSummary, dshLinkLabel, dshLinkInput); dshActions.append(copyDshLink, dshLinkDetails);
    dshCopyStatus.setAttribute("role", "status"); dshCopyStatus.hidden = true;
    dshBinding.append(dshActions, make("p", "agent-dsh-hint", "绑定既有 DSH 会话（外部模式）：安装 bridge 插件后发送“连接这个方案：<url>”，将 <url> 换成复制的链接。"), dshCopyStatus);
    const settings = make("details", "agent-settings"), settingsSummary = make("summary", "", "连接与模型设置"), settingsForm = make("form", "agent-settings-form");
    const modelLabel = make("label", "", "模型 ID"), modelInput = make("input"), effortLabel = make("label", "", "推理强度"), effortInput = make("select");
    const settingsUid = host.id || "sim-advisor-agent";
    const modeField = make("div", "agent-settings-field agent-mode-field"), modeLabel = make("label", "", "连接方式"), modeInput = make("select"); modeInput.id = `${settingsUid}-mode`; modeLabel.htmlFor = modeInput.id;
    [["managed", "网页托管（启动独立进程）"], ["external", "外部 Agent（绑定既有会话）"]].forEach(([value, text]) => { const option = make("option", "", text); option.value = value; modeInput.append(option); }); modeInput.value = "managed"; modeField.append(modeLabel, modeInput);
    const backendField = make("div", "agent-settings-field agent-backend-field"), backendLabel = make("label", "", "托管后端"), backendInput = make("select"); backendInput.id = `${settingsUid}-backend`; backendLabel.htmlFor = backendInput.id; backendField.append(backendLabel, backendInput);
    modelInput.id = `${settingsUid}-model`; modelLabel.htmlFor = modelInput.id; modelInput.type = "text"; modelInput.required = true; modelInput.autocomplete = "off"; modelInput.spellcheck = false; modelInput.placeholder = "填写本机 Codex 可用模型 ID";
    effortInput.id = `${settingsUid}-effort`; effortLabel.htmlFor = effortInput.id;
    const modelField = make("div", "agent-settings-field"), effortField = make("div", "agent-settings-field"); modelField.append(modelLabel, modelInput); effortField.append(effortLabel, effortInput);
    const settingsNote = make("p", "agent-settings-note"), settingsActions = make("div", "agent-message-actions"), saveSettings = make("button", "button button-primary", "保存设置"), resetSettings = button("还原已保存设置");
    saveSettings.type = "submit"; settingsNote.setAttribute("role", "status"); settingsActions.append(resetSettings, saveSettings); settingsForm.append(modeField, backendField, modelField, effortField, settingsNote, settingsActions); settings.append(settingsSummary, settingsForm);
    const latest = make("p", "agent-latest", "可直接讨论当前任务，或用规划按钮交给 Agent 继续。"), activity = make("p", "agent-activity");
    activity.setAttribute("role", "status");
    const errorBox = make("p", "agent-panel-error"); errorBox.setAttribute("role", "alert");
    const update = make("div", "agent-plan-update"), updateText = make("span"), refresh = button("刷新流程"); update.append(updateText, refresh);
    const conversation = make("details", "agent-conversation"), summary = make("summary", "", "对话与输入"), history = make("div", "agent-message-history");
    history.setAttribute("aria-label", "对话历史"); history.tabIndex = 0;
    const historyNotice = make("p", "agent-history-empty", "本页仅显示打开后收到的新消息。");
    history.append(historyNotice);
    const form = make("form", "agent-message-form"), label = make("label", "", "告诉 Agent 你想讨论或调整什么"), input = make("textarea");
    const uid = host.id || "sim-advisor-agent"; input.id = `${uid}-message`; label.htmlFor = input.id; input.rows = 3; input.maxLength = 12000; input.placeholder = "例如：重点解释网格构建，并说明还缺哪些信息。";
    const draftNote = make("p", "agent-draft-note"), actions = make("div", "agent-message-actions"), send = make("button", "button button-primary", "发送"), disconnect = button("断开连接");
    send.type = "submit"; actions.append(send, disconnect); form.append(label, input, draftNote, actions); conversation.append(summary, history, form);
    host.classList.add("agent-panel"); host.setAttribute("aria-label", "Agent 对话"); const connection = make("details", "agent-connection"), connectionSummary = make("summary", "", "连接方式与模型设置"); connection.append(connectionSummary, modelSummary, connectionNote, dshBinding, settings); conversation.open = true;
    host.replaceChildren(heading, latest, activity, update, errorBox, conversation, connection);

    function current() { return !destroyed && sessionId && options.getSession?.()?.id === sessionId; }
    function bindingURL() { const url = new URL("/", window.location.href); url.searchParams.set("session", sessionId); return url.href; }
    function busy() { return Boolean(options.isBlocked?.()); }
    function dirty() { return Boolean(options.isDirty?.()); }
    function running() { return ["starting", "running"].includes(snapshot?.status); }
    function connectionMode(data = snapshot) { return data?.connection_mode === "external" ? "external" : "managed"; }
    function managedBackend(data = snapshot) { return typeof data?.managed_backend === "string" && data.managed_backend ? data.managed_backend : "codex"; }
    function backendOptions() { return Array.isArray(backendData?.backend_options) && backendData.backend_options.length ? backendData.backend_options : defaultBackends; }
    function backendOption(id) { return backendOptions().find((item) => item.id === id) || defaultBackends.find((item) => item.id === id) || { id, label: id, default_model: "", default_effort: "high", effort_options: ["high"] }; }
    function backendName(id) { return id === "dsh" ? "DSH" : id === "codex" ? "Codex" : backendOption(id).label || id; }
    function syncBackendChoices(selected) {
      const choices = backendOptions().slice(); if (!choices.some((item) => item.id === selected)) choices.push(backendOption(selected));
      const key = JSON.stringify(choices); if (key === backendChoicesKey) return;
      backendInput.replaceChildren();
      for (const item of choices) { const option = make("option", "", `${item.label || item.id}${item.enabled === false ? " · 启动已禁用" : item.available === false ? " · 未发现命令" : ""}`); option.value = item.id; backendInput.append(option); }
      backendChoicesKey = key; setIfChanged(backendInput, "value", selected);
    }
    function setManagedFields(config) {
      const id = config.managed_backend || "codex", info = backendOption(id), efforts = Array.isArray(info.effort_options) && info.effort_options.length ? info.effort_options : [info.default_effort || "high"];
      syncBackendChoices(id); setIfChanged(backendInput, "value", id); fieldBackend = id;
      const key = JSON.stringify([id, efforts]); if (key !== effortChoicesKey) { effortInput.replaceChildren(); for (const value of efforts) { const option = make("option", "", value); option.value = value; effortInput.append(option); } effortChoicesKey = key; }
      setIfChanged(effortInput, "value", efforts.includes(config.reasoning_effort) ? config.reasoning_effort : info.default_effort || efforts[0]);
      setIfChanged(modelInput, "value", config.model ?? info.default_model ?? "");
    }
    function managedStartBlocked() { const info = backendOption(managedBackend()); return connectionMode() === "managed" && !snapshot?.connected && !snapshot?.alive && (info.enabled === false || info.available === false); }
    function managedStartReason() { return backendOption(managedBackend()).enabled === false ? "当前服务禁用托管启动；可以保存配置，或使用外部 Agent 绑定。" : `本机尚未发现 ${backendName(managedBackend())} 命令；可以保存配置，连接前请检查运行环境。`; }
    function externalLive() { return connectionMode() === "external" && Boolean(snapshot?.external?.registered && snapshot.external.alive); }
    function settingsLocked() { return Boolean(snapshot?.connected || snapshot?.alive || running() || (connectionMode() === "external" && snapshot?.external?.registered)); }
    function selectedSettings() { return { model: modelInput.value.trim(), reasoning_effort: effortInput.value, connection_mode: modeInput.value, managed_backend: backendInput.value }; }
    function settingsDirty() { return Boolean(savedSettings && JSON.stringify(selectedSettings()) !== JSON.stringify(savedSettings)); }
    function rememberSettingsDraft() { if (!sessionId) return; if (settingsDirty()) settingsDrafts.set(sessionId, selectedSettings()); else if (savedSettings) settingsDrafts.delete(sessionId); }
    function applySettings(data, explicitlySaved = false) {
      backendData = data;
      const mode = connectionMode(data), backend = managedBackend(data), info = backendOption(backend), config = data.managed_backend_settings?.[backend], managedModel = data.managed_model ?? config?.model ?? (mode === "managed" ? data.model : savedSettings?.model), managedEffort = data.managed_reasoning_effort ?? config?.reasoning_effort ?? (mode === "managed" ? data.reasoning_effort : savedSettings?.reasoning_effort);
      syncBackendChoices(backendInput.value || backend);
      const next = { model: typeof managedModel === "string" ? managedModel.trim() : info.default_model || "", reasoning_effort: managedEffort || info.default_effort, connection_mode: mode, managed_backend: backend };
      if (!explicitlySaved && JSON.stringify(next) === JSON.stringify(savedSettings)) return;
      const preserve = settingsDirty() || settingsDrafts.has(sessionId) || [modelInput, effortInput, modeInput, backendInput].includes(document.activeElement), newlyRegistered = mode === "external" && data.external?.registered && savedSettings?.connection_mode !== "external";
      savedSettings = next;
      if (explicitlySaved || !preserve) { setManagedFields(next); setIfChanged(modeInput, "value", next.connection_mode); if (explicitlySaved) backendDrafts.delete(`${sessionId}:${backend}`); }
      else if (newlyRegistered) setIfChanged(modeInput, "value", "external");
      rememberSettingsDraft();
    }
    function revision(value) { const n = Number(value); return Number.isFinite(n) && n >= 0 ? n : null; }
    function rememberRevision() {
      const remote = revision(snapshot?.plan_revision), local = revision(options.getSession?.()?.revision);
      if (remote != null && local != null && remote > local) pendingRevision = remote;
      if (pendingRevision != null && local != null && local >= pendingRevision) pendingRevision = null;
    }
    function notify() {
      const detail = { ...(snapshot || { status: "disconnected", connected: false }), commandPending, settingsDirty: settingsDirty(), updateAvailable: pendingRevision != null, refreshingPlan };
      const key = JSON.stringify([sessionId, detail.status, detail.connected, commandPending, detail.settingsDirty, detail.updateAvailable, refreshingPlan, detail.model, detail.reasoning_effort, detail.connection_mode, detail.managed_backend, detail.external?.registered, detail.external?.alive, detail.external?.name]);
      if (key !== notifiedState) { notifiedState = key; options.onState?.(detail); }
    }
    function clearMessages(reset = false) {
      messages.clear(); messageRows.clear(); messageSequences.clear();
      textIfChanged(historyNotice, reset ? "消息订阅已重置；此前输出未在本页保留，可能存在缺口。" : "本页仅显示打开后收到的新消息。");
      history.replaceChildren(historyNotice);
    }
    function upsertMessage(id, message, sequence = null) {
      if (sequence != null && messageSequences.has(id) && sequence <= messageSequences.get(id)) return;
      if (sequence != null) messageSequences.set(id, sequence);
      messages.set(id, message);
      let view = messageRows.get(id);
      if (!view) {
        const row = make("article", "agent-message"), meta = make("div", "agent-message-meta"), role = make("strong"), time = make("time"), text = make("p", "agent-message-text");
        row.dataset.messageId = id; meta.append(role, time); row.append(meta, text); history.append(row);
        view = {row, role, time, text}; messageRows.set(id, view);
      }
      const role = ["user", "assistant", "system"].includes(message.role) ? message.role : "system";
      setIfChanged(view.row, "className", `agent-message agent-message-${role}`);
      const roleText = { user: "你", assistant: "Agent", system: "系统" }[role];
      textIfChanged(view.role, roleText);
      textIfChanged(view.text, message.text || "");
      const date = message.at ? new Date(message.at) : null, validDate = date && !Number.isNaN(date.getTime());
      const timeText = validDate ? date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : "";
      textIfChanged(view.time, timeText);
      setIfChanged(view.time, "hidden", !validDate); if (validDate) setIfChanged(view.time, "dateTime", date.toISOString());
    }
    function mergeMessages(data) {
      const incremental = Array.isArray(data.message_updates), cursor = typeof data.message_cursor === "string" && data.message_cursor ? data.message_cursor : null;
      if (incremental && cursor && cursor === messageCursor) return;
      const atBottom = history.scrollHeight - history.scrollTop - history.clientHeight < 48;
      if (incremental) {
        if (data.messages_reset) clearMessages(true);
        for (const update of data.message_updates) {
          const message = update?.message, id = message?.id;
          if (typeof id !== "string" || !id) continue;
          upsertMessage(id, message, Number.isFinite(update.sequence) ? update.sequence : null);
        }
        if (cursor) messageCursor = cursor;
      } else if (Array.isArray(data.messages)) {
        // Compatibility with older local servers; current requests subscribe to updates only.
        data.messages.forEach((message, index) => upsertMessage(String(message.id || `legacy-${index}`), message));
        if (messageCursor === "new") messageCursor = "legacy";
      }
      if (atBottom) history.scrollTop = history.scrollHeight;
    }
    function render() {
      setIfChanged(host, "hidden", !sessionId);
      if (!sessionId) { notify(); return; }
      const link = bindingURL(); if (dshLinkInput.value !== link) dshLinkInput.value = link;
      setIfChanged(copyDshLink, "disabled", !current() || copyingLink); textIfChanged(copyDshLink, copyingLink ? "正在复制…" : "复制 DSH 绑定链接");
      const state = snapshot?.status || "disconnected", external = connectionMode() === "external", binding = snapshot?.external || {}, live = externalLive(), backend = managedBackend(), backendTitle = backendName(backend), startBlocked = managedStartBlocked();
      textIfChanged(status, external ? !binding.registered ? "外部 Agent 未绑定" : !live ? binding.offline_reason === "process_exited" ? "外部 · 已离线" : "外部 · 心跳失联" : state === "running" ? "外部 · 运行中" : "外部 · 已接入" : labels[state] || "状态待核对"); setIfChanged(status.dataset, "status", external && binding.registered && !live ? "error" : state);
      textIfChanged(connect, external ? live ? "外部已接入" : binding.registered ? "外部 Agent 已离线" : "外部 Agent 未绑定" : snapshot?.connected ? `${backendTitle} 已连接` : `连接 ${backendTitle}`);
      const configDirty = settingsDirty(), configLocked = settingsLocked(), subscribing = messageCursor === "new";
      setIfChanged(connect, "disabled", external || startBlocked || !current() || commandPending || busy() || !savedSettings || subscribing || configDirty || Boolean(snapshot?.connected) || state === "starting");
      setIfChanged(connect, "title", external ? "请复制本方案链接，在 DSH 当前会话重新绑定；这里不会启动托管进程" : configDirty ? "设置尚未保存，请先保存或还原设置" : startBlocked ? managedStartReason() : `启动并连接当前方案的托管 ${backendTitle}`);
      setIfChanged(stop, "hidden", !running()); setIfChanged(stop, "disabled", subscribing || commandPending || busy() || (external && !live));
      textIfChanged(disconnect, external ? "断开绑定" : backend === "dsh" ? "断开托管 DSH" : "断开连接"); setIfChanged(disconnect, "title", external ? "仅解除工作台绑定，不终止外部 Agent 进程" : backend === "dsh" ? "关闭工作台启动的独立 DSH 进程；保留既有桌面 DSH 会话" : "断开当前托管调试连接");
      setIfChanged(disconnect, "disabled", subscribing || !configLocked || commandPending || busy());
      setIfChanged(send, "disabled", subscribing || !current() || startBlocked || commandPending || busy() || !savedSettings || configDirty || (external && !live) || !input.value.trim());
      setIfChanged(send, "title", configDirty ? "设置尚未保存，请先保存或还原设置" : external && !live ? !binding.registered ? "外部 Agent 未绑定，请先在 DSH 重新绑定本方案" : "外部 Agent 已离线，请恢复连接" : startBlocked ? managedStartReason() : "");
      textIfChanged(send, state === "running" ? "补充说明" : commandPending ? "发送中…" : "发送");
      setIfChanged(input, "disabled", commandPending || busy());
      textIfChanged(modelSummary, savedSettings ? `${external ? `外部 Agent：${binding.name || "名称未报告"}` : `网页托管 ${backendTitle}`} · ${snapshot?.model || "模型未报告"} / ${snapshot?.reasoning_effort || "推理强度未报告"}${configDirty ? " · 有未保存修改" : ""}` : "正在读取当前连接配置…");
      const heartbeat = binding.last_seen ? new Date(typeof binding.last_seen === "number" ? binding.last_seen * 1000 : binding.last_seen) : null;
      const offlineReason = { process_exited: "登记的进程已退出", heartbeat_timeout: "心跳已超时", not_registered: "尚未注册" }[binding.offline_reason] || binding.offline_reason || "暂时无法确认外部 Agent 在线";
      textIfChanged(connectionNote, !external ? startBlocked ? managedStartReason() : backend === "dsh" ? "网页托管 DSH 使用独立的官方 profile 进程；停止中断本轮，断开关闭该托管进程。既有桌面 DSH 会话独立保留。" : "" : !binding.registered ? "当前方案未绑定外部 Agent。请复制本方案的 DSH 绑定链接，在 DSH 当前会话发送“连接这个方案：<url>”重新绑定；工作台不会自动启动或注册 Agent。" : !live ? `外部 Agent 已离线：${offlineReason}。请检查该 Agent，或断开绑定。` : `断开仅解除绑定，不终止外部进程。${heartbeat && !Number.isNaN(heartbeat.getTime()) ? ` 最近心跳 ${heartbeat.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}` : ""}`);
      setIfChanged(connectionNote, "hidden", !connectionNote.textContent); classIfChanged(connectionNote, "agent-connection-offline", external && binding.registered && !live);
      textIfChanged(settingsSummary, configDirty ? "连接与模型设置 · 未保存" : "连接与模型设置");
      const selectedInfo = backendOption(backendInput.value);
      textIfChanged(settingsNote, configLocked ? `已连接或绑定，请先断开再修改后端、模型和强度。${configDirty ? "未保存修改仍已保留，暂不能发送。" : ""}` : configDirty ? "设置尚未保存，连接和发送已暂停。保存或还原设置后继续。" : modeInput.value === "external" ? "外部模型由 Agent 自报；托管后端配置保留，当前不使用。" : selectedInfo.enabled === false ? "当前服务禁用托管启动；仍可保存配置或使用外部绑定。" : selectedInfo.available === false ? `本机未发现 ${backendName(backendInput.value)} 命令，连接前请检查运行环境。` : `填写本机 ${backendName(backendInput.value)} 可用模型 ID；默认 ${selectedInfo.default_model} / ${selectedInfo.default_effort}。`);
      classIfChanged(settingsNote, "agent-settings-unsaved", configDirty);
      setIfChanged(modeInput, "disabled", !savedSettings || configLocked || commandPending || busy());
      setIfChanged(backendInput, "disabled", modeInput.disabled);
      for (const field of [backendField, modelField, effortField]) setIfChanged(field, "hidden", modeInput.value === "external");
      for (const field of [modelInput, effortInput]) setIfChanged(field, "disabled", modeInput.disabled || modeInput.value === "external");
      setIfChanged(modelInput, "placeholder", `填写本机 ${backendName(backendInput.value)} 可用模型 ID`);
      setIfChanged(saveSettings, "disabled", subscribing || !savedSettings || configLocked || commandPending || busy() || !configDirty || !modelInput.value.trim());
      setIfChanged(resetSettings, "disabled", !savedSettings || configLocked || commandPending || busy() || !configDirty);
      textIfChanged(draftNote, dirty() ? "回答尚未保存，Agent 暂时只能看到已保存的任务。未保存回答会保留。" : external && !live ? !binding.registered ? "请先在 DSH 重新绑定本方案，然后在这里发送消息。" : "外部 Agent 已离线，请恢复连接后继续。" : state === "running" ? "运行中也可补充说明，或停止当前任务。" : external ? "消息发送到当前外部 Agent；Ctrl / ⌘ + Enter 发送。" : "发送后自动连接当前任务；Enter 换行，Ctrl / ⌘ + Enter 发送。");
      textIfChanged(errorBox, commandError || localError || (typeof snapshot?.error === "string" ? snapshot.error : snapshot?.error?.message || "")); setIfChanged(errorBox, "hidden", !errorBox.textContent);
      const report = snapshot?.activity;
      const reportText = typeof report === "string" ? report : report?.label || report?.message || "";
      textIfChanged(activity, running() ? String(reportText || "Agent 正在处理任务，回复会自动更新。").slice(0, 200) : ""); setIfChanged(activity, "hidden", !activity.textContent);
      const reply = [...messages.values()].reverse().find((message) => message.role === "assistant" && message.text);
      textIfChanged(latest, reply ? `最近回复：${String(reply.text).slice(0, 180)}${String(reply.text).length > 180 ? "…（展开查看全文）" : ""}` : external && !binding.registered ? "外部 Agent 未绑定。重新绑定后才能在这里收发消息。" : running() ? "任务已交给 Agent，正在等待回复。" : "可直接讨论当前任务，或用规划按钮交给 Agent 继续。");
      setIfChanged(update, "hidden", pendingRevision == null);
      textIfChanged(updateText, dirty() ? "方案有更新；可刷新流程并保留仍适用的回答。" : refreshingPlan ? "正在读取更新后的流程…" : "方案有更新，可刷新流程。");
      textIfChanged(refresh, dirty() ? "刷新并保留回答" : "刷新流程");
      setIfChanged(refresh, "disabled", busy() || refreshingPlan || commandPending);
      notify();
    }
    async function boundedJSON(path, controller, settings = {}, timeoutMs = 15000) {
      let deadline, onAbort;
      const operation = (async () => {
        const response = await fetch(path, {credentials: "same-origin", signal: controller.signal, ...settings});
        let data; try { data = await response.json(); } catch { throw new Error(`无法读取 Agent 响应（HTTP ${response.status}）。`); }
        return {response, data};
      })();
      const timeout = new Promise((_resolve, reject) => {
        deadline = setTimeout(() => {
          const message = settings.method === "POST" ? "Agent 请求超时，结果待核对；Agent 可能仍在后台运行。将恢复状态轮询，不会自动重发任务，请先核对回复。" : "Agent 状态或方案读取超时，将稍后重试；不会自动重新发送任务。";
          reject(new Error(message)); controller.abort();
        }, timeoutMs);
      });
      const cancelled = new Promise((_resolve, reject) => { onAbort = () => reject(Object.assign(new Error("Agent 请求已取消"), {name: "AbortError"})); controller.signal.addEventListener("abort", onAbort, {once: true}); if (controller.signal.aborted) onAbort(); });
      try { return await Promise.race([operation, timeout, cancelled]); }
      finally { clearTimeout(deadline); controller.signal.removeEventListener("abort", onAbort); }
    }
    async function request(suffix, body, token = epoch) {
      const id = sessionId, controller = new AbortController(); controllers.add(controller);
      try {
        const query = new URLSearchParams({view: "updates", after: messageCursor});
        const {response, data} = await boundedJSON(`/api/sessions/${encodeURIComponent(id)}/agent${suffix}?${query}`, controller, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}, suffix === "/connect" ? 120000 : body === undefined ? 15000 : 30000);
        if (!response.ok) throw new Error([data.error || `请求失败（HTTP ${response.status}）`, typeof data.detail === "string" ? data.detail : data.detail ? JSON.stringify(data.detail) : ""].filter(Boolean).join("\n"));
        if (token !== epoch || id !== sessionId || !current()) return null;
        return data;
      } finally { controllers.delete(controller); }
    }
    async function readPlan(token = epoch, preserveDrafts = false) {
      if (!current() || refreshingPlan || pendingRevision == null || (dirty() && !preserveDrafts) || busy()) return;
      const id = sessionId, controller = new AbortController(); controllers.add(controller); refreshingPlan = true; render();
      try {
        const {response, data} = await boundedJSON(`/api/sessions/${encodeURIComponent(id)}`, controller);
        if (!response.ok) throw new Error(data.error || `读取流程失败（HTTP ${response.status}）。`);
        if (token !== epoch || id !== sessionId || !current() || (dirty() && !preserveDrafts) || busy()) return;
        const local = revision(options.getSession?.()?.revision), received = revision(data.revision);
        if (data.id !== id || received == null || (local != null && received < local)) return;
        await options.onSession?.(data, { preserveDrafts }); localError = ""; if (preserveDrafts) commandError = ""; rememberRevision();
      } catch (error) { if (token === epoch && error.name !== "AbortError") { if (preserveDrafts) commandError = error.message; else localError = error.message; } }
      finally { controllers.delete(controller); if (token === epoch) { refreshingPlan = false; render(); } }
    }
    // Poll on a timer only while something can change on its own: a connected or running Agent, a bound
    // external Agent, or a plan update waiting to load. Idle pages check when opened, focused or shown.
    function live() {
      return Boolean(snapshot?.connected || ["starting", "running"].includes(snapshot?.status) || snapshot?.external?.registered || pendingRevision != null);
    }
    function pageHidden() { return typeof document !== "undefined" && document.visibilityState === "hidden"; }
    async function poll(token = epoch) {
      const serial = ++pollSerial;
      clearTimeout(timer);
      if (!current() || commandPending || pageHidden()) return;
      try {
        const result = await request("", undefined, token);
        if (result && token === epoch && serial === pollSerial) { mergeMessages(result); applySettings(result); snapshot = result; localError = ""; rememberRevision(); render(); await readPlan(token); }
      } catch (error) { if (token === epoch && serial === pollSerial && error.name !== "AbortError") { localError = error.message; render(); } }
      finally { if (token === epoch && serial === pollSerial && current() && !pageHidden() && (live() || !snapshot)) timer = setTimeout(() => poll(token), 2000); }
    }
    async function command(action, body = {}, messageText = null) {
      if (!current() || commandPending || busy()) return false;
      if (messageCursor === "new") { commandError = "正在订阅当前方案消息，请稍后再操作。"; render(); return false; }
      if (["connect", "message"].includes(action) && (!savedSettings || settingsDirty())) { commandError = savedSettings ? "模型设置尚未保存，请先保存或还原设置，再连接或发送。" : "正在读取模型配置，请稍后再连接或发送。"; settings.open = true; connection.open = true; render(); return false; }
      if (["connect", "message"].includes(action) && managedStartBlocked()) { commandError = managedStartReason(); render(); return false; }
      if (connectionMode() === "external" && (action === "connect" || (action === "message" && !externalLive()))) { render(); return false; }
      if (action === "settings" && settingsLocked()) { commandError = "已连接，请先断开再修改模型设置。"; settings.open = true; connection.open = true; render(); return false; }
      const token = epoch;
      pollSerial++; clearTimeout(timer); for (const controller of controllers) controller.abort();
      commandPending = true; localError = ""; commandError = ""; render();
      try {
        const result = await request(`/${action}`, body, token);
        if (token !== epoch || !current()) return false;
        if (result && typeof result.status === "string" && result.session_id === sessionId) { mergeMessages(result); applySettings(result, action === "settings"); snapshot = result; rememberRevision(); }
        if (messageText != null && input.value.trim() === messageText) { input.value = ""; drafts.delete(sessionId); }
        return true;
      } catch (error) { if (token === epoch && error.name !== "AbortError") commandError = error.message; return false; }
      finally { if (token === epoch) { commandPending = false; render(); if (!commandError) poll(token); else timer = setTimeout(() => poll(token), 2000); } }
    }
    function bindSession() {
      const nextId = options.getSession?.()?.id || null;
      if (nextId === sessionId) { rememberRevision(); render(); return; }
      if (sessionId) drafts.set(sessionId, input.value);
      rememberSettingsDraft();
      epoch++; pollSerial++; clearTimeout(timer); for (const controller of controllers) controller.abort(); controllers.clear();
      sessionId = nextId; snapshot = null; savedSettings = null; backendData = null; backendChoicesKey = ""; effortChoicesKey = ""; pendingRevision = null; commandPending = false; refreshingPlan = false; localError = ""; commandError = ""; notifiedState = ""; copyingLink = false; messageCursor = "new";
      textIfChanged(dshCopyStatus, ""); dshCopyStatus.hidden = true; dshLinkDetails.open = false;
      const configDraft = settingsDrafts.get(nextId); setManagedFields(configDraft || { managed_backend: "codex", model: "", reasoning_effort: "xhigh" }); modeInput.value = configDraft?.connection_mode || "managed";
      clearMessages(); input.value = drafts.get(nextId) || ""; conversation.open = true; settings.open = false; render(); if (nextId) poll(epoch);
    }
    connect.addEventListener("click", () => command("connect"));
    copyDshLink.addEventListener("click", async () => {
      if (!current() || copyingLink) return;
      const token = epoch, id = sessionId, link = bindingURL(); copyingLink = true; dshCopyStatus.hidden = true; render();
      try {
        if (!window.navigator?.clipboard?.writeText) throw new Error("clipboard_unavailable");
        await window.navigator.clipboard.writeText(link);
        if (token !== epoch || id !== sessionId || !current()) return;
        textIfChanged(dshCopyStatus, "链接已复制。在 DSH 当前会话发送“连接这个方案：”并粘贴链接。"); dshCopyStatus.hidden = false;
      } catch {
        if (token !== epoch || id !== sessionId || !current()) return;
        textIfChanged(dshCopyStatus, "浏览器未允许复制，请在“手动复制链接”中选取完整地址。"); dshCopyStatus.hidden = false; dshLinkDetails.open = true; dshLinkInput.value = link; dshLinkInput.focus(); dshLinkInput.select();
      } finally { if (token === epoch) { copyingLink = false; render(); } }
    });
    stop.addEventListener("click", () => command("interrupt"));
    disconnect.addEventListener("click", () => command("disconnect"));
    refresh.addEventListener("click", () => readPlan(epoch, dirty()));
    [modelInput, effortInput, modeInput].forEach((control) => control.addEventListener("input", () => { rememberSettingsDraft(); render(); }));
    backendInput.addEventListener("input", () => {
      if (backendInput.disabled) return;
      backendDrafts.set(`${sessionId}:${fieldBackend}`, { model: modelInput.value, reasoning_effort: effortInput.value });
      const id = backendInput.value, info = backendOption(id), config = backendDrafts.get(`${sessionId}:${id}`) || backendData?.managed_backend_settings?.[id] || { model: info.default_model, reasoning_effort: info.default_effort };
      setManagedFields({ ...config, managed_backend: id }); rememberSettingsDraft(); render();
    });
    settingsForm.addEventListener("submit", async (event) => { event.preventDefault(); if (!saveSettings.disabled) await command("settings", selectedSettings()); });
    resetSettings.addEventListener("click", () => { if (!savedSettings || resetSettings.disabled) return; setManagedFields(savedSettings); modeInput.value = savedSettings.connection_mode; settingsDrafts.delete(sessionId); for (const key of backendDrafts.keys()) if (key.startsWith(`${sessionId}:`)) backendDrafts.delete(key); commandError = ""; render(); });
    input.addEventListener("input", () => { if (sessionId) drafts.set(sessionId, input.value); render(); });
    input.addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); form.requestSubmit(); } });
    form.addEventListener("submit", async (event) => { event.preventDefault(); const text = input.value.trim(); if (text) await command("message", { text }, text); });
    let lastWake = 0;
    function onVisibility() { if (pageHidden() || !sessionId || commandPending || Date.now() - lastWake < 1000) return; lastWake = Date.now(); poll(); }
    function destroy() { destroyed = true; epoch++; clearTimeout(timer); for (const controller of controllers) controller.abort(); controllers.clear(); document.removeEventListener?.("visibilitychange", onVisibility); window.removeEventListener?.("focus", onVisibility); }
    document.addEventListener?.("visibilitychange", onVisibility); window.addEventListener?.("focus", onVisibility);
    window.addEventListener("pagehide", destroy, { once: true });
    bindSession();
    return { bindSession, refresh: () => poll(), loadUpdate: () => readPlan(epoch, dirty()), sendTask: (text) => command("message", { text: String(text) }), destroy };
  }
  return { mount };
})();
