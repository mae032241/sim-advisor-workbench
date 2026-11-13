"""A structural review must remain useful without inventing physical findings."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.plan_review import review_plan


def plan():
    return {"title": "实施草稿", "summary": "按当前公开任务实施。", "assumptions": [],
            "steps": [{"id": "setup", "title": "设置", "purpose": "建立所选路线", "action": "按用户输入设置表达式。",
                       "location": "组件 / 定义", "selection": "按命名选择定位作用域",
                       "inputs": ["输入表达式及单位见任务回答"], "checks": ["实施前核对定义及量纲"],
                       "depends_on": [], "source_ids": ["read"], "difficulty": "实施前检查尚未执行。"}],
            "missing_information": [], "difficulties": [], "evidence_status": "方法依据仅限已读正文。"}


def test_empty_fields_are_actionable_but_never_a_physics_validation_or_save_gate():
    draft = plan()
    draft["steps"][0].update(action="", selection="", inputs=[], checks=[])
    result = review_plan(draft)
    finding = next(item for item in result["warnings"] if item["code"] == "implementation_fields_empty")
    assert finding["fields"] == ["action", "selection", "inputs", "checks"]
    assert finding["category"] == "implementation_check"
    assert result["blocking"] is False
    assert result["physical_correctness"] == "not_evaluated"
    assert result["source_applicability"] == "agent_review_required"


def test_nonempty_physics_words_cannot_prove_complete_units_equations_or_coupling():
    first, second = plan(), plan()
    first["steps"][0]["action"] = "Use interface and solver; consider every physical factor."
    second["steps"][0]["action"] = "任意未求解的符号、接口和参数。"
    a, b = review_plan(first), review_plan(second)
    assert a == b
    assert a["per_stage_coverage"][0]["content_review"] == "agent_review_required"
    assert a["physical_correctness"] == "not_evaluated"


def test_inventory_uses_public_goal_answers_model_and_authored_route_only():
    context = {"goal": "交付量A与量B", "answers": {"outputs": "A [m], B [s]", "conditions": "已给工况"},
               "questions": [{"id": "outputs", "label": "目标量", "required": True}],
               "model": {"components": [{"tag": "comp1"}], "path": "private-local-path", "parser_detail": "not-public"},
               "unrelated_internal_data": "must not be read"}
    brief = {"candidate_routes": [{"id": "chosen", "status": "recommended", "name": "所选路线"},
                                  {"id": "other", "status": "rejected", "name": "未选路线"}]}
    result = review_plan(plan(), context=context, design_brief=brief)
    ids = {item["id"] for item in result["requirement_inventory"]}
    assert ids == {"goal", "answer:outputs", "answer:conditions", "model:components", "route:chosen"}
    assert all(item["mapping_status"] == "review_required" for item in result["requirement_inventory"])
    assert result["input_gaps"] == []
    assert "private-local-path" not in str(result)
    assert "must not be read" not in str(result)


def test_authored_subrequirements_map_outputs_without_literal_text_matching_or_mutation():
    draft = plan()
    context = {"goal": "交付两个不同定义的目标量。", "answers": {"outputs": "全部定义已提供。"}}
    checklist = [{"requirement_id": "goal", "statement": "目标量A的实现与提取", "step_ids": ["setup"],
                  "status": "implemented", "reason": "步骤已列明实现。", "source_ids": ["read"]},
                 {"requirement_id": "goal", "statement": "目标量B的实现与提取", "step_ids": ["setup"],
                  "status": "conditional", "reason": "适用条件需实施时检查。"}]
    before = copy.deepcopy((draft, context, checklist))
    result = review_plan(draft, context=context, checklist=checklist,
                         evidence={"read_source_ids": ["read"], "available_source_ids": ["read"]})
    requirement = result["requirement_inventory"][0]
    assert requirement["mapping_status"] == "mixed_authored_status"
    assert len(requirement["mappings"]) == 2
    assert result["evidence_gaps"] == []
    assert (draft, context, checklist) == before
    assert result["physical_correctness"] == "not_evaluated"


def test_ledger_and_current_availability_are_separate_from_authored_claims():
    draft = plan()
    draft["steps"][0]["source_ids"] = ["read", "unread", "closed"]
    result = review_plan(draft, evidence={"read_source_ids": ["read", "closed"], "available_source_ids": ["read", "unread"]})
    gaps = {item["code"]: item for item in result["evidence_gaps"]}
    assert gaps["source_not_in_read_ledger"]["source_ids"] == ["unread"]
    assert gaps["source_unavailable"]["source_ids"] == ["closed"]
    assert result["input_gaps"] == []
    assert review_plan(draft)["evidence_gaps"] == []
    assert review_plan(draft, evidence={"read_source_ids": [], "available_source_ids": []})["evidence_gaps"]


def test_mapping_errors_remain_advisory_and_do_not_invent_user_input_gaps():
    checklist = [{"requirement_id": "goal", "step_ids": [], "status": "implemented", "reason": "作者遗漏映射。"},
                 {"requirement_id": "goal", "step_ids": ["absent"], "status": "unresolved", "reason": "方法需补查。"},
                 {"requirement_id": "not-public", "step_ids": ["setup"], "status": "implemented", "reason": "无公开来源。"}]
    result = review_plan(plan(), context={"goal": "当前目标"}, checklist=checklist)
    codes = {item["code"] for item in result["implementation_checks"]}
    assert {"requirement_has_no_implementation_step", "unknown_step_mapping", "authored_requirement_unresolved", "unknown_requirement_mapping"} <= codes
    assert result["input_gaps"] == []
    assert result["blocking"] is False


def test_decision_and_simplification_evidence_is_checked_in_addition_to_steps():
    brief = {"decisions": [{"id": "choice", "source_ids": ["unread"]}],
             "simplification": {"candidates": [{"id": "simplify", "source_ids": ["closed"]}]}}
    result = review_plan(plan(), design_brief=brief,
                         evidence={"read_source_ids": ["read", "closed"], "available_source_ids": ["read", "unread"]})
    assert any(item.get("decision_id") == "choice" and item["code"] == "source_not_in_read_ledger" for item in result["evidence_gaps"])
    assert any(item.get("simplification_id") == "simplify" and item["code"] == "source_unavailable" for item in result["evidence_gaps"])


def test_long_model_payload_is_a_bounded_fact_summary_not_a_requirement_to_reimplement():
    result = review_plan(plan(), context={"model": {"geometry": {"entities": "large model payload " * 2000}}})
    entry = result["requirement_inventory"][0]
    assert entry["kind"] == "model_fact" and entry["text_truncated"] is True
    assert len(entry["text"]) == 1000
    assert result["implementation_checks"] == []


def test_required_unanswered_questions_and_declared_method_gaps_stay_distinct():
    context = {"questions": [{"id": "blank", "required": True, "label": "原有必答问题"},
                             {"id": "given", "required": True, "label": "已有回答"},
                             {"id": "optional", "required": False, "label": "可选问题"}],
               "answers": {"given": "用户已经提供，不能重新询问"}, "evidence_gaps": ["关键方法依赖尚未读到。"]}
    result = review_plan(plan(), context=context)
    assert [item["requirement_id"] for item in result["input_gaps"]] == ["question:blank"]
    assert [item["code"] for item in result["evidence_gaps"]] == ["declared_method_gap"]
    assert not any(item.get("requirement_id") == "answer:given" for item in result["input_gaps"])


def test_authored_not_applicable_explanation_is_retained_without_requiring_fake_steps():
    checklist = [{"requirement_id": "goal", "step_ids": [], "status": "not_applicable", "reason": "当前选定路线不需要此环节。"}]
    result = review_plan(plan(), context={"goal": "当前公开范围"}, checklist=checklist)
    assert result["requirement_inventory"][0]["mappings"] == checklist
    assert result["implementation_checks"] == []


@pytest.mark.parametrize("checklist", [{}, [{}], [{"requirement_id": "goal", "step_ids": [], "status": "passed", "reason": "理由"}],
                                      [{"requirement_id": "goal", "step_ids": [1], "status": "implemented", "reason": "理由"}]])
def test_malformed_authored_checklist_is_rejected_without_mutating_plan(checklist):
    draft = plan()
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError):
        review_plan(draft, checklist=checklist)
    assert draft == before


def test_broken_dependency_order_is_a_structural_implementation_finding():
    draft = plan()
    draft["steps"][0]["depends_on"] = ["later"]
    result = review_plan(draft)
    assert any(item["code"] == "step_dependency_unresolved" for item in result["implementation_checks"])
    assert result["evidence_gaps"] == []
