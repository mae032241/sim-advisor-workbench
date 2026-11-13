import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const core = source.slice(0, source.indexOf('$("intake-form").addEventListener("submit", createSession);'));
const bindings = source.slice(source.indexOf('$("open-versions").addEventListener'), source.indexOf('$("toggle-deleted-sessions").addEventListener'));
const flush = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); };
class Node {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = new Map(); this.dataset = {}; this.attributes = new Map(); this.className = ''; this._text = ''; this.open = false; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  get childElementCount() { return this.children.length; }
  get classList() { const node = this; return {contains(value) { return node.className.split(/\s+/).includes(value); }, add(value) { node.className += ` ${value}`; }, remove(value) { node.className = node.className.split(/\s+/).filter(item => item !== value).join(' '); }, toggle(value, enabled) { enabled ? this.add(value) : this.remove(value); }}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  async emit(name) { return await this.listeners.get(name)?.({target: this, preventDefault() {}}); }
  async click() { if (!this.disabled) return await this.emit('click'); }
  showModal() { this.open = true; }
  close() { this.open = false; void this.emit('close'); }
  focus() {}
  scrollIntoView() {}
  querySelector(selector) { return all(this).slice(1).find(node => selector.startsWith('.') ? node.classList.contains(selector.slice(1)) : node.tagName === selector.toUpperCase()) || null; }
}
const all = node => [node, ...node.children.flatMap(all)];
const plan = revision => ({id: 'A', revision, title: `Plan ${revision}`, goal: `Goal ${revision}`, status: 'draft', planning_incomplete: true, model: {},
  sources: [{id: 'old-ref', title: 'Historical source', availability: {readable: false}}], questions: [{id: 'q', label: 'Operating temperature', required: true}], answers: {q: `saved ${revision}`},
  plan: {title: `Version ${revision} mesh plan`, engine: 'fixture', assumptions: ['Check support'], steps: [{id: 'mesh', title: '网格构建', purpose: 'Check grid credibility', action: '设置四面体网格，局部加密；检查网格质量。', inputs: ['根据间隙确定网格尺寸'], difficulty: 'Thin gaps need inspection', selection: '未定位的流体区域', checks: ['加密网格比较压降收敛'], source_ids: ['old-ref'], depends_on: []}]}});
function fixture() {
  const nodes = new Map(), calls = [], agentBindings = [], current = plan(3);
  const get = id => { if (!nodes.has(id)) { const node = new Node(); node.id = id; nodes.set(id, node); } return nodes.get(id); };
  const api = {list: {session_id: 'A', current_revision: 3, versions: [{revision: 3, available: true, current: true, summary: 'Current'}, {revision: 2, available: true, current: false, summary: 'Mesh draft'}, {revision: 1, available: false, current: false, summary: 'Old log'}]}, version: async revision => plan(revision), restoreError: null};
  const context = vm.createContext({URL, URLSearchParams, AbortController, setTimeout, clearTimeout, console,
    window: {location: {href: 'http://localhost/?session=A'}, history: {state: {}, replaceState() {}}, confirm() { throw Error('Native confirmation must never be used'); }},
    document: {getElementById: get, createElement: tag => new Node(tag), body: new Node(), querySelectorAll: () => []},
    fetch: async (path, settings = {}) => {
      calls.push({path, settings}); let body, code = 200;
      if (path === '/api/sessions/A/versions') body = api.list;
      else if (/^\/api\/sessions\/A\/versions\/\d+$/.test(path)) body = await api.version(Number(path.split('/').at(-1)), settings.signal);
      else if (/^\/api\/sessions\/A\/versions\/\d+\/restore$/.test(path)) {
        if (api.restoreError) { code = 409; body = {error: api.restoreError}; }
        else body = {...plan(4), version_restore: {from_revision: Number(path.split('/').at(-2)), review_required: true}};
      } else throw Error(`Unexpected request ${path}`);
      return {ok: code === 200, status: code, json: async () => structuredClone(body)};
    }});
  vm.runInContext(core, context);
  context.current = current; context.agentBindings = agentBindings;
  vm.runInContext('state.session = current; state.draftAnswers = {q:"unsaved draft"}; state.agent = {session_id:"A", connected:false, alive:false, status:"disconnected", external:{registered:false}}; renderModel = renderSession = renderSessions = syncConfirmation = syncRefine = () => {}; refreshStatus = async () => {}; agentPanel = {bindSession() {agentBindings.push(state.session?.id);}};', context);
  vm.runInContext(bindings, context);
  return {get, calls, api, current, agentBindings, run: code => vm.runInContext(code, context),
    async preview(revision = 2) { await get('open-versions').click(); await context.previewVersion(revision); }};
}

test('the visible header entry loads only version metadata; legacy log entries cannot request missing snapshots', async () => {
  assert.match(html, /id="open-versions"[^>]*>方案版本<\/button>/);
  const f = fixture(); assert.equal(f.calls.length, 0); await f.get('open-versions').click();
  assert.deepEqual(f.calls.map(call => call.path), ['/api/sessions/A/versions']);
  const unavailable = f.get('versions-list').children.find(node => node.textContent.includes('r1'));
  assert.match(unavailable.textContent, /仅操作记录，未保存快照/); assert.equal(unavailable.children[0].disabled, true); await unavailable.children[0].click();
  assert.equal(f.calls.length, 1); assert.match(f.get('history-list').textContent, /r2/);
});

test('historical preview shows flow, mesh and saved answers without adopting it, altering drafts or binding an Agent', async () => {
  const f = fixture(); await f.preview();
  assert.equal(f.run('state.session'), f.current); assert.equal(f.run('state.session.revision'), 3); assert.equal(f.run('state.draftAnswers.q'), 'unsaved draft');
  assert.equal(f.agentBindings.length, 0); assert.ok(f.calls.every(call => !call.settings.method));
  assert.match(f.get('version-preview').textContent, /网格构建.*Thin gaps need inspection/s);
  assert.match(f.get('version-preview').textContent, /网格类型与分区.*尺度依据.*质量检查.*收敛.*未提及：局部加密/s);
  assert.doesNotMatch(f.get('version-preview').textContent, /尚需补充/);
  assert.match(f.get('version-preview').textContent, /saved 2/); assert.match(f.get('version-preview').textContent, /只读/);
  assert.equal(all(f.get('version-preview')).some(node => node.tagName === 'INPUT' || node.tagName === 'TEXTAREA'), false);
  assert.equal(f.run('versionsState.versions.some(item => Boolean(item.plan))'), false);
});

test('restoring is disabled for dirty input, connected or unknown Agent state and the current version', async () => {
  const f = fixture(); await f.preview(); assert.equal(f.get('restore-version').disabled, false);
  f.run('state.dirty = true; syncVersionControls()'); assert.equal(f.get('restore-version').disabled, true); assert.match(f.get('version-restore-hint').textContent, /尚未保存/);
  f.run('state.dirty = false; state.agent.connected = true; syncVersionControls()'); assert.match(f.get('version-restore-hint').textContent, /断开/);
  f.run('state.agent.connected = false; state.agent.external.registered = true; syncVersionControls()'); assert.equal(f.get('restore-version').disabled, true);
  f.run('state.agent = null; syncVersionControls()'); assert.match(f.get('version-restore-hint').textContent, /核对/);
  await f.run('previewVersion(3)'); assert.match(f.get('version-restore-hint').textContent, /当前版本无需恢复/);
  assert.ok(f.calls.every(call => !call.settings.method));
});

test('restore requires in-page confirmation, posts current revision and creates a new unconfirmed revision of the same plan', async () => {
  const f = fixture(); await f.preview(); await f.get('restore-version').click();
  assert.equal(f.get('version-restore-confirmation').hidden, false); assert.ok(f.calls.every(call => !call.settings.method));
  await f.get('cancel-version-restore').click(); assert.equal(f.get('version-restore-confirmation').hidden, true);
  await f.get('restore-version').click(); await f.get('confirm-version-restore').click();
  const post = f.calls.find(call => call.settings.method === 'POST'); assert.equal(post.path, '/api/sessions/A/versions/2/restore');
  assert.deepEqual(JSON.parse(post.settings.body), {revision: 3, confirmed: true});
  assert.equal(f.run('state.session.id'), 'A'); assert.equal(f.run('state.session.revision'), 4); assert.equal(f.run('state.session.status'), 'draft');
  assert.equal(f.run('versionsState'), null); assert.equal(f.get('versions-dialog').open, false); assert.ok(f.agentBindings.length >= 1); assert.ok(f.agentBindings.every(id => id === 'A'));
});

test('a restore conflict preserves the current plan and shows the backend reason without retrying', async () => {
  const f = fixture(); f.api.restoreError = '请先断开Agent'; await f.preview(); await f.get('restore-version').click(); await f.get('confirm-version-restore').click();
  assert.equal(f.run('state.session'), f.current); assert.match(f.get('version-error').textContent, /请先断开Agent/);
  assert.equal(f.calls.filter(call => call.settings.method === 'POST').length, 1); assert.equal(f.get('version-restore-confirmation').hidden, true);
});

test('closing preview cancels late reads and releases its only snapshot; switching plans cannot revive old content', async () => {
  const f = fixture(); await f.get('open-versions').click(); let release, signal;
  f.api.version = (_revision, incoming) => { signal = incoming; return new Promise(resolve => { release = resolve; }); };
  const reading = f.run('previewVersion(2)'); await flush(); f.get('versions-dialog').close(); await reading;
  assert.equal(signal.aborted, true); assert.equal(f.run('versionsState.snapshot'), null);
  f.run('adoptSession({id:"B",revision:1,goal:"Another",model:{},questions:[],answers:{}})'); release(plan(2)); await flush();
  assert.equal(f.run('state.session.id'), 'B'); assert.equal(f.run('versionsState'), null);
  assert.doesNotMatch(f.get('version-preview').textContent, /Version 2 mesh plan/); assert.equal(f.get('versions-dialog').open, false);
});

test('switching plans clears a fully rendered previous snapshot before another version dialog can open', async () => {
  const f = fixture(); await f.preview(); assert.match(f.get('version-preview').textContent, /Version 2 mesh plan/);
  f.run('adoptSession({id:"B",revision:1,goal:"Another",model:{},questions:[],answers:{}})');
  assert.equal(f.run('versionsState'), null); assert.equal(f.get('versions-dialog').open, false);
  assert.doesNotMatch(f.get('version-preview').textContent, /Version 2 mesh plan|saved 2/);
});

test('historical dependency navigation stays inside the preview even when the current flow has identical step ids', async () => {
  const f = fixture(); f.run('renderSimulationFlow($("plan-content"), state.session.plan.steps, state.session)');
  const currentNode = f.get('plan-content').querySelector('.flow-node-details');
  f.api.version = async revision => { const snapshot = plan(revision); snapshot.plan.steps.push({id: 'check', title: '验收', depends_on: ['mesh']}); return snapshot; };
  await f.preview(); const historicalNode = f.get('version-preview').querySelector('.flow-node-details');
  await f.get('version-preview').querySelector('.flow-dependency').click();
  assert.equal(historicalNode.open, true); assert.equal(currentNode.open, false); assert.equal(f.run('state.session.revision'), 3);
  assert.ok(f.calls.every(call => !call.settings.method));
});
