import { randomUUID } from 'node:crypto';
import { BridgeError, parseWorkbenchUrl, requestJSON } from './http.js';

export const name = 'sim-advisor-bridge';
export const inject = ['agents', 'tools', 'systemPrompt'];
// Compatibility only: older servers ignore ?bound=true and return mailbox
// administration tools too. New servers own the complete bound-tool catalog.
const LEGACY_PLANNING_TOOLS = ['get_task_context', 'search_knowledge', 'search_cases', 'get_case_guide', 'read_case_configuration', 'read_source', 'save_plan', 'update_steps', 'get_library_scope', 'set_libraries_enabled'];
const LEGACY_OPTIONAL_TOOLS = ['get_plan_draft', 'repair_plan_draft', 'review_plan', 'save_plan_settings', 'get_plan_settings', 'get_mechanism_checks', 'get_tool_contract', 'get_html_navigation', 'read_html_section', 'get_case_navigation', 'inspect_case_source'];
const DISPLAY_LIMIT = 15900;
const OUTPUT = { schema: {}, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] };
const objectSchema = properties => ({ type: 'object', properties, required: Object.keys(properties), additionalProperties: false });
const sameAgent = (a, b) => a?.id === b?.id && a?.session === b?.session;
const turnId = turn => turn === undefined ? undefined : `dsh-turn-${turn}`;
const bounded = text => text.length <= DISPLAY_LIMIT ? text : text.slice(0, DISPLAY_LIMIT) + '\n[工作台显示已截断；完整回复保留在 DSH。]';

function boundDefinitions(catalog) {
  const tools = catalog?.tools;
  const seen = new Set(['bind_workbench', 'unbind_workbench']);
  if (!Array.isArray(tools) || !tools.length || tools.some(definition => {
    if (!definition || typeof definition.name !== 'string' || !definition.name || seen.has(definition.name)
        || typeof definition.description !== 'string' || definition.inputSchema?.type !== 'object'
        || !definition.inputSchema.properties || typeof definition.inputSchema.properties !== 'object'
        || Array.isArray(definition.inputSchema.properties)
        || (definition.inputSchema.required !== undefined && (!Array.isArray(definition.inputSchema.required)
          || definition.inputSchema.required.some(key => typeof key !== 'string')))) return true;
    seen.add(definition.name);
    return false;
  })) throw new BridgeError('工作台工具目录包含无效或重复定义，请更新并启动工作台后重试。');
  if (catalog.bound === true) {
    if (tools.some(definition => Object.hasOwn(definition.inputSchema.properties, 'session_id')
      || Object.hasOwn(definition.inputSchema.properties, 'binding_id'))) throw new BridgeError('工作台绑定工具目录暴露了方案或凭据参数，请更新工作台后重试。');
    return tools;
  }
  const definitions = LEGACY_PLANNING_TOOLS.map(name => tools.find(tool => tool.name === name));
  if (definitions.some(definition => !definition)) throw new BridgeError('工作台缺少完整的规划与资料库工具，请更新并启动工作台后重试。');
  for (const name of LEGACY_OPTIONAL_TOOLS) {
    const definition = tools.find(tool => tool.name === name);
    if (definition) definitions.push(definition);
  }
  return definitions;
}

function userMessage(text) {
  // Matches installed dsh-llm createUserMessage: identified immutable user data.
  return Object.freeze({ id: randomUUID(), role: 'user', source: Object.freeze({ kind: 'user' }),
    content: Object.freeze([Object.freeze({ type: 'text', text })]) });
}

export class WorkbenchBridge {
  #states = new Map();
  #disposers = [];
  #closed = false;

  constructor(ctx, { request = requestJSON, pollInterval = 2000 } = {}) {
    this.ctx = ctx;
    this.request = request;
    this.pollInterval = pollInterval;
  }

  start() {
    this.#disposers.push(this.ctx.on('agent/created', ({ agent }) => this.attach(agent), { global: true }));
    for (const agent of this.ctx.agents.list()) this.attach(agent);
    return this;
  }

  attach(agent) {
    if (this.#closed || this.#states.has(agent.id)) return;
    const state = { agent, binding: null, connecting: false, pendingAbort: null, closed: false, resources: [], scopeDispose: null };
    this.#states.set(agent.id, state);
    const check = exec => {
      if (state.closed || !sameAgent(exec?.agent, agent)) throw new BridgeError('此工具只能由其所属的当前 DSH 会话调用。');
    };
    try {
      state.resources.push(agent.ctx.tools.register({ name: 'bind_workbench',
        description: '将当前 DSH 会话连接到已运行的本机仿真工作台。传入网页链接（含 ?session=方案ID）；不会启动新模型或改变当前模型。',
        parameters: objectSchema({ url: { type: 'string', description: '本机工作台方案链接，例如 http://127.0.0.1:8765/?session=32位方案ID' } }),
        output: OUTPUT,
        execute: async (args, exec) => { check(exec); return this.bind(agent, args?.url, exec.signal); },
      }));
      state.resources.push(agent.ctx.tools.register({ name: 'unbind_workbench',
        description: '解除当前 DSH 会话与工作台的连接，移除规划工具及提示词，保留 DSH 会话和进程。',
        parameters: objectSchema({}), output: OUTPUT,
        execute: async (_args, exec) => { check(exec); return this.unbind(agent); },
      }));
      state.scopeDispose = agent.ctx.effect(() => async () => {
        state.closed = true;
        await this.unbind(agent);
        for (const dispose of state.resources.splice(0).reverse()) dispose();
        this.#states.delete(agent.id);
      }, 'sim-advisor-bridge.agent');
    } catch (error) {
      for (const dispose of state.resources.splice(0).reverse()) dispose();
      this.#states.delete(agent.id);
      throw error;
    }
  }

  async bind(agent, url, signal) {
    const target = parseWorkbenchUrl(url);
    const state = this.#states.get(agent.id);
    if (this.#closed || !state || state.closed) throw new BridgeError('当前 DSH 会话的工作台连接工具已卸载。');
    if (state.binding || state.connecting) throw new BridgeError('当前 DSH 会话已有工作台连接或正在连接；请先 unbind_workbench。');
    state.connecting = true;
    const pendingAbort = new AbortController();
    state.pendingAbort = pendingAbort;
    signal = signal ? AbortSignal.any([signal, pendingAbort.signal]) : pendingAbort.signal;
    const requirePending = () => {
      if (this.#closed || state.closed || signal.aborted) throw new BridgeError('连接过程中当前会话已退出、断开或操作被取消。');
    };
    let binding;
    try {
      requirePending();
      const catalog = await this.request(target.origin, '/api/tools?bound=true', undefined, { signal });
      requirePending();
      const definitions = boundDefinitions(catalog);
      const selection = agent.model?.current?.() ?? {};
      const metadata = { name: 'DSH', pid: process.pid, backend: 'dsh', conversation_id: agent.id };
      const model = selection.model ?? agent.options?.model;
      const effort = selection.reasoningEffort ?? agent.options?.reasoningEffort;
      if (typeof model === 'string' && model) metadata.model = model;
      if (typeof effort === 'string' && effort) metadata.reasoning_effort = effort;
      const registered = await this.request(target.origin, `/api/sessions/${target.sid}/external/register`, metadata, { signal });
      if (typeof registered.binding_id !== 'string' || !registered.binding_id || typeof registered.instructions !== 'string') {
        throw new BridgeError('工作台注册响应缺少绑定凭据或规划指令。');
      }
      binding = { ...target, token: registered.binding_id, agent, state, cursor: 0, resources: [], closed: false,
        abort: new AbortController(), timer: null, outgoing: Promise.resolve(),
        lastEndedTurn: -1, turn: null, currentTurn: undefined, lastStatus: undefined };
      requirePending();
      state.binding = binding;
      for (const definition of definitions) {
        const parameters = structuredClone(definition.inputSchema);
        if (catalog.bound !== true) {
          delete parameters.properties.session_id;
          parameters.required = (parameters.required ?? []).filter(key => key !== 'session_id');
        }
        binding.resources.push(agent.ctx.tools.register({ name: definition.name, description: definition.description,
          parameters, output: OUTPUT,
          execute: async (args, exec) => {
            if (binding.closed || !sameAgent(exec?.agent, agent)) throw new BridgeError('工作台规划工具不属于当前 DSH 会话，或绑定已断开。');
            if (!args || typeof args !== 'object' || Array.isArray(args) || Object.hasOwn(args, 'session_id') || Object.hasOwn(args, 'binding_id')) {
              throw new BridgeError('请仅传工具声明的业务参数；方案与凭据由当前绑定固定。');
            }
            try {
              const response = await this.request(binding.origin, `/api/sessions/${binding.sid}/external/tool`,
                { binding_id: binding.token, name: definition.name, arguments: args }, { signal: exec.signal, timeout: 180000 });
              if (!response.result || typeof response.result !== 'object') throw new BridgeError('工作台工具响应缺少 result。');
              return response.result;
            } catch (error) {
              if (error instanceof BridgeError && error.payload && Array.isArray(error.payload.errors)
                  && (typeof error.payload.draft_id === 'string' || error.payload.error_code === 'tool_arguments_invalid')) {
                // Preserve actionable field paths and the retained draft ID.
                // They are business validation feedback, not a connection loss.
                return JSON.parse(JSON.stringify({ success: false, ...error.payload })
                  .replaceAll(binding.token, '[私有绑定凭据]'));
              }
              throw new BridgeError(this.#safe(error, binding));
            }
          },
        }));
      }
      binding.resources.push(agent.ctx.systemPrompt.section({ name: 'sim-advisor:workbench', order: 850,
        text: registered.instructions + `\n当前绑定工作台方案：${target.sid}。规划工具已固定到此方案；不要传 session_id 或绑定凭据。资料库目录与启停为全局设置，按研究需求选择若干库，不自动全部启用。` }));
      binding.resources.push(agent.ctx.on('session/event', (session, event) => {
        if (session === agent.session) this.#sessionEvent(binding, event);
      }));
      binding.resources.push(agent.ctx.on('agent/status', ({ agent: observed, status }) => {
        if (sameAgent(observed, agent) && ['running', 'idle'].includes(status)) {
          this.#publishState(binding, status);
        }
      }));
      binding.resources.push(agent.ctx.on('agent/error', ({ agent: observed }) => {
        if (sameAgent(observed, agent)) this.#publish(binding, { type: 'error', text: 'DSH 本轮执行未完成；请查看 DSH，已保存的方案保留。' });
      }));
      this.#publishState(binding, agent.status === 'running' ? 'running' : 'idle');
      this.#schedule(binding, 0);
      return { bound: true, session_id: target.sid, conversation_id: agent.id, url: target.url, instructions_loaded: true };
    } catch (error) {
      if (binding) { state.binding = binding; await this.unbind(agent); }
      throw new BridgeError(this.#safe(error, binding));
    } finally { state.connecting = false; state.pendingAbort = null; }
  }

  #safe(error, binding) {
    const message = error instanceof BridgeError ? error.message : 'DSH 工作台连接失败，请检查本机工作台和当前会话状态。';
    return binding ? message.replaceAll(binding.token, '[私有绑定凭据]').slice(0, 1000) : message.slice(0, 1000);
  }

  #publish(binding, event) {
    if (binding.closed) return;
    binding.outgoing = binding.outgoing.then(async () => {
      if (binding.closed) return;
      const result = await this.request(binding.origin, `/api/sessions/${binding.sid}/external/event`,
        { binding_id: binding.token, event }, { signal: binding.abort.signal });
      if (result.accepted === false) throw new BridgeError('工作台已解除外部绑定，请重新 bind_workbench。');
    }).catch(error => this.#failed(binding, error));
  }

  #publishState(binding, status) {
    if (binding.closed || binding.lastStatus === status) return;
    binding.lastStatus = status;
    if (status === 'running') binding.currentTurn = undefined;
    this.#publish(binding, { type: 'state', status, ...(binding.currentTurn ? { turn_id: binding.currentTurn } : {}) });
  }

  #failed(binding, error) {
    if (binding.closed) return;
    this.ctx.logger?.warn?.(`工作台连接已断开：${this.#safe(error, binding)}`);
    void this.unbind(binding.agent);
  }

  #schedule(binding, delay = this.pollInterval) {
    if (binding.closed) return;
    binding.timer = setTimeout(() => { void this.#poll(binding); }, delay);
    binding.timer.unref?.();
  }

  async #poll(binding) {
    try {
      const response = await this.request(binding.origin, `/api/sessions/${binding.sid}/external/poll`,
        { binding_id: binding.token, after: binding.cursor }, { signal: binding.abort.signal });
      if (binding.closed) return;
      if (!Array.isArray(response.commands) || !Number.isSafeInteger(response.cursor) || response.cursor < binding.cursor || typeof response.connected !== 'boolean') throw new BridgeError('工作台收件箱响应无效。');
      // A detached inbox can still contain messages queued before disconnect.
      // Never dispatch any of them after the server has revoked the binding.
      if (!response.connected) { await this.unbind(binding.agent); return; }
      for (const command of response.commands) {
        if (command.type === 'disconnect') { await this.unbind(binding.agent); return; }
        if (command.type === 'interrupt') {
          Promise.resolve(binding.agent.cancel({ kind: 'user' })).catch(() => {
            this.#publish(binding, { type: 'error', text: 'DSH 未能停止当前运行，请在 DSH 中检查。' });
          });
        } else if (command.type === 'message' && typeof command.text === 'string') {
          const message = userMessage(command.text);
          const accepted = binding.agent.status === 'running' ? binding.agent.steer(message) : binding.agent.followup(message);
          // Do not await a model lifetime: polling and stop dispatch stay live.
          Promise.resolve(accepted).catch(() => this.#publish(binding, { type: 'error', text: 'DSH 未能接收工作台消息，请重试。' }));
        }
      }
      binding.cursor = response.cursor;
      this.#schedule(binding);
    } catch (error) { this.#failed(binding, error); }
  }

  #sessionEvent(binding, event) {
    if (binding.closed || !['turn/start', 'assistant/message', 'turn/end'].includes(event?.type)) return;
    const turn = event.data?.turn;
    if (!Number.isSafeInteger(turn) || turn < 0 || turn <= binding.lastEndedTurn) return;
    if (event.type === 'turn/end') {
      // Installed DSH 0.2.2-rc.1 appends {turn, reason:{kind}} in the
      // loop's finally block. Only completed proves a normal turn end.
      // Native turn numbers increase within this bound session, so a single
      // high-water mark also rejects duplicate/replayed ends without a map.
      binding.lastEndedTurn = turn;
      if (binding.turn?.id !== turn) return;
      const latest = binding.turn.latest;
      binding.turn = null;
      binding.currentTurn = turnId(turn);
      if (event.data.reason?.kind !== 'completed' || !latest) return;
      this.#publish(binding, { type: 'message', message_id: `dsh-turn-${turn}-message-${latest.id}`,
        text: latest.text, turn_id: turnId(turn) });
      return;
    }
    if (binding.turn && turn < binding.turn.id) return;
    if (event.type === 'turn/start') {
      if (binding.turn?.id !== turn) binding.turn = { id: turn, latest: null };
      binding.currentTurn = turnId(turn);
      return;
    }
    // Binding during a running turn uses only messages received after bind;
    // no history scan is needed when its start event was already emitted.
    if (binding.turn?.id !== turn) binding.turn = { id: turn, latest: null };
    binding.turn.latest = null;
    const message = event.data.message;
    // Every latest message replaces the prior candidate, including empty,
    // interrupted or tool-bearing messages. Never fall back to progress text.
    if (event.data.interrupted || !message || typeof message.id !== 'string' || !Array.isArray(message.content) ||
        message.content.some(block => block?.type === 'tool-call')) return;
    const text = message.content.filter(block => block?.type === 'text' && typeof block.text === 'string').map(block => block.text).join('');
    if (text.trim()) binding.turn.latest = { id: message.id, text: bounded(text) };
  }

  async unbind(agent) {
    const state = this.#states.get(agent.id);
    const binding = state?.binding;
    const connecting = Boolean(state?.connecting);
    state?.pendingAbort?.abort();
    if (!binding) return { unbound: connecting, ...(connecting ? {cancelled: true} : {}) };
    state.binding = null;
    binding.closed = true;
    clearTimeout(binding.timer);
    binding.abort.abort();
    for (const dispose of binding.resources.splice(0).reverse()) dispose();
    try {
      await this.request(binding.origin, `/api/sessions/${binding.sid}/external/unregister`, { binding_id: binding.token }, { timeout: 3000 });
    } catch { this.ctx.logger?.warn?.('工作台注销未确认；本地连接已移除，远端绑定将由心跳检查过期。'); }
    return { unbound: true, session_id: binding.sid, conversation_id: agent.id };
  }

  async dispose() {
    if (this.#closed) return;
    this.#closed = true;
    for (const dispose of this.#disposers.splice(0).reverse()) dispose();
    for (const state of [...this.#states.values()]) {
      await state.scopeDispose?.();
    }
    this.#states.clear();
  }
}

export function apply(ctx) {
  const bridge = new WorkbenchBridge(ctx);
  ctx.effect(() => () => bridge.dispose(), 'sim-advisor-bridge.lifecycle');
  bridge.start();
}
