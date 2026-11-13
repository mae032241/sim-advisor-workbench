import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const script = readFileSync(new URL('../web/case-guide.js', import.meta.url), 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = new Map(); this.open = false; }
  set textContent(text) { this.text = String(text); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_) { throw Error('Untrusted case text must not be HTML'); }
  append(...children) { children.forEach(child => {child.parent = this; this.children.push(child);}); }
  replaceChildren() { this.children = []; this.text = ''; }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  async fire(name) { return this.listeners.get(name)?.(); }
}
const all = node => [node, ...node.children.flatMap(all)];
function fixture(respond) {
  const requests = [], opened = [], container = new Element('section'), window = {};
  const context = vm.createContext({window, document: {createElement: tag => new Element(tag)}, AbortController});
  vm.runInContext(script, context);
  const view = window.SimAdvisorCaseGuide.mount(container, {
    call: async (name, args, signal) => {requests.push({name, args, signal}); return respond(name, args);},
    openSource: id => opened.push(id),
  });
  return {view, requests, container, opened};
}
const guide = {state: 'ready', gaps: [{detail: 'not physically verified'}], stages: [{
  id: 'mesh', title: '网格', observed: [{hint: '<img src=x onerror=bad()>', basis: 'saved_configuration'}],
  source_refs: [{id: 'lo-mesh', title: 'Mesh Size'}],
  configuration_requests: [{case_id: 'lo-case', category: 'mesh', node_path: 'compact:node/3', source: 'saved', limit: 1}],
  guidance: [{text: 'Check target-quantity convergence'}], gaps: [],
}]};
test('opening a source never fetches navigation/configuration until an explicit action', async () => {
  const f = fixture(name => name === 'get_case_guide' ? guide : {returned: 1});
  await f.view.show('lo-case'); assert.equal(f.requests.length, 0);
  await all(f.container).find(node => node.tag === 'button').fire('click');
  assert.deepEqual(f.requests.map(item => item.name), ['get_case_guide']);
  const stages = all(f.container).filter(node => node.className?.includes('case-guide-node'));
  assert.equal(stages.length, 1); assert.equal(stages[0].open, false);
  assert.match(f.container.textContent, /<img src=x onerror=bad\(\)>/);
  const config = all(f.container).find(node => node.className === 'case-guide-configuration');
  config.open = true; await config.fire('toggle'); await config.fire('toggle');
  assert.deepEqual(f.requests.map(item => item.name), ['get_case_guide', 'read_case_configuration']);
  assert.equal(f.requests[1].args.node_path, 'compact:node/3');
  await all(f.container).find(node => node.tag === 'button' && node.textContent === 'Mesh Size').fire('click');
  assert.deepEqual(f.opened, ['lo-mesh']);
});
test('closing or replacing a source drops pending navigation without polling or stale content', async () => {
  let finish; const f = fixture(() => new Promise(resolve => {finish = resolve;}));
  await f.view.show('lo-old');
  const pending = all(f.container).find(node => node.tag === 'button').fire('click');
  f.view.reset(); assert.equal(f.requests[0].signal.aborted, true);
  finish(guide); await pending;
  assert.equal(f.container.hidden, true); assert.equal(f.container.children.length, 0);
  assert.equal(f.requests.length, 1);
});
test('unprepared case stays explicit instead of showing another case or making fallback searches', async () => {
  const f = fixture(() => ({state: 'not_prepared', detail: '尚未准备导航'}));
  await f.view.show('lo-unprepared'); await all(f.container).find(node => node.tag === 'button').fire('click');
  assert.match(f.container.textContent, /尚未准备导航/); assert.equal(f.requests.length, 1);
  assert.equal(all(f.container).some(node => node.className === 'case-guide-flow'), false);
});

test('flow overview fetches only the stage that is expanded and never refetches on reopening', async () => {
  const f = fixture((_name, args) => args.stage ? guide : {
    state: 'ready', stages: [{id: 'mesh', title: '网格', detail_available: true, source_count: 1, key_hint: 'Saved Size'}],
  });
  await f.view.show('lo-case'); await all(f.container).find(node => node.tag === 'button').fire('click');
  assert.equal(f.requests.length, 1);
  const stage = all(f.container).find(node => node.className?.includes('case-guide-node'));
  stage.open = true; await stage.fire('toggle'); await stage.fire('toggle');
  assert.equal(f.requests.length, 2);
  assert.deepEqual(JSON.parse(JSON.stringify(f.requests[1].args)), {case_id: 'lo-case', stage: 'mesh'});
  assert.equal(all(f.container).filter(node => node.className === 'case-guide-configuration').length, 1);
  assert.equal(f.requests.some(item => item.name === 'read_case_configuration'), false);
});
