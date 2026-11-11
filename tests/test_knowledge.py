from pathlib import Path
import json
import shutil
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.knowledge import DEFAULT_DOC_ROOT, KnowledgeBase, TopicParser, expand_query, source_id
from advisor.retrieval_backends import PageIndexAdapter, backend_status


def write_topic(root, package, name, title, paragraphs):
    folder = root / package
    folder.mkdir(parents=True, exist_ok=True)
    body = "".join(f'<div class="Body_text">{p}</div>' for p in paragraphs)
    (folder / name).write_text(f'<html><head><title>{title}</title><script>fake_secret_ignored()</script></head><body><div class="Head1">{title}</div>{body}</body></html>', encoding="utf-8")


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "COMSOL63/doc"
    write_topic(root, "com.comsol.help.heat", "heat_parent.html", "Heat Transfer Features", ["Heat transfer features contain the available domain and boundary conditions for heat transfer in solids and fluids."])
    write_topic(root, "com.comsol.help.heat", "heat_flux.html", "Heat Flux", ["The Heat Flux node prescribes the inward heat flux on selected boundaries. Choose a convective heat flux to define heat exchange with the environment."])
    write_topic(root, "com.comsol.help.cfd", "inlet.html", "Inlet", ["For laminar fluid flow, select the inlet boundary and specify a velocity or pressure condition. The pressure outlet must be consistent with the flow boundary conditions."])
    write_topic(root, "com.comsol.help.sme", "fixed.html", "Fixed Constraint", ["For solid mechanics, the Fixed Constraint node sets the displacement to zero on the selected boundaries. Inspect reaction forces and stress concentrations near constrained edges."])
    write_topic(root, "com.comsol.help.models.heat.heat_sink", "heat_sink.html", "Heat Sink", ["This example simulates laminar flow and conjugate heat transfer through an aluminum heat sink. The base receives a heat flux while the air enters a channel.", "The material thermal conductivity, density, and heat capacity are temperature dependent in this example."])
    write_topic(root, "com.comsol.help.models.sme.bracket_static", "bracket_static.html", "Bracket — Static Analysis", ["The bracket tutorial solves a solid mechanics static analysis with a fixed constraint. Examine von Mises stress and displacement after applying the load."])
    write_topic(root, "com.comsol.help.comsol", "comsol_api_mesh.46.01.html", "Mesh Sequence API", ['To create a mesh sequence, use the Java API. <code>model.component("comp1").mesh().create("mesh1");</code> This creates the sequence; subsequent features still require explicit settings.'])
    (root / "com.comsol.help.heat/toc.xml").write_text('<toc label="Heat Transfer Module"><topic label="Heat Transfer Features" href="/com.comsol.help.heat/heat_parent.html"><topic label="Heat Flux" href="/com.comsol.help.heat/heat_flux.html"/></topic></toc>', encoding="utf-8")
    kb = KnowledgeBase(tmp_path / "data", root)
    assert kb.build()["ready"]
    return kb


def test_source_provenance_hierarchy_and_paragraphs(corpus):
    result = corpus.search("熱通量", kind="manual")[0]
    assert result["title"] == "Heat Flux"
    assert result["version"] == "6.3"
    assert result["url"] == "https://doc.comsol.com/6.3/doc/com.comsol.help.heat/heat_flux.html"
    assert result["section_path"] == ["Heat Transfer Module", "Heat Transfer Features", "Heat Flux"]
    assert result["id"] == source_id("com.comsol.help.heat/heat_flux.html")
    document = corpus.get_document(result["id"])
    assert document["parent"]["title"] == "Heat Transfer Features"
    assert result["excerpt"] in document["text"]
    assert document["retrieval_backend"] == "native-html-hierarchy-sqlite-fts5"
    assert "fake_secret_ignored" not in document["text"]


def test_chinese_physics_and_case_disambiguation(corpus):
    assert corpus.search("散熱器 共軛傳熱 層流")[0]["title"] == "Heat Sink"
    assert corpus.search("支架 固體力學 靜力 應力", kind="case")[0]["title"] == "Bracket — Static Analysis"
    assert corpus.search("固体力学 固定约束", kind="manual")[0]["title"] == "Fixed Constraint"
    assert corpus.search("層流 入口 壓力", kind="manual")[0]["title"] == "Inlet"
    assert all(r["kind"] == "manual" for r in corpus.search("heat", kind="manual"))
    assert corpus.search("heat", filters={"physics": "structure"}) == []
    assert corpus.search("heat", filters={"version": "6.2"}) == []


def test_code_branch_is_real_source(corpus):
    result = corpus.search("mesh Java API", kind="code")[0]
    assert result["branch"] == "api-examples"
    assert 'model.component("comp1").mesh().create("mesh1");' in result["text"]
    assert result["url"].endswith("comsol_api_mesh.46.01.html")


def test_exact_title_over_specialized_constraint_variants(corpus):
    write_topic(corpus.doc_root, "com.comsol.help.sme", "thermal.html", "Thermal Expansion", ["The Thermal Expansion multiphysics coupling accounts for temperature induced strain in solid mechanics. Select the solid domains and a temperature source."])
    write_topic(corpus.doc_root, "com.comsol.help.sme", "thermal_constraints.html", "Thermal Expansion (for Constraints)", ["Thermal expansion for constraints specifies thermal expansion in a fixed constraint. These thermal expansion settings only apply to the constraints."])
    write_topic(corpus.doc_root, "com.comsol.help.sme", "modeling_thermal.html", "Modeling Thermal Expansion", ["Thermal expansion couples the heat transfer solution to structural mechanics. Temperature changes lead to mechanical strains and sometimes stress."])
    corpus.build()
    assert corpus.search("Thermal Expansion")[0]["title"] == "Thermal Expansion"
    assert corpus.search("Modeling Thermal Expansion")[0]["title"] == "Modeling Thermal Expansion"
    assert corpus.search("熱膨脹 固體力學")[0]["title"] == "Thermal Expansion"


def test_incremental_build_preserves_ids_and_updates(corpus):
    before = corpus.search("熱通量")[0]
    status = corpus.build()
    assert status["last_build"]["indexed"] == 0
    assert status["last_build"]["unchanged"] == 7
    write_topic(corpus.doc_root, "com.comsol.help.heat", "heat_flux.html", "Heat Flux", ["The Heat Flux node prescribes the inward heat flux on selected boundaries. This updated source paragraph contains an explicit calibration coefficient."])
    status = corpus.build()
    assert status["last_build"]["indexed"] == 1
    after = corpus.get_document(before["id"])
    assert after["id"] == before["id"]
    assert "calibration coefficient" in after["text"]


def test_missing_or_invalid_inputs_are_explicit(tmp_path, corpus):
    kb = KnowledgeBase(tmp_path / "empty", tmp_path / "missing")
    status = kb.build()
    assert not status["ready"]
    assert "not found" in status["error"]
    assert kb.search("heat") == []
    assert corpus.get_document("../../anything") is None
    assert corpus.search('" OR * --') == []
    with pytest.raises(ValueError):
        corpus.search("heat", kind="fabricated")
    with pytest.raises(ValueError, match="not PageIndex"):
        corpus.search("heat", filters={"backend": "pageindex"})


def test_aliases_and_honest_optional_status(corpus, monkeypatch):
    assert "heat sink" in expand_query("散热器") ["phrases"]
    assert "stress" in expand_query("支架应力") ["terms"]
    state = backend_status(corpus.data_dir)
    assert state["native"]["available"]
    assert state["pageindex"]["available"] is False
    assert state["qmd"]["semantic_available"] is False
    monkeypatch.delenv("SIM_ADVISOR_PAGEINDEX_MODEL", raising=False)
    with pytest.raises(RuntimeError):
        PageIndexAdapter(corpus.data_dir).search("query", "doc-id")


@pytest.mark.skipif(not DEFAULT_DOC_ROOT.is_dir(), reason="Local official COMSOL 6.3 help is not installed")
def test_real_installed_heat_structure_flow_and_api_sources(tmp_path):
    # A bounded real-source integration fixture keeps this test independent of
    # the application's canonical corpus/build and never writes the originals.
    paths = [
        "com.comsol.help.heat/heat_ug_ht_features.09.051.html",
        "com.comsol.help.models.heat.heat_sink/heat_sink.html",
        "com.comsol.help.models.sme.bracket_static/bracket_static.html",
        "com.comsol.help.sme/sme_ug_solid.07.063.html",
        "com.comsol.help.cfd/cfd_ug_fluidflow_single.06.008.html",
        "com.comsol.help.comsol/comsol_api_general.47.01.html",
    ]
    root = tmp_path / "COMSOL63/doc"
    for rel in paths:
        original = DEFAULT_DOC_ROOT / rel
        assert original.is_file(), rel
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
    for package in ("com.comsol.help.heat", "com.comsol.help.sme", "com.comsol.help.cfd", "com.comsol.help.comsol"):
        shutil.copyfile(DEFAULT_DOC_ROOT / package / "toc.xml", root / package / "toc.xml")
    kb = KnowledgeBase(tmp_path / "data", root)
    assert kb.build()["count"] == len(paths)
    heat = kb.search("散热器 共轭传热 层流", kind="case")[0]
    assert heat["title"] == "Heat Sink"
    assert "1 W" in heat["text"] or "1\xa0W" in heat["text"]
    structure = kb.search("支架 固体力学 静力 应力", kind="case")[0]
    assert structure["title"] == "Bracket — Static Analysis"
    assert "Modeling Instructions" in structure["text"]
    flow = kb.search("层流 流体", filters={"package": "com.comsol.help.cfd"})[0]
    assert "Laminar Flow" in flow["title"]
    api = kb.search("general commands creating model", kind="code")[0]
    assert api["url"].endswith(paths[-1])
    assert all(kb.get_document(r["id"])["text"] for r in (heat, structure, flow, api))
