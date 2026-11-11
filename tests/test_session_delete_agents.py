"""Deletion gates for connected Agents and callbacks from deleted incarnations."""
from __future__ import annotations

import threading
from unittest.mock import Mock

import pytest

from advisor.agent_sessions import AgentSessions
from advisor.sessions import ConflictError, SessionStore
from test_agent_sessions import FakeRuntime
from test_workflow import FakeKB, FakePlanner, GOAL


@pytest.fixture
def bound_plan(tmp_path):
    kb = FakeKB()
    store = SessionStore(tmp_path, kb, FakePlanner())
    plan = store.create(GOAL, None)
    factory = Mock(side_effect=lambda **kwargs: FakeRuntime(**kwargs))
    factory.backend_id = "custom"
    factory.status.return_value = {"available": True, "mode": "fixture"}
    agents = AgentSessions(tmp_path, store, kb, runtime_factory=factory)
    # Tests drive monitor iterations deterministically rather than wait 5s.
    agents.stopping.set()
    agents.monitor.join(1)
    agents.external.process_alive = lambda _pid: True
    yield agents, store, plan, factory
    agents.close()


@pytest.mark.parametrize("busy", [False, True])
def test_connected_managed_runtime_blocks_delete_even_when_idle(bound_plan, busy):
    agents, store, plan, _factory = bound_plan
    sid = plan["id"]
    agents.connect(sid)
    runtime = agents.entries[sid]["runtime"]
    runtime.busy = busy
    runtime.close = Mock(wraps=runtime.close)
    with pytest.raises(ConflictError, match="先断开"):
        agents.delete_session(sid, plan["revision"], confirmed=True)
    assert store.get(sid)["revision"] == plan["revision"]
    assert runtime.running
    runtime.close.assert_not_called()


@pytest.mark.parametrize("condition", ["idle", "running", "expired", "dead_pid"])
def test_registered_external_binding_blocks_delete_even_when_not_alive(bound_plan, condition):
    agents, store, plan, _factory = bound_plan
    sid = plan["id"]
    clock = [1000.0]
    agents.external.clock = lambda: clock[0]
    binding = agents.register_external(sid, {"name": "External fixture", "pid": 101})
    if condition == "running":
        agents.publish_external(sid, {"binding_id": binding["binding_id"],
            "event": {"type": "state", "status": "running", "turn_id": "turn-1"}})
    elif condition == "expired":
        clock[0] += agents.external.TTL + 1
    elif condition == "dead_pid":
        agents.external.process_alive = lambda _pid: False
    status = agents.external.status(sid)
    assert status["registered"]
    if condition in {"expired", "dead_pid"}:
        assert not status["alive"]
    with pytest.raises(ConflictError, match="先解除"):
        agents.delete_session(sid, plan["revision"], confirmed=True)
    assert store.get(sid)["id"] == sid
    assert agents.external.status(sid)["registered"]


def test_delete_requires_confirmation_and_current_revision(bound_plan):
    agents, store, plan, _factory = bound_plan
    sid = plan["id"]
    with pytest.raises(ValueError):
        agents.delete_session(sid, plan["revision"])
    with pytest.raises(ConflictError):
        agents.delete_session(sid, plan["revision"] + 1, confirmed=True)
    assert store.get(sid)["revision"] == plan["revision"]


def test_external_disconnect_then_delete_preserves_process_and_revokes_old_binding_on_restore(bound_plan):
    agents, store, plan, factory = bound_plan
    sid = plan["id"]
    binding = agents.register_external(sid, {"name": "External fixture", "pid": 101})
    agents.disconnect(sid)
    external_path = agents.external.directory / (sid + ".json")
    history = external_path.read_bytes()
    result = agents.delete_session(sid, plan["revision"], confirmed=True)
    assert result["id"] == sid and result["status"] == "deleted"
    assert external_path.read_bytes() == history
    factory.assert_not_called()
    with pytest.raises(FileNotFoundError):
        store.get(sid)
    with pytest.raises(FileNotFoundError):
        agents.poll_external(sid, {"binding_id": binding["binding_id"]})
    restored = store.restore(sid)
    assert restored["revision"] > plan["revision"]
    assert not agents.poll_external(sid, {"binding_id": binding["binding_id"]})["connected"]
    assert not agents.publish_external(sid, {"binding_id": binding["binding_id"],
        "event": {"type": "message", "message_id": "late", "text": "Old process reply"}})["accepted"]
    with pytest.raises(PermissionError):
        agents.external_tool(sid, {"binding_id": binding["binding_id"],
            "name": "get_task_context", "arguments": {}})
    replacement = agents.register_external(sid, {"name": "New explicit registration", "pid": 101})
    assert replacement["binding_id"] != binding["binding_id"]


@pytest.mark.parametrize("operation", ["connect", "message", "register_external", "external_tool"])
@pytest.mark.parametrize("restore", [False, True])
def test_command_that_already_obtained_entry_cannot_restart_deleted_plan(bound_plan, monkeypatch, operation, restore):
    agents, store, plan, factory = bound_plan
    sid = plan["id"]
    agents._entry(sid)
    entered, release = threading.Event(), threading.Event()
    original_entry = agents._entry
    failures = []

    def delayed_entry(value):
        entry = original_entry(value)
        if threading.current_thread().name == "pending-plan-command":
            entered.set()
            assert release.wait(3), "test did not release pending command"
        return entry

    def pending():
        try:
            if operation == "connect":
                agents.connect(sid)
            elif operation == "message":
                agents.message(sid, "A queued message")
            elif operation == "register_external":
                agents.register_external(sid, {"name": "Queued registration", "pid": 101})
            else:
                agents.external_tool(sid, {"binding_id": "old-token", "name": "get_task_context", "arguments": {}})
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(agents, "_entry", delayed_entry)
    thread = threading.Thread(target=pending, name="pending-plan-command")
    thread.start()
    try:
        assert entered.wait(3)
        agents.delete_session(sid, plan["revision"], confirmed=True)
        if restore:
            store.restore(sid)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], (FileNotFoundError, ConflictError))
    factory.assert_not_called()
    if restore:
        assert not agents.external.status(sid)["registered"]
        # A new explicit request uses the restored incarnation normally.
        assert not agents.status(sid)["connected"]
    else:
        with pytest.raises(FileNotFoundError):
            store.get(sid)


def test_old_managed_callbacks_are_ignored_after_delete_and_restore(bound_plan):
    agents, store, plan, factory = bound_plan
    sid = plan["id"]
    agents.connect(sid)
    runtime = agents.entries[sid]["runtime"]
    agents.disconnect(sid)
    chat_path = agents.directory / (sid + ".json")
    chat_before = chat_path.read_bytes()
    agents.delete_session(sid, plan["revision"], confirmed=True)
    for restore in (False, True):
        if restore:
            store.restore(sid)
        runtime.kwargs["on_event"]({"type": "message", "text": "Late assistant", "item_id": "late"})
        runtime.kwargs["on_event"]({"type": "error", "message": "Late error"})
        runtime.kwargs["on_event"]({"type": "turn", "event": "completed"})
        result = runtime.kwargs["on_tool"]("get_task_context", {})
        assert result["success"] is False
        assert chat_path.read_bytes() == chat_before
    assert factory.call_count == 1
    assert store.get(sid)["revision"] == plan["revision"] + 1


def test_late_direct_event_does_not_create_deleted_entry(bound_plan):
    agents, _store, plan, _factory = bound_plan
    sid = plan["id"]
    agents.delete_session(sid, plan["revision"], confirmed=True)
    agents._event(sid, {"type": "message", "text": "Late external reply"})
    assert sid not in agents.entries


def test_monitor_continues_past_deleted_session_without_persisting_it(bound_plan, monkeypatch):
    agents, store, plan, _factory = bound_plan
    sid = plan["id"]
    agents.connect(sid)
    agents.disconnect(sid)
    store.delete(sid, plan["revision"], confirmed=True)
    other = store.create("Another independent simulation plan", None)
    agents.connect(other["id"])
    dead_runtime = agents.entries[other["id"]]["runtime"]
    agents.disconnect(other["id"])
    agents.entries[other["id"]]["runtime"] = dead_runtime

    class OneIteration:
        calls = 0
        def wait(self, _timeout):
            self.calls += 1
            return self.calls > 1
        def set(self):
            self.calls = 2

    persisted = []
    monkeypatch.setattr(agents, "stopping", OneIteration())
    monkeypatch.setattr(agents, "_persist", lambda value, _entry: persisted.append(value))
    agents._monitor()
    assert persisted == [other["id"]]


def test_managed_delta_stream_does_not_read_or_write_full_plan_per_token(bound_plan, monkeypatch):
    agents, store, plan, _factory = bound_plan
    sid = plan["id"]
    agents.connect(sid)
    entry = agents.entries[sid]
    callback = entry["runtime"].kwargs["on_event"]
    read_plan = Mock(wraps=store.get)
    persist = Mock(wraps=agents._persist)
    monkeypatch.setattr(store, "get", read_plan)
    monkeypatch.setattr(agents, "_persist", persist)
    for _ in range(200):
        callback({"type": "text_delta", "text": "x", "item_id": "streamed-message"})
    callback({"type": "tool", "name": "search_knowledge"})
    read_plan.assert_not_called()
    persist.assert_not_called()
    assert entry["record"]["messages"][-1]["text"] == "x" * 200
    callback({"type": "message", "text": "Completed reply", "item_id": "streamed-message"})
    assert read_plan.call_count == 1
    assert persist.call_count == 1


def test_external_delta_stream_adds_no_plan_reads_beyond_bridge_validation(bound_plan, monkeypatch):
    agents, store, plan, _factory = bound_plan
    sid = plan["id"]
    binding = agents.register_external(sid, {"name": "Streaming external fixture", "pid": 101})
    entry = agents.entries[sid]
    read_plan = Mock(wraps=store.get)
    lookup_entry = Mock(wraps=agents._entry)
    persist_chat = Mock(wraps=agents._persist)
    monkeypatch.setattr(store, "get", read_plan)
    monkeypatch.setattr(agents, "_entry", lookup_entry)
    monkeypatch.setattr(agents, "_persist", persist_chat)
    event = {"type": "delta", "text": "x", "message_id": "stream"}
    # The real bridge checks session existence on both load and save. The
    # conversation adapter must not add a third full-plan read per delta.
    agents.external.publish(sid, binding["binding_id"], event)
    baseline_reads = read_plan.call_count
    assert baseline_reads == 2
    read_plan.reset_mock()
    for _ in range(100):
        assert agents.publish_external(sid, {"binding_id": binding["binding_id"], "event": event})["accepted"]
    assert read_plan.call_count == baseline_reads * 100
    lookup_entry.assert_not_called()
    persist_chat.assert_not_called()
    assert entry["record"]["messages"][-1]["text"] == "x" * 100


def test_external_delivery_cannot_cross_delete_and_restore(bound_plan, monkeypatch):
    agents, store, plan, _factory = bound_plan
    sid = plan["id"]
    binding = agents.register_external(sid, {"name": "Delayed external fixture", "pid": 101})
    original_publish = agents.external.publish

    def publish_then_delete(*args, **kwargs):
        event = original_publish(*args, **kwargs)
        agents.disconnect(sid)
        agents.delete_session(sid, plan["revision"], confirmed=True)
        store.restore(sid)
        return event

    monkeypatch.setattr(agents.external, "publish", publish_then_delete)
    assert agents.publish_external(sid, {"binding_id": binding["binding_id"], "event": {
        "type": "delta", "text": "Must not enter restored chat", "message_id": "old"}})["accepted"]
    assert agents.status(sid)["messages"] == []


def test_slow_durable_event_plan_read_does_not_hold_global_chat_lock(bound_plan, monkeypatch):
    agents, store, plan, _factory = bound_plan
    agents.connect(plan["id"])
    first_callback = agents.entries[plan["id"]]["runtime"].kwargs["on_event"]
    other = store.create("Another independent simulation plan", None)
    agents.connect(other["id"])
    second_callback = agents.entries[other["id"]]["runtime"].kwargs["on_event"]
    entered, release, delta_done = threading.Event(), threading.Event(), threading.Event()
    original_get = store.get

    def slow_get(sid):
        if threading.current_thread().name == "slow-persistent-event":
            entered.set()
            assert release.wait(3)
        return original_get(sid)

    monkeypatch.setattr(store, "get", slow_get)
    first = threading.Thread(target=lambda: first_callback({"type": "message", "text": "Complete reply", "item_id": "m1"}),
        name="slow-persistent-event")
    def emit_delta():
        second_callback({"type": "text_delta", "text": "Still responsive", "item_id": "m2"})
        delta_done.set()
    second = threading.Thread(target=emit_delta)
    first.start()
    try:
        assert entered.wait(3)
        second.start()
        assert delta_done.wait(1), "large plan read blocked another conversation's token callback"
    finally:
        release.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not first.is_alive() and not second.is_alive()


@pytest.mark.parametrize("slow_operation", ["store", "runtime"])
def test_monitor_slow_checks_do_not_hold_global_chat_lock(bound_plan, monkeypatch, slow_operation):
    agents, store, plan, _factory = bound_plan
    sid = plan["id"]
    agents.connect(sid)
    runtime = agents.entries[sid]["runtime"]
    callback = runtime.kwargs["on_event"]
    entered, release, delta_done = threading.Event(), threading.Event(), threading.Event()
    target = store if slow_operation == "store" else runtime
    name = "get" if slow_operation == "store" else "status"
    original = getattr(target, name)

    def slow_check(*args, **kwargs):
        if threading.current_thread().name == "slow-agent-monitor":
            entered.set()
            assert release.wait(3)
        return original(*args, **kwargs)

    class OneIteration:
        calls = 0
        def wait(self, _timeout):
            self.calls += 1
            return self.calls > 1
        def set(self):
            self.calls = 2

    monkeypatch.setattr(target, name, slow_check)
    monkeypatch.setattr(agents, "stopping", OneIteration())
    monitor = threading.Thread(target=agents._monitor, name="slow-agent-monitor")
    def emit_delta():
        callback({"type": "text_delta", "text": "Still responsive", "item_id": "m1"})
        delta_done.set()
    stream = threading.Thread(target=emit_delta)
    monitor.start()
    try:
        assert entered.wait(3)
        stream.start()
        assert delta_done.wait(1), "slow monitor check blocked the token callback"
    finally:
        release.set()
        monitor.join(3)
        if stream.ident is not None:
            stream.join(3)
    assert not monitor.is_alive() and not stream.is_alive()

