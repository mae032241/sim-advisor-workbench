"""Provider permission checks stay live without hydrating unrelated case data."""
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_provider import LibraryKnowledgeProvider
from advisor.library_store import LibraryStore, encoded
from advisor.tutorial_aliases import ensure_alias_schema
from test_library_query_provider import provider, backend
from test_library_search import obj


def test_case_scope_authorization_decodes_once_without_model_or_tutorial_hydration():
    value = provider()
    case = {'id': 'lo-case', 'type': 'model_case', 'library_id': 'manual', 'case_group': 'pinned'}
    value.store.get = Mock(return_value=case)
    value.store.read = Mock(side_effect=AssertionError('Scope authorization must not hydrate compact settings/tutorial links'))
    value.store.require_not_heldout = Mock()
    value.store.require_readable_library = Mock()
    engine = backend(Mock(return_value=[]))
    with patch('advisor.library_provider.LibrarySearch', return_value=engine):
        assert value.search('mesh', filters={'case_id': case['id'], 'stage': 'mesh'}) == []
    value.store.get.assert_called_once_with(case['id'])
    value.store.read.assert_not_called()
    assert value.store.require_readable_library.call_count == 2
    assert value.store.require_not_heldout.call_args_list[-1].args == (case['id'], case)


@pytest.mark.parametrize('gate', ['library', 'holdout'])
def test_case_scope_rechecks_fresh_gates_after_query_without_reading_body(gate):
    value = provider()
    case = {'id': 'lo-case', 'type': 'model_case', 'library_id': 'manual', 'case_group': 'pinned'}
    value.store.get = Mock(return_value=case)
    value.store.read = Mock(side_effect=AssertionError('No model hydration'))
    changed = [False]
    def check(*_args):
        if changed[0]:
            raise PermissionError('changed live gate')
    value.store.require_not_heldout = check if gate == 'holdout' else Mock()
    value.store.require_readable_library = check if gate == 'library' else Mock()
    def query(*_args, **_kwargs):
        changed[0] = True
        return []
    with patch('advisor.library_provider.LibrarySearch', return_value=backend(query)), pytest.raises(PermissionError, match='live gate'):
        value.search('mesh', filters={'case_id': case['id']})
    value.store.get.assert_called_once_with(case['id'])
    value.store.read.assert_not_called()


def catalog(tmp_path):
    store = LibraryStore(tmp_path / 'libraries')
    sources = tmp_path / 'sources'
    sources.mkdir()
    for library in ('open', 'closed'):
        store.register(library, 'user-models', [{'id': 'root', 'path': str(sources)}], library_id=library)
        store.set_enabled(library, library == 'open')
    return LibraryKnowledgeProvider(SimpleNamespace(get_document=lambda _identity: None), store.root)


def test_many_children_use_scalar_batches_and_one_visibility_snapshot(tmp_path, monkeypatch):
    value = catalog(tmp_path)
    children = [obj('child-' + str(index), 'open' if index % 3 else 'closed') for index in range(1201)]
    parent = obj('parent', 'open')
    parent['children'] = [{'id': child['id'], 'title': child['title']} for child in children] + [None, [], {'id': []}]
    with closing(value.store.connect()) as db, db:
        for source in [parent, *children]:
            db.execute('INSERT INTO revisions VALUES(?,?,?,?)', (source['id'], source['object_id'], source['library_id'], encoded(source)))
    get = value.store.get
    decoded = []
    def tracked(identity):
        decoded.append(identity)
        return get(identity)
    monkeypatch.setattr(value.store, 'get', tracked)
    monkeypatch.setattr(value, 'source_available', lambda *_args: pytest.fail('Ordinary registered children must use one batch, not N permission calls'))
    monkeypatch.setattr(value, '_libraries', lambda: pytest.fail('A source without HTML links must not scan library object counts'))
    searchable = value.store.searchable_library_ids
    checks = []
    def live(requested=None):
        checks.append(requested)
        return searchable(requested)
    monkeypatch.setattr(value.store, 'searchable_library_ids', live)
    document = value.get_document(parent['id'])
    assert [child['id'] for child in document['children']] == [child['id'] for child in children if child['library_id'] == 'open']
    assert decoded == [parent['id']]
    assert len(checks) <= 6


def test_batch_navigation_preserves_alias_and_retired_tutorial_permissions(tmp_path):
    value = catalog(tmp_path)
    canonical, retired = obj('canonical', 'closed'), obj('retired', 'open')
    alias = 'lo-old-alias'
    with closing(value.store.connect()) as db, db:
        for source in (canonical, retired):
            db.execute('INSERT INTO revisions VALUES(?,?,?,?)', (source['id'], source['object_id'], source['library_id'], encoded(source)))
        ensure_alias_schema(db)
        db.execute('INSERT INTO tutorial_aliases VALUES(?,?,?,?,?)', (alias, 'open', canonical['id'], 'closed', 'canonical'))
        db.execute('INSERT INTO tutorial_retired_sources VALUES(?,?,?,?)', (retired['id'], 'open', 'model_tutorial_copy', 'sha'))
        db.execute('INSERT INTO tutorial_associations VALUES(?,?,?,?,?,?,?)', (retired['id'], 'open', canonical['id'], 'closed', 'canonical_tutorial', 'sha', 'fixture'))
    refs = [alias, canonical['id'], retired['id']]
    assert value._available_navigation_ids(refs) == set()
    value.store.set_enabled('closed', True)
    assert value._available_navigation_ids(refs) == set(refs)
    value.store.set_enabled('open', False)
    assert value._available_navigation_ids(refs) == {canonical['id']}


def test_batch_navigation_rechecks_policy_each_call_and_never_decodes_children(tmp_path, monkeypatch):
    value = catalog(tmp_path)
    child = obj('child', 'open')
    with closing(value.store.connect()) as db, db:
        db.execute('INSERT INTO revisions VALUES(?,?,?,?)', (child['id'], child['object_id'], child['library_id'], encoded(child)))
    monkeypatch.setattr(value.store, 'get', lambda *_args: pytest.fail('Permission checks need only scalar ownership'))
    assert value._available_navigation_ids([child['id']]) == {child['id']}
    policy = HoldoutPolicy(object_ids=frozenset([child['object_id']]), token='new-holdout')
    monkeypatch.setattr(value.store, 'evaluation_policy', lambda: policy)
    assert value._available_navigation_ids([child['id']]) == set()


def test_public_case_authority_never_hydrates_and_enforces_full_holdout_and_lifecycle(tmp_path, monkeypatch):
    value = catalog(tmp_path)
    case = obj('case', 'open')
    case.update(type='model_case', case_group='immutable-group')
    with closing(value.store.connect()) as db, db:
        db.execute('INSERT INTO revisions VALUES(?,?,?,?)', (case['id'], case['object_id'], case['library_id'], encoded(case)))
    monkeypatch.setattr(value.store, 'read', lambda *_args: pytest.fail('Authority reads must not hydrate full source/navigation'))
    monkeypatch.setattr(value.store, '_compact_context', lambda *_args: pytest.fail('Authority reads must not open compact data'))
    assert value.get_case_authority(case['id']) == case
    policy = HoldoutPolicy(case_groups=frozenset([case['case_group']]), token='held-group')
    monkeypatch.setattr(value.store, 'evaluation_policy', lambda: policy)
    with pytest.raises(PermissionError):
        value.get_case_authority(case['id'])
    monkeypatch.setattr(value.store, 'evaluation_policy', lambda: HoldoutPolicy(token='clear'))
    value.store.set_enabled('open', False)
    with pytest.raises(PermissionError):
        value.get_case_authority(case['id'])


def test_public_case_authority_rejects_held_identity_before_reading_payload(tmp_path, monkeypatch):
    value = catalog(tmp_path)
    policy = HoldoutPolicy(citation_ids=frozenset(['lo-blocked']), token='held-id')
    monkeypatch.setattr(value.store, 'evaluation_policy', lambda: policy)
    monkeypatch.setattr(value.store, 'get', lambda *_args: pytest.fail('A blocked ID must not decode a source'))
    with pytest.raises(PermissionError):
        value.get_case_authority('lo-blocked')
