import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from scripts.compare_offline_mph import (compare_case, compare_properties, compare_selection, compare_values, pair_nodes)


def xml(kind, tag, parent="", type_=None, fields=None):
    return {"path": parent + f'/{kind}[@tag="{tag}"]', "node_kind": kind, "tag": tag, "type": type_,
            "properties": [{"element": key, "text": value, "attributes": {}} for key, value in (fields or {}).items()],
            "normalized_properties": {"properties": []}}


def test_pairing_uses_ancestor_scope_not_tag_count_and_keeps_ambiguous_aliases():
    geom = xml("GeomSequence", "geom1", fields={"storedParent": "/modelNode/comp1"})
    heat = xml("Physics", "ht", type_="HeatTransfer", fields={"geom": "/geom/geom1"})
    solid = xml("Physics", "solid", type_="SolidMechanics", fields={"geom": "/geom/geom1"})
    a = xml("PhysicsFeature", "init1", heat["path"], "init")
    b = xml("PhysicsFeature", "init1", solid["path"], "init")
    material = xml("Material", "mat1", type_="Common")
    api = [
        {"path": "model/component(comp1)/physics(ht)/feature(init1)", "category": "feature", "tag": "init1", "type": "init"},
        {"path": "model/component(comp1)/physics(solid)/feature(init1)", "category": "feature", "tag": "init1", "type": "init"},
        {"path": "model/material(mat1)", "category": "material", "tag": "mat1", "type": "Common"},
        {"path": "model/component(comp1)/material(mat1)", "category": "material", "tag": "mat1", "type": "Common"},
    ]
    result = pair_nodes([geom, heat, solid, a, b, material], api)
    pairs = result["pairs"]
    assert len(pairs) == 2
    assert "physics(ht)" in pairs[0]["api"]["path"] and "Physics[@tag=\"ht\"]" in pairs[0]["offline"]["path"]
    assert len(result["ambiguous"]) == 1 and len(result["ambiguous"][0]["api_candidates"]) == 2


def test_type_conflicts_and_duplicate_offline_nodes_are_not_forced_matches():
    a = xml("StudyFeature", "stat", '/Study[@tag="std1"]', "Stationary")
    target = {"path": "model/study(std1)/feature(stat)", "category": "feature", "tag": "stat", "type": "Transient"}
    result = pair_nodes([a], [target])
    assert not result["pairs"] and len(result["type_conflicts"]) == 1
    target["type"] = "Stationary"
    result = pair_nodes([a, copy.deepcopy(a)], [target])
    assert not result["pairs"] and len(result["ambiguous"]) == 2


def test_typed_values_expressions_and_unverified_matrix_layout_are_distinct():
    assert compare_values("0", 0) == "typed_value_difference"
    assert compare_values(True, 1) == "typed_value_difference"
    assert compare_values("DV", "0[V]") == "expression_text_difference"
    assert compare_values([1.0], [1.0 + 1e-14]) == "numeric_within_tolerance"
    offline = {"normalized_properties": {"properties": [{"name": "matrix", "status": "decoded", "value_type": "matrix", "value": {"shape": [2, 1], "rows": [[1], [2]], "layout": "not_verified"}}]}}
    api = {"properties": [{"name": "matrix", "read_status": "observed", "value_type": "DoubleMatrix", "value": [[1], [2]]}]}
    assert compare_properties(offline, api)["items"][0]["status"] == "structured_matrix_layout_not_verified"


def test_properties_keep_duplicates_and_do_not_call_api_only_values_defaults():
    offline = {"properties": [{"element": "propertyValue", "attributes": {"name": "p:rawOnly"}}], "normalized_properties": {"properties": [
        {"name": "dup", "status": "decoded", "value": 1}, {"name": "dup", "status": "decoded", "value": 2},
        {"name": "undecoded", "status": "unresolved", "value": None}, {"name": "getter", "status": "decoded", "value": "x"}]}}
    api = {"properties": [{"name": name, "read_status": "observed", "value": 1} for name in ("dup", "undecoded", "rawOnly", "otherEncoding")] + [{"name": "getter", "read_status": "unresolved", "value": None}]}
    before = copy.deepcopy(offline)
    result = {item["name"]: item for item in compare_properties(offline, api)["items"]}
    assert result["dup"]["status"] == "ambiguous_property_name"
    assert result["undecoded"]["reasons"] == ["offline_undecoded"]
    assert result["getter"]["reasons"] == ["api_read_failed"]
    assert result["rawOnly"]["xml_observation"] == "present_in_raw_xml_not_structured"
    assert "not_found_in_current_structured_output" in result["otherEncoding"]["xml_observation"]
    assert offline == before


def test_selection_only_compares_same_dimension_decoded_sets_and_preserves_indirection():
    offline = {"normalized_selections": {"status": "observed_structure", "entries": [{"explicit": [{"status": "decoded_saved", "decoded_entities": [30], "entity_dimension": 2, "highest_dimension": 2, "dimension_relation": "same"}], "remaining": [{"values": [31]}], "named_references": ["sel1"]}]}}
    api = {"selection": {"dim": {"status": "observed", "value": 2}, "entities": {"status": "observed", "value": [30]}, "geometry_built_by_extractor": False}}
    before = copy.deepcopy(offline)
    result = compare_selection(offline, api)
    assert result["comparisons"][0]["status"] == "same_dimension_entity_sets_equal"
    assert result["saved_indirections"]["remaining"] == 1 and result["physical_membership"] == "not_validated"
    offline["normalized_selections"]["entries"][0]["explicit"][0]["dimension_relation"] = "different"
    assert compare_selection(offline, api)["comparisons"][0]["status"] == "dimension_relation_different_or_unknown_not_compared"
    assert before["normalized_selections"]["entries"][0]["remaining"][0]["values"] == [31]


def test_selection_distinguishes_saved_input_ids_from_current_getter_membership():
    offline = {"normalized_selections": {"status": "observed_structure", "entries": [{"explicit": [{"status": "decoded_saved", "decoded_entities": [1, 2, 3], "entity_dimension": 2, "highest_dimension": 2, "dimension_relation": "same"}]}]}}
    api = {"selection": {"dim": {"status": "observed", "value": 2}, "inputEntities": {"status": "observed", "value": [1, 2, 3]}, "entities": {"status": "observed", "value": [1, 2]}}}
    result = compare_selection(offline, api)
    assert result["summary"]["inputEntities:same_dimension_entity_sets_equal"] == 1
    assert result["summary"]["entities:same_dimension_entity_sets_different"] == 1
    assert result["physical_membership"] == "not_validated"


def test_api_is_hash_linked_and_never_input_to_offline_reader_or_process(tmp_path):
    model = tmp_path / "part.mph"
    with zipfile.ZipFile(model, "w") as archive: archive.writestr("dmodel.xml", "<model/>")
    fingerprint = hashlib.sha256(model.read_bytes()).hexdigest()
    state = tmp_path / "api-state.json"
    state.write_text(json.dumps({"nodes": [], "runtime_version": "6.3", "file_saved_version": "6.2"}), encoding="utf-8")
    calls = []

    def reader(*args):
        import subprocess
        calls.append(args)
        try: subprocess.Popen(["must-not-run"])
        except AssertionError: pass
        else: raise AssertionError("process guard missing")
        return [], {"status": "bounded_saved_state", "normalization": {}}, {}

    module = SimpleNamespace(_xml_state=reader, _read_api_state=lambda path: json.loads(path.read_text(encoding="utf-8")))
    catalog = {fingerprint: [{"state": state, "manifest": state, "state_sha256": hashlib.sha256(state.read_bytes()).hexdigest()}]}
    with patch.dict(sys.modules, {"advisor.library_model": module}):
        result = compare_case(model, tmp_path, tmp_path, catalog)
    assert calls == [(model,)]
    assert result["source_sha256"] == fingerprint and result["source_unchanged"]
    assert result["api_runtime_version"] == "6.3" and result["api_file_saved_version"] == "6.2"
    assert result["api_snapshot_unchanged"]
