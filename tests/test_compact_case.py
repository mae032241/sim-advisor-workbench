"""The prototype keeps one canonical database and hash-bound original evidence."""
import copy
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET
import zipfile

import pytest

from advisor.compact_case import (DATABASE, CompactCase, CompactCaseIntegrityError,
                                 RawEvidenceUnavailable, add_compact_api, build_compact_case)


XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<Model><ModelNode tag="comp1" name="Component"><Wrapper><MeshSequence tag="mesh1" name="Mesh">
<param param="hmax" value="0.05"/><param param="hauto" value="3"/>
<param param="unknown" strange="unresolved"/><selection name="inlet_selection"><named name="inlet"/></selection>
<selection/><param param="matrix" valueMatrix="2|2,'1','2'|2,'3','4'"/>
</MeshSequence></Wrapper></ModelNode></Model>'''


def fixture(tmp_path, *, extra_properties=0):
    source_dir = tmp_path / "original"
    source_dir.mkdir()
    source = source_dir / "case.mph"
    raw_xml = XML.replace(b"</MeshSequence>", b"".join(f'<param param="additional_{index}" value="0.05"/>'.encode()
                          for index in range(extra_properties)) + b"</MeshSequence>")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("dmodel.xml", raw_xml)
        archive.writestr("modelinfo.xml", '<modelinfo version="6.3"/>')
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    parsed = {"case": {"source_path": str(source), "source_sha256": source_hash,
                       "source_size_bytes": source.stat().st_size, "title": "A saved case", "version": "6.3",
                       "parser_signature": "fixture/v1", "coverage": {"properties_unresolved": 1},
                       "parser_contract": {"effective_settings": "not_evaluated", "geometry_run": False}},
              "nodes": [{"id": 1, "parent_id": None, "xml_route": [], "kind": "Model", "label": "Case"},
                        {"id": 2, "parent_id": 1, "xml_route": [0], "kind": "ModelNode", "tag": "comp1", "label": "Component"},
                        {"id": 3, "parent_id": 2, "xml_route": [0, 0], "kind": "MeshSequence", "tag": "mesh1", "label": "Mesh", "feature_branch": "mesh"}],
              "properties": [{"node_id": 3, "ordinal": 1, "child_index": 0, "name": "hmax", "value": 0.05,
                              "status": "decoded", "value_type": "number"},
                             {"node_id": 3, "ordinal": 2, "child_index": 1, "name": "hauto", "value": 3,
                              "status": "decoded", "value_type": "number"},
                             {"node_id": 3, "ordinal": 3, "child_index": 2, "name": "unknown", "value": None,
                              "status": "unresolved", "value_type": "unknown", "issues": ["unsupported_payload"]},
                             {"node_id": 3, "ordinal": 4, "child_index": 5, "name": "matrix",
                              "value": {"shape": [2, 2], "rows": [["1", "2"], ["3", "4"]], "row_lengths": [2, 2]},
                              "status": "decoded", "value_type": "matrix"}],
              "selections": [{"node_id": 3, "ordinal": 1, "child_index": 3, "kind": "selection", "status": "observed_structure",
                              "value": {"named_references": ["inlet_selection", "inlet"]}},
                             {"node_id": 3, "ordinal": 2, "child_index": 4, "kind": "selection", "status": "unresolved", "value": {}}],
              "documents": [{"id": 1, "source_uri": "file:///fixture/tutorial.html", "source_sha256": "d" * 64, "kind": "associated_tutorial_html"}],
              "passages": [{"id": 1, "document_id": 1, "ordinal": 1, "title": "Mesh instructions", "text": "Use a documented mesh size.", "kind": "method_step", "locators": ["html:#mesh"]},
                           {"id": 2, "document_id": 1, "ordinal": 2, "title": "Repeated reference", "text": "Use a documented mesh size.", "kind": "case_context", "locators": ["html:#note"]}],
              "raw_xml": raw_xml}
    # Repeated values exercise interning while each row retains its own source.
    for index in range(extra_properties):
        parsed["properties"].append({"node_id": 3, "ordinal": 5 + index, "child_index": 6 + index,
            "name": f"additional_{index}", "value": 0.05, "status": "decoded", "value_type": "number"})
    calls = []

    def parser(path, docs_root=None):
        assert path == source
        calls.append(path)
        return copy.deepcopy(parsed)

    return source, parsed, parser, calls


def build(tmp_path, **kwargs):
    source, parsed, parser, calls = fixture(tmp_path, **kwargs)
    output = tmp_path / "compact"
    result = build_compact_case(source, output, parser=parser, signature="fixture/v1")
    return source, parsed, parser, calls, output, CompactCase(output), result


def test_single_database_preserves_typed_configuration_and_context(tmp_path):
    source, parsed, _, _, output, reader, result = build(tmp_path)
    assert [path.name for path in output.iterdir()] == [DATABASE]
    assert result["counts"] == {"nodes": 3, "properties": 4, "selections": 1, "documents": 1, "passages": 2}
    assert not result["cache_hit"] and result["format"].endswith("prototype-v1")
    node = reader.read_node(3)
    assert node["case_ref"] == result["case_ref"] and node["parent_id"] == 2
    assert reader.get_property(3, "hmax")["value"] == 0.05
    assert reader.get_property(3, "hauto")["value"] == 3
    assert reader.get_property(3, "matrix")["value"] == parsed["properties"][3]["value"]
    unknown = reader.get_property(3, "unknown")
    assert unknown["value"] is None and unknown["status"] == "unresolved" and unknown["decoded"] is False
    assert "coverage" not in node and "source_sha256" not in node
    document = reader.read_document(1)
    assert document["passages"][0]["text"] == "Use a documented mesh size."
    assert document["passages"][0]["locators"] == ["html:#mesh"]
    assert source.is_file() and reader.read_raw_xml() == XML


def test_default_storage_keeps_all_properties_and_selections_without_local_search(tmp_path):
    _, _, _, _, output, reader, _ = build(tmp_path, extra_properties=700)
    for name in ("hmax", "hauto", "additional_699"):
        assert reader.get_property(3, name)["name"] == name
    assert reader.read_node(3)["selections"][0]["value"]["named_references"] == ["inlet_selection", "inlet"]
    with pytest.raises(ValueError, match="register the source library.*qmd_keyword"):
        reader.search("hmax")
    assert len(list(reader.iter_nodes())) == 3
    with closing(sqlite3.connect(output / DATABASE)) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'native_fts%'").fetchone()
        # 703 decoded property occurrences share just 3 typed values.
        assert db.execute("SELECT count(*) FROM value_pool").fetchone()[0] == 4  # 3 property values + 1 selection
        assert db.execute("SELECT count(*) FROM text_pool").fetchone()[0] == 1


def test_evidence_routes_recover_original_property_and_nontrivial_parent_path(tmp_path):
    _, _, _, _, _, reader, result = build(tmp_path)
    prop = reader.get_property(3, "hmax")
    evidence = reader.read_raw_evidence(property_id=prop["id"])
    assert evidence["status"] == "available" and evidence["xml_route"] == [0, 0, 0, 0]
    assert 'param="hmax"' in evidence["xml"] and 'value="0.05"' in evidence["xml"]
    assert evidence["raw_xml_sha256"] == result["xml_sha256"] == hashlib.sha256(XML).hexdigest()
    assert reader.validate_evidence_routes()


def test_unchanged_build_reuses_database_without_parser_or_extra_files(tmp_path):
    source, _, parser, calls, output, reader, _ = build(tmp_path)
    original = (output / DATABASE).read_bytes()
    stamp = (output / DATABASE).stat().st_mtime_ns
    reused = build_compact_case(source, output, parser=parser, signature="fixture/v1")
    assert reused["cache_hit"] and len(calls) == 1
    assert (output / DATABASE).read_bytes() == original and (output / DATABASE).stat().st_mtime_ns == stamp
    assert [path.name for path in output.iterdir()] == [DATABASE]


def test_missing_original_keeps_normalized_information_and_reports_evidence_unavailable(tmp_path):
    source, _, _, _, _, reader, _ = build(tmp_path)
    source.unlink()
    assert reader.get_property(3, "hmax")["value"] == 0.05
    assert reader.read_case()["source_available"] is False
    assert reader.read_raw_evidence(3)["status"] == "unavailable"
    with pytest.raises(RawEvidenceUnavailable):
        reader.read_raw_xml()


def test_changed_original_cannot_supply_raw_evidence_or_portable_rebind(tmp_path):
    source, _, _, _, output, reader, _ = build(tmp_path)
    source.write_bytes(b"Changed original; no longer the hash-bound source")
    assert reader.get_property(3, "hmax")["value"] == 0.05
    with pytest.raises(CompactCaseIntegrityError, match="changed"):
        reader.read_raw_evidence(3)
    with pytest.raises(CompactCaseIntegrityError, match="not the original"):
        reader.rebind_source(source)
    assert [path.name for path in output.iterdir()] == [DATABASE]


def test_exact_source_can_be_moved_and_rebound_with_no_snapshot(tmp_path):
    source, _, _, _, output, reader, _ = build(tmp_path)
    moved = tmp_path / "moved.mph"
    source.rename(moved)
    assert reader.read_raw_evidence(3)["status"] == "unavailable"
    assert reader.rebind_source(moved)["source_path"] == str(moved)
    assert reader.read_raw_xml() == XML
    assert [path.name for path in output.iterdir()] == [DATABASE]


def test_bad_pointer_failure_leaves_previous_case_and_no_staging_files(tmp_path):
    source, parsed, _, _, output, reader, _ = build(tmp_path)
    before = (output / DATABASE).read_bytes()
    parsed["nodes"][2]["xml_route"] = [999]
    parsed["case"]["parser_signature"] = "changed/v2"
    with pytest.raises(CompactCaseIntegrityError, match="does not resolve"):
        build_compact_case(source, output, parser=lambda *a, **kw: parsed, signature="changed/v2")
    assert (output / DATABASE).read_bytes() == before
    assert [path.name for path in output.iterdir()] == [DATABASE]
    assert reader.get_property(3, "hmax")["value"] == 0.05


def test_source_only_descriptor_is_not_reported_as_full_decoded_value(tmp_path):
    source, parsed, _, _ = fixture(tmp_path)
    parsed["properties"][3].update(value={"source_only": True, "shape": [100, 100], "sample": [1, 2, 3, 4]}, value_omitted=True)
    output = tmp_path / "compact"
    build_compact_case(source, output, parser=lambda *a, **kw: parsed, signature="fixture/v1")
    prop = CompactCase(output).get_property(3, "matrix")
    assert prop["status"] == "decoded" and prop["value_omitted"] and not prop["decoded"]
    assert prop["value_complete"] is False
    assert prop["value"]["source_only"] is True


def test_default_parser_integrates_with_typed_reader_and_original_zip_evidence(tmp_path):
    source, _, _, _ = fixture(tmp_path)
    docs = tmp_path / "empty-documents"
    docs.mkdir()
    output = tmp_path / "actual-parser"
    result = build_compact_case(source, output, docs_root=docs)
    reader = CompactCase(output)
    mesh = next(node for node in reader.iter_nodes() if node["tag"] == "mesh1")
    assert reader.get_property(mesh["id"], "hmax")["value"] == "0.05"  # Saved expression stays a string.
    assert reader.get_property(mesh["id"], "unknown")["value_complete"] is False
    assert reader.read_raw_xml() == XML and reader.validate_evidence_routes()
    assert result["xml_sha256"] == hashlib.sha256(XML).hexdigest()
    assert [path.name for path in output.iterdir()] == [DATABASE]
    assert build_compact_case(source, output, docs_root=docs)["cache_hit"] is True


def api_bundle(case, observations):
    return {"source_sha256": case["source_sha256"],
            "run": {"schema": "compact-api-fixture/v1", "version": "6.3", "contract": {"geometry_run": False,
                    "mesh_run": False, "solved": False, "value_scope": "saved/current getters; effectiveness not evaluated"},
                    "scope": "selected mapped functional nodes", "counts": {"selected": len(observations)}},
            "nodes": [{"id": 1, "compact_node_id": 3, "api_path": "model/component(comp1)/mesh(mesh1)"}],
            "observations": observations}


def test_api_corroboration_reuses_same_canonical_value_and_preserves_xml_rows(tmp_path):
    _, _, _, _, output, reader, case = build(tmp_path)
    before_node = reader.read_node(3)
    with closing(sqlite3.connect(output / DATABASE)) as db:
        values_before = db.execute("SELECT count(*) FROM value_pool").fetchone()[0]
        saved_id = db.execute("SELECT value_id FROM properties WHERE node_id=3 AND ordinal=1").fetchone()[0]
    bundle = api_bundle(case, [{"api_node_id": 1, "kind": "property", "name": "hmax", "value_type": "double",
        "status": "observed", "getter": "getDouble", "relation": "corroborates_saved_xml", "xml_property_ordinal": 1}])
    result = add_compact_api(output, bundle)
    assert result["api"]["counts"] == {"runs": 1, "nodes": 1, "observations": 1}
    assert reader.read_node(3)["properties"] == before_node["properties"]
    observed = reader.get_property(3, "hmax", source="api")
    assert observed["value"] == 0.05 and observed["comparison"] == "same_saved_value" and observed["value_complete"]
    assert observed["relation"] == "corroborates_saved_xml" and observed["source"] == "api_getter_observation"
    with closing(sqlite3.connect(output / DATABASE)) as db:
        assert db.execute("SELECT count(*) FROM value_pool").fetchone()[0] == values_before
        assert db.execute("SELECT value_id FROM api_observations").fetchone()[0] == saved_id
    assert [path.name for path in output.iterdir()] == [DATABASE]
    before = (output / DATABASE).read_bytes()
    assert reader.add_api(bundle)["api_cache_hit"] is True
    assert (output / DATABASE).read_bytes() == before


def test_new_api_default_remains_readable_without_becoming_a_saved_xml_property(tmp_path):
    _, _, _, _, output, reader, case = build(tmp_path)
    bundle = api_bundle(case, [{"api_node_id": 1, "kind": "property", "name": "hminactive", "value": True,
        "value_type": "boolean", "status": "observed", "getter": "getBoolean", "relation": "api_only"}])
    reader.add_api(bundle)
    with pytest.raises(KeyError):
        reader.get_property(3, "hminactive")
    observed = reader.get_property(3, "hminactive", source="api")
    assert observed["value"] is True and observed["comparison"] == "api_only"
    evidence = reader.read_api_evidence(observed["id"])
    assert evidence["api_path"].endswith("mesh(mesh1)") and evidence["getter"] == "getBoolean"
    assert evidence["source_sha256"] == case["source_sha256"] and evidence["xml_evidence"] is False
    assert "xml" not in evidence and evidence["run"]["version"] == "6.3"
    with closing(sqlite3.connect(output / DATABASE)) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'native_fts%'").fetchone()


def test_different_api_value_is_explicit_and_does_not_overwrite_saved_value(tmp_path):
    _, _, _, _, _, reader, case = build(tmp_path)
    reader.add_api(api_bundle(case, [{"api_node_id": 1, "name": "hmax", "kind": "property", "value": 0.1,
        "value_type": "double", "status": "observed", "getter": "getDouble", "relation": "different_from_saved_xml"}]))
    assert reader.get_property(3, "hmax")["value"] == 0.05
    observed = reader.get_property(3, "hmax", source="api")
    assert observed["value"] == 0.1 and observed["comparison"] == "differs_from_saved"
    assert observed["saved_property_id"] == reader.get_property(3, "hmax")["id"]


def test_api_source_only_descriptor_and_unresolved_getter_never_claim_complete_values(tmp_path):
    _, _, _, _, _, reader, case = build(tmp_path)
    reader.add_api(api_bundle(case, [
        {"api_node_id": 1, "name": "tensor", "value": {"source_only": True, "shape": [100, 100], "sample": [1, 2]},
         "value_type": "matrix", "status": "observed", "getter": "getDoubleMatrix", "relation": "api_only", "value_omitted": True},
        {"api_node_id": 1, "name": "unknown", "value": None, "value_type": "unknown", "status": "unresolved",
         "getter": "get", "relation": "unresolved", "issues": ["unsupported_getter"]}]))
    assert reader.get_property(3, "tensor", source="api")["value_complete"] is False
    unknown = reader.get_property(3, "unknown", source="api")
    assert unknown["value"] is None and unknown["status"] == "unresolved" and not unknown["decoded"]
    assert unknown["comparison"] == "not_comparable"


def test_failed_api_extractor_and_invalid_mapping_leave_database_byte_identical(tmp_path):
    _, _, _, _, output, reader, case = build(tmp_path)
    before = (output / DATABASE).read_bytes()

    def failed(_reader):
        raise RuntimeError("Read-only API fixture failed before a result")

    with pytest.raises(RuntimeError, match="fixture failed"):
        reader.add_api(extractor=failed)
    assert (output / DATABASE).read_bytes() == before
    invalid = api_bundle(case, [])
    invalid["nodes"].append({"id": 2, "compact_node_id": 2, "api_path": invalid["nodes"][0]["api_path"]})
    with pytest.raises(ValueError, match="Ambiguous"):
        reader.add_api(invalid)
    assert (output / DATABASE).read_bytes() == before and [path.name for path in output.iterdir()] == [DATABASE]
    assert reader.read_case()["api"]["status"] == "not_requested"


def test_api_observations_keep_their_context_after_original_is_offline(tmp_path):
    source, _, _, _, _, reader, case = build(tmp_path)
    reader.add_api(api_bundle(case, [{"api_node_id": 1, "name": "hmax", "value": 0.05, "status": "observed",
                                    "value_type": "double", "getter": "getDouble", "relation": "corroborates_saved_xml"}]))
    source.unlink()
    observed = reader.get_property(3, "hmax", source="api")
    assert observed["value"] == 0.05
    assert reader.read_api_evidence(observed["id"])["source_sha256"] == case["source_sha256"]
    assert reader.read_raw_evidence(3)["status"] == "unavailable"


def test_unresolved_api_without_payload_is_preserved_and_failed_runs_do_not_write(tmp_path):
    _, _, _, _, output, reader, case = build(tmp_path)
    bundle = api_bundle(case, [{"api_node_id": 1, "name": "unknown", "status": "unresolved", "value_type": "unknown",
                               "getter": "get", "relation": "getter_unavailable", "issues": ["unsupported_getter"]}])
    reader.add_api(bundle)
    unresolved = reader.get_property(3, "unknown", source="api")
    assert unresolved["value"] is None and not unresolved["value_complete"]
    before = (output / DATABASE).read_bytes()
    bundle["run"]["status"] = "failed"
    with pytest.raises(ValueError, match="did not complete"):
        reader.add_api(bundle)
    assert (output / DATABASE).read_bytes() == before


def test_valueless_api_corroboration_cannot_promote_an_unresolved_saved_property(tmp_path):
    _, _, _, _, output, reader, case = build(tmp_path)
    before = (output / DATABASE).read_bytes()
    bundle = api_bundle(case, [{"api_node_id": 1, "name": "unknown", "status": "observed", "value_type": "unknown",
                               "getter": "get", "relation": "corroborates_saved_xml", "xml_property_ordinal": 3}])
    with pytest.raises(ValueError, match="incomplete saved value"):
        reader.add_api(bundle)
    assert (output / DATABASE).read_bytes() == before


def test_genuine_preview_keeps_metadata_without_invented_settings_or_xml_copy(tmp_path):
    originals = tmp_path / "originals"
    originals.mkdir()
    source = originals / "preview.mph"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("modelinfo.xml", '<modelInfo nodeType="preview" comsolVersion="6.3" title="Waveguide preview"/>')
        archive.writestr("thumbnail.png", b"Synthetic source thumbnail; never copy it")
    output = tmp_path / "compact-preview"
    result = build_compact_case(source, output, docs_root=tmp_path / "no-documents")
    reader = CompactCase(output)
    assert result["title"] == "Waveguide preview" and result["version"] == "6.3"
    assert result["coverage"]["source_container"]["status"] == "preview_only"
    assert result["counts"] == {"nodes": 0, "properties": 0, "selections": 0, "documents": 0, "passages": 0}
    assert result["xml_member"] is None and result["xml_sha256"] is None
    assert list(reader.iter_nodes()) == []
    evidence = reader.read_raw_evidence()
    assert evidence["status"] == "unavailable" and evidence["reason"] == "preview_model_no_settings"
    with pytest.raises(RawEvidenceUnavailable, match="no saved settings"):
        reader.read_raw_xml()
    assert reader.validate_evidence_routes()
    assert build_compact_case(source, output, docs_root=tmp_path / "no-documents")["cache_hit"] is True
    assert [path.name for path in output.iterdir()] == [DATABASE]


@pytest.mark.parametrize("node_type,xml", [("compact", None), ("preview", b"<Model")])
def test_missing_nonpreview_or_malformed_settings_still_fail_honestly(tmp_path, node_type, xml):
    originals = tmp_path / "originals"
    originals.mkdir()
    source = originals / "invalid.mph"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("modelinfo.xml", f'<modelInfo nodeType="{node_type}" title="Invalid settings"/>')
        if xml is not None:
            archive.writestr("dmodel.xml", xml)
    output = tmp_path / "compact-invalid"
    with pytest.raises((ValueError, ET.ParseError)):
        build_compact_case(source, output, docs_root=tmp_path / "no-documents")
    assert not output.exists()


def test_repeated_default_api_skips_getter_and_force_or_custom_extractor_executes(tmp_path, monkeypatch):
    from advisor import compact_api
    source, _, _, _, output, reader, case = build(tmp_path)
    calls = []

    def getter(path, temporary, fingerprint):
        assert path == source and fingerprint == case["source_sha256"]
        calls.append(path)
        return {"schema": "mph-api-state/v1", "read_contract": compact_api.API_READ_CONTRACT,
                "runtime_version": "6.3-fixture", "file_saved_version": "6.3", "read_errors": 0,
                "geometry_run": False, "mesh_run": False, "solver_run": False, "defaults_generated": False,
                "nodes": [{"path": "model/component(comp1)/mesh(mesh1)", "properties": [
                    {"name": "hmax", "value": 0.05, "value_type": "Double", "read_status": "observed", "getter": "getDouble"}]}]}, [], {}

    monkeypatch.setattr(compact_api, "_deep_state", getter)
    first = reader.add_api(temp_root=tmp_path / "temporary-getters")
    assert first["api"]["counts"]["observations"] == 1 and len(calls) == 1
    before = (output / DATABASE).read_bytes()

    def forbidden(*_args, **_kwargs):
        raise RuntimeError("Getter execution was requested")

    monkeypatch.setattr(compact_api, "_deep_state", forbidden)
    repeated = reader.add_api(temp_root=tmp_path / "temporary-getters")
    assert repeated["api_cache_hit"] and repeated["api_extraction_reused"]
    assert (output / DATABASE).read_bytes() == before and [path.name for path in output.iterdir()] == [DATABASE]
    with pytest.raises(RuntimeError, match="execution was requested"):
        reader.add_api(force=True, temp_root=tmp_path / "temporary-getters")
    with pytest.raises(RuntimeError, match="execution was requested"):
        reader.add_api(extractor=forbidden)
    assert (output / DATABASE).read_bytes() == before
    policy = compact_api.current_runtime_policy()
    monkeypatch.setattr(compact_api, "current_runtime_policy", lambda: dict(policy, fixture_policy_changed=True))
    with pytest.raises(RuntimeError, match="execution was requested"):
        reader.add_api(temp_root=tmp_path / "temporary-getters")
    assert (output / DATABASE).read_bytes() == before


def test_timestamp_only_api_changes_do_not_duplicate_facts_or_rows(tmp_path):
    _, _, _, _, output, reader, case = build(tmp_path)
    bundle = api_bundle(case, [{"api_node_id": 1, "name": "hmax", "value": 0.05, "status": "observed",
                               "value_type": "double", "getter": "getDouble", "relation": "corroborates_saved_xml"}])
    bundle["run"]["observed_at"] = "first timestamp"
    reader.add_api(bundle)
    before = (output / DATABASE).read_bytes()
    bundle["run"]["observed_at"] = "later timestamp"
    assert reader.add_api(bundle)["api_cache_hit"]
    assert (output / DATABASE).read_bytes() == before and reader.read_case()["api"]["counts"]["runs"] == 1


def test_source_override_reads_rebound_original_without_changing_immutable_database(tmp_path):
    source, _, _, _, output, reader, _ = build(tmp_path)
    before = (output / DATABASE).read_bytes()
    stamp = (output / DATABASE).stat().st_mtime_ns
    moved = tmp_path / "rebound-original.mph"
    source.rename(moved)
    rebound = CompactCase(output, source_override=moved)
    assert rebound.read_case()["source_path"] == str(moved)
    assert rebound.read_raw_xml() == XML and rebound.read_raw_evidence(3)["status"] == "available"
    assert reader.read_raw_evidence(3)["status"] == "unavailable"
    assert (output / DATABASE).read_bytes() == before and (output / DATABASE).stat().st_mtime_ns == stamp


def test_shared_library_mode_omits_per_case_fts_and_keeps_api_readers(tmp_path):
    source, _, parser, _ = fixture(tmp_path)
    output = tmp_path / "shared-index-case"
    result = build_compact_case(source, output, parser=parser, signature="fixture/v1", native_index=False)
    reader = CompactCase(output)
    assert result["native_index"] is False
    assert build_compact_case(source, output, parser=parser, signature="fixture/v1", native_index=False)["cache_hit"]
    reader.add_api(api_bundle(result, [{"api_node_id": 1, "name": "hmax", "value": 0.05, "status": "observed",
                                      "value_type": "double", "getter": "getDouble", "relation": "corroborates_saved_xml"}]))
    assert reader.get_property(3, "hmax", source="api")["value"] == 0.05
    with closing(sqlite3.connect(output / DATABASE)) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'native_fts%'").fetchall()
    with pytest.raises(ValueError, match="register the source library.*qmd_keyword"):
        reader.search("hmax")
    assert [path.name for path in output.iterdir()] == [DATABASE]
    original = (output / DATABASE).read_bytes()
    with pytest.raises(ValueError, match="native search is retired"):
        build_compact_case(source, output, parser=parser, signature="fixture/v1", native_index=True)
    assert (output / DATABASE).read_bytes() == original


def test_existing_artifact_with_historical_fts_reuses_nodes_without_rebuild_or_refresh(tmp_path):
    source, _, parser, calls, output, reader, case = build(tmp_path)
    with closing(sqlite3.connect(output / DATABASE)) as db, db:
        db.execute("CREATE VIRTUAL TABLE native_fts USING fts5(title,keywords,content='',tokenize='unicode61')")
        db.execute("INSERT INTO native_fts(rowid,title,keywords) VALUES(3,'Mesh','hmax')")
        nodes = db.execute("SELECT * FROM nodes ORDER BY id").fetchall()
    with closing(sqlite3.connect(output / DATABASE)) as db:
        postings = db.execute("SELECT * FROM native_fts_data ORDER BY id").fetchall()
    original = (output / DATABASE).read_bytes()
    reused = build_compact_case(source, output, parser=parser, signature="fixture/v1")
    assert reused["cache_hit"] and len(calls) == 1 and reused["native_index"] is False
    assert (output / DATABASE).read_bytes() == original
    with pytest.raises(ValueError, match="register the source library.*qmd_keyword"):
        reader.search("hmax")
    reader.add_api(api_bundle(case, [{"api_node_id": 1, "name": "hmax", "value": 0.05, "status": "observed",
                                     "value_type": "double", "getter": "getDouble", "relation": "corroborates_saved_xml"}]))
    with closing(sqlite3.connect(output / DATABASE)) as db:
        assert db.execute("SELECT * FROM nodes ORDER BY id").fetchall() == nodes
        assert db.execute("SELECT * FROM native_fts_data ORDER BY id").fetchall() == postings
    assert reader.get_property(3, "hmax")["value"] == 0.05
    assert reader.get_property(3, "hmax", source="api")["value"] == 0.05
