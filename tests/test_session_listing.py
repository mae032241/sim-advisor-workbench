"""The plan manager can page all summaries independently of recent status."""
import json
import threading
from types import SimpleNamespace

import pytest

from advisor.server import make_server
from advisor.sessions import SessionStore, write_json
from test_session_deletion_api import request


@pytest.fixture
def listing_server(tmp_path):
    store = SessionStore(tmp_path, None, None)
    for number in range(1, 206):
        sid = f"{number:032x}"
        write_json(store.path(sid), {"id": sid, "title": f"Plan {number}", "status": "draft", "revision": 1,
                                    "updated_at": "2026-11-09T00:00:00+00:00", "goal": "Private detailed goal",
                                    "plan": {"summary": "PRIVATE_PLAN_BODY"}, "sources": [{"text": "PRIVATE_SOURCE_BODY"}]})
    store.delete(f"{205:032x}", 1, confirmed=True)
    (store.directory / ("f" * 32 + ".json")).write_bytes(b"broken record")
    server = make_server(SimpleNamespace(sessions=store), 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield store, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)


def test_all_active_summaries_can_be_paged_with_stable_order_without_private_bodies(listing_server):
    store, base = listing_server
    rows, offset = [], 0
    while offset is not None:
        code, page = request(base, f"/api/sessions?offset={offset}&limit=100")
        assert code == 200 and page["total"] == 204
        assert len(page["sessions"]) <= 100
        assert "PRIVATE_" not in json.dumps(page) and "Private detailed goal" not in json.dumps(page)
        assert all(set(row) == {"id", "title", "status", "revision", "updated_at"} for row in page["sessions"])
        rows.extend(page["sessions"])
        offset = page["next_offset"]
    assert [row["id"] for row in rows] == [f"{number:032x}" for number in range(204, 0, -1)]
    assert len({row["id"] for row in rows}) == 204
    assert len(store.list()) == 100
    assert all(set(row) == {"id", "title", "status", "updated_at"} for row in store.list())
    assert request(base, "/api/sessions")[1]["sessions"] == rows[:100]
    assert len(request(base, "/api/sessions?limit=200")[1]["sessions"]) == 200
    assert request(base, "/api/sessions?offset=1000")[1] == {"sessions": [], "total": 204, "next_offset": None}


@pytest.mark.parametrize("query", ["offset=-1", "offset=bad", "offset=1.5", "limit=0", "limit=-1", "limit=201", "limit=true"])
def test_summary_pagination_rejects_invalid_bounds(listing_server, query):
    _, base = listing_server
    assert request(base, "/api/sessions?" + query)[0] == 422
