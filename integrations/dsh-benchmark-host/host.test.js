import test from 'node:test';
import assert from 'node:assert/strict';
import { startBenchmarkHost, apply, MAX_BYTES } from './index.js';

const names = ['get_library_scope', 'search_knowledge', 'read_source'];
class FakeNativeExecutor { constructor(ctx) { this.ctx = ctx; } }

function fixture({ tools = names, persistence, readyAccepted = true } = {}) {
  const requests = [], messages = [], listeners = new Set(), local = new Map();
  let selected, restricted = false, executor;
  const agent = { id: 'memory-agent', session: {}, model: {
    select(value) { selected = value; }, current() { return selected; },
  }, followup(message) { messages.push(message); } };
  const agentCtx = { tools: {
    restrict(filter) { assert.deepEqual(filter, { allow: [] }); restricted = true; return () => {}; },
    setExecutor(value) { executor = value; return () => {}; },
    register(definition) { local.set(definition.name, definition); return () => local.delete(definition.name); },
  }, systemPrompt: { section() { return () => {}; } } };
  let createCount = 0;
  const ctx = { get(name) { return name === 'sessionPersistence' ? persistence : undefined; },
    llm: { async resolveCallConfig(value) { return value; } },
    agents: { async create(options) { createCount++; await options.setup(agentCtx, agent); return { agent, async dispose() {} }; } },
    tools: { view() { return { visible: new Map([...(restricted ? [] : [['shell', {}], ['filesystem', {}]]), ...local]) }; } },
    on(event, handler) { assert.equal(event, 'session/event'); listeners.add(handler); return () => listeners.delete(handler); },
  };
  const config = { provider: 'deepseek-official', model: 'deepseek-flash', reasoning_effort: 'high',
    cwd: process.cwd(), instructions: 'Shared planning policy.', prompt: 'Public task only.',
    module_resolution_base: '/public/sdk/package.json', tools: tools.map(name => ({ name, description: name, inputSchema: { type: 'object' } })) };
  let toolSuccess = true;
  const options = { callback: 'http://127.0.0.1:12345/', token: 't'.repeat(40),
    nativeExecutor: async () => FakeNativeExecutor,
    async request(_origin, route, _token, data, timeout) {
      requests.push({ route, data, timeout });
      if (route === '/config') return config;
      if (route === '/ready') { assert.equal(messages.length, 0); return { accepted: readyAccepted }; }
      if (route === '/tool') return toolSuccess ? { success: true, value: { id: 'source', text: 'Knowledge body.' } }
        : { success: false, error: 'Source ID was not found.' };
      return { accepted: true };
    } };
  const emit = (type, data) => { for (const listener of listeners) listener(agent.session, { type, data }); };
  return { ctx, options, requests, messages, local, emit, agent, setToolSuccess(value) { toolSuccess = value; },
    get executor() { return executor; }, get createCount() { return createCount; } };
}

test('creates one exact memory Agent, waits for ready ACK, exposes only scoped knowledge tools', async () => {
  const f = fixture();
  const host = await startBenchmarkHost(f.ctx, f.options);
  assert.equal(f.createCount, 1);
  assert.ok(f.executor instanceof FakeNativeExecutor);
  assert.equal(f.messages.length, 1);
  const ready = f.requests.find(call => call.route === '/ready').data;
  assert.deepEqual(ready.tool_names, [...names].sort());
  assert.equal(ready.ephemeral, true);
  assert.equal(ready.provider, 'deepseek-official');
  assert.equal(ready.model, 'deepseek-flash');
  assert.equal(ready.reasoning_effort, 'high');
  f.emit('turn/start', { turn: 1 });
  f.emit('assistant/message', { turn: 1, message: { role: 'assistant', content: [{ type: 'reasoning', text: 'SECRET_REASONING' }, { type: 'text', text: 'Intermediate' }] } });
  assert.equal(f.requests.some(call => call.route === '/complete'), false);
  const text = '# Final\n' + 'x'.repeat(20000) + '\n[unread-source]  \n';
  f.emit('assistant/message', { turn: 1, message: { content: [{ type: 'reasoning', text: 'SECRET_REASONING' }, { type: 'text', text }] } });
  f.emit('llm/response', { turn: 1, usage: { inputTokens: 12, outputTokens: 5, private: 'SECRET_REASONING' } });
  f.emit('turn/end', { turn: 1, reason: { kind: 'completed' } });
  const result = await host.finished;
  assert.equal(result.text, text);
  assert.equal(result.status, 'completed');
  assert.deepEqual(result.usage, { inputTokens: 12, outputTokens: 5 });
  assert.equal(JSON.stringify(f.requests).includes('SECRET_REASONING'), false);
  await host.dispose();
});

test('ordinary tool error remains an Agent tool error and callback timeout is 300 seconds', async () => {
  const f = fixture();
  const host = await startBenchmarkHost(f.ctx, f.options);
  const tool = f.local.get('read_source');
  f.setToolSuccess(false);
  await assert.rejects(tool.execute({ id: 'typo' }, { agent: f.agent, callId: 'first' }), /Source ID/);
  f.setToolSuccess(true);
  assert.equal((await tool.execute({ id: 'correct' }, { agent: f.agent, callId: 'second' })).id, 'source');
  assert.ok(f.requests.filter(call => call.route === '/tool').every(call => call.timeout === 300000));
  assert.equal(f.requests.some(call => call.route === '/error'), false);
  await host.dispose();
});

test('no-library Agent exposes zero tools and does not reuse an earlier tool-call message as final', async () => {
  const f = fixture({ tools: [] });
  const host = await startBenchmarkHost(f.ctx, f.options);
  assert.deepEqual(f.requests.find(call => call.route === '/ready').data.tool_names, []);
  f.emit('turn/start', { turn: 1 });
  f.emit('assistant/message', { turn: 1, message: { content: [{ type: 'text', text: 'Earlier explanation.' }] } });
  f.emit('assistant/message', { turn: 1, message: { content: [{ type: 'text', text: 'Tool text.' }, { type: 'tool-call', name: 'other' }] } });
  f.emit('turn/end', { turn: 1, reason: { kind: 'aborted' } });
  const result = await host.finished;
  assert.equal(result.status, 'interrupted');
  assert.equal(result.text, '');
  await host.dispose();
});

test('persistence or rejected ready prevents any model followup', async () => {
  for (const settings of [{ persistence: {} }, { readyAccepted: false }]) {
    const f = fixture(settings);
    await assert.rejects(startBenchmarkHost(f.ctx, f.options), /benchmark_host_failed/);
    assert.equal(f.messages.length, 0);
    assert.equal(f.requests.at(-1).route, '/error');
    assert.ok(['session_persistence_enabled', 'ready_rejected'].includes(f.requests.at(-1).data.code));
  }
});

test('overflow is explicit and preserves an exact available prefix without UI truncation', async () => {
  const f = fixture();
  const host = await startBenchmarkHost(f.ctx, f.options);
  const text = '原样🙂\n'.repeat(120000);
  f.emit('turn/start', { turn: 1 });
  f.emit('assistant/message', { turn: 1, message: { content: [{ type: 'text', text }] } });
  f.emit('turn/end', { turn: 1, reason: { kind: 'completed' } });
  const result = await host.finished;
  assert.equal(result.overflow, true);
  const error = f.requests.find(call => call.route === '/error').data;
  assert.equal(error.code, 'output_overflow');
  assert.equal(error.overflow, true);
  assert.ok(text.startsWith(error.partial_text) && error.partial_text.length > 15900);
  assert.ok(Buffer.byteLength(JSON.stringify(error)) <= MAX_BYTES);
  assert.equal(f.requests.some(call => call.route === '/complete'), false);
  await host.dispose();
});

test('apply clears private bridge environment before scheduling initialization', () => {
  const previous = { callback: process.env.SIM_ADVISOR_DSH_CALLBACK, token: process.env.SIM_ADVISOR_DSH_BRIDGE_TOKEN };
  process.env.SIM_ADVISOR_DSH_CALLBACK = 'http://127.0.0.1:12345';
  process.env.SIM_ADVISOR_DSH_BRIDGE_TOKEN = 't'.repeat(40);
  try {
    apply({ effect() {} });
    assert.equal(process.env.SIM_ADVISOR_DSH_CALLBACK, undefined);
    assert.equal(process.env.SIM_ADVISOR_DSH_BRIDGE_TOKEN, undefined);
  } finally {
    for (const [key, value] of [['SIM_ADVISOR_DSH_CALLBACK', previous.callback], ['SIM_ADVISOR_DSH_BRIDGE_TOKEN', previous.token]]) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});
