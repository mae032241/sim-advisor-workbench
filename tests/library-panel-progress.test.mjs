import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/library-panel.js', import.meta.url), 'utf8');
const epoch = Date.parse('2026-11-08T08:00:00Z');
const jobId = 'a'.repeat(32);

// A small DOM surface, with reflected progress attributes and real async fetch
// boundaries. It exercises the shipped mount/controller without a DOM dependency.
class Element {
  constructor(tag, document) { this.tagName = tag.toUpperCase(); this.ownerDocument = document; this.children = []; this.attributes = new Map(); this.listeners = new Map(); this.className = ''; this._text = ''; this._value = ''; this.hidden = false; this.open = false; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.children.forEach(child => { child.parentNode = null; }); this.children = []; this._text = String(value); }
  get classList() { const element = this; return {add(...values) { const classes = new Set(element.className.split(/\s+/).filter(Boolean)); values.forEach(value => classes.add(value)); element.className = [...classes].join(' '); }, remove(...values) { element.className = element.className.split(/\s+/).filter(value => !values.includes(value)).join(' '); }, contains(value) { return element.className.split(/\s+/).includes(value); }, toggle(value, force) { const enabled = force ?? !this.contains(value); enabled ? this.add(value) : this.remove(value); return enabled; }}; }
  append(...children) { for (const child of children) { const value = typeof child === 'string' ? Object.assign(new Element('span', this.ownerDocument), {textContent: child}) : child; value.remove(); value.parentNode = this; this.children.push(value); } }
  insertBefore(child, reference) { if (child === reference) return child; if (reference !== null && !this.children.includes(reference)) throw Error('Reference node is not a child'); child.remove(); const index = reference === null ? this.children.length : this.children.indexOf(reference); child.parentNode = this; this.children.splice(index, 0, child); return child; }
  contains(value) { return value === this || this.children.some(child => child.contains(value)); }
  replaceChildren(...children) { this.textContent = ''; this.append(...children); }
  remove() { if (this.parentNode) { if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = null; if (this.contains(this.ownerDocument.selection?.anchorNode)) this.ownerDocument.selection = null; this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; } }
  setAttribute(name, value) { this.attributes.set(name, String(value)); if (name === 'class') this.className = String(value); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  set value(value) { this._value = String(value); if (this.tagName === 'PROGRESS') this.setAttribute('value', value); }
  get value() { return this.tagName === 'PROGRESS' ? Number(this.getAttribute('value') || 0) : this.tagName === 'SELECT' && !this._value ? this.options[0]?.value || '' : this._value; }
  set max(value) { this.setAttribute('max', value); }
  get max() { return Number(this.getAttribute('max') || 1); }
  get options() { return this.children.filter(child => child.tagName === 'OPTION'); }
  addEventListener(type, listener) { const listeners = this.listeners.get(type) || []; listeners.push(listener); this.listeners.set(type, listeners); }
  async emit(type, details = {}) { for (const listener of this.listeners.get(type) || []) await listener({type, target: this, preventDefault() {}, ...details}); }
  matches(selector) { if (selector === 'input:checked') return this.tagName === 'INPUT' && this.checked; if (selector.startsWith('.')) return this.classList.contains(selector.slice(1)); return this.tagName.toLowerCase() === selector; }
  querySelectorAll(selector) { const found = []; const visit = element => { for (const child of element.children) { if (child.matches(selector)) found.push(child); visit(child); } }; visit(this); return found; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  scrollIntoView() { this.scrolled = true; }
  focus() { this.ownerDocument.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; void this.emit('close'); }
}

const flush = async () => { for (let index = 0; index < 80; index++) await Promise.resolve(); };
function clock() {
  let time = 0, next = 1;
  const timers = new Map();
  const add = (callback, delay, interval = false) => { const id = next++; timers.set(id, {callback, delay, at: time + delay, interval}); return id; };
  return {timers, now: () => epoch + time, setTimeout: (callback, delay) => add(callback, delay), setInterval: (callback, delay) => add(callback, delay, true), clear: id => timers.delete(id),
    async tick(amount) { const end = time + amount; for (;;) { const due = [...timers].filter(([, timer]) => timer.at <= end).sort((left, right) => left[1].at - right[1].at)[0]; if (!due) break; const [id, timer] = due; time = timer.at; if (timer.interval) timer.at += timer.delay; else timers.delete(id); timer.callback(); await flush(); } time = end; await flush(); }};
}
function running(overrides = {}) {
  return {id: jobId, library_id: 'models', status: 'running', stage: 'extracting', started_at: new Date(epoch - 123000).toISOString(), updated_at: new Date(epoch).toISOString(), completed: 2, total: 4,
    progress: {completed: 2, total: 4, unit: 'files', current_file: 'D:/cases/bracket.mph', detail: '读取模型配置'}, counts: {succeeded: 1, partial: 1, failed: 0}, items: [], failed: [], ...overrides};
}
async function fixture(job = running(), options = {}) {
  const timer = clock(), calls = [], document = {createElement: tag => new Element(tag, document)}, target = document.createElement('main');
  const libraries = [{id: 'idle', name: '其他资料库', type: 'official-manuals', roots: []}, {id: 'models', name: '模型试点', type: 'user-models', roots: []}];
  const api = {snapshot: {libraries, jobs: job ? [job] : [], indexes: {modes: {native: {ready: true}, qmd_keyword: {ready: true}, qmd_vector: {ready: false}}}}, getJob: async () => api.snapshot.jobs[0], build: running({status: 'queued', stage: 'queued', progress: {completed: null, total: null, unit: 'files', current_file: null, detail: ''}})};
  api.getLibraries = async url => ({...api.snapshot, libraries: api.snapshot.libraries.filter(library => url.includes('include_deleted=1') || !library.deleted_at)});
  api.evaluationSets = {sets: [], set_count: 0, model_count: 0, read_only: true, index_rebuilt: false, retrieval_enabled: false, access_token: 'web-access-token'};
  api.getEvaluationSets = async () => api.evaluationSets;
  api.changeEvaluationAccess = async body => { api.evaluationSets = {...api.evaluationSets, retrieval_enabled: body.enabled, access_token: 'next-web-access-token'}; return api.evaluationSets; };
  api.models = async id => ({library_id: id, files: [{path: `D:/cases/${id}.mph`, relative_path: `${id}.mph`, root_id: 'main'}], has_more: false, missing_roots: [], status: 'ready'});
  api.changeLibrary = async (id, action, body) => {
    const library = api.snapshot.libraries.find(item => item.id === id);
    if (action === 'purge') { api.snapshot.libraries = api.snapshot.libraries.filter(item => item.id !== id); return {library_id: id, status: 'purged', counts: {objects: 1}}; }
    if (action === 'delete') Object.assign(library, {deleted_at: new Date(timer.now()).toISOString(), enabled: false});
    else if (action === 'restore') Object.assign(library, {deleted_at: null, enabled: false});
    else library.enabled = body.enabled;
    return library;
  };
  api.changeLibraries = async body => ({libraries: api.snapshot.libraries.filter(library => body.library_ids.includes(library.id)).map(library => Object.assign(library, {enabled: body.enabled})), library_ids: body.library_ids, enabled: body.enabled, index_rebuilt: false});
  api.cancelJob = async id => { const accepted = {...api.snapshot.jobs.find(value => value.id === id), status: 'cancel_requested', cancel_requested_at: new Date(timer.now()).toISOString()}; api.snapshot.jobs = api.snapshot.jobs.map(value => value.id === id ? accepted : value); return accepted; };
  api.rebind = async (id, body) => { const library = api.snapshot.libraries.find(item => item.id === id); library.roots = library.roots.map(root => root.id === body.root_id ? {...root, path: body.path} : root); return {...library, verified_evidence: 2}; };
  api.embeddingSettings = {enabled: false, configured: false, model_path: '', detail: '默认未启用'};
  api.getEmbeddingSettings = async () => api.embeddingSettings;
  api.saveEmbeddingSettings = async body => { const model_path = body.model_path ?? api.embeddingSettings.model_path; api.embeddingSettings = {enabled: body.enabled, configured: Boolean(model_path), model_path}; return api.embeddingSettings; };
  api.purgeProgress = async id => ({library_id: id, status: 'idle', stage: null, completed: null, total: null, unit: null});
  api.previewCleanup = async () => ({file_count: 3, bytes: 14 * 1024 * 1024, generation_count: 2, preview_token: 'preview-current-and-history', can_clean: true, building: false, skipped_count: 1, warnings: ['无法安全归属的文件已保留。']});
  api.cleanupVectors = async () => ({status: 'cleaned', cleaned: true, file_count: 3, bytes: 14 * 1024 * 1024, generation_count: 2});
  const fetch = async (url, settings = {}) => {
    calls.push({url, settings}); let data;
    if (url === '/api/libraries?view=summary' || url === '/api/libraries?view=summary&include_deleted=1') data = await api.getLibraries(url, settings.signal);
    else if (url === '/api/libraries/evaluation-sets') data = await api.getEvaluationSets(settings.signal);
    else if (url === '/api/libraries/evaluation-sets/access' && settings.method === 'POST') data = await api.changeEvaluationAccess(JSON.parse(settings.body), settings.signal);
    else if (url === '/api/retrieval/embedding-settings') data = settings.method === 'POST' ? await api.saveEmbeddingSettings(JSON.parse(settings.body), settings.signal) : await api.getEmbeddingSettings(settings.signal);
    else if (url === '/api/library-indexes/unfinished-vectors') data = settings.method === 'POST' ? await api.cleanupVectors(JSON.parse(settings.body), settings.signal) : await api.previewCleanup(settings.signal);
    else if (url === '/api/libraries/enable' && settings.method === 'POST') data = await api.changeLibraries(JSON.parse(settings.body), settings.signal);
    else if (url.endsWith('/purge-progress')) data = await api.purgeProgress(url.split('/').at(-2), settings.signal);
    else if (/^\/api\/libraries\/[^/]+\/models$/.test(url)) data = await api.models(url.split('/').at(-2), settings.signal);
    else if (url.endsWith('/rebind') && settings.method === 'POST') data = await api.rebind(url.split('/').at(-2), JSON.parse(settings.body), settings.signal);
    else if (/\/(enable|delete|restore|purge)$/.test(url) && settings.method === 'POST') data = await api.changeLibrary(url.split('/').at(-2), url.split('/').at(-1), JSON.parse(settings.body), settings.signal);
    else if (url.startsWith('/api/search?')) data = await api.search(url, settings.signal);
    else if (url.startsWith('/api/sources/')) data = await api.source(url, settings.signal);
    else if (url.startsWith('/api/library-objects/')) data = await api.configuration(url, settings.signal);
    else if (url.endsWith('/cancel') && settings.method === 'POST') data = await api.cancelJob(url.split('/').at(-2), settings.signal);
    else if (url.startsWith('/api/library-jobs/')) data = await api.getJob(url.split('/').pop().split('?')[0], settings.signal);
    else if (url.endsWith('/build')) data = typeof api.build === 'function' ? await api.build(settings.signal) : api.build;
    else throw Error(`Unexpected endpoint: ${url}`);
    return {ok: true, status: 200, json: async () => structuredClone(data)};
  };
  class LocalDate extends Date { constructor(...args) { super(...(args.length ? args : [timer.now()])); } static now() { return timer.now(); } }
  const window = {};
  vm.runInNewContext(source, {window, document, Element, fetch, AbortController, URLSearchParams, Date: LocalDate,
    setTimeout: timer.setTimeout, clearTimeout: timer.clear, setInterval: timer.setInterval, clearInterval: timer.clear});
  const controller = window.SimAdvisorLibraryPanel.mount(target, options); await controller.ready;
  const get = selector => { const element = target.querySelector(selector); assert.ok(element, selector); return element; };
  return {target, api, calls, timer, controller, get};
}

async function relocatable(options = {}) {
  const f = await fixture(null, options);
  f.api.snapshot.libraries[1].roots = [{id: 'main', role: 'models', path: 'D:/old/models'}, {id: 'docs', role: 'docs', path: 'D:/old/docs'}];
  await f.controller.refresh(); f.controller.selectLibrary('models'); await flush();
  return f;
}

const cleanupPath = '/api/library-indexes/unfinished-vectors';
const cleanupPosts = f => f.calls.filter(call => call.url === cleanupPath && call.settings.method === 'POST');
const embeddingPath = '/api/retrieval/embedding-settings';
const embeddingPosts = f => f.calls.filter(call => call.url === embeddingPath && call.settings.method === 'POST');

test('isolated evaluation sets load on demand with one global access entry and readonly case summaries', async () => {
  const f = await fixture(null), before = f.calls.length;
  f.api.evaluationSets = {...f.api.evaluationSets, sets: [{id: 'diverse-cases', name: '跨方向测试集', description: '用于独立评估的现有案例。', model_count: 2,
    models: [{title: '换热器', direction: '传热与流动', object_id: 'private-object', text: 'PRIVATE ANSWER'}, {title: '<script>字面标题</script>', direction: '声学'}]}], read_only: true};
  assert.equal(f.calls.some(call => call.url === '/api/libraries/evaluation-sets'), false);
  await f.get('.lp-tab-evaluation').emit('click'); await flush();
  assert.deepEqual(f.calls.slice(before).map(call => call.url), ['/api/libraries/evaluation-sets']);
  assert.equal(f.get('.lp-tab-evaluation').getAttribute('aria-selected'), 'true');
  assert.match(f.get('.lp-selected-heading').textContent, /跨方向测试集/);
  assert.match(f.get('.lp-evaluation-summary').textContent, /传热与流动.*换热器.*声学.*<script>字面标题<\/script>/);
  assert.doesNotMatch(f.get('.lp-evaluation-summary').textContent, /PRIVATE ANSWER|private-object/);
  assert.match(f.get('.lp-evaluation-notice').textContent, /不参与 Agent 检索.*不能由 Agent 启用/);
  for (const selector of ['.lp-global-toolbar', '.lp-library-actions', '.lp-live-progress', '.lp-build', '.lp-search-form', '.lp-tree-actions', '.lp-library-status-filter']) assert.equal(f.get(selector).hidden, true, selector);
  assert.equal(f.get('.lp-library-list').querySelectorAll('.lp-group-switch').length, 0);
  assert.equal(f.get('.lp-build-submit').disabled, true);
  await f.get('.lp-build-form').emit('submit'); await f.get('.lp-library-enable').emit('click');
  assert.equal(f.calls.slice(before).some(call => call.settings.method === 'POST'), false);
  assert.equal(f.get('.lp-evaluation-summary').querySelectorAll('button').length, 1);
  assert.equal(f.get('.lp-evaluation-access-toggle').textContent, '开放测试集检索'); assert.equal(f.get('.lp-evaluation-access-toggle').disabled, false);
  await f.get('.lp-tab-current').emit('click'); await flush();
  assert.equal(f.get('.lp-evaluation-summary').hidden, true); assert.equal(f.get('.lp-global-toolbar').hidden, false);
  assert.equal(f.get('.lp-search-form').hidden, false); assert.equal(f.get('.lp-evaluation-access-toggle').disabled, true); f.controller.destroy();
});

test('evaluation summary failures remain readonly and refreshing recovers without creating or rebuilding a library', async () => {
  const f = await fixture(null); f.api.getEvaluationSets = async () => { throw Error('registry temporarily unavailable'); };
  await f.get('.lp-tab-evaluation').emit('click'); await flush();
  assert.match(f.get('.lp-evaluation-summary').textContent, /摘要读取失败.*registry temporarily unavailable/);
  assert.equal(f.get('.lp-evaluation-access-toggle').disabled, true);
  await f.get('.lp-evaluation-access-toggle').emit('click');
  assert.equal(f.get('.lp-library-actions').hidden, true); assert.equal(f.get('.lp-build-submit').disabled, true);
  f.api.getEvaluationSets = async () => ({...f.api.evaluationSets, sets: [{id: 'empty', name: '空测试集', models: [], model_count: 0}]});
  await f.get('.lp-button-subtle').emit('click'); await flush();
  assert.match(f.get('.lp-selected-heading').textContent, /空测试集/); assert.match(f.get('.lp-evaluation-summary').textContent, /尚无案例摘要/);
  assert.equal(f.get('.lp-evaluation-access-toggle').disabled, false);
  assert.equal(f.calls.some(call => call.settings.method === 'POST'), false); f.controller.destroy();
});

test('evaluation tabs and directory filter support keyboard navigation while keeping heldout sets out of library selection', async () => {
  const f = await fixture(null); f.api.evaluationSets = {sets: [{id: 'thermal', name: '传热集', models: [{title: '换热器', direction: '传热'}]}, {id: 'acoustic', name: '声学集', models: [{title: '扬声器', direction: '声学'}]}]};
  await f.get('.lp-tab-current').emit('keydown', {key: 'End'}); await flush();
  assert.equal(f.target.ownerDocument.activeElement, f.get('.lp-tab-evaluation'));
  const query = f.get('.lp-library-filter'); query.value = '声学'; await query.emit('input');
  const cards = f.get('.lp-library-list').querySelectorAll('.lp-evaluation-card'); assert.equal(cards.length, 1);
  await cards[0].emit('click'); assert.match(f.get('.lp-selected-heading').textContent, /声学集/);
  f.controller.selectLibrary('models'); assert.match(f.get('.lp-selected-heading').textContent, /声学集/);
  await f.get('.lp-tab-evaluation').emit('keydown', {key: 'Home'}); await flush();
  assert.equal(f.get('.lp-tab-current').getAttribute('aria-selected'), 'true'); assert.equal(f.target.ownerDocument.activeElement, f.get('.lp-tab-current'));
  assert.equal(f.get('.lp-tree-actions').hidden, false); f.controller.destroy();
});

test('evaluation summaries cannot replace another tab after a late response and background progress stays hidden', async () => {
  const f = await fixture(running()); f.api.evaluationSets = {sets: [{id: 'set', name: '隔离集', models: []}]};
  await f.get('.lp-tab-evaluation').emit('click'); await f.timer.tick(2500);
  assert.equal(f.get('.lp-live-progress').hidden, true); assert.equal(f.get('.lp-library-actions').hidden, true);
  let release; f.api.getEvaluationSets = async () => { await new Promise(resolve => { release = resolve; }); return {sets: [{id: 'late', name: '迟到摘要', models: []}]}; };
  const reading = f.controller.refresh(true); await flush();
  await f.get('.lp-tab-current').emit('click'); release(); await reading;
  assert.equal(f.get('.lp-tab-current').getAttribute('aria-selected'), 'true');
  assert.doesNotMatch(f.get('.lp-selected-heading').textContent, /迟到摘要/);
  assert.equal(f.get('.lp-evaluation-summary').hidden, true); f.controller.destroy();
});

const evaluationAccessPath = '/api/libraries/evaluation-sets/access';
const evaluationAccessPosts = f => f.calls.filter(call => call.url === evaluationAccessPath && call.settings.method === 'POST');

test('one explicit web control opens and isolates all evaluation groups with fresh access tokens and independent library flags', async () => {
  const f = await fixture(null);
  f.api.snapshot.libraries[0].enabled = false; f.api.snapshot.libraries[1].enabled = true;
  const originalLibraries = structuredClone(f.api.snapshot.libraries);
  f.api.evaluationSets.sets = [{id: 'thermal', name: '传热集', models: [{title: '换热器', direction: '传热'}]}, {id: 'acoustic', name: '声学集', models: [{title: '扬声器', direction: '声学'}]}];
  assert.equal(f.target.querySelectorAll('.lp-evaluation-access-toggle').length, 0, 'Ordinary libraries have no evaluation access entry');
  await f.get('.lp-tab-evaluation').emit('click');
  const reads = f.calls.length;
  assert.equal(f.get('.lp-tab-evaluation').textContent, '测试集');
  assert.match(f.get('.lp-evaluation-notice').textContent, /开放后仍遵循原资料库启用范围/);
  assert.equal(evaluationAccessPosts(f).length, 0, 'Reading the evaluation view cannot open retrieval');
  assert.equal(f.target.querySelectorAll('.lp-evaluation-access-toggle').length, 1);
  assert.ok(f.get('.lp-library-list').querySelectorAll('.lp-evaluation-card').every(card => card.querySelectorAll('button').length === 0));
  await f.get('.lp-evaluation-access-toggle').emit('click');
  assert.deepEqual(JSON.parse(evaluationAccessPosts(f)[0].settings.body), {enabled: true, access_token: 'web-access-token'});
  assert.equal(f.calls.length, reads + 1, 'Opening uses the returned summary without extra reads, indexing or builds');
  assert.equal(f.get('.lp-evaluation-access-toggle').textContent, '恢复测试集隔离');
  assert.equal(f.get('.lp-evaluation-access-toggle').getAttribute('aria-pressed'), 'true');
  assert.ok(f.get('.lp-library-list').querySelectorAll('.lp-evaluation-card').every(card => /隔离已解除/.test(card.textContent)));
  assert.match(f.get('.lp-evaluation-notice').textContent, /隔离已解除.*原资料库启用范围/);
  await f.get('.lp-library-list').querySelectorAll('.lp-evaluation-card')[1].emit('click');
  assert.match(f.get('.lp-selected-heading').textContent, /声学集/);
  assert.equal(f.get('.lp-evaluation-access-toggle').textContent, '恢复测试集隔离');
  assert.equal(f.target.querySelectorAll('.lp-evaluation-access-toggle').length, 1, 'Changing groups keeps the single global entry');
  await f.get('.lp-evaluation-access-toggle').emit('click');
  assert.deepEqual(JSON.parse(evaluationAccessPosts(f)[1].settings.body), {enabled: false, access_token: 'next-web-access-token'});
  assert.equal(f.get('.lp-evaluation-access-toggle').textContent, '开放测试集检索');
  assert.ok(f.get('.lp-library-list').querySelectorAll('.lp-evaluation-card').every(card => /已隔离/.test(card.textContent)));
  assert.deepEqual(f.api.snapshot.libraries, originalLibraries);
  assert.doesNotMatch(f.target.textContent, /web-access-token/);
  await f.get('.lp-tab-current').emit('click');
  assert.equal(f.get('.lp-evaluation-summary').hidden, true); assert.equal(f.get('.lp-evaluation-access-toggle').disabled, true);
  await f.get('.lp-evaluation-access-toggle').emit('click');
  await f.get('.lp-tab-deleted').emit('click'); await f.get('.lp-evaluation-access-toggle').emit('click');
  assert.equal(evaluationAccessPosts(f).length, 2, 'Ordinary and deleted views cannot use the hidden entry');
  const agentSource = readFileSync(new URL('../web/agent-panel.js', import.meta.url), 'utf8');
  assert.doesNotMatch(agentSource, /evaluation-sets\/access|开放测试集检索|恢复测试集隔离/);
  f.controller.destroy();
});

test('evaluation access stays locked until an explicit boolean state and nonempty web token are read', async () => {
  for (const fields of [{}, {retrieval_enabled: 'false', access_token: 'token'}, {retrieval_enabled: true}, {retrieval_enabled: false, access_token: ''}, {retrieval_enabled: false, access_token: '  '}]) {
    const f = await fixture(null); f.api.evaluationSets = {sets: [{id: 'set', name: '测试集', models: []}], ...fields};
    await f.get('.lp-tab-evaluation').emit('click');
    assert.equal(f.get('.lp-evaluation-access-toggle').disabled, true);
    assert.match(f.get('.lp-evaluation-access-status').textContent, /未确认.*刷新状态核对/);
    await f.get('.lp-evaluation-access-toggle').emit('click');
    assert.equal(evaluationAccessPosts(f).length, 0); f.controller.destroy();
  }
});

test('evaluation access waits for service confirmation and prevents duplicate clicks or overlapping refresh', async () => {
  const f = await fixture(null); await f.get('.lp-tab-evaluation').emit('click');
  let release; f.api.changeEvaluationAccess = body => new Promise(resolve => { release = () => resolve({...f.api.evaluationSets, retrieval_enabled: body.enabled}); });
  const submitting = f.get('.lp-evaluation-access-toggle').emit('click'); await flush();
  assert.equal(f.get('.lp-evaluation-access-toggle').disabled, true);
  assert.equal(f.get('.lp-evaluation-access').getAttribute('aria-busy'), 'true');
  assert.equal(f.get('.lp-evaluation-access-toggle').getAttribute('aria-pressed'), 'false', 'No optimistic state change');
  await f.get('.lp-evaluation-access-toggle').emit('click');
  const before = f.calls.length; assert.equal(await f.controller.refresh(), false); assert.equal(f.calls.length, before);
  release(); await submitting;
  assert.equal(evaluationAccessPosts(f).length, 1); assert.equal(f.get('.lp-evaluation-access-toggle').disabled, false);
  assert.equal(f.get('.lp-evaluation-access').getAttribute('aria-busy'), 'false');
  assert.equal(f.get('.lp-evaluation-access-toggle').textContent, '恢复测试集隔离'); f.controller.destroy();
});

test('failed or unconfirmed evaluation updates require a manual refresh and never guess the current access state', async () => {
  for (const fail of [async () => { throw Error('access denied'); }, async () => ({retrieval_enabled: false, access_token: 'token'})]) {
    const f = await fixture(null); await f.get('.lp-tab-evaluation').emit('click'); f.api.changeEvaluationAccess = fail;
    await f.get('.lp-evaluation-access-toggle').emit('click');
    assert.equal(f.get('.lp-evaluation-access-toggle').disabled, true);
    assert.equal(f.get('.lp-evaluation-access-toggle').getAttribute('aria-pressed'), 'mixed');
    assert.match(f.get('.lp-evaluation-access-status').textContent, /更新未确认.*刷新.*不会自动重发/);
    assert.match(f.get('.lp-evaluation-notice').textContent, /当前检索状态待核对/);
    await f.get('.lp-evaluation-access-toggle').emit('click'); await f.timer.tick(60000);
    assert.equal(evaluationAccessPosts(f).length, 1);
    f.api.evaluationSets.retrieval_enabled = true; f.api.evaluationSets.access_token = 'manually-refreshed-token';
    await f.get('.lp-button-subtle').emit('click');
    assert.equal(f.get('.lp-evaluation-access-toggle').disabled, false);
    assert.equal(f.get('.lp-evaluation-access-toggle').textContent, '恢复测试集隔离');
    assert.equal(evaluationAccessPosts(f).length, 1); f.controller.destroy();
  }
});

test('an evaluation access timeout is not resubmitted and a late response cannot overwrite manual refresh', async () => {
  const f = await fixture(null); await f.get('.lp-tab-evaluation').emit('click');
  let release, signal; f.api.changeEvaluationAccess = (_body, incoming) => { signal = incoming; return new Promise(resolve => { release = resolve; }); };
  const submitting = f.get('.lp-evaluation-access-toggle').emit('click'); await flush();
  await f.timer.tick(30000); await submitting;
  assert.equal(signal.aborted, true); assert.equal(f.get('.lp-evaluation-access-toggle').disabled, true);
  assert.match(f.get('.lp-evaluation-access-status').textContent, /结果待核对.*不会自动重发/);
  await f.timer.tick(60000); assert.equal(evaluationAccessPosts(f).length, 1);
  await f.controller.refresh();
  release({...f.api.evaluationSets, retrieval_enabled: true}); await flush();
  assert.equal(f.get('.lp-evaluation-access-toggle').textContent, '开放测试集检索');
  assert.equal(f.get('.lp-evaluation-access-toggle').getAttribute('aria-pressed'), 'false');
  f.controller.destroy(); assert.equal(f.timer.timers.size, 0);
});

test('evaluation access responses cannot switch tabs and destruction cancels an in-flight access request', async () => {
  const f = await fixture(null); await f.get('.lp-tab-evaluation').emit('click');
  let release; f.api.changeEvaluationAccess = body => new Promise(resolve => { release = () => { f.api.evaluationSets.retrieval_enabled = body.enabled; resolve(f.api.evaluationSets); }; });
  const submitting = f.get('.lp-evaluation-access-toggle').emit('click'); await flush();
  await f.get('.lp-tab-current').emit('click'); release(); await submitting;
  assert.equal(f.get('.lp-tab-current').getAttribute('aria-selected'), 'true'); assert.equal(f.get('.lp-evaluation-summary').hidden, true);
  await f.get('.lp-tab-evaluation').emit('click'); assert.equal(f.get('.lp-evaluation-access-toggle').textContent, '恢复测试集隔离');
  let signal; f.api.changeEvaluationAccess = (_body, incoming) => { signal = incoming; return new Promise(() => {}); };
  const pending = f.get('.lp-evaluation-access-toggle').emit('click'); await flush(); f.controller.destroy(); await pending;
  assert.equal(signal.aborted, true); assert.equal(f.target.children.length, 0); assert.equal(f.timer.timers.size, 0);
});

test('returning to evaluation during a pending access change ignores an old library read and releases controls', async () => {
  const f = await fixture(null); await f.get('.lp-tab-evaluation').emit('click');
  let releaseAccess, releaseLibraries;
  f.api.changeEvaluationAccess = body => new Promise(resolve => { releaseAccess = () => resolve({...f.api.evaluationSets, retrieval_enabled: body.enabled}); });
  const submitting = f.get('.lp-evaluation-access-toggle').emit('click'); await flush();
  f.api.getLibraries = () => new Promise(resolve => { releaseLibraries = () => resolve(f.api.snapshot); });
  const changing = f.get('.lp-tab-current').emit('click'); await flush();
  await f.get('.lp-tab-evaluation').emit('click'); releaseAccess(); await submitting;
  assert.equal(f.get('.lp-evaluation-access-toggle').disabled, false);
  assert.equal(f.get('.lp-button-subtle').disabled, false);
  assert.equal(f.get('.lp-evaluation-access-toggle').textContent, '恢复测试集隔离');
  releaseLibraries(); await changing;
  assert.equal(f.get('.lp-tab-evaluation').getAttribute('aria-selected'), 'true');
  assert.equal(f.get('.lp-evaluation-access-toggle').disabled, false); f.controller.destroy();
});

test('embedding is globally off even with ready legacy vectors; keyword works without any weights and native is hidden', async () => {
  const f = await fixture(null);
  f.api.snapshot.indexes.modes = {native: {ready: true}, qmd_keyword: {ready: true}, qmd_vector: {ready: true}};
  await f.controller.refresh(true);
  assert.equal(f.get('.lp-embedding-status').textContent, 'QMD embedding：未启用');
  assert.match(f.get('.lp-embedding-hint').textContent, /QMD 关键词检索无需权重/);
  const vector = f.get('.lp-vector-choice').querySelector('input'), form = f.get('.lp-search-form'), mode = form.querySelector('select'), query = form.querySelector('input');
  assert.equal(vector.disabled, true); assert.equal(vector.checked, false);
  assert.equal(mode.options.find(option => option.value === 'qmd_vector').disabled, true);
  f.api.search = async () => ({results: []}); query.value = 'heat'; mode.value = 'qmd_keyword'; await mode.emit('change'); await form.emit('submit');
  assert.ok(f.calls.some(call => call.url.startsWith('/api/search?') && call.url.includes('mode=qmd_keyword')));
  const searches = f.calls.filter(call => call.url.startsWith('/api/search?')).length;
  mode.value = 'qmd_vector'; await mode.emit('change'); await form.emit('submit');
  assert.equal(f.calls.filter(call => call.url.startsWith('/api/search?')).length, searches);
  assert.match(f.get('.lp-search-status').textContent, /先在 QMD 控制中手动启用 embedding/);
  vector.checked = true; await vector.emit('change'); assert.equal(vector.checked, false); assert.equal(embeddingPosts(f).length, 0);
  vector.checked = true; await f.get('.lp-build-form').emit('submit');
  assert.equal(f.calls.some(call => call.url.endsWith('/build')), false); assert.match(f.get('.lp-message').textContent, /先在 QMD 控制中手动启用 embedding/);
  vector.checked = false; f.get('.lp-mode-choices').querySelectorAll('input').find(check => check.value === 'qmd_keyword').checked = true;
  await f.get('.lp-build-form').emit('submit');
  assert.deepEqual(JSON.parse(f.calls.find(call => call.url.endsWith('/build')).settings.body).modes, ['qmd_keyword']);
  assert.equal(embeddingPosts(f).length, 0); assert.equal(f.calls.some(call => /download|model-info|model-status/.test(call.url)), false); f.controller.destroy();
});

test('global embedding setup is visible before any library exists and cancelling a draft saves nothing', async () => {
  const f = await fixture(null); f.api.snapshot.libraries = []; await f.controller.refresh(true);
  const before = f.calls.length; await f.get('.lp-embedding-configure').emit('click');
  assert.equal(f.get('.lp-embedding-dialog').open, true); assert.match(f.get('.lp-embedding-dialog').textContent, /全局控制/);
  assert.match(f.get('.lp-embedding-dialog').textContent, /不会自动下载模型/); assert.match(f.get('.lp-embedding-dialog').textContent, /关闭功能保留已有向量索引与模型文件/);
  const toggle = f.get('.lp-embedding-enable'), path = f.get('.lp-embedding-path');
  assert.equal(toggle.checked, false); assert.equal(path.disabled, true);
  toggle.checked = true; await toggle.emit('change'); path.value = 'D:/models/local.gguf';
  assert.equal(f.get('.lp-embedding-save').textContent, '确认启用并保存'); assert.equal(f.calls.length, before);
  await f.get('.lp-embedding-cancel').emit('click'); assert.equal(embeddingPosts(f).length, 0);
  assert.equal(f.get('.lp-embedding-dialog').open, false); assert.equal(f.get('.lp-embedding-status').textContent, 'QMD embedding：未启用');
  assert.equal(f.get('.lp-cleanup-vectors').disabled, false); f.controller.destroy();
});

test('embedding enable validates a local GGUF path and waits for one explicit confirmed save without starting work', async () => {
  const f = await fixture(null); await f.get('.lp-embedding-configure').emit('click');
  const toggle = f.get('.lp-embedding-enable'), path = f.get('.lp-embedding-path'), form = f.get('.lp-embedding-form');
  toggle.checked = true; await toggle.emit('change');
  for (const value of ['', 'models/local.gguf', 'https://example.com/local.gguf', 'D:/models', 'D:/models/local.bin', 'D:/models/bad\n.gguf']) { path.value = value; await form.emit('submit'); assert.equal(embeddingPosts(f).length, 0); }
  assert.match(f.get('.lp-embedding-dialog-status').textContent, /GGUF.*绝对路径/);
  const index = f.get('.lp-index-card'); let release; const save = f.api.saveEmbeddingSettings;
  f.api.saveEmbeddingSettings = async (...args) => { await new Promise(resolve => { release = resolve; }); return save(...args); };
  path.value = 'D:\\models\\embedding.gguf'; const saving = form.emit('submit'); await flush();
  await form.emit('submit'); await f.get('.lp-embedding-dialog').emit('cancel');
  assert.equal(f.get('.lp-embedding-dialog').open, true); assert.equal(f.get('.lp-embedding-cancel').disabled, true);
  assert.equal(embeddingPosts(f).length, 1); assert.deepEqual(JSON.parse(embeddingPosts(f)[0].settings.body), {enabled: true, model_path: 'D:\\models\\embedding.gguf'});
  assert.equal(f.get('.lp-embedding-status').textContent, 'QMD embedding：未启用'); release(); await saving;
  assert.equal(f.get('.lp-embedding-status').textContent, 'QMD embedding：已启用'); assert.equal(f.get('.lp-embedding-dialog').open, false);
  const vector = f.get('.lp-vector-choice').querySelector('input'); assert.equal(vector.disabled, false); assert.equal(vector.checked, false);
  assert.equal(f.get('.lp-index-card'), index); assert.equal(f.calls.some(call => call.url.endsWith('/build') || call.url.startsWith('/api/search?')), false);
  f.controller.destroy();
});

test('turning embedding off aborts stale vector queries, unchecks vector builds and preserves configured files', async () => {
  const f = await fixture(null); f.api.embeddingSettings = {enabled: true, configured: true, model_path: 'D:/models/existing.gguf'};
  f.api.snapshot.indexes.modes.qmd_vector.ready = true; await f.controller.refresh();
  const vector = f.get('.lp-vector-choice').querySelector('input'); vector.checked = true; await vector.emit('change');
  const form = f.get('.lp-search-form'), mode = form.querySelector('select'); mode.value = 'qmd_vector'; await mode.emit('change'); form.querySelector('input').value = 'query';
  let signal, release; f.api.search = (_url, incoming) => { signal = incoming; return new Promise(resolve => { release = resolve; }); };
  const searching = form.emit('submit'); await flush();
  await f.get('.lp-embedding-configure').emit('click'); const toggle = f.get('.lp-embedding-enable'); toggle.checked = false; await toggle.emit('change');
  await f.get('.lp-embedding-form').emit('submit'); await searching;
  assert.equal(signal.aborted, true); release({results: [{id: 'late', title: 'stale vector hit'}]}); await flush();
  assert.doesNotMatch(f.get('.lp-results').textContent, /stale vector hit/);
  assert.deepEqual(JSON.parse(embeddingPosts(f)[0].settings.body), {enabled: false}); assert.equal(f.api.embeddingSettings.model_path, 'D:/models/existing.gguf');
  assert.equal(f.api.snapshot.indexes.modes.qmd_vector.ready, true, 'disabling policy does not delete existing vectors');
  assert.equal(vector.disabled, true); assert.equal(vector.checked, false); assert.equal(mode.options.find(option => option.value === 'qmd_vector').disabled, true);
  assert.match(f.get('.lp-search-status').textContent, /先在 QMD 控制中手动启用 embedding/); await form.emit('submit');
  assert.equal(f.calls.filter(call => call.url.startsWith('/api/search?')).length, 1);
  await f.controller.refresh(true); assert.equal(f.get('.lp-vector-choice').querySelector('input').checked, false);
  assert.match(f.get('.lp-message').textContent, /已有向量索引及模型文件保留/); assert.equal(cleanupPosts(f).length, 0); f.controller.destroy();
});

test('embedding settings are read on mount and explicit refresh, never on background job polls', async () => {
  const f = await fixture(); const reads = () => f.calls.filter(call => call.url === embeddingPath && !call.settings.method).length;
  assert.equal(reads(), 1); await f.timer.tick(10000); assert.equal(reads(), 1);
  await f.controller.refresh(true); assert.equal(reads(), 1);
  await f.controller.refresh(); assert.equal(reads(), 2); assert.equal(embeddingPosts(f).length, 0); f.controller.destroy();
});

test('unsupported or malformed embedding settings fail closed while keyword retrieval remains usable', async () => {
  const f = await fixture(null); f.api.snapshot.indexes.modes.qmd_keyword.ready = true; f.api.snapshot.indexes.modes.qmd_vector.ready = true;
  f.api.getEmbeddingSettings = async () => { throw Error('旧版服务不支持该设置'); }; await f.controller.refresh();
  assert.match(f.get('.lp-embedding-status').textContent, /未启用.*状态未确认/); assert.equal(f.get('.lp-vector-choice').querySelector('input').disabled, true);
  const form = f.get('.lp-search-form'), mode = form.querySelector('select'); f.api.search = async () => ({results: []});
  mode.value = 'qmd_keyword'; await mode.emit('change'); form.querySelector('input').value = 'keyword'; await form.emit('submit');
  assert.ok(f.calls.some(call => call.url.includes('mode=qmd_keyword')));
  await f.get('.lp-embedding-configure').emit('click'); f.get('.lp-embedding-enable').checked = true; await f.get('.lp-embedding-enable').emit('change'); f.get('.lp-embedding-path').value = 'D:/models/missing.gguf';
  f.api.saveEmbeddingSettings = async () => ({enabled: true, model_path: 'D:/models/missing.gguf'});
  await f.get('.lp-embedding-form').emit('submit'); assert.match(f.get('.lp-embedding-dialog-status').textContent, /结果未确认/);
  assert.equal(f.get('.lp-vector-choice').querySelector('input').disabled, true); assert.equal(f.get('.lp-embedding-dialog').open, true);
  f.controller.destroy();
});

test('embedding save timeout stays uncertain without retrying or enabling vector operations', async () => {
  const f = await fixture(null); await f.get('.lp-embedding-configure').emit('click');
  f.get('.lp-embedding-enable').checked = true; await f.get('.lp-embedding-enable').emit('change'); f.get('.lp-embedding-path').value = 'D:/models/local.gguf';
  let signal; f.api.saveEmbeddingSettings = (_body, incoming) => { signal = incoming; return new Promise(() => {}); };
  const saving = f.get('.lp-embedding-form').emit('submit'); await flush(); await f.timer.tick(30000); await saving;
  assert.equal(signal.aborted, true); assert.match(f.get('.lp-embedding-dialog-status').textContent, /结果未确认.*结果待核对.*不会自动重发/);
  assert.equal(f.get('.lp-embedding-save').disabled, false); assert.equal(f.get('.lp-vector-choice').querySelector('input').disabled, true);
  await f.timer.tick(10000); assert.equal(embeddingPosts(f).length, 1); f.controller.destroy(); assert.equal(f.timer.timers.size, 0);
});

test('destroying settings reads or saves aborts them and late replies cannot enable a removed panel', async () => {
  for (const phase of ['read', 'save']) {
    const f = await fixture(null); let release, signal, pending;
    const wait = incoming => { signal = incoming; return new Promise(resolve => { release = resolve; }); };
    if (phase === 'read') { f.api.getEmbeddingSettings = wait; pending = f.controller.refresh(); }
    else {
      await f.get('.lp-embedding-configure').emit('click'); f.get('.lp-embedding-enable').checked = true; await f.get('.lp-embedding-enable').emit('change'); f.get('.lp-embedding-path').value = 'D:/models/local.gguf';
      f.api.saveEmbeddingSettings = (_body, incoming) => wait(incoming); pending = f.get('.lp-embedding-form').emit('submit');
    }
    await flush(); f.controller.destroy(); await pending; assert.equal(signal.aborted, true);
    release({enabled: true, configured: true, model_path: 'D:/models/local.gguf'}); await flush();
    assert.equal(f.target.children.length, 0); assert.equal(f.timer.timers.size, 0);
  }
});

test('global unfinished-vector cleanup is visible without libraries and scans only on explicit request', async () => {
  const f = await fixture(null);
  const entry = f.get('.lp-cleanup-entry'), control = f.get('.lp-cleanup-vectors');
  assert.equal(control.textContent, '清理未完成向量索引'); assert.match(entry.textContent, /全局.*当前及历史/);
  f.api.snapshot.libraries = []; await f.controller.refresh(true);
  await f.timer.tick(10000);
  assert.equal(control.disabled, false); assert.equal(f.calls.some(call => call.url === cleanupPath), false);
  await control.emit('click');
  const dialog = f.get('.lp-cleanup-dialog');
  assert.equal(dialog.open, true); assert.match(dialog.textContent, /所有资料库.*当前及历史/);
  assert.match(dialog.textContent, /不受所选资料库限制/); assert.match(dialog.textContent, /尚未发布为可用索引的向量文件/);
  assert.match(dialog.textContent, /不会清理其他历史索引或解析产物/);
  for (const kept of ['QMD 关键词索引', '已发布 / 已就绪向量索引', '原始模型与手册', '已入库内容', 'GGUF 模型']) assert.ok(dialog.textContent.includes(kept));
  assert.match(f.get('.lp-cleanup-summary').textContent, /3 个文件，14.0 MiB.*2 个索引代次/);
  assert.match(f.get('.lp-cleanup-status').textContent, /1 项保留.*无法安全归属/s);
  assert.equal(cleanupPosts(f).length, 0);
  const count = f.calls.length; await f.timer.tick(10000); assert.equal(f.calls.length, count);
  await f.get('.lp-cleanup-cancel').emit('click');
  assert.equal(dialog.open, false); assert.equal(f.target.ownerDocument.activeElement, control);
  assert.equal(cleanupPosts(f).length, 0); f.controller.destroy();
});

test('preview, refresh-free cancellation and library selection preserve existing controls and expanded evidence', async () => {
  const f = await fixture(running({status: 'completed', stage: 'finished', indexes: {generation: 'history'}}));
  f.controller.selectLibrary('models'); await flush();
  const details = f.get('.lp-job-index-details'); details.open = true; await details.emit('toggle');
  const pre = details.querySelector('pre'), form = f.get('.lp-search-form'), query = form.querySelector('input'); query.value = 'keep this query';
  const index = f.get('.lp-index-card'), job = f.get('.lp-job'), count = f.calls.length;
  await f.get('.lp-cleanup-vectors').emit('click');
  assert.equal(f.get('.lp-index-card'), index); assert.equal(f.get('.lp-job'), job);
  assert.equal(details.open, true); assert.equal(details.querySelector('pre'), pre); assert.equal(query.value, 'keep this query');
  await f.get('.lp-cleanup-dialog').emit('cancel');
  assert.equal(f.get('.lp-cleanup-dialog').open, false); assert.equal(f.calls.length, count + 1);
  assert.equal(f.get('.lp-index-card'), index); assert.equal(details.querySelector('pre'), pre);
  f.controller.selectLibrary('idle'); await flush(); assert.equal(f.calls.filter(call => call.url === cleanupPath).length, 1);
  f.controller.destroy();
});

test('confirm posts the exact global preview token once, waits for success and refreshes index status', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  let release; const cleaned = f.api.cleanupVectors;
  f.api.cleanupVectors = async (...args) => { await new Promise(resolve => { release = resolve; }); f.api.snapshot.indexes.generation = 'after-cleanup'; f.api.snapshot.indexes.modes.qmd_vector.state = 'cleaned'; return cleaned(...args); };
  await f.get('.lp-cleanup-vectors').emit('click');
  const beforeRefresh = f.calls.filter(call => call.url.startsWith('/api/libraries?')).length;
  const pending = f.get('.lp-cleanup-confirm').emit('click'); await flush();
  await f.get('.lp-cleanup-confirm').emit('click'); await f.get('.lp-cleanup-dialog').emit('cancel');
  assert.equal(cleanupPosts(f).length, 1); assert.deepEqual(JSON.parse(cleanupPosts(f)[0].settings.body), {confirmed: true, token: 'preview-current-and-history'});
  assert.equal(f.get('.lp-cleanup-cancel').disabled, true); assert.equal(f.get('.lp-cleanup-dialog').open, true);
  assert.doesNotMatch(f.get('.lp-message').textContent, /已清理/);
  release(); await pending;
  assert.equal(f.get('.lp-cleanup-dialog').open, false); assert.match(f.get('.lp-message').textContent, /已清理：3 个文件，14.0 MiB/);
  assert.ok(f.calls.filter(call => call.url.startsWith('/api/libraries?')).length > beforeRefresh);
  assert.match(f.get('.lp-content').textContent, /after-cleanup/);
  assert.match(f.get('.lp-content').textContent, /已清理/);
  assert.equal(f.api.snapshot.libraries.length, 2); assert.equal(f.api.snapshot.libraries[1].enabled, undefined);
  assert.equal(cleanupPosts(f).length, 1); f.controller.destroy();
});

test('any active global build disables cleanup even when a different library is selected', async () => {
  const f = await fixture(); f.controller.selectLibrary('idle'); await flush();
  assert.equal(f.get('.lp-cleanup-vectors').disabled, true);
  await f.get('.lp-cleanup-vectors').emit('click');
  assert.equal(f.get('.lp-cleanup-dialog').open, false); assert.equal(f.calls.some(call => call.url === cleanupPath), false);
  f.controller.destroy();
  const g = await fixture(null); g.api.snapshot.indexes.modes.qmd_vector.state = 'building'; await g.controller.refresh(true);
  assert.equal(g.get('.lp-cleanup-vectors').disabled, true); g.controller.destroy();
});

test('empty, busy and malformed cleanup previews cannot authorize any mutation', async () => {
  for (const override of [{file_count: 0, generation_count: 0, bytes: 0, can_clean: false}, {building: true, can_clean: false}, {preview_token: ''}, {file_count: null}, {can_clean: undefined}, {generation_count: 0}]) {
    const f = await fixture(null), original = f.api.previewCleanup;
    f.api.previewCleanup = async () => ({...await original(), ...override});
    await f.get('.lp-cleanup-vectors').emit('click');
    assert.equal(f.get('.lp-cleanup-confirm').disabled, true, JSON.stringify(override));
    await f.get('.lp-cleanup-confirm').emit('click'); assert.equal(cleanupPosts(f).length, 0);
    await f.get('.lp-cleanup-cancel').emit('click'); f.controller.destroy();
  }
});

test('cancelling an in-flight preview aborts it, and its late result cannot replace a newer token', async () => {
  const f = await fixture(null); let release, signal; const original = f.api.previewCleanup;
  f.api.previewCleanup = incoming => { signal = incoming; return new Promise(resolve => { release = resolve; }); };
  const first = f.get('.lp-cleanup-vectors').emit('click'); await flush();
  assert.equal(f.get('.lp-cleanup-confirm').disabled, true);
  await f.get('.lp-cleanup-cancel').emit('click'); await first;
  assert.equal(signal.aborted, true); assert.equal(cleanupPosts(f).length, 0);
  f.api.previewCleanup = async () => ({...await original(), file_count: 7, preview_token: 'new-preview'});
  await f.get('.lp-cleanup-vectors').emit('click');
  release({...await original(), file_count: 99, preview_token: 'stale-preview'}); await flush();
  assert.match(f.get('.lp-cleanup-summary').textContent, /7 个文件/);
  await f.get('.lp-cleanup-confirm').emit('click');
  assert.equal(JSON.parse(cleanupPosts(f)[0].settings.body).token, 'new-preview'); f.controller.destroy();
});

test('a build detected during an open preview invalidates confirmation until an explicit recheck', async () => {
  const f = await fixture(null); await f.get('.lp-cleanup-vectors').emit('click');
  const dialog = f.get('.lp-cleanup-dialog'), summary = f.get('.lp-cleanup-summary');
  f.api.snapshot.jobs = [running()]; await f.controller.refresh(true);
  assert.equal(f.get('.lp-cleanup-confirm').disabled, true); assert.match(f.get('.lp-cleanup-status').textContent, /预览已失效/);
  f.api.snapshot.jobs = [running({status: 'completed', stage: 'finished', updated_at: new Date(epoch + 1000).toISOString()})]; await f.controller.refresh(true);
  assert.equal(f.get('.lp-cleanup-confirm').disabled, true); assert.equal(f.get('.lp-cleanup-retry').disabled, false);
  assert.equal(f.get('.lp-cleanup-dialog'), dialog); assert.equal(f.get('.lp-cleanup-summary'), summary);
  assert.equal(f.calls.filter(call => call.url === cleanupPath).length, 1);
  await f.get('.lp-cleanup-retry').emit('click'); assert.equal(f.get('.lp-cleanup-confirm').disabled, false);
  assert.equal(f.calls.filter(call => call.url === cleanupPath).length, 2); f.controller.destroy();
});

test('preview failure and timeout release controls without a deletion or automatic scan retry', async () => {
  const f = await fixture(null); f.api.previewCleanup = async () => { throw Error('预览不可用'); };
  await f.get('.lp-cleanup-vectors').emit('click');
  assert.match(f.get('.lp-cleanup-status').textContent, /尚未清理.*预览不可用/); assert.equal(f.get('.lp-cleanup-retry').disabled, false);
  let signal; f.api.previewCleanup = incoming => { signal = incoming; return new Promise(() => {}); };
  const pending = f.get('.lp-cleanup-retry').emit('click'); await flush(); await f.timer.tick(15000); await pending;
  assert.equal(signal.aborted, true); assert.match(f.get('.lp-cleanup-status').textContent, /超时/);
  assert.equal(f.get('.lp-cleanup-confirm').disabled, true); assert.equal(f.get('.lp-cleanup-retry').disabled, false);
  assert.equal(f.calls.filter(call => call.url === cleanupPath).length, 2); assert.equal(cleanupPosts(f).length, 0);
  f.controller.destroy(); assert.equal(f.timer.timers.size, 0);
});

test('partial or contradictory cleanup results are reported honestly and require a fresh preview', async () => {
  for (const result of [{status: 'partial', cleaned: false, warnings: ['部分文件清理失败']}, {status: 'cleaned', cleaned: false}, {status: 'cleaned'}, {status: 'unknown'}]) {
    const f = await fixture(null); f.api.cleanupVectors = async () => result;
    await f.get('.lp-cleanup-vectors').emit('click'); const beforeRefresh = f.calls.filter(call => call.url.startsWith('/api/libraries?')).length;
    await f.get('.lp-cleanup-confirm').emit('click');
    assert.match(f.get('.lp-cleanup-status').textContent, /清理结果未确认.*尚未确认清理完成/s);
    assert.doesNotMatch(f.get('.lp-message').textContent, /未完成向量索引已清理/);
    assert.equal(f.get('.lp-cleanup-confirm').disabled, true); assert.equal(f.get('.lp-cleanup-retry').disabled, false);
    await f.get('.lp-cleanup-confirm').emit('click'); assert.equal(cleanupPosts(f).length, 1);
    assert.equal(f.calls.filter(call => call.url.startsWith('/api/libraries?')).length, beforeRefresh);
    f.controller.destroy();
  }
});

test('cleanup timeout remains uncertain, releases the dialog and never replays the mutation', async () => {
  const f = await fixture(null); let signal; f.api.cleanupVectors = (_body, incoming) => { signal = incoming; return new Promise(() => {}); };
  await f.get('.lp-cleanup-vectors').emit('click');
  const pending = f.get('.lp-cleanup-confirm').emit('click'); await flush(); await f.timer.tick(30000); await pending;
  assert.equal(signal.aborted, true); assert.match(f.get('.lp-cleanup-status').textContent, /清理结果未确认.*结果待核对.*不会自动重发/s);
  assert.equal(f.get('.lp-cleanup-cancel').disabled, false); assert.equal(f.get('.lp-cleanup-confirm').disabled, true);
  assert.equal(cleanupPosts(f).length, 1); await f.timer.tick(10000); assert.equal(cleanupPosts(f).length, 1);
  await f.get('.lp-cleanup-cancel').emit('click'); assert.equal(f.get('.lp-cleanup-vectors').disabled, false);
  f.controller.destroy(); assert.equal(f.timer.timers.size, 0);
});

test('destroying pending preview or cleanup cancels requests and ignores late results', async () => {
  for (const phase of ['preview', 'cleanup']) {
    const f = await fixture(null); let release, signal;
    const wait = incoming => { signal = incoming; return new Promise(resolve => { release = resolve; }); };
    if (phase === 'preview') f.api.previewCleanup = wait;
    else f.api.cleanupVectors = (_body, incoming) => wait(incoming);
    let pending = f.get('.lp-cleanup-vectors').emit('click');
    if (phase === 'cleanup') { await pending; pending = f.get('.lp-cleanup-confirm').emit('click'); }
    await flush(); f.controller.destroy(); await pending;
    assert.equal(signal.aborted, true); assert.equal(f.target.children.length, 0); assert.equal(f.timer.timers.size, 0);
    release({status: 'cleaned', cleaned: true, file_count: 123}); await flush();
    assert.equal(cleanupPosts(f).length, phase === 'cleanup' ? 1 : 0); assert.equal(f.target.children.length, 0); assert.equal(f.timer.timers.size, 0);
  }
});

test('each registered root offers rebind and cancelling edits performs no request', async () => {
  const f = await relocatable();
  const controls = f.target.querySelectorAll('.lp-rebind-open');
  assert.equal(controls.length, 2);
  await controls[1].emit('click');
  assert.equal(f.get('.lp-rebind-dialog').open, true);
  assert.match(f.get('.lp-rebind-identity').textContent, /docs/);
  f.get('.lp-rebind-form').querySelector('input').value = 'E:/new/docs';
  await f.get('.lp-rebind-cancel').emit('click');
  assert.equal(f.get('.lp-rebind-dialog').open, false);
  assert.ok(!f.calls.some(call => call.url.endsWith('/rebind')));
  assert.equal(f.api.snapshot.libraries[1].roots[1].path, 'D:/old/docs');
  f.controller.destroy();
});

test('rebind targets the exact root, preserves disabled state and refreshes model enumeration without rebuilding indexes', async () => {
  const f = await relocatable();
  f.api.snapshot.libraries[1].enabled = false;
  await f.controller.refresh();
  const before = f.calls.filter(call => call.url.endsWith('/models')).length;
  await f.get('.lp-rebind-open').emit('click');
  f.get('.lp-rebind-form').querySelector('input').value = 'E:/new/models';
  await f.get('.lp-rebind-form').emit('submit'); await flush();
  const sent = f.calls.find(call => call.url.endsWith('/rebind'));
  assert.equal(sent.url, '/api/libraries/models/rebind');
  assert.deepEqual(JSON.parse(sent.settings.body), {root_id: 'main', path: 'E:/new/models'});
  assert.equal(f.get('.lp-rebind-dialog').open, false);
  assert.ok(f.get('.lp-bindings').textContent.includes('E:/new/models'));
  assert.ok(f.get('.lp-bindings').textContent.includes('D:/old/docs'));
  assert.equal(f.api.snapshot.libraries[1].enabled, false);
  assert.equal(f.calls.filter(call => call.url.endsWith('/models')).length, before + 1);
  assert.match(f.get('.lp-message').textContent, /已校验 2 个源文件/);
  assert.ok(!f.calls.some(call => /\/(build|index|enable)$/.test(call.url)));
  f.controller.destroy();
});

test('a failed content check keeps the old path and entered replacement available for correction', async () => {
  const f = await relocatable();
  f.api.rebind = async () => { throw new Error('新目录文件内容不一致'); };
  await f.get('.lp-rebind-open').emit('click');
  const path = f.get('.lp-rebind-form').querySelector('input'); path.value = 'E:/wrong';
  await f.get('.lp-rebind-form').emit('submit');
  assert.equal(f.get('.lp-rebind-dialog').open, true);
  assert.match(f.get('.lp-rebind-status').textContent, /文件内容不一致/);
  assert.equal(path.value, 'E:/wrong');
  assert.equal(path.disabled, false);
  assert.ok(f.get('.lp-bindings').textContent.includes('D:/old/models'));
  f.controller.destroy();
});

test('rebind submits once, survives status refresh without losing edits, and disables close while validating', async () => {
  const f = await relocatable();
  let release;
  const original = f.api.rebind;
  f.api.rebind = async (...args) => { await new Promise(resolve => { release = resolve; }); return original(...args); };
  await f.get('.lp-rebind-open').emit('click');
  const path = f.get('.lp-rebind-form').querySelector('input'); path.value = 'E:/new/models';
  await f.controller.refresh(); assert.equal(path.value, 'E:/new/models');
  const pending = f.get('.lp-rebind-form').emit('submit'); await flush();
  await f.get('.lp-rebind-form').emit('submit');
  await f.get('.lp-rebind-dialog').emit('cancel');
  assert.equal(f.get('.lp-rebind-dialog').open, true);
  assert.equal(f.get('.lp-rebind-cancel').disabled, true);
  assert.equal(f.calls.filter(call => call.url.endsWith('/rebind')).length, 1);
  release(); await pending; f.controller.destroy();
});

test('rebind timeout is an uncertain result, releases the form, and never automatically replays the request', async () => {
  const f = await relocatable();
  f.api.rebind = () => new Promise(() => {});
  await f.get('.lp-rebind-open').emit('click');
  const pending = f.get('.lp-rebind-form').emit('submit'); await flush();
  await f.timer.tick(180001); await pending;
  assert.match(f.get('.lp-rebind-status').textContent, /结果待核对/);
  assert.equal(f.get('.lp-rebind-submit').disabled, false);
  assert.equal(f.calls.filter(call => call.url.endsWith('/rebind')).length, 1);
  f.controller.destroy();
});

test('deleted or actively building libraries cannot rebind their roots', async () => {
  const f = await relocatable();
  f.api.snapshot.jobs = [running()]; await f.controller.refresh();
  assert.equal(f.get('.lp-rebind-open').disabled, true);
  await f.get('.lp-rebind-open').emit('click');
  assert.equal(f.get('.lp-rebind-dialog').open, false);
  f.api.snapshot.jobs = []; f.api.snapshot.libraries[1].deleted_at = new Date(epoch).toISOString();
  await f.get('.lp-tab-deleted').emit('click');
  f.controller.selectLibrary('models');
  assert.equal(f.get('.lp-rebind-open').disabled, true);
  f.controller.destroy();
});

test('refresh restores the active library and shows stage, file results and a ticking elapsed time', async () => {
  const f = await fixture();
  assert.equal(f.get('.lp-selected-heading').textContent.includes('模型试点'), true);
  const card = f.get('.lp-live-progress'), bar = f.get('.lp-stage-progress');
  assert.equal(card.getAttribute('data-job-id'), jobId);
  assert.equal(card.getAttribute('aria-busy'), 'true');
  assert.equal(bar.value, 2); assert.equal(bar.max, 4);
  assert.equal(f.get('.lp-progress-file').textContent.includes('D:/cases/bracket.mph'), true);
  assert.equal(f.get('.lp-progress-counts').textContent, '成功 1 · 部分 1 · 失败 0');
  const before = f.get('.lp-progress-elapsed').textContent; await f.timer.tick(1000);
  assert.notEqual(f.get('.lp-progress-elapsed').textContent, before);
  const content = f.get('.lp-content'); assert.ok(content.children.indexOf(card) < content.children.indexOf(f.get('.lp-indexes')));
  f.controller.destroy(); assert.equal(f.timer.timers.size, 0);
});

test('indexing cannot reuse completed file attempts as a fake overall 100 percent', async () => {
  const f = await fixture(running({stage: 'indexing_vector', completed: 4, total: 4, progress: {completed: null, total: null, unit: 'chunks', current_file: null, detail: '建立本地向量索引'}}));
  assert.equal(f.get('.lp-progress-stage').textContent, '构建向量索引');
  assert.equal(f.get('.lp-stage-progress').hasAttribute('value'), false);
  assert.equal(f.get('.lp-progress-amount').textContent.includes('总量未报告'), true);
  assert.equal(f.get('.lp-progress-files').textContent, '文件已尝试 4 / 4');
  assert.equal(f.get('.lp-live-progress').textContent.includes('100%'), false);
  f.controller.destroy();
});

test('only real stage totals make a determinate index bar; legacy extraction is still readable', async () => {
  const index = await fixture(running({stage: 'indexing_keyword', progress: {completed: 3, total: 6, unit: 'objects', current_file: null, detail: ''}}));
  assert.equal(index.get('.lp-stage-progress').value, 3); assert.equal(index.get('.lp-stage-progress').max, 6);
  assert.equal(index.get('.lp-progress-amount').textContent, '本阶段 3 / 6 个对象'); index.controller.destroy();
  const legacy = await fixture(running({progress: undefined, counts: undefined, items: [{status: 'completed', coverage: {deep_api: {status: 'preview_only'}}}], failed: [{path: 'bad.mph', error: 'unreadable'}]}));
  assert.equal(legacy.get('.lp-stage-progress').value, 2);
  assert.equal(legacy.get('.lp-progress-counts').textContent, '成功 0 · 部分 1 · 失败 1'); legacy.controller.destroy();
});

test('an unknown completed count or inconsistent counters never create an invented ratio', async () => {
  const unknown = await fixture(running({stage: 'scanning', progress: {completed: null, total: 12, unit: 'files'}}));
  assert.equal(unknown.get('.lp-stage-progress').hasAttribute('value'), false);
  assert.equal(unknown.get('.lp-progress-amount').textContent, '本阶段完成数待报告 · 总计 12 个文件'); unknown.controller.destroy();
  const invalid = await fixture(running({progress: {completed: 5, total: 4, unit: 'files'}}));
  assert.equal(invalid.get('.lp-stage-progress').hasAttribute('value'), false);
  assert.equal(invalid.get('.lp-progress-amount').textContent.includes('计数需核对'), true); invalid.controller.destroy();
});

test('poll follows stages, preserves a terminal partial result against stale refresh and stops timers', async () => {
  const original = running(), f = await fixture(original);
  f.api.getJob = async () => running({stage: 'indexing_keyword', progress: {completed: null, total: null, unit: 'indexes', current_file: null, detail: ''}, updated_at: new Date(epoch + 1000).toISOString()});
  await f.timer.tick(2500); assert.equal(f.get('.lp-progress-stage').textContent, '构建关键词索引');
  f.api.getJob = async () => running({status: 'partial', stage: 'finished', finished_at: new Date(epoch + 5000).toISOString(), elapsed_seconds: 128, progress: {completed: null, total: null, unit: 'indexes'}, failed_indexes: ['qmd_vector'], updated_at: new Date(epoch + 5000).toISOString()});
  // The list endpoint can be an older snapshot than the individual job response.
  f.api.snapshot.jobs = [original]; await f.timer.tick(2500);
  assert.equal(f.get('.lp-live-progress').getAttribute('data-status'), 'partial');
  assert.equal(f.get('.lp-progress-warning').textContent.includes('QMD 向量索引'), true);
  assert.equal(f.get('.lp-stage-progress').hidden, true);
  const reads = f.calls.length, elapsed = f.get('.lp-progress-elapsed').textContent;
  assert.equal(f.timer.timers.size, 0); await f.timer.tick(20000);
  assert.equal(f.calls.length, reads); assert.equal(f.get('.lp-progress-elapsed').textContent, elapsed); f.controller.destroy();
});

test('poll failures keep the last real progress, retry reads and do not resubmit builds', async () => {
  const f = await fixture(); f.api.getJob = async () => { throw Error('service temporarily offline'); };
  await f.timer.tick(2500);
  assert.equal(f.get('.lp-live-progress').getAttribute('data-status'), 'running');
  assert.equal(f.get('.lp-progress-warning').textContent.includes('保留上次进度'), true);
  assert.equal(f.calls.some(call => call.settings.method === 'POST'), false);
  const completed = running({status: 'completed', stage: 'finished', finished_at: new Date(epoch + 5000).toISOString()});
  f.api.getJob = async () => completed; f.api.snapshot.jobs = [completed]; await f.timer.tick(2500);
  assert.equal(f.get('.lp-live-progress').getAttribute('data-status'), 'completed');
  assert.equal(f.get('.lp-progress-warning').hidden, true); assert.equal(f.timer.timers.size, 0); f.controller.destroy();
});

test('destroy aborts an in-flight read and late responses cannot restore removed UI', async () => {
  const f = await fixture(); let release, signal;
  f.api.getJob = (_id, value) => { signal = value; return new Promise(resolve => { release = resolve; }); };
  await f.timer.tick(2500); assert.ok(signal); f.controller.destroy(); assert.equal(signal.aborted, true);
  release(running()); await flush(); await f.timer.tick(10000);
  assert.equal(f.target.children.length, 0); assert.equal(f.timer.timers.size, 0);
});

test('switching libraries does not let an old job response replace the selected library', async () => {
  const f = await fixture(); let release;
  f.api.getJob = () => new Promise(resolve => { release = resolve; }); await f.timer.tick(2500);
  f.controller.selectLibrary('idle'); assert.equal(f.get('.lp-live-progress').getAttribute('data-job-id'), '');
  release(running({stage: 'indexing_vector'})); await flush();
  assert.equal(f.get('.lp-selected-heading').textContent.includes('其他资料库'), true);
  assert.equal(f.get('.lp-live-progress').getAttribute('data-job-id'), ''); f.controller.destroy();
});

test('submitting a build reveals the persistent card immediately and starts read-only polling', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  const modes = f.get('.lp-mode-choices').querySelectorAll('input');
  assert.deepEqual(modes.filter(input => input.checked).map(input => input.value), ['qmd_keyword']);
  assert.equal(modes.find(input => input.value === 'qmd_keyword').checked, true);
  assert.ok(!modes.some(input => input.value === 'native'));
  assert.equal(modes.find(input => input.value === 'qmd_vector').checked, false);
  assert.match(f.get('.lp-vector-help').textContent, /先在 QMD 控制中手动启用 embedding/);
  await f.get('.lp-build-form').emit('submit');
  assert.equal(f.get('.lp-live-progress').getAttribute('data-status'), 'queued');
  assert.equal(f.get('.lp-live-progress').scrolled, true);
  assert.equal(f.calls.filter(call => call.settings.method === 'POST').length, 1);
  const submitted = f.calls.find(call => call.settings.method === 'POST'); assert.deepEqual(JSON.parse(submitted.settings.body).modes, ['qmd_keyword']);
  assert.equal(f.get('.lp-stage-progress').hasAttribute('value'), false); f.controller.destroy();
});

const cancelPosts = f => f.calls.filter(call => call.settings.method === 'POST');
function assertOnlyCancel(f, id = jobId) { for (const call of cancelPosts(f)) { assert.equal(call.url, `/api/library-jobs/${id}/cancel`); assert.deepEqual(JSON.parse(call.settings.body), {}); } }

test('cancel coalesces duplicate clicks and restored cancel_requested stays pending until worker cleanup', async () => {
  const f = await fixture(); let release;
  f.api.cancelJob = () => new Promise(resolve => { release = resolve; });
  const first = f.get('.lp-cancel-build').emit('click'), second = f.get('.lp-cancel-build').emit('click'); await flush();
  assert.equal(cancelPosts(f).length, 1); assert.equal(f.get('.lp-cancel-build').disabled, true);
  assert.equal(f.get('.lp-cancel-build').textContent, '正在中断…');
  assert.equal(f.get('.lp-live-progress').getAttribute('data-status'), 'running');
  const requested = running({status: 'cancel_requested', cancel_requested_at: new Date(epoch).toISOString()});
  release(requested); await Promise.all([first, second]);
  assert.equal(f.get('.lp-live-progress').getAttribute('data-status'), 'cancel_requested');
  assert.equal(f.get('.lp-progress-announcement').textContent.includes('正在中断'), true);
  assert.equal(f.get('.lp-build-submit').disabled, true);
  assert.ok(f.timer.timers.size > 0); assertOnlyCancel(f); f.controller.destroy();
  const restored = await fixture(requested);
  assert.equal(restored.get('.lp-cancel-build').disabled, true); assert.equal(restored.get('.lp-cancel-build').textContent, '正在中断…');
  await restored.get('.lp-cancel-build').emit('click'); assert.equal(cancelPosts(restored).length, 0);
  assert.ok(restored.timer.timers.size > 0); restored.controller.destroy();
});

test('a failed cancel can be retried without claiming cancellation or submitting another build', async () => {
  const f = await fixture(); f.api.cancelJob = async () => { throw Error('connection lost'); };
  await f.get('.lp-cancel-build').emit('click');
  assert.equal(f.get('.lp-live-progress').getAttribute('data-status'), 'running');
  assert.equal(f.get('.lp-cancel-build').disabled, false); assert.equal(f.get('.lp-cancel-build').textContent, '重试中断');
  assert.equal(f.get('.lp-progress-warning').textContent.includes('中断请求未确认'), true);
  f.api.cancelJob = async () => running({status: 'cancel_requested'}); await f.get('.lp-cancel-build').emit('click');
  assert.equal(f.get('.lp-live-progress').getAttribute('data-status'), 'cancel_requested');
  assert.equal(f.get('.lp-progress-warning').hidden, true); assert.equal(cancelPosts(f).length, 2);
  assertOnlyCancel(f); f.controller.destroy();
});

test('late running polls cannot overwrite cancel_requested; only a terminal response stops polling', async () => {
  const f = await fixture(); let release;
  f.api.getJob = () => new Promise(resolve => { release = resolve; }); await f.timer.tick(2500);
  await f.get('.lp-cancel-build').emit('click');
  // A later timestamp does not make an old non-cancel-aware response authoritative.
  release(running({updated_at: new Date(epoch + 20000).toISOString()})); await flush();
  assert.equal(f.get('.lp-live-progress').getAttribute('data-status'), 'cancel_requested');
  assert.equal(f.get('.lp-cancel-build').disabled, true); assert.ok(f.timer.timers.size > 0);
  const elapsed = f.get('.lp-progress-elapsed').textContent; await f.timer.tick(1000);
  assert.notEqual(f.get('.lp-progress-elapsed').textContent, elapsed);
  f.api.getJob = async () => running({status: 'cancelled', stage: 'finished', elapsed_seconds: 130, finished_at: new Date(epoch + 7000).toISOString()});
  // The terminal refresh may still return an older running list snapshot.
  f.api.snapshot.jobs = [running()]; await f.timer.tick(1500);
  assert.equal(f.get('.lp-live-progress').getAttribute('data-status'), 'cancelled');
  assert.equal(f.get('.lp-cancel-build').hidden, true); assert.equal(f.timer.timers.size, 0);
  const reads = f.calls.length; await f.timer.tick(15000); assert.equal(f.calls.length, reads);
  assertOnlyCancel(f); f.controller.destroy();
});

test('a global reindex is visible in every covered library and shares one cancellation request', async () => {
  const global = running({kind: 'reindex', scope: 'all_libraries', library_id: null, library_ids: ['models', 'idle'], stage: 'indexing_vector', progress: {completed: null, total: null, unit: 'chunks'}, cancel_supported: true, cancellable: true});
  const f = await fixture(global); assert.equal(f.get('.lp-selected-heading').textContent.includes('模型试点'), true);
  assert.equal(f.get('.lp-progress-files').hidden, true); assert.equal(f.get('.lp-progress-counts').hidden, true);
  assert.equal(f.get('.lp-progress-cancel-help').textContent.includes('2 个资料库'), true);
  let release; f.api.cancelJob = () => new Promise(resolve => { release = resolve; });
  const first = f.get('.lp-cancel-build').emit('click'); f.controller.selectLibrary('idle');
  assert.equal(f.get('.lp-live-progress').getAttribute('data-job-id'), jobId);
  assert.equal(f.get('.lp-build-submit').disabled, true);
  assert.equal(f.get('.lp-cancel-build').disabled, true); await f.get('.lp-cancel-build').emit('click');
  assert.equal(cancelPosts(f).length, 1); release({...global, status: 'cancel_requested'}); await first;
  assert.equal(f.get('.lp-live-progress').getAttribute('data-status'), 'cancel_requested');
  assertOnlyCancel(f); f.controller.destroy();
});

test('explicitly unsupported legacy tasks and terminal tasks do not send cancellation requests', async () => {
  for (const capability of [{cancellable: false}, {cancel_supported: false}]) {
    const f = await fixture(running(capability)); assert.equal(f.get('.lp-cancel-build').disabled, true);
    await f.get('.lp-cancel-build').emit('click'); assert.equal(cancelPosts(f).length, 0); f.controller.destroy();
  }
  const finished = await fixture(running({status: 'completed', stage: 'finished'}));
  assert.equal(finished.get('.lp-cancel-build').hidden, true); await finished.get('.lp-cancel-build').emit('click');
  assert.equal(cancelPosts(finished).length, 0); assert.equal(finished.timer.timers.size, 0); finished.controller.destroy();
});

test('terminal idempotent cancel replies keep their true status instead of inventing cancelled', async () => {
  const f = await fixture(); f.api.cancelJob = async () => running({status: 'completed', stage: 'finished', finished_at: new Date(epoch).toISOString()});
  await f.get('.lp-cancel-build').emit('click');
  assert.equal(f.get('.lp-live-progress').getAttribute('data-status'), 'completed');
  assert.equal(f.get('.lp-cancel-build').hidden, true); assert.equal(f.timer.timers.size, 0);
  await f.get('.lp-cancel-build').emit('click'); assert.equal(cancelPosts(f).length, 1); assertOnlyCancel(f); f.controller.destroy();
});

test('destroy aborts an in-flight cancel and its late response cannot revive the panel', async () => {
  const f = await fixture(); let release, signal;
  f.api.cancelJob = (_id, value) => { signal = value; return new Promise(resolve => { release = resolve; }); };
  const cancellation = f.get('.lp-cancel-build').emit('click'); await flush(); f.controller.destroy();
  assert.equal(signal.aborted, true); release(running({status: 'cancel_requested'})); await cancellation;
  assert.equal(f.target.children.length, 0); assert.equal(f.timer.timers.size, 0); assertOnlyCancel(f);
});

test('libraries default enabled; closing and enabling are explicit operations with no builds', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  assert.match(f.get('.lp-library-list').textContent, /已启用/);
  await f.get('.lp-library-enable').emit('click');
  assert.equal(f.get('.lp-library-enable').textContent, '启用资料库');
  assert.equal(f.get('.lp-build-submit').disabled, true);
  assert.match(f.get('.lp-library-action-help').textContent, /不参与检索/);
  assert.equal(f.get('.lp-search-form').querySelector('button').disabled, true);
  await f.get('.lp-library-enable').emit('click');
  assert.equal(f.get('.lp-library-enable').textContent, '临时关闭');
  assert.equal(f.get('.lp-build-submit').disabled, false);
  assert.doesNotMatch(f.get('.lp-search-status').textContent, /已关闭|请先启用/);
  assert.match(f.get('.lp-search-status').textContent, /输入问题/);
  const posts = f.calls.filter(call => call.settings.method === 'POST');
  assert.deepEqual(posts.map(call => [call.url, JSON.parse(call.settings.body)]), [['/api/libraries/models/enable', {enabled: false}], ['/api/libraries/models/enable', {enabled: true}]]);
  f.controller.destroy();
});

test('confirmed enable toggle does not wait for global status or scan closed model roots', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  const before = f.calls.length;
  f.api.getLibraries = async () => { throw Error('Toggle must not request the complete library status'); };
  await f.get('.lp-library-enable').emit('click');
  assert.equal(f.get('.lp-library-enable').textContent, '启用资料库');
  assert.equal(f.get('.lp-library-enable').disabled, false);
  assert.deepEqual(f.calls.slice(before).map(call => call.url), ['/api/libraries/models/enable']);
  assert.match(f.get('.lp-case-summary').textContent, /已关闭/);
  f.controller.destroy();
});

test('current and deleted tabs are disjoint, include disabled libraries, and load trash only when requested', async () => {
  const f = await fixture(null);
  f.api.snapshot.libraries[1].enabled = false;
  f.api.snapshot.libraries.push({id: 'trash', name: '已删除样本', type: 'user-models', deleted_at: new Date(epoch).toISOString(), enabled: false, roots: []});
  await f.controller.refresh(true);
  const ids = () => f.target.querySelectorAll('.lp-library-card').map(item => item.getAttribute('data-library-id'));
  assert.deepEqual(ids(), ['idle', 'models']); assert.match(f.get('.lp-library-list').textContent, /已关闭/);
  assert.equal(f.get('.lp-tab-current').getAttribute('aria-selected'), 'true');
  assert.equal(f.target.querySelector('.lp-show-deleted'), null); assert.doesNotMatch(f.target.textContent, /显示已删除资料库/);
  assert.equal(f.calls.some(call => call.url.includes('include_deleted=1')), false);
  await f.get('.lp-tab-deleted').emit('click');
  assert.deepEqual(ids(), ['trash']); assert.equal(f.get('.lp-tab-deleted').getAttribute('aria-selected'), 'true');
  for (const selector of ['.lp-build', '.lp-indexes', '.lp-jobs', '.lp-search-form', '.lp-case-selection', '.lp-extraction']) assert.equal(f.get(selector).hidden, true, selector);
  assert.equal(f.get('.lp-library-enable').hidden, true); assert.equal(f.get('.lp-library-delete').hidden, true);
  assert.equal(f.get('.lp-library-restore').hidden, false); assert.equal(f.get('.lp-library-purge').hidden, false);
  assert.equal(f.calls.some(call => call.url === '/api/libraries/trash/models'), false);
  assert.equal(f.calls.filter(call => call.url.includes('include_deleted=1')).length, 1);
  await f.get('.lp-tab-deleted').emit('keydown', {key: 'ArrowLeft'}); await flush();
  assert.deepEqual(ids(), ['idle', 'models']); assert.equal(f.target.ownerDocument.activeElement, f.get('.lp-tab-current'));
  assert.equal(f.calls.at(-1).url, '/api/libraries?view=summary'); f.controller.destroy();
});

test('changing tabs aborts model, search and source reads, clears their contents and rejects late replies', async () => {
  const f = await fixture(null); let modelSignal, searchSignal, sourceSignal, releaseModels, releaseSearch, releaseSource;
  f.api.models = (_id, signal) => { modelSignal = signal; return new Promise(resolve => { releaseModels = resolve; }); };
  f.controller.selectLibrary('models'); await flush();
  f.api.search = async () => ({results: [{id: 'source-old', title: '旧来源', type: 'model_case'}]});
  const form = f.get('.lp-search-form'); form.querySelector('input').value = '旧检索'; await form.emit('submit');
  const sourceButton = f.get('.lp-object-title');
  f.api.source = (_url, signal) => { sourceSignal = signal; return new Promise(resolve => { releaseSource = resolve; }); };
  const sourceRead = sourceButton.emit('click'); await flush(); assert.equal(f.get('.lp-source-dialog').open, true);
  f.api.search = (_url, signal) => { searchSignal = signal; return new Promise(resolve => { releaseSearch = resolve; }); };
  const searching = form.emit('submit'); await flush();
  await f.get('.lp-tab-deleted').emit('click'); await Promise.all([sourceRead, searching]);
  assert.equal(modelSignal.aborted, true); assert.equal(searchSignal.aborted, true); assert.equal(sourceSignal.aborted, true);
  assert.equal(form.querySelector('input').value, ''); assert.equal(f.get('.lp-results').textContent, ''); assert.equal(f.get('.lp-case-files').textContent, '');
  assert.equal(f.get('.lp-source-dialog').open, false); assert.equal(f.get('.lp-source-content').textContent, '');
  releaseModels({files: [{path: 'D:/late.mph'}]}); releaseSearch({results: [{id: 'late', title: 'late hit'}]}); releaseSource({id: 'source-old', title: 'late source', type: 'model_case'}); await flush();
  assert.doesNotMatch(f.target.textContent, /late/); const reads = f.calls.length; await sourceButton.emit('click'); assert.equal(f.calls.length, reads);
  f.api.models = async id => ({library_id: id, files: [{path: 'D:/fresh.mph'}], status: 'ready'});
  await f.get('.lp-tab-current').emit('click'); f.controller.selectLibrary('models'); await flush();
  assert.match(f.get('.lp-case-files').textContent, /fresh/); assert.equal(form.querySelector('input').value, ''); f.controller.destroy();
});

test('changing tabs closes configuration evidence and aborts its request without accepting late nodes', async () => {
  const f = await fixture(null);
  f.api.search = async () => ({results: [{id: 'source-config', title: '配置来源', type: 'model_case'}]});
  f.api.source = async () => ({id: 'source-config', title: '配置来源', type: 'model_case'});
  f.get('.lp-search-form').querySelector('input').value = '配置'; await f.get('.lp-search-form').emit('submit'); await f.get('.lp-object-title').emit('click');
  let signal, release; f.api.configuration = (_url, incoming) => { signal = incoming; return new Promise(resolve => { release = resolve; }); };
  const form = f.get('.lp-configuration-form'), reading = form.emit('submit'); await flush();
  await f.get('.lp-tab-deleted').emit('click'); await reading; assert.equal(signal.aborted, true);
  release({nodes: [{tag: 'late-node'}]}); await flush(); assert.equal(f.get('.lp-source-content').textContent, ''); assert.equal(f.get('.lp-source-dialog').open, false);
  const reads = f.calls.length; await form.emit('submit'); assert.equal(f.calls.length, reads); f.controller.destroy();
});

test('rapid tab changes ignore delayed summaries and cancel an outstanding deletion confirmation', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  const deleting = f.get('.lp-library-delete').emit('click'); await flush(); assert.equal(f.get('.lp-delete-dialog').open, true);
  const original = f.api.getLibraries; let release;
  f.api.getLibraries = url => url.includes('include_deleted=1') ? new Promise(resolve => { release = resolve; }) : original(url);
  const trashRead = f.get('.lp-tab-deleted').emit('click'); await flush(); await deleting;
  assert.equal(f.get('.lp-delete-dialog').open, false); assert.equal(f.get('.lp-library-restore').hidden, true);
  await f.get('.lp-tab-current').emit('click');
  release({libraries: [{id: 'late-trash', name: 'late trash', deleted_at: new Date(epoch).toISOString()}], jobs: [], indexes: {}}); await trashRead;
  assert.equal(f.get('.lp-tab-current').getAttribute('aria-selected'), 'true'); assert.doesNotMatch(f.get('.lp-library-list').textContent, /late trash/);
  await f.get('.lp-delete-confirm').emit('click'); assert.equal(f.calls.some(call => call.settings.method === 'POST'), false); f.controller.destroy();
});

test('soft delete requires a reversible-data-preserving confirmation and restore stays disabled', async () => {
  let approved = false, prompt;
  const f = await fixture(null, {confirm: value => { prompt = value; return approved; }});
  f.controller.selectLibrary('models'); await flush();
  await f.get('.lp-library-delete').emit('click');
  assert.equal(f.calls.some(call => call.settings.method === 'POST'), false);
  assert.match(prompt, /可恢复删除/); assert.match(prompt, /原文件、索引与历史引用均保留/); assert.match(prompt, /不做物理删除/);
  approved = true; await f.get('.lp-library-delete').emit('click');
  assert.equal(f.target.querySelectorAll('.lp-library-deleted').length, 0);
  assert.match(f.get('.lp-message').textContent, /已可恢复删除/);
  await f.get('.lp-tab-deleted').emit('click');
  assert.equal(f.calls.at(-1).url, '/api/libraries?view=summary&include_deleted=1');
  assert.equal(f.target.querySelectorAll('.lp-library-deleted').length, 1);
  const discovered = f.calls.filter(call => call.url.endsWith('/models')).length;
  f.controller.selectLibrary('models'); await flush();
  assert.equal(f.calls.filter(call => call.url.endsWith('/models')).length, discovered, 'Deleted libraries must not enumerate directories');
  assert.equal(f.get('.lp-library-restore').hidden, false);
  await f.get('.lp-library-restore').emit('click');
  assert.equal(f.get('.lp-tab-deleted').getAttribute('aria-selected'), 'true');
  assert.equal(f.target.querySelectorAll('.lp-library-card').length, 0);
  await f.get('.lp-tab-current').emit('click'); f.controller.selectLibrary('models'); await flush();
  assert.equal(f.get('.lp-library-enable').textContent, '启用资料库');
  assert.equal(f.get('.lp-build-submit').disabled, true);
  assert.match(f.get('.lp-message').textContent, /恢复并保持关闭/);
  const restored = f.api.snapshot.libraries.find(library => library.id === 'models');
  assert.equal(restored.enabled, false); assert.equal(restored.deleted_at, null);
  assert.match(f.get('.lp-search-status').textContent, /已关闭/);
  await f.get('.lp-library-enable').emit('click');
  assert.doesNotMatch(f.get('.lp-search-status').textContent, /已关闭|请先启用/);
  assert.equal(f.calls.some(call => call.url.endsWith('/build')), false);
  await f.controller.refresh(true);
  assert.equal(f.calls.at(-1).url, '/api/libraries?view=summary');
  f.controller.destroy();
});

test('active jobs block enable and delete until actual cancellation finishes', async () => {
  const f = await fixture();
  assert.equal(f.get('.lp-library-enable').disabled, true); assert.equal(f.get('.lp-library-delete').disabled, true);
  assert.match(f.get('.lp-library-action-help').textContent, /请先中断并等待收尾/);
  await f.get('.lp-library-enable').emit('click'); await f.get('.lp-library-delete').emit('click');
  assert.equal(f.calls.some(call => call.settings.method === 'POST'), false);
  f.api.snapshot.jobs = [running({status: 'cancelled', stage: 'finished', finished_at: new Date(epoch).toISOString()})];
  await f.controller.refresh(true);
  assert.equal(f.get('.lp-library-enable').disabled, false); assert.equal(f.get('.lp-library-delete').disabled, false);
  f.controller.destroy();
});

test('failed lifecycle requests keep the previous state and duplicate pending clicks coalesce', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  const change = f.api.changeLibrary;
  f.api.changeLibrary = async () => { throw Error('请先中断正在运行的任务'); };
  await f.get('.lp-library-enable').emit('click');
  assert.equal(f.get('.lp-library-enable').textContent, '临时关闭');
  assert.match(f.get('.lp-message').textContent, /状态未更改.*请先中断/);
  let release;
  f.api.changeLibrary = (...args) => new Promise(resolve => { release = async () => resolve(await change(...args)); });
  const one = f.get('.lp-library-enable').emit('click'), two = f.get('.lp-library-enable').emit('click');
  assert.equal(f.get('.lp-library-enable').disabled, true);
  assert.equal(f.calls.filter(call => call.url.endsWith('/enable')).length, 2, 'One failed attempt and one pending retry');
  await release(); await Promise.all([one, two]);
  assert.equal(f.get('.lp-library-enable').textContent, '启用资料库');
  f.controller.destroy();
});

test('model discovery lists every file read-only and build submits the entire library without paths or limit', async () => {
  const f = await fixture(null);
  f.api.models = async id => ({library_id: id, files: Array.from({length: 25}, (_, index) => ({path: `D:/all/model${index}.mph`, relative_path: `model${index}.mph`, root_id: 'all-models'})), missing_roots: [], has_more: false});
  f.controller.selectLibrary('models'); await flush();
  assert.match(f.get('.lp-case-summary').textContent, /已发现 25 个模型，将构建全部/);
  assert.equal(f.get('.lp-case-files').children.length, 25);
  assert.equal(f.target.querySelectorAll('textarea').length, 0);
  assert.equal(f.get('.lp-case-selection').querySelectorAll('input').length, 0);
  assert.equal(f.controller.setModelPaths, undefined);
  assert.ok(f.calls.some(call => call.url === '/api/libraries/models/models'));
  assert.equal(f.calls.some(call => call.url.includes('/models?')), false);
  assert.equal(f.calls.some(call => call.settings.method === 'POST'), false, 'Read-only enumeration does not build or scan');
  await f.get('.lp-build-form').emit('submit');
  const post = f.calls.find(call => call.settings.method === 'POST');
  assert.equal(post.url, '/api/libraries/models/build');
  assert.deepEqual(JSON.parse(post.settings.body), {deep: false, enrich: false, modes: ['qmd_keyword']});
  f.controller.destroy();
});

test('model extraction defaults offline and explicit deep choice reaches the build request', async () => {
  for (const selected of ['offline', 'deep']) {
    const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
    const mode = f.get('.lp-extraction-select');
    assert.equal(mode.value, 'offline'); assert.equal(f.get('.lp-extraction').hidden, false);
    mode.value = selected; await mode.emit('change');
    assert.match(f.get('.lp-extraction-help').textContent, selected === 'deep' ? /本机 COMSOL/ : /无需 COMSOL/);
    await f.get('.lp-build-form').emit('submit');
    const post = f.calls.find(call => call.url.endsWith('/build'));
    assert.deepEqual(JSON.parse(post.settings.body), {deep: selected === 'deep', enrich: false, modes: ['qmd_keyword']});
    f.controller.destroy();
  }
});

test('switching libraries resets extraction offline and active jobs lock the selector', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  f.get('.lp-extraction-select').value = 'deep'; await f.get('.lp-extraction-select').emit('change');
  await f.controller.refresh(); assert.equal(f.get('.lp-extraction-select').value, 'deep');
  f.controller.selectLibrary('idle'); assert.equal(f.get('.lp-extraction').hidden, true);
  f.controller.selectLibrary('models'); await flush(); assert.equal(f.get('.lp-extraction-select').value, 'offline');
  f.api.snapshot.jobs = [running()]; await f.controller.refresh();
  assert.equal(f.get('.lp-extraction-select').disabled, true); f.controller.destroy();
});

test('manual libraries build registered documents without requesting models or per-build paths', async () => {
  const f = await fixture(null);
  assert.equal(f.get('.lp-case-selection').hidden, true);
  assert.equal(f.get('.lp-extraction').hidden, true);
  assert.equal(f.calls.some(call => call.url.endsWith('/models')), false);
  await f.get('.lp-build-form').emit('submit');
  const post = f.calls.find(call => call.settings.method === 'POST');
  assert.equal(post.url, '/api/libraries/idle/build');
  assert.deepEqual(JSON.parse(post.settings.body), {deep: false, enrich: false, modes: ['qmd_keyword']});
  f.controller.destroy();
});

test('missing or incomplete directories block building and never invite manual path input', async () => {
  for (const data of [{files: [], missing_roots: [], has_more: false}, {files: [{path: 'D:/one.mph'}], missing_roots: ['D:/missing'], has_more: false}, {files: [{path: 'D:/one.mph'}], missing_roots: [{root_id: 'main', path: 'D:/missing with spaces', reason: 'directory_enumeration_failed'}]}, {files: [{path: 'D:/one.mph'}], missing_roots: [], has_more: true}, {files: [{path: 'D:/one.mph'}], missing_roots: [], status: 'partial'}, {files: [{path: 'D:/one.mph'}], missing_roots: [], status: 'offline'}]) {
    const f = await fixture(null); f.api.models = async id => ({library_id: id, ...data});
    f.controller.selectLibrary('models'); await flush();
    assert.equal(f.get('.lp-build-submit').disabled, true);
    assert.equal(f.target.querySelectorAll('textarea').length, 0);
    await f.get('.lp-build-form').emit('submit');
    assert.equal(f.calls.some(call => call.settings.method === 'POST'), false);
    assert.match(f.get('.lp-case-summary').textContent, /目录|清单尚不完整/);
    if (typeof data.missing_roots?.[0] === 'object') { assert.match(f.get('.lp-case-roots').textContent, /D:\/missing with spaces/); assert.doesNotMatch(f.get('.lp-case-roots').textContent, /root_id|directory_enumeration_failed|\{"/); }
    f.controller.destroy();
  }
});

test('failed model listing can be retried, ordinary refresh does not rescan or build', async () => {
  const f = await fixture(null); f.api.models = async () => { throw Error('目录不可访问'); };
  f.controller.selectLibrary('models'); await flush();
  assert.equal(f.get('.lp-build-submit').disabled, true);
  assert.match(f.get('.lp-case-summary').textContent, /目录不可访问/);
  f.api.models = async id => ({library_id: id, files: [{path: 'D:/recovered/model.mph', relative_path: 'model.mph', root_id: 'main'}], has_more: false, missing_roots: []});
  await f.get('.lp-find-models').emit('click');
  assert.equal(f.get('.lp-build-submit').disabled, false);
  const reads = f.calls.filter(call => call.url.endsWith('/models')).length;
  await f.controller.refresh(true); await flush();
  assert.equal(f.calls.filter(call => call.url.endsWith('/models')).length, reads);
  assert.equal(f.calls.some(call => call.settings.method === 'POST'), false);
  f.controller.destroy();
});

test('late model results cannot replace another library, and destroying aborts directory reads', async () => {
  const f = await fixture(null); f.api.snapshot.libraries.push({id: 'second', name: '第二模型库', type: 'user-models', roots: []}); await f.controller.refresh(true);
  let releaseFirst;
  f.api.models = id => id === 'models' ? new Promise(resolve => { releaseFirst = resolve; }) : Promise.resolve({library_id: id, files: [{path: 'E:/second/model.mph', relative_path: 'second.mph'}], missing_roots: [], has_more: false});
  f.controller.selectLibrary('models'); f.controller.selectLibrary('second'); await flush();
  releaseFirst({library_id: 'models', files: Array.from({length: 30}, (_, index) => ({path: `D:/old/${index}.mph`})), missing_roots: [], has_more: false}); await flush();
  assert.match(f.get('.lp-selected-heading').textContent, /第二模型库/);
  assert.match(f.get('.lp-case-summary').textContent, /已发现 1 个模型/);
  assert.doesNotMatch(f.get('.lp-case-files').textContent, /D:\/old/);
  let release, signal;
  f.api.models = (_id, value) => { signal = value; return new Promise(resolve => { release = resolve; }); };
  const pendingRead = f.get('.lp-find-models').emit('click'); await flush(); f.controller.destroy();
  assert.equal(signal.aborted, true);
  release({library_id: 'second', files: [], missing_roots: []}); await pendingRead;
  assert.equal(f.target.children.length, 0); assert.equal(f.timer.timers.size, 0);
});

test('closing a library invalidates delayed search hits and aborts its old model discovery', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  let releaseSearch, releaseModels, discoverySignal;
  f.api.search = () => new Promise(resolve => { releaseSearch = resolve; });
  f.get('.lp-search-form').querySelector('input').value = 'thermal';
  const search = f.get('.lp-search-form').emit('submit'); await flush();
  let modelRequests = 0;
  f.api.models = (id, signal) => ++modelRequests === 1 ? new Promise(resolve => { discoverySignal = signal; releaseModels = resolve; }) : Promise.resolve({library_id: id, files: [{path: 'D:/current/safe.mph'}], missing_roots: [], has_more: false, status: 'ready'});
  const oldDiscovery = f.get('.lp-find-models').emit('click'); await flush();
  await f.get('.lp-library-enable').emit('click'); await flush();
  assert.equal(discoverySignal.aborted, true);
  assert.equal(f.get('.lp-library-enable').textContent, '启用资料库');
  releaseSearch({results: [{id: 'lo-stale', title: 'stale network hit'}]});
  releaseModels({library_id: 'models', files: [{path: 'D:/stale/old.mph'}], missing_roots: [], has_more: false});
  await Promise.all([search, oldDiscovery]); await flush();
  assert.equal(f.get('.lp-results').children.length, 0);
  assert.doesNotMatch(f.get('.lp-case-files').textContent, /stale/);
  assert.match(f.get('.lp-case-files').textContent, /models\.mph/);
  assert.equal(modelRequests, 1, 'Closing aborts discovery without starting another scan');
  assert.equal(f.get('.lp-build-submit').disabled, true);
  f.controller.destroy();
});

test('default delete confirmation is an in-page dialog; cancel sends nothing and duplicate confirmation deletes once', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  const cancelled = f.get('.lp-library-delete').emit('click'); await flush();
  const dialog = f.get('.lp-delete-dialog');
  assert.equal(dialog.tagName, 'DIALOG'); assert.equal(dialog.open, true);
  assert.match(dialog.textContent, /可恢复删除/); assert.match(dialog.textContent, /原文件、索引与历史引用均保留/);
  assert.equal(f.target.ownerDocument.activeElement, f.get('.lp-delete-cancel'));
  assert.equal(f.calls.some(call => call.settings.method === 'POST'), false);
  await f.get('.lp-delete-cancel').emit('click'); await cancelled;
  assert.equal(dialog.open, false); assert.equal(f.calls.some(call => call.settings.method === 'POST'), false);
  const first = f.get('.lp-library-delete').emit('click'), second = f.get('.lp-library-delete').emit('click'); await flush();
  assert.equal(dialog.open, true);
  await f.get('.lp-delete-confirm').emit('click'); await Promise.all([first, second]);
  assert.equal(f.calls.filter(call => call.url.endsWith('/delete')).length, 1);
  assert.equal(dialog.open, false);
  assert.doesNotMatch(source, /window\.confirm/);
  f.controller.destroy();
});

test('Escape, direct close and destroying the panel cancel default confirmations without late deletion', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  const escaped = f.get('.lp-library-delete').emit('click'); await flush(); await f.get('.lp-delete-dialog').emit('cancel'); await escaped;
  assert.equal(f.get('.lp-delete-dialog').open, false);
  const closed = f.get('.lp-library-delete').emit('click'); await flush(); f.get('.lp-delete-dialog').close(); await closed;
  const removed = f.get('.lp-library-delete').emit('click'); await flush(); f.controller.destroy(); await removed;
  assert.equal(f.calls.some(call => call.settings.method === 'POST'), false);
  assert.equal(f.target.children.length, 0); assert.equal(f.timer.timers.size, 0);
});

test('an injected async confirmation is awaited and a destroyed pending prompt cannot delete later', async () => {
  let resolve, prompts = 0;
  const f = await fixture(null, {confirm: () => { prompts++; return new Promise(value => { resolve = value; }); }});
  f.controller.selectLibrary('models'); await flush();
  const first = f.get('.lp-library-delete').emit('click'), second = f.get('.lp-library-delete').emit('click'); await flush();
  assert.equal(prompts, 1); assert.equal(f.calls.some(call => call.settings.method === 'POST'), false);
  resolve(true); await Promise.all([first, second]);
  assert.equal(f.calls.filter(call => call.url.endsWith('/delete')).length, 1); f.controller.destroy();
  let later;
  const g = await fixture(null, {confirm: () => new Promise(value => { later = value; })}); g.controller.selectLibrary('models'); await flush();
  const pending = g.get('.lp-library-delete').emit('click'); await flush(); g.controller.destroy(); await pending;
  later(true); await flush();
  assert.equal(g.calls.some(call => call.settings.method === 'POST'), false);
});

test('active-library refresh preserves ordinary empty results and custom search messages', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  f.api.search = async () => ({results: []});
  f.get('.lp-search-form').querySelector('input').value = 'no matching source';
  await f.get('.lp-search-form').emit('submit');
  const empty = f.get('.lp-search-status').textContent; assert.match(empty, /未找到匹配对象/);
  await f.controller.refresh(true); assert.equal(f.get('.lp-search-status').textContent, empty);
  f.get('.lp-search-status').textContent = '保留当前自定义检索提示';
  await f.controller.refresh(true); assert.equal(f.get('.lp-search-status').textContent, '保留当前自定义检索提示');
  f.controller.destroy();
});

async function showDeletedModelLibrary(f) {
  Object.assign(f.api.snapshot.libraries.find(item => item.id === 'models'), {deleted_at: new Date(epoch).toISOString(), enabled: false});
  await f.get('.lp-tab-deleted').emit('click');
  f.controller.selectLibrary('models'); await flush();
}

test('the large progress card shows a planning heartbeat with elapsed time and no invented percentage', async () => {
  const f = await fixture(null, {confirm: async () => true}); await showDeletedModelLibrary(f);
  let release; const original = f.api.changeLibrary;
  f.api.changeLibrary = async (...args) => { await new Promise(resolve => { release = resolve; }); return original(...args); };
  let progress = {library_id: 'models', status: 'running', stage: 'planning', phase: 'jobs', completed: 5, total: null, unit: 'objects', updated_at: new Date(epoch).toISOString()};
  f.api.purgeProgress = async () => progress;
  const card = f.get('.lp-live-progress'), stage = f.get('.lp-progress-stage'), bar = f.get('.lp-stage-progress'), tab = f.get('.lp-tab-deleted'); tab.focus();
  let scrolls = 0; card.scrollIntoView = () => { scrolls++; };
  const pending = f.get('.lp-library-purge').emit('click'); await flush();
  assert.equal(card.hidden, false); assert.equal(card.getAttribute('data-operation'), 'purge'); assert.equal(card.getAttribute('aria-label'), '资料库彻底删除进度');
  assert.equal(bar.hidden, false); assert.equal(bar.hasAttribute('value'), false); assert.equal(f.get('.lp-cancel-build').hidden, true);
  assert.doesNotMatch(card.textContent, /尚无构建任务|提交构建后|%/);
  await f.timer.tick(1500);
  assert.match(stage.textContent, /制定清理计划.*核对构建任务/); assert.match(f.get('.lp-progress-amount').textContent, /5 个对象.*总量未报告/); assert.equal(bar.hasAttribute('value'), false);
  await f.timer.tick(30000); assert.match(f.get('.lp-progress-elapsed').textContent, /31 秒/);
  assert.equal(f.get('.lp-live-progress'), card); assert.equal(f.get('.lp-progress-stage'), stage); assert.equal(f.get('.lp-stage-progress'), bar); assert.equal(f.target.ownerDocument.activeElement, tab); assert.equal(scrolls, 1);
  for (const [phase, label, unit] of [['revisions', '核对资料版本', 'objects'], ['artifacts', '核对提取文件与缓存', 'files'], ['evidence', '核对来源证据', 'directories'], ['validation', '校验清理范围', 'files']]) {
    progress = {...progress, phase, completed: 2, total: 7, unit, updated_at: new Date(f.timer.now()).toISOString()};
    await f.timer.tick(1500); assert.match(stage.textContent, new RegExp(label)); assert.equal(bar.value, 2); assert.equal(bar.max, 7);
  }
  progress = {...progress, stage: 'indexes', phase: null, completed: null, total: null, unit: 'generations', updated_at: new Date(f.timer.now()).toISOString()};
  await f.timer.tick(1500); assert.match(stage.textContent, /清理可归属索引/); assert.equal(card.getAttribute('data-phase'), ''); assert.equal(bar.hasAttribute('value'), false);
  release(); await pending; const reads = f.calls.length; await f.timer.tick(10000); assert.equal(f.calls.length, reads); assert.equal(f.timer.timers.size, 0); f.controller.destroy();
});

test('another library job poll cannot replace purge progress or repaint the library when it finishes', async () => {
  const otherJob = running({library_id: 'idle'}), f = await fixture(otherJob, {confirm: async () => true}); await showDeletedModelLibrary(f);
  let release; const original = f.api.changeLibrary;
  f.api.changeLibrary = async (...args) => { await new Promise(resolve => { release = resolve; }); return original(...args); };
  f.api.purgeProgress = async () => ({library_id: 'models', status: 'running', stage: 'planning', phase: 'evidence', completed: 4, total: null, unit: 'directories'});
  f.api.getJob = async () => ({...otherJob, status: 'completed', stage: 'finished'});
  const card = f.get('.lp-live-progress'), heading = f.get('.lp-selected-heading'), reads = f.calls.filter(call => call.url.startsWith('/api/libraries?')).length;
  let replacements = 0; const replace = heading.replaceChildren.bind(heading); heading.replaceChildren = (...children) => { replacements++; return replace(...children); };
  const pending = f.get('.lp-library-purge').emit('click'); await flush(); await f.timer.tick(2500);
  assert.equal(f.get('.lp-live-progress'), card); assert.equal(card.getAttribute('data-operation'), 'purge'); assert.equal(card.hidden, false);
  assert.match(f.get('.lp-progress-stage').textContent, /制定清理计划.*核对来源证据/); assert.match(f.get('.lp-progress-amount').textContent, /4 个目录/); assert.equal(f.get('.lp-stage-progress').hasAttribute('value'), false);
  assert.equal(replacements, 0); assert.equal(f.calls.filter(call => call.url.startsWith('/api/libraries?')).length, reads);
  release(); await pending; f.controller.destroy();
});

test('a slow purge uses only compact progress reads, survives 30 seconds and patches unchanged help without rebuilding DOM', async () => {
  const f = await fixture(null, {confirm: async () => true}); await showDeletedModelLibrary(f);
  const original = f.api.changeLibrary; let release;
  f.api.changeLibrary = async (...args) => { await new Promise(resolve => { release = resolve; }); return original(...args); };
  let progress = {library_id: 'models', status: 'running', stage: 'indexes', completed: 1, total: 7, unit: 'generations', updated_at: new Date(epoch).toISOString()};
  f.api.purgeProgress = async () => progress;
  const help = f.get('.lp-library-action-help'), index = f.get('.lp-index-card'), heading = f.get('.lp-selected-heading');
  const details = index.querySelector('details'); details.open = true; const summary = details.querySelector('summary'); summary.focus();
  f.target.ownerDocument.selection = {anchorNode: details.querySelector('pre'), selectedText: 'ready'};
  let helpWrites = 0, replacements = 0; const textContent = Object.getOwnPropertyDescriptor(Element.prototype, 'textContent');
  Object.defineProperty(help, 'textContent', {get() { return textContent.get.call(this); }, set(value) { helpWrites++; textContent.set.call(this, value); }});
  for (const element of [f.get('.lp-library-list'), f.get('.lp-indexes'), heading]) { const replace = element.replaceChildren.bind(element); element.replaceChildren = (...children) => { replacements++; return replace(...children); }; }
  const reads = f.calls.filter(call => call.url.startsWith('/api/libraries?')).length;
  const pending = f.get('.lp-library-purge').emit('click'); await flush(); await f.timer.tick(1500);
  assert.match(help.textContent, /清理可归属索引.*1 \/ 7 个索引代次/);
  const writes = helpWrites; await f.timer.tick(30000);
  assert.equal(helpWrites, writes, 'identical compact snapshots must not replace help text');
  assert.equal(replacements, 0); assert.equal(f.get('.lp-index-card'), index); assert.equal(f.get('.lp-selected-heading'), heading);
  assert.equal(details.open, true); assert.equal(f.target.ownerDocument.activeElement, summary); assert.equal(f.target.ownerDocument.selection.anchorNode, details.querySelector('pre'));
  assert.equal(f.calls.filter(call => call.url.startsWith('/api/libraries?')).length, reads);
  assert.equal(f.calls.filter(call => call.settings.method === 'POST').length, 1);
  assert.equal(f.get('.lp-library-purge').disabled, true); assert.equal(f.get('.lp-library-purge').textContent, '正在彻底删除…');
  assert.ok(f.calls.filter(call => call.url.endsWith('/purge-progress')).length >= 20);
  progress = {...progress, stage: 'files', completed: 4, total: 12, unit: 'files', updated_at: new Date(epoch + 1000).toISOString()};
  await f.timer.tick(1500); assert.match(help.textContent, /清理可归属资料文件.*4 \/ 12 个文件/);
  progress = {...progress, status: 'completed', stage: 'finished'}; await f.timer.tick(1500);
  assert.match(help.textContent, /服务报告清理结束，等待最终响应确认/); assert.doesNotMatch(f.get('.lp-message').textContent, /已彻底删除/);
  release(); await pending; const after = f.calls.length; await f.timer.tick(10000);
  assert.equal(f.calls.length, after); assert.match(f.get('.lp-message').textContent, /已彻底删除/); assert.equal(f.timer.timers.size, 0); f.controller.destroy();
});

test('purge idle progress, read failures and older snapshots do not confirm deletion or replay the POST', async () => {
  const f = await fixture(null, {confirm: async () => true}); await showDeletedModelLibrary(f);
  let reject; f.api.changeLibrary = () => new Promise((_resolve, fail) => { reject = fail; });
  const pending = f.get('.lp-library-purge').emit('click'); await flush(); await f.timer.tick(1500);
  assert.match(f.get('.lp-library-action-help').textContent, /等待服务报告清理阶段/);
  f.api.purgeProgress = async () => ({library_id: 'models', status: 'running', stage: 'catalog', completed: null, total: null, updated_at: new Date(epoch + 2000).toISOString()});
  await f.timer.tick(1500); assert.match(f.get('.lp-library-action-help').textContent, /更新资料目录/);
  f.api.purgeProgress = async () => ({library_id: 'models', status: 'running', stage: 'planning', updated_at: new Date(epoch).toISOString()});
  await f.timer.tick(1500); assert.match(f.get('.lp-library-action-help').textContent, /更新资料目录/);
  f.api.purgeProgress = async () => { throw Error('进度连接不可用'); };
  await f.timer.tick(1500); assert.match(f.get('.lp-library-action-help').textContent, /更新资料目录.*进度读取暂不可用.*进度连接不可用/);
  assert.equal(f.get('.lp-library-purge').disabled, true); assert.equal(f.calls.filter(call => call.settings.method === 'POST').length, 1);
  reject(Error('删除响应连接中断')); await pending;
  assert.match(f.get('.lp-message').textContent, /彻底删除结果未确认.*删除响应连接中断.*可能已处理部分内容.*不会自动重发/s);
  const after = f.calls.length; await f.timer.tick(10000); assert.equal(f.calls.length, after); assert.equal(f.timer.timers.size, 0); f.controller.destroy();
});

test('a stalled progress read never overlaps and is aborted when the purge response finishes', async () => {
  const f = await fixture(null, {confirm: async () => true}); await showDeletedModelLibrary(f);
  let release, signal, progressReads = 0; const original = f.api.changeLibrary;
  f.api.changeLibrary = async (...args) => { await new Promise(resolve => { release = resolve; }); return original(...args); };
  f.api.purgeProgress = (_id, incoming) => { signal = incoming; progressReads++; return new Promise(() => {}); };
  const pending = f.get('.lp-library-purge').emit('click'); await flush(); await f.timer.tick(10000);
  assert.equal(progressReads, 1); assert.equal(signal.aborted, false);
  release(); await pending; assert.equal(signal.aborted, true);
  await f.timer.tick(10000); assert.equal(progressReads, 1); assert.equal(f.timer.timers.size, 0); f.controller.destroy();
});

test('a hung purge has a ten-minute deadline with an uncertain result and no automatic POST retry', async () => {
  const f = await fixture(null, {confirm: async () => true}); await showDeletedModelLibrary(f);
  let signal; f.api.changeLibrary = (_id, _action, _body, incoming) => { signal = incoming; return new Promise(() => {}); };
  const pending = f.get('.lp-library-purge').emit('click'); await flush(); await f.timer.tick(599999);
  assert.equal(signal.aborted, false); assert.equal(f.get('.lp-library-purge').disabled, true);
  await f.timer.tick(1); await pending;
  assert.equal(signal.aborted, true); assert.match(f.get('.lp-message').textContent, /彻底删除结果未确认.*结果待核对.*不会自动重发/s);
  assert.equal(f.get('.lp-library-purge').disabled, false); assert.equal(f.calls.filter(call => call.settings.method === 'POST').length, 1);
  const after = f.calls.length; await f.timer.tick(10000); assert.equal(f.calls.length, after); assert.equal(f.timer.timers.size, 0); f.controller.destroy();
});

test('destroying a pending purge stops progress reads and ignores late progress and deletion responses', async () => {
  const f = await fixture(null, {confirm: async () => true}); await showDeletedModelLibrary(f);
  let releasePost, releaseProgress, postSignal, progressSignal;
  f.api.changeLibrary = (_id, _action, _body, signal) => { postSignal = signal; return new Promise(resolve => { releasePost = resolve; }); };
  f.api.purgeProgress = (_id, signal) => { progressSignal = signal; return new Promise(resolve => { releaseProgress = resolve; }); };
  const pending = f.get('.lp-library-purge').emit('click'); await flush(); await f.timer.tick(1500);
  f.controller.destroy(); await pending;
  assert.equal(postSignal.aborted, true); assert.equal(progressSignal.aborted, true); assert.equal(f.timer.timers.size, 0);
  const count = f.calls.length;
  releasePost({status: 'purged'}); releaseProgress({library_id: 'models', status: 'completed', stage: 'finished'}); await flush(); await f.timer.tick(10000);
  assert.equal(f.target.children.length, 0); assert.equal(f.calls.length, count); assert.equal(f.timer.timers.size, 0);
});

test('purge only appears for deleted libraries and requires a separate irreversible in-page confirmation', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  assert.equal(f.get('.lp-library-purge').hidden, true);
  await f.get('.lp-library-purge').emit('click'); assert.equal(f.get('.lp-delete-dialog').open, false);
  await showDeletedModelLibrary(f);
  assert.equal(f.get('.lp-library-purge').hidden, false);
  const cancelled = f.get('.lp-library-purge').emit('click'); await flush();
  const dialog = f.get('.lp-delete-dialog');
  assert.equal(dialog.open, true); assert.match(dialog.textContent, /不可恢复/);
  assert.match(dialog.textContent, /已登记的提取资料、可归属缓存及索引/); assert.match(dialog.textContent, /历史引用将失效/);
  assert.match(dialog.textContent, /原始 MPH 模型和手册文件保留/); assert.match(dialog.textContent, /其他资料库中的同源副本不受影响/);
  assert.equal(f.get('.lp-delete-confirm').textContent, '确认彻底删除');
  await f.get('.lp-delete-cancel').emit('click'); await cancelled;
  assert.equal(f.calls.some(call => call.settings.method === 'POST'), false);
  const purge = f.api.changeLibrary;
  f.api.changeLibrary = async (...args) => ({...await purge(...args), warnings: ['未猜删无法安全归属的旧缓存，请人工核对。']});
  const first = f.get('.lp-library-purge').emit('click'), second = f.get('.lp-library-purge').emit('click'); await flush();
  await f.get('.lp-delete-confirm').emit('click'); await Promise.all([first, second]);
  const posts = f.calls.filter(call => call.settings.method === 'POST');
  assert.equal(posts.length, 1); assert.equal(posts[0].url, '/api/libraries/models/purge');
  assert.deepEqual(JSON.parse(posts[0].settings.body), {confirmed: true});
  assert.equal(f.target.querySelectorAll('.lp-library-card').some(item => item.getAttribute('data-library-id') === 'models'), false);
  assert.match(f.get('.lp-message').textContent, /已彻底删除，不可恢复/);
  assert.match(f.get('.lp-message').textContent, /未猜删无法安全归属的旧缓存，请人工核对/);
  assert.match(f.get('.lp-message').textContent, /原始 MPH 与手册文件保留/);
  f.controller.destroy();
});

test('purge is disabled by active builds and failed purges keep the deleted library for retry', async () => {
  const busy = await fixture(); await showDeletedModelLibrary(busy);
  assert.equal(busy.get('.lp-library-purge').disabled, true);
  await busy.get('.lp-library-purge').emit('click');
  assert.equal(busy.calls.some(call => call.settings.method === 'POST'), false); busy.controller.destroy();
  const f = await fixture(null, {confirm: async () => true}); await showDeletedModelLibrary(f);
  f.api.changeLibrary = async () => { throw Error('缓存清理失败，请重试'); };
  await f.get('.lp-library-purge').emit('click');
  assert.equal(f.target.querySelectorAll('.lp-library-deleted').length, 1);
  assert.match(f.get('.lp-message').textContent, /彻底删除结果未确认.*缓存清理失败/);
  assert.equal(f.get('.lp-library-purge').disabled, false);
  f.api.changeLibrary = async () => ({library_id: 'models', status: 'partial'});
  await f.get('.lp-library-purge').emit('click');
  assert.equal(f.target.querySelectorAll('.lp-library-deleted').length, 1);
  assert.match(f.get('.lp-message').textContent, /尚未确认彻底删除完成/);
  f.controller.destroy();
});

test('destroying a pending purge confirmation sends nothing and cannot delete on a late decision', async () => {
  let accept;
  const f = await fixture(null, {confirm: () => new Promise(resolve => { accept = resolve; })}); await showDeletedModelLibrary(f);
  const pending = f.get('.lp-library-purge').emit('click'); await flush(); f.controller.destroy(); await pending;
  accept(true); await flush();
  assert.equal(f.calls.some(call => call.settings.method === 'POST'), false);
  assert.equal(f.target.children.length, 0);
});

test('list and job polling request compact summaries; closed index details never stringify large manifests', async () => {
  let bodyReads = 0;
  const manifest = {generation: 'g1', modes: {qmd_keyword: {ready: true}}, mapping: {huge: 'raw mapping'}, representation: {huge: 'raw representation'}};
  Object.defineProperty(manifest.mapping, 'sentinel', {enumerable: true, get() { bodyReads++; return 'must not serialize'; }});
  const f = await fixture(running({indexes: manifest}));
  assert.equal(f.calls[0].url, '/api/libraries?view=summary');
  const details = f.get('.lp-job-index-details');
  assert.equal(details.querySelector('pre'), null); assert.equal(bodyReads, 1, 'Fixture transport clones its response once');
  details.open = true; await details.emit('toggle');
  assert.match(details.querySelector('pre').textContent, /g1/);
  assert.doesNotMatch(details.querySelector('pre').textContent, /raw mapping|raw representation|must not serialize/);
  await f.timer.tick(2500);
  assert.ok(f.calls.some(call => call.url === `/api/library-jobs/${jobId}?view=summary`));
  f.controller.destroy();
});

test('four identical active job polls do not rebuild cards, collapse details or detach focus', async () => {
  const f = await fixture(running({indexes: {generation: 'g1', modes: {qmd_keyword: {ready: true}}}}));
  const jobs = f.get('.lp-jobs'), card = f.get('.lp-job'), details = f.get('.lp-job-index-details');
  details.open = true; await details.emit('toggle');
  const summary = details.querySelector('summary'), content = details.querySelector('pre'); summary.focus();
  f.target.ownerDocument.selection = {anchorNode: content, selectedText: 'g1'};
  let replacements = 0, cardRemovals = 0;
  const replace = jobs.replaceChildren.bind(jobs);
  jobs.replaceChildren = (...children) => { replacements++; return replace(...children); };
  const remove = card.remove.bind(card); card.remove = () => { cardRemovals++; return remove(); };
  await f.timer.tick(10000);
  assert.equal(f.calls.filter(call => call.url.startsWith('/api/library-jobs/')).length, 4);
  assert.equal(replacements, 0, `unchanged job polling rebuilt the complete list ${replacements} times`);
  assert.equal(cardRemovals, 0, 'unchanged cards must never be detached/reinserted');
  assert.equal(f.get('.lp-job'), card);
  assert.equal(f.get('.lp-job-index-details'), details);
  assert.equal(details.querySelector('pre'), content);
  assert.equal(details.open, true);
  assert.equal(summary.parentNode, details);
  assert.equal(f.target.ownerDocument.activeElement, summary);
  assert.equal(f.target.ownerDocument.selection.anchorNode, content);
  f.controller.destroy();
});

test('real stage and index progress update in place without replacing expanded job details', async () => {
  const f = await fixture(running({indexes: {generation: 'g1', modes: {qmd_keyword: {ready: false}}}}));
  const card = f.get('.lp-job'), heading = card.querySelector('.lp-job-heading'), details = card.querySelector('.lp-job-index-details');
  details.open = true; await details.emit('toggle');
  const summary = details.querySelector('summary'), pre = details.querySelector('pre'); summary.focus();
  f.api.getJob = async () => running({stage: 'indexing_keyword', completed: 3,
    progress: {completed: 3, total: 6, unit: 'objects', detail: '索引资料章节', current_file: 'D:/current.html'},
    indexes: {generation: 'g2', modes: {qmd_keyword: {ready: true}}}, updated_at: new Date(epoch + 1000).toISOString()});
  await f.timer.tick(2500);
  assert.equal(f.get('.lp-job'), card); assert.equal(card.querySelector('.lp-job-heading'), heading);
  assert.match(heading.textContent, /构建关键词索引/); assert.match(card.textContent, /已处理 3 \/ 4/);
  assert.equal(f.get('.lp-stage-progress').value, 3); assert.equal(f.get('.lp-stage-progress').max, 6);
  assert.match(f.get('.lp-progress-file').textContent, /current\.html/);
  assert.equal(card.querySelector('.lp-job-index-details'), details); assert.equal(details.open, true);
  assert.equal(details.querySelector('pre'), pre); assert.match(pre.textContent, /g2/);
  assert.equal(f.target.ownerDocument.activeElement, summary);
  f.controller.destroy();
});

test('adding and completing another job retains previous cards, expanded history and focus', async () => {
  const f = await fixture(running({indexes: {generation: 'original'}}));
  const originalCard = f.get('.lp-job'), originalDetails = originalCard.querySelector('.lp-job-index-details');
  originalDetails.open = true; await originalDetails.emit('toggle');
  const originalSummary = originalDetails.querySelector('summary'); originalSummary.focus();
  const otherId = 'b'.repeat(32);
  const other = running({id: otherId, stage: 'scanning', progress: {completed: null, total: null, unit: 'files'}, indexes: {generation: 'new'}});
  f.api.snapshot.jobs = [other, f.api.snapshot.jobs[0]];
  await f.controller.refresh(true);
  let cards = f.target.querySelectorAll('.lp-job');
  assert.equal(cards.length, 2); assert.equal(cards[1], originalCard);
  assert.equal(cards[0].getAttribute('data-job-id'), otherId);
  const history = f.get('.lp-history');
  assert.equal(history.open, true, 'moving the focused card into history keeps it visible');
  assert.equal(originalDetails.open, true); assert.equal(f.target.ownerDocument.activeElement, originalSummary);
  const selectedPre = originalDetails.querySelector('pre');
  f.target.ownerDocument.selection = {anchorNode: selectedPre, selectedText: 'original'};
  // The newer task becomes terminal while the original task remains active.
  f.api.getJob = async id => id === otherId ? {...other, status: 'completed', stage: 'finished', finished_at: new Date(epoch + 3000).toISOString(), updated_at: new Date(epoch + 3000).toISOString()} : f.api.snapshot.jobs.find(job => job.id === id);
  await f.timer.tick(2500);
  cards = f.target.querySelectorAll('.lp-job');
  assert.equal(cards.find(card => card.getAttribute('data-job-id') === jobId), originalCard);
  assert.equal(originalCard.querySelector('.lp-job-index-details'), originalDetails);
  assert.equal(originalDetails.open, true);
  assert.equal(f.target.ownerDocument.activeElement, originalSummary);
  // A real placement change may detach the selected card, but completing a
  // different job must not recreate any of its details/text nodes.
  assert.equal(originalDetails.querySelector('pre'), selectedPre);
  assert.equal(f.get('.lp-history'), history);
  f.controller.destroy();
});

test('another job insertion and progress change do not detach selected text or focus in historical details', async () => {
  const f = await fixture(running());
  const oldId = 'c'.repeat(32), newId = 'b'.repeat(32);
  const old = running({id: oldId, status: 'completed', stage: 'finished', indexes: {generation: 'kept-history'}});
  f.api.snapshot.jobs.push(old); await f.controller.refresh(true);
  const history = f.get('.lp-history'); history.open = true;
  const card = f.target.querySelectorAll('.lp-job').find(value => value.getAttribute('data-job-id') === oldId);
  const details = card.querySelector('.lp-job-index-details'); details.open = true; await details.emit('toggle');
  const pre = details.querySelector('pre'), summary = details.querySelector('summary'); summary.focus();
  f.target.ownerDocument.selection = {anchorNode: pre, selectedText: 'kept-history'};
  f.api.snapshot.jobs.unshift(running({id: newId})); await f.controller.refresh(true);
  assert.equal(card.parentNode, history); assert.equal(details.open, true); assert.equal(history.open, true);
  assert.equal(f.target.ownerDocument.selection.anchorNode, pre); assert.equal(f.target.ownerDocument.activeElement, summary);
  f.api.getJob = async id => id === newId ? running({id: newId, stage: 'indexing_vector', progress: {completed: 1, total: 10, unit: 'chunks'}}) : f.api.snapshot.jobs.find(job => job.id === id);
  await f.timer.tick(2500);
  assert.equal(f.target.querySelectorAll('.lp-job').find(value => value.getAttribute('data-job-id') === oldId), card);
  assert.equal(card.querySelector('.lp-job-index-details'), details); assert.equal(details.querySelector('pre'), pre);
  assert.equal(f.target.ownerDocument.selection.anchorNode, pre); assert.equal(f.target.ownerDocument.activeElement, summary);
  f.controller.destroy();
});

test('without pending jobs there is no background job polling or job-card repaint', async () => {
  const f = await fixture(null), jobs = f.get('.lp-jobs'), empty = jobs.children[0];
  const requests = f.calls.length;
  let replacements = 0; const replace = jobs.replaceChildren.bind(jobs);
  jobs.replaceChildren = (...children) => { replacements++; return replace(...children); };
  assert.equal(f.timer.timers.size, 0);
  await f.timer.tick(10000);
  assert.equal(f.calls.length, requests); assert.equal(replacements, 0); assert.equal(jobs.children[0], empty);
  f.controller.destroy();
});

test('one stalled job cannot hold back other jobs, and its own request has a bounded deadline', async () => {
  const f = await fixture(), otherId = 'b'.repeat(32), reads = new Map(), signals = [];
  f.api.snapshot.jobs.push(running({id: otherId})); await f.controller.refresh(true);
  f.api.getJob = (id, signal) => { reads.set(id, (reads.get(id) || 0) + 1); if (id === jobId) { signals.push(signal); return new Promise(() => {}); } return Promise.resolve(running({id})); };
  await f.timer.tick(10000);
  assert.equal(reads.get(jobId), 1, 'An in-flight job is not requested again');
  assert.equal(reads.get(otherId), 4, 'The responsive job keeps its normal read-only polling cadence');
  await f.timer.tick(10000);
  assert.equal(signals[0].aborted, true); assert.ok(reads.get(jobId) >= 2, 'A stalled read can be retried after its deadline');
  assert.ok(f.calls.every(call => !call.settings.method));
  f.controller.destroy(); await flush(); assert.equal(f.timer.timers.size, 0);
});

test('a stalled build submission releases controls with result-to-check wording and no automatic resubmission', async () => {
  const f = await fixture(null); f.api.build = () => new Promise(() => {});
  const submitting = f.get('.lp-build-form').emit('submit'); await flush();
  await f.timer.tick(30000); await submitting;
  assert.match(f.get('.lp-message').textContent, /结果待核对.*不会自动重发/);
  assert.equal(f.get('.lp-build-form').getAttribute('aria-busy'), 'false');
  assert.equal(f.get('.lp-build-submit').disabled, false);
  assert.equal(f.calls.filter(call => call.url.endsWith('/build')).length, 1);
  f.controller.destroy(); await flush(); assert.equal(f.timer.timers.size, 0);
});

test('changing libraries cancels a stale search and immediately permits searching the new library', async () => {
  const f = await fixture(null); f.controller.selectLibrary('models'); await flush();
  let release, signal; f.api.search = (_url, incoming) => { signal = incoming; return new Promise(resolve => { release = resolve; }); };
  const form = f.get('.lp-search-form'); form.querySelector('input').value = 'first library query';
  const searching = form.emit('submit'); await flush();
  f.controller.selectLibrary('idle'); await flush();
  assert.equal(signal.aborted, true); assert.equal(form.querySelector('button').disabled, false);
  await searching; release({results: [{id: 'stale', title: 'stale result'}]}); await flush();
  assert.doesNotMatch(f.get('.lp-results').textContent, /stale result/);
  f.controller.destroy(); await flush();
});

test('source full-body and raw JSON stay out of closed details until explicitly expanded', async () => {
  const f = await fixture(null), full = 'large-complete-source-'.repeat(50000);
  f.api.search = async () => ({results: [{id: 'lo-complete', title: 'Full object', type: 'manual_topic'}]});
  f.api.source = async () => ({id: 'lo-complete', title: 'Full object', type: 'manual_topic', text: full});
  f.get('.lp-search-form').querySelector('input').value = 'query'; await f.get('.lp-search-form').emit('submit');
  await f.get('.lp-object-title').emit('click');
  const content = f.get('.lp-source-content');
  assert.equal(content.querySelectorAll('pre').length, 0); assert.doesNotMatch(content.textContent, /large-complete-source-/);
  const details = content.children.filter(node => node.tagName === 'DETAILS');
  details[0].open = true; await details[0].emit('toggle'); assert.equal(content.querySelectorAll('pre').length, 1);
  details[1].open = true; await details[1].emit('toggle'); assert.equal(content.querySelectorAll('pre').length, 2);
  await details[1].emit('toggle'); assert.equal(content.querySelectorAll('pre').length, 2);
  f.controller.destroy(); await flush();
});

test('offline saved-state gaps remain visible before expanding file details', async () => {
  const f = await fixture(running({status: 'partial', stage: 'finished', counts: undefined,
    items: [{path: 'bracket.mph', status: 'completed', objects: 45, coverage: {deep_api: {status: 'not_requested'},
      raw_xml: {status: 'bounded_saved_state', node_count: 404, normalization: {properties_total: 599,
        properties_partial: 8, properties_unresolved: 3, selection_statuses: {unresolved: 24}}}}}]}));
  f.controller.selectLibrary('models'); await flush();
  const summary = f.get('.lp-job-coverage');
  assert.equal(summary.hidden, false);
  assert.match(summary.textContent, /已读 404 个节点，599 项属性/);
  assert.match(summary.textContent, /属性未完整解码 8 项/);
  assert.match(summary.textContent, /属性未解析 3 项/);
  assert.match(summary.textContent, /选择编码未解析 24 项/);
  assert.match(summary.textContent, /未运行几何、网格或求解/);
  assert.doesNotMatch(summary.textContent, /API：|100%|"normalization"/);
  assert.equal(f.get('.lp-progress-counts').textContent, '成功 0 · 部分 1 · 失败 0');
  assert.equal(f.get('.lp-job').querySelector('.lp-job-items').open, false);
  f.controller.destroy();
});

test('multi-model coverage shows a bounded summary with accurate gap count', async () => {
  const f = await fixture(running({status: 'partial', stage: 'finished', counts: undefined,
    items: [{path: 'one.mph', coverage: {raw_xml: {node_count: 2, truncated: true}}},
      {path: 'two.mph', coverage: {raw_xml: {node_count: 3, normalization: {properties_total: 4}}}},
      {path: 'three.html', coverage: {text: true}}]}));
  f.controller.selectLibrary('models'); await flush();
  assert.match(f.get('.lp-job-coverage').textContent, /2 个文件，1 个存在读取缺口/);
  assert.match(f.get('.lp-job-coverage').textContent, /不代表完整解析或仿真验证/);
  assert.equal(f.get('.lp-progress-counts').textContent, '成功 2 · 部分 1 · 失败 0');
  f.controller.destroy();
});

test('API coverage does not invent a zero count when the backend omitted read errors', async () => {
  const f = await fixture(running({status: 'partial', stage: 'finished', items: [{path: 'model.mph',
    coverage: {deep_api: {status: 'partial', node_count: 118}}}]}));
  f.controller.selectLibrary('models'); await flush();
  assert.match(f.get('.lp-job-coverage').textContent, /API：已读 118 个节点，未报告 项读取缺口/);
  assert.doesNotMatch(f.get('.lp-job-coverage').textContent, /，0 项读取缺口/);
  f.controller.destroy();
});

test('bulk item previews identify their scope and keep whole-job coverage totals', async () => {
  const f = await fixture(running({status: 'partial', stage: 'finished', items_total: 201, items_truncated: true,
    counts: {succeeded: 81, partial: 120, failed: 0}, coverage_summary: {partial_files: 120},
    items: [{path: 'last.mph', status: 'partial', coverage: {raw_xml: {node_count: 14, normalization: {properties_unresolved: 2}}}}]}));
  f.controller.selectLibrary('models'); await flush();
  assert.match(f.get('.lp-job-coverage').textContent, /201 个文件，120 个存在读取缺口/);
  assert.match(f.get('.lp-job').textContent, /最近 1 \/ 共 201 个文件.*统计涵盖全部文件/);
  assert.equal(f.get('.lp-progress-counts').textContent, '成功 81 · 部分 120 · 失败 0');
  f.controller.destroy();
});

test('hierarchical directory filters fifty libraries locally without remote queries or root scans', async () => {
  const f = await fixture(null);
  f.api.snapshot.libraries = [{id:'docs', name:'COMSOL 6.3 官方文档 · HTML / PDF', type:'official-manuals', enabled:false, roots:[]},
    ...Array.from({length:48}, (_, i) => ({id:`module-${i}`, name:`COMSOL 6.3 示例 · Module_${i}`, type:'official-models', enabled:false, roots:[{id:'main',path:`D:/modules/module-${i}`,role:'models'}]})),
    {id:'custom', name:'个人阀门模型', type:'user-models', enabled:false, roots:[]}];
  await f.controller.refresh(true);
  assert.equal(f.target.querySelectorAll('.lp-library-card').length,50);
  const groups = () => f.target.querySelectorAll('.lp-library-group');
  assert.deepEqual(groups().map(group => group.getAttribute('data-library-group')), ['official','official-manuals','official-models','custom','user-models']);
  assert.match(f.get('.lp-browser-count').textContent,/50 个库 · 已启用 0/);
  assert.equal(groups().find(group => group.getAttribute('data-library-group')==='official-models').open,false);
  const before = f.calls.length;
  f.get('.lp-library-filter').value='module-47'; await f.get('.lp-library-filter').emit('input');
  assert.deepEqual(f.target.querySelectorAll('.lp-library-card').map(item=>item.getAttribute('data-library-id')),['module-47']);
  assert.equal(f.calls.length,before,'Filter only loaded library metadata');
  assert.ok(groups().every(group=>group.open),'Search reveals matching descendants');
  await f.get('.lp-tree-collapse').emit('click'); assert.ok(groups().every(group=>!group.open));
  f.get('.lp-library-list').scrollTop=320; f.get('.lp-content').scrollTop=600;
  await f.get('.lp-library-card').emit('click');
  assert.equal(f.get('.lp-library-list').scrollTop,320);
  assert.equal(f.get('.lp-content').scrollTop,0);
  assert.match(f.get('.lp-selected-heading').textContent,/官方资料 \/ 模型案例/);
  assert.equal(f.calls.length,before,'Selecting a disabled model never enumerates its source directory');
  f.controller.destroy();
});

test('group controls update only matching libraries once and reuse indexes without global refresh', async () => {
  const f = await fixture(null);
  f.api.snapshot.libraries=[{id:'manual',name:'官方手册',type:'official-manuals',enabled:false,roots:[]},
    {id:'heat',name:'传热模块',type:'official-models',enabled:false,roots:[]},
    {id:'cfd',name:'CFD 模块',type:'official-models',enabled:false,roots:[]},
    {id:'own',name:'自定义模块',type:'user-models',enabled:false,roots:[]}];
  await f.controller.refresh(true);
  const before=f.calls.length;
  const modelGroup=f.target.querySelectorAll('.lp-library-group').find(group=>group.getAttribute('data-library-group')==='official-models');
  await modelGroup.querySelector('.lp-group-enable').emit('click');
  const post=f.calls.slice(before).filter(call=>call.settings.method==='POST');
  assert.equal(post.length,1); assert.equal(post[0].url,'/api/libraries/enable');
  assert.deepEqual(JSON.parse(post[0].settings.body),{library_ids:['heat','cfd'],enabled:true});
  assert.equal(f.calls.length,before+1,'No global status, rebuilding or source enumeration after group toggle');
  assert.equal(f.api.snapshot.libraries.find(library=>library.id==='manual').enabled,false);
  assert.equal(f.api.snapshot.libraries.find(library=>library.id==='own').enabled,false);
  assert.match(f.get('.lp-browser-count').textContent,/已启用 2/);
  f.get('.lp-library-filter').value='传热'; await f.get('.lp-library-filter').emit('input');
  await f.get('.lp-group-disable').emit('click');
  assert.deepEqual(JSON.parse(f.calls.at(-1).settings.body),{library_ids:['heat'],enabled:false});
  assert.equal(f.api.snapshot.libraries.find(library=>library.id==='cfd').enabled,true);
  f.controller.destroy();
});

test('group failure preserves flags and no deleted-library tab offers enable controls', async () => {
  const f=await fixture(null); f.api.snapshot.libraries.forEach(library=>{library.enabled=false;}); await f.controller.refresh(true);
  f.api.changeLibraries=async()=>{throw Error('scope update failed');};
  const before=f.calls.length; await f.get('.lp-group-enable').emit('click');
  assert.equal(f.calls.length,before+1); assert.ok(f.api.snapshot.libraries.every(library=>library.enabled===false));
  assert.match(f.get('.lp-message').textContent,/状态更新未确认/);
  f.api.snapshot.libraries.push({id:'trash',name:'已删除模型',type:'user-models',enabled:false,deleted_at:'2026-11-10',roots:[]});
  await f.get('.lp-tab-deleted').emit('click');
  assert.equal(f.target.querySelectorAll('.lp-group-switch').length,0);
  f.controller.destroy();
});

test('custom manuals use the declared source rather than names or directories and never request models', async () => {
  const f=await fixture(null);
  f.api.snapshot.libraries=[{id:'custom-doc',name:'官方手册副本 · 我的项目说明',type:'user-manuals',enabled:false,roots:[{id:'docs',path:'D:/COMSOL63/doc',role:'docs'}]}];
  await f.controller.refresh(true); f.controller.selectLibrary('custom-doc'); await flush();
  assert.deepEqual(f.target.querySelectorAll('.lp-library-group').map(group=>group.getAttribute('data-library-group')),['custom','user-manuals']);
  assert.match(f.get('.lp-selected-heading').textContent,/自定义资料 \/ 手册/);
  assert.equal(f.get('.lp-case-selection').hidden,true); assert.equal(f.get('.lp-extraction').hidden,true);
  await f.get('.lp-library-enable').emit('click');
  await f.get('.lp-build-form').emit('submit');
  const build=f.calls.find(call=>call.url==='/api/libraries/custom-doc/build');
  assert.ok(build); assert.equal(JSON.parse(build.settings.body).deep,false);
  assert.equal(f.calls.filter(call=>call.url.endsWith('/models')).length,0);
  f.controller.destroy();
});

test('API enrichment permits retrieval toggles while keeping delete and reconstruction locked', async () => {
  const f=await fixture(running({kind:'api_enrichment',stage:'deep_api',progress:{completed:2,total:10,unit:'cases'}}));
  assert.equal(f.get('.lp-library-enable').disabled,false);
  assert.equal(f.get('.lp-library-delete').disabled,true);
  assert.equal(f.get('.lp-build-submit').disabled,true);
  assert.match(f.get('.lp-library-action-help').textContent,/可正常启停检索/);
  assert.match(f.get('.lp-progress-stage').textContent,/Deep API/);
  await f.get('.lp-library-enable').emit('click');
  assert.equal(f.api.snapshot.libraries.find(library=>library.id==='models').enabled,false);
  assert.match(f.get('.lp-library-action-help').textContent,/当前已关闭/);
  assert.equal(f.get('.lp-library-enable').disabled,false);
  assert.equal(f.get('.lp-cancel-build').disabled,false);
  f.controller.destroy();
});


test('an old native-only manifest never creates a ready QMD index or hides QMD build choices', async () => {
  const f = await fixture(null);
  f.api.snapshot.indexes = {modes: {native: {ready: true}}};
  await f.controller.refresh(); f.controller.selectLibrary('models'); await flush();
  const modes = f.get('.lp-mode-choices').querySelectorAll('input');
  assert.deepEqual(modes.map(input => input.value), ['qmd_keyword', 'qmd_vector']);
  assert.deepEqual(modes.filter(input => input.checked).map(input => input.value), ['qmd_keyword']);
  assert.ok(f.get('.lp-indexes').textContent.includes('尚无此模式的可用索引'));
  assert.ok(!f.get('.lp-indexes').textContent.includes('原生层级检索'));
  f.controller.destroy();
});
