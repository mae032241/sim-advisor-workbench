import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.agent_backend import AgentBackendError, CodexJSONBackend
from advisor.research_planner import ResearchPlanner, _bounded_data, _evidence_for_model, _read_context, geometry_reference_catalog

S1 = "cs-" + "a" * 24
S2 = "cs-" + "b" * 24
S3 = "cs-" + "c" * 24


def source(sid, title, text, kind="manual"):
    return {"id": sid, "title": title, "text": text, "excerpt": text,
            "url": f"https://doc.comsol.com/6.3/doc/com.comsol.help.cfd/{sid}.html",
            "section_path": ["CFD Module", title], "kind": kind, "version": "6.3",
            "branch": "manual-tree" if kind == "manual" else "application-library", "score": 1.0,
            "parent": {"id": S3, "title": "Fluid Flow", "excerpt": "Actual fixture parent context: domain and boundary features.", "section_path": ["CFD Module", "Fluid Flow"]}}


class FixtureKB:
    def __init__(self):
        self.docs = {S1: source(S1, "The Laminar Flow Interface", "This source describes velocity and pressure for a fluid domain. Choose the study and boundary conditions based on the intended physical problem."),
                     S2: source(S2, "Creating an Internal Fluid Domain", "Construct and verify an internal cavity domain before applying the flow interface. The CAD wall material does not identify the medium that fills this domain.", "case")}
        self.searches, self.reads = [], []

    def search(self, query, limit=8, kind="all", filters=None):
        self.searches.append({"query": query, "limit": limit, "kind": kind})
        ids = [S2] if "domain" in query.lower() else [S1]
        return [copy.deepcopy(self.docs[sid]) for sid in ids if kind == "all" or self.docs[sid]["kind"] == kind]

    def get_document(self, sid):
        self.reads.append(sid)
        return copy.deepcopy(self.docs.get(sid))


def session():
    return {"goal": "只有阀门 STEP 几何，想研究开度变化时的压降和流量关系，并比较合理实现路线。",
            "model": {"format": "STEP", "file_name": "valve.step", "path": "D:/private/original/valve.step",
                      "physics": [], "materials": [], "geometry": {"solid_count": 1},
                      "facts": [{"key": "solid_count", "label": "几何实体数", "value": 1, "source": "STEP entities"}]},
            "questions": [{"id": "outputs", "label": "关注什么结果？", "kind": "text", "required": True, "reason": "原始需求"}],
            "answers": {"outputs": "计算内流体压降和流量关系；阀体变形暂不研究。"}}


def formulation(queries=None):
    return {"problem_summary": "从阀门几何构造内部流体计算域；目标量为压降而不是实体应力。",
            "candidate_physics": [{"name": "Laminar Flow，适用性待核对", "reason": "需求量是压降与流量，几何没有给出流体或雷诺数。",
                                   "assumptions": ["先比较固定开度准稳态内流与需要运动网格的瞬态路线。"],
                                   "domain_hypothesis": "CAD 实体可能是阀体壁面，需要抽取内部空腔，不能将该实体直接当作流体。"}],
            "queries": queries if queries is not None else [{"query": "Laminar Flow interface", "kind": "manual", "reason": "核对流动接口所需假设与输入。"},
                                                          {"query": "internal fluid domain construction", "kind": "case", "reason": "核对从几何获得空腔流体域的方法。"}],
            "geometry_unknowns": ["流体空腔是否封闭，入口出口位置、单位和实际开度未知。"]}


def response(ids=None, action="draft"):
    ids = ids or [S1, S2]
    ref = ids[-1]
    return {"action": action,
            "plan": {"title": "阀门压降的需求到计算域实现指南", "summary": "先确认流动域与工况，再比较稳态开度序列和瞬态运动路线。",
                     "assumptions": ["实体几何不等于流体计算域。"],
                     "steps": [{"id": "construct_domain", "title": "建立并验证内部空腔域", "purpose": "得到可用于流动计算的域。",
                                "action": "预期产物为单独的流体体积和入口/出口命名选择；依据实际封闭性选择构造方法，不能直接选 CAD 壁面实体作为流体。",
                                "location": "组件 / 几何 / 域构造与命名选择", "selection": "待用户确认内腔连通关系，不编造边界编号。",
                                "inputs": ["几何单位、入口/出口位置和实际开度"], "depends_on": [], "source_ids": [ref],
                                "checks": ["体积为正，入口到出口连通；壁面没有泄漏或重叠。"],
                                "difficulty": "若几何未闭合会产生外部连通域；通过截面和连通性诊断，修复或定义封口。"}],
                     "difficulties": ["流体物性和工况缺失，不能判定层流适用性。"], "missing_information": ["流体及入口/出口工况"],
                     "evidence_status": "已读取来源仅支持候选构造方法；目标几何和物理适用性仍未验证。"},
            "design_brief": {"objective": "形成开度—流量—压降关系的实现路线。", "domain_strategy": "先验证或构造内部空腔流体域；CAD 阀体只作为壁面几何来源。",
                             "candidate_routes": [{"id": "fixed_opening", "name": "固定开度流动序列", "physics": ["Laminar Flow（待雷诺数检查）"],
                                                   "rationale": "可先检验固定开度的压降，暂不引入阀体应力。", "assumptions": ["开度可视作准稳态。"],
                                                   "required_inputs": ["介质、流量或压差、几何开度"], "tradeoffs": "成本较低；不捕捉快速启闭的瞬态效应。", "status": "blocked"}],
                             "decisions": [{"id": "fluid_domain", "topic": "计算域选择", "recommendation": "验证并构造内流体域。", "reason": "压降属于流体问题，CAD 实体材料不说明空腔介质。",
                                            "alternatives": ["已有流体体积时直接验证", "若研究外流需外部域"], "source_ids": [ref], "requires_confirmation": True}],
                             "geometry_interpretation": [{"statement": "STEP 快照记录一个 CAD 实体。", "status": "observed", "evidence": "model.geometry.solid_count=1"},
                                                         {"statement": "实体内部可能存在流体空腔。", "status": "hypothesis", "evidence": "需求为阀门内流压降，几何尚未验证。"},
                                                         {"statement": "流体材料和实际边界未知。", "status": "unknown", "evidence": "STEP 不提供物理工况。"}],
                             "simplification": {"objective": "在保留压降目标敏感路径的前提下评估几何简化。", "protected_features": ["节流通道、间隙、入口出口和压差测量位置"],
                                                "candidates": [], "unresolved_geometry": ["当前统计没有给出局部特征定位与尺寸。"]}},
            "questions": [{"id": "fluid_medium", "label": "内腔中流动的介质、温度和物性来源是什么？", "kind": "text", "options": [], "required": True,
                           "reason": "密度与黏度影响流动尺度和接口选择；几何不提供这些数据。"}],
            "retired_questions": [], "selected_source_ids": list(ids), "followup_queries": [], "evidence_gaps": ["尚不能验证内腔构造和流动假设。"]}


class FakeRunner:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def __call__(self, args, *, input_text, cwd, timeout, max_output):
        self.calls.append({"args": args, "prompt": input_text, "cwd": cwd, "timeout": timeout, "max_output": max_output})
        value = self.values.pop(0)
        if callable(value):
            value = value(input_text)
        output = Path(args[args.index("-o") + 1])
        output.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")


def planner(tmp_path, kb, values):
    runner = FakeRunner(values)
    backend = CodexJSONBackend(tmp_path, runner=runner, command="fake-codex", model="gpt-6.1-sol")
    return ResearchPlanner(tmp_path, kb, backend=backend), runner


def test_requirement_to_real_search_read_and_design_has_no_fixed_profile(tmp_path, monkeypatch):
    import advisor.planner as legacy
    monkeypatch.setattr(legacy, "detect_profile", lambda *_: pytest.fail("Legacy fixed physics profile was called"), raising=False)
    kb = FixtureKB()
    research, runner = planner(tmp_path, kb, [formulation(), response()])
    original = session()
    before = copy.deepcopy(original)
    bundle = research.refine(original)
    assert original == before
    assert len(runner.calls) == 2
    assert [item["query"] for item in kb.searches] == [item["query"] for item in formulation()["queries"]]
    assert set(kb.reads) == {S1, S2}
    assert "Solid Mechanics" not in bundle["design_brief"]["candidate_routes"][0]["physics"]
    assert "空腔流体域" in bundle["design_brief"]["domain_strategy"]
    assert bundle["plan"]["engine"] == "injected-runner / gpt-6.1-sol / xhigh"
    assert any("STEP 只读几何" in item for item in bundle["plan"]["assumptions"])
    assert len(bundle["sources"]) == 2
    assert all(source["url"].startswith("https://doc.comsol.com/6.3/") for source in bundle["sources"])
    assert "Actual fixture parent context" in runner.calls[1]["prompt"]
    assert "D:/private/original" not in runner.calls[0]["prompt"]
    assert not (tmp_path / "research-planner-verification.json").exists()
    for call in runner.calls:
        assert "--ephemeral" in call["args"] and "--ignore-user-config" in call["args"]
        assert call["args"][call["args"].index("--sandbox") + 1] == "read-only"
        assert call["args"][call["args"].index("-m") + 1] == "gpt-6.1-sol"
        assert 'model_reasoning_effort="xhigh"' in call["args"]
        assert 0 < call["timeout"] <= 600 and call["max_output"] == 1024 * 1024
        assert not call["cwd"].exists()


def test_agent_requests_evidence_feedback_and_revises_route(tmp_path):
    kb = FixtureKB()
    initial_queries = [{"query": "Laminar Flow interface", "kind": "manual", "reason": "核对初始流动假说。"}]
    first = response([S1], "research")
    first["followup_queries"] = [{"query": "internal fluid domain construction", "kind": "case", "reason": "现有资料没有支持 STEP 内腔域构造，需要补充实现依据。"}]
    final = response()
    final["design_brief"]["candidate_routes"][0]["rationale"] = "补充内腔构造证据后，先验证流体域，再检验固定开度路线。"
    research, runner = planner(tmp_path, kb, [formulation(initial_queries), first, final])
    bundle = research.refine(session())
    assert len(runner.calls) == 3
    assert kb.reads == [S1, S2]
    assert "Construct and verify an internal cavity domain" not in runner.calls[1]["prompt"]
    assert "Construct and verify an internal cavity domain" in runner.calls[2]["prompt"]
    assert "补充内腔构造证据" in bundle["design_brief"]["candidate_routes"][0]["rationale"]
    assert any(item["stage"] == "evidence_feedback" for item in bundle["research_trace"])
    assert bundle["research_trace"][-1]["model_calls"] == 3


def test_second_feedback_request_exhausts_budget_and_returns_blocked_draft(tmp_path):
    kb = FixtureKB()
    first = response([S1], "research")
    first["followup_queries"] = [{"query": "internal fluid domain construction", "kind": "case", "reason": "缺少构造依据。"}]
    second = response(action="research")
    second["followup_queries"] = [{"query": "unperformed next search", "kind": "manual", "reason": "仍不充分。"}]
    second["design_brief"]["candidate_routes"][0]["status"] = "recommended"
    research, runner = planner(tmp_path, kb, [formulation([formulation()["queries"][0]]), first, second])
    bundle = research.refine(session())
    assert len(runner.calls) == 3
    assert all(item["query"] != "unperformed next search" for item in kb.searches)
    assert "budget_exhausted" in bundle["plan"]["evidence_status"]
    assert bundle["design_brief"]["candidate_routes"][0]["status"] == "blocked"
    assert any(item.get("status") == "budget_exhausted" for item in bundle["research_trace"])


def test_preserves_answered_question_identity_and_stabilizes_new_ids(tmp_path):
    kb = FixtureKB()
    altered = response()
    altered["questions"].append({"id": "outputs", "label": "关注什么结果？", "kind": "text", "options": [], "required": False, "reason": "原始需求"})
    research, _ = planner(tmp_path, kb, [formulation(), altered])
    first = research.refine(session())
    assert first["questions"][0] == session()["questions"][0]
    new = first["questions"][1]
    assert new["id"].startswith("rq_") and new["options"] == []
    changed_id = response()
    changed_id["questions"][0]["id"] = "another_random_id"
    research2, _ = planner(tmp_path / "second", kb, [formulation(), changed_id])
    second = research2.refine(session())
    assert second["questions"][1]["id"] == new["id"]
    assert "answer" not in new and "default" not in new and "value" not in new


@pytest.mark.parametrize("field", ["label", "kind", "options"])
def test_rejects_incompatible_reuse_of_question_identity(tmp_path, field):
    changed = {"id": "outputs", "label": "关注什么结果？", "kind": "text", "options": [], "required": True, "reason": "Question identity must not change."}
    if field == "label":
        changed["label"] = "这个旧 ID 现在被用于询问其他事项。"
    elif field == "kind":
        changed["kind"] = "select"
        changed["options"] = [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]
    else:
        changed["options"] = [{"value": "unexpected", "label": "Unexpected"}]
    value = response()
    value["questions"].append(changed)
    research, _ = planner(tmp_path, FixtureKB(), [formulation(), value])
    original = session()
    before = copy.deepcopy(original)
    with pytest.raises(ValueError, match="已有问题身份"):
        research.refine(original)
    assert original == before
    assert not research.status()["busy"]


@pytest.mark.parametrize("blocking_gap,route_status,expected", [
    (True, "recommended", True), (False, "blocked", True), (False, "recommended", False),
])
def test_draft_action_cannot_bypass_documentation_or_route_blockers(tmp_path, blocking_gap, route_status, expected):
    value = response()
    value["evidence_gaps"] = ["推荐内腔提取方法的适用性仍缺少文档依据。"] if blocking_gap else []
    value["design_brief"]["candidate_routes"][0]["status"] = route_status
    research, runner = planner(tmp_path, FixtureKB(), [formulation(), value])
    bundle = research.refine(session())
    assert len(runner.calls) == 2
    assert bundle["planning_incomplete"] is expected
    assert any(item.get("status") == "blocked_evidence" for item in bundle["research_trace"]) is expected
    if blocking_gap:
        assert value["evidence_gaps"][0] in bundle["plan"]["missing_information"]


@pytest.mark.parametrize("location", ["plan", "decision", "selection"])
def test_rejects_fabricated_or_unread_citations(tmp_path, location):
    invalid = response()
    if location == "plan":
        invalid["plan"]["steps"][0]["source_ids"] = [S3]
    elif location == "decision":
        invalid["design_brief"]["decisions"][0]["source_ids"] = [S3]
    else:
        invalid["selected_source_ids"] = [S3]
    research, _ = planner(tmp_path, FixtureKB(), [formulation(), invalid])
    with pytest.raises(ValueError):
        research.refine(session())
    assert not research.status()["busy"]


def test_rejects_unsafe_question_ids_and_excessive_queries(tmp_path):
    invalid = response()
    invalid["questions"][0]["id"] = "../../not_safe"
    research, _ = planner(tmp_path / "question", FixtureKB(), [formulation(), invalid])
    with pytest.raises(ValueError, match="ID"):
        research.refine(session())
    too_many = formulation([{ "query": f"query {i}", "kind": "manual", "reason": "reason" } for i in range(7)])
    kb = FixtureKB()
    research, runner = planner(tmp_path / "queries", kb, [too_many])
    with pytest.raises(ValueError):
        research.refine(session())
    assert len(runner.calls) == 1 and not kb.searches


def test_process_failure_is_bounded_and_does_not_leak_logs(tmp_path):
    def failing(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "authentication token=DO_NOT_EXPOSE")
    backend = CodexJSONBackend(tmp_path, runner=failing, command="fake-codex")
    with pytest.raises(AgentBackendError) as error:
        backend.generate("sensitive context", {"type": "object"}, stage="formulate")
    assert "DO_NOT_EXPOSE" not in str(error.value) and "sensitive context" not in str(error.value)
    def timing_out(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
    backend = CodexJSONBackend(tmp_path, runner=timing_out, command="fake-codex")
    with pytest.raises(AgentBackendError, match="限制"):
        backend.generate("context", {}, stage="design", timeout=5)


def test_sources_are_read_and_bounded_to_fourteen_across_feedback(tmp_path):
    class WideKB:
        def __init__(self):
            self.docs = {}
            self.reads = []
            self.searches = []

        def search(self, query, limit=8, kind="all"):
            self.searches.append(query)
            values = []
            for index in range(5):
                sid = "cs-" + hashlib.sha256((query + str(index)).encode()).hexdigest()[:24]
                self.docs[sid] = source(sid, query + " document " + str(index), "Actual fixture paragraph for " + query + ". It explains the method and its limitations in this source.")
                values.append(copy.deepcopy(self.docs[sid]))
            return values

        def get_document(self, sid):
            self.reads.append(sid)
            return copy.deepcopy(self.docs[sid])

    def from_evidence(prompt, feedback=False):
        payload = json.loads(prompt.split("输入 JSON:\n", 1)[1])
        result = response([item["id"] for item in payload["evidence"]], action="research" if feedback else "draft")
        if feedback:
            result["followup_queries"] = [{"query": f"followup evidence {index}", "kind": "manual", "reason": "Fill a specific evidence gap."} for index in range(3)]
        return result

    kb = WideKB()
    initial = formulation([{ "query": f"initial evidence {index}", "kind": "manual", "reason": "Resolve an initial modelling hypothesis." } for index in range(6)])
    research, runner = planner(tmp_path, kb, [initial, lambda prompt: from_evidence(prompt, True), from_evidence])
    bundle = research.refine(session())
    assert len(runner.calls) == 3 and len(kb.searches) == 9
    assert len(kb.reads) == len(set(kb.reads)) == 14
    assert len(bundle["sources"]) == 14
    assert sum(item["stage"] == "read" and item["status"] == "completed" for item in bundle["research_trace"]) == 14


def test_large_single_paragraph_is_actual_bounded_evidence(tmp_path):
    assert _read_context({"text": "A" * 7001}, "A") == "A" * 7000
    kb = FixtureKB()
    kb.docs[S1]["text"] = "A" * 7001
    research, runner = planner(tmp_path, kb, [formulation([formulation()["queries"][0]]), response([S1])])
    bundle = research.refine(session())
    actual = bundle["sources"][0]
    assert actual["text"] == "A" * 7000
    assert actual["text_truncated"] is True
    assert actual["read_scope"] == "bounded_source_excerpt"
    assert "A" * 7000 in runner.calls[1]["prompt"]


def test_empty_context_cannot_become_citable_evidence(tmp_path):
    kb = FixtureKB()
    kb.docs[S1]["text"] = " " * 8000
    research, runner = planner(tmp_path, kb, [formulation([formulation()["queries"][0]]), response([S1])])
    with pytest.raises(ValueError):
        research.refine(session())
    payload = json.loads(runner.calls[1]["prompt"].split("输入 JSON:\n", 1)[1])
    assert payload["evidence"] == []


def test_route_change_retires_question_without_reinterpreting_old_answer(tmp_path):
    previous = session()
    previous["questions"].append({"id": "wall_elasticity", "label": "阀体弹性模量是什么？", "kind": "text", "options": [], "required": True, "reason": "此前考虑流固耦合。"})
    previous["answers"]["wall_elasticity"] = "尚未提供"
    previous["design_brief"] = {"candidate_routes": [{"id": "fsi", "name": "考虑阀体变形的流固耦合", "physics": ["Fluid-Structure Interaction"]}]}
    before = copy.deepcopy(previous)
    revised = response()
    revised["retired_questions"] = [{"id": "wall_elasticity", "reason": "用户明确本轮不研究阀体变形，当前改为刚性壁面内流路线，弹性模量不再是本轮输入。"}]
    research, _ = planner(tmp_path, FixtureKB(), [formulation(), revised])
    bundle = research.refine(previous)
    assert all(item["id"] != "wall_elasticity" for item in bundle["questions"])
    assert bundle["retired_questions"] == revised["retired_questions"]
    assert previous == before  # Persistence/answer archival belongs to caller.
    assert any(item.get("status") == "retired" for item in bundle["research_trace"])


@pytest.mark.parametrize("qid,reason", [("unknown_question", "No such input."), ("decision_local", "Do not retire decisions."), ("outputs", " ")])
def test_rejects_invalid_question_retirement(tmp_path, qid, reason):
    original = session()
    original["questions"].append({"id": "decision_local", "label": "采用建议吗？", "kind": "text", "options": [], "required": True, "reason": "Local decision."})
    revised = response()
    revised["retired_questions"] = [{"id": qid, "reason": reason}]
    research, _ = planner(tmp_path, FixtureKB(), [formulation(), revised])
    with pytest.raises(ValueError):
        research.refine(original)


def test_default_deadlines_are_longer_but_still_hard_bounded(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ADVISOR_AGENT_TIMEOUT", raising=False)
    monkeypatch.delenv("SIM_ADVISOR_RESEARCH_TIMEOUT", raising=False)
    research, _ = planner(tmp_path, FixtureKB(), [formulation(), response()])
    assert research.backend.timeout == 600
    assert research.status()["total_timeout_seconds"] == 900
    monkeypatch.setenv("SIM_ADVISOR_AGENT_TIMEOUT", "99999")
    monkeypatch.setenv("SIM_ADVISOR_RESEARCH_TIMEOUT", "99999")
    bounded, _ = planner(tmp_path / "bounded", FixtureKB(), [formulation(), response()])
    assert bounded.backend.timeout == 600
    assert bounded.status()["total_timeout_seconds"] == 900


def test_model_evidence_omits_redundant_metadata_without_losing_content(tmp_path):
    kb = FixtureKB()
    kb.docs[S1]["related_links"] = [{"href": "LINK_TRANSPORT_ONLY" + str(index), "title": "unread linked topic" * 80} for index in range(80)]
    kb.docs[S1]["relative_path"] = "duplicate/local/path.html"
    kb.docs[S1]["headings"] = ["Fluid Domain", "Boundary Conditions"]
    research, runner = planner(tmp_path, kb, [formulation(), response([S1])])
    bundle = research.refine(session())
    assert "LINK_TRANSPORT_ONLY" not in runner.calls[1]["prompt"]
    assert "duplicate/local/path.html" not in runner.calls[1]["prompt"]
    evidence = json.loads(runner.calls[1]["prompt"].split("输入 JSON:\n", 1)[1])["evidence"]
    actual = next(item for item in evidence if item["id"] == S1)
    assert actual["text"] == kb.docs[S1]["text"]
    assert actual["parent"] == kb.docs[S1]["parent"]
    assert actual["headings"] == kb.docs[S1]["headings"]
    assert actual["url"] == kb.docs[S1]["url"]
    assert bundle["sources"][0]["related_links"] == kb.docs[S1]["related_links"]
    assert len(json.dumps(_evidence_for_model([kb.docs[S1]]))) < len(json.dumps([kb.docs[S1]]))


def test_live_status_reports_formulation_retrieval_and_design_elapsed_time(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ADVISOR_AGENT_TIMEOUT", raising=False)
    monkeypatch.delenv("SIM_ADVISOR_RESEARCH_TIMEOUT", raising=False)
    snapshots = []
    kb = FixtureKB()
    research = None
    def observe(value):
        def observed(_):
            snapshots.append(research.status())
            return value
        return observed
    research, _ = planner(tmp_path, kb, [observe(formulation()), observe(response())])
    search = kb.search
    def observed_search(*args, **kwargs):
        snapshots.append(research.status())
        return search(*args, **kwargs)
    kb.search = observed_search
    bundle = research.refine(session())
    assert {item["stage"] for item in snapshots} == {"formulate", "retrieve", "design"}
    assert all(item["busy"] and item["stage_elapsed_seconds"] >= 0 and item["elapsed_seconds"] >= 0 for item in snapshots)
    assert all(0 < item["remaining_seconds"] <= 900 for item in snapshots)
    assert all(item["stage_timeout_seconds"] == 600 for item in snapshots if item["stage"] != "retrieve")
    state = research.status()
    assert not state["busy"] and state["stage"] is None and state["stage_elapsed_seconds"] is None
    assert state["last_attempt"]["status"] == "completed"
    assert [item["stage"] for item in state["last_attempt"]["stages"]] == ["formulate", "retrieve", "design"]
    call_records = [item for item in bundle["research_trace"] if item["stage"] in {"formulate", "design"}]
    assert all(item["prompt_characters"] > 0 and item["schema_characters"] > 0 and item["timeout_seconds"] == 600 for item in call_records)


def test_timeout_retains_sanitized_failed_phase_diagnostics(tmp_path):
    first = FakeRunner([formulation()])
    def timeout_design(args, **kwargs):
        if Path(kwargs["cwd"]).name.startswith("design-"):
            raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr="DO_NOT_EXPOSE_DIAGNOSTIC_SECRET")
        return first(args, **kwargs)
    backend = CodexJSONBackend(tmp_path, runner=timeout_design, command="fake-codex")
    research = ResearchPlanner(tmp_path, FixtureKB(), backend=backend)
    original = session()
    before = copy.deepcopy(original)
    with pytest.raises(AgentBackendError, match="design"):
        research.refine(original)
    assert original == before
    state = research.status()
    assert not state["busy"] and state["stage"] is None
    assert state["last_attempt"]["status"] == "failed"
    assert state["last_attempt"]["failed_stage"] == "design"
    assert state["last_attempt"]["stages"][-1]["error_type"] == "TimeoutExpired"
    assert "DO_NOT_EXPOSE" not in json.dumps(state)
    assert "只有阀门" not in json.dumps(state, ensure_ascii=False)
    assert not (tmp_path / "research-planner-last-attempt.json").exists()


def simplification_candidate(sid=S2):
    return {"id": "external_attachments", "target": "待检查：不参与内流边界且不影响密封配合的外部附件", "action": "在评估副本中比较忽略这些附件的候选，不直接改动原 CAD。",
            "rationale": "压降目标首先依赖节流通道与测量截面；案例策略仅在附件不参与该路径时可迁移。", "applicability": "确认附件与流体边界、密封和压差测量无关，否则保留。",
            "expected_benefit": "可能减少参与实体与网格负担，收益未测量。", "lost_effects": ["若附件参与外流、传热或支承，将丢失相应影响。"],
            "geometry_evidence": ["model.geometry.solid_count=1 只是统计；sim-cli 未定位附件或读取局部尺寸。"], "geometry_fact_refs": [], "target_scope": "local_feature", "source_ids": [sid],
            "verification": ["待执行原始/候选模型压降和流量比较，允许偏差待用户确认。"], "status": "needs_inspection", "requires_confirmation": True}


def focused_base():
    original, completed = session(), response()
    original.update(plan=completed["plan"], design_brief=completed["design_brief"], planning_incomplete=True, plan_stale=False)
    original["plan"]["engine"] = "codex-cli-research / gpt-6.1-sol / xhigh"
    original["plan"]["missing_information"].append("原方案未解决的方法依据缺口。")
    original["design_brief"].pop("simplification")
    return original


def focused_result():
    guide = response()["design_brief"]["simplification"]
    guide["candidates"] = [simplification_candidate()]
    revision = copy.deepcopy(response()["plan"]["steps"][0])
    revision.update(id="assess_simplification", title="评估非参与附件的简化候选", action="建立评估副本，保留流体节流与密封路径，满足前提后对比原始/简化压降。", depends_on=["construct_domain"])
    return {"simplification": guide, "step_revisions": [revision], "questions": response()["questions"], "retired_questions": [], "selected_source_ids": [S2], "evidence_gaps": []}


def test_simplification_is_formal_goal_sensitive_case_grounded_guidance(tmp_path):
    kb = FixtureKB()
    kb.docs[S2]["text"] += " The tutorial omits external support details only when internal fluid boundaries are unchanged and compares pressure drop before accepting the simplification."
    draft = response([S1])
    draft["design_brief"]["simplification"]["candidates"] = [simplification_candidate()]
    draft["plan"]["steps"][0]["action"] += " 简化评估副本保留节流通道，比较忽略非参与外部附件的候选。"
    draft["questions"].append({"id": "attachment_role", "label": "外部附件是否参与内流边界或密封？", "kind": "text", "options": [], "required": True, "reason": "决定案例简化条件能否迁移。"})
    research, runner = planner(tmp_path, kb, [formulation(), draft])
    bundle = research.refine(session())
    guide = bundle["design_brief"]["simplification"]
    assert "节流通道" in guide["protected_features"][0]
    assert guide["candidates"][0]["target"].startswith("待检查")
    assert S2 in {item["id"] for item in bundle["sources"]}  # Solely cited by simplification.
    assert "简化评估" in bundle["plan"]["steps"][0]["action"]
    assert any("外部附件" in item["label"] for item in bundle["questions"])
    assert "简化案例或手册检索" in runner.calls[0]["prompt"]
    assert "统计和名称不足以定位" in runner.calls[1]["prompt"] or "sim-cli/几何快照" in runner.calls[1]["prompt"]
    assert bundle["research_trace"][-1]["simplification_candidate_count"] == 1


@pytest.mark.parametrize("defect", ["citation", "entity_id", "no_verification", "conditional_without_source"])
def test_rejects_ungrounded_or_unverifiable_simplification(tmp_path, defect):
    candidate = simplification_candidate()
    if defect == "citation":
        candidate["source_ids"] = [S3]
    elif defect == "entity_id":
        candidate["target"] = "Remove face 999"
    elif defect == "no_verification":
        candidate["verification"] = []
    else:
        candidate.update(status="conditional", source_ids=[])
    draft = response()
    draft["design_brief"]["simplification"]["candidates"] = [candidate]
    kb = FixtureKB()
    kb.docs[S2]["text"] += " The example uses face 999, which is not a target model entity."
    research, _ = planner(tmp_path, kb, [formulation(), draft])
    with pytest.raises(ValueError):
        research.refine(session())


def test_focused_simplification_reuses_sources_with_one_call_and_preserves_blockers(tmp_path):
    original, kb = focused_base(), FixtureKB()
    before = copy.deepcopy(original)
    research, runner = planner(tmp_path, kb, [focused_result()])
    bundle = research.refine_simplification(original, [{"query": "internal fluid domain simplification", "kind": "case", "reason": "核对策略迁移条件。"}])
    assert original == before and len(runner.calls) == 1 and len(kb.searches) == 1
    assert S2 in kb.reads
    assert bundle["plan"]["steps"][0] == before["plan"]["steps"][0]
    assert bundle["plan"]["steps"][-1]["id"] == "assess_simplification"
    assert before["plan"]["missing_information"] == bundle["plan"]["missing_information"][:len(before["plan"]["missing_information"])]
    assert bundle["planning_incomplete"] is True
    assert bundle["design_brief"]["candidate_routes"] == before["design_brief"]["candidate_routes"]
    assert bundle["research_trace"][-1]["model_calls"] == 1
    assert "不重发整套 plan" in runner.calls[0]["prompt"]
    assert Path(runner.calls[0]["cwd"]).name.startswith("simplification-")


def test_focused_update_inherits_blocked_trace_and_rejects_stale_or_extra_queries(tmp_path):
    original = focused_base()
    original["planning_incomplete"] = False
    original["design_brief"]["candidate_routes"][0]["status"] = "recommended"
    original["research_trace"] = [{"stage": "readiness", "status": "blocked_evidence"}]
    research, _ = planner(tmp_path, FixtureKB(), [focused_result()])
    assert research.refine_simplification(original, queries=[])["planning_incomplete"] is True
    research, runner = planner(tmp_path / "guards", FixtureKB(), [])
    with pytest.raises(ValueError):
        research.refine_simplification(session())
    original["plan_stale"] = True
    with pytest.raises(ValueError):
        research.refine_simplification(original)
    original["plan_stale"] = False
    with pytest.raises(ValueError):
        research.refine_simplification(original, [{"query": str(i) + " strategy", "kind": "case", "reason": "reason"} for i in range(4)])
    assert runner.calls == []


def test_focused_full_source_budget_reports_unread_new_evidence(tmp_path):
    original, kb = focused_base(), FixtureKB()
    ids = ["cs-" + f"{index:024x}" for index in range(14)]
    original["plan"]["steps"][0]["source_ids"] = ids
    original["design_brief"]["decisions"][0]["source_ids"] = []
    for index, sid in enumerate(ids):
        kb.docs[sid] = source(sid, f"Prior reference {index}", "Actual prior source evidence about geometry and physical modeling.")
    value = focused_result()
    value["simplification"]["candidates"][0]["source_ids"] = [ids[0]]
    value["step_revisions"][0]["source_ids"] = [ids[0]]
    value["selected_source_ids"] = []
    research, _ = planner(tmp_path, kb, [value])
    bundle = research.refine_simplification(original, [{"query": "new domain simplification case", "kind": "case", "reason": "Need a new method source."}])
    assert len(kb.reads) == 14 and S2 not in kb.reads
    assert bundle["planning_incomplete"]
    assert any(item.get("status") == "source_budget_exhausted" for item in bundle["research_trace"])
    assert any("14 个来源槽位" in item for item in bundle["plan"]["missing_information"])


def research_request():
    first = response([S1], action="research")
    first["followup_queries"] = [{"query": "internal fluid domain construction", "kind": "case", "reason": "缺少可迁移的域构造依据。"}]
    first["design_brief"]["candidate_routes"][0]["status"] = "recommended"
    return first


def test_retains_validated_real_draft_instead_of_starting_unaffordable_rewrite(tmp_path, monkeypatch):
    import advisor.research_planner as module
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setenv("SIM_ADVISOR_RESEARCH_TIMEOUT", "900")
    first = research_request()
    def formulate_after_ten(_):
        clock[0] += 10
        return formulation([formulation()["queries"][0]])
    def design_after_five_hundred(_):
        clock[0] += 500
        return first
    research, runner = planner(tmp_path, FixtureKB(), [formulate_after_ten, design_after_five_hundred])
    original = session()
    before = copy.deepcopy(original)
    bundle = research.refine(original)
    assert original == before and len(runner.calls) == 2
    assert bundle["plan"]["title"] == first["plan"]["title"]
    assert bundle["plan"]["steps"] == first["plan"]["steps"]
    assert bundle["design_brief"]["candidate_routes"][0]["status"] == "blocked"
    assert bundle["planning_incomplete"] is True
    assert "retained-blocked-draft" in bundle["plan"]["engine"]
    budget = next(item for item in bundle["research_trace"] if item.get("partial_draft_retained"))
    assert budget["remaining_seconds"] == 390 and budget["previous_design_seconds"] == 500
    assert research.status()["last_attempt"]["status"] == "partial_draft_retained"


def test_followup_timeout_retains_only_previously_validated_model_draft(tmp_path):
    first = research_request()
    def timeout(_):
        raise subprocess.TimeoutExpired(["fake-codex"], 600)
    research, runner = planner(tmp_path, FixtureKB(), [formulation([formulation()["queries"][0]]), first, timeout])
    bundle = research.refine(session())
    assert len(runner.calls) == 3
    assert bundle["plan"]["steps"] == first["plan"]["steps"]
    assert bundle["planning_incomplete"] and "补充设计未完成" in bundle["plan"]["evidence_status"]
    assert any(item.get("stage") == "design_followup" and item.get("status") == "failed" for item in bundle["research_trace"])
    assert bundle["research_trace"][-1]["partial_draft_retained"] is True
    assert not (tmp_path / "research-planner-verification.json").exists()


def test_first_design_failure_still_raises_and_legacy_focused_plan_rejected_early(tmp_path):
    def timeout(_):
        raise subprocess.TimeoutExpired(["fake-codex"], 600)
    research, runner = planner(tmp_path, FixtureKB(), [formulation(), timeout])
    original = session()
    before = copy.deepcopy(original)
    with pytest.raises(AgentBackendError):
        research.refine(original)
    assert original == before and len(runner.calls) == 2
    old = focused_base()
    old["design_brief"] = {}
    research, runner = planner(tmp_path / "legacy", FixtureKB(), [])
    with pytest.raises(ValueError, match="旧版方案缺少路线设计"):
        research.refine_simplification(old)
    assert runner.calls == []


BODY_ID = "body-" + "9" * 20


def scoped_session(body_id=BODY_ID):
    value = session()
    value["model"].update(id="model-current", fingerprint="current-file-fingerprint",
        facts=[{"key": "cad.topology", "label": "Topology", "value": {"faces": 99, "solids": 1}, "source": "gmsh getEntities"},
               {"key": "step.raw_entity_counts", "label": "STEP declaration counts", "value": {"ADVANCED_FACE": 99}, "source": "native STEP #99 record"},
               {"key": "cad.wall_thickness", "label": "Wall thickness availability", "value": {"status": "not_implemented", "value": None}, "source": "capability inventory"}],
        geometry={"bodies": [{"id": body_id, "kernel_dim_tag": [3, 99], "surface_count": 99, "source": "gmsh/OpenCASCADE read-only imported body",
                             "bounding_box": {"min": [0, 0, 0], "max": [1, 1, 1], "size": [1, 1, 1], "unit": "m"}, "volume": {"value": 0.5, "unit": "m^3"}}],
                  "capabilities": {"snapshot_scoped_body_ids": "observed", "per_face_inventory": "not_implemented", "wall_thickness": "not_implemented"}})
    return value


def test_exact_scoped_refs_do_not_accept_face99_as_face9_or_step_record_ids(tmp_path):
    for index, target in enumerate(("Remove face 9", "Remove face 99", "Remove boundary 9")):
        original = scoped_session()
        original["goal"] += " 用户文本含 face99；这不是有效逐面清单。"
        candidate = simplification_candidate()
        candidate.update(target=target, geometry_fact_refs=["fact:step.raw_entity_counts"])
        draft = response()
        draft["design_brief"]["simplification"]["candidates"] = [candidate]
        research, _ = planner(tmp_path / str(index), FixtureKB(), [formulation(), draft])
        with pytest.raises(ValueError, match="实体编号清单"):
            research.refine(original)
    candidate = simplification_candidate()
    candidate["geometry_fact_refs"] = ["body:#99"]
    draft["design_brief"]["simplification"]["candidates"] = [candidate]
    research, _ = planner(tmp_path / "record", FixtureKB(), [formulation(), draft])
    with pytest.raises(ValueError, match="白名单"):
        research.refine(scoped_session())


def test_body_refs_are_exact_current_snapshot_ids_with_scoped_observations(tmp_path):
    original = scoped_session()
    candidate = simplification_candidate()
    candidate.update(target="候选 " + BODY_ID + " 的整体省略策略", target_scope="body", status="conditional", geometry_fact_refs=["body:" + BODY_ID])
    draft = response()
    draft["design_brief"]["simplification"]["candidates"] = [candidate]
    research, _ = planner(tmp_path, FixtureKB(), [formulation(), draft])
    bundle = research.refine(original)
    assert bundle["design_brief"]["simplification"]["candidates"][0]["status"] == "conditional"
    entry = next(item for item in bundle["geometry_reference_catalog"] if item["ref"] == "body:" + BODY_ID)
    assert entry["scope"] == "body" and entry["conditional_support"]
    assert entry["snapshot_id"] == "model-current" and entry["fingerprint"] == "current-file-fingerprint"
    assert entry["value"]["volume"]["value"] == 0.5
    candidate["target"] = "候选 " + BODY_ID[:-1] + " 的整体省略策略"
    research, _ = planner(tmp_path / "prefix", FixtureKB(), [formulation(), draft])
    with pytest.raises(ValueError, match="精确引用"):
        research.refine(original)
    candidate["geometry_fact_refs"] = ["body:body-" + "1" * 20]
    research, _ = planner(tmp_path / "other_snapshot", FixtureKB(), [formulation(), draft])
    with pytest.raises(ValueError, match="白名单"):
        research.refine(original)


def test_body_api_or_unit_labels_without_actual_measurements_do_not_support_conditional(tmp_path):
    original = scoped_session()
    body = original["model"]["geometry"]["bodies"][0]
    body["bounding_box"] = {"unit": "m", "status": "not_implemented"}
    body["volume"] = {"unit": "m^3", "value": None}
    candidate = simplification_candidate()
    candidate.update(target_scope="body", status="conditional", geometry_fact_refs=["body:" + BODY_ID])
    draft = response()
    draft["design_brief"]["simplification"]["candidates"] = [candidate]
    research, _ = planner(tmp_path, FixtureKB(), [formulation(), draft])
    bundle = research.refine(original)
    returned = bundle["design_brief"]["simplification"]["candidates"][0]
    assert returned["status"] == "needs_inspection"
    entry = next(item for item in bundle["geometry_reference_catalog"] if item["ref"] == "body:" + BODY_ID)
    assert not entry["conditional_support"]
    assert "bounding_box" not in entry["observed_fields"] and "volume" not in entry["observed_fields"]


@pytest.mark.parametrize("scope,refs", [("global", ["fact:cad.topology"]), ("body", ["fact:cad.topology"]), ("local_feature", ["body:" + BODY_ID]), ("local_feature", ["fact:cad.wall_thickness"])])
def test_measurement_prose_cannot_promote_unimplemented_or_coarse_data(tmp_path, scope, refs):
    candidate = simplification_candidate()
    candidate.update(status="conditional", target_scope=scope, geometry_fact_refs=refs,
                     geometry_evidence=["已测量的局部壁厚为0.5毫米，所有曲率和间隙均已核实。"])
    draft = response()
    draft["design_brief"]["geometry_interpretation"] = [{"statement": "全部逐面厚度和曲率均已测得。", "status": "observed", "evidence": "Agent asserts it was inspected."}]
    draft["design_brief"]["simplification"]["candidates"] = [candidate]
    research, _ = planner(tmp_path, FixtureKB(), [formulation(), draft])
    bundle = research.refine(scoped_session())
    returned = bundle["design_brief"]["simplification"]["candidates"][0]
    assert returned["status"] == "needs_inspection"
    assert returned["geometry_evidence"][0].startswith("候选解释（非观察数据）：")
    assert any("作用范围匹配" in text for text in bundle["design_brief"]["simplification"]["unresolved_geometry"])
    thickness = next(item for item in bundle["geometry_reference_catalog"] if item["ref"] == "fact:cad.wall_thickness")
    assert not thickness["available"] and not thickness["conditional_support"]
    assert thickness["value"] == {"status": "not_implemented", "value": None}
    interpretation = bundle["design_brief"]["geometry_interpretation"][0]
    assert interpretation["status"] == "hypothesis" and interpretation["evidence"].startswith("Agent解释（非解析器观察）：")


def test_catalog_only_contains_bounded_objects_and_rejects_unprovided_refs(tmp_path):
    original = scoped_session()
    template = original["model"]["geometry"]["bodies"][0]
    original["model"]["geometry"]["bodies"] = [{**copy.deepcopy(template), "id": "body-" + f"{index:020x}"} for index in range(66)]
    research, _ = planner(tmp_path, FixtureKB(), [])
    context = research._context(original)
    body_refs = [item["ref"] for item in context["geometry_reference_catalog"] if item["kind"] == "body"]
    assert len(body_refs) == 60
    assert "not evidence of absence" in context["model"]["geometry"]["bodies"][-1]
    assert "body:body-" + f"{65:020x}" not in body_refs
    assert len(json.dumps(context, ensure_ascii=False)) < 100000
    candidate = simplification_candidate()
    candidate.update(target_scope="body", geometry_fact_refs=["body:body-" + f"{65:020x}"])
    draft = response()
    draft["design_brief"]["simplification"]["candidates"] = [candidate]
    research, _ = planner(tmp_path / "unprovided", FixtureKB(), [formulation(), draft])
    with pytest.raises(ValueError, match="白名单"):
        research.refine(original)


def test_reason_cannot_reinterpret_pressure_quantity_or_units(tmp_path):
    original = session()
    original["questions"] = [{"id": "inlet_pressure", "label": "入口测量截面的表压（Pa）是多少？", "kind": "text", "options": [], "required": True, "reason": "提供表压，单位Pa，参考出口大气压。"}]
    original["answers"] = {"inlet_pressure": "100000"}
    changed = copy.deepcopy(original["questions"][0])
    changed["reason"] = "此处读取的是绝对压力，单位kPa。"
    draft = response()
    draft["questions"].append(changed)
    research, _ = planner(tmp_path, FixtureKB(), [formulation(), draft])
    before = copy.deepcopy(original)
    with pytest.raises(ValueError, match="量定义理由"):
        research.refine(original)
    assert original == before


def test_safe_uppercase_simplification_ids_normalize_and_collisions_reject(tmp_path):
    draft = response()
    first, second = simplification_candidate(), simplification_candidate()
    first["id"], second["id"] = "S1", "S2"
    draft["design_brief"]["simplification"]["candidates"] = [first, second]
    research, _ = planner(tmp_path, FixtureKB(), [formulation(), draft])
    assert [item["id"] for item in research.refine(session())["design_brief"]["simplification"]["candidates"]] == ["s1", "s2"]
    second["id"] = "s1"
    research, _ = planner(tmp_path / "collision", FixtureKB(), [formulation(), draft])
    with pytest.raises(ValueError, match="ID 重复"):
        research.refine(session())
