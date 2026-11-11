import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const core = source.slice(0, source.indexOf('$("intake-form").addEventListener("submit", createSession);'));
const bindings = source.slice(source.indexOf('$("new-session").addEventListener'), source.indexOf('$("refresh-status").addEventListener'));
const plan = (id, revision = 1) => ({id, revision, title: `Title ${id}`, goal: `Goal ${id}`, status: 'needs_replan', model: {path: `D:/models/${id}.mph`}, questions: [], answers: {}, plan: {engine: 'intake-only', steps: []}});
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };

class Node {
  static writes = [];
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.listeners = new Map(); this.attributes = new Map(); this._text = ''; this.value = ''; this.hidden = false; this.open = false; this.className = '';
    return new Proxy(this, {set(target, key, value, receiver) { if (['textContent', 'value', 'hidden', 'disabled', 'className', 'title', 'placeholder'].includes(key)) Node.writes.push({node: receiver, property: key}); return Reflect.set(target, key, value, receiver); }});
  }
  set textContent(value) { this._text = String(value); this.textNode = this._text ? {data: this._text} : null; this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  get classList() { const node = this; return {add(value) { const values = new Set(node.className.split(/\s+/).filter(Boolean)); values.add(value); node.className = [...values].join(' '); }, remove(value) { node.className = node.className.split(/\s+/).filter(item => item !== value).join(' '); }, contains(value) { return node.className.split(/\s+/).includes(value); }, toggle(value, force) { const selected = force ?? !this.contains(value); selected ? this.add(value) : this.remove(value); }}; }
  append(...children) { Node.writes.push({node: this, property: 'append'}); this.children.push(...children); }
  replaceChildren(...children) { Node.writes.push({node: this, property: 'replaceChildren'}); this._text = ''; this.children = children; }
  setAttribute(key, value) { Node.writes.push({node: this, property: key}); this.attributes.set(key, String(value)); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  removeAttribute(key) { this.attributes.delete(key); }
  addEventListener(type, callback) { const listeners = this.listeners.get(type) || []; listeners.push(callback); this.listeners.set(type, listeners); }
  async emit(type) { for (const callback of this.listeners.get(type) || []) await callback({target: this, preventDefault() {}}); }
  async click() { return await this.emit('click'); }
  showModal() { this.open = true; }
  close() { this.open = false; void this.emit('close'); }
  focus() {}
  scrollIntoView() {}
  reset() { this.onReset?.(); }
}
function fixture(url = 'http://localhost:49268/?session=A', {realStatus = false} = {}) {
  const nodes = new Map(), calls = [], agentBindings = [];
  for (const match of html.matchAll(/<([\w-]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) { const node = new Node(match[1]); node.id = match[2]; node.hidden = /\bhidden(?:\s|>)/.test(match[0]); nodes.set(node.id, node); }
  const get = id => { assert.ok(nodes.has(id), id); return nodes.get(id); };
  get('intake-form').onReset = () => { get('goal').value = ''; get('model-path').value = ''; };
  const active = new Map([['A', plan('A', 3)], ['B', plan('B', 2)]]), deleted = new Map();
  const api = {active, deleted, statusOnce: null, listOnce: null, deletedOnce: null, readOverride: null, deleteError: null, purgeError: null, purgeOverride: null};
  const status = () => ({ready: true, services: {}, corpus: {}, examples: [], sessions: [...active.values()].map(value => structuredClone(value))});
  const location = {href: url}, history = {state: {}, replaceState(value, _title, next) { this.state = value; location.href = String(next); }};
  const context = vm.createContext({console, URL, URLSearchParams, AbortController, setTimeout, clearTimeout, agentBindings,
    window: {location, history, confirm() { throw Error('Deletion/restoration must not call a native confirmation'); }},
    document: {getElementById: get, createElement: tag => new Node(tag), body: new Node('body'), querySelectorAll: selector => selector === 'dialog' ? [...nodes.values()].filter(node => node.tagName === 'DIALOG') : []},
    fetch: async (path, settings = {}) => {
      calls.push({path, settings}); let body, code = 200;
      if (path === '/api/status?view=summary') { const once = api.statusOnce; api.statusOnce = null; body = once ? await once() : status(); }
      else if (path === '/api/sessions/deleted') { const once = api.deletedOnce; api.deletedOnce = null; body = once ? await once(settings.signal) : {sessions: [...deleted.values()]}; }
      else if (path.startsWith('/api/sessions?')) { const once = api.listOnce; api.listOnce = null; const params = new URL(path, 'http://localhost').searchParams, offset = Number(params.get('offset')), limit = Number(params.get('limit')), summaries = [...active.values()]; body = once ? await once(settings.signal) : {sessions: summaries.slice(offset, offset + limit), total: summaries.length, next_offset: offset + limit < summaries.length ? offset + limit : null}; }
      else {
        const match = /^\/api\/sessions\/([^/]+)(?:\/(delete|restore|purge))?$/.exec(path); assert.ok(match, path);
        const [, id, action] = match;
        if (!action) body = api.readOverride ? await api.readOverride(id, settings.signal) : active.get(id);
        else if (action === 'delete') {
          if (api.deleteError) { code = 409; body = {error: api.deleteError}; }
          else { const payload = JSON.parse(settings.body), value = active.get(id); assert.equal(payload.revision, value.revision); assert.equal(payload.confirmed, true); body = {...value, status: 'deleted', deleted_at: '2026-11-09T01:00:00Z'}; deleted.set(id, body); active.delete(id); }
        } else if (action === 'purge') {
          const payload = JSON.parse(settings.body), value = deleted.get(id); assert.equal(payload.revision, value.revision); assert.equal(payload.confirmed, true);
          if (api.purgeError) { code = 409; body = {error: api.purgeError}; }
          else if (api.purgeOverride) body = await api.purgeOverride(id, settings.signal);
          else { deleted.delete(id); body = {id, status: 'purged'}; }
        } else { assert.deepEqual(JSON.parse(settings.body), {confirmed: true}); const value = deleted.get(id); body = {...value, status: 'draft', revision: value.revision + 1}; delete body.deleted_at; active.set(id, body); deleted.delete(id); }
      }
      if (!body) { code = 404; body = {error: 'not found'}; }
      const copy = structuredClone(body); return {ok: code < 400, status: code, json: async () => copy};
    }});
  vm.runInContext(core, context);
  vm.runInContext(`renderModel = () => {}; renderSession = () => {}; ${realStatus ? '' : 'renderExamples = () => {}; renderSystem = () => {};'} syncConfirmation = () => {}; syncRefine = () => {}; agentPanel = {bindSession() {agentBindings.push(state.session?.id || null);}};`, context);
  vm.runInContext(bindings, context);
  context.initialStatus = status(); vm.runInContext('state.status = initialStatus; renderSessions()', context);
  const run = (code, input) => { context.input = input; return vm.runInContext(code, context); };
  const select = id => { run('adoptSession(input); renderSessions()', active.get(id)); };
  return {get, run, select, calls, api, status, agentBindings, location, history,
    manage: () => run('openPlanManagement()'),
    managerRow: id => get('managed-session-list').children.find(row => row.dataset.sessionId === id),
    state: expression => JSON.parse(run(`JSON.stringify(${expression})`)),
    row: id => get('session-list').children.find(row => row.dataset.sessionId === id)};
}

test('sidebar is a quick-open list and management deletion fetches latest revision before enabling confirmation', async () => {
  const f = fixture(); f.select('A');
  assert.equal(f.row('A').children.length, 1); assert.equal(f.row('A').children[0].tagName, 'BUTTON');
  await f.manage(); const row = f.managerRow('A');
  assert.equal(row.children[1].children[0].tagName, 'BUTTON'); assert.equal(row.children[1].children[1].tagName, 'BUTTON');
  let release, signal;
  f.api.readOverride = (id, incoming) => { signal = incoming; return new Promise(resolve => { release = resolve; }); };
  const loading = row.children[1].children[1].click(); await flush();
  assert.equal(f.get('session-delete-dialog').open, true); assert.equal(f.get('confirm-session-delete').disabled, true);
  assert.equal(f.calls.at(-1).path, '/api/sessions/A');
  await f.get('cancel-session-delete').click(); assert.equal(signal.aborted, true);
  release(plan('A', 99)); await loading;
  assert.equal(f.get('session-delete-dialog').open, false); assert.equal(f.state('state.sessionDeletion'), null);
  assert.equal(f.calls.some(call => call.settings.method === 'POST'), false);
});

test('deleting current dirty plan clears URL, Agent and return context without a second native prompt', async () => {
  const f = fixture(); f.select('A'); f.run('state.dirty = true; state.draftAnswers.q1 = "unsaved"; state.agent = {session_id:"A", connected:true};');
  f.get('source-dialog').open = true; f.get('search-dialog').open = true;
  await f.get('delete-current-session').click();
  assert.equal(f.get('source-dialog').open, false); assert.equal(f.get('search-dialog').open, false);
  assert.match(f.get('session-delete-name').textContent, /Title A · r3/);
  assert.match(f.get('session-delete-unsaved').textContent, /未保存修改.*丢弃/);
  await Promise.all([f.get('confirm-session-delete').click(), f.get('confirm-session-delete').click()]);
  assert.equal(f.calls.filter(call => call.path.endsWith('/delete')).length, 1);
  assert.deepEqual(f.state('[state.session,state.agent,state.returnSessionId,state.dirty]'), [null, null, null, false]);
  assert.equal(new URL(f.location.href).searchParams.has('session'), false); assert.equal(f.history.state.returnSessionId, null);
  assert.equal(f.get('workbench').hidden, true); assert.equal(f.get('goal').value, '');
  assert.equal(f.agentBindings.at(-1), null); assert.equal(f.row('A'), undefined);
});

test('deleting a different plan preserves current dirty answers and stays on the same workspace', async () => {
  const f = fixture(); f.select('B'); f.run('state.dirty = true; state.draftAnswers.q1 = "keep draft";');
  await f.manage(); await f.managerRow('A').children[1].children[1].click(); await f.get('confirm-session-delete').click();
  assert.equal(f.state('state.session.id'), 'B'); assert.equal(f.state('state.draftAnswers.q1'), 'keep draft');
  assert.equal(f.state('state.dirty'), true); assert.equal(new URL(f.location.href).searchParams.get('view'), 'sessions'); assert.equal(f.history.state.returnSessionId, 'B');
  assert.equal(f.get('goal').value, 'Goal B'); assert.equal(f.row('A'), undefined);
});

test('connected-Agent 409 keeps plan intact and reports backend reason in the dialog', async () => {
  const f = fixture(); f.select('A'); f.api.deleteError = '请先断开Agent';
  await f.get('delete-current-session').click(); await f.get('confirm-session-delete').click();
  assert.equal(f.state('state.session.id'), 'A'); assert.equal(f.get('session-delete-dialog').open, true);
  assert.match(f.get('session-delete-error').textContent, /请先断开Agent/);
  assert.equal(f.get('confirm-session-delete').disabled, false); assert.equal(f.api.active.has('A'), true);
  assert.equal(f.calls.some(call => /agent\/(disconnect|interrupt)/.test(call.path)), false);
  await f.get('cancel-session-delete').click();
});

test('restoring a deleted plan uses confirmed restore and does not switch the current dirty plan', async () => {
  const f = fixture(); const deleted = {...f.api.active.get('A'), deleted_at: '2026-11-09T01:00:00Z', status: 'deleted'}; f.api.active.delete('A'); f.api.deleted.set('A', deleted);
  f.select('B'); f.run('state.dirty=true;state.draftAnswers.q1="keep";');
  await f.manage();
  await f.get('toggle-deleted-sessions').click(); assert.equal(f.calls.at(-1).path, '/api/sessions/deleted');
  const row = f.managerRow('A'); assert.equal(row.children[0].tagName, 'DIV'); await row.children[1].children[0].click();
  assert.equal(f.state('state.showDeletedSessions'), true); assert.equal(f.managerRow('A'), undefined); assert.equal(f.state('state.session.id'), 'B');
  assert.equal(f.state('state.draftAnswers.q1'), 'keep'); assert.equal(f.state('state.dirty'), true);
  assert.ok(f.row('A')); assert.equal(f.calls.some(call => call.path === '/api/sessions/A'), false);
});

test('old status and Agent payloads cannot revive a deleted plan or its history row', async () => {
  const f = fixture(); f.select('A'); const oldStatus = f.status(); let release;
  f.api.statusOnce = () => new Promise(resolve => { release = resolve; }); const oldRequest = f.run('refreshStatus(true)'); await flush();
  await f.get('delete-current-session').click(); await f.get('confirm-session-delete').click();
  release(oldStatus); await oldRequest;
  assert.equal(f.row('A'), undefined); assert.equal(f.state('state.session'), null);
  assert.equal(f.run('adoptSession(input)', plan('A', 100)), false);
  f.run('updateAgentState(input)', {session_id: 'A', connected: true, alive: true});
  assert.equal(f.state('state.agent'), null); assert.equal(f.state('state.session'), null);
});

test('deleting only a remembered return plan preserves the global-library view and clears its URL context', async () => {
  const f = fixture('http://localhost:49268/?view=libraries');
  f.run('state.workspaceView="libraries"; state.returnSessionId="A"; syncWorkspaceURL();');
  await f.run('openSessionDeletion("A")'); await f.get('confirm-session-delete').click();
  assert.equal(f.state('state.returnSessionId'), null); assert.equal(f.state('state.workspaceView'), 'libraries');
  assert.equal(new URL(f.location.href).searchParams.get('view'), 'libraries'); assert.equal(new URL(f.location.href).searchParams.has('session'), false);
});

test('summary status requests coalesce, while a lifecycle epoch aborts stale work and permits a fresh read', async () => {
  const f = fixture(); let release;
  const oldSnapshot = f.status(); f.api.statusOnce = () => new Promise(resolve => { release = resolve; });
  const one = f.run('refreshStatus(true)'), two = f.run('refreshStatus(true)'); await flush();
  assert.equal(f.calls.length, 1); assert.equal(f.calls[0].path, '/api/status?view=summary');
  f.run('state.statusRequest++'); await f.run('refreshStatus(true)');
  assert.equal(f.calls[0].settings.signal.aborted, true); assert.equal(f.calls.length, 2);
  release(oldSnapshot); await Promise.all([one, two]);
  assert.equal(f.calls.length, 2);
});

test('identical summary refreshes preserve history, examples and expanded system details with zero DOM writes', async () => {
  const f = fixture(undefined, {realStatus: true});
  const status = {...f.status(), examples: [{id: 'example', title: 'Example', path: 'model.step', goal: 'A goal'}], corpus: {ready: true, document_count: 2},
    services: {qmd: {component_available: true, library_indexes: {modes: {native: {ready: true}, qmd_vector: {ready: false, state: 'interrupted', error: 'Build owner exited before this mode completed; completed modes remain usable'}}}}}};
  f.api.statusOnce = async () => status; await f.run('refreshStatus(true)');
  const firstHistory = f.row('A').children[0], firstExample = f.get('example-list').children[0], firstService = f.get('service-list').children[0], firstCorpus = f.get('corpus-status').children[0];
  const find = (node, tag) => node.tagName === tag ? node : node.children.map(child => find(child, tag)).find(Boolean);
  const details = find(f.get('service-list'), 'DETAILS'); assert.ok(details); details.open = true;
  Node.writes.length = 0;
  f.api.statusOnce = async () => ({...structuredClone(status), fetched_at: 'a different non-rendered timestamp'});
  await f.run('refreshStatus(true)');
  assert.equal(Node.writes.length, 0, 'No same text/attribute writes or subtree replacements on identical rendered data');
  assert.equal(f.row('A').children[0], firstHistory); assert.equal(f.get('example-list').children[0], firstExample);
  assert.equal(f.get('service-list').children[0], firstService); assert.equal(f.get('corpus-status').children[0], firstCorpus);
  assert.equal(find(f.get('service-list'), 'DETAILS'), details); assert.equal(details.open, true);
});

test('status render keys still update changed history, services, index controls and failure recovery', async () => {
  const f = fixture(undefined, {realStatus: true}); await f.run('refreshStatus(true)');
  const initialHistory = f.row('A'), initialService = f.get('service-list').children[0];
  f.api.active.get('A').title = 'Updated actual title';
  f.api.statusOnce = async () => ({...f.status(), services: {sim_cli: {available: true, detail: 'Now available'}}, corpus: {ready: true, document_count: 5}});
  await f.run('refreshStatus(true)');
  assert.notEqual(f.row('A'), initialHistory); assert.match(f.row('A').textContent, /Updated actual title/);
  assert.notEqual(f.get('service-list').children[0], initialService);
  assert.match(f.get('corpus-status').textContent, /5/);
  f.run('state.indexBusy = true; renderSystem()');
  assert.equal(f.get('build-index').disabled, true); assert.equal(f.get('index-progress').hidden, false);
  f.run('state.indexBusy = false; renderSystem()');
  assert.equal(f.get('build-index').disabled, false); assert.equal(f.get('index-progress').hidden, true);

  const fresh = fixture(undefined, {realStatus: true});
  fresh.run('state.status = null');
  fresh.api.statusOnce = async () => { throw Error('temporary outage'); }; await fresh.run('refreshStatus(true)');
  assert.match(fresh.get('session-list').textContent, /暂时无法读取历史/);
  await fresh.run('refreshStatus(true)');
  assert.ok(fresh.row('A')); assert.doesNotMatch(fresh.get('session-list').textContent, /暂时无法读取历史/);
});

test('a revision change with the same timestamp refreshes the history row and deletes the latest revision', async () => {
  const f = fixture(); const previous = f.row('A');
  const value = f.api.active.get('A'), previousTime = value.updated_at;
  value.revision = 9; f.run('state.status = input; renderSessions()', f.status());
  assert.equal(value.updated_at, previousTime); assert.notEqual(f.row('A'), previous);
  await f.manage(); await f.managerRow('A').children[1].children[1].click(); await f.get('confirm-session-delete').click();
  const deletion = f.calls.find(call => call.path === '/api/sessions/A/delete');
  assert.equal(JSON.parse(deletion.settings.body).revision, 9);
});

test('management navigation is independent of selection and keeps dirty drafts when returning to the current plan', async () => {
  const f = fixture(); f.select('B'); f.run('state.dirty=true;state.draftAnswers.q1="keep this draft";');
  await f.manage();
  assert.equal(f.get('plan-management').hidden, false); assert.equal(f.get('plan-workspace').hidden, true);
  assert.equal(f.get('open-plan-management').getAttribute('aria-current'), 'page');
  assert.equal(f.get('open-workspace').getAttribute('aria-current'), null);
  assert.equal(new URL(f.location.href).searchParams.get('view'), 'sessions'); assert.equal(new URL(f.location.href).searchParams.has('session'), false);
  await f.managerRow('B').children[1].children[0].click();
  assert.equal(f.state('state.workspaceView'), 'plans'); assert.equal(f.state('state.draftAnswers.q1'), 'keep this draft'); assert.equal(f.state('state.dirty'), true);
  assert.equal(f.calls.some(call => call.path === '/api/sessions/B'), false);
  await f.manage(); f.run('window.confirm=()=>false');
  await f.managerRow('A').children[1].children[0].click();
  assert.equal(f.state('state.workspaceView'), 'sessions'); assert.equal(f.state('state.session.id'), 'B'); assert.equal(f.calls.some(call => call.path === '/api/sessions/A'), false);
  const independent = fixture('http://localhost/?view=sessions'); await independent.manage();
  assert.equal(independent.state('state.session'), null); assert.equal(independent.state('state.workspaceView'), 'sessions');
});

test('management pages reach old plans through summary pagination while the sidebar remains five quick entries', async () => {
  const f = fixture(); f.api.active.clear();
  for (let index = 0; index < 205; index++) f.api.active.set(`plan-${index}`, {...plan(`plan-${index}`), title: index === 204 ? 'Old unique plan' : `Task ${index}`});
  f.run('state.status=input;renderSessions()', f.status());
  assert.equal(f.get('session-list').children.length, 5);
  await f.manage(); assert.equal(f.get('managed-session-list').children.length, 100);
  const search = f.get('managed-session-search'); search.value = 'Old unique'; await search.emit('input');
  assert.match(f.get('managed-session-list').textContent, /没有匹配/); assert.equal(f.get('load-more-sessions').hidden, false);
  await f.get('load-more-sessions').click(); await f.get('load-more-sessions').click();
  assert.ok(f.managerRow('plan-204')); assert.equal(f.get('load-more-sessions').hidden, true); assert.match(f.get('managed-sessions-status').textContent, /205 \/ 205.*匹配 1/);
  assert.equal(f.get('managed-session-search'), search); assert.equal(search.value, 'Old unique');
  assert.deepEqual(f.calls.map(call => call.path), ['/api/sessions?offset=0&limit=100', '/api/sessions?offset=100&limit=100', '/api/sessions?offset=200&limit=100']);
  Node.writes.length = 0; f.run('renderSessions();renderPlanManagement();'); assert.equal(Node.writes.length, 0);
});

const moveToDeleted = (f, id = 'A') => { const value = {...f.api.active.get(id), deleted_at: '2026-11-09T01:00:00Z', status: 'deleted'}; f.api.active.delete(id); f.api.deleted.set(id, value); return value; };

test('permanent deletion requires a cancellable latest-revision confirmation and removes only a confirmed result', async () => {
  const f = fixture(); const value = moveToDeleted(f); f.select('B'); f.run('state.dirty=true;state.draftAnswers.q1="keep";');
  await f.manage(); await f.get('toggle-deleted-sessions').click(); value.revision = 8;
  await f.managerRow('A').children[1].children[1].click();
  assert.equal(f.get('session-purge-dialog').open, true); assert.match(f.get('session-purge-name').textContent, /Title A · r8/);
  assert.equal(f.calls.some(call => call.path.endsWith('/purge')), false);
  await f.get('cancel-session-purge').click(); assert.ok(f.managerRow('A'));
  await f.managerRow('A').children[1].children[1].click();
  await Promise.all([f.get('confirm-session-purge').click(), f.get('confirm-session-purge').click()]);
  assert.equal(f.calls.filter(call => call.path.endsWith('/purge')).length, 1);
  assert.equal(f.managerRow('A'), undefined); assert.equal(f.get('session-purge-dialog').open, false); assert.equal(f.api.deleted.has('A'), false);
  assert.equal(f.state('state.session.id'), 'B'); assert.equal(f.state('state.dirty'), true); assert.equal(f.state('state.draftAnswers.q1'), 'keep'); assert.equal(f.state('state.workspaceView'), 'sessions');
  assert.match(f.get('notice').textContent, /彻底删除.*无法恢复/); assert.equal(f.run('adoptSession(input)', plan('A', 200)), false);
});

test('purge cancellation aborts late verification and a 409 keeps the plan visible for explicit reconfirmation', async () => {
  const f = fixture(); moveToDeleted(f); await f.manage(); await f.get('toggle-deleted-sessions').click();
  let release, signal;
  f.api.deletedOnce = incoming => { signal = incoming; return new Promise(resolve => { release = resolve; }); };
  const opening = f.managerRow('A').children[1].children[1].click(); await flush();
  assert.equal(f.get('confirm-session-purge').disabled, true); await f.get('cancel-session-purge').click(); assert.equal(signal.aborted, true);
  release({sessions: [...f.api.deleted.values()]}); await opening; assert.equal(f.state('state.sessionPurge'), null);
  await f.managerRow('A').children[1].children[1].click(); f.api.purgeError = '方案版本已改变，请重新核对'; await f.get('confirm-session-purge').click();
  assert.ok(f.managerRow('A')); assert.equal(f.get('session-purge-dialog').open, true); assert.match(f.get('session-purge-error').textContent, /版本已改变/);
  assert.equal(f.calls.filter(call => call.path.endsWith('/purge')).length, 1); assert.equal(f.get('confirm-session-purge').disabled, false);
  f.api.purgeError = null; f.api.purgeOverride = async id => ({id, status: 'deleted'}); await f.get('confirm-session-purge').click();
  assert.ok(f.managerRow('A')); assert.match(f.get('session-purge-error').textContent, /未确认已彻底删除/);
});

test('purge invalidates in-flight trash, active-summary and status reads so late payloads cannot revive the plan', async () => {
  const f = fixture(); const value = moveToDeleted(f); await f.manage(); await f.get('toggle-deleted-sessions').click();
  let releaseTrash, releaseList, releaseStatus;
  f.api.deletedOnce = () => new Promise(resolve => { releaseTrash = resolve; }); const trash = f.run('refreshDeletedSessions()'); await flush();
  f.api.listOnce = () => new Promise(resolve => { releaseList = resolve; }); const active = f.run('refreshManagedSessions()'); await flush();
  f.api.statusOnce = () => new Promise(resolve => { releaseStatus = resolve; }); const status = f.run('refreshStatus(true)'); await flush();
  await f.run('openSessionPurge("A")'); await f.get('confirm-session-purge').click();
  releaseTrash({sessions: [value]}); releaseList({sessions: [plan('A')], total: 1, next_offset: null}); releaseStatus({...f.status(), sessions: [plan('A')]});
  await Promise.all([trash, active, status]);
  assert.equal(f.managerRow('A'), undefined); assert.equal(f.row('A'), undefined); assert.equal(f.state('state.deletedSessions.length'), 0); assert.equal(f.state('state.managedSessions.some(session=>session.id==="A")'), false);
});
