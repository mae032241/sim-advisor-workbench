import json

import pytest

from scripts import audit_case_methods as audit


def frozen_pack(tmp_path, *, joint=True, overview_only=False):
    text_ref = {"source_id": "tutorial", "context": "source_text", "locator": "text:0:20"}
    config_ref = {"source_id": "case", "context": "case_configuration", "locator": "saved:node/1"}
    claim = {"origin": "llm_inference", "explanation": "Synthetic source correspondence; unverified.",
             "limitations": ["Synthetic fixture, no physical claim."], "evidence_refs": [text_ref, config_ref]}
    sections = {"method_and_assumptions": [claim]}
    if not joint:
        sections = {"method_and_assumptions": [{**claim, "evidence_refs": [text_ref]}],
                    "domains_and_interfaces": [{**claim, "evidence_refs": [config_ref]}]}
    profile = {"case_id": "case", "sections": sections}
    pack = {"profile": profile, "profile_sha256": audit.sha(audit.canonical(profile)),
            "dependencies": {"case": {"type": "model_case", "asset_key": "models:test.mph"},
                             "tutorial": {"type": "model_case" if overview_only else "method_step",
                                          "asset_key": "models:test.mph" if overview_only else
                                          "help:com.comsol.help.models.test/test.html"}},
            "reads": [{**text_ref, "origin": "source_text"}, {**config_ref, "origin": "saved_configuration"}]}
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    return path, audit.sha(path.read_bytes())


def test_joint_audit_requires_tutorial_configuration_in_same_core_claim(tmp_path):
    path, digest = frozen_pack(tmp_path)
    report = audit.inspect_pack(path, digest)
    assert report["joint_construction_recorded"]
    assert report["joint_core_claim_count"] == 1
    assert "unverified" in audit.markdown({"model": "synthetic", "effort": "high", "profile_count": 1,
                                          "joint_construction_count": 1, "cases": [report]})


@pytest.mark.parametrize("kwargs", [{"joint": False}, {"overview_only": True}])
def test_two_separate_sources_or_overview_do_not_establish_joint_explanation(tmp_path, kwargs):
    path, digest = frozen_pack(tmp_path, **kwargs)
    assert not audit.inspect_pack(path, digest)["joint_construction_recorded"]


def test_modified_frozen_pack_is_rejected(tmp_path):
    path, digest = frozen_pack(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="Frozen pack changed"):
        audit.inspect_pack(path, digest)
