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
