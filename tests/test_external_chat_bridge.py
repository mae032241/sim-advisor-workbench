import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from advisor.server import App,make_server
from test_workflow import GOAL,TEST_TEMP,FakeKB,SOURCE
from test_agent_sessions import FakeRuntime,payload


class ExternalChatBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TEST_TEMP)
        self.root=Path(self.temp.name)
        self.app=App(self.root,self.root/"missing-docs",runtime_factory="none")
        self.sid=self.app.sessions.create(GOAL,None)["id"]
        self.server=make_server(self.app,0)
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
        self.base=f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.worker.join()
        self.app.agents.close();self.temp.cleanup()

    def request(self,path,body=None):
        data=None if body is None else json.dumps(body,ensure_ascii=False).encode("utf-8")
        try:
            with urlopen(Request(self.base+path,data=data,headers={"Content-Type":"application/json"}),timeout=5) as response:
                return response.status,json.load(response)
        except HTTPError as exc:
            return exc.code,json.load(exc)

    def path(self,tail):
        return f"/api/sessions/{self.sid}/"+tail

    def register(self, **metadata):
        code,result=self.request(self.path("external/register"),{"name":"test external harness","pid":os.getpid(),"model":"external-model","reasoning_effort":"custom-effort","conversation_id":"external-conversation",**metadata})
        self.assertEqual(code,200)
        return result["binding_id"]

    def event(self,token,event):
        return self.request(self.path("external/event"),{"binding_id":token,"event":event})

    def test_web_inbox_stream_reply_and_stop_without_managed_runtime(self):
        with patch("subprocess.Popen",side_effect=AssertionError("must not start Agent")):
            token=self.register()
            state=self.request(self.path("agent"))[1]
            self.assertEqual(state["connection_mode"],"external")
            self.assertEqual(state["model"],"external-model")
            self.assertNotEqual(state["managed_model"],"external-model")
            self.assertNotIn(token,json.dumps(state))
            self.assertEqual(self.request(self.path("agent/message"),{"text":"请细化网格"})[0],200)
            polled=self.request(self.path("external/poll"),{"binding_id":token,"after":0})[1]
            self.assertEqual(polled["commands"][0]["text"],"请细化网格")
            cursor=polled["cursor"]
            for event in [{"type":"state","status":"running","turn_id":"t1"},
                          {"type":"delta","message_id":"m1","text":"已"},
                          {"type":"delta","message_id":"m1","text":"收到"},
                          {"type":"message","message_id":"m1","text":"已收到"}]:
                self.assertEqual(self.event(token,event)[0],200)
            self.assertEqual(self.request(self.path("agent"))[1]["status"],"running")
            self.assertEqual(self.request(self.path("agent/interrupt"),{})[0],200)
            commands=self.request(self.path("external/poll"),{"binding_id":token,"after":cursor})[1]["commands"]
            self.assertEqual(commands[0]["type"],"interrupt")
            self.event(token,{"type":"state","status":"idle"})
            state=self.request(self.path("agent"))[1]
            self.assertEqual(state["status"],"idle")
            self.assertEqual([m["text"] for m in state["messages"]],["请细化网格","已收到"])
            self.assertEqual(self.app.sessions.get(self.sid)["revision"],1)

    def test_detach_does_not_kill_process_or_fall_back_to_debug_agent(self):
        token=self.register()
        result=self.request(self.path("agent/disconnect"),{})
        self.assertEqual(result[0],200);self.assertFalse(result[1]["alive"])
        self.assertEqual(result[1]["connection_mode"],"external")
        code,result=self.request(self.path("agent/message"),{"text":"断开后的消息"})
        self.assertEqual(code,502);self.assertIn("外部",result["error"])
        inbox=self.request(self.path("external/poll"),{"binding_id":token})[1]
        self.assertFalse(inbox["connected"]);self.assertEqual(inbox["commands"][-1]["type"],"disconnect")
        self.assertEqual(os.getpid(),self.app.agents.external.status(self.sid)["pid"])
        late=self.event(token,{"type":"message","message_id":"late","text":"不应写入"})
        self.assertFalse(late[1]["accepted"])
        self.assertEqual(self.request(self.path("agent"))[1]["messages"],[])

    def test_mcp_style_public_tools_registration_is_task_scoped(self):
        code,result=self.request("/api/tools/call",{"name":"register_external_agent","arguments":{"session_id":self.sid,"name":"MCP external","pid":os.getpid()}})
        self.assertEqual(code,200)
        token=result["result"]["binding_id"]
        other=self.app.sessions.create("另一份完全独立的模型任务",None)["id"]
        self.request(f"/api/sessions/{other}/external/register",{"name":"other","pid":os.getpid()})
        self.assertEqual(self.request(f"/api/sessions/{other}/external/event",{"binding_id":token,"event":{"type":"state","status":"running"}})[0],403)
        self.assertEqual(self.request(self.path("agent/settings"),{"model":"other-model","reasoning_effort":"high","connection_mode":"managed"})[0],409)
        self.assertEqual(self.request(self.path("external/event"),{"binding_id":token,"event":{"type":"message","text":123}})[0],422)
        self.assertEqual(self.request(self.path("external/unregister"),{"binding_id":token})[0],200)

    def test_live_managed_agent_cannot_be_replaced_by_registration(self):
        self.app.agents.runtime_factory=lambda **kwargs:FakeRuntime(**kwargs)
        self.request(self.path("agent/connect"),{})
        self.assertEqual(self.request(self.path("external/register"),{"name":"attempt","pid":os.getpid()})[0],409)
        self.assertEqual(self.request(self.path("agent"))[1]["connection_mode"],"managed")

    def bound_tool(self, token, name="get_task_context", arguments=None, *, sid=None):
        return self.request(f"/api/sessions/{sid or self.sid}/external/tool",
                            {"binding_id":token,"name":name,"arguments":{} if arguments is None else arguments})

    def test_bound_http_tools_save_with_external_metadata_not_debug_settings(self):
        self.app.tools.kb=FakeKB()
        with patch("subprocess.Popen",side_effect=AssertionError("must not start Agent")), patch.object(self.app.agents,"call_tool",side_effect=AssertionError("must not attribute through managed adapter")):
            token=self.register(backend="dsh",model="gpt-6.1-sol",reasoning_effort="xhigh",conversation_id="dsh-chat-1")
            listed=self.request("/api/tools")[1]["tools"]
            from advisor.planning_service import tool_definitions
            from advisor.chat_bridge_tools import external_tool_definitions
            self.assertEqual({tool['name'] for tool in listed},
                             {tool['name'] for tool in tool_definitions()+external_tool_definitions()})
            self.assertNotIn("external_tool",{item["name"] for item in listed})
            register_schema=next(item["inputSchema"] for item in listed if item["name"]=="register_external_agent")
            self.assertIn("backend",register_schema["properties"])
            self.assertNotIn("backend",register_schema["required"])
            self.assertEqual(self.request(self.path("agent"))[1]["backend"],"dsh")
            code,context=self.bound_tool(token)
            self.assertEqual(code,200);self.assertEqual(context["result"]["id"],self.sid)
            self.assertEqual(self.bound_tool(token,"get_plan")[1],context)
            search={"query":"thermal expansion","kind":"manual","mode":"native"}
            self.assertEqual(self.bound_tool(token,"search_knowledge",search)[1]["result"]["results"][0]["id"],SOURCE["id"])
            draft=payload(self.app.sessions.get(self.sid))
            self.assertEqual(self.bound_tool(token,"save_plan",draft)[0],422)  # Search is not read evidence.
            self.assertEqual(self.bound_tool(token,"read_source",{"id":SOURCE["id"],"offset":0,"length":500})[0],200)
            self.assertEqual(self.app.tools.read_sources(self.sid),[SOURCE["id"]])
            code,result=self.bound_tool(token,"save_plan",draft)
            self.assertEqual(code,200);self.assertTrue(result["result"]["saved"])
            saved=self.app.sessions.get(self.sid)
            self.assertEqual(saved["plan"]["engine"],"dsh / gpt-6.1-sol / xhigh")
            provenance={"backend":"dsh","model":"gpt-6.1-sol","reasoning_effort":"xhigh","conversation_id":"dsh-chat-1"}
            self.assertEqual({key:saved["research_trace"][-1][key] for key in provenance},provenance)
            step=copy.deepcopy(saved["plan"]["steps"][0]);step["difficulty"]+=" 核查网格敏感性。"
            code,result=self.bound_tool(token,"update_steps",{"revision":saved["revision"],"steps":[step]})
            self.assertEqual(code,200);self.assertEqual(result["result"]["revision"],3)
            edited=self.app.sessions.get(self.sid)
            self.assertEqual({key:edited["research_trace"][-1][key] for key in provenance},provenance)
            self.assertEqual(edited["research_trace"][-1]["changed_step_ids"],[step["id"]])
            self.assertEqual(self.bound_tool(token,"update_steps",{"revision":2,"steps":[step]})[0],409)
            self.assertNotIn(token,json.dumps(edited,ensure_ascii=False))

    def test_bound_http_scope_credentials_and_tool_allowlist(self):
        token=self.register()
        other=self.app.sessions.create("完全独立的另一份仿真任务",None)["id"]
        self.assertEqual(self.bound_tool("wrong")[0],403)
        self.assertEqual(self.bound_tool("非ASCII凭据")[0],403)
        self.assertEqual(self.bound_tool(token,sid=other)[0],403)
        self.assertEqual(self.bound_tool(token,arguments={"session_id":other})[0],403)
        self.assertEqual(self.bound_tool(token,arguments={"session_id":self.sid})[0],200)
        for name in ["register_external_agent","publish_external_event","unknown",[]]:
            self.assertEqual(self.bound_tool(token,name)[0],422)
        self.assertEqual(self.bound_tool(token,arguments=[])[0],422)
        self.assertEqual(self.bound_tool(token,arguments={"provenance":{"backend":"forged"}})[0],422)
        extra={"binding_id":token,"name":"get_task_context","arguments":{},"model":"forged"}
        self.assertEqual(self.request(self.path("external/tool"),extra)[0],422)
        self.assertEqual(self.app.sessions.get(self.sid)["revision"],1)
        self.assertEqual(self.app.sessions.get(other)["revision"],1)

    def test_bound_http_refuses_detached_expired_replaced_and_dead_process(self):
        now=[1000.0]
        self.app.agents.external.clock=lambda:now[0]
        token=self.register()
        now[0]+=31
        self.assertEqual(self.bound_tool(token)[0],403)
        self.assertEqual(self.app.agents.external.status(self.sid)["last_seen"],1000.0)
        self.assertEqual(self.request(self.path("external/poll"),{"binding_id":token,"after":0})[0],200)
        self.assertEqual(self.bound_tool(token)[0],200)  # Explicit heartbeat restores the live owner.
        with patch.object(self.app.agents.external,"process_alive",return_value=False):
            self.assertEqual(self.bound_tool(token)[0],403)
        self.assertEqual(self.request(self.path("agent/disconnect"),{})[0],200)
        self.assertEqual(self.bound_tool(token)[0],403)
        replacement=self.register(backend="replacement-harness")
        self.assertNotEqual(token,replacement)
        self.assertEqual(self.bound_tool(token)[0],403)
        self.assertEqual(self.bound_tool(replacement)[0],200)
        self.assertEqual(self.request(self.path("external/unregister"),{"binding_id":replacement})[0],200)
        self.assertEqual(self.bound_tool(replacement)[0],403)

    def test_public_unbound_planning_calls_remain_compatible_after_binding(self):
        self.app.tools.kb=FakeKB()
        self.register(backend="dsh")
        request={"name":"search_knowledge","arguments":{"query":"thermal","kind":"manual","mode":"native"}}
        self.assertEqual(self.request("/api/tools/call",request)[0],200)
        request={"name":"get_task_context","arguments":{"session_id":self.sid}}
        self.assertEqual(self.request("/api/tools/call",request)[1]["result"]["id"],self.sid)
        self.assertEqual(self.request(self.path("external/unregister"),{"binding_id":self.app.agents.external._load(self.sid)["binding_id"]})[0],200)
        self.assertEqual(self.request("/api/tools/call",request)[0],200)


if __name__=="__main__":
    unittest.main()
