import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const core = source.slice(0, source.indexOf('$("intake-form").addEventListener("submit", createSession);'));

class Node {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.dataset = {}; this.open = false; this.hidden = false; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_value) { throw Error('Status text must never use HTML rendering'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  showModal() { this.open = true; }
}
function fixture() {
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  const context = vm.createContext({document: {getElementById: get, createElement: tag => new Node(tag)}, console, URL});
  vm.runInContext(core, context);
  vm.runInContext('syncConfirmation = () => {}; syncRefine = () => {};', context);
  const run = (code, input) => { context.input = input; return vm.runInContext(code, context); };
  const cards = (status, session = null, agent = null) => JSON.parse(run('JSON.stringify(systemStatusCards(input.status, input.session, input.agent))', {status, session, agent}));
  return {get, run, cards};
}
const registered = {
  ready: true, building: false,
  modes: {native: {ready: true, state: 'ready'}, qmd_keyword: {ready: true, state: 'ready'}, qmd_vector: {ready: false, state: 'interrupted', error: '构建已中断'}},
};
const current = {id: 'plan-a'};
const external = {session_id: 'plan-a', connection_mode: 'external', connected: true, alive: true, status: 'idle', model: 'deepseek-flash', reasoning_effort: 'high', external: {registered: true, alive: true, name: 'DSH'}};
const card = (cards, id) => cards.find(value => value.id === id);

test('modern registered indexes override legacy unverified readiness, including old API fallback', () => {
  const f = fixture();
  for (const status of [
    {services: {qmd: {available: true, installed: true, component_available: true, library_indexes: registered, legacy: {available: false}}}},
    {services: {qmd: {available: false, installed: true, mode: 'installed-unverified', detail: 'run old ingest to verify'}}, corpus: {library_indexes: registered}},
  ]) {
    const qmd = card(f.cards(status), 'qmd');
    assert.equal(qmd.label, '关键词可用');
    assert.equal(qmd.tone, 'good');
    assert.deepEqual(qmd.modes.map(mode => [mode.id, mode.label]), [['qmd_keyword', '索引就绪'], ['qmd_vector', '已中断']]);
    assert.equal(qmd.modes[1].error, '构建已中断');
    assert.doesNotMatch(qmd.detail, /run old ingest/);
  }
});

test('QMD installed does not imply indexes exist; absent mode data remains unknown', () => {
  const f = fixture();
  const qmd = card(f.cards({services: {qmd: {installed: true, available: false}}}), 'qmd');
  assert.equal(qmd.label, '已安装 · 索引未就绪');
  assert.ok(qmd.modes.every(mode => mode.label === '状态未报告'));
  const requested = card(f.cards({services: {qmd: {installed: true, library_indexes: {modes: {native: {ready: true}, qmd_vector: {ready: false, state: 'not_requested'}}}}}}), 'qmd');
  assert.equal(requested.modes[1].label, '未构建（按需）');
  assert.equal(requested.label, '已安装 · 索引未就绪');
});

test('missing QMD component is reported without exposing legacy native mode', () => {
  const f = fixture();
  const qmd = card(f.cards({services: {qmd: {component_available: false, library_indexes: registered}}}), 'qmd');
  assert.equal(qmd.label, '组件未配置');
  assert.equal(qmd.modes[0].label, '索引就绪');
  assert.notEqual(qmd.tone, 'good');
});

test('closed libraries retain physical index readiness without claiming an enabled search scope', () => {
  const f = fixture();
  const source = {services: {qmd: {installed: true, library_indexes: registered}},
    corpus: {library_visibility: {enabled_library_count: 0, disabled_library_count: 2, deleted_library_count: 1}}};
  const qmd = card(f.cards(source), 'qmd');
  assert.equal(qmd.label, '索引保留 · 无启用资料库');
  assert.equal(qmd.tone, 'neutral');
  assert.equal(qmd.modes[0].label, '索引就绪');
  assert.match(qmd.detail, /启用 0 · 关闭 2 · 已删除 1/);
  assert.equal(registered.modes.qmd_keyword.ready, true);
});

test('PageIndex explicitly separates optional absent, installed awaiting model, verified, and awaiting verification', () => {
  const f = fixture();
  const states = [
    [{installed: false, available: false}, '可选 · 未安装', 'neutral'],
    [{installed: true, available: false, model_configured: false, status_label: '待配置 / 核验'}, '已安装 · 待模型配置', 'neutral'],
    [{installed: true, available: false, model_configured: true, mode: 'awaiting-model-configuration'}, '已安装 · 待验证', 'neutral'],
    [{installed: true, available: true, model_configured: true}, '可用', 'good'],
  ];
  for (const [pageindex, label, tone] of states) {
    const page = card(f.cards({services: {pageindex}}), 'pageindex');
    assert.equal(page.label, label); assert.equal(page.tone, tone);
  }
});

test('managed backend being disabled does not mark a genuinely bound external DSH unavailable', () => {
  const f = fixture();
  const cards = f.cards({services: {planner: {available: false, mode: 'disabled', status_label: '托管后端已关闭'}}}, current, external);
  assert.equal(card(cards, 'planner').title, '工作台托管 Agent');
  assert.equal(card(cards, 'planner').label, '托管后端已关闭');
  assert.equal(card(cards, 'session_agent').label, '外部已连接');
  assert.equal(card(cards, 'session_agent').mode, '外部会话：DSH');
  assert.equal(card(cards, 'session_agent').tone, 'good');
});

test('a disconnected managed plan does not suggest connecting a backend disabled by this service', () => {
  const f = fixture();
  const status = {services: {planner: {available: false, mode: 'disabled'}}};
  const agent = {session_id: current.id, connection_mode: 'managed', managed_backend: 'codex', connected: false, alive: false, status: 'disconnected', external: {registered: false, alive: false, offline_reason: 'not_registered'}};
  const entry = card(f.cards(status, current, agent), 'session_agent');
  assert.equal(entry.label, '未连接');
  assert.equal(entry.tone, 'neutral');
  assert.equal(entry.detail, '当前服务的托管后端已关闭；可将当前方案切换为外部模式后绑定 DSH 等 Agent。');
  assert.doesNotMatch(entry.detail, /可在当前方案的 Agent 对话中连接/);
  assert.equal(card(f.cards(status, current, external), 'session_agent').label, '外部已连接');
  const enabled = card(f.cards({services: {planner: {available: true, mode: 'codex-app-server'}}}, current, agent), 'session_agent');
  assert.equal(enabled.detail, '可在当前方案的 Agent 对话中连接。');
});

test('no selected plan, pending snapshots and old-plan responses never imply an external binding', () => {
  const f = fixture();
  assert.equal(card(f.cards({}, null, external), 'session_agent').label, '未选择方案');
  for (const agent of [null, {status: 'disconnected', connected: false}, {...external, session_id: 'old-plan'}]) {
    const entry = card(f.cards({}, current, agent), 'session_agent');
    assert.equal(entry.label, '连接状态未读取');
    assert.notEqual(entry.tone, 'good');
  }
  const waiting = {...external, connected: false, external: {registered: false, alive: false}};
  assert.equal(card(f.cards({}, current, waiting), 'session_agent').label, '外部 Agent 未绑定');
});

test('external heartbeat loss and a managed connecting state stay explicit', () => {
  const f = fixture();
  const offline = {...external, connected: false, alive: false, external: {...external.external, alive: false, offline_reason: '心跳已过期'}};
  assert.equal(card(f.cards({}, current, offline), 'session_agent').label, '外部连接已失联');
  assert.equal(card(f.cards({}, current, offline), 'session_agent').detail, '心跳已过期');
  const managed = {session_id: current.id, connection_mode: 'managed', managed_backend: 'dsh', connected: false, status: 'starting'};
  const entry = card(f.cards({}, current, managed), 'session_agent');
  assert.equal(entry.label, '托管正在连接');
  assert.match(entry.mode, /DSH/);
});

test('ordinary unregistered state is neutral, and managed mode ignores unrelated external reasons', () => {
  const f = fixture();
  for (const mode of ['external', 'managed']) {
    const agent = {session_id: current.id, connection_mode: mode, managed_backend: 'codex', connected: false, alive: false, status: 'disconnected', external: {registered: false, alive: false, offline_reason: 'not_registered'}};
    const entry = card(f.cards({}, current, agent), 'session_agent');
    assert.equal(entry.label, mode === 'external' ? '外部 Agent 未绑定' : '未连接');
    assert.equal(entry.tone, 'neutral');
    assert.doesNotMatch(entry.detail, /not_registered/);
  }
  for (const [reason, expected] of [['heartbeat_timeout', /心跳已超时/], ['process_exited', /进程已退出/]]) {
    const entry = card(f.cards({}, current, {...external, connected: false, external: {...external.external, alive: false, offline_reason: reason}}), 'session_agent');
    assert.equal(entry.tone, 'warn'); assert.match(entry.detail, expected);
  }
});

test('known interrupted build errors get a short Chinese explanation and retain original technical detail', () => {
  const f = fixture();
  const error = 'Build owner exited before this mode completed; completed modes remain usable';
  const indexes = {...registered, modes: {...registered.modes, qmd_vector: {ready: false, state: 'interrupted', error}}};
  const qmd = card(f.cards({services: {qmd: {installed: true, library_indexes: indexes}}}), 'qmd');
  assert.match(qmd.modes[1].error, /构建进程已退出/);
  assert.equal(qmd.modes[1].technical, error);
  f.run('state.status = input; renderSystem()', {services: {qmd: {installed: true, library_indexes: indexes}}});
  assert.match(f.get('service-list').textContent, /技术错误详情/);
  assert.match(f.get('service-list').textContent, /Build owner exited/);
});

test('rendered per-mode errors and external names are text, never markup', () => {
  const f = fixture();
  f.run('state.status = input.status; state.session = input.session; state.agent = input.agent; renderSystem();', {
    status: {services: {sim_cli: {available: true}, qmd: {installed: true, library_indexes: {...registered, modes: {...registered.modes, qmd_vector: {ready: false, state: 'error', error: '<script>fail()</script>'}}}}}},
    session: current, agent: {...external, external: {...external.external, name: '<img src=x onerror=fail()>'}},
  });
  const list = f.get('service-list');
  assert.equal(list.children.length, 5);
  assert.match(list.textContent, /sim-cli/);
  assert.match(list.textContent, /<script>fail\(\)<\/script>/);
  assert.match(list.textContent, /<img src=x onerror=fail\(\)>/);
  assert.equal(list.children.find(value => value.dataset.service === 'qmd').children.at(-1).children.length, 2);
});

test('opening the system dialog paints current state immediately and requests fresh server status', () => {
  const f = fixture();
  f.run('state.session = input.session; state.agent = input.agent; globalThis.refreshCalls = 0; refreshStatus = quiet => { if (quiet) refreshCalls++; }; openSystem();', {session: current, agent: external});
  assert.equal(f.get('system-dialog').open, true);
  assert.match(f.get('service-list').textContent, /外部已连接/);
  assert.equal(f.run('refreshCalls'), 1);
});

test('AgentPanel state callbacks update an open modal without waiting for the 15-second service poll', () => {
  const f = fixture();
  f.run('state.session = input; openDialog("system-dialog");', current);
  f.run('updateAgentState(input)', external);
  assert.match(f.get('service-list').textContent, /外部已连接/);
  f.run('updateAgentState(input)', {...external, connected: false, external: {...external.external, alive: false, offline_reason: '心跳失联'}});
  assert.match(f.get('service-list').textContent, /外部连接已失联/);
  assert.doesNotMatch(f.get('service-list').textContent, /外部已连接/);
  f.run('state.session = null; updateAgentState(input)', external);
  assert.match(f.get('service-list').textContent, /未选择方案/);
});

test('keyword maintenance uses the registered QMD engine under a closed disclosure', () => {
  assert.match(html, /<details class="corpus-block system-legacy"><summary>资料库关键词索引维护<\/summary>/);
  for (const id of ['corpus-status', 'build-index', 'index-progress', 'service-list']) assert.match(html, new RegExp(`id="${id}"`));
  assert.match(source, /onState: updateAgentState/);
});
