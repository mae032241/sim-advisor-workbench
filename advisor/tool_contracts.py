"""Transport-independent contracts derived from the schemas used to validate calls.

Runtime tool renderers may shorten nested schemas to ``unknown``. This module
keeps the original schema readable as JSON, without opening a task or a source.
"""
from __future__ import annotations

import copy
import hashlib
import json

CONTRACT_TOOL = "get_tool_contract"
CONTRACT_VERSION = "planning-tools/v1"
MAX_CONTRACT_PAGE = 16000
CONTRACT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["tool"],
    "properties": {
        "tool": {"type": "string", "minLength": 1, "maxLength": 100,
                 "description": "当前工具目录中的精确工具名。"},
        "path": {"type": "string", "maxLength": 2000, "default": "",
                 "description": "输入数据JSON pointer，如/design_brief/simplification/candidates/0/status；空文本返回完整输入schema。数组下标仅用于定位items规则。"},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "length": {"type": "integer", "minimum": 1, "maximum": MAX_CONTRACT_PAGE,
                   "default": MAX_CONTRACT_PAGE},
    },
}

# Semantic rules cannot all be expressed by JSON Schema. Publish them alongside
# it; they describe existing checks, and never attest physical correctness.
_RULES = {
    "save_plan": [
        "revision必须等于get_task_context返回的当前版本；保存前重新读取，不能固定为1。",
        "步骤ID唯一，依赖只能指向前序步骤；引用必须来自当前方案真实阅读账本或仍可用的已有来源。关闭库和隔离测试集不能用于新引用。",
        "geometry_interpretation是Agent解释，不是解析器观察；observed会降为hypothesis。几何事实/实体引用只能来自当前任务白名单。",
        "简化候选需要非空几何依据或未知限制、验证方法和目标敏感特征；conditional的来源及几何支持规则见相应字段说明。",
        "已有问题身份不可重定义；删除问题需retired_questions逐项说明。text问题options为空，select问题有2至8个value唯一的选项。新增问题最终ID由服务根据label生成。",
        "暂存设置表的已登记项不能通过省略静默删除。plan_settings省略表示沿用；显式plan_settings=[]会清空设置表并跳过登记项处置检查，不能将其当作通用样例默认值。",
        "设置source_refs必须复制实际read_source/read_case_configuration的evidence_refs；导航、API观察、保存值和正文范围不可混用。机制设置的实施落点须包含proposed_value原文；只证明文本保留。",
        "无效提交保留任务专属草稿；修补成功应用到草稿不等于方案已保存。仅saved=true表示所有保存校验通过；不证明物理正确或已求解。",
        "单个完整草稿最多2097152个UTF-8字节；方案文本不截断或自动改写。",
    ],
    "repair_plan_draft": [
        "使用最近错误返回的精确draft_id和最新draft_revision，不能手写猜测；add要求字段不存在，replace/remove要求字段存在。",
        "add/replace必须提供value，remove不得提供value；不允许替换草稿根。整批补丁原子应用，批次结构错误不会部分改写。",
        "补丁应用后重新运行全部save_plan校验；仍有错误时草稿版本可能已增加，下一次须使用返回的新版本。saved=false不表示之前有效补丁丢失。",
    ],
    "get_plan_draft": [
        "只读取本任务最近草稿；path是输入数据JSON pointer，offset/length按JSON序列化文本字符分页。",
        "当前草稿值不是合法字段规范；字段类型、允许值请用get_tool_contract(tool=save_plan,path=同一路径)。不会授予来源阅读权限。",
    ],
    "save_plan_settings": [
        "每行紧凑JSON最多12288个UTF-8字节，整表最多2097152字节。设置id唯一；source_refs与step_ids不可重复。",
        "adopted/adapted/alternative需要非空proposed_value；unresolved表示未决，gap字段允许空文本，仍应在reason/transfer_rationale说明问题。",
        "source_refs应复制真实阅读回执；服务审阅会提示精确context/locator是否匹配，但该提示不是全部保存场景的硬门槛，也不验证stated_value抄写或物理适用性。",
        "仅暂存设置表，不提交方案；save_plan/update_steps再次验证来源、版本、登记项处置、步骤映射与实施落点。",
    ],
    "get_plan_settings": ["view=unhandled与step_id不可同时使用；默认offset=0、limit=6、view=settings。按next_offset续页，完整行页大小受24KiB限制。"],
    "update_steps": ["只能按原id替换已有且互不重复的步骤；增删步骤用save_plan。保留其他步骤、问题和缺口；当前版本、来源、设置落点仍会重新验证。"],
    "review_plan": ["draft_id与plan互斥；两者都不提供时审阅当前方案。结果是建议，physical_correctness未评估，不授予正文阅读权限。"],
    "search_knowledge": [
        "stage要求同时提供精确case_id；case_id取search_cases返回的id，不能使用asset_key或文件路径。",
        "library_ids仅缩小已启用范围，不自动启用库；feature_branch是显式标注硬过滤，会排除未标注来源。",
        "case_id/stage范围当前仅支持qmd_keyword；stage是案例流程环节，与feature_branch标签过滤不同。兼容输入native/qmd先归一为qmd_keyword，不启动其他后端。",
        "命中、摘要、目录导航不算已读证据；需read_source或read_case_configuration。",
    ],
    "search_cases": ["只查询已启用、未删除且非隔离测试集的案例概要；返回id供case_id使用，asset_key/路径不能替代。命中不授予阅读回执。"],
    "get_case_guide": ["默认流程概要；stage展开单个环节。not_prepared/need_rebuild表示导航状态，不能据此断言没有案例。导航及其source_refs不能代替正文或配置阅读回执。"],
    "read_case_configuration": ["source默认saved、limit默认1；saved响应source可能为xml，evidence定位仍用返回的saved前缀；api是独立观察，不能冒充保存值或已生效状态。", "只对实际返回的节点/属性及其evidence_locator建立阅读范围；partial、未生效和未评估状态保持原样。", "node_path一般为子串匹配；完整compact:node/N用于精确定位。truncated=true时通过案例流程选择更窄node_path，不猜offset分页。"],
    "read_source": [
        "offset/length按原正文Unicode字符计数，length是请求上限；尾页可少于最小请求长度，直接使用next_offset，null表示结束。",
        "total_characters是当前知识对象可返回text的长度，不是原始PDF/MPH/整篇来源长度。next_offset=null仅表示该text结束；仍需保留text_truncated/read_scope及配置提示的限制。",
        "只能引用返回text所覆盖的范围；evidence_refs直接复制，父子目录、链接、related_sources、配置结构摘要不算关联正文或属性已读。",
        "空白页或超出正文范围不会授予阅读权限；先查看total_characters和next_offset。来源所属库关闭/删除或被隔离后，旧id也不能新读取。",
    ],
    "get_library_scope": ["query是目录元数据字面子串，不是语义检索；零匹配时可不带query查看全目录。目录查询不启用库、不授予正文阅读权限。"],
    "set_libraries_enabled": ["全局批量操作，成功后再检索；启用与检索有依赖，不能并行。未知或已删除ID拒绝整批；隔离测试集不能由Agent启用。"],
    "get_mechanism_checks": ["返回通用实施问题，不是来源证据或物理判定；不启用资料库、不修改方案。"],
}
_OUTPUTS = {
    "read_source": {"identity": "id/source_id取实际返回身份", "paging": "offset,total_characters,next_offset；text为原文切片", "evidence": "evidence_refs只证明返回正文范围已读，不证明适用性"},
    "read_case_configuration": {"identity": "case_id是案例概要ID", "evidence": "evidence_refs及属性evidence_locator是实际读取范围", "coverage": "source,partial,read_scope,extraction_coverage不是求解或有效性认证"},
    "search_knowledge": {"results": "候选来源，引用前须读正文", "search_scope": "有效过滤范围，不是所有库均被查询的证明"},
    "search_cases": {"results": "候选案例概要；精确id作为case_id"},
    "save_plan": {"saved": "true才是持久化方案成功", "revision": "保存后的方案版本", "draft_revision": "草稿版本，与方案版本不同"},
    "repair_plan_draft": {"saved": "true才表示提交方案；false时看draft_revision及errors继续修补"},
}


def explicit_schema(schema):
    """Expose the existing bounded-validator defaults instead of hiding them."""
    result = copy.deepcopy(schema)
    if result.get("type") == "string":
        result.setdefault("maxLength", 30000)
    if result.get("type") == "array":
        result.setdefault("maxItems", 200)
    if "properties" in result:
        result["properties"] = {key: explicit_schema(value) for key, value in result["properties"].items()}
    for key in ("items", "additionalProperties"):
        if isinstance(result.get(key), dict):
            result[key] = explicit_schema(result[key])
    return result


def expectations(schema):
    keys = ("type", "enum", "const", "required", "additionalProperties", "minimum", "maximum",
            "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "pattern",
            "minItems", "maxItems", "uniqueItems", "default", "description")
    return {key: copy.deepcopy(schema[key]) for key in keys if key in schema}


class ToolContractError(ValueError):
    def __init__(self, tool, errors, message="工具参数不符合已发布契约。"):
        super().__init__(message)
        self.tool, self.errors = tool, errors

    def to_dict(self):
        return {"error": str(self), "error_code": "tool_arguments_invalid", "tool": self.tool,
                "errors": copy.deepcopy(self.errors), "saved": False,
                "next_action": {"tool": CONTRACT_TOOL, "arguments": {"tool": self.tool},
                                "instruction": "读取错误path的精确schema；按expected修正，不猜字段、枚举或边界。"}}


def schema_node(schema, path):
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise ValueError("path必须是输入数据的JSON pointer，空文本表示根schema。")
    node = schema
    for part in path.split("/")[1:] if path else []:
        part = part.replace("~1", "/").replace("~0", "~")
        if node.get("type") == "object" and part in node.get("properties", {}):
            node = node["properties"][part]
        elif node.get("type") == "array" and (part.isdecimal() or part == "*"):
            node = node.get("items", {})
        else:
            raise ValueError("该path不在工具输入契约中；请查询父路径的properties，不猜字段。")
    return node


def describe_contract(definition):
    """Descriptions survive code-mode type shortening; bounds stay generated."""
    name, schema = definition["name"], definition["inputSchema"]
    constraints = {}
    for key, spec in schema.get("properties", {}).items():
        if spec.get("type") in {"string", "integer", "number", "boolean"}:
            values = {k: v for k, v in expectations(spec).items() if k != "description"}
            constraints[key] = values
    required = schema.get("required", [])
    return (definition["description"] + "\n输入契约（与服务校验同源）：required=" + json.dumps(required, ensure_ascii=False)
            + "; scalar=" + json.dumps(constraints, ensure_ascii=False, separators=(",", ":"))
            + f"。签名中unknown、深层对象或修补值的完整字段规范，用{CONTRACT_TOOL}(tool=\"{name}\",path=输入字段JSON pointer)读取；按next_offset续页，不猜结构。")


def read_contract(definitions, arguments):
    from .plan_drafts import schema_errors
    errors = schema_errors(arguments, CONTRACT_SCHEMA)
    if errors:
        raise ToolContractError(CONTRACT_TOOL, errors)
    by_name = {item["name"]: item for item in definitions}
    tool = arguments["tool"]
    if tool not in by_name:
        raise ToolContractError(CONTRACT_TOOL, [{"path": "/tool", "code": "enum", "message": "未知工具名。", "expected": {"enum": sorted(by_name)}}])
    definition = by_name[tool]
    schema = explicit_schema(definition["inputSchema"])
    path, offset, length = arguments.get("path", ""), arguments.get("offset", 0), arguments.get("length", MAX_CONTRACT_PAGE)
    try:
        node = schema_node(schema, path)
    except ValueError as error:
        raise ToolContractError(CONTRACT_TOOL, [{"path": "/path", "code": "path", "message": str(error)}]) from error
    text = json.dumps(node, ensure_ascii=False, indent=2)
    if offset >= len(text) and offset != 0:
        raise ToolContractError(CONTRACT_TOOL, [{"path": "/offset", "code": "maximum", "message": "契约分页超出范围。", "expected": {"maximum": len(text) - 1}}])
    canonical = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    rules = list(_RULES.get(tool, []))
    if tool == "repair_plan_draft":
        rules += _RULES["save_plan"]
    return {"contract_version": CONTRACT_VERSION, "tool": tool, "path": path,
            "schema_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "format": "json", "text": text[offset:offset + length], "offset": offset,
            "returned_characters": len(text[offset:offset + length]), "total_characters": len(text),
            "next_offset": offset + length if offset + length < len(text) else None,
            "expected": expectations(node), "properties": sorted(node.get("properties", {})),
            "rules": rules, "output_contract": copy.deepcopy(_OUTPUTS.get(tool, {})),
            "read_authority": "none", "physical_correctness": "not_evaluated"}
