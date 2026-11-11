import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import crypto from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import {createClientModule} from '../config-ui.js';
import {Config} from '../config.js';

const React = {createElement: (type, props, ...children) => ({type, props, children})};
const client = createClientModule(id => { assert.equal(id, 'react'); return React; });
const ok = value => ({ok: true, value});
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

function fixture(options = {}) {
  let snapshot = {status: 'ready', writable: true, revision: 'form-1', value: {offlineBundle: options.root || 'D:/old', dataDir: options.dataDir || ''}};
  let setup = {revision: 1, state: 'ready', bundle_root: snapshot.value.offlineBundle,
    data_dir_setting: snapshot.value.dataDir, data_dir: snapshot.value.dataDir || 'C:/fixture/default-data', workbench: {running: false, owned: false, state: 'stopped', url: null, pid: null, detail: 'Service stopped'}};
  const calls = [], subscriptions = new Set();
  const form = {
    getSnapshot: () => snapshot,
    subscribe(listener) { subscriptions.add(listener); return () => subscriptions.delete(listener); },
    async mutate(operations, revision) {
      calls.push({method: 'mutate', operations, revision});
      if (options.accept === false) return false;
      const value = {...snapshot.value};
      for (const operation of operations) value[operation.path[0]] = operation.value;
      snapshot = {...snapshot, revision: 'form-2', value};
      for (const listener of subscriptions) listener();
      return true;
    },
  };
  const remote = {simWorkbenchSetup: {
    status: async () => { calls.push({method: 'status'}); return ok(setup); },
    configure: async () => {
      calls.push({method: 'configure'});
      setup = {revision: setup.revision + 1, state: 'checking', bundle_root: snapshot.value.offlineBundle,
        data_dir_setting: snapshot.value.dataDir, data_dir: snapshot.value.dataDir || 'C:/fixture/default-data'};
      return ok(setup);
    },
    start: async () => {
      calls.push({method: 'start'});
      const result = options.start ? await options.start() : ok({running: true, owned: true, url: 'http://127.0.0.1:43210'});
      if (result.ok && result.value?.running === true) setup = {...setup, workbench: result.value};
      return result;
    },
    stop: async () => {
      calls.push({method: 'stop'});
      const result = options.stop ? await options.stop() : ok({running: false, state: 'stopped', url: null, pid: null});
      if (result.ok && result.value?.running === false) setup = {...setup, workbench: result.value};
      return result;
    },
    verify: async () => {
      calls.push({method: 'verify'});
      if (options.verify) return await options.verify();
      setup = {...setup, revision: setup.revision + 1, state: 'checking'};
      return ok(setup);
    },
  }};
  const controller = (options.panel ? client.createPanelController : client.createController)(form, remote, {setInterval: () => 1, clearInterval() {}, ...options});
  controller.start();
  return {controller, calls, form, remote, subscriptions,
    setSetup(value) { setup = value; },
    setSnapshot(value) { snapshot = value; for (const listener of subscriptions) listener(); }};
}

test('a hung setup-status RPC times out and permits later status refresh without losing edited directories', async t => {
  const f = fixture({rpcTimeoutMs: 20});
  t.after(() => f.controller.dispose());
  await f.controller.refresh();
  f.controller.editData('E:/keep-draft');
  const original = f.remote.simWorkbenchSetup.status;
  f.remote.simWorkbenchSetup.status = () => new Promise(() => {});
  const outcome = await Promise.race([f.controller.refresh().then(() => 'settled'), pause(100).then(() => 'pending')]);
  assert.equal(outcome, 'settled');
  assert.equal(f.controller.getSnapshot().dataDraft, 'E:/keep-draft');
  f.remote.simWorkbenchSetup.status = original;
  await f.controller.refresh();
  assert.equal(f.controller.getSnapshot().configuration.state, 'ready');
});

test('a hung shared startup RPC releases the launch UI and late success cannot overwrite an explicit retry', async t => {
  let release, calls = 0;
  const gate = new Promise(resolve => { release = resolve; });
  const f = fixture({launchTimeoutMs: 20, start: () => ++calls === 1 ? gate : ok({running: true, url: 'http://127.0.0.1:5555'})});
  t.after(() => { f.controller.dispose(); release(ok({running: true, url: 'http://127.0.0.1:4444'})); });
  await f.controller.refresh();
  const outcome = await Promise.race([f.controller.launch().then(() => 'settled'), pause(100).then(() => 'pending')]);
  assert.equal(outcome, 'settled');
  assert.equal(f.controller.getSnapshot().launchPhase, 'error');
  assert.equal(calls, 1);
  await f.controller.launch();
  assert.equal(f.controller.getSnapshot().workbenchUrl, 'http://127.0.0.1:5555/');
  release(ok({running: true, url: 'http://127.0.0.1:4444'})); await pause(5);
  assert.equal(f.controller.getSnapshot().workbenchUrl, 'http://127.0.0.1:5555/');
});

for (const stage of ['mutation', 'configure']) {
  test(`a hung configuration ${stage} times out without replaying writes`, async t => {
    const f = fixture({rpcTimeoutMs: 20});
    t.after(() => f.controller.dispose());
    await f.controller.refresh();
    f.controller.edit('D:/new-root');
    let attempts = 0;
    if (stage === 'mutation') f.form.mutate = () => { attempts++; return new Promise(() => {}); };
    else f.remote.simWorkbenchSetup.configure = () => { attempts++; return new Promise(() => {}); };
    const outcome = await Promise.race([f.controller.save().then(() => 'settled'), pause(100).then(() => 'pending')]);
    assert.equal(outcome, 'settled');
    assert.equal(f.controller.getSnapshot().phase, 'error');
    assert.match(f.controller.getSnapshot().message, /超时.*结果待核对|超时.*后台可能仍在校验/);
    await f.controller.refresh();
    assert.equal(attempts, 1);
    assert.equal(f.controller.getSnapshot().draft, 'D:/new-root');
  });
}

test('equal ready polls keep snapshot identity and listeners quiet while the timer still requests status', async () => {
  let tick, delay;
  const f = fixture({setInterval(callback, ms) { tick = callback; delay = ms; return 1; }});
  const info = {revision: 1, state: 'ready', bundle_root: 'D:/old', data_dir_setting: '', data_dir: 'C:/fixture/default-data',
    checks: {python: {ready: true, paths: ['python.exe', 'Lib']}, errors: []}};
  f.setSetup(info); await f.controller.refresh();
  const snapshot = f.controller.getSnapshot(), configuration = snapshot.configuration;
  let notifications = 0;
  f.controller.subscribe(() => notifications++);
  const before = f.calls.filter(call => call.method === 'status').length;
  assert.equal(delay, 1500);
  for (let i = 0; i < 5; i++) {
    // Every RPC returns a fresh object; JSON key ordering is not a change.
    f.setSetup({checks: {errors: [], python: {paths: ['python.exe', 'Lib'], ready: true}}, data_dir: info.data_dir,
      data_dir_setting: '', bundle_root: 'D:/old', state: 'ready', revision: 1});
    await tick();
    assert.equal(f.controller.getSnapshot(), snapshot);
    assert.equal(f.controller.getSnapshot().configuration, configuration);
  }
  assert.equal(f.calls.filter(call => call.method === 'status').length, before + 5);
  assert.equal(notifications, 0);
  f.controller.dispose();
});

test('same dirty input and unchanged form/status callbacks do not notify or replace the draft snapshot', async () => {
  const f = fixture(); await f.controller.refresh();
  f.controller.edit('D:/editing');
  f.controller.editData('E:/editing-data');
  const snapshot = f.controller.getSnapshot();
  let notifications = 0;
  f.controller.subscribe(() => notifications++);
  f.controller.edit('D:/editing'); f.controller.editData('E:/editing-data');
  f.setSnapshot({...f.form.getSnapshot(), value: {...f.form.getSnapshot().value}});
  for (let i = 0; i < 3; i++) {
    f.setSetup({revision: 1, state: 'ready', bundle_root: 'D:/old', data_dir_setting: '', data_dir: 'C:/fixture/default-data'});
    await f.controller.refresh();
  }
  assert.equal(f.controller.getSnapshot(), snapshot);
  assert.equal(f.controller.getSnapshot().phase, 'dirty');
  assert.equal(f.controller.getSnapshot().draft, 'D:/editing');
  assert.equal(notifications, 0);
  f.controller.dispose();
});

test('actual revision nested status error and launch changes still notify', async () => {
  const f = fixture(); await f.controller.refresh();
  let notifications = 0;
  f.controller.subscribe(() => notifications++);
  let previous = f.controller.getSnapshot();
  await f.controller.launch();
  assert.ok(notifications > 0);
  assert.notEqual(f.controller.getSnapshot(), previous);
  assert.equal(f.controller.getSnapshot().launchPhase, 'running');
  previous = f.controller.getSnapshot(); notifications = 0;
  await f.controller.refresh();
  assert.equal(f.controller.getSnapshot(), previous);
  assert.equal(notifications, 0);
  f.setSetup({revision: 2, state: 'ready', bundle_root: 'D:/old', data_dir_setting: '', data_dir: 'C:/fixture/default-data'});
  await f.controller.refresh();
  assert.ok(notifications > 0);
  assert.equal(f.controller.getSnapshot().configuration.revision, 2);
  assert.equal(f.controller.getSnapshot().workbenchUrl, null);
  previous = f.controller.getSnapshot(); notifications = 0;
  f.setSetup({revision: 2, state: 'error', bundle_root: 'D:/old', data_dir_setting: '', data_dir: 'C:/fixture/default-data', detail: {message: '实际配置错误', causes: ['invalid checksum']}});
  await f.controller.refresh();
  assert.ok(notifications > 0);
  assert.notEqual(f.controller.getSnapshot(), previous);
  assert.match(f.controller.getSnapshot().message, /实际配置错误/);
  previous = f.controller.getSnapshot(); notifications = 0;
  f.setSetup({detail: {causes: ['invalid checksum'], message: '实际配置错误'}, data_dir: 'C:/fixture/default-data',
    data_dir_setting: '', bundle_root: 'D:/old', state: 'error', revision: 2});
  await f.controller.refresh();
  assert.equal(f.controller.getSnapshot(), previous);
  assert.equal(notifications, 0);
  f.setSetup({...previous.configuration, detail: {message: '实际配置错误', causes: ['new failure']}});
  await f.controller.refresh();
  assert.ok(notifications > 0);
  f.controller.dispose();
});

test('Config uses genuine vendored Schemastery and preserves ordinary config fields', () => {
  const parsed = Config({offlineBundle: 'D:/offline data', dataDir: 'D:/workbench data', startupTimeoutMs: 1200});
  assert.equal(parsed.offlineBundle.get(), 'D:/offline data');
  assert.equal(parsed.dataDir.get(), 'D:/workbench data');
  assert.equal(Config.dict.offlineBundle.meta.volatile, true);
  assert.equal(Config.dict.dataDir.meta.volatile, true);
  assert.equal(Config({}).dataDir.get(), '');
  assert.equal(Config.dict.pythonPath.meta.volatile, undefined);
  assert.equal(parsed.pythonPath, '');
  assert.throws(() => Config({startupTimeoutMs: 999}));
  assert.throws(() => Config({startupTimeoutMs: 1000.5}));
  assert.throws(() => Config({offlineBundle: 123}));
});

test('vendored source and license bytes match the recorded official files', () => {
  const directory = new URL('../vendor/', import.meta.url);
  const inventory = JSON.parse(fs.readFileSync(new URL('VENDOR.json', directory), 'utf8'));
  assert.equal(inventory.files.length, 7);
  for (const file of inventory.files) {
    const data = fs.readFileSync(new URL(file.path, directory));
    assert.equal(data.length, file.bytes);
    assert.equal(crypto.createHash('sha256').update(data).digest('hex'), file.sha256);
  }
});

test('shipped classic browser artifact matches its tested factory and registers exact DSH slots/RPC', async () => {
  let loaded;
  vm.runInNewContext(fs.readFileSync(new URL('../client.js', import.meta.url), 'utf8'), {window: {__ModuleLoader__: {load(value) { loaded = value; }}}});
  assert.equal(loaded.id, '@local/sim-advisor-workbench');
  assert.equal(loaded.factory.toString(), createClientModule.toString());
  const mounted = [], slots = [], navigations = [], types = [];
  await client.apply({inject(_names, callback) { return callback(this); }, remote: {$mount: value => mounted.push(value)},
    effect: callback => callback(),
    sidebarRightTabs: {register: definition => { types.push(definition); return () => {}; }},
    sidebarRight: {},
    layout: {selectPanel: id => navigations.push(id)},
    configForms: {get: name => { assert.equal(name, 'sim-advisor-workbench'); return {}; }},
    slots: {inject: (name, callback) => { assert.ok(['plugins.row.config', 'main', 'sidebar.panellist', 'sidebar.right.pane.tab'].includes(name)); callback(); }, register: (...args) => slots.push(args)}});
  assert.deepEqual(mounted[0].descriptors.map(value => value.method), ['status', 'configure', 'start', 'stop', 'verify']);
  assert.equal(slots[0][0].key, '@local/sim-advisor-workbench#sim-advisor-workbench');
  assert.equal(slots.find(([meta]) => meta.name === 'main')[0].key, 'sim-advisor-workbench');
  slots.find(([meta]) => meta.name === 'main')[0].inject().onHide();
  assert.deepEqual(navigations, [null]);
  const navigation = slots.find(([meta]) => meta.name === 'sidebar.panellist')[0];
  assert.equal(navigation.id, 'sim-advisor-workbench');
  assert.equal(navigation.label, '仿真工作台');
  assert.equal(slots.some(([meta]) => meta.name === 'sidebar.footer.action'), false, 'Do not restore the duplicate footer entry');
  const dock = slots.find(([meta]) => meta.name === 'sidebar.right.pane.tab')[0];
  assert.equal(dock.key, types[0].id);
  dock.inject().onFullPage();
  assert.deepEqual(navigations, [null, 'sim-advisor-workbench']);
  assert.deepEqual(client.inject, ['slots', 'configForms', 'remote']);
});

test('the ordinary left workbench entry exists without the right dock or a selected conversation', async () => {
  const slots = [];
  await client.apply({remote: {$mount: async () => {}}, layout: {},
    inject(names, callback) { if (!names.includes('sidebarRight')) return callback(this); },
    configForms: {get: () => ({})},
    slots: {inject: (_name, callback) => callback(), register: (...args) => { slots.push(args); return () => {}; }}});
  const main = slots.find(([meta]) => meta.name === 'main')[0];
  const left = slots.find(([meta]) => meta.name === 'sidebar.panellist')[0];
  assert.equal(left.id, main.key);
  assert.equal(left.label, '仿真工作台');
  assert.equal(slots.some(([meta]) => meta.name === 'sidebar.footer.action'), false);
});

test('native picker receives no invented options and cancellation preserves the edited path', async () => {
  const f = fixture({desktop: {async pick(...args) { assert.deepEqual(args, []); return null; }}});
  await f.controller.refresh();
  f.controller.edit('D:/keep me');
  await f.controller.pick();
  assert.equal(f.controller.getSnapshot().draft, 'D:/keep me');
  assert.equal(f.calls.some(call => call.method === 'mutate'), false);
  f.controller.dispose();
});

test('configuration page waits for the real asynchronous Remote namespace mount', async () => {
  let release, registered = false;
  const mounted = new Promise(resolve => { release = resolve; });
  const pending = client.apply({inject(names, callback) { if (!names.includes('sidebarRight')) return callback(this); }, remote: {$mount: () => mounted}, configForms: {get: () => ({})},
    slots: {inject: (_name, callback) => callback(), register: () => { registered = true; }}});
  assert.equal(registered, false);
  release(); await pending;
  assert.equal(registered, true);
});

test('actual row slot reserved form face cannot shadow the ConfigForms controller', async () => {
  const react = {...React, useMemo: build => build(),
    useSyncExternalStore: (_subscribe, snapshot) => snapshot(), useEffect() {}};
  const exports = createClientModule(() => react);
  const scope = {getSnapshot: () => ({status: 'ready', writable: true, value: {offlineBundle: 'D:/offline'}}),
    subscribe: () => () => {}, mutate: async () => true};
  let registration;
  await exports.apply({inject(names, callback) { if (!names.includes('sidebarRight')) return callback(this); }, remote: {$mount: async () => {}}, configForms: {get: () => scope},
    slots: {inject: (_name, callback) => callback(), register: (meta, component) => { if (meta.name === 'plugins.row.config') registration = {meta, component}; }}});
  assert.equal(registration.meta.inject().formScope, scope);
  // This is the distinct official slot shape observed in the actual DSH GUI.
  const reservedForm = {state: scope.getSnapshot(), mutate: () => { throw Error('unexpected reserved-face invocation'); }};
  const page = registration.component({...registration.meta.inject(), form: reservedForm, view: 'page'});
  assert.doesNotThrow(() => page.type(page.props));
  assert.equal(page.type(page.props).type, 'section');
});

test('new Remote namespace is consumed only through post-mount nested injection', async () => {
  let mounted = false, registration;
  const setup = {status: async () => ok({state: 'unconfigured', revision: 0})};
  const rootRemote = {$mount: async () => { mounted = true; }};
  Object.defineProperty(rootRemote, 'simWorkbenchSetup', {get() { throw Error('cannot get property remote.simWorkbenchSetup without inject'); }});
  Object.defineProperty(rootRemote, 'directoryPicker', {get() { throw Error('cannot get property remote.directoryPicker without inject'); }});
  const scoped = {remote: {simWorkbenchSetup: setup},
    configForms: {get: () => ({})},
    inject(names) { assert.ok(names.includes('remote.directoryPicker') || names.includes('sidebarRight')); /* optional services absent */ },
    slots: {inject: (_name, callback) => { callback(); return new Promise(() => {}); }, register: (meta, component) => { if (meta.name === 'plugins.row.config') registration = {meta, component}; }}};
  await client.apply({remote: rootRemote, inject(names, callback) {
    assert.equal(mounted, true);
    assert.ok(names.includes('remote.simWorkbenchSetup'));
    const returned = callback(scoped);
    assert.equal(returned, undefined, 'Parent apply must not await a slot child Fiber');
    return returned;
  }});
  assert.equal(registration.meta.inject().remote.simWorkbenchSetup, setup);
  assert.equal(registration.meta.inject().remote.directoryPicker, undefined);
});

test('web picker uses the actual RemoteResult contract; an unavailable picker permits manual entry', async () => {
  assert.equal(await client.pickDirectory({directoryPicker: {pick: async () => ok('D:/chosen folder')}}, null), 'D:/chosen folder');
  await assert.rejects(client.pickDirectory({}, null), /绝对路径/);
});

test('saving triggers configure and stays checking until the exact root gets a new ready revision', async () => {
  const f = fixture(); await f.controller.refresh();
  f.controller.edit('D:/new offline');
  await f.controller.save();
  assert.equal(f.controller.getSnapshot().phase, 'checking');
  assert.deepEqual(f.calls.find(call => call.method === 'mutate'), {method: 'mutate', operations: [{op: 'set', path: ['offlineBundle'], value: 'D:/new offline'}, {op: 'set', path: ['dataDir'], value: ''}], revision: 'form-1'});
  assert.equal(f.calls.filter(call => call.method === 'configure').length, 1);
  f.setSetup({revision: 3, state: 'ready', bundle_root: 'D:/different'}); await f.controller.refresh();
  assert.equal(f.controller.getSnapshot().phase, 'checking');
  f.setSetup({revision: 1, state: 'ready', bundle_root: 'D:/new offline'}); await f.controller.refresh();
  assert.equal(f.controller.getSnapshot().phase, 'checking');
  f.setSetup({revision: 0, state: 'ready', bundle_root: 'D:/new offline'}); await f.controller.refresh();
  assert.equal(f.controller.getSnapshot().phase, 'checking');
  f.setSetup({revision: 2, state: 'ready', bundle_root: 'd:\\NEW OFFLINE\\'}); await f.controller.refresh();
  assert.equal(f.controller.getSnapshot().phase, 'ready');
  f.controller.dispose(); assert.equal(f.subscriptions.size, 0);
});

test('backend failure remains visible and the same saved directory can be retried', async () => {
  const f = fixture(); await f.controller.refresh();
  f.controller.edit('D:/offline'); await f.controller.save();
  f.setSetup({revision: 2, state: 'error', bundle_root: 'D:/offline', detail: '模型校验失败'});
  await f.controller.refresh();
  assert.equal(f.controller.getSnapshot().phase, 'error');
  assert.match(f.controller.getSnapshot().message, /模型校验失败/);
  await f.controller.save();
  assert.equal(f.calls.filter(call => call.method === 'configure').length, 2);
  assert.equal(f.controller.getSnapshot().phase, 'checking');
  f.setSetup({revision: 3, state: 'ready', bundle_root: 'D:/offline'}); await f.controller.refresh();
  assert.equal(f.controller.getSnapshot().phase, 'ready'); f.controller.dispose();
});

test('rejected form mutation does not initialize or claim success', async () => {
  const f = fixture({accept: false}); await f.controller.refresh();
  f.controller.edit('D:/offline'); await f.controller.save();
  assert.equal(f.controller.getSnapshot().phase, 'error');
  assert.equal(f.calls.some(call => call.method === 'configure'), false); f.controller.dispose();
});

test('read-only configuration cannot invoke mutation or initialization', async () => {
  const f = fixture(); await f.controller.refresh();
  f.setSnapshot({status: 'ready', writable: false, revision: 'locked', value: {offlineBundle: ''}});
  f.controller.edit('D:/offline'); await f.controller.save();
  assert.equal(f.calls.some(call => ['configure', 'mutate'].includes(call.method)), false); f.controller.dispose();
});

test('bundle and data directory are persisted in one atomic mutation before no-argument configure', async () => {
  const f = fixture(); await f.controller.refresh();
  f.controller.edit('D:/offline new'); f.controller.editData('  E:/workspace data  ');
  await f.controller.save();
  const mutations = f.calls.filter(call => call.method === 'mutate');
  assert.equal(mutations.length, 1);
  assert.deepEqual(mutations[0].operations, [
    {op: 'set', path: ['offlineBundle'], value: 'D:/offline new'},
    {op: 'set', path: ['dataDir'], value: 'E:/workspace data'},
  ]);
  assert.equal(f.calls.filter(call => call.method === 'configure').length, 1);
  assert.equal(f.form.getSnapshot().value.dataDir, 'E:/workspace data');
  assert.equal(f.controller.getSnapshot().dataDraft, 'E:/workspace data');
  assert.equal(f.controller.getSnapshot().phase, 'checking');
  f.setSetup({revision: 2, state: 'ready', bundle_root: 'D:/offline new',
    data_dir_setting: 'E:/workspace data', data_dir: 'E:\\workspace data'});
  await f.controller.refresh();
  assert.equal(f.controller.getSnapshot().phase, 'ready');
  assert.equal(f.controller.canLaunch(), true); f.controller.dispose();
});

test('an empty data setting restores the default and displays the resolved effective path', async () => {
  const f = fixture({dataDir: 'D:/old-data'}); await f.controller.refresh();
  f.controller.editData('   '); await f.controller.save();
  assert.equal(f.form.getSnapshot().value.dataDir, '');
  assert.equal(f.calls.find(call => call.method === 'mutate').operations[1].value, '');
  assert.equal(f.controller.getSnapshot().dataDraft, '');
  f.setSetup({revision: 2, state: 'ready', bundle_root: 'D:/old', data_dir_setting: '', data_dir: 'C:/fixture/default-data'});
  await f.controller.refresh();
  assert.equal(f.controller.getSnapshot().phase, 'ready');
  assert.equal(f.controller.getSnapshot().configuration.data_dir, 'C:/fixture/default-data');
  assert.equal(f.controller.canLaunch(), true); f.controller.dispose();
});

test('changing only data directory rejects old ready even with the same bundle and a new revision', async () => {
  const f = fixture({dataDir: 'D:/old-data'}); await f.controller.refresh();
  f.controller.editData('E:/new-data'); await f.controller.save();
  for (const stale of [
    {revision: 2, data_dir_setting: 'D:/old-data'},
    {revision: 1, data_dir_setting: 'E:/new-data'},
    {revision: 2, data_dir_setting: 'e:/NEW-DATA'},
  ]) {
    f.setSetup({state: 'ready', bundle_root: 'D:/old', data_dir: 'E:/new-data', ...stale});
    await f.controller.refresh();
    assert.equal(f.controller.getSnapshot().phase, 'checking', JSON.stringify(stale));
    assert.equal(f.controller.canLaunch(), false);
  }
  f.setSetup({revision: 2, state: 'ready', bundle_root: 'D:/old', data_dir_setting: 'E:/new-data', data_dir: 'E:/new-data'});
  await f.controller.refresh();
  assert.equal(f.controller.getSnapshot().phase, 'ready');
  assert.equal(f.controller.canLaunch(), true); f.controller.dispose();
});

test('data picker cancellation preserves both drafts and never saves or migrates anything', async () => {
  const f = fixture({dataDir: 'D:/current', desktop: {pick: async () => null}}); await f.controller.refresh();
  f.controller.edit('D:/staged bundle'); f.controller.editData('E:/staged data');
  await f.controller.pick('data');
  assert.equal(f.controller.getSnapshot().draft, 'D:/staged bundle');
  assert.equal(f.controller.getSnapshot().dataDraft, 'E:/staged data');
  assert.equal(f.controller.getSnapshot().picking, false);
  assert.equal(f.calls.some(call => ['mutate', 'configure', 'start'].includes(call.method)), false); f.controller.dispose();
});

test('the data picker edits only the data field and unsaved data changes prohibit launch', async () => {
  const f = fixture({desktop: {pick: async () => 'E:/chosen data'}}); await f.controller.refresh();
  assert.equal(f.controller.canLaunch(), true);
  await f.controller.pick('data');
  assert.equal(f.controller.getSnapshot().draft, 'D:/old');
  assert.equal(f.controller.getSnapshot().dataDraft, 'E:/chosen data');
  assert.equal(f.controller.canLaunch(), false);
  await f.controller.refresh(); await f.controller.launch();
  assert.equal(f.controller.getSnapshot().dataDraft, 'E:/chosen data');
  assert.equal(f.controller.getSnapshot().phase, 'dirty');
  assert.equal(f.calls.some(call => call.method === 'start'), false); f.controller.dispose();
});

test('a saved data setting that differs from initial backend ready remains editable and cannot launch', async () => {
  const f = fixture({dataDir: 'E:/saved-data'});
  f.setSetup({revision: 2, state: 'ready', bundle_root: 'D:/old', data_dir_setting: 'D:/previous', data_dir: 'D:/previous'});
  await f.controller.refresh();
  assert.notEqual(f.controller.getSnapshot().phase, 'ready');
  assert.equal(f.controller.getSnapshot().writable, true);
  assert.equal(f.controller.canLaunch(), false); f.controller.dispose();
});

test('saving UI directory settings neither copies, moves nor removes existing data', async () => {
  const oldDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'sim-ui-data-dir-'));
  const sentinel = path.join(oldDirectory, 'existing-session.json');
  const original = '{"revision":8,"preserve":"original history"}\n';
  fs.writeFileSync(sentinel, original);
  const selected = path.join(oldDirectory, 'new-data-directory-not-created');
  const f = fixture({dataDir: oldDirectory});
  try {
    await f.controller.refresh(); f.controller.editData(selected); await f.controller.save();
    assert.equal(fs.readFileSync(sentinel, 'utf8'), original);
    assert.equal(fs.existsSync(selected), false);
    assert.deepEqual(fs.readdirSync(oldDirectory), ['existing-session.json']);
    assert.ok(f.calls.every(call => ['status', 'mutate', 'configure'].includes(call.method)));
    assert.deepEqual(f.calls.find(call => call.method === 'mutate').operations.map(operation => operation.path[0]), ['offlineBundle', 'dataDir']);
  } finally {
    f.controller.dispose();
    const owned = fs.realpathSync(oldDirectory);
    assert.equal(path.dirname(owned), fs.realpathSync(os.tmpdir()));
    assert.match(path.basename(owned), /^sim-ui-data-dir-/);
    fs.rmSync(owned, {recursive: true, force: true});
  }
});

function browserFixture({blocked = false, throwOnOpen = false} = {}) {
  const calls = [];
  const blank = {closed: false, opener: {}, document: {head: {appendChild() {}}, createElement: () => ({})},
    location: {href: 'about:blank', replace(url) { this.href = url; }}, close() { this.closed = true; }};
  const browser = {open(...args) {
    calls.push(args);
    if (throwOnOpen) throw Error('popup unavailable');
    if (blocked) return null;
    return args[0] === 'about:blank' ? blank : {};
  }};
  return {browser, calls, blank};
}

test('status refresh never starts a service or opens a page, and an unsaved draft disables launch', async () => {
  const browser = browserFixture();
  const f = fixture({browser: browser.browser}); await f.controller.refresh(); await f.controller.refresh();
  assert.equal(f.controller.canLaunch(), true);
  assert.equal(f.calls.some(call => call.method === 'start'), false);
  assert.equal(browser.calls.length, 0);
  f.controller.edit('D:/unsaved');
  assert.equal(f.controller.canLaunch(), false);
  await f.controller.launch();
  assert.equal(f.calls.some(call => call.method === 'start'), false);
  assert.equal(browser.calls.length, 0); f.controller.dispose();
});

test('launch reserves one popup synchronously and coalesces repeated pending clicks', async () => {
  let resolveStart;
  const pendingStart = new Promise(resolve => { resolveStart = resolve; });
  const browser = browserFixture();
  const f = fixture({browser: browser.browser, start: () => pendingStart}); await f.controller.refresh();
  const launched = f.controller.launch();
  assert.equal(f.controller.getSnapshot().launchPhase, 'starting');
  assert.deepEqual(browser.calls, [['about:blank', '_blank']]);
  assert.equal(browser.blank.opener, null);
  assert.equal(f.controller.canLaunch(), false);
  await f.controller.launch();
  assert.equal(f.calls.filter(call => call.method === 'start').length, 1);
  assert.equal(browser.calls.length, 1);
  resolveStart(ok({running: true, owned: true, url: 'http://127.0.0.1:43210'}));
  await launched;
  assert.equal(browser.blank.location.href, 'http://127.0.0.1:43210/');
  assert.equal(f.controller.getSnapshot().workbenchUrl, 'http://127.0.0.1:43210/');
  assert.equal(f.controller.getSnapshot().launchPhase, 'running');
  await f.controller.refresh();
  assert.equal(f.calls.filter(call => call.method === 'start').length, 1);
  assert.equal(f.controller.getSnapshot().workbenchUrl, 'http://127.0.0.1:43210/'); f.controller.dispose();
});

test('a failed start closes only its owned blank page, exposes the error and permits retry', async () => {
  const browser = browserFixture();
  const f = fixture({browser: browser.browser, start: () => ({ok: false, error: {message: '启动进程失败'}})});
  await f.controller.refresh(); await f.controller.launch();
  assert.equal(browser.blank.closed, true);
  assert.equal(f.controller.getSnapshot().launchPhase, 'error');
  assert.match(f.controller.getSnapshot().launchMessage, /启动进程失败/);
  assert.equal(f.controller.getSnapshot().workbenchUrl, null);
  assert.equal(f.controller.canLaunch(), true); f.controller.dispose();
});

test('an accepted response without a running process does not claim startup success', async () => {
  const browser = browserFixture();
  const f = fixture({browser: browser.browser, start: () => ok({running: false, url: 'http://127.0.0.1:43210'})});
  await f.controller.refresh(); await f.controller.launch();
  assert.equal(f.controller.getSnapshot().launchPhase, 'error');
  assert.equal(f.controller.getSnapshot().workbenchUrl, null);
  assert.equal(browser.blank.closed, true); f.controller.dispose();
});

test('invalid and non-loopback startup URLs never become links or navigation targets', async () => {
  for (const url of ['javascript:alert(1)', 'file:///C:/test', 'https://example.com', 'http://example.com', 'http://127.0.0.1.example.com:123', 'http://user:password@127.0.0.1:123', 'http://[::]:123', ' http://127.0.0.1:123']) {
    const browser = browserFixture();
    const f = fixture({browser: browser.browser, start: () => ok({running: true, url})});
    await f.controller.refresh(); await f.controller.launch();
    assert.equal(f.controller.getSnapshot().launchPhase, 'error', url);
    assert.equal(f.controller.getSnapshot().workbenchUrl, null, url);
    assert.equal(browser.calls.length, 1, url);
    assert.equal(browser.blank.closed, true, url); f.controller.dispose();
  }
  assert.equal(client.safeWorkbenchURL('http://[::1]:43210'), 'http://[::1]:43210/');
});

test('popup blocking keeps genuine service success and a permanent safe fallback URL', async () => {
  const browser = browserFixture({blocked: true});
  const f = fixture({browser: browser.browser}); await f.controller.refresh(); await f.controller.launch();
  assert.equal(f.controller.getSnapshot().launchPhase, 'running');
  assert.equal(f.controller.getSnapshot().workbenchUrl, 'http://127.0.0.1:43210/');
  assert.match(f.controller.getSnapshot().launchMessage, /打开工作台/);
  assert.deepEqual(browser.calls[1], ['http://127.0.0.1:43210/', '_blank', 'noopener,noreferrer']);
  f.controller.dispose();
});

test('disposing while startup is pending closes its blank and never opens after unmount', async () => {
  let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  const browser = browserFixture();
  const f = fixture({browser: browser.browser, start: () => pending}); await f.controller.refresh();
  const launched = f.controller.launch(); f.controller.dispose();
  assert.equal(browser.blank.closed, true);
  finish(ok({running: true, url: 'http://127.0.0.1:43210'})); await launched;
  assert.equal(browser.calls.length, 1);
});

test('rendered running panel retains an explicit opener link independently of popups', async () => {
  const f = fixture({browser: browserFixture({blocked: true}).browser});
  await f.controller.refresh(); await f.controller.launch();
  const react = {...React, useMemo: () => f.controller,
    useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(), useEffect() {}};
  const renderedClient = createClientModule(() => react);
  let registration;
  await renderedClient.apply({inject(names, callback) { if (!names.includes('sidebarRight')) callback(this); }, remote: {$mount: async () => {}},
    configForms: {get: () => f.form}, slots: {inject: (_name, callback) => callback(), register: (meta, component) => { if (meta.name === 'plugins.row.config') registration = {meta, component}; }}});
  const page = registration.component({...registration.meta.inject(), view: 'page'});
  const tree = page.type(page.props);
  const elements = [];
  const walk = element => { if (element && typeof element === 'object') { elements.push(element); for (const child of element.children || []) walk(child); } };
  walk(tree);
  const link = elements.find(element => element.type === 'a');
  assert.ok(elements.some(element => element.type === 'button' && element.children.includes('重新校验离线包')));
  assert.equal(link.props.href, 'http://127.0.0.1:43210/');
  assert.equal(link.props.rel, 'noopener noreferrer');
  assert.equal(link.props.target, '_blank');
  const dataInput = elements.find(element => element.type === 'input' && element.props.id === 'sim-workbench-data-root');
  assert.equal(dataInput.props.placeholder, '留空使用默认目录');
  const copy = elements.flatMap(element => element.children || []).filter(value => typeof value === 'string').join('\n');
  assert.match(copy, /旧数据保留/);
  assert.match(copy, /停止当前工作台并断开连接/);
  assert.match(copy, /空目录可建立新工作空间/);
  assert.match(copy, /先停止工作台，再完整复制数据目录/);
  assert.doesNotMatch(copy, /另行处理索引|重建索引/);
  assert.match(copy, /当前生效的数据目录：C:\/fixture\/default-data/); f.controller.dispose();
});

const settled = () => new Promise(resolve => setImmediate(resolve));
function renderedPanel(f, props = {}) {
  const react = {...React, Fragment: Symbol.for('react.fragment'), useMemo: () => f.controller,
    useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(), useEffect() {}};
  const tree = createClientModule(() => react).WorkbenchPanel({formScope: f.form, remote: f.remote, ...props});
  const elements = [];
  const walk = value => {
    if (value && typeof value === 'object') {
      elements.push(value);
      for (const child of value.children || []) walk(child);
    }
  };
  walk(tree);
  return elements;
}

test('client manifest explicitly loads the official main and sidebar slot owners', () => {
  const manifest = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
  assert.ok(manifest.dsh.client.inject.includes('@deepseek-ai/dsh-client-ui-layout'));
  assert.ok(manifest.dsh.client.inject.includes('@deepseek-ai/dsh-client-ui-sidebar'));
  assert.ok(manifest.dsh.client.inject.includes('@deepseek-ai/dsh-client-ui-sidebar-right'));
});

test('inactive or collapsed dock unmounts the workbench instead of retaining a polling iframe', () => {
  let closes = 0;
  const hidden = client.WorkbenchDock({useTabInfo: () => ({tab: {visible: false, actions: {close() { closes++; }}}})});
  assert.equal(hidden, null);
  const visible = client.WorkbenchDock({useTabInfo: () => ({tab: {id: 'tab-1', visible: true}}), hideTab(id) { assert.equal(id, 'tab-1'); closes++; }, remote: 'remote'});
  assert.equal(visible.type, client.WorkbenchPanel);
  assert.equal(visible.props.docked, true);
  visible.props.onHide();
  assert.equal(closes, 1);
});

test('opening a configured sidebar only reads status; explicit launch starts once without popups or poll relaunches', async () => {
  const browser = browserFixture();
  const f = fixture({panel: true, browser: browser.browser});
  await f.controller.refresh(); await settled();
  assert.equal(f.calls.filter(call => call.method === 'start').length, 0);
  assert.equal(renderedPanel(f).some(element => element.type === 'iframe'), false);
  assert.ok(f.calls.every(call => call.method === 'status'));
  await f.controller.launch();
  assert.equal(f.calls.filter(call => call.method === 'start').length, 1);
  assert.equal(f.controller.getSnapshot().launchPhase, 'running');
  assert.equal(f.controller.getSnapshot().workbenchUrl, 'http://127.0.0.1:43210/');
  await f.controller.refresh(); await f.controller.refresh();
  assert.equal(f.calls.filter(call => call.method === 'start').length, 1);
  assert.equal(browser.calls.length, 0);
  assert.ok(f.calls.every(call => ['status', 'start'].includes(call.method)));
  f.controller.dispose();
});

test('an unconfigured sidebar only reads status and shows the existing editable configuration form', async () => {
  const f = fixture({panel: true});
  f.setSetup({revision: 0, state: 'unconfigured', bundle_root: null});
  await f.controller.refresh(); await settled();
  assert.equal(f.calls.some(call => call.method === 'start'), false);
  const elements = renderedPanel(f);
  assert.ok(elements.some(element => element.type?.name === 'ConfigForm'));
  assert.equal(elements.some(element => element.type === 'iframe'), false);
  assert.equal(f.controller.getSnapshot().writable, true);
  f.controller.dispose();
});

test('configuration completion only enables the sidebar launch button and never starts by itself', async () => {
  const f = fixture({panel: true});
  f.setSetup({revision: 0, state: 'unconfigured', bundle_root: null});
  await f.controller.refresh();
  f.controller.edit('D:/configured offline'); await f.controller.save();
  assert.equal(f.calls.some(call => call.method === 'start'), false);
  f.setSetup({revision: 2, state: 'ready', bundle_root: 'D:/configured offline', data_dir_setting: ''});
  await f.controller.refresh(); await settled();
  assert.equal(f.calls.filter(call => call.method === 'start').length, 0);
  await f.controller.launch();
  assert.equal(f.calls.filter(call => call.method === 'start').length, 1);
  assert.equal(f.controller.getSnapshot().launchPhase, 'running');
  f.controller.editData('E:/unsaved data');
  await f.controller.refresh(); await settled();
  assert.equal(f.calls.filter(call => call.method === 'start').length, 1);
  f.controller.dispose();
});

test('sidebar startup failure is honest, polling does not retry, and an explicit retry works', async () => {
  let tries = 0;
  const f = fixture({panel: true, start: () => ++tries === 1 ? {ok: false, error: {message: 'health check failed'}} : ok({running: true, url: 'http://127.0.0.1:43210'})});
  await f.controller.refresh(); await settled();
  await f.controller.launch();
  assert.equal(f.controller.getSnapshot().launchPhase, 'error');
  assert.match(f.controller.getSnapshot().launchMessage, /health check failed/);
  await f.controller.refresh(); await settled();
  assert.equal(tries, 1);
  assert.ok(renderedPanel(f).some(element => element.type === 'button' && element.children.includes('重试启动')));
  await f.controller.launch();
  assert.equal(tries, 2);
  assert.equal(f.controller.getSnapshot().launchPhase, 'running');
  f.controller.dispose();
});

test('two concurrently mounted panel controllers share one pending Host start RPC', async () => {
  let finish;
  const f = fixture({panel: true, start: () => new Promise(resolve => { finish = resolve; })});
  await f.controller.refresh();
  const firstLaunch = f.controller.launch();
  const other = client.createPanelController(f.form, f.remote, {setInterval: () => 2, clearInterval() {}});
  other.start(); await other.refresh();
  const secondLaunch = other.launch(); await settled();
  assert.equal(f.calls.filter(call => call.method === 'start').length, 1);
  finish(ok({running: true, url: 'http://127.0.0.1:43210'})); await settled();
  await firstLaunch; await secondLaunch;
  assert.equal(f.controller.getSnapshot().launchPhase, 'running');
  assert.equal(other.getSnapshot().launchPhase, 'running');
  other.dispose(); f.controller.dispose();
});

test('leaving the sidebar while launch is pending neither navigates late nor stops the service', async () => {
  let finish;
  const browser = browserFixture();
  const f = fixture({panel: true, browser: browser.browser, start: () => new Promise(resolve => { finish = resolve; })});
  await f.controller.refresh();
  const launching = f.controller.launch(); await settled();
  f.controller.dispose();
  finish(ok({running: true, url: 'http://127.0.0.1:43210'})); await settled();
  await launching;
  assert.equal(f.controller.getSnapshot().workbenchUrl, null);
  assert.equal(browser.calls.length, 0);
  assert.equal(f.subscriptions.size, 0);
  assert.ok(f.calls.every(call => ['status', 'start'].includes(call.method)));
});

test('running sidebar embeds only a checked loopback URL and always retains the external opener', async () => {
  const f = fixture({panel: true});
  await f.controller.refresh(); await settled();
  await f.controller.launch();
  const elements = renderedPanel(f);
  const frame = elements.find(element => element.type === 'iframe');
  assert.equal(frame.props.src, 'http://127.0.0.1:43210/');
  assert.equal(frame.props.referrerPolicy, 'no-referrer');
  assert.match(frame.props.title, /仿真方案工作台/);
  const link = elements.find(element => element.type === 'a');
  assert.equal(link.props.href, frame.props.src);
  assert.equal(link.props.rel, 'noopener noreferrer');
  assert.equal(link.props.target, '_blank');
  assert.ok(link.children.includes('在浏览器打开'));
  assert.equal(elements.some(element => element.type?.name === 'ConfigForm'), false);
  f.controller.dispose();
});

test('collapsing the embedded page releases the frame without stopping, relaunching or reopening on status refresh', async t => {
  const f = fixture({panel: true}); t.after(() => f.controller.dispose());
  await f.controller.refresh(); await f.controller.launch();
  const before = f.calls.length;
  const collapse = renderedPanel(f).find(element => element.type === 'button' && element.children.includes('收起页面'));
  collapse.props.onClick();
  assert.equal(f.calls.length, before);
  assert.equal(f.controller.getSnapshot().workbench.running, true);
  assert.equal(renderedPanel(f).some(element => element.type === 'iframe'), false);
  await f.controller.refresh(); await f.controller.refresh();
  assert.equal(renderedPanel(f).some(element => element.type === 'iframe'), false);
  await f.controller.launch();
  assert.ok(renderedPanel(f).some(element => element.type === 'iframe'));
  assert.equal(f.calls.filter(call => call.method === 'start').length, 1);
  assert.equal(f.calls.filter(call => call.method === 'stop').length, 0);
});

test('hiding the panel returns to the conversation without stopping the running service', async t => {
  const f = fixture({panel: true}); t.after(() => f.controller.dispose());
  await f.controller.refresh(); await f.controller.launch();
  let hidden = 0;
  const elements = renderedPanel(f, {onHide: () => { hidden++; }});
  elements.find(element => element.type === 'button' && element.children.includes('隐藏面板')).props.onClick();
  assert.equal(hidden, 1);
  assert.equal(f.controller.getSnapshot().workbench.running, true);
  assert.equal(f.calls.filter(call => call.method === 'stop').length, 0);
  const details = elements.find(element => element.type === 'details' && element.props['aria-label'] === '服务与配置');
  assert.equal(details.props.open, undefined);
});

test('an unsafe sidebar response cannot create an iframe or external link', async () => {
  const f = fixture({panel: true, start: () => ok({running: true, url: 'https://not-local.example/'})});
  await f.controller.refresh(); await settled();
  await f.controller.launch();
  assert.equal(f.controller.getSnapshot().launchPhase, 'error');
  assert.equal(renderedPanel(f).some(element => ['iframe', 'a'].includes(element.type)), false);
  f.controller.dispose();
});

test('reopening a running service shows only its status until the user explicitly opens its page', async () => {
  const f = fixture({panel: true});
  f.setSetup({revision: 1, state: 'ready', bundle_root: 'D:/old', data_dir_setting: '', workbench: {running: true, owned: true, state: 'running', pid: 123, url: 'http://127.0.0.1:4567', detail: 'Python 服务与健康检查已就绪。'}});
  await f.controller.refresh();
  let elements = renderedPanel(f);
  assert.equal(elements.some(element => ['iframe', 'a'].includes(element.type)), false);
  assert.ok(elements.some(element => element.children?.includes('服务运行中')));
  assert.ok(elements.some(element => element.children?.includes('Python 服务与健康检查已就绪。')));
  const open = elements.find(element => element.type === 'button' && element.children.includes('打开工作台'));
  await open.props.onClick(); elements = renderedPanel(f);
  assert.equal(elements.find(element => element.type === 'iframe').props.src, 'http://127.0.0.1:4567/');
  assert.ok(f.calls.every(call => call.method === 'status'), 'Showing an already running service does not invoke start or stop');
  f.controller.dispose();
  const other = client.createPanelController(f.form, f.remote, {setInterval: () => 2, clearInterval() {}});
  other.start(); await other.refresh();
  assert.equal(other.getSnapshot().workbench.running, true); assert.equal(other.getSnapshot().workbenchUrl, null);
  other.dispose();
});

test('explicit stop clears the page only after confirmed shutdown and never restarts during polling', async () => {
  const f = fixture({panel: true}); await f.controller.refresh(); await f.controller.launch();
  const saved = structuredClone(f.form.getSnapshot());
  await f.controller.stop();
  assert.equal(f.controller.getSnapshot().stopPhase, 'stopped'); assert.equal(f.controller.getSnapshot().workbench.running, false);
  assert.equal(f.controller.getSnapshot().workbenchUrl, null); assert.match(f.controller.getSnapshot().stopMessage, /资料库和方案保留/);
  assert.equal(renderedPanel(f).some(element => ['iframe', 'a'].includes(element.type)), false);
  for (let i = 0; i < 3; i++) await f.controller.refresh();
  assert.equal(f.calls.filter(call => call.method === 'stop').length, 1); assert.equal(f.calls.filter(call => call.method === 'start').length, 1);
  assert.deepEqual(f.form.getSnapshot(), saved); assert.equal(f.calls.some(call => ['mutate', 'configure'].includes(call.method)), false);
  assert.equal(f.controller.canStop(), false); assert.equal(f.controller.canLaunch(), true); f.controller.dispose();
});

test('a pending stop coalesces clicks and locks settings and launch until it settles', async () => {
  let finish; const f = fixture({panel: true, stop: () => new Promise(resolve => { finish = resolve; })});
  await f.controller.refresh(); await f.controller.launch();
  const stopping = f.controller.stop(); await settled();
  f.controller.edit('E:/blocked'); f.controller.editData('F:/blocked'); await f.controller.save(); await f.controller.launch(); await f.controller.stop();
  assert.equal(f.controller.getSnapshot().draft, 'D:/old'); assert.equal(f.controller.getSnapshot().dataDraft, '');
  assert.equal(f.controller.canLaunch(), false); assert.equal(f.controller.canStop(), false);
  const stopButton = renderedPanel(f).find(element => element.type === 'button' && element.children.includes('正在停止…')); assert.equal(stopButton.props.disabled, true);
  assert.equal(f.calls.filter(call => call.method === 'stop').length, 1); assert.equal(f.calls.filter(call => call.method === 'start').length, 1);
  finish(ok({running: false, state: 'stopped'})); await stopping; assert.equal(f.controller.canLaunch(), true); f.controller.dispose();
});

test('stop failure preserves the page as uncertain and requires explicit checking or retry, not an automatic restart', async () => {
  let attempts = 0; const f = fixture({panel: true, stop: () => ++attempts === 1 ? {ok: false, error: {message: 'process still stopping'}} : ok({running: false, state: 'stopped'})});
  await f.controller.refresh(); await f.controller.launch(); const url = f.controller.getSnapshot().workbenchUrl;
  await f.controller.stop();
  assert.equal(f.controller.getSnapshot().stopPhase, 'error'); assert.match(f.controller.getSnapshot().stopMessage, /结果待核对.*process still stopping/);
  assert.equal(f.controller.getSnapshot().workbenchUrl, url); assert.equal(f.controller.canLaunch(), false); assert.equal(f.controller.canStop(), true);
  assert.ok(renderedPanel(f).some(element => element.children?.includes('核对服务状态')));
  assert.equal(attempts, 1); await f.controller.stop(); assert.equal(attempts, 2); assert.equal(f.controller.getSnapshot().workbenchUrl, null);
  assert.equal(f.calls.filter(call => call.method === 'start').length, 1); f.controller.dispose();
});

test('an unconfirmed stop response cannot claim shutdown, and the next real status can resolve uncertainty', async () => {
  const f = fixture({panel: true, stop: () => ok({running: true, state: 'running'})});
  await f.controller.refresh(); await f.controller.launch(); await f.controller.stop();
  assert.equal(f.controller.getSnapshot().stopPhase, 'error'); assert.notEqual(f.controller.getSnapshot().workbenchUrl, null);
  await f.controller.refresh(); assert.match(f.controller.getSnapshot().stopMessage, /仍在运行/);
  assert.equal(f.calls.filter(call => call.method === 'stop').length, 1); f.controller.dispose();
});

test('stop timeout leaves an unknown result without replay, and late RPC completion cannot overwrite a manual retry', async t => {
  let finish, attempts = 0; const gate = new Promise(resolve => { finish = resolve; });
  const f = fixture({panel: true, stopTimeoutMs: 20, stop: () => ++attempts === 1 ? gate : ok({running: false, state: 'stopped'})});
  t.after(() => { finish(ok({running: false, state: 'stopped'})); f.controller.dispose(); });
  await f.controller.refresh(); await f.controller.launch(); await f.controller.stop();
  assert.equal(f.controller.getSnapshot().stopPhase, 'error'); assert.match(f.controller.getSnapshot().stopMessage, /超时.*结果待核对/);
  assert.equal(attempts, 1); await f.controller.stop(); assert.equal(f.controller.getSnapshot().stopPhase, 'stopped');
  finish(ok({running: true, state: 'running'})); await settled();
  assert.equal(f.controller.getSnapshot().workbenchUrl, null); assert.equal(f.controller.getSnapshot().stopPhase, 'stopped');
});

test('Stop is available during startup and a late successful start cannot reopen the stopped page', async () => {
  let finish; const f = fixture({panel: true, start: () => new Promise(resolve => { finish = resolve; })});
  await f.controller.refresh(); const launching = f.controller.launch(); await settled();
  assert.equal(f.controller.canStop(), true); assert.equal(renderedPanel(f).find(element => element.type === 'button' && element.children.includes('停止工作台')).props.disabled, false);
  await f.controller.stop(); assert.equal(f.controller.getSnapshot().stopPhase, 'stopped');
  finish(ok({running: true, owned: true, url: 'http://127.0.0.1:4567'})); await launching;
  assert.equal(f.controller.getSnapshot().workbenchUrl, null); assert.equal(f.controller.getSnapshot().workbench.running, false);
  assert.equal(renderedPanel(f).some(element => element.type === 'iframe'), false);
  assert.equal(f.calls.filter(call => call.method === 'start').length, 1); assert.equal(f.calls.filter(call => call.method === 'stop').length, 1); f.controller.dispose();
});

test('a delayed pre-stop status cannot turn a confirmed stopped service back into running', async () => {
  const f = fixture({panel: true}); await f.controller.refresh(); await f.controller.launch();
  let finish; f.remote.simWorkbenchSetup.status = () => new Promise(resolve => { finish = resolve; });
  const reading = f.controller.refresh(); await settled(); await f.controller.stop();
  finish(ok({revision: 1, state: 'ready', bundle_root: 'D:/old', data_dir_setting: '', workbench: {running: true, url: 'http://127.0.0.1:43210'}})); await reading;
  assert.equal(f.controller.getSnapshot().workbench.running, false); assert.equal(f.controller.getSnapshot().workbenchUrl, null); f.controller.dispose();
});

test('verification only runs on an explicit click, does not mutate settings, and locks other operations until fresh readiness', async () => {
  const f = fixture({panel: true}); await f.controller.refresh();
  for (let i = 0; i < 3; i++) await f.controller.refresh();
  assert.equal(f.calls.some(call => call.method === 'verify'), false); assert.equal(f.controller.canVerify(), true);
  await f.controller.verify(); assert.equal(f.controller.getSnapshot().phase, 'checking');
  await f.controller.verify(); await f.controller.launch(); await f.controller.stop(); await f.controller.save(); f.controller.edit('E:/blocked');
  assert.equal(f.controller.getSnapshot().draft, 'D:/old'); assert.equal(f.controller.canLaunch(), false); assert.equal(f.controller.canStop(), false);
  assert.equal(f.calls.filter(call => call.method === 'verify').length, 1); assert.equal(f.calls.some(call => ['start', 'stop', 'mutate', 'configure'].includes(call.method)), false);
  f.setSetup({revision: 2, state: 'ready', bundle_root: 'D:/old', data_dir_setting: '', workbench: {running: false, state: 'stopped'}});
  await f.controller.refresh(); assert.equal(f.controller.getSnapshot().phase, 'ready'); assert.equal(f.controller.canLaunch(), true);
  assert.match(f.controller.getSnapshot().message, /完整校验通过/);
  const verifiedSnapshot = f.controller.getSnapshot(); await f.controller.refresh(); assert.equal(f.controller.getSnapshot(), verifiedSnapshot);
  assert.equal(f.calls.filter(call => call.method === 'verify').length, 1); f.controller.dispose();
});

test('ordinary ready status describes reusable configuration and can stop an already pending service startup', async () => {
  const f = fixture({panel: true});
  await f.controller.refresh(); assert.equal(f.controller.getSnapshot().message, '离线依赖已配置，可启动工作台。');
  f.setSetup({revision: 1, state: 'ready', bundle_root: 'D:/old', data_dir_setting: '', workbench: {running: false, state: 'starting', detail: '正在等待 Python 健康检查。'}});
  await f.controller.refresh(); assert.equal(f.controller.canStop(), true);
  assert.ok(renderedPanel(f).some(element => element.children?.includes('正在等待 Python 健康检查。')));
  await f.controller.stop(); assert.equal(f.calls.filter(call => call.method === 'start').length, 0); assert.equal(f.controller.getSnapshot().stopPhase, 'stopped'); f.controller.dispose();
});

test('unsaved directory edits and pending startup prohibit manual verification', async () => {
  let finish; const f = fixture({panel: true, start: () => new Promise(resolve => { finish = resolve; })});
  await f.controller.refresh(); f.controller.editData('E:/unsaved');
  assert.equal(f.controller.canVerify(), false); await f.controller.verify(); assert.equal(f.calls.some(call => call.method === 'verify'), false);
  f.controller.dispose();
  const g = fixture({panel: true, start: () => new Promise(resolve => { finish = resolve; })}); await g.controller.refresh();
  const launching = g.controller.launch(); await settled(); assert.equal(g.controller.canVerify(), false); await g.controller.verify();
  assert.equal(g.calls.some(call => call.method === 'verify'), false); finish(ok({running: true, url: 'http://127.0.0.1:4567'})); await launching; g.controller.dispose();
});

test('manual verification timeout is honest and polling never resubmits it', async t => {
  const f = fixture({panel: true, rpcTimeoutMs: 20, verify: () => new Promise(() => {})}); t.after(() => f.controller.dispose());
  await f.controller.refresh(); await f.controller.verify();
  assert.equal(f.controller.getSnapshot().phase, 'error'); assert.match(f.controller.getSnapshot().message, /重新校验请求超时.*不会自动重发/);
  await f.controller.refresh(); await f.controller.refresh(); assert.equal(f.calls.filter(call => call.method === 'verify').length, 1);
  assert.equal(f.calls.some(call => ['mutate', 'configure', 'start', 'stop'].includes(call.method)), false);
});

test('manual full verification cannot implicitly stop an already running workbench', async () => {
  const f = fixture({panel: true}); await f.controller.refresh(); await f.controller.launch();
  assert.equal(f.controller.canVerify(), false); await f.controller.verify();
  assert.equal(f.calls.some(call => call.method === 'verify'), false);
  assert.ok(f.controller.getSnapshot().workbenchUrl);
  await f.controller.stop(); assert.equal(f.controller.canVerify(), true);
  f.controller.dispose();
});
