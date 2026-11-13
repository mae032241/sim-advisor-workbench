/** Private ordinary Host plugin for an independently owned `dsh sdk` process. */
import http from 'node:http';
import { randomUUID, timingSafeEqual } from 'node:crypto';
import path from 'node:path';

export const name = 'sim-advisor-managed-host';
export const inject = ['agents', 'tools', 'systemPrompt', 'llm'];
const MAX_BYTES = 2 * 1024 * 1024;
const TEXT_LIMIT = 15900;

function call(origin, route, token, data, timeout = 10000, signal) {
  return new Promise((resolve, reject) => {
    const body = data === undefined ? undefined : Buffer.from(JSON.stringify(data));
    const deadline = AbortSignal.timeout(timeout);
    const request = http.request(new URL(route, origin), { method: body ? 'POST' : 'GET', signal: signal ? AbortSignal.any([signal, deadline]) : deadline,
      headers: { 'X-DSH-Bridge-Token': token, ...(body ? { 'Content-Type': 'application/json', 'Content-Length': body.length } : {}) },
    }, response => {
      const chunks = []; let bytes = 0;
      response.on('data', chunk => { bytes += chunk.length; if (bytes > MAX_BYTES) { response.destroy(); reject(new Error('callback size limit')); } else chunks.push(chunk); });
      response.on('end', () => {
        try {
          const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          if (response.statusCode !== 200) throw new Error('callback rejected');
          resolve(value);
        } catch { reject(new Error('callback failed')); }
      });
      response.on('error', reject);
    });
    request.on('error', reject);
    if (body) request.write(body);
    request.end();
  });
}

export async function startManagedHost(ctx, { callback, token }) {
  const url = new URL(callback);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('invalid private callback');
  if (typeof token !== 'string' || token.length < 32) throw new Error('invalid private token');
  const config = await call(callback, '/config', token);
  const selection = { provider: config.provider, model: config.model, reasoningEffort: config.reasoning_effort };
  // Native turn/message counters can recur after reconnect. Public identity is
  // scoped to this Host lifetime, never to the private callback token.
  const messageScope = randomUUID();
  let handle, agent, server, closed = false, currentTurn, nativeTurn, startedTurn, cancelledTurn;
  let lastSequence = -1, lastNativeTurn = 0;
  const disposers = [];
  let outgoing = Promise.resolve();
  const deliveryWarning = () => ctx.logger?.warn?.('工作台事件未确认送达；请在 DSH 查看回复，不会重新执行 Agent 任务。');
  const postEvent = async event => {
    const result = await call(callback, '/event', token, { ...event, thread_id: agent.id });
    if (result?.accepted !== true) throw new Error('callback not acknowledged');
  };
  const sendEvent = event => {
    if (closed) return;
    outgoing = outgoing.then(() => {
      if (!closed) return postEvent(event);
    }).catch(deliveryWarning);
  };
  const sendFinal = (reply, completion) => {
    if (closed) return;
    outgoing = outgoing.then(async () => {
      if (closed) return;
      let delivered = false;
      // Retry only this immutable output event. A lost ACK may mean the
      // backend already saved it, so every attempt keeps the same item_id.
      for (let attempt = 0; attempt < 3 && !closed; attempt++) {
        if (attempt) await new Promise(resolve => setTimeout(resolve, attempt === 1 ? 100 : 250));
        if (closed) return;
        try { await postEvent(reply); delivered = true; break; } catch { /* bounded output delivery retry */ }
      }
      if (closed) return;
      if (!delivered) {
        deliveryWarning();
        await postEvent({ type: 'error', code: 'reply_delivery_failed', turn_id: reply.turn_id,
          message: 'DSH 本轮已结束，但最终回复未确认送达工作台；请在 DSH 查看，不会自动重新执行任务。' }).catch(deliveryWarning);
      }
      await postEvent(delivered ? completion : { ...completion, status: 'failed' });
    }).catch(deliveryWarning);
  };
  const started = turn => {
    if (startedTurn === turn) return;
    startedTurn = turn;
    sendEvent({ type: 'turn', event: 'started', status: 'inProgress', turn_id: turn });
  };
  const latestVisible = event => {
    const message = event.data?.message;
    let text = '', toolCall = false;
    if (!message || !Array.isArray(message.content) || message.role && message.role !== 'assistant') return { seq: event.seq };
    for (const block of message.content) {
      if (block?.type === 'tool-call') toolCall = true;
      if (block?.type === 'text' && typeof block.text === 'string' && text.length <= TEXT_LIMIT) {
        text += block.text.slice(0, TEXT_LIMIT + 1 - text.length);
      }
    }
    if (toolCall || event.data.interrupted === true || !text.trim()) return { seq: event.seq };
    if (text.length > TEXT_LIMIT) text = text.slice(0, TEXT_LIMIT) + '\n[工作台显示截断；完整回复保留在 DSH。]';
    return { seq: event.seq, text };
  };
  const belongs = observed => observed?.id === agent?.id && observed?.session === agent?.session;
  const setup = async (agentCtx, prepared) => {
    agent = prepared;
    agent.model.select(selection);
    const storedCwd = agent.session.header?.cwd;
    const normalize = value => process.platform === 'win32' ? path.resolve(value).toLowerCase() : path.resolve(value);
    if (config.thread_id && storedCwd && normalize(storedCwd) !== normalize(config.cwd)) throw new Error('resume workspace mismatch');
    for (const tool of config.tools) {
      disposers.push(agentCtx.tools.register({ name: tool.name, description: tool.description, parameters: tool.inputSchema,
        output: { schema: {}, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
        execute: async (args, exec) => {
          if (closed || !belongs(exec.agent)) throw new Error('managed tool scope mismatch');
          const response = await call(callback, '/tool', token, { name: tool.name, arguments: args, thread_id: agent.id, call_id: exec.callId }, 180000, exec.signal);
          if (!response.success) throw new Error(response.error || 'Local planning tool failed.');
          const value = response.value;
          if (value && typeof value === 'object' && Array.isArray(value.contentItems) && typeof value.success === 'boolean') {
            const text = value.contentItems.filter(item => item.type === 'inputText' && typeof item.text === 'string').map(item => item.text).join('\n');
            if (!value.success) {
              try {
                const payload = JSON.parse(text);
                if (payload && typeof payload === 'object' && Array.isArray(payload.errors)
                    && (typeof payload.draft_id === 'string' || payload.error_code === 'tool_arguments_invalid')) {
                  return JSON.parse(JSON.stringify({success: false, ...payload}).replaceAll(token, '[私有绑定凭据]'));
                }
              } catch { /* Non-structured errors keep the existing short form. */ }
              throw new Error(text.slice(0, 2000) || 'Local tool validation failed.');
            }
            return text;
          }
          return value;
        },
      }));
    }
    disposers.push(agentCtx.systemPrompt.section({ name: 'sim-advisor:managed-task', order: 850, text: config.instructions }));
    disposers.push(agentCtx.on('agent/status', ({ agent: observed, status }) => {
      if (!belongs(observed) || !['running', 'idle'].includes(status)) return;
      if (status === 'running') {
        currentTurn ??= `dsh-turn-${randomUUID()}`;
        started(currentTurn);
      } else if (currentTurn) {
        // Idle is a scheduling state, never proof of a successful turn.
        sendEvent({ type: 'turn', event: 'completed', status: cancelledTurn === currentTurn ? 'interrupted' : 'idle', turn_id: currentTurn });
        if (!nativeTurn && cancelledTurn !== currentTurn) currentTurn = undefined;
      }
    }));
    disposers.push(agentCtx.on('agent/error', ({ agent: observed }) => {
      if (!belongs(observed)) return;
      if (nativeTurn) nativeTurn.failed = true;
      sendEvent({ type: 'error' }); // Never serialize raw error chains.
    }));
    disposers.push(agentCtx.on('session/event', (session, event) => {
      if (closed || session !== agent.session || !event || !Number.isSafeInteger(event.seq) || event.seq < 0) return;
      const turn = event.data?.turn;
      if (!Number.isSafeInteger(turn) || turn < 1 || event.seq <= lastSequence) return;
      if (event.type === 'turn/start') {
        if (turn <= lastNativeTurn) return;
        lastNativeTurn = turn;
        lastSequence = event.seq;
        currentTurn = nativeTurn ? `dsh-turn-${randomUUID()}` : currentTurn ?? `dsh-turn-${randomUUID()}`;
        nativeTurn = { turn, id: currentTurn, latest: null, cancelled: cancelledTurn === currentTurn, failed: false };
        started(currentTurn);
        return;
      }
      if (!nativeTurn || nativeTurn.turn !== turn || !['assistant/message', 'turn/end'].includes(event.type)) return;
      lastSequence = event.seq;
      if (event.type === 'assistant/message') {
        // Every committed assistant message replaces the previous candidate,
        // including tool-only, empty, reasoning-only and interrupted messages.
        nativeTurn.latest = latestVisible(event);
        return;
      }
      const ended = nativeTurn;
      nativeTurn = undefined;
      currentTurn = undefined;
      const completed = event.data.reason?.kind === 'completed' && !ended.cancelled && !ended.failed;
      const completion = { type: 'turn', event: 'completed', status: completed ? 'completed' : ended.cancelled || event.data.reason?.kind === 'aborted' ? 'interrupted' : 'failed', turn_id: ended.id };
      if (completed && ended.latest?.text) {
        sendFinal({ type: 'message', text: ended.latest.text,
          item_id: `dsh-${messageScope}-turn-${turn}-message-${ended.latest.seq}`, turn_id: ended.id }, completion);
      } else sendEvent(completion);
      if (cancelledTurn === ended.id) cancelledTurn = undefined;
    }));
  };

  try {
    // This validates the route/configuration like SDK initialize, without inference.
    await ctx.llm.resolveCallConfig(selection);
    handle = config.thread_id ? await ctx.agents.resume({ resumeSessionId: config.thread_id, agentOptions: selection, setup })
      : await ctx.agents.create({ sessionId: `session-${randomUUID()}`, meta: { cwd: config.cwd }, agentOptions: selection, setup });
    agent = handle.agent;
    if (config.thread_id && agent.id !== config.thread_id) throw new Error('resume identity mismatch');
    const chosen = agent.model.current();
    if (chosen?.provider !== selection.provider || chosen?.model !== selection.model || chosen?.reasoningEffort !== selection.reasoningEffort) throw new Error('selection mismatch');
    server = http.createServer(async (request, response) => {
      const reply = (value, status = 200) => { response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)); };
      try {
        const presented = Buffer.from(request.headers['x-dsh-bridge-token'] ?? ''); const expected = Buffer.from(token);
        if (request.headers.origin || presented.length !== expected.length || !timingSafeEqual(presented, expected)) return reply({ error: 'Unauthorized' }, 403);
        if (request.method !== 'POST') return reply({ error: 'Unsupported method' }, 405);
        let raw = ''; for await (const chunk of request) { raw += chunk; if (Buffer.byteLength(raw) > MAX_BYTES) throw new Error('size limit'); }
        const body = JSON.parse(raw);
        if (closed && request.url !== '/close') return reply({ error: 'Session closed' }, 410);
        if (request.url === '/send') {
          if (typeof body.text !== 'string' || !body.text.trim() || body.text.length > 128000) return reply({ error: 'Invalid text' }, 422);
          const mode = agent.status === 'running' ? 'steer' : 'start';
          if (mode === 'start' && !nativeTurn && cancelledTurn === currentTurn) {
            currentTurn = undefined; cancelledTurn = undefined;
          }
          currentTurn ??= `dsh-turn-${randomUUID()}`;
          const message = Object.freeze({ id: randomUUID(), role: 'user', source: Object.freeze({ kind: 'user' }),
            content: Object.freeze([Object.freeze({ type: 'text', text: body.text })]) });
          if (mode === 'steer') agent.steer(message); else agent.followup(message);
          return reply({ thread_id: agent.id, turn_id: currentTurn, mode });
        }
        if (request.url === '/interrupt') {
          const running = agent.status === 'running';
          if (running) {
            cancelledTurn = currentTurn;
            if (nativeTurn) nativeTurn.cancelled = true;
            agent.cancel({ kind: 'user' });
          }
          return reply({ requested: running, turn_id: currentTurn });
        }
        if (request.url === '/close') {
          await dispose(); return reply({ closed: true });
        }
        return reply({ error: 'Unknown control request' }, 404);
      } catch { return reply({ error: 'DSH control failed' }, 422); }
    });
    server.listen(0, '127.0.0.1'); await new Promise((resolve, reject) => { server.once('listening', resolve); server.once('error', reject); });
    await call(callback, '/ready', token, { thread_id: agent.id, control_url: `http://127.0.0.1:${server.address().port}`,
      provider: selection.provider, model: selection.model, reasoning_effort: selection.reasoningEffort, busy: agent.status === 'running',
      tool_names: [...ctx.tools.view(agent).visible.keys()] });
  } catch {
    await call(callback, '/error', token, { code: config.thread_id ? 'resume_failed' : 'prepare_failed' }).catch(() => {});
    await dispose();
    throw new Error('Managed DSH session preparation failed.');
  }

  async function dispose() {
    if (closed) return;
    closed = true; nativeTurn = undefined;
    for (const dispose of disposers.splice(0).reverse()) dispose();
    await handle?.dispose();
    // Do not await close from inside its own active HTTP /close response.
    server?.close();
  }
  return { dispose };
}

export function apply(ctx) {
  const callback = process.env.SIM_ADVISOR_DSH_CALLBACK;
  const token = process.env.SIM_ADVISOR_DSH_BRIDGE_TOKEN;
  // Native code/shell tools must not inherit the private bridge token.
  delete process.env.SIM_ADVISOR_DSH_CALLBACK;
  delete process.env.SIM_ADVISOR_DSH_BRIDGE_TOKEN;
  ctx.effect(async () => {
    const host = await startManagedHost(ctx, { callback, token });
    return () => host.dispose();
  }, 'sim-advisor-managed-host');
}
