"""HTTP plans retain business data without transporting source-body archives."""
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from advisor.server import make_server, session_view
from test_library_lifecycle_api import request


def plan():
    return {"id": "a" * 32, "revision": 3, "model": {"facts": ["keep exact model"]},
            "plan": {"steps": [{"id": "mesh", "source_ids": ["closed"]}]},
            "questions": [{"id": "q", "label": "keep exact question"}], "answers": {"q": "yes"},
            "sources": [{"id": "open", "title": "Open", "library_id": "open", "text": "OPEN_BODY" * 50000},
                        {"id": "closed", "title": "Historical reference", "library_id": "closed",
                         "text": "CLOSED_PRIVATE_BODY" * 50000, "excerpt": "CLOSED_PRIVATE_EXCERPT",
                         "data": {"code": "CLOSED_PRIVATE_CODE"}}]}


def test_session_transport_preserves_source_identity_and_model_plan_without_body_bypass():
    value = plan()
    before = copy.deepcopy(value)
    kb = SimpleNamespace(source_available=lambda source: source["library_id"] == "open")
    result = session_view(value, kb)
    assert len(json.dumps(result)) < 3000
    assert [source["id"] for source in result["sources"]] == ["open", "closed"]
    assert result["sources"][1]["availability"]["readable"] is False
    assert "CLOSED_PRIVATE" not in json.dumps(result)
    assert all("text" not in source for source in result["sources"])
    for field in ("id", "revision", "model", "plan", "questions", "answers"):
        assert result[field] == value[field]
    assert value == before


@pytest.mark.parametrize("route,body", [("", None), ("/export?format=json", None),
    ("/answers", {"revision": 3, "answers": {}}), ("/confirm", {"revision": 3}),
    ("/restore", {"confirmed": True})])
def test_all_http_plan_read_mutation_and_export_paths_use_source_summaries(route, body):
    value = plan()
    app = SimpleNamespace(kb=SimpleNamespace(source_available=lambda source: source["library_id"] == "open"),
        sessions=SimpleNamespace(get=lambda _sid: value, answers=lambda *_args: value,
                                 confirm=lambda *_args: value, restore=lambda _sid: value))
    server = make_server(app, 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        status, result = request(f"http://127.0.0.1:{server.server_port}", "/api/sessions/" + value["id"] + route, body)
        assert status == 200 and result == session_view(value, app.kb)
        assert "CLOSED_PRIVATE" not in json.dumps(result)
        assert "CLOSED_PRIVATE_BODY" in value["sources"][1]["text"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
