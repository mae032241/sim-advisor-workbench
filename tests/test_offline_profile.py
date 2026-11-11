import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from collections import OrderedDict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.library_search import LibrarySearch, _hash
from advisor.offline_profile import load_offline_profile, local_model_path
from advisor.retrieval_backends import QMDAdapter, qmd_command
from test_library_search import enable_test_embedding


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in ('SIM_ADVISOR_QMD_DIR', 'SIM_ADVISOR_QMD', 'SIM_ADVISOR_NODE',
                'SIM_ADVISOR_EMBED_MODEL', 'SIM_ADVISOR_SEARCH_CACHE'):
        monkeypatch.delenv(key, raising=False)


def profile_fixture(tmp_path):
    bundle = tmp_path / 'read-only offline bundle'
    qmd = bundle / 'qmd/node_modules/@tobilu/qmd'
    (qmd / 'dist/cli').mkdir(parents=True)
    (qmd / 'dist/index.js').write_text('// SDK path fixture')
    (qmd / 'dist/cli/qmd.js').write_text('// CLI path fixture')
    node = bundle / 'node/node.exe'
    node.parent.mkdir()
    node.write_bytes(b'node path fixture')
    model = bundle / 'models/embedding.gguf'
    model.parent.mkdir()
    model.write_bytes(b'GGUFmodel-file-fixture-not-actual-weights')
    data = tmp_path / 'selected data'
    data.mkdir()
    value = {'schema': 'sim-advisor-offline-dependencies/v1', 'bundleRoot': str(bundle),
             'paths': {'nodePath': 'node/node.exe', 'qmdDir': 'qmd/node_modules/@tobilu/qmd',
                       'embedModelPath': 'models/embedding.gguf'}, 'externalPaths': {}}
    (data / 'dependency-profile.json').write_text(json.dumps(value), encoding='utf-8')
    enable_test_embedding(data / 'libraries/search', model)
    return data, bundle, node, qmd, model


def test_selected_library_descendant_resolves_its_own_profile_and_cache(tmp_path):
    data, bundle, node, qmd, model = profile_fixture(tmp_path)
    search = LibrarySearch(data / 'libraries/search')
    assert search.node == str(node.resolve())
    assert search.qmd_dir == qmd.resolve()
    assert search.model_uri == str(model.resolve())
    assert search.cache_dir == data / 'cache/library-search'
    status = search.dependency_status()
    assert status['profile']['path'] == str(data / 'dependency-profile.json')
    assert status['sources']['node'] == 'profile:nodePath'
    assert status['sources']['embed_model'] == 'settings:model_path'
    assert status['download_policy'] == 'local-only'
    assert status['embedding_model']['sha256'] == hashlib.sha256(model.read_bytes()).hexdigest()
    assert not (data / 'cache').exists(), 'Read-only resolution must not write a receipt'


def test_explicit_arguments_then_environment_override_profile_without_fallback(tmp_path, monkeypatch):
    data, bundle, node, qmd, model = profile_fixture(tmp_path)
    alternate = tmp_path / 'explicit node.exe'
    alternate.write_bytes(b'fixture')
    monkeypatch.setenv('SIM_ADVISOR_NODE', str(alternate))
    assert LibrarySearch(data / 'libraries/search').node == str(alternate.resolve())
    assert LibrarySearch(data / 'libraries/search', node=node).node == str(node.resolve())
    missing = tmp_path / 'explicit missing.exe'
    monkeypatch.setenv('SIM_ADVISOR_NODE', str(missing))
    search = LibrarySearch(data / 'libraries/search')
    assert search.node is None
    assert str(missing) in '; '.join(search.dependencies['errors'])
    assert qmd_command(data) is None


def test_explicit_other_data_never_borrows_a_default_or_environment_data_profile(tmp_path, monkeypatch):
    data, *_ = profile_fixture(tmp_path)
    monkeypatch.setenv('SIM_ADVISOR_DATA', str(data))
    monkeypatch.setenv('LOCALAPPDATA', str(data))
    other = tmp_path / 'another data' / 'libraries/search'
    resolved = LibrarySearch(other).dependency_status()
    assert not resolved['profile']['present']
    assert resolved['sources']['qmd_dir'] == 'default'


def test_legacy_keyword_adapter_consumes_the_same_associated_profile(tmp_path):
    data, _, node, qmd, model = profile_fixture(tmp_path)
    assert qmd_command(data) == [str(node.resolve()), str(qmd / 'dist/cli/qmd.js')]
    adapter = QMDAdapter(data)
    assert adapter.command == qmd_command(data)
    assert adapter._env()['QMD_EMBED_MODEL'] == str(model.resolve())
    assert adapter._env()['QMD_LLAMA_GPU'] == 'false'


def test_native_file_uri_is_normalized_and_model_receipts_are_per_file(tmp_path, monkeypatch):
    first = tmp_path / 'embedding %20 first.gguf'
    second = tmp_path / 'embedding second.gguf'
    first.write_bytes(b'GGUFfirst-fixture')
    second.write_bytes(b'GGUFsecond-fixture')
    assert local_model_path(first.as_uri()) == first.resolve()
    cache = tmp_path / 'receipts'
    enable_test_embedding(tmp_path / 'search', first)
    search = LibrarySearch(tmp_path / 'search', embed_model=first.as_uri(), cache_dir=cache)
    monkeypatch.setattr(search, '_engine', lambda *_args, **_kwargs: pytest.fail('Local file must never invoke pull'))
    a = search.prepare_embedding()
    assert a['path'] == str(first.resolve())
    b = LibrarySearch(tmp_path / 'search', embed_model=second, cache_dir=cache).prepare_embedding()
    assert a['sha256'] != b['sha256']
    assert (cache / ('embedding-' + _hash(str(first.resolve()))[:16] + '.json')).exists()
    assert (cache / ('embedding-' + _hash(str(second.resolve()))[:16] + '.json')).exists()
    state = search.status()
    assert state['dependencies']['embedding_model']['available']
    assert not state['modes']['qmd_vector']['ready'], 'A GGUF/receipt is not an embedding index'


def test_missing_offline_model_and_broken_profile_never_trigger_download(tmp_path, monkeypatch):
    data, _, _, _, model = profile_fixture(tmp_path)
    model.unlink()
    search = LibrarySearch(data / 'libraries/search')
    monkeypatch.setattr(search, '_engine', lambda *_args, **_kwargs: pytest.fail('No download may be attempted'))
    with pytest.raises(RuntimeError, match='no download attempted'):
        search.prepare_embedding()
    (data / 'dependency-profile.json').write_text('{invalid', encoding='utf-8')
    broken = LibrarySearch(data / 'libraries/search')
    assert broken.dependencies['profile']['present'] and broken.dependencies['errors']
    monkeypatch.setattr(broken, '_engine', lambda *_args, **_kwargs: pytest.fail('No fallback/download may be attempted'))
    with pytest.raises(RuntimeError, match='no download attempted'):
        broken.prepare_embedding()
    assert broken.node is None


def test_remote_env_does_not_silently_download_in_an_associated_offline_profile(tmp_path, monkeypatch):
    data, _, _, _, model = profile_fixture(tmp_path)
    monkeypatch.setenv('SIM_ADVISOR_EMBED_MODEL', 'hf:someone/explicit-other-model.gguf')
    search = LibrarySearch(data / 'libraries/search')
    assert search.model_uri == str(model.resolve()), 'Manual model selection takes precedence over ambient environment'
    monkeypatch.setattr(search, '_engine', lambda *_args, **_kwargs: pytest.fail('Offline profile forbids pull'))
    assert search.prepare_embedding()['available']


def test_profile_cannot_escape_bundle_without_explicit_external_paths(tmp_path):
    data, *_ = profile_fixture(tmp_path)
    path = data / 'dependency-profile.json'
    value = json.loads(path.read_text())
    value['paths']['nodePath'] = '../outside.exe'
    path.write_text(json.dumps(value))
    profile = load_offline_profile(data / 'libraries/search')
    assert 'nodePath' not in profile['paths']
    assert any('escapes bundleRoot' in error for error in profile['errors'])


def test_cli_runtime_uses_explicit_library_root_and_does_not_create_catalog(tmp_path):
    data, _, node, qmd, model = profile_fixture(tmp_path)
    library = data / 'libraries'
    script = Path(__file__).resolve().parents[1] / 'library_cli.py'
    environment = dict(os.environ)
    for key in ('SIM_ADVISOR_QMD_DIR', 'SIM_ADVISOR_NODE', 'SIM_ADVISOR_EMBED_MODEL', 'SIM_ADVISOR_SEARCH_CACHE'):
        environment.pop(key, None)
    completed = subprocess.run([sys.executable, '-B', str(script), '--data-dir', str(library), 'runtime'],
        capture_output=True, text=True, encoding='utf-8', env=environment, timeout=15)
    assert completed.returncode == 0, completed.stderr
    value = json.loads(completed.stdout)
    assert value['node'] == str(node.resolve())
    assert value['qmd_dir'] == str(qmd.resolve())
    assert value['embedding_model']['path'] == str(model.resolve())
    assert not library.exists()


def test_cli_index_default_is_native_keyword_and_requested_mode_failure_is_nonzero(tmp_path, monkeypatch, capsys):
    import library_cli
    requested = []

    class Store:
        def __init__(self, path):
            self.root = path

        def reindex(self, modes):
            requested.append(modes)
            return {'modes': {mode: {'ready': mode != 'qmd_vector', 'error': 'deliberate fixture failure' if mode == 'qmd_vector' else None} for mode in modes}}

    monkeypatch.setattr(library_cli, 'LibraryStore', Store)
    monkeypatch.setattr(sys, 'argv', ['library_cli.py', '--data-dir', str(tmp_path), 'index'])
    assert library_cli.main() == 0
    assert requested[-1] == ['native', 'qmd_keyword']
    capsys.readouterr()
    monkeypatch.setattr(sys, 'argv', ['library_cli.py', '--data-dir', str(tmp_path), 'index', '--modes', 'native,qmd_vector'])
    assert library_cli.main() == 1
    assert requested[-1] == ['native', 'qmd_vector']
    assert json.loads(capsys.readouterr().out)['modes']['qmd_vector']['ready'] is False


def test_removed_node_after_resolution_is_reported_and_never_replaced(tmp_path):
    data, _, node, _, _ = profile_fixture(tmp_path)
    search = LibrarySearch(data / 'libraries/search')
    node.unlink()
    assert not search.dependency_status()['qmd_available']
    with pytest.raises(RuntimeError, match='QMD SDK/Node unavailable') as error:
        search._engine({'op': 'update'})
    assert str(node) in str(error.value)


def test_fresh_status_instances_share_model_hash_without_diagnostic_writes(tmp_path, monkeypatch):
    from advisor import library_search as module
    data, _, _, _, model = profile_fixture(tmp_path)
    monkeypatch.setattr(module, '_MODEL_HASH_CACHE', OrderedDict())
    original = module._hash_model_file
    reads = []

    def counted(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(module, '_hash_model_file', counted)
    hashes = [LibrarySearch(data / 'libraries/search').status()['dependencies']['embedding_model']['sha256'] for _ in range(5)]
    assert len(set(hashes)) == 1
    assert reads == [], 'The identity verified when enabling is reused without hashing again'
    assert not (data / 'cache').exists(), 'Status polling must not create model receipts'
    assert not (data / 'libraries').exists(), 'Status polling must not create an index'


def test_changed_model_stat_invalidates_shared_hash_and_cache_is_bounded(tmp_path, monkeypatch):
    from advisor import library_search as module
    monkeypatch.setattr(module, '_MODEL_HASH_CACHE', OrderedDict())
    monkeypatch.setattr(module, '_MODEL_HASH_CACHE_LIMIT', 2)
    original = module._hash_model_file
    reads = []

    def counted(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(module, '_hash_model_file', counted)
    model = tmp_path / 'first.gguf'
    model.write_bytes(b'GGUFfirst')
    enable_test_embedding(tmp_path / 'search', model)
    lookup = lambda path: LibrarySearch(tmp_path / 'search', embed_model=path, cache_dir=tmp_path / 'unused-cache').status()['dependencies']['embedding_model']['sha256']
    initial = lookup(model)
    stat = model.stat()
    model.write_bytes(b'GGUFother')
    os.utime(model, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    assert lookup(model) != initial
    assert reads == [model.resolve()]
    for index in range(3):
        another = tmp_path / f'other-{index}.gguf'
        another.write_bytes(b'GGUFfixture')
        lookup(another)
    assert len(module._MODEL_HASH_CACHE) == 2
    assert not (tmp_path / 'unused-cache').exists()
