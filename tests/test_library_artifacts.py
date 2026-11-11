"""Canonical extraction storage stays reusable without mutable evidence links."""
from collections import Counter
import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from advisor.build_control import BuildCancelled
from advisor.library_store import LibraryStore, encoded, extraction_coverage, file_hash, job_counts


def fixture(tmp_path):
    source = tmp_path / "sources"
    source.mkdir()
    model = source / "case.mph"
    model.write_bytes(b"Original MPH fixture; do not alter")
    store = LibraryStore(tmp_path / "data/libraries")
    library = store.register("Fixture", "user-models", [{"path": str(source)}])
    library = store.set_enabled(library["id"], True)
    return store, library, model


def extractor(version="one", feature_count=1, on_extract=None):
    def extract(path, *, artifact_dir, **_):
        output = artifact_dir / "mph-fixture"
        output.mkdir(exist_ok=True)
        if on_extract:
            on_extract(output)
        payloads = {"saved-state.json": encoded({"nodes": [{"path": "Model/Study", "version": version}], "padding": "x" * 100000}),
                    "api-state.json": encoded({"nodes": [{"path": "model/study", "version": version}]}),
                    "dmodel.xml": "<Model version='" + version + "'/>",
                    "model_export.java": "// Export " + version,
                    "attachment.bin": "Unreferenced-in-evidence asset " + version}
        for name, payload in payloads.items():
            (output / name).write_text(payload, encoding="utf-8")
        (output / "api-manifest.json").write_text('{"fixture":"API signature"}', encoding="utf-8")
        return {"objects": [
            {"local_id": "model-case" if number == 0 else "feature-" + str(number),
             "type": "model_case" if number == 0 else "saved_feature", "title": "Case " + str(number),
             "data": {"configuration_assets": [{"asset_path": str(output / "saved-state.json")}]},
             "evidence": [{"asset_path": str(path), "method": "source"},
                          {"asset_path": str(output / "saved-state.json"), "method": "native_mph_xml_snapshot"}]}
            for number in range(feature_count)],
                "assets": [{"path": str(output / name), "asset_path": str(output / name), "derived": True} for name in payloads]}
    return extract


def cache_for(store):
    return next((store.root / "artifacts").glob("*/extraction.json"))


def invalidate(store):
    cache = cache_for(store)
    value = json.loads(cache.read_text(encoding="utf-8"))
    value["parser_hash"] = "stale-parser"
    cache.write_text(encoded(value), encoding="utf-8")


def test_success_has_one_payload_copy_and_unchanged_retry_needs_no_extractor(tmp_path):
    store, library, model = fixture(tmp_path)
    original = model.read_bytes()
    with patch("advisor.library_model.extract_case", side_effect=extractor()):
        assert store.build(library["id"], deep=False, modes=[])["status"] == "completed"
    cache = cache_for(store)
    value = json.loads(cache.read_text(encoding="utf-8"))
    redirects = value["artifact_blobs"]
    assert len(redirects) == 5
    for entry in redirects:
        assert not (cache.parent / entry["path"]).exists()
        blob = store.paths.resolve(entry["blob"])
        assert file_hash(blob) == entry["sha256"]
        matches = [p for p in store.root.rglob("*") if p.is_file() and file_hash(p) == entry["sha256"]]
        assert matches == [blob]
    before = store.objects()
    cache_bytes = cache.read_bytes()
    with patch("advisor.library_model.extract_case", side_effect=AssertionError("Must reuse canonical cache")):
        assert store.build(library["id"], deep=False, modes=[])["status"] == "completed"
    assert store.objects() == before
    assert cache.read_bytes() == cache_bytes
    assert model.read_bytes() == original


def test_parser_invalidation_restores_independent_api_cache_and_keeps_old_citations(tmp_path):
    store, library, model = fixture(tmp_path)
    with patch("advisor.library_model.extract_case", side_effect=extractor()):
        store.build(library["id"], deep=False, modes=[])
    old = store.objects()[0]
    old_evidence = old["evidence"][1]
    old_blob = store.root / old_evidence["relative_path"]
    original = old_blob.read_bytes()
    invalidate(store)

    def check(output):
        assert (output / "api-manifest.json").is_file()
        assert json.loads((output / "api-state.json").read_text())["nodes"][0]["version"] == "one"
        (output / "saved-state.json").write_text("mutable working copy", encoding="utf-8")
        assert old_blob.read_bytes() == original

    with patch("advisor.library_model.extract_case", side_effect=extractor("two", on_extract=check)):
        assert store.build(library["id"], deep=False, modes=[])["status"] == "completed"
    assert old_blob.read_bytes() == original
    assert store.get(old["id"]) == old
    assert store.objects()[0]["id"] != old["id"]
    assert not list(cache_for(store).parent.rglob("saved-state.json"))


def test_failure_during_reparse_keeps_evidence_and_retry_reuses_work(tmp_path):
    store, library, _ = fixture(tmp_path)
    with patch("advisor.library_model.extract_case", side_effect=extractor()):
        store.build(library["id"], deep=False, modes=[])
    before = store.objects()
    hashes = {p.name: file_hash(p) for p in (store.root / "blobs").iterdir()}
    invalidate(store)

    def fail(path, *, artifact_dir, **_):
        (artifact_dir / "mph-fixture/saved-state.json").write_text("failed new snapshot", encoding="utf-8")
        raise ValueError("Fixture failure after writing a working file")

    with patch("advisor.library_model.extract_case", side_effect=fail):
        assert store.build(library["id"], deep=False, modes=[])["status"] == "failed"
    assert store.objects() == before
    assert {p.name: file_hash(p) for p in (store.root / "blobs").iterdir()} == hashes
    with patch("advisor.library_model.extract_case", side_effect=extractor("retry")):
        assert store.build(library["id"], deep=False, modes=[])["status"] == "completed"


@pytest.mark.parametrize("boundary", ["copy", "cache", "prepare"])
def test_cancelled_archive_is_retryable_and_purge_discovers_all_owned_blobs(tmp_path, boundary):
    store, library, _ = fixture(tmp_path)
    original_archive = store._archive_blob
    original_write = store._write_json
    copied = []

    def archive(*args, **kwargs):
        result = original_archive(*args, **kwargs)
        copied.append(result)
        if boundary == "copy":
            raise BuildCancelled()
        return result

    def write(path, value):
        original_write(path, value)
        if boundary == "cache" and path.name == "extraction.json" and value.get("artifact_blobs"):
            raise BuildCancelled()

    with patch("advisor.library_model.extract_case", side_effect=extractor()), patch.object(store, "_archive_blob", side_effect=archive), patch.object(store, "_write_json", side_effect=write), patch.object(store, "_prepare", side_effect=BuildCancelled()):
        assert store.build(library["id"], deep=False, modes=[])["status"] == "cancelled"
    assert copied and not store.objects()
    assert (cache_for(store).parent / "artifact-blobs.json").stat().st_size < 10000
    # Both raw-working and canonical cache states survive these boundaries.
    with patch("advisor.library_model.extract_case", side_effect=AssertionError("Reuse the saved extraction")):
        assert store.build(library["id"], deep=False, modes=[])["status"] == "completed"
    store.delete_library(library["id"])
    assert store.purge_library(library["id"], confirmed=True)["status"] == "purged"
    assert not list((store.root / "blobs").iterdir())


def test_shared_cache_only_assets_survive_first_purge_and_failed_history_is_purgeable(tmp_path):
    store, first, _ = fixture(tmp_path)
    source = tmp_path / "second"
    source.mkdir()
    (source / "case.mph").write_bytes(b"Second immutable source")
    second = store.register("Second", "user-models", [{"path": str(source)}])
    second = store.set_enabled(second["id"], True)
    with patch("advisor.library_model.extract_case", side_effect=extractor()), patch.object(store, "_prepare", side_effect=BuildCancelled()):
        for library in (first, second):
            assert store.build(library["id"], deep=False, modes=[])["status"] == "cancelled"
    old_blobs = set((store.root / "blobs").iterdir())
    assert len(old_blobs) == 5
    # Failed old versions have no revisions, but remain in the small ownership ledger.
    own_cache = next(p for p in (store.root / "artifacts").glob("*/extraction.json")
                     if json.loads((p.parent / "library-owner.json").read_text())["library_id"] == first["id"])
    value = json.loads(own_cache.read_text())
    value["parser_hash"] = "stale"
    own_cache.write_text(encoded(value), encoding="utf-8")
    with patch("advisor.library_model.extract_case", side_effect=extractor("new")), patch.object(store, "_prepare", side_effect=BuildCancelled()):
        store.build(first["id"], deep=False, modes=[])
    assert len(list((store.root / "blobs").iterdir())) == 10
    store.delete_library(first["id"])
    # Purge consumes only the compact manifest, not the large extraction result.
    with patch("advisor.library_store.json.loads", wraps=json.loads) as loads:
        assert store.purge_library(first["id"], confirmed=True)["status"] == "purged"
    assert all(len(call.args[0]) < 100000 for call in loads.call_args_list)
    assert set((store.root / "blobs").iterdir()) == old_blobs
    store.delete_library(second["id"])
    assert store.purge_library(second["id"], confirmed=True)["status"] == "purged"
    assert not list((store.root / "blobs").iterdir())


def test_purge_blob_unlink_failure_keeps_cache_only_ownership_for_retry(tmp_path):
    store, library, _ = fixture(tmp_path)
    with patch("advisor.library_model.extract_case", side_effect=extractor()), patch.object(store, "_prepare", side_effect=BuildCancelled()):
        store.build(library["id"], deep=False, modes=[])
    manifest = cache_for(store).parent / "artifact-blobs.json"
    blobs = set((store.root / "blobs").iterdir())
    assert blobs and not store.objects()
    store.delete_library(library["id"])
    unlink = Path.unlink

    def fail_blob(path, *args, **kwargs):
        if path in blobs:
            raise PermissionError("Fixture canonical blob remains locked")
        return unlink(path, *args, **kwargs)

    with patch.object(Path, "unlink", autospec=True, side_effect=fail_blob):
        with pytest.raises(PermissionError, match="remains locked"):
            store.purge_library(library["id"], confirmed=True)
    assert manifest.is_file()
    assert store.library(library["id"], include_deleted=True)["deleted_at"]
    assert store.purge_library(library["id"], confirmed=True)["status"] == "purged"
    assert not list((store.root / "blobs").iterdir())


def test_interrupted_archive_staging_is_owned_and_removed_by_retry_or_purge(tmp_path):
    store, library, _ = fixture(tmp_path)
    with patch("advisor.library_model.extract_case", side_effect=extractor()):
        store.build(library["id"], deep=False, modes=[])
    cache = cache_for(store)
    entry = json.loads(cache.read_text())["artifact_blobs"][0]
    work = cache.parent / entry["path"]
    staging = work.with_name(work.name + ".archive.tmp")
    staging.write_bytes(b"Partial copy left by a terminated process")
    invalidate(store)
    with patch("advisor.library_model.extract_case", side_effect=extractor()):
        assert store.build(library["id"], deep=False, modes=[])["status"] == "completed"
    assert not staging.exists()
    staging.write_bytes(b"Another interrupted copy")
    store.delete_library(library["id"])
    assert store.purge_library(library["id"], confirmed=True)["status"] == "purged"
    assert not staging.exists()


def test_partial_artifact_tree_deletion_retains_owner_until_payload_is_gone(tmp_path):
    store, library, _ = fixture(tmp_path)
    with patch("advisor.library_model.extract_case", side_effect=extractor()), patch.object(store, "_prepare", side_effect=BuildCancelled()):
        store.build(library["id"], deep=False, modes=[])
    artifact = cache_for(store).parent
    payload = artifact / "mph-fixture/snapshot.json.archive.tmp"
    payload.write_bytes(b"Locked interrupted working payload")
    store.delete_library(library["id"])
    import shutil
    rmtree = shutil.rmtree

    def partial(path, *args, **kwargs):
        if Path(path) == payload.parent:
            raise PermissionError("Fixture working payload remains locked")
        return rmtree(path, *args, **kwargs)

    with patch("advisor.library_store.shutil.rmtree", side_effect=partial):
        with pytest.raises(PermissionError, match="remains locked"):
            store.purge_library(library["id"], confirmed=True)
    assert (artifact / "library-owner.json").is_file()
    assert payload.is_file()
    assert store.purge_library(library["id"], confirmed=True)["status"] == "purged"
    assert not artifact.exists()


def test_many_features_hash_shared_snapshot_a_bounded_number_of_times(tmp_path):
    store, library, _ = fixture(tmp_path)
    calls = Counter()

    def counted(path):
        calls[Path(path).name] += 1
        return file_hash(path)

    with patch("advisor.library_model.extract_case", side_effect=extractor(feature_count=180)), patch("advisor.library_store.file_hash", side_effect=counted):
        assert store.build(library["id"], deep=False, modes=[])["status"] == "completed"
    snapshot = next(entry for entry in json.loads(cache_for(store).read_text())["artifact_blobs"] if entry["path"].endswith("saved-state.json"))
    assert calls[Path(snapshot["blob"]).name] < 5
    assert calls["saved-state.json"] < 5
    assert len(store.objects()) == 180


def test_corrupt_existing_canonical_blob_is_preserved_and_blocks_publication(tmp_path):
    store, library, _ = fixture(tmp_path)
    with patch("advisor.library_model.extract_case", side_effect=extractor()):
        store.build(library["id"], deep=False, modes=[])
    before = store.objects()
    blob = store.root / before[0]["evidence"][1]["relative_path"]
    blob.write_bytes(b"corrupted canonical evidence")
    with patch("advisor.library_model.extract_case", side_effect=extractor()):
        result = store.build(library["id"], deep=False, modes=[])
    assert result["status"] == "failed"
    assert blob.read_bytes() == b"corrupted canonical evidence"
    assert store.objects() == before


def test_raw_coverage_gaps_are_partial_independently_of_deep_not_requested():
    raw = {"status": "bounded_saved_state", "truncated": False,
           "normalization": {"properties_total": 3, "properties_unresolved": 0, "properties_partial": 0,
                             "selection_statuses": {"decoded_saved": 1}}}
    complete = {"coverage": {"raw_xml": raw, "deep_api": {"status": "not_requested"}}}
    assert extraction_coverage(complete)["status"] == "complete"
    for field, change in (("properties_unresolved", 1), ("properties_partial", 1), ("selection_statuses", {"unresolved": 1})):
        item = copy.deepcopy(complete)
        item["coverage"]["raw_xml"]["normalization"][field] = change
        assert job_counts({"items": [item], "failed": []}) == {"succeeded": 0, "partial": 1, "failed": 0}
        assert extraction_coverage(item)["deep_api_status"] == "not_requested"
    for coverage in ({"raw_xml": {}}, {"extraction_level": "saved_xml_inventory"},
                     {"raw_xml": {"status": "missing"}}, {"raw_xml": {"status": "bounded_saved_state"}},
                     {"raw_xml": dict(raw, truncated=True)}, {"deep_api": {"status": "observed"}},
                     {"saved_features": {"omitted_nodes": 1}}):
        assert extraction_coverage({"coverage": coverage})["status"] == "partial"
    assert extraction_coverage({"coverage": {"saved_features": {"preview_truncations": 3}}})["status"] == "not_applicable"


def test_legacy_completed_offline_job_reports_saved_read_gaps_in_status_summary(tmp_path):
    store = LibraryStore(tmp_path / "catalog")
    store._save_job({"id": "legacy-offline", "status": "completed", "items": [
        {"status": "completed", "coverage": {"raw_xml": {"status": "bounded_saved_state", "normalization": {
            "properties_total": 8, "properties_unresolved": 2, "properties_partial": 1, "selection_statuses": {"unresolved": 3}}},
            "deep_api": {"status": "not_requested"}}}], "failed": []})
    job = store.job_summary("legacy-offline")
    assert job["status"] == "partial"
    assert job["counts"] == {"succeeded": 0, "partial": 1, "failed": 0}
    assert job["coverage_summary"] == {"status": "partial", "partial_files": 1,
                                       "deep_api_not_requested": 1, "properties_unresolved": 2, "properties_partial": 1}
    assert "saved_selections_unresolved" in job["items"][0]["coverage_summary"]["reasons"]
