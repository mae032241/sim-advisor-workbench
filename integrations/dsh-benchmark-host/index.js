/** One memory-only native DSH task. Execution and offline evaluation are separate. */
import http from 'node:http';
import { randomUUID } from 'node:crypto';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

export const name = 'sim-advisor-benchmark-host';
export const inject = ['agents', 'tools', 'systemPrompt', 'llm'];
export const MAX_BYTES = 512 * 1024;
const TOOL_NAMES = ['get_library_scope', 'search_knowledge', 'read_source'];

class HostError extends Error {
  constructor(code) { super(code); this.code = code; }
}

export function callbackRequest(origin, route, token, data, timeout = 10000) {
  const body = data === undefined ? undefined : Buffer.from(JSON.stringify(data));
  if (body && body.length > MAX_BYTES) return Promise.reject(new HostError('message_overflow'));
  return new Promise((resolve, reject) => {
    const request = http.request(new URL(route, origin), {
      method: body ? 'POST' : 'GET', signal: AbortSignal.timeout(timeout),
      headers: { 'X-DSH-Bridge-Token': token, ...(body ? {
        'Content-Type': 'application/json', 'Content-Length': body.length,
      } : {}) },
    }, response => {
      let bytes = 0; const chunks = [];
      response.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > MAX_BYTES) { response.destroy(); reject(new HostError('message_overflow')); }
        else chunks.push(chunk);
      });
      response.on('end', () => {
        try {
          if (response.statusCode !== 200) throw new HostError('callback_rejected');
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
        } catch { reject(new HostError('callback_failed')); }
      });
      response.on('error', () => reject(new HostError('callback_failed')));
    });
    request.on('error', () => reject(new HostError('callback_failed')));
    if (body) request.write(body);
    request.end();
  });
}

async function loadNativeExecutor(base) {
  if (typeof base !== 'string' || !path.isAbsolute(base)) throw new HostError('invalid_module_base');
  const resolved = createRequire(pathToFileURL(base)).resolve('@deepseek-ai/dsh-tool-executor-native');
  const module = await import(pathToFileURL(resolved).href);
  if (typeof module.NativeToolExecutor !== 'function') throw new HostError('native_executor_unavailable');
  return module.NativeToolExecutor;
}

function selected(selection) {
  return { provider: selection.provider, model: selection.model, reasoning_effort: selection.reasoningEffort };
}

function sameSelection(value, selection) {
  return value?.provider === selection.provider && value?.model === selection.model
    && value?.reasoningEffort === selection.reasoningEffort;
}

function finalText(event) {
  const message = event.data?.message;
  if (!message || message.role && message.role !== 'assistant' || !Array.isArray(message.content)
      || event.data.interrupted === true || message.content.some(block => block?.type === 'tool-call')) return '';
  return message.content.filter(block => block?.type === 'text' && typeof block.text === 'string')
    .map(block => block.text).join('');
}

export function overflowPayload(text, metadata) {
  const points = Array.from(text);
  const prefix = { code: 'output_overflow', overflow: true, ...metadata,
    original_text_bytes: Buffer.byteLength(text), partial_text: '' };
  let low = 0, high = points.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (Buffer.byteLength(JSON.stringify({ ...prefix, partial_text: points.slice(0, middle).join('') })) <= MAX_BYTES) low = middle;
    else high = middle - 1;
  }
  return { ...prefix, partial_text: points.slice(0, low).join('') };
}

export async function startBenchmarkHost(ctx, { callback, token,
  request = callbackRequest, nativeExecutor = loadNativeExecutor } = {}) {
  const origin = new URL(callback);
  if (origin.protocol !== 'http:' || origin.hostname !== '127.0.0.1' || !origin.port
      || origin.username || origin.password || origin.search || origin.hash || origin.pathname !== '/') throw new HostError('invalid_callback');
  if (typeof token !== 'string' || token.length < 32) throw new HostError('invalid_callback_token');
  const call = (route, data, timeout) => request(callback, route, token, data, timeout);
  const error = async (code, details = {}) => { try { await call('/error', { code, ...details }); } catch { /* observer only */ } };
  let handle, agent, closed = false, terminal = false, turn, latest = '', usage = null;
  let resolveFinished; const finished = new Promise(resolve => { resolveFinished = resolve; });
  const disposers = [];
  const dispose = async () => {
    if (closed) return;
    closed = true;
    for (const stop of disposers.splice(0).reverse()) await stop();
    await handle?.dispose();
  };
  try {
    const config = await call('/config');
    const selection = { provider: config.provider, model: config.model, reasoningEffort: config.reasoning_effort };
    if (Object.values(selection).some(value => typeof value !== 'string' || !value || value.length > 256)
        || typeof config.prompt !== 'string' || typeof config.instructions !== 'string'
        || typeof config.cwd !== 'string' || !path.isAbsolute(config.cwd)) throw new HostError('invalid_configuration');
    const tools = config.tools;
    if (!Array.isArray(tools) || ![0, 3].includes(tools.length)
        || new Set(tools.map(tool => tool.name)).size !== tools.length
        || tools.some(tool => !TOOL_NAMES.includes(tool.name) || typeof tool.description !== 'string'
          || !tool.inputSchema || typeof tool.inputSchema !== 'object')) throw new HostError('invalid_tools');
    if (ctx.get('sessionPersistence') !== undefined) throw new HostError('session_persistence_enabled');
    const NativeExecutor = await nativeExecutor(config.module_resolution_base);
    const resolved = await ctx.llm.resolveCallConfig(selection);
    if (!sameSelection(resolved, selection)) throw new HostError('selection_mismatch');
    handle = await ctx.agents.create({ sessionId: `benchmark-${randomUUID()}`, meta: { cwd: config.cwd },
      agentOptions: selection, setup: async (agentCtx, prepared) => {
        agent = prepared;
        agent.model.select(selection);
        disposers.push(agentCtx.tools.restrict({ allow: [] }));
        disposers.push(agentCtx.tools.setExecutor(new NativeExecutor(agentCtx)));
        for (const tool of tools) disposers.push(agentCtx.tools.register({ name: tool.name,
          description: tool.description, parameters: tool.inputSchema,
          output: { schema: {}, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
          execute: async (arguments_, exec) => {
            if (closed || exec.agent?.id !== agent.id || exec.agent?.session !== agent.session) throw new HostError('tool_scope_mismatch');
            const response = await call('/tool', { name: tool.name, arguments: arguments_, call_id: exec.callId }, 300000);
            if (response?.success !== true) throw new Error(typeof response?.error === 'string' ? response.error : 'Knowledge tool request failed.');
            return response.value;
          },
        }));
        disposers.push(agentCtx.systemPrompt.section({ name: 'sim-advisor:benchmark-task', order: 850, text: config.instructions }));
      } });
    agent = handle.agent;
    if (ctx.get('sessionPersistence') !== undefined) throw new HostError('session_persistence_enabled');
    if (!sameSelection(agent.model.current(), selection)) throw new HostError('selection_mismatch');
    const visible = [...ctx.tools.view(agent).visible.keys()].sort();
    if (JSON.stringify(visible) !== JSON.stringify(tools.map(tool => tool.name).sort())) throw new HostError('tool_visibility_mismatch');
    const complete = async event => {
      if (terminal || closed) return;
      terminal = true;
      const reason = event.data?.reason?.kind;
      const status = reason === 'completed' ? 'completed' : reason === 'aborted' ? 'interrupted' : 'failed';
      const result = { status, text: latest, selected: selected(selection), usage };
      if (Buffer.byteLength(JSON.stringify(result)) > MAX_BYTES) {
        await error('output_overflow', overflowPayload(latest, { status, selected: result.selected, usage }));
        resolveFinished({ status: 'failed', overflow: true });
        return;
      }
      try { await call('/complete', result, 30000); resolveFinished(result); }
      catch { await error('completion_delivery_failed'); resolveFinished({ status: 'failed' }); }
    };
    disposers.push(ctx.on('session/event', (session, event) => {
      if (closed || terminal || session !== agent.session || !event?.data) return;
      if (event.type === 'turn/start') { turn = event.data.turn; latest = ''; }
      if (event.type === 'llm/response' && event.data.usage && typeof event.data.usage === 'object') {
        const numeric = Object.fromEntries(Object.entries(event.data.usage).filter(([key, value]) =>
          ['inputTokens', 'outputTokens', 'cachedInputTokens', 'cacheReadTokens', 'cacheWriteTokens',
            'input_tokens', 'output_tokens', 'cached_input_tokens'].includes(key) && typeof value === 'number' && Number.isFinite(value) && value >= 0));
        if (Object.keys(numeric).length) {
          usage ??= {};
          for (const [key, value] of Object.entries(numeric)) usage[key] = (usage[key] ?? 0) + value;
        }
      }
      if (event.data.turn !== turn || turn === undefined) return;
      if (event.type === 'assistant/message') latest = finalText(event);
      if (event.type === 'turn/end') void complete(event);
    }));
    const ready = await call('/ready', { ...selected(selection), thread_id: agent.id, tool_names: visible, ephemeral: true });
    if (ready?.accepted !== true) throw new HostError('ready_rejected');
    agent.followup(Object.freeze({ id: randomUUID(), role: 'user', source: Object.freeze({ kind: 'user' }),
      content: Object.freeze([Object.freeze({ type: 'text', text: config.prompt })]) }));
    return { agent, finished, dispose };
  } catch (failure) {
    await error(failure instanceof HostError ? failure.code : 'prepare_failed');
    await dispose();
    throw new HostError('benchmark_host_failed');
  }
}

export function apply(ctx) {
  const callback = process.env.SIM_ADVISOR_DSH_CALLBACK;
  const token = process.env.SIM_ADVISOR_DSH_BRIDGE_TOKEN;
  delete process.env.SIM_ADVISOR_DSH_CALLBACK;
  delete process.env.SIM_ADVISOR_DSH_BRIDGE_TOKEN;
  ctx.effect(async () => {
    try {
      const host = await startBenchmarkHost(ctx, { callback, token });
      return () => host.dispose();
    } catch { return () => {}; }
  }, 'sim-advisor-benchmark-host');
}
