import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const script = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const core = script.slice(0, script.indexOf('$("intake-form").addEventListener("submit", createSession);'));
const all = node => [node, ...node.children.flatMap(all)];
const matching = (node, className) => all(node).filter(item => item.className.split(/\s+/).includes(className));
class Node {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.className = ''; this.children = []; this.listeners = new Map(); this.dataset = {}; this.attributes = new Map(); this._text = ''; this.open = false; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_value) { throw Error('Setting values must be inserted as text'); }
  get childElementCount() { return this.children.length; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  emit(name) { return this.listeners.get(name)?.({target: this}); }
  click() { if (!this.disabled) return this.emit('click'); }
  querySelector(selector) { return all(this).slice(1).find(item => selector.startsWith('.') ? item.className.split(/\s+/).includes(selector.slice(1)) : item.tagName === selector.toUpperCase()) || null; }
}
function fixture(session) {
  const nodes = new Map(), requests = [], openedSources = [];
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  const context = vm.createContext({URL, URLSearchParams, AbortController, setTimeout, clearTimeout, console,
    document: {getElementById: get, createElement: tag => new Node(tag)},
    fetch(path) { requests.push(path); throw Error(`Rendering must not request ${path}`); }});
  vm.runInContext(core, context);
  context.session = session; context.openedSources = openedSources;
  vm.runInContext('state.session = session; showSource = id => openedSources.push(id);', context);
  return {get, requests, openedSources, run: code => vm.runInContext(code, context),
    render() { vm.runInContext('renderSimulationFlow($("flow"), state.session.plan.steps)', context); return get('flow'); }};
}
const session = () => ({id: 'plan', revision: 2, sources: [{id: 'manual', title: 'Manual'}, {id: 'closed', title: 'Closed case', availability: {readable: false}}],
  plan: {steps: [{id: 'physics', title: '物理设置'}, {id: 'mesh', title: '网格设置'}, {id: 'study', title: '研究'}]}});
const setting = (overrides = {}) => ({id: 'pressure', step_ids: ['physics'], requirement: '读取压降', setting: '入口压力', proposed_value: '2 Pa', status: 'adapted',
  source_refs: [{source_id: 'manual', context: 'source_text', locator: 'p. 2', stated_value: '1 Pa'}],
  transfer_rationale: '入口工况不同', reason: '当前需求', gap: '仍需核对单位', ...overrides});
const open = details => { details.open = true; details.emit('toggle'); };

test('settings stay collapsed within their matching steps and render only once when opened', () => {
  const current = session(); current.plan_settings = [setting(), setting({id: 'size', step_ids: ['mesh'], setting: '网格上限', proposed_value: '0.2 mm'}), setting({id: 'unlinked', step_ids: ['absent'], setting: '不得显示'})];
  const f = fixture(current), flow = f.render(), steps = matching(flow, 'flow-node'), disclosures = matching(flow, 'flow-settings');
  assert.equal(steps.length, 3); assert.equal(disclosures.length, 2);
  assert.equal(matching(steps[2], 'flow-settings').length, 0);
  assert.ok(matching(flow, 'flow-node-details').every(node => !node.open)); assert.ok(disclosures.every(node => !node.open));
  assert.equal(matching(flow, 'plan-setting').length, 0); assert.doesNotMatch(flow.textContent, /入口压力|网格上限|不得显示/);
  open(disclosures[0]);
  assert.match(steps[0].textContent, /入口压力.*调整采用.*读取压降.*2 Pa.*入口工况不同.*当前需求.*仍需核对单位/s);
  assert.match(steps[0].textContent, /Agent.*不代表已实施或已完成物理验证/s);
  assert.doesNotMatch(steps[0].textContent, /网格上限|不得显示/); assert.doesNotMatch(steps[1].textContent, /入口压力|2 Pa/);
  open(disclosures[1]); open(disclosures[0]); open(disclosures[1]);
  assert.equal(matching(flow, 'plan-setting').length, 2, 'Reopening never duplicates the table in step cards');
  assert.doesNotMatch(flow.textContent, /不得显示/); assert.deepEqual(f.requests, []);
});

test('unmapped settings are available below the flow on demand without repeating any mapped row', () => {
  const current = session(); current.plan_settings = [setting({step_ids: ['physics', 'missing'], setting: '仅当前步骤'}),
    setting({id: 'alternative', step_ids: [], setting: '备选配置', status: 'alternative'}),
    setting({id: 'unresolved', step_ids: [], setting: '待处理值', status: 'unresolved'}),
    setting({id: 'not-applicable', step_ids: [], setting: '不适用配置', status: 'not_applicable'}),
    setting({id: 'obsolete', step_ids: ['previous-step'], setting: '旧环节记录', status: 'alternative'})];
  const f = fixture(current), flow = f.render(), steps = matching(flow, 'flow-node'), unmapped = matching(flow, 'flow-unmapped-settings');
  assert.equal(unmapped.length, 1); assert.equal(unmapped[0].open, false);
  assert.match(unmapped[0].textContent, /^未采用与待处理设置（4 项）$/);
  assert.ok(flow.children.indexOf(unmapped[0]) > flow.children.indexOf(matching(flow, 'simulation-flow')[0]), 'Disclosure follows the workflow');
  assert.equal(matching(unmapped[0], 'plan-setting').length, 0); assert.doesNotMatch(flow.textContent, /备选配置|待处理值|不适用配置|旧环节记录/);
  open(unmapped[0]);
  assert.match(unmapped[0].textContent, /备选配置.*备选.*待处理值.*待明确.*不适用配置.*不适用.*旧环节记录/s);
  assert.doesNotMatch(unmapped[0].textContent, /仅当前步骤/); assert.ok(steps.every(step => !/备选配置|待处理值|不适用配置|旧环节记录/.test(step.textContent)));
  open(matching(steps[0], 'flow-settings')[0]); open(unmapped[0]);
  assert.equal(matching(flow, 'plan-setting').length, 5); assert.match(steps[0].textContent, /仅当前步骤/); assert.deepEqual(current.plan_settings[0].step_ids, ['physics', 'missing']);
  assert.deepEqual(f.requests, []); assert.deepEqual(f.openedSources, []);
});

test('a plan without steps still retains its unmapped settings in a collapsed disclosure', () => {
  const current = session(); current.plan.steps = []; current.plan_settings = [setting({step_ids: [], status: 'unresolved', setting: '待补工况'})];
  const f = fixture(current), flow = f.render(), unmapped = matching(flow, 'flow-unmapped-settings')[0];
  assert.match(flow.textContent, /尚未形成实施流程/); assert.ok(unmapped); assert.equal(unmapped.open, false); assert.doesNotMatch(flow.textContent, /待补工况/);
  open(unmapped); assert.match(unmapped.textContent, /待补工况.*待明确/s); assert.deepEqual(f.requests, []);
});

test('all setting and evidence fields stay text, while only available known sources use the existing viewer', () => {
  const payload = '<img src=x onerror="globalThis.pwned=true">', current = session();
  current.plan_settings = [setting({requirement: payload, setting: payload, proposed_value: {value: payload}, transfer_rationale: payload, reason: payload, gap: payload,
    source_refs: [{source_id: 'manual', context: 'source_text', locator: payload, stated_value: payload}, {source_id: 'closed', context: 'case_configuration', locator: 'Root > inlet', stated_value: 0}, {source_id: 'javascript:alert(1)', context: 'navigation', locator: payload, stated_value: false}]})];
  const f = fixture(current), flow = f.render(); open(matching(flow, 'flow-settings')[0]);
  const row = matching(flow, 'plan-setting')[0]; assert.ok(row.textContent.split(payload).length >= 8);
  assert.match(row.textContent, /文档原文.*案例配置.*导航线索/s); assert.match(row.textContent, /来源记载值0/); assert.match(row.textContent, /来源记载值false/);
  assert.match(row.textContent, /来源已关闭或删除/); assert.match(row.textContent, /javascript:alert\(1\)/);
  assert.ok(all(row).every(node => !['IMG', 'SCRIPT', 'A', 'IFRAME'].includes(node.tagName)));
  const citations = matching(row, 'citation'); assert.equal(citations.length, 1); assert.deepEqual(f.openedSources, []); assert.deepEqual(f.requests, []);
  citations[0].click(); assert.deepEqual(f.openedSources, ['manual']); assert.deepEqual(f.requests, []);
});

test('all adoption states are clearly labelled without changing legacy flows', () => {
  const current = session(), f = fixture(current), baseline = f.render().textContent;
  f.get('flow').replaceChildren(); current.plan_settings = []; assert.equal(f.render().textContent, baseline);
  assert.equal(matching(f.get('flow'), 'flow-settings').length, 0);
  current.plan_settings = ['adopted', 'adapted', 'alternative', 'unresolved', 'not_applicable'].map((status, index) => setting({id: String(index), status}));
  f.get('flow').replaceChildren(); const flow = f.render(); open(matching(flow, 'flow-settings')[0]);
  assert.match(flow.textContent, /方案采用.*调整采用.*备选.*待明确.*不适用/s); assert.equal(matching(flow, 'plan-setting').length, 5); assert.deepEqual(f.requests, []);
});

test('historical preview uses the selected snapshot settings and sources without changing the current version', () => {
  const current = session(); current.plan_settings = [setting({setting: '当前设置'})];
  const historical = session(); historical.revision = 1; historical.plan_settings = [setting({setting: '历史设置', source_refs: [{source_id: 'archived', context: 'case_configuration', locator: 'saved node', stated_value: '1 Pa'}]}), setting({step_ids: [], setting: '历史未采用设置', status: 'alternative'})];
  historical.sources = [{id: 'archived', title: 'Historical case', availability: {readable: false}}];
  const f = fixture(current); f.run(`renderVersionPreview(${JSON.stringify(historical)})`);
  const preview = f.get('version-preview'); open(matching(preview, 'flow-settings')[0]);
  assert.match(preview.textContent, /历史设置.*Historical case.*saved node/s); assert.doesNotMatch(preview.textContent, /当前设置/);
  const unmapped = matching(preview, 'flow-unmapped-settings')[0]; assert.equal(unmapped.open, false); open(unmapped); assert.match(unmapped.textContent, /历史未采用设置/); assert.doesNotMatch(unmapped.textContent, /当前设置|历史设置/);
  assert.equal(f.run('state.session.revision'), 2); assert.deepEqual(f.requests, []);
  assert.equal(matching(matching(preview, 'plan-setting')[0], 'citation').length, 0);
});
