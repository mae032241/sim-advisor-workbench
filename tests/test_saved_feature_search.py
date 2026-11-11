"""Offline saved feature recall, with exact evidence and bounded index input."""
import hashlib
import json
from pathlib import Path
import shutil
from unittest.mock import patch
import zipfile

import pytest

from advisor import library_model as library
from advisor.case_metadata import link_case_objects, object_feature_branch
from advisor.library_search import LibrarySearch, _representation
from advisor.library_store import LibraryStore


XML = '''<Model tag="m"><MeshList tag="mesh"><MeshSequence tag="mesh1">
<MeshFeature tag="size" op="Size" name="Local size">
<param param="hmax" value="1|1,'elSize'"/><propertyValue name="p:hauto" value="4"/>
<selection T="32" selType="GEOMDIM"><named>/selection/cooling_faces</named>
<explicit dim="2" hDim="2" geom="geom1" entities="10,1,4,5"/></selection>
</MeshFeature></MeshSequence></MeshList>
<Physics tag="ht" op="HeatTransfer"><PhysicsFeature tag="size" op="HeatFluxBoundary">
<param param="h" value="1|1,'10'"/></PhysicsFeature></Physics></Model>'''


def mph(path, xml=XML):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as out:
        out.writestr("modelinfo.xml", '<modelInfo comsolVersion="6.3" title="Standalone fixture"/>')
        out.writestr("dmodel.xml", xml)
    return path


def extract(path, tmp_path):
    with patch.object(library, "_deep_state", side_effect=AssertionError("COMSOL must not start")):
        return library.extract_case(path, artifact_dir=tmp_path / "artifacts", docs_root=tmp_path / "unbound-docs", deep=False)


def search_objects(result):
    return [dict(item, id=item["local_id"], library_id="standalone", version=result["version"],
                 object_id=item["local_id"], revision_id="fixture", source_uri="fixture://" + item["local_id"])
            for item in link_case_objects(result["objects"])]


def pointer(document, locator):
    value = document
    for part in locator.removeprefix("json:/").split("/"):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def test_standalone_saved_properties_and_selections_recall_exact_snapshot_without_comsol(tmp_path):
    path = mph(tmp_path / "source" / "standalone.mph")
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = extract(path, tmp_path)
    assert not any(item["type"] in {"method_step", "implementation_snippet"} for item in result["objects"])
    saved = [item for item in result["objects"] if item["type"] == "saved_configuration"]
    assert saved and result["coverage"]["saved_features"]["properties_represented"] == 3
    snapshot = json.loads(Path(saved[0]["evidence"][0]["asset_path"]).read_text(encoding="utf-8"))
    assert snapshot["source_sha256"] == hashlib.sha256(before[0]).hexdigest()
    for item in saved:
        assert "not proven effective" in item["text"]
        assert item["data"]["effective_activation"] == "not_evaluated"
        assert item["data"]["selection_membership"] == "not_validated"
        assert all(e["method"] == "native_mph_xml_snapshot" for e in item["evidence"])
        node = pointer(snapshot, item["data"]["snapshot_locator"])
        assert node["path"] == item["data"]["node_path"]
        for prop in item["data"]["saved_properties"]:
            assert pointer(snapshot, prop["snapshot_locator"])["name"] == prop["name"]
        for selection in item["data"]["saved_selections"]:
            assert pointer(snapshot, selection["snapshot_locator"])["status"] == selection["status"]
    search = LibrarySearch(tmp_path / "search", cache_dir=tmp_path / "cache")
    search.build(search_objects(result), modes=["native"], generation="saved-features")
    for query in ("hmax", "hauto", "cooling_faces", "decoded_entities"):
        hits = search.search(query, mode="native", feature_branch="mesh")
        assert hits and all(item["type"] == "saved_configuration" for item in hits), query
        assert all(object_feature_branch(item) == "mesh" for item in hits)
    mesh = next(item for item in saved if item["data"]["saved_properties"] and object_feature_branch(item) == "mesh")
    assert "hmax=\"elSize\"" in mesh["text"] and "hauto=4" in mesh["text"]
    assert path.read_bytes() == before[0] and path.stat().st_mtime_ns == before[1]
    repeated = extract(path, tmp_path)
    assert [item["local_id"] for item in result["objects"]] == [item["local_id"] for item in repeated["objects"]]


def test_large_property_nodes_keep_mesh_controls_selection_and_report_omissions(tmp_path):
    properties = "".join(f'<propertyValue name="p:other_{i}" value="' + "x" * 300 + '"/>' for i in range(490))
    xml = '<Model tag="m"><MeshFeature tag="size" op="Size">' + properties + '''
      <propertyValue name="p:hmax" value="0.007"/><propertyValue name="p:hauto" value="1"/>
      <selection T="32" selType="GEOMDIM"><named>late_selection</named></selection>
      </MeshFeature></Model>'''
    result = extract(mph(tmp_path / "large.mph", xml), tmp_path)
    features = [item for item in result["objects"] if item["type"] == "saved_configuration"]
    coverage = result["coverage"]["saved_features"]
    assert len(features) == library.SAVED_FEATURE_LIMITS["max_parts_per_node"]
    assert coverage["truncated"] and coverage["omitted_properties"] > 400
    assert coverage["selections_represented"] == 1 and coverage["preview_truncations"]
    represented = "\n".join(_representation(item)[0] for item in features)
    assert all(token in represented for token in ("hmax=0.007", "hauto=1", "late_selection"))
    assert max(len(item["text"]) for item in features) < 750
    assert max(len(json.dumps(item, ensure_ascii=False).encode("utf-8")) for item in features) < 16000
    assert max(len(item["data"]["saved_properties"]) for item in features) <= library.SAVED_FEATURE_LIMITS["max_items_per_object"]


def test_total_object_limit_keeps_exact_node_locator_and_visible_omission_counts(tmp_path):
    xml = '<Model tag="m">' + "".join(f'<MeshFeature tag="size{i}" op="Size"><propertyValue name="p:hmax" value="{i}"/></MeshFeature>' for i in range(20)) + '</Model>'
    records = library._xml_state(mph(tmp_path / "many.mph", xml))[0]
    with patch.dict(library.SAVED_FEATURE_LIMITS, {"max_objects": 7}):
        objects, coverage = library._saved_feature_objects(records, tmp_path / "snapshot.json")
    assert len(objects) == 7 and len({item["local_id"] for item in objects}) == 7
    assert coverage["represented_nodes"] == 7 and coverage["omitted_nodes"] == 13
    assert coverage["omitted_properties"] == 13 and coverage["truncated"]
    assert all(item["evidence"][0]["locator"] == item["data"]["snapshot_locator"] for item in objects)


def test_old_single_object_cache_invalidates_and_unchanged_saved_feature_cache_reuses(tmp_path):
    path = mph(tmp_path / "sources" / "model.mph")
    store = LibraryStore(tmp_path / "catalog")
    registered = store.register("standalone", "user-models", [{"id": "models", "path": str(path.parent)}])
    registered = store.set_enabled(registered["id"], True)
    with patch("advisor.model_reader.parser_status", return_value={"available": False, "detail": "Test uses native saved XML"}):
        first = store.build(registered["id"], deep=False, modes=[])
        assert not first.get("failed"), first
        cache = next((store.root / "artifacts").rglob("extraction.json"))
        candidate = json.loads(cache.read_text(encoding="utf-8"))
        candidate["parser_hash"] = "old-single-model-case-parser"
        candidate["result"]["objects"] = [item for item in candidate["result"]["objects"] if item["type"] == "model_case"]
        cache.write_text(json.dumps(candidate), encoding="utf-8")
        with patch.object(library, "extract_case", wraps=library.extract_case) as refreshed:
            rebuilt = store.build(registered["id"], deep=False, modes=[])
        assert not rebuilt.get("failed"), rebuilt
        assert refreshed.call_count == 1
        assert any(item["type"] == "saved_configuration" for item in store.objects([registered["id"]]))
        with patch.object(library, "extract_case", side_effect=AssertionError("Unchanged cache should be reused")):
            reused = store.build(registered["id"], deep=False, modes=[])
        assert not reused.get("failed"), reused
        dependencies = json.loads(cache.read_text(encoding="utf-8"))["result"]["extractor_dependencies"]
        assert {Path(item["path"]).name for item in dependencies} >= {"mph_saved_values.py", "mph_saved_selections.py", "case_metadata.py"}


def test_actual_bracket_standalone_native_and_qmd_keyword_saved_features(tmp_path):
    original = Path("D:/comsol/COMSOL63/Multiphysics/applications/Structural_Mechanics_Module/Tutorials/bracket_basic.mph")
    if not original.is_file():
        pytest.skip("Optional installed official bracket_basic sample unavailable")
    search = LibrarySearch(tmp_path / "search", cache_dir=tmp_path / "cache")
    if not search.node or not (search.qmd_dir / "dist/index.js").is_file():
        pytest.skip("Optional local QMD/Node integration dependencies unavailable")
    source = tmp_path / "standalone.mph"
    shutil.copyfile(original, source)
    result = extract(source, tmp_path)
    assert result["coverage"]["raw_xml"]["node_count"] > 300
    assert result["coverage"]["tutorial"]["status"] == "missing"
    assert result["coverage"]["implementation"]["status"] == "missing"
    objects = search_objects(result)
    built = search.build(objects, modes=["native", "qmd_keyword"], generation="actual-bracket")
    assert built["modes"]["qmd_keyword"]["ready"], built["modes"]["qmd_keyword"]
    assert not built["modes"].get("qmd_vector", {}).get("ready")
    for mode in ("native", "qmd_keyword"):
        for query in ("hmax", "hauto", "adj1"):
            hits = search.search(query, mode=mode, feature_branch="mesh")
            assert hits and any(item["type"] == "saved_configuration" for item in hits), (mode, query)
            assert all(item["data"]["effective_activation"] == "not_evaluated" for item in hits)
    defaults = next(item for item in objects if item["data"].get("feature_type") == "MeshSizeDefault")
    assert "hmax=0.028" in defaults["text"] and "hauto=4" in defaults["text"]
    assert result["coverage"]["raw_xml"]["normalization"]["selection_statuses"].get("unresolved", 0) > 0
