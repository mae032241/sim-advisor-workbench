"""Closed libraries cannot grant new knowledge reads or citation authority."""
import copy
import json

import pytest

from advisor.knowledge import source_id
from advisor.library_provider import LibraryKnowledgeProvider
from advisor.planning_service import PlanningService
from advisor.sessions import SessionStore
from test_agent_sessions import payload
from test_library_visibility import LegacySpy, manual_fixture, modern_fixture
from test_workflow import FakeKB, FakePlanner, GOAL, SOURCE


def setup_tools(tmp_path):
    store, _search, _manifest, objects = modern_fixture(tmp_path)
    provider = LibraryKnowledgeProvider(LegacySpy(), store.root)
    sessions = SessionStore(store.root.parent, provider, FakePlanner())
    plan = sessions.create(GOAL, None)
    tools = PlanningService(store.root.parent, sessions, provider)
    return store, provider, sessions, plan, tools, objects


def read(tools, sid, citation):
    return tools.call(sid, "read_source", {"id": citation, "offset": 0, "length": 500})


def plan_args(plan, source):
    args = payload(plan)
    for step in args["plan"]["steps"]:
        step["source_ids"] = [source["id"]]
    return args


@pytest.mark.parametrize("action", ["disable", "delete"])
def test_closed_registered_ids_cannot_be_read_or_added_to_an_agent_ledger(tmp_path, action):
    store, provider, sessions, plan, tools, objects = setup_tools(tmp_path)
    target = objects[0]
    if action == "delete":
        store.delete_library("a")
    else:
        store.set_enabled("a", False)
    assert not provider.source_available(target)
    with pytest.raises(PermissionError):
        read(tools, plan["id"], target["id"])
    assert tools.read_sources(plan["id"]) == []
    assert store.get(target["id"]) == target
    assert sessions.get(plan["id"])["revision"] == 1


def test_source_closing_during_a_page_read_does_not_grant_citation_credit(tmp_path):
    kb = FakeKB()
    sessions = SessionStore(tmp_path, kb, FakePlanner())
    plan = sessions.create(GOAL, None)
    tools = PlanningService(tmp_path, sessions, kb)
    checks = []
    def available(_source):
        checks.append(1)
        return len(checks) == 1
    kb.source_available = available
    with pytest.raises(PermissionError):
        read(tools, plan["id"], SOURCE["id"])
    assert len(checks) == 2
    assert tools.read_sources(plan["id"]) == []


def test_old_read_ledger_cannot_authorize_a_new_full_plan_after_library_close(tmp_path):
    store, _provider, sessions, plan, tools, objects = setup_tools(tmp_path)
    target = objects[0]
    read(tools, plan["id"], target["id"])
    assert tools.read_sources(plan["id"]) == [target["id"]]
    store.set_enabled("a", False)
    with pytest.raises(PermissionError):
        tools.call(plan["id"], "save_plan", plan_args(plan, target))
    assert sessions.get(plan["id"])["revision"] == 1
    assert tools.read_sources(plan["id"]) == [target["id"]], "History is retained but grants no current permission"


def test_context_retains_only_identity_for_closed_pinned_sources_and_patch_keeps_untouched_history(tmp_path):
    store, _provider, sessions, plan, tools, objects = setup_tools(tmp_path)
    closed, opened = objects
    for source in objects:
        read(tools, plan["id"], source["id"])
    args = plan_args(plan, closed)
    args["plan"]["steps"][0]["source_ids"] = [opened["id"]]
    tools.call(plan["id"], "save_plan", args)
    saved = sessions.get(plan["id"])
    store.set_enabled("a", False)
    context = tools.call(plan["id"], "get_task_context", {})
    pinned = next(source for source in context["sources"] if source["id"] == closed["id"])
    assert pinned["availability"]["readable"] is False
    assert not {"text", "excerpt", "data", "coverage"} & set(pinned)
    assert pinned["title"] == closed["title"]
    assert next(source for source in context["sources"] if source["id"] == opened["id"])["availability"]["readable"]
    assert context["plan"] == saved["plan"]
    with pytest.raises(PermissionError):
        tools.call(plan["id"], "save_plan", plan_args(saved, closed))
    changed = copy.deepcopy(saved["plan"]["steps"][0]); changed["difficulty"] += " Updated using an open source."
    result = tools.call(plan["id"], "update_steps", {"revision": saved["revision"], "steps": [changed]})
    current = sessions.get(plan["id"])
    assert result["revision"] == saved["revision"] + 1
    assert current["plan"]["steps"][1:] == saved["plan"]["steps"][1:]
    assert closed in current["sources"], "Historical plan evidence stays archived, not silently deleted"
    forbidden = copy.deepcopy(current["plan"]["steps"][1]); forbidden["difficulty"] += " New edit."
    with pytest.raises(PermissionError):
        tools.call(plan["id"], "update_steps", {"revision": current["revision"], "steps": [forbidden]})


def test_full_plan_rechecks_source_availability_before_commit(tmp_path, monkeypatch):
    import advisor.planning_service as module
    store, _provider, sessions, plan, tools, objects = setup_tools(tmp_path)
    target = objects[0]
    read(tools, plan["id"], target["id"])
    validate = module.validate_response
    def closing_validation(*args, **kwargs):
        result = validate(*args, **kwargs)
        store.set_enabled("a", False)
        return result
    monkeypatch.setattr(module, "validate_response", closing_validation)
    with pytest.raises(PermissionError):
        tools.call(plan["id"], "save_plan", plan_args(plan, target))
    assert sessions.get(plan["id"])["revision"] == 1


def test_legacy_closed_parent_children_links_and_planning_topics_are_withheld(tmp_path):
    kb, store = manual_fixture(tmp_path)
    closed_rel, open_rel = "com.comsol.help.closed_part/closed-0.html", "com.comsol.help.closedXpart/open.html"
    closed, opened = source_id(closed_rel), source_id(open_rel)
    with kb._connect() as db, db:
        db.execute("UPDATE documents SET parent_id=?,links=? WHERE id=?", (closed, json.dumps([
            {"href": "../" + closed_rel, "text": "Closed candidate"}, {"href": "open.html", "text": "Allowed candidate"}]), opened))
        db.execute("UPDATE documents SET parent_id=? WHERE id=?", (opened, closed))
    library = store.register("Closed manuals", "official-manuals", [{"id": "manual", "role": "manuals", "path": str(kb.doc_root / "com.comsol.help.closed_part")}])
    library = store.set_enabled(library["id"], True)
    store.set_enabled(library["id"], False)
    provider = LibraryKnowledgeProvider(kb, store.root)
    document = provider.get_document(opened)
    assert "parent" not in document and document["children"] == []
    assert all("closed_part" not in link["href"] for link in document["related_links"])
    sessions = SessionStore(store.root.parent, provider, FakePlanner()); plan = sessions.create(GOAL, None)
    tools = PlanningService(store.root.parent, sessions, provider)
    page = read(tools, plan["id"], opened)
    assert all(item["id"] != closed for item in page["linked_topics"])
    with pytest.raises(PermissionError):
        read(tools, plan["id"], closed)
    assert closed not in tools.read_sources(plan["id"])


def test_related_targets_and_lightweight_source_checks_obey_current_lifecycle(tmp_path, monkeypatch):
    store, provider, _sessions, _plan, _tools, objects = setup_tools(tmp_path)
    closed, opened = objects
    store.set_enabled("a", False)
    monkeypatch.setattr(provider.store, "related_sources", lambda *_args, **_kwargs: [
        {"id": closed["id"], "title": "Closed target"}, {"id": opened["id"], "title": "Allowed target"}])
    assert [item["id"] for item in provider.related_sources(opened["id"])] == [opened["id"]]
    monkeypatch.setattr(provider.store, "get", lambda *_args: pytest.fail("Permission-only checks must not load full object payloads"))
    assert provider.source_available(opened)
    assert not provider.source_available(closed)


def test_filtered_registered_navigation_keeps_body_provenance_valid_without_authorizing_foreign_content(tmp_path, monkeypatch):
    store, provider, _sessions, _plan, _tools, objects = setup_tools(tmp_path)
    closed, opened = objects
    store.set_enabled("a", False)
    original_get = provider.store.get
    def archived(citation_id):
        value = original_get(citation_id)
        if citation_id == opened["id"]:
            value["children"] = [{"id": closed["id"], "title": "Closed navigation", "excerpt": "Closed content"}]
        return value
    monkeypatch.setattr(provider.store, "get", archived)
    visible = provider.get_document(opened["id"])
    assert visible["children"] == []
    assert provider.validate_source(visible)
    forged = {**visible, "text": "A different body"}
    assert not provider.validate_source(forged)
    assert not provider.validate_source({**visible, "children": archived(opened["id"])["children"][:-1] + [{"id": "invented"}]})


@pytest.mark.parametrize("role", ["models", "attachments", None])
def test_closed_official_manual_roots_block_legacy_search_and_new_reads_for_every_ingested_role(tmp_path, role):
    kb, store = manual_fixture(tmp_path)
    root = {"id": "manual", "path": str(kb.doc_root / "com.comsol.help.closed_part")}
    if role is not None:
        root["role"] = role
    library = store.register("Manuals with ingestion-compatible root role", "official-manuals", [root])
    library = store.set_enabled(library["id"], True)
    if role is None:
        assert library["roots"][0]["role"] == "models", "Registration defaults must not bypass manual lifecycle"
    store.set_enabled(library["id"], False)
    provider = LibraryKnowledgeProvider(kb, store.root)
    closed = source_id("com.comsol.help.closed_part/closed-0.html")
    opened = source_id("com.comsol.help.closedXpart/open.html")
    assert [hit["id"] for hit in provider.search("mesh sizing")] == [opened]
    assert not provider.source_available(closed)
    assert provider.source_available(opened)
    sessions = SessionStore(store.root.parent, provider, FakePlanner()); plan = sessions.create(GOAL, None)
    tools = PlanningService(store.root.parent, sessions, provider)
    with pytest.raises(PermissionError):
        read(tools, plan["id"], closed)
    assert tools.read_sources(plan["id"]) == []
    assert read(tools, plan["id"], opened)["text"]
