import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const core = source.slice(0, source.indexOf('$("intake-form").addEventListener("submit", createSession);'));
const flush = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); };
class Node {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.listeners = new Map(); this._text = ''; this.open = false; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  querySelector() { return null; }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
}
function fixture(respond) {
  let next = 0, time = 0; const timers = new Map(), calls = [], nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  const context = vm.createContext({URL, URLSearchParams, AbortController, console,
    document: {getElementById: get, createElement: tag => new Node(tag)},
    setTimeout(callback, delay) { const id = ++next; timers.set(id, {callback, at: time + delay}); return id; }, clearTimeout: id => timers.delete(id),
    fetch: async (path, settings) => { calls.push({path, settings}); return await respond(path, settings); }});
  vm.runInContext(core, context);
  return {calls, timers, get, run: code => vm.runInContext(code, context),
    async tick(amount) { time += amount; for (const [id, timer] of [...timers]) if (timer.at <= time) { timers.delete(id); timer.callback(); await flush(); } }};
}
const ok = value => ({ok: true, status: 200, json: async () => value});

test('a stalled status JSON read has a deadline; coalesced refresh recovers without extra pending requests', async () => {
  let reads = 0;
  const f = fixture(() => ++reads === 1 ? {...ok(null), json: () => new Promise(() => {})} : ok({ready: true}));
  f.run('renderExamples = renderSessions = renderSystem = syncRefine = () => {};');
  const first = f.run('refreshStatus(true)'), duplicate = f.run('refreshStatus(true)'); await flush();
  assert.equal(f.calls.length, 1); await f.tick(15000); await first; await duplicate;
  assert.equal(f.calls[0].settings.signal.aborted, true); assert.equal(f.run('statusRead'), null);
  await f.run('refreshStatus(true)'); assert.equal(f.calls.length, 2); assert.equal(f.get('server-status').textContent, '本地服务已连接');
  assert.equal(f.timers.size, 0);
});

test('a stalled write releases operation state and reports an unknown result without automatic retry', async () => {
  const f = fixture(() => new Promise(() => {}));
  f.run('setBusy = value => {state.operation = value;}; notice = message => {globalThis.lastNotice = message;};');
  const pending = f.run('runOperation("answers", "", "", () => post("/api/sessions/A/answers", {revision: 1, answers: {}}))');
  await flush(); assert.equal(f.run('state.operation'), 'answers');
  await f.tick(30000); await pending;
  assert.equal(f.run('state.operation'), null); assert.match(f.run('lastNotice'), /结果待核对.*不会自动重发/);
  assert.equal(f.calls.length, 1); assert.equal(f.calls[0].settings.signal.aborted, true); assert.equal(f.timers.size, 0);
});

test('caller cancellation settles even if fetch ignores AbortSignal, and clears its deadline', async () => {
  const f = fixture(() => new Promise(() => {}));
  const pending = f.run('globalThis.abortRead = new AbortController(); api("/api/sources/old", {signal: abortRead.signal})');
  await flush(); f.run('abortRead.abort()');
  await assert.rejects(pending, error => error.name === 'AbortError');
  assert.equal(f.calls[0].settings.signal.aborted, true); assert.equal(f.timers.size, 0);
});

test('full model snapshot JSON is absent from closed details and constructed once on demand', async () => {
  const f = fixture(() => ok({}));
  f.run('state.model = {file_name: "sample.mph", facts: [], diagnostics: "large-snapshot-".repeat(50000)}; renderModel();');
  const all = node => [node, ...node.children.flatMap(all)];
  const raw = all(f.get('model-content')).find(node => node.className === 'raw-snapshot-details');
  assert.ok(raw); assert.equal(raw.children.some(node => node.tag === 'pre'), false);
  assert.doesNotMatch(f.get('model-content').textContent, /large-snapshot-/);
  raw.open = true; raw.listeners.get('toggle')(); const pre = raw.children.find(node => node.tag === 'pre');
  assert.match(pre.textContent, /large-snapshot-/); raw.listeners.get('toggle')(); assert.equal(raw.children.filter(node => node.tag === 'pre').length, 1);
});
