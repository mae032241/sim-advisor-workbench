"""Compact useful simulation facts preserve saved decoding and source routes."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import pytest

from advisor import compact_model as compact
from advisor.mph_saved_values import normalize_properties


def mph(path, xml, *, title="Compact fixture"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("modelinfo.xml", f'<modelInfo comsolVersion="6.3" title="{title}" modelTitle="modeldescr_test/compact"/>')
        archive.writestr("dmodel.xml", xml)
    return path


def parse(path, tmp_path):
    # These existing entry points can start subprocesses or create expanded
    # snapshots. The compact parser must use the native bounded container only.
    with patch("advisor.library_model.extract_case", side_effect=AssertionError("No expanded extractor")), \
         patch("advisor.library_model._deep_state", side_effect=AssertionError("No COMSOL")), \
         patch("advisor.model_reader.inspect_model", side_effect=AssertionError("No external reader")):
        return compact.parse_compact_model(path, docs_root=tmp_path / "missing-docs")


def recover_nodes(result):
    root = ET.fromstring(result["raw_xml"])
    resolved = {}
    for node in result["nodes"]:
        element = resolved[node["parent_id"]] if node["parent_id"] is not None else root
        for index in node["xml_route"]:
            element = element[index]
        assert element.get("tag") == node["tag"]
        resolved[node["id"]] = element
    return root, resolved


def test_compact_container_limit_is_independent_and_binary_solution_is_never_opened(tmp_path, monkeypatch):
    from advisor import model_reader
    source = mph(tmp_path / "solved.mph", '<Model><Study tag="std1"/></Model>')
    with zipfile.ZipFile(source, 'a') as archive:
        archive.writestr('solution.bin', b'\x00' * 4096)
    monkeypatch.setattr(model_reader, 'MAX_FILE_BYTES', 1)
    monkeypatch.setattr(compact, 'MAX_CONTAINER_BYTES', source.stat().st_size)
    original = zipfile.ZipFile.open
    def open_member(archive, member, *args, **kwargs):
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        assert name != 'solution.bin'
        return original(archive, member, *args, **kwargs)
    with patch.object(zipfile.ZipFile, 'open', open_member):
        assert parse(source, tmp_path)['case']['source_size_bytes'] == source.stat().st_size
    monkeypatch.setattr(compact, 'MAX_CONTAINER_BYTES', source.stat().st_size - 1)
    with pytest.raises(model_reader.ModelInspectionError, match='inspection size limit'):
        parse(source, tmp_path)


def test_typed_values_once_and_exact_routes_through_skipped_wrappers(tmp_path):
    xml = '''<Model tag="m"><PhysicsList tag="physics"><Physics tag="ht" op="HeatTransfer" name="Heat" created="10" unit="K">
      <UnknownWrapper tag="wrapper"><wrapper><PhysicsFeature tag="hf1" op="HeatFluxBoundary" name="Convection">
      <active>true</active><param param="h" value="1|1,'10'"/>
      <param param="length" value="1|1,'2[mm]'"/><propertyValue name="p:hmax" value="0.007"/>
      <propertyValue name="p:enabledValue" value="false"/><expressions name="formula" expr="a*sin(t)"/>
      <propertyValue name="p:custom_new_model_setting" value="keep_me"/>
      <selection T="32" selType="GEOMDIM"><named>/selection/faces</named><explicit dim="2" hDim="3" geom="g1" entities="10,1,4,5"/></selection>
      </PhysicsFeature></wrapper></UnknownWrapper></Physics></PhysicsList></Model>'''
    path = mph(tmp_path / "source.mph", xml)
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = parse(path, tmp_path)
    _, resolved = recover_nodes(result)
    assert len(result["nodes"]) == 2
    heat, flux = result["nodes"]
    assert heat["parent_id"] is None and flux["parent_id"] == heat["id"]
    assert flux["xml_route"] == [0, 0, 0]
    assert heat["attributes"] == {"unit": "K"}
    values = {row["name"]: row for row in result["properties"]}
    assert values["h"]["value"] == "10" and values["length"]["value"] == "2[mm]"
    assert values["hmax"]["value"] == 0.007 and values["enabledValue"]["value"] is False
    assert values["formula"]["value"] == "a*sin(t)" and values["formula"]["value_type"] == "expression"
    assert values["custom_new_model_setting"]["value"] == "keep_me"
    for row in result["properties"]:
        source = resolved[row["node_id"]][row["child_index"]]
        original = source.get("param") or source.get("name", "").removeprefix("p:")
        assert original == row["name"]
        assert not {"raw_xml", "raw_value", "raw_fields", "attributes", "value_preview", "text", "locator"} & row.keys()
    selection = result["selections"][0]
    assert resolved[selection["node_id"]][selection["child_index"]].tag == "selection"
    explicit = selection["value"]["explicit"][0]
    assert explicit["decoded_entities"] == [1, 4, 5]
    assert explicit["entity_dimension"] == 2 and explicit["encoded_entity_dimension"] == 3
    assert selection["value"]["named_references"] == ["/selection/faces"]
    assert path.read_bytes() == before[0] and path.stat().st_mtime_ns == before[1]
    assert result["case"]["source_sha256"] == hashlib.sha256(before[0]).hexdigest()
    assert result["case"]["coverage"]["validation"]["physical_validity"] == "not_evaluated"
    assert not any(tmp_path.rglob("saved-state.json")) and not any(tmp_path.rglob("*.gz"))


def test_partial_values_and_unsupported_raw_trees_remain_distinct(tmp_path):
    xml = '''<Model tag="m"><MeshFeature tag="size" op="Size">
      <propertyValue name="p:future"><UnknownMatrix><row>1 2</row></UnknownMatrix></propertyValue>
      <propertyValue name="p:sourceface"><Selection selType="OBJECT"><object feature="b1"/></Selection></propertyValue>
      <propertyValue name="p:partial" valueMatrix="2|2,'a','b'"/>
      <propertyValue name="p:missing"/><param param="zero_marker" value="0"/>
      <propertyValue name="p:reference" Reference="/study/std1" ReferenceContainerList="/study"/>
      </MeshFeature></Model>'''
    result = parse(mph(tmp_path / "partial.mph", xml), tmp_path)
    _, resolved = recover_nodes(result)
    values = {row["name"]: row for row in result["properties"]}
    for name, status in (("future", "unresolved"), ("sourceface", "partial")):
        row = values[name]
        assert row["value"] is None and row["value_omitted"] and row["status"] == status
        assert row["issues"] and len(resolved[row["node_id"]][row["child_index"]])
    partial = values["partial"]
    assert partial["status"] == "partial" and not partial.get("value_omitted")
    assert partial["value"]["rows"] == [["a", "b"]]
    assert "declared_row_count_mismatch" in partial["issues"] and "entries" not in partial["value"]
    assert values["missing"]["status"] == "unresolved"
    assert values["zero_marker"]["value"] == "0" and values["zero_marker"]["status"] == "unresolved"
    assert values["reference"]["value"] == {"reference": "/study/std1", "container": "/study"}
    assert values["reference"]["status"] == "partial"


def test_large_tables_use_descriptors_but_small_anisotropic_matrices_keep_values(tmp_path):
    small = "6|" + "|".join("6," + ",".join("'" + str(row * 6 + col) + "'" for col in range(6)) for row in range(6))
    large = "100|" + "|".join("1,'" + str(index) + "[Pa]'" for index in range(100))
    xml = f'''<Model tag="m"><MaterialModel tag="def" op="LinearElasticMaterial">
       <propertyValue name="p:elasticity" valueMatrix="{small}"/>
       <propertyValue name="p:table" valueMatrix="{large}"/></MaterialModel></Model>'''
    result = parse(mph(tmp_path / "arrays.mph", xml), tmp_path)
    _, resolved = recover_nodes(result)
    small_row, large_row = result["properties"]
    assert small_row["value"]["shape"] == [6, 6] and len(small_row["value"]["rows"]) == 6
    assert not small_row.get("value_omitted") and "entries" not in small_row["value"]
    assert large_row["value_omitted"] and large_row["status"] == "decoded"
    assert large_row["value"]["shape"] == [100, 1]
    assert large_row["value"]["entries_observed"] == 100
    assert large_row["value"]["sample"] == ["0[Pa]", "1[Pa]", "98[Pa]", "99[Pa]"]
    assert "rows" not in large_row["value"]
    raw = resolved[large_row["node_id"]][large_row["child_index"]]
    assert raw.get("valueMatrix") == large


def test_functional_selection_excludes_junk_but_keeps_numerical_descendants(tmp_path):
    xml = '''<Model tag="m"><ViewList tag="views"><View tag="view"><propertyValue name="p:camera" value="junk"/></View></ViewList>
      <FutureWidget tag="widget"><propertyValue name="p:garbage" value="junk"/></FutureWidget>
      <ResultFeatureList tag="results"><ResultFeature tag="plot" op="Surface"><propertyValue name="p:colortable" value="junk"/></ResultFeature>
      <DatasetFeature tag="dset" op="Solution"><propertyValue name="p:solution" value="sol1"/><propertyValue name="p:defaultPlotIDs" value="junk"/></DatasetFeature>
      <NumericalFeature tag="eval" op="EvalGlobal"><propertyValue name="p:expr" value="intop1(p)"/></NumericalFeature>
      <ProbeFeature tag="probe" op="Global"><propertyValue name="p:expr" value="maxop1(T)"/></ProbeFeature></ResultFeatureList>
      <PhysicsFeature tag="disabled" active="false"><propertyValue name="p:q" value="9"/></PhysicsFeature>
      <PhysicsFeature tag="ambiguous" active="0"><active>0</active><propertyValue name="p:q" value="3"/>
      <propertyValue name="p:showPhysicsSymbols" value="1"/><propertyValue name="p:novel_physics" value="keep"/>
      <selection selType="NONE"/></PhysicsFeature></Model>'''
    result = parse(mph(tmp_path / "filtered.mph", xml), tmp_path)
    _, resolved = recover_nodes(result)
    assert {node["tag"] for node in result["nodes"]} == {"dset", "eval", "probe", "ambiguous"}
    assert {row["value"] for row in result["properties"]} == {"sol1", "intop1(p)", "maxop1(T)", 3, "keep"}
    assert not result["selections"]
    coverage = result["case"]["coverage"]["raw_xml"]
    reasons = {entry["reason"] for entry in coverage["exclusions"]}
    assert {"display_or_plot_presentation", "outside_functional_allowlist", "explicit_saved_false_node_or_ancestor"} <= reasons
    assert coverage["properties_source_only"] == 2 and coverage["empty_selections_source_only"] == 1
    root = ET.fromstring(result["raw_xml"])
    for entry in coverage["exclusions"]:
        for example in entry["examples"]:
            element = resolved[example["parent_id"]] if example["parent_id"] is not None else root
            for index in example["xml_route"]:
                element = element[index]
            assert element.tag == entry["kind"]


def test_bounds_report_unread_data_without_erasing_raw_source(tmp_path, monkeypatch):
    properties = ''.join(f'<propertyValue name="p:v{i}" value="' + 'x' * 100 + '"/>' for i in range(3))
    xml = '<Model tag="m">' + ''.join(f'<MeshFeature tag="size{i}">{properties}</MeshFeature>' for i in range(4)) + '</Model>'
    monkeypatch.setattr(compact, "MAX_NODES", 2)
    monkeypatch.setattr(compact, "MAX_PROPERTIES", 2)
    monkeypatch.setattr(compact, "MAX_TEXT", 50)
    result = parse(mph(tmp_path / "bounded.mph", xml), tmp_path)
    coverage = result["case"]["coverage"]["raw_xml"]
    assert len(result["nodes"]) == 2 and len(result["properties"]) == 4
    assert coverage["nodes_omitted_by_limit"] == 2 and coverage["properties_unread"] == 2
    assert coverage["truncated"] and all(row["truncated"] for row in result["properties"])
    assert all(row["status"] == "partial" and "value_text_limit" in row["issues"] for row in result["properties"])
    assert result["raw_xml"].decode() == xml


def test_external_tutorial_keeps_only_reference_metadata_and_no_passages(tmp_path):
    path = mph(tmp_path / "compact.mph", '<Model tag="m"><MeshFeature tag="size"/></Model>')
    html = tmp_path / "tutorial.html"
    html.write_text('''<html><title>Document title</title><h2 id="intro">Introduction</h2><p>Physical context.</p>
      <h2>Modeling Instructions</h2><h3 id="mesh">Mesh</h3><p>Set the maximum element size.</p></html>''', encoding="utf-8")
    with patch("advisor.library_model._tutorial_objects", side_effect=AssertionError("No tutorial body copies")), \
         patch("advisor.case_metadata.tutorial_context_objects", side_effect=AssertionError("No tutorial body copies")):
        result = compact.parse_compact_model(path, docs_root=html)
    assert result["documents"] == [] and result["passages"] == []
    reference = result["case"]["tutorial_refs"][0]
    assert reference["source_uri"] == html.as_uri()
    assert reference["source_sha256"] == hashlib.sha256(html.read_bytes()).hexdigest()
    assert reference["title"] == "Document title"
    assert reference["relation_status"] == "reference_not_verified_model_agreement"
    assert any(item["locator"] == "html:#mesh" and item["category"] == "mesh" for item in reference["chapters"])
    persisted = json.dumps({key: value for key, value in result.items() if key != "raw_xml"})
    assert "Physical context." not in persisted and "Set the maximum element size." not in persisted
    assert result["case"]["coverage"]["tutorial"]["body_storage"] == "external_document_owner"


def test_no_duplicate_normalization_boilerplate_or_full_paths_in_rows(tmp_path):
    result = parse(mph(tmp_path / "compact.mph", '''<Model tag="m"><MeshFeature tag="size">
      <propertyValue name="p:hmax" value="0.1"/><selection T="32" selType="GEOMDIM">
      <explicit dim="2" hDim="2" geom="g1" entities="9,2,4"/></selection></MeshFeature></Model>'''), tmp_path)
    forbidden = {"raw_xml", "raw_fields", "raw_value", "raw_encoded", "format_evidence", "limits", "coverage", "parser_contract", "path", "node_path", "value_preview", "type_basis", "value_origin", "effective_activation", "membership_status"}

    def check(value):
        if isinstance(value, dict):
            assert not forbidden & value.keys()
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    for key in ("nodes", "properties", "selections", "documents", "passages"):
        check(result[key])
    assert len(result["case"]["parser_signature"]) == 64
    assert result["case"]["source_member"] == "dmodel.xml"


def test_unsupported_nonempty_selection_keeps_actual_gap_and_source_pointer(tmp_path):
    xml = '''<Model tag="m"><PhysicsFeature tag="inlet"><selection T="32" selType="FUTURE">
      <futureTopology association="keep-in-source"/></selection><selection selType="NONE"/>
      </PhysicsFeature></Model>'''
    result = parse(mph(tmp_path / "unknown-selection.mph", xml), tmp_path)
    _, resolved = recover_nodes(result)
    assert len(result["selections"]) == 1
    row = result["selections"][0]
    assert row["status"] == "unresolved" and row["value"]["selection_type"] == "FUTURE"
    assert any("Unrecognized direct selection child: futureTopology" in issue for issue in row["value"]["issues"])
    raw = resolved[row["node_id"]][row["child_index"]]
    assert raw[0].get("association") == "keep-in-source"
    assert result["case"]["coverage"]["raw_xml"]["empty_selections_source_only"] == 1


def test_material_and_inactive_selectors_exclude_only_their_exact_unused_base(tmp_path):
    xml = '''<Model tag="m"><PhysicsFeature tag="fluid"><param param="rho_mat" value="1|1,'from_mat'"/>
      <param param="rho" value="1|1,'0'"/><param param="c_mat" value="1|1,'userdef'"/><param param="c" value="1|1,'0'"/>
      <propertyValue name="p:physical_zero" value="0"/></PhysicsFeature>
      <MeshFeature tag="size"><propertyValue name="p:hminactive" value="off"/><propertyValue name="p:hmin" value="0.0098"/>
      <propertyValue name="p:hmaxactive" value="0"/><propertyValue name="p:hmax" value="0.1"/>
      <propertyValue name="p:hcurveactive" value="false"/><propertyValue name="p:hcurve" value="0.2"/></MeshFeature></Model>'''
    result = parse(mph(tmp_path / "selectors.mph", xml), tmp_path)
    values = {row["name"]: row["value"] for row in result["properties"]}
    assert not {"rho", "hmin", "hcurve"} & values.keys()
    assert values["rho_mat"] == "from_mat" and values["c"] == "0" and values["physical_zero"] == 0
    assert values["hminactive"] == "off" and values["hmaxactive"] == 0 and values["hmax"] == 0.1
    reasons = {row["reason"] for row in result["case"]["coverage"]["raw_xml"]["property_exclusions"]}
    assert reasons == {"material_selected_base_value", "explicit_inactive_base_value"}


def test_selector_policy_is_physical_scope_and_exact_known_mesh_size_type(tmp_path):
    xml = '''<Model tag="m"><ModelParam tag="parameters"><propertyValue name="p:rho_mat" value="from_mat"/>
      <propertyValue name="p:rho" value="0"/><propertyValue name="p:fooactive" value="off"/><propertyValue name="p:foo" value="0"/></ModelParam>
      <MeshFeature tag="manual" op="Size"><propertyValue name="p:custom" value="on"/>
      <propertyValue name="p:hauto" value="4"/><propertyValue name="p:hmax" value="0.1"/></MeshFeature>
      <MeshFeature tag="auto" op="MeshSizeDefault"><propertyValue name="p:custom" value="off"/>
      <propertyValue name="p:hauto" value="4"/><propertyValue name="p:hmax" value="0.2"/></MeshFeature>
      <MeshFeature tag="unknown" op="FutureSize"><propertyValue name="p:custom" value="on"/>
      <propertyValue name="p:hauto" value="3"/></MeshFeature></Model>'''
    result = parse(mph(tmp_path / "scoped-selectors.mph", xml), tmp_path)
    rows = {node["tag"]: {row["name"]: row["value"] for row in result["properties"] if row["node_id"] == node["id"]} for node in result["nodes"]}
    assert rows["parameters"]["rho"] == 0 and rows["parameters"]["foo"] == 0
    assert rows["manual"] == {"custom": "on", "hmax": .1}
    assert rows["auto"] == {"custom": "off", "hauto": 4}
    assert rows["unknown"] == {"custom": "on", "hauto": 3}


def test_actual_head_torso_retained_values_match_original_normalization(tmp_path):
    path = Path("D:/comsol/COMSOL63/Multiphysics/applications/Acoustics_Module/Tutorials,_Pressure_Acoustics/head_torso_hrtf.mph")
    if not path.is_file():
        pytest.skip("Optional official head_torso_hrtf sample unavailable")
    result = parse(path, tmp_path)
    _, resolved = recover_nodes(result)
    normalized = {node_id: normalize_properties(element)["properties"] for node_id, element in resolved.items()}
    for row in result["properties"]:
        source = normalized[row["node_id"]][row["ordinal"] - 1]
        assert row["name"] == source["name"] and row["status"] == source["status"]
        assert row["issues"] == source["issues"] and row["truncated"] == source["truncated"]
        if not row.get("value_omitted"):
            expected = source["value"]
            if source["value_type"] == "matrix":
                expected = {key: expected[key] for key in ("shape", "rows", "row_lengths") if key in expected}
            assert row["value"] == expected
    assert any(row["name"] == "hmax" and row["value"] == "min(20[mm],lam0/4)" for row in result["properties"])
    assert all(node["kind"] not in {"View", "ViewFeature", "ResultFeature"} for node in result["nodes"])
    assert len(result["nodes"]) < 200 and len(result["properties"]) < 700
    size = len(json.dumps({key: value for key, value in result.items() if key != "raw_xml"}, ensure_ascii=False).encode())
    assert size < 300000
