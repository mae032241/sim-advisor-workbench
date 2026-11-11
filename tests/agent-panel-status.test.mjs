import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const script = readFileSync(new URL('../web/agent-panel.js', import.meta.url), 'utf8');
class Node {
  static writes = [];
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.className = ''; this._text = ''; this._value = ''; this.listeners = new Map(); this.open = false;
    this.dataset = new Proxy({}, {set: (target, key, value) => { Node.writes.push({node: this, property: `data-${key}`, value, same: target[key] === value}); target[key] = value; return true; }});
    return new Proxy(this, {set(target, key, value, receiver) {
      if (['textContent', 'value', 'hidden', 'disabled', 'className', 'title', 'placeholder', 'dateTime', 'open'].includes(key)) Node.writes.push({node: receiver, property: key, value, same: target[key] === value});
      return Reflect.set(target, key, value, receiver);
    }});
  }
  set textContent(value) { this._text = String(value); this.textNode = this._text ? {data: this._text} : null; this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  get value() { return this._value || (this.tagName === 'SELECT' ? this.options[0]?.value || '' : ''); }
  set value(value) { this._value = String(value); }
  get options() { return this.children.filter(child => child.tagName === 'OPTION'); }
  get classList() { const node = this; return {contains(value) { return node.className.split(/\s+/).includes(value); }, add(value) { node.className += ` ${value}`; }, toggle(value, enabled) { const values = new Set(node.className.split(/\s+/).filter(Boolean)); enabled ? values.add(value) : values.delete(value); node.className = [...values].join(' '); }}; }
  append(...children) { Node.writes.push({node: this, property: 'append'}); this.children.push(...children); }
  replaceChildren(...children) { Node.writes.push({node: this, property: 'replaceChildren'}); this._text = ''; this.children = children; }
  setAttribute(name, value) { this[name] = String(value); }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  async emit(name) { return await this.listeners.get(name)?.({target: this, preventDefault() {}}); }
}
const flush = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); };
const find = (root, match) => match(root) ? root : root.children.map(child => find(child, match)).find(Boolean);
const requestURL = path => new URL(path, 'http://localhost:49268');
const endpoint = path => requestURL(path).pathname;

function fixture(response) {
  let serial = 0, now = 0; const timers = new Map(), calls = [], document = {activeElement: null, createElement: tag => new Node(tag)}, host = new Node('section'); host.id = 'agent-panel';
  const current = {session: {id: 'A', revision: 1}};
  const window = {location: {href: 'http://localhost:49268/?session=A'}, addEventListener() {}};
  vm.runInNewContext(script, {window, document, URL, URLSearchParams, AbortController,
    setTimeout(callback, delay) { const id = ++serial; timers.set(id, {callback, delay, at: now + delay}); return id; }, clearTimeout(id) { timers.delete(id); },
    fetch: async (path, settings) => { calls.push({path, settings}); return {ok: true, status: 200, json: async () => await response(path, settings)}; }});
  const panel = window.SimAdvisorAgentPanel.mount(host, {getSession: () => current.session, isDirty: () => false, isBlocked: () => false});
  const get = className => { const node = find(host, item => item.className.split(/\s+/).includes(className)); assert.ok(node, className); return node; };
  return {calls, timers, document, panel, host, get, current, async tick(amount = 2000) { const end = now + amount; for (;;) { const due = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0]; if (!due) break; const [id, entry] = due; timers.delete(id); now = entry.at; entry.callback(); await flush(); } now = end; await flush(); }};
}
const unbound = {session_id: 'A', connection_mode: 'external', status: 'disconnected', connected: false, alive: false, plan_revision: 1,
  managed_backend: 'codex', managed_model: 'gpt-6.1-sol', managed_reasoning_effort: 'xhigh', messages: [], external: {registered: false, alive: false, offline_reason: 'unregistered'}};

test('an unregistered external Agent clearly says unbound, points to DSH and makes no registration or model request', async () => {
  const f = fixture(async () => unbound); await flush();
  assert.equal(f.get('agent-panel-status').textContent, '外部 Agent 未绑定');
  assert.match(f.get('agent-connection-note').textContent, /DSH 当前会话.*重新绑定/);
  assert.match(f.get('agent-latest').textContent, /重新绑定后才能/);
  assert.doesNotMatch(f.host.textContent, /正在等待回复|等待外部 Agent/);
  assert.ok(f.calls.every(call => endpoint(call.path).endsWith('/agent') && !call.settings.method));
  f.panel.destroy();
});

test('status polling waits for the response and reuses unchanged chat DOM and focus', async () => {
  let release; const pending = new Promise(resolve => { release = resolve; });
  const f = fixture(() => pending); await flush();
  assert.equal(f.calls.length, 1); assert.equal([...f.timers.values()].some(timer => timer.delay === 2000), false);
  await f.tick(); await f.tick(); assert.equal(f.calls.length, 1, 'An unresolved response does not schedule another poll');
  release(unbound); await flush();
  const history = f.get('agent-message-history'), initial = history.children[0], input = find(f.host, node => node.tagName === 'TEXTAREA');
  input.value = 'still editing'; f.document.activeElement = input;
  await f.tick();
  assert.equal(f.calls.length, 2); assert.equal(history.children[0], initial); assert.equal(f.document.activeElement, input); assert.equal(input.value, 'still editing');
  assert.equal(f.timers.size, 1); f.panel.destroy(); assert.equal(f.timers.size, 0);
});

test('a stalled status/body read times out, aborts and resumes read-only polling', async () => {
  let reads = 0;
  const f = fixture(() => ++reads === 1 ? new Promise(() => {}) : Promise.resolve(unbound)); await flush();
  await f.tick(15000);
  assert.equal(f.calls[0].settings.signal.aborted, true);
  assert.match(f.get('agent-panel-error').textContent, /读取超时/);
  assert.ok([...f.timers.values()].some(timer => timer.delay === 2000));
  await f.tick(2000); assert.equal(f.calls.length, 2);
  assert.ok(f.calls.every(call => !call.settings.method));
  assert.equal(f.get('agent-panel-error').hidden, true); f.panel.destroy();
});

const managed = {...unbound, connection_mode: 'managed', connected: true, alive: true, status: 'running', model: 'gpt-6.1-sol', reasoning_effort: 'xhigh'};
test('a stalled message releases controls after its deadline without replaying the task or stopping the Agent', async () => {
  const f = fixture(path => endpoint(path).endsWith('/message') ? new Promise(() => {}) : Promise.resolve(managed)); await flush();
  const sending = f.panel.sendTask('one explicitly requested task'); await flush();
  assert.equal(f.get('agent-stop').disabled, true);
  await f.tick(30000); assert.equal(await sending, false);
  assert.match(f.get('agent-panel-error').textContent, /结果待核对.*不会自动重发/);
  assert.equal(f.get('agent-stop').disabled, false);
  const posts = () => f.calls.filter(call => call.settings.method === 'POST');
  assert.equal(posts().length, 1); assert.ok(endpoint(posts()[0].path).endsWith('/message'));
  await f.tick(2000);
  assert.equal(posts().length, 1); assert.ok(endpoint(f.calls.at(-1).path).endsWith('/agent'));
  f.panel.destroy();
});

test('switching sessions quietly cancels old deadlines even if its response ignores abort', async () => {
  const f = fixture(path => endpoint(path).endsWith('/message') ? new Promise(() => {}) : Promise.resolve({...managed, session_id: endpoint(path).split('/')[3]})); await flush();
  const sending = f.panel.sendTask('old task'); await flush();
  const old = f.calls.find(call => endpoint(call.path).endsWith('/message'));
  f.current.session = {id: 'B', revision: 1}; f.panel.bindSession(); await flush();
  assert.equal(await sending, false); assert.equal(old.settings.signal.aborted, true);
  assert.equal(f.get('agent-panel-error').hidden, true);
  assert.equal([...f.timers.values()].filter(timer => timer.delay > 2000).length, 0);
  f.panel.destroy(); assert.equal(f.timers.size, 0);
});

const updates = (cursor, items = [], extra = {}) => {
  const {messages: _oldMessages, ...status} = managed;
  return {...status, message_cursor: cursor, message_updates: items, messages_reset: false, ...extra};
};
const message = (sequence, id, text, role = 'assistant') => ({sequence, message: {id, role, text, at: '2026-11-09T12:00:00Z'}});
const rows = history => history.children.filter(node => node.tagName === 'ARTICLE');

test('all Agent requests subscribe after a cursor without loading history, including command replies', async () => {
  let reads = 0;
  const f = fixture((path, settings) => settings.method === 'POST'
    ? updates('epoch:42', [message(42, 'sent', 'new complete output')])
    : updates(++reads === 1 ? 'epoch:41' : 'epoch:42'));
  await flush();
  const first = requestURL(f.calls[0].path);
  assert.equal(first.searchParams.get('view'), 'updates'); assert.equal(first.searchParams.get('after'), 'new');
  assert.equal(rows(f.get('agent-message-history')).length, 0);
  assert.match(f.get('agent-message-history').textContent, /打开后收到的新消息/);
  await f.panel.sendTask('please plan'); await flush();
  const post = f.calls.find(call => call.settings.method === 'POST'), posted = requestURL(post.path);
  assert.equal(posted.searchParams.get('view'), 'updates'); assert.equal(posted.searchParams.get('after'), 'epoch:41');
  assert.deepEqual(JSON.parse(post.settings.body), {text: 'please plan'});
  assert.equal(rows(f.get('agent-message-history')).length, 1);
  assert.match(f.get('agent-latest').textContent, /new complete output/);
  assert.equal(requestURL(f.calls.at(-1).path).searchParams.get('after'), 'epoch:42');
  assert.ok(f.calls.every(call => requestURL(call.path).searchParams.get('view') === 'updates'));
  f.panel.destroy();
});

test('commands wait for the initial subscription cursor and never POST after=new', async () => {
  let release; const pending = new Promise(resolve => { release = resolve; });
  const f = fixture(() => pending); await flush();
  assert.equal(await f.panel.sendTask('not yet subscribed'), false);
  assert.equal(f.calls.length, 1); assert.ok(f.calls.every(call => !call.settings.method));
  assert.match(f.get('agent-panel-error').textContent, /正在订阅/);
  release(updates('epoch:14')); await flush(); f.panel.destroy();
});

test('incremental upserts change only their message and repeated polls preserve rows, focus and draft', async () => {
  const batches = [updates('epoch:1'), updates('epoch:3', [message(2, 'one', 'first'), message(3, 'two', 'second')]),
    updates('epoch:4', [message(4, 'two', 'second corrected')]), updates('epoch:4', [message(4, 'two', 'second corrected')]),
    updates('epoch:5', [message(3, 'two', 'old duplicate')])];
  const f = fixture(() => batches.shift() || updates('epoch:5')); await flush(); await f.tick();
  const history = f.get('agent-message-history'), [one, two] = rows(history), firstText = one.children[1], secondText = two.children[1];
  const input = find(f.host, node => node.tagName === 'TEXTAREA'); input.value = 'unsent draft'; f.document.activeElement = input;
  await f.tick();
  assert.deepEqual(rows(history), [one, two]); assert.equal(one.children[1], firstText); assert.equal(firstText.textContent, 'first');
  assert.equal(two.children[1], secondText); assert.equal(secondText.textContent, 'second corrected');
  await f.tick(); await f.tick();
  assert.deepEqual(rows(history), [one, two]); assert.equal(secondText.textContent, 'second corrected');
  assert.equal(f.document.activeElement, input); assert.equal(input.value, 'unsent draft');
  assert.equal(requestURL(f.calls.at(-1).path).searchParams.get('after'), 'epoch:4');
  assert.ok(f.calls.every(call => !call.settings.method && !call.settings.body)); f.panel.destroy();
});

test('cursor resets show an output gap and duplicate reset responses do not rebuild messages', async () => {
  const reset = updates('new-epoch:1', [message(1, 'new', 'after restart')], {messages_reset: true});
  const batches = [updates('old-epoch:4', [message(4, 'old', 'before restart')]), reset, reset];
  const f = fixture(() => batches.shift()); await flush(); await f.tick();
  const history = f.get('agent-message-history'), [row] = rows(history);
  assert.equal(rows(history).length, 1); assert.match(history.textContent, /缺口/); assert.doesNotMatch(history.textContent, /before restart/);
  assert.match(history.textContent, /after restart/); await f.tick(); assert.deepEqual(rows(history), [row]);
  f.panel.destroy();
});

test('a new session starts at new and ignores late output from the previous subscription', async () => {
  let release, aReads = 0;
  const waiting = new Promise(resolve => { release = resolve; });
  const f = fixture(path => endpoint(path).includes('/A/')
    ? ++aReads === 1 ? updates('A:1', [message(1, 'same-id', 'A output')]) : waiting
    : updates('B:8', [message(8, 'same-id', 'B output')], {session_id: 'B'}));
  await flush(); await f.tick();
  f.current.session = {id: 'B', revision: 1}; f.panel.bindSession(); await flush();
  const bCall = f.calls.find(call => endpoint(call.path).includes('/B/'));
  assert.equal(requestURL(bCall.path).searchParams.get('after'), 'new');
  release(updates('A:2', [message(2, 'same-id', 'late A output')])); await flush();
  assert.equal(rows(f.get('agent-message-history')).length, 1); assert.match(f.get('agent-message-history').textContent, /B output/);
  assert.doesNotMatch(f.get('agent-message-history').textContent, /A output/);
  await f.tick(); assert.equal(requestURL(f.calls.at(-1).path).searchParams.get('after'), 'B:8');
  f.panel.destroy();
});

test('unchanged idle polls and input events make zero redundant text, attribute or subtree writes', async () => {
  for (const state of [updates('stable:1', [], {status: 'idle'}), updates('stable:1', [], {...unbound, messages: undefined})]) {
    const f = fixture(() => state); await flush();
    const input = find(f.host, node => node.tagName === 'TEXTAREA'), latest = f.get('agent-latest'), selectedText = latest.textNode;
    f.document.activeElement = input;
    input.selectionStart = 1; input.selectionEnd = 1;
    Node.writes.length = 0;
    await f.tick(); await f.tick(); await f.tick();
    assert.equal(Node.writes.length, 0, 'Identical polling must not replace text or set same reflected attributes');
    for (const text of ['甲', '甲乙', '甲乙丙']) {
      input.value = text; input.selectionStart = text.length; input.selectionEnd = text.length;
      Node.writes.length = 0; await input.emit('input');
      assert.equal(Node.writes.filter(write => write.same).length, 0, 'Typing must not repeat any DOM setter');
      assert.ok(Node.writes.every(write => write.property === 'disabled' && write.value === false), 'Only enabling Send on the first character is needed');
      assert.equal(f.document.activeElement, input); assert.equal(input.selectionStart, text.length); assert.equal(input.selectionEnd, text.length);
      assert.equal(latest.textNode, selectedText, 'A selection anchor in unchanged text keeps its text-node identity');
    }
    f.panel.destroy();
  }
});

test('real heartbeat and connection changes still update their text and controls', async () => {
  const live = {connection_mode: 'external', status: 'idle', external: {registered: true, alive: true, name: 'DSH', last_seen: '2026-11-09T12:00:00Z'}};
  const snapshots = [updates('heartbeat:1', [], live), updates('heartbeat:1', [], {...live, external: {...live.external, last_seen: '2026-11-09T12:00:02Z'}}),
    updates('heartbeat:1', [], {...live, connected: false, alive: false, external: {...live.external, alive: false, offline_reason: 'heartbeat_timeout'}})];
  const f = fixture(() => snapshots.shift()); await flush();
  const note = f.get('agent-connection-note'), first = note.textNode; Node.writes.length = 0;
  await f.tick();
  assert.notEqual(note.textNode, first); assert.equal(Node.writes.filter(write => write.property === 'textContent').length, 1);
  await f.tick(); assert.match(f.get('agent-panel-status').textContent, /心跳失联/); assert.match(note.textContent, /已超时/);
  f.panel.destroy();
});
