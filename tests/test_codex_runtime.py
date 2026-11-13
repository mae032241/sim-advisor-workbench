"""Persistent stdio protocol tests using a local fake server, never a model."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from advisor.codex_runtime import CodexRuntime, CodexRuntimeError


FAKE_SERVER = r'''
import json,sys
from pathlib import Path
log=Path(sys.argv[1]);thread_id='mock-thread';turn_id=None;counter=0
def write(packet):
 sys.stdout.buffer.write((json.dumps(packet,ensure_ascii=False)+'\n').encode('utf-8'));sys.stdout.buffer.flush()
def note(method,params):write({'method':method,'params':dict(threadId=thread_id,**params)})
def answer(packet,result):write({'id':packet['id'],'result':result})
def completed(status='completed'):
 note('turn/completed',{'turn':{'id':turn_id,'status':status,'items':[]}})
for line in sys.stdin.buffer:
 packet=json.loads(line)
 with log.open('a',encoding='utf-8') as stream:stream.write(json.dumps(packet,ensure_ascii=False)+'\n')
 method=packet.get('method');p=packet.get('params',{})
 if method=='initialize':
  sys.stderr.write('RAW STDERR SECRET should never reach events\n');sys.stderr.flush();answer(packet,{})
 elif method in ('thread/start','thread/resume'):
  thread_id=p.get('threadId','mock-thread')
  answer(packet,{'thread':{'id':thread_id,'status':{'type':'idle'},'turns':[]},'model':p['model'],'sandbox':{'type':'workspaceWrite','networkAccess':False}})
 elif method=='turn/start':
  text=p['input'][0]['text']
  if text=='hang':continue
  if text=='exit':sys.exit(2)
  if text=='rpc_error':
   write({'id':packet['id'],'error':{'code':-32001,'message':'SECRET RAW ERROR Bearer private-secret'}});continue
  counter+=1;turn_id='turn-'+str(counter)
  note('turn/started',{'turn':{'id':turn_id,'status':'inProgress','items':[]}})
  if text=='complete_before_ack':
   note('item/reasoning/textDelta',{'delta':'PRIVATE REASONING','turnId':turn_id,'itemId':'reason-1'})
   note('item/agentMessage/delta',{'delta':'幾何觀察。','turnId':turn_id,'itemId':'msg-1'})
   note('item/completed',{'item':{'type':'agentMessage','id':'msg-1','text':'幾何觀察。','phase':'partial_answer'},'turnId':turn_id})
   completed()
  answer(packet,{'turn':{'id':turn_id,'status':'inProgress','items':[]}})
  if text=='tool':
   note('item/started',{'turnId':turn_id,'item':{'type':'dynamicToolCall','id':'call-77','tool':'lookup','status':'inProgress','arguments':{'q':'私有參數'}}})
   write({'id':'request-77','method':'item/tool/call','params':{'threadId':thread_id,'turnId':turn_id,'callId':'call-77','tool':'lookup','arguments':{'q':'私有參數'}}})
  elif text=='unregistered_tool':
   write({'id':99,'method':'item/tool/call','params':{'threadId':thread_id,'turnId':turn_id,'callId':'unknown-call','tool':'not_registered','arguments':{'secret':'DO NOT EXPOSE'}}})
  elif text=='wrong_thread_tool':
   write({'id':98,'method':'item/tool/call','params':{'threadId':'wrong-thread','turnId':turn_id,'callId':'bad-thread','tool':'lookup','arguments':{}}})
  elif text=='approval':
   write({'id':'approval-1','method':'item/commandExecution/requestApproval','params':{'threadId':thread_id,'turnId':turn_id,'command':'SECRET COMMAND'}})
  elif text=='secret':
   note('item/agentMessage/delta',{'delta':'api_key="sk-synthetic-secret12345" Bearer private-token-value TEST_PRIVATE_TOKEN_12345','turnId':turn_id,'itemId':'msg-secret'})
   note('item/completed',{'turnId':turn_id,'item':{'id':'msg-secret','type':'agentMessage','phase':'commentary','text':'api_key="sk-synthetic-secret12345" Bearer private-token-value TEST_PRIVATE_TOKEN_12345'}})
   note('item/completed',{'turnId':turn_id,'item':{'id':'reason-1','type':'reasoning','text':'HIDDEN REASONING'}})
   note('error',{'turnId':turn_id,'error':{'message':'SECRET ERROR api_key=private'},'willRetry':True})
   write({'method':'item/agentMessage/delta','params':{'threadId':'unrelated','turnId':turn_id,'itemId':'other','delta':'OTHER THREAD SECRET'}})
  elif text=='retry_then_complete':
   note('error',{'turnId':turn_id,'error':{'message':'SECRET ERROR Bearer private-value','additionalDetails':'SECRET DETAILS','codexErrorInfo':{'httpConnectionFailed':{'httpStatusCode':503}}},'willRetry':True})
   completed()
  elif text=='failed_without_error':completed('failed')
  elif text=='malformed':
   sys.stdout.buffer.write(b'{not-json}\n');sys.stdout.buffer.flush()
  elif text=='overflow':
   sys.stdout.buffer.write(b'x'*8192+b'\n');sys.stdout.buffer.flush()
 elif method=='turn/steer':
  answer(packet,{'turnId':turn_id})
  if p['input'][0]['text']=='complete':completed()
 elif method=='turn/interrupt':
  answer(packet,{});completed('interrupted')
 elif 'id' in packet and method is None:
  if packet['id']=='request-77':
   result=packet.get('result',{})
   note('item/completed',{'turnId':turn_id,'item':{'type':'dynamicToolCall','id':'call-77','tool':'lookup','status':'completed','success':result.get('success'),'contentItems':result.get('contentItems')}})
   completed()
  elif packet['id'] in (99,98,'approval-1'):completed()
'''


class CodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-runtime-", dir=Path(__file__).parent)
        self.root = Path(self.temporary.name).resolve()
        self.script = self.root / "fake_server.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.log = self.root / "packets.jsonl"
        self.events, self.calls, self.runtimes = [], [], []

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.close()
        self.assertEqual(self.root.parent, Path(__file__).parent.resolve())
        self.temporary.cleanup()

    def runtime(self, callback=None, listener=None, *, reasoning_effort="xhigh"):
        def default_tool(name, args):
            self.calls.append((name, args))
            return {"observed": ["實際來源"], "status": "observed"}
        runtime = CodexRuntime([sys.executable, str(self.script), str(self.log)], self.root,
                               listener or self.events.append, callback or default_tool,
                               reasoning_effort=reasoning_effort)
        runtime.REQUEST_TIMEOUT = 2
        self.runtimes.append(runtime)
        return runtime

    @staticmethod
    def tools():
        return [{"name": "lookup", "description": "Read bounded evidence", "inputSchema": {"type": "object"}}]

    def packets(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def wait_for(self, predicate):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("mock protocol condition did not complete")

    def test_start_steer_complete_and_new_turn_share_one_process(self):
        runtime = self.runtime()
        started = runtime.start(tools=self.tools(), instructions="只寫方案；不求解。")
        self.assertEqual(started["thread_id"], "mock-thread")
        self.assertTrue(runtime.status()["running"])
        self.assertFalse(runtime.status()["busy"])
        self.assertEqual(runtime.status()["reasoning_effort"], "xhigh")
        first = runtime.send("first")
        self.assertEqual(first["mode"], "start")
        self.assertTrue(runtime.status()["busy"])
        second = runtime.send("complete")
        self.assertEqual(second, {"thread_id": "mock-thread", "turn_id": "turn-1", "mode": "steer"})
        self.wait_for(lambda: not runtime.status()["busy"])
        third = runtime.send("next")
        self.assertEqual(third["mode"], "start")
        self.assertEqual(third["turn_id"], "turn-2")
        self.assertEqual(runtime.status()["pid"], started["pid"])
        packets = self.packets()
        self.assertEqual([p.get("method") for p in packets[:3]], ["initialize", "initialized", "thread/start"])
        params = packets[2]["params"]
        self.assertTrue(packets[0]["params"]["capabilities"]["experimentalApi"])
        self.assertEqual(params["sandbox"], "workspace-write")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["cwd"], str(self.root))
        self.assertFalse(params["allowProviderModelFallback"])
        self.assertEqual(params["dynamicTools"][0]["type"], "function")
        turn_start = next(p for p in packets if p.get("method") == "turn/start")
        self.assertEqual(turn_start["params"]["model"], "gpt-6.1-sol")
        self.assertEqual(turn_start["params"]["effort"], "xhigh")
        self.assertFalse(turn_start["params"]["sandboxPolicy"]["networkAccess"])
        self.assertEqual(turn_start["params"]["sandboxPolicy"]["writableRoots"], [str(self.root)])
        steer = next(p for p in packets if p.get("method") == "turn/steer")
        self.assertEqual(steer["params"]["expectedTurnId"], first["turn_id"])
        self.assertNotIn("jsonrpc", packets[0])

    def test_selected_effort_passes_unchanged_to_start_resume_and_turn(self):
        for effort in ("low", "medium", "high", "xhigh"):
            for resumed in (False, True):
                with self.subTest(effort=effort, resumed=resumed):
                    runtime = self.runtime(reasoning_effort=effort)
                    runtime.start(thread_id="resumed-thread" if resumed else None)
                    runtime.send("first")
                    self.assertEqual(runtime.status()["reasoning_effort"], effort)
                    packets = self.packets()
                    method = "thread/resume" if resumed else "thread/start"
                    thread = next(p for p in reversed(packets) if p.get("method") == method)
                    turn = next(p for p in reversed(packets) if p.get("method") == "turn/start")
                    self.assertEqual(thread["params"]["config"]["model_reasoning_effort"], effort)
                    self.assertEqual(turn["params"]["effort"], effort)
                    runtime.close()

    def test_effort_requires_a_nonempty_string_before_starting_process(self):
        for invalid in (None, "", "  ", 1, True, [], {}):
            with self.subTest(effort=invalid), self.assertRaisesRegex(ValueError, "reasoning_effort"):
                self.runtime(reasoning_effort=invalid)
        self.assertFalse(self.log.exists())

    def test_completed_before_start_ack_does_not_resurrect_busy_state(self):
        runtime = self.runtime()
        runtime.start()
        result = runtime.send("complete_before_ack")
        self.assertEqual(result["turn_id"], "turn-1")
        self.assertEqual(runtime.status()["state"], "idle")
        self.assertIsNone(runtime.status()["turn_id"])
        self.assertEqual([e["text"] for e in self.events if e["type"] == "text_delta"], ["幾何觀察。"])
        self.assertEqual([e["text"] for e in self.events if e["type"] == "message"], ["幾何觀察。"])
        self.assertNotIn("PRIVATE REASONING", json.dumps(self.events))
        self.assertEqual([e["event"] for e in self.events if e["type"] == "turn"], ["started", "completed"])

    def test_resume_preserves_thread_and_does_not_reregister_tools(self):
        runtime = self.runtime()
        started = runtime.start(thread_id="existing-thread", tools=self.tools(), instructions="Resume bounded work")
        self.assertEqual(started["thread_id"], "existing-thread")
        packet = self.packets()[2]
        self.assertEqual(packet["method"], "thread/resume")
        self.assertTrue(packet["params"]["excludeTurns"])
        self.assertNotIn("dynamicTools", packet["params"])
        self.assertNotIn("history", packet["params"])
        self.assertNotIn("path", packet["params"])
        self.assertEqual(runtime.start(thread_id="existing-thread"), started)

    def test_dynamic_tool_response_uses_rpc_id_and_hides_arguments_and_results(self):
        runtime = self.runtime()
        runtime.start(tools=self.tools())
        runtime.send("tool")
        self.wait_for(lambda: any(e["type"] == "turn" and e["event"] == "completed" for e in self.events))
        self.assertEqual(self.calls, [("lookup", {"q": "私有參數"})])
        response = next(p for p in self.packets() if p.get("id") == "request-77" and "method" not in p)
        self.assertTrue(response["result"]["success"])
        self.assertEqual(response["result"]["contentItems"][0]["type"], "inputText")
        self.assertIn("實際來源", response["result"]["contentItems"][0]["text"])
        visible = json.dumps(self.events, ensure_ascii=False)
        self.assertNotIn("私有參數", visible)
        self.assertNotIn("實際來源", visible)
        self.assertEqual([e["event"] for e in self.events if e["type"] == "tool"], ["started", "completed"])

    def test_blocking_tool_does_not_block_interrupt_or_reader(self):
        entered, release = threading.Event(), threading.Event()
        def tool(name, args):
            entered.set()
            release.wait(3)
            return "done"
        runtime = self.runtime(callback=tool)
        runtime.start(tools=self.tools())
        runtime.send("tool")
        self.assertTrue(entered.wait(2))
        try:
            self.assertTrue(runtime.interrupt()["requested"])
            self.wait_for(lambda: any(e.get("status") == "interrupted" for e in self.events))
            self.assertFalse(runtime.status()["busy"])
        finally:
            release.set()

    def test_unknown_tool_wrong_thread_and_approval_never_execute_callback(self):
        for text, rpc_id in (("unregistered_tool", 99), ("wrong_thread_tool", 98), ("approval", "approval-1")):
            with self.subTest(text=text):
                self.events.clear()
                runtime = self.runtime()
                runtime.start(tools=self.tools())
                runtime.send(text)
                self.wait_for(lambda: any(e["type"] == "turn" and e["event"] == "completed" for e in self.events))
                packet = next(p for p in reversed(self.packets()) if p.get("id") == rpc_id and "method" not in p)
                if text == "approval":
                    self.assertEqual(packet["error"]["code"], -32601)
                else:
                    self.assertFalse(packet["result"]["success"])
                runtime.close()
        self.assertEqual(self.calls, [])
        self.assertNotIn("SECRET COMMAND", json.dumps(self.events))

    def test_tool_exceptions_and_oversize_results_return_safe_failure(self):
        def broken(name, args):
            raise RuntimeError("SECRET TOOL TRACE")
        for callback in (broken, lambda name, args: "x" * 2000):
            with self.subTest(callback=callback):
                self.events.clear()
                runtime = self.runtime(callback=callback)
                runtime.MAX_TOOL_BYTES = 256
                runtime.start(tools=self.tools())
                runtime.send("tool")
                self.wait_for(lambda: any(e["type"] == "turn" and e["event"] == "completed" for e in self.events))
                response = next(p for p in reversed(self.packets()) if p.get("id") == "request-77" and "method" not in p)
                self.assertFalse(response["result"]["success"])
                self.assertNotIn("SECRET TOOL TRACE", json.dumps(response))
                runtime.close()

    def test_redaction_reasoning_stderr_other_threads_and_retrying_error(self):
        with patch.dict("os.environ", {"TEST_PRIVATE_TOKEN": "TEST_PRIVATE_TOKEN_12345"}):
            runtime = self.runtime()
        runtime.start()
        runtime.send("secret")
        self.wait_for(lambda: any(e["type"] == "error" for e in self.events))
        visible = json.dumps(self.events)
        for secret in ("sk-synthetic-secret12345", "private-token-value", "TEST_PRIVATE_TOKEN_12345", "HIDDEN REASONING", "OTHER THREAD SECRET", "RAW STDERR SECRET", "SECRET ERROR"):
            self.assertNotIn(secret, visible)
        self.assertIn("[redacted]", visible)
        self.assertTrue(runtime.status()["busy"])
        self.assertTrue(next(e for e in self.events if e["type"] == "error")["will_retry"])

    def test_rpc_error_is_safe_and_does_not_pretend_turn_was_started(self):
        runtime = self.runtime()
        runtime.start()
        with self.assertRaisesRegex(CodexRuntimeError, "code -32001") as caught:
            runtime.send("rpc_error")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertFalse(runtime.status()["busy"])
        self.assertTrue(runtime.status()["running"])

    def test_retrying_error_completes_same_turn_and_exposes_only_safe_metadata(self):
        runtime = self.runtime()
        runtime.start()
        runtime.send("retry_then_complete")
        self.wait_for(lambda: not runtime.status()["busy"])
        state = runtime.status()
        self.assertTrue(state["turn_outcome_supported"])
        self.assertEqual(state["last_turn"], {"turn_id": "turn-1", "status": "completed"})
        self.assertIsNone(state["error_metadata"])
        error = next(event for event in self.events if event["type"] == "error")
        self.assertEqual({key: error[key] for key in ("category", "will_retry", "http_status", "fatal")},
                         {"category": "httpConnectionFailed", "will_retry": True, "http_status": 503, "fatal": False})
        self.assertNotIn("SECRET", json.dumps(self.events))
        self.assertNotIn("private-value", json.dumps(self.events))
        methods = [packet.get("method") for packet in self.packets()]
        self.assertEqual(methods.count("turn/start"), 1)
        self.assertNotIn("turn/steer", methods)
        self.assertNotIn("turn/interrupt", methods)

    def test_failed_outcome_without_error_and_prior_turn_notifications_do_not_pollute_next_turn(self):
        runtime = self.runtime()
        runtime.start()
        runtime.send("failed_without_error")
        self.wait_for(lambda: not runtime.status()["busy"])
        self.assertEqual(runtime.status()["last_turn"], {"turn_id": "turn-1", "status": "failed"})
        runtime.send("next")
        self.assertIsNone(runtime.status()["last_turn"])
        self.assertIsNone(runtime.status()["error_metadata"])
        runtime._notification("error", {"threadId": "mock-thread", "turnId": "turn-1", "error": {}, "willRetry": False})
        runtime._notification("turn/completed", {"threadId": "mock-thread", "turn": {"id": "turn-1", "status": "failed"}})
        self.assertTrue(runtime.status()["busy"])
        self.assertEqual(runtime.status()["turn_id"], "turn-2")
        self.assertIsNone(runtime.status()["last_turn"])
        self.assertIsNone(runtime.status()["error_metadata"])
        # Missing or malformed retryability is unknown, never inferred true.
        runtime._notification("error", {"threadId": "mock-thread", "turnId": "turn-2",
                                        "willRetry": "yes", "error": {"codexErrorInfo": "SECRET CATEGORY"}})
        self.assertEqual(runtime.status()["error_metadata"]["category"], "unknown")
        self.assertIsNone(runtime.status()["error_metadata"]["will_retry"])
        self.assertNotIn("SECRET CATEGORY", json.dumps(self.events))

    def test_secret_split_across_delta_boundaries_never_streams(self):
        runtime = self.runtime()
        runtime.start()
        chunks = ["正常可見的進度。" * 10 + " s", "k-synthetic-", "private-key12345 with more details"]
        for chunk in chunks:
            runtime._notification("item/agentMessage/delta", {"threadId": "mock-thread", "turnId": "turn-1", "itemId": "sensitive", "delta": chunk})
        runtime._notification("item/completed", {"threadId": "mock-thread", "turnId": "turn-1", "item": {"id": "sensitive", "type": "agentMessage", "phase": "final_answer", "text": "".join(chunks)}})
        visible = json.dumps(self.events, ensure_ascii=False)
        self.assertNotIn("synthetic", visible)
        self.assertNotIn("private-key12345", visible)
        self.assertIn("[redacted]", visible)
        self.assertIn("正常可見的進度", visible)

    def test_eof_malformed_oversize_and_timeout_fail_without_raw_content(self):
        for text in ("exit", "malformed", "overflow", "hang"):
            with self.subTest(text=text):
                self.events.clear()
                runtime = self.runtime()
                runtime.start()
                runtime.REQUEST_TIMEOUT = 0.2
                if text == "overflow":
                    runtime.MAX_MESSAGE_BYTES = 2048
                try:
                    runtime.send(text)
                except CodexRuntimeError:
                    pass
                self.wait_for(lambda: runtime.status()["state"] == "error")
                self.assertFalse(runtime.status()["running"])
                self.assertFalse(runtime.status()["busy"])
                runtime.close()
                self.assertIsNotNone(runtime._process.poll())

    def test_close_is_idempotent_and_callback_failure_does_not_break_reader(self):
        def broken_listener(event):
            raise ValueError("listener unavailable")
        runtime = self.runtime(listener=broken_listener)
        runtime.start()
        runtime.send("complete_before_ack")
        self.assertFalse(runtime.status()["busy"])
        self.assertFalse(runtime.interrupt()["requested"])
        runtime.close()
        runtime.close()
        self.assertEqual(runtime.status()["state"], "closed")
        self.assertFalse(runtime.status()["running"])
        with self.assertRaises(CodexRuntimeError):
            runtime.send("after close")


if __name__ == "__main__":
    unittest.main()
