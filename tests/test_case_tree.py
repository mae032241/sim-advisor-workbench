"""Synthetic retained-node coverage, reference resolution and lifecycle gates."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.case_tree import CaseTreeRepository, build_case_trees, get_case_navigation
from advisor.compact_case import CompactCaseIntegrityError
from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_store import LibraryStore
from test_compact_model import mph


def make_case(tmp_path, *, filename="case.mph", count=27, extra="", label="Wall"):
    features = "".join(f'<PhysicsFeature tag="wall{i}" op="Wall" name="{label}{i}"><expressions name="expr" expr="alpha*sin(t)+mystery+2[W/(m*K)]"/></PhysicsFeature>'
                       for i in range(count))
    xml = ('<Model><ModelParam tag="par1"><propertyValue name="p:alpha" value="2"/></ModelParam>'
           '<ModelNode tag="comp1" name="First component"><GeomSequence tag="geom1"/>'
           '<SelectionFeature tag="faces" op="Explicit"/>'
           '<Physics tag="heat" op="HeatTransfer" name="Heat interface">' + features +
           '<PhysicsFeature tag="selected" op="Temperature" geom="geom1"><selection selType="GEOMDIM"><named>/selection/faces</named></selection>'
           '<propertyValue name="p:label" value="not_a_variable"/><propertyValue name="p:expr" value="alpha*T"/></PhysicsFeature></Physics>'
           '<Physics tag="flow" op="LaminarFlow" name="Flow interface"><PhysicsFeature tag="wallflow" op="Wall" name="Wall"/></Physics>'
           '</ModelNode>' + extra + '</Model>')
    return mph(tmp_path / "sources" / filename, xml)


@pytest.fixture
def fixture(tmp_path):
    source = make_case(tmp_path)
    store = LibraryStore(tmp_path / "catalog")
    store.register("Synthetic cases", "user-models", [{"id": "models", "role": "models", "path": str(source.parent)}], library_id="cases")
    store.set_enabled("cases", True)
    job = store.build("cases", paths=[source], model_format="compact", deep=False, modes=[])
    assert job["status"] in {"completed", "partial"}, job
    case = next(obj for obj in store.objects() if obj["type"] == "model_case")
    return store, case, source


def all_nodes(repo, identity):
    result, offset = [], 0
    while True:
        page = repo.list_nodes(identity, offset=offset, limit=7)
        result.extend(page["items"])
        if not page["has_more"]:
            return result
        offset = page["next_offset"]


def all_edges(store, identity, **kwargs):
    result, offset = [], 0
    while True:
        page = get_case_navigation(store, identity, view="dependencies", offset=offset, limit=8, **kwargs)
        result.extend(page["items"])
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= 7000
        if not page["has_more"]:
            return result
        assert page["next_offset"] > offset
        offset = page["next_offset"]


def test_full_parent_tree_pagination_nearest_physics_and_exact_read_entries(fixture):
    store, case, _ = fixture
    from advisor.compact_library import _reader
    authority, original = store._compact_context(case)
    expected = _reader(authority, store.root, original).read_case()["counts"]["nodes"]
    built = build_case_trees(store, [case["id"]])
    assert built["node_count"] == expected and expected > 30
    repo = CaseTreeRepository(store)
    nodes = all_nodes(repo, case["id"])
    assert len(nodes) == expected
    counts = {node["node_path"]: sum(child["parent_path"] == node["node_path"] for child in nodes) for node in nodes}
    assert any(count == 0 for count in counts.values()) and any(count > 0 for count in counts.values())
    assert all(node["child_count"] == counts[node["node_path"]] and node["has_children"] == bool(counts[node["node_path"]]) for node in nodes)
    seen, parents = set(), [None]
    while parents:
        parent = parents.pop()
        offset = 0
        while True:
            page = get_case_navigation(store, case["id"], node_path=parent, offset=offset, limit=8)
            assert not page["is_read_evidence"]
            for node in page["items"]:
                assert node["child_count"] == counts[node["node_path"]]
                assert node["has_children"] == bool(counts[node["node_path"]])
                assert node["node_path"] not in seen
                seen.add(node["node_path"])
                parents.append(node["node_path"])
            if not page["has_more"]:
                break
            offset = page["next_offset"]
    assert seen == {node["node_path"] for node in nodes}
    heat_wall = next(node for node in nodes if node["tag"] == "wall0")
    flow_wall = next(node for node in nodes if node["tag"] == "wallflow")
    assert heat_wall["nearest_physics"]["type"] == "HeatTransfer"
    assert flow_wall["nearest_physics"]["type"] == "LaminarFlow"
    assert heat_wall["ancestor_paths"][-1] == heat_wall["parent_path"]
    assert heat_wall["source_refs"] and heat_wall["source_refs"][0]["source_mode"] == "saved"
    assert heat_wall["read_requests"] == [{"case_id": case["id"], "node_path": heat_wall["node_path"], "source": "saved"}]
    assert repo.node(case["id"], heat_wall["node_path"]) == heat_wall


def test_observable_dependencies_do_not_turn_units_labels_or_builtin_fields_into_missing_symbols(fixture):
    store, case, _ = fixture
    build_case_trees(store, [case["id"]])
    edges = all_edges(store, case["id"])
    alpha = [edge for edge in edges if edge["token"] == "alpha"]
    assert alpha and all(edge["status"] == "confirmed" for edge in alpha)
    assert all(edge["property_locator"].startswith("saved:compact:node/") for edge in alpha)
    assert {edge["token"] for edge in edges if edge["status"] == "unresolved"} == {"mystery"}
    assert any(edge["kind"] == "named_selection" and edge["status"] == "confirmed" for edge in edges)
    assert any(edge["kind"] == "geometry_ref" and edge["status"] == "confirmed" for edge in edges)
    assert all(edge["physical_causality"] == "not_inferred" and edge["effective_activation"] == "not_evaluated" for edge in edges)
    definition = alpha[0]["target_node_path"]
    incoming = all_edges(store, case["id"], node_path=definition, direction="incoming")
    assert incoming and all(edge["target_node_path"] == definition for edge in incoming)
    assert all(edge["from_node_path"] == definition for edge in all_edges(store, case["id"], node_path=definition, direction="outgoing"))


def test_ambiguous_definitions_are_not_resolved_by_phase_order(fixture):
    store, _, source = fixture
    make_case(source.parent.parent, extra='<ModelParam tag="par2"><propertyValue name="p:alpha" value="3"/></ModelParam>')
    store.build("cases", paths=[source], model_format="compact", deep=False, modes=[])
    case = next(obj for obj in store.objects() if obj["type"] == "model_case")
    build_case_trees(store, [case["id"]])
    edges = [edge for edge in all_edges(store, case["id"]) if edge["token"] == "alpha"]
    assert edges and all(edge["status"] == "ambiguous" and edge["candidate_count"] == 2 for edge in edges)
    assert len({edge["target_node_path"] for edge in edges}) == 2


def test_values_and_tutorial_text_are_never_copied_to_tree_index(fixture):
    store, case, _ = fixture
    build_case_trees(store, [case["id"]])
    raw = CaseTreeRepository(store).index_path.read_bytes()
    assert b"alpha*sin(t)+mystery" not in raw
    assert b"not_a_variable" not in raw
    with closing(CaseTreeRepository(store)._connect()) as db:
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"metadata", "cases", "nodes", "dependencies"}


def test_incremental_selected_case_build_preserves_other_case(fixture):
    store, first, source = fixture
    build_case_trees(store, [first["id"]])
    generation = CaseTreeRepository(store).case(first["id"])["generation"]
    second_source = make_case(source.parent.parent, filename="second.mph", count=1)
    store.build("cases", paths=[second_source], model_format="compact", deep=False, modes=[])
    second = next(obj for obj in store.objects() if obj["type"] == "model_case" and obj["id"] != first["id"])
    build_case_trees(store, [second["id"]])
    assert CaseTreeRepository(store).case(first["id"])["generation"] == generation
    assert CaseTreeRepository(store).case(second["id"])["state"] == "ready"


def test_cancellation_and_failure_preserve_last_published_index(fixture, tmp_path):
    store, case, _ = fixture
    build_case_trees(store, [case["id"]])
    repo = CaseTreeRepository(store)
    old = repo.index_path.read_bytes()
    control = BuildControl(tmp_path / "cancel")
    def progress(_update):
        control.cancel_path.touch()
    with build_control(control), pytest.raises(BuildCancelled):
        build_case_trees(store, [case["id"]], progress=progress)
    assert repo.index_path.read_bytes() == old
    with pytest.raises(ValueError):
        build_case_trees(store, [case["id"], "missing-case"])
    assert repo.index_path.read_bytes() == old
    assert not list(repo.directory.glob(".building-*")) and not (repo.directory / ".build.lock").exists()


def test_live_disabled_deleted_and_heldout_gates(fixture, monkeypatch):
    store, case, _ = fixture
    build_case_trees(store, [case["id"]])
    store.set_enabled("cases", False)
    with pytest.raises(PermissionError):
        CaseTreeRepository(store).case(case["id"])
    store.set_enabled("cases", True)
    monkeypatch.setattr(store, "evaluation_policy", lambda: HoldoutPolicy(case_groups=frozenset({case["case_group"]})))
    with pytest.raises(PermissionError):
        get_case_navigation(store, case["id"])
    with pytest.raises(PermissionError):
        build_case_trees(store, [case["id"]])
    monkeypatch.setattr(store, "evaluation_policy", lambda: HoldoutPolicy())
    with closing(store.connect()) as db, db:
        db.execute("DELETE FROM active WHERE library_id='cases'")
    with pytest.raises(ValueError, match="no longer current"):
        CaseTreeRepository(store).case(case["id"])


def test_old_case_id_never_reads_new_revision(fixture):
    store, old, source = fixture
    build_case_trees(store, [old["id"]])
    make_case(source.parent.parent, count=2)
    store.build("cases", paths=[source], model_format="compact", deep=False, modes=[])
    new = next(obj for obj in store.objects() if obj["type"] == "model_case")
    assert new["id"] != old["id"]
    with pytest.raises(ValueError, match="no longer current"):
        get_case_navigation(store, old["id"])
    assert get_case_navigation(store, new["id"])["state"] == "not_prepared"


def test_changed_compact_hash_invalidates_cached_navigation(fixture):
    store, case, _ = fixture
    build_case_trees(store, [case["id"]])
    repo = CaseTreeRepository(store)
    repo.case(case["id"])
    archive = store.root / case["data"]["compact_case"]["path"]
    with archive.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(CompactCaseIntegrityError):
        repo.case(case["id"])


def test_queries_reuse_verified_version_without_reopening_compact_or_iterating_nodes(fixture, monkeypatch):
    store, case, _ = fixture
    build_case_trees(store, [case["id"]])
    monkeypatch.setattr("advisor.case_tree._reader", lambda *_a, **_kw: pytest.fail("unchanged archive must not be reopened"))
    assert CaseTreeRepository(store).list_nodes(case["id"])["items"]
    assert get_case_navigation(store, case["id"])["state"] == "ready"


def test_relocation_uses_new_registered_roots_and_owned_relative_artifacts(fixture, tmp_path):
    store, case, _ = fixture
    build_case_trees(store, [case["id"]])
    moved = tmp_path / "relocated"
    shutil.copytree(store.root, moved)
    other = LibraryStore(moved)
    assert CaseTreeRepository(other).case(case["id"])["node_count"] > 30
    assert str(store.root).encode() not in CaseTreeRepository(other).index_path.read_bytes()


@pytest.mark.parametrize("kwargs", [{"offset": -1}, {"limit": 9}, {"limit": True}, {"node_path": "../3"}, {"view": "everything"}, {"direction": "causes"}])
def test_navigation_rejects_invalid_inputs(fixture, kwargs):
    store, case, _ = fixture
    with pytest.raises(ValueError):
        get_case_navigation(store, case["id"], **kwargs)


def test_cli_dry_run_uses_public_sorted_catalog_and_never_builds(fixture, monkeypatch, capsys):
    store, case, _ = fixture
    location = Path(__file__).parents[1] / "scripts/build_case_tree.py"
    spec = importlib.util.spec_from_file_location("case_tree_cli_fixture", location)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "build_case_trees", lambda *_a, **_k: pytest.fail("dry run must not construct an index"))
    assert cli.main(["--data-dir", str(store.root), "--library", "cases", "--limit", "1", "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["selection"]["case_ids"] == [case["id"]]
    assert not CaseTreeRepository(store).index_path.exists()


def test_global_metadata_query_can_locate_nested_nodes_without_searching_values(fixture):
    store, case, _ = fixture
    build_case_trees(store, [case["id"]])
    page = get_case_navigation(store, case["id"], query="Wall")
    assert page["total"] == 28 and page["search_scope"] == "all_retained_nodes"
    assert all(node["type"] == "Wall" for node in page["items"])
    assert get_case_navigation(store, case["id"], query="mystery")["total"] == 0


def test_long_metadata_uses_explicit_hint_projection_without_losing_nodes(fixture):
    store, _, source = fixture
    make_case(source.parent.parent, count=2, label="长标题" * 5000)
    store.build("cases", paths=[source], model_format="compact", deep=False, modes=[])
    case = next(obj for obj in store.objects() if obj["type"] == "model_case")
    build_case_trees(store, [case["id"]])
    nodes = all_nodes(CaseTreeRepository(store), case["id"])
    wall = next(node for node in nodes if node["tag"] == "wall0")
    found, offset = [], 0
    while True:
        page = get_case_navigation(store, case["id"], node_path=wall["parent_path"], offset=offset)
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= 7000
        found.extend(page["items"])
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert {node["tag"] for node in found} == {"wall0", "wall1", "selected"}
    assert any(node.get("hint_truncated") for node in found)
    assert all(node["child_count"] == 0 and node["has_children"] is False for node in found)


def test_function_calls_saved_and_api_origins_have_exact_independent_locators():
    from collections import defaultdict
    from advisor.case_tree import _dependencies, _node_metadata
    def prop(identity, name, value, **extra):
        return {"id": identity, "ordinal": identity, "name": name, "value": value,
                "value_type": "expression", "value_complete": True, **extra}
    nodes = [{"id": 1, "parent_id": None, "tag": "pars", "kind": "ModelParam", "properties": [prop(1, "alpha", "2")]},
             {"id": 2, "parent_id": None, "tag": "fun1", "kind": "FunctionFeature", "type": "Analytic",
              "properties": [prop(2, "funcname", "profile"), prop(3, "expr", "sin(local_coord)"), prop(5, "args", "local_coord")]},
             {"id": 3, "parent_id": None, "tag": "ht", "kind": "Physics", "type": "HeatTransfer",
              "properties": [prop(4, "expr", "profile(t)*alpha")],
              "api_observations": [prop(104, "expr", "alpha*T", run_id=8)]}]
    mappings = defaultdict(list)
    metadata = _node_metadata(nodes, mappings, "fixture-case")
    edges = _dependencies(nodes, mappings, metadata, "fixture-case")
    call = next(edge for edge in edges if edge["token"] == "profile")
    assert not any(edge["token"] == "local_coord" for edge in edges)
    assert call["kind"] == "function_call" and call["status"] == "confirmed"
    assert call["target_node_path"] == "compact:node/2"
    api = next(edge for edge in edges if edge["source_mode"] == "api")
    assert api["origin"] == "api_configuration" and api["property_locator"] == "api:compact:node/3::property/104"
    assert api["target_refs"][0]["property_locator"] == "saved:compact:node/1::property/1"
    assert {request["source"] for request in api["read_requests"]} == {"saved", "api"}
    assert metadata[3]["read_requests"][0]["source"] == "api"


def test_qualified_references_preserve_namespace_and_require_the_correct_target_family():
    from collections import defaultdict
    from advisor.case_tree import _dependencies, _node_metadata
    def prop(identity, name, value):
        return {"id": identity, "ordinal": identity, "name": name, "value": value,
                "value_type": "expression", "value_complete": True}
    nodes = [{"id": 1, "parent_id": None, "tag": "comp1", "kind": "ModelNode"},
             {"id": 2, "parent_id": 1, "tag": "g1", "kind": "GeomSequence"},
             {"id": 3, "parent_id": 1, "tag": "vars", "kind": "Variable", "properties": [prop(1, "alpha", "2")]},
             {"id": 4, "parent_id": 1, "tag": "ht", "kind": "Physics", "properties":
              [prop(2, "geom", "comp1.g1"), prop(3, "expr", "comp1.alpha*T"), prop(4, "study", "comp1.g1")]},
             {"id": 5, "parent_id": None, "tag": "comp2", "kind": "ModelNode"},
             {"id": 6, "parent_id": 5, "tag": "g1", "kind": "GeomSequence"}]
    mappings = defaultdict(list)
    edges = _dependencies(nodes, mappings, _node_metadata(nodes, mappings, "fixture"), "fixture")
    geom = next(edge for edge in edges if edge["kind"] == "geometry_ref")
    assert geom["status"] == "confirmed" and geom["token"] == "comp1.g1" and geom["target_node_path"] == "compact:node/2"
    symbol = next(edge for edge in edges if edge["kind"] == "expression_symbol")
    assert symbol["token"] == "comp1.alpha" and symbol["target_node_path"] == "compact:node/3"
    study = next(edge for edge in edges if edge["kind"] == "study_ref")
    assert study["status"] == "unresolved" and study["token"] == "comp1.g1"
    assert not any(edge["target_node_path"] == "compact:node/1" for edge in edges)
