"""Progressive tool views keep evidence complete without duplicated context."""
import copy
import json
from types import SimpleNamespace

from advisor.planning_service import _configuration_view, _guide_view
from advisor import compact_library


def test_default_navigation_is_small_and_expansion_keeps_exact_stage_data():
    mesh = {"id": "mesh", "title": "网格", "observed": [{"hint": "Saved Size", "property_hints": ["x" * 1000]}],
            "source_refs": [{"id": "lo-mesh"}], "configuration_requests": [{"node_path": "compact:node/3"}]}
    full = {"state": "ready", "case_id": "lo-case", "stages": [mesh, {**mesh, "id": "solver"}], "is_read_evidence": False}
    before = copy.deepcopy(full)
    summary = _guide_view(full)
    assert summary["detail_scope"] == "flow_overview"
    assert len(json.dumps(summary)) < 1000
    assert summary["stages"][0]["source_count"] == 1
    assert "configuration_requests" not in summary["stages"][0]
    assert "property_hints" not in json.dumps(summary)
    expanded = _guide_view(full, "mesh")
    assert expanded["stages"] == [mesh] and expanded["is_read_evidence"] is False
    assert full == before


def test_saved_configuration_keeps_typed_values_and_status_without_other_mode_or_per_row_provenance():
    typed = {"rows": [[0, 10, 20]], "row_lengths": [3]}
    original = {"source": "xml", "returned": 1, "physical_validation": "not_run", "effective_activation": "not_evaluated",
                "extraction_coverage": {"status": "partial", "read_errors": [f"error-{n}" for n in range(50)]},
                "nodes": [{"path": "compact:node/3", "id": 3, "case_ref": "same-model", "children": [4, 5],
                           "properties": [{"name": "tlist", "ordinal": 1, "value": typed, "status": "decoded", "value_complete": True, "case_ref": "same-model"}],
                           "api_observations": [{"name": "tlist", "value": typed}]}]}
    before = copy.deepcopy(original)
    value = _configuration_view(original)
    assert value["nodes"][0]["properties"][0]["value"] == typed
    assert value["nodes"][0]["properties"][0]["value_complete"] is True
    assert "api_observations" not in value["nodes"][0]
    assert "case_ref" not in json.dumps(value)
    assert value["extraction_coverage"]["status"] == "partial" and value["coverage_read_scope"] == "bounded_summary"
    assert value["extraction_coverage"]["read_errors"][-1] == {"remaining_items": 38}
    assert original == before


def test_api_configuration_retains_getter_and_comparison_without_saved_value_duplication():
    observation = {"name": "hmax", "value": 0.1, "getter": "getDouble", "status": "observed", "comparison": "different", "case_ref": "same-model"}
    result = _configuration_view({"source": "api", "nodes": [{"path": "compact:node/3", "api_observations": [observation]}]})
    returned = result["nodes"][0]["api_observations"][0]
    assert returned == {key: value for key, value in observation.items() if key != "case_ref"}


def test_exact_configuration_reads_one_primary_key_without_hydrating_other_nodes(monkeypatch):
    read_ids = []
    def read_node(identity):
        read_ids.append(identity)
        return {"id": identity, "kind": "MeshFeature", "feature_branch": "mesh", "properties": []}
    def unrelated():
        raise AssertionError("Exact reads must not iterate all case nodes")
    reader = SimpleNamespace(read_case=lambda: {"coverage": {"raw_xml": {"status": "partial"}}},
                             read_node=read_node, iter_nodes=unrelated)
    monkeypatch.setattr(compact_library, "_reader", lambda *_args: reader)
    result = compact_library.configuration({"id": "lo-case"}, library_root="unused", node_path="compact:node/3 ", category="mesh")
    assert read_ids == [3] and result["returned"] == 1 and result["nodes"][0]["path"] == "compact:node/3"
