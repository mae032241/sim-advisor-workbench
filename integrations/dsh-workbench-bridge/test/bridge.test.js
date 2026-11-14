import assert from 'node:assert/strict';
import { once } from 'node:events';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { WorkbenchBridge, apply, inject } from '../index.js';
import { parseWorkbenchUrl, requestJSON } from '../http.js';

const SID_A = 'a'.repeat(32), SID_B = 'b'.repeat(32);
const NAMES = ['get_task_context', 'search_knowledge', 'search_cases', 'get_case_guide', 'read_case_configuration', 'read_source', 'save_plan', 'update_steps', 'get_library_scope', 'set_libraries_enabled'];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate) {
  for (let i = 0; i < 150; i++) { if (predicate()) return; await sleep(10); }
  assert.fail('Mock bridge condition timed out');
}
const startTurn = (f, agent, turn) => f.emit('session/event', agent.session, { type: 'turn/start', data: { turn } });
const endTurn = (f, agent, turn, reason = { kind: 'completed' }) =>
  f.emit('session/event', agent.session, { type: 'turn/end', data: { turn, reason } });
const commit = (f, agent, turn, id, content, extra = {}) => f.emit('session/event', agent.session, {
  type: 'assistant/message', data: { turn, step: 1, message: { id, content }, ...extra } });

function host() {
  const listeners = [], agents = [], warnings = [];
  function context(scope) {
    const effects = [], tools = new Map(), prompts = new Map();
    return {
      tools: { register(definition) {
        assert.equal(typeof definition.execute, 'function');
        assert.equal(typeof definition.output.render, 'function');
        assert.equal(definition.parameters.type, 'object');
        if (tools.has(definition.name)) throw new Error('native tool name conflict');
        tools.set(definition.name, definition);
        return () => { if (tools.get(definition.name) === definition) tools.delete(definition.name); };
      } },
      systemPrompt: { section(section) {
        assert.ok(Number.isFinite(section.order));
        if (prompts.has(section.name)) throw new Error('prompt conflict');
        prompts.set(section.name, section);
        return () => prompts.delete(section.name);
      } },
      on(name, callback) {
        const entry = { name, callback, scope }; listeners.push(entry);
        return () => { const index = listeners.indexOf(entry); if (index >= 0) listeners.splice(index, 1); };
      },
      effect(factory) {
        const cleanup = factory(); let disposed = false;
        const dispose = async () => { if (!disposed) { disposed = true; await cleanup?.(); } };
        effects.push(dispose); return dispose;
      },
      toolsMap: tools, prompts, effects,
    };
  }
  const ctx = context(null);
  ctx.agents = { list: () => [...agents] };
  ctx.logger = { warn: message => warnings.push(message) };
  function emit(name, ...args) {
    const subject = name === 'session/event' ? args[0].id : args[0]?.agent?.id;
    for (const listener of [...listeners]) if (listener.name === name && (!listener.scope || listener.scope === subject)) listener.callback(...args);
  }
  function addAgent(id) {
    const agent = { id, session: { id }, status: 'idle', ctx: context(id),
      options: { model: 'existing-dsh-model', reasoningEffort: 'high' },
      model: { current: () => ({ model: 'existing-dsh-model', reasoningEffort: 'high' }) },
      received: [], cancellations: [],
      followup(message) { this.received.push({ mode: 'followup', message }); },
      steer(message) { this.received.push({ mode: 'steer', message }); },
      cancel(reason) { this.cancellations.push(reason); },
    };
    agents.push(agent); emit('agent/created', { agent }); return agent;
  }
  return { ctx, addAgent, emit, listeners, warnings };
}

async function fixture(t, options = {}) {
  const records = new Map(), requests = [], requestOptions = [];
  const server = http.createServer(async (request, response) => {
    let raw = ''; for await (const chunk of request) raw += chunk;
    const body = raw ? JSON.parse(raw) : undefined;
    requests.push({ path: request.url, body });
    const send = (value, status = 200) => { response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)); };
    if (request.url === '/api/tools?bound=true') {
      return send({ ...(options.bound === undefined ? {} : {bound: options.bound}), tools: options.definitions ?? [...NAMES, ...(options.extraTools ?? [])].map(name => ({ name, description: name, inputSchema: { type: 'object',
        properties: { session_id: { type: 'string' }, ...(name === 'get_task_context' ? {} : { revision: { type: 'integer' } }) },
        required: ['session_id'], additionalProperties: false } })) });
    }
    const match = /^\/api\/sessions\/([ab]{32})\/external\/(register|poll|event|tool|unregister)$/.exec(request.url);
    if (!match) return send({ error: 'not found' }, 404);
    const [, sid, action] = match;
    if (action === 'register') {
      if (options.failRegister) return send({ error: '该方案已有存活的外部 Agent 绑定。' }, 409);
      const record = { token: `PRIVATE-BINDING-${sid}`, cursor: 0, commands: [], events: [], saves: [], metadata: body };
      records.set(sid, record);
      return send({ binding_id: record.token, instructions: '基于实际资料规划；不要运行求解。', session_id: sid });
    }
    const record = records.get(sid);
    if (!record || body.binding_id !== record.token) return send({ error: 'invalid binding' }, 403);
    if (action === 'poll') return send({ commands: record.commands.filter(c => c.id > body.after), cursor: record.cursor, connected: !record.detached });
    if (action === 'unregister') { record.unregistered = true; return send({ unregistered: true }); }
    if (action === 'event') { record.events.push(body.event); return send({ accepted: !record.detached }); }
    if (action === 'tool') {
      if (options.contractFailure) return send({ error: `Invalid arguments ${record.token}`,
        error_code: 'tool_arguments_invalid', tool: body.name,
        errors: [{path: '/offset', code: 'minimum', expected: {type: 'integer', minimum: 0, description: 'x'.repeat(2500)}}],
        next_action: {tool: 'get_tool_contract', arguments: {tool: body.name}} }, 422);
      if (options.draftFailure) return send({ error: 'Invalid save shape', draft_id: 'c'.repeat(32),
        draft_revision: 2, errors: [{ path: '/plan/steps/0/location_note', code: 'additionalProperties' }],
        next_action: { tool: 'repair_plan_draft' } }, 422);
      if (options.failTool) return send({ error: `版本冲突。${record.token}` }, 409);
      if (body.name === 'save_plan' || body.name === 'update_steps') record.saves.push(body);
      return send({ result: { from: sid, name: body.name, arguments: body.arguments, revision: 4 } });
    }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  const native = host();
  const bridge = new WorkbenchBridge(native.ctx, { pollInterval: 15, async request(origin, path, body, settings) {
    requestOptions.push({path, settings});
    const response = await requestJSON(origin, path, body, settings);
    await options.afterResponse?.(path);
    return response;
  } }).start();
  const agent = native.addAgent('dsh-agent-A');
  t.after(async () => { await bridge.dispose(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
  return { ...native, agent, bridge, records, requests, requestOptions, origin,
    url: sid => `${origin}/?session=${sid}`,
    call: (agent, name, args = {}) => agent.ctx.toolsMap.get(name).execute(args, { agent, signal: new AbortController().signal }),
    command(sid, type, text) { const record = records.get(sid); record.commands.push({ id: ++record.cursor, type, text }); },
  };
}

test('save validation keeps draft identity and field errors available through the native bridge', async t => {
  const f = await fixture(t, { draftFailure: true });
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  const result = await f.call(f.agent, 'save_plan', { revision: 3 });
  assert.equal(result.success, false);
  assert.equal(result.draft_id, 'c'.repeat(32));
  assert.equal(result.draft_revision, 2);
  assert.equal(result.errors[0].path, '/plan/steps/0/location_note');
  assert.equal(result.next_action.tool, 'repair_plan_draft');
  assert.ok(f.agent.ctx.toolsMap.has('save_plan'));
});

test('contract errors preserve complete expected fields without a draft ID or private token', async t => {
  const f = await fixture(t, {contractFailure: true, extraTools: ['get_tool_contract']});
  await f.call(f.agent, 'bind_workbench', {url: f.url(SID_A)});
  const result = await f.call(f.agent, 'get_tool_contract', {tool: 'save_plan', offset: -1});
  assert.equal(result.success, false);
  assert.equal(result.error_code, 'tool_arguments_invalid');
  assert.equal(result.tool, 'get_tool_contract');
  assert.equal(result.errors[0].expected.minimum, 0);
  assert.equal(result.errors[0].expected.description.length, 2500);
  assert.equal(result.next_action.tool, 'get_tool_contract');
  assert.equal(JSON.stringify(result).includes('PRIVATE-BINDING'), false);
  assert.equal(f.agent.ctx.toolsMap.has('get_tool_contract'), true);
});

test('available settings, review and draft tools bind and disconnect with their owning agent', async t => {
  const extraTools = ['save_plan_settings', 'get_plan_settings', 'review_plan', 'get_plan_draft', 'repair_plan_draft', 'get_mechanism_checks'];
  const f = await fixture(t, { extraTools });
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  for (const name of extraTools) {
    assert.ok(f.agent.ctx.toolsMap.has(name));
    assert.equal(f.agent.ctx.toolsMap.get(name).parameters.properties.session_id, undefined);
    await f.call(f.agent, name, { revision: 3 });
    const sent = f.requests.findLast(r => r.body?.name === name);
    assert.equal(sent.body.name, name);
    assert.equal(sent.path, `/api/sessions/${SID_A}/external/tool`);
  }
  await f.call(f.agent, 'unbind_workbench', {});
  for (const name of extraTools) assert.equal(f.agent.ctx.toolsMap.has(name), false);
});

test('server-owned bound catalog registers contract and future tools with their exact schemas', async t => {
  const definitions = [
    {name: 'get_tool_contract', description: 'actual contract guidance', inputSchema: {type: 'object', properties: {
      tool: {type: 'string'}, path: {type: 'string'}, offset: {type: 'integer', minimum: 0}, length: {type: 'integer', minimum: 1},
    }, required: ['tool'], additionalProperties: false}},
    {name: 'future_planning_tool', description: 'server-owned future tool', inputSchema: {type: 'object', properties: {
      mode: {type: 'string', enum: ['a', 'b']}, nested: {type: 'object', properties: {flag: {type: 'boolean'}}, additionalProperties: false},
    }, required: ['mode'], additionalProperties: false}},
  ];
  const f = await fixture(t, {bound: true, definitions});
  await f.call(f.agent, 'bind_workbench', {url: f.url(SID_A)});
  for (const definition of definitions) {
    const native = f.agent.ctx.toolsMap.get(definition.name);
    assert.deepEqual(native.parameters, definition.inputSchema);
    assert.equal(native.description, definition.description);
  }
  const args = {tool: 'save_plan', path: '/design_brief/simplification/candidates/0/status', offset: 19, length: 71};
  const result = await f.call(f.agent, 'get_tool_contract', args);
  assert.deepEqual(result.arguments, args);
  const sent = f.requests.findLast(r => r.body?.name === 'get_tool_contract');
  assert.equal(sent.path, `/api/sessions/${SID_A}/external/tool`);
  assert.equal(sent.body.arguments.session_id, undefined);
  assert.equal(sent.body.binding_id, f.records.get(SID_A).token);
  await f.call(f.agent, 'unbind_workbench');
  for (const definition of definitions) assert.equal(f.agent.ctx.toolsMap.has(definition.name), false);
});

test('legacy catalog fallback discovers optional navigation and contract while excluding mailbox and unknown tools', async t => {
  const f = await fixture(t, {extraTools: ['get_tool_contract', 'get_html_navigation', 'read_html_section', 'get_case_navigation', 'register_external_agent', 'future_planning_tool']});
  await f.call(f.agent, 'bind_workbench', {url: f.url(SID_A)});
  assert.equal(f.agent.ctx.toolsMap.has('get_tool_contract'), true);
  assert.equal(f.agent.ctx.toolsMap.has('get_html_navigation'), true);
  assert.equal(f.agent.ctx.toolsMap.has('read_html_section'), true);
  assert.equal(f.agent.ctx.toolsMap.has('get_case_navigation'), true);
  assert.equal(f.agent.ctx.toolsMap.has('register_external_agent'), false);
  assert.equal(f.agent.ctx.toolsMap.has('future_planning_tool'), false);
});

test('invalid bound catalogs fail before registering an external binding', async t => {
  for (const definitions of [
    [{name: 'bad', description: 'bad', inputSchema: {type: 'object', properties: {session_id: {type: 'string'}}}}],
    [{name: 'bad', description: 'bad', inputSchema: {type: 'object', properties: {binding_id: {type: 'string'}}}}],
    [{name: 'bad', description: 'bad', inputSchema: {type: 'object', properties: []}}],
    [1],
    ['duplicate', 'duplicate'].map(name => ({name, description: name, inputSchema: {type: 'object', properties: {}}})),
  ]) {
    const f = await fixture(t, {bound: true, definitions});
    await assert.rejects(f.call(f.agent, 'bind_workbench', {url: f.url(SID_A)}), /工具目录/);
    assert.equal(f.requests.some(r => r.path.endsWith('/register')), false);
  }
});

test('bundle metadata, locales, exports and original icon form a native Host package', async () => {
  const root = new URL('../', import.meta.url);
  const manifest = JSON.parse(await readFile(new URL('package.json', root), 'utf8'));
  assert.equal(manifest.name, '@local/sim-advisor-bridge');
  assert.equal(manifest.type, 'module');
  assert.equal(manifest.dsh.bundle.patch, './cordis.patch.yml');
  assert.equal(manifest.dependencies, undefined);
  assert.deepEqual(inject, ['agents', 'tools', 'systemPrompt']);
  assert.equal(typeof apply, 'function');
  for (const locale of ['en', 'zh']) {
    const value = JSON.parse(await readFile(new URL(`locale/${locale}.json`, root), 'utf8'));
    assert.ok(value.meta.title && value.meta.description);
  }
  const svg = await readFile(new URL('icon.svg', root), 'utf8');
  assert.match(svg, /<svg[^>]*xmlns="http:\/\/www.w3.org\/2000\/svg"/);
  assert.match(svg, /<title>Simulation workbench bridge<\/title>/);
  assert.ok(Buffer.byteLength(svg) < 256 * 1024);
  for (const file of ['index.js', 'http.js', 'cordis.patch.yml', 'README.md']) assert.ok((await readFile(new URL(file, root))).length);
});

test('bind is scoped to executing Agent, registers private planning tools and never returns token', async t => {
  const f = await fixture(t);
  const other = f.addAgent('dsh-agent-B');
  await assert.rejects(f.agent.ctx.toolsMap.get('bind_workbench').execute({ url: f.url(SID_A) }, { agent: other }), /所属/);
  const result = await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  assert.equal(result.conversation_id, 'dsh-agent-A');
  assert.equal(result.session_id, SID_A);
  assert.ok(!JSON.stringify(result).includes('PRIVATE-BINDING'));
  assert.deepEqual([...f.agent.ctx.toolsMap.keys()].sort(), ['bind_workbench', 'unbind_workbench', ...NAMES].sort());
  assert.deepEqual([...other.ctx.toolsMap.keys()].sort(), ['bind_workbench', 'unbind_workbench']);
  const record = f.records.get(SID_A);
  assert.equal(record.metadata.pid, process.pid);
  assert.equal(record.metadata.backend, 'dsh');
  assert.equal(record.metadata.name, 'DSH');
  assert.equal(record.metadata.model, 'existing-dsh-model');
  assert.match(f.agent.ctx.prompts.get('sim-advisor:workbench').text, /不要运行求解/);
  assert.equal(other.ctx.prompts.size, 0);
  await assert.rejects(f.call(f.agent, 'bind_workbench', { url: f.url(SID_B) }), /已有工作台/);
});

test('same tool names on two bound Agents proxy only their own plan and private token', async t => {
  const f = await fixture(t), other = f.addAgent('dsh-agent-B');
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  await f.call(other, 'bind_workbench', { url: f.url(SID_B) });
  for (const agent of [f.agent, other]) {
    const definition = agent.ctx.toolsMap.get('save_plan');
    assert.equal(definition.parameters.properties.session_id, undefined);
    assert.ok(!definition.parameters.required.includes('session_id'));
    const value = await f.call(agent, 'save_plan', { revision: 3 });
    assert.equal(value.from, agent === f.agent ? SID_A : SID_B);
    assert.ok(!JSON.stringify(value).includes('PRIVATE-BINDING'));
  }
  assert.equal(f.records.get(SID_A).saves[0].arguments.session_id, undefined);
  assert.equal(f.records.get(SID_A).saves[0].binding_id, f.records.get(SID_A).token);
  await assert.rejects(f.call(f.agent, 'save_plan', { revision: 3, session_id: SID_B }), /方案与凭据/);
  await assert.rejects(f.agent.ctx.toolsMap.get('save_plan').execute({ revision: 3 }, { agent: other }), /不属于/);
});

test('bound library discovery and visibility tools forward explicit selection with private binding', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', {url: f.url(SID_A)});
  const args = {library_ids: ['manual-a', 'model-b'], enabled: true};
  await f.call(f.agent, 'get_library_scope', {query: 'acoustics'});
  const result = await f.call(f.agent, 'set_libraries_enabled', args);
  const request = f.requests.filter(item => item.path.endsWith('/tool')).at(-1);
  assert.equal(request.body.name, 'set_libraries_enabled');
  assert.deepEqual(request.body.arguments, args);
  assert.equal(request.body.binding_id, f.records.get(SID_A).token);
  assert.equal(result.name, 'set_libraries_enabled');
  assert.ok(!JSON.stringify(result).includes('PRIVATE-BINDING'));
  assert.match(f.agent.ctx.prompts.get('sim-advisor:workbench').text, /全局设置/);
  await assert.rejects(f.call(f.agent, 'set_libraries_enabled', {...args, session_id: SID_B}), /方案与凭据/);
});

test('planning tools use the managed 180s deadline and preserve execution cancellation signals', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', {url: f.url(SID_A)});
  const controller = new AbortController(), tool = f.agent.ctx.toolsMap.get('search_knowledge');
  await tool.execute({revision: 3}, {agent: f.agent, signal: controller.signal});
  const first = f.requestOptions.find(request => request.path.endsWith('/tool'));
  assert.equal(first.settings.timeout, 180000);
  assert.equal(first.settings.signal, controller.signal);
  controller.abort();
  await assert.rejects(tool.execute({revision: 3}, {agent: f.agent, signal: controller.signal}), /取消|无法连接/);
  const cancelled = f.requestOptions.filter(request => request.path.endsWith('/tool')).at(-1);
  assert.equal(cancelled.settings.timeout, 180000);
  assert.equal(cancelled.settings.signal, controller.signal);
  assert.equal(cancelled.settings.signal.aborted, true);
  assert.ok(f.requestOptions.filter(request => !request.path.endsWith('/tool')).every(request => request.settings?.timeout === undefined || request.settings.timeout <= 10000));
  assert.ok(f.agent.ctx.toolsMap.has('search_knowledge'), 'cancelling one tool does not detach the binding');
});

test('poll dispatches followup or steer and keeps receiving stop while a model lifetime is pending', async t => {
  const f = await fixture(t);
  f.agent.followup = function (message) { this.received.push({ mode: 'followup', message }); return new Promise(() => {}); };
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  f.command(SID_A, 'message', '先分析几何');
  await until(() => f.agent.received.length === 1);
  const first = f.agent.received[0];
  assert.equal(first.mode, 'followup');
  assert.equal(first.message.role, 'user');
  assert.equal(first.message.source.kind, 'user');
  assert.equal(first.message.content[0].text, '先分析几何');
  assert.ok(first.message.id && Object.isFrozen(first.message));
  f.agent.status = 'running';
  f.command(SID_A, 'message', '重点修改第七步');
  f.command(SID_A, 'interrupt');
  await until(() => f.agent.received.length === 2 && f.agent.cancellations.length === 1);
  assert.equal(f.agent.received[1].mode, 'steer');
  assert.deepEqual(f.agent.cancellations, [{ kind: 'user' }]);
  assert.ok(f.requests.filter(r => r.path.endsWith('/poll')).length >= 2);
});

test('running intermediate messages and tools keep heartbeat polling and stop live without forwarding progress', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  f.agent.status = 'running';
  f.emit('agent/status', { agent: f.agent, status: 'running' });
  startTurn(f, f.agent, 1);
  commit(f, f.agent, 1, 'progress', [{ type: 'text', text: 'PRIVATE intermediate progress' }]);
  await f.call(f.agent, 'search_knowledge', { revision: 3 });
  const count = f.requests.filter(request => request.path.endsWith('/poll')).length;
  await until(() => f.requests.filter(request => request.path.endsWith('/poll')).length >= count + 2);
  f.command(SID_A, 'interrupt');
  await until(() => f.agent.cancellations.length === 1 && f.records.get(SID_A).events.some(event => event.type === 'state' && event.status === 'running'));
  endTurn(f, f.agent, 1, { kind: 'aborted', reason: { kind: 'user' } });
  await sleep(20);
  assert.deepEqual(f.agent.cancellations, [{ kind: 'user' }]);
  assert.deepEqual(f.records.get(SID_A).events.map(event => event.type), ['state', 'state']);
  assert.ok(!JSON.stringify(f.records.get(SID_A).events).includes('PRIVATE'));
  assert.ok(f.requests.filter(request => request.path.endsWith('/poll')).every(request => request.body.binding_id === f.records.get(SID_A).token));
});

test('1000 stream chunks and complete messages publish nothing until successful turn end', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  await until(() => f.records.get(SID_A).events.length === 1);
  assert.ok(!f.listeners.some(listener => listener.name === 'agent/assistant-stream'));
  startTurn(f, f.agent, 2);
  const emit = frame => f.emit('agent/assistant-stream', { agent: f.agent, frame });
  emit({ type: 'start', attemptId: 'attempt-1', turn: 2, step: 1, revision: 1 });
  for (let index = 0; index < 1000; index++) {
    emit({ type: 'chunk', attemptId: 'attempt-1', index, chunk: { type: index % 2 ? 'text-delta' : 'reasoning-delta', index: 0, text: 'PRIVATE STREAM' } });
  }
  emit({ type: 'chunk', attemptId: 'attempt-1', index: 1000, chunk: { type: 'block-end', index: 0, block: { type: 'text', text: 'PRIVATE STREAM' } } });
  await sleep(30);
  assert.equal(f.records.get(SID_A).events.length, 1, 'stream must not add any event requests');
  const final = { type: 'assistant/message', data: { turn: 2, step: 1, message: { id: 'final-1', content: [
    { type: 'text', text: '实际' }, { type: 'reasoning', text: 'PRIVATE REASONING' },
    { type: 'text', text: '观察' },
  ] } } };
  f.emit('session/event', f.agent.session, final); f.emit('session/event', f.agent.session, final);
  await sleep(20);
  assert.equal(f.records.get(SID_A).events.length, 1, 'committed step messages must wait for turn/end');
  endTurn(f, f.agent, 2); endTurn(f, f.agent, 2);
  await until(() => f.records.get(SID_A).events.some(e => e.type === 'message'));
  await sleep(30);
  const events = f.records.get(SID_A).events, messages = events.filter(e => e.type === 'message');
  assert.equal(events.filter(e => e.type === 'delta').length, 0);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].message_id, 'dsh-turn-2-message-final-1');
  assert.equal(messages[0].turn_id, 'dsh-turn-2');
  assert.equal(messages[0].text, '实际观察');
  assert.ok(!JSON.stringify(events).includes('PRIVATE'));
});

test('only the last message of a multi-step turn is sent, without tool activity', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  await until(() => f.records.get(SID_A).events.length === 1);
  startTurn(f, f.agent, 1);
  commit(f, f.agent, 1, 'progress', [{ type: 'text', text: 'Intermediate progress' }]);
  commit(f, f.agent, 1, 'tool', [{ type: 'text', text: 'Going to search' }, { type: 'tool-call', name: 'search_knowledge' }], { step: 2 });
  await f.call(f.agent, 'search_knowledge', { revision: 3 });
  commit(f, f.agent, 1, 'last', [{ type: 'text', text: 'Final turn reply' }], { step: 3 });
  await sleep(20);
  assert.equal(f.records.get(SID_A).events.length, 1);
  endTurn(f, f.agent, 1);
  await until(() => f.records.get(SID_A).events.some(event => event.type === 'message'));
  assert.deepEqual(f.records.get(SID_A).events.filter(event => event.type === 'message').map(event => event.text), ['Final turn reply']);
  assert.equal(f.records.get(SID_A).events.filter(event => event.type === 'activity').length, 0);
});

test('empty, reasoning, tool-bearing or interrupted latest messages never fall back to progress', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  await until(() => f.records.get(SID_A).events.length === 1);
  const cases = [
    { content: [] },
    { content: [null, { type: 'text', text: '  ' }] },
    { content: [{ type: 'reasoning', text: 'PRIVATE REASONING' }] },
    { content: [{ type: 'tool-call', arguments: { secret: 'PRIVATE TOOL ARGS' } }] },
    { content: [{ type: 'text', text: 'Tool progress' }, { type: 'tool-call', name: 'save_plan' }] },
    { content: [{ type: 'text', text: 'Interrupted reply' }], extra: { interrupted: true } },
  ];
  for (const [index, value] of cases.entries()) {
    const turn = index + 1;
    startTurn(f, f.agent, turn);
    commit(f, f.agent, turn, 'progress', [{ type: 'text', text: 'Must not fall back to this progress' }]);
    commit(f, f.agent, turn, 'last', value.content, value.extra);
    endTurn(f, f.agent, turn);
    // A late visible replay after the end must not resurrect a completed turn.
    commit(f, f.agent, turn, 'late', [{ type: 'text', text: 'Late replay' }]);
    endTurn(f, f.agent, turn);
  }
  await sleep(30);
  assert.equal(f.records.get(SID_A).events.length, 1);
});

test('reasoning-only message can be replaced by visible text with the same ID before turn end', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  startTurn(f, f.agent, 1);
  commit(f, f.agent, 1, 'shared-id', [{ type: 'reasoning', text: 'PRIVATE REASONING' }]);
  commit(f, f.agent, 1, 'shared-id', [{ type: 'text', text: 'Visible final reply' }]);
  endTurn(f, f.agent, 1);
  await until(() => f.records.get(SID_A).events.some(event => event.type === 'message'));
  assert.deepEqual(f.records.get(SID_A).events.filter(event => event.type === 'message').map(event => event.text), ['Visible final reply']);
});

test('consecutive turns publish independently even if Agent never becomes idle between turns', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  f.agent.status = 'running'; f.emit('agent/status', { agent: f.agent, status: 'running' });
  for (const turn of [1, 2]) {
    startTurn(f, f.agent, turn);
    commit(f, f.agent, turn, 'same-message-id', [{ type: 'text', text: `Final reply ${turn}` }]);
    endTurn(f, f.agent, turn);
  }
  await until(() => f.records.get(SID_A).events.filter(event => event.type === 'message').length === 2);
  const messages = f.records.get(SID_A).events.filter(event => event.type === 'message');
  assert.deepEqual(messages.map(event => event.text), ['Final reply 1', 'Final reply 2']);
  assert.notEqual(messages[0].message_id, messages[1].message_id);
  assert.deepEqual(f.records.get(SID_A).events.filter(event => event.type === 'state').map(event => event.status), ['idle', 'running']);
});

test('non-completed turn end reasons never publish a final message', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  await until(() => f.records.get(SID_A).events.length === 1);
  const reasons = [{ kind: 'aborted', reason: { kind: 'user' } }, { kind: 'error', error: { code: 'UNKNOWN' } },
    { kind: 'blocked' }, { kind: 'max-tokens' }, { kind: 'interrupted' }, null, 'completed', {}];
  for (const [index, reason] of reasons.entries()) {
    const turn = index + 1;
    startTurn(f, f.agent, turn);
    commit(f, f.agent, turn, 'last', [{ type: 'text', text: 'Not a successful final reply' }]);
    endTurn(f, f.agent, turn, reason);
    endTurn(f, f.agent, turn); // A replay cannot change an already closed outcome.
  }
  await sleep(20);
  assert.equal(f.records.get(SID_A).events.length, 1);
});

test('turn start resets prior candidates and mid-turn bind uses only newly observed messages', async t => {
  const f = await fixture(t);
  commit(f, f.agent, 1, 'history', [{ type: 'text', text: 'Before binding' }]);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  await until(() => f.records.get(SID_A).events.length === 1);
  endTurn(f, f.agent, 1);
  startTurn(f, f.agent, 2);
  commit(f, f.agent, 2, 'progress', [{ type: 'text', text: 'Old turn progress' }]);
  startTurn(f, f.agent, 3);
  endTurn(f, f.agent, 3);
  // No turn/start was observed for 4, as when binding during an existing turn.
  commit(f, f.agent, 4, 'post-bind', [{ type: 'text', text: 'Observed after binding' }]);
  endTurn(f, f.agent, 4);
  await until(() => f.records.get(SID_A).events.some(event => event.type === 'message'));
  assert.deepEqual(f.records.get(SID_A).events.filter(event => event.type === 'message').map(event => event.text), ['Observed after binding']);
});

test('running and idle status changes publish once per transition', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  for (const status of ['idle', 'running', 'idle']) {
    f.agent.status = status;
    for (let index = 0; index < 100; index++) f.emit('agent/status', { agent: f.agent, status });
  }
  await until(() => f.records.get(SID_A).events.filter(event => event.type === 'state').length >= 3);
  await sleep(20);
  assert.deepEqual(f.records.get(SID_A).events.filter(event => event.type === 'state').map(event => event.status), ['idle', 'running', 'idle']);
});

test('turns are deduplicated independently for each bound conversation', async t => {
  const f = await fixture(t), other = f.addAgent('dsh-agent-B');
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  await f.call(other, 'bind_workbench', { url: f.url(SID_B) });
  for (const agent of [f.agent, other]) {
    startTurn(f, agent, 1);
    const event = { type: 'assistant/message', data: { turn: 1, step: 1,
      message: { id: 'same-native-id', content: [{ type: 'text', text: agent.id }] } } };
    f.emit('session/event', agent.session, event);
    f.emit('session/event', agent.session, event);
    endTurn(f, agent, 1); endTurn(f, agent, 1);
  }
  await until(() => [SID_A, SID_B].every(sid => f.records.get(sid).events.some(event => event.type === 'message')));
  await sleep(20);
  assert.deepEqual(f.records.get(SID_A).events.filter(event => event.type === 'message').map(event => event.text), [f.agent.id]);
  assert.deepEqual(f.records.get(SID_B).events.filter(event => event.type === 'message').map(event => event.text), [other.id]);
});

test('another DSH Agent cannot leak its stream into a bound conversation', async t => {
  const f = await fixture(t), other = f.addAgent('dsh-agent-B');
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  f.emit('agent/assistant-stream', { agent: other, frame: { type: 'start', attemptId: 'other', turn: 1, step: 1 } });
  f.emit('agent/assistant-stream', { agent: other, frame: { type: 'chunk', attemptId: 'other', index: 0, chunk: { type: 'text-delta', index: 0, text: 'OTHER AGENT' } } });
  f.emit('session/event', other.session, { type: 'assistant/message', data: { turn: 1, step: 1, message: { id: 'other', content: [{ type: 'text', text: 'OTHER AGENT' }] } } });
  endTurn(f, other, 1);
  await sleep(30);
  assert.ok(!JSON.stringify(f.records.get(SID_A).events).includes('OTHER AGENT'));
});

test('unbind removes planning tools, prompt, listeners and polling while preserving DSH', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  await until(() => f.requests.some(r => r.path.endsWith('/poll')));
  const result = await f.call(f.agent, 'unbind_workbench');
  assert.equal(result.unbound, true);
  assert.deepEqual([...f.agent.ctx.toolsMap.keys()].sort(), ['bind_workbench', 'unbind_workbench']);
  assert.equal(f.agent.ctx.prompts.size, 0);
  assert.ok(!f.listeners.some(l => l.scope === f.agent.id && l.name !== 'agent/created'));
  assert.ok(f.records.get(SID_A).unregistered);
  const count = f.requests.filter(r => r.path.endsWith('/poll')).length;
  await sleep(40);
  assert.equal(f.requests.filter(r => r.path.endsWith('/poll')).length, count);
  assert.equal(f.ctx.agents.list()[0], f.agent);
  assert.equal(f.agent.cancellations.length, 0);
});

test('unbind during registration revokes a late registration without installing tools or polling', async t => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, {afterResponse: path => path.endsWith('/register') ? gate : undefined});
  const pending = f.call(f.agent, 'bind_workbench', {url: f.url(SID_A)});
  const rejected = assert.rejects(pending, /取消|断开|退出/);
  await until(() => f.records.has(SID_A));
  const result = await f.call(f.agent, 'unbind_workbench');
  release();
  await rejected;
  assert.equal(result.unbound, true);
  assert.equal(f.records.get(SID_A).unregistered, true);
  assert.equal(f.agent.ctx.prompts.size, 0);
  assert.deepEqual([...f.agent.ctx.toolsMap.keys()].sort(), ['bind_workbench', 'unbind_workbench']);
  assert.ok(!f.requests.some(request => request.path.endsWith('/poll')));
});

test('unbind during catalog loading prevents remote registration and allows a new binding', async t => {
  let release, held = true;
  const gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, {afterResponse: path => path === '/api/tools?bound=true' && held ? gate : undefined});
  const pending = f.call(f.agent, 'bind_workbench', {url: f.url(SID_A)});
  const rejected = assert.rejects(pending, /取消|断开|退出/);
  await until(() => f.requests.some(request => request.path === '/api/tools?bound=true'));
  assert.equal((await f.call(f.agent, 'unbind_workbench')).unbound, true);
  held = false; release(); await rejected;
  assert.equal(f.records.size, 0);
  assert.equal((await f.call(f.agent, 'bind_workbench', {url: f.url(SID_B)})).bound, true);
});

test('remote disconnect only detaches and plugin unload cleans every owned registration', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  f.command(SID_A, 'disconnect'); f.records.get(SID_A).detached = true;
  await until(() => !f.agent.ctx.toolsMap.has('get_task_context'));
  assert.equal(f.agent.cancellations.length, 0);
  await f.bridge.dispose();
  assert.equal(f.agent.ctx.toolsMap.size, 0);
  assert.equal(f.listeners.length, 0);
  assert.equal(f.agent.ctx.prompts.size, 0);
});

test('disconnected poll drops queued messages and interrupts before they can execute', async t => {
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  f.command(SID_A, 'message', 'Queued before disconnect; must never start a model');
  f.command(SID_A, 'interrupt');
  f.command(SID_A, 'disconnect');
  f.records.get(SID_A).detached = true;
  await until(() => !f.agent.ctx.toolsMap.has('get_task_context'));
  assert.equal(f.agent.received.length, 0);
  assert.equal(f.agent.cancellations.length, 0);
  assert.equal(f.records.get(SID_A).unregistered, true);
});

test('invalid links and failed registration give actionable safe errors without tools', async t => {
  for (const url of ['https://example.com/?session=' + SID_A, 'http://evil.test/?session=' + SID_A,
    'http://user:secret@127.0.0.1/?session=' + SID_A, 'http://127.0.0.1/private?session=' + SID_A,
    'http://127.0.0.1/?session=other', 'http://127.0.0.1/?session=' + SID_A + '&session=' + SID_B]) assert.throws(() => parseWorkbenchUrl(url));
  const f = await fixture(t, { failRegister: true });
  await assert.rejects(f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) }), /409.*已有存活/);
  assert.equal(f.agent.ctx.prompts.size, 0);
  assert.equal(f.agent.ctx.toolsMap.size, 2);
  assert.ok(!f.requests.some(r => r.path.endsWith('/poll')));
});

test('save validation errors retain binding but never echo its private token', async t => {
  const f = await fixture(t, { failTool: true });
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  await assert.rejects(f.call(f.agent, 'save_plan', { revision: 0 }), error => /版本冲突/.test(error.message) && !error.message.includes('PRIVATE-BINDING'));
  assert.ok(f.agent.ctx.toolsMap.has('save_plan'));
});

test('HTTP bridge refuses redirects and bounded visible replies explicitly mark truncation', async t => {
  const server = http.createServer((_req, res) => { res.writeHead(302, { Location: 'http://external.invalid/' }); res.end('{}'); });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => new Promise(resolve => server.close(resolve)));
  await assert.rejects(requestJSON(`http://127.0.0.1:${server.address().port}`, '/'), /302/);
  const f = await fixture(t);
  await f.call(f.agent, 'bind_workbench', { url: f.url(SID_A) });
  f.emit('session/event', f.agent.session, { type: 'assistant/message', data: { turn: 1, step: 1, message: { id: 'long', content: [{ type: 'text', text: 'x'.repeat(17000) }] } } });
  endTurn(f, f.agent, 1);
  await until(() => f.records.get(SID_A).events.some(e => e.type === 'message'));
  const text = f.records.get(SID_A).events.find(e => e.type === 'message').text;
  assert.ok(text.length <= 16000);
  assert.match(text, /显示已截断/);
});
