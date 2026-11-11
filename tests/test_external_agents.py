"""External Agent inbox lifecycle, identity isolation, and bounded events."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from advisor.external_agents import ExternalAgentBridge
from advisor.sessions import ConflictError


class Sessions:
    ids = {"a" * 32, "b" * 32}
    def get(self, sid):
        if sid not in self.ids:
            raise FileNotFoundError("Unknown plan")
        return {"id": sid}


class ExternalAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="external-agent-", dir=Path(__file__).parent)
        self.root = Path(self.temporary.name).resolve()
        self.sid, self.other = "a" * 32, "b" * 32
        self.now = 1000.0
        self.live = {101, 202}
        self.bridge = self.new_bridge()

    def tearDown(self):
        self.assertEqual(self.root.parent, Path(__file__).parent.resolve())
        self.temporary.cleanup()

    def new_bridge(self):
        return ExternalAgentBridge(self.root, Sessions(), clock=lambda: self.now, process_alive=lambda pid: pid in self.live)

    def register(self, sid=None, **metadata):
        return self.bridge.register(sid or self.sid, {"name": "External debugger", "pid": 101, **metadata})

    def test_registration_is_persistent_and_public_status_omits_token(self):
        registered = self.register(model="other-model", reasoning_effort="high", conversation_id="client-chat")
        self.assertTrue(registered["binding_id"])
        status = self.new_bridge().status(self.sid)
        self.assertEqual(status["pid"], 101)
        self.assertEqual(status["model"], "other-model")
        self.assertEqual(status["reasoning_effort"], "high")
        self.assertEqual(status["conversation_id"], "client-chat")
        self.assertEqual(status["status"], "idle")
        self.assertTrue(status["registered"])
        self.assertTrue(status["alive"])
        self.assertNotIn("binding_id", status)
        self.assertNotIn(registered["binding_id"], json.dumps(status))
        disk = json.loads((self.root / "external-agents" / (self.sid + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(disk["binding_id"], registered["binding_id"])

    def test_sid_is_checked_before_creating_record_and_pid_must_be_alive(self):
        for pid in (None, True, 0, -1, "101", 999, 0x100000000 + 101):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                self.register(pid=pid)
        with self.assertRaises(FileNotFoundError):
            self.register(sid="../escape")
        self.assertEqual(list((self.root / "external-agents").iterdir()), [])
        with self.assertRaises(ValueError):
            self.register(name="x" * 129)

    def test_live_duplicate_conflicts_and_offline_reregistration_replaces_token(self):
        old = self.register()
        first = self.bridge.enqueue(self.sid, "message", "first")
        with self.assertRaises(ConflictError):
            self.register(pid=202)
        self.now += 31
        replacement = self.register(pid=202)
        self.assertNotEqual(old["binding_id"], replacement["binding_id"])
        second = self.bridge.enqueue(self.sid, "message", "second")
        self.assertGreater(second["id"], first["id"])
        for action in (lambda: self.bridge.poll(self.sid, old["binding_id"]),
                       lambda: self.bridge.publish(self.sid, old["binding_id"], {"type": "state", "status": "idle"}),
                       lambda: self.bridge.unregister(self.sid, old["binding_id"])):
            with self.assertRaises(PermissionError):
                action()
        polled = self.bridge.poll(self.sid, replacement["binding_id"])
        self.assertEqual(polled["commands"], [second])
        self.assertTrue(polled["connected"])

    def test_poll_updates_heartbeat_and_after_cursor_consumes_only_seen_commands(self):
        token = self.register()["binding_id"]
        first = self.bridge.enqueue(self.sid, "message", "研究模型")
        second = self.bridge.enqueue(self.sid, "interrupt")
        self.now += 31
        self.assertEqual(self.bridge.status(self.sid)["offline_reason"], "heartbeat_timeout")
        result = self.bridge.poll(self.sid, token, after=first["id"])
        self.assertEqual(result["commands"], [second])
        self.assertEqual(result["cursor"], second["id"])
        self.assertTrue(result["connected"])
        self.assertEqual(self.bridge.status(self.sid)["last_seen"], self.now)
        self.assertEqual(self.bridge.poll(self.sid, token, after=second["id"])["commands"], [])
        for after in (True, -1, 1.5, "0", second["id"] + 1):
            with self.subTest(after=after), self.assertRaises(ValueError):
                self.bridge.poll(self.sid, token, after=after)

    def test_status_and_server_enqueue_do_not_fabricate_heartbeat(self):
        self.register()
        self.now += 20
        self.bridge.enqueue(self.sid, "message", "new input")
        self.assertEqual(self.bridge.status(self.sid)["last_seen"], 1000.0)
        self.now += 11
        self.assertFalse(self.bridge.status(self.sid)["alive"])
        with self.assertRaises(ConflictError):
            self.bridge.enqueue(self.sid, "message", "offline")

    def test_process_exit_is_independent_of_heartbeat_and_replacement_allowed(self):
        token = self.register()["binding_id"]
        self.live.remove(101)
        self.assertEqual(self.bridge.status(self.sid)["offline_reason"], "process_exited")
        self.assertFalse(self.bridge.poll(self.sid, token)["connected"])
        self.assertIsNone(self.bridge.publish(self.sid, token, {"type": "message", "message_id": "m", "text": "not alive"}))
        replacement = self.register(pid=202)
        self.assertNotEqual(token, replacement["binding_id"])

    def test_disconnect_remains_detached_during_poll_and_is_idempotent(self):
        self.assertFalse(self.bridge.disconnect(self.sid)["registered"])
        token = self.register()["binding_id"]
        self.bridge.enqueue(self.sid, "message", "pending")
        self.assertFalse(self.bridge.disconnect(self.sid)["alive"])
        first = self.bridge.poll(self.sid, token)
        self.assertFalse(first["connected"])
        self.assertEqual([c["type"] for c in first["commands"]], ["message", "disconnect"])
        self.bridge.disconnect(self.sid)
        self.assertEqual(self.bridge.poll(self.sid, token)["cursor"], first["cursor"])
        self.now += 1
        self.assertIsNone(self.bridge.publish(self.sid, token, {"type": "state", "status": "running"}))
        self.assertFalse(self.bridge.status(self.sid)["alive"])
        self.assertFalse(self.bridge.status(self.sid)["registered"])

    def test_unregister_rejects_wrong_tokens_and_never_revives(self):
        token = self.register()["binding_id"]
        for invalid in (None, "wrong", "非ASCII凭据"):
            with self.subTest(token=invalid), self.assertRaises(PermissionError):
                self.bridge.unregister(self.sid, invalid)
        self.assertEqual(self.bridge.unregister(self.sid, token)["offline_reason"], "unregistered")
        self.assertFalse(self.bridge.poll(self.sid, token)["connected"])

    def test_state_is_authoritative_and_messages_are_scoped_without_token(self):
        token = self.register()["binding_id"]
        running = self.bridge.publish(self.sid, token, {"type": "state", "status": "running", "turn_id": "turn-1"})
        self.assertEqual(running, {"type": "turn", "event": "started", "status": "inProgress", "turn_id": "turn-1"})
        delta = self.bridge.publish(self.sid, token, {"type": "delta", "message_id": "m1", "text": "幾何"})
        whitespace = self.bridge.publish(self.sid, token, {"type": "delta", "message_id": "m1", "text": "\n"})
        message = self.bridge.publish(self.sid, token, {"type": "message", "message_id": "m1", "text": "幾何觀察"})
        self.assertEqual(delta["type"], "text_delta")
        self.assertEqual(delta["item_id"], message["item_id"])
        self.assertEqual(whitespace["text"], "\n")
        self.assertNotIn(token, json.dumps(message))
        self.assertEqual(self.bridge.status(self.sid)["status"], "running")
        idle = self.bridge.publish(self.sid, token, {"type": "state", "status": "idle"})
        self.assertEqual(idle["event"], "completed")
        self.assertEqual(idle["turn_id"], "turn-1")
        self.assertEqual(self.bridge.status(self.sid)["status"], "idle")
        self.assertIsNone(self.bridge.status(self.sid)["turn_id"])
        self.bridge.disconnect(self.sid)
        next_token = self.register()["binding_id"]
        new_message = self.bridge.publish(self.sid, next_token, {"type": "message", "message_id": "m1", "text": "new binding"})
        self.assertNotEqual(new_message["item_id"], message["item_id"])
        self.assertNotIn(next_token, json.dumps(new_message))

    def test_activity_errors_and_event_heartbeat_have_bounded_visible_fields(self):
        token = self.register()["binding_id"]
        self.now += 31
        event = self.bridge.publish(self.sid, token, {"type": "activity", "text": "正在阅读证据", "raw": {"secret": "IGNORED"}})
        self.assertEqual(event["type"], "tool")
        self.assertEqual(event["name"], "正在阅读证据")
        self.assertNotIn("IGNORED", json.dumps(event))
        self.assertTrue(self.bridge.status(self.sid)["alive"])
        self.assertEqual(self.bridge.status(self.sid)["activity"], "正在阅读证据")
        error = self.bridge.publish(self.sid, token, {"type": "error", "text": "需要补充输入"})
        self.assertEqual(error["message"], "需要补充输入")
        self.assertFalse(error["will_retry"])
        for invalid in ({"type": "delta", "text": "no identity"}, {"type": "message", "message_id": "m", "text": "x" * 16001},
                        {"type": "activity", "text": "x" * 1001}, {"type": "error", "text": "x" * 2001},
                        {"type": "state", "status": "completed"}, {"type": "reasoning", "text": "private"}):
            with self.subTest(event_type=invalid["type"]), self.assertRaises(ValueError):
                self.bridge.publish(self.sid, token, invalid)

    def test_inbox_bound_does_not_block_disconnect_and_poll_frees_consumed_entries(self):
        token = self.register()["binding_id"]
        self.bridge.MAX_COMMANDS = 2
        first = self.bridge.enqueue(self.sid, "message", "one")
        self.bridge.enqueue(self.sid, "message", "two")
        with self.assertRaises(ConflictError):
            self.bridge.enqueue(self.sid, "message", "full")
        self.bridge.poll(self.sid, token, after=first["id"])
        self.bridge.enqueue(self.sid, "message", "three")
        self.bridge.disconnect(self.sid)
        self.assertEqual(self.bridge.poll(self.sid, token)["commands"][-1]["type"], "disconnect")

    def test_two_plans_do_not_share_commands_tokens_or_message_scope(self):
        first = self.register()
        second = self.register(self.other)
        with self.assertRaises(PermissionError):
            self.bridge.poll(self.other, first["binding_id"])
        self.bridge.enqueue(self.sid, "message", "first plan only")
        self.assertEqual(self.bridge.poll(self.other, second["binding_id"])["commands"], [])
        self.assertTrue(self.bridge.status(self.other)["alive"])

    def test_concurrent_registration_keeps_exactly_one_live_binding(self):
        results = []
        def register():
            try:
                results.append(self.register())
            except ConflictError:
                results.append("conflict")
        threads = [threading.Thread(target=register) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(results.count("conflict"), 1)

    def test_default_native_pid_helper_can_observe_current_process_without_killing(self):
        bridge = ExternalAgentBridge(self.root, Sessions())
        registered = bridge.register(self.sid, {"name": "This test process", "pid": os.getpid()})
        self.assertTrue(bridge.status(self.sid)["alive"])
        bridge.unregister(self.sid, registered["binding_id"])
        self.assertEqual(os.getpid(), registered["pid"])

    def test_planning_authority_uses_live_binding_metadata_without_heartbeat_or_token(self):
        registered=self.register(backend="dsh",model="gpt-6.1-sol",reasoning_effort="xhigh",conversation_id="dsh-conversation")
        token=registered["binding_id"]
        expected={"backend":"dsh","model":"gpt-6.1-sol","reasoning_effort":"xhigh","conversation_id":"dsh-conversation"}
        self.assertEqual(self.bridge.planning_provenance(self.sid,token),expected)
        self.assertEqual(self.bridge.status(self.sid)["backend"],"dsh")
        self.now+=31
        with self.assertRaises(PermissionError):
            self.bridge.planning_provenance(self.sid,token)
        self.assertEqual(self.bridge.status(self.sid)["last_seen"],1000.0)
        self.bridge.poll(self.sid,token)
        self.assertEqual(self.bridge.planning_provenance(self.sid,token),expected)
        self.live.remove(101)
        with self.assertRaises(PermissionError):
            self.bridge.planning_provenance(self.sid,token)

    def test_backend_defaults_and_invalid_values(self):
        for backend in (None,"", " ",1,"x"*129):
            with self.subTest(backend=backend),self.assertRaises(ValueError):
                self.register(backend=backend)
        registered=self.register()
        self.assertEqual(registered["backend"],"external-agent")
        self.assertEqual(self.bridge.planning_provenance(self.sid,registered["binding_id"])["backend"],"external-agent")


if __name__ == "__main__":
    unittest.main()
