"""Saved revision snapshots, conservative restoration and filtered HTTP views."""
import copy
import json
import os
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from advisor.server import make_server
from advisor.sessions import ConflictError, SessionStore, write_json


GOAL = "Design a reviewable simulation for the supplied geometry and measured inputs"


def fixture_store(tmp_path):
    closed = set()
    kb = SimpleNamespace(source_available=lambda source: source.get("library_id") not in closed)
    store = SessionStore(tmp_path / "data", kb, None)
    return store, kb, closed


def planned(store, model=None):
    value = store.create(GOAL, model)
    value.update(plan_stale=False, planning_incomplete=False, design_brief={"objective": "original objective"},
                 questions=[{"id": "flow", "label": "Measured flow", "kind": "text", "required": True}],
                 answers={"flow": "measured 2 L/min"}, evidence_gaps=["Original measurement uncertainty"],
                 sources=[{"id": "lo-example@r1", "revision_id": "r1", "library_id": "models", "kind": "case",
                           "title": "Pinned example", "text": "PRIVATE ORIGINAL SOURCE BODY",
                           "data": {"code": "PRIVATE FULL IMPLEMENTATION"}, "url": "/api/sources/lo-example@r1"}])
    value["plan"].update(engine="fixture-agent", summary="Original complete plan", evidence_status="Source was read for this revision")
    store._write(value)
    return value


def advance(store, value, summary="Updated plan"):
    value = copy.deepcopy(value)
    value["plan"]["summary"] = summary
    return store.save_event(value, "fixture_updated", summary)


def test_archives_last_saved_record_only_when_a_higher_revision_replaces_it(tmp_path):
    store, _, _ = fixture_store(tmp_path)
    value = planned(store)
    sid = value["id"]
    assert not store.versions_directory.exists()
    confirmed = store.confirm(sid, 1)
    assert not store.versions_directory.exists(), "Same-revision confirmation must not freeze an earlier draft"
    assert store.version(sid, 1) == confirmed
    changed = advance(store, confirmed)
    archived = store.version(sid, 1)
    assert archived["status"] == "confirmed" and archived["confirmed_revision"] == 1
    assert archived["plan"]["summary"] == "Original complete plan"
    assert archived["history"][-1]["event"] == "confirmed"
    assert store.version(sid, 2) == changed
    assert not store._version_path(sid, 2).exists()
    assert [row["event"] for row in store.versions(sid)["versions"]] == ["fixture_updated", "confirmed"]
    archived["plan"]["summary"] = "Caller mutation"
    assert store.version(sid, 1)["plan"]["summary"] == "Original complete plan"


def test_legacy_operation_log_is_never_fabricated_into_a_snapshot(tmp_path):
    store, _, _ = fixture_store(tmp_path)
    value = store.create(GOAL, None)
    value["revision"] = 4
    value["history"] += [{"revision": 2, "event": "legacy_edit", "at": "old-at", "summary": "Legacy edit"},
                         {"revision": 4, "event": "legacy_edit", "at": "last-at", "summary": "Last legacy edit"}]
    write_json(store.path(value["id"]), value)  # Simulates a record predating snapshot support.
    rows = store.versions(value["id"])
    assert rows["current_revision"] == 4
    assert [(row["revision"], row["available"], row["current"]) for row in rows["versions"]] == [(4, True, True), (2, False, False), (1, False, False)]
    assert all(set(row) == {"revision", "available", "current", "event", "at", "summary"} for row in rows["versions"])
    with pytest.raises(FileNotFoundError):
        store.version(value["id"], 2)
    advance(store, value)
    assert store.version(value["id"], 4)["history"][-1]["at"] == "last-at"
    assert next(row for row in store.versions(value["id"])["versions"] if row["revision"] == 2)["available"] is False


def test_restore_copies_planning_fields_into_a_new_revision_without_rolling_back_identity(tmp_path):
    store, _, _ = fixture_store(tmp_path)
    old = planned(store)
    old["question_archive"] = [{"question": {"id": "retired"}, "reason": "Original retired question"}]
    store._write(old)
    current = advance(store, old)
    current["design_brief"] = {"objective": "Changed design"}
    current["questions"] = []
    current["answers"] = {}
    current["geometry_reference_catalog"] = ["Only present in newer revision"]
    current["title"] = "Current title survives restoration"
    store._write(current)
    current = store.confirm(current["id"], 2)
    restored = store.restore_version(current["id"], 1, 2, confirmed=True)
    assert restored["id"] == current["id"] and restored["revision"] == 3
    for key in ("goal", "model", "title", "created_at", "lifecycle_revision"):
        assert restored[key] == current[key]
    for key in ("plan", "design_brief", "questions", "answers", "sources", "evidence_gaps", "question_archive"):
        assert restored[key] == old[key]
    assert "geometry_reference_catalog" not in restored
    assert restored["status"] == "draft"
    assert "confirmed_revision" not in restored and "confirmed_at" not in restored
    assert restored["history"][:-1] == current["history"]
    assert restored["history"][-1]["event"] == "version_restored"
    assert restored["version_restore"]["from_revision"] == 1 and restored["version_restore"]["review_required"] is True
    assert store.version(current["id"], 2)["status"] == "confirmed"
    assert store.version(current["id"], 1)["plan"] == old["plan"]


def test_restore_keeps_current_input_but_marks_a_historical_geometry_plan_stale(tmp_path):
    store, _, _ = fixture_store(tmp_path)
    old = planned(store, {"path": str(tmp_path / "old.step"), "fingerprint": "old"})
    current = copy.deepcopy(old)
    current["goal"] = "A different current simulation goal with changed geometry"
    current["model"] = {"path": str(tmp_path / "new.step"), "fingerprint": "new"}
    current = advance(store, current)
    restored = store.restore_version(old["id"], 1, current["revision"], confirmed=True)
    assert restored["goal"] == current["goal"] and restored["model"] == current["model"]
    assert restored["plan_stale"] is True and restored["planning_incomplete"] is True
    assert restored["status"] == "needs_replan" and restored["version_restore"]["input_changed"] is True
    assert any("当前需求和模型已保留" in item for item in restored["evidence_gaps"])
    with pytest.raises(ValueError, match="先规划"):
        store.confirm(old["id"], restored["revision"])


def test_restore_unreadable_references_preserves_ids_and_requires_new_evidence_review(tmp_path):
    store, _, closed = fixture_store(tmp_path)
    old = planned(store)
    current = advance(store, old)
    closed.add("models")
    restored = store.restore_version(old["id"], 1, current["revision"], confirmed=True)
    assert restored["sources"] == old["sources"], "Stored citations remain pinned, not silently removed or rewritten"
    assert restored["version_restore"]["unavailable_source_ids"] == [old["sources"][0]["id"]]
    assert restored["status"] == "needs_replan" and restored["planning_incomplete"] is True
    assert "未重新核实" in restored["plan"]["evidence_status"]
    assert any("目前不可读取" in gap for gap in restored["evidence_gaps"])
    assert store.version(old["id"], 1)["plan"]["evidence_status"] == old["plan"]["evidence_status"]


@pytest.mark.parametrize("confirmed", [False, None, 1, "true", [], {}])
def test_restore_requires_exact_confirmation_and_current_revision(tmp_path, confirmed):
    store, _, _ = fixture_store(tmp_path)
    current = advance(store, planned(store))
    before = store.path(current["id"]).read_bytes()
    with pytest.raises(ValueError, match="确认"):
        store.restore_version(current["id"], 1, current["revision"], confirmed=confirmed)
    for revision in (1, True, "2", None):
        with pytest.raises(ConflictError):
            store.restore_version(current["id"], 1, revision, confirmed=True)
    assert store.path(current["id"]).read_bytes() == before


@pytest.mark.parametrize("revision", [0, -1, True, "1", None, 999])
def test_revision_locator_is_a_positive_existing_integer(tmp_path, revision):
    store, _, _ = fixture_store(tmp_path)
    value = store.create(GOAL, None)
    with pytest.raises(FileNotFoundError):
        store.version(value["id"], revision)


def test_concurrent_restoration_has_one_winner_and_cannot_overwrite_the_new_revision(tmp_path):
    store, _, _ = fixture_store(tmp_path)
    current = advance(store, planned(store))
    start = threading.Barrier(3)
    results = []
    def restore():
        start.wait()
        try:
            results.append(store.restore_version(current["id"], 1, 2, confirmed=True))
        except ConflictError as exc:
            results.append(exc)
    workers = [threading.Thread(target=restore) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(3)
        assert not worker.is_alive()
    assert sum(isinstance(item, ConflictError) for item in results) == 1
    assert store.get(current["id"])["revision"] == 3


def test_archive_failure_leaves_current_record_unchanged_and_retry_captures_new_confirmation(tmp_path):
    store, _, _ = fixture_store(tmp_path)
    value = planned(store)
    before = store.path(value["id"]).read_bytes()
    with patch("advisor.sessions.write_json", side_effect=OSError("Snapshot write failed")):
        with pytest.raises(OSError):
            advance(store, value)
    assert store.path(value["id"]).read_bytes() == before
    original_write = write_json
    def fail_current(path, body):
        if path == store.path(value["id"]):
            raise OSError("Current write failed after archive")
        return original_write(path, body)
    with patch("advisor.sessions.write_json", side_effect=fail_current):
        with pytest.raises(OSError):
            advance(store, value)
    assert store.path(value["id"]).read_bytes() == before
    confirmed = store.confirm(value["id"], 1)
    advance(store, confirmed)
    assert store.version(value["id"], 1)["status"] == "confirmed", "Retry updates the still-current archive after same-revision confirmation"


def test_delete_hides_history_and_trash_restore_keeps_all_revision_snapshots(tmp_path):
    store, _, _ = fixture_store(tmp_path)
    current = advance(store, planned(store))
    sid = current["id"]
    archived = store._version_path(sid, 1).read_bytes()
    store.delete(sid, 2, confirmed=True)
    for read in (lambda: store.versions(sid), lambda: store.version(sid, 1), lambda: store.version(sid, 2)):
        with pytest.raises(FileNotFoundError):
            read()
    restored = store.restore(sid)
    assert restored["revision"] == 3
    assert store._version_path(sid, 1).read_bytes() == archived
    assert store.version(sid, 2)["plan"] == current["plan"]
    assert [item["revision"] for item in store.versions(sid)["versions"]] == [3, 2, 1]


def test_history_model_paths_move_with_the_data_root(tmp_path):
    store, _, _ = fixture_store(tmp_path)
    model = store.paths.root / "uploads/part.step"
    model.parent.mkdir()
    model.write_text("Neutral fixture CAD", encoding="utf-8")
    current = advance(store, planned(store, {"path": str(model), "fingerprint": "fixture"}))
    stored = json.loads(store._version_path(current["id"], 1).read_text(encoding="utf-8"))
    assert stored["model"]["path"] == "uploads/part.step"
    moved_root = tmp_path / "copied data with spaces"
    shutil.copytree(store.paths.root, moved_root)
    moved = SessionStore(moved_root, None, None)
    assert moved.version(current["id"], 1)["model"]["path"] == str(moved_root / "uploads/part.step")
    result = moved.restore_version(current["id"], 1, 2, confirmed=True)
    assert result["version_restore"]["input_changed"] is False
    assert json.loads(moved.path(current["id"]).read_text(encoding="utf-8"))["model"]["path"] == "uploads/part.step"


def test_version_archive_cannot_overwrite_the_original_model_through_a_hardlink(tmp_path):
    store, _, _ = fixture_store(tmp_path)
    model = tmp_path / "original.mph"
    value = planned(store, {"path": str(model)})
    target = store._version_path(value["id"], 1)
    target.parent.mkdir(parents=True)
    model.write_text("Original model bytes", encoding="utf-8")
    os.link(model, target)
    before = store.path(value["id"]).read_bytes()
    with pytest.raises(PermissionError, match="原始模型"):
        advance(store, value)
    assert store.path(value["id"]).read_bytes() == before and model.read_text(encoding="utf-8") == "Original model bytes"


def test_snapshot_directories_cannot_escape_via_symlink(tmp_path):
    store, _, _ = fixture_store(tmp_path)
    value = planned(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        store.versions_directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable in this Windows test environment")
    before = store.path(value["id"]).read_bytes()
    with pytest.raises(PermissionError):
        advance(store, value)
    with pytest.raises(PermissionError):
        store.versions(value["id"])
    assert not list(outside.iterdir()) and store.path(value["id"]).read_bytes() == before


@pytest.fixture
def http_versions(tmp_path):
    store, kb, closed = fixture_store(tmp_path)
    old = planned(store)
    current = advance(store, old)
    app = SimpleNamespace(sessions=store, kb=kb)
    server = make_server(app, 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def call(suffix, body=None, headers=None):
        request = Request(base + f"/api/sessions/{old['id']}/versions" + suffix,
                          data=None if body is None else json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())
    try:
        yield store, old, current, closed, app, call
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)


def test_http_metadata_and_current_and_historical_details_use_lifecycle_filtered_source_views(http_versions):
    store, old, current, closed, _, call = http_versions
    code, data = call("")
    assert code == 200 and data["current_revision"] == 2
    assert set(data) == {"session_id", "current_revision", "versions"}
    assert "PRIVATE" not in json.dumps(data)
    assert call("/2")[1]["plan"] == current["plan"]
    closed.add("models")
    for suffix in ("/1", "/2"):
        code, value = call(suffix)
        assert code == 200
        assert "PRIVATE" not in json.dumps(value)
        assert value["sources"][0]["id"] == old["sources"][0]["id"]
        assert value["sources"][0]["availability"]["readable"] is False
    assert store.version(old["id"], 1)["sources"][0]["text"] == "PRIVATE ORIGINAL SOURCE BODY"


def test_http_restore_is_revision_checked_and_filters_closed_source_content(http_versions):
    store, old, _, closed, _, call = http_versions
    assert call("/1/restore", {"revision": 2, "confirmed": 1})[0] == 422
    assert call("/1/restore", {"revision": 2, "confirmed": True, "plan": {}})[0] == 422
    assert call("/1/restore", {"revision": 1, "confirmed": True})[0] == 409
    closed.add("models")
    code, value = call("/1/restore", {"revision": 2, "confirmed": True})
    assert code == 200 and value["revision"] == 3 and value["status"] == "needs_replan"
    assert "PRIVATE" not in json.dumps(value) and value["sources"][0]["availability"]["readable"] is False
    assert store.get(old["id"])["sources"][0]["text"] == "PRIVATE ORIGINAL SOURCE BODY"
    assert call("/1/restore", {"revision": 2, "confirmed": True})[0] == 409


def test_http_restore_routes_through_the_agent_guard_when_present(http_versions):
    _, _, _, _, app, call = http_versions
    guard = Mock(side_effect=ConflictError("请先断开该方案的 Agent"))
    app.agents = SimpleNamespace(restore_version=guard)
    code, value = call("/1/restore", {"revision": 2, "confirmed": True})
    assert code == 409 and "断开" in value["error"]
    guard.assert_called_once()
    assert guard.call_args.args[1:] == (1, 2) and guard.call_args.kwargs == {"confirmed": True}


def test_http_deleted_history_and_invalid_versions_return_404_and_origin_policy_remains(http_versions):
    store, old, _, _, _, call = http_versions
    for suffix in ("/0", "/-1", "/01", "/999"):
        assert call(suffix)[0] == 404
    assert call("", headers={"Origin": "https://remote.example"})[0] == 403
    assert call("/1", headers={"Host": "remote.example"})[0] == 403
    store.delete(old["id"], 2, confirmed=True)
    for suffix in ("", "/1", "/2"):
        assert call(suffix)[0] == 404
    assert call("/1/restore", {"revision": 2, "confirmed": True})[0] == 404
