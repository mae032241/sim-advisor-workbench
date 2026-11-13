"""Implementation links retain authored text without evaluating its physics."""
import copy
import json

import pytest

from advisor.plan_drafts import PlanDraftError
from advisor.plan_settings import (PLAN_SETTING_SCHEMA, review_plan_settings,
                                   validate_plan_settings)
from advisor.planning_service import PlanningService
from test_agent_sessions import payload
from test_plan_drafts import workbench, read
from test_plan_settings_service import row
from test_workflow import SOURCE


CHECK_ID = "common.variable_definitions"
VALUE = "q_wall=Q_in/A_heat [W/m²]; scope=heater_faces"


def binding(step_id="configure", field="action"):
    return dict(row(step_id), proposed_value=VALUE, check_ids=[CHECK_ID],
                implementation={"step_id": step_id, "field": field})


def implementation_plan(field="action", *, include_value=True):
    step = {"id": "configure", "action": "Configure the selected boundary.",
            "inputs": ["Current supplied constants"], "checks": ["Confirm the boundary selection"]}
    if include_value:
        if field == "action":
            step[field] += " Use " + VALUE + "."
        else:
            step[field].append("Use " + VALUE + ".")
    return {"steps": [step]}


def codes(result):
    return {item["code"] for item in result["warnings"]}


def context_record(tools, sid):
    return json.loads((tools.directory / (sid + ".json")).read_text(encoding="utf-8"))


def save_bound(workbench, field="action"):
    _, _, sessions, task, tools = workbench
    read(tools, task["id"])
    args = payload(task)
    step = args["plan"]["steps"][0]
    setting = binding(step["id"], field)
    if field == "action":
        step[field] += " Use " + VALUE + "."
    else:
        step[field].append("Use " + VALUE + ".")
    args["plan_settings"] = [setting]
    tools.call(task["id"], "save_plan", args)
    return sessions.get(task["id"])


def test_implementation_fields_are_optional_and_validation_retains_owned_copies():
    legacy = row("configure")
    authored = binding()
    inputs = [legacy, dict(authored, id="bound-setting")]
    before = copy.deepcopy(inputs)
    validated = validate_plan_settings(inputs)
    assert validated == before and inputs == before
    assert "check_ids" not in PLAN_SETTING_SCHEMA["required"]
    assert "implementation" not in PLAN_SETTING_SCHEMA["required"]
    assert PLAN_SETTING_SCHEMA["properties"]["check_ids"]["maxItems"] == 16
    validated[1]["check_ids"].clear()
    validated[1]["implementation"]["field"] = "checks"
    assert inputs == before


@pytest.mark.parametrize("change", [
    {"check_ids": CHECK_ID}, {"check_ids": [False]},
    {"check_ids": ["invented.check"]}, {"check_ids": [CHECK_ID] * 17},
    {"implementation": []}, {"implementation": {"step_id": "configure"}},
    {"implementation": {"step_id": " ", "field": "action"}},
    {"implementation": {"step_id": "configure", "field": "summary"}},
    {"implementation": {"step_id": "configure", "field": "action", "quote": VALUE}},
])
def test_malformed_optional_bindings_are_rejected_without_mutation(change):
    setting = dict(binding(), **change)
    before = copy.deepcopy(setting)
    with pytest.raises(ValueError, match="plan_settings"):
        validate_plan_settings([setting])
    assert setting == before


@pytest.mark.parametrize("status", ["adopted", "adapted", "alternative"])
def test_mechanism_linked_active_rows_need_a_binding_even_when_text_is_elsewhere(status):
    setting = binding()
    setting.pop("implementation")
    setting["status"] = status
    result = review_plan_settings([setting], plan=implementation_plan())
    assert "implementation_binding_missing" in codes(result)
    assert result["blocking"] is False
    assert result["physical_correctness"] == "not_evaluated"


@pytest.mark.parametrize("field", ["action", "inputs", "checks"])
def test_literal_binding_accepts_each_allowed_field_without_claiming_value_verification(field):
    setting, plan = binding(field=field), implementation_plan(field)
    before = copy.deepcopy((setting, plan))
    result = review_plan_settings([setting], plan=plan)
    assert not {"implementation_binding_missing", "implementation_value_missing",
                "implementation_step_unmapped"} & codes(result)
    assert result["physical_correctness"] == "not_evaluated"
    assert result["source_value_verification"] == "not_evaluated"
    assert result["plan_settings"][0]["proposed_value"] == VALUE
    assert (setting, plan) == before


@pytest.mark.parametrize("location", ["summary", "source_value", "different_field", "split_items", "normalized"])
def test_only_the_declared_field_and_one_literal_item_can_satisfy_a_binding(location):
    setting = binding(field="inputs" if location == "split_items" else "action")
    plan = implementation_plan(include_value=False)
    step = plan["steps"][0]
    if location == "summary":
        plan["summary"] = VALUE
    elif location == "source_value":
        setting["source_refs"] = [{"source_id": SOURCE["id"], "context": "source_text",
                                   "locator": "text:0:500", "stated_value": VALUE}]
    elif location == "different_field":
        step["inputs"].append(VALUE)
    elif location == "split_items":
        split = len(VALUE) // 2
        step["inputs"] = [VALUE[:split], VALUE[split:]]
    else:
        step["action"] += " " + VALUE.replace("m²", "m^2")
    result = review_plan_settings([setting], plan=plan)
    assert "implementation_value_missing" in codes(result)


def test_binding_must_target_a_mapped_step_even_if_another_step_contains_the_value():
    setting, plan = binding(), implementation_plan()
    setting["implementation"]["step_id"] = "elsewhere"
    plan["steps"].append(dict(plan["steps"][0], id="elsewhere"))
    result = review_plan_settings([setting], plan=plan)
    assert "implementation_step_unmapped" in codes(result)


@pytest.mark.parametrize("status", ["unresolved", "not_applicable"])
def test_explicit_gap_rows_keep_mechanism_links_without_inventing_implementation(status):
    setting = binding()
    setting.pop("implementation")
    setting.update(status=status, proposed_value="", step_ids=[],
                   gap="Boundary area is still unavailable.",
                   reason="Keep this decision open until the required input is supplied.")
    result = review_plan_settings([setting], plan=implementation_plan(include_value=False))
    assert "implementation_binding_missing" not in codes(result)
    assert result["plan_settings"] == [setting]
    assert result["gaps"]


@pytest.mark.parametrize("missing", ["pointer", "literal", "legacy_pointer_literal"])
def test_full_save_rejects_unimplemented_settings_with_a_repairable_settings_path(workbench, missing):
    _, _, sessions, task, tools = workbench
    read(tools, task["id"])
    args = payload(task)
    setting = binding(args["plan"]["steps"][0]["id"])
    if missing == "pointer":
        setting.pop("implementation")
    elif missing == "legacy_pointer_literal":
        setting.pop("check_ids")
    args["plan_settings"] = [setting]
    before = copy.deepcopy((args, sessions.get(task["id"]), sessions.versions(task["id"])))
    with pytest.raises(PlanDraftError) as caught:
        tools.call_public("save_plan", dict(args, session_id=task["id"]))
    assert any(item["code"] == "semantic" and item["path"].startswith("/plan_settings")
               for item in caught.value.errors)
    assert args == before[0]
    assert sessions.get(task["id"]) == before[1]
    assert sessions.versions(task["id"]) == before[2]


def test_repairing_only_the_step_literal_commits_the_retained_pending_binding(workbench):
    _, _, sessions, task, tools = workbench
    read(tools, task["id"])
    args = payload(task)
    setting = binding(args["plan"]["steps"][0]["id"])
    tools.call(task["id"], "save_plan_settings", {"revision": 1, "settings": [setting]})
    record = context_record(tools, task["id"])
    with pytest.raises(PlanDraftError) as caught:
        tools.call(task["id"], "save_plan", args)
    assert sessions.get(task["id"]) == task
    assert context_record(tools, task["id"]) == record
    action = args["plan"]["steps"][0]["action"] + " Use " + VALUE + "."
    result = tools.call(task["id"], "repair_plan_draft", {
        "draft_id": caught.value.draft_id, "draft_revision": caught.value.draft_revision,
        "patches": [{"op": "replace", "path": "/plan/steps/0/action", "value": action}]})
    saved = sessions.get(task["id"])
    assert result["saved"] and saved["revision"] == 2
    assert saved["plan_settings"] == [setting]
    assert saved["plan"]["steps"][0]["action"] == action
    assert "plan_settings_stage" not in context_record(tools, task["id"])


def test_partial_edit_without_a_pending_table_cannot_remove_saved_literal_atomically(workbench):
    _, _, sessions, _, tools = workbench
    current = save_bound(workbench, "checks")
    sid = current["id"]
    assert tools.call(sid, "get_plan_settings", {})["pending"] is False
    before = copy.deepcopy((current, sessions.versions(sid), context_record(tools, sid)))
    step = copy.deepcopy(current["plan"]["steps"][0])
    step["checks"] = [item for item in step["checks"] if VALUE not in item]
    with pytest.raises(ValueError, match="plan_settings"):
        tools.call(sid, "update_steps", {"revision": current["revision"], "steps": [step]})
    assert sessions.get(sid) == before[0]
    assert sessions.versions(sid) == before[1]
    assert context_record(tools, sid) == before[2]
    step = copy.deepcopy(current["plan"]["steps"][0])
    step["difficulty"] += " Confirm the implementation location."
    tools.call(sid, "update_steps", {"revision": current["revision"], "steps": [step]})
    assert sessions.get(sid)["plan_settings"] == current["plan_settings"]
    assert sessions.version(sid, current["revision"])["plan_settings"] == current["plan_settings"]


def test_old_tables_remain_editable_across_reconnection_history_and_restore(workbench):
    root, kb, sessions, task, tools = workbench
    read(tools, task["id"])
    args = payload(task)
    legacy = row(args["plan"]["steps"][0]["id"])
    args["plan_settings"] = [legacy]
    tools.call(task["id"], "save_plan", args)
    tools = PlanningService(root, sessions, kb)
    page = tools.call(task["id"], "get_plan_settings", {})
    assert page["rows"] == [legacy] and page["pending"] is False
    current = sessions.get(task["id"])
    step = copy.deepcopy(current["plan"]["steps"][0])
    step["action"] += " Document the legacy decision."
    tools.call(task["id"], "update_steps", {"revision": 2, "steps": [step]})
    assert sessions.get(task["id"])["plan_settings"] == [legacy]
    assert sessions.version(task["id"], 2)["plan_settings"] == [legacy]
    sessions.restore_version(task["id"], 2, 3, confirmed=True)
    restored = sessions.get(task["id"])
    assert restored["revision"] == 4 and restored["plan_settings"] == [legacy]
    review = tools.call(task["id"], "review_plan", {})["settings_review"]
    assert review["physical_correctness"] == "not_evaluated"
