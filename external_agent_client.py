"""Stdlib client for an existing Agent to join a local workbench conversation.

The harness owns its model, scheduler and cancellation. This client only moves
commands/events and optionally polls on a separate heartbeat/inbox thread. It
does not start an Agent, inspect credentials, or manage the owner's process.
Public planning tools remain available through list_tools/call_tool or MCP.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ExternalAgentError(RuntimeError):
    def __init__(self, payload, status=None):
        self.payload, self.status = payload, status
        super().__init__(str(payload.get("error", "External Agent request failed")))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def resolve_workbench_url(url=None, *, data_dir=None):
    if url is None:
        directory = Path(data_dir) if data_dir is not None else Path(os.environ.get("SIM_ADVISOR_DATA", Path(__file__).resolve().parent / "data"))
        try:
            url = json.loads((directory / "server.json").read_text(encoding="utf-8"))["url"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("无法读取 data/server.json；先启动工作台或指定 --url。") from exc
    if not isinstance(url, str) or not url or any(c.isspace() for c in url):
        raise ValueError("工作台地址必须是本机 HTTP 地址。")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
        if host == "localhost":
            host = "127.0.0.1"
        address = ipaddress.ip_address(host or "")
    except ValueError as exc:
        raise ValueError("工作台地址必须使用 localhost 或 loopback IP。") from exc
    if (parsed.scheme != "http" or not address.is_loopback or "%" in (host or "")
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or port == 0):
        raise ValueError("仅支持无凭据、无路径的 loopback HTTP 地址。")
    netloc = f"[{address}]" if address.version == 6 else str(address)
    return urlunsplit(("http", netloc + (f":{port}" if port is not None else ""), "", "", ""))


class ExternalAgentClient:
    """One registered owner PID and session; no automatic model operations.

    start_pump()/receive() keep HTTP polling independent of a long-running model
    callback. The harness must continue dispatching interrupt/disconnect itself.
    The inbox is process-local, not a durable queue; no writes are auto-retried.
    """
    def __init__(self, session_id, url=None, *, timeout=10, binding_id=None):
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise ValueError("session_id 必须为32位十六进制方案 ID。")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("timeout 必须为 0–300 秒之间的正数。")
        if binding_id is not None and (not isinstance(binding_id, str) or not binding_id):
            raise ValueError("binding_id 必须为非空字符串。")
        self.session_id, self.url, self.timeout = session_id, resolve_workbench_url(url), timeout
        self.binding_id, self.instructions, self.registration = binding_id, "", None
        self.cursor, self.connected, self.last_error = 0, False, None
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._lock, self._poll_lock = threading.RLock(), threading.Lock()
        self._inbox = queue.Queue()
        self._stop = threading.Event()
        self._pump = None

    def _request(self, method, path, body=None, *, timeout=None):
        try:
            data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("请求必须是合法 JSON。") from exc
        if data is not None and len(data) > 2 * 1024 * 1024:
            raise ValueError("请求超过2 MiB；请缩小消息或工具参数。")
        request = Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout if timeout is None else timeout) as response:
                return self._decode(response)
        except HTTPError as exc:
            try:
                payload = self._decode(exc)
            except ExternalAgentError:
                payload = {"error": "工作台返回无效错误响应。"}
            finally:
                exc.close()
            raise ExternalAgentError(payload, exc.code) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise ExternalAgentError({"error": "本机工作台不可达或请求超时；操作结果尚未确认，重试写入前请读取当前状态。"}) from exc

    @staticmethod
    def _decode(response):
        raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ExternalAgentError({"error": "工作台响应超过8 MiB；请分页读取。"})
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ExternalAgentError({"error": "工作台未返回合法 JSON。"}) from exc
        if not isinstance(value, dict):
            raise ExternalAgentError({"error": "工作台响应必须是 JSON 对象。"})
        return value

    def _bound_request(self, action, **body):
        with self._lock:
            binding = self.binding_id
        if not binding:
            raise RuntimeError("尚未注册外部 Agent。")
        return self._request("POST", f"/api/sessions/{self.session_id}/external/{action}", dict(body, binding_id=binding), timeout=min(self.timeout, 10))

    def register(self, name, *, backend=None, model=None, reasoning_effort=None, conversation_id=None):
        if self.binding_id:
            raise RuntimeError("客户端已有 binding；请先 unregister。")
        body = {"name": name, "pid": os.getpid()}
        body.update({k: v for k, v in {"backend": backend, "model": model, "reasoning_effort": reasoning_effort, "conversation_id": conversation_id}.items() if v is not None})
        value = self._request("POST", f"/api/sessions/{self.session_id}/external/register", body, timeout=min(self.timeout, 10))
        if not isinstance(value.get("binding_id"), str) or not value["binding_id"] or value.get("session_id") != self.session_id or not isinstance(value.get("instructions"), str):
            raise ExternalAgentError({"error": "注册响应缺少匹配的 binding_id、session_id 或 instructions。"})
        with self._lock:
            self.registration, self.binding_id, self.instructions = value, value["binding_id"], value["instructions"]
            self.cursor, self.connected, self.last_error = 0, True, None
            self._inbox = queue.Queue()
        return value

    def poll(self, after=None):
        if self._pump and self._pump.is_alive() and threading.current_thread() is not self._pump:
            raise RuntimeError("后台 pump 已在接收命令；请调用 receive，避免两个消费者推进游标。")
        with self._poll_lock:
            after = self.cursor if after is None else after
            if type(after) is not int or after < 0:
                raise ValueError("after 必须为非负整数。")
            value = self._bound_request("poll", after=after)
            commands, cursor, connected = value.get("commands"), value.get("cursor"), value.get("connected")
            if not isinstance(commands, list) or type(cursor) is not int or cursor < after or type(connected) is not bool:
                raise ExternalAgentError({"error": "收件响应 commands/cursor/connected 无效。"})
            if any(not isinstance(c, dict) or c.get("type") not in {"message", "interrupt", "disconnect"} or "id" not in c for c in commands):
                raise ExternalAgentError({"error": "收件响应含无效命令。"})
            with self._lock:
                self.cursor, self.connected, self.last_error = cursor, connected, None
            return value

    def publish(self, event):
        if not isinstance(event, dict) or event.get("type") not in {"delta", "message", "state", "activity", "error"}:
            raise ValueError("event.type 必须为 delta/message/state/activity/error。")
        return self._bound_request("event", event=event)

    def list_tools(self):
        """Discover the same backend-independent public tools as the MCP bridge."""
        value = self._request("GET", "/api/tools")
        if not isinstance(value.get("tools"), list):
            raise ExternalAgentError({"error": "公共工具响应缺少 tools 数组。"})
        return value["tools"]

    def call_tool(self, name, arguments, *, timeout=None):
        """Pass arguments unchanged: include session_id explicitly when needed."""
        value = self._request("POST", "/api/tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        if not isinstance(value.get("result"), dict):
            raise ExternalAgentError({"error": "公共工具响应缺少 result 对象。"})
        return value["result"]

    def call_bound_tool(self, name, arguments, *, timeout=None):
        """Use this live binding's fixed plan and reported backend attribution."""
        with self._lock:
            binding = self.binding_id
        if not binding:
            raise RuntimeError("绑定工具调用前需 register。")
        value = self._request("POST", f"/api/sessions/{self.session_id}/external/tool",
                              {"binding_id": binding, "name": name, "arguments": arguments}, timeout=timeout)
        if not isinstance(value.get("result"), dict):
            raise ExternalAgentError({"error": "绑定工具响应缺少 result 对象。"})
        return value["result"]

    def start_pump(self, *, interval=2):
        if not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0 < interval <= 10:
            raise ValueError("心跳轮询间隔须大于0且不超过10秒。")
        if not self.binding_id:
            raise RuntimeError("启动 pump 前需 register。")
        if self._pump and self._pump.is_alive():
            return self
        self._stop.clear()
        self.last_error = None
        def pump():
            try:
                while not self._stop.is_set():
                    value = self.poll()
                    for command in value["commands"]:
                        self._inbox.put(command)
                    if not value["connected"]:
                        return
                    self._stop.wait(interval)
            except (ExternalAgentError, ValueError, RuntimeError) as exc:
                self.last_error, self.connected = exc, False
            finally:
                self._stop.set()
        self._pump = threading.Thread(target=pump, name="workbench-external-inbox", daemon=True)
        self._pump.start()
        return self

    def receive(self, timeout=None):
        """Return one queued wire command or None; no callback runs on the pump."""
        if self._pump is None:
            raise RuntimeError("receive 前需 start_pump。")
        if timeout is not None and (not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("receive timeout 必须为非负秒数或 None。")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                wait = .25 if deadline is None else max(0, min(.25, deadline - time.monotonic()))
                return self._inbox.get(timeout=wait)
            except queue.Empty:
                if self.last_error:
                    raise self.last_error
                if self._stop.is_set() or (deadline is not None and time.monotonic() >= deadline):
                    return None

    def stop_pump(self):
        self._stop.set()
        if self._pump and threading.current_thread() is not self._pump:
            self._pump.join(timeout=min(self.timeout, 10) + 1)

    def unregister(self):
        self.stop_pump()
        if not self.binding_id:
            return {"unregistered": False}
        try:
            value = self._bound_request("unregister")
        except ExternalAgentError as exc:
            if exc.status not in {404, 410}:
                raise
            value = {"unregistered": False, "already_expired": True}
        with self._lock:
            self.binding_id, self.connected = None, False
        return value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        try:
            self.unregister()
        except ExternalAgentError:
            if exc_type is None:
                raise


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="既有外部 Agent 的工作台会话客户端；不启动模型。register 保持本进程存活并收件。")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("register", "poll", "event", "unregister"):
        command = sub.add_parser(action)
        command.add_argument("session_id")
        command.add_argument("--url", help="默认读取 data/server.json")
        command.add_argument("--timeout", type=float, default=10)
        if action != "register":
            command.add_argument("--binding-id", required=True, help="仍存活的注册进程返回的绑定 ID")
        if action == "register":
            command.add_argument("--name", default="External Agent inbox")
            command.add_argument("--backend")
            command.add_argument("--model")
            command.add_argument("--reasoning-effort")
            command.add_argument("--conversation-id")
            command.add_argument("--interval", type=float, default=2)
        if action == "poll":
            command.add_argument("--after", type=int, default=0)
        if action == "event":
            command.add_argument("--type", required=True, choices=["delta", "message", "state", "activity", "error"])
            command.add_argument("--text")
            command.add_argument("--message-id")
            command.add_argument("--turn-id")
            command.add_argument("--status", choices=["running", "idle"])
    args = parser.parse_args(argv)
    client = None
    def output(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    try:
        client = ExternalAgentClient(args.session_id, args.url, timeout=args.timeout, binding_id=getattr(args, "binding_id", None))
        if args.action == "register":
            registration = client.register(args.name, backend=args.backend, model=args.model, reasoning_effort=args.reasoning_effort, conversation_id=args.conversation_id)
            client.start_pump(interval=args.interval)
            output(registration)
            while True:
                command = client.receive(timeout=.5)
                if command:
                    output({"command": command})
                    if command["type"] == "disconnect":
                        break
                elif not client.connected:
                    break
        elif args.action == "poll":
            output(client.poll(after=args.after))
        elif args.action == "event":
            event = {k: v for k, v in {"type": args.type, "text": args.text, "message_id": args.message_id, "turn_id": args.turn_id, "status": args.status}.items() if v is not None}
            output(client.publish(event))
        else:
            output(client.unregister())
    except KeyboardInterrupt:
        pass
    except (ExternalAgentError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if client and args.action == "register":
            try:
                client.unregister()
            except ExternalAgentError:
                print("工作台不可达；注册将由心跳/PID检查过期，不会终止本进程之外的 Agent。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
