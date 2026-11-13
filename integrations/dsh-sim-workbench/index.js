import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { WorkbenchService } from './service.js';
import { WorkbenchBridge } from './bridge/index.js';
import { BridgeError, requestJSON } from './bridge/http.js';
export { Config } from './config.js';

export const name = 'sim-advisor-workbench';
export const inject = ['agents', 'tools', 'systemPrompt', 'typert'];
export const SETUP_STATUS_DESCRIPTOR = {
  id: '@local/sim-advisor-workbench#simWorkbenchSetup/status', service: 'simWorkbenchSetup',
  namespace: 'simWorkbenchSetup', method: 'status', parameters: [], result: { mode: 'src-json' }, invocation: { kind: 'direct' },
};
export const SETUP_CONFIGURE_DESCRIPTOR = { ...SETUP_STATUS_DESCRIPTOR,
  id: '@local/sim-advisor-workbench#simWorkbenchSetup/configure', method: 'configure' };
export const SETUP_START_DESCRIPTOR = { ...SETUP_STATUS_DESCRIPTOR,
  id: '@local/sim-advisor-workbench#simWorkbenchSetup/start', method: 'start' };
export const SETUP_STOP_DESCRIPTOR = { ...SETUP_STATUS_DESCRIPTOR,
  id: '@local/sim-advisor-workbench#simWorkbenchSetup/stop', method: 'stop' };
export const SETUP_VERIFY_DESCRIPTOR = { ...SETUP_STATUS_DESCRIPTOR,
  id: '@local/sim-advisor-workbench#simWorkbenchSetup/verify', method: 'verify' };
const CONFIG_PATHS = ['offlineBundle', 'dataDir', 'pythonPath', 'docRoot', 'applications', 'simPython', 'qmd', 'qmdDir', 'nodePath', 'pageindexPython', 'comsolHome'];
/** Ordinary Host config, without importing profile/shipped npm dependencies.
 * The installed reference uses schemastery for Config; file-URI bundles cannot
 * assume that package resolves, so this bundle validates its plain config here.
 */
export function normalizeConfig(config = {}) {
  if (!config || typeof config !== 'object' || Array.isArray(config)
      || Object.keys(config).some(key => ![...CONFIG_PATHS, 'startupTimeoutMs'].includes(key))) throw new BridgeError('工作台插件配置字段无效。');
  const result = { startupTimeoutMs: 30000 };
  for (const key of CONFIG_PATHS) {
    const value = key === 'dataDir' && typeof config[key] === 'string' ? config[key].trim() : config[key];
    if (value === undefined || value === '') continue;
    if (typeof value !== 'string' || /\0/.test(value)) throw new BridgeError(`${key}配置必须是路径或命令文本。`);
    if (['offlineBundle', 'dataDir'].includes(key) && (!path.isAbsolute(value) || /[\r\n]/.test(value))) {
      throw new BridgeError(key === 'dataDir' ? '工作台数据目录需要本机绝对路径；留空使用默认位置。' : '请选择已解压离线依赖包的绝对目录。');
    }
    result[key] = value;
  }
  if (config.startupTimeoutMs !== undefined) {
    if (!Number.isSafeInteger(config.startupTimeoutMs) || config.startupTimeoutMs < 1000 || config.startupTimeoutMs > 120000) throw new BridgeError('startupTimeoutMs须为1000–120000的整数。');
    result.startupTimeoutMs = config.startupTimeoutMs;
  }
  return result;
}
const OUTPUT = { schema: {}, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] };
const object = (properties, required = Object.keys(properties)) => ({ type: 'object', properties, required, additionalProperties: false });
const text = { type: 'string' };
const MODES = ['qmd_keyword', 'qmd_vector'];
const normalizeMode = mode => mode === 'native' || mode === 'qmd' ? 'qmd_keyword' : mode;
const FEATURE_BRANCHES = ['geometry', 'mesh', 'physics', 'material', 'multiphysics', 'selection', 'study', 'solver', 'result', 'param', 'func', 'variable', 'misc'];
const LIBRARY_TYPES = ['official-manuals', 'official-models', 'user-manuals', 'user-models'];
const ACTIONS = ['add', 'scan', 'build', 'index', 'search', 'list', 'scope', 'enable_many', 'status', 'cancel', 'rebind', 'read', 'configuration', 'evidence', 'enable', 'disable', 'delete', 'restore', 'purge', 'models'];
const LIBRARY_ARGS = object({
  name: text, type: { type: 'string', enum: LIBRARY_TYPES, description: '注册者声明的官方/自定义手册或模型类型，不根据文件路径推断官方身份。' },
  path: text, docs_root: text, version: text, id: text, library_id: text, job_id: text,
  paths: { type: 'array', items: text }, limit: { type: 'integer', minimum: 1, maximum: 1000000 },
  shallow: { type: 'boolean' }, enrich: { type: 'boolean', description: '默认false；仅用户明确要求语义注释时true，会联网调用已配置的注释模型，可能使用Codex。' },
  modes: { type: 'array', items: { type: 'string', enum: MODES } }, query: text,
  mode: { type: 'string', enum: MODES }, library_ids: { type: 'array', items: text },
  kind: { type: 'string', enum: ['all', 'manual', 'case', 'code'] }, feature_branch: { type: 'string', enum: FEATURE_BRANCHES },
  include_deleted: { type: 'boolean' }, enabled: { type: 'boolean' },
  confirmed: { type: 'boolean', description: '仅用户明确要求不可恢复删除该库时为true；原始文件保留。' },
  root_id: text, category: text, node_path: text, source: { type: 'string', enum: ['api', 'saved'] },
  index: { type: 'integer', minimum: 0 }, line: { type: 'integer', minimum: 1 },
}, []);

function string(value, field) {
  if (typeof value !== 'string' || !value.trim() || value.startsWith('-') || /[\0\r\n]/.test(value) || value.length > 16000) {
    throw new BridgeError(`${field} 需要非空文本，不能以-开头或包含控制换行。`);
  }
  return value;
}

/** Typed operation -> fixed library_cli.py argv; no shell or raw argv escape. */
export function libraryArgv(action, args = {}) {
  const allowed = {
    add: ['name', 'type', 'path', 'docs_root', 'version', 'id'], scan: ['library_id', 'paths', 'limit'],
    build: ['library_id', 'paths', 'limit', 'shallow', 'enrich', 'modes'], index: ['modes'],
    search: ['query', 'mode', 'library_ids', 'limit', 'kind', 'feature_branch'], list: ['include_deleted'], scope: ['query', 'type'], enable_many: ['library_ids', 'enabled'], status: ['library_id'], cancel: ['job_id'],
    enable: ['library_id'], disable: ['library_id'], delete: ['library_id'], restore: ['library_id'], models: ['library_id', 'limit'],
    purge: ['library_id', 'confirmed'],
    rebind: ['library_id', 'root_id', 'path'], read: ['id'],
    configuration: ['id', 'category', 'node_path', 'source', 'limit'], evidence: ['id', 'index', 'line', 'limit'],
  };
  if (!Object.hasOwn(allowed, action) || !args || typeof args !== 'object' || Array.isArray(args)
      || Object.keys(args).some(key => !allowed[action].includes(key))) throw new BridgeError('资料库操作或参数不在白名单；不支持任意argv或data-dir覆盖。');
  const argv = [action === 'enable_many' ? 'enable-many' : action];
  const addText = (flag, key, fallback) => {
    const value = args[key] ?? fallback;
    if (value !== undefined) argv.push(flag, string(value, key));
  };
  const addInt = (flag, key, fallback, minimum = 1) => {
    const value = args[key] ?? fallback;
    if (value === undefined) return;
    if (!Number.isSafeInteger(value) || value < minimum || value > 1000000) throw new BridgeError(`${key} 需要范围内整数。`);
    argv.push(flag, String(value));
  };
  const addList = (flag, key) => {
    if (args[key] === undefined) return;
    if (!Array.isArray(args[key]) || args[key].length > 1000) throw new BridgeError(`${key} 需要有界文本数组。`);
    for (const value of args[key]) argv.push(flag, string(value, key));
  };
  const addModes = () => {
    const values = Array.isArray(args.modes) ? [...new Set(args.modes.map(normalizeMode))] : args.modes ?? ['qmd_keyword'];
    if (!Array.isArray(values) || !values.length || values.some(value => !MODES.includes(value))) throw new BridgeError('modes请选择qmd_keyword/qmd_vector。');
    argv.push('--modes', [...new Set(values)].join(','));
  };
  if (action === 'add') {
    if (!LIBRARY_TYPES.includes(args.type)) throw new BridgeError('type不是支持的资料库类型。');
    argv.push(string(args.name, 'name'), '--type', args.type, '--path', string(args.path, 'path'));
    addText('--docs-root', 'docs_root'); addText('--version', 'version'); addText('--id', 'id');
  } else if (['scan', 'build', 'status', 'rebind'].includes(action)) {
    argv.push(string(args.library_id, 'library_id'));
    if (action === 'scan' || action === 'build') { addList('--path', 'paths'); addInt('--limit', 'limit'); }
    if (action === 'build') {
      for (const key of ['shallow', 'enrich']) {
        if (args[key] !== undefined && typeof args[key] !== 'boolean') throw new BridgeError(`${key}需要显式布尔值。`);
        if (args[key] === true) argv.push('--' + key);
      }
      addModes();
    }
    if (action === 'rebind') argv.push('--root-id', string(args.root_id ?? 'main', 'root_id'), '--path', string(args.path, 'path'));
  } else if (action === 'index') addModes();
  else if (action === 'list') {
    if (args.include_deleted !== undefined && typeof args.include_deleted !== 'boolean') throw new BridgeError('include_deleted需要显式布尔值。');
    if (args.include_deleted) argv.push('--include-deleted');
  } else if (action === 'scope') {
    if (args.type !== undefined && !LIBRARY_TYPES.includes(args.type)) throw new BridgeError('type不是支持的资料库类型。');
    addText('--query', 'query'); addText('--type', 'type');
  } else if (action === 'enable_many') {
    if (!Array.isArray(args.library_ids) || !args.library_ids.length || typeof args.enabled !== 'boolean') throw new BridgeError('enable_many需要非空library_ids数组与显式enabled布尔值。');
    addList('--library', 'library_ids'); argv.push('--enabled', String(args.enabled));
  } else if (['enable', 'disable', 'delete', 'restore', 'purge', 'models'].includes(action)) {
    argv.push(string(args.library_id, 'library_id'));
    if (action === 'purge') {
      if (args.confirmed !== true) throw new BridgeError('彻底删除不可恢复，需用户明确确认后传confirmed:true。');
      argv.push('--confirm');
    }
    if (action === 'models') addInt('--limit', 'limit');
  }
  else if (action === 'cancel') {
    if (typeof args.job_id !== 'string' || !/^[a-f0-9]{32}$/.test(args.job_id)) throw new BridgeError('job_id必须是构建任务的32位标识。');
    argv.push(args.job_id);
  }
  else if (action === 'search') {
    const mode = normalizeMode(args.mode ?? 'qmd_keyword');
    if (!MODES.includes(mode)) throw new BridgeError('检索mode无效。');
    if (args.kind !== undefined && !['all', 'manual', 'case', 'code'].includes(args.kind)) throw new BridgeError('检索kind无效。');
    if (args.feature_branch !== undefined && !FEATURE_BRANCHES.includes(args.feature_branch)) throw new BridgeError('feature_branch不是支持的建模分支。');
    argv.push(string(args.query, 'query'), '--mode', mode); addList('--library', 'library_ids'); addInt('--limit', 'limit', 8);
    addText('--kind', 'kind'); addText('--feature-branch', 'feature_branch');
  } else if (['read', 'configuration', 'evidence'].includes(action)) {
    argv.push(string(args.id, 'id'));
    if (action === 'configuration') {
      if (args.source !== undefined && !['api', 'saved'].includes(args.source)) throw new BridgeError('source必须为api或saved。');
      addText('--category', 'category'); addText('--node-path', 'node_path'); addText('--source', 'source'); addInt('--limit', 'limit', 20);
    }
    if (action === 'evidence') { addInt('--index', 'index', 0, 0); addInt('--line', 'line'); addInt('--limit', 'limit', 120); }
  }
  return argv;
}

class TrackedBridge extends WorkbenchBridge {
  constructor(ctx, host, options) { super(ctx, options); this.host = host; }
  async bind(agent, url, signal) {
    if (this.host.stopping || this.host.closed) throw new BridgeError('工作台正在停止或插件已卸载。');
    const epoch = this.host.serviceEpoch;
    const owner = this.host.service.status();
    const ownedOrigin = owner.owned && owner.url ? new URL(owner.url).origin : null;
    const result = await super.bind(agent, url, signal);
    // A registration can finish after stop has already drained tracked bindings.
    // Only invalidate this Host's owned origin; foreign workbenches stay bound.
    if (result.bound && ownedOrigin === new URL(result.url).origin
        && (this.host.closed || epoch !== this.host.serviceEpoch)) {
      await super.unbind(agent);
      throw new BridgeError('连接期间工作台已停止；绑定已移除，请启动后重新连接。');
    }
    if (result.bound) this.host.bindings.set(agent.id, { agent, ...result });
    return result;
  }
  async unbind(agent) {
    try { return await super.unbind(agent); }
    finally { this.host.bindings.delete(agent.id); }
  }
}

export class WorkbenchHost {
  constructor(ctx, config = {}, dependencies = {}) {
    this.ctx = ctx;
    const clean = normalizeConfig(config);
    this.offlineBundle = clean.offlineBundle;
    this.dataDirSetting = clean.dataDir ?? '';
    this.configuration = { revision: 0, state: 'unconfigured', bundle_root: clean.offlineBundle ?? '',
      data_dir_setting: this.dataDirSetting, detail: '请选择已解压的离线依赖包目录。' };
    this.configurationQueue = Promise.resolve();
    const serviceOptions = { ...clean, packageRoot: fileURLToPath(new URL('.', import.meta.url)),
      onSetupProgress: detail => { if (this.configuration.state === 'checking') this.configuration.detail = detail; ctx.logger?.info?.(detail); } };
    this.createService = dependencies.createService ?? (options => new WorkbenchService({ ...serviceOptions, ...options }));
    this.service = dependencies.service ?? this.createService(clean);
    this.request = dependencies.request ?? requestJSON;
    this.bindings = new Map(); this.states = new Map(); this.disposers = [];
    this.bridge = dependencies.bridge ?? new TrackedBridge(ctx, this, { request: this.request });
    this.closed = this.stopping = false;
    this.serviceEpoch = 0;
  }

  start() {
    this.bridge.start();
    this.disposers.push(this.ctx.on('agent/created', ({ agent }) => this.attach(agent), { global: true }));
    for (const agent of this.ctx.agents.list()) this.attach(agent);
    return this;
  }

  configurationStatus() {
    const service = this.service.status();
    return { ...this.configuration, data_dir: service.data_dir,
      workbench: {running: service.running === true, owned: service.owned === true,
        state: service.state ?? (service.running ? 'running' : 'stopped'),
        url: service.running ? service.url ?? null : null, pid: service.pid ?? null,
        detail: service.detail ?? ''} };
  }

  /** The settings button starts the same owned service as the Agent tool. */
  async launchWorkbench() {
    if (this.closed || this.stopping) throw new BridgeError('工作台正在停止或插件已卸载。');
    if (this.configuration.state !== 'ready') throw new BridgeError('请先完成离线依赖配置，再启动工作台。');
    const revision = this.configuration.revision;
    const service = this.service;
    const started = await service.start();
    if (this.closed || this.stopping || revision !== this.configuration.revision || this.configuration.state !== 'ready') {
      await service.stop();
      throw new BridgeError('依赖配置已变化，请完成配置后重新启动工作台。');
    }
    return started;
  }

  /** A failed selection stays editable; the previous successful profile is retained. */
  configureOffline(bundleRoot, dataDir = this.dataDirSetting, {forceVerify = false, allowInitialize = true} = {}) {
    const requestedDataDir = typeof dataDir === 'string' ? dataDir.trim() : dataDir;
    if (!forceVerify && this.configuration.state === 'checking' && bundleRoot === this.configuration.bundle_root
        && requestedDataDir === this.configuration.data_dir_setting) return this.configurationQueue;
    const revision = this.configuration.revision + 1;
    this.configuration = { revision, state: 'checking', bundle_root: bundleRoot, data_dir_setting: requestedDataDir,
      detail: '正在配置所选离线依赖包与数据目录。' };
    this.configurationQueue = this.configurationQueue.catch(() => {}).then(async () => {
      if (this.closed || revision !== this.configuration.revision) return this.configurationStatus();
      try {
        const selectedConfig = normalizeConfig({ offlineBundle: bundleRoot, dataDir: requestedDataDir });
        const selected = selectedConfig.offlineBundle;
        if (!selected) throw new BridgeError('请选择已解压的离线依赖包目录。');
        const selectedDataDir = selectedConfig.dataDir ?? '';
        if (selectedDataDir !== this.dataDirSetting) {
          // Recreate the owner with a new root; do not copy or merge user data.
          const replacement = this.createService({ dataDir: selectedConfig.dataDir, offlineBundle: selected });
          await this.stop();
          if (this.closed || revision !== this.configuration.revision) { await replacement.stop(); return this.configurationStatus(); }
          this.service = replacement;
          this.dataDirSetting = selectedDataDir;
        } else if (this.service.status().owned) await this.stop();
        if (this.closed || revision !== this.configuration.revision) return this.configurationStatus();
        this.offlineBundle = selected;
        const result = await this.service.configureOffline(selected, {forceVerify, allowInitialize});
        if (!this.closed && revision === this.configuration.revision) this.configuration = { revision, state: 'ready', bundle_root: selected,
          data_dir_setting: selectedDataDir,
          detail: result.dependency_setup?.detail ?? '离线依赖校验与配置完成。' };
      } catch (error) {
        if (!this.closed && revision === this.configuration.revision) this.configuration = { revision, state: 'error', bundle_root: bundleRoot,
          data_dir_setting: requestedDataDir,
          detail: error instanceof Error ? error.message : '离线依赖配置失败，请重新选择目录。' };
      }
      return this.configurationStatus();
    });
    return this.configurationQueue;
  }

  attach(agent) {
    if (this.closed || this.states.has(agent.id)) return;
    const state = { agent, resources: [], creating: false, closed: false, dispose: null };
    this.states.set(agent.id, state);
    const add = (name, description, parameters, execute) => state.resources.push(agent.ctx.tools.register({ name, description, parameters, output: OUTPUT,
      execute: async (args, exec) => {
        if (this.closed || state.closed || exec?.agent?.id !== agent.id || exec.agent.session !== agent.session) throw new BridgeError('工具不属于当前DSH会话或已卸载。');
        return execute(args ?? {}, exec);
      } }));
    try {
      add('start_workbench', '懒启动本插件自带的本机仿真方案工作台，返回可点击URL；不创建方案或调用模型。创建并绑定请另用create_workbench_plan。', object({}), async () => {
        if (this.stopping) throw new BridgeError('工作台正在停止。');
        return this.service.start();
      });
      add('workbench_status', '查看本插件工作台进程、离线配置与数据目录；不启动进程、不读取登录凭据。', object({}), async () => ({ ...this.service.status(), offline_setup: this.configurationStatus() }));
      add('stop_workbench', '仅用户明确要求时停止本插件拥有的工作台进程，并解除其方案绑定；保留资料和方案，不停止用户自行启动的服务或DSH。', object({}), async () => this.stop());
      add('create_workbench_plan', '显式创建新仿真方案并绑定当前DSH会话；已有绑定时先返回冲突，不覆盖原绑定。model_path可选，读取STEP/STP/MPH原件，不求解。', object({
        goal: { type: 'string', minLength: 8, maxLength: 12000 }, model_path: text,
      }, ['goal']), async (args, exec) => this.createPlan(state, args, exec.signal));
      add('workbench_library', '资料库登记/扫描/构造/索引/查询/重绑定与证据读取。先scope查询轻量目录、分组与启停（包括关闭库），可按query/type筛选；enable_many用library_ids与enabled显式批量启停所选库，无需用户确认、不构建索引，按需选库不要自动全部启用。兼容enable/disable、可恢复delete/restore、不可恢复purge及只读models文件列表。构建默认覆盖登记目录全部文件，可显式limit小样本；index/build默认仅qmd_keyword，项目术语扩展后以OR执行一次BM25；qmd_vector默认关闭，需显式选择。enrich默认关闭，仅用户明确要求时设true。delete保留原文件、历史引用与索引；purge需confirmed:true且用户明确确认不可恢复清除，仅适用已删除库，保留原文件及其它库副本。', object({
        action: { type: 'string', enum: ACTIONS }, args: LIBRARY_ARGS,
      }, ['action']), async args => {
        if (this.stopping) throw new BridgeError('工作台正在停止。');
        const argv = libraryArgv(args.action, args.args);
        const result = await this.service.runCli('library_cli.py', argv);
        return { action: args.action, result };
      });
      state.dispose = agent.ctx.effect(() => () => {
        state.closed = true;
        for (const dispose of state.resources.splice(0).reverse()) dispose();
        this.states.delete(agent.id);
      }, 'sim-advisor-workbench.agent');
    } catch (error) {
      for (const dispose of state.resources.splice(0).reverse()) dispose();
      this.states.delete(agent.id);
      throw error;
    }
  }

  async createPlan(state, args, signal) {
    if (this.stopping || this.bindings.has(state.agent.id) || state.creating) throw new BridgeError('当前DSH会话已有绑定、正在创建或工作台正在停止；请先明确unbind_workbench。');
    if (typeof args.goal !== 'string' || args.goal.trim().length < 8 || args.goal.length > 12000
        || Object.keys(args).some(key => !['goal', 'model_path'].includes(key))) throw new BridgeError('创建方案需要8–12000字符goal，可选model_path。');
    if (args.model_path !== undefined && (typeof args.model_path !== 'string' || !args.model_path.trim())) throw new BridgeError('model_path必须为非空路径。');
    state.creating = true;
    let created;
    try {
      const service = await this.service.start();
      if (this.closed || state.closed || this.stopping || signal?.aborted) throw new BridgeError('创建已取消或工作台正在停止；未提交新方案。');
      const origin = new URL(service.url).origin;
      const body = { goal: args.goal.trim(), ...(args.model_path === undefined ? {} : { model_path: args.model_path }) };
      created = await this.request(origin, '/api/sessions', body, { signal, timeout: 60000 });
      if (!/^[a-f0-9]{32}$/.test(created.id ?? '')) throw new BridgeError('工作台创建响应缺少有效方案ID。');
      const url = `${origin}/?session=${created.id}`;
      try {
        const bound = await this.bridge.bind(state.agent, url, signal);
        return { created: true, ...bound, session_id: created.id, url };
      } catch {
        return { created: true, bound: false, session_id: created.id, url,
          note: '方案已创建，但当前会话绑定未完成；未覆盖已有绑定。请检查状态后用bind_workbench连接此链接。' };
      }
    } finally { state.creating = false; }
  }

  async stop() {
    if (this.stopping) throw new BridgeError('工作台正在停止。');
    this.stopping = true;
    this.serviceEpoch++;
    try {
      const state = this.service.status();
      if (state.owned && state.url) {
        const origin = new URL(state.url).origin;
        for (const binding of [...this.bindings.values()]) {
          if (new URL(binding.url).origin === origin) await this.bridge.unbind(binding.agent);
        }
      }
      return await this.service.stop();
    } finally { this.stopping = false; }
  }

  async dispose() {
    if (this.closed) return;
    this.closed = true;
    for (const dispose of this.disposers.splice(0).reverse()) dispose();
    await this.bridge.dispose();
    for (const state of [...this.states.values()]) await state.dispose?.();
    await this.service.stop();
  }
}

export async function startPlugin(ctx, config = {}, dependencies = {}) {
  const host = new WorkbenchHost(ctx, config, dependencies);
  ctx.effect(() => () => host.dispose(), 'sim-advisor-workbench.lifecycle');
  try {
    if (host.offlineBundle) await host.configureOffline(host.offlineBundle, host.dataDirSetting, {allowInitialize: false});
    if (host.closed) throw new BridgeError('插件已卸载，依赖配置已结束。');
    return host.start();
  } catch (error) {
    await host.dispose();
    throw error;
  }
}

export async function apply(ctx, config = {}) {
  const snapshot = () => Object.fromEntries(Object.entries(config).map(([key, value]) =>
    [key, ['offlineBundle', 'dataDir'].includes(key) && value && typeof value.get === 'function' ? value.get() : value]));
  const host = await startPlugin(ctx, snapshot());
  const setupService = { status: () => host.configurationStatus(), start: () => host.launchWorkbench(), stop: () => {
    if (host.closed) throw new BridgeError('插件已卸载，无法执行工作台操作。');
    return host.stop();
  }, verify: () => {
    const workbench = host.service.status();
    if (host.stopping || workbench.running || workbench.owned || ['starting', 'stopping'].includes(workbench.state)) throw new BridgeError('请先停止工作台，再手动重新校验离线包。');
    const selected = snapshot();
    void host.configureOffline(selected.offlineBundle, selected.dataDir ?? '', {forceVerify: true});
    return host.configurationStatus();
  }, configure: () => {
    const selected = snapshot();
    void host.configureOffline(selected.offlineBundle, selected.dataDir ?? '');
    return host.configurationStatus();
  } };
  Object.defineProperty(setupService, 'typertRemote', { value: {
    service: setupService, serviceKey: 'simWorkbenchSetup', namespace: 'simWorkbenchSetup',
  } });
  ctx.provide('simWorkbenchSetup', setupService);
  ctx.typert.register({ package: '@local/sim-advisor-workbench', face: 'host', model: {}, schemas: [], invocations: [SETUP_STATUS_DESCRIPTOR, SETUP_CONFIGURE_DESCRIPTOR, SETUP_START_DESCRIPTOR, SETUP_STOP_DESCRIPTOR, SETUP_VERIFY_DESCRIPTOR] });
  ctx.on('loader/volatile-update', paths => {
    if (paths.some(parts => ['offlineBundle', 'dataDir'].includes(parts[0]))) {
      const selected = snapshot();
      void host.configureOffline(selected.offlineBundle, selected.dataDir ?? '');
    }
  });
}
