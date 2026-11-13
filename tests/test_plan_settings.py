"""Authored transfers cannot create evidence or verify a physical choice."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.plan_settings import (PLAN_SETTINGS_SCHEMA, review_plan_settings,
                                   validate_plan_settings)


def row():
    return {"id": "setting_a", "requirement_id": "answer:outputs", "route_id": "route_a",
            "requirement": "User-defined output A", "setting": "Configuration choice A",
            "proposed_value": "case-specific expression [unit]", "status": "adapted",
            "reason": "Current inputs differ from the example.",
            "transfer_rationale": "Retain the method and derive the expression from the given inputs.",
            "gap": "Check applicability during implementation.", "step_ids": ["configure"],
            "source_refs": [{"source_id": "case-exact", "context": "case_configuration",
                             "locator": "component/a/node", "stated_value": "example expression [unit]"}]}


def ledger():
    return {"read_source_ids": ["case-exact"], "available_source_ids": ["case-exact"],
            "read_contexts": [{"source_id": "case-exact", "context": "case_configuration",
                               "locator": "component/a/node"}]}


def plan():
    return {"steps": [{"id": "configure"}]}


def codes(result):
    return {warning["code"] for warning in result["warnings"]}


def test_source_stated_and_proposed_values_remain_distinct_without_automatic_verification():
    setting, evidence = row(), ledger()
    result = review_plan_settings([setting], plan=plan(), evidence=evidence)
    saved = result["plan_settings"][0]
    assert saved["source_refs"][0]["stated_value"] == "example expression [unit]"
    assert saved["proposed_value"] == "case-specific expression [unit]"
    assert result["per_setting"][0]["source_reviews"][0]["evidence_status"] == "read_context_recorded"
    assert result["source_value_verification"] == "not_evaluated"
    assert result["physical_correctness"] == "not_evaluated"
    assert result["source_applicability"] == "agent_review_required"
    assert result["blocking"] is False
    assert codes(result) == {"authored_setting_gap"}


def test_values_and_physics_words_are_not_used_to_grade_transfer_applicability():
    first, second = row(), row()
    first.update(setting="solver physics units", proposed_value="999999 bananas",
                 transfer_rationale="Interfaces and convergence.")
    second.update(setting="arbitrary symbols", proposed_value="x",
                  transfer_rationale="A different author explanation.")
    a = review_plan_settings([first], plan=plan(), evidence=ledger())
    b = review_plan_settings([second], plan=plan(), evidence=ledger())
    a.pop("plan_settings")
    b.pop("plan_settings")
    assert a == b


def test_read_receipt_does_not_verify_an_authored_claim_about_the_source_value():
    setting = row()
    setting["source_refs"][0]["stated_value"] = "An unverified author claim about the source."
    result = review_plan_settings([setting], evidence=ledger())
    source_review = result["per_setting"][0]["source_reviews"][0]
    assert source_review["evidence_status"] == "read_context_recorded"
    assert source_review["source_value_verification"] == "not_evaluated"
    assert result["plan_settings"][0]["source_refs"][0]["stated_value"] == setting["source_refs"][0]["stated_value"]


def test_source_less_user_input_or_derivation_is_valid_and_has_explicit_source_gap():
    setting = row()
    setting.update(source_refs=[], reason="The user supplied the value.", gap="")
    assert validate_plan_settings([setting]) == [setting]
    result = review_plan_settings([setting], plan=plan(), evidence=ledger())
    assert codes(result) == {"setting_has_no_source"}
    assert result["gaps"][0]["setting_id"] == setting["id"]
    assert result["plan_settings"][0]["source_refs"] == []
    assert result["plan_settings"][0]["reason"] == "The user supplied the value."


def test_alternative_route_keeps_its_reason_without_requiring_fake_implementation_steps():
    setting = row()
    setting.update(status="alternative", route_id="route_b", step_ids=[], gap="",
                   reason="Retain this route if its additional input becomes available.")
    result = review_plan_settings([setting], plan=plan(), evidence=ledger())
    assert result["plan_settings"] == [setting]
    assert result["warnings"] == []


@pytest.mark.parametrize("status", ["unresolved", "not_applicable"])
def test_explicit_unresolved_or_inapplicable_reason_does_not_require_an_invented_value(status):
    setting = row()
    setting.update(status=status, proposed_value="", step_ids=[], source_refs=[])
    result = review_plan_settings([setting], plan=plan(), evidence=ledger())
    assert result["plan_settings"][0]["proposed_value"] == ""
    assert result["implementation_checks"] == []
    assert ("authored_setting_unresolved" in codes(result)) is (status == "unresolved")


@pytest.mark.parametrize("change", [
    {"source_id": "case-exact-prefix"},
    {"source_id": "CASE-EXACT"},
    {"locator": "component/a/node/child"},
    {"locator": "component/A/node"},
    {"locator": "component/a/node "},
    {"context": "source_text"},
])
def test_source_context_receipts_match_exact_id_mode_and_locator(change):
    setting = row()
    setting["source_refs"][0].update(change)
    evidence = ledger()
    # Reading the source does not imply reading every context in it.
    evidence["read_source_ids"].append(setting["source_refs"][0]["source_id"])
    evidence["available_source_ids"].append(setting["source_refs"][0]["source_id"])
    result = review_plan_settings([setting], evidence=evidence)
    assert "source_context_not_in_read_ledger" in codes(result)
    assert result["per_setting"][0]["source_reviews"][0]["evidence_status"] == "review_required"


def test_navigation_receipt_never_becomes_source_text_or_configuration_evidence():
    setting, evidence = row(), ledger()
    setting["source_refs"][0]["context"] = "navigation"
    evidence["read_contexts"][0]["context"] = "navigation"
    result = review_plan_settings([setting], evidence=evidence)
    assert "navigation_is_not_setting_evidence" in codes(result)
    assert result["per_setting"][0]["source_reviews"][0]["evidence_status"] == "review_required"


def test_source_text_read_cannot_establish_case_configuration_even_when_source_is_read():
    evidence = ledger()
    evidence["read_contexts"][0].update(context="source_text", locator="text:0:500")
    result = review_plan_settings([row()], evidence=evidence)
    assert "source_context_not_in_read_ledger" in codes(result)
    assert "source_not_in_read_ledger" not in codes(result)


def test_unknown_and_known_empty_ledgers_are_distinct_without_trusting_authored_references():
    unknown = review_plan_settings([row()])
    empty = review_plan_settings([row()], evidence={"read_source_ids": [],
                                                   "available_source_ids": [], "read_contexts": []})
    assert "source_context_unknown" in codes(unknown)
    assert "source_not_in_read_ledger" not in codes(unknown)
    assert set(unknown["evidence_ledger_scope"].values()) == {False}
    assert {"source_not_in_read_ledger", "source_unavailable", "source_context_not_in_read_ledger"} <= codes(empty)
    assert set(empty["evidence_ledger_scope"].values()) == {True}
    assert unknown["per_setting"][0]["source_reviews"][0]["evidence_status"] == "review_required"


def test_available_source_ids_and_read_receipts_are_independent_trust_boundaries():
    evidence = ledger()
    evidence["available_source_ids"] = []
    unavailable = review_plan_settings([row()], evidence=evidence)
    assert "source_unavailable" in codes(unavailable)
    assert "source_not_in_read_ledger" not in codes(unavailable)
    assert unavailable["per_setting"][0]["source_reviews"][0]["evidence_status"] == "review_required"
    evidence = ledger()
    evidence["read_source_ids"] = []
    unread = review_plan_settings([row()], evidence=evidence)
    assert "source_not_in_read_ledger" in codes(unread)
    assert "source_unavailable" not in codes(unread)


def test_missing_source_stated_value_is_a_gap_even_with_an_exact_read_receipt():
    setting = row()
    setting["source_refs"][0]["stated_value"] = ""
    result = review_plan_settings([setting], evidence=ledger())
    assert "source_stated_value_missing" in codes(result)
    assert result["plan_settings"][0]["source_refs"][0]["stated_value"] == ""
    assert result["source_value_verification"] == "not_evaluated"


def test_dangling_steps_and_missing_implementation_mapping_are_advisory():
    setting = row()
    setting["step_ids"] = ["absent"]
    result = review_plan_settings([setting], plan=plan(), evidence=ledger())
    finding = next(item for item in result["implementation_checks"] if item["code"] == "unknown_setting_step")
    assert finding["step_ids"] == ["absent"]
    assert result["blocking"] is False
    setting["step_ids"] = []
    result = review_plan_settings([setting], plan=plan(), evidence=ledger())
    assert "setting_has_no_implementation_step" in codes(result)
    assert "unknown_setting_step" not in codes(result)


def test_missing_plan_is_unknown_but_explicit_empty_plan_has_no_matching_steps():
    assert review_plan_settings([row()])["plan_step_ids_known"] is False
    result = review_plan_settings([row()], plan={"steps": []})
    assert result["plan_step_ids_known"] is True
    assert "unknown_setting_step" in codes(result)


def test_review_returns_owned_copies_and_does_not_mutate_rows_plan_or_ledger():
    inputs = ([row()], plan(), ledger())
    before = copy.deepcopy(inputs)
    result = review_plan_settings(inputs[0], plan=inputs[1], evidence=inputs[2])
    assert inputs == before
    result["plan_settings"][0]["source_refs"][0]["source_id"] = "changed"
    result["plan_settings"][0]["step_ids"].append("changed")
    assert inputs == before


@pytest.mark.parametrize("field,value", [
    ("status", "verified"), ("reason", "  "), ("transfer_rationale", ""),
    ("proposed_value", ""), ("step_ids", "configure"), ("step_ids", [False]),
    ("step_ids", ["configure", "configure"]), ("source_refs", {}),
    ("source_refs", [{"source_id": "case-exact"}]), ("evidence", ledger()),
    ("setting", "x" * 6001), ("route_id", ""), ("requirement_id", 1),
])
def test_malformed_rows_are_rejected_without_mutating_input(field, value):
    setting = row()
    setting[field] = value
    before = copy.deepcopy(setting)
    with pytest.raises(ValueError):
        validate_plan_settings([setting])
    assert setting == before


@pytest.mark.parametrize("change", [
    {"source_id": ""}, {"context": "configuration_verified"}, {"locator": " "},
    {"stated_value": 2}, {"read": True}, {"locator": "x" * 2001},
])
def test_malformed_source_refs_cannot_smuggle_read_or_verified_claims(change):
    setting = row()
    setting["source_refs"][0].update(change)
    with pytest.raises(ValueError):
        validate_plan_settings([setting])


def test_row_reference_and_identity_bounds_are_enforced():
    with pytest.raises(ValueError):
        validate_plan_settings([row()] * 65)
    with pytest.raises(ValueError):
        validate_plan_settings([row(), row()])
    setting = row()
    setting["source_refs"] *= 9
    with pytest.raises(ValueError):
        validate_plan_settings([setting])
    setting["source_refs"] = [copy.deepcopy(row()["source_refs"][0])] * 2
    with pytest.raises(ValueError):
        validate_plan_settings([setting])
    assert validate_plan_settings([]) == []
    assert PLAN_SETTINGS_SCHEMA["maxItems"] == 64


def test_byte_limit_includes_json_escaping_without_truncating_the_authored_row():
    setting = row()
    setting["gap"] = "\0" * 3000
    before = copy.deepcopy(setting)
    with pytest.raises(ValueError, match="canonical UTF-8 JSON bytes"):
        validate_plan_settings([setting])
    assert setting == before


def test_strings_without_a_valid_utf8_encoding_are_a_validation_error():
    setting = row()
    setting["setting"] = "\ud800"
    before = copy.deepcopy(setting)
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        validate_plan_settings([setting])
    assert setting == before


@pytest.mark.parametrize("evidence", [
    {"read_source_ids": "case-exact"}, {"available_source_ids": [False]},
    {"read_contexts": {}}, {"read_contexts": [{}]},
    {"read_contexts": [{"source_id": "case-exact", "context": "verified", "locator": "node"}]},
])
def test_malformed_trusted_ledger_is_rejected_instead_of_assumed_authoritative(evidence):
    with pytest.raises(ValueError):
        review_plan_settings([row()], evidence=evidence)
