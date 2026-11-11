"""Saved syntax decoding is typed, bounded, and never a solver/API evaluation."""
import json
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

import pytest

from advisor.mph_saved_values import normalize_properties


def normalized(xml, **kwargs):
    return normalize_properties(ET.fromstring("<Node>" + xml + "</Node>"), **kwargs)


def test_param_string_expression_unit_and_empty_cell_remain_strings():
    result = normalized('''<param T="33" param="h" value="1|1,'10'"/>
<param param="displacement" value="1|1,'DV'"/>
<param param="load" value="1|1,'2[MPa]'"/>
<param param="empty" value="1|1,''"/>
<expressions name="length" expr="20[mm]"/>''')
    assert [item["value"] for item in result["properties"]] == ["10", "DV", "2[MPa]", "", "20[mm]"]
    assert all(item["status"] == "decoded" for item in result["properties"])
    assert result["properties"][0]["raw_value"] == "1|1,'10'"
    assert result["properties"][0]["attributes"]["T"] == "33"
    assert all(item["effective_activation"] == "not_evaluated" for item in result["properties"])
    assert result["expressions_evaluated"] is False and result["API_types_validated"] is False


@pytest.mark.parametrize("literal,value,kind", [("1.92E-5", 1.92e-5, "number"), ("0.001", .001, "number"), ("6", 6, "integer"),
                                                ("-2", -2, "integer"), ("+.5", .5, "number"), ("true", True, "boolean"),
                                                ("off", "off", "string"), ("on", "on", "string"), ("2[mm]", "2[mm]", "string"),
                                                ("DV", "DV", "string"), ("1/rho(T)", "1/rho(T)", "string")])
def test_mesh_propertyvalue_literal_types_and_original_name(literal, value, kind):
    element = ET.Element("MeshFeature", {"tag": "size1"})
    ET.SubElement(element, "propertyValue", {"T": "30", "name": "p:hmax", "value": literal})
    result = normalize_properties(element)
    item = result["properties"][0]
    assert item["name"] == "hmax" and item["original_name"] == "p:hmax"
    assert item["value"] == value and item["value_type"] == kind
    assert item["status"] == "decoded" and item["name_prefix_removed"] is True
    assert item["locator"] == 'child[1]/propertyValue[@name="p:hmax"]'
    assert item["raw_fields"]["value"] == literal
    assert all(key in item for key in ("element", "attributes", "text", "raw_xml", "truncated"))


def test_only_leading_property_namespace_is_removed():
    result = normalized('''<propertyValue name="p:p:hmax" value="1"/>
<propertyValue name="other:hmax" value="2"/><param param="p:hmax" value="1|1,'3'"/>''')
    assert [p["name"] for p in result["properties"]] == ["p:hmax", "other:hmax", "p:hmax"]


def test_serialized_matrix_shape_cells_order_and_reserved_escapes():
    result = normalized(r'''<propertyValue name="p:table" valueMatrix="2|2,'a\,b','c\|d'|2,'\tau','expr(x)'"/>
<param param="vector" value="3|1,'1'|1,'0'|1,'DV'"/>''')
    first, second = result["properties"]
    assert first["status"] == "decoded"
    assert first["value"] == {"shape": [2, 2], "rows": [["a,b", "c|d"], [r"\tau", "expr(x)"]], "row_lengths": [2, 2],
                              "entries": ["a,b", "c|d", r"\tau", "expr(x)"], "entry_order": "serialized", "layout": "not_verified"}
    assert first["value_type"] == "matrix"
    assert second["value"]["entries"] == ["1", "0", "DV"]
    assert second["value"]["shape"] == [3, 1]
    assert first["entries_complete"] is True


def test_multiple_representations_are_preserved_without_asserting_equivalence():
    result = normalized('''<propertyValue name="p:pos" value="0 0 0" valueMatrix="1|3,'0','0','0'"/>
<propertyValue name="p:solution" value="none" Reference="/sol/sol1" CompositeIndex="1"/>''')
    matrix, scalar = result["properties"]
    assert matrix["selected_representation"] == "valueMatrix"
    assert matrix["raw_fields"]["value"] == "0 0 0"
    assert matrix["representation_consistency"] == "not_validated"
    assert scalar["value"] == "none" and scalar["raw_fields"]["Reference"] == "/sol/sol1"


def test_null_missing_and_param_zero_are_not_conflated():
    result = normalized('''<propertyValue name="p:unset">null</propertyValue>
<propertyValue name="p:nodeNull"><null/></propertyValue>
<propertyValue name="p:missing"/><propertyValue name="p:zero" value="0"/>
<param param="pairs" value="0"/>''')
    text_null, child_null, missing, zero, ambiguous = result["properties"]
    assert text_null["value"] is None and text_null["value_type"] == "null" and text_null["status"] == "decoded"
    assert child_null["value"] is None and child_null["status"] == "decoded"
    assert missing["status"] == "unresolved"
    assert zero["value"] == 0 and zero["status"] == "decoded"
    assert ambiguous["value"] == "0" and ambiguous["status"] == "unresolved"
    assert ambiguous["value_type"] == "ambiguous_zero_marker"


def test_references_rows_selection_and_unknown_subtree_stay_scoped():
    result = normalized(r'''<propertyValue name="p:study" Reference="/study/std1" ReferenceContainerList="/study"/>
<propertyValue name="p:probes" ReferenceArray="0"/>
<propertyValue name="p:xdim" ReferenceArray="1,'/modelNode/xdim1'"/>
<propertyValue name="p:clist" ValueRows="2,'range(0\, 1\, 3)[s]','0.1[s]'"/>
<propertyValue name="p:input"><Selection selType="OBJECT"><base geom="/geom/g1"/><object feature="/geom/g1/feature/b1"/></Selection></propertyValue>
<propertyValue name="p:future"><UnknownMatrix rows="2"><row>1 2</row></UnknownMatrix></propertyValue>''')
    reference, empty, array, rows, selection, unknown = result["properties"]
    assert reference["value"]["reference"] == "/study/std1" and reference["status"] == "partial"
    assert empty["value"] == [] and empty["value_type"] == "reference_array"
    assert array["value"] == ["/modelNode/xdim1"]
    assert rows["value"] == ["range(0, 1, 3)[s]", "0.1[s]"]
    assert selection["status"] == "partial" and selection["value_type"] == "selection_structure"
    assert selection["value"]["children"][0]["children"][1]["attributes"]["feature"] == "/geom/g1/feature/b1"
    assert unknown["status"] == "unresolved" and unknown["value_type"] == "xml_structure"
    assert "UnknownMatrix" in unknown["raw_xml"]
    assert result["unsupported_child_kinds"] == {"UnknownMatrix": 1}


@pytest.mark.parametrize("encoded,issue", [("2|2,'1','2'", "declared_row_count_mismatch"), ("1|1,'bad", "unterminated_quoted_cell"),
                                          ("1|1,5", "expected_quoted_cell"), ("1|1,'x',", "trailing_delimiter"),
                                          ("1|1,'x'bad", "unexpected_data_after_cell")])
def test_malformed_matrix_reports_gap_without_executing_or_inventing(encoded, issue):
    element = ET.Element("Node")
    ET.SubElement(element, "propertyValue", {"name": "p:bad", "valueMatrix": encoded})
    item = normalize_properties(element)["properties"][0]
    assert item["status"] != "decoded"
    assert issue in item["issues"] and item["raw_value"] == encoded


def test_no_eval_even_for_code_looking_expression(tmp_path):
    target = tmp_path / "must-not-be-created"
    text = "__import__(\"pathlib\").Path(\"" + str(target) + "\").touch()"
    element = ET.Element("Node")
    ET.SubElement(element, "propertyValue", {"name": "p:expression", "value": text})
    with patch("builtins.eval", side_effect=AssertionError("No eval")), patch("builtins.exec", side_effect=AssertionError("No exec")):
        result = normalize_properties(element)
    assert result["properties"][0]["value"] == text
    assert not target.exists()


def test_explicit_ragged_rows_are_not_reshaped_or_padded():
    item = normalized('<propertyValue name="p:levels" valueMatrix="2|2,\'0\',\'1\'|3,\'0\',\'1\',\'2\'"/>')["properties"][0]
    assert item["status"] == "decoded"
    assert item["value"]["shape"] == [2, None]
    assert item["value"]["row_lengths"] == [2, 3]
    assert item["value"]["rows"] == [["0", "1"], ["0", "1", "2"]]


def test_limits_bound_property_count_cells_raw_text_and_nested_structure():
    element = ET.Element("Node")
    for i in range(5):
        ET.SubElement(element, "propertyValue", {"name": "p:long" + str(i), "value": "x"*1000})
    result = normalize_properties(element, max_items=2, max_text=80)
    assert result["total"] == 5 and len(result["properties"]) == 2 and result["unread_count"] == 3
    assert result["truncated"] is True
    assert all(len(item["raw_value"]) <= 80 and len(item["raw_xml"]) <= 80 and item["status"] == "partial" for item in result["properties"])
    matrix = normalized('<propertyValue name="p:a" valueMatrix="1|5,\'a\',\'b\',\'c\',\'d\',\'e\'"/>', max_items=2)
    assert matrix["properties"][0]["value"]["entries"] == ["a", "b"]
    assert matrix["properties"][0]["truncated"] is True
    deep = ET.Element("Node")
    prop = ET.SubElement(deep, "propertyValue", {"name": "p:tree"})
    current = prop
    for _ in range(100):
        current = ET.SubElement(current, "Future")
    tree = normalize_properties(deep, max_items=10, max_text=120)["properties"][0]
    assert tree["truncated"] is True and len(tree["raw_xml"]) <= 120
    assert len(json.dumps(tree)) < 6000


def test_nonfinite_and_out_of_range_values_are_strict_json():
    result = normalized('''<propertyValue name="p:n" value="NaN"/>
<propertyValue name="p:i" value="Infinity"/><propertyValue name="p:huge" value="1e9999"/>''')
    assert [p["value"]["numeric_status"] for p in result["properties"]] == ["non_finite", "non_finite", "out_of_range"]
    json.dumps(result, allow_nan=False)


def test_raw_metadata_and_missing_name_are_preserved_without_fake_property_name():
    result = normalized('''<active>off</active><storedParent value="/geom/g1"/><propertyValue value="42"/>''')
    assert result["total"] == 1 and result["raw_total"] == 3
    assert len(result["raw_properties"]) == 3
    assert result["properties"][0]["name"] is None and result["unresolved_count"] == 1


@pytest.mark.parametrize("kwargs", [{"max_items": -1}, {"max_items": True}, {"max_text": 0}, {"max_text": "80"}])
def test_invalid_limits_are_explicit(kwargs):
    with pytest.raises(ValueError):
        normalized("<propertyValue name='x' value='1'/>", **kwargs)
