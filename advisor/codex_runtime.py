"""One persistent app-server process and Codex thread per simulation plan.

The caller supplies the complete argv (normally ``codex app-server --stdio``),
an existing dedicated workspace, and fast ``on_event(dict)`` / ``on_tool(name,
arguments)`` callbacks. Event callbacks run on worker threads. Resume restores
server-persisted tool definitions; ``tools`` only registers their local allowlist
when resuming. This module never copies credentials or exposes raw protocol logs.
Protocol: local codex-cli 0.167.0-alpha.3.2 generated experimental JSON schemas.
"""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeout
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading

from .process_runner import _ProcessTree


class CodexRuntimeError(RuntimeError):
    """A safe, bounded runtime error; never contains server logs or tool data."""


class CodexRuntime:
    REQUEST_TIMEOUT = 30
    MAX_MESSAGE_BYTES = 4 * 1024 * 1024
    MAX_TOOL_BYTES = 1024 * 1024

    def __init__(self, command, cwd, on_event, on_tool, model="gpt-6.1-sol", *, reasoning_effort="xhigh"):
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise ValueError("command must be a nonempty argv list")
        # The local protocol accepts model-advertised nonempty effort strings,
        # rather than a fixed enum. Preserve the requested value without fallback.
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            raise ValueError("reasoning_effort must be a nonempty string")
        self.command = list(command)
        self.cwd = Path(cwd).resolve(strict=True)
        if not self.cwd.is_dir():
            raise ValueError("cwd must be an existing dedicated directory")
        if not callable(on_event) or not callable(on_tool):
            raise TypeError("on_event and on_tool must be callable")
        self.on_event, self.on_tool, self.model = on_event, on_tool, model
        self.reasoning_effort = reasoning_effort
        self._lock = threading.RLock()
        self._operations = threading.Lock()
        self._stop_lock = threading.Lock()
        self._outbox = queue.Queue(maxsize=64)
        self._pending = {}
        self._next_id = 0
        self._process = self._tree = None
        self._reader = self._writer = None
        self._thread_id = self._turn_id = None
        self._state, self._last_error = "new", None
        self._last_turn = self._error_metadata = None
        self._closed = False
        self._completed_turns = []
        self._tool_phases = []
        self._delta_tails = {}
        self._private_items = set()
        self._allowed_tools = set()
        self._interrupt_pending = False
        self._tool_slots = threading.BoundedSemaphore(8)
        self._secrets = tuple(value for name, value in os.environ.items()
                              if len(value) >= 8 and re.search(r"token|password|secret|api.?key", name, re.I))
        self._delta_tail_size = max([32, *(len(value) for value in self._secrets)])

    def _safe_text(self, text):
        text = str(text)
        for secret in self._secrets:
            text = text.replace(secret, "[redacted]")
        text = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [redacted]", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[redacted]", text)
        return re.sub(r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|password|authorization|client[_-]?secret)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
                      r"\1[redacted]", text)

    def _emit(self, event):
        # Only explicitly built allowlisted events reach this boundary.
        with self._lock:
            event.setdefault("thread_id", self._thread_id)
        event = {key: self._safe_text(value) if isinstance(value, str) else value for key, value in event.items()}
        try:
            self.on_event(event)
        except Exception:
            pass  # A UI listener must not break the protocol reader.

    def status(self):
        with self._lock:
            running = self._process is not None and self._process.poll() is None and not self._closed and self._state != "error"
            return {"running": running, "busy": running and self._state in {"starting_turn", "busy", "interrupting"},
                    "state": self._state, "thread_id": self._thread_id, "turn_id": self._turn_id,
                    "pid": self._process.pid if self._process else None, "model": self.model,
                    "reasoning_effort": self.reasoning_effort,
                    "last_error": self._last_error,
                    "turn_outcome_supported": True,
                    "last_turn": dict(self._last_turn) if self._last_turn else None,
                    "error_metadata": dict(self._error_metadata) if self._error_metadata else None}

    @staticmethod
    def _error_info(error, *, will_retry=None, turn_id=None):
        """Keep only protocol categories and bounded numeric diagnostics.

        Error messages/additionalDetails may contain prompts or credentials.
        Unknown categories and missing retry flags never imply a retry.
        """
        error = error if isinstance(error, dict) else {}
        info = error.get("codexErrorInfo")
        category = info if isinstance(info, str) else next(iter(info), None) if isinstance(info, dict) and len(info) == 1 else None
        allowed = {"contextWindowExceeded", "usageLimitExceeded", "httpConnectionFailed", "httpTooManyRequests",
                   "responseStreamConnectionFailed", "responseStreamDisconnected", "responseStreamTooManyFailedAttempts",
                   "internalServerError", "unauthorized", "badRequest", "sandboxError", "threadRollbackFailed", "other"}
        result = {"code": "codex_error", "category": category if category in allowed else "unknown",
                  "will_retry": will_retry if type(will_retry) is bool else None,
                  "turn_id": turn_id if isinstance(turn_id, str) and len(turn_id) <= 256 else None}
        detail = info.get(category) if isinstance(info, dict) and category in allowed else None
        http_status = detail.get("httpStatusCode") if isinstance(detail, dict) else None
        if type(http_status) is int and 100 <= http_status <= 599:
            result["http_status"] = http_status
        return result

    def _send_packet(self, packet):
        data = (json.dumps(packet, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > self.MAX_MESSAGE_BYTES:
            raise CodexRuntimeError("Protocol message exceeds the runtime size limit.")
        with self._lock:
            if self._closed or self._process is None or self._process.poll() is not None or self._state == "error":
                raise CodexRuntimeError("Codex runtime is not running.")
        try:
            self._outbox.put_nowait(data)
        except queue.Full:
            raise CodexRuntimeError("Codex runtime request queue is full.") from None

    def _request(self, method, params):
        future = Future()
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = future
        try:
            self._send_packet({"id": request_id, "method": method, "params": params})
            return future.result(timeout=self.REQUEST_TIMEOUT)
        except FutureTimeout:
            # The remote side may have accepted a timed-out turn. Terminate this
            # process instead of allowing an ambiguous second turn to start.
            self._fail("request_timeout", "Codex protocol request timed out.")
            raise CodexRuntimeError("Codex protocol request timed out.") from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _write_loop(self):
        try:
            while True:
                data = self._outbox.get()
                if data is None:
                    return
                self._process.stdin.write(data)
                self._process.stdin.flush()
        except (OSError, ValueError):
            if not self._closed:
                self._fail("transport_closed", "Codex protocol transport closed.")

    def _read_loop(self):
        try:
            while True:
                line = self._process.stdout.readline(self.MAX_MESSAGE_BYTES + 1)
                if not line:
                    if not self._closed:
                        self._fail("process_exited", "Codex app-server exited.")
                    return
                if len(line) > self.MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                    raise ValueError("invalid protocol line")
                packet = json.loads(line)
                if not isinstance(packet, dict):
                    raise ValueError("invalid protocol packet")
                if "method" in packet:
                    if "id" in packet:
                        self._server_request(packet)
                    else:
                        self._notification(packet.get("method"), packet.get("params", {}))
                else:
                    with self._lock:
                        future = self._pending.get(packet.get("id"))
                        if future is not None and not future.done():
                            if "error" in packet:
                                error = packet["error"]
                                code = error.get("code") if isinstance(error, dict) else None
                                suffix = f" (code {code})" if isinstance(code, int) and not isinstance(code, bool) else ""
                                future.set_exception(CodexRuntimeError("Codex protocol request failed" + suffix + "."))
                            elif not isinstance(packet.get("result"), dict):
                                raise ValueError("invalid response result")
                            else:
                                future.set_result(packet["result"])
        except (OSError, ValueError, TypeError):
            if not self._closed:
                self._fail("protocol_error", "Codex returned an invalid protocol message.")

    def _stop_process(self):
        with self._stop_lock:
            if self._tree is not None:
                self._tree.close()
                self._tree = None

    def _fail(self, code, message):
        with self._lock:
            if self._closed or self._state == "error":
                return
            self._state, self._last_error = "error", message
            self._error_metadata = {"code": code, "category": "runtime_failure", "will_retry": False,
                                    "turn_id": self._turn_id}
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CodexRuntimeError(message))
        self._emit({"type": "error", "message": message, "fatal": True, **self._error_metadata})
        self._stop_process()

    @staticmethod
    def _tools(tools):
        specs = json.loads(json.dumps(tools or [], ensure_ascii=False, allow_nan=False))
        if not isinstance(specs, list):
            raise ValueError("tools must be a list of dynamic tool definitions")
        names = set()
        for spec in specs:
            if not isinstance(spec, dict):
                raise ValueError("tool definitions must be objects")
            spec.setdefault("type", "function")
            namespace = spec.get("name") if spec["type"] == "namespace" else None
            children = spec.get("tools", []) if namespace else [spec]
            if namespace and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", namespace):
                raise ValueError("invalid dynamic tool namespace")
            if not children:
                raise ValueError("dynamic tool namespace must contain tools")
            for child in children:
                if not isinstance(child, dict):
                    raise ValueError("invalid dynamic tool definition")
                child.setdefault("type", "function")
                name = child.get("name", "")
                if child["type"] != "function" or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                    raise ValueError("invalid dynamic tool definition")
                if not isinstance(child.get("description"), str) or "inputSchema" not in child:
                    raise ValueError("tool description and inputSchema are required")
                qualified = f"{namespace}.{name}" if namespace else name
                if qualified in names:
                    raise ValueError("duplicate dynamic tool name")
                names.add(qualified)
        return specs, names

    def start(self, thread_id=None, tools=None, instructions=""):
        specs, names = self._tools(tools)
        if thread_id is not None and (not isinstance(thread_id, str) or not thread_id):
            raise ValueError("thread_id must be a nonempty string")
        if not isinstance(instructions, str):
            raise TypeError("instructions must be a string")
        with self._operations:
            with self._lock:
                if self._closed:
                    raise CodexRuntimeError("Codex runtime is closed.")
                if self._process is not None:
                    if self.status()["running"] and self._thread_id and thread_id in (None, self._thread_id):
                        return {"thread_id": self._thread_id, "pid": self._process.pid}
                    raise CodexRuntimeError("Create a new runtime to reconnect this thread.")
                self._state, self._thread_id, self._allowed_tools = "starting", thread_id, names
            try:
                flags = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
                self._process = subprocess.Popen(self.command, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 stderr=subprocess.DEVNULL, shell=False, **flags)
                self._tree = _ProcessTree(self._process)
                self._writer = threading.Thread(target=self._write_loop, name="codex-stdin", daemon=True)
                self._reader = threading.Thread(target=self._read_loop, name="codex-stdout", daemon=True)
                self._writer.start()
                self._reader.start()
                self._request("initialize", {"clientInfo": {"name": "simulation_advisor", "title": "Simulation Advisor", "version": "0.1.0"},
                                              "capabilities": {"experimentalApi": True}})
                self._send_packet({"method": "initialized"})
                params = {"model": self.model, "cwd": str(self.cwd), "sandbox": "workspace-write", "approvalPolicy": "never",
                          "runtimeWorkspaceRoots": [str(self.cwd)],
                          "config": {"model_reasoning_effort": self.reasoning_effort, "sandbox_workspace_write": {"network_access": False}}}
                if instructions:
                    params["developerInstructions"] = instructions
                if thread_id:
                    params.update(threadId=thread_id, excludeTurns=True)
                else:
                    params.update(dynamicTools=specs, ephemeral=False, allowProviderModelFallback=False, experimentalRawEvents=False)
                result = self._request("thread/resume" if thread_id else "thread/start", params)
                actual_id = result.get("thread", {}).get("id")
                if not isinstance(actual_id, str) or not actual_id or (thread_id and actual_id != thread_id):
                    raise CodexRuntimeError("Codex returned an invalid thread identity.")
                if result.get("model", self.model) != self.model:
                    raise CodexRuntimeError("Codex did not retain the requested model.")
                sandbox = result.get("sandbox", {})
                if sandbox and (sandbox.get("type") != "workspaceWrite" or sandbox.get("networkAccess", False)):
                    raise CodexRuntimeError("Codex did not retain the requested workspace policy.")
                with self._lock:
                    self._thread_id = actual_id
                    if self._state == "starting":
                        self._state = "idle"
                    for turn in result["thread"].get("turns", []):
                        if turn.get("status") == "inProgress" and isinstance(turn.get("id"), str):
                            self._turn_id, self._state = turn["id"], "busy"
                    if result["thread"].get("status", {}).get("type") == "active" and self._turn_id is None:
                        raise CodexRuntimeError("Codex resumed an active thread without an active turn identity.")
                self._emit({"type": "runtime", "state": self._state, "pid": self._process.pid})
                return {"thread_id": actual_id, "pid": self._process.pid}
            except Exception as exc:
                safe = str(exc) if isinstance(exc, CodexRuntimeError) else "Codex app-server could not start."
                self._fail("startup_failed", safe)
                raise CodexRuntimeError(safe) from None

    def send(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a nonempty string")
        with self._operations:
            with self._lock:
                if not self.status()["running"] or not self._thread_id:
                    raise CodexRuntimeError("Start the Codex runtime before sending input.")
                thread_id, turn_id = self._thread_id, self._turn_id
                mode = "steer" if turn_id else "start"
                previous_state = self._state
                if mode == "start":
                    self._state = "starting_turn"
                    self._last_turn = self._error_metadata = self._last_error = None
            params = {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
            if mode == "steer":
                params["expectedTurnId"] = turn_id
            else:
                params.update(model=self.model, effort=self.reasoning_effort, cwd=str(self.cwd), approvalPolicy="never",
                              sandboxPolicy={"type": "workspaceWrite", "writableRoots": [str(self.cwd)], "networkAccess": False,
                                             "excludeTmpdirEnvVar": True, "excludeSlashTmp": True})
            try:
                result = self._request("turn/steer" if mode == "steer" else "turn/start", params)
                accepted = result.get("turnId") if mode == "steer" else result.get("turn", {}).get("id")
                if not isinstance(accepted, str) or not accepted or len(accepted) > 256 or (mode == "steer" and accepted != turn_id):
                    self._fail("protocol_error", "Codex returned an invalid turn identity.")
                    raise CodexRuntimeError("Codex returned an invalid turn identity.")
                with self._lock:
                    if accepted not in self._completed_turns and self._state != "error" and not self._closed:
                        self._turn_id = accepted
                        if self._state != "interrupting":
                            self._state = "busy"
                self._dispatch_pending_interrupt()
                return {"thread_id": thread_id, "turn_id": accepted, "mode": mode}
            except CodexRuntimeError:
                with self._lock:
                    if self._state == "starting_turn":
                        self._state = previous_state
                raise

    def interrupt(self):
        with self._lock:
            if not self.status()["busy"]:
                return {"requested": False, "turn_id": self._turn_id}
            self._interrupt_pending = True
            turn_id = self._turn_id
        if turn_id:
            self._dispatch_pending_interrupt(background=False)
        return {"requested": True, "turn_id": turn_id}

    def _dispatch_pending_interrupt(self, background=True):
        with self._lock:
            if not self._interrupt_pending or not self._turn_id or self._closed:
                return
            self._interrupt_pending = False
            thread_id, turn_id = self._thread_id, self._turn_id
            self._state = "interrupting"
        def request():
            try:
                self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            except CodexRuntimeError:
                self._emit({"type": "error", "code": "interrupt_failed", "message": "Codex interrupt request failed.", "turn_id": turn_id, "will_retry": False})
        if background:
            threading.Thread(target=request, name="codex-interrupt", daemon=True).start()
        else:
            request()

    def _notification(self, method, params):
        if not isinstance(params, dict):
            return
        with self._lock:
            if self._closed or not self._thread_id or params.get("threadId") != self._thread_id:
                return
        ids = {"turn_id": params.get("turnId"), "item_id": params.get("itemId")}
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn", {})
            turn_id, status = turn.get("id"), turn.get("status")
            if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256 or status not in ({"inProgress"} if method.endswith("started") else {"completed", "interrupted", "failed"}):
                return
            with self._lock:
                if method == "turn/started":
                    if turn_id in self._completed_turns:
                        return
                    if self._turn_id != turn_id:
                        self._last_turn = self._error_metadata = self._last_error = None
                    self._turn_id, self._state = turn_id, "busy"
                else:
                    if turn_id in self._completed_turns or self._turn_id not in (None, turn_id):
                        return  # Delayed prior-turn notifications cannot replace the current outcome.
                    self._completed_turns = (self._completed_turns + [turn_id])[-64:]
                    self._last_turn = {"turn_id": turn_id, "status": status}
                    if status == "completed":
                        self._error_metadata = self._last_error = None
                    elif status == "failed":
                        self._error_metadata = self._error_info(turn.get("error"), will_retry=False, turn_id=turn_id)
                    if self._turn_id in (None, turn_id):
                        self._turn_id, self._state, self._interrupt_pending = None, "idle", False
            if method == "turn/completed":
                self._flush_deltas(turn_id=turn_id)
            self._emit({"type": "turn", "event": "started" if method.endswith("started") else "completed", "status": status, "turn_id": turn_id,
                        "error_metadata": dict(self._error_metadata) if self._error_metadata else None})
            if method == "turn/started":
                self._dispatch_pending_interrupt()
        elif method == "item/agentMessage/delta" and isinstance(params.get("delta"), str):
            self._visible_delta(params["delta"], ids)
        elif method in {"item/started", "item/completed"}:
            item = params.get("item", {})
            if not isinstance(item, dict):
                return
            ids["item_id"] = item.get("id")
            if item.get("type") == "agentMessage" and method == "item/completed" and isinstance(item.get("text"), str):
                if item.get("phase") in (None, "commentary", "partial_answer", "final_answer"):
                    self._flush_deltas(item_id=ids["item_id"])
                    self._emit({"type": "message", "text": item["text"], "phase": item.get("phase"), **ids})
            elif item.get("type") in {"dynamicToolCall", "mcpToolCall", "commandExecution", "fileChange", "webSearch"}:
                name = item.get("tool") if item.get("type") in {"dynamicToolCall", "mcpToolCall"} else item["type"]
                self._tool_event("started" if method.endswith("started") else "completed", name, item.get("id"), ids["turn_id"], item.get("success"))
        elif method == "error":
            with self._lock:
                turn_id = ids["turn_id"]
                if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256:
                    turn_id = self._turn_id
                if turn_id in self._completed_turns or (self._turn_id and turn_id != self._turn_id):
                    return
                self._error_metadata = self._error_info(params.get("error"), will_retry=params.get("willRetry"), turn_id=turn_id)
                metadata = dict(self._error_metadata)
            self._emit({"type": "error", "message": "Codex reported an error.", "fatal": False, **metadata})

    def _visible_delta(self, text, ids):
        key = (ids["turn_id"], ids["item_id"])
        if key in self._private_items:
            return
        buffered = self._delta_tails.pop(key, "") + text
        # Keep credential introducers together even when a token boundary falls
        # inside e.g. "sk-" or an environment secret. Once detected, stream no
        # more of this item; item/completed supplies the fully redacted message.
        marker = r"(?i)sk-|\bBearer\b|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|password|authorization|client[_-]?secret"
        if re.search(marker, buffered) or any(secret in buffered for secret in self._secrets):
            self._private_items.add(key)
            return
        if len(buffered) > self._delta_tail_size:
            self._emit({"type": "text_delta", "text": buffered[:-self._delta_tail_size], **ids})
        self._delta_tails[key] = buffered[-self._delta_tail_size:]

    def _flush_deltas(self, *, turn_id=None, item_id=None):
        keys = [key for key in self._delta_tails if (turn_id is None or key[0] == turn_id) and (item_id is None or key[1] == item_id)]
        for key in keys:
            text = self._delta_tails.pop(key)
            if text and key not in self._private_items:
                self._emit({"type": "text_delta", "text": text, "turn_id": key[0], "item_id": key[1]})
        if turn_id is not None:
            self._private_items = {key for key in self._private_items if key[0] != turn_id}

    def _tool_event(self, event, name, call_id, turn_id, success=None):
        if not isinstance(name, str):
            return
        key = (call_id, event)
        with self._lock:
            if key in self._tool_phases:
                return
            self._tool_phases = (self._tool_phases + [key])[-256:]
        payload = {"type": "tool", "event": event, "name": name, "call_id": call_id, "turn_id": turn_id}
        if isinstance(success, bool):
            payload["success"] = success
        self._emit(payload)

    def _server_request(self, packet):
        request_id, method, params = packet.get("id"), packet.get("method"), packet.get("params", {})
        if method != "item/tool/call":
            # Do not auto-approve native commands, edits, permission elevation,
            # elicitation or auth refresh. The selected profile uses never.
            self._send_packet({"id": request_id, "error": {"code": -32601, "message": "Unsupported client request."}})
            self._emit({"type": "error", "code": "unsupported_request", "message": "Codex requested an unsupported client action.", "will_retry": False})
            return
        name = params.get("tool") if isinstance(params, dict) else None
        namespace = params.get("namespace") if isinstance(params, dict) else None
        if namespace:
            name = f"{namespace}.{name}"
        if not isinstance(params, dict) or params.get("threadId") != self._thread_id or name not in self._allowed_tools:
            self._send_packet({"id": request_id, "result": self._tool_failure("Tool is not registered for this runtime.")})
            return
        if not self._tool_slots.acquire(blocking=False):
            self._send_packet({"id": request_id, "result": self._tool_failure("Too many concurrent tool calls; retry later.")})
            return
        def execute():
            self._tool_event("started", name, params.get("callId"), params.get("turnId"))
            try:
                value = self.on_tool(name, params.get("arguments"))
                if isinstance(value, dict) and "contentItems" in value and "success" in value:
                    if not isinstance(value["success"], bool) or not isinstance(value["contentItems"], list):
                        raise ValueError("invalid tool result")
                    result = {"success": value["success"], "contentItems": value["contentItems"]}
                    for item in result["contentItems"]:
                        if not isinstance(item, dict) or item.get("type") != "inputText" or not isinstance(item.get("text"), str):
                            raise ValueError("only text tool results supported")
                else:
                    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
                    result = {"success": True, "contentItems": [{"type": "inputText", "text": text}]}
                if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > self.MAX_TOOL_BYTES:
                    result = self._tool_failure("Tool result exceeded the runtime limit; request a smaller result.")
            except Exception:
                result = self._tool_failure("Tool execution failed.")
            try:
                self._send_packet({"id": request_id, "result": result})
                self._tool_event("completed", name, params.get("callId"), params.get("turnId"), result["success"])
            except (CodexRuntimeError, ValueError):
                pass
            finally:
                self._tool_slots.release()
        threading.Thread(target=execute, name="codex-tool", daemon=True).start()

    @staticmethod
    def _tool_failure(message):
        return {"success": False, "contentItems": [{"type": "inputText", "text": message}]}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed, self._state, self._turn_id = True, "closed", None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CodexRuntimeError("Codex runtime is closed."))
        try:
            self._outbox.put_nowait(None)
        except queue.Full:
            pass
        self._stop_process()
        for worker in (self._reader, self._writer):
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=1)
        if self._process is not None:
            for stream in (self._process.stdin, self._process.stdout):
                if stream is not None:
                    stream.close()
        self._emit({"type": "runtime", "state": "closed"})
