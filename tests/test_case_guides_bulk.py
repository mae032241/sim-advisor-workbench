"""Full-library guides remain incremental, bounded and live-authorized."""
import copy
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from advisor import case_guides as guides
from advisor.build_control import BuildCancelled
from test_case_guides import Store, prepared


@pytest.fixture
def batch(tmp_path, monkeypatch):
    store = Store(tmp_path)
    identities = [f"case-{index:05}" for index in range(1200)]
    store.sources.update({identity: {"id": identity, "library_id": "models", "type": "model_case", "title": identity}
                          for identity in identities})
    navigation = {"meta": {"generation": "g", "corpus_hash": "h", "objects_signature": [[1, 2]],
                           "prepared_case_count": len(identities)}}
    monkeypatch.setattr(guides, "_navigation", lambda _: ({}, copy.deepcopy(navigation)))
    monkeypatch.setattr(guides, "_scope", lambda *args: None)
    from advisor import case_navigation
    monkeypatch.setattr(case_navigation, "iter_prepared_case_ids", lambda _: (identity for identity in identities))
    calls = []
    def derive(store, authority, navigation):
        calls.append(authority["id"])
        reference = guides._reference(authority, "misc", "overview")
        return {"state": "ready", "case_id": authority["id"], "library_id": "models", "overview": reference,
                "stages": [], "source_refs": [reference], "gaps": [], "is_read_evidence": False}
    monkeypatch.setattr(guides, "_guide", derive)
    monkeypatch.setattr(guides, "_available_references", lambda _, refs: {ref["id"] for ref in refs})
    return store, navigation, identities, calls


def test_all_streams_and_resumes_without_a_library_json_or_all_id_report(batch):
    store, _, identities, calls = batch
    progress = []
    result = guides.build_case_guides(store, progress=progress.append)
    assert result["count"] == result["built_count"] == result["stored_count"] == 1200
    assert "case_ids" not in result and len(json.dumps(result)) < 2000
    assert not guides._path(store).exists()
    assert len(progress) == 1200 and progress[-1]["completed"] == 1200
    calls.clear()
    result = guides.build_case_guides(store)
    assert result["skipped_count"] == 1200 and result["built_count"] == 0 and not calls
    result = guides.get_case_guide(store, identities[-1])
    assert result["case_id"] == identities[-1] and result["state"] == "ready"


def test_selected_cases_have_no_five_case_limit_and_preserve_other_rows(batch):
    store, _, identities, _ = batch
    guides.build_case_guides(store, identities[:200])
    result = guides.build_case_guides(store, identities[200:400])
    assert result["count"] == 200 and result["stored_count"] == 400
    assert "case_ids" not in result
    assert guides.get_case_guide(store, identities[0])["state"] == "ready"


def test_failures_are_committed_separately_and_retry_only_failed_cases(batch, monkeypatch):
    store, _, identities, calls = batch
    original = guides._guide
    def fail_one(store, authority, navigation):
        if authority["id"] == identities[2]:
            raise ValueError("Pinned compact artifact is missing")
        return original(store, authority, navigation)
    monkeypatch.setattr(guides, "_guide", fail_one)
    result = guides.build_case_guides(store, identities[:5])
    assert result["state"] == "partial" and result["failed_count"] == 1 and result["count"] == 4
    assert result["failures"][0]["case_id"] == identities[2]
    with sqlite3.connect(guides.database_path(store)) as db:
        assert db.execute("SELECT count(*) FROM failures").fetchone()[0] == 1
    calls.clear()
    monkeypatch.setattr(guides, "_guide", original)
    result = guides.build_case_guides(store, identities[:5])
    assert result["state"] == "ready" and result["skipped_count"] == 4 and calls == [identities[2]]


def test_cancellation_retains_only_completed_atomic_cases_and_can_resume(batch, monkeypatch):
    store, _, identities, calls = batch
    original = guides._guide
    def interrupt(store, authority, navigation):
        if authority["id"] == identities[3]:
            raise BuildCancelled()
        return original(store, authority, navigation)
    monkeypatch.setattr(guides, "_guide", interrupt)
    with pytest.raises(BuildCancelled):
        guides.build_case_guides(store, identities[:8])
    with sqlite3.connect(guides.database_path(store)) as db:
        assert db.execute("SELECT count(*) FROM guides").fetchone()[0] == 3
    monkeypatch.setattr(guides, "_guide", original)
    result = guides.build_case_guides(store, identities[:8])
    assert result["skipped_count"] == 3 and result["built_count"] == 5


def test_binding_changes_cannot_resume_old_guides_or_commit_a_mixed_case(batch, monkeypatch):
    store, navigation, identities, _ = batch
    guides.build_case_guides(store, identities[:2])
    navigation["meta"]["objects_signature"] = [[4, 5]]
    assert guides.get_case_guide(store, identities[0])["state"] == "need_rebuild"
    result = guides.build_case_guides(store, identities[:1])
    assert result["built_count"] == result["stored_count"] == 1
    original = guides._guide
    def change(store, authority, nav):
        value = original(store, authority, nav)
        navigation["meta"]["generation"] = "different"
        return value
    monkeypatch.setattr(guides, "_guide", change)
    with pytest.raises(RuntimeError, match="changed"):
        guides.build_case_guides(store, identities[1:2])
    with sqlite3.connect(guides.database_path(store)) as db:
        assert db.execute("SELECT case_id FROM guides").fetchall() == [(identities[0],)]


def test_empty_all_prepared_set_succeeds(batch, monkeypatch):
    store, navigation, _, _ = batch
    from advisor import case_navigation
    navigation["meta"]["prepared_case_count"] = 0
    monkeypatch.setattr(case_navigation, "iter_prepared_case_ids", lambda _: iter(()))
    result = guides.build_case_guides(store)
    assert result["state"] == "ready" and result["count"] == result["stored_count"] == 0
    with pytest.raises(ValueError):
        guides.build_case_guides(store, [])


@pytest.mark.parametrize("identity", ["models:pilot.mph", "pilot.mph", "models/pilot", "C:\\models\\pilot.mph"])
def test_asset_keys_and_paths_are_rejected_before_permission_or_payload_checks(batch, monkeypatch, identity):
    store, _, _, _ = batch
    monkeypatch.setattr(store, "source_available", lambda _: pytest.fail("Invalid identity reached the permission gate"))
    with pytest.raises(ValueError, match="exact model_case citation"):
        guides.get_case_guide(store, identity)
    assert not store.gets


def test_closed_sources_remove_saved_api_property_hints_and_callable_routes(prepared):
    store, _ = prepared
    guides.build_case_guides(store, ["case"])
    visible = guides.get_case_guide(store, "case")
    requests = [request for stage in visible["stages"] for request in stage["configuration_requests"]]
    assert {request["source"] for request in requests} == {"saved", "api"}
    assert all("source_ids" not in request for request in requests)
    store.blocked.update({"mesh", "api", "tutorial"})
    hidden = guides.get_case_guide(store, "case")
    assert all(not stage["source_refs"] and not stage["observed"] and not stage["configuration_requests"]
               for stage in hidden["stages"])
    assert "lambda/6" not in json.dumps(hidden) and "Documented mesh step" not in json.dumps(hidden)


def test_legacy_json_remains_readable_but_sqlite_never_falls_back_to_it(prepared):
    store, navigation = prepared
    guide = guides._guide(store, store.get("case"), navigation)
    path = guides._path(store)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": guides.LEGACY_SCHEMA, "binding": guides._binding(navigation),
                                "cases": {"case": guide}}), encoding="utf-8")
    assert guides.get_case_guide(store, "case")["state"] == "ready"
    guides.database_path(store).write_bytes(b"invalid SQLite cache")
    assert guides.get_case_guide(store, "case")["state"] == "need_rebuild"


@pytest.mark.parametrize("link_kind", ["hardlink", "symlink"])
def test_aliased_guide_storage_is_refused_without_modifying_its_target(batch, tmp_path, link_kind):
    store, _, identities, _ = batch
    target = tmp_path / "unrelated.sqlite3"
    target.write_bytes(b"must remain unchanged")
    path = guides.database_path(store)
    path.parent.mkdir(parents=True)
    try:
        (os.link if link_kind == "hardlink" else os.symlink)(target, path)
    except OSError:
        pytest.skip("This account cannot create this link type")
    with pytest.raises(ValueError):
        guides.build_case_guides(store, identities[:1])
    assert guides.get_case_guide(store, identities[0])["state"] == "need_rebuild"
    assert target.read_bytes() == b"must remain unchanged"


def test_scalar_reference_gate_hides_retired_document_copies_and_closed_alias_targets(tmp_path):
    path = tmp_path / "owners.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE revisions(id TEXT,library_id TEXT,object_id TEXT);
            CREATE TABLE tutorial_retired_sources(citation_id TEXT,reason TEXT);
            CREATE TABLE tutorial_associations(source_id TEXT,target_library_id TEXT);
            CREATE TABLE tutorial_aliases(citation_id TEXT,owner_library_id TEXT,target_id TEXT,target_library_id TEXT);""")
        db.executemany("INSERT INTO revisions VALUES(?,?,?)", [("old", "models", "old-logical"),
                       ("alias", "models", "alias-logical"), ("canonical", "docs", "canonical-logical")])
        db.execute("INSERT INTO tutorial_retired_sources VALUES('old','model_tutorial_copy')")
        db.execute("INSERT INTO tutorial_associations VALUES('old','docs')")
        db.execute("INSERT INTO tutorial_aliases VALUES('alias','models','canonical','docs')")
    visible = {"models"}
    blocked = set()
    store = SimpleNamespace(connect=lambda: sqlite3.connect(path), searchable_library_ids=lambda: visible,
                            evaluation_policy=lambda: SimpleNamespace(blocks_identity=lambda identity, logical=None: identity in blocked))
    refs = [{"id": identity} for identity in ("old", "alias", "canonical")]
    assert guides._available_references(store, refs) == set()
    visible.add("docs")
    assert guides._available_references(store, refs) == {"old", "alias", "canonical"}
    blocked.add("canonical")
    assert "alias" not in guides._available_references(store, refs)
    blocked.clear()
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM revisions WHERE id='canonical'")
    assert "alias" not in guides._available_references(store, refs)


@pytest.mark.parametrize("unsafe", ["hardlink", "reparse", "runtime_reparse"])
def test_runtime_ownership_guards_reject_unshared_and_reparse_metadata(batch, monkeypatch, unsafe):
    store, _, identities, _ = batch
    path = guides.database_path(store)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"unchanged")
    checked = path.parent if unsafe == "runtime_reparse" else path
    original = Path.lstat
    def lstat(value, *args, **kwargs):
        info = original(value, *args, **kwargs)
        if value == checked:
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=2 if unsafe == "hardlink" else 1,
                                   st_file_attributes=0 if unsafe == "hardlink" else 0x400)
        return info
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError):
        guides.build_case_guides(store, identities[:1])
    assert guides.get_case_guide(store, identities[0])["state"] == "need_rebuild"
    assert path.read_bytes() == b"unchanged"


def test_members_do_not_decode_unselected_implementation_payloads(tmp_path, monkeypatch):
    catalog, navigation = tmp_path / "catalog.sqlite3", tmp_path / "navigation.sqlite3"
    with sqlite3.connect(catalog) as db:
        db.executescript("CREATE TABLE revisions(id TEXT,library_id TEXT,object_id TEXT);"
                         "CREATE TABLE case_members(citation_id TEXT,local_id TEXT);")
        db.executemany("INSERT INTO revisions VALUES(?,?,?)", [(f"node-{n}", "models", str(n)) for n in range(2000)])
        db.executemany("INSERT INTO case_members VALUES(?,?)", [(f"node-{n}", f"compact-node-{n}-1") for n in range(2000)])
    with sqlite3.connect(navigation) as db:
        db.execute("CREATE TABLE members(case_id TEXT,object_id TEXT,role TEXT,stage TEXT,global_visible INTEGER)")
        db.executemany("INSERT INTO members VALUES('case',?,'implementation','mesh',1)", [(f"node-{n}",) for n in range(2000)])
        db.execute("INSERT INTO members VALUES('case','display','implementation','mesh',0)")
    gets = []
    def get(identity):
        gets.append(identity)
        node_id = int(identity.removeprefix("node-"))
        return {"id": identity, "library_id": "models", "type": "saved_configuration", "title": identity,
                "data": {"compact_ref": {"node_id": node_id}}}
    store = SimpleNamespace(root=tmp_path, connect=lambda: sqlite3.connect(catalog), get=get,
                            require_not_heldout=lambda *args: None,
                            evaluation_policy=lambda: SimpleNamespace(blocks_identity=lambda *args: False))
    nodes = ({"id": n, "feature_branch": "mesh", "type": "Size", "label": str(n), "properties": []} for n in range(2000))
    monkeypatch.setattr(guides, "_reader", lambda *args: SimpleNamespace(iter_nodes=lambda: nodes))
    result = guides._guide(store, {"id": "case", "library_id": "models"}, {"path": str(navigation)})
    assert len(gets) == 8
    mesh = next(stage for stage in result["stages"] if stage["id"] == "mesh")
    assert len(mesh["observed"]) == len(mesh["configuration_requests"]) == 8
    assert mesh["total_node_count"] == 2000 and mesh["selected_node_count"] == 8 and mesh["summary_truncated"]
