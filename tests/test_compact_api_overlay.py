"""Existing saved compact facts stay immutable through API enrichment."""
from contextlib import closing
import copy
import gzip
import json
from pathlib import Path
import shutil
import sqlite3
from unittest.mock import patch

import pytest

from advisor import compact_case, compact_library, compact_api
from advisor.compact_api_overlay import OVERLAY_FILE, OverlayCompactCase, export_api_overlay, stage_api_overlay
from advisor.compact_case import CompactCase, CompactCaseIntegrityError
from test_compact_case import fixture as saved_fixture, api_bundle


def fixture(tmp_path):
    source, parsed, parser, calls = saved_fixture(tmp_path)
    root = tmp_path / "libraries"
    output = root / "artifacts/base"
    compact_case.build_compact_case(source, output, parser=parser, signature="fixture/v1", native_index=False)
    reader = CompactCase(output)
    authority = {"id": "original-case-citation", "case_group": "original-saved-group", "type": "model_case", "title": "Saved case",
                 "data": {"compact_case": {"format": compact_case.FORMAT, "path": reader.db_path.relative_to(root).as_posix(),
                                             "sha256": compact_library._hash(reader.db_path)}}}
    bundle = api_bundle(reader.read_case(), [
        {"api_node_id": 1, "name": "hmax", "status": "observed", "value_type": "Double", "getter": "getDouble",
         "relation": "corroborates_saved_xml", "xml_property_ordinal": 1},
        {"api_node_id": 1, "name": "hauto", "value": 9, "status": "observed", "value_type": "Int", "getter": "getInt",
         "relation": "differs_from_saved_xml", "xml_property_ordinal": 2},
        {"api_node_id": 1, "name": "api_only", "value": [1, 2, 3], "status": "observed", "value_type": "IntArray", "getter": "getIntArray",
         "relation": "missing_saved_property"},
        {"api_node_id": 1, "name": "unresolved", "status": "unresolved", "value_type": "Selection", "getter": "selection",
         "relation": "fills_saved_gap", "issues": ["api_getter_failed"]},
    ])
    bundle["run"].update(status="partial", selector_signature="fixture-selector", runtime_policy={"fixture": True})
    return source, root, reader, authority, bundle


def test_overlay_contains_only_new_api_and_reads_saved_comparisons_without_reparsing(tmp_path):
    source, root, base, authority, bundle = fixture(tmp_path)
    original_db, original_source = base.db_path.read_bytes(), source.read_bytes()
    saved_node = base.read_node(3)
    output = tmp_path / OVERLAY_FILE
    with patch("advisor.compact_model.parse_compact_model", side_effect=AssertionError("No XML parsing")):
        report = stage_api_overlay(base.db_path, output, bundle=bundle)
    assert base.db_path.read_bytes() == original_db and source.read_bytes() == original_source
    assert report["saved_rows_unchanged"] and report["new_observations"] == 4
    assert output.stat().st_size < 8192
    assert not list(tmp_path.glob(".api-work-*"))
    payload = json.loads(gzip.decompress(output.read_bytes()))
    assert not {"nodes", "properties", "selections", "raw_xml", "documents"} & payload.keys()
    corroboration = next(row for row in payload["observations"] if row["name"] == "hmax")
    assert "value" not in corroboration and corroboration["base_value_property_id"]
    reader = OverlayCompactCase(base, output)
    assert reader.read_node(3)["properties"] == saved_node["properties"]
    assert reader.get_property(3, "hmax", source="api")["value"] == .05
    assert reader.get_property(3, "hauto", source="api")["value"] == 9
    assert reader.get_property(3, "hauto", source="xml")["value"] == 3
    assert reader.get_property(3, "unresolved", source="api")["value_complete"] is False
    assert reader.read_case()["api"]["counts"] == {"runs": 1, "nodes": 1, "observations": 4}
    assert base.read_case()["api"]["counts"]["observations"] == 0
    source.unlink()
    assert reader.get_property(3, "api_only", source="api")["value"] == [1, 2, 3]
    assert reader.read_api_evidence(1)["xml_evidence"] is False


def test_library_enrichment_emits_only_api_delta_and_preserves_old_reader(tmp_path):
    source, root, base, authority, bundle = fixture(tmp_path)
    frozen, before = copy.deepcopy(authority), base.db_path.read_bytes()
    result = compact_library.enrich_case(authority, library_root=root, artifact_dir=root / "artifacts/enriched", bundle=bundle)
    assert result["api_status"] == "partial"
    model, *children = result["objects"]
    assert children and all(item["local_id"].startswith("deep-api-") for item in children)
    assert all(item["data"]["compact_ref"]["api_only"] for item in children)
    assert not any(item["type"] == "saved_configuration" for item in result["objects"])
    assert model["data"]["compact_case"] == authority["data"]["compact_case"]
    assert model["data"]["base_case_id"] == authority["id"] and model["data"]["base_case_group"] == authority["case_group"]
    hydrated = compact_library.hydrate_source(model, children[0], library_root=root)
    assert hydrated["data"]["saved_properties"] == [] and hydrated["data"]["api_observations"]
    assert compact_library.configuration(model, library_root=root, source="api")["nodes"]
    assert compact_library.configuration(model, library_root=root, source="xml")["nodes"]
    with pytest.raises(ValueError, match="no API"):
        compact_library.configuration(authority, library_root=root, source="api")
    evidence = compact_library.read_evidence(model, children[0], library_root=root)
    assert evidence["evidence_kind"] == "api_getter_observation" and not evidence["xml_evidence"]
    assert authority == frozen and base.db_path.read_bytes() == before
    assert not list((root / "artifacts/enriched").rglob(compact_case.DATABASE))


def test_completed_default_overlay_cache_skips_getters_and_force_retries(tmp_path, monkeypatch):
    source, root, base, authority, bundle = fixture(tmp_path)
    calls = []
    def extract(reader, **kwargs):
        calls.append(reader.read_case()["source_sha256"])
        return copy.deepcopy(bundle)
    monkeypatch.setattr(compact_api, "current_runtime_policy", lambda: {"fixture": True})
    monkeypatch.setattr(compact_api, "parser_signature", lambda: "fixture-selector")
    monkeypatch.setattr(compact_api, "extract_compact_api", extract)
    artifact = root / "artifacts/cached"
    first = compact_library.enrich_case(authority, library_root=root, artifact_dir=artifact)
    def forbidden(*args, **kwargs):
        raise RuntimeError("forced getter executed")
    monkeypatch.setattr(compact_api, "extract_compact_api", forbidden)
    second = compact_library.enrich_case(authority, library_root=root, artifact_dir=artifact)
    assert second["cache_hit"] and second["objects"] == first["objects"] and len(calls) == 1
    with pytest.raises(RuntimeError, match="forced getter"):
        compact_library.enrich_case(authority, library_root=root, artifact_dir=artifact, force=True)
    assert not list(artifact.glob(".api-*"))


def test_export_completed_pilot_needs_no_java_and_refuses_changed_saved_rows(tmp_path):
    source, root, base, authority, bundle = fixture(tmp_path)
    augmented = tmp_path / "pilot"
    augmented.mkdir()
    shutil.copyfile(base.db_path, augmented / compact_case.DATABASE)
    compact_case.add_compact_api(augmented, bundle)
    source.unlink()
    with patch("advisor.compact_api.extract_compact_api", side_effect=AssertionError("Pilot already read")):
        report = export_api_overlay(base.db_path, augmented, tmp_path / OVERLAY_FILE)
    assert report["new_observations"] == 4
    assert OverlayCompactCase(base, tmp_path / OVERLAY_FILE).get_property(3, "hauto", source="api")["value"] == 9
    with closing(sqlite3.connect(augmented / compact_case.DATABASE)) as db, db:
        db.execute("UPDATE nodes SET parent_id=NULL WHERE id=3")
    target = tmp_path / "broken" / OVERLAY_FILE
    with pytest.raises(CompactCaseIntegrityError, match="changed"):
        export_api_overlay(base.db_path, augmented, target)
    assert not target.exists()


def test_wrong_or_tampered_overlay_is_refused_and_failed_api_preserves_base(tmp_path):
    source, root, base, authority, bundle = fixture(tmp_path)
    before = base.db_path.read_bytes()
    def failed(reader):
        raise RuntimeError("getter failed")
    target = tmp_path / "failed" / OVERLAY_FILE
    with pytest.raises(RuntimeError, match="getter failed"):
        stage_api_overlay(base.db_path, target, extractor=failed)
    assert not target.exists() and base.db_path.read_bytes() == before
    result = compact_library.enrich_case(authority, library_root=root, artifact_dir=root / "artifacts/version", bundle=bundle)
    model = result["objects"][0]
    overlay = root / model["data"]["compact_api_overlay"]["path"]
    overlay.write_bytes(overlay.read_bytes() + b"corrupt")
    with pytest.raises(CompactCaseIntegrityError, match="overlay"):
        compact_library.configuration(model, library_root=root, source="api")


def test_public_overlay_uses_new_api_but_old_saved_ids_and_tutorial_links_remain_pinned(tmp_path):
    from test_deep_enrichment import fixture as catalog_fixture, builder
    from advisor import deep_enrichment, tutorial_catalog
    from test_tutorial_migration import _put, _record, _source
    store, library, authorities = catalog_fixture(tmp_path, count=1)
    old = authorities[0]
    saved = [item for item in store.objects() if item["type"] == "saved_configuration"]
    _, record = deep_enrichment._source_record(store, old)
    result = builder(old, library_root=store.root, source_override=Path(record["path"]))
    published = deep_enrichment.publish_case_overlay(store, old, result, batch_id="test", job_id="test")
    assert store.library(library["id"])["enabled"] is False
    with pytest.raises(PermissionError):
        store.configuration(published["authority_id"], source="api")
    store.set_enabled(library["id"], True)
    current = store.get(published["authority_id"])
    api = next(item for item in store.objects() if item["local_id"].startswith("deep-api-"))
    assert store.configuration(current["id"], source="api")["nodes"]
    with pytest.raises(ValueError, match="no API"):
        store.configuration(old["id"], source="api")
    assert all(store.get(item["id"]) == item for item in saved)
    assert all(not store.read(item["id"])["data"].get("api_observations") for item in saved)
    assert any(item["id"] == old["id"] and item["relation"] == "saved_xml_base" for item in store.related_sources(api["id"]))
    assert any(item["id"] in {value["id"] for value in saved} and item["relation"] == "saved_xml_configuration"
               for item in store.related_sources(current["id"]))
    doc = tmp_path / "manuals/tutorial.html"
    doc.parent.mkdir()
    doc.write_text("Canonical tutorial", encoding="utf-8")
    manual = store.register("Tutorial", "official-manuals", [{"id": "docs", "path": str(doc.parent)}])
    documented = store._prepare(manual, _record(doc, doc.parent, "docs"), _source("topic", "manual_topic", "Tutorial", "Canonical tutorial", doc))
    _put(store, [documented])
    digest = compact_library._hash(doc)
    with closing(store.connect()) as db, db:
        tutorial_catalog.ensure_schema(db)
        db.execute("INSERT INTO tutorial_model_documents VALUES(?,?,?,?)", (old["id"], old["library_id"], digest, "models.test.fixture"))
        db.execute("INSERT INTO tutorial_document_sources VALUES(?,?,?,?,?,?)", (digest, "models.test.fixture", manual["id"], documented["id"], "document", "mesh"))
    assert not any(item["id"] == documented["id"] for item in store.related_sources(api["id"]))
    store.set_enabled(manual["id"], True)
    assert any(item["id"] == documented["id"] for item in store.related_sources(api["id"]))
