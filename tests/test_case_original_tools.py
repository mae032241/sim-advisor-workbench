"""Original saved XML access stays exact-case, bounded, offline and revocable."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import zipfile

import pytest

from advisor.case_original_tools import CaseOriginalInspector, TOOL_NAME, tool_definition
from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_store import LibraryStore
from advisor.raw_resource_tools import RawResourceTools


def write_mph(path, xml):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("modelinfo.xml", '<modelInfo title="Original snapshot"/>')
        archive.writestr("dmodel.xml", xml)
        archive.writestr("solution.bin", b"not opened")
    return path


def publish_case(store, source, *, data=None, kind="model_case", local_id="model-case"):
    """Register an immutable overview without building any parsed artifacts."""
    library = store.library("cases")
    record = {"root_id": "models", "relative_path": source.name, "path": str(source),
              "content_hash": hashlib.sha256(source.read_bytes()).hexdigest()}
    obj = store._prepare(library, record, {"local_id": local_id, "type": kind,
        "title": "Synthetic original case", "text": "Overview only", "data": data or {},
        "evidence": [{"asset_path": str(source), "locator": "original", "method": "fixture"}]})
    obj = store._pin_case_group([obj])[0]
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (obj["id"], obj["object_id"], "cases", json.dumps(obj)))
        db.execute("INSERT OR REPLACE INTO active VALUES(?,?,?)", ("cases", obj["object_id"], obj["id"]))
        if obj.get("case_group"):
            db.execute("INSERT OR REPLACE INTO case_members VALUES(?,?,?)",
                       (obj["case_group"], local_id, obj["id"]))
    return obj


@pytest.fixture
def fixture(tmp_path):
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<Model>'
           '<View tag="view1"/>'
           '<PhysicsList><Physics tag="heat" op="HeatTransfer">'
           '<PhysicsFeature tag="wall" op="Wall"><active>false</active>'
           '<propertyValue name="p:value" value="0.42"/>'
           '</PhysicsFeature></Physics></PhysicsList></Model>')
    source = write_mph(tmp_path / "sources" / "original.mph", xml)
    store = LibraryStore(tmp_path / "catalog")
    store.register("Synthetic cases", "user-models", [{"id": "models", "path": str(source.parent)}],
                   library_id="cases")
    store.set_enabled("cases", True)
    case = publish_case(store, source)
    return store, case, source, xml


def bounded(page):
    assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= 7000
    assert page["basis"] == "original_saved_xml"
    assert page["is_read_evidence"] is False
    assert "evidence_refs" not in page and "evidence_refs" not in page["raw_result"]
    return page["raw_result"]


def test_real_native_original_needs_no_compact_build_or_comsol(fixture, monkeypatch):
    store, case, source, xml = fixture
    import subprocess
    from advisor import compact_model, library_model, model_reader
    def prohibited(*args, **kwargs):
        pytest.fail("Original source inspection must stay offline and independent of built artifacts")
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    monkeypatch.setattr(store, "_compact_context", prohibited)
    monkeypatch.setattr(store, "read", prohibited)
    monkeypatch.setattr(compact_model, "parse_compact_model", prohibited)
    monkeypatch.setattr(library_model, "extract_case", prohibited)
    monkeypatch.setattr(model_reader, "inspect_model", prohibited)
    opened, original_open = [], zipfile.ZipFile.open
    def track(archive, name, *args, **kwargs):
        opened.append(getattr(name, "filename", name))
        return original_open(archive, name, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, "open", track)
    tools = CaseOriginalInspector(store)
    first = bounded(tools.call({"case_id": case["id"]}))
    assert first["path"] == str(source) and first["sha256"] == case["asset_hash"]
    assert [(item["node_path"], item["element"]) for item in first["items"]] == [
        ("/0", "View"), ("/1", "PhysicsList")]
    assert first["node"]["node_path"] == "/" and first["cache_reused"] is False
    full = bounded(tools.call({"case_id": case["id"], "view": "xml", "length": 3200}))
    assert full["text"] == xml and full["representation"] == "original_decoded_xml_member"
    assert full["cache_reused"] is True
    child = bounded(tools.call({"case_id": case["id"], "view": "xml", "node_path": "/1/0/0"}))
    assert 'tag="wall"' in child["text"] and '<active>false</active>' in child["text"]
    assert child["representation"] == "serialized_xml_subtree" and not child["original_markup_layout_preserved"]
    info = bounded(tools.call({"case_id": case["id"], "member": "modelinfo.xml", "view": "xml"}))
    assert "Original snapshot" in info["text"]
    assert child["effective_activation"] == child["physical_validation"] == "not_evaluated"
    assert not child["binary_mesh_or_solution_read"]
    assert set(opened) == {"dmodel.xml", "modelinfo.xml"}
    assert not (store.root / "case-tree").exists()
    assert "compact_case" not in case["data"]


def test_contract_rejects_arbitrary_paths_and_unbounded_pages_before_read(fixture, monkeypatch):
    store, case, _, _ = fixture
    definition = tool_definition()
    assert definition["name"] == TOOL_NAME == "inspect_case_source"
    properties = definition["inputSchema"]["properties"]
    assert "path" not in properties and not definition["inputSchema"]["additionalProperties"]
    assert definition["inputSchema"]["required"] == ["case_id"]
    assert properties["length"]["maximum"] == 3200 and properties["limit"]["maximum"] == 20
    monkeypatch.setattr(store, "get", lambda *_: pytest.fail("Invalid arguments must fail before reading"))
    tools = CaseOriginalInspector(store)
    for arguments in ({}, {"case_id": " "}, {"case_id": case["id"], "path": "private.mph"},
                      {"case_id": case["id"], "length": 3201}, {"case_id": case["id"], "limit": True},
                      {"case_id": case["id"], "offset": -1}, {"case_id": case["id"], "node_path": "compact:node/1"},
                      {"case_id": case["id"], "member": "solution.bin"},
                      {"case_id": case["id"], "view": "api"}, {"case_id": case["id"], "code": "anything"}):
        with pytest.raises(ValueError):
            tools.call(arguments)


@pytest.mark.parametrize("blocked", ["closed", "deleted", "heldout_id", "heldout_hash", "heldout_group", "unavailable"])
def test_cached_xml_never_caches_identity_authorization(fixture, monkeypatch, blocked):
    store, case, _, _ = fixture
    tools = CaseOriginalInspector(store)
    bounded(tools.call({"case_id": case["id"]}))
    if blocked == "closed":
        store.set_enabled("cases", False)
    elif blocked == "deleted":
        store.delete_library("cases")
    elif blocked == "heldout_id":
        monkeypatch.setattr(store, "evaluation_policy", lambda: HoldoutPolicy(citation_ids=frozenset({case["id"]})))
    elif blocked == "heldout_hash":
        monkeypatch.setattr(store, "evaluation_policy", lambda: HoldoutPolicy(source_hashes=frozenset({case["asset_hash"]})))
    elif blocked == "heldout_group":
        monkeypatch.setattr(store, "evaluation_policy", lambda: HoldoutPolicy(case_groups=frozenset({case["case_group"]})))
    else:
        monkeypatch.setattr(store, "source_available", lambda *_: False)
    monkeypatch.setattr(RawResourceTools, "call", lambda *_: pytest.fail("Revoked case must not reach native reader"))
    with pytest.raises((PermissionError, FileNotFoundError)):
        tools.call({"case_id": case["id"], "view": "xml"})


@pytest.mark.parametrize("identity", ["missing", "asset", "filename", "configuration"])
def test_only_exact_registered_model_case_ids_are_accepted(fixture, identity):
    store, case, source, _ = fixture
    if identity == "configuration":
        other = publish_case(store, source, kind="saved_configuration", local_id="configuration")
        value = other["id"]
    else:
        value = {"missing": "unknown-model-case", "asset": case["asset_key"], "filename": str(source)}[identity]
    with pytest.raises(ValueError, match="exact model_case"):
        CaseOriginalInspector(store).call({"case_id": value})


def test_replacement_never_reuses_or_parses_a_previous_revision(fixture, monkeypatch):
    store, case, source, _ = fixture
    tools = CaseOriginalInspector(store)
    bounded(tools.call({"case_id": case["id"]}))
    write_mph(source, '<Model><Unrelated tag="private"/></Model>')
    monkeypatch.setattr(RawResourceTools, "call", lambda *_: pytest.fail("A replaced original must fail before parsing"))
    with pytest.raises(PermissionError, match="SHA256"):
        tools.call({"case_id": case["id"], "view": "xml"})


def test_unchanged_pages_reuse_hash_but_a_new_physical_version_is_rehashed(fixture, monkeypatch):
    store, case, source, _ = fixture
    tools = CaseOriginalInspector(store)
    opens = {"verification": 0, "native_hash": 0}
    actual_os_open, actual_path_open, actual_get = os.open, Path.open, store.get
    authorizations = []
    def os_open(path, *args, **kwargs):
        if Path(path) == source:
            opens["verification"] += 1
        return actual_os_open(path, *args, **kwargs)
    def path_open(path, *args, **kwargs):
        if path == source:
            opens["native_hash"] += 1
        return actual_path_open(path, *args, **kwargs)
    def get(identity):
        authorizations.append(identity)
        return actual_get(identity)
    monkeypatch.setattr(os, "open", os_open)
    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(store, "get", get)
    assert not bounded(tools.call({"case_id": case["id"]}))["cache_reused"]
    assert opens == {"verification": 1, "native_hash": 1}
    for arguments in ({"offset": 1, "limit": 1}, {"view": "xml", "length": 80},
                      {"view": "xml", "offset": 80, "length": 80},
                      {"view": "xml", "member": "modelinfo.xml"}):
        assert bounded(tools.call({"case_id": case["id"], **arguments}))["cache_reused"]
    assert opens == {"verification": 1, "native_hash": 1}
    assert authorizations == [case["id"]] * 10  # Fresh authorization before and after each page.

    # Even unchanged bytes get a new hash when the physical file version moves.
    info = source.stat()
    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    changed = bounded(tools.call({"case_id": case["id"], "view": "xml"}))
    assert not changed["cache_reused"] and changed["sha256"] == case["asset_hash"]
    assert opens == {"verification": 2, "native_hash": 2}
    write_mph(source, '<Model><Replaced/></Model>')
    with pytest.raises(PermissionError, match="SHA256"):
        tools.call({"case_id": case["id"]})
    assert opens == {"verification": 3, "native_hash": 2}


def test_hash_cache_is_bound_to_expected_immutable_digest(fixture, monkeypatch):
    store, case, source, _ = fixture
    tools = CaseOriginalInspector(store)
    bounded(tools.call({"case_id": case["id"]}))
    actual_open, actual_get = os.open, store.get
    opened = []
    def track(path, *args, **kwargs):
        if Path(path) == source:
            opened.append(path)
        return actual_open(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", track)
    monkeypatch.setattr(store, "get", lambda identity: dict(actual_get(identity), asset_hash="0" * 64))
    monkeypatch.setattr(RawResourceTools, "call", lambda *_: pytest.fail("Wrong immutable digest must fail before parsing"))
    with pytest.raises(PermissionError, match="SHA256"):
        tools.call({"case_id": case["id"]})
    assert len(opened) == 1


def test_cached_verification_rechecks_guards_on_both_sides_of_lookup(fixture, monkeypatch):
    store, case, source, _ = fixture
    tools = CaseOriginalInspector(store)
    bounded(tools.call({"case_id": case["id"]}))
    actual_guard, guards = RawResourceTools._guard, []
    def guard(reader, path, **kwargs):
        guards.append(path)
        if len(guards) == 2:
            write_mph(source, '<Model><ChangedDuringCacheLookup/></Model>')
        return actual_guard(reader, path, **kwargs)
    monkeypatch.setattr(RawResourceTools, "_guard", guard)
    monkeypatch.setattr(RawResourceTools, "call", lambda *_: pytest.fail("Changed cached version must fail before parsing"))
    with pytest.raises(PermissionError, match="cached case verification"):
        tools.call({"case_id": case["id"]})
    assert len(guards) == 2


def test_new_immutable_revision_parses_new_file_and_never_returns_old_xml(fixture):
    store, case, source, _ = fixture
    tools = CaseOriginalInspector(store)
    bounded(tools.call({"case_id": case["id"]}))
    xml = '<Model><Changed tag="new"/></Model>'
    write_mph(source, xml)
    new = publish_case(store, source)
    assert new["id"] != case["id"]
    result = bounded(tools.call({"case_id": new["id"], "view": "xml"}))
    assert result["text"] == xml and result["sha256"] == new["asset_hash"]
    assert not result["cache_reused"]


def test_rebind_uses_only_current_registered_roots(fixture, tmp_path):
    store, case, source, xml = fixture
    tools = CaseOriginalInspector(store)
    bounded(tools.call({"case_id": case["id"]}))
    moved = tmp_path / "moved"
    shutil.copytree(source.parent, moved)
    store.rebind("cases", "models", moved)
    write_mph(source, '<Model><OldPathShouldNotBeRead/></Model>')
    result = bounded(tools.call({"case_id": case["id"], "view": "xml"}))
    assert result["path"] == str(moved / source.name) and result["text"] == xml
    (moved / source.name).unlink()
    with pytest.raises(FileNotFoundError):
        tools.call({"case_id": case["id"]})


@pytest.mark.parametrize("mutation", ["closed", "deleted", "heldout", "roots", "file", "identity"])
def test_in_flight_changes_are_revalidated_before_return(fixture, tmp_path, monkeypatch, mutation):
    store, case, source, _ = fixture
    moved = tmp_path / "moved"
    if mutation == "roots":
        shutil.copytree(source.parent, moved)
    original_call = RawResourceTools.call
    def changed(reader, name, arguments):
        result = original_call(reader, name, arguments)
        if mutation == "closed":
            store.set_enabled("cases", False)
        elif mutation == "deleted":
            store.delete_library("cases")
        elif mutation == "heldout":
            monkeypatch.setattr(store, "evaluation_policy", lambda: HoldoutPolicy(source_hashes=frozenset({case["asset_hash"]})))
        elif mutation == "roots":
            store.rebind("cases", "models", moved)
        elif mutation == "file":
            write_mph(source, '<Model><ChangedDuringRead/></Model>')
        else:
            original_get = store.get
            monkeypatch.setattr(store, "get", lambda identity: dict(original_get(identity), asset_key="models:elsewhere.mph"))
        return result
    monkeypatch.setattr(RawResourceTools, "call", changed)
    with pytest.raises((PermissionError, FileNotFoundError)):
        CaseOriginalInspector(store).call({"case_id": case["id"], "view": "xml"})


def test_api_enhanced_overview_ignores_metadata_paths_and_scoring_material(fixture, tmp_path, monkeypatch):
    store, _, source, xml = fixture
    private = tmp_path / "scoring-answer.xml"
    private.write_text("must never be read", encoding="utf-8")
    data = {"compact_case": {"path": str(private)}, "compact_api_overlay": {"path": str(private)},
            "source_path": str(private), "base_case_id": "not-authorized", "method_cards": str(private)}
    case = publish_case(store, source, data=data, local_id="api-enhanced-case")
    original_open = Path.open
    opened = []
    def track(path, *args, **kwargs):
        opened.append(path)
        assert path != private, "Scoring and metadata paths are outside exact original permission"
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", track)
    result = bounded(CaseOriginalInspector(store).call({"case_id": case["id"], "view": "xml"}))
    assert result["text"] == xml and result["path"] == str(source)
    assert private not in opened


@pytest.mark.parametrize("asset", ["missing:original.mph", "models:../secret.mph", "models:C:\\private.mph",
                                   "models:/secret.mph", "models:original.mph:hidden", "models:original.xml"])
def test_unregistered_or_escaping_assets_fail_closed(fixture, monkeypatch, asset):
    store, case, _, _ = fixture
    original_get = store.get
    monkeypatch.setattr(store, "get", lambda identity: dict(original_get(identity), asset_key=asset))
    monkeypatch.setattr(RawResourceTools, "call", lambda *_: pytest.fail("An invalid asset must not reach native reader"))
    with pytest.raises((PermissionError, ValueError)):
        CaseOriginalInspector(store).call({"case_id": case["id"]})


def test_children_and_unicode_xml_pages_stay_within_wrapped_byte_budget(fixture):
    store, _, source, _ = fixture
    xml = '<Model>' + ''.join(f'<Native tag="n{i}" name="漢字😀{i}"/>' for i in range(65)) + '<Text>' + '漢字😀"\\\n' * 2000 + '</Text></Model>'
    write_mph(source, xml)
    case = publish_case(store, source)
    tools = CaseOriginalInspector(store)
    nodes, offset = [], 0
    while True:
        raw = bounded(tools.call({"case_id": case["id"], "limit": 20, "offset": offset}))
        nodes.extend(raw["items"])
        if raw["next_offset"] is None:
            break
        assert raw["next_offset"] > offset
        offset = raw["next_offset"]
    assert [node["node_path"] for node in nodes] == [f"/{i}" for i in range(66)]
    pieces, offset = [], 0
    while True:
        raw = bounded(tools.call({"case_id": case["id"], "view": "xml", "length": 3200, "offset": offset}))
        assert raw["range"]["start"] == offset and raw["range"]["end"] == raw["end_offset"]
        pieces.append(raw["text"])
        if raw["next_offset"] is None:
            break
        assert raw["next_offset"] > offset
        offset = raw["next_offset"]
    assert ''.join(pieces) == xml
    assert len(pieces) > 4
