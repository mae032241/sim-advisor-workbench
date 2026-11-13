"""Scope reaches the retrieval engine; legacy manuals cannot bypass it."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from advisor.library_provider import LibraryKnowledgeProvider


def provider():
    value = object.__new__(LibraryKnowledgeProvider)
    value.legacy = SimpleNamespace(search=Mock(return_value=[{"id": "legacy", "kind": "manual"}]))
    value.store = SimpleNamespace(root=__import__('pathlib').Path('unused'), objects=lambda libraries: [1],
                                  evaluation_policy=lambda: SimpleNamespace(token='empty', document_relpaths=(), source_paths=()),
                                  searchable_library_ids=lambda requested=None: ["fixture"] if requested is None else [key for key in requested if key == "fixture"],
                                  current_generation=lambda: 'current')
    return value


def backend(hits):
    return SimpleNamespace(search=Mock(return_value=hits), query_snapshot=lambda: SimpleNamespace(status={
        'generation': 'current', 'modes': {'native': {'ready': True}, 'qmd_keyword': {'ready': True}}}))


def test_code_and_feature_scope_are_passed_before_retrieval_and_exclude_unclassified_legacy():
    value = provider()
    engine = backend([{"id": "mesh", "kind": "code", "library_id": "fixture"}])
    with patch("advisor.library_provider.LibrarySearch", return_value=engine):
        result = value.search("layer distribution", kind="code", filters={"mode": "qmd_keyword", "feature_branch": "mesh"})
    assert [item["id"] for item in result] == ["mesh"]
    assert engine.search.call_args.kwargs["feature_branch"] == "mesh"
    assert engine.search.call_args.kwargs["kind"] == "code"
    value.legacy.search.assert_not_called()


def test_invalid_feature_scope_is_not_silently_replaced_by_global_native_results():
    value = provider()
    with pytest.raises(ValueError):
        value.search("mesh", filters={"feature_branch": "invalid"})
    value.legacy.search.assert_not_called()


def test_unfiltered_legacy_mode_alias_only_uses_registered_qmd():
    value = provider()
    engine = backend([{"id": "modern", "kind": "case", "library_id": "fixture"}])
    with patch("advisor.library_provider.LibrarySearch", return_value=engine):
        result = value.search("heat")
    assert {item["id"] for item in result} == {"modern"}
    value.legacy.search.assert_not_called()
    assert engine.search.call_args.kwargs["mode"] == "qmd_keyword"
