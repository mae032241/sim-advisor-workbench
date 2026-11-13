"""Generic check selection and explicit mappings must not become physics tests."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.mechanism_checks import CHECK_IDS, known_check_ids, mechanism_checklist, review_mechanism_coverage


def mapping(identity="porous_transport.thermal_advection", status="adopted", **changes):
    return {"check_ids": [identity], "status": status,
            "implementation": {"step_id": "setup", "field": "action"}, **changes}


def test_empty_selection_exposes_bounded_catalog_and_common_checks_only():
    result = mechanism_checklist()
    assert result == mechanism_checklist([])
    assert result["selected_profiles"] == []
    assert {item["id"] for item in result["catalog"]} == {
        "conservation_coupling", "porous_transport", "moving_interface",
        "wave_transport", "charge_transport", "radiative_transitions"}
    assert all(item["id"].startswith("common.") for item in result["checks"])
    assert len(json.dumps(result["catalog"]).encode("utf-8")) < 5 * 1024
    assert result["physical_correctness"] == "not_evaluated"


@pytest.mark.parametrize("selection", ["porous_transport", {}, ["unknown"], ["common"], [None], ["porous_transport"] * 7])
def test_invalid_profile_selection_is_rejected(selection):
    with pytest.raises(ValueError, match="known profile"):
        mechanism_checklist(selection)


def test_multiple_profiles_deduplicate_preserve_order_and_include_common_once():
    selected = ["moving_interface", "porous_transport", "moving_interface"]
    result = mechanism_checklist(selected)
    identities = [item["id"] for item in result["checks"]]
    assert result["selected_profiles"] == ["porous_transport", "moving_interface"]
    assert len(identities) == len(set(identities))
    assert sum(identity.startswith("common.") for identity in identities) == len(mechanism_checklist()["checks"])
    assert len(identities) <= 32
    assert len(json.dumps(result).encode("utf-8")) < 20 * 1024
    assert selected == ["moving_interface", "porous_transport", "moving_interface"]


def test_all_checks_are_atomic_questions_with_generic_lookup_terms_and_known_fields():
    result = mechanism_checklist([item["id"] for item in mechanism_checklist()["catalog"]])
    assert len(result["checks"]) <= 64
    assert {item["id"] for item in result["checks"]} == CHECK_IDS == known_check_ids()
    for item in result["checks"]:
        assert set(item) == {"id", "title", "question", "search_terms", "needs"}
        assert item["question"].endswith("?")
        assert 1 <= len(item["search_terms"]) <= 4
        assert all(term.isascii() and len(term) < 70 for term in item["search_terms"])
        assert item["needs"] and set(item["needs"]) <= {"producer", "receiver", "scope", "expression", "unit", "activation"}


def test_result_mutation_cannot_change_future_catalog_or_checks():
    original = mechanism_checklist(["charge_transport"])
    modified = mechanism_checklist(["charge_transport"])
    modified["checks"][0]["question"] = "changed"
    modified["checks"][0]["needs"].append("changed")
    modified["catalog"][0]["title"] = "changed"
    assert mechanism_checklist(["charge_transport"]) == original
    with pytest.raises(AttributeError):
        known_check_ids().add("invented")


@pytest.mark.parametrize("rows", [None, [], [{"status": "adopted", "statement": "all physics interfaces units and solver are complete"}]])
def test_no_check_ids_means_unknown_and_never_an_automatic_pass(rows):
    result = review_mechanism_coverage(rows)
    assert result["coverage_state"] == "unknown"
    assert result["status"] == "advisory" and result["blocking"] is False
    assert result["counts"]["mapped"] == 0
    assert result["counts"]["unaddressed"] == len(mechanism_checklist()["checks"])
    assert result["physical_correctness"] == "not_evaluated"
    assert any(item["code"] == "mechanism_coverage_unknown" for item in result["warnings"])
    assert "pass" not in result


def test_one_tag_expands_the_whole_profile_and_common_questions():
    result = review_mechanism_coverage([mapping()])
    expected = [item["id"] for item in mechanism_checklist(["porous_transport"])["checks"]]
    assert result["selected_profiles"] == ["porous_transport"]
    assert result["expected_check_ids"] == expected
    assert result["mapped_check_ids"] == ["porous_transport.thermal_advection"]
    assert set(result["unaddressed_check_ids"]) == set(expected) - {"porous_transport.thermal_advection"}
    assert result["coverage_state"] == "unaddressed_checks"
    assert result["counts"]["active"] == 1


def test_unknown_check_ids_warn_without_selecting_profiles_or_granting_coverage():
    result = review_mechanism_coverage([mapping("invented.profile")])
    assert result["coverage_state"] == "unknown"
    assert result["selected_profiles"] == []
    assert result["mapped_check_ids"] == []
    warning = next(item for item in result["warnings"] if item["code"] == "mechanism_check_unknown")
    assert warning["check_ids"] == ["invented.profile"]


def test_content_keywords_cannot_change_authored_coverage_or_validate_physics():
    rows = [mapping(statement="thermal flow advection with correct units and coupling", reason="verified")]
    changed = copy.deepcopy(rows)
    changed[0].update(statement="arbitrary unrelated words", reason="no correctness assertion")
    first = {"steps": [{"id": "setup", "action": "energy, radiation, charge, mesh and units are complete"}]}
    second = {"steps": [{"id": "setup", "action": "unknown content"}]}
    assert review_mechanism_coverage(rows, first) == review_mechanism_coverage(changed, second)
    assert review_mechanism_coverage(rows, first)["physical_correctness"] == "not_evaluated"


def test_unresolved_and_inapplicable_dispositions_are_explicit_without_an_automatic_pass():
    checks = mechanism_checklist(["moving_interface"])["checks"]
    rows = [mapping(item["id"], "not_applicable" if index % 2 else "unresolved", implementation=None)
            for index, item in enumerate(checks)]
    result = review_mechanism_coverage(rows)
    assert result["unaddressed_check_ids"] == []
    assert result["counts"]["mapped"] == len(checks)
    assert result["counts"]["active"] == 0
    assert result["counts"]["unresolved"] > 0 and result["counts"]["not_applicable"] > 0
    assert {item["code"] for item in result["warnings"]} >= {"mechanism_check_unresolved", "mechanism_check_not_applicable"}
    assert result["physical_correctness"] == "not_evaluated"
    assert result["status"] == "advisory" and "pass" not in result


def test_mixed_dispositions_retain_unresolved_and_inapplicable_claims():
    result = review_mechanism_coverage([mapping(status="adopted"), mapping(status="unresolved"), mapping(status="not_applicable")])
    entry = next(item for item in result["dispositions"] if item["check_id"] == "porous_transport.thermal_advection")
    assert entry["status"] == "mixed_authored_status"
    assert entry["authored_statuses"] == ["adopted", "unresolved", "not_applicable"]
    assert entry["row_indexes"] == [0, 1, 2]
    assert result["counts"]["mapped"] == result["counts"]["unresolved"] == result["counts"]["not_applicable"] == 1


def test_duplicate_ids_in_a_row_count_once_and_multiple_profiles_expand():
    row = mapping(check_ids=["porous_transport.thermal_advection", "porous_transport.thermal_advection", "charge_transport.field_description"])
    result = review_mechanism_coverage([row])
    assert result["selected_profiles"] == ["porous_transport", "charge_transport"]
    assert result["counts"]["mapped"] == 2
    assert all(len(item["row_indexes"]) <= 1 for item in result["dispositions"])


def test_implementation_references_are_advisory_and_do_not_evaluate_field_content():
    rows = [mapping(), mapping("common.mesh_distribution", implementation=None)]
    result = review_mechanism_coverage(rows, {"steps": []})
    codes = {item["code"] for item in result["warnings"]}
    assert {"mechanism_implementation_missing", "mechanism_implementation_step_unknown"} <= codes
    assert result["blocking"] is False
    assert not any(item["code"] == "mechanism_implementation_step_unknown" for item in review_mechanism_coverage([mapping()])["warnings"])


@pytest.mark.parametrize("plan", [{}, {"steps": None}, {"steps": {}},
                                  {"steps": [None, [], "unknown", {"id": []}]}])
def test_arbitrary_plan_dicts_remain_advisory_without_inspecting_physics_content(plan):
    result = review_mechanism_coverage([mapping()], plan)
    assert result["status"] == "advisory" and result["physical_correctness"] == "not_evaluated"
    assert any(item["code"] == "mechanism_implementation_step_unknown" for item in result["warnings"])


def test_malformed_implementation_field_is_a_warning_without_granting_proof():
    result = review_mechanism_coverage([mapping(implementation={"step_id": "setup", "field": []})])
    assert any(item["code"] == "mechanism_implementation_missing" for item in result["warnings"])
    assert result["physical_correctness"] == "not_evaluated"


def test_common_only_mapping_preserves_unknown_mechanism_scope():
    result = review_mechanism_coverage([mapping("common.variable_definitions")])
    assert result["selected_profiles"] == []
    assert any(item["code"] == "mechanism_profile_scope_unknown" for item in result["warnings"])


def test_unspecified_status_does_not_become_active_coverage():
    result = review_mechanism_coverage([mapping(status="invented")])
    assert result["counts"]["unspecified"] == 1
    assert result["counts"]["active"] == 0
    assert any(item["code"] == "mechanism_disposition_unspecified" for item in result["warnings"])


@pytest.mark.parametrize("status", ["adopted", "adapted", "alternative"])
def test_actual_plan_setting_statuses_remain_authored_active_dispositions(status):
    result = review_mechanism_coverage([mapping(status=status)])
    entry = next(item for item in result["dispositions"] if item["check_id"] == "porous_transport.thermal_advection")
    assert entry["status"] == status and result["counts"][status] == 1
    assert result["counts"]["active"] == 1 and result["counts"]["unspecified"] == 0
    assert result["physical_correctness"] == "not_evaluated"


def test_review_is_detached_and_does_not_mutate_rows_or_plan():
    rows, plan = [mapping()], {"steps": [{"id": "setup", "action": "authored content"}]}
    before = copy.deepcopy((rows, plan))
    result = review_mechanism_coverage(rows, plan)
    result["dispositions"][0]["row_indexes"].append(100)
    result["mapped_check_ids"].append("invented")
    assert (rows, plan) == before
    assert review_mechanism_coverage(rows, plan)["counts"]["mapped"] == 1


@pytest.mark.parametrize("rows", [{}, [mapping()] * 65, [None], [mapping(check_ids="common.variable_definitions")],
                                  [mapping(check_ids=["common.variable_definitions"] * 17)], [mapping(check_ids=["x" * 121])]])
def test_review_input_bounds_are_explicit(rows):
    with pytest.raises(ValueError):
        review_mechanism_coverage(rows)


def test_unknown_id_warning_samples_remain_bounded():
    rows = [mapping(check_ids=[f"unknown_{index}_{other}" for other in range(16)]) for index in range(64)]
    result = review_mechanism_coverage(rows)
    warning = next(item for item in result["warnings"] if item["code"] == "mechanism_check_unknown")
    assert warning["count"] == 1024 and warning["truncated"] is True
    assert len(warning["check_ids"]) == 16
    assert len(json.dumps(result).encode("utf-8")) < 20 * 1024
