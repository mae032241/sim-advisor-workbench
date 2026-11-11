import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const script = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const css = readFileSync(new URL('../web/styles.css', import.meta.url), 'utf8');
const core = script.slice(0, script.indexOf('$("intake-form").addEventListener("submit", createSession);'));
const navigation = script.slice(script.indexOf('const libraryManagement = $("library-management");'), script.lastIndexOf('\ninit();'));
const plan = (id, revision = 1) => ({id, revision, goal: `goal-${id}`, status: 'needs_replan', questions: [{id: 'q1', kind: 'text', label: '工况', required: true}], answers: {}, plan: {engine: 'intake-only', steps: []}, history: []});

function classes(initial = '') {
  const values = new Set(initial.split(/\s+/).filter(Boolean));
  return {add: value => values.add(value), remove: value => values.delete(value), contains: value => values.has(value), toggle(value, force) { const enabled = force ?? !values.has(value); enabled ? values.add(value) : values.delete(value); return enabled; }};
}

// Navigation uses existing element identities. Renderers are deliberately stubbed:
// native GUI QA covers layout, while these tests exercise the shipped lifecycle,
// navigation event handlers, URL state and network boundaries without a big DOM.
function fixture(url = 'http://127.0.0.1:4321/', historyState = null) {
  const f = {requests: [], mounts: 0, refreshes: 0, confirm: true, prompts: 0, renders: 0};
  const elements = new Map();
  for (const match of html.matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const attributes = new Map([...match[2].matchAll(/([\w-]+)="([^"]*)"/g)].map(value => [value[1], value[2]]));
    const element = {id: match[3], value: '', textContent: '', hidden: /(?:^|\s)hidden(?:\s|$)/.test(match[2]), open: /(?:^|\s)open(?:\s|$)/.test(match[2]), classList: classes(attributes.get('class')), attributes, listeners: new Map(),
      setAttribute(key, value) { attributes.set(key, String(value)); }, getAttribute: key => attributes.get(key) ?? null, removeAttribute: key => attributes.delete(key),
      addEventListener(type, listener) { const listeners = this.listeners.get(type) || []; listeners.push(listener); this.listeners.set(type, listeners); },
      async click() { for (const listener of this.listeners.get('click') || []) await listener({preventDefault() {}}); },
      focus() { f.focus = this.id; }, scrollIntoView() {}, showModal() { this.open = true; }, close() { this.open = false; },
      reset() { elements.get('goal').value = ''; elements.get('model-path').value = ''; }};
    elements.set(element.id, element);
  }
  const location = {href: url}, history = {state: historyState, replaceState(value, _title, next) { this.state = value; location.href = String(next); }};
  const window = {location, history, confirm() { f.prompts++; return f.confirm; }, SimAdvisorLibraryPanel: {mount(target) { assert.equal(target.id, 'library-management'); f.mounts++; return {ready: Promise.resolve(), refresh() { f.refreshes++; }}; }}};
  const context = vm.createContext({window, document: {getElementById: id => elements.get(id) || null, body: {classList: classes()}}, URL, URLSearchParams, AbortController, setTimeout, clearTimeout, console,
    fetch: async path => { f.requests.push(path); if (path.startsWith('/api/sessions?')) return {ok: true, status: 200, json: async () => ({sessions: [], total: 0, next_offset: null})}; const id = path.match(/^\/api\/sessions\/([^/]+)$/)?.[1]; if (!id) throw Error(`Unexpected request: ${path}`); return {ok: true, status: 200, json: async () => plan(id)}; }});
  vm.runInContext(core, context);
  vm.runInContext('renderModel = () => {}; renderSession = () => {}; renderSessions = () => {}; renderPlanManagement = () => {}; refreshStatus = async () => {}; notice = () => {}; setBusy = (operation) => { state.operation = operation; };', context);
  vm.runInContext(navigation, context);
  f.run = expression => vm.runInContext(expression, context);
  f.read = expression => JSON.parse(f.run(`JSON.stringify(${expression})`));
  f.get = id => { assert.ok(elements.has(id), id); return elements.get(id); };
  f.location = location; f.history = history; f.init = () => f.run('init()'); return f;
}

test('the global library section is outside the entire plan workspace; both mobile entries exist', () => {
  const stack = [], parents = new Map();
  for (const match of html.matchAll(/<\/?section\b[^>]*>/g)) {
    if (match[0].startsWith('</')) stack.pop();
    else { const id = match[0].match(/\bid="([^"]+)"/)?.[1] || ''; if (id) parents.set(id, [...stack]); stack.push(id); }
  }
  for (const id of ['intake', 'workbench', 'how-it-works']) assert.ok(parents.get(id).includes('plan-workspace'));
  assert.equal(parents.get('library-management').includes('plan-workspace'), false);
  assert.equal(parents.get('plan-management').includes('plan-workspace'), false);
  assert.match(html, /id="open-plan-management-mobile"/);
  assert.match(html, /id="open-workspace-mobile"/); assert.match(html, /id="open-libraries-mobile"/);
  assert.match(css, /@media\(max-width:720px\)\{\s*\.mobile-view-nav\{display:flex/);
});

test('desktop navigation hides plan content without clearing input or remounting the library', async () => {
  const f = fixture(); await f.init(); f.get('goal').value = '尚未提交的目标'; f.get('model-path').value = 'D:/part.step';
  assert.equal(f.mounts, 0, 'Starting on plans does not load libraries or trigger model directory discovery');
  await f.get('open-libraries').click();
  assert.equal(f.get('plan-workspace').hidden, true); assert.equal(f.get('library-management').hidden, false);
  assert.equal(f.get('open-libraries').getAttribute('aria-current'), 'page');
  assert.equal(new URL(f.location.href).searchParams.get('view'), 'libraries'); assert.equal(f.requests.length, 0);
  await f.get('open-workspace').click();
  assert.equal(f.get('plan-workspace').hidden, false); assert.equal(f.get('library-management').hidden, true);
  assert.equal(f.get('goal').value, '尚未提交的目标'); assert.equal(f.get('model-path').value, 'D:/part.step');
  assert.equal(f.mounts, 1); assert.equal(f.refreshes, 0);
  await f.get('open-libraries').click(); assert.equal(f.mounts, 1); assert.equal(f.refreshes, 1);
});

test('returning through mobile navigation preserves loaded plan, unsaved answers and open inputs', async () => {
  const f = fixture('http://127.0.0.1:4321/?session=A'); await f.init();
  f.run('state.draftAnswers.q1 = "draft"; state.dirty = true; state.tab = "intent";'); f.get('task-inputs').open = true;
  await f.get('open-libraries-mobile').click(); await f.get('open-workspace-mobile').click();
  assert.equal(f.read('state.session.id'), 'A'); assert.equal(f.read('state.draftAnswers.q1'), 'draft');
  assert.equal(f.read('state.dirty'), true); assert.equal(f.read('state.tab'), 'intent'); assert.equal(f.get('task-inputs').open, true);
  assert.equal(f.prompts, 0); assert.deepEqual(f.requests, ['/api/sessions/A']);
});

test('background plan adoption cannot steal the global-library view or put session into its URL', async () => {
  const f = fixture('http://127.0.0.1:4321/?session=A'); await f.init(); await f.get('open-libraries').click();
  f.run(`adoptSession(${JSON.stringify(plan('A', 2))})`);
  assert.equal(f.get('plan-workspace').hidden, true); assert.equal(f.get('library-management').hidden, false);
  assert.equal(f.read('state.session.revision'), 2); assert.equal(new URL(f.location.href).searchParams.has('session'), false);
  assert.equal(f.history.state.returnSessionId, 'A'); assert.equal(f.get('workspace-name').textContent, '资料库管理');
});

test('library deep links and refresh do not load sessions; explicit return restores saved context', async () => {
  const f = fixture('http://127.0.0.1:4321/?view=libraries&session=A'); await f.init();
  assert.equal(f.mounts, 1, 'A direct library URL still initializes the library panel');
  assert.equal(f.get('plan-workspace').hidden, true); assert.equal(f.requests.length, 0);
  assert.equal(new URL(f.location.href).searchParams.has('session'), false);
  const refreshed = fixture(f.location.href, f.history.state); await refreshed.init(); assert.equal(refreshed.requests.length, 0);
  await refreshed.get('open-workspace-mobile').click();
  assert.deepEqual(refreshed.requests, ['/api/sessions/A']); assert.equal(refreshed.get('plan-workspace').hidden, false);
  assert.equal(new URL(refreshed.location.href).searchParams.get('session'), 'A');
});

test('a library deep link starts its panel without waiting for unrelated global status', async () => {
  const f = fixture('http://localhost:49268/?view=libraries');
  f.run('globalThis.releaseStatus = null; refreshStatus = () => new Promise(resolve => {releaseStatus = resolve;});');
  const initializing = f.init();
  assert.equal(f.mounts, 1); assert.equal(f.get('library-management').hidden, false);
  f.run('releaseStatus()'); await initializing; assert.equal(f.mounts, 1);
});

test('new task returns to the plan view and clears return context only after the existing dirty guard', async () => {
  const f = fixture('http://127.0.0.1:4321/?session=A'); await f.init(); await f.get('open-libraries').click();
  f.run('state.dirty = true'); f.confirm = false; f.run('newSession()');
  assert.equal(f.get('library-management').hidden, false); assert.equal(f.read('state.session.id'), 'A');
  f.confirm = true; f.run('newSession()');
  assert.equal(f.get('plan-workspace').hidden, false); assert.equal(f.get('library-management').hidden, true);
  assert.equal(f.read('state.session'), null); assert.equal(f.history.state.returnSessionId, null);
  assert.equal(new URL(f.location.href).search, ''); assert.equal(f.get('task-inputs').open, true); assert.equal(f.mounts, 1);
});

test('explicit history restoration returns to the plan view; search remains global', async () => {
  const f = fixture('http://127.0.0.1:4321/?view=libraries'); await f.init();
  f.run('openSearch()'); assert.equal(f.get('search-dialog').open, true); assert.equal(f.get('library-management').hidden, false);
  await f.run('restoreSession("B")');
  assert.equal(f.read('state.session.id'), 'B'); assert.equal(f.get('plan-workspace').hidden, false);
  assert.equal(f.get('library-management').hidden, true); assert.equal(f.get('open-workspace').getAttribute('aria-current'), 'page');
});

test('management deep links load summaries independently and remember a plan only for explicit return', async () => {
  const f = fixture('http://localhost/?view=sessions&session=A');
  f.run('globalThis.releaseStatus = null; refreshStatus = () => new Promise(resolve => {releaseStatus = resolve;});');
  const initializing = f.init();
  assert.equal(f.get('plan-management').hidden, false); assert.equal(f.get('plan-workspace').hidden, true);
  assert.deepEqual(f.requests, ['/api/sessions?offset=0&limit=100']);
  f.run('releaseStatus()'); await initializing;
  assert.equal(f.read('state.session'), null); assert.equal(new URL(f.location.href).searchParams.has('session'), false);
  const refreshed = fixture(f.location.href, f.history.state); await refreshed.init();
  assert.equal(refreshed.read('state.session'), null); assert.deepEqual(refreshed.requests, ['/api/sessions?offset=0&limit=100']);
  await refreshed.get('open-workspace').click(); assert.equal(refreshed.read('state.session.id'), 'A'); assert.equal(refreshed.read('state.workspaceView'), 'plans');
});

test('desktop and mobile management entries preserve current drafts and background adoption cannot change views', async () => {
  const f = fixture('http://localhost/?session=A'); await f.init();
  f.run('state.dirty=true;state.draftAnswers.q1="draft"'); await f.get('open-plan-management-mobile').click();
  assert.equal(f.get('plan-management').hidden, false); assert.equal(f.get('open-plan-management').getAttribute('aria-current'), 'page'); assert.equal(f.prompts, 0);
  assert.equal(f.read('state.dirty'), true); assert.equal(f.read('state.draftAnswers.q1'), 'draft');
  f.run(`adoptSession(${JSON.stringify(plan('A', 2))})`);
  assert.equal(f.read('state.workspaceView'), 'sessions'); assert.equal(new URL(f.location.href).searchParams.get('view'), 'sessions'); assert.equal(new URL(f.location.href).searchParams.has('session'), false);
  await f.get('open-workspace-mobile').click(); await f.get('open-plan-management').click();
  assert.equal(f.read('state.workspaceView'), 'sessions'); assert.equal(f.get('library-management').hidden, true);
});
