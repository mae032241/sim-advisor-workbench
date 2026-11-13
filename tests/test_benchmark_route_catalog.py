import json
from types import SimpleNamespace
from unittest.mock import Mock

from advisor.bounded_knowledge import BoundedKnowledge
from advisor.route_cards import tool_definitions
from scripts import benchmark_route_catalog as pilot
from test_planning_case_tools import case_tools  # noqa: F401


def installed(fixture, tmp_path):
    original = lambda sid, name, args, source_entry=None: fixture.tools.call(sid, name, args)
    app = SimpleNamespace(kb=fixture.provider, sessions=fixture.sessions,
                          agents=SimpleNamespace(runtime_tool=original, _require_current_entry=Mock()))
    cards = SimpleNamespace(list_methods=lambda **args: {"state": "ready" if args["enabled"] else "not_prepared",
                                                       "methods": [], "total": 0},
                            read_method=lambda **args: {"state": "ready" if args["enabled"] else "not_prepared"})
    scopes = fixture.store.searchable_library_ids()
    conditions = {fixture.sid: "existing_navigation"}
    audit = tmp_path / "tools.jsonl"
    definitions = pilot.install_tools(app, cards,
        tool_definitions() + BoundedKnowledge(lambda *_: {}).tool_definitions(),
        conditions, scopes, {fixture.sid: audit})
    return app, conditions, audit, definitions


def test_same_tools_both_conditions_only_cards_differ(case_tools, tmp_path):
    app, conditions, audit, definitions = installed(case_tools, tmp_path)
    assert {"read_tool_page", "list_route_methods", "read_method_card"}.issubset({tool["name"] for tool in definitions})
    assert "get_case_method" not in {tool["name"] for tool in definitions}
    call = app.agents.runtime_tool
    assert call(case_tools.sid, "list_route_methods", {})["state"] == "not_prepared"
    conditions[case_tools.sid] = "case_methods"
    assert call(case_tools.sid, "list_route_methods", {})["state"] == "ready"
    assert max(json.loads(line)["result_json_bytes"] for line in audit.read_text(encoding="utf-8").splitlines()) <= 7000


def test_raw_source_read_is_bounded_and_no_plan_write(case_tools, tmp_path):
    app, _, _, _ = installed(case_tools, tmp_path)
    response = app.agents.runtime_tool(case_tools.sid, "read_source", {"id": case_tools.overview["id"]})
    assert len(json.dumps(response, ensure_ascii=False).encode("utf-8")) <= 7000
    denied = app.agents.runtime_tool(case_tools.sid, "save_plan", {})
    assert denied["success"] is False
    assert app.agents.runtime_tool(case_tools.sid, "get_case_method", {"case_id": case_tools.overview["id"]})["success"] is False


def test_compatible_profile_requires_single_small_tool_call():
    assert pilot.NATIVE_PROFILE["features.code_mode"] is True
    assert pilot.NATIVE_PROFILE["features.code_mode_host"] is True
    assert "每个exec单元只能调用一次业务工具" in pilot.PAGING_INSTRUCTIONS
    assert "Hphi" not in pilot.MESSAGE and "Kirchhoff" not in pilot.MESSAGE
