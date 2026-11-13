"""Generation purge lineage stays text-free and bounded at corpus scale."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

import pytest

from advisor import case_context_dependencies as dependencies
from advisor.case_context_store import pending_path
from advisor.ui_status import status_summary


def _generation(tmp_path, name="current"):
    root = tmp_path / "search"
    directory = root / "generations" / name
    directory.mkdir(parents=True)
    return root, {"directory": str(directory), "generation": "catalog-1", "corpus_hash": "immutable-corpus"}


def _profile(index, *, owner="models", source="manuals"):
    return {"case_id": "case-" + str(index), "library_id": owner,
            "qmd_path": "lib_" + "a" * 20 + "/case-" + str(index) + ".md",
            "source_refs": [{"id": "source-" + str(index), "library_id": source}],
            "text": "EXCERPT BODY MUST NEVER ENTER A GENERATION SIDECAR",
            "excerpts": [{"text": "MODEL OR TUTORIAL BODY MUST NEVER BE DUPLICATED"}]}


def _mark(root, active, profiles):
    return dependencies.mark_generation_dependencies(root, active, profiles)


def _path(root, manifest):
    return root / manifest[dependencies.MANIFEST_KEY]["path"]


def _publish(active):
    (Path(active["directory"]) / "manifest.json").write_text(json.dumps(active), encoding="utf-8")


def test_metadata_only_generation_snapshot_counts_owner_and_exact_source_libraries(tmp_path):
    root, active = _generation(tmp_path)
    profiles = {"case-0": _profile(0), "case-1": _profile(1, source="closed-manuals")}
    marked = _mark(root, dict(active, case_context_dependencies={"obsolete": ["old"]}), profiles)
    descriptor = marked[dependencies.MANIFEST_KEY]
    assert "case_context_dependencies" not in marked
    assert descriptor["context_count"] == marked["case_context_count"] == 2
    assert descriptor["dependency_count"] == 2
    assert dependencies.dependent_case_ids(root, marked, "models") == {"case-0", "case-1"}
    assert dependencies.dependent_case_ids(root, marked, "manuals") == {"case-0"}
    assert dependencies.dependent_case_ids(root, marked, "closed-manuals") == {"case-1"}
    path = _path(root, marked)
    assert "EXCERPT BODY" not in path.read_bytes().decode("utf-8", errors="ignore")
    assert "MODEL OR TUTORIAL BODY" not in path.read_bytes().decode("utf-8", errors="ignore")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT source_id FROM dependencies ORDER BY case_id").fetchall() == [("source-0",), ("source-1",)]
        assert {row[1] for row in db.execute("PRAGMA table_info(contexts)")} == {"case_id", "library_id", "qmd_path"}
    assert len(json.dumps(status_summary(marked))) < 1000


def test_immutable_snapshots_keep_rollback_authority_then_prune_only_published_generation(tmp_path):
    root, active = _generation(tmp_path)
    _, other = _generation(tmp_path, "retained")
    original = _mark(root, active, {"case-0": _profile(0), "case-1": _profile(1)})
    retained = _mark(root, other, {"case-2": _profile(2)})
    updated = dependencies.remove_generation_dependencies(root, original, {"case-0"})
    assert dependencies.dependent_case_ids(root, original, "manuals") == {"case-0", "case-1"}
    assert dependencies.dependent_case_ids(root, updated, "manuals") == {"case-1"}
    assert updated["case_context_count"] == 1 and _path(root, original) != _path(root, updated)
    untouched = Path(active["directory"]) / "case-context-dependencies-not-a-generated-snapshot.sqlite3"
    untouched.write_bytes(b"unowned name must be preserved")
    _publish(original)
    with pytest.raises(ValueError, match="published"):
        dependencies.prune_generation_dependencies(root, updated)
    assert _path(root, original).exists()
    _publish(updated)
    pending_path(root).parent.mkdir()
    pending_path(root).write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="pending"):
        dependencies.prune_generation_dependencies(root, updated)
    pending_path(root).unlink()
    assert dependencies.prune_generation_dependencies(root, updated) == {"removed_snapshots": 1}
    assert _path(root, updated).exists() and _path(root, retained).exists() and untouched.exists()
    assert not _path(root, original).exists()


def test_original_generation_binding_ignores_representation_file_mtime_and_is_portable(tmp_path):
    root, active = _generation(tmp_path)
    objects = Path(active["directory"]) / "objects.sqlite3"
    objects.write_bytes(b"statistics file identity is not context source identity")
    marked = _mark(root, active, {"case-0": _profile(0)})
    info = objects.stat()
    os.utime(objects, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    assert _mark(root, active, {"case-0": _profile(0)})[dependencies.MANIFEST_KEY] == marked[dependencies.MANIFEST_KEY]
    relative = dict(marked, directory="generations/current")
    assert dependencies.dependent_case_ids(root, relative, "manuals") == {"case-0"}
    root.rename(tmp_path / "relocated-search")
    assert dependencies.dependent_case_ids(tmp_path / "relocated-search", relative, "manuals") == {"case-0"}


@pytest.mark.parametrize("locator", ["../foreign.sqlite3", "generations/retained/context.sqlite3", "C:/foreign.sqlite3",
                                    "generations/current/../foreign.sqlite3"])
def test_foreign_descriptor_paths_are_rejected(tmp_path, locator):
    root, active = _generation(tmp_path)
    marked = _mark(root, active, {"case-0": _profile(0)})
    marked[dependencies.MANIFEST_KEY]["path"] = locator
    with pytest.raises(ValueError, match="locator"):
        dependencies.dependent_case_ids(root, marked, "manuals")


@pytest.mark.parametrize("directory", ["../foreign", "generations/../foreign", "generations/current/extra"])
def test_foreign_generation_paths_are_rejected(tmp_path, directory):
    root, active = _generation(tmp_path)
    with pytest.raises(ValueError):
        _mark(root, dict(active, directory=directory), {"case-0": _profile(0)})


def test_tampering_and_cross_generation_reuse_fail_closed(tmp_path):
    root, active = _generation(tmp_path)
    marked = _mark(root, active, {"case-0": _profile(0)})
    with pytest.raises(ValueError, match="binding"):
        dependencies.dependent_case_ids(root, dict(marked, generation="different"), "manuals")
    with closing(sqlite3.connect(_path(root, marked))) as db, db:
        db.execute("UPDATE dependencies SET library_id='wrong'")
    with pytest.raises(ValueError, match="binding"):
        dependencies.dependent_case_ids(root, marked, "manuals")


def test_legacy_inline_maps_remain_readable_and_can_be_shrunk_without_profile_reads(tmp_path):
    root, active = _generation(tmp_path)
    active.update(case_context_dependencies={"case-a": ["models", "manuals"], "case-b": ["models", "other"]},
                  case_context_count=2)
    assert dependencies.dependent_case_ids(root, active, "manuals") == {"case-a"}
    updated = dependencies.remove_generation_dependencies(root, active, {"case-a"})
    assert updated["case_context_dependencies"] == {"case-b": ["models", "other"]}
    assert updated["case_context_count"] == 1
    assert active["case_context_count"] == 2 and len(active["case_context_dependencies"]) == 2
    assert not list(Path(active["directory"]).glob("*.sqlite3"))


def test_rollback_to_legacy_manifest_prunes_new_unreferenced_metadata_snapshot(tmp_path):
    root, active = _generation(tmp_path)
    legacy = dict(active, case_context_dependencies={"case-0": ["models", "manuals"]})
    unpublished = _mark(root, active, {"case-0": _profile(0)})
    _publish(legacy)
    assert dependencies.prune_generation_dependencies(root, legacy) == {"removed_snapshots": 1}
    assert not _path(root, unpublished).exists()


def test_hardlinked_snapshot_cannot_be_read_or_pruned(tmp_path):
    root, active = _generation(tmp_path)
    original = _mark(root, active, {"case-0": _profile(0)})
    foreign = tmp_path / "foreign-link.sqlite3"
    try:
        os.link(_path(root, original), foreign)
    except OSError:
        pytest.skip("This environment does not permit creating hardlinks")
    with pytest.raises(ValueError, match="unshared"):
        dependencies.dependent_case_ids(root, original, "manuals")
    empty = _mark(root, active, {})
    _publish(empty)
    with pytest.raises(ValueError, match="shared"):
        dependencies.prune_generation_dependencies(root, empty)
    assert _path(root, original).exists() and foreign.exists()


def test_two_thousand_profiles_stream_scalar_metadata_without_reading_source_text(tmp_path, monkeypatch):
    root, active = _generation(tmp_path)
    runtime = root / "runtime" / "case-contexts.sqlite3"
    runtime.parent.mkdir()
    with closing(sqlite3.connect(runtime)) as db, db:
        db.executescript("""CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE contexts(case_id TEXT,library_id TEXT,qmd_path TEXT,profile_json TEXT);
          CREATE TABLE dependencies(case_id TEXT,source_id TEXT,library_id TEXT);""")
        db.executemany("INSERT INTO meta VALUES(?,?)", ((key, json.dumps(active[key])) for key in ("generation", "corpus_hash")))
        for index in range(2000):
            entry = _profile(index)
            # Invalid JSON catches accidental profile hydration as well.
            db.execute("INSERT INTO contexts VALUES(?,?,?,?)", (entry["case_id"], entry["library_id"], entry["qmd_path"],
                       "INVALID JSON; EXCERPT BODY MUST NEVER ENTER A GENERATION SIDECAR"))
            db.execute("INSERT INTO dependencies VALUES(?,?,?)", (entry["case_id"], "source-" + str(index), "manuals"))
    statements = []
    original = dependencies._readonly
    def traced(root, path):
        db = original(root, path)
        if path == runtime:
            db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(dependencies, "_readonly", traced)
    marked = dependencies.mark_generation_dependencies(root, active)
    assert marked["case_context_count"] == 2000
    assert len(dependencies.dependent_case_ids(root, marked, "manuals")) == 2000
    assert all("profile_json" not in statement.lower() for statement in statements)
    assert "EXCERPT BODY" not in _path(root, marked).read_bytes().decode("utf-8", errors="ignore")
    assert len(json.dumps(status_summary(marked))) < 1000
    assert _path(root, marked).stat().st_size < 1024 * 1024


def test_empty_or_conflicting_source_ownership_never_creates_an_unguarded_case(tmp_path):
    root, active = _generation(tmp_path)
    missing = _profile(0)
    missing["source_refs"] = []
    missing["excerpts"] = []
    with pytest.raises(ValueError, match="exact source"):
        _mark(root, active, {"case-0": missing})
    conflicting = _profile(0)
    conflicting["source_refs"].append({"id": "source-0", "library_id": "another-owner"})
    with pytest.raises(ValueError, match="conflicting"):
        _mark(root, active, {"case-0": conflicting})
    assert not list(Path(active["directory"]).iterdir())
