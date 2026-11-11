"""User manuals follow text ingestion and retain their registered ownership."""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.library_search import LibrarySearch
from advisor.library_store import LibraryStore


@pytest.mark.parametrize('role', [None, 'docs', 'manuals', 'attachments'])
def test_custom_html_manual_defaults_off_builds_as_text_and_keeps_ownership_after_rebind(tmp_path, monkeypatch, role):
    sources = tmp_path / 'COMSOL official-looking documents'
    sources.mkdir()
    document = sources / 'guide.html'
    document.write_text('<html><head><title>My acoustic guide</title></head><body><h1>Boundary settings</h1>'
                        '<p>customacousticboundary is an independently supplied manual example.</p></body></html>', encoding='utf-8')
    (sources / 'unselected.mph').write_bytes(b'This MPH must never enter manual ingestion')
    root = {'id': 'manuals', 'path': str(sources)}
    if role is not None:
        root['role'] = role
    store = LibraryStore(tmp_path / 'data/libraries')
    library = store.register('My documents', 'user-manuals', [root], library_id='custom-manuals')
    assert library['enabled'] is False and store.searchable_library_ids() == []
    with pytest.raises(ValueError, match='关闭'):
        store.build(library['id'], modes=['native'])
    assert store.jobs() == []
    store.set_enabled(library['id'], True)
    def no_model(*args, **kwargs):
        pytest.fail('Custom manuals must never use MPH extraction or semantic enrichment')
    monkeypatch.setattr('advisor.library_model.extract_case', no_model)
    monkeypatch.setattr('advisor.compact_library.build_case', no_model)
    monkeypatch.setattr('advisor.library_enrichment.enrich_case', no_model)
    assert [record['relative_path'] for record in store.scan(library['id'])] == ['guide.html']
    assert store.model_candidates(library['id'])['status'] == 'not_applicable'
    built = store.build(library['id'], modes=['native'], model_format='compact')
    assert built['status'] == 'completed' and built['model_format'] == 'text'
    assert built['completed'] == 1 and not built['failed']
    search = LibrarySearch(store.root / 'search')
    hits = search.search('customacousticboundary', library_ids=[library['id']], mode='native')
    assert hits and all(hit['library_id'] == library['id'] and hit['origin'] == 'user-manuals' for hit in hits)
    assert all(hit['type'] == 'manual_topic' and hit['kind'] == 'manual' for hit in hits)
    scope = store.library_scope(type='user-manuals')
    assert scope['library_count'] == 1
    custom = scope['groups'][1]
    assert custom['libraries'][0]['id'] == library['id']
    assert [group['id'] for group in custom['groups']] == ['user-manuals', 'user-models']
    assert custom['groups'][0]['libraries'][0]['id'] == library['id']
    assert not any(group['libraries'] for group in scope['groups'][0]['groups'])
    citation = hits[0]['id']
    manifest = store.root / 'search/active.json'
    before = manifest.read_bytes(), document.read_bytes()
    moved = tmp_path / 'moved user documents'
    shutil.copytree(sources, moved)
    shutil.rmtree(sources)
    store.set_enabled(library['id'], False)
    rebound = store.rebind(library['id'], 'manuals', moved)
    assert rebound['type'] == 'user-manuals' and rebound['verified_evidence'] == 1
    assert rebound['enabled'] is False and search.search('customacousticboundary', library_ids=[library['id']]) == []
    store.set_enabled(library['id'], True)
    assert 'customacousticboundary' in store.read_evidence(citation)['text']
    assert (manifest.read_bytes(), (moved / 'guide.html').read_bytes()) == before


def test_custom_manual_scan_supports_document_extensions_but_excludes_mph(tmp_path):
    sources = tmp_path / 'documents'
    sources.mkdir()
    for name in ('guide.html', 'notes.htm', 'readme.md', 'notes.txt', 'report.pdf', 'skip.mph', 'image.png'):
        (sources / name).write_bytes(b'scan-only source fixture')
    store = LibraryStore(tmp_path / 'libraries')
    library = store.register('Custom documents', 'user-manuals', [{'path': str(sources)}])
    assert {record['relative_path'] for record in store.scan(library['id'])} == {
        'guide.html', 'notes.htm', 'readme.md', 'notes.txt', 'report.pdf'}
