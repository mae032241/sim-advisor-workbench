"""All-case extraction stays generic, bounded, isolated and resumable."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from advisor import case_context_bulk as bulk
from advisor import case_context_index as coordinator
from advisor.evaluation_sets import _watermark
from scripts.enrich_all_case_contexts import main
from test_case_context import Store, overview, section


NARRATIVE = ("The example investigates a rotating channel carrying a reactive suspension under a prescribed periodic inlet. "
             "The objective is to compare the outlet concentration over time for several rotation rates.")


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "libraries"
    root.mkdir()
    store = Store(root)
    with sqlite3.connect(store.db_path) as db:
        db.execute("CREATE TABLE case_members(group_id TEXT,local_id TEXT,citation_id TEXT)")
    store.add(section("unfamiliar-intro", "Introduction", 1, NARRATIVE), document="document-sha")
    search = root / "search"
    generation = search / "generations" / "unchanged-generation"
    generation.mkdir(parents=True)
    objects = generation / "objects.sqlite3"
    with sqlite3.connect(objects) as db:
        db.execute("CREATE TABLE objects(id TEXT PRIMARY KEY)")
    keyword = generation / "qmd-keyword.sqlite3"
    keyword.write_bytes(b"unchanged QMD fixture")
    active = {"generation": "unchanged-generation", "corpus_hash": "unchanged-corpus", "directory": str(generation)}
    monkeypatch.setattr(coordinator, "_active", lambda _: dict(active))
    monkeypatch.setattr(coordinator, "_keyword_path", lambda _: keyword)
    result = {"root": root, "store": store, "active": active, "keyword": keyword, "objects": objects, "cases": []}

    def add(count, *, link=True):
        start = len(result["cases"])
        for index in range(start, start + count):
            identity = "unfamiliar-case-" + str(index).zfill(3)
            case = overview(id=identity, object_id=identity + "-object", case_group=identity + "-group",
                            asset_key="models:unseen_channel_" + str(index) + ".mph", asset_hash="asset-" + str(index))
            store.add(case)
            with sqlite3.connect(store.db_path) as db:
                db.execute("INSERT INTO case_members VALUES(?,?,?)", (case["case_group"], "model-case", identity))
            with sqlite3.connect(objects) as db:
                db.execute("INSERT INTO objects VALUES(?)", (identity,))
            if link:
                store.link(identity)
            result["cases"].append(case)
        return result["cases"][start:]

    result["add"] = add
    return result


def _holdout(corpus, case):
    with closing(corpus["store"].connect()) as db:
        watermark = _watermark(db)
    exclusions = {name: [] for name in ("citation_ids", "object_ids", "case_groups", "source_hashes", "qmd_paths")}
    # The current overview ID is deliberately absent. Filtering must inspect
    # the full immutable raw identity, even with the web read switch open.
    exclusions["citation_ids"] = ["historical-heldout-revision"]
    exclusions["asset_keys"] = [[case["library_id"], case["asset_key"]]]
    registry = {"schema": "evaluation-sets/v1", "sets": [{"id": "holdout", "name": "Holdout", "models": [
        {"library_id": case["library_id"], "asset_key": case["asset_key"]}]}],
        "exclusions": exclusions, "catalog_revision_watermark": watermark}
    (corpus["root"] / "evaluation-sets.json").write_text(json.dumps(registry), encoding="utf-8")
    (corpus["root"] / "evaluation-access.json").write_text(json.dumps(
        {"schema": "evaluation-access/v1", "enabled": True}), encoding="utf-8")


def _publisher(monkeypatch):
    batches, refreshed = [], []

    def publish(data_dir, profiles, capabilities):
        batches.append((dict(profiles), dict(capabilities)))
        return {"published_profiles": len(profiles), "changed_documents": len(profiles), "unchanged_documents": 0}

    monkeypatch.setattr(bulk, "_publish_batch", publish)
    monkeypatch.setattr(bulk, "_refresh_navigation", lambda data_dir: refreshed.append(data_dir))
    return batches, refreshed


def test_all_active_exact_ids_are_extracted_in_bounded_batches_without_holdout_or_manual_text(corpus, monkeypatch, tmp_path):
    cases = corpus["add"](69)
    corpus["add"](1, link=False)
    heldout, broken = cases[5]["id"], cases[6]["id"]
    _holdout(corpus, cases[5])
    called = []
    constructor = bulk.build_case_context

    def derive(store, identity):
        called.append(identity)
        assert identity != heldout
        if identity == broken:
            raise ValueError("A malformed canonical link in this file")
        return constructor(store, identity)

    monkeypatch.setattr(bulk, "build_case_context", derive)
    batches, refreshed = _publisher(monkeypatch)
    report = tmp_path / "outcomes.json"
    before = corpus["store"].db_path.read_bytes(), corpus["keyword"].read_bytes()
    result = bulk.run_bulk_case_contexts(corpus["root"], apply=True, batch_size=17, report=report)
    assert result["state"] == "partial"
    assert result["counts"] == {"active_overviews": 70, "eligible": 69, "heldout": 1, "classified": 70,
                                "pending": 0, "staged": 0, "published": 67, "committed": 67, "noop": 0,
                                "derived": 0, "degraded": 1, "failed": 1, "processed": 70, "ready": 67}
    assert [len(profiles) for profiles, _ in batches] == [17, 17, 17, 16]
    assert len(called) == 69 and len(set(called)) == 69
    assert len(refreshed) == 1 and result["navigation_refreshed"]
    for profiles, capabilities in batches:
        assert len(capabilities) == 69 and heldout not in capabilities and heldout not in profiles
        assert all(profile["text"] == NARRATIVE for profile in profiles.values())
        assert all(profile["excerpts"][0]["source_text_start"] == 0 for profile in profiles.values())
    assert before == (corpus["store"].db_path.read_bytes(), corpus["keyword"].read_bytes())
    assert result["source_counts"]["unique_sections"] == result["source_counts"]["unique_documents"] == 1
    assert result["storage"]["pending_profile_characters"] == 0
    checkpoint = Path(result["checkpoint"])
    assert NARRATIVE.encode() not in bulk._native_path(checkpoint).read_bytes()
    with bulk._checkpoint_connection(checkpoint) as db:
        assert db.execute("SELECT count(*) FROM cases WHERE profile_json IS NOT NULL").fetchone()[0] == 0
        assert db.execute("SELECT reason FROM cases WHERE case_id=?", (heldout,)).fetchone()[0] == "administrator_holdout"
    assert json.loads(report.read_text(encoding="utf-8")) == result
    assert NARRATIVE not in report.read_text(encoding="utf-8")
    progress = json.loads(bulk._native_path(result["progress"]).read_text(encoding="utf-8"))
    assert progress["counts"]["committed"] == 67 and len(json.dumps(progress)) < 4000
    assert not any(result[key] for key in ("source_parsing", "embedding_invoked", "full_qmd_rebuild", "query_input_used", "handwritten_case_text"))
    again = bulk.run_bulk_case_contexts(corpus["root"], apply=True, resume=True)
    assert again["counts"] == result["counts"] and len(called) == 69 and len(refreshed) == 1


def test_dry_run_discards_all_profile_text_and_never_publishes(corpus, monkeypatch):
    corpus["add"](4)
    batches, refreshed = _publisher(monkeypatch)
    result = bulk.run_bulk_case_contexts(corpus["root"])
    assert result["state"] == "completed" and result["dry_run"]
    assert result["counts"]["derived"] == 4 and result["counts"]["committed"] == 0
    assert batches == refreshed == [] and NARRATIVE.encode() not in bulk._native_path(result["checkpoint"]).read_bytes()
    with bulk._checkpoint_connection(result["checkpoint"]) as db:
        assert db.execute("SELECT count(*) FROM cases WHERE profile_json IS NOT NULL").fetchone()[0] == 0


def test_stop_file_and_resume_reuse_pending_profiles_without_reextracting(corpus, monkeypatch, tmp_path):
    cases = corpus["add"](8)
    stop = tmp_path / "operator-stop"
    constructor = bulk.build_case_context
    calls = []

    def derive(store, identity):
        calls.append(identity)
        profile = constructor(store, identity)
        if len(calls) == 2:
            stop.touch()
        return profile

    monkeypatch.setattr(bulk, "build_case_context", derive)
    batches, refreshed = _publisher(monkeypatch)
    stopped = bulk.run_bulk_case_contexts(corpus["root"], apply=True, batch_size=5, stop_file=stop)
    assert stopped["state"] == "cancelled" and stopped["counts"]["staged"] == 2
    assert stopped["storage"]["pending_profile_characters"] > 0
    assert batches == refreshed == []
    stop.unlink()
    resumed = bulk.run_bulk_case_contexts(corpus["root"], apply=True, batch_size=5, resume=True, stop_file=stop)
    assert resumed["state"] == "completed" and resumed["counts"]["committed"] == 8
    assert [len(value[0]) for value in batches] == [5, 3]
    assert calls == [case["id"] for case in cases] and len(refreshed) == 1
    assert NARRATIVE.encode() not in bulk._native_path(resumed["checkpoint"]).read_bytes()


def test_publication_failure_retains_only_one_batch_and_replays_it_on_resume(corpus, monkeypatch):
    corpus["add"](8)
    constructor = bulk.build_case_context
    calls = []

    def derive(store, identity):
        calls.append(identity)
        return constructor(store, identity)

    monkeypatch.setattr(bulk, "build_case_context", derive)
    monkeypatch.setattr(bulk, "_publish_batch", lambda *args: (_ for _ in ()).throw(RuntimeError("Publication interrupted")))
    result = bulk.run_bulk_case_contexts(corpus["root"], apply=True, batch_size=5)
    assert result["state"] == "failed" and result["counts"]["staged"] == 5 and len(calls) == 5
    batches, refreshed = _publisher(monkeypatch)
    resumed = bulk.run_bulk_case_contexts(corpus["root"], apply=True, batch_size=5, resume=True)
    assert resumed["state"] == "completed" and len(calls) == 8
    assert [len(value[0]) for value in batches] == [5, 3] and len(refreshed) == 1


def test_resume_refuses_new_corpus_and_validation_bounds_cli(corpus, monkeypatch, capsys):
    corpus["add"](1)
    bulk.run_bulk_case_contexts(corpus["root"])
    corpus["active"]["generation"] = "different-generation"
    with pytest.raises(FileNotFoundError, match="No bulk checkpoint matches"):
        bulk.run_bulk_case_contexts(corpus["root"], resume=True)
    with pytest.raises(ValueError, match="between 1 and 64"):
        bulk.run_bulk_case_contexts(corpus["root"], batch_size=65)
    assert main(["--data-dir", str(corpus["root"]), "--all", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["derived"] == 1


def test_closed_holdout_policy_rejects_stale_registry_and_runner_lock_excludes_writers(corpus):
    cases = corpus["add"](1)
    _holdout(corpus, cases[0])
    corpus["add"](1)
    with pytest.raises(PermissionError, match="watermark"):
        bulk.run_bulk_case_contexts(corpus["root"])
    lock = corpus["root"] / "test-runner.lock"
    with bulk._runner_lock(lock):
        with pytest.raises(RuntimeError, match="Another bulk"):
            with bulk._runner_lock(lock):
                pytest.fail("A second writer acquired the lock")


def test_noop_publications_are_committed_and_counted(corpus, monkeypatch):
    corpus["add"](3)
    monkeypatch.setattr(bulk, "_publish_batch", lambda data_dir, profiles, capabilities:
                        {"published_profiles": len(profiles), "changed_documents": 0, "unchanged_documents": len(profiles)})
    monkeypatch.setattr(bulk, "_refresh_navigation", lambda _: None)
    result = bulk.run_bulk_case_contexts(corpus["root"], apply=True)
    assert result["counts"]["committed"] == result["counts"]["noop"] == result["unchanged_documents"] == 3
    assert result["changed_documents"] == 0 and result["storage"]["pending_profile_characters"] == 0


def test_corpus_change_during_extraction_stops_before_publication(corpus, monkeypatch):
    corpus["add"](3)
    constructor = bulk.build_case_context
    calls = []

    def derive(store, identity):
        calls.append(identity)
        profile = constructor(store, identity)
        if len(calls) == 2:
            corpus["active"]["corpus_hash"] = "changed-during-extraction"
        return profile

    monkeypatch.setattr(bulk, "build_case_context", derive)
    batches, refreshed = _publisher(monkeypatch)
    result = bulk.run_bulk_case_contexts(corpus["root"], apply=True, batch_size=2)
    assert result["state"] == "failed" and "changed" in result["error"]
    assert result["counts"]["staged"] == 2 and len(calls) == 2 and batches == refreshed == []
