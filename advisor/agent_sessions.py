"""Plan-bound agent conversations; business tools live in PlanningService."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from collections import deque
from pathlib import Path

from .planning_service import PlanningService,tool_definitions,SAVE_SCHEMA,PATCH_SCHEMA,CONTRACT_TOOLS
from .runtime_adapters import (get_runtime_factory,runtime_command,MANAGED_BACKENDS,
    managed_backend_key,managed_defaults,RuntimeUnavailableError)
from .sessions import ConflictError, now, write_json
from .external_agents import ExternalAgentBridge
from .workspace_paths import WorkspacePaths
from .research_policy import load_planner_instructions








PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "comsol_planner.v1.txt"
INSTRUCTIONS = load_planner_instructions(PROMPT_PATH)
_UNSET = object()




def planning_tools():
    return tool_definitions(bound=True,include_alias=True)


# A resumed Codex thread keeps its original dynamic tools. Compare a startup
# constant rather than inspecting configuration or rehashing on each request.
PLANNING_CONTRACT_SHA256 = hashlib.sha256(json.dumps(
    {"tools": planning_tools(), "instructions": INSTRUCTIONS},
    ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class AgentSessions:
    def __init__(self, data_dir, sessions, kb, *, runtime_factory=None, tools=None):
        self.data_dir, self.sessions, self.kb = Path(data_dir), sessions, kb
        self.workspace_paths = WorkspacePaths(self.data_dir)
        self.directory = self.data_dir / "agent-sessions"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.runtime_factory = get_runtime_factory(runtime_factory)
        self.backend_id = getattr(self.runtime_factory,"backend_id","custom-agent")
        self.default_managed_backend = managed_backend_key(self.runtime_factory)
        if self.default_managed_backend == "none":
            self.default_managed_backend = "codex"  # Configuration may be chosen; launch stays disabled.
        self.tools = tools or PlanningService(self.data_dir,sessions,kb)
        self.external = ExternalAgentBridge(self.data_dir,sessions)
        self.entries = {}
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.monitor = threading.Thread(target=self._monitor, daemon=True)
        self.monitor.start()

    def _entry(self, sid):
        session = self.sessions.get(sid)  # Validate identity before forming any path.
        with self.lock:
            previous = self.entries.get(sid)
            if previous is not None and previous.get("lifecycle_revision", 0) != session.get("lifecycle_revision", 0):
                # A restored plan keeps its conversation, but pending commands
                # and callbacks from its deleted incarnation are no longer valid.
                previous["deleted"] = True
                self.entries.pop(sid, None)
            if sid not in self.entries:
                path = self.directory / (sid + ".json")
                record = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
                previous_root = record.get("workspace_root")
                relocated = bool(record and (Path(previous_root) != self.data_dir.resolve() if previous_root
                                             else self.workspace_paths.previous_roots))
                # Pre-selector records are Codex history even if this server
                # was subsequently started with a different default backend.
                selected = record.get("managed_backend") or ("codex" if path.is_file() else self.default_managed_backend)
                profiles = {key:managed_defaults(key) for key in MANAGED_BACKENDS}
                for key, profile in record.get("backend_settings", {}).items():
                    if isinstance(profile, dict) and isinstance(profile.get("model"), str) and isinstance(profile.get("reasoning_effort"), str):
                        profiles[key] = {"model":profile["model"],"reasoning_effort":profile["reasoning_effort"]}
                if selected not in profiles:
                    profiles[selected] = managed_defaults(selected)
                if not record.get("backend_settings"):
                    profiles[selected] = {"model":record.get("model") or profiles[selected]["model"],
                                          "reasoning_effort":record.get("reasoning_effort") or profiles[selected]["reasoning_effort"]}
                threads = {key:value for key,value in record.get("backend_threads", {}).items() if isinstance(value,str) and value}
                if record.get("thread_id") and (not threads or not record.get("managed_backend")):
                    threads.setdefault(selected,record["thread_id"])
                if relocated:
                    # Conversation history is portable; live harness identity
                    # belongs to the old host/workspace and must be reconnected.
                    threads = {}
                normalized = {"managed_backend":selected,"backend_settings":profiles,"backend_threads":threads,
                    "backend_contracts": {key:value for key,value in record.get("backend_contracts",{}).items()
                                          if isinstance(value,str)},
                    "thread_id":threads.get(selected),"messages":record.get("messages",[]),"read_sources":record.get("read_sources",[]),
                    "workspace_root": str(self.data_dir.resolve()),
                    **profiles[selected],"connection_mode":record.get("connection_mode","managed")}
                self.entries[sid] = {"record": normalized,
                    "runtime": None, "activity": "", "error": None, "warning": None, "error_metadata": None,
                    "last_turn": None, "turn_outcome_supported": False, "command_lock": threading.Lock(),
                    "chat_epoch": uuid.uuid4().hex, "chat_sequence": 0, "message_updates": deque(maxlen=256),
                    "lifecycle_revision": session.get("lifecycle_revision", 0), "deleted": False}
                if path.is_file() and normalized != record:
                    self._persist(sid,self.entries[sid])
            return self.entries[sid]

    def _require_current_entry(self, sid, entry):
        """Revalidate after waiting for a command lock, including after restore."""
        session = self.sessions.get(sid)
        if entry.get("deleted") or entry.get("lifecycle_revision", 0) != session.get("lifecycle_revision", 0):
            raise ConflictError("方案已删除或恢复，请重新加载后再操作 Agent。")
        return session

    def _persist(self, sid, entry):
        write_json(self.directory / (sid + ".json"), entry["record"])

    def _factory_for(self, backend):
        current_key = managed_backend_key(self.runtime_factory)
        if current_key == "none" or not hasattr(self.runtime_factory,"backend_id") or backend == current_key:
            return self.runtime_factory
        if backend in MANAGED_BACKENDS:
            return get_runtime_factory(backend)
        raise RuntimeUnavailableError("该方案保存的自定义 Agent 后端不可用；请断开后明确选择其他托管后端。")

    def _managed_info(self, entry):
        selected = entry["record"]["managed_backend"]
        enabled = managed_backend_key(self.runtime_factory) != "none"
        options = []
        for key, spec in MANAGED_BACKENDS.items():
            factory = self._factory_for(key) if enabled else None
            report = factory.status() if callable(getattr(factory,"status",None)) else {}
            options.append({"id":key,**copy.deepcopy(spec),"available":bool(report.get("available")),"enabled":enabled})
        if selected not in MANAGED_BACKENDS:
            defaults = managed_defaults(selected)
            options.append({"id":selected,"label":selected,"default_model":defaults["model"],"default_effort":defaults["reasoning_effort"],
                            "effort_options":list(MANAGED_BACKENDS["codex"]["effort_options"]),"available":enabled,"enabled":enabled})
        return {"managed_backend":selected,"backend_options":options,
                "managed_backend_settings":copy.deepcopy(entry["record"]["backend_settings"]),
                "managed_model":entry["record"]["model"],"managed_reasoning_effort":entry["record"]["reasoning_effort"]}

    def _managed_provenance(self, entry):
        backend = entry["record"]["managed_backend"]
        try:
            factory = self._factory_for(backend)
        except RuntimeUnavailableError:
            factory = None
        return {"backend":getattr(entry["runtime"],"backend_id",getattr(factory,"backend_id",backend)),
                "model":entry["record"]["model"],"reasoning_effort":entry["record"]["reasoning_effort"],
                "conversation_id":entry["record"]["backend_threads"].get(backend)}

    def _completed_message(self, entry, message):
        """Publish immutable completed replies/user messages, never token deltas.

        This bounded live feed starts when the server loads the conversation;
        persisted history is not replayed into a newly subscribed web page.
        Call under self.lock, after checking the entry incarnation.
        """
        message.setdefault("id", message.get("item_id") or uuid.uuid4().hex)
        visible = {key: message[key] for key in ("id", "role", "text", "at") if key in message}
        for previous in reversed(entry["message_updates"]):
            if previous["message"]["id"] == message["id"]:
                if previous["message"] == visible:
                    return
                break
        entry["chat_sequence"] += 1
        entry["message_updates"].append({"sequence": entry["chat_sequence"], "message": visible})

    @staticmethod
    def _chat_updates(entry, after):
        if not isinstance(after, str) or (after != "new" and not re.fullmatch(r"[a-f0-9]{32}:[0-9]{1,16}", after)):
            raise ValueError("消息游标无效，请重新订阅当前方案。")
        epoch, sequence = entry["chat_epoch"], entry["chat_sequence"]
        result = {"message_cursor": f"{epoch}:{sequence}", "message_updates": [], "messages_reset": False}
        if after == "new":
            return result
        previous_epoch, previous_sequence = after.split(":")
        if previous_epoch != epoch:
            result["messages_reset"] = True
            return result
        previous_sequence = int(previous_sequence)
        if previous_sequence > sequence:
            raise ValueError("消息游标超过当前进度，请重新订阅当前方案。")
        updates = entry["message_updates"]
        result["messages_reset"] = bool(updates and previous_sequence < updates[0]["sequence"] - 1)
        result["message_updates"] = [copy.deepcopy(update) for update in updates if update["sequence"] > previous_sequence]
        return result

    def chat_updates(self, sid, after):
        entry = self._entry(sid)
        with self.lock:
            if entry.get("deleted") or self.entries.get(sid) is not entry:
                raise FileNotFoundError("方案已删除或恢复，请重新加载。")
            return self._chat_updates(entry, after)

    def status(self, sid, *, after=_UNSET):
        entry = self._entry(sid)
        with self.lock:
            if entry.get("deleted") or self.entries.get(sid) is not entry:
                raise FileNotFoundError("方案已删除或恢复，请重新加载。")
            runtime = entry["runtime"]
            state = runtime.status() if runtime else {}
            alive = bool(state.get("running"))
            external = self.external.status(sid)
            mode = entry["record"]["connection_mode"]
            managed = self._managed_info(entry)
            chat = {"messages": copy.deepcopy(entry["record"]["messages"])} if after is _UNSET else self._chat_updates(entry, after)
            if mode == "external":
                alive = bool(external.get("alive"))
                return {"session_id":sid,"connected":alive,"alive":alive,"connection_mode":mode,
                    "pid":external.get("pid"),"thread_id":external.get("conversation_id"),
                    "conversation_id":external.get("conversation_id"),"backend":external.get("backend", "external-agent"),
                    "status":external.get("status","disconnected") if alive else "disconnected",
                    "active_turn_id":external.get("turn_id"),"plan_revision":self.sessions.get(sid)["revision"],
                    "model":external.get("model") or "未报告","reasoning_effort":external.get("reasoning_effort") or "未报告",
                    **managed,
                    "external":external,**chat,
                    "activity":entry["activity"] or external.get("activity", ""),"error":entry["error"]}
            return {"session_id": sid, "connected": alive, "alive": alive,
                "connection_mode":mode,"external":external,
                "pid": state.get("pid"), "thread_id": entry["record"].get("thread_id"),
                "status": "running" if alive and state.get("busy") else "idle" if alive else "error" if entry["error"] else "disconnected",
                "active_turn_id": state.get("turn_id"), "plan_revision": self.sessions.get(sid)["revision"],
                "backend": self._managed_provenance(entry)["backend"], "conversation_id": entry["record"].get("thread_id"),
                "model": state.get("model") or entry["record"]["model"],
                "reasoning_effort": state.get("reasoning_effort") or entry["record"]["reasoning_effort"],
                **managed,
                **chat, "activity": entry["activity"], "error": entry["error"], "warning": entry["warning"],
                "turn_outcome_supported": bool(state.get("turn_outcome_supported", entry["turn_outcome_supported"])),
                "last_turn": copy.deepcopy(state.get("last_turn", entry["last_turn"])),
                "error_metadata": copy.deepcopy(state.get("error_metadata", entry["error_metadata"]))}

    def settings(self, sid, model=_UNSET, reasoning_effort=_UNSET, *, connection_mode=None, managed_backend=None):
        entry = self._entry(sid)
        with entry["command_lock"]:
            self._require_current_entry(sid, entry)
            record = entry["record"]
            selected = record["managed_backend"] if managed_backend is None else managed_backend
            if not isinstance(selected,str) or (selected not in MANAGED_BACKENDS and selected != record["managed_backend"]):
                raise ValueError("托管后端需为 codex 或 dsh。")
            profile = record["backend_settings"].get(selected,managed_defaults(selected))
            model = profile["model"] if model is _UNSET else model
            reasoning_effort = profile["reasoning_effort"] if reasoning_effort is _UNSET else reasoning_effort
            if not isinstance(model,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}",model.strip()):
                raise ValueError("请输入所选 Agent 后端可用的模型 ID（字母、数字及 . _ : / -，最多128字符）。")
            efforts = MANAGED_BACKENDS.get(selected,MANAGED_BACKENDS["codex"])["effort_options"]
            if not isinstance(reasoning_effort,str) or reasoning_effort not in efforts:
                raise ValueError("所选后端的推理强度需为 " + "、".join(efforts) + "。")
            model = model.strip()
            mode = entry["record"]["connection_mode"] if connection_mode is None else connection_mode
            if not isinstance(mode,str) or mode not in {"managed","external"}:
                raise ValueError("连接方式需为managed或external。")
            unchanged = model == record["model"] and reasoning_effort == record["reasoning_effort"] and mode == record["connection_mode"] and selected == record["managed_backend"]
            if not unchanged and ((entry["runtime"] and entry["runtime"].status().get("running")) or self.external.status(sid).get("registered")):
                raise ConflictError("请先断开当前 Agent，再修改模型配置；不会中断正在运行的任务。")
            if selected != record["managed_backend"] and entry["runtime"] is not None:
                # Only an already inactive runtime reaches this branch. Live
                # processes require the explicit disconnect path above.
                entry["runtime"].close()
                entry["runtime"] = None
            with self.lock:
                record["backend_settings"][selected] = {"model":model,"reasoning_effort":reasoning_effort}
                record.update(managed_backend=selected,model=model,reasoning_effort=reasoning_effort,
                              thread_id=record["backend_threads"].get(selected),connection_mode=mode)
                if not unchanged:
                    entry["error"] = None
                self._persist(sid, entry)
        return self.status(sid)

    def connect(self, sid):
        entry = self._entry(sid)
        with entry["command_lock"]:
            self._require_current_entry(sid, entry)
            if entry["record"]["connection_mode"] == "external":
                if not self.external.status(sid).get("alive"):
                    raise RuntimeError("等待外部 Agent 注册或恢复心跳；不会自动启动工作台调试 Agent。")
                return self.status(sid)
            if entry["runtime"] and entry["runtime"].status().get("running"):
                return self.status(sid)
            cwd = self.data_dir / "agent-workspaces" / sid
            cwd.mkdir(parents=True, exist_ok=True)
            backend = entry["record"]["managed_backend"]
            factory = self._factory_for(backend)
            runtime = factory(cwd=cwd,
                on_event=lambda event: self._event(sid, event, source_entry=entry),
                on_tool=lambda name, args: self.runtime_tool(sid, name, args, source_entry=entry),
                model=entry["record"]["model"], reasoning_effort=entry["record"]["reasoning_effort"])
            entry["runtime"] = runtime
            entry["error"] = entry["warning"] = entry["error_metadata"] = entry["last_turn"] = None
            entry["turn_outcome_supported"] = False
            thread_id = entry["record"].get("thread_id")
            refreshing_contract = bool(backend == "codex" and thread_id and
                entry["record"]["backend_contracts"].get(backend) != PLANNING_CONTRACT_SHA256)
            if refreshing_contract:
                # Keep workbench messages, plan and old harness history intact;
                # a fresh harness thread is required to register the new tools.
                thread_id = None
            try:
                started = runtime.start(thread_id=thread_id,
                                        tools=planning_tools(), instructions=INSTRUCTIONS)
            except Exception:
                runtime.close()
                entry["runtime"] = None
                entry["error"] = "Agent 会话启动失败，请检查后端、模型、推理强度与登录配置；原方案保留。"
                raise RuntimeError(entry["error"]) from None
            with self.lock:
                entry["record"]["thread_id"] = started["thread_id"]
                entry["record"]["backend_threads"][backend] = started["thread_id"]
                entry["record"]["backend_contracts"][backend] = PLANNING_CONTRACT_SHA256
                if refreshing_contract:
                    entry["warning"] = "工具规范已升级，已重新建立Agent执行会话；原方案和工作台聊天记录保留。"
                self._persist(sid, entry)
        return self.status(sid)

    def message(self, sid, text):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 12000:
            raise ValueError("消息需为1–12000字。")
        self.connect(sid)
        entry = self._entry(sid)
        with entry["command_lock"]:
            self._require_current_entry(sid, entry)
            if entry["record"]["connection_mode"] == "external":
                with self.lock:
                    command = self.external.enqueue(sid,"message",text=text.strip())
                    entry["error"] = None
                    entry["record"]["messages"].append({"role":"user","text":text.strip(),"at":now(),"command_id":command["id"]})
                    self._completed_message(entry, entry["record"]["messages"][-1])
                    self._persist(sid,entry)
                return self.status(sid)
            with self.lock:
                entry["error"] = None
                if not entry["runtime"].status().get("busy"):
                    entry["warning"] = entry["error_metadata"] = entry["last_turn"] = None
                entry["record"]["messages"].append({"role": "user", "text": text.strip(), "at": now()})
                self._completed_message(entry, entry["record"]["messages"][-1])
                self._persist(sid, entry)
            try:
                entry["runtime"].send(text.strip())
            except Exception:
                entry["error"] = "消息未成功提交给 Agent；可重试发送。"
                raise RuntimeError(entry["error"]) from None
        return self.status(sid)

    def interrupt(self, sid):
        entry = self._entry(sid)
        if entry["record"]["connection_mode"] == "external":
            self.external.enqueue(sid,"interrupt")
            entry["activity"] = "中断请求已发给外部 Agent，等待其处理。"
            return self.status(sid)
        if entry["runtime"]:
            entry["runtime"].interrupt()
        return self.status(sid)

    def disconnect(self, sid):
        entry = self._entry(sid)
        with entry["command_lock"]:
            self._require_current_entry(sid, entry)
            if entry["record"]["connection_mode"] == "external":
                self.external.disconnect(sid)
                entry["activity"] = "已解除外部 Agent 绑定，未终止其进程。"
                return self.status(sid)
            runtime, entry["runtime"] = entry["runtime"], None
            if runtime:
                runtime.close()
        return self.status(sid)

    def delete_session(self, sid, revision, confirmed=False):
        """Delete only a disconnected plan; never terminate an external process."""
        entry = self._entry(sid)
        with entry["command_lock"]:
            with self.lock:
                session = self._require_current_entry(sid, entry)
                self.sessions.require_revision(session, revision)
                runtime = entry["runtime"]
                if runtime and runtime.status().get("running"):
                    raise ConflictError("请先断开该方案的 Agent，再删除方案；不会自动终止正在连接的 Agent。")
                # An expired heartbeat can be renewed by poll. Only an explicit
                # detach revokes that binding, so alive/busy is insufficient.
                if self.external.status(sid).get("registered"):
                    raise ConflictError("请先解除该方案的外部 Agent 绑定，再删除方案；不会终止外部进程。")
                result = self.sessions.delete(sid, revision, confirmed=confirmed)
                entry["deleted"] = True
                self.entries.pop(sid, None)
                return result

    def restore_version(self, sid, target_revision, revision, confirmed=False):
        """Restore content as a new revision only after its Agent is detached."""
        entry = self._entry(sid)
        with entry["command_lock"]:
            with self.lock:
                session = self._require_current_entry(sid, entry)
                self.sessions.require_revision(session, revision)
                runtime = entry["runtime"]
                if runtime and runtime.status().get("running"):
                    raise ConflictError("请先断开该方案的 Agent，再从历史版本创建新修订。")
                if self.external.status(sid).get("registered"):
                    raise ConflictError("请先解除该方案的外部 Agent 绑定，再从历史版本创建新修订。")
                result = self.sessions.restore_version(sid, target_revision, revision, confirmed=confirmed)
                entry["activity"], entry["error"] = "", None
                return result

    def _event(self, sid, event, *, source_entry=None):
        if source_entry is not None and source_entry.get("deleted"):
            return
        kind = event.get("type")
        try:
            entry = source_entry if source_entry is not None else self._entry(sid)
            if kind in {"message", "error", "turn"}:
                # Durable boundaries still check the store, but never hold the
                # global conversation lock while reading a potentially large
                # plan. Token deltas only mutate their bound in-memory entry.
                self._require_current_entry(sid, entry)
        except (FileNotFoundError, ConflictError):
            return
        with self.lock:
            # delete_session marks/removes this exact incarnation under the
            # same lock. A restored plan gets a different entry, so old runtime
            # callbacks cannot update it without per-token JSON I/O.
            if entry.get("deleted") or self.entries.get(sid) is not entry:
                return
            if kind in {"turn", "error"} and event.get("turn_id") and entry["runtime"]:
                state = entry["runtime"].status()
                current = state.get("turn_id") or (state.get("last_turn") or {}).get("turn_id")
                if current and current != event["turn_id"]:
                    return
            if kind in {"text_delta", "message"} and event.get("text"):
                messages = entry["record"]["messages"]
                identity = event.get("item_id") or event.get("turn_id")
                found = next((m for m in reversed(messages) if identity and m.get("item_id") == identity and m["role"] == "assistant"), None)
                if found is None:
                    found = {"role": "assistant", "text": "", "at": now(), "item_id": identity}
                    messages.append(found)
                found["text"] = found["text"] + event["text"] if kind == "text_delta" else event["text"]
                # Persist completed messages, rather than rewrite disk per token.
                if kind == "message":
                    self._completed_message(entry, found)
                    self._persist(sid, entry)
            elif kind == "tool":
                entry["activity"] = event.get("name", "正在使用工具")
            elif kind == "error":
                message = str(event.get("message") or "Agent 本轮执行未完成；已保存的方案保留，可继续对话。")[:500]
                metadata = {key: event[key] for key in ("code", "category", "will_retry", "turn_id", "http_status") if key in event}
                metadata["will_retry"] = event.get("will_retry") if type(event.get("will_retry")) is bool else None
                entry["error_metadata"] = metadata
                runtime = entry["runtime"]
                if event.get("fatal") is False or (event.get("fatal") is not True and runtime and runtime.status().get("busy")):
                    entry["warning"] = message
                else:
                    entry["error"] = message
                self._persist(sid, entry)
            elif kind == "turn":
                entry["activity"] = ""
                status = event.get("status")
                if event.get("event") == "started":
                    entry["last_turn"] = entry["error_metadata"] = entry["error"] = entry["warning"] = None
                elif event.get("event") == "completed" and status in {"completed", "failed", "interrupted"}:
                    entry["turn_outcome_supported"] = True
                    entry["last_turn"] = {"turn_id": event.get("turn_id"), "status": status}
                    if status == "completed":
                        entry["error"] = entry["warning"] = entry["error_metadata"] = None
                    else:
                        entry["error"] = "Agent 本轮执行失败；已保存的方案保留。" if status == "failed" else "Agent 本轮已中断；已保存的方案保留。"
                        entry["error_metadata"] = copy.deepcopy(event.get("error_metadata")) or entry["error_metadata"]
                self._persist(sid, entry)

    def register_external(self,sid,body):
        entry=self._entry(sid)
        with entry["command_lock"]:
            self._require_current_entry(sid, entry)
            if entry["runtime"] and entry["runtime"].status().get("running"):
                raise ConflictError("请先断开工作台启动的 Agent，再注册外部 Agent。")
            result=self.external.register(sid,body)
            with self.lock:
                entry["record"]["connection_mode"]="external"
                entry["activity"]=""
                entry["error"]=None
                self._persist(sid,entry)
        return {**result,"session_id":sid,"instructions":INSTRUCTIONS,"tools_path":"/api/tools"}

    def poll_external(self,sid,body):
        return self.external.poll(sid,body.get("binding_id"),body.get("after",0))

    def publish_external(self,sid,body):
        with self.lock:
            entry = self.entries.get(sid)
        if entry is None:
            entry = self._entry(sid)
        event=self.external.publish(sid,body.get("binding_id"),body.get("event"))
        if event:
            # publish already validates the live session, binding and process.
            # Bind this delivery to the entry captured before authorization so
            # deletion/restoration cannot redirect an old event to a new entry.
            self._event(sid,event,source_entry=entry)
        return {"accepted":event is not None}

    def unregister_external(self,sid,body):
        entry=self._entry(sid)
        with entry["command_lock"]:
            self._require_current_entry(sid, entry)
            self.external.unregister(sid,body.get("binding_id"))
            entry["activity"]="外部 Agent 已解除绑定。"
        return {"unregistered":True}

    def external_tool(self, sid, body):
        """Fixed-session planning tools authorized by the live external binding.

        Reuse the public schema/service directly. Managed-runtime configuration
        is deliberately not consulted for either validation or attribution.
        """
        if not isinstance(body, dict) or set(body) != {"binding_id", "name", "arguments"}:
            raise ValueError("绑定工具请求需包含且仅包含 binding_id、name、arguments。")
        name, arguments = body["name"], body["arguments"]
        allowed = {item["name"] for item in tool_definitions(include_alias=True)}
        if not isinstance(name, str) or name not in allowed or not isinstance(arguments, dict):
            raise ValueError("绑定入口仅支持公开规划/资料库工具及 get_plan 别名，arguments 须为 JSON 对象。")
        if "session_id" in arguments and arguments["session_id"] != sid:
            raise PermissionError("绑定工具不能访问或覆盖其他方案的 session_id。")
        fixed_arguments = {**arguments, "session_id": sid}
        entry = self._entry(sid)
        with entry["command_lock"]:
            self._require_current_entry(sid, entry)
            if entry["record"]["connection_mode"] != "external":
                raise PermissionError("此方案当前没有启用外部 Agent 绑定。")
            provenance = self.external.planning_provenance(sid, body["binding_id"])
            if name in CONTRACT_TOOLS:
                return self.tools.call(sid, name, arguments, provenance=provenance)
            return self.tools.call_public(name, fixed_arguments, provenance=provenance)


    def backend_status(self):
        report=getattr(self.runtime_factory,"status",None)
        if callable(report) and not isinstance(self.runtime_factory,type):
            return report()
        return {"available":True,"mode":self.backend_id,"detail":"使用注入的Agent运行时适配器。"}

    def call_tool(self,sid,name,args):
        entry=self._entry(sid)
        return self.tools.call(sid,name,args,provenance=self._managed_provenance(entry))

    def runtime_tool(self, sid, name, args, *, source_entry=None):
        """Send actionable validation feedback without returning tracebacks."""
        try:
            if source_entry is not None:
                self._require_current_entry(sid, source_entry)
            return self.call_tool(sid, name, args)
        except (ValueError, FileNotFoundError, PermissionError) as exc:
            details = getattr(exc, 'to_dict', None)
            error = details() if callable(details) else {
                "error": str(exc)[:500], "next_action": "核对参数；版本冲突先get_plan，来源缺失先检索/读取，再修正重试。"}
            return {"success": False, "contentItems": [{"type": "inputText", "text": json.dumps(error, ensure_ascii=False)}]}
        except RuntimeError:
            return {"success": False, "contentItems": [{"type": "inputText", "text": "本地工具暂不可用，未修改方案。请检查 QMD 运行组件和资料库索引状态，保留未解决的证据缺口。"}]}


    def _monitor(self):
        while not self.stopping.wait(5):
            with self.lock:
                entries = list(self.entries.items())
            for sid, entry in entries:
                try:
                    # Store and runtime checks can involve filesystem I/O or
                    # runtime locks; do them outside the global chat lock.
                    self._require_current_entry(sid, entry)
                    if entry["record"]["connection_mode"] == "external":
                        self.external.status(sid)
                        continue
                    runtime = entry["runtime"]
                    state = runtime.status() if runtime else {}
                    with self.lock:
                        if entry.get("deleted") or self.entries.get(sid) is not entry or entry["runtime"] is not runtime:
                            continue
                        if runtime and not state.get("running"):
                            entry["activity"] = "Agent 进程已断开"
                            self._persist(sid, entry)
                except (FileNotFoundError, ConflictError):
                    # A single deleted/restored plan must not stop monitoring
                    # other conversations or recreate its chat state.
                    continue

    def close(self):
        self.stopping.set()
        for sid in list(self.entries):
            try:
                self.disconnect(sid)
            except FileNotFoundError:
                continue
