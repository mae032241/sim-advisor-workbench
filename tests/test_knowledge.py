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


def test_source_archive_preserves_hierarchy_and_paragraphs(corpus):
    identity = source_id("com.comsol.help.heat/heat_flux.html")
    document = corpus.get_document(identity)
    assert document["title"] == "Heat Flux"
    assert document["version"] == "6.3"
    assert document["url"] == "https://doc.comsol.com/6.3/doc/com.comsol.help.heat/heat_flux.html"
    assert document["section_path"] == ["Heat Transfer Module", "Heat Transfer Features", "Heat Flux"]
    assert document["parent"]["title"] == "Heat Transfer Features"
    assert document["excerpt"] in document["text"]
    assert document["retrieval_backend"] == "legacy-html-archive"
    assert "fake_secret_ignored" not in document["text"]


def test_new_archive_does_not_construct_a_second_fulltext_index(corpus):
    with corpus._connect() as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    assert "documents" in names and "topics_fts" not in names


def test_archive_updates_preserve_historical_source_ids(corpus):
    identity = source_id("com.comsol.help.heat/heat_flux.html")
    assert corpus.build()["last_build"]["indexed"] == 0
    write_topic(corpus.doc_root, "com.comsol.help.heat", "heat_flux.html", "Heat Flux",
                ["The Heat Flux node now documents an explicit calibration coefficient."])
    assert corpus.build()["last_build"]["indexed"] == 1
    after = corpus.get_document(identity)
    assert after["id"] == identity and "calibration coefficient" in after["text"]


def test_old_search_entry_delegates_to_the_single_registered_provider(corpus):
    from unittest.mock import patch
    with patch("advisor.library_provider.LibraryKnowledgeProvider") as provider:
        provider.return_value.search.return_value = [{"id": "registered-qmd"}]
        assert corpus.search("heat flux", kind="manual", limit=3, filters={"mode": "native"}) == [{"id": "registered-qmd"}]
    provider.assert_called_once_with(corpus, corpus.data_dir / "libraries")
    provider.return_value.search.assert_called_once_with("heat flux", kind="manual", limit=3, filters={"mode": "native"})


def test_archived_manuals_are_not_a_search_fallback(corpus):
    assert corpus.status()["document_count"] == 7
    assert corpus.search("heat flux") == []
    assert corpus.get_document(source_id("com.comsol.help.heat/heat_flux.html"))


def test_missing_or_invalid_inputs_are_explicit(tmp_path, corpus):
    kb = KnowledgeBase(tmp_path / "empty", tmp_path / "missing")
    assert not kb.build()["ready"]
    assert kb.search("heat") == []
    assert corpus.get_document("../../anything") is None
    with pytest.raises(ValueError):
        corpus.search("heat", kind="fabricated")
    with pytest.raises(ValueError, match="backend"):
        corpus.search("heat", filters={"backend": "pageindex"})


def test_aliases_and_honest_optional_status(corpus, monkeypatch):
    assert "heat sink" in expand_query("散热器")["phrases"]
    assert "stress" in expand_query("支架应力")["terms"]
    state = backend_status(corpus.data_dir)
    assert "native" not in state
    assert state["pageindex"]["available"] is False
    assert state["qmd"]["semantic_available"] is False
    monkeypatch.delenv("SIM_ADVISOR_PAGEINDEX_MODEL", raising=False)
    with pytest.raises(RuntimeError):
        PageIndexAdapter(corpus.data_dir).search("query", "doc-id")


@pytest.mark.skipif(not DEFAULT_DOC_ROOT.is_dir(), reason="Local official COMSOL help is not installed")
def test_installed_source_archive_remains_readable_without_legacy_search(tmp_path):
    paths = ["com.comsol.help.heat/heat_ug_ht_features.09.051.html",
             "com.comsol.help.models.heat.heat_sink/heat_sink.html",
             "com.comsol.help.models.sme.bracket_static/bracket_static.html",
             "com.comsol.help.sme/sme_ug_solid.07.063.html",
             "com.comsol.help.cfd/cfd_ug_fluidflow_single.06.008.html",
             "com.comsol.help.comsol/comsol_api_general.47.01.html"]
    root = tmp_path / "COMSOL63/doc"
    for rel in paths:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAULT_DOC_ROOT / rel, target)
    kb = KnowledgeBase(tmp_path / "data", root)
    assert kb.build()["count"] == len(paths)
    for rel in paths:
        saved = kb.get_document(source_id(rel))
        assert saved and saved["text"] and saved["url"].endswith(rel)
    assert kb.search("heat") == []
