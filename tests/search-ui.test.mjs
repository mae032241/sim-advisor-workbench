import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const script = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const core = script.slice(0, script.indexOf('$("intake-form").addEventListener("submit", createSession);'));
const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
class Node {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.listeners = new Map(); this.value = ''; this.open = false; this.hidden = false; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_) { throw Error('Source metadata must remain text'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  showModal() { this.open = true; }
  focus() {}
  setAttribute(name, value) { this[name] = String(value); }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  async click() { return await this.listeners.get('click')?.(); }
}
function fixture(sources = {}, respond = null) {
  const nodes = new Map(), requests = [];
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  const context = vm.createContext({document: {getElementById: get, createElement: tag => new Node(tag)}, URL, URLSearchParams, AbortController, setTimeout, clearTimeout, console,
    fetch: async (path, options) => {
      requests.push({path, options});
      const id = path.startsWith('/api/sources/') ? decodeURIComponent(path.slice('/api/sources/'.length)) : null;
      const body = respond ? respond(path, options) : id ? sources[id] : {results: []};
      return {ok: Boolean(body), status: body ? 200 : 404, json: async () => body || {error: 'not found'}};
    }});
  vm.runInContext(core, context);
  get('search-kind').value = 'all';
  return {get, requests, run: code => vm.runInContext(code, context)};
}
const nodesOf = node => [node, ...node.children.flatMap(nodesOf)];

test('branch selector defaults to unrestricted and exposes all canonical categories', () => {
  const select = html.match(/<select id="search-feature-branch">([\s\S]*?)<\/select>/)?.[1];
  assert.ok(select);
  const options = [...select.matchAll(/<option value="([^"]*)">/g)].map(match => match[1]);
  assert.equal(options[0], '');
  assert.equal(options.length, 14);
  assert.ok(options.includes('multiphysics') && options.includes('func') && options.includes('variable'));
  assert.doesNotMatch(select, /<\/option\s/);
});

test('search only sends an explicitly selected branch and never starts builds', async () => {
  const f = fixture();
  f.get('search-query').value = 'boundary layer';
  await f.run('searchSources({preventDefault() {}})');
  assert.equal(new URL(f.requests[0].path, 'http://localhost').searchParams.has('feature_branch'), false);
  f.get('search-kind').value = 'code'; f.get('search-feature-branch').value = 'mesh';
  await f.run('searchSources({preventDefault() {}})');
  const params = new URL(f.requests[1].path, 'http://localhost').searchParams;
  assert.equal(params.get('kind'), 'code'); assert.equal(params.get('feature_branch'), 'mesh');
  assert.ok(f.requests.every(request => request.path.startsWith('/api/search?') && !request.options.method));
});

test('case discovery uses the overview tool and ignores a leftover branch filter', async () => {
  const f = fixture({}, () => ({result: {results: []}}));
  f.get('search-query').value = 'potato drying';
  f.get('search-kind').value = 'case'; f.get('search-feature-branch').value = 'mesh';
  await f.run('searchSources({preventDefault() {}})');
  assert.equal(f.requests.length, 1); assert.equal(f.requests[0].path, '/api/tools/call');
  assert.deepEqual(JSON.parse(f.requests[0].options.body), {name: 'search_cases', arguments: {query: 'potato drying'}});
});

test('related references stay summaries until explicitly opened, and unresolved hints are never links', async () => {
  const f = fixture({
    'lo-main': {id: 'lo-main', title: 'Main source', text: 'full source body', related_sources: [
      {id: 'lo-tutorial', title: 'Mesh tutorial', relation: 'documented_by', status: 'candidate_same_branch', alignment: 'not_verified', feature_branch: 'mesh'},
      {title: 'missing function', relation: 'requires', status: 'unresolved'},
    ]},
    'lo-tutorial': {id: 'lo-tutorial', title: 'Tutorial', text: 'Explicitly opened tutorial body', related_sources: []},
  });
  await f.run('showSource("lo-main")');
  assert.equal(f.requests.length, 1);
  const related = f.get('source-related'), buttons = nodesOf(related).filter(node => node.tag === 'button');
  assert.equal(buttons.length, 1);
  assert.match(related.textContent, /同类候选，未核对对齐/);
  assert.match(related.textContent, /未解析，待查证/);
  assert.match(related.textContent, /未证明已阅读、可执行/);
  await buttons[0].click();
  assert.equal(f.requests[1].path, '/api/sources/lo-tutorial');
  assert.match(f.get('source-body').textContent, /Explicitly opened/);
  assert.equal(f.get('source-related').hidden, true);
  assert.ok(f.requests.every(request => request.path.startsWith('/api/sources/') && !request.options.method));
});

test('related lookup warnings leave full body readable and metadata cannot execute markup', async () => {
  const f = fixture({'lo-x': {id: 'lo-x', title: 'Source', text: 'intact full body', related_sources_warning: '关联来源摘要暂不可读取', related_sources: [{title: '<script>bad()</script>', status: 'unresolved', relation: 'requires'}]}});
  await f.run('showSource("lo-x")');
  assert.equal(f.get('source-body').textContent, 'intact full body');
  assert.match(f.get('source-related').textContent, /<script>bad\(\)<\/script>/);
  assert.match(f.get('source-related').textContent, /关联来源摘要暂不可读取/);
  assert.equal(nodesOf(f.get('source-related')).some(node => node.tag === 'button'), false);
});

test('source body stays separate from technical JSON, related sources precede a reset closed disclosure', async () => {
  const evidence = [{method: 'provided_source', locator: 'lines:1-5'}], coverage = {limitations: ['<script>never run()</script>'], largeDetail: 'x'.repeat(8000)};
  const f = fixture({
    'lo-a': {id: 'lo-a', text: 'model.mesh().create("mesh1");', evidence, coverage, related_sources: [{id: 'lo-b', title: 'Dependency', relation: 'requires', status: 'source_relation'}]},
    'lo-b': {id: 'lo-b', text: 'second body', evidence: [{locator: 'lines:8-9'}], related_sources: []},
    'lo-c': {id: 'lo-c', excerpt: 'candidate excerpt is not full body', related_sources: []},
  });
  await f.run('showSource("lo-a")');
  assert.equal(f.get('source-body').textContent, 'model.mesh().create("mesh1");');
  assert.equal(f.get('source-technical').open, false);
  assert.equal(f.get('source-technical').hidden, false);
  assert.equal(f.get('source-technical-body').textContent, '', 'Technical JSON is not built before expansion');
  f.get('source-technical').open = true; f.run('renderSourceTechnical()');
  assert.match(f.get('source-technical-body').textContent, /provided_source/);
  assert.match(f.get('source-technical-body').textContent, /<script>never run\(\)<\/script>/);
  assert.match(f.get('source-technical-body').textContent, new RegExp('x'.repeat(8000)));
  assert.ok(html.indexOf('id="source-body"') < html.indexOf('id="source-related"'));
  assert.ok(html.indexOf('id="source-related"') < html.indexOf('id="source-technical"'));
  assert.match(html, /<summary>来源与提取覆盖详情<\/summary>/);
  f.get('source-technical').open = true;
  await f.run('showSource("lo-b")');
  assert.equal(f.get('source-technical').open, false);
  assert.doesNotMatch(f.get('source-technical-body').textContent, /largeDetail|provided_source/);
  await f.run('showSource("lo-c")');
  assert.equal(f.get('source-technical').hidden, true);
  assert.doesNotMatch(f.get('source-body').textContent, /candidate excerpt/);
  assert.match(html, /所有建模分支/);
  assert.doesNotMatch(html, /所有功能分支/);
});

test('reading a different source aborts the previous read and late content cannot overwrite the new source', async () => {
  let release; const pending = new Promise(resolve => { release = resolve; });
  const f = fixture({'lo-old': pending, 'lo-new': {id: 'lo-new', title: 'Current source', text: 'current body'}});
  const old = f.run('showSource("lo-old")'); await Promise.resolve();
  await f.run('showSource("lo-new")'); await old;
  assert.equal(f.requests[0].options.signal.aborted, true);
  release({id: 'lo-old', title: 'Old source', text: 'late old body'}); for (let i = 0; i < 30; i++) await Promise.resolve();
  assert.equal(f.get('source-title').textContent, 'Current source'); assert.equal(f.get('source-body').textContent, 'current body');
});

test('a superseded search is aborted and cannot release the new search busy state or append old results', async () => {
  const releases = new Map(); const f = fixture({}, path => new Promise(resolve => releases.set(new URL(path, 'http://localhost').searchParams.get('q'), resolve)));
  f.get('search-query').value = 'first'; const old = f.run('searchSources({preventDefault(){}})');
  f.get('search-query').value = 'second'; const current = f.run('searchSources({preventDefault(){}})');
  await old; assert.equal(f.requests[0].options.signal.aborted, true); assert.equal(f.get('search-submit').disabled, true);
  releases.get('first')({results: [{id: 'old', title: 'Old hit'}]});
  releases.get('second')({results: [{id: 'new', title: 'Current hit'}]}); await current;
  assert.match(f.get('search-results').textContent, /Current hit/); assert.doesNotMatch(f.get('search-results').textContent, /Old hit/);
  assert.equal(f.get('search-submit').disabled, false);
});

test('unreadable historical sources retain their names and citations without requesting text; old metadata remains clickable', async () => {
  const f = fixture({'lo-old': {id: 'lo-old', title: 'Legacy source', text: 'readable body'}});
  f.run('state.session = {sources: [{id:"lo-closed", title:"Preserved reference", availability:{readable:false}}, {id:"lo-old", title:"Legacy source"}]}; renderSources();');
  const [closed, legacy] = f.get('source-list').children;
  assert.equal(closed.disabled, true); assert.match(closed.textContent, /Preserved reference/); assert.match(closed.textContent, /资料库已关闭或删除，引用记录保留/);
  await closed.click(); const citation = f.run('citationButton("lo-closed")'); assert.equal(citation.disabled, true); await citation.click();
  assert.equal(f.requests.length, 0);
  assert.equal(legacy.disabled, false); await legacy.click(); assert.equal(f.requests.length, 1); assert.equal(f.get('source-body').textContent, 'readable body');
});
