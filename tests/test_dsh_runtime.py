"""Managed DSH bridge tests use a fake process and loopback HTTP, never a model."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from advisor.dsh_command import discover_dsh_command, dsh_status, launch_command
from advisor.dsh_runtime import DshRuntime, DshRuntimeError


FAKE_DSH = r'''
import json,os,sys,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import Request,build_opener,ProxyHandler
from pathlib import Path
callback=os.environ['SIM_ADVISOR_DSH_CALLBACK'];token=os.environ['SIM_ADVISOR_DSH_BRIDGE_TOKEN']
opener=build_opener(ProxyHandler({}));log=Path('fake-activity.jsonl')
def callback_call(route,data=None):
 body=None if data is None else json.dumps(data,ensure_ascii=False).encode('utf-8')
 req=Request(callback+route,data=body,headers={'X-DSH-Bridge-Token':token,'Content-Type':'application/json'})
 with opener.open(req,timeout=5) as response:return json.load(response)
config=callback_call('/config');sid=config['thread_id'] or 'session-fake-created'
def log_value(data):
 with log.open('a',encoding='utf-8') as f:f.write(json.dumps(data,ensure_ascii=False)+'\n')
log_value({'stage':'prepared','config':config,'argv':sys.argv[1:],'home':os.environ.get('HOME'),'userprofile':os.environ.get('USERPROFILE')})
if sid=='missing-session':
 callback_call('/error',{'code':'resume_failed'});sys.exit(2)
if sid=='startup-hang':threading.Event().wait(30)
busy=False;turn=None;counter=0
def emit(event):callback_call('/event',dict(event,thread_id=sid))
def complete(status='completed'):
 global busy
 emit({'type':'turn','event':'completed','status':status,'turn_id':turn});busy=False
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  global busy,turn,counter
  data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if self.headers.get('X-DSH-Bridge-Token')!=token:return self.reply({},403)
  log_value({'path':self.path,'data':data})
  if self.path=='/send':
   mode='steer' if busy else 'start'
   if not busy:
    counter+=1;turn='turn-'+str(counter);busy=True
    emit({'type':'turn','event':'started','status':'inProgress','turn_id':turn})
   text=data['text']
   if text=='complete-before-ack':
    emit({'type':'text_delta','text':'实际观察','item_id':'m1','turn_id':turn})
    emit({'type':'message','text':'实际观察','item_id':'m1','turn_id':turn});complete()
   if text=='private-output':
    sys.stdout.buffer.write(('SDK PRIVATE REASONING '+token+'\n').encode()*4000);sys.stdout.buffer.flush()
    sys.stderr.buffer.write(('RAW STDERR PRIVATE '+token+'\n').encode()*4000);sys.stderr.buffer.flush()
    emit({'type':'reasoning','text':'DO NOT EXPOSE'})
    emit({'type':'message','text':'safe '+token,'item_id':'safe-message','turn_id':turn})
    emit({'type':'error','message':'PRIVATE RAW ERROR '+token})
   if text=='invoke-tool':
    def tool():
     result=callback_call('/tool',{'name':config['tools'][0]['name'],'arguments':{'query':'几何证据'},'thread_id':sid,'call_id':'c1'})
     log_value({'tool_result':result});complete()
    threading.Thread(target=tool,daemon=True).start()
   if text=='exit-now':os._exit(2)
   return self.reply({'thread_id':sid,'turn_id':turn,'mode':mode})
  if self.path=='/interrupt':
   was_busy=busy
   if busy:complete('interrupted')
   return self.reply({'requested':was_busy,'turn_id':turn})
  if self.path=='/close':
   self.reply({'closed':True});threading.Thread(target=server.shutdown,daemon=True).start();return
  self.reply({},404)
 def reply(self,data,status=200):
  body=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
callback_call('/ready',{'thread_id':sid,'control_url':'http://127.0.0.1:'+str(server.server_port),
 'model':config['model'],'provider':config['provider'],'reasoning_effort':config['reasoning_effort'],'busy':False,
 'tool_names':['read','write','edit','pwsh']+[t['name'] for t in config['tools']]})
server.serve_forever(poll_interval=.05);server.server_close()
'''


class DshRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dsh-managed-", dir=Path(__file__).parent)
        self.root = Path(self.temporary.name).resolve()
        self.cwd = self.root / "workspace with spaces"
        self.cwd.mkdir()
        self.script = self.root / "fake_dsh.py"
        self.script.write_text(FAKE_DSH, encoding="utf-8")
        self.events, self.calls, self.runtimes = [], [], []
        self.thread_errors = []
        self.exception_hook = patch.object(threading, "excepthook", side_effect=self.thread_errors.append)
        self.exception_hook.start()

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.close()
        self.exception_hook.stop()
        self.assertEqual(self.thread_errors, [])
        self.assertEqual(self.root.parent, Path(__file__).parent.resolve())
        self.temporary.cleanup()

    def runtime(self, callback=None, **kwargs):
        def tool(name, args):
            self.calls.append((name, args)); return {"facts": ["observed"]}
        runtime = DshRuntime([sys.executable, str(self.script)], self.cwd, self.events.append, callback or tool, **kwargs)
        runtime.START_TIMEOUT = 5
        self.runtimes.append(runtime)
        return runtime

    @staticmethod
    def tools():
        return [{"name": "get_task_context", "description": "Read context", "inputSchema": {"type": "object"}}]

    def logs(self):
        return [json.loads(line) for line in (self.cwd / "fake-activity.jsonl").read_text(encoding="utf-8").splitlines()]

    def wait_for(self, predicate):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.01)
        self.fail("fake DSH condition timed out")

    def test_start_prepares_real_session_contract_without_prompt_and_keeps_native_tools(self):
        runtime = self.runtime(model="chosen-model", reasoning_effort="medium", provider="chosen-provider")
        started = runtime.start(tools=self.tools(), instructions="只做方案与代码调试，不主动求解。")
        self.assertEqual(started["thread_id"], "session-fake-created")
        self.assertTrue(runtime.status()["running"])
        self.assertFalse(runtime.status()["busy"])
        self.assertEqual(runtime.status()["backend_id"], "dsh")
        self.assertEqual(runtime.status()["provider"], "chosen-provider")
        self.assertTrue({"read", "write", "edit", "pwsh", "get_task_context"} <= set(runtime.status()["tool_names"]))
        record = self.logs()[0]
        self.assertEqual(record["config"]["model"], "chosen-model")
        self.assertEqual(record["config"]["reasoning_effort"], "medium")
        self.assertEqual(record["config"]["cwd"], str(self.cwd))
        self.assertEqual(record["home"], os.environ.get("HOME"))
        self.assertEqual(record["userprofile"], os.environ.get("USERPROFILE"))
        self.assertEqual(record["argv"][-3:-1], ["sdk", "--patch"])
        self.assertFalse(any(log.get("path") == "/send" for log in self.logs()))
        self.assertNotIn(runtime._token, (self.cwd / ".dsh-managed/host.patch.yml").read_text(encoding="utf-8"))
        self.assertEqual(self.calls, [])

    def test_delivery_failure_uses_only_an_allowlisted_code_and_fixed_local_message(self):
        runtime = self.runtime()
        runtime._thread_id = "fixture-session"
        runtime._event({"type": "error", "thread_id": "fixture-session", "code": "reply_delivery_failed",
                        "message": "PRIVATE HOST MESSAGE " + runtime._token, "turn_id": "fixture-turn"})
        error = self.events[-1]
        self.assertEqual(error["code"], "reply_delivery_failed")
        self.assertIn("网页回复未确认送达", error["message"])
        self.assertIn("不会自动重新执行", error["message"])
        self.assertNotIn("PRIVATE", json.dumps(error))
        self.assertNotIn(runtime._token, json.dumps(error))
        runtime._event({"type": "error", "thread_id": "fixture-session", "code": "arbitrary_private_code",
                        "message": "PRIVATE HOST MESSAGE"})
        self.assertEqual(self.events[-1]["code"], "dsh_error")
        self.assertEqual(self.events[-1]["message"], "DSH 本轮执行未完成；已保存方案保留。")

    def test_resume_preserves_exact_identity_and_missing_resume_never_creates_session(self):
        runtime = self.runtime()
        self.assertEqual(runtime.start(thread_id="existing-session")["thread_id"], "existing-session")
        runtime.close()
        failed = self.runtime()
        with self.assertRaisesRegex(DshRuntimeError, "无法恢复指定会话"):
            failed.start(thread_id="missing-session")
        self.assertFalse(failed.status()["running"])
        self.assertIsNone(failed.status()["thread_id"])
        self.assertEqual(self.logs()[-1]["config"]["thread_id"], "missing-session")

    def test_start_steer_interrupt_and_early_completion_do_not_resurrect_busy(self):
        runtime = self.runtime(); runtime.start()
        first = runtime.send("first")
        self.assertEqual(first["mode"], "start")
        self.assertTrue(runtime.status()["busy"])
        self.assertEqual(runtime.send("follow-up")["mode"], "steer")
        self.assertTrue(runtime.interrupt()["requested"])
        self.assertFalse(runtime.status()["busy"])
        completed = runtime.send("complete-before-ack")
        self.assertEqual(completed["mode"], "start")
        self.assertFalse(runtime.status()["busy"])
        self.assertIsNone(runtime.status()["turn_id"])
        self.assertIn("实际观察", [event.get("text") for event in self.events])
        self.assertFalse(any(event["type"] == "text_delta" for event in self.events))

    def test_callback_tool_can_block_while_interrupt_control_still_works(self):
        entered, release = threading.Event(), threading.Event()
        def tool(name, args):
            self.calls.append((name, args)); entered.set(); release.wait(3); return {"done": True}
        runtime = self.runtime(callback=tool); runtime.start(tools=self.tools())
        runtime.send("invoke-tool")
        self.assertTrue(entered.wait(2))
        try:
            self.assertTrue(runtime.interrupt()["requested"])
            self.assertFalse(runtime.status()["busy"])
        finally:
            release.set()
        self.wait_for(lambda: any(log.get("tool_result") for log in self.logs()))
        self.assertEqual(self.calls, [("get_task_context", {"query": "几何证据"})])
        self.assertFalse(any(event["type"] == "tool" for event in self.events))

    def test_legacy_token_tool_and_reasoning_events_are_not_mirrored(self):
        runtime = self.runtime(); runtime.start()
        sid = runtime.status()["thread_id"]
        count = len(self.events)
        for kind in ("text_delta", "tool", "reasoning"):
            runtime._event({"type": kind, "thread_id": sid, "item_id": "private-progress",
                            "text": "PRIVATE_PROGRESS", "name": "PRIVATE_TOOL"})
        self.assertEqual(len(self.events), count)

    def test_stdio_reasoning_and_raw_errors_are_dropped_and_private_token_is_redacted(self):
        runtime = self.runtime(); runtime.start(); runtime.send("private-output")
        visible = json.dumps(self.events, ensure_ascii=False)
        self.assertNotIn(runtime._token, visible)
        for private in ("SDK PRIVATE", "RAW STDERR", "DO NOT EXPOSE", "PRIVATE RAW ERROR"):
            self.assertNotIn(private, visible)
        self.assertIn("[redacted]", visible)
        self.assertTrue(runtime.status()["busy"])

    def test_callback_requires_token_and_origin_and_tools_are_whitelisted(self):
        runtime = self.runtime(); runtime.start(tools=self.tools())
        url = f"http://127.0.0.1:{runtime._server.server_port}"
        opener = build_opener(ProxyHandler({}))
        with self.assertRaises(HTTPError) as caught:
            opener.open(url + "/config")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        request = Request(url + "/config", headers={"X-DSH-Bridge-Token": runtime._token, "Origin": "http://evil.test"})
        with self.assertRaises(HTTPError) as caught:
            opener.open(request)
        self.assertEqual(caught.exception.code, 403); caught.exception.close()
        result = runtime._callback("/tool", {"name": "not-registered", "thread_id": runtime.status()["thread_id"]})
        self.assertFalse(result["success"])
        self.assertEqual(self.calls, [])

    def test_start_timeout_and_process_exit_are_bounded_safe_failures(self):
        runtime = self.runtime(); runtime.START_TIMEOUT = .2
        with self.assertRaisesRegex(DshRuntimeError, "超时"):
            runtime.start(thread_id="startup-hang")
        self.assertFalse(runtime.status()["running"])
        runtime.close()
        exited = self.runtime(); exited.start()
        with self.assertRaises(DshRuntimeError):
            exited.send("exit-now")
        self.wait_for(lambda: exited.status()["state"] == "error")
        self.assertFalse(exited.status()["running"])

    def test_close_is_idempotent_and_reaps_only_owned_child(self):
        runtime = self.runtime(); runtime.start()
        pid = runtime.status()["pid"]
        runtime.close(); runtime.close()
        self.assertEqual(runtime.status()["state"], "closed")
        self.assertIsNotNone(runtime._process.poll())
        self.assertNotEqual(pid, os.getpid())
        self.assertFalse(any(event["type"] == "error" for event in self.events))

    def test_discovery_prefers_explicit_configuration_and_status_never_launches(self):
        with patch.dict(os.environ, {"SIM_ADVISOR_DSH": str(self.script)}), patch("subprocess.Popen") as popen:
            self.assertEqual(discover_dsh_command(), [str(self.script)])
            self.assertTrue(dsh_status()["available"])
            popen.assert_not_called()
        with patch.dict(os.environ, {"SIM_ADVISOR_DSH": "definitely-missing-dsh-command"}), patch("advisor.dsh_command.shutil.which", return_value=None):
            self.assertIsNone(discover_dsh_command())

    def test_windows_shim_uses_single_createprocess_command_line_for_spaces(self):
        if os.name != "nt":
            self.skipTest("Windows shim contract")
        command = launch_command([r"C:\Program Files\DSH\dsh.cmd"], self.cwd / "host patch.yml")
        self.assertIsInstance(command, str)
        self.assertIn('/d /s /c ""C:\\Program Files\\DSH\\dsh.cmd"'.replace('\\\\', '\\'), command)
        self.assertIn('"' + str(self.cwd / "host patch.yml") + '"', command)
        with self.assertRaises(ValueError):
            launch_command([r"C:\bad&path\dsh.cmd"], self.cwd / "host.yml")

    def test_invalid_provider_and_tool_shapes_fail_before_process_creation(self):
        for provider in ("", " ", False, 1):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                self.runtime(provider=provider)
        runtime = self.runtime()
        for tools in ({}, "", [{"name": "bad", "description": "x"}]):
            with self.subTest(tools=tools), self.assertRaises(ValueError):
                runtime.start(tools=tools)
        self.assertIsNone(runtime._process)


if __name__ == "__main__":
    unittest.main()
