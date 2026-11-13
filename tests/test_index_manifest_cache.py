import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from advisor import index_manifest_cache as cache
from advisor.build_control import BuildCancelled


@pytest.fixture(autouse=True)
def empty_cache():
    cache._SUMMARIES.clear()
    cache._FULL = None
    cache._RESOLVED.clear()
    yield
    cache._SUMMARIES.clear()
    cache._FULL = None
    cache._RESOLVED.clear()


def write(path, generation="first"):
    value = {"generation": generation, "modes": {"qmd_keyword": {"ready": True}},
             "libraries": {"one": {"db_path": "generations/first/lib_one/qmd-keyword.sqlite3"}},
             "mapping": {"one/source.md": "citation"},
             "representation": {"objects": {"citation": {"indexed_characters": 100}}}}
    path.write_text(json.dumps(value), encoding="utf-8")
    return value


def test_summary_parses_once_keeps_no_bulk_and_returns_independent_metadata(tmp_path, monkeypatch):
    path = tmp_path / "active.json"
    write(path)
    load = Mock(wraps=json.load)
    monkeypatch.setattr(cache.json, "load", load)
    first = cache.read_manifest(path, summary=True)
    first["modes"]["qmd_keyword"]["ready"] = False
    second = cache.read_manifest(path, summary=True)
    assert second["modes"]["qmd_keyword"]["ready"]
    assert "mapping" not in second and "representation" not in second
    assert load.call_count == 1
    assert cache._FULL is None
    assert not list(tmp_path.glob("*.tmp")) and len(list(tmp_path.iterdir())) == 1


def test_full_cache_shares_only_bulk_metadata_and_also_warms_summary(tmp_path, monkeypatch):
    path = tmp_path / "active.json"
    write(path)
    load = Mock(wraps=json.load)
    monkeypatch.setattr(cache.json, "load", load)
    first = cache.read_manifest(path)
    first["libraries"]["one"]["db_path"] = "caller runtime locator"
    first["modes"]["qmd_keyword"]["ready"] = False
    second = cache.read_manifest(path)
    assert second["modes"]["qmd_keyword"]["ready"]
    assert second["libraries"]["one"]["db_path"].startswith("generations/")
    assert first["mapping"] is second["mapping"]
    assert first["representation"] is second["representation"]
    assert cache.read_manifest(path, summary=True)["generation"] == "first"
    assert load.call_count == 1


def test_replacement_same_size_and_mtime_deletion_and_recreation_invalidate(tmp_path):
    path = tmp_path / "active.json"
    write(path, "first")
    cache.read_manifest(path)
    original = path.stat()
    replacement = tmp_path / "next.json"
    write(replacement, "other")
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_size == original.st_size
    assert cache.read_manifest(path, summary=True)["generation"] == "other"
    assert cache.read_manifest(path)["generation"] == "other"
    path.unlink()
    assert cache.read_manifest(path, summary=True) is None
    assert cache.read_manifest(path) is None
    write(path, "third")
    assert cache.read_manifest(path, summary=True)["generation"] == "third"


def test_publication_during_read_retries_and_never_caches_previous_generation(tmp_path, monkeypatch):
    path = tmp_path / "active.json"
    write(path)
    load = Mock(wraps=json.load)
    monkeypatch.setattr(cache.json, "load", load)
    checks = 0

    def publish_after_read():
        nonlocal checks
        checks += 1
        if checks == 4:
            replacement = tmp_path / "next.json"
            write(replacement, "published")
            replacement.replace(path)

    monkeypatch.setattr(cache, "check_cancelled", publish_after_read)
    assert cache.read_manifest(path)["generation"] == "published"
    assert load.call_count == 2
    assert cache.read_manifest(path, summary=True)["generation"] == "published"


def test_cancelled_read_does_not_publish_partial_cache(tmp_path, monkeypatch):
    path = tmp_path / "active.json"
    write(path)
    checks = 0

    def cancel_after_parse():
        nonlocal checks
        checks += 1
        if checks == 4:
            raise BuildCancelled()

    monkeypatch.setattr(cache, "check_cancelled", cancel_after_parse)
    with pytest.raises(BuildCancelled):
        cache.read_manifest(path)
    assert not cache._SUMMARIES and cache._FULL is None
    monkeypatch.setattr(cache, "check_cancelled", lambda: None)
    assert cache.read_manifest(path)["generation"] == "first"


def test_caches_are_bounded_and_oversized_full_manifests_are_not_retained(tmp_path, monkeypatch):
    for index in range(cache._SUMMARY_LIMIT + 2):
        path = tmp_path / f"{index}.json"
        write(path, str(index))
        cache.read_manifest(path, summary=True)
    assert len(cache._SUMMARIES) == cache._SUMMARY_LIMIT
    monkeypatch.setattr(cache, "_FULL_MAX_BYTES", 1)
    cache.read_manifest(path)
    assert cache._FULL is None


@pytest.mark.parametrize("body", ["not-json", "[]"])
def test_invalid_manifest_cannot_reuse_previously_available_state(tmp_path, body):
    path = tmp_path / "active.json"
    write(path)
    cache.read_manifest(path)
    path.write_text(body, encoding="utf-8")
    if body == "[]":
        with pytest.raises(ValueError, match="must be an object"):
            cache.read_manifest(path)
    else:
        assert cache.read_manifest(path) is None
    assert cache._FULL is None
