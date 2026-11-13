"""Scripted, source-backed case context publication without source reparsing."""
from __future__ import annotations

from contextlib import closing
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from collections import Counter


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def context_title(item, context=None):
    title = str(item.get("title") or item["id"])
    if context is None:
        return title
    # A language-neutral identity from the actual source filename, not query
    # expansion or a hand-authored list of aliases for the pilot questions.
    name = str(item.get("asset_key", "")).rsplit("/", 1)[-1].split(":")[-1].rsplit(".", 1)[0]
    return title + (" · " + name.replace("_", " ") if name else "")


def context_representation(item, base_text, stats, context):
    if item.get("type") != "model_case" or context.get("case_id") != item["id"] or context.get("library_id") != item["library_id"]:
        raise ValueError("Case context differs from its immutable overview identity")
    text = context.get("text")
    if not isinstance(text, str) or not text or len(text) > 3600:
        raise ValueError("Case context must contain bounded extractive source text")
    enriched = "DOCUMENTED CASE CONTEXT (tutorial excerpts; model agreement not verified):\n" + text + "\n\n" + base_text
    return enriched, dict(stats, policy="case-context-extractive/v1", indexed_characters=len(enriched),
                          character_budget=5200, case_context_included=True,
                          case_context_hash=context["content_hash"],
                          case_context_basis="documented_not_model_validated",
                          case_context_source_ids=[ref.get("id", ref.get("source_id")) for ref in context.get("source_refs", [])])


def profiles_for_build(search_root):
    """Load only previously opted-in profiles; rederive from current sources."""
    from .case_context_store import assert_no_pending, read_contexts
    from .case_context import build_case_context
    root = Path(search_root)
    assert_no_pending(root)
    if not (root / "runtime/case-contexts.sqlite3").exists():
        return {}
    from scripts.normalize_tutorials import ReadOnlyLibraryStore
    store = ReadOnlyLibraryStore(root.parent)
    result = {}
    for identity in read_contexts(root):
        try:
            profile = build_case_context(store, identity)
        except (FileNotFoundError, PermissionError):
            continue
        if profile.get("state") == "ready" and profile.get("text"):
            result[identity] = profile
    return result


def context_readable(store, profile):
    """A derived excerpt is readable only while every exact source is active."""
    return profile["case_id"] in readable_contexts(store, {profile["case_id"]: profile})


def readable_contexts(store, profiles):
    """Authorize one result batch; retain no permissions across requests."""
    if not profiles:
        return set()
    visible = set(store.searchable_library_ids())
    from .object_index import qmd_path
    policy = store.evaluation_policy()
    identities = list(dict.fromkeys(identity for profile in profiles.values()
                     for identity in [*profile.get("lineage_ids", [profile["case_id"]]),
                                      *(ref.get("id", ref.get("source_id")) for ref in profile.get("source_refs", []))]))
    sources, active, aliases = {}, set(), set()
    try:
        with closing(store.connect()) as db:
            has_aliases = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tutorial_aliases'").fetchone()
            for start in range(0, len(identities), 400):
                batch = identities[start:start + 400]
                placeholders = ",".join("?" for _ in batch)
                # Probe revision IDs and the existing active (library,object)
                # primary key, never scan the catalog or decode model bodies.
                for identity, raw, current in db.execute(
                        "SELECT r.id,r.payload,a.citation_id FROM revisions r LEFT JOIN active a "
                        "ON a.library_id=r.library_id AND a.object_id=r.object_id WHERE r.id IN (" + placeholders + ")", batch):
                    sources[identity] = json.loads(raw)
                    if current == identity:
                        active.add(identity)
                if has_aliases:
                    aliases.update(row[0] for row in db.execute(
                        "SELECT citation_id FROM tutorial_aliases WHERE citation_id IN (" + placeholders + ")", batch))
        # Rare historical aliases keep their existing canonical-owner gates.
        for identity in aliases:
            sources[identity] = store.get(identity)

        def allowed(identity):
            value = sources.get(identity)
            return bool(value is not None and not policy.blocks_identity(identity)
                        and not policy.blocks_object(value) and qmd_path(value) not in policy.qmd_paths)

        readable = set()
        for case_id, profile in profiles.items():
            if not all(allowed(identity) for identity in profile.get("lineage_ids", [case_id])):
                continue
            valid = True
            for ref in profile.get("source_refs", []):
                identity = ref.get("id", ref.get("source_id"))
                if (ref["library_id"] not in visible or identity not in active or not allowed(identity)
                        or sources[identity]["library_id"] != ref["library_id"]):
                    valid = False
                    break
            if valid:
                readable.add(case_id)
        # Lifecycle and holdout changes during the batch win over prior reads.
        if set(store.searchable_library_ids()) != visible or store.evaluation_policy() != policy:
            return set()
        return readable
    except (PermissionError, FileNotFoundError):
        return set()


def dependency_library_scope(store):
    """Discovery-only library metadata, never excerpts or evidence credit."""
    from .case_context_store import context_path
    from .index_manifest import _owned
    root = store.root / "search"
    path = context_path(root)
    if not path.is_file():
        return {}
    _owned(root, path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        pairs = db.execute("SELECT DISTINCT c.library_id,d.library_id FROM contexts c JOIN dependencies d ON d.case_id=c.case_id").fetchall()
    visible = set(store.searchable_library_ids())
    dependencies = {}
    for owner, source in pairs:
        dependencies.setdefault(owner, []).append(source)
    return {"case_context_dependencies": dependencies,
            "case_context_disabled_dependencies": sorted({source for _, source in pairs if source not in visible})}


def _active(root):
    from .index_manifest_cache import read_manifest
    value = read_manifest(Path(root) / "active.json", summary=True, resolve_paths=True)
    if value is None:
        raise RuntimeError("An existing QMD keyword generation is required")
    directory = Path(value.get("directory", "")).resolve()
    if not directory.is_relative_to(Path(root).resolve() / "generations"):
        raise ValueError("Case context index escapes the owned generation directory")
    return value


def _keyword_path(active):
    from .qmd_shared import KEYWORD_STORAGE
    if active.get("keyword_storage") != KEYWORD_STORAGE or not active.get("modes", {}).get("qmd_keyword", {}).get("ready"):
        raise ValueError("Case contexts require the existing ready shared QMD keyword index")
    return Path(active["directory"]) / "qmd-keyword.sqlite3"


def _body_hash(title, text):
    return hashlib.sha256(("# " + title + "\n\n" + text).replace("\n", os.linesep).encode("utf-8")).hexdigest()


def _profile_entry(store, profile):
    from .library_search import _representation
    from .object_index import qmd_path
    raw = store.get(profile["case_id"])
    if raw is None:
        raise FileNotFoundError("Case context authority is missing")
    text, stats = _representation(raw, profile)
    path, title = qmd_path(raw), context_title(raw, profile)
    return dict(profile, qmd_path=path, qmd_hash=_body_hash(title, text)), {
        "collection": path.split("/")[0], "path": path, "title": title, "text": text}, stats


def _capabilities(store, *, exclude_library=None):
    from .case_context import classify_case_metadata
    from .case_navigation import _OVERVIEWS_SQL
    result = {}
    with closing(store.connect()) as db:
        for identity, owner, _ in db.execute(_OVERVIEWS_SQL):
            if owner != exclude_library:
                result[identity] = classify_case_metadata(store.get(identity))
    return result


def _raw_capabilities(root):
    from .case_context_store import context_path
    path = context_path(root)
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return {identity: json.loads(raw) for identity, raw in db.execute("SELECT case_id,evidence_json FROM capabilities")}


def _mark_manifest(active, profiles, search_root):
    from .case_context_dependencies import mark_generation_dependencies
    value = dict(active)
    # Old adapters must reject this index instead of bypassing the new source
    # dependency gates. New adapters still read untouched recipe-2 indexes.
    value.update(bridge_version="2-context2", case_contexts_required=True, case_context_schema="case-context/v1")
    return mark_generation_dependencies(search_root, value, profiles)


def rebind_for_publish(search_root, active, profiles, *, capabilities=None):
    """Bind the small cache to a newly written index, without changing QMD."""
    from scripts.normalize_tutorials import ReadOnlyLibraryStore
    from .case_context_store import write_contexts
    root = Path(search_root)
    store = ReadOnlyLibraryStore(root.parent)
    entries = {identity: _profile_entry(store, profile)[0] for identity, profile in profiles.items()}
    write_contexts(root, active, entries, _capabilities(store) if capabilities is None else capabilities)
    return _mark_manifest(active, entries, root)


def _atomic(path, payload):
    from scripts.normalize_tutorials import _atomic_bytes
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _atomic_bytes(Path(path), _json(payload).encode("utf-8"))


def _runtime(engine):
    return {"qmd_dir": engine.qmd_dir, "node": engine.node, "env": engine._env()}


def _backup(path):
    return base64.b64encode(path.read_bytes()).decode("ascii") if path.exists() else None


def _restore_file(path, backup):
    from scripts.normalize_tutorials import _atomic_bytes
    if backup is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_bytes(path, base64.b64decode(backup, validate=True))


def _update_stats(database, updates):
    from .object_index import encode_representation
    with closing(sqlite3.connect(database)) as db, db:
        for identity, stats in updates.items():
            if db.execute("UPDATE objects SET representation_json=? WHERE id=?", (encode_representation(stats), identity)).rowcount != 1:
                raise ValueError("Case context statistics target disappeared")
    # A persistent WAL must not change the metadata identity after publication.
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _refresh_navigation(store, prepared):
    from .case_navigation import build_navigation
    from .case_guides import build_case_guides
    navigation = build_navigation(store, case_ids=prepared)
    compact_cases = (None if prepared is None else
                     [identity for identity in prepared if (store.get(identity) or {}).get("data", {}).get("compact_case")])
    guides = build_case_guides(store, compact_cases) if compact_cases is None or compact_cases else None
    return {"navigation": navigation, "guides": guides}


def _prepared_cases(root):
    """Preserve full-scope intent, or legacy selected IDs, during publication."""
    from .case_navigation import navigation_path
    from .index_manifest import _owned
    path = navigation_path(root)
    if not path.is_file():
        return []
    _owned(root, path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        mode = db.execute("SELECT value FROM meta WHERE key='build_scope'").fetchone()
        if mode and json.loads(mode[0]) == "all":
            return None
        row = db.execute("SELECT value FROM meta WHERE key='prepared_case_ids'").fetchone()
        if row is None and db.execute("SELECT 1 FROM sqlite_master WHERE name='prepared_cases'").fetchone():
            return [value[0] for value in db.execute("SELECT case_id FROM prepared_cases ORDER BY case_id")]
    values = json.loads(row[0]) if row else []
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("Invalid prepared navigation identities")
    return values


def _prune_dependencies(root, active):
    from .case_context_dependencies import prune_generation_dependencies
    try:
        return prune_generation_dependencies(root, active)
    except (ValueError, OSError, RuntimeError) as exc:
        # Cleanup follows the commit point and must never roll a valid manifest
        # back to a dependency snapshot that may already have been removed.
        return {"cleanup_warning": str(exc)[:300]}


def publish_context_batch(data_dir, profiles, *, capabilities=None, refresh_navigation=False):
    """Commit at most 64 derived contexts with undo bounded to this batch."""
    from scripts.normalize_tutorials import ReadOnlyLibraryStore, _offline_build_lease
    from .case_context_store import (assert_no_pending, context_path, pending_path,
        prepare_context_batch, upsert_context_batch, restore_context_batch)
    from .index_manifest import publish_manifest
    from .library_search import LibrarySearch
    from .object_index import representation_stats
    from .qmd_patch import prepare_keyword_document_patch, patch_keyword_documents, restore_keyword_document_patch
    from .qmd_worker import suspend_workers
    store = ReadOnlyLibraryStore(data_dir)
    root = store.root / "search"
    assert_no_pending(root)
    values = list(profiles.values()) if isinstance(profiles, dict) else list(profiles)
    if not 1 <= len(values) <= 64 or len({profile["case_id"] for profile in values}) != len(values):
        raise ValueError("Select one to 64 unique case contexts per publication")
    for profile in values:
        original = {key: value for key, value in profile.items() if key not in {"content_hash", "qmd_path", "qmd_hash"}}
        if profile.get("state") != "ready" or hashlib.sha256(_json(original).encode("utf-8")).hexdigest() != profile.get("content_hash"):
            raise ValueError("Case context is incomplete or its immutable content hash changed")
        raw = store.get(profile["case_id"])
        if raw is None:
            raise FileNotFoundError("Selected case metadata is missing")
        store.require_not_heldout(raw["id"], raw)
        if any(profile.get(key) != raw.get(key) for key in ("library_id", "case_group", "asset_key", "asset_hash")):
            raise ValueError("Case context differs from its source asset")
        for ref in profile["source_refs"]:
            store.require_not_heldout(ref.get("id", ref.get("source_id")), store.get(ref.get("id", ref.get("source_id"))))
    active = _active(root)
    database, objects = _keyword_path(active), Path(active["directory"]) / "objects.sqlite3"
    server = store.root.parent / "server.json"
    if server.exists():
        record = json.loads(server.read_text(encoding="utf-8"))
        if isinstance(record.get("pid"), int) and LibrarySearch._pid_alive(record["pid"]):
            raise RuntimeError("请先停止工作台服务，再增量更新案例上下文；完成后使用新版插件启动。")
    engine = LibrarySearch(root)
    options = _runtime(engine)
    identities = [profile["case_id"] for profile in values]
    with _offline_build_lease(store), suspend_workers(root):
        current = _active(root)
        if any(current.get(key) != active.get(key) for key in ("directory", "generation", "corpus_hash")):
            raise RuntimeError("QMD generation changed before context publication")
        old_hashes = {}
        if context_path(root).exists():
            with closing(sqlite3.connect(context_path(root).as_uri() + "?mode=ro", uri=True)) as db:
                marks = ",".join("?" for _ in identities)
                old_hashes = dict(db.execute("SELECT case_id,qmd_hash FROM contexts WHERE case_id IN (" + marks + ")", identities))
        entries, updates, stats = {}, [], {}
        for profile in values:
            entry, update, statistics = _profile_entry(store, profile)
            identity = profile["case_id"]
            if identity in old_hashes:
                update["expected_old_hash"] = old_hashes[identity]
            entries[identity] = entry
            updates.append(update)
            stats[identity] = statistics
        with closing(sqlite3.connect(objects.as_uri() + "?mode=ro", uri=True)) as db:
            old_stats = {identity: representation_stats(db, identity) for identity in identities}
            if any(value is None for value in old_stats.values()):
                raise ValueError("Selected case is missing from the current object index")
        store_undo = prepare_context_batch(root, active, entries, capabilities=capabilities)
        patch_plan = prepare_keyword_document_patch(database, updates, runtime_dir=root / "runtime", **options)
        prepared = _prepared_cases(root)
        journal = {"schema": "case-context-batch-update/v1", "pid": os.getpid(),
                   "directory": str(Path(active["directory"]).relative_to(root)), "generation": active["generation"],
                   "manifest": active, "prepared_cases": prepared, "old_statistics": old_stats,
                   "store_undo": store_undo, "undo": patch_plan["undo"], "updates": updates, "plan": patch_plan["plan"]}
        if len(_json(journal).encode("utf-8")) > 16 * 1024 * 1024:
            raise ValueError("Context publication undo exceeds its bounded recovery size")
        _atomic(pending_path(root), journal)
        committed = False
        try:
            patch = patch_keyword_documents(database, updates, expected_plan=patch_plan["plan"], runtime_dir=root / "runtime", **options)
            committed = True
            changed_stats = {key: value for key, value in stats.items() if value != old_stats[key]}
            if changed_stats:
                _update_stats(objects, changed_stats)
            metadata = upsert_context_batch(root, active, entries, capabilities=capabilities, expected=store_undo)
            updated = _mark_manifest(active, None, root)
            summary = dict(updated.get("representation", {}))
            if isinstance(summary.get("indexed_characters"), int):
                summary["indexed_characters"] += sum(stats[key]["indexed_characters"] - old_stats[key]["indexed_characters"] for key in stats)
            updated["representation"] = summary
            publish_manifest(Path(updated["directory"]), updated, root, cancellable=False)
            if refresh_navigation:
                _refresh_navigation(store, prepared)
            pending_path(root).unlink()
            cleanup = _prune_dependencies(root, updated)
            return {"changed_documents": patch["changed"], "unchanged_documents": patch["unchanged"],
                    "published_profiles": len(values), "metadata_only_capabilities": metadata["meta"]["capability_count"],
                    "generation_unchanged": True, "profile_text_characters": sum(len(value["text"]) for value in values),
                    "undo_bytes": len(_json(journal).encode("utf-8")), "dependency_cleanup": cleanup,
                    "full_qmd_rebuild": False, "embedding_invoked": False}
        except BaseException:
            if committed:
                restore_keyword_document_patch(database, patch_plan["undo"], runtime_dir=root / "runtime", **options)
            _update_stats(objects, old_stats)
            restore_context_batch(root, active, store_undo)
            publish_manifest(Path(active["directory"]), active, root, cancellable=False)
            if refresh_navigation:
                _refresh_navigation(store, prepared)
            pending_path(root).unlink(missing_ok=True)
            _prune_dependencies(root, active)
            raise


def refresh_bulk_navigation(data_dir):
    from scripts.normalize_tutorials import ReadOnlyLibraryStore, _offline_build_lease
    from .qmd_worker import suspend_workers
    store = ReadOnlyLibraryStore(data_dir)
    root = store.root / "search"
    with _offline_build_lease(store), suspend_workers(root):
        result = _refresh_navigation(store, None)
    return {"build_scope": "all", "navigation_refreshed": True, **result}


def update_case_contexts(data_dir, case_ids, *, apply=False):
    """Derive reusable source text and patch only the selected overview rows."""
    from scripts.normalize_tutorials import ReadOnlyLibraryStore, _offline_build_lease
    from .case_context import build_case_context
    from .case_context_store import context_path, pending_path, assert_no_pending, read_contexts, write_contexts
    from .case_navigation import validated_navigation
    from .index_manifest import publish_manifest
    from .library_search import LibrarySearch
    from .object_index import representation_stats
    from .qmd_patch import prepare_keyword_document_patch, patch_keyword_documents, restore_keyword_document_patch
    from .qmd_worker import suspend_workers
    store = ReadOnlyLibraryStore(data_dir)
    root = store.root / "search"
    assert_no_pending(root)
    identities = list(dict.fromkeys(case_ids))
    if not identities or len(identities) > 64:
        raise ValueError("Select one to 64 exact case IDs for an incremental batch")
    active = _active(root)
    database = _keyword_path(active)
    objects = Path(active["directory"]) / "objects.sqlite3"
    with closing(sqlite3.connect(objects.as_uri() + "?mode=ro", uri=True)) as db:
        for identity in identities:
            if db.execute("SELECT 1 FROM objects WHERE id=?", (identity,)).fetchone() is None:
                raise ValueError("Selected case is not in the current QMD generation")
    profiles = {identity: build_case_context(store, identity) for identity in identities}
    degraded = [identity for identity, profile in profiles.items() if profile.get("state") != "ready"]
    capabilities = _capabilities(store)
    result = {"schema": "case-context-update/v1", "dry_run": not apply,
              "case_ids": identities, "profiles": profiles, "degraded": degraded,
              "capability_counts": dict(Counter(value["classification"] for value in capabilities.values())),
              "source_parsing": False, "embedding_invoked": False, "full_qmd_rebuild": False,
              "handwritten_case_text": False, "query_input_used": False}
    if not apply:
        return result
    if degraded:
        raise ValueError("Selected cases have no complete canonical context; inspect the dry-run gaps")
    result.update(publish_context_batch(data_dir, profiles, capabilities=capabilities, refresh_navigation=True))
    return result



def recover_case_context_update(data_dir):
    """Restore one interrupted incremental transaction without corpus copying."""
    from scripts.normalize_tutorials import ReadOnlyLibraryStore, _offline_build_lease
    from .case_context_store import context_path, pending_path, read_contexts, write_contexts, restore_context_batch
    from .index_manifest import publish_manifest
    from .library_search import LibrarySearch
    from .qmd_patch import restore_keyword_document_patch
    from .qmd_worker import suspend_workers
    store = ReadOnlyLibraryStore(data_dir)
    root = store.root / "search"
    marker = pending_path(root)
    if not marker.exists():
        return {"schema": "case-context-update/v1", "recovered": False, "detail": "No interrupted context update"}
    if marker.is_symlink() or marker.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Invalid context recovery journal")
    journal = json.loads(marker.read_text(encoding="utf-8"))
    active = _active(root)
    directory = Path(active["directory"])
    if (journal.get("schema") not in {"case-context-update/v1", "case-context-batch-update/v1"} or journal.get("generation") != active["generation"]
            or journal.get("directory") != str(directory.relative_to(root))):
        raise RuntimeError("Context recovery belongs to a different active index")
    # A process crash releases its OS search lock, but may leave this small
    # lease file. Reclaim only the journal owner's confirmed dead lease.
    lease = store.root / ".build.lock"
    if lease.exists():
        owner = json.loads(lease.read_text(encoding="utf-8"))
        if owner.get("pid") != journal.get("pid") or LibrarySearch._pid_alive(owner.get("pid")):
            raise RuntimeError("Another live owner must finish before context recovery")
        lease.unlink()
    engine = LibrarySearch(root)
    with _offline_build_lease(store), suspend_workers(root):
        restore_keyword_document_patch(_keyword_path(active), journal["undo"], runtime_dir=root / "runtime", **_runtime(engine))
        _update_stats(directory / "objects.sqlite3", journal["old_statistics"])
        if journal["schema"] == "case-context-batch-update/v1":
            restore_context_batch(root, journal["manifest"], journal["store_undo"])
        else:
            _restore_file(context_path(root), journal["old_sidecar"])
            if journal["old_sidecar"] is not None:
                write_contexts(root, journal["manifest"], read_contexts(root), _raw_capabilities(root))
        publish_manifest(directory, journal["manifest"], root, cancellable=False)
        _refresh_navigation(store, journal["prepared_cases"])
        marker.unlink()
        _prune_dependencies(root, journal["manifest"])
    return {"schema": "case-context-update/v1", "recovered": True, "restored_documents": len(journal["old_statistics"]),
            "embedding_invoked": False, "full_qmd_rebuild": False}


def rebind_case_contexts(data_dir):
    """Refresh file identities after directory migration; keep all QMD bytes."""
    from scripts.normalize_tutorials import ReadOnlyLibraryStore, _offline_build_lease
    from .case_context_store import assert_no_pending, read_contexts, write_contexts
    from .library_search import LibrarySearch
    from .qmd_patch import prepare_keyword_document_patch
    from .qmd_worker import suspend_workers
    store = ReadOnlyLibraryStore(data_dir)
    root = store.root / "search"
    assert_no_pending(root)
    active = _active(root)
    profiles = read_contexts(root)
    engine = LibrarySearch(root)
    with _offline_build_lease(store), suspend_workers(root):
        entries, updates = {}, []
        for identity, profile in profiles.items():
            entry, update, _ = _profile_entry(store, profile)
            update["expected_old_hash"] = profile["qmd_hash"]
            entries[identity] = entry
            updates.append(update)
        for offset in range(0, len(updates), 64):
            plan = prepare_keyword_document_patch(_keyword_path(active), updates[offset:offset + 64], runtime_dir=root / "runtime", **_runtime(engine))
            if plan["changed"]:
                raise RuntimeError("QMD text differs from saved case contexts; explicit context reconstruction is required")
        write_contexts(root, active, entries, _capabilities(store))
    return {"schema": "case-context-update/v1", "rebound": len(entries), "metadata_only": True,
            "qmd_documents_changed": 0, "embedding_invoked": False, "full_qmd_rebuild": False}


def strip_contexts_for_purge(search_root, library_id, *, on_progress=None):
    """Remove dependent extractive text before its source library is purged.

    Called inside the existing purge lease/worker suspension. Replaying a
    partial purge is harmless: the replacement is the same original overview.
    """
    from scripts.normalize_tutorials import ReadOnlyLibraryStore
    from .case_context_store import assert_no_pending, context_path, read_contexts, write_contexts
    from .index_manifest import _owned, publish_manifest
    from .index_manifest_cache import read_manifest
    from .library_search import LibrarySearch, _representation
    from .object_index import qmd_path, representation_stats
    from .qmd_patch import patch_keyword_documents
    from .case_context_dependencies import dependent_case_ids, remove_generation_dependencies
    root = Path(search_root)
    assert_no_pending(root)
    path = context_path(root)
    if not path.exists():
        if (root / "active.json").exists() and _active(root).get("case_contexts_required"):
            raise RuntimeError("Required case context metadata is missing; recover it before library purge")
        return {"restored_overviews": 0, "removed_profiles": 0}
    store = ReadOnlyLibraryStore(root.parent)
    profiles = read_contexts(root)
    affected = {identity for identity, profile in profiles.items()
                if profile["library_id"] == library_id or any(ref["library_id"] == library_id for ref in profile["source_refs"])}
    restored = 0
    engine = None
    # Retained generations disclose only dependency ownership in the manifest;
    # their full tutorial text is not copied to a second cache per generation.
    for directory in sorted((root / "generations").iterdir()):
        _owned(root, directory)
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue
        from .index_paths import resolve_index_manifest
        manifest = resolve_index_manifest(read_manifest(manifest_path, summary=True), root)
        ids = dependent_case_ids(root, manifest, library_id)
        if not ids:
            continue
        database, objects = directory / "qmd-keyword.sqlite3", directory / "objects.sqlite3"
        updates, stats = [], {}
        with closing(sqlite3.connect(objects.as_uri() + "?mode=ro", uri=True)) as db:
            old_stats = {}
            for identity in sorted(ids):
                raw = store.get(identity)
                if raw is None:
                    raise RuntimeError("Dependent archived model metadata is missing during purge")
                if raw["library_id"] == library_id:
                    continue  # The normal purge removes that entire collection.
                text, statistics = _representation(raw)
                locator = qmd_path(raw)
                updates.append({"collection": locator.split("/")[0], "path": locator,
                                "title": raw["title"], "text": text})
                stats[identity] = statistics
                old_stats[identity] = representation_stats(db, identity)
        if updates:
            engine = engine or LibrarySearch(root)
            for offset in range(0, len(updates), 64):
                patch_keyword_documents(database, updates[offset:offset + 64], runtime_dir=root / "runtime", **_runtime(engine))
                if on_progress:
                    on_progress({"stage": "indexes", "phase": "case_context_cleanup", "completed": min(offset + 64, len(updates)),
                                 "total": len(updates), "unit": "overviews"})
            _update_stats(objects, stats)
            restored += len(updates)
            if isinstance(manifest.get("representation", {}).get("indexed_characters"), int):
                manifest["representation"]["indexed_characters"] += sum(stats[key]["indexed_characters"] - old_stats[key]["indexed_characters"] for key in stats)
        manifest = remove_generation_dependencies(root, manifest, ids)
        active = _active(root)
        publish_manifest(directory, manifest, root, active=Path(active["directory"]) == directory, last_build=False, cancellable=False)
        _prune_dependencies(root, manifest)
    remaining = {identity: profile for identity, profile in profiles.items() if identity not in affected}
    active = _active(root)
    write_contexts(root, active, remaining, _raw_capabilities(root))
    # Also cover missing/old dependency manifests without discarding the guard.
    updated = _mark_manifest(active, remaining, root)
    publish_manifest(Path(active["directory"]), updated, root, last_build=False, cancellable=False)
    _prune_dependencies(root, updated)
    return {"restored_overviews": restored, "removed_profiles": len(affected)}


def rebind_after_purge(search_root):
    """Keep dependency gates valid after the purge retargets object metadata."""
    from .case_context_store import context_path, read_contexts, write_contexts
    from .index_manifest import publish_manifest
    root = Path(search_root)
    if not context_path(root).exists():
        return
    if not (root / "active.json").exists():
        if not read_contexts(root):
            context_path(root).unlink()
        return
    active = _active(root)
    objects = Path(active["directory"]) / "objects.sqlite3"
    if not objects.exists():
        return
    profiles, capabilities = read_contexts(root), _raw_capabilities(root)
    with closing(sqlite3.connect(objects.as_uri() + "?mode=ro", uri=True)) as db:
        exists = lambda identity: db.execute("SELECT 1 FROM objects WHERE id=?", (identity,)).fetchone() is not None
        profiles = {identity: value for identity, value in profiles.items() if exists(identity)}
        capabilities = {identity: value for identity, value in capabilities.items() if exists(identity)}
    write_contexts(root, active, profiles, capabilities)
    updated = _mark_manifest(active, profiles, root)
    publish_manifest(Path(active["directory"]), updated, root, last_build=False, cancellable=False)
    _prune_dependencies(root, updated)
