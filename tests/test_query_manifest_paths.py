"""Resolved metadata is shared by index version; physical changes invalidate it."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from advisor import index_manifest as manifests
from advisor import index_manifest_cache as cache
from advisor import index_paths


def value(root):
    directory = root / 'generations' / ('a' * 20 + '-12345678')
    libraries = {}
    for ordinal in range(3):
        key = 'lib_' + f'{ordinal:020x}'
        libraries[str(ordinal)] = dict(key=key, collection=key,
            corpus=str(directory / key / 'corpus'), db_path=str(directory / 'qmd-keyword.sqlite3'))
    return dict(generation='test', directory=str(directory), libraries=libraries,
                keyword_storage=index_paths.SHARED_KEYWORD_STORAGE, modes={'qmd_keyword': {'ready': True}})


@pytest.mark.parametrize('pointer', [False, True])
def test_runtime_read_resolves_once_without_changing_disk_cache(tmp_path, monkeypatch, pointer):
    source = value(tmp_path)
    path = tmp_path / 'active.json'
    if pointer:
        manifests.publish_manifest(source['directory'], source, tmp_path)
    else:
        path.write_text(json.dumps(index_paths.serialize_index_manifest(source, tmp_path)))
    original = cache.read_manifest(path, summary=True)
    resolved = Mock(wraps=index_paths.resolve_index_manifest)
    monkeypatch.setattr(cache, 'resolve_index_manifest', resolved)
    monkeypatch.setattr(manifests, 'resolve_index_manifest', resolved)
    runtime = cache.read_manifest(path, summary=True, resolve_paths=True)
    assert resolved.call_count == (0 if pointer else 1)
    assert runtime['directory'] == str(Path(source['directory']))
    assert runtime['libraries'] == source['libraries']
    runtime['libraries']['0']['corpus'] = 'modified'
    assert cache.read_manifest(path, summary=True) == original


def test_pointer_active_and_last_build_share_version_resolution(tmp_path, monkeypatch):
    source = value(tmp_path)
    manifests.publish_manifest(source['directory'], source, tmp_path)
    resolved = Mock(wraps=index_paths.resolve_index_manifest)
    monkeypatch.setattr(manifests, 'resolve_index_manifest', resolved)
    first = cache.read_manifest(tmp_path/'active.json', summary=True, resolve_paths=True)
    for _ in range(4):
        assert cache.read_manifest(tmp_path/'last-build.json', summary=True, resolve_paths=True) == first
        assert cache.read_manifest(tmp_path/'active.json', summary=True, resolve_paths=True) == first
    assert resolved.call_count == 1


def test_concurrent_queries_resolve_a_cold_version_only_once(tmp_path, monkeypatch):
    source = value(tmp_path)
    manifests.publish_manifest(source['directory'], source, tmp_path)
    resolved = Mock(wraps=index_paths.resolve_index_manifest)
    monkeypatch.setattr(manifests, 'resolve_index_manifest', resolved)
    with ThreadPoolExecutor(max_workers=8) as executor:
        hits = list(executor.map(lambda _: cache.read_manifest(tmp_path/'active.json', summary=True,
                                                              resolve_paths=True), range(16)))
    assert resolved.call_count == 1
    assert all(hit == hits[0] for hit in hits)
    hits[0]['libraries']['0']['corpus'] = 'caller changed snapshot'
    assert hits[1]['libraries']['0']['corpus'] == source['libraries']['0']['corpus']


def test_changed_authority_refreshes_resolution_even_when_generation_is_unchanged(tmp_path, monkeypatch):
    source = value(tmp_path)
    manifests.publish_manifest(source['directory'], source, tmp_path)
    resolved = Mock(wraps=index_paths.resolve_index_manifest)
    monkeypatch.setattr(manifests, 'resolve_index_manifest', resolved)
    assert cache.read_manifest(tmp_path/'active.json', summary=True, resolve_paths=True)
    authority = Path(source['directory'])/'manifest.json'
    changed = json.loads(authority.read_text())
    changed['configuration_revision'] = 'updated'
    manifests._write_json(authority, changed)
    assert cache.read_manifest(tmp_path/'active.json', resolve_paths=True)['configuration_revision'] == 'updated'
    assert resolved.call_count == 2


def test_data_directory_migration_does_not_reuse_old_absolute_paths(tmp_path, monkeypatch):
    original = tmp_path/'original'
    source = value(original)
    manifests.publish_manifest(source['directory'], source, original)
    assert cache.read_manifest(original/'active.json', resolve_paths=True)
    moved = tmp_path/'moved'
    original.rename(moved)
    loaded = cache.read_manifest(moved/'active.json', resolve_paths=True)
    assert Path(loaded['directory']).is_relative_to(moved)
    assert all(Path(item['db_path']).is_relative_to(moved) for item in loaded['libraries'].values())


def test_cached_layout_refreshes_after_directory_replacement(tmp_path, monkeypatch):
    source = value(tmp_path)
    directory = Path(source['directory'])
    library = directory/source['libraries']['0']['key']
    (library/'corpus').mkdir(parents=True)
    manifests.publish_manifest(directory, source, tmp_path)
    resolved = Mock(wraps=index_paths.resolve_index_manifest)
    monkeypatch.setattr(manifests, 'resolve_index_manifest', resolved)
    assert cache.read_manifest(tmp_path/'active.json', resolve_paths=True)
    library.rename(directory/'old-library')
    (library/'corpus').mkdir(parents=True)
    assert cache.read_manifest(tmp_path/'active.json', resolve_paths=True)
    assert resolved.call_count == 2


def test_publication_during_cold_resolution_does_not_return_old_metadata(tmp_path, monkeypatch):
    source = value(tmp_path)
    manifests.publish_manifest(source['directory'], source, tmp_path)
    authority = Path(source['directory'])/'manifest.json'
    original = index_paths.resolve_index_manifest
    changed = False

    def resolve_then_publish(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            replacement = json.loads(authority.read_text())
            replacement['modes']['qmd_keyword']['ready'] = False
            manifests._write_json(authority, replacement)
        return result

    monkeypatch.setattr(manifests, 'resolve_index_manifest', resolve_then_publish)
    loaded = cache.read_manifest(tmp_path/'active.json', resolve_paths=True)
    assert not loaded['modes']['qmd_keyword']['ready']


def test_shared_database_locator_is_checked_once_per_conversion(tmp_path, monkeypatch):
    source = value(tmp_path)
    check = Mock(wraps=index_paths._inside)
    monkeypatch.setattr(index_paths, '_inside', check)
    for _ in range(2):
        index_paths.resolve_index_manifest(source, tmp_path)
    database = ('generations', 'a' * 20 + '-12345678', 'qmd-keyword.sqlite3')
    assert sum(call.args[1] == database for call in check.call_args_list) == 2


def test_runtime_read_rejects_replaced_authority_after_cache_hit(tmp_path):
    source = value(tmp_path)
    manifests.publish_manifest(source['directory'], source, tmp_path)
    path = tmp_path / 'active.json'
    assert cache.read_manifest(path, resolve_paths=True)
    authority = Path(source['directory']) / 'manifest.json'
    changed = json.loads(authority.read_text())
    changed['libraries']['0']['db_path'] = '../other.sqlite3'
    manifests._write_json(authority, changed)
    with pytest.raises(ValueError):
        cache.read_manifest(path, resolve_paths=True)


def test_receipts_bind_returned_content_on_cold_and_hot_reads(tmp_path):
    source = value(tmp_path)
    manifests.publish_manifest(source['directory'], source, tmp_path)
    pointer = tmp_path / 'active.json'
    authority = Path(source['directory']) / 'manifest.json'
    for _ in range(2):
        receipt = {}
        assert cache.read_manifest(pointer, summary=True, resolve_paths=True, receipt=receipt)
        assert receipt == {str(path.absolute()): cache._identity(path.stat()) for path in (pointer, authority)}


def test_shared_authority_receipts_preserve_conflicts(tmp_path):
    source = value(tmp_path)
    manifests.publish_manifest(source['directory'], source, tmp_path)
    receipt = {}
    assert cache.read_manifest(tmp_path / 'active.json', summary=True, receipt=receipt)
    authority = Path(source['directory']) / 'manifest.json'
    changed = json.loads(authority.read_text())
    changed['modes']['qmd_keyword']['ready'] = False
    manifests._write_json(authority, changed)
    assert cache.read_manifest(tmp_path / 'last-build.json', summary=True, receipt=receipt)
    assert receipt[str(authority.absolute())] is None
    assert cache.read_manifest(tmp_path / 'last-build.json', summary=True, receipt=receipt)
    assert receipt[str(authority.absolute())] is None
