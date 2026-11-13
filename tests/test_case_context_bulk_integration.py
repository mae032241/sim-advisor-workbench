"""Real offline QMD publication across the bulk runner's batch boundary."""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3

from advisor import case_context_store as contexts
from advisor.case_context_bulk import run_bulk_case_contexts
from advisor.case_navigation import validated_navigation
from advisor.object_index import qmd_path
from test_case_context_publication import (
    publication, _active, _put, _qmd, _source_snapshot, _stats,
)
from test_library_search import obj


def test_real_bulk_qmd_publishes_65_unfamiliar_cases_in_two_batches_and_resume_is_unchanged(publication):
    fixture = publication
    extra = []
    for index in range(63):
        case = obj("unfamiliar-rotating-channel-" + str(index).zfill(3), "models", "Original bounded model overview")
        template = fixture.cases[0]
        case.update(type="model_case", local_id="model-case", case_group="unfamiliar-rotating-group-" + str(index),
                    asset_key=template["asset_key"], asset_hash=template["asset_hash"],
                    coverage=copy.deepcopy(template["coverage"]))
        case["data"]["tutorial_refs"] = copy.deepcopy(template["data"]["tutorial_refs"])
        extra.append(case)
    _put(fixture.store, extra)
    fixture.cases.extend(extra)
    fixture.items.extend(extra)
    with fixture.store.object_stream() as objects:
        built = fixture.search.build(objects, modes=["qmd_keyword"], generation=objects.generation)
    assert built["modes"]["qmd_keyword"]["ready"]

    before_active = _active(fixture)
    before_qmd, before_stats = _qmd(fixture), _stats(fixture)
    source_snapshot = _source_snapshot(fixture)
    case_ids = {case["id"] for case in fixture.cases}
    case_paths = {qmd_path(case) for case in fixture.cases}
    ordinary_paths = set(before_qmd) - case_paths
    ordinary_ids = {item["id"] for item in fixture.items} - case_ids
    assert len(case_ids) == 65 and ordinary_paths and ordinary_ids

    result = run_bulk_case_contexts(fixture.store.root, apply=True, batch_size=64)
    # These synthetic archives intentionally contain no compact saved
    # configuration. Context publication succeeds, while full guide coverage
    # honestly remains partial rather than fabricating native settings.
    assert result["state"] == "partial", result
    assert result['navigation']['navigation']['prepared_case_count'] == 65
    assert result['navigation']['guides']['failed_count'] == 65
    for name in ("active_overviews", "eligible", "ready", "committed", "published"):
        assert result["counts"][name] == 65
    for name in ("heldout", "degraded", "failed", "pending", "staged", "noop"):
        assert result["counts"][name] == 0
    assert result["published_batches"] == 2 and result["changed_documents"] == 65
    assert result["unchanged_documents"] == 0 and result["navigation_refreshed"]
    assert result["source_parsing"] is result["embedding_invoked"] is result["full_qmd_rebuild"] is False
    assert _source_snapshot(fixture) == source_snapshot

    after_active, after_qmd, after_stats = _active(fixture), _qmd(fixture), _stats(fixture)
    assert {key: after_active[key] for key in ("generation", "corpus_hash", "directory")} == {
        key: before_active[key] for key in ("generation", "corpus_hash", "directory")}
    assert set(after_qmd) == set(before_qmd)
    assert {key: after_qmd[key] for key in ordinary_paths} == {key: before_qmd[key] for key in ordinary_paths}
    assert {key: after_stats[key] for key in ordinary_ids} == {key: before_stats[key] for key in ordinary_ids}
    for path in case_paths:
        assert after_qmd[path][:4] == before_qmd[path][:4]
        assert "DOCUMENTED CASE CONTEXT" in after_qmd[path][9]
        assert "reactive suspension" in after_qmd[path][9]
    assert all(after_stats[identity]["case_context_included"] for identity in case_ids)

    generation = Path(after_active["directory"])
    manifest_path = generation / "manifest.json"
    assert manifest_path.stat().st_size < 8 * 1024
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["bridge_version"] == "2-context2" and manifest["case_context_count"] == 65
    assert not isinstance(manifest.get("case_context_dependencies"), dict)
    validated = contexts.validated_contexts(fixture.root, after_active)
    assert validated["meta"]["context_count"] == validated["meta"]["capability_count"] == 65
    with closing(sqlite3.connect(contexts.context_path(fixture.root))) as db:
        assert {row[0] for row in db.execute("SELECT case_id FROM contexts")} == case_ids
        assert {row[0] for row in db.execute("SELECT DISTINCT source_id FROM dependencies")} == {
            section["id"] for section in fixture.sections}
    validated_navigation(fixture.root, after_active)
    assert not contexts.pending_path(fixture.root).exists()
    checkpoint = Path(result["checkpoint"])
    with closing(sqlite3.connect(checkpoint)) as db:
        assert db.execute("SELECT count(*) FROM cases WHERE profile_json IS NOT NULL").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM cases WHERE state='published'").fetchone()[0] == 65
    assert result["storage"]["pending_profile_characters"] == 0
    assert result["storage"]["checkpoint_compacted"] is True
    assert result["storage"]["case_context_sidecar_bytes"] == contexts.context_path(fixture.root).stat().st_size
    assert fixture.sections[0]["text"].encode("utf-8") not in checkpoint.read_bytes()

    keyword = generation / "qmd-keyword.sqlite3"
    unchanged_files = [keyword, generation / "objects.sqlite3", manifest_path,
                       contexts.context_path(fixture.root), fixture.root / "active.json", fixture.root / "last-build.json"]
    after_files = {str(path): (path.stat().st_mtime_ns, path.read_bytes()) for path in unchanged_files}
    repeated = run_bulk_case_contexts(fixture.store.root, apply=True, batch_size=64, resume=True)
    assert repeated["state"] == result["state"] == "partial" and repeated["published_batches"] == 2
    assert repeated["counts"] == result["counts"]
    assert {str(path): (path.stat().st_mtime_ns, path.read_bytes()) for path in unchanged_files} == after_files
    assert _qmd(fixture) == after_qmd and _stats(fixture) == after_stats
    assert _source_snapshot(fixture) == source_snapshot and not contexts.pending_path(fixture.root).exists()
