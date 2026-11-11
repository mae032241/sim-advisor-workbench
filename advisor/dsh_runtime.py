"""Managed DSH SDK Host with a private, loopback-only native Agent bridge.

SDK stdio is drained without logging; it is not misrepresented as a dynamic-tool
or cancellation API. A task-owned ordinary Host plugin uses public Agent APIs.
"""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .dsh_command import launch_command
from .process_runner import _ProcessTree


class DshRuntimeError(RuntimeError):
    """Safe runtime error, excluding captured native logs and credentials."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class DshRuntime:
    backend_id = "dsh"
    START_TIMEOUT = 60
    REQUEST_TIMEOUT = 10
    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, command, cwd, on_event, on_tool, model="deepseek-flash", reasoning_effort="high", *, provider=None):
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise ValueError("command must be a nonempty argv list")
        for key, value in (("model", model), ("reasoning_effort", reasoning_effort)):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"{key} must be a nonempty bounded string")
        provider = os.environ.get("SIM_ADVISOR_DSH_PROVIDER", "deepseek-official") if provider is None else provider
        if not isinstance(provider, str) or not provider.strip() or len(provider) > 256:
            raise ValueError("provider must be a nonempty bounded string")
        self.cwd = Path(cwd).resolve(strict=True)
        if not self.cwd.is_dir() or not callable(on_event) or not callable(on_tool):
            raise ValueError("cwd must be a directory and callbacks must be callable")
        self.command, self.on_event, self.on_tool = list(command), on_event, on_tool
        self.model, self.reasoning_effort, self.provider = model, reasoning_effort, provider
        self._lock, self._operations, self._cleanup_lock = threading.RLock(), threading.Lock(), threading.RLock()
        self._process = self._tree = self._server = self._server_thread = None
        self._workers = []
        self._state, self._thread_id, self._turn_id, self._last_error = "new", None, None, None
        self._ready, self._closed = threading.Event(), False
        self._token, self._control_url, self._config = secrets.token_urlsafe(32), None, None
        self._tools, self._native_tools = set(), []
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._secrets = [value for key, value in os.environ.items() if len(value) >= 8 and re.search(r"token|password|secret|api.?key", key, re.I)]

    def _safe(self, value):
        text = str(value).replace(self._token, "[redacted]")
        for secret in self._secrets:
            text = text.replace(secret, "[redacted]")
        text = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+|\bsk-[A-Za-z0-9_-]{8,}", "[redacted]", text)
        return re.sub(r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|authorization)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''', r"\1[redacted]", text)

    def _emit(self, event):
        event = {key: self._safe(value) if isinstance(value, str) else value for key, value in event.items()}
        event.setdefault("thread_id", self._thread_id)
        try:
            self.on_event(event)
        except Exception:
            pass

    def status(self):
        with self._lock:
            running = self._process is not None and self._process.poll() is None and not self._closed and self._state != "error"
            return {"running": running, "busy": running and self._state in {"busy", "interrupting"}, "state": self._state,
                    "pid": self._process.pid if self._process else None, "thread_id": self._thread_id, "turn_id": self._turn_id,
                    "model": self.model, "reasoning_effort": self.reasoning_effort, "provider": self.provider,
                    "backend_id": self.backend_id, "last_error": self._last_error, "tool_names": list(self._native_tools)}

    def _callback_server(self):
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def handle_request(self, method):
                try:
                    if self.headers.get("Origin") or not hmac.compare_digest(self.headers.get("X-DSH-Bridge-Token", "").encode(), runtime._token.encode()):
                        return self.respond({"error": "Unauthorized"}, 403)
                    if method == "GET" and self.path == "/config":
                        return self.respond(runtime._config)
                    length = int(self.headers.get("Content-Length", "0"))
                    if method != "POST" or not 0 < length <= runtime.MAX_BYTES or self.headers.get("Transfer-Encoding"):
                        raise ValueError("invalid callback body")
                    self.connection.settimeout(10)
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError("invalid callback object")
                    return self.respond(runtime._callback(self.path, data))
                except (ValueError, TypeError, UnicodeError):
                    self.respond({"error": "Invalid bridge request"}, 422)
                except Exception:
                    self.respond({"error": "Bridge callback failed"}, 500)

            def respond(self, value, status=200):
                encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                if len(encoded) > runtime.MAX_BYTES:
                    status, encoded = 422, b'{"error":"Bridge result exceeds size limit"}'
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def do_GET(self):
                self.handle_request("GET")

            def do_POST(self):
                self.handle_request("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._server_thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._server_thread.start()
        return f"http://127.0.0.1:{self._server.server_port}"

    def _callback(self, path, data):
        if self._closed:
            return {"accepted": False}
        if path == "/ready":
            control = urlsplit(data.get("control_url", ""))
            thread_id = data.get("thread_id")
            if control.scheme != "http" or control.hostname != "127.0.0.1" or not control.port or control.username or control.password or control.path not in {"", "/"} or control.query or control.fragment:
                raise ValueError("invalid control origin")
            if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 256 or (self._config["thread_id"] and thread_id != self._config["thread_id"]):
                raise ValueError("invalid session identity")
            if any(data.get(key) != getattr(self, key) for key in ("model", "provider", "reasoning_effort")):
                self._fail("configuration_mismatch", "DSH 未保留所选模型、提供方或推理强度。")
                return {"accepted": False}
            with self._lock:
                self._control_url, self._thread_id = data["control_url"], thread_id
                self._state = "busy" if data.get("busy") else "idle"
                self._native_tools = [name for name in data.get("tool_names", [])[:256] if isinstance(name, str) and len(name) <= 128]
            self._ready.set()
            self._emit({"type": "runtime", "state": self._state})
            return {"accepted": True}
        if path == "/error":
            message = "DSH 无法恢复指定会话；未创建替代会话。" if self._config["thread_id"] else "DSH 原生会话准备失败；请检查模型、提供方及本机配置。"
            self._fail("session_prepare_failed", message)
            return {"accepted": False}
        if path == "/event":
            self._event(data)
            return {"accepted": True}
        if path == "/tool":
            if data.get("name") not in self._tools or data.get("thread_id") != self._thread_id:
                return {"success": False, "error": "Tool is not registered for this managed session."}
            try:
                result = self.on_tool(data["name"], data.get("arguments"))
                if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > self.MAX_BYTES - 1024:
                    raise ValueError("result too large")
                value = {"success": True, "value": result}
            except Exception:
                value = {"success": False, "error": "本地规划工具执行失败；未返回内部日志。"}
            return value
        raise ValueError("unsupported callback")

    def _event(self, data):
        if data.get("thread_id") != self._thread_id or self._closed:
            return
        kind = data.get("type")
        ids = {key: data.get(key) for key in ("item_id", "turn_id") if isinstance(data.get(key), str) and len(data[key]) <= 256}
        # The DSH Host publishes only the latest reply at a successful turn/end.
        # Ignore legacy token/tool notifications instead of mirroring progress.
        if kind == "message" and isinstance(data.get("text"), str) and len(data["text"]) <= 16000 and ids.get("item_id"):
            self._emit({"type": kind, "text": data["text"], **ids})
        elif kind == "turn" and data.get("event") in {"started", "completed"}:
            with self._lock:
                started = data["event"] == "started"
                self._state, self._turn_id = ("busy", ids.get("turn_id")) if started else ("idle", None)
            self._emit({"type": "turn", "event": data["event"], "status": "inProgress" if started else data.get("status", "completed"), **ids})
        elif kind == "error":
            delivery_failed = data.get("code") == "reply_delivery_failed"
            self._emit({"type": "error", "code": "reply_delivery_failed" if delivery_failed else "dsh_error",
                        "message": "DSH 本轮可能已完成，但网页回复未确认送达；请先在 DSH 核对，不会自动重新执行任务。" if delivery_failed else "DSH 本轮执行未完成；已保存方案保留。",
                        "will_retry": False, **ids})

    def _control(self, path, data, timeout=None):
        if not self._control_url:
            raise DshRuntimeError("DSH 控制通道尚未就绪。")
        request = Request(self._control_url + path, data=json.dumps(data, ensure_ascii=False).encode("utf-8"), method="POST",
                          headers={"Content-Type": "application/json", "X-DSH-Bridge-Token": self._token})
        try:
            with self._opener.open(request, timeout=timeout or self.REQUEST_TIMEOUT) as response:
                raw = response.read(self.MAX_BYTES + 1)
                if len(raw) > self.MAX_BYTES:
                    raise ValueError("response too large")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError("invalid response")
                return result
        except (URLError, OSError, ValueError):
            raise DshRuntimeError("DSH 控制请求失败或超时；请检查当前会话状态。") from None

    def start(self, thread_id=None, tools=None, instructions=""):
        if thread_id is not None and (not isinstance(thread_id, str) or not thread_id.strip() or len(thread_id) > 256):
            raise ValueError("thread_id must be a nonempty bounded string")
        if not isinstance(instructions, str) or len(instructions) > 128000:
            raise ValueError("instructions must be bounded text")
        specs = json.loads(json.dumps([] if tools is None else tools, ensure_ascii=False, allow_nan=False))
        if not isinstance(specs, list) or len(specs) > 64:
            raise ValueError("tools must be a bounded list")
        names = set()
        for tool in specs:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool["name"]) or tool["name"] in names or not isinstance(tool.get("description"), str) or not isinstance(tool.get("inputSchema"), dict):
                raise ValueError("invalid dynamic tool definition")
            names.add(tool["name"])
        with self._operations:
            if self._closed:
                raise DshRuntimeError("DSH runtime is closed.")
            if self._process is not None:
                if self.status()["running"] and self._thread_id and thread_id in (None, self._thread_id):
                    return {"thread_id": self._thread_id, "pid": self._process.pid}
                raise DshRuntimeError("请创建新运行时恢复该 DSH 会话。")
            self._tools, self._state = names, "starting"
            self._config = {"thread_id": thread_id, "cwd": str(self.cwd), "provider": self.provider, "model": self.model,
                            "reasoning_effort": self.reasoning_effort, "tools": specs, "instructions": instructions}
            try:
                callback = self._callback_server()
                helper = Path(__file__).resolve().parents[1] / "integrations/dsh-managed-host/index.js"
                runtime_dir = self.cwd / ".dsh-managed"
                runtime_dir.mkdir(exist_ok=True)
                patch = runtime_dir / "host.patch.yml"
                patch.write_text("- insert:\n    - id: sim-advisor-managed-host\n      name: " + json.dumps(helper.as_uri()) + "\n      config: {}\n", encoding="utf-8")
                env = dict(os.environ, SIM_ADVISOR_DSH_CALLBACK=callback, SIM_ADVISOR_DSH_BRIDGE_TOKEN=self._token)
                flags = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
                self._process = subprocess.Popen(launch_command(self.command, patch), cwd=self.cwd, env=env, shell=False,
                                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **flags)
                self._tree = _ProcessTree(self._process)
                for stream in (self._process.stdout, self._process.stderr):
                    worker = threading.Thread(target=self._drain, args=(stream,), daemon=True)
                    self._workers.append(worker); worker.start()
                monitor = threading.Thread(target=self._monitor, daemon=True)
                self._workers.append(monitor); monitor.start()
                if not self._ready.wait(self.START_TIMEOUT):
                    self._fail("startup_timeout", "DSH 会话准备超时；未启动替代后端。")
                if self._state == "error" or not self._thread_id:
                    raise DshRuntimeError(self._last_error or "DSH 会话准备失败。")
                return {"thread_id": self._thread_id, "pid": self._process.pid}
            except Exception as error:
                message = str(error) if isinstance(error, DshRuntimeError) else "DSH SDK Host 无法启动；请检查本机 CLI 与启动配置。"
                self._fail("startup_failed", message)
                raise DshRuntimeError(message) from None

    @staticmethod
    def _drain(stream):
        try:
            while stream.read(65536):
                pass  # SDK stdio can contain reasoning/full session events. Never retain/forward it.
        except (OSError, ValueError):
            pass

    def _monitor(self):
        try:
            self._process.wait()
        except OSError:
            # Windows tree cleanup can close the already-reaped process handle
            # while this concurrent wait is finishing its final exit-code read.
            if self._closed or self._state == "error":
                return
        if not self._closed:
            self._fail("process_exited", "DSH SDK Host 已退出。")

    def _fail(self, code, message):
        with self._lock:
            if self._closed or self._state == "error":
                return
            self._state, self._last_error = "error", message
            self._ready.set()
        self._emit({"type": "error", "code": code, "message": message, "will_retry": False})
        self._cleanup()

    def send(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 128000:
            raise ValueError("text must be nonempty bounded text")
        with self._operations:
            if not self.status()["running"] or not self._thread_id:
                raise DshRuntimeError("请先连接 DSH 会话。")
            return self._control("/send", {"text": text})

    def interrupt(self):
        if not self.status()["running"]:
            return {"requested": False, "turn_id": None}
        result = self._control("/interrupt", {})
        return result

    def _cleanup(self):
        with self._cleanup_lock:
            if self._tree is not None:
                self._tree.close(); self._tree = None
            if self._server is not None:
                self._server.shutdown(); self._server.server_close(); self._server = None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed, self._state, self._turn_id = True, "closed", None
            self._ready.set()
        if self._control_url and self._process is not None and self._process.poll() is None:
            try:
                self._control("/close", {}, timeout=5)
                self._process.stdin.close()
                self._process.wait(timeout=3)
            except (DshRuntimeError, OSError, subprocess.TimeoutExpired):
                pass
        self._cleanup()
        for worker in self._workers:
            if worker is not threading.current_thread():
                worker.join(timeout=0.5)
        if self._process is not None:
            for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                if stream is not None:
                    stream.close()
        self._emit({"type": "runtime", "state": "closed"})


DSHRuntime = DshRuntime
