"""Invalid saves retain detailed content and repairs never bypass save gates."""
import copy
import json

import pytest

from advisor.plan_drafts import (
    MAX_DRAFT_BYTES, PlanDraftError, PlanDraftConflictError,
    PlanDraftPermissionError, PlanDraftStore, schema_errors,
)
from advisor.planning_service import PlanningService, SAVE_SCHEMA, tool_definitions
from advisor.sessions import ConflictError, SessionStore
from test_agent_sessions import payload
from test_workflow import FakeKB, FakePlanner, GOAL, SOURCE


@pytest.fixture
def workbench(tmp_path):
    kb = FakeKB()
    sessions = SessionStore(tmp_path, kb, FakePlanner())
    task = sessions.create(GOAL, None)
    tools = PlanningService(tmp_path, sessions, kb)
    return tmp_path, kb, sessions, task, tools


def read(tools, sid):
    tools.call(sid, "read_source", {"id": SOURCE["id"], "offset": 0, "length": 500})


def failed_save(tools, task, args):
    with pytest.raises(PlanDraftError) as caught:
        tools.call_public("save_plan", dict(args, session_id=task["id"]))
    return caught.value


def repair(tools, task, error, patches):
    return tools.call_public("repair_plan_draft", {
        "session_id": task["id"], "draft_id": error.draft_id,
        "draft_revision": error.draft_revision, "patches": patches})


def draft_record(root, task):
    return json.loads((root / "plan-drafts" / (task["id"] + ".json")).read_text(encoding="utf-8"))


def test_incremental_schema_repairs_preserve_original_expressions_and_only_then_commit(workbench):
    root, kb, sessions, task, tools = workbench
    read(tools, task["id"])
    args = payload(task)
    expression = "h_gap = h0 - u_wall; Re = rho*U*Dh/mu; q = -k*d(T,x) [W/m^2]"
    strategy = "p_in(t)=p0*(1-exp(-t/tau)); retain ALE mesh velocity w_m=du_wall/dt"
    args["plan"]["steps"][0]["action"] = expression
    args["design_brief"]["domain_strategy"] = strategy
    title = args["plan"].pop("title")
    args["plan"]["steps"][0]["unexpected_flag"] = True
    args["design_brief"]["candidate_routes"][0]["status"] = "invented"
    args["evidence_gaps"] = "invalid list"
    original = copy.deepcopy(args)

    error = failed_save(tools, task, args)
    assert isinstance(error, ValueError)
    assert {(item["path"], item["code"]) for item in error.errors} == {
        ("/plan/title", "required"), ("/plan/steps/0/unexpected_flag", "additionalProperties"),
        ("/design_brief/candidate_routes/0/status", "enum"), ("/evidence_gaps", "type")}
    assert error.to_dict()["next_action"]["tool"] == "repair_plan_draft"
    assert draft_record(root, task)["payload"] == original
    assert sessions.get(task["id"])["revision"] == 1
    assert len(sessions.versions(task["id"])["versions"]) == 1

    # Reconnection retrieves the same inert draft without a full plan rewrite.
    tools = PlanningService(root, sessions, kb)
    page = tools.call_public("get_plan_draft", {"session_id": task["id"], "draft_id": error.draft_id,
        "path": "/plan/steps/0/action", "length": 12})
    assert len(page["text"]) == 12 and page["next_offset"] == 12
    assert page["draft_revision"] == 1
    with pytest.raises(PlanDraftError) as partial:
        repair(tools, task, error, [{"op": "add", "path": "/plan/title", "value": title},
                                  {"op": "replace", "path": "/evidence_gaps", "value": []}])
    error2 = partial.value
    assert error2.draft_id == error.draft_id and error2.draft_revision == 2
    retained = draft_record(root, task)["payload"]
    assert retained["plan"]["steps"][0]["action"] == expression
    assert retained["plan"]["steps"][0]["unexpected_flag"] is True
    assert retained["design_brief"]["domain_strategy"] == strategy
    assert sessions.get(task["id"])["revision"] == 1

    result = repair(tools, task, error2, [
        {"op": "remove", "path": "/plan/steps/0/unexpected_flag"},
        {"op": "replace", "path": "/design_brief/candidate_routes/0/status", "value": "recommended"}])
    assert result["saved"] and result["revision"] == 2 and result["draft_revision"] == 3
    saved = sessions.get(task["id"])
    assert saved["plan"]["steps"][0]["action"] == expression
    assert saved["design_brief"]["domain_strategy"] == strategy
    assert draft_record(root, task)["status"] == "saved"
    assert tools.read_sources(task["id"]) == [SOURCE["id"]]


def test_schema_error_collection_covers_all_branches_and_constraints():
    schema = {"type": "object", "additionalProperties": False, "required": ["missing"], "properties": {
        "low": {"type": "number", "minimum": 2}, "high": {"type": "integer", "maximum": 3},
        "boolean_integer": {"type": "integer"}, "short": {"type": "array", "minItems": 2, "items": {"type": "string"}},
        "long": {"type": "array", "maxItems": 1, "items": {"type": "string", "enum": ["ok"]}},
        "text": {"type": "string", "pattern": "^[a-z]+$", "maxLength": 2}}}
    errors = schema_errors({"low": 1, "high": 4, "boolean_integer": True, "short": [5],
        "long": ["bad", "worse"], "text": "123", "extra/key": None}, schema)
    assert {(item["path"], item["code"]) for item in errors} == {
        ("/missing", "required"), ("/low", "minimum"), ("/high", "maximum"),
        ("/boolean_integer", "type"), ("/short", "minItems"), ("/short/0", "type"),
        ("/long", "maxItems"), ("/long/0", "enum"), ("/long/1", "enum"),
        ("/text", "maxLength"), ("/text", "pattern"), ("/extra~1key", "additionalProperties")}


def test_failed_patch_batch_is_atomic_and_stale_patch_revision_cannot_overwrite(workbench):
    root, _kb, _sessions, task, tools = workbench
    args = payload(task)
    args["extra"] = {"a/b": {"~key": ["physical expression"]}}
    error = failed_save(tools, task, args)
    with pytest.raises(PlanDraftError):
        repair(tools, task, error, [{"op": "replace", "path": "/plan/title", "value": "should roll back"},
                                  {"op": "remove", "path": "/does-not-exist"}])
    assert draft_record(root, task)["payload"] == args
    assert draft_record(root, task)["draft_revision"] == 1
    with pytest.raises(PlanDraftError) as partial:
        repair(tools, task, error, [{"op": "replace", "path": "/extra/a~1b/~0key/0", "value": "preserved new expression"}])
    current = draft_record(root, task)
    assert current["payload"]["extra"]["a/b"]["~key"][0] == "preserved new expression"
    assert partial.value.draft_revision == 2
    with pytest.raises(PlanDraftConflictError):
        repair(tools, task, error, [{"op": "remove", "path": "/extra"}])
    assert draft_record(root, task) == current


def test_repair_rechecks_live_revision_and_retains_successful_patches_on_conflict(workbench):
    root, _kb, sessions, task, tools = workbench
    read(tools, task["id"])
    args = payload(task)
    title = args["plan"].pop("title")
    error = failed_save(tools, task, args)
    advanced = sessions.save_event(sessions.get(task["id"]), "fixture", "Concurrent update")
    with pytest.raises(ConflictError) as conflict:
        repair(tools, task, error, [{"op": "add", "path": "/plan/title", "value": title}])
    failure = conflict.value
    assert isinstance(failure, PlanDraftConflictError)
    assert failure.to_dict()["errors"][0]["path"] == "/revision"
    assert failure.draft_revision == 2
    assert draft_record(root, task)["payload"]["plan"]["title"] == title
    assert sessions.get(task["id"])["revision"] == advanced["revision"]
    saved = repair(tools, task, failure, [{"op": "replace", "path": "/revision", "value": advanced["revision"]}])
    assert saved["revision"] == advanced["revision"] + 1


def test_draft_read_and_repair_never_authorize_unread_citations(workbench):
    root, _kb, sessions, task, tools = workbench
    args = payload(task)
    title = args["plan"].pop("title")
    error = failed_save(tools, task, args)
    tools.call(task["id"], "get_plan_draft", {"draft_id": error.draft_id})
    assert tools.read_sources(task["id"]) == []
    with pytest.raises(PlanDraftError) as unread:
        repair(tools, task, error, [{"op": "add", "path": "/plan/title", "value": title}])
    assert unread.value.errors[0]["code"] == "semantic"
    assert tools.read_sources(task["id"]) == []
    assert sessions.get(task["id"])["revision"] == 1
    assert draft_record(root, task)["payload"]["plan"]["title"] == title
    # A real source read is still required; no full plan rewrite is needed.
    read(tools, task["id"])
    result = repair(tools, task, unread.value, [{"op": "replace", "path": "/plan/title", "value": title}])
    assert result["saved"]


def test_repair_rechecks_closed_source_and_availability_again_before_commit(workbench, monkeypatch):
    _root, kb, sessions, task, tools = workbench
    read(tools, task["id"])
    args = payload(task)
    title = args["plan"].pop("title")
    error = failed_save(tools, task, args)
    kb.source_available = lambda _source: False
    with pytest.raises(PlanDraftPermissionError) as closed:
        repair(tools, task, error, [{"op": "add", "path": "/plan/title", "value": title}])
    assert isinstance(closed.value, PermissionError)
    assert closed.value.errors[0]["code"] == "permission"
    assert sessions.get(task["id"])["revision"] == 1

    import advisor.planning_service as module
    kb.source_available = lambda _source: True
    original = module.validate_response
    def close_during_validation(*arguments, **kwargs):
        result = original(*arguments, **kwargs)
        kb.source_available = lambda _source: False
        return result
    monkeypatch.setattr(module, "validate_response", close_during_validation)
    with pytest.raises(PlanDraftPermissionError):
        repair(tools, task, closed.value, [{"op": "replace", "path": "/plan/title", "value": title}])
    assert sessions.get(task["id"])["revision"] == 1


def test_old_read_ledger_cannot_authorize_a_source_that_no_longer_exists(workbench):
    _root, kb, sessions, task, tools = workbench
    read(tools, task["id"])
    args = payload(task)
    title = args["plan"].pop("title")
    error = failed_save(tools, task, args)
    kb.get_document = lambda _source_id: None
    with pytest.raises(PlanDraftError) as stale:
        repair(tools, task, error, [{"op": "add", "path": "/plan/title", "value": title}])
    assert stale.value.errors[0]["code"] == "semantic"
    assert tools.read_sources(task["id"]) == [SOURCE["id"]]
    assert sessions.get(task["id"])["revision"] == 1


def test_latest_draft_identity_is_task_owned_and_previous_attempt_cannot_overwrite(workbench):
    root, _kb, sessions, task, tools = workbench
    args = payload(task)
    args["unknown"] = "first detailed expression"
    first = failed_save(tools, task, args)
    args["unknown"] = "second detailed expression"
    second = failed_save(tools, task, args)
    assert first.draft_id != second.draft_id
    assert len(list((root / "plan-drafts").glob("*.json"))) == 1
    with pytest.raises(PlanDraftConflictError) as conflict:
        repair(tools, task, first, [{"op": "remove", "path": "/unknown"}])
    assert conflict.value.draft_id == second.draft_id
    other = sessions.create("Another task", None)
    with pytest.raises(PlanDraftError):
        tools.call(other["id"], "get_plan_draft", {"draft_id": second.draft_id})
    with pytest.raises((PlanDraftError, FileNotFoundError)):
        tools.call(task["id"], "get_plan_draft", {"draft_id": "../../escape"})
    assert draft_record(root, task)["payload"] == args


def test_payload_limit_rejects_new_attempt_without_losing_previous_draft(workbench):
    root, _kb, _sessions, task, tools = workbench
    args = payload(task)
    args["unknown"] = "original expression"
    error = failed_save(tools, task, args)
    previous = draft_record(root, task)
    oversized = payload(task)
    oversized["huge"] = "x" * MAX_DRAFT_BYTES
    with pytest.raises(PlanDraftError) as rejected:
        tools.call(task["id"], "save_plan", oversized)
    assert rejected.value.errors[0]["code"] == "maxBytes"
    assert draft_record(root, task) == previous
    assert error.draft_id == previous["draft_id"]
    with pytest.raises(PlanDraftError) as rejected_patch:
        repair(tools, task, error, [{"op": "replace", "path": "/unknown", "value": "x" * MAX_DRAFT_BYTES}])
    assert rejected_patch.value.draft_id == error.draft_id
    assert draft_record(root, task) == previous


@pytest.mark.parametrize("invalid", [{"key": float("nan")}, {"key": (1, 2)}, {1: "nonstring key"}])
def test_non_json_values_are_never_silently_coerced_or_persisted(workbench, invalid):
    root, _kb, _sessions, task, tools = workbench
    with pytest.raises(PlanDraftError) as rejected:
        tools.call(task["id"], "save_plan", invalid)
    assert rejected.value.errors[0]["code"] == "json"
    assert not (root / "plan-drafts").exists()


def test_draft_read_limits_and_public_schemas_are_backend_neutral(workbench):
    _root, _kb, _sessions, task, tools = workbench
    args = payload(task)
    args["unknown"] = True
    error = failed_save(tools, task, args)
    for kwargs in ({"offset": -1}, {"length": 16001}, {"offset": True}, {"path": "/plan/not-present"}):
        with pytest.raises(PlanDraftError):
            tools.call(task["id"], "get_plan_draft", {"draft_id": error.draft_id, **kwargs})
    public = {tool["name"]: tool for tool in tool_definitions()}
    bound = {tool["name"]: tool for tool in tool_definitions(bound=True)}
    for name in ("get_plan_draft", "repair_plan_draft"):
        assert "session_id" in public[name]["inputSchema"]["required"]
        assert "session_id" not in bound[name]["inputSchema"]["properties"]


def test_nonobject_save_payload_is_retained_before_shape_validation(workbench):
    root, _kb, sessions, task, tools = workbench
    with pytest.raises(PlanDraftError) as invalid:
        tools.call(task["id"], "save_plan", ["q=-k*d(T,x)"])
    assert [{k: e[k] for k in ("path", "code", "message")} for e in invalid.value.errors] == [{"path": "", "code": "type", "message": "Expected object."}]
    assert invalid.value.errors[0]["expected"]["type"] == "object"
    assert draft_record(root, task)["payload"] == ["q=-k*d(T,x)"]
    assert sessions.get(task["id"])["revision"] == 1


def test_store_rejects_symlinked_draft_directory(tmp_path):
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir(); outside.mkdir()
    try:
        (root / "plan-drafts").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating a symbolic link requires local permission")
    store = PlanDraftStore(root)
    with pytest.raises(PermissionError):
        store.capture("a" * 32, {"physical_expression": "q=-k*d(T,x)"})
    assert not list(outside.iterdir())
