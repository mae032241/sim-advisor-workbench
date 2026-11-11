// Generated from config-ui.js; no runtime npm/browser imports.
window.__ModuleLoader__.load({id: "@local/sim-advisor-workbench", factory: function createClientModule(require) {
  const React = require('react');
  const h = React.createElement;
  const PACKAGE = '@local/sim-advisor-workbench';
  const ENTRY = 'sim-advisor-workbench';
  const DOCK = `${PACKAGE}#workbench`;
  const starts = new WeakMap();
  const stops = new WeakMap();
  const stopEpochs = new WeakMap();
  const descriptors = ['status', 'configure', 'start', 'stop', 'verify'].map(method => ({
    id: `${PACKAGE}#simWorkbenchSetup/${method}`,
    service: 'simWorkbenchSetup', namespace: 'simWorkbenchSetup', method,
    parameters: [], result: {mode: 'src-json'}, invocation: {kind: 'direct'},
  }));
  function pathKey(value) {
    const text = String(value || '').trim().replace(/\\/g, '/').replace(/\/+$/, '');
    return /^[a-z]:/i.test(text) ? text.toLowerCase() : text;
  }
  function dataSetting(value) { return String(value || '').trim(); }
  function detailText(value) {
    if (typeof value === 'string') return value;
    if (value && typeof value === 'object') return value.message || value.error || JSON.stringify(value);
    return '';
  }
  function unwrap(answer) {
    if (!answer?.ok) throw new Error(detailText(answer?.error) || '工作台配置接口当前不可用。');
    return answer.value;
  }
  function safeWorkbenchURL(value) {
    if (typeof value !== 'string' || !value || value !== value.trim() || /[\s\0]/.test(value)) throw new Error('服务未返回安全的本机 HTTP 地址。');
    let url;
    try { url = new URL(value); } catch { throw new Error('服务未返回安全的本机 HTTP 地址。'); }
    const local = url.hostname === 'localhost' || url.hostname === '[::1]' || /^127\.(?:\d{1,3}\.){2}\d{1,3}$/.test(url.hostname);
    if (url.protocol !== 'http:' || !local || url.username || url.password) throw new Error('服务未返回安全的本机 HTTP 地址。');
    return url.href;
  }
  function reserveWindow(browser) {
    let reserved;
    try {
      reserved = browser?.open?.('about:blank', '_blank');
      if (reserved) {
        reserved.opener = null;
        if (reserved.document) {
          reserved.document.title = '正在启动仿真方案工作台…';
          const meta = reserved.document.createElement?.('meta');
          if (meta) { meta.name = 'referrer'; meta.content = 'no-referrer'; reserved.document.head?.appendChild(meta); }
        }
      }
    } catch { /* A blocked browser popup must not prevent starting the service. */ }
    return reserved;
  }
  function closeReserved(reserved) {
    try { if (reserved && !reserved.closed && reserved.location.href === 'about:blank') reserved.close(); } catch { /* It may have been closed or navigated by the user. */ }
  }
  function openWorkbench(url, reserved, browser) {
    try {
      if (reserved && !reserved.closed && reserved.location.href === 'about:blank') {
        reserved.location.replace(url); return true;
      }
    } catch { /* Keep the permanent link if the reserved tab is unavailable. */ }
    try { return Boolean(browser?.open?.(url, '_blank', 'noopener,noreferrer')); }
    catch { return false; }
  }
  async function pickDirectory(remote, desktop = globalThis.__DSH_DIRECTORY_PICKER__) {
    if (typeof desktop?.pick === 'function') return await desktop.pick();
    if (typeof remote?.directoryPicker?.pick === 'function') return unwrap(await remote.directoryPicker.pick());
    throw new Error('此环境未提供目录选择器，请填写主机上的绝对路径。');
  }
  async function boundedRPC(operation, timeoutMs, message) {
    let timer;
    try {
      return await Promise.race([
        Promise.resolve().then(operation),
        new Promise((_resolve, reject) => { timer = setTimeout(() => reject(new Error(message)), timeoutMs); }),
      ]);
    } finally { clearTimeout(timer); }
  }
  function startService(remote, timeoutMs = 330000) {
    const service = remote.simWorkbenchSetup;
    if (starts.has(service)) return starts.get(service);
    // Configuration and the sidebar can be mounted at the same time. Share
    // only the pending RPC; the Host decides whether an existing service lives.
    const pending = boundedRPC(() => service.start(), timeoutMs, '启动请求超时，结果待核对；工作台可能仍在启动。不会自动重发，请核对后再手动重试。');
    starts.set(service, pending);
    pending.finally(() => { if (starts.get(service) === pending) starts.delete(service); }).catch(() => {});
    return pending;
  }
  function stopService(remote, timeoutMs = 30000) {
    const service = remote.simWorkbenchSetup;
    if (stops.has(service)) return stops.get(service);
    const pending = boundedRPC(() => service.stop(), timeoutMs, '停止请求超时，结果待核对；请先核对服务状态，再手动重试。不会自动重发或重新启动。');
    stops.set(service, pending);
    pending.finally(() => { if (stops.get(service) === pending) stops.delete(service); }).catch(() => {});
    return pending;
  }
  function serviceView(info) { return info && typeof info === 'object' ? {running: typeof info.running === 'boolean' ? info.running : null, owned: info.owned === true, state: info.state || 'unknown', pid: info.pid ?? null, url: info.url || null, detail: detailText(info.detail).slice(0, 500)} : null; }
  function sameConfiguration(left, right) {
    if (Object.is(left, right)) return true;
    if (!left || !right || typeof left !== 'object' || typeof right !== 'object'
        || Array.isArray(left) !== Array.isArray(right)) return false;
    const keys = Object.keys(left);
    return keys.length === Object.keys(right).length && keys.every(key =>
      Object.prototype.hasOwnProperty.call(right, key) && sameConfiguration(left[key], right[key]));
  }
  function createController(form, remote, options = {}) {
    const listeners = new Set();
    const interval = options.setInterval || globalThis.setInterval;
    const cancel = options.clearInterval || globalThis.clearInterval;
    let state = {draft: form.getSnapshot().value?.offlineBundle || '', dataDraft: form.getSnapshot().value?.dataDir || '', phase: 'loading', message: '正在读取配置状态…', configuration: null, picking: false, pickTarget: null, writable: false, launchPhase: 'idle', launchMessage: '', workbenchUrl: null, workbench: null, stopPhase: 'idle', stopMessage: ''};
    let alive = true, started = false, timer, unsubscribe, reading;
    let dirty = false, applying = false, verifying = false, launching = false, stopping = false, operationEpoch = 0, target, targetData, previousRevision, pendingWindow;
    const statusRPC = () => boundedRPC(() => remote.simWorkbenchSetup.status(), options.rpcTimeoutMs ?? 15000,
      '状态读取超时，将稍后重试；不会自动重发配置或启动请求。');
    const notify = patch => {
      if (!alive) return;
      const changes = Object.fromEntries(Object.entries(patch).filter(([key, value]) =>
        ['configuration', 'workbench'].includes(key) ? !sameConfiguration(state[key], value) : !Object.is(state[key], value)));
      // useSyncExternalStore needs the same snapshot identity until something
      // actually changes. Equal status polls must not rerender the iframe tree.
      if (Object.keys(changes).length === 0) return;
      state = {...state, ...changes};
      for (const listener of listeners) listener();
    };
    const readForm = () => {
      const snapshot = form.getSnapshot();
      const patch = {writable: snapshot.status === 'ready' && snapshot.writable === true};
      if (!dirty && !applying) {
        patch.draft = snapshot.value?.offlineBundle || '';
        patch.dataDraft = snapshot.value?.dataDir || '';
      }
      if (snapshot.status === 'unavailable') patch.message = '插件配置暂不可编辑，请检查插件是否已启用。';
      notify(patch);
    };
    const acceptStatus = info => {
      if (!info || !['unconfigured', 'checking', 'ready', 'error'].includes(info.state)) throw new Error('配置状态响应无效。');
      const changed = state.configuration && state.configuration.revision !== info.revision;
      const {workbench: reportedWorkbench, ...configuration} = info;
      notify({configuration, ...(changed && !launching ? {workbenchUrl: null, launchPhase: 'idle', launchMessage: ''} : {})});
      const workbench = serviceView(reportedWorkbench);
      if (workbench) notify({workbench, ...(!launching && !stopping && workbench.running === false ? {workbenchUrl: null, ...(state.launchPhase === 'running' ? {launchPhase: 'idle', launchMessage: ''} : {})} : {}), ...(!launching && !stopping && state.stopPhase === 'error' && typeof workbench.running === 'boolean' ? {stopPhase: 'idle', stopMessage: workbench.running ? '已核对：服务仍在运行，可重新停止。' : '已核对：工作台已停止，资料库和方案保留。'} : {}), ...(!launching && !stopping && state.stopPhase === 'stopped' && workbench.running === true ? {stopPhase: 'idle', stopMessage: ''} : {})});
      if (dirty && !applying) return;
      if (applying) {
        const sameRoot = pathKey(info.bundle_root) === pathKey(target);
        const sameData = dataSetting(info.data_dir_setting) === targetData;
        const fresh = previousRevision === undefined || (typeof info.revision === 'number' && info.revision > previousRevision);
        if (!sameRoot || !sameData || !fresh) return;
      } else if (info.state === 'ready') {
        const saved = form.getSnapshot().value || {};
        if (pathKey(info.bundle_root) !== pathKey(saved.offlineBundle)
            || dataSetting(info.data_dir_setting) !== dataSetting(saved.dataDir)) {
          notify({phase: 'idle', message: '已保存目录与当前服务配置不一致，请保存并配置。'}); return;
        }
      }
      if (info.state === 'ready') {
        const verified = verifying; applying = false; verifying = false;
        const verifiedMessage = '离线依赖完整校验通过，可启动工作台。';
        notify({phase: 'ready', message: verified || !changed && state.phase === 'ready' && state.message === verifiedMessage ? verifiedMessage : '离线依赖已配置，可启动工作台。'});
      } else if (info.state === 'error') {
        applying = false; verifying = false;
        notify({phase: 'error', message: '初始化失败：' + (detailText(info.detail) || '请检查目录后重新保存。')});
      } else if (info.state === 'checking') notify({phase: 'checking', message: '目录已保存，正在校验和配置离线依赖…'});
      else notify({phase: 'idle', message: '尚未配置离线依赖包。'});
    };
    const refresh = () => {
      if (!alive) return Promise.resolve();
      if (reading) return reading;
      const epoch = operationEpoch;
      reading = statusRPC().then(unwrap).then(info => { if (epoch === operationEpoch) acceptStatus(info); }).catch(error => {
        if (epoch !== operationEpoch) return;
        if (!dirty || applying) notify({phase: applying ? 'checking' : 'error', message: '无法确认初始化状态：' + error.message});
      }).finally(() => { reading = undefined; });
      return reading;
    };
    const controller = {
      getSnapshot: () => state,
      subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
      start() {
        if (started) return;
        started = true; readForm(); unsubscribe = form.subscribe(readForm);
        refresh(); timer = interval(refresh, 1500);
      },
      edit(value) {
        if (applying || launching || stopping || stops.has(remote.simWorkbenchSetup) || state.phase === 'saving') return;
        dirty = true; notify({draft: value, phase: 'dirty', message: '目录尚未保存。', workbenchUrl: null, launchPhase: 'idle', launchMessage: ''});
      },
      editData(value) {
        if (applying || launching || stopping || stops.has(remote.simWorkbenchSetup) || state.phase === 'saving') return;
        dirty = true; notify({dataDraft: value, phase: 'dirty', message: '目录尚未保存。', workbenchUrl: null, launchPhase: 'idle', launchMessage: ''});
      },
      async pick(field = 'bundle') {
        if (state.picking || applying || launching || stopping || stops.has(remote.simWorkbenchSetup) || state.phase === 'saving') return;
        notify({picking: true, pickTarget: field});
        try {
          const picked = await pickDirectory(remote, options.desktop);
          if (picked !== null && picked !== undefined) {
            if (field === 'data') controller.editData(picked);
            else controller.edit(picked);
          }
        } catch (error) { notify({phase: 'error', message: '选择目录失败：' + error.message}); }
        finally { notify({picking: false, pickTarget: null}); }
      },
      async save() {
        if (applying || launching || stopping || stops.has(remote.simWorkbenchSetup) || state.phase === 'saving' || !state.writable) return;
        const selected = state.draft.trim();
        const selectedData = state.dataDraft.trim();
        if (!selected || /[\0\r\n]/.test(selected)) {
          notify({phase: 'error', message: '请填写已解压离线依赖包的绝对目录。'}); return;
        }
        if (/[\0\r\n]/.test(selectedData)) {
          notify({phase: 'error', message: '数据目录不能包含控制换行；留空可使用默认目录。'}); return;
        }
        notify({phase: 'saving', message: '正在保存目录…', workbenchUrl: null, launchPhase: 'idle', launchMessage: ''});
        try {
          // Capture the old revision before mutation; an old ready reply must
          // never stand in for the new directory's completed initialization.
          const before = unwrap(await statusRPC());
          previousRevision = before.revision;
          const accepted = await boundedRPC(() => form.mutate([
            {op: 'set', path: ['offlineBundle'], value: selected},
            {op: 'set', path: ['dataDir'], value: selectedData},
          ], form.getSnapshot().revision), options.rpcTimeoutMs ?? 30000, '保存配置超时，结果待核对；请先刷新状态，不会自动重发。');
          if (!accepted) throw new Error('配置未获保存，可能已被其他页面修改；请检查后重试。');
          dirty = false; applying = true; target = selected; targetData = selectedData;
          notify({draft: selected, dataDraft: selectedData, phase: 'checking', message: '目录已保存，正在校验和配置离线依赖…'});
          const configured = unwrap(await boundedRPC(() => remote.simWorkbenchSetup.configure(), options.rpcTimeoutMs ?? 30000,
            '配置请求超时，后台可能仍在校验；请先刷新状态，不会自动重发。'));
          acceptStatus(configured);
          await refresh();
        } catch (error) {
          applying = false;
          notify({phase: 'error', message: error.message});
        }
      },
      canVerify() {
        const saved = form.getSnapshot().value || {};
        return alive && state.writable && !dirty && !applying && !launching && !stopping && !state.picking && !starts.has(remote.simWorkbenchSetup) && !stops.has(remote.simWorkbenchSetup)
          && !state.workbench?.running && !state.workbench?.owned && !['starting', 'stopping'].includes(state.workbench?.state)
          && !['loading', 'saving', 'checking'].includes(state.phase) && Boolean(saved.offlineBundle)
          && pathKey(state.draft) === pathKey(saved.offlineBundle) && dataSetting(state.dataDraft) === dataSetting(saved.dataDir)
          && pathKey(state.configuration?.bundle_root) === pathKey(saved.offlineBundle) && dataSetting(state.configuration?.data_dir_setting) === dataSetting(saved.dataDir);
      },
      async verify() {
        if (!controller.canVerify()) return;
        const saved = form.getSnapshot().value || {};
        previousRevision = state.configuration.revision; target = saved.offlineBundle; targetData = dataSetting(saved.dataDir); applying = true; verifying = true;
        notify({phase: 'checking', message: '正在重新校验离线包，请等待…'});
        try {
          const configured = unwrap(await boundedRPC(() => remote.simWorkbenchSetup.verify(), options.rpcTimeoutMs ?? 30000,
            '重新校验请求超时，后台可能仍在校验；请先核对状态，不会自动重发。'));
          acceptStatus(configured); await refresh();
        } catch (error) { applying = false; verifying = false; notify({phase: 'error', message: error.message}); }
      },
      canLaunch() {
        const saved = form.getSnapshot().value || {};
        return alive && !dirty && !applying && !launching && !stopping && !stops.has(remote.simWorkbenchSetup) && state.stopPhase !== 'error' && state.launchPhase !== 'starting' && !state.picking && state.phase === 'ready'
          && state.configuration?.state === 'ready'
          && pathKey(state.draft) === pathKey(saved.offlineBundle)
          && dataSetting(state.dataDraft) === dataSetting(saved.dataDir)
          && pathKey(state.configuration.bundle_root) === pathKey(saved.offlineBundle)
          && dataSetting(state.configuration.data_dir_setting) === dataSetting(saved.dataDir);
      },
      async launch() {
        if (!controller.canLaunch()) return;
        launching = true; const epoch = ++operationEpoch, serviceEpoch = stopEpochs.get(remote.simWorkbenchSetup) || 0;
        notify({launchPhase: 'starting', launchMessage: '正在启动工作台…', workbenchUrl: null, stopPhase: 'idle', stopMessage: ''});
        const browser = options.browser || globalThis.window;
        // This runs synchronously within the click gesture, before awaiting RPC.
        const reserved = !options.embedded ? reserveWindow(browser) : undefined; pendingWindow = reserved;
        try {
          const service = state.workbench?.running === true ? state.workbench : unwrap(await startService(remote, options.launchTimeoutMs));
          if (!alive || epoch !== operationEpoch) { closeReserved(reserved); return; }
          if (serviceEpoch !== (stopEpochs.get(remote.simWorkbenchSetup) || 0)) { notify({launchPhase: 'idle', launchMessage: '启动请求已取消，请核对服务状态。', workbenchUrl: null}); return; }
          if (service?.running !== true) throw new Error('服务尚未确认运行，请重试。');
          const url = safeWorkbenchURL(service.url);
          const opened = options.embedded || openWorkbench(url, reserved, browser);
          pendingWindow = undefined;
          launching = false; operationEpoch++;
          notify({launchPhase: 'running', workbenchUrl: url, workbench: serviceView(service),
            launchMessage: opened ? '工作台已启动。' : '工作台已启动；若网页未弹出，请点击“打开工作台”。'});
        } catch (error) {
          closeReserved(reserved); if (pendingWindow === reserved) pendingWindow = undefined;
          if (!alive || epoch !== operationEpoch) return;
          launching = false; operationEpoch++;
          notify({launchPhase: 'error', launchMessage: '启动失败：' + error.message, workbenchUrl: null});
        } finally { if (epoch === operationEpoch || state.launchPhase !== 'starting') launching = false; }
      },
      canStop() {
        return alive && !applying && !stopping && !stops.has(remote.simWorkbenchSetup) && !state.picking
          && (launching || !['loading', 'saving', 'checking'].includes(state.phase) && (state.workbench?.state === 'starting' || state.workbench?.running !== false || Boolean(state.workbenchUrl) || state.stopPhase === 'error' || state.launchPhase === 'error'));
      },
      async stop() {
        if (!controller.canStop()) return;
        stopping = true; operationEpoch++; launching = false;
        const service = remote.simWorkbenchSetup; stopEpochs.set(service, (stopEpochs.get(service) || 0) + 1); starts.delete(service);
        closeReserved(pendingWindow); pendingWindow = undefined;
        notify({stopPhase: 'stopping', stopMessage: '正在停止工作台…', ...(state.launchPhase === 'starting' ? {launchPhase: 'idle', launchMessage: ''} : {})});
        try {
          const service = unwrap(await stopService(remote, options.stopTimeoutMs));
          if (service?.running !== false) throw new Error('服务尚未确认停止，请核对状态后重试。');
          stopping = false; operationEpoch++;
          notify({workbench: serviceView(service), workbenchUrl: null, launchPhase: 'idle', launchMessage: '', stopPhase: 'stopped', stopMessage: '工作台已停止，资料库和方案保留。'});
        } catch (error) {
          stopping = false; operationEpoch++;
          notify({stopPhase: 'error', stopMessage: '停止结果待核对：' + error.message});
        } finally { stopping = false; }
      },
      hidePage() {
        if (launching) return;
        notify({workbenchUrl: null, launchPhase: 'idle', launchMessage: ''});
      },
      refresh,
      dispose() {
        alive = false; if (timer !== undefined) cancel(timer);
        closeReserved(pendingWindow); pendingWindow = undefined;
        unsubscribe?.(); listeners.clear();
      },
    };
    return controller;
  }
  function createPanelController(form, remote, options = {}) {
    return createController(form, remote, {...options, embedded: true});
  }
  function ConfigForm({formScope, remote, controller: provided}) {
    // `form` is a reserved plugins.row.config prop ({state, mutate}), not the
    // ConfigForms service controller. Keep our injected controller distinct.
    const controller = React.useMemo(() => provided || createController(formScope, remote), [formScope, remote, provided]);
    const state = React.useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
    React.useEffect(() => { if (!provided) { controller.start(); return () => controller.dispose(); } }, [controller, provided]);
    const configuring = state.phase === 'saving' || state.phase === 'checking';
    const busy = configuring || state.launchPhase === 'starting' || state.stopPhase === 'stopping';
    return h('section', {style: {maxWidth: '48rem', padding: '12px 0'}, 'aria-label': '仿真方案工作台离线依赖配置'},
      h('h3', null, '离线依赖包'),
      h('p', null, '选择或填写已解压的离线依赖包根目录。首次配置或更换离线包时校验，日常启动复用已有配置，无需运行 PowerShell。'),
      h('label', {htmlFor: 'sim-workbench-offline-root'}, '离线包目录'),
      h('div', {style: {display: 'flex', gap: '8px', margin: '8px 0'}},
        h('input', {id: 'sim-workbench-offline-root', type: 'text', value: state.draft, placeholder: '例如 D:\\Sim Advisor Offline', spellCheck: false, disabled: !state.writable || busy, style: {flex: 1, minWidth: 0, padding: '8px'}, onChange: event => controller.edit(event.target.value)}),
        h('button', {type: 'button', disabled: !state.writable || busy || state.picking, onClick: () => controller.pick()}, state.picking && state.pickTarget === 'bundle' ? '正在选择…' : '选择文件夹')),
      h('p', {style: {fontSize: '0.9em', opacity: 0.8}}, 'Web 端路径必须位于运行 DSH 的主机上；取消选择不会清空当前目录。'),
      h('label', {htmlFor: 'sim-workbench-data-root'}, '工作台数据目录'),
      h('div', {style: {display: 'flex', gap: '8px', margin: '8px 0'}},
        h('input', {id: 'sim-workbench-data-root', type: 'text', value: state.dataDraft, placeholder: '留空使用默认目录', spellCheck: false, disabled: !state.writable || busy, style: {flex: 1, minWidth: 0, padding: '8px'}, onChange: event => controller.editData(event.target.value)}),
        h('button', {type: 'button', disabled: !state.writable || busy || state.picking, onClick: () => controller.pick('data')}, state.picking && state.pickTarget === 'data' ? '正在选择…' : '选择数据目录')),
      h('p', {style: {fontSize: '0.9em', opacity: 0.8}}, '切换会停止当前工作台并断开连接，旧数据保留。使用空目录可建立新工作空间；沿用已有数据时，请先停止工作台，再完整复制数据目录并选择新目录。'),
      h('button', {type: 'button', disabled: !state.writable || busy || state.picking || !state.draft.trim(), onClick: () => controller.save()}, configuring ? '正在配置…' : '保存并配置'),
      h('button', {type: 'button', disabled: !controller.canVerify(), onClick: () => controller.verify(), style: {marginLeft: '12px'}}, '重新校验离线包'),
      h('p', {style: {fontSize: '0.9em', opacity: 0.8}}, '平时启动无需重新校验。手动完整校验前请先停止工作台。'),
      h('p', {style: {fontSize: '0.85em', opacity: 0.8}}, '仅在需要检查完整性时手动重新校验；状态刷新和普通启动不会自动重新校验。'),
      h('p', {role: state.phase === 'error' ? 'alert' : 'status', 'data-setup-state': state.phase, style: {whiteSpace: 'pre-wrap', overflowWrap: 'anywhere'}}, state.message),
      state.phase === 'ready' && state.configuration?.state === 'ready' && h('p', {style: {fontSize: '0.9em', overflowWrap: 'anywhere'}}, '当前依赖目录：' + state.configuration.bundle_root),
      state.configuration?.data_dir && h('p', {style: {fontSize: '0.9em', overflowWrap: 'anywhere'}}, '当前生效的数据目录：' + state.configuration.data_dir),
      h('div', {style: {display: 'flex', gap: '12px', alignItems: 'center', marginTop: '16px'}},
        h('button', {type: 'button', disabled: !controller.canLaunch(), onClick: () => controller.launch()}, state.launchPhase === 'starting' ? '正在启动…' : '启动工作台'),
        h('button', {type: 'button', disabled: !controller.canStop(), onClick: () => controller.stop()}, state.stopPhase === 'stopping' ? '正在停止…' : '停止工作台'),
        state.workbenchUrl && h('a', {href: state.workbenchUrl, target: '_blank', rel: 'noopener noreferrer'}, '打开工作台')),
      state.stopMessage && h('p', {role: state.stopPhase === 'error' ? 'alert' : 'status'}, state.stopMessage),
      state.launchMessage && h('p', {role: state.launchPhase === 'error' ? 'alert' : 'status', 'data-launch-state': state.launchPhase}, state.launchMessage));
  }
  function ConfigPage(props) {
    return props.view === 'summary' ? h('span', null, '选择离线包目录并自动配置本地依赖。') : h(ConfigForm, props);
  }
  function WorkbenchIcon({size = 16}) {
    return h('svg', {width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, 'aria-hidden': true},
      h('rect', {x: 3, y: 4, width: 18, height: 13, rx: 2}),
      h('path', {d: 'M8 21h8M12 17v4M6 12h3l2-5 3 8 2-4h2', strokeLinecap: 'round', strokeLinejoin: 'round'}));
  }
  function WorkbenchPanel({formScope, remote, onHide, onFullPage, docked = false}) {
    const controller = React.useMemo(() => createPanelController(formScope, remote), [formScope, remote]);
    const state = React.useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
    React.useEffect(() => { controller.start(); return () => controller.dispose(); }, [controller]);
    const url = state.workbenchUrl;
    const configured = state.phase === 'ready' && state.configuration?.state === 'ready';
    const serviceLabel = state.stopPhase === 'stopping' || state.workbench?.state === 'stopping' ? '正在停止工作台…' : state.stopPhase === 'error' ? '停止结果待核对' : state.launchPhase === 'starting' || state.workbench?.state === 'starting' ? '正在启动工作台…' : state.phase === 'checking' ? '正在校验离线依赖…' : state.workbench?.running === true ? '服务运行中' : state.workbench?.running === false ? '服务已停止' : '服务状态待核对';
    const serviceDetail = state.phase === 'checking' ? detailText(state.configuration?.detail) || state.message : state.workbench?.detail;
    const actionStyle = {padding: '5px 10px', borderRadius: '6px', border: '1px solid var(--dsh-border-color, #8885)', background: 'transparent', color: 'inherit', font: 'inherit', fontSize: '0.85rem', cursor: 'pointer'};
    // The DSH frame already reserves its own titlebar/shell clearance. Repeating
    // that padding here wastes height and pushes the bottom of a 100% panel out.
    return h('section', {'aria-label': '仿真工作台', style: {height: '100%', boxSizing: 'border-box', minHeight: 0, minWidth: 0, display: 'flex', flexDirection: 'column', paddingTop: 0}},
      h('details', {open: !url, 'aria-label': '上部菜单', 'data-workbench-menu': true, style: {flexShrink: 0, maxHeight: '60%', overflowY: 'auto'}},
      h('summary', {title: '仅折叠上部操作和配置，网页内容保持打开', style: {padding: '2px 10px', cursor: 'pointer', fontSize: '0.8rem', lineHeight: '18px', borderBottom: '1px solid var(--dsh-border-color, #8883)'}}, '上部菜单 · 点击折叠 / 展开'),
      h('header', {style: {display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px', padding: '8px 12px', borderBottom: '1px solid var(--dsh-border-color, #8883)', flexShrink: 0}},
        h(WorkbenchIcon, {size: 18}),
        h('h2', {style: {fontSize: '0.95rem', margin: '0 4px 0 0'}}, '仿真工作台'),
        h('span', {role: 'status', style: {fontSize: '0.8rem', opacity: 0.7, marginRight: 'auto'}}, serviceLabel),
        h('div', {'aria-label': '工作台服务开关', style: {display: 'flex', gap: '6px', flexWrap: 'wrap'}},
          !url && h('button', {type: 'button', style: actionStyle, disabled: !configured || !controller.canLaunch(), onClick: () => controller.launch()}, state.launchPhase === 'starting' ? '正在启动…' : state.workbench?.running === true ? '打开工作台' : state.launchPhase === 'error' ? '重试启动' : '启动工作台'),
          url && !docked && h('button', {type: 'button', style: actionStyle, onClick: () => controller.hidePage()}, '收起页面'),
          h('button', {type: 'button', style: actionStyle, disabled: !controller.canStop(), onClick: () => controller.stop()}, state.stopPhase === 'stopping' ? '正在停止…' : '停止工作台'),
          url && h('a', {href: url, target: '_blank', rel: 'noopener noreferrer', style: {...actionStyle, textDecoration: 'none'}}, '在浏览器打开'),
          onFullPage && h('button', {type: 'button', style: actionStyle, onClick: onFullPage}, '完整工作台'),
          onHide && h('button', {type: 'button', style: actionStyle, onClick: onHide, title: '释放工作台占用的界面空间，服务继续运行'}, docked ? '收起侧栏' : '隐藏面板'))),
      (state.launchPhase === 'error' || state.stopPhase === 'error' || state.phase === 'error') && h('p', {role: 'alert', style: {margin: '8px 12px', overflowWrap: 'anywhere'}}, state.stopPhase === 'error' ? state.stopMessage : state.launchPhase === 'error' ? state.launchMessage : state.message),
      state.stopPhase === 'error' && h('button', {type: 'button', style: actionStyle, onClick: () => controller.refresh()}, '核对服务状态'),
      h('details', {'aria-label': '服务与配置', style: {padding: '6px 12px', fontSize: '0.85rem', flexShrink: 0, maxHeight: '50vh', overflowY: 'auto'}},
        h('summary', {style: {cursor: 'pointer', opacity: 0.75}}, '服务与配置'),
        h('p', {style: {overflowWrap: 'anywhere'}}, state.stopMessage || state.launchMessage || state.message),
        serviceDetail && h('p', {style: {overflowWrap: 'anywhere'}}, serviceDetail),
        h('p', {style: {opacity: 0.7}}, '收起页面或隐藏面板不会停止服务。停止工作台后，资料库和方案保留。'),
        !configured && state.phase !== 'loading' && h(ConfigForm, {formScope, remote, controller}))),
      url ? h(React.Fragment, null,
        h('iframe', {src: safeWorkbenchURL(url), title: 'Simulation Advisor · 仿真方案工作台', referrerPolicy: 'no-referrer', style: {border: 0, width: '100%', flex: 1, minHeight: '240px', background: 'transparent'}}))
        : h('p', {style: {margin: '16px 12px', fontSize: '0.85rem', opacity: 0.6}}, configured ? '按需打开工作台；隐藏面板可返回会话。' : '展开“服务与配置”选择离线包。'),
    );
  }
  function WorkbenchDock({useTabInfo, hideTab, ...props}) {
    const {tab} = useTabInfo();
    // DSH retains inactive tab bodies. Unmount the actual UI and its controller
    // while hidden so neither status polls nor the embedded page keep running.
    return tab.visible ? h(WorkbenchPanel, {...props, docked: true, onHide: () => hideTab(tab.id)}) : null;
  }
  async function apply(ctx) {
    const remote = ctx.remote;
    await remote.$mount({package: PACKAGE, descriptors});
    // Remote child services enforce their own Cordis dependencies. Mount first,
    // then consume the new namespace in a child; a root dependency would wait
    // forever for the namespace this plugin itself has not mounted yet.
    ctx.inject(['remote.simWorkbenchSetup', 'slots', 'configForms', 'layout'], scoped => {
      let directoryPicker;
      scoped.inject(['remote.directoryPicker'], pickerScope => {
        directoryPicker = pickerScope.remote.directoryPicker;
        return () => { directoryPicker = undefined; };
      });
      const remoteFace = {simWorkbenchSetup: scoped.remote.simWorkbenchSetup,
        get directoryPicker() { return directoryPicker; }};
      const formScope = scoped.configForms.get(ENTRY);
      // These are tracked child effects, not our apply result: returning a
      // child Fiber would make this provider await that child's activation.
      scoped.slots.inject('plugins.row.config', () => scoped.slots.register({
        name: 'plugins.row.config', key: `${PACKAGE}#${ENTRY}`,
        inject: () => ({formScope, remote: remoteFace}),
      }, ConfigPage));
      // Keep the ordinary left navigation entry available independently of the
      // optional right dock, including when no conversation is selected.
      scoped.slots.inject('main', () => {
        const dispose = scoped.slots.register({name: 'main', key: ENTRY,
          inject: () => ({formScope, remote: remoteFace, onHide: () => scoped.layout.selectPanel(null)})}, WorkbenchPanel);
        scoped.slots.inject('sidebar.panellist', () => scoped.slots.register({
          name: 'sidebar.panellist', id: ENTRY, order: 20, label: '仿真工作台',
        }, WorkbenchIcon));
        return dispose;
      });
      scoped.inject(['sidebarRight', 'sidebarRightTabs'], dock => {
        dock.effect(() => dock.sidebarRightTabs.register({id: DOCK, kind: ENTRY,
          title: () => '仿真工作台', guide: [{id: 'open', order: 25,
            title: () => '仿真工作台', description: () => '与会话并排查看和规划仿真方案', icon: WorkbenchIcon}]}));
        dock.slots.inject('sidebar.right.pane.tab', () => dock.slots.register({
          name: 'sidebar.right.pane.tab', key: DOCK,
          inject: () => ({formScope, remote: remoteFace, hideTab: id => dock.sidebarRight.hide(id), onFullPage: () => dock.layout.selectPanel(ENTRY)}),
        }, WorkbenchDock));
      });
    });
  }
  return {inject: ['slots', 'configForms', 'remote'], apply,
    // Pure controller exports also make the shipped factory directly testable.
    createController, createPanelController, WorkbenchPanel, WorkbenchDock, pickDirectory, pathKey, safeWorkbenchURL, descriptors};
}});
