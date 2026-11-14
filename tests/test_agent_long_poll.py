import threading
import time
from types import SimpleNamespace

import pytest

from advisor.agent_sessions import AgentSessions
from advisor.sessions import SessionStore


@pytest.fixture
def plan(tmp_path):
    store = SessionStore(tmp_path / "data", SimpleNamespace(), None)
    created = store.create("long poll probe", None)
    agents = AgentSessions(tmp_path / "data", store, SimpleNamespace())
    agents.WAIT_RECHECK = 0.05
    try:
        yield agents, store, created["id"]
    finally:
        agents.close()


def _later(delay, action):
    thread = threading.Timer(delay, action)
    thread.start()
    return thread


def test_a_stale_token_answers_at_once(plan):
    agents, _store, sid = plan
    started = time.monotonic()
    result = agents.wait_status(sid, after="new", known="0" * 24, wait=5)
    assert time.monotonic() - started < 1
    assert len(result["state_token"]) == 24


def test_a_current_token_waits_until_the_deadline_when_nothing_changes(plan):
    agents, _store, sid = plan
    first = agents.wait_status(sid, after="new")
    started = time.monotonic()
    again = agents.wait_status(sid, after="new", known=first["state_token"], wait=0.4)
    assert time.monotonic() - started >= 0.35
    assert again["state_token"] == first["state_token"]


def test_a_new_chat_message_wakes_a_waiting_request(plan):
    agents, _store, sid = plan
    agents.WAIT_RECHECK = 10  # only the wakeup, not the periodic recheck, can answer in time
    first = agents.wait_status(sid, after="new")
    cursor = first["message_cursor"]
    entry = agents.entries[sid]

    def reply():
        with agents.lock:
            entry["record"]["messages"].append({"role": "assistant", "text": "完成", "at": "now"})
            agents._completed_message(entry, entry["record"]["messages"][-1])
            agents._persist(sid, entry)

    _later(0.2, reply)
    started = time.monotonic()
    result = agents.wait_status(sid, after=cursor, known=first["state_token"], wait=10)
    assert time.monotonic() - started < 2
    assert [update["message"]["text"] for update in result["message_updates"]] == ["完成"]
    assert result["state_token"] != first["state_token"]


def test_a_saved_plan_revision_wakes_a_waiting_request(plan):
    agents, store, sid = plan
    agents.WAIT_RECHECK = 10  # only the store listener, not the periodic recheck, can answer in time
    first = agents.wait_status(sid, after="new")

    def save():
        value = store.get(sid)
        value["revision"] += 1
        store._write(value)

    _later(0.2, save)
    started = time.monotonic()
    result = agents.wait_status(sid, after=first["message_cursor"], known=first["state_token"], wait=10)
    assert time.monotonic() - started < 2
    assert result["plan_revision"] == first["plan_revision"] + 1


def test_heartbeats_do_not_change_the_token():
    base = {"status": "idle", "external": {"registered": True, "last_seen": 1.0}}
    assert AgentSessions.state_token(base) == AgentSessions.state_token({**base, "external": {"registered": True, "last_seen": 99.0}})
    assert AgentSessions.state_token(base) != AgentSessions.state_token({**base, "status": "running"})


def test_closing_releases_waiters(plan):
    agents, _store, sid = plan
    first = agents.wait_status(sid, after="new")
    agents.WAIT_RECHECK = 10
    _later(0.2, agents.close)
    started = time.monotonic()
    agents.wait_status(sid, after="new", known=first["state_token"], wait=20)
    assert time.monotonic() - started < 2
