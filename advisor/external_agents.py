"""A small plan-bound inbox for cooperative external Agent processes.

No process is started or killed. Registration tokens are private credentials;
status and normalized chat events never contain them. Only state events decide
running/idle: a completed assistant message alone does not prove a turn ended.
"""
from __future__ import annotations

import hmac
import json
from pathlib import Path
import threading
import time
import uuid

from .library_store import process_alive as _process_alive
from .sessions import ConflictError, write_json
from .workspace_paths import WorkspacePaths


class ExternalAgentBridge:
    TTL = 30
    MAX_COMMANDS = 256
    MAX_TEXT = 16000

    def __init__(self, data_dir, sessions, *, clock=time.time, process_alive=_process_alive):
        self.workspace_paths = WorkspacePaths(data_dir)
        self.directory = Path(data_dir) / "external-agents"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sessions, self.clock, self.process_alive = sessions, clock, process_alive
        self.lock = threading.RLock()

    def _path(self, sid):
        if not isinstance(sid, str) or not sid:
            raise ValueError("session_id 必须是方案 ID 文本。")
        # Validate before constructing a filesystem path; head() avoids re-parsing an unchanged plan file.
        head = getattr(self.sessions, "head", None)
        head(sid) if callable(head) else self.sessions.get(sid)
        return self.directory / (sid + ".json")

    def _load(self, sid):
        path = self._path(sid)
        record = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        if record is not None:
            owner = record.get("workspace_root")
            moved = Path(owner) != self.workspace_paths.root if owner else bool(self.workspace_paths.previous_roots)
            if moved:
                # Never treat an unrelated same-number PID on the new host as
                # the external harness that owned the copied binding token.
                record.update(detached=True, state="disconnected", turn_id=None, activity="",
                              offline_reason="workspace_relocated", workspace_root=str(self.workspace_paths.root))
                write_json(path, record)
        return record

    def _save(self, sid, record):
        record["workspace_root"] = str(self.workspace_paths.root)
        write_json(self._path(sid), record)

    @staticmethod
    def _text(value, field, limit, *, optional=False):
        if value is None and optional:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{field} 必须是非空文本，且不超过 {limit} 字符。")
        return value

    def _pid_live(self, pid):
        try:
            return bool(self.process_alive(pid))
        except (OSError, ValueError, OverflowError):
            return False

    def _status(self, record):
        if record is None:
            return {"registered": False, "alive": False, "pid": None, "name": None, "model": None,
                    "backend": "external-agent", "reasoning_effort": None, "conversation_id": None, "status": "disconnected",
                    "turn_id": None, "activity": "", "last_seen": None, "offline_reason": "not_registered"}
        registered = not record["detached"]
        reason = record.get("offline_reason") if not registered else None
        if registered and not self._pid_live(record["pid"]):
            reason = "process_exited"
        elif registered and max(0, self.clock() - record["last_seen"]) > self.TTL:
            reason = "heartbeat_timeout"
        alive = registered and reason is None
        return {"registered": registered, "alive": alive,
                "backend": record.get("backend") or "external-agent",
                **{key: record.get(key) for key in ("pid", "name", "model", "reasoning_effort", "conversation_id", "last_seen")},
                "status": record["state"] if alive else "disconnected", "turn_id": record.get("turn_id") if alive else None,
                "activity": record.get("activity", "") if alive else "", "offline_reason": reason}

    def status(self, sid):
        with self.lock:
            return self._status(self._load(sid))

    def register(self, sid, metadata):
        with self.lock:
            old = self._load(sid)
            if not isinstance(metadata, dict) or set(metadata) - {"name", "pid", "backend", "model", "reasoning_effort", "conversation_id"}:
                raise ValueError("注册信息必须包含 name、pid，可选 backend、model、reasoning_effort、conversation_id。")
            name = self._text(metadata.get("name"), "name", 128)
            backend = self._text(metadata.get("backend", "external-agent"), "backend", 128)
            pid = metadata.get("pid")
            if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF or not self._pid_live(pid):
                raise ValueError("pid 必须是本机存活进程的正整数 ID。")
            optional = {key: self._text(metadata.get(key), key, limit, optional=True)
                        for key, limit in (("model", 128), ("reasoning_effort", 64), ("conversation_id", 256))}
            if old is not None and self._status(old)["alive"]:
                raise ConflictError("该方案已有存活的外部 Agent 绑定；请先断开。")
            at = self.clock()
            record = {"binding_id": str(uuid.uuid4()), "message_scope": uuid.uuid4().hex,
                      "name": name, "pid": pid, "backend": backend, **optional, "registered_at": at, "last_seen": at,
                      "state": "idle", "turn_id": None, "activity": "", "detached": False,
                      "offline_reason": None, "commands": [], "cursor": old.get("cursor", 0) if old else 0}
            self._save(sid, record)
            return {"binding_id": record["binding_id"], "registered_at": at, **self._status(record)}

    @staticmethod
    def _authorize(record, binding_id):
        if record is None or not isinstance(binding_id, str) or not binding_id.isascii() or not hmac.compare_digest(record["binding_id"], binding_id):
            raise PermissionError("外部 Agent 绑定凭据无效或已被替换。")

    def planning_provenance(self, sid, binding_id):
        """Authorize a planning call without reviving an expired heartbeat.

        The caller serializes tool calls with registration/disconnection for this
        session. Polling keeps its separate short lock so a slow search does not
        prevent the owner's heartbeat.
        """
        with self.lock:
            record = self._load(sid)
            self._authorize(record, binding_id)
            if not self._status(record)["alive"]:
                raise PermissionError("外部 Agent 绑定已断开、心跳过期或进程退出；请先恢复有效绑定。")
            return {"backend": record.get("backend") or "external-agent",
                    **{key: record.get(key) for key in ("model", "reasoning_effort", "conversation_id")}}

    def _enqueue(self, record, command_type, text):
        if command_type == "disconnect" and record["detached"]:
            previous = next((c for c in reversed(record["commands"]) if c["type"] == "disconnect"), None)
            if previous:
                return dict(previous)
        if len(record["commands"]) >= self.MAX_COMMANDS and command_type != "disconnect":
            raise ConflictError("外部 Agent 收件箱已满，请先处理已有命令或重新注册。")
        record["cursor"] += 1
        command = {"id": record["cursor"], "type": command_type, "text": text, "at": self.clock()}
        record["commands"].append(command)
        if command_type == "disconnect":
            record.update(detached=True, state="idle", turn_id=None, activity="", offline_reason="detached")
        return dict(command)

    def enqueue(self, sid, type, text=None):
        if type not in ("message", "interrupt", "disconnect"):
            raise ValueError("命令 type 必须是 message、interrupt 或 disconnect。")
        if type == "message":
            text = self._text(text, "text", 12000)
        elif text is not None:
            raise ValueError("interrupt/disconnect 命令不接受 text。")
        with self.lock:
            record = self._load(sid)
            if record is None:
                raise ConflictError("该方案尚未注册外部 Agent。")
            if type != "disconnect" and not self._status(record)["alive"]:
                raise ConflictError("外部 Agent 已离线，请先恢复心跳或重新注册。")
            command = self._enqueue(record, type, text)
            self._save(sid, record)
            return command

    def poll(self, sid, binding_id, after=0):
        if type(after) is not int or after < 0:
            raise ValueError("after 必须是非负整数游标。")
        with self.lock:
            record = self._load(sid)
            self._authorize(record, binding_id)
            if after > record["cursor"]:
                raise ValueError("after 超过当前收件箱游标。")
            record["last_seen"] = self.clock()
            # after is the client's consumed cursor. Drop only commands the
            # client explicitly passed, keeping this a bounded, simple inbox.
            record["commands"] = [c for c in record["commands"] if c["id"] > after]
            self._save(sid, record)
            return {"commands": [dict(c) for c in record["commands"]], "cursor": record["cursor"],
                    "connected": self._status(record)["alive"]}

    def disconnect(self, sid):
        with self.lock:
            record = self._load(sid)
            if record is not None:
                self._enqueue(record, "disconnect", None)
                self._save(sid, record)
            return self._status(record)

    def unregister(self, sid, binding_id):
        with self.lock:
            record = self._load(sid)
            self._authorize(record, binding_id)
            record.update(detached=True, state="idle", turn_id=None, activity="", offline_reason="unregistered")
            self._save(sid, record)
            return self._status(record)

    def publish(self, sid, binding_id, event):
        with self.lock:
            record = self._load(sid)
            self._authorize(record, binding_id)
            if record["detached"]:
                return None
            if not isinstance(event, dict) or event.get("type") not in ("delta", "message", "state", "activity", "error"):
                raise ValueError("事件必须是 delta、message、state、activity 或 error 对象。")
            kind = event["type"]
            turn_id = self._text(event.get("turn_id"), "turn_id", 128, optional=True)
            turn_id = turn_id if turn_id is not None else record.get("turn_id")
            normalized = {"turn_id": turn_id}
            if kind in ("delta", "message"):
                message_id = self._text(event.get("message_id"), "message_id", 128)
                text = event.get("text")
                if not isinstance(text, str) or not text or len(text) > self.MAX_TEXT:
                    raise ValueError(f"text 必须是非空文本，且不超过 {self.MAX_TEXT} 字符。")
                normalized.update(type="text_delta" if kind == "delta" else "message", text=text,
                                  item_id="external-" + record["message_scope"] + ":" + message_id)
            elif kind == "state":
                state = event.get("status")
                if state not in ("running", "idle"):
                    raise ValueError("state.status 必须是 running 或 idle。")
                normalized.update(type="turn", event="started" if state == "running" else "completed",
                                  status="inProgress" if state == "running" else "completed")
            elif kind == "activity":
                activity = event.get("text", "")
                if not isinstance(activity, str) or len(activity) > 1000:
                    raise ValueError("activity.text 必须是不超过 1000 字符的文本。")
                normalized.update(type="tool", name=activity)
            else:
                text = self._text(event.get("text", event.get("message")), "error.text", 2000)
                normalized.update(type="error", code="external_agent_error", message=text, will_retry=False)
            record["last_seen"] = self.clock()
            if not self._pid_live(record["pid"]):
                self._save(sid, record)
                return None
            if kind == "state":
                record["state"] = state
                record["turn_id"] = turn_id if state == "running" else None
                if state == "idle":
                    record["activity"] = ""
            elif kind == "activity":
                record["activity"] = activity
            self._save(sid, record)
            return normalized
