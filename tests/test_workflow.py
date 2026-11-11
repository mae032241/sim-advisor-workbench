import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from advisor.planner import baseline_plan, questions_for, detect_profile, validate_agent_plan, answer_is_resolved
from advisor.sessions import ConflictError, SessionStore, export_markdown
from advisor.server import App, make_server


SOURCE = {"id": "manual-thermal", "title": "Thermal Expansion", "text": "Temperature input and reference temperature.", "excerpt": "Thermal expansion coupling.", "url": "https://doc.comsol.com/6.3/doc/com.comsol.help.sme/sme_ug_multiphysics.18.50.html", "kind": "manual", "version": "6.3", "branch": "sme", "section_path": "Solid Mechanics > Thermal Expansion", "score": 1}
GOAL = "分析支架非均匀加热后的热应力和变形"
ANSWERS = {"outputs": "最大位移与等效应力；比较两级网格差异低于 2%", "regime": "stationary", "conditions": "支架底部固定，指定均匀温度 323.15 K", "materials": "采用模型中的 Structural steel，并核对热膨胀系数", "scope": "plan", "temperature_source": "prescribed"}
TEST_TEMP = Path(__file__).resolve().parents[1] / ".test-temp"
TEST_TEMP.mkdir(exist_ok=True)


class FakeKB:
    def search(self, query, limit=8, kind="all", filters=None):
        return [copy.deepcopy(SOURCE)] if kind in {"all", "manual"} else []
    def get_document(self, sid):
        return copy.deepcopy(SOURCE) if sid == SOURCE["id"] else None
    def status(self):
        return {"ready": True, "document_count": 1}


class FakePlanner:
    recommendation = None
    def status(self):
        return {"available": True, "mode": "test"}
    def refine(self, session):
        plan = baseline_plan(session["goal"], session["model"], session["answers"], [SOURCE])
        plan["engine"] = "test-refiner"
        decisions = [{"id": "coupling", "topic": "耦合路径", "recommendation": self.recommendation, "reason": "test evidence", "alternatives": ["alternative"], "source_ids": [SOURCE["id"]], "requires_confirmation": True}] if self.recommendation else []
        return {"plan": plan, "questions": questions_for(session["goal"]), "sources": [copy.deepcopy(SOURCE)], "design_brief": {"decisions": decisions}, "research_trace": []}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP)
        self.store = SessionStore(Path(self.temp.name), FakeKB(), FakePlanner())
        self.intake = self.store.create(GOAL, None)
        self.session = self.store.refine(self.intake["id"], self.intake["revision"])
    def tearDown(self):
        self.temp.cleanup()
    def test_confirm_requires_actual_answers(self):
        with self.assertRaises(ValueError):
            self.store.confirm(self.session["id"], 2)
        unresolved = dict(ANSWERS, regime="undecided", conditions="温度待定")
        saved = self.store.answers(self.session["id"], 2, unresolved)
        self.assertEqual(saved["status"], "needs_replan")
        self.assertTrue(saved["plan"]["missing_information"])
        saved = self.store.refine(saved["id"], saved["revision"])
        self.assertEqual(saved["status"], "needs_input")
        with self.assertRaises(ValueError):
            self.store.confirm(saved["id"], saved["revision"])
    def test_intake_does_not_assign_physics_or_run_keyword_search(self):
        cad = {"asset_kind": "cad_geometry", "physics": [], "materials": [], "studies": []}
        with patch.object(self.store.kb, "search") as search:
            created = self.store.create("只有阀门STEP，需要预测流量与压降", cad)
        search.assert_not_called()
        self.assertEqual(created["plan"]["engine"], "intake-only")
        self.assertEqual(created["plan"]["steps"], [])
        self.assertEqual(created["questions"], [])
        self.assertEqual(created["sources"], [])
        self.assertEqual(created["model"]["physics"], [])
        with self.assertRaises(ValueError):
            self.store.confirm(created["id"], 1)
    def test_save_confirm_edit_invalidates_and_persists(self):
        saved = self.store.answers(self.session["id"], 2, ANSWERS)
        self.assertEqual(saved["plan"], self.session["plan"])
        with self.assertRaises(ValueError):
            self.store.confirm(saved["id"], saved["revision"])
        saved = self.store.refine(saved["id"], saved["revision"])
        confirmed = self.store.confirm(saved["id"], saved["revision"])
        self.assertEqual(confirmed["confirmed_revision"], 4)
        changed = self.store.answers(saved["id"], 4, {"outputs": "最大位移，网格差异低于 1%"})
        self.assertEqual(changed["status"], "needs_replan")
        self.assertEqual(changed["plan"], confirmed["plan"])
        self.assertNotIn("confirmed_revision", changed)
        reloaded = SessionStore(Path(self.temp.name), FakeKB(), FakePlanner()).get(changed["id"])
        self.assertEqual(reloaded["revision"], 5)
        self.assertIn("1%", reloaded["answers"]["outputs"])
    def test_stale_revision_never_overwrites(self):
        self.store.answers(self.session["id"], 2, ANSWERS)
        with self.assertRaises(ConflictError):
            self.store.answers(self.session["id"], 2, {"outputs": "stale"})
        self.assertEqual(self.store.get(self.session["id"])["answers"]["outputs"], ANSWERS["outputs"])
    def test_refine_invalidates_confirmation(self):
        saved = self.store.answers(self.session["id"], 2, ANSWERS)
        saved = self.store.refine(saved["id"], saved["revision"])
        self.store.confirm(saved["id"], 4)
        refined = self.store.refine(saved["id"], 4)
        self.assertEqual((refined["revision"], refined["status"], refined["plan"]["engine"]), (5, "draft", "test-refiner"))
    def test_failed_refine_preserves_original_session(self):
        before = self.store.get(self.session["id"])
        with patch.object(self.store.planner, "refine", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                self.store.refine(before["id"], before["revision"])
        self.assertEqual(self.store.get(before["id"]), before)
    def test_concurrent_answer_edit_rejects_stale_agent_result(self):
        def concurrent_edit(session):
            self.store.answers(session["id"], session["revision"], ANSWERS)
            return dict(session["plan"], engine="stale-agent")
        with patch.object(self.store.planner, "refine", side_effect=concurrent_edit):
            with self.assertRaises(ConflictError):
                self.store.refine(self.session["id"], 2)
        self.assertEqual(self.store.get(self.session["id"])["plan"]["engine"], "test-refiner")
        self.assertTrue(self.store.get(self.session["id"])["plan_stale"])
        self.assertEqual(self.store.get(self.session["id"])["answers"], ANSWERS)
    def test_reject_bad_answer_and_bad_id(self):
        for answer in ({"unexpected": "x"}, {"regime": "bad"}, {"conditions": []}, {"conditions": None}):
            with self.assertRaises(ValueError):
                self.store.answers(self.session["id"], 2, answer)
        with self.assertRaises(FileNotFoundError):
            self.store.get("../secret")
    def test_export_contains_evidence_state_and_history(self):
        markdown = export_markdown(self.session)
        self.assertIn(SOURCE["url"], markdown)
        self.assertIn("test-refiner", markdown)
        self.assertIn("未提交", markdown)
        self.assertIn("版本记录", markdown)
    def test_decision_acceptance_does_not_trigger_endless_replanning(self):
        self.store.planner.recommendation = "先热后结构的单向求解"
        saved = self.store.answers(self.session["id"], 2, ANSWERS)
        planned = self.store.refine(saved["id"], saved["revision"])
        decision_id = next(q["id"] for q in planned["questions"] if q["id"].startswith("decision_"))
        accepted = self.store.answers(planned["id"], planned["revision"], {decision_id: "accept"})
        self.assertFalse(accepted["plan_stale"])
        self.store.confirm(accepted["id"], accepted["revision"])
        self.store.planner.recommendation = "考虑接触热阻反馈的双向耦合"
        revised = self.store.refine(accepted["id"], accepted["revision"])
        self.assertNotIn(decision_id, revised["answers"])
        self.assertTrue(revised["answer_archive"])
        with self.assertRaises(ValueError):
            self.store.confirm(revised["id"], revised["revision"])
    def test_changed_question_meaning_cannot_reuse_old_answer(self):
        saved = self.store.answers(self.session["id"], 2, ANSWERS)
        question = dict(saved["questions"][0], label="改为询问完全不同的设计限值")
        self.store.merge_questions(saved, {"questions": [question]})
        self.assertNotIn(question["id"], saved["answers"])
        self.assertTrue(saved["answer_archive"])
    def test_simplification_acceptance_is_scoped_to_conditions_and_validation(self):
        candidate = {"id": "small_holes", "target": "待检查的非承载小孔", "action": "检查后考虑去除", "rationale": "减少细网格",
                     "applicability": "不属于支承、热源和目标应力区域", "expected_benefit": "减少单元数", "lost_effects": ["局部应力集中"],
                     "geometry_evidence": ["当前快照没有逐面数据，未证实具体小孔"], "source_ids": [SOURCE["id"]],
                     "verification": ["以含孔模型对照目标位移，误差阈值由用户确认"], "status": "needs_inspection", "requires_confirmation": True}
        bundle = self.store.planner.refine(self.session)
        bundle["design_brief"]["simplification"] = {"objective": "保持目标位移精度", "protected_features": ["受载区域"], "candidates": [candidate], "unresolved_geometry": ["孔的位置与用途"]}
        with patch.object(self.store.planner, "refine", return_value=bundle):
            planned = self.store.refine(self.session["id"], self.session["revision"])
        question = next(q for q in planned["questions"] if q["id"].startswith("decision_simp_"))
        self.assertIn("纳入检查", question["label"])
        self.assertIn("不表示已验证", question["reason"])
        accepted = self.store.answers(planned["id"], planned["revision"], {question["id"]: "accept"})
        self.assertFalse(accepted["plan_stale"])
        self.assertEqual(accepted["design_brief"]["simplification"]["candidates"][0]["status"], "needs_inspection")
        markdown = export_markdown(accepted)
        for content in ("模型简化指导", "局部应力集中", "孔的位置与用途", "未修改 CAD", SOURCE["url"]):
            self.assertIn(content, markdown)
        candidate["applicability"] = "新增允许薄壁处去孔，需重审承载路径"
        with patch.object(self.store.planner, "refine", return_value=bundle):
            revised = self.store.refine(accepted["id"], accepted["revision"])
        self.assertNotIn(question["id"], revised["answers"])
        self.assertTrue(any(item["question"]["id"] == question["id"] for item in revised["answer_archive"]))
        with self.assertRaises(ValueError):
            self.store.confirm(revised["id"], revised["revision"])
        revised_question = next(q for q in revised["questions"] if q["id"].startswith("decision_simp_"))
        revised = self.store.answers(revised["id"], revised["revision"], {revised_question["id"]: "accept"})
        bundle["design_brief"]["simplification"]["protected_features"].append("局部应力峰值")
        with patch.object(self.store.planner, "refine", return_value=bundle):
            protected = self.store.refine(revised["id"], revised["revision"])
        self.assertNotIn(revised_question["id"], protected["answers"])
    def test_focused_simplification_cannot_bypass_intake_or_stale_plan(self):
        with self.assertRaises(ValueError):
            self.store.refine(self.intake["id"], self.intake["revision"], focus="simplification")
        bundle = self.store.planner.refine(self.session)
        with patch.object(self.store.planner, "refine_simplification", return_value=bundle, create=True) as focus:
            updated = self.store.refine(self.session["id"], self.session["revision"], focus="simplification")
            focus.assert_called_once()
            self.assertEqual(updated["history"][-1]["event"], "simplification_refined")
            changed = self.store.answers(updated["id"], updated["revision"], {"conditions": "支承形式尚未确定，增加新的热源"})
            with self.assertRaises(ValueError):
                self.store.refine(changed["id"], changed["revision"], focus="simplification")
            focus.assert_called_once()
    def test_route_change_can_retire_an_irrelevant_required_question(self):
        saved = self.store.answers(self.session["id"], 2, ANSWERS)
        self.store.merge_questions(saved, {"questions": [], "retired_questions": [{"id": "temperature_source", "reason": "路线改为直接使用已确认的温度输入，不再比较温度来源。"}]})
        self.assertNotIn("temperature_source", {q["id"] for q in saved["questions"]})
        self.assertNotIn("temperature_source", saved["answers"])
        self.assertEqual(saved["question_archive"][0]["question"]["id"], "temperature_source")
        self.assertTrue(any(entry["question"]["id"] == "temperature_source" for entry in saved["answer_archive"]))
    def test_blocked_routes_cannot_be_confirmed_even_without_questions(self):
        bundle = FakePlanner().refine(self.session)
        bundle["questions"] = []
        bundle["design_brief"] = {"candidate_routes": [{"status": "blocked"}], "decisions": []}
        intake = self.store.create("只有几何，但还不知道需要模拟什么物理问题", None)
        with patch.object(self.store.planner, "refine", return_value=bundle):
            planned = self.store.refine(intake["id"], intake["revision"])
        self.assertTrue(planned["planning_incomplete"])
        with self.assertRaises(ValueError):
            self.store.confirm(planned["id"], planned["revision"])


class PlanTests(unittest.TestCase):
    def test_traditional_chinese_profiles(self):
        self.assertEqual(detect_profile("熱應力分析"), "thermal_stress")
        self.assertEqual(detect_profile("流體壓降"), "flow")
        self.assertEqual(detect_profile("結構振動"), "structure")
        self.assertFalse(answer_is_resolved("入口溫度待確認"))
    def test_reject_fabricated_citations_and_dependency_cycle(self):
        plan = baseline_plan(GOAL, None, {}, [SOURCE])
        plan.pop("engine")
        validate_agent_plan(plan, [SOURCE])
        plan["steps"][0]["source_ids"] = ["invented"]
        with self.assertRaises(ValueError):
            validate_agent_plan(plan, [SOURCE])
        plan["steps"][0]["source_ids"] = []
        plan["steps"][0]["depends_on"] = [plan["steps"][-1]["id"]]
        with self.assertRaises(ValueError):
            validate_agent_plan(plan, [SOURCE])


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP)
        cls.app = App.__new__(App)
        cls.app.data_dir = Path(cls.temp.name)
        cls.app.kb = FakeKB()
        cls.app.planner = FakePlanner()
        cls.app.sessions = SessionStore(cls.app.data_dir, cls.app.kb, cls.app.planner)
        cls.app.index_lock = threading.Lock()
        cls.server = make_server(cls.app, 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()
    def request(self, path, body=None, headers=None):
        req = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, response.read(), response.headers
        except HTTPError as exc:
            return exc.code, exc.read(), exc.headers
    def test_round_trip_and_conflict(self):
        code, raw, _ = self.request("/api/sessions", {"goal": GOAL})
        self.assertEqual(code, 201)
        session = json.loads(raw)
        sid = session["id"]
        self.assertEqual(self.request(f"/api/sessions/{sid}/confirm", {"revision": 1})[0], 422)
        self.assertEqual(self.request(f"/api/sessions/{sid}/simplify", {"revision": 1})[0], 422)
        self.assertEqual(self.request(f"/api/sessions/{sid}/refine", {"revision": 1})[0], 200)
        code, raw, _ = self.request(f"/api/sessions/{sid}/answers", {"revision": 2, "answers": ANSWERS})
        self.assertEqual(code, 200)
        self.assertEqual(self.request(f"/api/sessions/{sid}/confirm", {"revision": 2})[0], 409)
        self.assertEqual(self.request(f"/api/sessions/{sid}/confirm", {"revision": 3})[0], 422)
        self.assertEqual(self.request(f"/api/sessions/{sid}/refine", {"revision": 3})[0], 200)
        self.assertEqual(self.request(f"/api/sessions/{sid}/confirm", {"revision": 4})[0], 200)
        code, raw, headers = self.request(f"/api/sessions/{sid}/export?format=markdown")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("confirmed", raw.decode())
    def test_cross_site_post_and_traversal_blocked(self):
        self.assertEqual(self.request("/api/sessions", {"goal": GOAL}, {"Origin": "https://untrusted.example"})[0], 403)
        self.assertEqual(self.request("/api/sessions", {"goal": GOAL}, {"Host": "untrusted.example"})[0], 403)
        self.assertEqual(self.request("/../advisor/config.py")[0], 404)
    def test_static_and_evidence(self):
        code, raw, headers = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/api/sources/manual-thermal")[0], 200)
        self.assertEqual(self.request("/api/sources/missing")[0], 404)


if __name__ == "__main__":
    unittest.main()
