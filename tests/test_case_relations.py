"""Case links are revision-pinned references, not assumed executable recipes."""
import copy
from pathlib import Path
from unittest.mock import patch

import pytest

from advisor.case_metadata import link_case_objects, normalize_feature_branch, object_feature_branch, tutorial_context_objects
from advisor.library_store import LibraryStore


def extraction(path, geometry_code):
    evidence = [{"asset_path": str(path), "locator": "fixture", "method": "fixture"}]
    def item(local, type_, title, data=None, relations=None):
        return {"local_id": local, "type": type_, "title": title, "text": geometry_code if local == "geom" else title,
                "data": data or {}, "relations": relations or [], "kind": "code" if type_ == "implementation_snippet" else "case", "evidence": evidence}
    return {"objects": [item("model-case", "model_case", "Case"),
                        item("mesh-step", "method_step", "Mesh 1", {"section": "Mesh 1"}),
                        item("geom", "implementation_snippet", "Geometry", {"scope_hint": "geom"}),
                        item("mesh", "implementation_snippet", "Mesh", {"feature_branch": "mesh", "code": "full mesh code"},
                             [{"type": "requires", "target_local_id": "geom", "status": "source_reference"}])],
            "coverage": {}, "warnings": [], "assets": []}


def test_branch_aliases_and_tutorial_links_do_not_claim_verified_alignment():
    assert normalize_feature_branch("geom") == "geometry"
    assert normalize_feature_branch("sol") == "solver"
    assert normalize_feature_branch("all") is None
    with pytest.raises(ValueError):
        normalize_feature_branch("guess-anything")
    original = extraction(Path("fixture.mph"), "geometry")
    before = copy.deepcopy(original)
    result = link_case_objects(original["objects"])
    assert original == before
    mesh = next(item for item in result if item["local_id"] == "mesh")
    assert object_feature_branch(mesh) == "mesh"
    assert {relation["type"] for relation in mesh["relations"]} == {"requires", "same_case", "documented_by"}
    documented = next(relation for relation in mesh["relations"] if relation["type"] == "documented_by")
    assert documented["status"] == "candidate_same_branch" and documented["alignment"] == "not_verified"
    assert object_feature_branch({"type": "model_case", "title": "Some Mesh"}) is None


def test_case_dependency_reads_stay_with_the_original_revision_after_rebuild(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    path = sources / "sample.mph"
    path.write_bytes(b"synthetic source: extractor is injected, no COMSOL")
    store = LibraryStore(tmp_path / "workspace/libraries")
    library = store.register("fixture", "user-models", [{"id": "main", "path": str(sources)}], library_id="fixture")
    library = store.set_enabled(library["id"], True)
    current = ["original geometry"]
    with patch("advisor.library_model.extract_case", side_effect=lambda *args, **kwargs: extraction(path, current[0])), patch.object(store, "_cached_extraction", return_value=None):
        assert store.build(library["id"], deep=False, enrich=False, modes=[])["status"] == "completed"
        old_mesh = next(item for item in store.objects() if item["local_id"] == "mesh")
        old_links = store.related_sources(old_mesh["id"])
        old_dependency = next(item["id"] for item in old_links if item["relation"] == "requires")
        assert store.get(old_dependency)["text"] == "original geometry"
        current[0] = "updated geometry"
        assert store.build(library["id"], deep=False, enrich=False, modes=[])["status"] == "completed"
    new_mesh = next(item for item in store.objects() if item["local_id"] == "mesh")
    new_dependency = next(item["id"] for item in store.related_sources(new_mesh["id"]) if item["relation"] == "requires")
    assert new_mesh["id"] != old_mesh["id"]
    assert new_dependency != old_dependency
    assert next(item["id"] for item in store.related_sources(old_mesh["id"]) if item["relation"] == "requires") == old_dependency
    assert all("text" not in item and "path" not in item and "code" not in item for item in old_links)
    assert path.read_bytes() == b"synthetic source: extractor is injected, no COMSOL"


def test_legacy_unpinned_relation_is_not_redirected_to_the_active_version(tmp_path):
    store = LibraryStore(tmp_path)
    store.register('Fixture', 'user-models', [{'id': 'models', 'path': str(tmp_path)}], library_id='fixture')
    store.set_enabled('fixture', True)
    source = {"id": "old", "object_id": "logical", "library_id": "fixture", "title": "Old code", "data": {},
              "relations": [{"type": "requires", "target_local_id": "same-local-id"}]}
    with patch.object(store, "get", return_value=source):
        links = store.related_sources("old")
    assert links == [{"relation": "requires", "status": "unresolved", "title": "same-local-id", "reason": "Referenced object is not pinned in this case revision"}]


def test_context_preserves_documented_purpose_and_definition_without_inventing_image_equations(tmp_path):
    blocks = [{"tag": "h2", "text": "Introduction", "line": 1},
              {"tag": "p", "text": "A documented heat transfer example.", "line": 2, "anchor": "intro"},
              {"tag": "h2", "text": "Model Definition", "line": 3},
              {"tag": "p", "text": "An equation is shown in the figure.", "line": 4},
              {"tag": "h2", "text": "Modeling Instructions", "line": 5},
              {"tag": "p", "text": "Click Compute.", "line": 6}]
    contexts = tutorial_context_objects(tmp_path / "tutorial.html", blocks)
    assert [item["data"]["section"] for item in contexts] == ["introduction", "model_definition"]
    assert contexts[0]["evidence"][0]["locator"] == "html:#intro"
    assert "Click Compute" not in "".join(item["text"] for item in contexts)
    assert all(item["data"]["model_agreement"] == "not_verified" for item in contexts)
    source = extraction(tmp_path / "source.mph", "geometry")["objects"] + contexts
    linked = link_case_objects(source)
    model = next(item for item in linked if item["type"] == "model_case")
    assert len([relation for relation in model["relations"] if relation["type"] == "context"]) == 2
