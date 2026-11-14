import json
from types import SimpleNamespace

import pytest

from advisor.sessions import SessionStore


def _store(tmp_path):
    return SessionStore(tmp_path / "data", SimpleNamespace(), None)


def test_revision_reads_once_until_the_plan_file_changes(tmp_path, monkeypatch):
    store = _store(tmp_path)
    created = store.create("cache probe", None)
    reads = []
    original = SessionStore._record
    monkeypatch.setattr(SessionStore, "_record", staticmethod(lambda path, sid: reads.append(sid) or original(path, sid)))

    assert store.revision(created["id"]) == created["revision"]
    assert store.revision(created["id"]) == created["revision"]
    assert len(reads) == 1, "an unchanged file is not parsed again"

    path = store.path(created["id"])
    value = json.loads(path.read_text(encoding="utf-8"))
    value["revision"] += 1
    value["padding"] = "x" * 64  # also changes size, so the signature differs even on coarse mtimes
    path.write_text(json.dumps(value), encoding="utf-8")
    assert store.revision(created["id"]) == created["revision"] + 1
    assert len(reads) == 2


def test_revision_reports_deleted_and_missing_plans(tmp_path):
    store = _store(tmp_path)
    created = store.create("cache probe", None)
    store.revision(created["id"])
    store.delete(created["id"], created["revision"], confirmed=True)
    with pytest.raises(FileNotFoundError):
        store.revision(created["id"])
    with pytest.raises(FileNotFoundError):
        store.revision("0" * 32)


def test_head_tracks_lifecycle_across_delete_and_restore(tmp_path):
    store = _store(tmp_path)
    created = store.create("cache probe", None)
    before = store.head(created["id"])
    assert before == {"id": created["id"], "revision": created["revision"], "lifecycle_revision": created.get("lifecycle_revision", 0)}
    store.delete(created["id"], created["revision"], confirmed=True)
    with pytest.raises(FileNotFoundError):
        store.head(created["id"])
    store.restore(created["id"])
    after = store.head(created["id"])
    assert after["lifecycle_revision"] == store.get(created["id"])["lifecycle_revision"]
    assert after["lifecycle_revision"] != before["lifecycle_revision"]
    with pytest.raises(FileNotFoundError):
        store.head("../escape")


def test_agent_status_poll_does_not_reparse_an_unchanged_plan(tmp_path, monkeypatch):
    from advisor.agent_sessions import AgentSessions

    store = _store(tmp_path)
    created = store.create("poll probe", None)
    agents = AgentSessions(tmp_path / "data", store, SimpleNamespace())
    try:
        agents.status(created["id"])  # warm the identity cache and the agent entry
        parses = []
        original = SessionStore._record
        monkeypatch.setattr(SessionStore, "_record", staticmethod(lambda path, sid: parses.append(sid) or original(path, sid)))
        monkeypatch.setattr(store, "get", lambda sid: pytest.fail("status poll parsed the full plan"))
        for _ in range(3):
            snapshot = agents.status(created["id"])
        assert parses == []
        assert snapshot["plan_revision"] == created["revision"]
        assert snapshot["external"]["registered"] is False
    finally:
        monkeypatch.undo()
        getattr(agents, "close", lambda: None)()
