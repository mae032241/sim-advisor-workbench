"""Optional MCP/HTTP tools for an external Agent mailbox, not its runtime."""
from __future__ import annotations
import copy


def _validate(value,schema,where="arguments"):
    kind=schema.get("type")
    if kind=="object":
        if not isinstance(value,dict) or set(value)-set(schema["properties"]) or set(schema.get("required",[]))-set(value):
            raise ValueError(where+"字段与公开schema不符。")
        for name,child in value.items():
            _validate(child,schema["properties"][name],where+"."+name)
    elif kind=="string" and not isinstance(value,str):
        raise ValueError(where+"需为文本。")
    elif kind=="integer" and (type(value) is not int or value<schema.get("minimum",value)):
        raise ValueError(where+"需为有效整数。")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(where+"取值不支持。")


def external_tool_definitions():
    string={"type":"string"}
    sid={"type":"string","description":"工作台方案ID，不是Agent或MCP会话ID。"}
    token={"type":"string","description":"注册响应中的binding_id。"}
    event={"type":"object","properties":{
        "type":{"type":"string","enum":["delta","message","state","activity","error"]},
        "message_id":string,"text":string,"status":{"type":"string","enum":["running","idle"]},"turn_id":string},
        "required":["type"],"additionalProperties":False}
    def definition(name,description,properties,required):
        return {"name":name,"description":description,"inputSchema":{"type":"object","properties":properties,"required":required,"additionalProperties":False}}
    return [
        definition("register_external_agent","将本机常驻外部Agent绑定到方案，不启动Codex。提供Agent/harness常驻PID；返回binding_id及参考规划指令。",{
            "session_id":sid,"name":string,"pid":{"type":"integer","minimum":1},
            "backend":{"type":"string","description":"外部harness报告的后端标识，默认external-agent；用于绑定工具保存归因。"},
            "model":string,"reasoning_effort":string,"conversation_id":string},["session_id","name","pid"]),
        definition("poll_external_messages","读取网页message/interrupt/disconnect命令并刷新心跳。持续轮询；after为上次cursor。",{
            "session_id":sid,"binding_id":token,"after":{"type":"integer","minimum":0}},["session_id","binding_id"]),
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
        _validate(arguments,schema)
        args=copy.deepcopy(arguments)
        sid=args.pop("session_id")
        if not isinstance(sid,str):
            raise ValueError("session_id需为方案ID文本。")
        method={"register_external_agent":"register_external","poll_external_messages":"poll_external",
            "publish_external_event":"publish_external","unregister_external_agent":"unregister_external"}[name]
        return getattr(self.agents,method)(sid,args)
