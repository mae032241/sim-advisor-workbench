"""Optional MCP/HTTP tools for an external Agent mailbox, not its runtime."""
from __future__ import annotations
import copy
from .external_agents import ExternalAgentBridge
from .plan_drafts import schema_errors
from .tool_contracts import ToolContractError, describe_contract, explicit_schema


def external_tool_definitions():
    def text(limit):
        return {"type":"string","minLength":1,"maxLength":limit,"pattern":r"\S"}
    sid={"type":"string","pattern":"^[a-f0-9]{32}$","description":"工作台方案ID，不是Agent或MCP会话ID。"}
    token={"type":"string","minLength":1,"description":"直接复制活动注册响应中的binding_id；凭据必须与本方案当前绑定一致。"}
    event={"type":"object","properties":{
        "type":{"type":"string","enum":["delta","message","state","activity","error"]},
        "message_id":text(128),"text":{"type":"string","maxLength":ExternalAgentBridge.MAX_TEXT,
            "description":"delta/message必须提供非空text；activity可省略或为空且最多1000字符；error必须提供非空白text且最多2000字符。"},
        "status":{"type":"string","enum":["running","idle"]},"turn_id":text(128)},
        "required":["type"],"additionalProperties":False,
        "description":"按type解释：delta/message必须有message_id及非空text；state必须有status；activity.text默认空且最多1000字符；error.text须非空白且最多2000字符。turn_id省略时沿用当前轮次；只有state切换running/idle，message本身不代表本轮结束。"}
    def definition(name,description,properties,required):
        value = {"name":name,"description":description,"inputSchema":explicit_schema({"type":"object","properties":properties,"required":required,"additionalProperties":False})}
        value["description"] = describe_contract(value)
        return value
    return [
        definition("register_external_agent","将本机常驻外部Agent绑定到方案，不启动Codex。提供Agent/harness常驻PID；返回binding_id及参考规划指令。",{
            "session_id":sid,"name":text(128),"pid":{"type":"integer","minimum":1,"maximum":0xFFFFFFFF,"description":"本机仍存活的常驻harness进程ID。"},
            "backend":dict(text(128),default="external-agent",description="外部harness报告的后端标识，默认external-agent；用于绑定工具保存归因。"),
            "model":text(128),"reasoning_effort":text(64),"conversation_id":text(256)},["session_id","name","pid"]),
        definition("poll_external_messages","读取网页message/interrupt/disconnect命令并刷新心跳。持续轮询；after为上次cursor。",{
            "session_id":sid,"binding_id":token,"after":{"type":"integer","minimum":0,"default":0,"description":"已消费的cursor；不能超过当前收件箱cursor。"}},["session_id","binding_id"]),
        definition("publish_external_event","回传回复、增量、运行状态或活动；delta/message需message_id，state.status决定running/idle。不修改方案。",{
            "session_id":sid,"binding_id":token,"event":event},["session_id","binding_id","event"]),
        definition("unregister_external_agent","解除外部Agent绑定，保留聊天和方案，不终止外部进程。",{
            "session_id":sid,"binding_id":token},["session_id","binding_id"]),
    ]


class ChatBridgeTools:
    def __init__(self,agents):
        self.agents=agents

    def call(self,name,arguments):
        definitions={d["name"]:d for d in external_tool_definitions()}
        if not isinstance(name,str) or name not in definitions or not isinstance(arguments,dict):
            raise ValueError("外部Agent工具名称或参数对象无效。")
        schema=definitions[name]["inputSchema"]
        errors = schema_errors(arguments, schema)
        if errors:
            raise ToolContractError(name, errors)
        if name == "publish_external_event":
            # Conditional fields share the base definition. Expose their
            # per-type rules in the event description and validate them here
            # before forwarding to the mailbox implementation.
            event = arguments["event"]
            event_schema = copy.deepcopy(schema["properties"]["event"])
            kind = event["type"]
            if kind in {"delta", "message"}:
                event_schema["required"] += ["message_id", "text"]
                event_schema["properties"]["text"]["minLength"] = 1
            elif kind == "state":
                event_schema["required"].append("status")
            elif kind == "activity":
                event_schema["properties"]["text"]["maxLength"] = 1000
            elif kind == "error":
                event_schema["required"].append("text")
                event_schema["properties"]["text"] = {"type":"string","minLength":1,"maxLength":2000,"pattern":r"\S"}
            errors = schema_errors(event, event_schema, "/event")
            if errors:
                raise ToolContractError(name, errors)
        args=copy.deepcopy(arguments)
        sid=args.pop("session_id")
        if not isinstance(sid,str):
            raise ValueError("session_id需为方案ID文本。")
        method={"register_external_agent":"register_external","poll_external_messages":"poll_external",
            "publish_external_event":"publish_external","unregister_external_agent":"unregister_external"}[name]
        return getattr(self.agents,method)(sid,args)
