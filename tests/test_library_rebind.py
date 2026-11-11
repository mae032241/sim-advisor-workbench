"""Directory relocation preserves derived data and cannot race construction."""
from pathlib import Path
import hashlib
import os
import shutil
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from advisor.library_store import LibraryLifecycleConflict, LibraryStore, file_hash
from advisor.library_provider import LibraryKnowledgeProvider


def fixture(tmp_path):
    source = tmp_path / "old"
    source.mkdir()
    (source / "guide.md").write_text("# Heat guide\nA documented thermal boundary condition.", encoding="utf-8")
    store = LibraryStore(tmp_path / "data/libraries")
    lib = store.register("Relocation", "official-manuals", [{"id": "docs", "path": str(source), "role": "docs"}])
    lib = store.set_enabled(lib["id"], True)
    store.build(lib["id"], modes=["native"])
    target = tmp_path / "new location"
    shutil.copytree(source, target)
    return store, lib, source, target


def test_rebind_moved_sources_keeps_citations_index_bytes_and_closed_state(tmp_path):
    store, lib, source, target = fixture(tmp_path)
    citation = store.objects()[0]
    manifest = store.index_status()
    files = [store.root / "search/active.json", Path(manifest["directory"]) / "objects.sqlite3"]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    shutil.rmtree(source)
    store.set_enabled(lib["id"], False)
    result = store.rebind(lib["id"], "docs", target)
    assert result["verified_evidence"] == 1 and result["enabled"] is False
    assert result["roots"][0]["path"] == str(target)
    assert store.get(citation["id"]) == citation
    assert {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files} == before
    store.set_enabled(lib["id"], True)
    assert "thermal boundary" in store.read_evidence(citation["id"])["text"]


@pytest.mark.parametrize("scope", ["same", "global", "unrelated"])
def test_rebind_rejects_affected_live_jobs_but_allows_an_independent_library(tmp_path, scope):
    store, lib, source, target = fixture(tmp_path)
    job = {"id": "f" * 32, "status": "running", "stage": "extracting", "pid": os.getpid(),
           "library_id": lib["id"] if scope == "same" else "another-library"}
    if scope == "global":
        job.update(kind="reindex", scope="all_libraries", library_id=None)
    store._save_job(job)
    if scope == "unrelated":
        assert store.rebind(lib["id"], "docs", target)["verified_evidence"] == 1
    else:
        with pytest.raises(LibraryLifecycleConflict, match="构建或索引"):
            store.rebind(lib["id"], "docs", target)
        assert store.library(lib["id"])["roots"][0]["path"] == str(source)


def test_a_job_started_during_hash_verification_blocks_rebind_commit(tmp_path):
    store, lib, source, target = fixture(tmp_path)
    def start_job_and_hash(path):
        store._save_job({"id": "e" * 32, "status": "queued", "pid": os.getpid(), "library_id": lib["id"]})
        return file_hash(path)
    with patch("advisor.library_store.file_hash", side_effect=start_job_and_hash):
        with pytest.raises(LibraryLifecycleConflict):
            store.rebind(lib["id"], "docs", target)
    assert store.library(lib["id"])["roots"][0]["path"] == str(source)


def test_moved_closed_manuals_also_block_the_old_cached_legacy_source(tmp_path):
    store, lib, source, target = fixture(tmp_path)
    cached = {"id": "cs-cached", "title": "Cached guide", "relative_path": "guide.md", "kind": "manual", "text": "thermal boundary"}
    legacy = SimpleNamespace(doc_root=source, get_document=lambda _id: dict(cached),
                             search=lambda *_args, **_kw: [dict(cached)])
    provider = LibraryKnowledgeProvider(legacy, store.root)
    assert provider.get_document(cached["id"])["text"] == cached["text"]
    store.rebind(lib["id"], "docs", target)
    shutil.rmtree(source)
    store.set_enabled(lib["id"], False)
    assert provider.search("thermal", filters={"mode": "native"}) == []
    with pytest.raises(PermissionError):
        provider.get_document(cached["id"])
    assert provider.source_available(cached["id"]) is False
    store.set_enabled(lib["id"], True)
    assert provider.get_document(cached["id"])["text"] == cached["text"]
