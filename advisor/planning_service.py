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
from .sessions import write_json, ConflictError
from .plan_drafts import (PlanDraftStore, PlanDraftError, PlanDraftConflictError,
    PlanDraftPermissionError, schema_errors, semantic_error)
from .case_metadata import FEATURE_BRANCHES, normalize_feature_branch
from .knowledge_views import candidate_view, related_sources_view, source_context_view, _compact
from .query_terms import normalize_mode
from .research_policy import (SEARCH_KNOWLEDGE_DESCRIPTION, READ_SOURCE_DESCRIPTION,
                              LIBRARY_SCOPE_DESCRIPTION, REVIEW_PLAN_DESCRIPTION)
from .plan_review import review_plan, CHECKLIST_SCHEMA
from .planning_views import candidate_for_agent, source_page_for_agent, library_scope_for_agent
from .plan_settings import (PLAN_SETTINGS_SCHEMA, validate_plan_settings, review_plan_settings,
                            require_setting_implementations)
from .mechanism_checks import mechanism_checklist, review_mechanism_coverage, known_mechanisms
from .tool_contracts import (CONTRACT_TOOL, CONTRACT_SCHEMA, ToolContractError,
                             explicit_schema, describe_contract, read_contract)

def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STR = {"type": "string"}


STRINGS = {"type": "array", "items": STR}
LIBRARY_TYPES = ["official-manuals", "official-models", "user-manuals", "user-models"]
LIBRARY_IDS = {"type": "array", "items": {"type": "string", "minLength": 1, "pattern": r"\S"}, "minItems": 1, "maxItems": 1000}
LIBRARY_TOOLS = {"get_library_scope", "set_libraries_enabled"}
KNOWLEDGE_TOOLS = {"search_knowledge", "read_source", "search_cases", "get_case_guide", "read_case_configuration"}
DRAFT_TOOLS = {"get_plan_draft", "repair_plan_draft"}
SETTINGS_TOOLS = {"save_plan_settings", "get_plan_settings"}
GUIDANCE_TOOLS = {"get_mechanism_checks"}
CONTRACT_TOOLS = {CONTRACT_TOOL}


def _configuration_view(value):
    """Keep typed values; omit duplicate provenance and the other source mode."""
    result = dict(value)
    result["extraction_coverage"] = _compact(value.get("extraction_coverage", {}))
    result["coverage_read_scope"] = "bounded_summary"
    nodes = []
    for original in value.get("nodes", []):
        node = {key: item for key, item in original.items()
                if key not in {"case_ref", "xml_route", "children", "api_observations"}}
        for name in ("properties", "selections"):
            if name in node:
                node[name] = [{key: item for key, item in entry.items()
                               if key not in {"case_ref", "node_id", "child_index"}}
                              for entry in node[name]]
        if value.get("source") == "api":
            node["api_observations"] = [{key: item for key, item in entry.items()
                                         if key not in {"case_ref", "node_id", "api_node_id"}}
                                        for entry in original.get("api_observations", [])]
        nodes.append(node)
    result["nodes"] = nodes
    return result


def _guide_view(guide, stage=None):
    if guide.get("state") != "ready":
        return guide
    result = {key: item for key, item in guide.items() if key != "stages"}
    if stage:
        result["stages"] = [item for item in guide["stages"] if item["id"] == stage]
        result["detail_scope"] = stage
    else:
        result["stages"] = [{"id": item["id"], "title": item["title"], "detail_available": True,
                             "source_count": len(item.get("source_refs", [])),
                             "setting_count": len(item.get("configuration_requests", [])),
                             **{key: item[key] for key in ('total_node_count', 'selected_node_count', 'summary_truncated') if key in item},
                             "key_hint": next((fact.get("hint") for fact in item.get("observed", []) if fact.get("hint")), None)}
                            for item in guide["stages"]]
        result["detail_scope"] = "flow_overview"
        result["next_action"] = "按需用get_case_guide的stage读取单个环节；导航不算正文已读。"
    return result


SAVE_SCHEMA = obj({"revision": {"type": "integer", "minimum": 1}, "plan": PLAN_SCHEMA,
    "design_brief": DESIGN_BRIEF_SCHEMA,
    "questions": {"type": "array", "items": QUESTION_SCHEMA, "maxItems": 16},
    "retired_questions": {"type": "array", "items": obj({"id": STR, "reason": STR}), "maxItems": 40},
    "evidence_gaps": STRINGS})
SAVE_SCHEMA["properties"]["plan_settings"] = PLAN_SETTINGS_SCHEMA
SAVE_SCHEMA = explicit_schema(SAVE_SCHEMA)
SETTINGS_SAVE_SCHEMA = obj({"revision": {"type": "integer", "minimum": 1}, "settings": PLAN_SETTINGS_SCHEMA})
SETTINGS_GET_SCHEMA = obj({"offset": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 8}, "step_id": STR,
    "view": {"type": "string", "enum": ["settings", "unhandled"]}})
SETTINGS_GET_SCHEMA["required"] = []
SETTINGS_GET_SCHEMA["properties"]["offset"]["default"] = 0
SETTINGS_GET_SCHEMA["properties"]["limit"]["default"] = 6
SETTINGS_GET_SCHEMA["properties"]["view"]["default"] = "settings"


PATCH_SCHEMA = obj({"revision": {"type": "integer", "minimum": 1},
    "steps": {"type": "array", "items": PLAN_SCHEMA["properties"]["steps"]["items"], "minItems": 1, "maxItems": 8}})

_DRAFT_ID_SCHEMA = {"type": "string", "pattern": "^[a-f0-9]{32}$", "maxLength": 32,
                    "description": "精确复制最近错误返回的draft_id，不是方案或聊天ID。"}
GET_DRAFT_SCHEMA = obj({"draft_id": _DRAFT_ID_SCHEMA, "path": {"type": "string", "maxLength": 2000, "default": "", "description": "可选：精确JSON pointer，如/plan/steps/0/action；空文本读取整个草稿。"},
    "offset": {"type": "integer", "minimum": 0, "default": 0},
    "length": {"type": "integer", "minimum": 1, "maximum": 16000, "default": 16000}})
GET_DRAFT_SCHEMA["required"] = ["draft_id"]
_DRAFT_PATCH = obj({"op": {"type": "string", "enum": ["add", "replace", "remove"]},
    "path": {"type": "string", "minLength": 1, "maxLength": 2000,
             "description": "精确JSON pointer，不允许根路径；数组下标须规范十进制，不能01。"},
    "value": {"description": "add/replace时必需的原始JSON值；remove时省略。"}})
_DRAFT_PATCH["required"] = ["op", "path"]
REPAIR_DRAFT_SCHEMA = obj({"draft_id": _DRAFT_ID_SCHEMA, "draft_revision": {"type": "integer", "minimum": 1},
    "patches": {"type": "array", "items": _DRAFT_PATCH, "minItems": 1, "maxItems": 64}})
REVIEW_SCHEMA = obj({"draft_id": STR, "plan": {"type": "object", "additionalProperties": True},
                     "checklist": CHECKLIST_SCHEMA, "plan_settings": PLAN_SETTINGS_SCHEMA})
REVIEW_SCHEMA['required'] = []


def planning_tools():
    def tool(name, description, schema):
        return {"name": name, "description": description, "inputSchema": explicit_schema(schema)}
    query_schema = {"type": "string", "minLength": 1, "maxLength": 2000, "pattern": r"\S"}
    case_id_schema = {"type": "string", "minLength": 1, "maxLength": 256, "pattern": r"\S",
                      "description": "search_cases返回的精确id，不是asset_key或模型文件路径。"}
    search_schema = obj({"query": query_schema, "kind": {"type": "string", "enum": ["all", "manual", "case", "code"], "default": "all"},
        "mode": {"type": "string", "enum": ["qmd_keyword", "qmd_vector"], "default": "qmd_keyword",
                 "description": "仅qmd_keyword/qmd_vector是当前模式；兼容输入native/qmd会先归一为qmd_keyword，不启用其他后端。"},
        "feature_branch": {"type": "string", "enum": list(FEATURE_BRANCHES), "description": "可选：仅匹配已标注的建模分支，未标注来源会被排除。查通用定义或案例概览时可省略。"},
        "library_ids": dict(LIBRARY_IDS, description="可选：仅检索这些已启用资料库；可先get_library_scope查询目录并按需启用指定库。")})
    search_schema["required"] = ["query"]
    search_schema["properties"]["case_id"] = case_id_schema
    search_schema["properties"]["stage"] = {"type": "string", "enum": list(FEATURE_BRANCHES), "description": "可选：仅在case_id范围内筛选实现环节；与feature_branch硬过滤分开。"}
    cases_schema = obj({"query": query_schema, "library_ids": LIBRARY_IDS,
        "include_geometry_only": {"type": "boolean", "description": "可选，默认false：查仿真路线时排除已确认仅几何的变体；未知分类仍保留。查几何构造时可显式true。"}})
    cases_schema["required"] = ["query"]
    configuration_schema = obj({"case_id": case_id_schema,
        "category": {"type": "string", "enum": list(FEATURE_BRANCHES)}, "node_path": STR,
        "source": {"type": "string", "enum": ["saved", "api"], "default": "saved",
                   "description": "saved读取保存配置，返回source可能标为xml；api读取独立API观察。两种locator不可混用。"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 8, "default": 1}})
    configuration_schema["required"] = ["case_id"]
    guide_schema = obj({"case_id": case_id_schema, "stage": {"type": "string", "enum": list(FEATURE_BRANCHES)}})
    guide_schema["required"] = ["case_id"]
    scope_schema = obj({"query": {"type": "string", "maxLength": 2000}, "type": {"type": "string", "enum": LIBRARY_TYPES}})
    scope_schema["required"] = []
    return [
        tool("get_plan", "读取绑定方案的需求、模型事实、用户回答、当前流程、版本与几何事实白名单。首次及保存前调用。", obj({})),
        tool("search_knowledge", SEARCH_KNOWLEDGE_DESCRIPTION, search_schema),
        tool("search_cases", "在已启用资料库中使用QMD查找模型案例概要；仅返回概要，不与Frame/配置节点竞争。先读概要判断适用性，再用返回的精确id调用get_case_guide沿流程查设置；asset_key和文件路径不是case_id。命中不等于已读。", cases_schema),
        tool("get_case_guide", "读取已构建案例的实现流程导航。case_id必须是search_cases返回的精确id，不是asset_key或文件路径。默认只返流程概要；用stage按需展开单个环节的关键线索、原始配置/教程入口及缺口。若not_prepared，可read_case_configuration按category读取已有配置。关闭库和隔离测试集不可读；导航不算正文已读或设置已生效。", guide_schema),
        tool("read_case_configuration", "按精确案例ID读取真实保存配置或API观测，可用category和node_path定位网格/求解节点。只核查实际返回节点；partial、未生效或物理有效性未评估状态保持原样。非空成功读取计入该案例的阅读账本。", configuration_schema),
        tool("read_source", READ_SOURCE_DESCRIPTION, {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 30000, "pattern": r"\S"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "length": {"type": "integer", "minimum": 500, "maximum": 16000, "default": 12000}}}),
        tool("save_plan", "保存完整、可审阅的路线设计与流程。仅可引用当前会话实际读取的来源或原方案已有来源；不会替用户回答/确认。缺少用户输入写questions，方法证据不足写evidence_gaps。失败时保留原始草稿并返回draft_id及全部结构错误；用get_plan_draft/repair_plan_draft只修错误字段，保留其他物理细节。", SAVE_SCHEMA),
        tool("get_plan_draft", "按draft_id和精确JSON pointer有界读取本任务最近一次保存草稿及错误；不会保存方案、改变版本或授予来源已读权限。长内容用offset/length分页。", GET_DRAFT_SCHEMA),
        tool("repair_plan_draft", "对保留草稿应用精确add/replace/remove补丁；须携带最新draft_revision。整批原子应用，其他内容保持原样；仍有错误时修补结果会保留并返回新草稿版本，全部保存校验通过后才更新方案。不会授予来源已读权限。", REPAIR_DRAFT_SCHEMA),
        tool("review_plan", REVIEW_PLAN_DESCRIPTION + "可选draft_id审查保留草稿；可选plan审查内存候选；均省略则审查当前方案。不会修改方案或自动认定物理正确。", REVIEW_SCHEMA),
        tool("get_mechanism_checks", "获取通用机制实施清单：省略mechanisms先查看可选类别，再选择当前路线涉及的类别。返回独立实施问题和补查词；按check_ids登记设置，再绑定实施落点。清单不含案例答案，不是来源证据或物理判定；不会启用库或修改方案。", {
            "type": "object", "additionalProperties": False, "required": [],
            "properties": {"mechanisms": {"type": "array", "maxItems": 6,
                            "items": {"type": "string", "enum": list(known_mechanisms())}, "uniqueItems": True}}}),
        tool("save_plan_settings", "分阶段保存参考设置与迁移决策表：先以unresolved提取相关输入和来源关键设置，再按相同id更新采用/调整/不适用及拟用值。单行最多12KiB。服务保留已提取id清单，遗漏条目会提示，不能静默从最终表删除；无关项用not_applicable说明理由。revision为当前方案版本，不修改流程或授予阅读权限。将读取结果evidence_refs复制到提交字段source_refs，补stated_value；导航不是设置依据。save_plan或update_steps将暂存表与流程一起提交。", SETTINGS_SAVE_SCHEMA),
        tool("get_plan_settings", "分页读取设置表，默认最多6行且完整行合计不超过24KiB，可按step_id筛选。view=unhandled查看已提取却从本次更新中消失的条目；补回并采用、调整或不适用，不能静默丢弃。优先同版本暂存表，否则读保存版本；不证明物理正确。", SETTINGS_GET_SCHEMA),
        tool("update_steps", "按原id替换最多8个已有流程节点，不重写全文。保留未更改节点、问题、确认门槛和未决项；新增/删除节点或清除全局阻塞时用save_plan。", PATCH_SCHEMA),
        tool("get_library_scope", LIBRARY_SCOPE_DESCRIPTION, scope_schema),
        tool("set_libraries_enabled", "显式批量启用或关闭指定library_ids，无需用户确认，不构建索引或向量。按研究需要选择若干库，不自动启用全部库；未知或已删除库会拒绝整批。全局状态变更，不修改方案。", obj({"library_ids": {**LIBRARY_IDS, "items": {"type": "string", "minLength": 1, "maxLength": 100, "pattern": r"\S"}}, "enabled": {"type": "boolean"}})),
        tool(CONTRACT_TOOL, "查询当前实际接线工具的完整输入契约及指定字段规则、输出含义和语义约束；schema直接来自后端校验。签名出现unknown或修补字段前先读取此工具；不启动Agent、不读资料正文、不改变方案或阅读权限。", CONTRACT_SCHEMA),
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
            if tool["name"] in CONTRACT_TOOLS:
                continue
            tool["inputSchema"]["properties"]["session_id"]={"type":"string","maxLength":30000,"description":"工作台方案ID，与Agent或MCP连接会话无关。"}
            if tool["name"] in LIBRARY_TOOLS | GUIDANCE_TOOLS:
                continue
            if tool["name"] not in KNOWLEDGE_TOOLS:
                tool["inputSchema"]["required"].insert(0,"session_id")
            else:
                tool["description"]+=" 可省略session_id独立查询；提供时将阅读证据关联该方案。"
    for tool in tools:
        tool["description"] = describe_contract(tool)
    return tools


class PlanningService:
    def __init__(self,data_dir,sessions,kb):
        self.data_dir,self.sessions,self.kb=Path(data_dir),sessions,kb
        self.directory=self.data_dir/"tool-contexts"
        self.directory.mkdir(parents=True,exist_ok=True)
        self.entries={}
        self.lock=threading.RLock()
        self.search_lock=threading.Lock()
        self.drafts=PlanDraftStore(self.data_dir)

    def _entry(self,sid):
        self.sessions.get(sid)
        with self.lock:
            if sid not in self.entries:
                path=self.directory/(sid+".json")
                legacy=self.data_dir/"agent-sessions"/(sid+".json")
                saved=json.loads(path.read_text(encoding="utf-8")) if path.is_file() else json.loads(legacy.read_text(encoding="utf-8")) if legacy.is_file() else {}
                self.entries[sid]={"record":{"read_sources":list(dict.fromkeys(saved.get("read_sources",[])))}}
                for key in ("read_contexts", "plan_settings_stage"):
                    if key in saved:
                        self.entries[sid]["record"][key] = copy.deepcopy(saved[key])
                if not path.is_file() and legacy.is_file():
                    self._persist(sid,self.entries[sid])
            return self.entries[sid]

    def _persist(self,sid,entry):
        write_json(self.directory/(sid+".json"),entry["record"])

    def read_sources(self,sid):
        with self.lock:
            return list(self._entry(sid)["record"]["read_sources"])

    def _record_read_contexts(self, sid, entry, references):
        if entry is None:
            return
        with self.lock:
            contexts = entry["record"].setdefault("read_contexts", [])
            for ref in references:
                if ref not in contexts:
                    contexts.append(copy.deepcopy(ref))
            # Small locators only, never another copy of source text/config.
            del contexts[:-2000]
            self._persist(sid, entry)

    def _settings_rows(self, sid, value):
        staged = self._entry(sid)["record"].get("plan_settings_stage") or {}
        if staged.get("base_revision") == value["revision"]:
            return copy.deepcopy(staged.get("rows", [])), True
        return copy.deepcopy(value.get("plan_settings", [])), False

    def _unhandled_settings(self, sid, value, rows):
        stage = self._entry(sid)["record"].get("plan_settings_stage") or {}
        if stage.get("base_revision") != value["revision"]:
            return []
        present = {row["id"] for row in rows}
        return [item for item in stage.get("inventory", []) if item["id"] not in present]

    def _require_setting_dispositions(self, sid, value, rows, *, cleared=False):
        if not cleared and self._unhandled_settings(sid, value, rows):
            raise ValueError("plan_settings 有已提取条目尚未处置；用get_plan_settings(view=unhandled)查看，采用、调整或保留不适用理由后再保存。")

    @staticmethod
    def _validate_settings_links(rows, plan):
        steps = {step["id"] for step in plan["steps"]}
        for index, row in enumerate(rows):
            if set(row["step_ids"]) - steps:
                raise ValueError(f"plan_settings[{index}].step_ids 指向不存在的流程节点；请更新映射或显式保留为未关联的取舍。")

    def _settings_evidence(self, sid, value):
        read = set(self.read_sources(sid))
        read.update(source["id"] for source in value.get("sources", []))
        available = []
        for identity in read:
            try:
                source = self.kb.get_document(identity)
            except PermissionError:
                source = None
            if source and self._source_available(source):
                available.append(identity)
        return {"read_source_ids": sorted(read), "available_source_ids": available,
                "read_contexts": copy.deepcopy(self._entry(sid)["record"].get("read_contexts", []))}

    def _review_settings(self, sid, value, rows, *, plan=None):
        result = review_plan_settings(rows, plan=plan, evidence=self._settings_evidence(sid, value))
        mechanism_review = review_mechanism_coverage(rows, plan=plan)
        mechanism_summary = {key: mechanism_review[key] for key in (
            "status", "blocking", "physical_correctness", "coverage_state", "selected_profiles",
            "counts", "unaddressed_check_ids") if key in mechanism_review}
        mechanism_summary["warnings"] = mechanism_review.get("warnings", [])[:8]
        mechanism_summary["warnings_omitted"] = max(0, len(mechanism_review.get("warnings", [])) - 8)
        warnings = result["warnings"]
        missing = self._unhandled_settings(sid, value, rows)
        if missing:
            warnings = [{"code": "extracted_settings_unhandled", "category": "implementation_check",
                         "message": "部分已提取设置没有保留处置结果。", "setting_ids": [x["id"] for x in missing[:8]],
                         "unhandled_count": len(missing)}] + warnings
        if not rows:
            warnings = [{"code": "plan_settings_missing", "category": "implementation_check",
                         "message": "尚未记录需求到实际设置的迁移表；请先save_plan_settings，再将具体设置落实到步骤。"}] + warnings
        warnings = [dict(item, message=item["message"][:400],
                         **({"message_truncated": True} if len(item["message"]) > 400 else {})) for item in warnings]
        # No duplicate table, source values or repeated warning arrays in replies.
        return {key: result[key] for key in ("schema", "status", "blocking", "physical_correctness",
                "source_value_verification", "source_applicability", "evidence_ledger_scope", "plan_step_ids_known")} | {
                    "row_count": len(rows), "warning_count": len(warnings), "warnings": warnings[:32],
                    "warnings_omitted": max(0, len(warnings) - 32),
                    "mechanism_coverage": mechanism_summary,
                    "next_action": "处理mechanism_coverage未登记项；将原子拟用值原文保留到implementation指定的步骤字段，实际动作另写action。有方法缺口就定向补查或保留条件。"}

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
        if name == "search_knowledge" and arguments.get("mode") in ("native", "qmd"):
            arguments = dict(arguments, mode=normalize_mode(arguments["mode"]))
        if name == CONTRACT_TOOL:
            from .chat_bridge_tools import external_tool_definitions
            return read_contract(list(definitions.values()) + external_tool_definitions(), arguments)
        if name in {"save_plan", "review_plan"} or name in DRAFT_TOOLS | SETTINGS_TOOLS:
            # Capture original content before shape validation. The task ID is
            # transport metadata and is never part of the saved draft payload.
            arguments = dict(arguments)
            sid = arguments.pop("session_id", None)
            return self.call(sid, name, arguments, provenance=provenance)
        schema=definitions[name]["inputSchema"]
        errors = schema_errors(arguments, schema)
        if errors:
            raise ToolContractError(name, errors)
        arguments=dict(arguments)
        sid=arguments.pop("session_id",None)
        return self.call(sid,name,arguments,provenance=provenance)

    def call(self, sid, name, args, *, provenance=None):
        if not isinstance(args, dict) and name != "save_plan":
            raise ValueError("工具参数必须是JSON对象。")
        if not isinstance(name,str) or name not in {"get_plan","get_task_context","save_plan","update_steps","review_plan"} | KNOWLEDGE_TOOLS | LIBRARY_TOOLS | DRAFT_TOOLS | SETTINGS_TOOLS | GUIDANCE_TOOLS | CONTRACT_TOOLS:
            raise ValueError("未知规划工具。")
        if sid is not None and not isinstance(sid,str):
            raise ValueError("session_id必须是方案ID文本。")
        name = "get_task_context" if name == "get_plan" else name
        if name == "search_knowledge" and args.get("mode") in ("native", "qmd"):
            args = dict(args, mode=normalize_mode(args["mode"]))
        if name != "save_plan":
            definition = next(t for t in tool_definitions(bound=True) if t["name"] == name)
            errors = schema_errors(args, definition["inputSchema"])
            if errors:
                if name in DRAFT_TOOLS:
                    raise PlanDraftError("Draft tool arguments do not match the public schema.", errors=errors, contract_tool=name)
                raise ToolContractError(name, errors)
        if name in CONTRACT_TOOLS:
            return read_contract(tool_definitions(bound=True, include_alias=True), args)
        if name in GUIDANCE_TOOLS:
            definition = next(t for t in planning_tools() if t["name"] == name)
            errors = schema_errors(args, definition["inputSchema"])
            if errors:
                raise ValueError("机制清单参数不合法：" + json.dumps(errors, ensure_ascii=False))
            return mechanism_checklist(args.get("mechanisms"))
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
                from .case_context_index import dependency_library_scope
                return library_scope_for_agent(dict(store.library_scope(**args), **dependency_library_scope(store)))
            return store.set_enabled_many(args["library_ids"], args["enabled"])
        if sid is None and name not in KNOWLEDGE_TOOLS:
            raise ValueError("此工具需要明确的 session_id。")
        entry = self._entry(sid) if sid is not None else None
        if name in SETTINGS_TOOLS:
            schema = SETTINGS_SAVE_SCHEMA if name == "save_plan_settings" else SETTINGS_GET_SCHEMA
            errors = schema_errors(args, schema)
            if errors:
                raise ValueError("设置表参数不合法：" + json.dumps(errors, ensure_ascii=False))
            value = self.sessions.get(sid)
            if name == "save_plan_settings":
                self.sessions.require_revision(value, args["revision"])
                rows = validate_plan_settings(args["settings"])
                with self.lock:
                    self.sessions.require_revision(self.sessions.get(sid), args["revision"])
                    previous = entry["record"].get("plan_settings_stage") or {}
                    inventory = {x["id"]: x for x in previous.get("inventory", [])} if previous.get("base_revision") == args["revision"] else {}
                    for row in rows:
                        inventory.setdefault(row["id"], {"id": row["id"], "setting": row["setting"][:160]})
                    if len(inventory) > 64:
                        raise ValueError("同版本设置清单过大；请先整理路线和处置已有条目。")
                    entry["record"]["plan_settings_stage"] = {"base_revision": args["revision"], "rows": rows,
                                                              "inventory": list(inventory.values())}
                    self._persist(sid, entry)
                review = self._review_settings(sid, value, rows)
                return {"saved": True, "scope": "pending_plan_settings", "revision": args["revision"],
                        "row_count": len(rows), "unhandled_count": len(self._unhandled_settings(sid, value, rows)), "review": review,
                        "note": "设置表已暂存；尚未修改方案流程。用该表细化步骤后save_plan，引用和版本会再次核对。"}
            rows, pending = self._settings_rows(sid, value)
            missing = self._unhandled_settings(sid, value, rows)
            view = args.get("view", "settings")
            if view == "unhandled":
                rows = missing
            if args.get("step_id") and view == "unhandled":
                raise ValueError("未处置清单不支持step_id筛选。")
            if args.get("step_id"):
                rows = [row for row in rows if args["step_id"] in row["step_ids"]]
            offset, limit = args.get("offset", 0), args.get("limit", 6)
            page = {"revision": value["revision"], "pending": pending, "row_count": len(rows),
                    "view": view, "unhandled_count": len(missing), "rows": [], "offset": offset,
                    "next_offset": None, "physical_correctness": "not_evaluated"}
            for row in rows[offset:offset + limit]:
                page["rows"].append(row)
                if len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 24 * 1024 - 256:
                    page["rows"].pop()
                    break
            if offset < len(rows) and not page["rows"]:
                raise ValueError("单行设置超出分页大小，请先按独立设置拆分该行。")
            end = offset + len(page["rows"])
            page["next_offset"] = end if end < len(rows) else None
            return page
        if name == 'review_plan':
            if sid is None:
                raise ValueError('review_plan需要当前方案session_id。')
            errors = schema_errors(args, REVIEW_SCHEMA)
            if errors:
                raise ValueError('review_plan参数不合法：' + json.dumps(errors, ensure_ascii=False))
            if 'draft_id' in args and 'plan' in args:
                raise ValueError('review_plan的draft_id与plan只能选择一个。')
            context = self.sessions.get(sid)
            candidate = self.drafts.load(sid, args['draft_id'])['payload'] if 'draft_id' in args else context
            if not isinstance(candidate, dict):
                raise ValueError('草稿根对象格式错误；请先根据草稿错误修复结构，再审查实施内容。')
            plan = args.get('plan', candidate.get('plan'))
            read = self.read_sources(sid)
            historical = {source['id']: source for source in context.get('sources', [])}
            available = []
            for identity in set(read) | set(historical):
                try:
                    source = self.kb.get_document(identity)
                except PermissionError:
                    source = None
                if source and self._source_available(source):
                    available.append(identity)
            result = review_plan(plan, context={key: context.get(key) for key in ('goal','model','questions','answers')},
                               design_brief=candidate.get('design_brief'), checklist=args.get('checklist'),
                               evidence={'read_source_ids': list(set(read) | set(historical)),
                                         'available_source_ids': available})
            if 'plan_settings' in args:
                settings = args['plan_settings']
            elif 'draft_id' in args and 'plan_settings' in candidate:
                settings = candidate['plan_settings']
            else:
                settings = self._settings_rows(sid, context)[0]
            result['settings_review'] = self._review_settings(sid, context, settings, plan=plan)
            return result
        if name in DRAFT_TOOLS:
            schema = GET_DRAFT_SCHEMA if name == "get_plan_draft" else REPAIR_DRAFT_SCHEMA
            errors = schema_errors(args, schema)
            if errors:
                raise PlanDraftError("Draft tool arguments do not match the public schema.", errors=errors)
            if name == "get_plan_draft":
                return self.drafts.read(sid, args["draft_id"], SAVE_SCHEMA,
                    **{key: args[key] for key in ("path", "offset", "length") if key in args})
            with self.drafts.lock:
                try:
                    draft = self.drafts.patch(sid, args["draft_id"], args["draft_revision"], args["patches"])
                except PlanDraftError as error:
                    error.contract_tool = "repair_plan_draft"
                    raise
                return self._commit_draft(sid, draft, provenance=provenance)
        if name == "get_task_context":
            value = self.sessions.get(sid)
            settings, pending_settings = self._settings_rows(sid, value)
            sources = []
            for source in value.get('sources', []):
                summary = candidate_for_agent(source_context_view(source, available=self._source_available(source)))
                summary.pop('candidate_only', None)
                summary.pop('read_required', None)
                sources.append(summary)
            return {key: value.get(key) for key in ("id", "revision", "goal", "model", "plan", "design_brief", "questions", "answers", "plan_stale", "planning_incomplete", "evidence_gaps", "research_trace")} | {
                "plan_settings_summary": {"row_count": len(settings), "pending": pending_settings,
                    "unhandled_count": len(self._unhandled_settings(sid, value, settings)),
                    "next_tool": "get_plan_settings", "physical_correctness": "not_evaluated"},
                "sources": sources, "sources_note": "已有来源保留引用身份和有界摘要；完整正文由 read_source 按 id 分页读取。已读证据账本保持不变。",
                "geometry_reference_catalog": geometry_reference_catalog(value.get("model"))}
        if name in {"search_cases", "get_case_guide", "read_case_configuration"}:
            schema = next(t["inputSchema"] for t in planning_tools() if t["name"] == name)
            if set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
                raise ValueError("案例工具参数字段与schema不符。")
            for key, value in args.items():
                _check_shape(value, schema["properties"][key], "arguments." + key)
            if name == "read_case_configuration" and "limit" in args:
                if type(args["limit"]) is not int or not 1 <= args["limit"] <= 8:
                    raise ValueError("配置读取limit必须在1至8之间。")
            if name == "search_cases":
                query = args["query"]
                if not query.strip() or len(query) > 2000:
                    raise ValueError("案例查询需要1至2000字符。")
                extra = {}
                if "include_geometry_only" in args:
                    if type(args["include_geometry_only"]) is not bool:
                        raise ValueError("include_geometry_only必须是布尔值。")
                    extra["include_geometry_only"] = args["include_geometry_only"]
                with self.search_lock:
                    hits = self.kb.search_cases(query, library_ids=args.get("library_ids"), limit=6, **extra)
                return {"mode": "qmd_keyword", "retrieval_view": "case_overviews", "results": [candidate_for_agent(h) for h in hits],
                        "note": "仅模型概要；用结果中的精确id作为case_id获取流程导航，再按stage展开或读取原始配置。增强概要还依赖教程来源库；get_library_scope会列出case_context_dependencies，须按需一并启用。需读取原文核对适用性。"}
            store = getattr(self.kb, "store", None)
            if store is None:
                raise RuntimeError("当前知识提供方未连接资料库。")
            if name == "get_case_guide":
                from .case_guides import get_case_guide
                return _guide_view(get_case_guide(store, args["case_id"]), args.get("stage"))
            authority_reader = getattr(self.kb, "get_case_authority", self.kb.get_document)
            source = authority_reader(args["case_id"])
            if not source or source.get("type") != "model_case":
                raise ValueError("配置读取需要精确的模型案例概要ID。")
            if not self._source_available(source):
                raise PermissionError("资料库已关闭或删除，不能新读取该来源。")
            node_path = args.get("node_path")
            # The storage reader accepts a locator substring. A compact node
            # number is an exact selection, so delimit it from node 30/300 etc.
            import re
            exact_node = isinstance(node_path, str) and re.fullmatch(r"compact:node/[0-9]+", node_path)
            lookup = node_path + " " if exact_node else node_path
            result = store.configuration(source["id"], category=args.get("category"), node_path=lookup,
                                         source=args.get("source", "saved"), limit=args.get("limit", 1))
            store.require_not_heldout(source["id"], source)
            if not self._source_available(source):
                raise PermissionError("资料库已关闭或删除，不能新读取该来源。")
            if exact_node and any(n.get("path") != node_path for n in result.get("nodes", [])):
                raise RuntimeError("配置读取返回了不同节点。")
            if result.get("returned", 0) and entry is not None:
                with self.lock:
                    store.require_not_heldout(source["id"], source)
                    if not self._source_available(source):
                        raise PermissionError("资料库已关闭或删除，不能新读取该来源。")
                    if source["id"] not in entry["record"]["read_sources"]:
                        entry["record"]["read_sources"].append(source["id"])
                        self._persist(sid, entry)
            viewed = _configuration_view(result)
            refs = []
            for node in viewed.get("nodes", []):
                path = node.get("path")
                if not isinstance(path, str) or not path:
                    continue
                evidence_path = args.get("source", "saved") + ":" + path
                refs.append({"source_id": source["id"], "context": "case_configuration", "locator": evidence_path})
                for index, prop in enumerate(node.get("properties", [])):
                    locator = evidence_path + "::property/" + str(prop.get("id", index))
                    prop["evidence_locator"] = locator
                    refs.append({"source_id": source["id"], "context": "case_configuration", "locator": locator})
            self._record_read_contexts(sid, entry, refs)
            return dict(viewed, case_id=source["id"], source_context=candidate_view(source)["source_context"],
                        evidence_refs=[ref for ref in refs if "::property/" not in ref["locator"]],
                        note="仅已返回节点的配置已读；保存值/API getter不等于实际生效，物理有效性未验证。")
        if name == "search_knowledge":
            query, kind, mode = args.get("query"), args.get("kind", "all"), args.get("mode", "qmd_keyword")
            branch = args.get("feature_branch")
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000 or not isinstance(kind,str) or kind not in {"all", "manual", "case", "code"} or not isinstance(mode,str) or mode not in {"native", "qmd", "qmd_keyword", "qmd_vector"}:
                raise ValueError("检索参数不合法。")
            if "feature_branch" in args and not isinstance(branch, str):
                raise ValueError("feature_branch不是支持的建模分支。")
            branch = normalize_feature_branch(branch)
            mode = normalize_mode(mode)
            filters = {"mode": mode}
            if "case_id" in args:
                if not isinstance(args["case_id"], str) or not args["case_id"].strip():
                    raise ValueError("case_id必须是精确案例概要ID。")
                filters["case_id"] = args["case_id"]
            if "stage" in args:
                if not args.get("case_id"):
                    raise ValueError("stage需要同时提供case_id。")
                filters["stage"] = normalize_feature_branch(args["stage"])
            if branch is not None:
                filters["feature_branch"] = branch
            if "library_ids" in args:
                library_ids = args["library_ids"]
                if not isinstance(library_ids, list) or not 1 <= len(library_ids) <= 1000 or any(not isinstance(lid, str) or not lid.strip() or len(lid) > 30000 for lid in library_ids):
                    raise ValueError("library_ids需要1至1000个非空资料库ID。")
                filters["library_ids"] = list(dict.fromkeys(library_ids))
            # Concurrent FTS scopes multiply CPU/IO and exceed tool deadlines.
            # Serialize only retrieval; task context, reads and library changes
            # retain their independent locking and lifecycle checks.
            with self.search_lock:
                hits = self.kb.search(query, limit=6, kind=kind, filters=filters)
            search_scope = {"feature_branch": branch,
                "branch_filter_mode": "explicit_labels_only" if branch is not None else "not_applied",
                "unlabeled_sources_excluded": branch is not None}
            if branch is not None:
                search_scope["note"] = "分支为硬过滤，仅匹配已标注来源；未标注的通用手册或案例概览会被排除。查定义、概览或结果不足时，可显式省略feature_branch重新检索。"
            return {"mode": mode, "search_scope": search_scope, "results": [candidate_for_agent(hit) for hit in hits], "note": "候选检索结果；请read_source后再引用。"}
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
                    links.append({"id": target["id"], "title": target["title"],
                                  **({"library_id": target["library_id"]} if target.get("library_id") else {})})
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
                "length": length, "returned_characters": len(text[offset:offset + length]),
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
            ref = {"source_id": source["id"], "context": "source_text", "locator": f"text:{offset}:{offset + len(response['text'])}"}
            self._record_read_contexts(sid, entry, [ref])
            viewed = dict(source_page_for_agent(response), evidence_refs=[ref])
            if source.get("type") == "saved_configuration":
                data = source.get("data") or {}
                node = (data.get("compact_ref") or {}).get("node_id")
                viewed["configuration_hint"] = {"tool": "read_case_configuration", "scope": "source_text_only",
                    "note": "本页可能仅有配置结构摘要，不代表已读实际属性。用search_cases/get_case_guide中的案例概要ID读取键值。",
                    **({"node_path": "compact:node/" + str(node)} if type(node) is int else {})}
            return viewed
        if name in {"save_plan", "update_steps"}:
            return self._save(sid, name, args, provenance=provenance)
        raise ValueError("未知规划工具。")


    def _save(self, sid, name, args, *, provenance=None):
        if name == "save_plan":
            with self.drafts.lock:
                draft = self.drafts.capture(sid, args)
                return self._commit_draft(sid, draft, provenance=provenance)
        return self._commit_save(sid, name, args, provenance=provenance)

    def _commit_draft(self, sid, draft, *, provenance=None):
        errors = schema_errors(draft["payload"], SAVE_SCHEMA)
        if errors:
            self.drafts.note_error(sid, draft, errors)
            raise PlanDraftError("Save draft has validation errors; repair only the listed fields.", draft=draft, errors=errors)
        try:
            result = self._commit_save(sid, "save_plan", draft["payload"], provenance=provenance)
        except (ValueError, PermissionError) as error:
            errors = [semantic_error(error)]
            self.drafts.note_error(sid, draft, errors)
            error_class = PlanDraftPermissionError if isinstance(error, PermissionError) else PlanDraftConflictError if isinstance(error, ConflictError) else PlanDraftError
            next_action = ({'tool': 'get_task_context', 'instruction': 'Read the current revision and changed inputs; then repair the retained draft only after checking whether its route remains applicable.'}
                           if isinstance(error, ConflictError) else None)
            raise error_class(str(error), draft=draft, errors=errors, next_action=next_action) from error
        self.drafts.mark_saved(sid, draft, result["revision"])
        return dict(result, draft_id=draft["draft_id"], draft_revision=draft["draft_revision"])

    def _commit_save(self, sid, name, args, *, provenance=None):
        errors = schema_errors(args, SAVE_SCHEMA if name == "save_plan" else PATCH_SCHEMA)
        if errors:
            raise ValueError(name + "参数不合法：" + json.dumps(errors, ensure_ascii=False))
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
            settings, pending_settings = self._settings_rows(sid, value)
            if pending_settings:
                settings = validate_plan_settings(settings)
                self._require_setting_dispositions(sid, value, settings)
            by_id = {step["id"]: step for step in args["steps"]}
            if not by_id or len(by_id) != len(args["steps"]) or not set(by_id) <= {s["id"] for s in plan["steps"]}:
                raise ValueError("局部更新须提供不同id的已有节点；增删节点请save_plan。")
            changed_sources = {source_id for step in args["steps"] for source_id in step["source_ids"]}
            if pending_settings:
                changed_sources.update(ref["source_id"] for row in settings for ref in row["source_refs"])
            self._require_available_sources(changed_sources, known)
            plan["steps"] = [copy.deepcopy(by_id.get(s["id"], s)) for s in plan["steps"]]
            if pending_settings:
                self._validate_settings_links(settings, plan)
            require_setting_implementations(settings, plan)
            engine = plan.pop("engine", None)
            validate_agent_plan(plan, list({**historical, **known}.values()))
            metadata=dict(provenance or {})
            plan["engine"] = engine or metadata.get("backend") or "external-agent"
            source_ids = {sid for s in plan["steps"] for sid in s["source_ids"]}
            if pending_settings:
                source_ids.update(ref["source_id"] for row in settings for ref in row["source_refs"])
            sources = list({**historical, **{sid: known.get(sid, historical.get(sid)) for sid in source_ids}}.values())
            trace=list(value.get("research_trace") or []) + [{"stage":"interactive_edit","status":"steps_updated",
                "backend":metadata.get("backend","external-agent"),"model":metadata.get("model"),
                "reasoning_effort":metadata.get("reasoning_effort"),"conversation_id":metadata.get("conversation_id"),
                "changed_step_ids":list(by_id),"physical_validation":"not_run"}]
            self._require_available_sources(changed_sources, known)
            bundle = {"plan": plan, "sources": sources,"research_trace":trace}
            if pending_settings:
                bundle["plan_settings"] = settings
            saved = self.sessions.apply_agent_bundle(sid, args["revision"], bundle, partial=True)
        else:
            response = copy.deepcopy(args)
            settings = validate_plan_settings(response.pop("plan_settings", self._settings_rows(sid, value)[0]))
            self._require_setting_dispositions(sid, value, settings, cleared=args.get("plan_settings") == [])
            response["selected_source_ids"] = []
            requested = {source_id for step in args["plan"]["steps"] for source_id in step["source_ids"]}
            requested.update(source_id for decision in args["design_brief"]["decisions"] for source_id in decision["source_ids"])
            requested.update(source_id for candidate in args["design_brief"]["simplification"]["candidates"] for source_id in candidate["source_ids"])
            requested.update(ref["source_id"] for row in settings for ref in row["source_refs"])
            if requested & unavailable:
                raise PermissionError("来源所属资料库已关闭或删除，不能用于本次新引用。请重新检索可用来源。")
            validate_response(response, known, {"model": value.get("model")})
            retired = _validated_retirements(args["retired_questions"], args["questions"], value["questions"])
            retired_ids = {q["id"] for q in retired}
            questions = _safe_question_list(args["questions"], [q for q in value["questions"] if q["id"] not in retired_ids])
            brief, plan = response["design_brief"], response["plan"]
            self._validate_settings_links(settings, plan)
            require_setting_implementations(settings, plan)
            ids = list(dict.fromkeys([sid for s in plan["steps"] for sid in s["source_ids"]] +
                [sid for d in brief["decisions"] for sid in d["source_ids"]] +
                [sid for c in brief["simplification"]["candidates"] for sid in c["source_ids"]] +
                [ref["source_id"] for row in settings for ref in row["source_refs"]]))
            gaps = [g.strip() for g in args["evidence_gaps"] if g.strip()]
            plan["missing_information"] = list(dict.fromkeys(plan["missing_information"] + gaps))
            incomplete = bool(gaps or all(r["status"] == "blocked" for r in brief["candidate_routes"]))
            metadata = dict(provenance or {})
            plan["engine"] = " / ".join(str(metadata[k]) for k in ("backend", "model", "reasoning_effort") if metadata.get(k)) or "external-agent"
            self._require_available_sources(ids, known)
            bundle = {"plan": plan, "design_brief": brief, "questions": questions, "retired_questions": retired,
                "plan_settings": settings,
                "sources": [known[s] for s in ids], "planning_incomplete": incomplete, "evidence_gaps": gaps,
                "geometry_reference_catalog": geometry_reference_catalog(value.get("model")),
                "research_trace": [{"stage": "conversation", "status": "blocked_evidence" if incomplete else "draft_validated",
                    "conversation_id": metadata.get("conversation_id"), "backend": metadata.get("backend", "external-agent"),
                    "model": metadata.get("model"), "reasoning_effort": metadata.get("reasoning_effort"), "source_count": len(ids),
                    "evidence_gaps": gaps, "physical_validation": "not_run"}]}
            self._require_available_sources(ids, known)
            saved = self.sessions.apply_agent_bundle(sid, args["revision"], bundle)
        with self.lock:
            record = self._entry(sid)["record"]
            if (record.get("plan_settings_stage") or {}).get("base_revision") == args["revision"]:
                record.pop("plan_settings_stage", None)
                try:
                    self._persist(sid, self._entry(sid))
                except OSError:
                    # A stale scratch has an older revision and cannot be reused.
                    pass
        return {"saved": True, "revision": saved["revision"], "status": saved["status"],
            "planning_incomplete": bool(saved.get("planning_incomplete")), "note": "已更新工作台流程；仍需用户审阅确认，未运行求解。"}

