from contextlib import closing
from unittest.mock import patch

import pytest

from advisor.library_store import LibraryStore
from advisor.library_search import LibrarySearch
from advisor.tutorial_catalog import refresh_links, links
from advisor.tutorial_migration import build_tutorial_migration_plan, apply_plan_to_catalog
from test_compact_model import mph


@pytest.mark.parametrize('model_first', [False, True])
def test_normal_builds_index_tutorial_once_and_resolve_case_references(tmp_path, model_first):
    docs = tmp_path / 'docs'
    package = docs / 'com.comsol.help.models.test.compact'
    package.mkdir(parents=True)
    html = package / 'compact.html'
    html.write_text('<html><title>Compact guide</title><body><h2 id="intro">Introduction</h2>'
                    '<p>OnlyCanonicalDescription stays in the document library.</p>'
                    '<h2>Modeling Instructions</h2><h3 id="mesh">Mesh</h3><p>Set hmax to 0.05.</p></body></html>', encoding='utf-8')
    pdf = package / 'models.test.compact.pdf'
    pdf.write_bytes(b'Unparseable PDF: it must be an alternate, never parsed here')
    model = mph(tmp_path / 'models/model.mph', '<Model><MeshSequence tag="mesh1"><MeshFeature tag="size" op="Size">'
                '<propertyValue name="p:custom" value="on"/><propertyValue name="p:hmax" value="0.05"/></MeshFeature></MeshSequence></Model>')
    store = LibraryStore(tmp_path / 'data/libraries')
    manuals = store.register('Manuals', 'official-manuals', [{'id': 'docs', 'path': str(docs), 'role': 'manuals'}])
    manuals = store.set_enabled(manuals["id"], True)
    models = store.register('Models', 'official-models', [{'id': 'models', 'path': str(model.parent), 'role': 'models'},
                                                        {'id': 'docs', 'path': str(docs), 'role': 'docs'}])
    models = store.set_enabled(models["id"], True)
    assert [r['path'] for r in store.scan(manuals['id'])] == [str(html)]
    assert [r['path'] for r in store.scan(manuals['id'], [pdf])] == [str(html)]
    for library in ([models, manuals] if model_first else [manuals, models]):
        result = store.build(library['id'], model_format='compact', deep=False, modes=['native'])
        assert result['status'] in {'completed', 'partial'}, result
    own = store.objects([models['id']])
    assert all(item['type'] not in {'case_context', 'method_step'} for item in own)
    case = next(item for item in own if item['type'] == 'model_case')
    read = store.read(case['id'])
    assert read['data']['tutorial_refs'] and read['data']['tutorial_links']
    with closing(store.connect()) as db:
        assert db.execute('SELECT count(*) FROM tutorial_associations WHERE source_id=?', (case['id'],)).fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM tutorial_document_sources').fetchone()[0] == len(store.objects([manuals['id']]))
    # Persist one document association while still browsing all its chapters.
    assert len(read['data']['tutorial_links']) > 1
    mesh = dict(case, data=dict(case['data'], feature_branch='mesh'))
    assert links(store, mesh, limit=1)[0]['feature_branch'] == 'mesh'
    assert all(item['library_id'] == manuals['id'] for item in read['data']['tutorial_links'])
    assert not any(item['asset_key'].endswith('.pdf') for item in store.objects([manuals['id']]))
    search = LibrarySearch(store.root / 'search')
    assert search.search('OnlyCanonicalDescription', library_ids=[manuals['id']])
    assert not search.search('OnlyCanonicalDescription', library_ids=[models['id']])
    assert search.search('hmax', library_ids=[models['id']])
    store.set_enabled(manuals['id'], False)
    assert store.read(case['id'])['data']['tutorial_links'] == []
    assert not store.related_sources(case['id'])


def test_old_model_tutorial_id_cannot_bypass_disabled_canonical_owner(tmp_path):
    from test_tutorial_migration import fixture
    store, _, cases, copies, _ = fixture(tmp_path)
    plan = build_tutorial_migration_plan(store, tmp_path / 'plan.sqlite3')
    with closing(store.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        apply_plan_to_catalog(db, plan)
        refresh_links(db)
    old = copies[0]
    assert store.source_available(old['id'])
    assert store.related_sources(cases[0]['id'])
    store.set_enabled('cs63-documents', False)
    assert not store.source_available(old['id'])
    with pytest.raises(PermissionError, match='统一文档库'):
        store.read(old['id'])
    assert not store.related_sources(cases[0]['id'])
    assert store.get(old['id']) is not None  # Internal archive remains intact.


def test_document_owner_closing_during_archived_read_is_rechecked(tmp_path):
    from test_tutorial_migration import fixture
    store, _, _, copies, _ = fixture(tmp_path)
    plan = build_tutorial_migration_plan(store, tmp_path / 'plan.sqlite3')
    with closing(store.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        apply_plan_to_catalog(db, plan)
        refresh_links(db)
    def closes_during_hydration(_source):
        store.set_enabled('cs63-documents', False)
        return None
    with patch.object(store, '_compact_context', side_effect=closes_during_hydration):
        with pytest.raises(PermissionError, match='统一文档库'):
            store.read(copies[0]['id'])
