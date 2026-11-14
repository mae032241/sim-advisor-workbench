from pathlib import Path

import pytest

from scripts import build_source_release as release


def test_only_source_is_exported_and_public_docs_override_local_notes(tmp_path):
    samples = {
        'run.py': b'print("ready")\n', 'advisor/service.py': b'# source\n',
        'README.md': b'local experiment results',
        'scripts/source_release_docs/README.md': b'public source instructions',
        'benchmarks/private/answer.json': b'private answer',
        'docs/evaluation-sets/comsol-diverse-v1.json': b'experiment selections',
        'scripts/geometry_challenge/rubric.json': b'private reference',
        'scripts/geometry_challenge/scenario.json': b'local scenario',
        'scripts/benchmark_product_report.py': b'local experiment commentary',
        'scripts/benchmark_product_replay.py': b'fixed local trial replay',
        'scripts/summarize_raw_address.py': b'local grading paths and result aggregation',
        'scripts/replay_case09_retrieval.py': b'fixed local retrieval targets',
        'data/index.sqlite3': b'index', 'advisor/model.mph': b'model',
        '.git/config': b'history', 'dist/offline.zip': b'offline bundle',
        'integrations/plugin.js': b'export default {}',
        'integrations/vendor/LICENSE': b'third-party license',
    }
    for name, value in samples.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    files, excluded = release.collect(tmp_path, tracked=list(samples))
    assert set(files) == {'run.py', 'advisor/service.py', 'README.md', 'integrations/plugin.js', 'integrations/vendor/LICENSE'}
    assert files['README.md'] == b'public source instructions'
    assert 'benchmarks/private/answer.json' in excluded


def test_real_credential_pattern_blocks_export_without_echoing_credential(tmp_path):
    target = tmp_path / 'run.py'
    secret = 'gh' + 'p_' + 'A' * 40
    target.write_text(secret, encoding='utf-8')
    with pytest.raises(ValueError, match='requires review') as error:
        release.collect(tmp_path, tracked=['run.py'])
    assert secret not in str(error.value)


def test_release_requires_new_dist_directory_and_never_replaces_source(tmp_path):
    with pytest.raises(ValueError, match='below the project dist'):
        release.build(tmp_path / 'advisor', root=tmp_path)
    destination = tmp_path / 'dist/existing'
    destination.mkdir(parents=True)
    (destination / 'keep.txt').write_text('keep', encoding='utf-8')
    with pytest.raises(ValueError, match='must be empty'):
        release.build(destination, root=tmp_path)
    assert (destination / 'keep.txt').read_text(encoding='utf-8') == 'keep'


def test_new_source_modules_are_discovered_without_temporary_data(tmp_path):
    names = ['advisor/new_module.py', 'web/new_view.js', 'tests/test_new_module.py',
             'scripts/build_new_feature.py', 'scripts/mph_extract/.actual-result.json',
             'integrations/host-test-live/session.json', 'advisor/data/corpus.json',
             'scripts/.test-output/report.json', '.test-temp/transcript.json',
             'benchmarks/private/answer.json', 'advisor/__pycache__/cached.py']
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    candidates = release.source_candidates(tmp_path)
    files, _ = release.collect(tmp_path, tracked=candidates)
    assert set(files) == set(names[:4])


def test_default_collect_includes_untracked_source(tmp_path, monkeypatch):
    path = tmp_path / 'advisor/new_module.py'
    path.parent.mkdir()
    path.write_bytes(b'# new source\n')
    monkeypatch.setattr(release.subprocess, 'run', lambda *a, **k: type('Result', (), {'stdout': b''})())
    files, _ = release.collect(tmp_path)
    assert files == {'advisor/new_module.py': b'# new source\n'}
