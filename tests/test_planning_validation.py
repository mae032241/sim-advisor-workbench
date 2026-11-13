"""Actionable feedback for actual save failures, without weaker acceptance."""
import copy
import json

import jsonschema
import pytest

from advisor.planning_service import PlanningService, tool_definitions
from advisor.planning_validation import (DESIGN_BRIEF_SCHEMA, QUESTION_SCHEMA, SAFE_ID,
    _check_shape, _safe_question_list, validate_response)
from advisor.sessions import SessionStore
from test_agent_sessions import payload
from test_workflow import FakeKB, FakePlanner, GOAL, SOURCE


def draft():
    result = payload({"goal": GOAL, "revision": 1})
    result["selected_source_ids"] = []
    result["design_brief"]["simplification"]["protected_features"] = ["保留与目标量相关的几何和作用路径。"]
    return result


def candidate(**changes):
    return {"id": "simplify_candidate", "target": "待检查：不影响目标量的外部细节", "action": "在副本中比较保留与忽略的差异。",
            "rationale": "简化适用性需要检查。", "applicability": "必须先定位并确认目标量不敏感。", "expected_benefit": "可能减少网格负担，尚未测量。",
            "lost_effects": [], "geometry_evidence": ["当前没有局部定位或测量，保留待检查状态。"], "geometry_fact_refs": [],
            "target_scope": "local_feature", "source_ids": [], "verification": ["待执行原始/候选模型目标量与守恒比较。"],
            "status": "needs_inspection", "requires_confirmation": True, **changes}


def validate(value):
    validate_response(value, {SOURCE["id"]: SOURCE})


def test_save_shape_error_reports_missing_and_unexpected_names_without_values(tmp_path):
    kb=FakeKB()
    sessions=SessionStore(tmp_path,kb,FakePlanner())
    task=sessions.create(GOAL,None)
    tools=PlanningService(tmp_path,sessions,kb)
    args=payload(task)
    args["design_brief"].pop("domain_strategy")
    args["design_brief"]["wrong_field"]={"private_value":"DO_NOT_ECHO_PAYLOAD"}
    with pytest.raises(ValueError) as caught:
        tools.call_public("save_plan",dict(args,session_id=task["id"]))
    message=json.dumps(caught.value.to_dict(),ensure_ascii=False)
    assert {(error['path'],error['code']) for error in caught.value.errors} == {
        ('/design_brief/domain_strategy','required'),('/design_brief/wrong_field','additionalProperties')}
    assert "DO_NOT_ECHO_PAYLOAD" not in message and "private_value" not in message
    assert sessions.get(task["id"])["revision"]==1


def test_array_shape_error_points_to_the_exact_nested_item():
    value=draft()["design_brief"]
    value["candidate_routes"].append(copy.deepcopy(value["candidate_routes"][0]))
    value["candidate_routes"][1]["required_inputs"]= ["known", 123]
    with pytest.raises(ValueError) as caught:
        _check_shape(value,DESIGN_BRIEF_SCHEMA,"arguments.design_brief")
    assert '"path": "/candidate_routes/1/required_inputs/1"' in str(caught.value)
    assert '"type": "string"' in str(caught.value)


@pytest.mark.parametrize("key,label_key", [("candidate_routes","name"),("decisions","topic")])
def test_route_and_decision_id_feedback_has_path_regex_and_preserved_prefix(key,label_key):
    value=draft()
    if key=="decisions":
        value["design_brief"][key]=[{"id":"valid", "topic":"范围", "recommendation":"待核对", "reason":"依据", "alternatives":[], "source_ids":[], "requires_confirmation":True}]
    value["design_brief"][key][0]["id"]="BAD ID_DO_NOT_ECHO"
    with pytest.raises(ValueError) as caught:
        validate(value)
    message=str(caught.value)
    assert message.startswith("Agent 设计路线/决定 ID 重复或无效。")
    assert f"design_brief.{key}[0].id" in message and SAFE_ID.pattern in message
    assert "BAD ID_DO_NOT_ECHO" not in message


def test_question_id_feedback_has_index_and_exact_existing_regex():
    question={"id":"Question.BAD_SECRET", "label":"入口表压（Pa）是多少？", "kind":"text", "options":[], "required":True, "reason":"明确入口量定义。"}
    with pytest.raises(ValueError) as caught:
        _safe_question_list([question],[])
    message=str(caught.value)
    assert message.startswith("questions[0] 字段不符合schema：")
    assert '"path": "/id"' in message and SAFE_ID.pattern in message
    assert "BAD_SECRET" not in message


@pytest.mark.parametrize("field", ["geometry_evidence","verification"])
@pytest.mark.parametrize("empty", [[],[" ","\n"]])
def test_empty_simplification_feedback_names_only_the_missing_nonblank_field(field,empty):
    value=draft()
    value["design_brief"]["simplification"]["candidates"]=[candidate(**{field:empty})]
    with pytest.raises(ValueError) as caught:
        validate(value)
    message=str(caught.value)
    assert message.startswith("简化候选需要几何事实/限制和待执行验证方法。")
    assert f"design_brief.simplification.candidates[0].{field}" in message
    other="verification" if field=="geometry_evidence" else "geometry_evidence"
    assert "."+other not in message


def test_unobserved_conditional_downgrades_before_source_gate_and_still_rejects_fabricated_sources():
    value=draft()
    value["design_brief"]["simplification"]["candidates"]=[candidate(status="conditional",source_ids=[])]
    validate(value)
    assert value["design_brief"]["simplification"]["candidates"][0]["status"] == "needs_inspection"
    value["design_brief"]["simplification"]["candidates"][0]["source_ids"]=["cs-not-actually-read"]
    with pytest.raises(ValueError,match="未实际读取"):
        validate(value)


def test_geometry_supported_conditional_still_requires_real_method_source(monkeypatch):
    import advisor.planning_validation as module
    monkeypatch.setattr(module,'geometry_reference_catalog',lambda _model:[
        {'ref':'body:body-abc','scope':'body','conditional_support':True}])
    value=draft()
    value['design_brief']['simplification']['candidates']=[candidate(
        status='conditional',target_scope='body',geometry_fact_refs=['body:body-abc'])]
    with pytest.raises(ValueError,match='source_ids.*status=conditional'):
        validate(value)


def test_public_schema_documents_ids_and_actual_simplification_requirements():
    save=next(t for t in tool_definitions() if t["name"]=="save_plan")["inputSchema"]["properties"]
    assert save["questions"]["items"]["properties"]["id"]["pattern"]==SAFE_ID.pattern
    assert QUESTION_SCHEMA["properties"]["id"]["description"]
    brief=save["design_brief"]["properties"]
    for key in ["candidate_routes","decisions"]:
        schema=brief[key]["items"]["properties"]["id"]
        assert schema["pattern"]==SAFE_ID.pattern and schema["description"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate("Route.Bad",schema)
    fields=brief["simplification"]["properties"]["candidates"]["items"]["properties"]
    assert all(fields[key]["description"] for key in ["geometry_evidence","verification","source_ids"])
    assert "conditional" in fields["source_ids"]["description"]
    jsonschema.validate("S1",fields["id"])  # Existing normalization remains compatible at MCP/schema boundary.


def test_normalization_duplicate_detection_and_step_ids_keep_existing_semantics():
    value=draft()
    step_ids={step["id"]:f"S{index+1}" for index,step in enumerate(value["plan"]["steps"])}
    for step in value["plan"]["steps"]:
        step["id"]=step_ids[step["id"]]
        step["depends_on"]=[step_ids[sid] for sid in step["depends_on"]]
    value["design_brief"]["simplification"]["candidates"]=[candidate(id="S1",geometry_evidence=["", "待检查范围尚未定位。"],verification=["", "待执行误差比较。"])]
    validate(value)
    assert value["plan"]["steps"][0]["id"]=="S1"
    assert value["design_brief"]["simplification"]["candidates"][0]["id"]=="s1"
    value["design_brief"]["simplification"]["candidates"].append(candidate(id="s1"))
    with pytest.raises(ValueError) as caught:
        validate(value)
    assert str(caught.value).startswith("简化候选 ID 重复或无效。")
    assert "candidates[1].id" in str(caught.value) and SAFE_ID.pattern in str(caught.value)
