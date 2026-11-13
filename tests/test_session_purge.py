"""Irreversible plan cleanup is confined to validated, deleted plan records."""
import copy
import json
import os
from pathlib import Path
import subprocess
import threading
from unittest.mock import patch

import pytest

from advisor.sessions import ConflictError, SessionStore, write_json


GOAL = "Review the geometry, material inputs and boundary conditions for this simulation"


def deleted_plan(store, model=None):
    value = store.create(GOAL, model)
    for _ in range(2):
        value = store.save_event(store.get(value["id"]), "edited", "Review the updated simulation inputs")
    store.delete(value["id"], value["revision"], confirmed=True)
    return value


def test_purge_removes_only_deleted_record_and_owned_snapshots(tmp_path):
    store = SessionStore(tmp_path / "data", None, None)
    model = tmp_path / "original model.mph"
    model.write_bytes(b"Original model remains unchanged")
    first = deleted_plan(store, {"path": str(model)})
    sid = first["id"]
    other = deleted_plan(store)
    active = store.create("Keep this independent active simulation plan", None)
    retained = [model, store._trash_path(other["id"]), store._version_path(other["id"], 1), store.path(active["id"])]
    for relative in (f"agent-sessions/{sid}.json", f"external-agents/{sid}.json", "exports/plan.md", "libraries/marker.json"):
        path = store.paths.root / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"Independently owned data remains unchanged")
        retained.append(path)
    before = {path: path.read_bytes() for path in retained}
    assert store.purge(sid, first["revision"], confirmed=True) == {"id": sid, "status": "purged"}
    assert not store._trash_path(sid).exists()
    assert not (store.versions_directory / sid).exists()
    assert not store.path(sid).exists()
    assert [row["id"] for row in store.deleted_list()] == [other["id"]]
    assert {path: path.read_bytes() for path in retained} == before
    for read in (lambda: store.get(sid), lambda: store.restore(sid), lambda: store.versions(sid),
                 lambda: store.purge(sid, first["revision"], confirmed=True)):
        with pytest.raises(FileNotFoundError):
            read()


def test_permanent_plan_removal_also_removes_its_retained_draft(tmp_path):
    from advisor.plan_drafts import PlanDraftStore
    store = SessionStore(tmp_path, None, None)
    task = deleted_plan(store)
    drafts = PlanDraftStore(tmp_path)
    drafts.capture(task['id'], {'revision': task['revision'], 'plan': {'summary': 'Original unsaved details'}})
    other = store.create('Keep this other task and its independent draft', None)
    drafts.capture(other['id'], {'revision': 1})
    store.purge(task['id'], task['revision'], confirmed=True)
    assert not (drafts.directory / (task['id'] + '.json')).exists()
    assert (drafts.directory / (other['id'] + '.json')).is_file()


def test_purge_accepts_deleted_plan_without_any_snapshots(tmp_path):
    store = SessionStore(tmp_path, None, None)
    value = store.create(GOAL, None)
    store.delete(value["id"], 1, confirmed=True)
    assert store.purge(value["id"], 1, confirmed=True)["status"] == "purged"
    assert not store.versions_directory.exists()


@pytest.mark.parametrize("confirmed", [False, None, 1, "true", [], {}])
def test_purge_requires_exact_confirmation_and_current_revision(tmp_path, confirmed):
    store = SessionStore(tmp_path, None, None)
    value = deleted_plan(store)
    sid = value["id"]
    before = {path: path.read_bytes() for path in store.directory.rglob("*.json")}
    with pytest.raises(ValueError, match="确认"):
        store.purge(sid, value["revision"], confirmed=confirmed)
    for revision in (value["revision"] - 1, value["revision"] + 1, True, "3", None):
        with pytest.raises(ConflictError):
            store.purge(sid, revision, confirmed=True)
    assert {path: path.read_bytes() for path in store.directory.rglob("*.json")} == before


def test_active_or_restored_plan_cannot_be_purged(tmp_path):
    store = SessionStore(tmp_path, None, None)
    value = store.create(GOAL, None)
    sid = value["id"]
    with pytest.raises(ConflictError, match="已删除"):
        store.purge(sid, 1, confirmed=True)
    store.delete(sid, 1, confirmed=True)
    restored = store.restore(sid)
    before = store.path(sid).read_bytes()
    with pytest.raises(ConflictError):
        store.purge(sid, 1, confirmed=True)
    assert store.path(sid).read_bytes() == before and store.get(sid) == restored
    # An inconsistent duplicate active record also prevents deletion of trash.
    write_json(store._trash_path(sid), dict(restored, deleted_at="fixture"))
    with pytest.raises(ConflictError):
        store.purge(sid, restored["revision"], confirmed=True)
    assert store._trash_path(sid).exists() and store.path(sid).read_bytes() == before


@pytest.mark.parametrize("sid", ["../outside", "A" * 32, "b" * 31, "b" * 33, None, 9])
def test_purge_identifier_never_selects_unrelated_paths(tmp_path, sid):
    store = SessionStore(tmp_path / "data", None, None)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"Unrelated data")
    with pytest.raises(FileNotFoundError):
        store.purge(sid, 1, confirmed=True)
    assert outside.read_bytes() == b"Unrelated data"


def test_old_saved_values_cannot_recreate_a_purged_plan(tmp_path):
    store = SessionStore(tmp_path, None, None)
    value = deleted_plan(store)
    stale = copy.deepcopy(value)
    store.purge(value["id"], value["revision"], confirmed=True)
    for save in (lambda: store._write(stale), lambda: store.save_event(stale, "late", "Delayed Agent result")):
        with pytest.raises(FileNotFoundError):
            save()
    assert stale["revision"] == value["revision"]
    assert not store.path(value["id"]).exists() and not (store.versions_directory / value["id"]).exists()


@pytest.mark.parametrize("content", ["unknown-file", "subdirectory", "wrong-id", "wrong-revision", "future-revision", "invalid-json"])
def test_complete_snapshot_set_is_validated_before_any_deletion(tmp_path, content):
    store = SessionStore(tmp_path, None, None)
    value = deleted_plan(store)
    sid = value["id"]
    directory = store.versions_directory / sid
    if content == "unknown-file":
        (directory / "unrelated.txt").write_bytes(b"Preserve unrelated file")
    elif content == "subdirectory":
        (directory / "nested").mkdir()
        (directory / "nested/marker").write_bytes(b"Preserve nested file")
    else:
        path = store._version_path(sid, 2)
        record = json.loads(path.read_text(encoding="utf-8"))
        if content == "wrong-id":
            record["id"] = "a" * 32
        elif content == "wrong-revision":
            record["revision"] = 1
        elif content == "future-revision":
            record["revision"] = 4
            path = store._version_path(sid, 4)
        write_json(path, record)
        if content == "invalid-json":
            path.write_bytes(b"broken JSON")
    before = {path: path.read_bytes() for path in store.directory.rglob("*") if path.is_file()}
    with pytest.raises((PermissionError, ValueError)):
        store.purge(sid, value["revision"], confirmed=True)
    assert {path: path.read_bytes() for path in store.directory.rglob("*") if path.is_file()} == before


def test_original_model_hardlinked_to_snapshot_is_preserved(tmp_path):
    store = SessionStore(tmp_path / "data", None, None)
    model = tmp_path / "original-model.mph"
    value = deleted_plan(store, {"path": str(model)})
    snapshot = store._version_path(value["id"], 1)
    os.link(snapshot, model)
    before = model.read_bytes()
    with pytest.raises(PermissionError, match="原始模型"):
        store.purge(value["id"], value["revision"], confirmed=True)
    assert model.read_bytes() == before and snapshot.read_bytes() == before
    assert store._trash_path(value["id"]).exists()


@pytest.mark.parametrize("where", ["sessions", "trash", "tombstone", "versions", "version-directory", "snapshot"])
def test_purge_refuses_symbolic_links_before_touching_targets(tmp_path, where):
    store = SessionStore(tmp_path / "data", None, None)
    value = deleted_plan(store)
    sid = value["id"]
    target = {"sessions": store.directory, "trash": store.trash_directory, "tombstone": store._trash_path(sid),
              "versions": store.versions_directory, "version-directory": store.versions_directory / sid,
              "snapshot": store._version_path(sid, 1)}[where]
    parked = tmp_path / "outside-target"
    is_directory = target.is_dir()
    target.rename(parked)
    try:
        target.symlink_to(parked, target_is_directory=is_directory)
    except OSError:
        parked.rename(target)
        pytest.skip("Creating symlinks is unavailable in this Windows test environment")
    before = ({path: path.read_bytes() for path in parked.rglob("*") if path.is_file()}
              if is_directory else {parked: parked.read_bytes()})
    with pytest.raises(PermissionError):
        store.purge(sid, value["revision"], confirmed=True)
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.skipif(os.name != "nt", reason="Native Windows directory junction boundary")
def test_purge_refuses_junction_even_when_target_is_inside_workspace(tmp_path):
    store = SessionStore(tmp_path, None, None)
    value = deleted_plan(store)
    original = store.versions_directory
    retained = store.paths.root / "retained-version-records"
    original.rename(retained)
    result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(original), str(retained)], capture_output=True)
    if result.returncode:
        retained.rename(original)
        pytest.skip("Creating Windows junctions is unavailable in this environment")
    before = {path: path.read_bytes() for path in retained.rglob("*.json")}
    with pytest.raises(PermissionError, match="联接"):
        store.purge(value["id"], value["revision"], confirmed=True)
    assert store._trash_path(value["id"]).exists()
    assert {path: path.read_bytes() for path in before} == before


def test_failed_snapshot_removal_leaves_tombstone_for_retry(tmp_path):
    store = SessionStore(tmp_path, None, None)
    value = deleted_plan(store)
    sid = value["id"]
    unlink = Path.unlink
    count = 0
    def fail_second_snapshot(path, *args, **kwargs):
        nonlocal count
        if path.parent == store.versions_directory / sid:
            count += 1
            if count == 2:
                raise OSError("Fixture disk failure")
        return unlink(path, *args, **kwargs)
    with patch.object(Path, "unlink", fail_second_snapshot):
        with pytest.raises(OSError, match="disk failure"):
            store.purge(sid, value["revision"], confirmed=True)
    assert store.deleted_list()[0]["id"] == sid
    with pytest.raises(FileNotFoundError):
        store._write(value)
    assert store.purge(sid, value["revision"], confirmed=True)["status"] == "purged"


def test_restore_and_purge_of_one_deleted_plan_are_serialized(tmp_path):
    store = SessionStore(tmp_path, None, None)
    value = deleted_plan(store)
    barrier = threading.Barrier(2)
    results, errors = [], []
    def perform(operation):
        barrier.wait()
        try:
            results.append(operation())
        except (ConflictError, FileNotFoundError) as exc:
            errors.append(exc)
    workers = [threading.Thread(target=perform, args=(operation,)) for operation in
               (lambda: store.restore(value["id"]), lambda: store.purge(value["id"], value["revision"], confirmed=True))]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(3)
        assert not worker.is_alive()
    assert len(results) == 1 and len(errors) == 1
    assert not store._trash_path(value["id"]).exists()
    if results[0]["status"] == "purged":
        assert not store.path(value["id"]).exists()
    else:
        assert store.get(value["id"])["revision"] == value["revision"] + 1
