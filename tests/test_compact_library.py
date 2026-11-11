"""Production adapter keeps searchable references thin and pinned typed facts whole."""
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from unittest.mock import patch

import pytest

from advisor import compact_library as adapter
from advisor.compact_case import CompactCase, CompactCaseIntegrityError
from advisor.library_search import LibrarySearch, _representation
from advisor.library_store import LibraryStore
from test_compact_model import mph


def fixture(tmp_path, *, count=4, docs=False):
    source = mph(tmp_path / "sources" / "model.mph", '<Model tag="m"><MeshSequence tag="mesh1"><MeshFeature tag="size" op="Size">'
                 + ''.join(f'<propertyValue name="p:actual_property_{index}" value="{index}"/>' for index in range(count))
                 + '<propertyValue name="p:hmax" value="0.05"/><propertyValue name="p:custom" value="on"/>'
                 + '<selection T="32" selType="GEOMDIM"><named>boundary_faces</named></selection></MeshFeature></MeshSequence></Model>')
    root = tmp_path / "libraries"
    artifact = root / "artifacts" / "source-owner"
    docs_root = tmp_path / "docs"
    if docs:
        package = docs_root / "com.comsol.help.models.test.compact"
        package.mkdir(parents=True)
        (package / "model.html").write_text('<html><title>Documented model</title><h2>Introduction</h2><p>Heat transfer context.</p>'
                                              '<h2>Modeling Instructions</h2><h3>Mesh</h3><p>Choose the maximum element size.</p></html>', encoding="utf-8")
    result = adapter.build_case(source, artifact_dir=artifact, library_root=root, docs_root=docs_root)
    return source, root, artifact, result


def authority(result):
    return next(item for item in result["objects"] if item["type"] == "model_case")


def historical_fixture(tmp_path, *, truncated=False):
    """Retain an explicit pre-migration passage citation without new indexing."""
    parse = adapter.compact_model.parse_compact_model

    def legacy_parse(path, docs_root=None):
        result = parse(path, docs_root=docs_root)
        document = Path(docs_root) / "com.comsol.help.models.test.compact/model.html"
        result["documents"] = [{"id": 1, "source_uri": document.as_uri(), "source_sha256": adapter._hash(document),
                                "kind": "associated_tutorial_html"}]
        result["passages"] = [{"id": 1, "document_id": 1, "ordinal": 1, "kind": "method_step", "title": "Mesh",
                              "text": "Choose the maximum element size.", "locators": ["html:line=1"], "truncated": truncated}]
        return result

    with patch.object(adapter.compact_model, "parse_compact_model", side_effect=legacy_parse):
        source, root, artifact, result = fixture(tmp_path, docs=True)
    model = authority(result)
    document = tmp_path / "docs/com.comsol.help.models.test.compact/model.html"
    reader = CompactCase(root / model["data"]["compact_case"]["path"])
    passage = adapter._passage_objects(reader, {adapter._hash(document): document})[0]
    return source, root, artifact, result, passage


def test_fresh_model_database_and_index_objects_never_contain_tutorial_body(tmp_path):
    with patch("advisor.library_model._tutorial_objects", side_effect=AssertionError("No tutorial body copies")), \
         patch("advisor.case_metadata.tutorial_context_objects", side_effect=AssertionError("No tutorial body copies")):
        _, root, _, result = fixture(tmp_path, docs=True)
    model = authority(result)
    assert model["data"]["tutorial_refs"][0]["relation_status"] == "reference_not_verified_model_agreement"
    assert not any(row["type"] in {"case_context", "method_step"} for row in result["objects"])
    assert "Heat transfer context." not in json.dumps(result)
    assert "Choose the maximum element size." not in json.dumps(result)
    with closing(sqlite3.connect(root / model["data"]["compact_case"]["path"])) as db:
        assert db.execute("SELECT count(*) FROM passages").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM text_pool").fetchone()[0] == 0


def test_thin_records_have_one_database_and_all_property_names_with_bound_text(tmp_path):
    _, root, artifact, result = fixture(tmp_path, count=140)
    model = authority(result)
    assert model["local_id"] == "model-case" and model.get("coverage")
    assert "parser_contract" not in model["data"]
    children = [item for item in result["objects"] if item is not model]
    assert len(children) < 15
    assert all(not item.get("coverage") and "compact_case" not in item["data"] for item in children)
    assert all(not {"saved_properties", "saved_selections", "properties", "nodes", "parser_contract"} & item["data"].keys() for item in children)
    assert all(len(item["evidence"]) == 1 and item["evidence"][0]["asset_path"].endswith(".mph") for item in children)
    assert all(len(item["text"]) <= adapter.MAX_RETRIEVAL_CHARS for item in children)
    representation = '\n'.join(_representation(item)[0] for item in children)
    assert all(f"actual_property_{index}" in representation for index in range(140))
    assert "hmax" in representation and "boundary_faces" in representation
    assert len(list(artifact.rglob("compactcase.sqlite3"))) == 1
    assert all(path.name == "compactcase.sqlite3" for path in artifact.rglob("*") if path.is_file())
    database = root / model["data"]["compact_case"]["path"]
    with closing(sqlite3.connect(database)) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'native_fts%'").fetchall()


def test_empty_functional_nodes_keep_physics_type_and_material_label_searchable(tmp_path):
    source = mph(tmp_path / 'sources/metadata.mph', '<Model><Physics tag="ht" op="HeatTransferInSolids" name="ThermalPhysics"/>'
                 '<Material tag="mat1" name="CopperReferenceMaterial"/><StudyFeature tag="stat" op="Stationary"/></Model>')
    root = tmp_path / 'libraries'
    store = LibraryStore(root)
    library = store.register('Functional metadata', 'user-models',
                             [{'id': 'models', 'role': 'models', 'path': str(source.parent)}])
    library = store.set_enabled(library["id"], True)
    built = store.build(library['id'], model_format='compact', deep=False, modes=['native'])
    assert built['status'] in {'completed', 'partial'}, built
    search = LibrarySearch(root / 'search')
    for query in ('HeatTransferInSolids', 'CopperReferenceMaterial', 'Stationary'):
        assert search.search(query, mode='native'), query


def test_unchanged_cache_preserves_database_bytes_ids_and_avoids_parser(tmp_path):
    source, root, artifact, first = fixture(tmp_path)
    ref = authority(first)["data"]["compact_case"]
    database = root / ref["path"]
    before = database.read_bytes(), database.stat().st_mtime_ns
    with patch.object(adapter.compact_model, "parse_compact_model", side_effect=AssertionError("Unchanged cache must not parse again")):
        repeated = adapter.build_case(source, artifact_dir=artifact, library_root=root, docs_root=tmp_path / "docs")
    assert repeated["cache_hit"] and repeated["objects"] == first["objects"]
    assert database.read_bytes() == before[0] and database.stat().st_mtime_ns == before[1]


def test_document_change_creates_new_version_while_old_citation_data_stays_readable(tmp_path):
    source, root, artifact, first, passage = historical_fixture(tmp_path)
    old = authority(first)
    old_database = root / old["data"]["compact_case"]["path"]
    original = old_database.read_bytes()
    doc = tmp_path / "docs/com.comsol.help.models.test.compact/model.html"
    doc.write_text(doc.read_text(encoding="utf-8").replace("Choose the maximum element size.", "Choose a revised documented size."), encoding="utf-8")
    second = adapter.build_case(source, artifact_dir=artifact, library_root=root, docs_root=tmp_path / "docs")
    assert authority(second)["data"]["compact_case"]["path"] != old["data"]["compact_case"]["path"]
    assert authority(second)["data"]["tutorial_refs"][0]["source_sha256"] != old["data"]["tutorial_refs"][0]["source_sha256"]
    assert not any(item["type"] == "method_step" for item in second["objects"])
    assert old_database.read_bytes() == original
    assert adapter.hydrate_source(old, passage, library_root=root)["text"] == "Choose the maximum element size."
    with pytest.raises(CompactCaseIntegrityError, match="tutorial changed"):
        adapter.read_evidence(old, passage, library_root=root)


def test_hydration_rebind_configuration_and_evidence_keep_canonical_values(tmp_path):
    source, root, _, result = fixture(tmp_path)
    model = authority(result)
    child = next(item for item in result["objects"] if item["type"] == "saved_configuration" and "hmax" in item["text"])
    frozen = copy.deepcopy(child)
    hydrated = adapter.hydrate_source(model, child, library_root=root)
    assert child == frozen
    assert next(row for row in hydrated["data"]["saved_properties"] if row["name"] == "hmax")["value"] == .05
    config = adapter.configuration(model, library_root=root, category="mesh", node_path="size", source="xml")
    assert config["returned"] == 1 and config["physical_validation"] == "not_run"
    moved = tmp_path / "moved/model.mph"
    moved.parent.mkdir()
    shutil.move(source, moved)
    before = (root / model["data"]["compact_case"]["path"]).read_bytes()
    assert adapter.read_evidence(model, child, library_root=root, source_override=moved)["source_hash_verified"]
    assert (root / model["data"]["compact_case"]["path"]).read_bytes() == before
    moved.unlink()
    assert adapter.hydrate_source(model, child, library_root=root)["data"]["saved_properties"]
    assert adapter.read_evidence(model, child, library_root=root)["status"] == "unavailable"


def test_compact_database_hash_and_owned_relative_path_are_enforced(tmp_path):
    _, root, _, result = fixture(tmp_path)
    model = authority(result)
    child = result["objects"][1]
    bad = copy.deepcopy(model)
    bad["data"]["compact_case"]["path"] = "../outside/compactcase.sqlite3"
    with pytest.raises(CompactCaseIntegrityError, match="escapes"):
        adapter.hydrate_source(bad, child, library_root=root)
    database = root / model["data"]["compact_case"]["path"]
    database.write_bytes(database.read_bytes() + b"tampered")
    with pytest.raises(CompactCaseIntegrityError, match="hash"):
        adapter.hydrate_source(model, child, library_root=root)


def test_source_version_change_never_overwrites_an_existing_authority(tmp_path, monkeypatch):
    source, root, artifact, first = fixture(tmp_path)
    old = authority(first)
    database = root / old["data"]["compact_case"]["path"]
    original = database.read_bytes()
    signature = adapter.compact_model.parser_signature()
    monkeypatch.setattr(adapter.compact_model, "parser_signature", lambda: "new-policy-" + signature)
    second = adapter.build_case(source, artifact_dir=artifact, library_root=root, docs_root=tmp_path / "docs")
    assert authority(second)["data"]["compact_case"]["path"] != old["data"]["compact_case"]["path"]
    assert database.read_bytes() == original


def test_value_hints_keep_expression_and_filename_recall_without_typed_duplicates(tmp_path):
    source = mph(tmp_path / "model.mph", '''<Model><FunctionFeature tag="an" op="Analytic">
      <propertyValue name="p:expr" value="sin(omega*t)"/><propertyValue name="p:filename" value="head_torso_geometry.stl"/>
      </FunctionFeature></Model>''')
    root = tmp_path / "libraries"
    result = adapter.build_case(source, artifact_dir=root / "artifacts/one", library_root=root, docs_root=tmp_path / "missing")
    children = [row for row in result["objects"] if row["type"] == "saved_configuration"]
    text = '\n'.join(_representation(row)[0] for row in children)
    assert "sin(omega*t)" in text and "head_torso_geometry.stl" in text
    assert all("value" not in row["data"] and "saved_properties" not in row["data"] for row in children)


def test_document_rebind_uses_current_hash_matched_path_without_database_rewrite(tmp_path):
    source, root, artifact, first = fixture(tmp_path, docs=True)
    model = authority(first)
    database = root / model["data"]["compact_case"]["path"]
    original = database.read_bytes()
    moved = tmp_path / "moved-docs"
    shutil.move(tmp_path / "docs", moved)
    with patch.object(adapter.compact_model, "parse_compact_model", side_effect=AssertionError("Relocation must reuse typed rows")):
        repeated = adapter.build_case(source, artifact_dir=artifact, library_root=root, docs_root=moved)
    assert repeated["cache_hit"] and authority(repeated)["data"]["compact_case"] == model["data"]["compact_case"]
    reference = authority(repeated)["data"]["tutorial_refs"][0]
    document = adapter._file_uri(reference["source_uri"])
    assert document.is_relative_to(moved)
    assert adapter._hash(document) == reference["source_sha256"]
    assert database.read_bytes() == original


def test_hydrated_passage_keeps_source_truncation_flag(tmp_path):
    _, root, _, result, passage = historical_fixture(tmp_path, truncated=True)
    model = authority(result)
    hydrated = adapter.hydrate_source(model, passage, library_root=root)
    assert hydrated["text_truncated"] and hydrated["read_scope"] == "bounded_compact_passage"


def test_actual_head_torso_registry_and_native_qmd_indexes_stay_under_two_mb(tmp_path):
    source = Path("D:/comsol/COMSOL63/Multiphysics/applications/Acoustics_Module/Tutorials,_Pressure_Acoustics/head_torso_hrtf.mph")
    docs = Path("D:/comsol/COMSOL63/Multiphysics/doc/help/wtpwebapps/ROOT/doc")
    if not source.is_file() or not docs.is_dir():
        pytest.skip("Optional official model/docs unavailable")
    probe = LibrarySearch(tmp_path / "probe", cache_dir=tmp_path / "cache")
    if not probe.node or not (probe.qmd_dir / "dist/index.js").is_file():
        pytest.skip("Optional actual local QMD dependencies unavailable")
    store = LibraryStore(tmp_path / "libraries")
    library = store.register("Actual head torso", "official-models", [{"id": "models", "role": "models", "path": str(source.parent)},
                                                                  {"id": "docs", "role": "docs", "path": str(docs)}])
    library = store.set_enabled(library["id"], True)
    before = source.stat().st_size, source.stat().st_mtime_ns
    built = store.build(library["id"], paths=[source], model_format="compact", deep=False, modes=["native", "qmd_keyword"])
    assert built["status"] in {"completed", "partial"}, built
    search = LibrarySearch(store.root / "search", cache_dir=tmp_path / "cache")
    for mode in ("native", "qmd_keyword"):
        hits = search.search("hmax", mode=mode, feature_branch="mesh")
        assert len(hits) == 2, (mode, hits)
        assert search.search("hauto", mode=mode, feature_branch="mesh") == []
    size = sum(path.stat().st_size for path in store.root.rglob("*") if path.is_file())
    # The shared registry and two external search schemas retain their own
    # metadata/postings. Count all of them, rather than only the compact DB.
    assert size < 3_500_000, size
    assert before == (source.stat().st_size, source.stat().st_mtime_ns)
    assert not list(store.root.rglob("*.mph")) and not list(store.root.rglob("saved-state.json"))
