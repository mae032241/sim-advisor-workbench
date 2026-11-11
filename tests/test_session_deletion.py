"""Recoverable session deletion, stale-write fencing and owned-path safety."""
import copy
import json
import os
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from advisor.sessions import ConflictError, SessionStore, write_json


GOAL = "Determine the geometry and boundary inputs for the requested simulation"


def store_at(tmp_path, planner=None):
    return SessionStore(tmp_path / "data", None, planner)


def test_delete_moves_only_owned_session_and_restore_keeps_model_and_chat(tmp_path):
    store = store_at(tmp_path)
    model = store.paths.root / "uploads/input.step"
    model.parent.mkdir()
    model.write_bytes(b"Original uploaded CAD stays intact")
    session = store.create(GOAL, {"id": "model-fixture", "path": str(model), "facts": []})
    sid = session["id"]
    sidecar = store.paths.root / "agent-sessions" / (sid + ".json")
    sidecar.parent.mkdir()
    sidecar.write_bytes(b"Agent history stays intact")
    export = store.paths.root / "exports/plan.md"
    export.parent.mkdir()
    export.write_bytes(b"Existing export stays intact")
    deleted = store.delete(sid, 1, confirmed=True)
    assert deleted["status"] == "deleted" and deleted["revision"] == 1 and deleted["deleted_at"]
    assert not store.path(sid).exists()
    trash = store.trash_directory / (sid + ".json")
    stored = json.loads(trash.read_text(encoding="utf-8"))
    assert stored["model"]["path"] == "uploads/input.step"
    with pytest.raises(FileNotFoundError):
        store.get(sid)
    assert store.list() == []
    rows = store.deleted_list()
    assert rows == [{"id": sid, "title": session["title"], "status": "deleted", "previous_status": "needs_replan",
                     "revision": 1, "updated_at": session["updated_at"], "deleted_at": deleted["deleted_at"]}]
    restored = store.restore(sid)
    assert restored["id"] == sid and restored["revision"] == 2 and restored["lifecycle_revision"] == 1
    assert restored["plan"] == session["plan"] and restored["answers"] == session["answers"]
    assert restored["model"]["path"] == str(model)
    assert restored["history"][-1]["event"] == "restored"
    assert not trash.exists() and store.deleted_list() == []
    assert store.get(sid) == restored
    assert model.read_bytes() == b"Original uploaded CAD stays intact"
    assert sidecar.read_bytes() == b"Agent history stays intact"
    assert export.read_bytes() == b"Existing export stays intact"


@pytest.mark.parametrize("confirmed", [False, None, 1, "true", [], {}])
def test_delete_requires_exact_confirmation_and_revision(tmp_path, confirmed):
    store = store_at(tmp_path)
    session = store.create(GOAL, None)
    original = store.path(session["id"]).read_bytes()
    with pytest.raises(ValueError, match="确认"):
        store.delete(session["id"], session["revision"], confirmed=confirmed)
    assert store.path(session["id"]).read_bytes() == original
    for revision in (0, True, "1", None):
        with pytest.raises(ConflictError):
            store.delete(session["id"], revision, confirmed=True)
    assert store.path(session["id"]).read_bytes() == original


@pytest.mark.parametrize("sid", ["../outside", "A"*32, "b"*31, "b"*33, None, 9])
def test_identifier_is_strict_for_delete_and_restore(tmp_path, sid):
    store = store_at(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.delete(sid, 1, confirmed=True)
    with pytest.raises(FileNotFoundError):
        store.restore(sid)


def test_held_old_value_cannot_revive_deleted_or_overwrite_restored_session(tmp_path):
    store = store_at(tmp_path)
    session = store.create(GOAL, None)
    stale = store.get(session["id"])
    store.delete(session["id"], 1, confirmed=True)
    for save in (lambda: store._write(stale), lambda: store.save_event(stale, "late", "Old Agent result")):
        with pytest.raises(FileNotFoundError):
            save()
    assert stale["revision"] == 1
    restored = store.restore(session["id"])
    original = store.path(session["id"]).read_bytes()
    # Even a caller which increments the old plan's revision to the restored
    # revision cannot cross the independent lifecycle fence.
    stale["revision"] = restored["revision"]
    for save in (lambda: store._write(stale), lambda: store.save_event(stale, "late", "Old Agent result")):
        with pytest.raises(ConflictError, match="删除或恢复"):
            save()
    assert store.path(session["id"]).read_bytes() == original
    current = store.get(session["id"])
    assert store.save_event(current, "new", "Fresh edit")["revision"] == 3


@pytest.mark.parametrize("stale,required,expected", [(True, False, "needs_replan"), (False, True, "needs_input"), (False, False, "draft")])
def test_restore_advances_revision_and_invalidates_old_confirmation(tmp_path, stale, required, expected):
    store = store_at(tmp_path)
    session = store.create(GOAL, None)
    session.update(status="confirmed", confirmed_revision=1, confirmed_at="2026-11-08T00:00:00+00:00", plan_stale=stale)
    if required:
        session["questions"] = [{"id": "boundary", "label": "Boundary inputs", "kind": "text", "required": True}]
    store._write(session)
    store.delete(session["id"], 1, confirmed=True)
    restored = store.restore(session["id"])
    assert restored["status"] == expected and restored["revision"] == 2
    assert "confirmed_revision" not in restored and "confirmed_at" not in restored


def test_restore_does_not_overwrite_existing_active_same_id(tmp_path):
    store = store_at(tmp_path)
    session = store.create(GOAL, None)
    store.delete(session["id"], 1, confirmed=True)
    trash = store.trash_directory / (session["id"] + ".json")
    write_json(store.path(session["id"]), dict(session, goal="Existing different active content"))
    original, deleted = store.path(session["id"]).read_bytes(), trash.read_bytes()
    with pytest.raises(ConflictError, match="已存在"):
        store.restore(session["id"])
    assert store.path(session["id"]).read_bytes() == original and trash.read_bytes() == deleted


def test_legacy_epoch_defaults_zero_and_survives_deleted_data_root_move(tmp_path):
    store = store_at(tmp_path)
    upload = store.paths.root / "uploads/legacy.step"
    upload.parent.mkdir()
    upload.write_bytes(b"CAD fixture")
    session = store.create(GOAL, {"path": str(upload)})
    old = json.loads(store.path(session["id"]).read_text(encoding="utf-8"))
    old.pop("lifecycle_revision")
    old["model"]["path"] = str(upload)
    write_json(store.path(session["id"]), old)
    assert store.get(session["id"])["lifecycle_revision"] == 0
    store.delete(session["id"], 1, confirmed=True)
    moved_root = tmp_path / "moved data with spaces"
    shutil.copytree(store.paths.root, moved_root)
    store.paths.root.rename(tmp_path / "retired data")
    moved = SessionStore(moved_root, None, None)
    assert moved.deleted_list()[0]["id"] == session["id"]
    restored = moved.restore(session["id"])
    assert restored["model"]["path"] == str(moved_root / "uploads/legacy.step")
    assert restored["lifecycle_revision"] == 1
    assert json.loads(moved.path(session["id"]).read_text(encoding="utf-8"))["model"]["path"] == "uploads/legacy.step"


def test_late_refine_result_cannot_recreate_deleted_plan(tmp_path):
    started, release = threading.Event(), threading.Event()
    class Planner:
        def refine(self, _):
            started.set()
            assert release.wait(3)
            return {"plan": {}, "sources": []}
    store = store_at(tmp_path, Planner())
    session = store.create(GOAL, None)
    errors = []
    def refine():
        try:
            store.refine(session["id"], 1)
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=refine)
    worker.start()
    try:
        assert started.wait(3)
        store.delete(session["id"], 1, confirmed=True)
    finally:
        release.set()
        worker.join(3)
    assert len(errors) == 1 and isinstance(errors[0], FileNotFoundError)
    assert not store.path(session["id"]).exists() and len(store.deleted_list()) == 1


def test_failed_delete_metadata_write_rolls_back_move(tmp_path):
    store = store_at(tmp_path)
    session = store.create(GOAL, None)
    original = store.path(session["id"]).read_bytes()
    with patch("advisor.sessions.write_json", side_effect=OSError("Fixture write failed")):
        with pytest.raises(OSError):
            store.delete(session["id"], 1, confirmed=True)
    assert store.path(session["id"]).read_bytes() == original and store.deleted_list() == []


def test_failed_restore_move_keeps_recoverable_trash_record(tmp_path):
    store = store_at(tmp_path)
    session = store.create(GOAL, None)
    store.delete(session["id"], 1, confirmed=True)
    trash = store.trash_directory / (session["id"] + ".json")
    original = trash.read_bytes()
    with patch.object(Path, "rename", side_effect=OSError("Fixture move failed")):
        with pytest.raises(OSError):
            store.restore(session["id"])
    assert not store.path(session["id"]).exists() and trash.read_bytes() == original
    assert store.restore(session["id"])["revision"] == 2


def test_model_hardlink_cannot_be_deleted_or_written_as_session(tmp_path):
    store = store_at(tmp_path)
    model = tmp_path / "original-model.mph"
    session = store.create(GOAL, {"path": str(model)})
    os.link(store.path(session["id"]), model)
    original = model.read_bytes()
    with pytest.raises(PermissionError, match="原始模型"):
        store.delete(session["id"], 1, confirmed=True)
    with pytest.raises(PermissionError, match="原始模型"):
        store._write(session)
    assert model.read_bytes() == original and store.path(session["id"]).read_bytes() == original


@pytest.mark.parametrize("where", ["sessions", "trash", "record"])
def test_symlink_paths_are_refused_without_touching_external_data(tmp_path, where):
    store = store_at(tmp_path)
    session = store.create(GOAL, None)
    external = tmp_path / "external"
    external.mkdir()
    original = store.path(session["id"]).read_bytes()
    target = external / (session["id"] + ".json")
    target.write_bytes(original)
    try:
        if where == "record":
            store.path(session["id"]).unlink()
            (store.directory / target.name).symlink_to(target)
        elif where == "trash":
            store.trash_directory.symlink_to(external, target_is_directory=True)
        else:
            store.path(session["id"]).unlink()
            store.directory.rmdir()
            store.directory.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable in this Windows environment")
    with pytest.raises(PermissionError):
        store.delete(session["id"], 1, confirmed=True)
    assert target.read_bytes() == original


def test_normal_new_sessions_and_uuid_collision_do_not_overwrite(tmp_path):
    store = store_at(tmp_path)
    first, second = store.create(GOAL, None), store.create(GOAL, None)
    assert first["id"] != second["id"] and len(store.list()) == 2
    before = store.path(first["id"]).read_bytes()
    with patch("advisor.sessions.uuid.uuid4", return_value=SimpleNamespace(hex=first["id"])):
        with pytest.raises(ConflictError):
            store.create("Another engineering task with a colliding identifier", None)
    assert store.path(first["id"]).read_bytes() == before


def test_get_checks_active_and_trash_once_without_repeating_filesystem_work(tmp_path):
    store = store_at(tmp_path)
    session = store.create(GOAL, None)
    active = store.directory / (session["id"] + ".json")
    trash = store.trash_directory / (session["id"] + ".json")
    with patch.object(store, "_owned_path", wraps=store._owned_path) as checked:
        assert store.get(session["id"])["id"] == session["id"]
    assert [call.args[0] for call in checked.call_args_list].count(active) == 1
    assert [call.args[0] for call in checked.call_args_list].count(trash) == 1
    assert checked.call_count == 2
    store.delete(session["id"], 1, confirmed=True)
    with pytest.raises(FileNotFoundError):
        store.get(session["id"])
