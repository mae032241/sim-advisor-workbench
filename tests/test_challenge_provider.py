import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.challenge_provider import ChallengeKnowledgeProvider


def manual(cid="cs-contact", **extra):
    result = {"id": cid, "title": "Contact", "text": "Contact pairs identify opposing boundaries.\nChoose an appropriate contact formulation.",
              "excerpt": "Contact pairs identify opposing boundaries.", "url": "https://doc.comsol.com/6.3/doc/com.comsol.help.sme/sme_ug_contact.html",
              "relative_path": "com.comsol.help.sme/sme_ug_contact.html", "kind": "manual", "section_path": ["Structural Mechanics", "Contact"],
              "package": "com.comsol.help.sme", "version": "6.3", "related_links": [], "parent_id": None,
              "retrieval_backend": "native-html-hierarchy-sqlite-fts5"}
    return dict(result, **extra)


def modern(cid="lo-basic", stem="bracket_basic", library="cases", **extra):
    result = {"id": cid, "title": "Bracket reference", "text": "Identify the loaded area and check mesh convergence.", "library_id": library,
              "url": f"library://{library}/{cid}", "source_uri": f"library://{library}/{cid}", "kind": "case", "version": "6.3",
              "package": library, "asset_key": f"applications:Structural_Mechanics_Module/Tutorials/{stem}.mph",
              "evidence": [{"root_id": "applications", "relative_path": f"Structural_Mechanics_Module/Tutorials/{stem}.mph", "method": "native_mph_xml_snapshot"}],
              "section_path": ["Cases", "Bracket reference"], "data": {"annotation": {"status": "inferred", "summary": "A related structural example"}}}
    return dict(result, **extra)


class FakeStore:
    def __init__(self, documents, active):
        self.documents = documents
        self.active = active

    def libraries(self):
        return [{"id": "cases"}, {"id": "manuals"}, {"id": "private"}]

    def objects(self, library_ids=None):
        return [copy.deepcopy(self.documents[cid]) for cid in self.active if library_ids is None or self.documents[cid]["library_id"] in library_ids]

    def current_generation(self):
        return "fixture-generation"


class FakeLegacy:
    def __init__(self, base):
        self.base = base
        self.calls = []

    def search(self, query, limit=8, kind="all", filters=None):
        self.calls.append({"query": query, "limit": limit, "kind": kind, "filters": filters})
        return [copy.deepcopy(doc) for doc in self.base.documents.values() if not doc["id"].startswith("lo-") and (kind == "all" or doc["kind"] == kind)][:limit]


class FakeBase:
    def __init__(self, documents, active=None):
        self.documents = {d["id"]: copy.deepcopy(d) for d in documents}
        self.store = FakeStore(self.documents, active if active is not None else [d["id"] for d in documents if d["id"].startswith("lo-")])
        self.legacy = FakeLegacy(self)
        self.calls = []
        self.reads = []
        self.backend_override = None

    def search(self, query, limit=8, kind="all", filters=None):
        self.calls.append({"query": query, "limit": limit, "kind": kind, "filters": filters})
        hits = self.store.objects(filters["library_ids"])
        for doc in hits:
            doc["retrieval"] = {"backend": self.backend_override or filters["mode"], "generation": "fixture-generation", "score": 0.81}
        return [d for d in hits if kind == "all" or d["kind"] == kind][:limit]

    def get_document(self, cid):
        self.reads.append(cid)
        return copy.deepcopy(self.documents.get(cid))

    def validate_source(self, doc):
        return doc == self.documents.get(doc.get("id"))

    def status(self):
        return {"ready": True, "count": 17865, "libraries": self.store.libraries(), "backend": "native-html-hierarchy-sqlite-fts5"}


def wrapper(documents, **kwargs):
    return ChallengeKnowledgeProvider(FakeBase(documents), ["bracket_contact"], mode="qmd_vector", library_ids=["cases", "manuals"], **kwargs)


def test_mixed_backends_allow_related_cases_and_generic_contact_with_audit():
    provider = wrapper([modern(), modern("lo-thermal", "bracket_thermal"), manual()])
    hits = provider.search("短时热脉冲 接触 热阻", limit=8)
    assert {d["id"] for d in hits} == {"lo-basic", "lo-thermal", "cs-contact"}
    assert provider._base.calls[0]["query"] == "短时热脉冲 接触 热阻"
    assert provider._base.calls[0]["filters"] == {"mode": "qmd_vector", "library_ids": ["cases", "manuals"]}
    assert provider._base.legacy.calls[0]["kind"] == "manual"
    assert provider._base.legacy.calls[0]["filters"] == {"backend": "native"}
    assert hits[0]["retrieval"]["backend"] == "qmd_vector"
    lexical = next(d for d in hits if d["id"] == "cs-contact")
    assert lexical["retrieval"]["backend"] == "native"
    assert lexical["retrieval_backend"] == "native-html-hierarchy-sqlite-fts5"
    read = provider.get_document("lo-basic")
    assert provider.validate_source(read)
    audit = provider.audit
    assert any(e["event"] == "read" and e["source_id"] == "lo-basic" and e["query_id"] == 1 for e in audit)
    assert any(e["event"] == "candidate_read" and e["actual_backend"] == "qmd_vector" for e in audit)
    assert {e["branch"] for e in audit if e["event"] == "search_branch"} == {"registered_libraries", "legacy_manuals"}
    audit.clear()
    assert provider.audit
    state = provider.status()["challenge"]
    assert state["actual_backends_seen"] == ["native-html-hierarchy-sqlite-fts5", "qmd_vector"]
    assert state["legacy_counts_are_unfiltered"]
    assert "bracket_contact" not in json.dumps(provider.status())


@pytest.mark.parametrize("locator", [
    "https://doc.comsol.com/6.2/doc/com.comsol.help.models.sme.bracket_contact/bracket_contact.html",
    r"D:\COMSOL64\Multiphysics\applications\Structural_Mechanics_Module\Tutorials\BRACKET_CONTACT.mph",
    "https://doc.comsol.com/6.4/doc/models.sme.bracket_contact.pdf?download=1",
    "com.comsol.help.models.sme.%62racket%5fcontact/bracket_contact_geom_sequence.html",
    "applications:Structural_Mechanics_Module/Tutorials/bracket_contact_solution.mph",
    "com.comsol.help.models.sme.bracket_contact/bracket_contact.java",
])
def test_exact_case_family_is_denied_by_source_locator_across_versions(locator):
    doc = modern(title="Neutral title", asset_key=locator)
    provider = wrapper([doc])
    assert provider.get_document(doc["id"]) is None
    assert not provider.validate_source(doc)
    assert provider.search("contact", kind="case") == []


def test_evidence_origin_rejects_code_even_when_blob_and_library_uri_are_opaque():
    doc = modern("lo-code", stem="bracket_contact", kind="code", evidence=[{"relative_path": "blobs/ab123.java", "root_id": "derived"}])
    provider = wrapper([doc, modern("lo-contactless", "bracket_contactless")])
    assert provider.get_document("lo-code") is None
    assert provider.get_document("lo-contactless")
    assert [d["id"] for d in provider.search("Contact")] == ["lo-contactless"]
    other = modern("lo-evidence", asset_key="applications:neutral.mph", evidence=[{"relative_path": "Tutorials/bracket_contact.mph"}])
    assert wrapper([other]).get_document(other["id"]) is None


def test_direct_historical_and_outside_library_ids_cannot_bypass_search_boundary():
    heldout = modern("lo-old", "bracket_contact")
    previous = modern("lo-previous", "bracket_basic")
    foreign = modern("lo-private", library="private")
    tutorial = manual("cs-tutorial", kind="case", url="https://doc.comsol.com/6.3/doc/com.comsol.help.models.sme.bracket_contact/bracket_contact.html")
    base = FakeBase([modern(), heldout, previous, foreign, tutorial], active=["lo-basic", "lo-private"])
    provider = ChallengeKnowledgeProvider(base, ["bracket_contact"], library_ids=["cases"])
    for doc in [heldout, previous, foreign, tutorial]:
        assert provider.get_document(doc["id"]) is None
        assert not provider.validate_source(doc)
    assert provider.get_document("lo-basic")


def test_reference_paragraph_links_and_annotations_are_removed_without_banning_manual():
    doc = manual(text="Contact uses a source and destination.\nFor the Bracket — Contact Analysis example, use ANSWER_SENTINEL.\nCheck penalty sensitivity and contact pressure.",
                 excerpt="See bracket_contact for ANSWER_SENTINEL.", related_links=[{"href": "../com.comsol.help.models.sme.bracket_contact/bracket_contact.html", "title": "Example"}, {"href": "sme_ug_contact_theory.html", "title": "Contact theory"}],
                 data={"annotation": {"summary": "See bracket_contact: ANSWER_SENTINEL", "status": "inferred"}, "links": [{"url": "models.sme.bracket_contact.pdf"}]})
    provider = wrapper([doc])
    clean = provider.get_document(doc["id"])
    assert clean and "Contact uses a source" in clean["text"] and "Check penalty" in clean["text"]
    assert clean["related_links"] == [{"href": "sme_ug_contact_theory.html", "title": "Contact theory"}]
    assert clean["data"]["annotation"] == {"status": "inferred"}
    assert clean["data"]["links"] == []
    assert clean["challenge_filter"]["redacted"]
    assert provider.validate_source(clean)
    assert "ANSWER_SENTINEL" not in json.dumps([clean, provider.search("Contact"), provider.audit])
    assert "bracket_contact" not in json.dumps(clean)
    assert doc["text"] == provider._base.documents[doc["id"]]["text"]


def test_parent_context_resolves_actual_source_and_rejects_opaque_forbidden_parent():
    parent = manual("cs-parent", relative_path="com.comsol.help.models.sme.bracket_contact/bracket_contact.html", text="ANSWER_SENTINEL")
    child = manual(parent_id="cs-parent", parent={"id": "cs-parent", "title": "General chapter", "excerpt": "ANSWER_SENTINEL"})
    provider = wrapper([parent, child])
    clean = provider.get_document(child["id"])
    assert clean and clean["parent_id"] is None and "parent" not in clean
    assert "ANSWER_SENTINEL" not in json.dumps(clean)
    assert any(e["event"] == "context_check" and e["context_id"] == "cs-parent" and not e["allowed"] for e in provider.audit)


def test_allowed_parent_is_sanitized_and_missing_or_cyclic_context_is_not_exposed():
    parent = manual("cs-parent", text="Generic theory.\nSee bracket_contact: ANSWER_SENTINEL", excerpt="See bracket_contact: ANSWER_SENTINEL")
    child = manual(parent_id="cs-parent", parent={"id": "cs-parent", "excerpt": "ANSWER_SENTINEL"})
    provider = wrapper([parent, child])
    clean = provider.get_document(child["id"])
    assert clean["parent"]["excerpt"] == "Generic theory."
    assert "ANSWER_SENTINEL" not in json.dumps(clean)
    assert provider.validate_source(clean)
    orphan = wrapper([manual(parent_id="missing")]).get_document("cs-contact")
    assert orphan["parent_id"] is None
    cyclic = wrapper([manual(parent_id="cs-contact")]).get_document("cs-contact")
    assert cyclic["parent_id"] is None


def test_validate_source_rejects_modified_text_evidence_and_unfiltered_source():
    provider = wrapper([modern(), manual(text="Allowed theory.\nSee bracket_contact: ANSWER_SENTINEL")])
    source = provider.get_document("lo-basic")
    assert provider.validate_source(source)
    for key, value in [("text", "Injected content"), ("evidence", [{"relative_path": "elsewhere.mph"}]), ("library_id", "private")]:
        modified = dict(source, **{key: value})
        assert not provider.validate_source(modified)
    assert not provider.validate_source(provider._base.documents["cs-contact"])
    modified = dict(source, extra_context="bracket_contact answer")
    assert not provider.validate_source(modified)


def test_caller_filters_can_only_narrow_pinned_mode_and_libraries():
    provider = wrapper([modern(), modern("lo-private", library="private"), manual()])
    hits = provider.search("bracket", filters={"library_ids": ["cases", "private"]})
    assert [d["id"] for d in hits] == ["lo-basic"]
    assert provider._base.calls[0]["filters"]["library_ids"] == ["cases"]
    assert not provider._base.legacy.calls
    assert provider.search("bracket", filters={"library_ids": ["private"]}) == []
    with pytest.raises(ValueError, match="pinned"):
        provider.search("bracket", filters={"mode": "native"})
    with pytest.raises(ValueError, match="pinned"):
        provider.search("bracket", filters={"backend": "native"})
    assert provider.search("bracket", filters={"backend": "qmd"})
    assert provider._base.calls[-1]["filters"]["mode"] == "qmd_vector"


def test_backend_mismatch_is_explicit_no_lexical_result_pretends_to_be_vector():
    provider = wrapper([modern()])
    provider._base.backend_override = "qmd_keyword"
    with pytest.raises(RuntimeError, match="backend"):
        provider.search("contact")
    assert any(e["event"] == "backend_mismatch" and e["actual"] == "qmd_keyword" for e in provider.audit)


def test_missing_original_provenance_and_no_safe_content_are_denied():
    opaque = modern(asset_key="library://cases/opaque", evidence=[{"relative_path": "blobs/hash.java"}])
    provider = wrapper([opaque, manual(text="Only bracket_contact: ANSWER_SENTINEL", excerpt="bracket_contact")])
    assert provider.get_document("lo-basic") is None
    assert provider.get_document("cs-contact") is None


def test_explicit_terms_remove_other_known_solution_reference_without_mutation():
    provider = wrapper([manual(text="Generic manual.\nThe special solution token ANSWER_SENTINEL explains this.")], excluded_terms=["special solution token"])
    assert provider.get_document("cs-contact")["text"] == "Generic manual."
    assert "ANSWER_SENTINEL" not in json.dumps(provider.audit)


def test_excluded_path_can_be_full_pdf_and_still_exclude_html_model_family():
    base = FakeBase([modern("lo-secret", "bracket_contact"), manual()])
    provider = ChallengeKnowledgeProvider(base, ["https://doc.comsol.com/6.3/doc/models.sme.bracket_contact.pdf"], library_ids=["cases"])
    assert provider.get_document("lo-secret") is None
    assert provider.get_document("cs-contact")


@pytest.mark.parametrize("locator", [
    "COMSOL_Multiphysics/Fluid_Dynamics/micromixer.mph",
    r"COMSOL_Multiphysics\Fluid_Dynamics\MICROMIXER_GEOM_SEQUENCE.mph",
    "https://doc.comsol.com/6.2/doc/com.comsol.help.models.mph.micromixer/micromixer.html",
    "https://doc.comsol.com/6.4/doc/models.mph.micromixer.pdf",
    "com.comsol.help.models.mph.micromixer_cluster/micromixer_cluster.java",
    "com.comsol.help.models.cfd.micromixer_particle_tracing/micromixer_particle_tracing.html",
])
def test_second_challenge_micromixer_family_preserves_fluid_baselines(locator):
    hidden = modern("lo-hidden", stem="micromixer", asset_key=locator)
    cylinder, backstep = modern("lo-cylinder", "cylinder_flow"), modern("lo-backstep", "backstep")
    legacy = manual("cs-hidden", relative_path="com.comsol.help.models.mph.micromixer/micromixer.html", kind="case")
    base = FakeBase([hidden, cylinder, backstep, legacy])
    provider = ChallengeKnowledgeProvider(base, ["bracket_contact", "COMSOL_Multiphysics/Fluid_Dynamics/micromixer.mph"], mode="qmd_vector", library_ids=["cases"])
    assert provider._forbidden_locator(locator)
    assert provider.get_document("lo-hidden") is None
    assert provider.get_document("cs-hidden") is None  # No preceding search needed.
    assert not provider.validate_source(hidden)
    assert {doc["id"] for doc in provider.search("internal channel pressure drop")} == {"lo-cylinder", "lo-backstep"}
    assert provider.validate_source(provider.get_document("lo-cylinder"))
    assert provider.validate_source(provider.get_document("lo-backstep"))


def test_second_challenge_reference_cleanup_and_exact_family_boundary():
    parent = manual("cs-parent", relative_path="com.comsol.help.models.mph.micromixer/micromixer.html", kind="case", text="WITHHELD_FIXTURE")
    guide = manual(text="Generic transport and mesh guidance.\nSee the Micromixer example: WITHHELD_FIXTURE.\nCheck mass conservation.",
                   related_links=[{"href": "../com.comsol.help.models.mph.micromixer/micromixer.html", "title": "Example"}],
                   parent_id="cs-parent", parent={"id": "cs-parent", "excerpt": "WITHHELD_FIXTURE"})
    provider = ChallengeKnowledgeProvider(FakeBase([parent, guide]), ["COMSOL_Multiphysics/Fluid_Dynamics/micromixer.mph"], mode="qmd_vector", library_ids=["cases"])
    clean = provider.get_document("cs-contact")
    assert clean["text"] == "Generic transport and mesh guidance.\nCheck mass conservation."
    assert clean["related_links"] == [] and clean["parent_id"] is None
    assert "WITHHELD_FIXTURE" not in json.dumps(clean)
    assert provider.validate_source(clean)
    assert not provider._forbidden_locator("COMSOL_Multiphysics/Fluid_Dynamics/micromixer2.mph")
    assert not provider._forbidden_locator("CFD_Module/Single-Phase_Flow/backstep.mph")
    assert not provider._forbidden_locator("COMSOL_Multiphysics/Fluid_Dynamics/cylinder_flow.mph")
