"""Loopback-only HTTP API and static UI, with no required web framework."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import config
from .knowledge import KnowledgeBase
from .library_provider import LibraryKnowledgeProvider
from .model_reader import inspect_model, parser_status
from .sessions import ConflictError, SessionStore, export_markdown, write_json
from .agent_sessions import AgentSessions
from .planning_service import PlanningService, tool_definitions
from .runtime_adapters import get_runtime_factory
from .chat_bridge_tools import ChatBridgeTools,external_tool_definitions
from .workspace_paths import WorkspacePaths
from .system_status import services_status
from .library_store import LibraryLifecycleConflict
from .knowledge_views import candidate_view, related_sources_view, source_context_view
from .ui_status import status_summary


class DisabledBatchPlanner:
    def status(self):
        return {"available": False, "mode": "disabled", "detail": "未启用内部批处理调试规划器。"}

    def refine(self, *_args, **_kwargs):
        raise RuntimeError("内部批处理调试规划器已禁用；请使用独立规划工具或所选Agent后端。")

    refine_simplification = refine


def session_view(value, kb=None):
    """Transport source identities, not duplicate bodies or closed-library text.

    The persisted plan is untouched. Body reads use the lifecycle-checked source
    API; model/plan/question data retain their existing contract.
    """
    if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
        return value
    available = getattr(kb, "source_available", None)
    return dict(value, sources=[source_context_view(source, available=bool(available(source)) if callable(available) else True)
                                for source in value["sources"]])


class App:
    def __init__(self, data_dir=None, doc_root=None, *, runtime_factory=None):
        self.allow_dsh_embedding = os.environ.get("SIM_ADVISOR_DSH_EMBED") == "1"
        self.paths = WorkspacePaths(data_dir or config.DATA)
        self.data_dir = self.paths.root
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.kb = LibraryKnowledgeProvider(KnowledgeBase(self.data_dir, Path(doc_root or config.DOC_ROOT)), self.data_dir / "libraries")
        self.libraries = self.kb.store
        factory = get_runtime_factory(runtime_factory)
        if factory.backend_id == "codex-app-server":
            from .planner import AgentPlanner
            self.planner = AgentPlanner(self.data_dir, self.kb)
        else:
            self.planner = DisabledBatchPlanner()
        self.sessions = SessionStore(self.data_dir, self.kb, self.planner)
        self.tools = PlanningService(self.data_dir, self.sessions, self.kb)
        self.agents = AgentSessions(self.data_dir, self.sessions, self.kb, runtime_factory=factory, tools=self.tools)
        self.chat_tools = ChatBridgeTools(self.agents)
        self.index_lock = threading.Lock()

    def inspect(self, path: str) -> dict:
        if not isinstance(path, str) or not path.strip() or len(path) > 4096:
            raise ValueError("请输入有效的 STEP/STP 几何或 MPH 模型路径。")
        snapshot = inspect_model(self.paths.resolve(path))
        write_json(self.data_dir / "models" / (snapshot["id"] + ".json"), self.paths.model_snapshot(snapshot, encode=True))
        return snapshot

    def model(self, model_id: str) -> dict:
        if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", model_id):
            raise ValueError("模型标识不合法。")
        return self.paths.model_snapshot(json.loads((self.data_dir / "models" / (model_id + ".json")).read_text(encoding="utf-8")))

    def status_summary(self):
        return self.status(summary=True)

    def status(self, *, summary=False):
        read_summary = getattr(self.kb, "status_summary", None)
        corpus = read_summary() if summary and callable(read_summary) else self.kb.status()
        return {"ready": True, "services": services_status(corpus, parser_status(), self.agents.backend_status()),
                "corpus": corpus, "examples": config.examples(), "sessions": self.sessions.list()}


def handler_for(app: App):
    evaluation_access_token = secrets.token_hex(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "SimAdvisor/0.1"

        def log_message(self, fmt, *args):
            # Log routes/status only. Never log request payloads, model details or agent output.
            print("[http] " + fmt % args, flush=True)

        def respond(self, data, status=200, content_type="application/json; charset=utf-8", download=None):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            elif isinstance(data, str):
                data = data.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            ancestors = "dsh-app://app http://127.0.0.1:* http://localhost:*" if getattr(app, "allow_dsh_embedding", False) else "'none'"
            self.send_header("Content-Security-Policy", f"default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors {ancestors}; base-uri 'none'; form-action 'self'")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.end_headers()
            self.wfile.write(data)

        def local_request(self):
            host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            if host not in {"127.0.0.1", "localhost", "::1"}:
                raise PermissionError("服务仅接受本机访问。")
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                raise PermissionError("拒绝跨站请求。请从本地助手页面操作。")

        def body(self, max_length=256 * 1024):
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("不支持分块上传。")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("请求长度不合法。") from exc
            if not 0 < length <= max_length:
                raise ValueError(f"请求内容为空或超过大小限制（{max_length // 1024} KiB）。")
            self.connection.settimeout(30)
            result = self.rfile.read(length)
            if len(result) != length:
                raise ValueError("请求内容不完整。")
            return result

        def json_body(self):
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise ValueError("此接口需要 application/json。")
            value = json.loads(self.body().decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("请求必须是 JSON 对象。")
            return value

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

        def handle_request(self, method):
            try:
                self.local_request()
                self.route(method)
            except (ConflictError, LibraryLifecycleConflict) as exc:
                self.respond({"error": str(exc)}, 409)
            except PermissionError as exc:
                self.respond({"error": str(exc)}, 403)
            except FileNotFoundError as exc:
                self.respond({"error": str(exc)}, 404)
            except (ValueError, UnicodeError) as exc:
                self.respond({"error": str(exc)}, 422)
            except RuntimeError as exc:
                self.respond({"error": str(exc)}, 502)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                print(f"[error] {type(exc).__name__}: {str(exc)[:200]}", flush=True)
                self.respond({"error": "本地服务处理失败；原会话保留，请查看终端日志。"}, 500)

        def route(self, method):
            url = urlsplit(self.path)
            path = unquote(url.path)
            query = parse_qs(url.query)
            def status_view(value):
                return status_summary(value) if query.get("view", [""])[0] == "summary" else value
            def status_read(owner, name, *args):
                reader = getattr(owner, name + "_summary", None)
                if query.get("view", [""])[0] == "summary" and callable(reader):
                    return reader(*args)
                return getattr(owner, name)(*args)
            if method == "GET" and path == "/api/health":
                # Process/HTTP readiness only. Corpus readiness belongs to the
                # full status route, which may read large index manifests.
                return self.respond({"ready": True, "pid": os.getpid(),
                                     "data_dir": str(app.data_dir.resolve()),
                                     "service": "sim-advisor-workbench", "protocol_version": 1})
            if method == "GET" and path == "/api/status":
                return self.respond(status_view(status_read(app, "status")))
            if path == "/api/retrieval/embedding-settings":
                if method == "GET":
                    return self.respond(app.libraries.embedding_settings())
                if method == "POST":
                    body = self.json_body()
                    if set(body) - {"enabled", "model_path"}:
                        raise ValueError("Embedding settings only accepts enabled and model_path")
                    if "model_path" in body and not isinstance(body["model_path"], str):
                        raise ValueError("model_path must be a local GGUF path string")
                    return self.respond(app.libraries.configure_embedding(enabled=body.get("enabled"), model_path=body.get("model_path")))
            if method == "GET" and path == "/api/tools":
                return self.respond({"api_version": "planning-tools/v1", "tools": tool_definitions()+external_tool_definitions()})
            if method == "POST" and path == "/api/tools/call":
                body = self.json_body()
                name=body.get("name")
                if isinstance(name,str) and name in {d["name"] for d in external_tool_definitions()}:
                    result=app.chat_tools.call(name,body.get("arguments"))
                else:
                    result=app.tools.call_public(name,body.get("arguments"))
                return self.respond({"result":result})
            if method == "GET" and path == "/api/search":
                text = query.get("q", [""])[0][:2000]
                filters = {}
                if "library" in query:
                    filters["library_ids"] = query["library"]
                if "mode" in query:
                    filters["mode"] = query["mode"][0]
                if "feature_branch" in query:
                    filters["feature_branch"] = query["feature_branch"][0]
                return self.respond({"results": [candidate_view(item) for item in app.kb.search(text, limit=max(1, min(int(query.get("limit", ["8"])[0]), 30)), kind=query.get("kind", ["all"])[0], filters=filters)]})
            if method == "GET" and path == "/api/libraries":
                libraries = app.libraries.libraries(include_deleted=True) if query.get("include_deleted", [""])[0] == "1" else app.libraries.libraries()
                return self.respond(status_view({"libraries": libraries, "jobs": status_read(app.libraries, "jobs"),
                                     "indexes": status_read(app.kb, "status").get("library_indexes", {})}))
            if path == "/api/libraries/evaluation-sets" or path.startswith("/api/libraries/evaluation-sets/"):
                # One explicit same-origin web action. Never route this through
                # ordinary library controls or publish it as an Agent tool.
                if method == "GET" and path == "/api/libraries/evaluation-sets":
                    return self.respond(dict(app.libraries.evaluation_set_summaries(), access_token=evaluation_access_token))
                if method == "POST" and path == "/api/libraries/evaluation-sets/access":
                    if not self.headers.get("Origin"):
                        raise PermissionError("请使用网页测试集页面的唯一开关。")
                    body = self.json_body()
                    token = body.get("access_token")
                    if (not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token)
                            or not secrets.compare_digest(token, evaluation_access_token)):
                        raise PermissionError("测试集操作凭据失效，请刷新网页。")
                    if set(body) - {"enabled", "access_token"}:
                        raise ValueError("测试集开关仅接受 enabled 与网页操作凭据。")
                    result = app.libraries.set_evaluation_retrieval_enabled(body.get("enabled"))
                    return self.respond(dict(result, access_token=evaluation_access_token))
                return self.respond({"error": "测试集为隔离的只读集合，不提供修改操作。"}, 405)
            if method == "GET" and path == "/api/library-scope":
                return self.respond(app.libraries.library_scope(query=query.get("query", [None])[0], type=query.get("type", [None])[0]))
            if method == "POST" and path == "/api/libraries/enable":
                body = self.json_body()
                if set(body) - {"library_ids", "enabled"}:
                    raise ValueError("Library scope changes only accept library_ids and enabled")
                return self.respond(app.libraries.set_enabled_many(body.get("library_ids"), body.get("enabled")))
            if path == "/api/library-indexes/unfinished-vectors":
                if method == "GET":
                    return self.respond(app.libraries.unfinished_vectors())
                if method == "POST":
                    body = self.json_body()
                    if set(body) - {"confirmed", "token"}:
                        raise ValueError("向量清理只接受 confirmed 与 token 字段。")
                    return self.respond(app.libraries.clean_unfinished_vectors(confirmed=body.get("confirmed"), token=body.get("token")))
            if method == "POST" and path == "/api/libraries":
                body = self.json_body()
                return self.respond(app.libraries.register(body.get("name", ""), body.get("type"), body.get("roots"), body.get("version", "")), 201)
            match = re.fullmatch(r"/api/libraries/([A-Za-z0-9_-]+)/models", path)
            if method == "GET" and match:
                limit = int(query["limit"][0]) if "limit" in query else None
                return self.respond(app.libraries.model_candidates(match[1], limit=limit))
            match = re.fullmatch(r"/api/libraries/([A-Za-z0-9_-]+)/purge-progress", path)
            if method == "GET" and match:
                return self.respond(app.libraries.purge_progress(match[1]))
            match = re.fullmatch(r"/api/libraries/([A-Za-z0-9_-]+)/(enable|delete|restore|purge)", path)
            if method == "POST" and match:
                library_id, action = match.groups()
                body = self.json_body()
                if action == "enable":
                    return self.respond(app.libraries.set_enabled(library_id, body.get("enabled")))
                if action == "delete":
                    return self.respond(app.libraries.delete_library(library_id))
                if action == "purge":
                    return self.respond(app.libraries.purge_library(library_id, confirmed=body.get("confirmed", False)))
                return self.respond(app.libraries.restore_library(library_id))
            match = re.fullmatch(r"/api/libraries/([A-Za-z0-9_-]+)/(build|scan|rebind)", path)
            if method == "POST" and match:
                library_id, action = match.groups()
                body = self.json_body()
                if action == "scan":
                    return self.respond({"files": app.libraries.scan(library_id, body.get("model_paths"), body.get("limit"))})
                if action == "rebind":
                    return self.respond(app.libraries.rebind(library_id, body.get("root_id", "main"), body["path"]))
                return self.respond(app.libraries.start_build(library_id, paths=body.get("model_paths"), limit=body.get("limit"),
                    deep=body.get("deep", True), enrich=body.get("enrich", False), modes=body.get("modes", ["native"]),
                    model_format=body.get("model_format", "compact")), 202)
            match = re.fullmatch(r"/api/library-jobs/([a-f0-9]{32})", path)
            if method == "GET" and match:
                return self.respond(status_view(status_read(app.libraries, "job", match[1])))
            match = re.fullmatch(r"/api/library-jobs/([a-f0-9]{32})/cancel", path)
            if method == "POST" and match:
                return self.respond(app.libraries.cancel_build(match[1]))
            match = re.fullmatch(r"/api/library-objects/(lo-[a-f0-9-]+)/configuration", path)
            if method == "GET" and match:
                return self.respond(app.libraries.configuration(match[1], category=query.get("category", [None])[0],
                    node_path=query.get("node_path", [None])[0], limit=query.get("limit", ["20"])[0], source=query.get("source", ["api"])[0]))
            match = re.fullmatch(r"/api/library-objects/(lo-[a-f0-9-]+)/evidence", path)
            if method == "GET" and match:
                return self.respond(app.libraries.read_evidence(match[1], index=query.get("index", ["0"])[0],
                    line=query.get("line", [None])[0], limit=query.get("limit", ["120"])[0]))
            if method == "GET" and path.startswith("/api/sources/"):
                source = app.kb.get_document(path.removeprefix("/api/sources/"))
                if not source:
                    raise FileNotFoundError("资料不存在。")
                source = dict(source)
                try:
                    related = getattr(app.kb, "related_sources", None)
                    source["related_sources"] = related_sources_view(related(source["id"], limit=8)) if callable(related) else []
                except Exception:
                    source["related_sources"] = []
                    source["related_sources_warning"] = "关联来源摘要暂不可读取；当前正文仍可查看。"
                return self.respond(source)
            if method == "POST" and path == "/api/models/inspect":
                return self.respond(app.inspect(self.json_body().get("path", "")))
            if method == "POST" and path == "/api/models/upload":
                name = unquote(self.headers.get("X-File-Name", "model.mph"))
                if Path(name).suffix.lower() not in {".mph", ".stp", ".step"} or any(c in name for c in ("/", "\\", "\x00", ":")) or len(name) > 200:
                    raise ValueError("只接受文件名合法的 .stp、.step 或 .mph 文件。")
                blob = self.body(128 * 1024 * 1024)
                directory = app.data_dir / "uploads" / uuid.uuid4().hex
                directory.mkdir(parents=True)
                destination = directory / name
                destination.write_bytes(blob)
                try:
                    return self.respond(app.inspect(str(destination)))
                except Exception:
                    destination.unlink(missing_ok=True)
                    directory.rmdir()
                    raise
            if method == "POST" and path == "/api/sessions":
                body = self.json_body()
                model = app.model(body["model_id"]) if body.get("model_id") else app.inspect(body["model_path"]) if body.get("model_path") else None
                return self.respond(session_view(app.sessions.create(body.get("goal", ""), model), getattr(app, "kb", None)), 201)
            if method == "GET" and path == "/api/sessions":
                return self.respond(app.sessions.list_page(offset=int(query.get("offset", ["0"])[0]),
                                                          limit=int(query.get("limit", ["100"])[0])))
            if method == "GET" and path == "/api/sessions/deleted":
                return self.respond({"sessions": app.sessions.deleted_list()})
            match = re.fullmatch(r"/api/sessions/([a-f0-9]{32})/versions(?:/([1-9][0-9]*)(/restore)?)?", path)
            if match:
                session_id, target_revision, action = match.groups()
                if method == "GET" and target_revision is None:
                    return self.respond(app.sessions.versions(session_id))
                if method == "GET" and action is None:
                    return self.respond(session_view(app.sessions.version(session_id, int(target_revision)), getattr(app, "kb", None)))
                if method == "POST" and action == "/restore":
                    body = self.json_body()
                    if set(body) - {"revision", "confirmed"}:
                        raise ValueError("历史版本恢复只接受当前 revision 与 confirmed 字段。")
                    store = app.agents if hasattr(app, "agents") else app.sessions
                    value = store.restore_version(session_id, int(target_revision), body.get("revision"), confirmed=body.get("confirmed"))
                    return self.respond(session_view(value, getattr(app, "kb", None)))
            match = re.fullmatch(r"/api/sessions/([a-f0-9]{32})/(delete|restore|purge)", path)
            if method == "POST" and match:
                session_id, action = match.groups()
                body = self.json_body()
                if body.get("confirmed") is not True:
                    raise ValueError("请明确确认方案删除或恢复操作。")
                if action == "purge":
                    if set(body) - {"revision", "confirmed"}:
                        raise ValueError("彻底删除方案只接受当前 revision 与 confirmed 字段。")
                    return self.respond(app.sessions.purge(session_id, body.get("revision"), confirmed=True))
                if action == "restore":
                    return self.respond(session_view(app.sessions.restore(session_id), getattr(app, "kb", None)))
                if hasattr(app, "agents"):
                    return self.respond(app.agents.delete_session(session_id, body.get("revision"), confirmed=True))
                return self.respond(app.sessions.delete(session_id, body.get("revision"), confirmed=True))
            match = re.fullmatch(r"/api/sessions/([a-f0-9]{32})/external/(register|poll|event|unregister|tool)", path)
            if method == "POST" and match:
                session_id,action=match.groups()
                if action == "tool":
                    return self.respond({"result": app.agents.external_tool(session_id, self.json_body())})
                name={"register":"register_external_agent","poll":"poll_external_messages",
                    "event":"publish_external_event","unregister":"unregister_external_agent"}[action]
                return self.respond(app.chat_tools.call(name,{**self.json_body(),"session_id":session_id}))
            match = re.fullmatch(r"/api/sessions/([a-f0-9]{32})/agent(?:/(connect|message|interrupt|disconnect|settings))?", path)
            if match:
                session_id, action = match.groups()
                updates_view = query.get("view", [""])[0] == "updates"
                after = query.get("after", ["new"])[0]
                if method == "GET" and action is None:
                    return self.respond(app.agents.status(session_id, after=after) if updates_view else app.agents.status(session_id))
                if method == "POST" and action:
                    if updates_view:
                        app.agents.chat_updates(session_id, after)  # Validate cursor before mutation.
                    body = self.json_body()
                    if action == "message":
                        result = app.agents.message(session_id, body.get("text", ""))
                    elif action == "settings":
                        result = app.agents.settings(session_id, **{key:body[key] for key in
                            ("model","reasoning_effort","connection_mode","managed_backend") if key in body})
                    else:
                        result = getattr(app.agents, action)(session_id)
                    if updates_view:
                        result = {key: value for key, value in result.items() if key != "messages"}
                        result.update(app.agents.chat_updates(session_id, after))
                    return self.respond(result)
            match = re.fullmatch(r"/api/sessions/([a-f0-9]{32})(?:/(answers|confirm|refine|simplify|export))?", path)
            if match:
                session_id, action = match.groups()
                if method == "GET" and action is None:
                    return self.respond(session_view(app.sessions.get(session_id), getattr(app, "kb", None)))
                if method == "GET" and action == "export":
                    value = app.sessions.get(session_id)
                    fmt = query.get("format", ["markdown"])[0]
                    if fmt == "json":
                        return self.respond(session_view(value, getattr(app, "kb", None)), download=f"simulation-plan-{session_id[:8]}-r{value['revision']}.json")
                    if fmt != "markdown":
                        raise ValueError("不支持的导出格式。")
                    return self.respond(export_markdown(value), content_type="text/markdown; charset=utf-8", download=f"simulation-plan-{session_id[:8]}-r{value['revision']}.md")
                if method == "POST" and action in {"answers", "confirm", "refine", "simplify"}:
                    body = self.json_body()
                    if action in {"refine", "simplify"} and hasattr(app, "agents") and app.agents.backend_status().get("mode") == "disabled":
                        raise RuntimeError("内部调试 Agent 已禁用；请通过独立规划工具接入外部 Agent。")
                    if action == "answers":
                        value = app.sessions.answers(session_id, body.get("revision"), body.get("answers"))
                    elif action == "confirm":
                        value = app.sessions.confirm(session_id, body.get("revision"))
                    else:
                        value = app.sessions.refine(session_id, body.get("revision"), focus="simplification" if action == "simplify" else None)
                    return self.respond(session_view(value, getattr(app, "kb", None)))
            if method == "POST" and path == "/api/index/build":
                self.json_body()
                if not app.index_lock.acquire(blocking=False):
                    raise ConflictError("索引构建已在进行。")
                try:
                    return self.respond(app.kb.build())
                finally:
                    app.index_lock.release()
            if method == "GET" and path in {"/", "/index.html", "/app.js", "/styles.css", "/library-panel.js", "/library-panel.css", "/workflow.css", "/agent-panel.js", "/agent-panel.css"}:
                target = config.ROOT / "web" / ("index.html" if path == "/" else path[1:])
                return self.respond(target.read_bytes(), content_type=(mimetypes.guess_type(target.name)[0] or "application/octet-stream") + "; charset=utf-8")
            raise FileNotFoundError("接口或文件不存在。")
    return Handler


def make_server(app: App, port=8765):
    return ThreadingHTTPServer(("127.0.0.1", port), handler_for(app))
