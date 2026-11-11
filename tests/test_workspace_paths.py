"""Data-root relocation changes locators, not immutable engineering evidence."""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import shutil
from unittest.mock import patch
import zipfile

import pytest

from advisor.library_store import LibraryStore, encoded, file_hash
from advisor.sessions import SessionStore
from advisor.server import App
from advisor.workspace_paths import SCHEMA, WorkspacePaths


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_codec_internal_external_and_precise_prefix(tmp_path):
    root = tmp_path / "data"
    codec = WorkspacePaths(root)
    inside = root / "uploads" / "part.step"
    outside = tmp_path / "data-other" / "uploads" / "part.step"
    assert codec.encode(inside) == "uploads/part.step"
    assert codec.resolve("uploads/part.step") == inside
    assert codec.encode(outside) == str(outside)
    assert codec.resolve(outside) == outside
    for bad in ("../external.step", "uploads/../../external.step", "C:relative.step"):
        with pytest.raises(ValueError):
            codec.resolve(bad)


def test_repeated_move_remembers_all_old_roots(tmp_path):
    first, second, third = (tmp_path / name for name in ("first", "second with spaces", "third"))
    WorkspacePaths(first)
    shutil.copytree(first, second)
    two = WorkspacePaths(second)
    assert two.resolve(first / "uploads/part.step") == second / "uploads/part.step"
    shutil.copytree(second, third)
    three = WorkspacePaths(third)
    assert set(three.previous_roots) == {first, second}
    for origin in (first, second, third):
        assert three.resolve(origin / "uploads/part.step") == third / "uploads/part.step"
    assert WorkspacePaths(third).previous_roots == three.previous_roots


def test_legacy_active_is_an_exact_location_anchor(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    write(new / "libraries/search/active.json", {"directory": str(old / "libraries/search/generations/g-123")})
    codec = WorkspacePaths(new)
    assert codec.previous_roots == (old,)
    assert codec.resolve(old / "models/a.json") == new / "models/a.json"
    assert codec.resolve(tmp_path / "old-other/models/a.json") == tmp_path / "old-other/models/a.json"


def test_legacy_owned_receipt_anchors_sessions_without_search_index(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    write(new / "plugin-offline-setup-receipt.json", {"schema": "sim-advisor-plugin-offline-setup/v1", "dataDir": str(old)})
    assert WorkspacePaths(new).resolve(old / "uploads/input.step") == new / "uploads/input.step"
    other = tmp_path / "unrecognized"
    write(other / "plugin-offline-setup-receipt.json", {"schema": "unrecognized/v1", "dataDir": str(old)})
    assert not WorkspacePaths(other).previous_roots


@pytest.mark.parametrize("directory", ["other/generations/g-123", "libraries/search", "libraries/search/generations", "other/search/generations/g-123"])
def test_arbitrary_old_directory_is_not_authority(tmp_path, directory):
    root = tmp_path / "data"
    write(root / "libraries/search/active.json", {"directory": str(tmp_path / "old" / directory)})
    assert not WorkspacePaths(root).previous_roots


def test_filesystem_root_anchor_is_rejected(tmp_path):
    root = tmp_path / "data"
    write(root / "workspace-location.json", {"schema": SCHEMA, "current_root": str(Path(tmp_path.anchor)), "previous_roots": []})
    with pytest.raises(ValueError, match="absolute"):
        WorkspacePaths(root)


def test_resolved_symlink_cannot_escape_workspace(tmp_path):
    codec = WorkspacePaths(tmp_path / "data")
    outside = tmp_path / "external"
    outside.mkdir()
    link = codec.root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is not permitted")
    with pytest.raises(ValueError, match="escapes"):
        codec.resolve("linked/source.mph")


def test_snapshot_only_transforms_explicit_intake_locator(tmp_path):
    codec = WorkspacePaths(tmp_path / "data")
    model = {"path": str(codec.root / "uploads/a.step"), "facts": [{"value": str(codec.root / "observed/path")}],
             "geometry": {"product_name": str(codec.root / "not-a-locator")}}
    before = copy.deepcopy(model)
    saved = codec.model_snapshot(model, encode=True)
    assert saved["path"] == "uploads/a.step"
    assert codec.model_snapshot(saved) == before
    assert saved["facts"] == model["facts"] and saved["geometry"] == model["geometry"]
    assert model == before


def mph(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as out:
        out.writestr("modelinfo.xml", '<modelInfo comsolVersion="6.3" title="Fixture"/>')
        out.writestr("dmodel.xml", '<Model tag="m"><Study tag="std1"/></Model>')


@pytest.mark.parametrize("legacy", [False, True])
def test_library_move_reuses_mph_cache_and_keeps_citations(tmp_path, legacy):
    old = tmp_path / "original data"
    model = old / "sources" / "sample.mph"
    mph(model)
    store = LibraryStore(old / "libraries")
    lib = store.register("Fixture", "user-models", [{"id": "models", "path": str(model.parent)}])
    lib = store.set_enabled(lib["id"], True)
    with closing(store.connect()) as db:
        stored = json.loads(db.execute("SELECT body FROM libraries WHERE id=?", (lib["id"],)).fetchone()[0])
    assert stored["roots"][0]["path"] == "sources"
    with patch("advisor.model_reader.parser_status", return_value={"available": False, "detail": "Fixture; external parser disabled"}):
        result = store.build(lib["id"], deep=False, modes=[])
    assert result["status"] == "completed", result.get("failed", result)
    before = store.objects()
    cache = next((store.root / "artifacts").rglob("extraction.json"))
    if legacy:
        value = json.loads(cache.read_text(encoding="utf-8"))
        value["root_bindings"] = lib["roots"]
        for dependency in value["dependencies"]:
            dependency["path"] = str(store.paths.resolve(dependency["path"]))
        # The installation may move independently of the data. An explicitly
        # declared extractor can move only when its exact content hash matches.
        for declared in value["result"]["extractor_dependencies"]:
            previous = declared["path"]
            declared["path"] = str(tmp_path / "unavailable former app/scripts" / Path(previous).name)
            for dependency in value["dependencies"]:
                if dependency["path"] == previous:
                    dependency["path"] = declared["path"]
        write(cache, value)
        with closing(store.connect()) as db, db:
            db.execute("UPDATE libraries SET body=? WHERE id=?", (encoded(lib), lib["id"]))
        # Exercise the pre-anchor upgrade path too.
        store.paths.anchor_path.unlink()
        write(old / "libraries/search/active.json", {"directory": str(old / "libraries/search/generations/old-index")})
    original_cache = cache.read_bytes()
    original_blobs = {p.name: file_hash(p) for p in (store.root / "blobs").iterdir()}
    new = tmp_path / "moved data"
    shutil.copytree(old, new)
    old.rename(tmp_path / "retired old location")
    moved = LibraryStore(new / "libraries")
    assert moved.library(lib["id"])["roots"][0]["path"] == str(new / "sources")
    assert moved.objects() == before
    with patch("advisor.library_model.extract_case", side_effect=AssertionError("Must reuse extraction without reopening COMSOL")):
        job = moved.build(lib["id"], deep=False, modes=[])
    assert job["status"] == "completed", job
    assert moved.objects() == before
    assert next((moved.root / "artifacts").rglob("extraction.json")).read_bytes() == original_cache
    assert {p.name: file_hash(p) for p in (moved.root / "blobs").iterdir()} == original_blobs
    case = next(obj for obj in before if obj["type"] == "model_case")
    assert moved.configuration(case["id"], source="xml")["nodes"]


def test_cache_relocation_never_ignores_changed_content_or_bindings(tmp_path):
    from advisor.library_model import JAVA_SOURCE
    store = LibraryStore(tmp_path / "data/libraries")
    source = store.paths.root / "sources/a.mph"
    mph(source)
    lib = store.register("Fixture", "user-models", [{"id": "models", "path": str(source.parent)}])
    lib = store.set_enabled(lib["id"], True)
    cache = store.root / "artifacts/extraction.json"
    old_extractor = str(tmp_path / "old-app/ReadMph.java")
    candidate = {"deep": True, "parser_hash": "parser", "root_bindings": store._roots(lib["roots"], encode=True),
                 "result": {"objects": [], "extractor_dependencies": [{"path": old_extractor, "sha256": file_hash(JAVA_SOURCE)}]},
                 "dependencies": [{"path": old_extractor, "hash": file_hash(JAVA_SOURCE)},
                                  {"path": store.paths.encode(source), "hash": file_hash(source)}]}
    write(cache, candidate)
    assert store._cached_extraction(cache, lib, True, "parser") == candidate["result"]
    source.write_bytes(b"changed")
    assert store._cached_extraction(cache, lib, True, "parser") is None
    candidate["dependencies"].pop()
    candidate["dependencies"][0]["hash"] = "wrong"
    write(cache, candidate)
    assert store._cached_extraction(cache, lib, True, "parser") is None
    candidate["dependencies"] = []
    write(cache, candidate)
    assert store._cached_extraction(cache, lib, False, "parser") is None
    assert store._cached_extraction(cache, lib, True, "changed-parser") is None
    changed = dict(lib, roots=[dict(lib["roots"][0], path=str(tmp_path / "other"))])
    assert store._cached_extraction(cache, changed, True, "parser") is None


def test_external_sources_do_not_silently_retarget_same_filename(tmp_path):
    data, external = tmp_path / "data", tmp_path / "external"
    external.mkdir()
    source = external / "manual.txt"
    source.write_text("Original external evidence", encoding="utf-8")
    store = LibraryStore(data / "libraries")
    lib = store.register("External", "official-manuals", [{"path": str(external)}])
    lib = store.set_enabled(lib["id"], True)
    assert store.build(lib["id"], modes=[])["status"] == "completed"
    citation = store.objects()[0]["id"]
    new = tmp_path / "new"
    shutil.copytree(data, new)
    source.unlink()
    wrong = new / "external/manual.txt"
    wrong.parent.mkdir()
    wrong.write_text("Original external evidence", encoding="utf-8")
    moved = LibraryStore(new / "libraries")
    assert moved.library(lib["id"])["roots"][0]["path"] == str(external)
    with pytest.raises(ValueError, match="unavailable"):
        moved.read_evidence(citation)
    moved.rebind(lib["id"], "root0", wrong.parent)
    assert moved.read_evidence(citation)["source_hash_verified"]


@pytest.mark.parametrize("legacy", [False, True])
def test_moved_running_build_and_live_pid_lease_are_not_resumed(tmp_path, legacy):
    old = tmp_path / "old"
    store = LibraryStore(old / "libraries")
    source = old / "sources/a.txt"
    source.parent.mkdir()
    source.write_text("fixture text", encoding="utf-8")
    lib = store.register("Fixture", "official-manuals", [{"path": str(source.parent)}])
    lib = store.set_enabled(lib["id"], True)
    job_id = "a" * 32
    store._save_job({"id": job_id, "pid": os.getpid(), "status": "running", "started_at": "2026-01-01T00:00:00+00:00"})
    lease = {"pid": os.getpid(), "workspace_root": str(old), "job_id": job_id}
    if legacy:
        lease.pop("workspace_root")
        with closing(store.connect()) as db, db:
            job = json.loads(db.execute("SELECT body FROM builds WHERE id=?", (job_id,)).fetchone()[0])
            job.pop("workspace_root")
            db.execute("UPDATE builds SET body=? WHERE id=?", (encoded(job), job_id))
    write(store.root / ".build.lock", lease)
    new = tmp_path / "new"
    shutil.copytree(old, new)
    moved = LibraryStore(new / "libraries")
    with patch("advisor.library_store.process_alive", side_effect=AssertionError("Never interpret an old machine's PID")):
        assert moved.job(job_id)["status"] == "interrupted"
        assert moved.build(lib["id"], modes=[])["status"] == "completed"


@pytest.mark.parametrize("legacy", [False, True])
def test_sessions_and_model_intake_resolve_upload_after_move(tmp_path, legacy):
    old = tmp_path / "old"
    app = App(old, doc_root=tmp_path / "empty-docs", runtime_factory="none")
    upload = old / "uploads" / "input.step"
    upload.parent.mkdir()
    upload.write_text("not parsed; mocked snapshot", encoding="utf-8")
    snapshot = {"id": "model-12345678", "path": str(upload), "file_name": "input.step", "format": "step", "facts": [], "physics": [], "fingerprint": "same"}
    with patch("advisor.server.inspect_model", return_value=snapshot):
        parsed = app.inspect(str(upload))
    session = app.sessions.create("Check the geometry for the intended engineering task", parsed)
    if legacy:
        write(old / "models" / (parsed["id"] + ".json"), parsed)
        write(app.sessions.path(session["id"]), session)
    else:
        assert json.loads((old / "models" / (parsed["id"] + ".json")).read_text(encoding="utf-8"))["path"] == "uploads/input.step"
        assert json.loads(app.sessions.path(session["id"]).read_text(encoding="utf-8"))["model"]["path"] == "uploads/input.step"
    new = tmp_path / "new with spaces"
    shutil.copytree(old, new)
    old.rename(tmp_path / "retired")
    moved = App(new, doc_root=tmp_path / "empty-docs", runtime_factory="none")
    assert moved.model(parsed["id"])["path"] == str(new / "uploads/input.step")
    reloaded = moved.sessions.get(session["id"])
    assert reloaded["model"]["path"] == str(new / "uploads/input.step")
    assert reloaded["revision"] == session["revision"]
    assert reloaded["model"]["fingerprint"] == "same"
    moved.sessions.save_event(reloaded, "test", "fixture persisted after move")
    assert json.loads(moved.sessions.path(session["id"]).read_text(encoding="utf-8"))["model"]["path"] == "uploads/input.step"

