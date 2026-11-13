import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import benchmark_product_setup as setup


def make_case(tmp_path):
    root = tmp_path / "benchmark"
    case = root / "public/cases/case-01"
    (case / "inputs").mkdir(parents=True)
    (case / "problem.md").write_bytes("# 公开需求\r\n保留完整需求。\r\n".encode("utf-8"))
    (case / "curve.csv").write_bytes(b"time,value\r\n0,1\r\n1,2\r\n")
    (case / "properties.txt").write_bytes("材料：铜\n  末行空格  \n".encode("utf-8"))
    (case / "reaction.json").write_bytes(b'{"reaction": [1, 2, 3]}\n')
    (case / "notes.md").write_bytes(b"Additional user data.\n")
    (case / "data").mkdir()
    (case / "data/material-data.md").write_bytes("# 材料数据\r\n密度：铜 8960 kg/m³\r\n  保留空格  \r\n".encode("utf-8"))
    (case / "inputs/geometry_only.mph").write_bytes(b"MPH\x00\xffgeometry-only")
    (case / "inputs/geometry.json").write_text("DERIVED_GEOMETRY", encoding="utf-8")
    (case / "inputs/topology.json").write_text("DERIVED_TOPOLOGY", encoding="utf-8")
    (case / "topology.json").write_text("DERIVED_TOP_LEVEL", encoding="utf-8")
    (root / "private").mkdir()
    (root / "private/reference.md").write_text("PRIVATE_CANARY", encoding="utf-8")
    return root, case


def test_goal_keeps_all_user_text_and_excludes_derived_or_private_inputs(tmp_path):
    root, case = make_case(tmp_path)
    goal = setup.public_goal(root, "case-01")
    for name in ("problem.md", "curve.csv", "properties.txt", "reaction.json", "notes.md", "data/material-data.md"):
        assert (case / name).read_bytes().decode("utf-8") in goal
    assert "### 用户附件：data/material-data.md\n\n" in goal
    assert "DERIVED_" not in goal and "PRIVATE_CANARY" not in goal
    assert goal == goal.strip()
    assert goal == setup.public_goal(root, "case-01")


def test_create_task_uses_real_upload_then_session_payload_and_hashes(tmp_path):
    root, case = make_case(tmp_path)
    calls = []
    model = {"id": "model-real-parser", "parser": "actual-product-parser", "facts": []}
    session = {"id": "a" * 32, "revision": 1, "model": model}

    def request(url, *, data, headers, timeout):
        calls.append((url, data, headers, timeout))
        return model if len(calls) == 1 else session

    result = setup.create_task("http://127.0.0.1:60001/", root, "case-01", request=request)
    assert [call[0] for call in calls] == ["http://127.0.0.1:60001/api/models/upload", "http://127.0.0.1:60001/api/sessions"]
    assert calls[0][1] == (case / "inputs/geometry_only.mph").read_bytes()
    assert calls[0][2] == {"Content-Type": "application/octet-stream", "X-File-Name": "geometry_only.mph"}
    assert calls[1][2] == {"Content-Type": "application/json"}
    assert all(call[3] == 120 for call in calls)
    goal = setup.public_goal(root, "case-01")
    assert json.loads(calls[1][1]) == {"goal": goal, "model_id": model["id"]}
    assert result["session"] is session and result["model"] is model
    manifest = result["input_manifest"]
    assert result["goal_chars"] == manifest["goal_chars"] == len(goal)
    assert manifest["goal_sha256"] == hashlib.sha256(goal.encode("utf-8")).hexdigest()
    assert len(manifest["files"]) == 7
    for item in manifest["files"]:
        blob = (root / item["path"]).read_bytes()
        assert item["bytes"] == len(blob)
        assert item["sha256"] == hashlib.sha256(blob).hexdigest()
    material = next(item for item in manifest["files"] if item["path"].endswith("/data/material-data.md"))
    assert material["role"] == "user_attachment"
    assert material["sha256"] == hashlib.sha256((case / "data/material-data.md").read_bytes()).hexdigest()
    again = setup.create_task("http://localhost:60002", root, "case-01", request=lambda *args, **kwargs: {"id": "other"})
    assert manifest == again["input_manifest"]


def test_oversized_complete_goal_fails_before_any_http_call(tmp_path):
    root, case = make_case(tmp_path)
    (case / "curve.csv").write_text("x" * 12000, encoding="utf-8")
    with pytest.raises(ValueError, match="no input was truncated"):
        setup.create_task("http://localhost:60001", root, "case-01", request=lambda *a, **kw: pytest.fail("HTTP must not be called"))


def test_upload_without_real_model_id_never_creates_session(tmp_path):
    root, _ = make_case(tmp_path)
    calls = []

    def request(url, **kwargs):
        calls.append(url)
        return {"facts": []}

    with pytest.raises(ValueError, match="model ID"):
        setup.create_task("http://localhost:60001", root, "case-01", request=request)
    assert len(calls) == 1


def test_transport_disables_proxy_and_redirects(monkeypatch):
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return b'{"id":"real"}'

    class Opener:
        def open(self, message, *, timeout):
            assert message.method == "POST" and message.data == b"payload"
            assert timeout == 120
            return Response()

    def build_opener(*handlers):
        observed["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(setup, "build_opener", build_opener)
    assert setup._post_json("http://localhost/api/test", data=b"payload", headers={}) == {"id": "real"}
    proxy, redirect = observed["handlers"]
    assert proxy.proxies == {}
    assert redirect.redirect_request(None, None, 302, "redirect", {}, "http://elsewhere") is None


def test_public_case_and_origin_cannot_escape_scope(tmp_path):
    root, _ = make_case(tmp_path)
    with pytest.raises(ValueError, match="case ID"):
        setup.public_goal(root, "../private")
    with pytest.raises(ValueError, match="loopback"):
        setup.create_task("https://remote.example", root, "case-01")


def test_nested_attachments_are_complete_and_labels_disambiguate_duplicate_names(tmp_path):
    root, case = make_case(tmp_path)
    (case / "data/deeper").mkdir()
    attachments = {"data/notes.md": b"Nested notes.\r\n",
                   "data/deeper/notes.md": b"Deep notes with exact trailing spaces.  \r\n",
                   "data/deeper/problem.md": b"A separately attached user problem.\n"}
    for name, blob in attachments.items():
        (case / name).write_bytes(blob)
    goal = setup.public_goal(root, "case-01")
    for name, blob in attachments.items():
        assert f"### 用户附件：{name}\n\n{blob.decode('utf-8')}\n\n" in goal
    assert goal.count("### 用户需求：problem.md\n\n") == 1
    assert "### 用户附件：notes.md\n\nAdditional user data.\n" in goal


def test_inputs_subtrees_derived_files_and_nontext_files_are_never_attached(tmp_path):
    root, case = make_case(tmp_path)
    (case / "inputs/deep").mkdir()
    (case / "data/inputs").mkdir()
    excluded = ["inputs/readme.md", "inputs/deep/curve.csv", "data/inputs/secret.txt",
                "data/geometry.json", "data/TOPOLOGY.JSON", "data/not-text.bin"]
    for name in excluded:
        (case / name).write_bytes(b"EXCLUDED_DERIVED_CONTENT")
    _, _, goal, files = setup._public_inputs(root, "case-01")
    assert "EXCLUDED_DERIVED_CONTENT" not in goal
    assert not any(path.relative_to(case).as_posix() in excluded for path, _, _ in files)


@pytest.mark.parametrize("alias_kind,is_directory", [("is_symlink", False), ("is_symlink", True), ("is_junction", True)])
def test_recursive_scan_rejects_aliases_before_descending_or_uploading(tmp_path, monkeypatch, alias_kind, is_directory):
    root, case = make_case(tmp_path)
    # Model alias detection without requiring Windows symlink privileges. A
    # nontext file must be checked too, rather than skipped by suffix first.
    alias = case / "data" / ("linked-dir" if is_directory else "linked-file.bin")
    if is_directory:
        alias.mkdir()
        (alias / "hidden-user-data.md").write_text("MUST_NOT_BE_VISITED", encoding="utf-8")
    else:
        alias.write_bytes(b"MUST_NOT_BE_READ")
    original_check = getattr(Path, alias_kind, lambda _path: False)
    monkeypatch.setattr(Path, alias_kind, lambda path: path == alias or original_check(path), raising=False)
    scanned = []
    original_scandir = os.scandir

    def scandir(path):
        scanned.append(Path(path))
        return original_scandir(path)

    monkeypatch.setattr(setup.os, "scandir", scandir)
    with pytest.raises(ValueError, match="filesystem aliases"):
        setup.create_task("http://localhost:60001", root, "case-01", request=lambda *a, **kw: pytest.fail("HTTP must not be called"))
    assert alias not in scanned
