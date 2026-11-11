"""Public delete/restore boundaries preserve sources and unrelated sessions."""
import json
from types import SimpleNamespace
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock

import pytest

from advisor.server import make_server
from advisor.sessions import ConflictError, SessionStore


@pytest.fixture
def deletion_server(tmp_path):
    store = SessionStore(tmp_path, None, None)
    source = tmp_path / "original.mph"
    source.write_bytes(b"untouched original model")
    marker = tmp_path / "libraries" / "marker.txt"
    marker.parent.mkdir(); marker.write_text("keep", encoding="utf-8")
    first = store.create("Compare heat transfer in the original model", {"path": str(source)})
    other = store.create("Keep this independent simulation proposal", None)
    app = SimpleNamespace(sessions=store)
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield store, app, f"http://127.0.0.1:{server.server_port}", first, other, source, marker
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def request(base, route, body=None):
    req = Request(base + route, data=json.dumps(body).encode() if body is not None else None,
                  headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def test_delete_confirm_revision_restore_and_other_data_preservation(deletion_server):
    store, _, base, first, other, source, marker = deletion_server
    route = f"/api/sessions/{first['id']}"
    assert request(base, route + "/delete", {"revision": first["revision"]})[0] == 422
    assert request(base, route + "/delete", {"revision": first["revision"], "confirmed": "true"})[0] == 422
    assert request(base, route + "/delete", {"revision": 0, "confirmed": True})[0] == 409
    assert request(base, route + "/delete", {"revision": first["revision"], "confirmed": True})[0] == 200
    assert request(base, route)[0] == 404
    assert request(base, route + "/export?format=json")[0] == 404
    assert [s["id"] for s in store.list()] == [other["id"]]
    trash = request(base, "/api/sessions/deleted")[1]["sessions"]
    assert [s["id"] for s in trash] == [first["id"]]
    assert request(base, route + "/restore", {})[0] == 422
    code, restored = request(base, route + "/restore", {"confirmed": True})
    assert code == 200 and restored["id"] == first["id"] and restored["revision"] > first["revision"]
    assert request(base, "/api/sessions/deleted")[1]["sessions"] == []
    assert store.get(other["id"]) == other
    assert source.read_bytes() == b"untouched original model" and marker.read_text() == "keep"


def test_agent_guard_conflict_reaches_http_without_deleting(deletion_server):
    store, app, base, first, *_ = deletion_server
    agent = Mock(); agent.delete_session.side_effect = ConflictError("请先断开 Agent")
    app.agents = agent
    code, result = request(base, f"/api/sessions/{first['id']}/delete", {"revision": first["revision"], "confirmed": True})
    assert code == 409 and "断开" in result["error"]
    agent.delete_session.assert_called_once_with(first["id"], first["revision"], confirmed=True)
    assert store.get(first["id"])["id"] == first["id"]


def test_invalid_session_id_never_reaches_store(deletion_server):
    _, _, base, first, *_ = deletion_server
    assert request(base, "/api/sessions/not-an-id/delete", {"revision": 1, "confirmed": True})[0] == 404


def test_purge_contract_requires_deleted_plan_exact_confirmation_and_revision(deletion_server):
    store, app, base, first, other, source, marker = deletion_server
    sid = first["id"]
    route = f"/api/sessions/{sid}/purge"
    body = {"revision": first["revision"], "confirmed": True}
    assert request(base, route, body)[0] == 409
    current = store.save_event(store.get(sid), "edited", "Updated plan inputs")
    store.delete(sid, current["revision"], confirmed=True)
    for confirmation in (None, False, 1, "true"):
        assert request(base, route, {"revision": current["revision"], "confirmed": confirmation})[0] == 422
    for revision in (None, True, "2", first["revision"], current["revision"] + 1):
        assert request(base, route, {"revision": revision, "confirmed": True})[0] == 409
    assert request(base, route, {"revision": current["revision"], "confirmed": True, "path": str(source)})[0] == 422
    assert request(base, route)[0] == 404
    # Purge can only target a plan that already passed deletion's Agent guard;
    # it does not reconnect to or terminate a conversation to remove a record.
    app.agents = Mock()
    code, result = request(base, route, {"revision": current["revision"], "confirmed": True})
    assert code == 200 and result == {"id": sid, "status": "purged"}
    assert not app.agents.mock_calls
    assert request(base, route, body)[0] == 404
    assert request(base, f"/api/sessions/{sid}/restore", {"confirmed": True})[0] == 404
    assert request(base, "/api/sessions/deleted")[1]["sessions"] == []
    assert not store._version_path(sid, 1).exists()
    assert store.get(other["id"]) == other
    assert source.read_bytes() == b"untouched original model" and marker.read_text() == "keep"


def test_restored_plan_invalidates_previous_purge_request(deletion_server):
    store, _, base, first, *_ = deletion_server
    sid = first["id"]
    route = f"/api/sessions/{sid}"
    store.delete(sid, first["revision"], confirmed=True)
    restored = store.restore(sid)
    body = {"revision": first["revision"], "confirmed": True}
    assert request(base, route + "/purge", body)[0] == 409
    store.delete(sid, restored["revision"], confirmed=True)
    assert request(base, route + "/purge", body)[0] == 409
    assert store.deleted_list()[0]["revision"] == restored["revision"]
    assert request(base, route + "/purge", {"revision": restored["revision"], "confirmed": True})[0] == 200


def test_invalid_purge_session_id_never_reaches_store(deletion_server):
    store, _, base, *_ = deletion_server
    store.purge = Mock(side_effect=AssertionError("Invalid ID reached purge"))
    for invalid in ("not-an-id", "A" * 32, "b" * 31, "..%2Foutside"):
        assert request(base, f"/api/sessions/{invalid}/purge", {"revision": 1, "confirmed": True})[0] == 404
    store.purge.assert_not_called()
