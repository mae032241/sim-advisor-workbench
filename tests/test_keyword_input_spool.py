"""Keyword text exists only during export and is erased before embedding."""
from contextlib import closing
from pathlib import Path
import sqlite3
import threading

import pytest

from advisor import library_search as module
from advisor.build_control import BuildCancelled, BuildControl, build_control, check_cancelled
from advisor.object_index import qmd_path, representation_stats
from advisor.library_store import LibraryStore, encoded
from advisor.qmd_shared import build_keyword_index, keyword_input_spool
from test_library_search import FakeEngineSearch, obj


def spools(root):
    return list(Path(root).rglob('.qmd-keyword-inputs-*'))


@pytest.mark.parametrize('sealed', [False, True])
def test_temporary_inputs_disappear_when_context_fails(tmp_path, sealed):
    with pytest.raises(RuntimeError, match='injected'):
        with keyword_input_spool(tmp_path) as inputs:
            item = obj('precise')
            inputs.add(item['library_id'], qmd_path(item), item['title'], 'exact source revision')
            if sealed:
                inputs.seal()
            assert inputs.path.exists()
            raise RuntimeError('injected')
    assert not spools(tmp_path)


def test_only_sealed_temporary_inputs_are_accepted_before_starting_sdk(tmp_path, monkeypatch):
    import advisor.qmd_shared as shared
    monkeypatch.setattr(shared, '_run', lambda *_args, **_kwargs: pytest.fail('Unsealed inputs must not start Node'))
    item = obj('one')
    with keyword_input_spool(tmp_path) as inputs:
        inputs.add(item['library_id'], qmd_path(item), item['title'], 'temporary')
        with pytest.raises(ValueError, match='sealed temporary'):
            build_keyword_index(inputs.path, tmp_path / 'out.sqlite3', qmd_dir=tmp_path, node=tmp_path)
        with pytest.raises(ValueError, match='Invalid temporary'):
            inputs.add('foreign', qmd_path(item), item['title'], 'wrong library')
        inputs.seal()
        with pytest.raises(ValueError, match='already sealed'):
            inputs.add(item['library_id'], qmd_path(item), item['title'], 'late write')
    assert not spools(tmp_path)


class CaptureInputs(FakeEngineSearch):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exported = []
        self.input_paths = []

    def _build_keyword(self, inputs_path, *args, **kwargs):
        self.input_paths.append(Path(inputs_path))
        with closing(sqlite3.connect(inputs_path)) as db:
            self.exported.append(db.execute('SELECT library_id,qmd_path,title,text FROM keyword_inputs ORDER BY qmd_path').fetchall())
        return super()._build_keyword(inputs_path, *args, **kwargs)


def test_new_build_and_rebuild_export_exact_revisions_and_remove_text_before_embedding(tmp_path, monkeypatch):
    search = CaptureInputs(tmp_path / 'search', preserve_history=True)
    first = obj('first', text='first precise revision keyword canary')
    latest = obj('latest', text='latest precise revision keyword canary')
    represented = []
    representation = module._representation
    def tracked(item):
        represented.append(item['id'])
        return representation(item)
    monkeypatch.setattr(module, '_representation', tracked)
    def observe(request):
        if request['op'] in {'update', 'embed', 'vector'}:
            assert not spools(search.data_dir), 'Keyword text must be erased before embedding starts'
    search.observer = observe
    for generation, item in (('one', first), ('two', latest)):
        built = search.build([item], modes=['qmd_keyword', 'qmd_vector'], generation=generation)
        assert built['modes']['qmd_keyword']['ready'] and built['modes']['qmd_vector']['ready']
        assert search.exported[-1] == [(item['library_id'], qmd_path(item), item['title'], representation(item)[0])]
        with closing(sqlite3.connect(Path(built['directory']) / 'objects.sqlite3')) as db:
            assert 'search_input' not in {row[1] for row in db.execute('PRAGMA table_info(objects)')}
        with closing(sqlite3.connect(Path(built['directory']) / 'qmd-keyword.sqlite3')) as db:
            body = db.execute('SELECT doc FROM content').fetchone()[0]
            assert item['text'] in body
        assert not spools(search.data_dir)
    assert represented == [first['id'], latest['id']]
    assert all(not path.exists() for path in search.input_paths)


@pytest.mark.parametrize('phase', ['export', 'keyword'])
def test_cancelled_build_erases_temporary_keyword_text_and_releases_lease(tmp_path, phase):
    event = threading.Event()
    class Cancelled(CaptureInputs):
        def _build_keyword(self, inputs_path, *args, **kwargs):
            assert Path(inputs_path).exists()
            if phase == 'keyword':
                event.set()
                check_cancelled()
            return super()._build_keyword(inputs_path, *args, **kwargs)
    search = Cancelled(tmp_path / 'search')
    def progress(info):
        if phase == 'export' and info['stage'] == 'indexing_objects' and info['progress']['completed'] == 1:
            event.set()
    with build_control(BuildControl(tmp_path / 'cancel', event)), pytest.raises(BuildCancelled):
        search.build([obj('one')], modes=['qmd_keyword'], generation='cancel', on_progress=progress)
    assert not spools(search.data_dir)
    assert not search.lock.locked() and not (search.data_dir / 'build-lease.json').exists()


@pytest.mark.parametrize('phase', ['export', 'keyword'])
def test_failed_build_erases_temporary_keyword_text(tmp_path, monkeypatch, phase):
    class Fails(CaptureInputs):
        def _build_keyword(self, inputs_path, *args, **kwargs):
            assert Path(inputs_path).exists()
            raise RuntimeError('injected keyword failure')
    search = Fails(tmp_path / 'search')
    if phase == 'export':
        def failed_export(item):
            assert spools(search.data_dir)
            raise RuntimeError('injected export failure')
        monkeypatch.setattr(module, '_representation', failed_export)
        with pytest.raises(RuntimeError, match='injected export'):
            search.build([obj('one')], modes=['qmd_keyword'], generation='failed')
    else:
        result = search.build([obj('one')], modes=['qmd_keyword'], generation='failed')
        assert not result['ready'] and result['modes']['qmd_keyword']['state'] == 'failed'
    assert not spools(search.data_dir)
    assert not search.lock.locked() and not (search.data_dir / 'build-lease.json').exists()


def test_managed_build_uses_active_exact_revision_and_stores_only_catalog_reference(tmp_path):
    store = LibraryStore(tmp_path / 'libraries')
    older = obj('older', text='selected older revision body')
    newer = obj('newer', text='newer catalog revision body')
    older['object_id'] = newer['object_id'] = 'same-logical-object'
    with closing(store.connect()) as db, db:
        for item in (older, newer):
            db.execute('INSERT INTO revisions VALUES(?,?,?,?)', (item['id'], item['object_id'], item['library_id'], encoded(item)))
        db.execute('INSERT INTO active VALUES(?,?,?)', (older['library_id'], older['object_id'], older['id']))
    search = CaptureInputs(store.root / 'search', preserve_history=True)
    for generation, item in (('selected-old', older), ('selected-new', newer)):
        with store.object_stream() as source:
            built = search.build(source, modes=['qmd_keyword'], generation=generation)
            assert not source._cursors
        assert built['modes']['qmd_keyword']['ready']
        assert search.exported[-1] == [(item['library_id'], qmd_path(item), item['title'], module._representation(item)[0])]
        assert built['mapping'] == {} and built['representation']['objects'] == {}
        with closing(sqlite3.connect(Path(built['directory']) / 'objects.sqlite3')) as db:
            assert db.execute('SELECT id,source_json FROM objects').fetchall() == [(item['id'], None)]
            assert 'search_input' not in {row[1] for row in db.execute('PRAGMA table_info(objects)')}
            assert representation_stats(db, item['id']) == module._representation(item)[1]
        assert not spools(search.data_dir)
        with closing(store.connect()) as db, db:
            db.execute('UPDATE active SET citation_id=?', (newer['id'],))
