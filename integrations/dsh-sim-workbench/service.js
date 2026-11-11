/** Own one lazy, package-independent workbench process. No profile/credential IO. */
import { spawn } from 'node:child_process';
import { access, mkdir, readFile, realpath, stat } from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { loadDependencyProfile } from './dependency-profile.js';
import { prepareOfflineBundle } from './offline-setup.js';

export class WorkbenchServiceError extends Error {}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const CLI_NAMES = new Set(['library_cli.py', 'tools_cli.py', 'agent_cli.py', 'agent_tools.py']);
const PROBE = `import sys,json,sqlite3,http.server,tomllib,hashlib
result={'version':list(sys.version_info[:3]),'executable':sys.executable,'fts5':False,'server_import':False}
try:
 db=sqlite3.connect(':memory:');db.execute('CREATE VIRTUAL TABLE probe_fts USING fts5(text)');db.execute('DROP TABLE probe_fts');db.close();result['fts5']=True
 sys.path.insert(0,sys.argv[1]);import advisor.server;result['server_import']=True
except Exception as error:result['error_type']=type(error).__name__
print(json.dumps(result))`;
const clean = value => typeof value === 'string' && value.trim() ? value : undefined;
const inside = (root, candidate) => { const relative = path.relative(root, candidate); return relative === '' || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative)); };

function defaultDataDir(env) {
  if (process.platform === 'win32') return path.join(env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'sim-advisor-workbench');
  if (process.platform === 'darwin') return path.join(os.homedir(), 'Library', 'Application Support', 'sim-advisor-workbench');
  return path.join(env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'), 'sim-advisor-workbench');
}

async function canonical(target) {
  const missing = []; let current = path.resolve(target);
  while (true) {
    try { return path.join(await realpath(current), ...missing.reverse()); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      const parent = path.dirname(current); if (parent === current) throw error;
      missing.push(path.basename(current)); current = parent;
    }
  }
}

function localOrigin(value) {
  try {
    const url = new URL(value);
    if (url.protocol === 'http:' && url.hostname === '127.0.0.1' && url.port && !url.username && !url.password && !url.search && !url.hash && url.pathname === '/') return url.origin;
  } catch { /* Invalid readiness metadata is never an instruction to connect. */ }
  return null;
}

function getJSON(origin, route, timeout = 1000, maxBytes = 2 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const request = http.get(new URL(route, origin), { signal: AbortSignal.timeout(timeout), headers: { Accept: 'application/json' } }, response => {
      const chunks = []; let bytes = 0;
      response.on('data', chunk => { bytes += chunk.length; if (bytes > maxBytes) { response.destroy(); reject(new Error('oversize')); } else chunks.push(chunk); });
      response.on('end', () => { try { if (response.statusCode !== 200) throw new Error('status'); resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); } catch { reject(new Error('invalid readiness')); } });
      response.on('error', reject);
    });
    request.on('error', reject);
  });
}

/** Options are paths/configuration, not shell commands. All methods retain data. */
export class WorkbenchService {
  #child = null;
  #children = new Set();
  #starting = null;
  #stopping = null;
  #generation = 0;
  #stopEpoch = 0;
  #snapshot;
  #python = null;
  #dependencyOptions = {};
  #offlineSetup = null;
  #validatedOffline = null;
  #copiedLegacyServer = false;

  constructor(options = {}) {
    if (typeof options.packageRoot !== 'string' || !options.packageRoot) throw new WorkbenchServiceError('packageRoot 必须是插件包目录。');
    this.packageRoot = path.resolve(options.packageRoot);
    this.runtimeRoot = path.join(this.packageRoot, 'runtime');
    this.options = options;
    this.env = { ...(options.env ?? process.env) };
    this.dataDir = path.resolve(clean(options.dataDir) || clean(this.env.SIM_ADVISOR_PLUGIN_DATA) || defaultDataDir(this.env));
    if (inside(this.packageRoot, this.dataDir)) throw new WorkbenchServiceError('dataDir 必须位于插件包外；插件包只保存程序资源。');
    this.startupTimeoutMs = options.startupTimeoutMs ?? 30000;
    if (!Number.isFinite(this.startupTimeoutMs) || this.startupTimeoutMs < 100 || this.startupTimeoutMs > 300000) throw new WorkbenchServiceError('startupTimeoutMs 需为100至300000毫秒。');
    this.spawnProcess = options.spawnProcess ?? spawn;
    this.prepareOfflineBundle = options.prepareOfflineBundle ?? prepareOfflineBundle;
    this.#snapshot = { running: false, owned: false, url: null, pid: null, data_dir: this.dataDir, python: null, state: 'stopped', detail: '工作台尚未启动。' };
  }

  status() { return { ...this.#snapshot }; }

  #environment() {
    const env = { ...this.env, SIM_ADVISOR_DATA: this.dataDir, SIM_ADVISOR_AGENT_BACKEND: 'none', SIM_ADVISOR_DSH_EMBED: '1',
      PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' };
    if (this.#dependencyOptions.pythonPath) {
      delete env.PYTHONPATH; delete env.PYTHONHOME; delete env.PYTHONUSERBASE;
      env.PYTHONNOUSERSITE = '1';
    }
    const paths = { docRoot: 'COMSOL_DOC_ROOT', applications: 'COMSOL_APPLICATIONS', simPython: 'SIM_ADVISOR_SIM_PYTHON',
      qmd: 'SIM_ADVISOR_QMD', qmdDir: 'SIM_ADVISOR_QMD_DIR', nodePath: 'SIM_ADVISOR_NODE',
      embedModelPath: 'SIM_ADVISOR_EMBED_MODEL', pageindexPython: 'SIM_ADVISOR_PAGEINDEX_PYTHON', comsolHome: 'SIM_ADVISOR_COMSOL_HOME' };
    for (const [option, variable] of Object.entries(paths)) {
      const value = clean(this.options[option]) || clean(env[variable]) || clean(this.#dependencyOptions[option]);
      if (value) env[variable] = value;
    }
    env.SIM_ADVISOR_SEARCH_CACHE = clean(this.options.searchCache) || clean(env.SIM_ADVISOR_SEARCH_CACHE) || path.join(this.dataDir, 'cache', 'library-search');
    return env;
  }

  async #prepareDirectories() {
    let root, data;
    try { root = await realpath(this.packageRoot); data = await canonical(this.dataDir); }
    catch { throw new WorkbenchServiceError('无法访问插件或数据目录，请检查路径和本机权限。'); }
    if (inside(root, data)) throw new WorkbenchServiceError('dataDir 解析后位于插件包内，请选择包外持久目录。');
    let offlineRoot;
    if (clean(this.options.offlineBundle)) {
      try { offlineRoot = await realpath(this.options.offlineBundle); } catch { /* The initializer reports a missing bundle. */ }
    }
    if (offlineRoot && inside(offlineRoot, data)) throw new WorkbenchServiceError('工作台数据目录必须位于离线依赖包外。');
    try {
      if (!(await stat(path.join(this.runtimeRoot, 'run.py'))).isFile()) throw new Error('missing');
      await mkdir(this.dataDir, { recursive: true });
      await mkdir(path.join(this.dataDir, 'cache', 'library-search'), { recursive: true });
    } catch { throw new WorkbenchServiceError('缺少 runtime/run.py 或数据目录不可写；请检查完整插件包与 dataDir。'); }
  }

  async #loadDependencies() {
    try {
      const dependencies = await loadDependencyProfile(this.dataDir);
      this.#dependencyOptions = dependencies.options;
      this.#snapshot = { ...this.#snapshot, dependency_profile: null, dependency_bundle: null,
        mcp_python: null, embedding_model: null, ...dependencies.status };
      const environment = this.#environment();
      this.#snapshot = { ...this.#snapshot, configured_node: environment.SIM_ADVISOR_NODE ?? null,
        configured_qmd_dir: environment.SIM_ADVISOR_QMD_DIR ?? null,
        embedding_model: environment.SIM_ADVISOR_EMBED_MODEL ?? this.#snapshot.embedding_model,
        search_cache: environment.SIM_ADVISOR_SEARCH_CACHE };
    } catch (error) { throw new WorkbenchServiceError(error.message); }
  }

  async #assertDataIdle() {
    const existing = await this.#serverFile();
    if (!Number.isSafeInteger(existing?.pid) || existing.pid <= 0) return;
    let alive = false;
    try { process.kill(existing.pid, 0); alive = true; } catch (error) { alive = error.code === 'EPERM'; }
    if (alive) throw new WorkbenchServiceError('此数据目录已有运行中的服务；不会接管或停止它，请改用独立 dataDir。');
  }

  #externalPaths() {
    const paths = {};
    for (const [key, variable] of Object.entries({ comsolHome: 'SIM_ADVISOR_COMSOL_HOME', docRoot: 'COMSOL_DOC_ROOT', applications: 'COMSOL_APPLICATIONS' })) {
      const value = clean(this.options[key]) || clean(this.env[variable]);
      if (value) paths[key] = value;
    }
    return paths;
  }

  #offlineSelection() {
    const bundle = clean(this.options.offlineBundle);
    return bundle ? JSON.stringify([path.resolve(bundle), this.dataDir, this.#externalPaths()]) : null;
  }

  /** Configure the selected bundle without a shell, before any workbench starts. */
  configureOffline(bundleRoot, { forceVerify = false, allowInitialize = true } = {}) {
    if (typeof forceVerify !== 'boolean' || typeof allowInitialize !== 'boolean') return Promise.reject(new WorkbenchServiceError('离线配置选项必须为布尔值。'));
    if (bundleRoot !== undefined) this.options.offlineBundle = bundleRoot;
    if (!clean(this.options.offlineBundle)) return Promise.resolve(this.status());
    if (this.#offlineSetup) return this.#offlineSetup;
    if (this.#stopping) return Promise.reject(new WorkbenchServiceError('工作台正在停止，请完成后重新配置。'));
    const generation = this.#generation;
    const selectedBundle = this.options.offlineBundle;
    const externalPaths = this.#externalPaths();
    const selection = this.#offlineSelection();
    // A completed selection avoids repeating setup before its following start.
    // Across owners/stops the helper reuses its persisted proof by default.
    this.#validatedOffline = null;
    this.#offlineSetup = (async () => {
      try {
        await this.#prepareDirectories();
        await this.#assertDataIdle();
        const progress = value => {
          const detail = typeof value === 'string' ? value : value.detail;
          this.#snapshot = { ...this.#snapshot, dependency_setup: { state: 'configuring', detail } };
          this.options.onSetupProgress?.(detail);
        };
        progress('正在配置所选离线依赖包。');
        const result = await this.prepareOfflineBundle({ bundleRoot: selectedBundle, dataDir: this.dataDir,
          externalPaths, forceVerify, allowInitialize, onProgress: progress,
          run: (command, args, options) => {
            if (generation !== this.#generation) throw new WorkbenchServiceError('依赖配置已取消。');
            return this.#run(command, args, options);
          } });
        if (generation !== this.#generation) throw new WorkbenchServiceError('依赖配置已取消。');
        await this.#loadDependencies();
        // An explicit configuration checks Python once. Startup/reload reuses
        // that configured interpreter and launches the service directly.
        if (allowInitialize || forceVerify) this.#python = await this.#choosePython({probe: true});
        if (generation !== this.#generation || selection !== this.#offlineSelection()) throw new WorkbenchServiceError('依赖配置已取消或选择已变化，请重新配置。');
        this.#validatedOffline = selection;
        this.#snapshot = { ...this.#snapshot, dependency_setup: { state: 'ready', reused: result.reused,
          detail: result.reused ? (forceVerify ? '离线依赖完整校验通过。' : '已读取已有离线依赖配置。') : '离线依赖校验与配置完成。' } };
        return this.status();
      } catch (error) {
        const detail = error instanceof Error ? error.message : '离线依赖配置失败，请检查所选目录。';
        this.#snapshot = { ...this.#snapshot, dependency_setup: { state: 'error', detail } };
        throw new WorkbenchServiceError(detail);
      }
    })().finally(() => { this.#offlineSetup = null; });
    return this.#offlineSetup;
  }

  async #prepare() {
    await this.#prepareDirectories();
    if (this.#offlineSelection() !== this.#validatedOffline) await this.configureOffline(undefined, { allowInitialize: false });
    const configuredProfile = this.#snapshot.dependency_profile;
    await this.#loadDependencies();
    if (configuredProfile && clean(this.options.offlineBundle) && !this.#snapshot.dependency_profile) {
      this.#validatedOffline = null;
      throw new WorkbenchServiceError('离线依赖配置缺失；请在插件设置中配置所选离线包后再启动。');
    }
  }

  #spawn(command, args, options = {}) {
    if (this.#stopping) throw new WorkbenchServiceError('操作已取消，工作台正在停止。');
    const child = this.spawnProcess(command, args, { cwd: this.dataDir, env: this.#environment(), shell: false,
      windowsHide: true, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'], ...options });
    this.#children.add(child);
    child.once('close', () => this.#children.delete(child));
    return child;
  }

  async #kill(child, grace = 1500) {
    if (!child || !this.#children.has(child)) return true;
    if (!child.pid || child.pid === process.pid) return false;
    const closed = new Promise(resolve => child.once('close', () => resolve(true)));
    if (process.platform === 'win32') {
      await new Promise(resolve => {
        let killer;
        try { killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, shell: false, stdio: 'ignore' }); }
        catch { resolve(); return; }
        const timer = setTimeout(() => { killer.kill(); resolve(); }, 3000); timer.unref?.();
        killer.once('error', () => { clearTimeout(timer); resolve(); });
        killer.once('close', () => { clearTimeout(timer); resolve(); });
      });
    } else {
      try { process.kill(-child.pid, 'SIGTERM'); } catch { /* Already exited. */ }
    }
    const ended = await Promise.race([closed, sleep(grace).then(() => false)]);
    if (!ended && child.exitCode === null && child.signalCode === null) {
      if (process.platform !== 'win32') { try { process.kill(-child.pid, 'SIGKILL'); } catch {} }
      else { try { child.kill(); } catch { /* Exit must still be confirmed below. */ } }
      await Promise.race([new Promise(resolve => child.once('close', resolve)), sleep(1000)]);
    }
    return !this.#children.has(child) || child.exitCode !== null || child.signalCode !== null;
  }

  async #run(command, args, { timeoutMs = 10000, maxOutput = 65536, cwd, onProgress } = {}) {
    return new Promise((resolve, reject) => {
      let child;
      try { child = this.#spawn(command, args, cwd ? { cwd } : {}); } catch (error) { reject(error instanceof WorkbenchServiceError ? error : new WorkbenchServiceError('无法启动配置的程序；请提供真实可执行文件路径。')); return; }
      const stdout = []; let bytes = 0, reason = null;
      const abort = message => { if (!reason) { reason = message; void this.#kill(child, 200); } };
      const timer = setTimeout(() => abort('操作超时；未自动重试，请检查当前状态。'), timeoutMs);
      child.stdout.on('data', chunk => { bytes += chunk.length; if (bytes > maxOutput) abort('输出超过大小限制，请缩小或分页操作。'); else { stdout.push(chunk); onProgress?.(chunk.toString('utf8')); } });
      child.stderr.on('data', () => {}); // Never pass raw subprocess diagnostics to an Agent.
      child.once('error', () => { clearTimeout(timer); reject(new WorkbenchServiceError('无法执行所选程序；请检查安装与路径。')); });
      child.once('close', code => {
        clearTimeout(timer);
        if (reason) reject(new WorkbenchServiceError(reason));
        else if (code !== 0) reject(new WorkbenchServiceError('程序未成功完成；请检查参数、依赖和数据目录权限。'));
        else resolve(Buffer.concat(stdout).toString('utf8'));
      });
    });
  }

  async #resolveExecutable(value) {
    if (/[\\/]/.test(value) || path.isAbsolute(value)) {
      try { if ((await stat(value)).isFile()) return path.resolve(value); } catch {}
      return null;
    }
    const extensions = process.platform === 'win32' && !path.extname(value) ? ['.exe', ''] : [''];
    for (const directory of (this.env.PATH || this.env.Path || '').split(path.delimiter)) {
      if (!directory) continue;
      for (const extension of extensions) {
        const candidate = path.join(directory.replace(/^"|"$/g, ''), value + extension);
        try { if ((await stat(candidate)).isFile()) return path.resolve(candidate); } catch {}
      }
    }
    return null;
  }

  #embeddedCandidates() {
    const binary = process.platform === 'win32' ? 'python.exe' : 'python3';
    const roots = [this.env.DSH_PRIMARY_RUNTIME, this.env.DSH_BUNDLED_PRIMARY_RUNTIME].filter(Boolean);
    const executableDir = path.dirname(this.options.execPath || process.execPath);
    roots.push(path.resolve(executableDir, '../../..'), path.join(executableDir, 'resources/runtime/primary-runtime'));
    return roots.flatMap(root => [path.join(root, 'dependencies/python/bin', binary), path.join(root, 'dependencies/python', binary)])
      .concat([path.resolve(executableDir, '../../python/bin', binary)], this.options.embeddedPythonPaths || []);
  }

  async #choosePython({probe = false} = {}) {
    const explicit = clean(this.options.pythonPath) || clean(this.env.SIM_ADVISOR_PYTHON) || clean(this.#dependencyOptions.pythonPath);
    const candidates = explicit ? [explicit] : [...this.#embeddedCandidates(), 'python3.13', 'python3.12', 'python3', 'python'];
    for (const candidate of [...new Set(candidates)]) {
      const executable = await this.#resolveExecutable(candidate);
      if (!executable || /[\\/]microsoft[\\/]windowsapps[\\/]/i.test(executable)) continue;
      if (!probe) return executable;
      try {
        const value = JSON.parse((await this.#run(executable, ['-c', PROBE, this.runtimeRoot])).trim());
        if (!Array.isArray(value.version) || value.version[0] !== 3 || value.version[1] < 12) throw new Error('version');
        if (!value.fts5) throw new WorkbenchServiceError('当前 Python 的 SQLite 缺少 FTS5，无法建立资料检索索引。');
        if (!value.server_import) throw new WorkbenchServiceError('无法导入 runtime/advisor.server；请检查完整运行包、prompts 与 Python 依赖。');
        return executable;
      } catch (error) {
        if (this.#stopping) throw new WorkbenchServiceError('启动已取消。');
        if (error instanceof WorkbenchServiceError && /无法导入 runtime/.test(error.message)) throw error;
        if (explicit && error instanceof WorkbenchServiceError && /FTS5/.test(error.message)) throw error;
        if (explicit) throw new WorkbenchServiceError('所选 Python 不可用；需要 Python 3.12+ 和 sqlite3/tomllib 标准库，不能使用 Store 别名。');
      }
    }
    throw new WorkbenchServiceError('未找到可用的 Python 3.12+；请设置 pythonPath 或 SIM_ADVISOR_PYTHON。');
  }

  async #serverFile() {
    let record;
    try { record = JSON.parse(await readFile(path.join(this.dataDir, 'server.json'), 'utf8')); } catch { return null; }
    const equal = (left, right) => {
      const a = path.resolve(left), b = path.resolve(right);
      return process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b;
    };
    if (typeof record?.data_dir === 'string' && path.isAbsolute(record.data_dir)) {
      return equal(record.data_dir, this.dataDir) ? record : null;
    }
    if (record?.pid === this.#child?.pid) return record;
    // Copied PID/URL metadata is old-host history, not a live service in this
    // selected workspace. Only explicit workbench-owned anchors authorize this
    // distinction; ordinary same-directory foreign-process protection remains.
    for (const [name, schema, field] of [
      ['workspace-location.json', 'sim-advisor-workspace-location/v1', 'current_root'],
      ['plugin-offline-setup-receipt.json', 'sim-advisor-plugin-offline-setup/v1', 'dataDir'],
    ]) {
      try {
        const anchor = JSON.parse(await readFile(path.join(this.dataDir, name), 'utf8'));
        if (anchor.schema === schema && typeof anchor[field] === 'string' && path.isAbsolute(anchor[field])
            && !equal(anchor[field], this.dataDir)) this.#copiedLegacyServer = true;
      } catch { /* Missing legacy anchor is not evidence of a moved workspace. */ }
    }
    return this.#copiedLegacyServer ? null : record;
  }

  start() {
    if (this.#stopping) return this.#stopping.then(() => this.start());
    if (this.#starting) return this.#starting;
    if (this.#child && this.#snapshot.running) return Promise.resolve(this.status());
    const generation = ++this.#generation;
    this.#starting = this.#start(generation).finally(() => { this.#starting = null; });
    return this.#starting;
  }

  async #start(generation) {
    this.#snapshot = { ...this.#snapshot, state: 'starting', detail: '正在准备数据目录与离线依赖。' };
    let child;
    try {
      await this.#prepare();
      if (generation !== this.#generation) throw new WorkbenchServiceError('启动已取消。');
      await this.#assertDataIdle();
      if (generation !== this.#generation) throw new WorkbenchServiceError('启动已取消。');
      this.#snapshot = { ...this.#snapshot, detail: '正在使用已配置的 Python 启动服务。' };
      this.#python = await this.#choosePython();
      if (generation !== this.#generation) throw new WorkbenchServiceError('启动已取消。');
      this.#snapshot = { ...this.#snapshot, detail: '正在启动独立工作台服务。' };
      child = this.#spawn(this.#python, [path.join(this.runtimeRoot, 'run.py'), '--agent-backend', 'none', '--port', '0']);
      this.#child = child;
      this.#snapshot = { ...this.#snapshot, detail: '正在等待自有工作台进程通过健康检查。' };
      let errorKind = null;
      child.stdout.on('data', () => {});
      child.stderr.on('data', chunk => {
        const text = chunk.toString('utf8');
        if (/PermissionError/.test(text)) errorKind = '数据目录或本机资源访问被拒绝。';
        else if (/ModuleNotFoundError|ImportError/.test(text)) errorKind = '运行包缺少模块或当前 Python 缺少依赖。';
        else if (/SyntaxError/.test(text)) errorKind = '运行包与当前 Python 不兼容。';
      });
      child.once('error', () => { errorKind = '无法启动所选 Python。'; });
      child.once('close', () => {
        if (this.#child === child) {
          this.#child = null;
          if (this.#snapshot.running) this.#snapshot = { ...this.#snapshot, running: false, owned: false, url: null, pid: null, state: 'error', detail: '工作台服务已退出；持久数据保留。' };
        }
      });
      const deadline = Date.now() + this.startupTimeoutMs;
      while (Date.now() < deadline) {
        if (generation !== this.#generation) throw new WorkbenchServiceError('启动已取消。');
        if (child.exitCode !== null || child.signalCode !== null || errorKind === '无法启动所选 Python。') throw new WorkbenchServiceError(errorKind || '工作台在就绪前退出，请检查完整运行包和 Python 依赖。');
        const metadata = await this.#serverFile();
        if (Number.isSafeInteger(child.pid) && child.pid > 0 && metadata?.pid === child.pid) {
          const origin = localOrigin(metadata.url);
          if (origin) {
            try {
              const health = await getJSON(origin, '/api/health', Math.max(1, Math.min(1000, deadline - Date.now())), 8192);
              const sameDirectory = typeof health?.data_dir === 'string' && path.isAbsolute(health.data_dir)
                && (process.platform === 'win32' ? path.resolve(health.data_dir).toLowerCase() === this.dataDir.toLowerCase()
                  : path.resolve(health.data_dir) === this.dataDir);
              if (health?.ready !== true || health.pid !== child.pid || !sameDirectory
                  || health.service !== 'sim-advisor-workbench' || health.protocol_version !== 1) throw new Error('readiness');
              if (generation !== this.#generation) throw new WorkbenchServiceError('启动已取消。');
              this.#snapshot = { ...this.#snapshot, running: true, owned: true, url: origin, pid: child.pid, data_dir: this.dataDir,
                python: this.#python, state: 'running', detail: '独立工作台已启动；默认不启动托管 Agent。' };
              return this.status();
            } catch (error) { if (error instanceof WorkbenchServiceError) throw error; }
          }
        }
        await sleep(50);
      }
      throw new WorkbenchServiceError('工作台启动超时，未通过自有进程的健康检查；请检查完整插件版本、Python 和 dataDir。');
    } catch (error) {
      if (child) await this.#kill(child);
      this.#snapshot = { ...this.#snapshot, running: false, owned: false, url: null, pid: null, state: 'error', detail: error instanceof WorkbenchServiceError ? error.message : '无法准备工作台服务，请检查路径和本机权限。' };
      throw new WorkbenchServiceError(this.#snapshot.detail);
    }
  }

  stop() {
    if (this.#stopping) return this.#stopping;
    ++this.#generation;
    ++this.#stopEpoch;
    this.#validatedOffline = null;
    const pendingStart = this.#starting;
    const pendingSetup = this.#offlineSetup;
    this.#snapshot = { ...this.#snapshot, running: false, state: 'stopping', detail: '正在停止自有工作台进程。' };
    this.#stopping = (async () => {
      try {
        for (const child of [...this.#children]) {
          if (!await this.#kill(child)) throw new WorkbenchServiceError('未能确认自有子进程已退出；未认定停止成功，请稍后重试。');
        }
        this.#child = null;
        if (pendingStart) await pendingStart.catch(() => {});
        if (pendingSetup) await pendingSetup.catch(() => {});
        this.#snapshot = { ...this.#snapshot, running: false, owned: false, url: null, pid: null, state: 'stopped', detail: '工作台已停止；数据保留。' };
        return { ...this.status(), stopped: true };
      } catch (error) {
        const running = Boolean(this.#child && this.#children.has(this.#child));
        this.#snapshot = { ...this.#snapshot, running, owned: running, state: 'error', detail: '未能确认自有子进程已退出；请稍后重试停止。' };
        throw error;
      }
    })().finally(() => { this.#stopping = null; });
    return this.#stopping;
  }

  async runCli(script, argv = [], { timeoutMs = 120000 } = {}) {
    if (!CLI_NAMES.has(script) || !Array.isArray(argv) || argv.length > 256 || argv.some(value => typeof value !== 'string' || value.length > 32768)
        || argv.some(value => value === '--data-dir' || value.startsWith('--data-dir='))) throw new WorkbenchServiceError('CLI 名称或参数不合法；数据目录由插件配置固定。');
    if (!Number.isFinite(timeoutMs) || timeoutMs < 100 || timeoutMs > 900000) throw new WorkbenchServiceError('CLI timeoutMs 需为100至900000毫秒。');
    const epoch = this.#stopEpoch;
    const current = () => {
      if (this.#stopping || epoch !== this.#stopEpoch) throw new WorkbenchServiceError('本次命令已取消，请在工作台停止后重新操作。');
    };
    current();
    await this.start();
    current();
    const target = path.join(this.runtimeRoot, script);
    try { await access(target); } catch { throw new WorkbenchServiceError('运行包未包含所请求的 CLI。'); }
    current();
    const text = await this.#run(this.#python, [target, ...argv], { timeoutMs, maxOutput: 4 * 1024 * 1024 });
    current();
    try { const value = JSON.parse(text.trim()); if (value === null || typeof value !== 'object') throw new Error('shape'); return value; }
    catch { throw new WorkbenchServiceError('CLI 未返回有效的 JSON 对象或数组；未转发原始输出。'); }
  }
}
