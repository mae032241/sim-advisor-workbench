"""Backend-neutral planning tools, evidence ledger and plan validation.

No agent runtime is created here. HTTP, MCP and a temporary debug adapter share
this service; plan identity is explicit and independent of transport sessions.
"""
from __future__ import annotations
import copy
import json
import posixpath
import threading
from pathlib import Path
from urllib.parse import unquote,urlsplit
from .knowledge import source_id as topic_id
from .planner import PLAN_SCHEMA,validate_agent_plan
from .planning_validation import (DESIGN_BRIEF_SCHEMA,QUESTION_SCHEMA,validate_response,
    _check_shape,_safe_question_list,_validated_retirements,geometry_reference_catalog)
from .sessions import write_json
from .case_metadata import FEATURE_BRANCHES, normalize_feature_branch
from .knowledge_views import candidate_view, related_sources_view, source_context_view

def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STR = {"type": "string"}


STRINGS = {"type": "array", "items": STR}
LIBRARY_TYPES = ["official-manuals", "official-models", "user-manuals", "user-models"]
LIBRARY_IDS = {"type": "array", "items": STR, "minItems": 1, "maxItems": 1000}
LIBRARY_TOOLS = {"get_library_scope", "set_libraries_enabled"}


SAVE_SCHEMA = obj({"revision": {"type": "integer"}, "plan": PLAN_SCHEMA,
    "design_brief": DESIGN_BRIEF_SCHEMA,
    "questions": {"type": "array", "items": QUESTION_SCHEMA, "maxItems": 16},
    "retired_questions": {"type": "array", "items": obj({"id": STR, "reason": STR}), "maxItems": 40},
    "evidence_gaps": STRINGS})


PATCH_SCHEMA = obj({"revision": {"type": "integer"},
    "steps": {"type": "array", "items": PLAN_SCHEMA["properties"]["steps"]["items"], "maxItems": 8}})


def planning_tools():
    def tool(name, description, schema):
        return {"name": name, "description": description, "inputSchema": schema}
    search_schema = obj({"query": STR, "kind": {"type": "string", "enum": ["all", "manual", "case", "code"]},
        "mode": {"type": "string", "enum": ["native", "qmd_keyword", "qmd_vector"]},
        "feature_branch": {"type": "string", "enum": list(FEATURE_BRANCHES), "description": "可选：仅检索指定建模分支，省略时不限制分支。"},
        "library_ids": dict(LIBRARY_IDS, description="可选：仅检索这些已启用资料库；可先get_library_scope查询目录并按需启用指定库。")})
    search_schema["required"].remove("feature_branch")
    search_schema["required"].remove("library_ids")
    scope_schema = obj({"query": STR, "type": {"type": "string", "enum": LIBRARY_TYPES}})
    scope_schema["required"] = []
    return [
        tool("get_plan", "读取绑定方案的需求、模型事实、用户回答、当前流程、版本与几何事实白名单。首次及保存前调用。", obj({})),
        tool("search_knowledge", "检索本地官方手册、官方案例和用户案例。kind与可选feature_branch先限制检索范围；native/QMD覆盖各自已建索引。结果只是候选，引用前必须read_source。", search_schema),
        tool("read_source", "分页读取真实来源、目录父子关系及链接。若是目录或摘要，沿关联章节继续读到设置正文，不用目录充当实施依据。", obj({
            "id": STR, "offset": {"type": "integer", "minimum": 0}, "length": {"type": "integer", "minimum": 500, "maximum": 16000}})),
        tool("save_plan", "保存完整、可审阅的路线设计与流程。仅可引用当前会话实际读取的来源或原方案已有来源；不会替用户回答/确认。缺少用户输入写questions，方法证据不足写evidence_gaps。", SAVE_SCHEMA),
        tool("update_steps", "按原id替换最多8个已有流程节点，不重写全文。保留未更改节点、问题、确认门槛和未决项；新增/删除节点或清除全局阻塞时用save_plan。", PATCH_SCHEMA),
        tool("get_library_scope", "查询可用资料库目录、分组、当前启停状态及轻量根路径概要；包括已关闭库，不返回正文、构建历史或索引。可按query或type筛选；官方/自定义类型来自注册声明，不根据路径推断。全局操作，无需方案。", scope_schema),
        tool("set_libraries_enabled", "显式批量启用或关闭指定library_ids，无需用户确认，不构建索引或向量。按研究需要选择若干库，不自动启用全部库；未知或已删除库会拒绝整批。全局状态变更，不修改方案。", obj({"library_ids": LIBRARY_IDS, "enabled": {"type": "boolean"}})),
    ]


def tool_definitions(*, bound=False, include_alias=False):
    tools=copy.deepcopy(planning_tools())
    tools[0]["name"]="get_task_context"
    tools[0]["description"]="读取当前仿真任务的需求、模型事实、回答、方案、缺口和版本。已有来源仅返回身份与摘要；完整正文请read_source分页读取。首次规划时方案可以为空。"
    if include_alias:
        alias=copy.deepcopy(tools[0]);alias["name"]="get_plan"
        alias["description"]="get_task_context的兼容别名，供既有客户端恢复会话。"
        tools.append(alias)
    if not bound:
        for tool in tools:
            tool["inputSchema"]["properties"]["session_id"]={"type":"string","description":"工作台方案ID，与Agent或MCP连接会话无关。"}
            if tool["name"] in LIBRARY_TOOLS:
                continue
            if tool["name"] not in {"search_knowledge","read_source"}:
                tool["inputSchema"]["required"].insert(0,"session_id")
            else:
                tool["description"]+=" 可省略session_id独立查询；提供时将阅读证据关联该方案。"
    return tools


class PlanningService:
    def __init__(self,data_dir,sessions,kb):
        self.data_dir,self.sessions,self.kb=Path(data_dir),sessions,kb
        self.directory=self.data_dir/"tool-contexts"
        self.directory.mkdir(parents=True,exist_ok=True)
        self.entries={}
        self.lock=threading.RLock()

    def _entry(self,sid):
        self.sessions.get(sid)
        with self.lock:
            if sid not in self.entries:
                path=self.directory/(sid+".json")
                legacy=self.data_dir/"agent-sessions"/(sid+".json")
                saved=json.loads(path.read_text(encoding="utf-8")) if path.is_file() else json.loads(legacy.read_text(encoding="utf-8")) if legacy.is_file() else {}
                self.entries[sid]={"record":{"read_sources":list(dict.fromkeys(saved.get("read_sources",[])))}}
                if not path.is_file() and legacy.is_file():
                    self._persist(sid,self.entries[sid])
            return self.entries[sid]

    def _persist(self,sid,entry):
        write_json(self.directory/(sid+".json"),entry["record"])

    def read_sources(self,sid):
        with self.lock:
            return list(self._entry(sid)["record"]["read_sources"])

    def _source_available(self, source):
        checker = getattr(self.kb, "source_available", None)
        return bool(checker(source)) if callable(checker) else True

    def _require_available_sources(self, source_ids, known):
        if any(source_id not in known or not self._source_available(known[source_id]) for source_id in source_ids):
            raise PermissionError("来源所属资料库已关闭、删除或不可用，不能用于本次新引用。请重新检索可用来源。")

    def call_public(self,name,arguments,*,provenance=None):
        if not isinstance(arguments,dict):
            raise ValueError("工具arguments必须是JSON对象。")
        definitions={item["name"]:item for item in tool_definitions(include_alias=True)}
        if not isinstance(name,str) or name not in definitions:
            raise ValueError("未知规划工具。")
        schema=definitions[name]["inputSchema"]
        if set(arguments)-set(schema["properties"]) or set(schema["required"])-set(arguments):
            raise ValueError("工具参数字段与公开schema不符；请通过/api/tools查询。")
        for key,value in arguments.items():
            _check_shape(value,schema["properties"][key],"arguments."+key)
        arguments=dict(arguments)
        sid=arguments.pop("session_id",None)
        return self.call(sid,name,arguments,provenance=provenance)

    def call(self, sid, name, args, *, provenance=None):
        if not isinstance(args, dict):
            raise ValueError("工具参数必须是JSON对象。")
        if not isinstance(name,str) or name not in {"get_plan","get_task_context","search_knowledge","read_source","save_plan","update_steps"} | LIBRARY_TOOLS:
            raise ValueError("未知规划工具。")
        if sid is not None and not isinstance(sid,str):
            raise ValueError("session_id必须是方案ID文本。")
        name = "get_task_context" if name == "get_plan" else name
        if name in LIBRARY_TOOLS:
            # Library scope is global, including when called by a bound Agent.
            # It neither opens a plan nor grants evidence-reading credit.
            schema = next(tool["inputSchema"] for tool in planning_tools() if tool["name"] == name)
            if set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
                raise ValueError("资料库工具参数字段与schema不符。")
            for key, value in args.items():
                _check_shape(value, schema["properties"][key], "arguments." + key)
            store = getattr(self.kb, "store", None)
            if store is None:
                raise RuntimeError("当前知识提供方未连接资料库目录。")
            if name == "get_library_scope":
                return store.library_scope(**args)
            return store.set_enabled_many(args["library_ids"], args["enabled"])
        if sid is None and name not in {"search_knowledge", "read_source"}:
            raise ValueError("此工具需要明确的 session_id。")
        entry = self._entry(sid) if sid is not None else None
        if name == "get_task_context":
            value = self.sessions.get(sid)
            sources = [source_context_view(source, available=self._source_available(source)) for source in value.get("sources", [])]
            return {key: value.get(key) for key in ("id", "revision", "goal", "model", "plan", "design_brief", "questions", "answers", "plan_stale", "planning_incomplete", "evidence_gaps", "research_trace")} | {
                "sources": sources, "sources_note": "已有来源保留引用身份和有界摘要；完整正文由 read_source 按 id 分页读取。已读证据账本保持不变。",
                "geometry_reference_catalog": geometry_reference_catalog(value.get("model"))}
        if name == "search_knowledge":
            query, kind, mode = args.get("query"), args.get("kind", "all"), args.get("mode", "native")
            branch = args.get("feature_branch")
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000 or not isinstance(kind,str) or kind not in {"all", "manual", "case", "code"} or not isinstance(mode,str) or mode not in {"native", "qmd_keyword", "qmd_vector"}:
                raise ValueError("检索参数不合法。")
            if "feature_branch" in args and not isinstance(branch, str):
                raise ValueError("feature_branch不是支持的建模分支。")
            branch = normalize_feature_branch(branch)
            filters = {"mode": mode}
            if branch is not None:
                filters["feature_branch"] = branch
            if "library_ids" in args:
                library_ids = args["library_ids"]
                if not isinstance(library_ids, list) or not 1 <= len(library_ids) <= 1000 or any(not isinstance(lid, str) or not lid.strip() or len(lid) > 30000 for lid in library_ids):
                    raise ValueError("library_ids需要1至1000个非空资料库ID。")
                filters["library_ids"] = list(dict.fromkeys(library_ids))
            hits = self.kb.search(query, limit=6, kind=kind, filters=filters)
            return {"mode": mode, "results": [candidate_view(hit) for hit in hits], "note": "候选检索结果；请read_source后再引用。"}
        if name == "read_source":
            source = self.kb.get_document(args.get("id", ""))
            if not source:
                raise FileNotFoundError("资料不存在。")
            if not self._source_available(source):
                raise PermissionError("资料库已关闭或删除，不能新读取该来源。")
            offset, length = args.get("offset", 0), args.get("length", 12000)
            if type(offset) is not int or offset < 0 or type(length) is not int or not 500 <= length <= 16000:
                raise ValueError("文档分页范围不合法。")
            text = source.get("text") or source.get("excerpt", "")
            if not isinstance(text, str) or not text.strip() or offset >= len(text) or not text[offset:offset + length].strip():
                raise ValueError("本页没有可读取正文，请核对来源或分页范围；本次不记为已读证据。")
            links = []
            for link in source.get("related_links", [])[:40]:
                parsed = urlsplit(link.get("href", ""))
                if parsed.scheme or parsed.netloc or not parsed.path or not source.get("relative_path"):
                    continue
                relative = posixpath.normpath(posixpath.join(posixpath.dirname(source["relative_path"]), unquote(parsed.path)))
                try:
                    target = self.kb.get_document(topic_id(relative))
                except PermissionError:
                    target = None
                if target:
                    links.append({"id": target["id"], "title": target["title"]})
            related, related_warning = [], None
            get_related = getattr(self.kb, "related_sources", None)
            if callable(get_related):
                try:
                    related = related_sources_view(get_related(source["id"], limit=8))
                except Exception:
                    # Optional dependency navigation must not prevent a valid
                    # source page from being read or hide that lookup failed.
                    related_warning = "关联来源摘要暂不可读取；可按依赖名称继续检索并逐项核实。"
            metadata = candidate_view(source)
            for key in ("candidate_only", "read_required", "excerpt"):
                metadata.pop(key, None)
            if metadata.get("parent") and not self._source_available(metadata["parent"]):
                metadata.pop("parent", None)
            if isinstance(metadata.get("children"), list):
                metadata["children"] = [item for item in metadata["children"] if self._source_available(item)]
            links = [item for item in links if self._source_available(item)]
            related = [item for item in related if not item.get("id") or self._source_available(item)]
            response = metadata | {
                "text": text[offset:offset + length], "offset": offset, "total_characters": len(text),
                "next_offset": offset + length if offset + length < len(text) else None, "linked_topics": links,
                "related_sources": related, **({"related_sources_warning": related_warning} if related_warning else {})}
            with self.lock:
                # A library may close while body/navigation reads are running.
                # Reauthorize before both granting citation credit and returning.
                if not self._source_available(source):
                    raise PermissionError("资料库已关闭或删除，不能新读取该来源。")
                if entry is not None and source["id"] not in entry["record"]["read_sources"]:
                    entry["record"]["read_sources"].append(source["id"])
                    self._persist(sid, entry)
            return response
        if name in {"save_plan", "update_steps"}:
            return self._save(sid, name, args, provenance=provenance)
        raise ValueError("未知规划工具。")


    def _save(self, sid, name, args, *, provenance=None):
        _check_shape(args, SAVE_SCHEMA if name == "save_plan" else PATCH_SCHEMA, name)
        value = self.sessions.get(sid)
        self.sessions.require_revision(value, args["revision"])
        historical = {s["id"]: s for s in value.get("sources", [])}
        known = {source_id: source for source_id, source in historical.items() if self._source_available(source)}
        unavailable = set(historical) - set(known)
        for source_id in self._entry(sid)["record"]["read_sources"]:
            try:
                source = self.kb.get_document(source_id)
            except PermissionError:
                source = None
                unavailable.add(source_id)
            if source:
                known[source_id] = source
        if name == "update_steps":
            plan = copy.deepcopy(value["plan"])
            by_id = {step["id"]: step for step in args["steps"]}
            if not by_id or len(by_id) != len(args["steps"]) or not set(by_id) <= {s["id"] for s in plan["steps"]}:
                raise ValueError("局部更新须提供不同id的已有节点；增删节点请save_plan。")
            changed_sources = {source_id for step in args["steps"] for source_id in step["source_ids"]}
            self._require_available_sources(changed_sources, known)
            plan["steps"] = [copy.deepcopy(by_id.get(s["id"], s)) for s in plan["steps"]]
            engine = plan.pop("engine", None)
            validate_agent_plan(plan, list({**historical, **known}.values()))
            metadata=dict(provenance or {})
            plan["engine"] = engine or metadata.get("backend") or "external-agent"
            source_ids = {sid for s in plan["steps"] for sid in s["source_ids"]}
            sources = list({**historical, **{sid: known.get(sid, historical.get(sid)) for sid in source_ids}}.values())
            trace=list(value.get("research_trace") or []) + [{"stage":"interactive_edit","status":"steps_updated",
                "backend":metadata.get("backend","external-agent"),"model":metadata.get("model"),
                "reasoning_effort":metadata.get("reasoning_effort"),"conversation_id":metadata.get("conversation_id"),
                "changed_step_ids":list(by_id),"physical_validation":"not_run"}]
            self._require_available_sources(changed_sources, known)
            saved = self.sessions.apply_agent_bundle(sid, args["revision"], {"plan": plan, "sources": sources,"research_trace":trace}, partial=True)
        else:
            response = copy.deepcopy(args)
            response["selected_source_ids"] = []
            requested = {source_id for step in args["plan"]["steps"] for source_id in step["source_ids"]}
            requested.update(source_id for decision in args["design_brief"]["decisions"] for source_id in decision["source_ids"])
            requested.update(source_id for candidate in args["design_brief"]["simplification"]["candidates"] for source_id in candidate["source_ids"])
            if requested & unavailable:
                raise PermissionError("来源所属资料库已关闭或删除，不能用于本次新引用。请重新检索可用来源。")
            validate_response(response, known, {"model": value.get("model")})
            retired = _validated_retirements(args["retired_questions"], args["questions"], value["questions"])
            retired_ids = {q["id"] for q in retired}
            questions = _safe_question_list(args["questions"], [q for q in value["questions"] if q["id"] not in retired_ids])
            brief, plan = response["design_brief"], response["plan"]
            ids = list(dict.fromkeys([sid for s in plan["steps"] for sid in s["source_ids"]] +
                [sid for d in brief["decisions"] for sid in d["source_ids"]] +
                [sid for c in brief["simplification"]["candidates"] for sid in c["source_ids"]]))
            gaps = [g.strip() for g in args["evidence_gaps"] if g.strip()]
            plan["missing_information"] = list(dict.fromkeys(plan["missing_information"] + gaps))
            incomplete = bool(gaps or all(r["status"] == "blocked" for r in brief["candidate_routes"]))
            metadata = dict(provenance or {})
            plan["engine"] = " / ".join(str(metadata[k]) for k in ("backend", "model", "reasoning_effort") if metadata.get(k)) or "external-agent"
            bundle = {"plan": plan, "design_brief": brief, "questions": questions, "retired_questions": retired,
                "sources": [known[s] for s in ids], "planning_incomplete": incomplete, "evidence_gaps": gaps,
                "geometry_reference_catalog": geometry_reference_catalog(value.get("model")),
                "research_trace": [{"stage": "conversation", "status": "blocked_evidence" if incomplete else "draft_validated",
                    "conversation_id": metadata.get("conversation_id"), "backend": metadata.get("backend", "external-agent"),
                    "model": metadata.get("model"), "reasoning_effort": metadata.get("reasoning_effort"), "source_count": len(ids),
                    "evidence_gaps": gaps, "physical_validation": "not_run"}]}
            self._require_available_sources(ids, known)
            saved = self.sessions.apply_agent_bundle(sid, args["revision"], bundle)
        return {"saved": True, "revision": saved["revision"], "status": saved["status"],
            "planning_incomplete": bool(saved.get("planning_incomplete")), "note": "已更新工作台流程；仍需用户审阅确认，未运行求解。"}

