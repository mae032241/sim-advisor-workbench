import assert from 'node:assert/strict';
import { once } from 'node:events';
import http from 'node:http';
import test from 'node:test';
import { startManagedHost } from '../index.js';

const TOKEN = 'private-managed-token-never-model-visible';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate) {
  for (let i = 0; i < 100; i++) { if (predicate()) return; await sleep(10); }
  assert.fail('native helper mock did not settle');
}

async function fixture(t, { resume = null, missing = false, finalFailures = 0, finalRejects = 0, holdTools = false, toolDefinitions, toolValue } = {}) {
  const calls = [], events = [], eventAttempts = [], warnings = [], toolCalls = [], nativeTools = new Map(['read', 'write', 'edit', 'pwsh'].map(name => [name, {}]));
  const tools = new Map(nativeTools), sections = new Map(), listeners = new Map();
  let ready, errorSeen, disposed = 0;
  const config = { thread_id: resume, cwd: process.cwd(), provider: 'deepseek-official', model: 'deepseek-flash', reasoning_effort: 'high',
    instructions: '保留原生编码工具；不主动求解。', tools: toolDefinitions ?? [{ name: 'get_task_context', description: 'Read plan', inputSchema: { type: 'object', properties: {}, additionalProperties: false } }] };
  const server = http.createServer(async (request, response) => {
    assert.equal(request.headers['x-dsh-bridge-token'], TOKEN);
    let text = ''; for await (const chunk of request) text += chunk;
    const value = text ? JSON.parse(text) : null;
    const send = value => { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(value)); };
    if (request.url === '/config') return send(config);
    if (request.url === '/ready') { ready = value; return send({ accepted: true }); }
    if (request.url === '/event') {
      eventAttempts.push(value);
      if (value.type === 'message' && finalFailures > 0) { finalFailures--; response.statusCode = 500; return send({error: 'fixture delivery failure'}); }
      if (value.type === 'message' && finalRejects > 0) { finalRejects--; return send({accepted: false}); }
      events.push(value); return send({ accepted: true });
    }
    if (request.url === '/tool') { toolCalls.push(value); if (holdTools) return; return send({ success: true, value: toolValue ?? { facts: ['observed'] } }); }
    if (request.url === '/error') { errorSeen = value; return send({ accepted: false }); }
    response.writeHead(404); response.end('{}');
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const callback = `http://127.0.0.1:${server.address().port}`;
  const agentCtx = {
    tools: { register(definition) { tools.set(definition.name, definition); return () => tools.delete(definition.name); } },
    systemPrompt: { section(section) { sections.set(section.name, section); return () => sections.delete(section.name); } },
    on(name, callback) { const list = listeners.get(name) ?? []; list.push(callback); listeners.set(name, list); return () => list.splice(list.indexOf(callback), 1); },
  };
  let selected;
  const agent = { id: resume || 'session-created', session: { header: { cwd: config.cwd } }, status: 'idle', model: {
    select(value) { selected = value; }, current() { return selected; },
  }, received: [], cancellations: [],
    followup(message) { this.received.push({ mode: 'start', message }); this.status = 'running'; emit('agent/status', { agent: this, status: 'running' }); },
    steer(message) { this.received.push({ mode: 'steer', message }); },
    cancel(reason) { this.cancellations.push(reason); this.status = 'idle'; emit('agent/status', { agent: this, status: 'idle' }); },
  };
  function emit(name, ...args) { for (const callback of [...(listeners.get(name) ?? [])]) callback(...args); }
  const create = async (options, type) => {
    calls.push({ type, options });
    if (missing) throw new Error('PRIVATE session error');
    agent.id = options.sessionId ?? options.resumeSessionId;
    await options.setup(agentCtx, agent);
    return { agent, async dispose() { disposed++; } };
  };
  const ctx = { logger: {warn: message => warnings.push(message)}, llm: { async resolveCallConfig(selection) { calls.push({ type: 'validate', selection }); } },
    agents: { create: options => create(options, 'create'), resume: options => create(options, 'resume') },
    tools: { view: () => ({ visible: tools }) },
  };
  let host;
  t.after(async () => { await host?.dispose(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
  try { host = await startManagedHost(ctx, { callback, token: TOKEN }); } catch (error) {
    if (!missing) throw error;
  }
  async function control(route, body = {}, token = TOKEN) {
    return new Promise((resolve, reject) => {
      const encoded = JSON.stringify(body);
      const request = http.request(new URL(route, ready.control_url), { method: 'POST', headers: { 'X-DSH-Bridge-Token': token, 'Content-Length': Buffer.byteLength(encoded) } }, response => {
        let text = ''; response.on('data', chunk => text += chunk); response.on('end', () => resolve({ code: response.statusCode, value: JSON.parse(text) }));
      }); request.on('error', reject); request.end(encoded);
    });
  }
  return { config, calls, events, eventAttempts, warnings, toolCalls, tools, sections, listeners, agent, emit, ready, errorSeen, control, host, disposed: () => disposed };
}

test('native create validates route, adds scoped tools/prompt, preserves native tools, and does not prompt', async t => {
  const f = await fixture(t);
  assert.equal(f.calls[0].type, 'validate');
  assert.equal(f.calls[1].type, 'create');
  assert.equal(f.calls[1].options.meta.cwd, process.cwd());
  assert.deepEqual(f.calls[1].options.agentOptions, { provider: 'deepseek-official', model: 'deepseek-flash', reasoningEffort: 'high' });
  assert.equal(f.agent.received.length, 0);
  assert.ok(['read', 'write', 'edit', 'pwsh', 'get_task_context'].every(name => f.ready.tool_names.includes(name)));
  assert.ok(f.sections.get('sim-advisor:managed-task').text.includes('不主动求解'));
  assert.ok(!JSON.stringify(f.ready).includes(TOKEN));
});

test('managed host registers navigation, contract and future tools directly from server definitions', async t => {
  const toolDefinitions = ['get_tool_contract', 'get_html_navigation', 'read_html_section', 'get_case_navigation', 'future_planning_tool'].map(name => ({
    name, description: 'server-owned contract guidance', inputSchema: {type: 'object', properties: {
      tool: {type: 'string'}, path: {type: 'string'}, offset: {type: 'integer'}, length: {type: 'integer'},
    }, required: ['tool'], additionalProperties: false},
  }));
  const f = await fixture(t, {toolDefinitions});
  for (const definition of toolDefinitions) {
    assert.equal(f.ready.tool_names.includes(definition.name), true);
    assert.deepEqual(f.tools.get(definition.name).parameters, definition.inputSchema);
    assert.equal(f.tools.get(definition.name).description, definition.description);
  }
  const arguments_ = {tool: 'save_plan', path: '/design_brief/simplification/candidates/0/status', offset: 7, length: 23};
  await f.tools.get('get_tool_contract').execute(arguments_, {agent: f.agent, callId: 'contract-call'});
  assert.equal(f.toolCalls[0].name, 'get_tool_contract');
  assert.deepEqual(f.toolCalls[0].arguments, arguments_);
  assert.equal(f.toolCalls[0].thread_id, f.agent.id);
  await f.host.dispose();
  for (const definition of toolDefinitions) assert.equal(f.tools.has(definition.name), false);
});

test('managed validation feedback preserves long expectations and next action while hiding its token', async t => {
  const payload = {error: `bad arguments ${TOKEN}`, error_code: 'tool_arguments_invalid', tool: 'get_task_context',
    errors: [{path: '/invented', code: 'additionalProperties', expected: {additionalProperties: false, description: 'x'.repeat(2500)}}],
    next_action: {tool: 'get_tool_contract', arguments: {tool: 'get_task_context'}}};
  const f = await fixture(t, {toolValue: {success: false, contentItems: [{type: 'inputText', text: JSON.stringify(payload)}]}});
  const result = await f.tools.get('get_task_context').execute({invented: 1}, {agent: f.agent, callId: 'invalid-call'});
  assert.equal(result.success, false);
  assert.equal(result.errors[0].expected.description.length, 2500);
  assert.deepEqual(result.next_action, payload.next_action);
  assert.equal(JSON.stringify(result).includes(TOKEN), false);
});

test('native resume uses exact session identity and never falls back to create', async t => {
  const f = await fixture(t, { resume: 'session-existing' });
  assert.equal(f.calls[1].type, 'resume');
  assert.equal(f.calls[1].options.resumeSessionId, 'session-existing');
  assert.equal(f.ready.thread_id, 'session-existing');
  const missing = await fixture(t, { resume: 'missing-session', missing: true });
  assert.equal(missing.errorSeen.code, 'resume_failed');
  assert.ok(!missing.calls.some(call => call.type === 'create'));
  assert.equal(missing.ready, undefined);
});

test('private controls use native followup/steer/cancel and callback tools', async t => {
  const f = await fixture(t);
  assert.equal((await f.control('/send', { text: 'first' }, 'wrong-token')).code, 403);
  assert.equal((await f.control('/send', { text: 'first' })).value.mode, 'start');
  assert.equal((await f.control('/send', { text: 'follow up' })).value.mode, 'steer');
  assert.equal(f.agent.received[0].message.role, 'user');
  const value = await f.tools.get('get_task_context').execute({}, { agent: f.agent, callId: 'call-1' });
  assert.deepEqual(value, { facts: ['observed'] });
  assert.equal(f.toolCalls[0].call_id, 'call-1');
  assert.equal((await f.control('/interrupt')).value.requested, true);
  assert.deepEqual(f.agent.cancellations, [{ kind: 'user' }]);
});

test('cancelling native tool execution aborts a pending callback read promptly', async t => {
  const f = await fixture(t, {holdTools: true});
  const controller = new AbortController();
  const pending = f.tools.get('get_task_context').execute({}, {agent: f.agent, callId: 'cancelled-tool', signal: controller.signal});
  // Install a rejection observer before aborting, and release the local server
  // through fixture cleanup even if a regression leaves the request pending.
  const outcome = pending.then(() => 'resolved', error => error.name);
  await until(() => f.toolCalls.length === 1);
  controller.abort();
  const result = await Promise.race([outcome, sleep(300).then(() => 'still-pending')]);
  assert.equal(result, 'AbortError');
});

function native(f, type, turn, seq, data = {}) {
  f.emit('session/event', f.agent.session, { type, seq, data: { turn, ...data } });
}

function message(f, turn, seq, text, extra = {}) {
  native(f, 'assistant/message', turn, seq, { message: { id: `message-${seq}`, role: 'assistant', content: [{ type: 'text', text }] }, ...extra });
}

const messages = f => f.events.filter(event => event.type === 'message');

for (const failure of ['finalFailures', 'finalRejects']) {
  test(`final output requires an ACK and retries the same event after ${failure}`, async t => {
    const f = await fixture(t, {[failure]: 1});
    await f.control('/send', {text: 'Exactly one model task'});
    native(f, 'turn/start', 1, 1);
    message(f, 1, 2, 'Final reply that must reach the workbench');
    native(f, 'turn/end', 1, 3, {reason: {kind: 'completed'}});
    await until(() => f.events.some(event => event.type === 'turn' && event.event === 'completed'));
    assert.equal(messages(f).length, 1, 'turn was marked completed even though its only final output was not acknowledged');
    const attempts = f.eventAttempts.filter(event => event.type === 'message');
    assert.equal(attempts.length, 2);
    assert.equal(attempts[0].item_id, attempts[1].item_id);
    assert.equal(attempts[0].text, attempts[1].text);
    assert.equal(f.agent.received.length, 1, 'delivery retry must never submit another Agent task');
  });
}

test('permanent final delivery failure is bounded, explicit and never blocks control or reports success', async t => {
  const f = await fixture(t, {finalFailures: 99});
  await f.control('/send', {text: 'Only one original task'});
  native(f, 'turn/start', 1, 1); message(f, 1, 2, 'A final reply that will be rejected');
  native(f, 'turn/end', 1, 3, {reason: {kind: 'completed'}});
  await until(() => f.eventAttempts.some(event => event.type === 'message'));
  // Delivery backoff is on the outgoing chain, not the HTTP control server.
  const stopped = await f.control('/interrupt');
  assert.equal(stopped.code, 200); assert.equal(stopped.value.requested, true);
  assert.equal(f.eventAttempts.filter(event => event.type === 'message').length, 1, 'stop must be handled before delivery retry backoff');
  await until(() => f.events.some(event => event.type === 'turn' && event.event === 'completed' && event.status === 'failed'));
  const attempts = f.eventAttempts.filter(event => event.type === 'message');
  assert.equal(attempts.length, 3);
  assert.equal(new Set(attempts.map(event => event.item_id)).size, 1);
  assert.equal(messages(f).length, 0);
  assert.ok(f.events.some(event => event.type === 'error' && event.code === 'reply_delivery_failed'));
  assert.ok(!f.events.some(event => event.type === 'turn' && event.status === 'completed'));
  assert.ok(f.warnings.some(message => /未确认送达/.test(message)));
  assert.equal(f.agent.received.length, 1);
  assert.equal(f.toolCalls.length, 0);
});

test('one multi-step native turn sends only its last visible message after successful turn/end', async t => {
  const f = await fixture(t);
  await f.control('/send', { text: 'inspect' });
  native(f, 'turn/start', 1, 1);
  message(f, 1, 2, 'intermediate planning');
  native(f, 'assistant/message', 1, 3, { message: { id: 'tool-step', content: [{ type: 'tool-call', name: 'get_task_context', arguments: {} }] } });
  native(f, 'tool/result', 1, 4, { text: 'PRIVATE TOOL OUTPUT' });
  native(f, 'assistant/message', 1, 5, { message: { id: 'final', content: [{ type: 'reasoning', text: 'PRIVATE REASONING' }, { type: 'text', text: '实际证据' }] }, usage: { private: true } });
  await until(() => f.events.some(event => event.type === 'turn'));
  await sleep(20);
  assert.equal(messages(f).length, 0);
  native(f, 'turn/end', 1, 6, { reason: { kind: 'completed' } });
  await until(() => messages(f).length === 1);
  assert.equal(messages(f)[0].text, '实际证据');
  assert.equal(f.events.filter(event => event.type === 'text_delta' || event.type === 'tool').length, 0);
  assert.ok(!JSON.stringify(f.events).includes('PRIVATE'));
  await until(() => f.events.filter(event => event.type === 'turn').at(-1).event === 'completed');
  assert.equal(f.events.filter(event => event.type === 'turn').at(-1).status, 'completed');
  await f.host.dispose();
  assert.equal(f.disposed(), 1);
  assert.ok(!f.tools.has('get_task_context'));
  assert.ok(f.tools.has('read'));
  assert.equal(f.sections.size, 0);
});

test('resuming one native session namespaces reused native turn/message sequences per Host lifetime', async t => {
  const first = await fixture(t, { resume: 'session-reconnected' });
  const second = await fixture(t, { resume: 'session-reconnected' });
  for (const f of [first, second]) {
    await f.control('/send', { text: 'same reply after reconnect' });
    native(f, 'turn/start', 1, 1);
    message(f, 1, 2, '相同回复');
    native(f, 'turn/end', 1, 3, { reason: { kind: 'completed' } });
    await until(() => messages(f).length === 1);
    assert.equal(messages(f)[0].thread_id, 'session-reconnected');
    assert.ok(!messages(f)[0].item_id.includes(TOKEN));
  }
  assert.notEqual(messages(first)[0].item_id, messages(second)[0].item_id);
});

test('1000 assistant stream frames produce no callback traffic or stream subscription', async t => {
  const f = await fixture(t);
  assert.ok(!f.listeners.has('agent/assistant-stream'));
  await f.control('/send', { text: 'inspect' });
  native(f, 'turn/start', 1, 1);
  await until(() => f.events.length === 1);
  const before = f.events.length;
  for (let i = 0; i < 1000; i++) {
    f.emit('agent/assistant-stream', { agent: f.agent, frame: { type: 'chunk', attemptId: 'private-attempt', index: i,
      chunk: { type: i % 2 ? 'text-delta' : 'reasoning-delta', text: 'PRIVATE STREAM' } } });
  }
  await sleep(20);
  assert.equal(f.events.length, before);
  assert.equal(messages(f).length, 0);
});

test('consecutive native turns deliver separate final messages without an idle status in between', async t => {
  const f = await fixture(t);
  await f.control('/send', { text: 'inspect' });
  native(f, 'turn/start', 1, 1); message(f, 1, 2, 'first final'); native(f, 'turn/end', 1, 3, { reason: { kind: 'completed' } });
  native(f, 'turn/start', 2, 4); message(f, 2, 5, 'second final'); native(f, 'turn/end', 2, 6, { reason: { kind: 'completed' } });
  await until(() => messages(f).length === 2);
  assert.deepEqual(messages(f).map(event => event.text), ['first final', 'second final']);
  assert.notEqual(messages(f)[0].turn_id, messages(f)[1].turn_id);
});

test('latest/end sequence dedup and turn scoping never replay an earlier candidate', async t => {
  const f = await fixture(t);
  await f.control('/send', { text: 'inspect' });
  message(f, 1, 0, 'old replay without current turn');
  native(f, 'turn/start', 1, 10);
  message(f, 1, 11, 'old intermediate'); message(f, 1, 12, 'newest'); message(f, 1, 11, 'out of order');
  native(f, 'turn/end', 1, 13, { reason: { kind: 'completed' } });
  native(f, 'turn/end', 1, 13, { reason: { kind: 'completed' } });
  message(f, 1, 14, 'late prior turn');
  native(f, 'turn/start', 1, 100); // Replayed old turn cannot consume the newer sequence.
  native(f, 'turn/start', 2, 15);
  message(f, 1, 16, 'wrong turn');
  native(f, 'turn/end', 2, 17, { reason: { kind: 'completed' } });
  await until(() => f.events.filter(event => event.type === 'turn' && event.event === 'completed').length === 2);
  assert.deepEqual(messages(f).map(event => event.text), ['newest']);
});

for (const kind of ['aborted', 'error', 'blocked', 'max-tokens']) {
  test(`non-success native ${kind} turn cannot publish an assistant message`, async t => {
    const f = await fixture(t);
    await f.control('/send', { text: 'inspect' });
    native(f, 'turn/start', 1, 1); message(f, 1, 2, 'not a successful result');
    native(f, 'turn/end', 1, 3, { reason: { kind, error: 'PRIVATE ERROR' } });
    await until(() => f.events.some(event => event.type === 'turn' && event.event === 'completed'));
    assert.equal(messages(f).length, 0);
    assert.ok(!JSON.stringify(f.events).includes('PRIVATE'));
  });
}

for (const [name, content, extra] of [
  ['tool-only', [{ type: 'tool-call', name: 'private_tool' }], {}],
  ['text with tool call', [{ type: 'text', text: 'still intermediate' }, { type: 'tool-call', name: 'private_tool' }], {}],
  ['reasoning-only', [{ type: 'reasoning', text: 'PRIVATE REASONING' }], {}],
  ['empty', [], {}],
  ['interrupted', [{ type: 'text', text: 'partial interrupted' }], { interrupted: true }],
]) {
  test(`latest ${name} message masks an earlier visible intermediate`, async t => {
    const f = await fixture(t);
    await f.control('/send', { text: 'inspect' });
    native(f, 'turn/start', 1, 1); message(f, 1, 2, 'must not fall back to this');
    native(f, 'assistant/message', 1, 3, { message: { id: 'latest', content }, ...extra });
    native(f, 'turn/end', 1, 4, { reason: { kind: 'completed' } });
    await until(() => f.events.some(event => event.type === 'turn' && event.event === 'completed'));
    assert.equal(messages(f).length, 0);
    assert.ok(!JSON.stringify(f.events).includes('PRIVATE'));
  });
}

test('stop/idle and raw error never turn a pending assistant message into success', async t => {
  const f = await fixture(t);
  await f.control('/send', { text: 'inspect' });
  native(f, 'turn/start', 1, 1); message(f, 1, 2, 'pending before stop');
  assert.equal((await f.control('/interrupt')).value.requested, true);
  native(f, 'turn/end', 1, 3, { reason: { kind: 'completed' } }); // Even a late completed flag cannot undo stop.
  await until(() => f.events.some(event => event.type === 'turn' && event.status === 'interrupted'));
  assert.equal(messages(f).length, 0);
  await f.control('/send', { text: 'next request' });
  native(f, 'turn/start', 2, 4); message(f, 2, 5, 'pending before error');
  f.emit('agent/error', { agent: f.agent, error: new Error('PRIVATE ERROR') });
  native(f, 'turn/end', 2, 6, { reason: { kind: 'completed' } });
  await until(() => f.events.some(event => event.type === 'error'));
  await sleep(20);
  assert.equal(messages(f).length, 0);
  assert.ok(!JSON.stringify(f.events).includes('PRIVATE'));
});

test('stop before native turn/start is not later promoted to a successful reply', async t => {
  const f = await fixture(t);
  await f.control('/send', { text: 'inspect' });
  await f.control('/interrupt');
  native(f, 'turn/start', 1, 1); message(f, 1, 2, 'late stopped result');
  native(f, 'turn/end', 1, 3, { reason: { kind: 'completed' } });
  await until(() => f.events.some(event => event.type === 'turn' && event.status === 'interrupted'));
  await sleep(20);
  assert.equal(messages(f).length, 0);
});

test('foreign session events and tool execution remain scoped while heartbeat state stays live', async t => {
  const f = await fixture(t);
  await f.control('/send', { text: 'inspect' });
  native(f, 'turn/start', 1, 1);
  const foreign = { ...f.agent, session: {} };
  f.emit('session/event', foreign.session, { type: 'assistant/message', seq: 2, data: { turn: 1, message: { content: [{ type: 'text', text: 'FOREIGN' }] } } });
  f.emit('agent/status', { agent: foreign, status: 'idle' });
  await assert.rejects(f.tools.get('get_task_context').execute({}, { agent: foreign, callId: 'wrong' }), /scope mismatch/);
  await f.tools.get('get_task_context').execute({}, { agent: f.agent, callId: 'right' });
  native(f, 'turn/end', 1, 3, { reason: { kind: 'completed' } });
  await until(() => f.events.some(event => event.type === 'turn' && event.event === 'completed'));
  assert.equal(messages(f).length, 0);
  assert.equal(f.toolCalls.length, 1);
  assert.equal(f.events.filter(event => event.type === 'tool').length, 0);
  assert.deepEqual(f.events.filter(event => event.type === 'turn').map(event => event.event), ['started', 'completed']);
});
