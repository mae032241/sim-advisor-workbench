import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from advisor.planning_service import PlanningService,tool_definitions
from advisor.server import App,make_server
from advisor.sessions import SessionStore,write_json
from test_workflow import FakeKB,FakePlanner,GOAL,SOURCE,TEST_TEMP
from test_agent_sessions import payload


class PlanningServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TEST_TEMP)
        self.root=Path(self.temp.name)
        self.kb=FakeKB()
        self.sessions=SessionStore(self.root,self.kb,FakePlanner())
        self.task=self.sessions.create(GOAL,None)
        self.sid=self.task["id"]
        self.tools=PlanningService(self.root,self.sessions,self.kb)

    def tearDown(self):
        self.temp.cleanup()

    def read(self,sid=None):
        args={"id":SOURCE["id"],"offset":0,"length":500}
        if sid: args["session_id"]=sid
        return self.tools.call_public("read_source",args)

    def test_public_schemas_are_backend_neutral_and_scope_is_explicit(self):
        definitions={s["name"]:s for s in tool_definitions()}
        self.assertEqual(set(definitions),{"get_task_context","search_knowledge","search_cases","get_case_guide","read_case_configuration","read_source","save_plan","update_steps","get_library_scope","set_libraries_enabled","get_plan_draft","repair_plan_draft","review_plan","save_plan_settings","get_plan_settings","get_mechanism_checks","get_tool_contract"})
        for name,definition in definitions.items():
            self.assertEqual("session_id" in definition["inputSchema"]["required"],name not in {"search_knowledge","search_cases","get_case_guide","read_case_configuration","read_source","get_library_scope","set_libraries_enabled","get_mechanism_checks","get_tool_contract"})
        self.assertIn("get_plan",{s["name"] for s in tool_definitions(bound=True,include_alias=True)})
        self.assertNotIn("session_id",tool_definitions(bound=True)[0]["inputSchema"]["properties"])

    def test_unbound_knowledge_does_not_create_agent_or_authorize_plan_citations(self):
        with patch("subprocess.Popen",side_effect=AssertionError("must not launch a process")):
            hits=self.tools.call_public("search_knowledge",{"query":"thermal","kind":"manual","mode":"native"})
            self.assertEqual(hits["results"][0]["id"],SOURCE["id"])
            self.read()
            self.assertFalse((self.root/"agent-sessions").exists())
            with self.assertRaises(ValueError):
                self.tools.call_public("save_plan",dict(payload(self.task),session_id=self.sid))
            self.read(self.sid)
            self.tools.call_public("save_plan",dict(payload(self.task),session_id=self.sid))
        self.assertEqual(self.sessions.get(self.sid)["plan"]["engine"],"external-agent")

    def test_evidence_and_plan_updates_are_shared_across_adapters(self):
        self.read(self.sid)
        reconnected=PlanningService(self.root,self.sessions,self.kb)
        self.assertEqual(reconnected.read_sources(self.sid),[SOURCE["id"]])
        reconnected.call(self.sid,"save_plan",payload(self.task),provenance={"backend":"test-harness","model":"model-a"})
        current=self.sessions.get(self.sid)
        step=copy.deepcopy(current["plan"]["steps"][0]);step["difficulty"]+=" 补查作用域。"
        result=self.tools.call_public("update_steps",{"session_id":self.sid,"revision":2,"steps":[step]})
        self.assertEqual(result["revision"],3)
        saved=self.sessions.get(self.sid)
        self.assertEqual(saved["plan"]["engine"],"test-harness / model-a")
        self.assertEqual(saved["research_trace"][-1]["backend"],"external-agent")
        self.assertEqual(saved["research_trace"][-1]["changed_step_ids"],[step["id"]])

    def test_legacy_read_ledger_migrates_without_runtime_dependency(self):
        write_json(self.root/"agent-sessions"/(self.sid+".json"),{"thread_id":"old-thread","read_sources":[SOURCE["id"]]})
        self.assertEqual(self.tools.read_sources(self.sid),[SOURCE["id"]])
        ledger=self.root/"tool-contexts"/(self.sid+".json")
        self.assertTrue(ledger.is_file())
        self.assertEqual(json.loads(ledger.read_text(encoding="utf-8")),{"read_sources":[SOURCE["id"]]})
        context=self.tools.call_public("get_task_context",{"session_id":self.sid})
        alias=self.tools.call_public("get_plan",{"session_id":self.sid})
        self.assertEqual(context,alias)
        self.assertNotIn("thread_id",context)

    def test_invalid_public_arguments_have_actionable_errors(self):
        cases=[([],{}),("get_task_context",{}),("get_task_context",{"session_id":[]}),
            ("search_knowledge",{"query":"thermal","kind":[],"mode":"native"}),
            ("search_knowledge",{"query":"thermal","kind":"all","mode":"native","unexpected":True})]
        for name,args in cases:
            with self.subTest(name=name,args=args),self.assertRaises(ValueError):
                self.tools.call_public(name,args)

    def test_context_summarizes_pinned_sources_but_keeps_business_context_and_read_ledger(self):
        self.read(self.sid)
        value = self.sessions.get(self.sid)
        source = {**SOURCE, "revision_id": "immutable-source-revision", "text": "BODY_SENTINEL " * 60000,
                  "evidence": [{"relative_path": "manual/source.html", "asset_hash": "source-hash"}],
                  "coverage": {"deep_api": {"status": "preview_only"}},
                  "data": {"summary": "保留来源作用域", "code": "CODE_SENTINEL " * 60000}}
        value["sources"] = [source]
        value["model"] = {"facts": [{"key": "cad.volume_sum", "value": {"value": 1, "unit": "m3"}}]}
        self.sessions._write(value)
        ledger = self.tools.read_sources(self.sid)
        context = self.tools.call_public("get_task_context", {"session_id": self.sid})
        summary = context["sources"][0]
        for key in ("id", "revision_id", "coverage"):
            self.assertEqual(summary[key], source[key])
        self.assertEqual(summary['evidence_summary']['count'], 1)
        self.assertTrue(summary['evidence_summary']['details_omitted'])
        self.assertNotIn("text", summary)
        self.assertNotIn("code", json.dumps(summary))
        self.assertNotIn("candidate_only", summary)
        self.assertNotIn("BODY_SENTINEL", json.dumps(context))
        self.assertNotIn("CODE_SENTINEL", json.dumps(context))
        self.assertIn("read_source", context["sources_note"])
        for key in ("model", "plan", "goal", "questions", "answers", "design_brief"):
            self.assertEqual(context[key], value.get(key))
        self.assertEqual(self.tools.read_sources(self.sid), ledger)
        with patch.object(self.kb, "get_document", return_value=copy.deepcopy(source)):
            page = self.tools.call(self.sid, "read_source", {"id": source["id"], "offset": 500, "length": 500})
        self.assertEqual(page["text"], source["text"][500:1000])
        self.assertEqual(page["next_offset"], 1000)

    def test_large_multi_source_context_fits_the_actual_managed_callback_without_losing_ids(self):
        from advisor.dsh_runtime import DshRuntime
        value = self.sessions.get(self.sid)
        value["sources"] = [{**SOURCE, "id": f"source-{index}", "revision_id": f"revision-{index}", "text": "x" * 750000} for index in range(3)]
        self.sessions._write(value)
        raw_bytes = len(json.dumps(value["sources"], ensure_ascii=False).encode())
        context = self.tools.call(self.sid, "get_task_context", {})
        compact_bytes = len(json.dumps(context, ensure_ascii=False).encode())
        self.assertGreater(raw_bytes, DshRuntime.MAX_BYTES)
        self.assertLess(compact_bytes, 16000)
        self.assertEqual([s["id"] for s in context["sources"]], [s["id"] for s in value["sources"]])
        runtime = object.__new__(DshRuntime)  # No process, environment or model initialization.
        runtime._closed, runtime._tools, runtime._thread_id = False, {"get_task_context"}, "fixture-thread"
        runtime.on_tool = lambda name, args: self.tools.call(self.sid, name, args)
        result = runtime._callback("/tool", {"name": "get_task_context", "thread_id": "fixture-thread", "arguments": {}})
        self.assertTrue(result["success"])


class IndependentHTTPToolsTests(unittest.TestCase):
    def test_none_mode_runs_tools_and_never_discovers_or_starts_codex(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory,patch("advisor.runtime_adapters.codex_command",side_effect=AssertionError("Codex unavailable")),patch("subprocess.Popen",side_effect=AssertionError("must not spawn Agent")):
            app=App(Path(directory),Path(directory)/"missing-docs",runtime_factory="none")
            app.tools.kb=FakeKB()
            task=app.sessions.create(GOAL,None)
            server=make_server(app,0)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            base=f"http://127.0.0.1:{server.server_port}"
            def request(path,body=None):
                data=None if body is None else json.dumps(body).encode()
                try:
                    with urlopen(Request(base+path,data=data,headers={"Content-Type":"application/json"}),timeout=5) as response:
                        return response.status,json.load(response)
                except HTTPError as exc:
                    return exc.code,json.load(exc)
            try:
                code,result=request("/api/tools")
                from advisor.chat_bridge_tools import external_tool_definitions
                self.assertEqual(code,200)
                self.assertEqual({tool['name'] for tool in result['tools']},
                                 {tool['name'] for tool in tool_definitions() + external_tool_definitions()})
                self.assertIn("get_mechanism_checks", {item["name"] for item in result["tools"]})
                code,result=request("/api/tools/call",{"name":"get_task_context","arguments":{"session_id":task["id"]}})
                self.assertEqual(code,200);self.assertEqual(result["result"]["id"],task["id"])
                code,result=request("/api/tools/call",{"name":"search_knowledge","arguments":{"query":"thermal","kind":"manual","mode":"native"}})
                self.assertEqual(code,200);self.assertEqual(result["result"]["results"][0]["id"],SOURCE["id"])
                self.assertEqual(request("/api/tools/call",{"name":[],"arguments":{}})[0],422)
                self.assertEqual(request(f"/api/sessions/{task['id']}/refine",{"revision":1})[0],502)
                self.assertEqual(request(f"/api/sessions/{task['id']}/agent/connect",{})[0],502)
                self.assertFalse(app.agents.status(task["id"])["alive"])
            finally:
                server.shutdown();server.server_close();worker.join();app.agents.close()


if __name__=="__main__":
    unittest.main()
