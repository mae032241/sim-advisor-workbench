"""Regression coverage for transfers across partial edits and replacement plans."""
import copy
import json

import pytest

from advisor.plan_drafts import PlanDraftError
from advisor.plan_settings import validate_plan_settings
from test_agent_sessions import payload
from test_plan_drafts import workbench, read
from test_plan_settings_service import row
from test_workflow import SOURCE


def saved_plan(workbench):
    _, _, sessions, task, tools = workbench
    read(tools, task["id"])
    args = payload(task)
    original = row(args["plan"]["steps"][0]["id"])
    original["proposed_value"] = "original user-derived value"
    tools.call(task["id"], "save_plan", {**args, "plan_settings": [original]})
    return sessions.get(task["id"])


def context_record(tools, sid):
    return json.loads((tools.directory / (sid + ".json")).read_text(encoding="utf-8"))


def stage(tools, current, setting):
    tools.call(current["id"], "save_plan_settings", {
        "revision": current["revision"], "settings": [setting]})


def changed_step(current):
    step = copy.deepcopy(current["plan"]["steps"][0])
    step["action"] += " Apply the revised current-case setting."
    return step


def replacement(current):
    args = payload(current)
    names = {step["id"]: "next_" + step["id"] for step in args["plan"]["steps"]}
    for step in args["plan"]["steps"]:
        step["id"] = names[step["id"]]
        step["depends_on"] = [names[identity] for identity in step["depends_on"]]
    return args


def json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def sized_row(step_id, identity, size):
    setting = dict(row(step_id), id=identity)
    remaining = size - json_bytes(setting)
    for field in ("requirement", "setting", "proposed_value", "reason", "transfer_rationale", "gap"):
        count = min(remaining, 6000 - len(setting[field]))
        setting[field] += "x" * count
        remaining -= count
    assert remaining == 0 and json_bytes(setting) == size
    return setting


def test_partial_edit_commits_current_pending_table_and_cleans_stage(workbench):
    _, _, sessions, _, tools = workbench
    current = saved_plan(workbench)
    pending = row(current["plan"]["steps"][0]["id"])
    pending["proposed_value"] = "revised user-derived value"
    stage(tools, current, pending)
    assert tools.call(current["id"], "get_plan_settings", {})["pending"] is True

    step = changed_step(current)
    tools.call(current["id"], "update_steps", {"revision": current["revision"], "steps": [step]})

    saved = sessions.get(current["id"])
    assert saved["plan"]["steps"][0] == step
    assert saved["plan_settings"] == [pending]
    assert sessions.version(current["id"], saved["revision"])["plan_settings"] == [pending]
    assert sessions.version(current["id"], current["revision"])["plan_settings"] == current["plan_settings"]
    assert "plan_settings_stage" not in context_record(tools, current["id"])
    page = tools.call(current["id"], "get_plan_settings", {})
    assert page["pending"] is False and page["rows"] == [pending]


def test_partial_edit_rejects_dangling_pending_steps_without_changing_plan_or_stage(workbench):
    _, _, sessions, _, tools = workbench
    current = saved_plan(workbench)
    stage(tools, current, row("absent_step"))
    record = context_record(tools, current["id"])

    with pytest.raises(ValueError):
        tools.call(current["id"], "update_steps", {
            "revision": current["revision"], "steps": [changed_step(current)]})

    assert sessions.get(current["id"]) == current
    assert context_record(tools, current["id"]) == record
    assert tools.call(current["id"], "get_plan_settings", {})["pending"] is True


def test_partial_edit_checks_pending_closed_reference_even_when_changed_step_has_none(workbench, monkeypatch):
    _, kb, sessions, _, tools = workbench
    current = saved_plan(workbench)
    receipt = context_record(tools, current["id"])["read_contexts"][0]
    pending = row(current["plan"]["steps"][0]["id"], [{**receipt, "stated_value": "earlier source method"}])
    stage(tools, current, pending)
    record = context_record(tools, current["id"])
    monkeypatch.setattr(kb, "source_available", lambda _: False, raising=False)
    step = changed_step(current)
    step["source_ids"] = []

    with pytest.raises(PermissionError):
        tools.call(current["id"], "update_steps", {"revision": current["revision"], "steps": [step]})

    assert sessions.get(current["id"]) == current
    assert context_record(tools, current["id"]) == record


def test_partial_edit_cannot_promote_an_unread_source_from_pending_table(workbench, monkeypatch):
    _, kb, sessions, _, tools = workbench
    current = saved_plan(workbench)
    unread = dict(SOURCE, id="manual-unread", title="Available but unread source")
    get_document = kb.get_document
    monkeypatch.setattr(kb, "get_document", lambda identity: copy.deepcopy(unread) if identity == unread["id"] else get_document(identity))
    pending = row(current["plan"]["steps"][0]["id"], [{
        "source_id": unread["id"], "context": "source_text", "locator": "text:0:40",
        "stated_value": "an authored claim is not a read receipt"}])
    stage(tools, current, pending)
    record = context_record(tools, current["id"])

    with pytest.raises(PermissionError):
        tools.call(current["id"], "update_steps", {
            "revision": current["revision"], "steps": [changed_step(current)]})

    assert sessions.get(current["id"]) == current
    assert context_record(tools, current["id"]) == record
    assert unread["id"] not in tools.read_sources(current["id"])


def test_partial_commit_does_not_invent_a_configuration_read_from_authored_refs(workbench):
    _, _, sessions, _, tools = workbench
    current = saved_plan(workbench)
    pending = row(current["plan"]["steps"][0]["id"], [{
        "source_id": SOURCE["id"], "context": "case_configuration",
        "locator": "saved:compact:node/17", "stated_value": "unverified authored value"}])
    stage(tools, current, pending)
    before = context_record(tools, current["id"])
    tools.call(current["id"], "update_steps", {
        "revision": current["revision"], "steps": [changed_step(current)]})

    after = context_record(tools, current["id"])
    assert after["read_sources"] == before["read_sources"]
    assert after["read_contexts"] == before["read_contexts"]
    assert sessions.get(current["id"])["plan_settings"] == [pending]
    review = tools.call(current["id"], "review_plan", {})["settings_review"]
    assert any(item["code"] == "source_context_not_in_read_ledger" for item in review["warnings"])
    assert review["source_value_verification"] == "not_evaluated"


def test_failed_partial_step_validation_leaves_pending_table_available(workbench):
    _, _, sessions, _, tools = workbench
    current = saved_plan(workbench)
    stage(tools, current, row(current["plan"]["steps"][0]["id"]))
    record = context_record(tools, current["id"])
    step = changed_step(current)
    step["depends_on"] = ["missing_dependency"]

    with pytest.raises(ValueError):
        tools.call(current["id"], "update_steps", {"revision": current["revision"], "steps": [step]})

    assert sessions.get(current["id"]) == current
    assert context_record(tools, current["id"]) == record


def test_full_save_rejects_inherited_dangling_rows_and_retains_exact_repair_path(workbench):
    _, _, sessions, _, tools = workbench
    current = saved_plan(workbench)
    args = replacement(current)

    with pytest.raises(PlanDraftError) as caught:
        tools.call(current["id"], "save_plan", args)

    assert sessions.get(current["id"]) == current
    assert any(item["path"].startswith("/plan_settings/0/step_ids") for item in caught.value.errors)
    draft = tools.drafts.load(current["id"], caught.value.draft_id)
    assert draft["status"] == "draft"
    assert draft["payload"]["plan"] == args["plan"]

    repaired = tools.call(current["id"], "repair_plan_draft", {
        "draft_id": caught.value.draft_id, "draft_revision": draft["draft_revision"],
        "patches": [{"op": "replace" if "plan_settings" in draft["payload"] else "add",
                     "path": "/plan_settings", "value": []}]})
    assert repaired["saved"] is True
    assert sessions.get(current["id"])["plan_settings"] == []


def test_explicit_empty_table_clears_saved_and_pending_rows_when_plan_is_replaced(workbench):
    _, _, sessions, _, tools = workbench
    current = saved_plan(workbench)
    stage(tools, current, row("absent_step"))
    args = replacement(current)
    args["plan_settings"] = []

    tools.call(current["id"], "save_plan", args)

    assert sessions.get(current["id"])["plan_settings"] == []
    assert "plan_settings_stage" not in context_record(tools, current["id"])
    page = tools.call(current["id"], "get_plan_settings", {})
    assert page["rows"] == [] and page["pending"] is False


def test_review_uses_same_pending_table_that_the_next_save_will_commit(workbench):
    _, _, _, _, tools = workbench
    current = saved_plan(workbench)
    pending = row("absent_step")
    stage(tools, current, pending)

    review = tools.call(current["id"], "review_plan", {"plan": current["plan"]})["settings_review"]

    assert any(item["code"] == "unknown_setting_step" for item in review["warnings"])
    assert tools.call(current["id"], "get_plan_settings", {})["rows"] == [pending]


def test_explicit_review_table_overrides_pending_table(workbench):
    _, _, _, _, tools = workbench
    current = saved_plan(workbench)
    stage(tools, current, row("pending_absent"))

    review = tools.call(current["id"], "review_plan", {
        "plan": current["plan"], "plan_settings": current["plan_settings"]})["settings_review"]

    assert not any(item["code"] == "unknown_setting_step" for item in review["warnings"])


def test_draft_owned_table_overrides_pending_table_during_review(workbench):
    _, _, _, _, tools = workbench
    current = saved_plan(workbench)
    stage(tools, current, row("pending_absent"))
    args = payload(current)
    args["plan_settings"] = [row("draft_absent")]
    args["plan"].pop("title")
    with pytest.raises(PlanDraftError) as caught:
        tools.call(current["id"], "save_plan", args)

    review = tools.call(current["id"], "review_plan", {
        "draft_id": caught.value.draft_id})["settings_review"]

    missing = [item["step_ids"] for item in review["warnings"] if item["code"] == "unknown_setting_step"]
    assert missing == [["draft_absent"]]


def test_setting_json_byte_limit_accepts_exact_boundary_and_counts_utf8():
    setting = sized_row("configure", "exact_limit", 12 * 1024)
    assert validate_plan_settings([setting]) == [setting]
    setting["gap"] += "海"
    assert json_bytes(setting) == 12 * 1024 + 3
    with pytest.raises(ValueError):
        validate_plan_settings([setting])


def test_large_staged_table_is_rejected_without_overwriting_previous_stage(workbench):
    _, _, sessions, _, tools = workbench
    current = saved_plan(workbench)
    pending = row(current["plan"]["steps"][0]["id"])
    stage(tools, current, pending)
    before = context_record(tools, current["id"])
    large = []
    for index in range(64):
        setting = dict(pending, id="large_" + str(index))
        for field in ("requirement", "setting", "proposed_value", "reason", "transfer_rationale", "gap"):
            setting[field] = "x" * 6000
        large.append(setting)
    assert json_bytes(large) > 2 * 1024 * 1024

    with pytest.raises(ValueError):
        tools.call(current["id"], "save_plan_settings", {
            "revision": current["revision"], "settings": large})

    assert sessions.get(current["id"]) == current
    assert context_record(tools, current["id"]) == before


def test_settings_pagination_adapts_to_byte_budget_without_truncating_rows(workbench):
    _, _, _, _, tools = workbench
    current = saved_plan(workbench)
    rows = [sized_row(current["plan"]["steps"][0]["id"], "large_" + str(index), 9 * 1024)
            for index in range(7)]
    tools.call(current["id"], "save_plan_settings", {
        "revision": current["revision"], "settings": rows})
    collected, offset = [], 0
    while True:
        page = tools.call(current["id"], "get_plan_settings", {"offset": offset, "limit": 8})
        assert json_bytes(page) <= 24 * 1024
        assert page["rows"] == rows[offset:offset + len(page["rows"])]
        assert page["rows"] and len(page["rows"]) <= 2
        collected.extend(page["rows"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + len(page["rows"])
        offset = page["next_offset"]
    assert collected == rows
