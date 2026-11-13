"""Public selection behavior uses explicit fictional identities, never corpora."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "scripts/source_release_docs/scripts/select_evaluation_cases.py"
SOURCE = OVERLAY if OVERLAY.is_file() else ROOT / "scripts/select_evaluation_cases.py"
SPEC = importlib.util.spec_from_file_location("public_evaluation_selection", SOURCE)
selection = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selection)


def choices():
    return {"id": "fictional-selection", "name": "Fictional user selection",
            "description": "Synthetic unit-test metadata only.", "choices": [{
                "library_id": "fictional-library", "asset_key": "fictional-inputs:chosen/example.mph",
                "direction": "User category", "selection_reason": "User-authored synthetic reason."}]}


def cases():
    return [{"id": "fictional-citation-" + str(index), "object_id": "fictional-object-" + str(index),
             "library_id": "fictional-library", "asset_key": key, "asset_hash": "synthetic-hash-" + str(index),
             "title": "Fictional candidate", "physics_types": ["SyntheticInterface"],
             "study_types": [], "coupling_types": [], "physics_features": []}
            for index, key in enumerate(("fictional-inputs:chosen/example.mph", "fictional-inputs:other/example.mph"))]


def test_user_choices_match_complete_identity_and_keep_user_metadata(tmp_path):
    value = choices()
    path = tmp_path / "choices.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    result = selection.select(selection.analyze(cases()), selection.load_choices(path))
    assert {key: result[key] for key in ("id", "name", "description")} == {
        key: value[key] for key in ("id", "name", "description")}
    assert len(result["models"]) == 1
    assert result["models"][0]["asset_key"] == value["choices"][0]["asset_key"]
    assert result["models"][0]["direction"] == value["choices"][0]["direction"]
    assert result["models"][0]["selection_reason"] == value["choices"][0]["selection_reason"]
    assert result["selection_method"]["candidate_count"] == 2


def test_missing_duplicate_and_invalid_choices_are_rejected():
    value = choices()
    invalid = [None, {}, dict(value, choices=[]), dict(value, extra=True)]
    duplicate = deepcopy(value)
    duplicate["choices"].append(deepcopy(duplicate["choices"][0]))
    invalid.append(duplicate)
    missing_field = deepcopy(value)
    del missing_field["choices"][0]["selection_reason"]
    invalid.append(missing_field)
    bad_type = deepcopy(value)
    bad_type["choices"][0]["asset_key"] = 1
    invalid.append(bad_type)
    for item in invalid:
        with pytest.raises(ValueError):
            selection.validate_choices(item)
    absent = deepcopy(value)
    absent["choices"][0]["asset_key"] = "fictional-inputs:missing/example.mph"
    with pytest.raises(ValueError, match="found 0"):
        selection.select(selection.analyze(cases()), absent)
    repeated_candidates = cases()
    repeated_candidates.append(deepcopy(repeated_candidates[0]))
    with pytest.raises(ValueError, match="found 2"):
        selection.select(selection.analyze(repeated_candidates), value)


def test_no_preselected_cases_and_choices_argument_is_mandatory(tmp_path):
    assert not hasattr(selection, "CHOICES")
    with pytest.raises(SystemExit) as error:
        selection.main(["--data-dir", str(tmp_path), "--output", str(tmp_path / "selection.json")])
    assert error.value.code == 2
    assert not (tmp_path / "selection.json").exists()
    with pytest.raises(FileNotFoundError):
        selection.load_choices(tmp_path / "missing-choices.json")
