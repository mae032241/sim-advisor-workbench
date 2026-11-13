import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from advisor.agent_sessions import AgentSessions, runtime_command
from advisor.planner import PLAN_SCHEMA, baseline_plan
from advisor.sessions import ConflictError, SessionStore
from test_workflow import FakeKB, FakePlanner, GOAL, SOURCE, TEST_TEMP


class FakeRuntime:
    starts = []

    def __init__(self, **kwargs):
        self.kwargs, self.running, self.busy = kwargs, False, False
        self.thread_id = None
        self.sent = []

    def start(self, thread_id=None, tools=None, instructions=""):
        self.thread_id, self.running = thread_id or "codex-test-thread", True
        self.starts.append(thread_id)
        return {"thread_id": self.thread_id, "pid": 12345}

    def status(self):
        return {"running": self.running, "busy": self.busy, "pid": 12345,
                "thread_id": self.thread_id, "turn_id": "turn-test" if self.busy else None}

    def send(self, text):
        mode = "steer" if self.busy else "start"
        self.sent.append((text, mode))
        self.busy = True
        return {"mode": mode, "thread_id": self.thread_id, "turn_id": "turn-test"}

    def interrupt(self):
        self.busy = False

    def close(self):
        self.running = False


def payload(value):
    plan = baseline_plan(value["goal"], None, {}, [SOURCE])
    return {"revision": value["revision"], "plan": {k: plan[k] for k in PLAN_SCHEMA["properties"]},
        "design_brief": {"objective": "检查目标热变形", "domain_strategy": "保留承载固体域",
            "candidate_routes": [{"id": "thermal", "name": "顺序热结构", "physics": ["heat", "solid"],
                "rationale": "热量产生温升与膨胀", "assumptions": [], "required_inputs": [],
                "tradeoffs": "先核对弱耦合", "status": "recommended"}], "decisions": [],
            "geometry_interpretation": [], "simplification": {"objective": "保留热路径与承载路径", "protected_features": [],
                "candidates": [], "unresolved_geometry": []}},
        "questions": [], "retired_questions": [], "evidence_gaps": []}


class AgentSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP)
        self.root = Path(self.temp.name)
        self.kb = FakeKB()
        self.sessions = SessionStore(self.root, self.kb, FakePlanner())
        self.plan = self.sessions.create(GOAL, None)
        self.sid = self.plan["id"]
        self.agent = AgentSessions(self.root, self.sessions, self.kb, runtime_factory=FakeRuntime)

    def tearDown(self):
        self.agent.close()
        self.temp.cleanup()

    def read(self):
        return self.agent.call_tool(self.sid, "read_source", {"id": SOURCE["id"], "offset": 0, "length": 500})

    def test_restore_version_rejects_connected_managed_agent_and_requires_exact_current_revision(self):
        value = self.sessions.get(self.sid)
        current = self.sessions.save_event(value, "fixture", "A second revision")
        self.agent.connect(self.sid)
        with self.assertRaises(ConflictError):
            self.agent.restore_version(self.sid, 1, current["revision"], confirmed=True)
        self.agent.disconnect(self.sid)
        with self.assertRaises(ConflictError):
            self.agent.restore_version(self.sid, 1, 1, confirmed=True)
        restored = self.agent.restore_version(self.sid, 1, current["revision"], confirmed=True)
        self.assertEqual(restored["revision"], current["revision"] + 1)
        self.assertEqual(restored["id"], self.sid)
        self.assertIsNone(self.agent.entries[self.sid]["runtime"])

    def test_restore_version_rejects_even_expired_external_binding_until_explicit_disconnect(self):
        value = self.sessions.save_event(self.sessions.get(self.sid), "fixture", "A second revision")
        self.agent.register_external(self.sid, {"name": "Fixture", "pid": os.getpid()})
        self.agent.external.clock = lambda: 10**12
        self.assertFalse(self.agent.external.status(self.sid)["alive"])
        with self.assertRaises(ConflictError):
            self.agent.restore_version(self.sid, 1, value["revision"], confirmed=True)
        self.agent.disconnect(self.sid)
        restored = self.agent.restore_version(self.sid, 1, value["revision"], confirmed=True)
        self.assertEqual(restored["revision"], value["revision"] + 1)
        self.assertFalse(self.agent.external.status(self.sid)["registered"])

    def test_one_thread_per_plan_and_busy_messages_steer(self):
        self.agent.message(self.sid, "先研究物理路线")
        entry = self.agent.entries[self.sid]
        runtime = entry["runtime"]
        self.agent.message(self.sid, "优先检查网格质量")
        self.assertIs(entry["runtime"], runtime)
        self.assertEqual([m[1] for m in runtime.sent], ["start", "steer"])
        other = self.sessions.create("另一台设备热传导问题", None)
        self.agent.connect(other["id"])
        self.assertIsNot(self.agent.entries[other["id"]]["runtime"], runtime)
        self.agent.disconnect(self.sid)
        state = self.agent.connect(self.sid)
        self.assertEqual(state["thread_id"], "codex-test-thread")
        self.assertEqual(self.agent.entries[self.sid]["runtime"].starts[-1], "codex-test-thread")

    def test_cannot_cite_unread_source_and_search_is_not_reading(self):
        args = payload(self.plan)
        self.agent.call_tool(self.sid, "search_knowledge", {"query": "thermal expansion", "kind": "manual", "mode": "native"})
        with self.assertRaises(ValueError):
            self.agent.call_tool(self.sid, "save_plan", args)
        self.assertEqual(self.sessions.get(self.sid)["revision"], 1)
        self.read()
        saved = self.agent.call_tool(self.sid, "save_plan", args)
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(self.sessions.get(self.sid)["plan"]["engine"].split(" / ")[0], "custom")

    def test_patch_retains_other_steps_and_blockers_and_invalidates_confirmation(self):
        self.read()
        args = payload(self.plan)
        args["evidence_gaps"] = ["尚未查到温度映射正文"]
        self.agent.call_tool(self.sid, "save_plan", args)
        previous = self.sessions.get(self.sid)
        step = copy.deepcopy(previous["plan"]["steps"][3])
        step["action"] += " 检查最差质量及其位置，并修复扭曲单元。"
        result = self.agent.call_tool(self.sid, "update_steps", {"revision": 2, "steps": [step]})
        current = self.sessions.get(self.sid)
        self.assertTrue(result["planning_incomplete"])
        self.assertEqual(current["evidence_gaps"], args["evidence_gaps"])
        self.assertEqual(current["plan"]["steps"][:3], previous["plan"]["steps"][:3])
        self.assertEqual(current["plan"]["steps"][3], step)
        with self.assertRaises(ValueError):
            self.sessions.confirm(self.sid, 3)
        with self.assertRaises(ConflictError):
            self.agent.call_tool(self.sid, "update_steps", {"revision": 2, "steps": [step]})

    def test_new_inputs_are_observed_and_cannot_silently_clear_stale_gate(self):
        planned = self.sessions.refine(self.sid, 1)
        changed = self.sessions.answers(self.sid, planned["revision"], {"outputs": "最大位移低于0.1毫米"})
        self.assertTrue(changed["plan_stale"])
        read = self.agent.call_tool(self.sid, "get_plan", {})
        self.assertEqual(read["answers"]["outputs"], "最大位移低于0.1毫米")
        step = changed["plan"]["steps"][0]
        self.agent.call_tool(self.sid, "update_steps", {"revision": changed["revision"], "steps": [step]})
        self.assertTrue(self.sessions.get(self.sid)["plan_stale"])

    def test_transcript_survives_new_manager_and_deltas_become_one_message(self):
        self.agent.message(self.sid, "先检查模型")
        for kind, text in [("text_delta", "已读取"), ("text_delta", "几何"), ("message", "已读取几何")]:
            self.agent._event(self.sid, {"type": kind, "text": text, "item_id": "m1"})
        self.assertEqual(len(self.agent.status(self.sid)["messages"]), 2)
        self.agent.close()
        self.agent = AgentSessions(self.root, self.sessions, self.kb, runtime_factory=FakeRuntime)
        state = self.agent.status(self.sid)
        self.assertEqual(state["messages"][-1]["text"], "已读取几何")
        self.assertFalse(state["connected"])
        self.agent.connect(self.sid)
        self.assertEqual(self.agent.status(self.sid)["thread_id"], "codex-test-thread")

    def test_nonterminal_error_is_warning_and_success_clears_it_but_retains_outcome(self):
        self.agent.message(self.sid, "先检查模型")
        entry = self.agent.entries[self.sid]
        self.agent._event(self.sid, {"type": "error", "message": "Codex reported an error.",
            "code": "codex_error", "category": "httpConnectionFailed", "will_retry": True,
            "fatal": False, "turn_id": "turn-test"})
        state = self.agent.status(self.sid)
        self.assertEqual(state["status"], "running")
        self.assertIsNone(state["error"])
        self.assertTrue(state["warning"])
        self.assertTrue(state["error_metadata"]["will_retry"])
        entry["runtime"].busy = False
        self.agent._event(self.sid, {"type": "turn", "event": "completed", "status": "completed", "turn_id": "turn-test"})
        state = self.agent.status(self.sid)
        self.assertEqual(state["last_turn"], {"turn_id": "turn-test", "status": "completed"})
        self.assertIsNone(state["error"])
        self.assertIsNone(state["warning"])
        self.assertIsNone(state["error_metadata"])
        self.agent.disconnect(self.sid)
        self.assertEqual(self.agent.status(self.sid)["last_turn"]["status"], "completed")

    def test_failed_turn_without_error_is_exposed_and_new_turn_clears_old_outcome(self):
        self.agent.message(self.sid, "先检查模型")
        runtime = self.agent.entries[self.sid]["runtime"]
        runtime.busy = False
        self.agent._event(self.sid, {"type": "turn", "event": "completed", "status": "failed", "turn_id": "turn-test"})
        state = self.agent.status(self.sid)
        self.assertEqual(state["last_turn"]["status"], "failed")
        self.assertTrue(state["error"])
        self.agent.message(self.sid, "继续检查")
        state = self.agent.status(self.sid)
        self.assertEqual(state["status"], "running")
        self.assertIsNone(state["last_turn"])
        self.assertIsNone(state["error"])
        self.agent._event(self.sid, {"type": "error", "message": "Late prior-turn error", "turn_id": "old-turn"})
        self.agent._event(self.sid, {"type": "turn", "event": "completed", "status": "failed", "turn_id": "old-turn"})
        self.assertIsNone(self.agent.status(self.sid)["error"])
        self.assertIsNone(self.agent.status(self.sid)["last_turn"])

    def test_unknown_session_is_not_a_filesystem_path(self):
        with self.assertRaises(FileNotFoundError):
            self.agent.status("../another-plan")

    def test_cli_config_override_uses_unquoted_dotted_keys(self):
        (self.root / "config.toml").write_text('[mcp_servers.node_repl]\nenabled = true\n', encoding="utf-8")
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root)}):
            argv = runtime_command("codex.exe")
        self.assertEqual(argv, ["codex.exe", "app-server", "--stdio", "-c", "mcp_servers.node_repl.enabled=false"])

    def test_tool_validation_returns_actionable_failure_to_model(self):
        self.read()
        args = payload(self.plan)
        self.agent.call_tool(self.sid, "save_plan", args)
        failed = self.agent.runtime_tool(self.sid, "save_plan", args)
        self.assertFalse(failed["success"])
        text = json.loads(failed["contentItems"][0]["text"])
        self.assertIn("版本已变化", text["error"])
        self.assertEqual(text["next_action"]['tool'], 'get_task_context')
        self.assertTrue(text['draft_id'])

    def test_agent_can_see_unresolved_method_gaps_before_reassessment(self):
        self.read()
        args = payload(self.plan)
        args["evidence_gaps"] = ["温度场映射设置未核实"]
        self.agent.call_tool(self.sid, "save_plan", args)
        context = self.agent.call_tool(self.sid, "get_plan", {})
        self.assertEqual(context["evidence_gaps"], args["evidence_gaps"])
        self.assertTrue(context["planning_incomplete"])
        self.assertEqual(context["research_trace"][0]["evidence_gaps"], args["evidence_gaps"])

    def test_empty_page_cannot_be_used_as_a_read_citation(self):
        with self.assertRaises(ValueError):
            self.agent.call_tool(self.sid, "read_source", {"id": SOURCE["id"], "offset": 999999, "length": 500})
        self.assertEqual(self.agent.entries[self.sid]["record"]["read_sources"], [])
        with self.assertRaises(ValueError):
            self.agent.call_tool(self.sid, "save_plan", payload(self.plan))

    def test_model_settings_are_plan_scoped_and_reused_on_reconnect(self):
        self.agent.settings(self.sid, "test-model", "medium")
        other = self.sessions.create("另一份待规划的传热任务", None)
        self.assertEqual(self.agent.status(other["id"])["reasoning_effort"], "xhigh")
        self.agent.connect(self.sid)
        runtime = self.agent.entries[self.sid]["runtime"]
        self.assertEqual(runtime.kwargs["model"], "test-model")
        self.assertEqual(runtime.kwargs["reasoning_effort"], "medium")
        self.agent.connect(self.sid)
        self.assertIs(runtime, self.agent.entries[self.sid]["runtime"])
        with self.assertRaises(ConflictError):
            self.agent.settings(self.sid, "other-model", "high")
        self.assertIs(runtime, self.agent.entries[self.sid]["runtime"])
        self.agent.disconnect(self.sid)
        self.agent.connect(self.sid)
        self.assertEqual(self.agent.entries[self.sid]["runtime"].kwargs["model"], "test-model")
        self.assertEqual(self.agent.entries[self.sid]["runtime"].starts[-1], "codex-test-thread")

    def test_model_settings_survive_service_restart_and_plan_engine_matches(self):
        self.agent.settings(self.sid, "test-model", "high")
        self.agent.close()
        self.agent = AgentSessions(self.root, self.sessions, self.kb, runtime_factory=FakeRuntime)
        state = self.agent.status(self.sid)
        self.assertEqual((state["model"], state["reasoning_effort"]), ("test-model", "high"))
        self.read()
        self.agent.call_tool(self.sid, "save_plan", payload(self.plan))
        self.assertEqual(self.sessions.get(self.sid)["plan"]["engine"], "custom / test-model / high")

    def test_invalid_settings_never_replace_valid_configuration(self):
        previous = self.agent.status(self.sid)
        for model, effort in [("bad;command", "high"), ("valid-model", "unsupported"), ("valid-model", []), (None, "low")]:
            with self.subTest(model=model, effort=effort), self.assertRaises(ValueError):
                self.agent.settings(self.sid, model, effort)
        current = self.agent.status(self.sid)
        self.assertEqual(current["model"], previous["model"])
        self.assertEqual(current["reasoning_effort"], previous["reasoning_effort"])


if __name__ == "__main__":
    unittest.main()
