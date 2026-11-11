"""Selective API projection uses scoped identity and keeps temporary reads temporary."""
import copy
from pathlib import Path

import pytest

from advisor import compact_api as api
from advisor.compact_model import parse_compact_model
from advisor.build_control import BuildCancelled
from test_compact_model import mph


XML = '''<Model tag="m"><ModelNode tag="comp1"/><GeomSequence tag="geom1"><storedParent>/modelNode/comp1</storedParent></GeomSequence>
<MeshSequence tag="mesh1"><geom>/geom/geom1</geom><MeshFeature tag="size" op="Size">
<propertyValue name="p:hmax" value="0.1"/><propertyValue name="p:hminactive" value="off"/>
<propertyValue name="p:hmin" value="0.02"/><propertyValue name="p:customInput"><Selection><future/></Selection></propertyValue>
<MeshFeature tag="size2" op="Size"><propertyValue name="p:hmax" value="0.2"/></MeshFeature>
</MeshFeature></MeshSequence></Model>'''
PATH = "model/component(comp1)/mesh(mesh1)/feature(size)"


def fixture(tmp_path, xml=XML):
    path = mph(tmp_path / "fixture.mph", xml)
    saved = parse_compact_model(path, docs_root=tmp_path / "missing-docs")
    return path, saved


def snapshot(nodes):
    return {"schema": "mph-api-state/v1", "read_contract": api.API_READ_CONTRACT, "nodes": nodes,
            "geometry_run": False, "mesh_run": False, "solver_run": False, "defaults_generated": False,
            "runtime_version": "6.3-test", "file_saved_version": "6.3", "read_errors": 0, "read_coverage": {}}


def prop(name, value, kind="Double", status="observed"):
    return {"name": name, "value": value, "value_type": kind, "read_status": status, "getter": "get" + kind}


def test_exact_scope_matches_owner_and_feature_chain_without_tag_only_merge(tmp_path):
    _, saved = fixture(tmp_path)
    identities, _ = api.compact_node_identities(saved)
    node = next(node for node in saved["nodes"] if node["tag"] == "size2")
    assert identities[node["id"]] == PATH + "/feature(size2)"
    data = snapshot([
        {"path": PATH, "type": "Size", "properties": [prop("hmax", .1)]},
        {"path": "model/component(comp2)/mesh(mesh1)/feature(size)", "type": "Size", "properties": [prop("hmax", .9)]},
        {"path": "model/component(comp1)/mesh(other)/feature(size)", "type": "Size", "properties": [prop("hmax", .8)]},
        {"path": "model/component(comp1)/mesh(mesh1)/feature(size2)", "type": "Size", "properties": [prop("hmax", .7)]},
    ])
    result = api.select_compact_api(saved, data)
    assert len(result["nodes"]) == 1 and result["nodes"][0]["api_path"] == PATH
    assert result["run"]["counts"]["unmatched_api_nodes"] == 3
    row = result["observations"][0]
    assert row["relation"] == "corroborates_saved_xml" and "value" not in row


def test_ambiguous_geometry_component_and_duplicate_api_paths_are_not_forced(tmp_path):
    _, saved = fixture(tmp_path, XML.replace('<ModelNode tag="comp1"/>', '<ModelNode tag="comp1"/><ModelNode tag="comp2"/>')
                       .replace('<MeshSequence tag="mesh1">', '<GeomSequence tag="geom1"><storedParent>/modelNode/comp2</storedParent></GeomSequence><MeshSequence tag="mesh1">'))
    result = api.select_compact_api(saved, snapshot([{"path": PATH, "properties": [prop("hmax", .1)]}]))
    assert not result["observations"]
    _, saved = fixture(tmp_path)
    node = {"path": PATH, "type": "Size", "properties": [prop("hmax", .1)]}
    result = api.select_compact_api(saved, snapshot([node, node]))
    assert not result["observations"] and result["run"]["counts"]["ambiguous_paths"] == 2


def test_selects_repairs_and_whitelist_but_never_readds_inactive_or_gui_defaults(tmp_path):
    _, saved = fixture(tmp_path)
    data = snapshot([{"path": PATH, "type": "Size", "local_active": True, "properties": [
        prop("hmax", .2), prop("hauto", 4, "Int"), prop("hmin", .0098), prop("cameraColor", "red", "String"),
        prop("customInput", {"read_complete": True, "scope": "boilerplate", "entities": {"status": "observed", "value": [3, 8]},
                             "dim": {"status": "observed", "value": 2}}, "Selection")]}])
    original = copy.deepcopy(saved)
    result = api.select_compact_api(saved, data)
    assert saved == original
    rows = {row["name"]: row for row in result["observations"]}
    assert not {"hmin", "cameraColor"} & rows.keys()
    assert rows["hmax"]["relation"] == "differs_from_saved_xml" and rows["hmax"]["value"] == .2
    assert rows["hauto"]["relation"] == "missing_saved_property" and rows["hauto"]["value"] == 4
    assert rows["customInput"]["relation"] == "fills_saved_gap"
    assert rows["customInput"]["value"] == {"dim": 2, "entities": [3, 8]}
    assert rows["@local_active"]["relation"] == "local_flag_not_effective_activation"
    assert result["run"]["counts"]["selector_controlled_values_excluded"] == 1


def test_exact_typed_equality_does_not_coerce_boolean_number_or_expression(tmp_path):
    _, saved = fixture(tmp_path)
    data = snapshot([{"path": PATH, "type": "Size", "properties": [prop("hmax", "0.1", "String")]}])
    row = api.select_compact_api(saved, data)["observations"][0]
    assert row["relation"] == "differs_from_saved_xml" and row["value"] == "0.1"
    assert not api._same_saved_value({"status": "decoded", "value": 0}, False)
    assert not api._same_saved_value({"status": "decoded", "value": "2[mm]"}, .002)


def test_api_whitelist_does_not_reintroduce_unused_automatic_mesh_preset(tmp_path):
    xml = XML.replace('<propertyValue name="p:hmax" value="0.1"/>', '<propertyValue name="p:hmax" value="0.1"/><propertyValue name="p:custom" value="on"/>')
    _, saved = fixture(tmp_path, xml)
    data = snapshot([{"path": PATH, "type": "Size", "properties": [prop("hauto", 4, "Int"), prop("hmax", .1)]}])
    result = api.select_compact_api(saved, data)
    assert {row["name"] for row in result["observations"]} == {"hmax"}
    assert result["run"]["counts"]["selector_controlled_values_excluded"] == 1


def test_selected_getter_errors_are_compact_and_large_arrays_are_descriptors(tmp_path):
    _, saved = fixture(tmp_path)
    broken = {"name": "customInput", "value_type": "Selection", "read_status": "unresolved", "getter": "selection(String)",
              "read_error": {"reason": "api_getter_failed", "message": "large diagnostic" * 1000}}
    data = snapshot([{"path": PATH, "type": "Size", "properties": [broken, prop("hauto", list(range(100)), "IntArray")]}])
    data.update(read_errors=1, read_coverage={"failure_reasons": {"api_getter_failed": 1}})
    result = api.select_compact_api(saved, data)
    rows = {row["name"]: row for row in result["observations"]}
    assert rows["customInput"]["status"] == "unresolved" and "value" not in rows["customInput"]
    assert rows["customInput"]["issues"] == ["api_getter_failed"]
    assert rows["hauto"]["value_omitted"] and rows["hauto"]["value"]["entries_observed"] == 100
    assert rows["hauto"]["value"]["sample"] == [0, 1, 98, 99]
    assert result["run"]["errors"]["selected_failure_reasons"] == {"api_getter_failed": 1}
    assert "large diagnostic" not in str(result)


def test_bounded_observations_prioritize_saved_gap_repair(tmp_path, monkeypatch):
    _, saved = fixture(tmp_path)
    monkeypatch.setattr(api, "MAX_OBSERVATIONS_PER_NODE", 1)
    data = snapshot([{"path": PATH, "type": "Size", "local_active": True, "properties": [
        prop("hmax", .1), prop("hauto", 4, "Int"),
        prop("customInput", {"read_complete": True, "entities": {"status": "observed", "value": [7]},
                             "dim": {"status": "observed", "value": 2}}, "Selection")]}])
    result = api.select_compact_api(saved, data)
    assert len(result["observations"]) == 1
    assert result["observations"][0]["name"] == "customInput"
    assert result["run"]["counts"]["observations_omitted_by_limit"] == 3


def test_prohibited_execution_flags_are_rejected_before_projection(tmp_path):
    _, saved = fixture(tmp_path)
    data = snapshot([])
    for flag in ("geometry_run", "mesh_run", "solver_run", "defaults_generated"):
        with pytest.raises(api.CompactAPIReadError, match="no-execution"):
            api.select_compact_api(saved, dict(data, **{flag: True}))


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
def test_runner_cleans_all_snapshot_java_and_model_files_on_every_outcome(tmp_path, outcome):
    source, saved = fixture(tmp_path)
    before = source.read_bytes(), source.stat().st_mtime_ns
    temporary_paths = []

    def runner(path, output, fingerprint):
        assert path == source and fingerprint == saved["case"]["source_sha256"]
        temporary_paths.append(output)
        for name in ("input.mph", "api-state.json", "model_export.java", "api-manifest.json"):
            (output / name).write_text("temporary only", encoding="utf-8")
        if outcome == "cancelled":
            raise BuildCancelled()
        if outcome == "failure":
            return None, [], {"status": "failed", "reason": "fixture getter failure"}
        return snapshot([{"path": PATH, "type": "Size", "properties": [prop("hmax", .1)]}]), [], {"status": "observed"}

    if outcome == "success":
        result = api.extract_compact_api(saved, temp_root=tmp_path, runner=runner)
        assert result["source_sha256"] == saved["case"]["source_sha256"]
    else:
        error = BuildCancelled if outcome == "cancelled" else api.CompactAPIReadError
        with pytest.raises(error):
            api.extract_compact_api(saved, temp_root=tmp_path, runner=runner)
    assert temporary_paths and all(not path.exists() for path in temporary_paths)
    assert source.read_bytes() == before[0] and source.stat().st_mtime_ns == before[1]


def test_reader_interface_uses_original_xml_and_rejects_changed_source(tmp_path):
    from advisor.compact_case import CompactCase, build_compact_case
    source, _ = fixture(tmp_path)
    output = tmp_path / "case"
    build_compact_case(source, output, docs_root=tmp_path / "no-docs")
    reader = CompactCase(output)
    result = api.extract_compact_api(reader, temp_root=tmp_path,
                                     runner=lambda *_: (snapshot([{ "path": PATH, "type": "Size", "properties": [prop("hmax", .1)]}]), [], {}))
    assert result["observations"][0]["relation"] == "corroborates_saved_xml"
    source.write_bytes(b"changed original")
    with pytest.raises(ValueError):
        api.extract_compact_api(reader, temp_root=tmp_path, runner=lambda *_: pytest.fail("Changed source must not run COMSOL"))


def test_preview_api_fails_before_source_read_temp_directory_or_java(tmp_path):
    case = {"source_path": str(tmp_path / "absent-original.mph"), "source_sha256": "f" * 64,
            "coverage": {"source_container": {"status": "preview_only", "node_type": "preview"},
                         "raw_xml": {"status": "missing"}}}

    class PreviewReader:
        def read_case(self):
            return case

        def iter_nodes(self):
            pytest.fail("Preview must fail before trying feature rows")

        def read_raw_xml(self):
            pytest.fail("Preview must fail before trying absent saved XML")

    workspace = tmp_path / "must-not-create"
    for reader in (PreviewReader(), {"case": case, "nodes": [], "properties": [], "selections": [], "raw_xml": b""}):
        with pytest.raises(api.CompactAPIReadError, match="Preview-only"):
            api.extract_compact_api(reader, temp_root=workspace, runner=lambda *_: pytest.fail("Preview must never launch Java"))
    assert not workspace.exists()


def test_actual_waveguide_preview_has_no_api_attempt(tmp_path):
    path = Path("D:/comsol/COMSOL63/Multiphysics/applications/RF_Module/Filters/waveguide_diplexer.mph")
    if not path.is_file():
        pytest.skip("Optional official preview unavailable")
    parsed = parse_compact_model(path)
    assert parsed["raw_xml"] == b"" and not parsed["nodes"] and not parsed["properties"]
    assert parsed["case"]["coverage"]["source_container"]["status"] == "preview_only"
    assert parsed["documents"] == [] and parsed["passages"] == []
    assert parsed["case"]["tutorial_refs"][0]["relation_status"] == "reference_not_verified_model_agreement"
    with pytest.raises(api.CompactAPIReadError, match="Preview-only"):
        api.extract_compact_api(parsed, temp_root=tmp_path, runner=lambda *_: pytest.fail("Preview must never launch Java"))


def test_runtime_policy_uses_installed_metadata_and_changes_when_plugin_changes(tmp_path, monkeypatch):
    root = tmp_path / "comsol"
    binaries = root / "java/win64/jre/bin"
    binaries.mkdir(parents=True)
    for name in ("java.exe", "javac.exe"):
        (binaries / name).write_bytes(b"fixture binary")
    (root / "plugins").mkdir()
    (root / "plugins/com.comsol.model.jar").write_bytes(b"fixture getter library")
    (root / "bin/win64").mkdir(parents=True)
    (root / "bin/win64/model.dll").write_bytes(b"fixture native library")
    monkeypatch.setattr(api, "_comsol_root", lambda: root)
    first = api.current_runtime_policy()
    assert first == api.current_runtime_policy()
    assert first["plugins"]["file_count"] == 1 and first["java"]["size"] == 14
    (root / "plugins/com.comsol.model.jar").write_bytes(b"changed getter library file")
    changed = api.current_runtime_policy()
    assert first["plugins"]["stat_sha256"] != changed["plugins"]["stat_sha256"]
    assert first["selector_signature"] == changed["selector_signature"]


@pytest.mark.parametrize("relative", [
    "Structural_Mechanics_Module/Tutorials/bracket_thermal.mph",
    "COMSOL_Multiphysics/Multiphysics/busbar.mph",
    "COMSOL_Multiphysics/Fluid_Dynamics/cylinder_flow.mph",
])
def test_actual_crossphysics_functional_identities_have_proven_scopes(tmp_path, relative):
    path = Path("D:/comsol/COMSOL63/Multiphysics/applications") / relative
    if not path.is_file():
        pytest.skip("Optional official cross-physics model unavailable")
    before = path.stat().st_size, path.stat().st_mtime_ns
    saved = parse_compact_model(path, docs_root=tmp_path / "no-docs")
    identities, reasons = api.compact_node_identities(saved)
    supported = set(api._ROOT_ACCESSORS) | set(api._CHILD_ACCESSORS) | set(api._RESULT_ACCESSORS)
    assert not reasons.get("component_not_proven"), reasons
    for node in saved["nodes"]:
        if node["kind"] in supported:
            assert identities[node["id"]], node
        if node["kind"] in api._COMPONENT_REQUIRED:
            assert "/component(" in identities[node["id"]], node
    assert before == (path.stat().st_size, path.stat().st_mtime_ns)
    assert saved["case"]["coverage"]["validation"]["source_unchanged"]
