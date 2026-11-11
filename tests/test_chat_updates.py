"""Live web feed: completed/new messages only, no historical replay or tokens."""
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from advisor.server import make_server
from test_session_delete_agents import bound_plan


def event(agents, sid, text, identity="reply"):
    agents._event(sid, {"type": "message", "text": text, "item_id": identity})


def test_new_subscription_never_replays_persisted_history_or_streamed_deltas(bound_plan):
    agents, _store, plan, _factory = bound_plan
    sid = plan["id"]
    event(agents, sid, "old reply" * 1000, "old")
    initial = agents.status(sid, after="new")
    assert "messages" not in initial and initial["message_updates"] == []
    cursor = initial["message_cursor"]
    for _ in range(100):
        agents._event(sid, {"type": "text_delta", "text": "partial text", "item_id": "new"})
    assert agents.status(sid, after=cursor)["message_updates"] == []
    event(agents, sid, "completed reply", "new")
    result = agents.status(sid, after=cursor)
    assert len(result["message_updates"]) == 1
    message = result["message_updates"][0]["message"]
    assert message["text"] == "completed reply" and message["id"] == "new"
    assert "old reply" not in json.dumps(result)
    assert agents.status(sid, after=result["message_cursor"])["message_updates"] == []
    assert len(agents.status(sid)["messages"]) == 2  # Legacy diagnostics still retain history.


def test_completed_message_deduplication_retry_and_correction(bound_plan):
    agents, _store, plan, _factory = bound_plan
    sid = plan["id"]
    cursor = agents.chat_updates(sid, "new")["message_cursor"]
    event(agents, sid, "answer")
    once = agents.chat_updates(sid, cursor)
    event(agents, sid, "answer")
    assert agents.chat_updates(sid, cursor) == once
    event(agents, sid, "corrected answer")
    corrected = agents.chat_updates(sid, once["message_cursor"])
    assert len(corrected["message_updates"]) == 1
    assert corrected["message_updates"][0]["message"]["id"] == "reply"
    assert corrected["message_updates"][0]["message"]["text"] == "corrected answer"


def test_cursor_scope_bounded_retention_and_restore_epoch(bound_plan):
    agents, store, plan, _factory = bound_plan
    sid = plan["id"]
    cursor = agents.chat_updates(sid, "new")["message_cursor"]
    for index in range(260):
        event(agents, sid, f"reply {index}", str(index))
    overflow = agents.chat_updates(sid, cursor)
    assert overflow["messages_reset"] and len(overflow["message_updates"]) == 256
    other = store.create("Independent plan feed", None)
    assert agents.chat_updates(other["id"], overflow["message_cursor"])["message_updates"] == []
    assert agents.chat_updates(other["id"], overflow["message_cursor"])["messages_reset"]
    agents.delete_session(sid, plan["revision"], confirmed=True)
    with pytest.raises(FileNotFoundError):
        agents.chat_updates(sid, cursor)
    store.restore(sid)
    restored = agents.chat_updates(sid, overflow["message_cursor"])
    assert restored["messages_reset"] and restored["message_updates"] == []
    assert restored["message_cursor"] != overflow["message_cursor"]


@pytest.mark.parametrize("after", [None, 0, "", "../bad", "a" * 32 + ":-1", "a" * 32 + ":1e6"])
def test_invalid_cursors_are_rejected(bound_plan, after):
    agents, _store, plan, _factory = bound_plan
    with pytest.raises(ValueError, match="游标"):
        agents.chat_updates(plan["id"], after)


def test_http_commands_and_idle_polls_carry_only_new_messages(bound_plan):
    agents, store, plan, _factory = bound_plan
    from types import SimpleNamespace
    server = make_server(SimpleNamespace(agents=agents), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sid = plan["id"]
    base = f"http://127.0.0.1:{server.server_port}/api/sessions/{sid}/agent"
    def request(suffix="", body=None):
        req = Request(base + suffix, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"} if body is not None else {})
        with urlopen(req, timeout=5) as response:
            return json.load(response)
    try:
        event(agents, sid, "OLD_HISTORY_" * 1000)
        subscribed = request("?view=updates&after=new")
        cursor = subscribed["message_cursor"]
        message = request(f"/message?view=updates&after={cursor}", {"text": "New user task"})
        assert "messages" not in message and "OLD_HISTORY_" not in json.dumps(message)
        assert [u["message"]["text"] for u in message["message_updates"]] == ["New user task"]
        assert request("?view=updates&after=" + message["message_cursor"])["message_updates"] == []
        with pytest.raises(HTTPError) as error:
            request("/message?view=updates&after=invalid", {"text": "Must not be dispatched"})
        assert error.value.code == 422
        assert agents.entries[sid]["runtime"].sent == [("New user task", "start")]
        event(agents, sid, "Final visible reply", "final")
        new = request("?view=updates&after=" + message["message_cursor"])
        assert [u["message"]["text"] for u in new["message_updates"]] == ["Final visible reply"]
        assert request()["messages"][0]["text"].startswith("OLD_HISTORY_")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
