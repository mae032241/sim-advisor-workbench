import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { WorkbenchHost, apply, startPlugin, inject, libraryArgv, normalizeConfig } from '../index.js';

const SID_A = 'a'.repeat(32), SID_B = 'b'.repeat(32);
const NAMES = ['get_task_context', 'search_knowledge', 'read_source', 'save_plan', 'update_steps', 'get_library_scope', 'set_libraries_enabled', 'get_tool_contract'];

function nativeHost() {
  const agents = [], listeners = [];
  function context(scope) {
    const tools = new Map(), prompts = new Map(), effects = [];
    return {
      toolsMap: tools, prompts, effects,
      tools: { register(definition) {
        assert.equal(typeof definition.execute, 'function');
        assert.equal(typeof definition.output.render, 'function');
        assert.equal(definition.parameters.type, 'object');
        if (tools.has(definition.name)) throw new Error('native tool conflict');
        tools.set(definition.name, definition);
        return () => tools.delete(definition.name);
      } },
      systemPrompt: { section(value) { prompts.set(value.name, value); return () => prompts.delete(value.name); } },
      on(name, callback) { const value = { name, callback, scope }; listeners.push(value); return () => listeners.splice(listeners.indexOf(value), 1); },
      effect(factory) { const cleanup = factory(); let done = false; const dispose = async () => { if (!done) { done = true; await cleanup?.(); } }; effects.push(dispose); return dispose; },
    };
  }
  const ctx = context(null);
  ctx.agents = { list: () => [...agents] };
  ctx.logger = { warn() {} };
  const addAgent = id => {
    const agent = { id, session: { id }, status: 'idle', ctx: context(id),
      model: { current: () => ({ model: 'existing-model', reasoningEffort: 'high' }) },
      followup() {}, steer() {}, cancel() {} };
    agents.push(agent);
    for (const listener of [...listeners]) if (listener.name === 'agent/created') listener.callback({ agent });
    return agent;
  };
  return { ctx, addAgent, listeners };
}

async function fixture(t, options = {}) {
  const native = nativeHost();
  const first = native.addAgent('current-conversation');
  const calls = [], records = new Map(); let sequence = 0;
  const service = {
    running: false, starts: 0, stops: 0, configures: 0, cli: [], dataDir: options.config?.dataDir || 'D:/fixture/default',
    status() { return { running: this.running, owned: this.running, url: 'http://127.0.0.1:8765', pid: this.running ? 12345 : null, data_dir: this.dataDir }; },
    async start() { this.starts++; this.running = true; return this.status(); },
    async configureOffline(bundleRoot, options) { this.configures++; (this.configurationCalls ??= []).push({bundleRoot, options}); return this.status(); },
    async stop() { this.stops++; this.running = false; return { ...this.status(), stopped: true }; },
    async runCli(script, argv) { this.cli.push({ script, argv }); return options.cliResult ?? [{ id: 'library-result' }]; },
  };
  async function request(origin, path, body) {
    calls.push({ origin, path, body });
    if (path === '/api/sessions') return { id: sequence++ === 0 ? SID_A : SID_B };
    if (path === '/api/tools?bound=true') return { bound: true, tools: NAMES.map(name => ({ name, description: name,
      inputSchema: { type: 'object', properties: {}, required: [], additionalProperties: false } })) };
    const match = /^\/api\/sessions\/([ab]{32})\/external\/(register|poll|event|tool|unregister)$/.exec(path);
    assert.ok(match, 'Only workbench routes are requested');
    const [, sid, action] = match;
    if (action === 'register') {
      if (options.failBind) throw new Error('registration failed');
      records.set(sid, { token: `PRIVATE-BINDING-${sid}`, metadata: body });
      return { binding_id: records.get(sid).token, session_id: sid, instructions: 'Use real sources; never solve automatically.' };
    }
    assert.equal(body.binding_id, records.get(sid).token);
    if (action === 'poll') return { commands: [], cursor: 0, connected: true };
    if (action === 'event') return { accepted: true };
    if (action === 'tool') return { result: { sid, name: body.name } };
    if (action === 'unregister') { records.get(sid).unregistered = true; return { unregistered: true }; }
  }
  const host = await startPlugin(native.ctx, options.config ?? {}, { service, request, createService: options.createService });
  t.after(() => host.dispose());
  const call = (agent, name, args = {}) => agent.ctx.toolsMap.get(name).execute(args, { agent, signal: new AbortController().signal });
  return { ...native, first, calls, records, service, host, call };
}

test('source bundle declares all packed resources and localized original display metadata', async () => {
  const root = new URL('../', import.meta.url);
  const manifest = JSON.parse(await readFile(new URL('package.json', root), 'utf8'));
  assert.equal(manifest.name, '@local/sim-advisor-workbench'); assert.match(manifest.version, /^\d+\.\d+\.\d+$/);
  assert.equal(manifest.dsh.bundle.patch, './cordis.patch.yml');
  assert.equal(manifest.dependencies, undefined);
  for (const resource of ['service.js', 'index.js', 'bridge/', 'runtime/', 'CONTENTS.json', 'README.md', 'locale/*.json', 'icon.svg']) assert.ok(manifest.files.includes(resource));
  assert.equal(manifest.exports['./icon'], './icon.svg');
  assert.deepEqual(inject, ['agents', 'tools', 'systemPrompt', 'typert']);
  assert.equal(typeof apply, 'function'); assert.equal(typeof startPlugin, 'function');
  for (const locale of ['zh', 'en']) {
    const value = JSON.parse(await readFile(new URL(`locale/${locale}.json`, root), 'utf8'));
    assert.ok(value.meta.title && value.meta.description);
    if (locale === 'zh') assert.equal(value.meta.title, '仿真方案工作台');
  }
  assert.match(await readFile(new URL('icon.svg', root), 'utf8'), /<title>Simulation planning workbench<\/title>/);
  assert.match(await readFile(new URL('cordis.patch.yml', root), 'utf8'), /@local\/sim-advisor-workbench/);
  assert.doesNotMatch(await readFile(new URL('index.js', root), 'utf8'), /from ['"]@/); // File URI needs no shipped/npm resolver.
});

test('controls attach to existing/new Agent contexts, stay lazy and reject another conversation', async t => {
  const f = await fixture(t);
  const second = f.addAgent('second-conversation');
  const expected = ['start_workbench', 'workbench_status', 'stop_workbench', 'create_workbench_plan', 'workbench_library', 'bind_workbench', 'unbind_workbench'];
  assert.deepEqual([...f.first.ctx.toolsMap.keys()].sort(), expected.sort());
  assert.equal(f.service.starts, 0);
  const status = await f.call(f.first, 'workbench_status');
  assert.equal(status.running, false); assert.equal(f.service.starts, 0);
  await assert.rejects(f.first.ctx.toolsMap.get('start_workbench').execute({}, { agent: second }), /不属于/);
  const started = await f.call(f.first, 'start_workbench');
  assert.equal(started.url, 'http://127.0.0.1:8765'); assert.equal(f.service.starts, 1);
  assert.equal(f.calls.length, 0); // Start alone neither creates nor binds.
});

test('explicit create binds existing DSH Agent and supplies planning and library tools without exposing token', async t => {
  const f = await fixture(t);
  const result = await f.call(f.first, 'create_workbench_plan', { goal: '判断内部通道压降与混合程度', model_path: 'D:/models/channel.step' });
  assert.equal(result.created, true); assert.equal(result.bound, true);
  assert.equal(result.conversation_id, f.first.id); assert.equal(result.session_id, SID_A);
  assert.equal(result.url, `http://127.0.0.1:8765/?session=${SID_A}`);
  assert.ok(!JSON.stringify(result).includes('PRIVATE-BINDING'));
  assert.deepEqual(f.calls.find(c => c.path === '/api/sessions').body, { goal: '判断内部通道压降与混合程度', model_path: 'D:/models/channel.step' });
  for (const name of NAMES) {
    const definition = f.first.ctx.toolsMap.get(name);
    assert.ok(definition); assert.equal(definition.parameters.properties.session_id, undefined);
  }
  const arguments_ = {tool: 'save_plan', path: '/design_brief/simplification/candidates/0/status', offset: 0, length: 40};
  await f.call(f.first, 'get_tool_contract', arguments_);
  const lookup = f.calls.findLast(call => call.body?.name === 'get_tool_contract');
  assert.equal(lookup.path, `/api/sessions/${SID_A}/external/tool`);
  assert.deepEqual(lookup.body.arguments, arguments_);
  assert.equal(lookup.body.arguments.session_id, undefined);
  assert.equal(f.records.get(SID_A).metadata.model, 'existing-model');
  const count = f.calls.filter(c => c.path === '/api/sessions').length;
  await assert.rejects(f.call(f.first, 'create_workbench_plan', { goal: '另一个新的方案不能隐式替换' }), /已有绑定/);
  assert.equal(f.calls.filter(c => c.path === '/api/sessions').length, count);
});

test('manual bind is retained and prevents hidden create/rebind; unbind permits explicit new plan', async t => {
  const f = await fixture(t);
  await f.call(f.first, 'bind_workbench', { url: `http://127.0.0.1:8765/?session=${SID_B}` });
  await assert.rejects(f.call(f.first, 'create_workbench_plan', { goal: '不要覆盖已存在的当前方案绑定' }), /已有绑定/);
  assert.ok(!f.calls.some(c => c.path === '/api/sessions'));
  await f.call(f.first, 'unbind_workbench');
  assert.equal(f.records.get(SID_B).unregistered, true);
  assert.equal((await f.call(f.first, 'create_workbench_plan', { goal: '现在明确创建另一份任务方案' })).bound, true);
});

test('failed binding preserves a reviewable created plan URL and never claims successful binding', async t => {
  const f = await fixture(t, { failBind: true });
  const result = await f.call(f.first, 'create_workbench_plan', { goal: '创建成功但绑定应明确失败的任务' });
  assert.equal(result.created, true); assert.equal(result.bound, false); assert.equal(result.session_id, SID_A);
  assert.match(result.url, /session=/); assert.equal(f.first.ctx.toolsMap.has('save_plan'), false);
});

test('stop detaches only the owned service bindings and leaves user-supplied services and DSH alive', async t => {
  const f = await fixture(t);
  const second = f.addAgent('second-conversation');
  await f.call(f.first, 'create_workbench_plan', { goal: '绑定插件自己启动的工作台实例' });
  await f.call(second, 'bind_workbench', { url: `http://127.0.0.1:9999/?session=${SID_B}` });
  assert.equal((await f.call(f.first, 'stop_workbench')).stopped, true);
  assert.equal(f.records.get(SID_A).unregistered, true);
  assert.equal(f.records.get(SID_B).unregistered, undefined);
  assert.ok(second.ctx.toolsMap.has('save_plan'));
  assert.ok(f.first.ctx.toolsMap.has('start_workbench'));
  assert.equal(f.first.ctx.toolsMap.has('save_plan'), false);
});

test('an owned registration finishing after stop is unregistered without leaving tools, prompts or a successful binding', async t => {
  const f = await fixture(t);
  await f.service.start();
  let entered, release;
  const waiting = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  t.after(release);
  const request = f.host.bridge.request;
  f.host.bridge.request = async (...args) => {
    if (args[1].endsWith('/register')) { entered(); await gate; }
    return request(...args);
  };
  const pending = f.call(f.first, 'bind_workbench', { url: `http://127.0.0.1:8765/?session=${SID_A}` });
  const rejected = assert.rejects(pending, /已停止.*绑定已移除/);
  await waiting;
  assert.equal(f.host.bindings.size, 0);
  await f.host.stop();
  assert.equal(f.service.running, false);
  release(); await rejected;
  assert.equal(f.records.get(SID_A).unregistered, true);
  assert.equal(f.host.bindings.size, 0);
  assert.equal(f.first.ctx.prompts.size, 0);
  for (const name of NAMES) assert.equal(f.first.ctx.toolsMap.has(name), false);
  assert.ok(!f.listeners.some(value => value.scope === f.first.id && ['session/event', 'agent/status', 'agent/error'].includes(value.name)));
  assert.ok(!f.calls.some(value => value.path.endsWith('/poll')));
  await f.service.start();
  assert.equal((await f.call(f.first, 'bind_workbench', { url: `http://127.0.0.1:8765/?session=${SID_B}` })).bound, true);
  assert.equal(f.host.bindings.size, 1, 'A fresh explicit connection after restart still works');
});

test('a pending foreign-origin registration remains valid when the owned service stops', async t => {
  const f = await fixture(t);
  await f.service.start();
  let entered, release;
  const waiting = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  t.after(release);
  const request = f.host.bridge.request;
  f.host.bridge.request = async (...args) => {
    if (args[1].endsWith('/register')) { entered(); await gate; }
    return request(...args);
  };
  const pending = f.call(f.first, 'bind_workbench', { url: `http://127.0.0.1:9999/?session=${SID_B}` });
  await waiting;
  await f.host.stop();
  release();
  assert.equal((await pending).bound, true);
  assert.equal(f.service.running, false);
  assert.equal(f.records.get(SID_B).unregistered, undefined);
  assert.equal(f.host.bindings.size, 1);
  assert.ok(f.first.ctx.toolsMap.has('save_plan'));
  assert.equal(f.first.ctx.prompts.size, 1);
});

test('library tool only invokes the fixed CLI with mapped typed arguments and preserves JSON arrays', async t => {
  const f = await fixture(t);
  const result = await f.call(f.first, 'workbench_library', { action: 'add', args: { name: '官方案例', type: 'official-models', path: 'D:/comsol/applications', version: '6.3' } });
  assert.deepEqual(result, { action: 'add', result: [{ id: 'library-result' }] });
  assert.deepEqual(f.service.cli[0], { script: 'library_cli.py', argv: ['add', '官方案例', '--type', 'official-models', '--path', 'D:/comsol/applications', '--version', '6.3'] });
  await assert.rejects(f.call(f.first, 'workbench_library', { action: 'list', args: { data_dir: 'D:/other' } }), /白名单/);
  await assert.rejects(f.call(f.first, 'workbench_library', { action: 'shell', args: {} }), /白名单/);
  assert.equal(f.service.cli.length, 1);
});

test('library argv covers constructors, indexing, filters, rebind, configuration and evidence', () => {
  assert.deepEqual(libraryArgv('list'), ['list']);
  assert.deepEqual(libraryArgv('scan', { library_id: 'lib-a', paths: ['a.mph'] }), ['scan', 'lib-a', '--path', 'a.mph']);
  assert.deepEqual(libraryArgv('build', { library_id: 'lib-a', enrich: false, modes: ['native', 'qmd_keyword', 'qmd_vector'] }), ['build', 'lib-a', '--modes', 'qmd_keyword,qmd_vector']);
  assert.ok(!libraryArgv('build', { library_id: 'lib-a' }).includes('--enrich'));
  assert.ok(libraryArgv('build', { library_id: 'lib-a', shallow: true, enrich: true }).includes('--enrich'));
  assert.deepEqual(libraryArgv('index'), ['index', '--modes', 'qmd_keyword']);
  assert.deepEqual(libraryArgv('search', { query: '压降', library_ids: ['lib-a'], mode: 'qmd_vector' }), ['search', '压降', '--mode', 'qmd_vector', '--library', 'lib-a', '--limit', '8']);
  assert.deepEqual(libraryArgv('search', { query: '边界层', kind: 'code', feature_branch: 'mesh' }), ['search', '边界层', '--mode', 'qmd_keyword', '--limit', '8', '--kind', 'code', '--feature-branch', 'mesh']);
  assert.deepEqual(libraryArgv('status', { library_id: 'lib-a' }), ['status', 'lib-a']);
  assert.deepEqual(libraryArgv('cancel', { job_id: 'a'.repeat(32) }), ['cancel', 'a'.repeat(32)]);
  for (const job_id of ['../foreign', '--data-dir', 'short', 123]) assert.throws(() => libraryArgv('cancel', { job_id }));
  assert.deepEqual(libraryArgv('rebind', { library_id: 'lib-a', path: 'E:/moved' }), ['rebind', 'lib-a', '--root-id', 'main', '--path', 'E:/moved']);
  assert.deepEqual(libraryArgv('read', { id: 'lo-a' }), ['read', 'lo-a']);
  assert.deepEqual(libraryArgv('configuration', { id: 'lo-a', category: 'physics', node_path: 'comp1/spf', source: 'saved' }), ['configuration', 'lo-a', '--category', 'physics', '--node-path', 'comp1/spf', '--source', 'saved', '--limit', '20']);
  assert.deepEqual(libraryArgv('evidence', { id: 'lo-a', index: 1, line: 8 }), ['evidence', 'lo-a', '--index', '1', '--line', '8', '--limit', '120']);
  for (const [action, args] of [['add', { name: '--data-dir=X', type: 'user-models', path: 'D:/models' }], ['build', { library_id: 'lib-a', enrich: 'true' }], ['index', { modes: ['fake-vector'] }], ['read', { id: '-x' }], ['list', { argv: ['--data-dir', 'X'] }]]) assert.throws(() => libraryArgv(action, args));
});

test('library lifecycle and model discovery use typed actions and no implicit file limit', async t => {
  const f = await fixture(t);
  for (const action of ['enable', 'disable', 'delete', 'restore', 'models']) {
    assert.deepEqual(libraryArgv(action, {library_id: 'lib-a'}), [action, 'lib-a']);
    await f.call(f.first, 'workbench_library', {action, args: {library_id: 'lib-a'}});
    assert.throws(() => libraryArgv(action, {library_id: 'lib-a', path: 'C:/foreign'}), /白名单/);
  }
  assert.deepEqual(libraryArgv('models', {library_id: 'lib-a', limit: 12}), ['models', 'lib-a', '--limit', '12']);
  assert.deepEqual(libraryArgv('list', {include_deleted: true}), ['list', '--include-deleted']);
  assert.deepEqual(libraryArgv('build', {library_id: 'lib-a', limit: 13}), ['build', 'lib-a', '--limit', '13', '--modes', 'qmd_keyword']);
  assert.throws(() => libraryArgv('list', {include_deleted: 'true'}), /布尔/);
  assert.ok(!libraryArgv('build', {library_id: 'lib-a'}).includes('--limit'));
  assert.equal(f.calls.length, 0, 'Lifecycle CLI actions do not create or bind a plan');
});

test('library scope and explicit batch visibility are available without a bound plan or confirmation', async t => {
  const f = await fixture(t);
  await f.call(f.first, 'workbench_library', {action: 'scope', args: {query: 'acoustics', type: 'user-manuals'}});
  await f.call(f.first, 'workbench_library', {action: 'enable_many', args: {library_ids: ['manual-a', 'model-b'], enabled: true}});
  await f.call(f.first, 'workbench_library', {action: 'enable_many', args: {library_ids: ['manual-a'], enabled: false}});
  assert.deepEqual(f.service.cli, [
    {script: 'library_cli.py', argv: ['scope', '--query', 'acoustics', '--type', 'user-manuals']},
    {script: 'library_cli.py', argv: ['enable-many', '--library', 'manual-a', '--library', 'model-b', '--enabled', 'true']},
    {script: 'library_cli.py', argv: ['enable-many', '--library', 'manual-a', '--enabled', 'false']},
  ]);
  assert.equal(f.calls.length, 0, 'Library controls neither create nor bind a plan');
  assert.deepEqual(libraryArgv('scope'), ['scope']);
  assert.deepEqual(libraryArgv('add', {name: 'My notes', type: 'user-manuals', path: 'D:/notes'}), ['add', 'My notes', '--type', 'user-manuals', '--path', 'D:/notes']);
  assert.ok(f.first.ctx.toolsMap.get('workbench_library').parameters.properties.args.properties.type.enum.includes('user-manuals'));
  for (const args of [{}, {library_ids: [], enabled: true}, {library_ids: ['a'], enabled: 'true'}, {library_ids: ['-x'], enabled: true}, {library_ids: ['a'], enabled: false, confirmed: true}, {library_ids: Array(1001).fill('a'), enabled: true}]) {
    assert.throws(() => libraryArgv('enable_many', args));
  }
  assert.throws(() => libraryArgv('scope', {type: 'wrong'}), /type/);
});

test('branch search remains a read-only CLI operation with optional typed filters', async t => {
  const f = await fixture(t);
  await f.call(f.first, 'workbench_library', {action: 'search', args: {query: '材料温度函数', kind: 'code', feature_branch: 'func', mode: 'qmd_keyword'}});
  assert.deepEqual(f.service.cli, [{script: 'library_cli.py', argv: ['search', '材料温度函数', '--mode', 'qmd_keyword', '--limit', '8', '--kind', 'code', '--feature-branch', 'func']}]);
  assert.equal(f.calls.length, 0, 'Search does not create or bind any workbench plan');
  const schema = f.first.ctx.toolsMap.get('workbench_library').parameters.properties.args;
  assert.ok(!schema.required.includes('feature_branch'));
  assert.ok(schema.properties.feature_branch.enum.includes('multiphysics'));
  for (const branch of ['../mesh', '--data-dir', 'Mesh', '', null, ['mesh']]) assert.throws(() => libraryArgv('search', {query: 'test', feature_branch: branch}), /feature_branch/);
  for (const kind of ['model_case', '--enrich', '', null]) assert.throws(() => libraryArgv('search', {query: 'test', kind}), /kind/);
  assert.throws(() => libraryArgv('build', {library_id: 'lib-a', feature_branch: 'mesh'}), /白名单/);
});

test('ordinary Host config validates supported paths and never relies on imported schema dependencies', () => {
  assert.deepEqual(normalizeConfig({ dataDir: '', pythonPath: '', qmdDir: 'E:/qmd', nodePath: 'E:/node.exe', pageindexPython: 'E:/pi/python.exe', comsolHome: 'E:/COMSOL' }),
    { startupTimeoutMs: 30000, qmdDir: 'E:/qmd', nodePath: 'E:/node.exe', pageindexPython: 'E:/pi/python.exe', comsolHome: 'E:/COMSOL' });
  assert.equal(normalizeConfig({ offlineBundle: 'D:/offline with spaces' }).offlineBundle, 'D:/offline with spaces');
  assert.equal(normalizeConfig({ dataDir: ' D:/custom data ' }).dataDir, 'D:/custom data');
  for (const value of [{ shell: 'arbitrary command' }, { qmdDir: false }, { startupTimeoutMs: 0 }, { offlineBundle: './relative' }, { dataDir: './relative' }, null]) assert.throws(() => normalizeConfig(value));
});

test('selecting an offline bundle configures it on plugin enable without starting a workbench', async t => {
  const f = await fixture(t, { config: { offlineBundle: 'D:/offline bundle' } });
  assert.equal(f.service.configures, 1);
  assert.equal(f.service.starts, 0);
  assert.ok(f.first.ctx.toolsMap.has('start_workbench'));
});

test('failed automatic dependency setup stays editable and never advertises a ready configuration', async () => {
  const native = nativeHost(), agent = native.addAgent('setup-failure');
  let stopped = false;
  const service = { status() { return { owned: false }; }, async configureOffline() { throw new Error('invalid selected bundle'); }, async stop() { stopped = true; } };
  const host = await startPlugin(native.ctx, { offlineBundle: 'D:/broken bundle' }, { service });
  assert.equal(stopped, false);
  assert.equal(host.configurationStatus().state, 'error');
  assert.match(host.configurationStatus().detail, /invalid selected bundle/);
  assert.ok(agent.ctx.toolsMap.has('start_workbench'));
  await host.dispose();assert.equal(stopped, true);
});

test('a failed directory selection can be corrected in place with a new ready revision', async t => {
  const f = await fixture(t);
  f.service.configureOffline = async root => {
    if (root === 'D:/missing bundle') throw new Error('Selected bundle missing');
    return { dependency_setup: { state: 'ready', detail: 'Verified selected bundle' } };
  };
  const failed = await f.host.configureOffline('D:/missing bundle');
  assert.equal(failed.state, 'error');assert.equal(failed.revision, 1);
  const ready = await f.host.configureOffline('D:/valid bundle');
  assert.equal(ready.state, 'ready');assert.equal(ready.bundle_root, 'D:/valid bundle');assert.equal(ready.revision, 2);
  assert.equal(f.service.starts, 0);
  assert.ok(f.first.ctx.toolsMap.has('start_workbench'));
});

test('the settings launch action requires ready dependencies and reuses the owned service without a plan', async t => {
  const f = await fixture(t);
  await assert.rejects(f.host.launchWorkbench(), /先完成离线依赖配置/);
  assert.equal(f.service.starts, 0);
  await f.host.configureOffline('D:/offline bundle');
  f.service.start = async function () { if (!this.running) { this.starts++; this.running = true; } return this.status(); };
  const [first, repeated] = await Promise.all([f.host.launchWorkbench(), f.host.launchWorkbench()]);
  assert.equal(first.running, true);assert.equal(first.url, 'http://127.0.0.1:8765');
  assert.equal(repeated.pid, first.pid);assert.equal(f.service.starts, 1);
  assert.equal(f.calls.length, 0);assert.equal(f.host.bindings.size, 0);
});

test('a failed GUI launch can retry without repeating dependency configuration', async t => {
  const f = await fixture(t);
  await f.host.configureOffline('D:/offline bundle');
  f.service.start = async () => { throw new Error('Python not ready'); };
  await assert.rejects(f.host.launchWorkbench(), /Python not ready/);
  assert.equal(f.host.configurationStatus().state, 'ready');
  f.service.start = async function () { this.running = true; return this.status(); };
  assert.equal((await f.host.launchWorkbench()).running, true);
  assert.equal(f.service.configures, 1);
});

test('a configuration change during GUI launch does not return an outdated ready URL', async t => {
  const f = await fixture(t);
  await f.host.configureOffline('D:/first bundle');
  let release;
  f.service.start = () => new Promise(resolve => { release = () => { f.service.running = true; resolve(f.service.status()); }; });
  const launching = f.host.launchWorkbench();
  await f.host.configureOffline('D:/second bundle');
  release();await assert.rejects(launching, /依赖配置已变化/);
  assert.equal(f.service.running, false);
});

test('switching data roots stops the old service, detaches its plan, and configures a fresh owner', async t => {
  let f;const replacements=[];
  f=await fixture(t,{createService: options=>{
    const next={...f.service,dataDir:options.dataDir??'D:/fixture/default',running:false,starts:0,stops:0,configures:0,cli:[]};
    replacements.push(next);return next;
  }});
  await f.call(f.first,'create_workbench_plan',{goal:'检查切换数据目录保留旧方案并解除连接'});
  const result=await f.host.configureOffline('D:/offline bundle',' E:/new workspace ');
  assert.equal(result.state,'ready');assert.equal(result.data_dir_setting,'E:/new workspace');
  assert.equal(result.data_dir,'E:/new workspace');assert.equal(f.service.running,false);assert.equal(f.service.stops,1);
  assert.equal(f.records.get(SID_A).unregistered,true);assert.equal(f.host.bindings.size,0);
  assert.equal(replacements[0].configures,1);assert.equal(replacements[0].starts,0);
  const restored=await f.host.configureOffline('D:/offline bundle','');
  assert.equal(restored.data_dir_setting,'');assert.equal(restored.data_dir,'D:/fixture/default');
  assert.equal(replacements[0].stops,1);assert.equal(replacements.length,2);
});

test('a rejected data path is editable and does not stop the current owned service', async t => {
  const f=await fixture(t);await f.service.start();
  const result=await f.host.configureOffline('D:/offline bundle','relative-data');
  assert.equal(result.state,'error');assert.match(result.detail,/绝对路径/);
  assert.equal(f.service.running,true);assert.equal(f.service.stops,0);
  assert.equal(result.data_dir,'D:/fixture/default');
});

test('a superseded data-root selection never configures its obsolete replacement after waiting for stop', async t=>{
 const f=await fixture(t);const replacements=[];let entered,release;
 const waiting=new Promise(resolve=>entered=resolve),gate=new Promise(resolve=>release=resolve);
 f.service.running=true;
 f.service.stop=async function(){this.stops++;entered();await gate;this.running=false;return{stopped:true};};
 f.host.createService=options=>{
  const next={dataDir:options.dataDir,configures:0,stops:0,status(){return{running:false,owned:false,data_dir:this.dataDir};},
   async configureOffline(){this.configures++;return{dependency_setup:{detail:'Verified current fixture'}};},async stop(){this.stops++;return{stopped:true};}};
  replacements.push(next);return next;
 };
 const obsolete=f.host.configureOffline('D:/bundle-a','E:/obsolete');await waiting;
 const latest=f.host.configureOffline('D:/bundle-b','F:/latest');release();await obsolete;await latest;
 assert.equal(replacements[0].configures,0);assert.equal(replacements[0].stops,1);
 assert.equal(replacements[1].configures,1);
 assert.equal(f.host.configurationStatus().data_dir,'F:/latest');
});

test('a superseded same-root bundle selection is not verified after its awaited service stop',async t=>{
 const f=await fixture(t);let entered,release;const verified=[];
 const waiting=new Promise(resolve=>entered=resolve),gate=new Promise(resolve=>release=resolve);
 f.service.running=true;
 f.service.stop=async function(){entered();await gate;this.running=false;return{stopped:true};};
 f.service.configureOffline=async root=>{verified.push(root);return{dependency_setup:{detail:'Verified current fixture'}};};
 const obsolete=f.host.configureOffline('D:/obsolete-bundle');await waiting;
 const latest=f.host.configureOffline('D:/latest-bundle');release();await obsolete;await latest;
 assert.deepEqual(verified,['D:/latest-bundle']);
});

test('source Host uses the canonical turn-completion bridge without replaying stream or intermediate replies',async t=>{
 const f=await fixture(t);
 await f.call(f.first,'create_workbench_plan',{goal:'Only the final completed native turn may appear in the workbench'});
 const emit=event=>{for(const listener of [...f.listeners])if(listener.scope===f.first.id&&listener.name==='session/event')listener.callback(f.first.session,event);};
 assert.ok(!f.listeners.some(listener=>listener.name==='agent/assistant-stream'));
 emit({type:'turn/start',seq:1,data:{turn:1}});
 emit({type:'assistant/message',seq:2,data:{turn:1,step:1,message:{id:'intermediate',content:[{type:'text',text:'Never publish this intermediate'}]}}});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(f.calls.filter(call=>call.path.endsWith('/event')&&call.body.event.type==='message').length,0);
 emit({type:'assistant/message',seq:3,data:{turn:1,step:2,message:{id:'final',content:[{type:'text',text:'Only completed final'}]}}});
 emit({type:'turn/end',seq:4,data:{turn:1,reason:{kind:'completed'}}});
 for(let i=0;i<10&&!f.calls.some(call=>call.body?.event?.type==='message');i++)await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(f.calls.filter(call=>call.body?.event?.type==='message').map(call=>call.body.event.text),['Only completed final']);
});

test('a launch from the previous data root cannot stop its replacement service', async t => {
  let f;let next;
  f=await fixture(t,{createService: options=>{
    next={...f.service,dataDir:options.dataDir,running:false,starts:0,stops:0,configures:0};return next;
  }});
  await f.host.configureOffline('D:/offline bundle');
  let release;
  f.service.start=()=>new Promise(resolve=>{release=()=>{f.service.running=true;resolve(f.service.status());};});
  const oldLaunch=f.host.launchWorkbench();
  await f.host.configureOffline('D:/offline bundle','E:/replacement');
  release();await assert.rejects(oldLaunch,/依赖配置已变化/);
  assert.equal(next.stops,0);assert.equal(f.service.running,false);
  assert.equal(f.host.service,next);assert.equal(f.host.configurationStatus().state,'ready');
});

test('native setup RPC keeps a visible Typert binding and volatile changes can be retried', async t => {
  const native = nativeHost(), services = new Map();
  let registry, selected = '';
  native.ctx.provide = (key, value) => services.set(key, value);
  native.ctx.typert = { register(value) { registry = value; } };
  await apply(native.ctx, { offlineBundle: { get: () => selected } });
  t.after(async () => { for (const dispose of native.ctx.effects) await dispose(); });
  const setup = services.get('simWorkbenchSetup');
  assert.equal(setup.typertRemote.service, setup);
  assert.equal(setup.typertRemote.serviceKey, 'simWorkbenchSetup');
  assert.equal(setup.typertRemote.namespace, 'simWorkbenchSetup');
  assert.deepEqual(registry.invocations.map(value => value.method), ['status', 'configure', 'start', 'stop', 'verify']);
  assert.equal(setup.status().state, 'unconfigured');
  assert.equal(setup.status().workbench.running, false);
  assert.equal(setup.status().workbench.url, null);
  assert.equal((await setup.stop()).state, 'stopped');
  selected = 'D:/definitely missing offline bundle';
  const changed = native.listeners.find(listener => listener.name === 'loader/volatile-update');
  changed.callback([['offlineBundle']]);
  const checking = setup.configure();
  assert.equal(checking.state, 'checking');assert.equal(checking.revision, 1);
  const waitForError = async () => {
    const deadline = Date.now() + 3000;
    while (setup.status().state === 'checking' && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(setup.status().state, 'error');
  };
  await waitForError();
  assert.equal(setup.configure().revision, 2);await waitForError();
  assert.equal(setup.verify().revision, 3);await waitForError();
  assert.equal(setup.status().bundle_root, selected);
});

test('plugin reload cannot initialize dependencies and only an explicit request forces verification', async t => {
  const f = await fixture(t, {config: {offlineBundle: 'D:/fixture/offline'}});
  assert.deepEqual(f.service.configurationCalls[0].options, {forceVerify: false, allowInitialize: false});
  assert.equal(f.service.starts, 0);
  await f.host.configureOffline('D:/fixture/offline');
  assert.deepEqual(f.service.configurationCalls.at(-1).options, {forceVerify: false, allowInitialize: true});
  await f.host.configureOffline('D:/fixture/offline', f.host.dataDirSetting, {forceVerify: true});
  assert.deepEqual(f.service.configurationCalls.at(-1).options, {forceVerify: true, allowInitialize: true});
});

test('configuration polling carries only compact owned-service state and never starts a process', async t => {
  const f = await fixture(t);
  const original = f.service.status.bind(f.service);
  f.service.status = () => ({...original(), state: 'running', detail: 'Service ready', arbitrary_payload: 'x'.repeat(100000)});
  f.service.running = true;
  const status = f.host.configurationStatus();
  assert.deepEqual(status.workbench, {running: true, owned: true, state: 'running', url: 'http://127.0.0.1:8765', pid: 12345, detail: 'Service ready'});
  assert.ok(JSON.stringify(status).length < 2000);
  assert.equal(f.service.starts, 0);
  await f.host.stop();
  assert.equal(f.host.configurationStatus().workbench.running, false);
  assert.equal(f.host.configurationStatus().workbench.url, null);
});

test('purge requires explicit irreversible confirmation and emits a fixed flag', () => {
  assert.throws(() => libraryArgv('purge', {library_id:'cases'}), /confirmed/);
  assert.throws(() => libraryArgv('purge', {library_id:'cases',confirmed:'true'}), /confirmed/);
  assert.deepEqual(libraryArgv('purge', {library_id:'cases',confirmed:true}), ['purge','cases','--confirm']);
});

test('disposing native agent/plugin effects removes tools/listeners and stops owned service', async t => {
  const f = await fixture(t);
  await f.call(f.first, 'create_workbench_plan', { goal: '检查原生生命周期清理不会留下绑定' });
  for (const dispose of f.first.ctx.effects) await dispose();
  assert.equal(f.first.ctx.toolsMap.size, 0); assert.equal(f.records.get(SID_A).unregistered, true);
  await f.host.dispose();
  assert.equal(f.listeners.length, 0); assert.equal(f.service.running, false);
});
