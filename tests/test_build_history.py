from contextlib import closing
import copy
import json

import pytest

from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.build_history import compact_build_history, compact_job_indexes
from advisor.library_store import LibraryStore, encoded


def legacy_job():
    return {'id': 'a' * 32, 'status': 'partial', 'stage': 'finished', 'library_ids': ['models'],
            'items': [{'path': 'model.mph', 'status': 'partial', 'error': 'unresolved selection'}],
            'failed': [{'path': 'large.mph', 'error': 'bounded metadata limit'}],
            'indexes': {'generation': 'same-generation', 'object_count': 2,
                        'mapping': {'a.md': 'citation-a', 'b.md': 'citation-b'},
                        'representation': {'indexed_characters': 20, 'objects': {'citation-a': {'size': 10}, 'citation-b': {'size': 10}}},
                        'modes': {'native': {'ready': True}},
                        'last_build': {'mapping': {'a.md': 'citation-a'}}}}


def test_history_projection_keeps_outcomes_without_bulk_or_input_mutation():
    job = legacy_job()
    before = copy.deepcopy(job)
    result = compact_job_indexes(job)
    assert job == before and result['items'] == before['items'] and result['failed'] == before['failed']
    assert result['indexes']['generation'] == 'same-generation'
    assert result['indexes']['representation'] == {'indexed_characters': 20}
    assert 'mapping' not in result['indexes'] and 'mapping' not in result['indexes']['last_build']
    assert compact_job_indexes(result) == result


def test_new_saved_jobs_and_offline_history_do_not_duplicate_index_metadata(tmp_path):
    store = LibraryStore(tmp_path / 'libraries')
    job = legacy_job()
    store._save_job(job)
    assert 'mapping' not in store.job(job['id'])['indexes']
    raw = encoded(legacy_job())
    with closing(store.connect()) as db, db:
        db.execute('UPDATE builds SET body=? WHERE id=?', (raw, job['id']))
        db.execute('INSERT INTO revisions VALUES(?,?,?,?)', ('citation', 'object', 'models', '  {"retained": true}  '))
    result = compact_build_history(store.db_path)
    assert result['changed_jobs'] == 1 and result['removed_json_bytes'] > 0
    with closing(store.connect()) as db:
        assert db.execute('SELECT payload FROM revisions').fetchone()[0] == '  {"retained": true}  '
        assert json.loads(db.execute('SELECT body FROM builds').fetchone()[0])['failed'] == legacy_job()['failed']
    assert compact_build_history(store.db_path)['changed_jobs'] == 0


def test_history_cancel_rolls_back_diagnostic_changes(tmp_path):
    store = LibraryStore(tmp_path / 'libraries')
    raw = encoded(legacy_job())
    with closing(store.connect()) as db, db:
        db.execute('INSERT INTO builds VALUES(?,?)', ('a' * 32, raw))
    marker = tmp_path / 'cancel'
    with build_control(BuildControl(marker)):
        with pytest.raises(BuildCancelled):
            compact_build_history(store.db_path, on_progress=lambda _: marker.touch())
    with closing(store.connect()) as db:
        assert db.execute('SELECT body FROM builds').fetchone()[0] == raw
