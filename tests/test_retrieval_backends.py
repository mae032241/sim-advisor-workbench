"""Compatibility entry points must not expose or query a second index."""
import json
from unittest.mock import Mock

from advisor.retrieval_backends import QMDAdapter, backend_status


def test_old_qmd_receipt_is_an_archive_and_never_reports_a_native_fallback(tmp_path, monkeypatch):
    receipt = tmp_path / "qmd" / "verified.json"
    receipt.parent.mkdir()
    receipt.write_text(json.dumps({"ready": True, "document_count": 17865}), encoding="utf-8")
    monkeypatch.setattr("advisor.retrieval_backends.qmd_command", lambda _: ["fixture-node"])
    monkeypatch.setattr("advisor.retrieval_backends.retrieval_runtime", lambda *_: {"errors": []})
    monkeypatch.setattr("advisor.retrieval_backends._pageindex_python", lambda _: None)
    state = backend_status(tmp_path)
    assert "native" not in state
    assert state["qmd"]["installed"] and not state["qmd"]["available"]
    assert state["qmd"]["archived_legacy_keyword"] == {
        "verified": True, "document_count": 17865, "searchable": False}


def test_qmd_compatibility_search_delegates_to_registered_keyword_engine(tmp_path, monkeypatch):
    archive = object()
    provider = Mock()
    provider.search.return_value = [{"id": "lo-qmd", "retrieval": {"backend": "qmd_keyword"}}]
    factory = Mock(return_value=provider)
    monkeypatch.setattr("advisor.knowledge.KnowledgeBase", lambda path: archive)
    monkeypatch.setattr("advisor.library_provider.LibraryKnowledgeProvider", factory)
    hits = QMDAdapter(tmp_path).search("mesh", limit=3)
    factory.assert_called_once_with(archive, tmp_path / "libraries")
    provider.search.assert_called_once_with("mesh", limit=3, filters={"mode": "qmd_keyword"})
    assert hits[0]["retrieval"]["backend"] == "qmd_keyword"
    assert not (tmp_path / "qmd").exists()
