import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.mph_saved_selections import ENCODING, normalize_selections


def selection(raw, *, dim="2", high="2", type="GEOMDIM", T="32"):
    root = ET.Element("selection", {"T": T, "selection": "XDLocalSelection", "selType": type})
    attrs = {"dim": dim, "hDim": high, "geom": "/geom/geom1"}
    if raw is not None:
        attrs["entities"] = raw
    ET.SubElement(root, "explicit", attrs)
    return root


def decoded(element, **kwargs):
    return normalize_selections(element, **kwargs)["entries"][0]["explicit"][0]


def test_negative_span_and_offset_are_headers_not_entity_numbers():
    result = decoded(selection("-98,-1,30"))
    assert result["status"] == "decoded_saved" and result["decoded_entities"] == [30]
    assert result["raw_encoded"] == "-98,-1,30" and result["encoding"] == ENCODING
    assert result["encoding_header"] == {"header_tokens": [-98, -1], "span": 98, "offset": -1,
                                         "lower_bound": 1, "upper_bound_exclusive": 99}
    assert result["entity_dimension"] == result["highest_dimension"] == result["encoded_entity_dimension"] == 2
    assert result["geometry_ref"] == "/geom/geom1" and result["membership_status"] == "not_evaluated"


def test_nonnegative_first_integer_is_also_a_header_and_duplicates_follow_set_semantics():
    result = decoded(selection("10,8,3,2,7,3", dim="3", high="3"))
    assert result["decoded_entities"] == [2, 3, 7, 8]
    assert result["encoding_header"]["header_tokens"] == [10]
    assert result["serialized_value_count"] == 5 and result["decoded_count"] == 4
    assert decoded(selection("0"))["decoded_entities"] == []


def test_signed_payload_members_are_retained_when_inside_the_encoded_bounds():
    result = decoded(selection("-3,1,-1,0,1"))
    assert result["decoded_entities"] == [-1, 0, 1], "Dropping every negative integer would corrupt this saved bitset"
    assert result["encoding_header"]["lower_bound"] == -1
    assert decoded(selection("-3,1,1"))["decoded_entities"] == [1]


@pytest.mark.parametrize("raw", ["-98", "-98,-1,99", "-98,-1,0", "0,1", "-2147483648,-1,3",
                                  "3,1.5", "{3,1}", "not-an-integer", "3,,1"])
def test_unproven_or_out_of_bounds_encoding_is_preserved_unresolved(raw):
    result = decoded(selection(raw))
    assert result["status"] == "unresolved" and result["raw_encoded"] == raw
    assert "decoded_entities" not in result and result["reason"]


def test_requested_dim_and_saved_highest_dim_remain_distinct():
    result = decoded(selection("7,2,5", dim="2", high="3"))
    assert result["decoded_entities"] == [2, 5]
    assert result["entity_dimension"] == 2 and result["encoded_entity_dimension"] == result["highest_dimension"] == 3
    assert result["dimension_relation"] == "different" and result["membership_status"] == "not_evaluated"


def test_missing_entities_and_unrecognized_context_do_not_become_empty_or_all():
    result = decoded(selection(None))
    assert result["status"] == "unresolved" and "decoded_entities" not in result
    assert "absence does not prove" in result["reason"]
    for element in (selection("-98,-1,30", T="31"), selection("-98,-1,30", type="UNKNOWN")):
        assert decoded(element)["status"] == "unresolved"


def test_only_this_feature_direct_selection_children_are_collected():
    feature = ET.Element("PhysicsFeature", {"tag": "pot1"})
    feature.append(selection("-98,-1,30"))
    nested = ET.SubElement(feature, "PhysicsFeature", {"tag": "other"})
    nested.append(selection("-98,-1,10"))
    result = normalize_selections(feature)
    assert len(result["entries"]) == 1 and result["entries"][0]["explicit"][0]["decoded_entities"] == [30]
    assert normalize_selections(ET.Element("Feature"))["status"] == "not_present"


def test_named_output_and_object_references_are_not_resolved_into_membership():
    feature = ET.fromstring('''<GeomFeature tag="blk1"><selection T="32" selType="GEOMOBJECT">
      <base dim="2" geom="/geom/geom1"/><object flags="11" geom="/geom/geom1/feature/blk1/geom/blk1(1)"
      feature="/geom/geom1/feature/blk1" assocId="{3,0,-1962137116,455459143}"/></selection>
      <selection T="32" selType="GEOMDIM"><named>/selection/sel1</named></selection>
      <outputSelection T="50">/geom/geom1/selection/blk1.bnd</outputSelection></GeomFeature>''')
    result = normalize_selections(feature)
    obj = result["entries"][0]["objects"][0]
    assert obj["association_id"]["values"] == [3, 0, -1962137116, 455459143]
    assert obj["association_id"]["status"] == "observed_identifier_tuple"
    assert result["entries"][0]["base"][0]["entity_dimension"] == 2
    assert result["entries"][1]["named_references"] == ["/selection/sel1"]
    assert result["entries"][2]["output_references"] == ["/geom/geom1/selection/blk1.bnd"]
    assert not result["membership_evaluated"] and not result["geometry_executed"]


def test_remaining_keeps_ordered_groups_without_guessing_dimension_or_final_members():
    element = ET.fromstring('<selection T="32" selection="MeshSelection" selType="MESHREMAINING"><remaining>{1,2},{3},{4,5},{1}</remaining></selection>')
    result = normalize_selections(element)
    groups = result["entries"][0]["remaining"]
    assert [item["values"] for item in groups] == [[1, 2], [3], [4, 5], [1]]
    assert [item["group_index"] for item in groups] == [0, 1, 2, 3]
    assert all(item["entity_dimension"] is None and item["status"] == "saved_group_not_final_membership" for item in groups)
    assert not result["entries"][0]["explicit"] and result["status"] == "observed_structure"
    broken = ET.fromstring('<selection><remaining>{1:9},{2}</remaining></selection>')
    assert normalize_selections(broken)["status"] == "unresolved"


def test_limits_are_honest_and_do_not_decode_a_truncated_numeric_stream():
    result = normalize_selections(selection("20,1,2,3,4,5,6,7,8"), max_items=4)
    explicit = result["entries"][0]["explicit"][0]
    assert result["truncated"] and explicit["status"] == "partial" and len(explicit["decoded_entities"]) <= 4
    result = normalize_selections(selection("-98,-1,30"), max_text=5)
    assert result["truncated"] and "decoded_entities" not in result["entries"][0]["explicit"][0]
    giant = selection("-98,-1,30", dim="1" * 10000)
    giant[0].set("geom", "x" * 100000)
    result = normalize_selections(giant, max_text=100)
    assert result["truncated"] and len(result["entries"][0]["explicit"][0]["geometry_ref"] or "") <= 100
    assert result["entries"][0]["explicit"][0]["entity_dimension"] is None


@pytest.mark.parametrize("kwargs", [{"max_items": 0}, {"max_text": 0}, {"max_items": True}, {"max_text": 1000001}])
def test_invalid_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        normalize_selections(selection("1"), **kwargs)
