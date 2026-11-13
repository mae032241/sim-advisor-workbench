"""One QMD backend preserves scope and bounds hydrated metadata-filter pools."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from advisor.library_provider import LibraryKnowledgeProvider


def provider():
    value = object.__new__(LibraryKnowledgeProvider)
    value.store = SimpleNamespace(root=Path('unused'), has_objects=lambda ids: True,
        searchable_library_ids=lambda ids=None: ['manual'] if ids is None else [x for x in ids if x == 'manual'],
        current_generation=lambda: 'current')
    value.legacy = SimpleNamespace(search=Mock(return_value=[]))
    value._legacy_exclusions = lambda: (False, [])
    return value


def backend(search):
    return SimpleNamespace(search=search, query_snapshot=Mock(return_value=SimpleNamespace(status={
        'generation': 'current', 'modes': {'native': {'ready': True}, 'qmd_keyword': {'ready': True}}})))


@pytest.mark.parametrize('mode,scope,post,expected', [
    ('native', True, {}, 6),
    ('native', True, {'version': '6.3'}, 40),
    ('native', True, {'package': 'acdc'}, 40),
    ('native', False, {}, 6),
    ('qmd_keyword', True, {}, 6),
    ('qmd_keyword', True, {'version': '6.3'}, 40),
])
def test_hydration_limit_preserves_fusion_and_postfilter_candidates(mode, scope, post, expected):
    value = provider()
    engine = backend(Mock(return_value=[]))
    filters = {'mode': mode, **post}
    if scope:
        filters['library_ids'] = ['manual']
    with patch('advisor.library_provider.LibrarySearch', return_value=engine):
        assert value.search('electromagnetic', kind='manual', limit=6, filters=filters) == []
    assert engine.search.call_args.kwargs['limit'] == expected
    engine.query_snapshot.assert_called_once_with()
    assert engine.search.call_args.kwargs['snapshot'] is engine.query_snapshot.return_value
    assert engine.search.call_args.kwargs['mode'] == 'qmd_keyword'
    assert engine.search.call_args.kwargs['candidate_limit'] == 40
    value.legacy.search.assert_not_called()


def test_metadata_filter_still_reaches_valid_candidates_beyond_first_six():
    value = provider()
    objects = [{'id': str(i), 'kind': 'manual', 'library_id': 'manual', 'version': '6.2'} for i in range(8)]
    objects.append({'id': 'valid', 'kind': 'manual', 'library_id': 'manual', 'version': '6.3'})
    engine = backend(Mock(return_value=objects))
    with patch('advisor.library_provider.LibrarySearch', return_value=engine):
        result = value.search('electric', limit=6, filters={'library_ids': ['manual'], 'version': '6.3'})
    assert [item['id'] for item in result] == ['valid']


def test_closed_during_search_is_filtered_from_final_output():
    value = provider()
    enabled = True
    value.store.searchable_library_ids = lambda ids=None: ['manual'] if enabled else []
    def close_during_search(*args, **kwargs):
        nonlocal enabled
        enabled = False
        return [{'id': 'stale', 'kind': 'manual', 'library_id': 'manual'}]
    engine = backend(close_during_search)
    with patch('advisor.library_provider.LibrarySearch', return_value=engine):
        assert value.search('electric', filters={'library_ids': ['manual']}) == []


def test_stale_generation_is_rejected_before_search():
    value = provider()
    value.store.current_generation = lambda: 'changed'
    engine = backend(Mock(return_value=[]))
    with patch('advisor.library_provider.LibrarySearch', return_value=engine):
        with pytest.raises(RuntimeError, match='stale'):
            value.search('electric', filters={'library_ids': ['manual']})
    engine.search.assert_not_called()


def test_unscoped_query_never_calls_or_merges_a_legacy_backend():
    value = provider()
    value.legacy.search.return_value = [{'id': 'legacy'}]
    engine = backend(Mock(return_value=[{'id': 'qmd', 'kind': 'manual', 'library_id': 'manual'}]))
    with patch('advisor.library_provider.LibrarySearch', return_value=engine):
        assert [x['id'] for x in value.search('mesh')] == ['qmd']
    value.legacy.search.assert_not_called()


def test_unavailable_qmd_cannot_fall_back_to_retired_native():
    value = provider()
    engine = backend(Mock(return_value=[]))
    engine.query_snapshot.return_value.status['modes'] = {'native': {'ready': True}}
    with patch('advisor.library_provider.LibrarySearch', return_value=engine):
        with pytest.raises(RuntimeError, match='QMD index'):
            value.search('mesh', filters={'mode': 'native'})
    value.legacy.search.assert_not_called()
    engine.search.assert_not_called()
