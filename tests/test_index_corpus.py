from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.index_corpus import cleanup_generated_corpus
from advisor.index_paths import SHARED_KEYWORD_STORAGE, serialize_index_manifest
from advisor.native_index import create_schema, insert_object, qmd_path


def fixture(tmp_path):
    root = tmp_path / 'data/libraries/search'
    generation = root / 'generations/abc123'
    generation.mkdir(parents=True)
    libraries = {}
    files = []
    with closing(sqlite3.connect(generation / 'objects.sqlite3')) as db, db:
        create_schema(db)
        for n in range(3):
            item = {'id': f'source-{n}', 'library_id': 'fixture', 'kind': 'manual', 'title': f'Example {n}', 'text': 'hmax mesh'}
            insert_object(db, item, item['text'], {}, '', standalone=True)
            path = qmd_path(item)
            corpus = generation / Path(path).parent / 'corpus'
            corpus.mkdir(parents=True, exist_ok=True)
            file = corpus / Path(path).name
            file.write_text('# ' + item['title'] + '\n\n' + item['text'], encoding='utf-8')
            files.append(file)
        key = Path(path).parts[0]
        keyword = generation / key / 'qmd-keyword.sqlite3'
        keyword.write_bytes(b'persisted keyword DB fixture')
        libraries['fixture'] = {'key': key, 'corpus': str(corpus), 'db_path': str(keyword)}
    return root, {'directory': str(generation), 'libraries': libraries, 'modes': {'qmd_keyword': {'ready': True}}}, files


def test_cleanup_preserves_databases_and_is_idempotent(tmp_path):
    root, manifest, files = fixture(tmp_path)
    database = Path(manifest['directory']) / 'objects.sqlite3'
    keyword = Path(manifest['libraries']['fixture']['db_path'])
    before = database.read_bytes(), keyword.read_bytes()
    size = sum(p.stat().st_size for p in files)
    result = cleanup_generated_corpus(root, manifest)
    assert result['removed_files'] == 3 and result['removed_bytes'] == size
    assert not any(p.exists() for p in files)
    assert (database.read_bytes(), keyword.read_bytes()) == before
    assert cleanup_generated_corpus(root, manifest)['removed_files'] == 0


def test_cleanup_resolves_relative_shared_keyword_database_once_for_all_exports(tmp_path):
    root, manifest, files = fixture(tmp_path)
    item = manifest['libraries']['fixture']
    shared = Path(manifest['directory']) / 'qmd-keyword.sqlite3'
    Path(item['db_path']).replace(shared)
    item.update(db_path=str(shared), collection=item['key'])
    manifest['keyword_storage'] = SHARED_KEYWORD_STORAGE
    stored = serialize_index_manifest(manifest, root)
    result = cleanup_generated_corpus(root, stored)
    assert result['removed_files'] == 3 and shared.is_file()
    assert cleanup_generated_corpus(root, stored)['removed_files'] == 0
    assert not any(path.exists() for path in files)


def test_vector_only_cleanup_does_not_require_an_unrequested_shared_keyword_database(tmp_path):
    root, manifest, files = fixture(tmp_path)
    item = manifest['libraries']['fixture']
    vector = Path(item['db_path']).with_name('qmd-vector.sqlite3')
    Path(item['db_path']).replace(vector)
    item.update(vector_db_path=str(vector), db_path=str(Path(manifest['directory']) / 'qmd-keyword.sqlite3'),
                collection=item['key'])
    manifest.update(keyword_storage=SHARED_KEYWORD_STORAGE,
                    modes={'qmd_keyword': {'ready': False}, 'qmd_vector': {'ready': True}})
    assert cleanup_generated_corpus(root, manifest)['removed_files'] == 3
    assert vector.is_file() and not Path(item['db_path']).exists()
    assert not any(path.exists() for path in files)


def test_unowned_entries_refuse_before_any_deletion(tmp_path):
    root, manifest, files = fixture(tmp_path)
    unknown = files[0].parent / 'personal-notes.md'
    unknown.write_text('Preserve this', encoding='utf-8')
    with pytest.raises(ValueError, match='Unexpected/unowned'):
        cleanup_generated_corpus(root, manifest)
    assert all(p.is_file() for p in [*files, unknown])


def test_registered_sources_and_unready_keyword_are_preserved(tmp_path):
    root, manifest, files = fixture(tmp_path)
    manifest['modes']['qmd_keyword']['ready'] = False
    assert cleanup_generated_corpus(root, manifest)['status'] == 'not_ready'
    assert all(p.is_file() for p in files)
    manifest['modes']['qmd_keyword']['ready'] = True
    catalog = root.parent / 'catalog.sqlite3'
    with closing(sqlite3.connect(catalog)) as db, db:
        db.execute('CREATE TABLE libraries(id TEXT,body TEXT)')
        db.execute('INSERT INTO libraries VALUES(?,?)', ('source', json.dumps({'roots': [{'path': str(files[0].parent)}]})))
    with pytest.raises(ValueError, match='overlaps a registered source'):
        cleanup_generated_corpus(root, manifest)
    assert all(p.is_file() for p in files)


def test_cancel_after_validation_leaves_exports_and_retry_finishes(tmp_path):
    root, manifest, files = fixture(tmp_path)
    marker = tmp_path / 'cancel'
    with build_control(BuildControl(marker)):
        with pytest.raises(BuildCancelled):
            cleanup_generated_corpus(root, manifest, on_progress=lambda _: marker.touch())
    assert all(p.is_file() for p in files)
    marker.unlink()
    files[0].unlink()  # A prior interrupted cleanup may already remove some.
    assert cleanup_generated_corpus(root, manifest)['removed_files'] == 2
