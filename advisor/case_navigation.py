"""Revision-bound case scopes for the existing QMD retrieval backend.

This disposable sidecar stores identities and classifications only. It neither
contains source text nor grants access: live library/holdout checks still govern
every query and source read.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
import gzip
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .build_control import check_cancelled
from .case_metadata import FEATURE_BRANCHES
from .catalog_storage import register_catalog_functions
from .compact_api_overlay import pinned_base_authority
from .evaluation_sets import _watermark
from .index_manifest import _owned
from .index_manifest_cache import read_manifest
from .query_scope_counts import database_signature

SCHEMA = "case-navigation/v2"
LEGACY_SCHEMA = "case-navigation/v1"
FILENAME = "case-navigation.sqlite3"
STAGES = ("overview", *FEATURE_BRANCHES, "reference")
_LOW_VALUE_KINDS = {"Frame", "FrameFeature", "ViewList", "View", "ViewFeatureList", "ViewFeature",
                    "HideDrawList", "HideGeomList", "HideMeshList", "ResultFeature", "ResultFeatureList",
                    "ColorTableFeature", "ColorTableFeatureList"}
_DISPLAY_PROPERTIES = {"showPhysicsSymbols", "showphysicssymbols", "defaultPlotIDs", "showgeom", "showmesh",
                       "showsolutionparams", "showlegend", "legendpos", "titletype", "titlenumberformat",
                       "colortable", "linewidth", "linestyle", "expanded", "collapsed", "selected", "windowid"}
_OVERVIEWS_SQL = """SELECT r.id,r.library_id,m.group_id FROM case_members m
  JOIN revisions r ON r.id=m.citation_id
  JOIN active a ON a.library_id=r.library_id AND a.object_id=r.object_id AND a.citation_id=r.id
  WHERE m.local_id='model-case' ORDER BY r.library_id,r.object_id"""


class NavigationStale(RuntimeError):
    state = "stale"


class NavigationNotPrepared(RuntimeError):
    state = "not_prepared"


class _NavigationExcluded(PermissionError):
    """A known holdout is skipped only during an explicit full compilation."""


def navigation_path(search_root):
    return Path(search_root).resolve() / "runtime" / FILENAME


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _read_only(path):
    db = register_catalog_functions(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30))
    db.execute("PRAGMA query_only=ON")
    return db


def _objects_path(search_root, active):
    root = Path(search_root).resolve()
    value = active.get("directory")
    if not isinstance(value, str):
        raise NavigationStale("Case navigation requires a current QMD generation")
    path = Path(value)
    path = path.resolve() if path.is_absolute() else (root / path).resolve()
    if not path.is_relative_to(root / "generations") or not (path / "objects.sqlite3").is_file():
        raise NavigationStale("Case navigation generation no longer exists")
    return path / "objects.sqlite3"


def _identity(search_root, active):
    path = _objects_path(search_root, active)
    return {"generation": active.get("generation"), "corpus_hash": active.get("corpus_hash"),
            "objects_signature": database_signature(path)}


def validated_navigation(search_root, active):
    """Validate generation binding; this metadata never authorizes a source."""
    path = navigation_path(search_root)
    try:
        _owned(Path(search_root).resolve(), path)
    except ValueError as exc:
        raise NavigationStale("Case navigation refuses aliased runtime metadata paths") from exc
    if (not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1
            or getattr(path, "is_junction", lambda: False)()):
        raise NavigationNotPrepared("Case navigation is not_prepared; prepare its metadata first")
    expected = _identity(search_root, active)
    try:
        with closing(_read_only(path)) as db:
            meta = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM meta")}
            if meta.get("schema") == SCHEMA:
                columns = {row[1]: row for row in db.execute("PRAGMA table_info(prepared_cases)")}
                if ("case_id" not in columns or columns["case_id"][5] != 1
                        or type(meta.get("prepared_case_count")) is not int or meta["prepared_case_count"] < 0
                        or meta.get("build_scope") not in {"all", "selected", "overviews"}):
                    raise ValueError("Invalid prepared navigation table")
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise NavigationStale("Case navigation metadata is unreadable; prepare it again") from exc
    if meta.get("schema") not in {SCHEMA, LEGACY_SCHEMA} or any(meta.get(key) != value for key, value in expected.items()):
        raise NavigationStale("Case navigation metadata is stale; prepare it for the current QMD generation")
    result = {"path": str(path), "meta": meta}
    if meta["schema"] == LEGACY_SCHEMA:
        prepared = meta.get("prepared_case_ids")
        if not isinstance(prepared, list) or any(not isinstance(value, str) for value in prepared):
            raise NavigationStale("Case navigation pilot metadata is invalid")
        result.update(prepared_case_ids=list(prepared), prepared_case_count=len(prepared))
    else:
        result["prepared_case_count"] = meta["prepared_case_count"]
    return result


def iter_prepared_case_ids(navigation):
    """Stream identities from validated metadata, including historical pilots.

    This is an offline enumeration helper, never a source-access decision.
    """
    if navigation["meta"]["schema"] == LEGACY_SCHEMA:
        yield from navigation["prepared_case_ids"]
        return
    with closing(_read_only(navigation["path"])) as db:
        for (case_id,) in db.execute("SELECT case_id FROM prepared_cases ORDER BY case_id"):
            check_cancelled()
            yield case_id


def case_scope(search_root, active, case_id):
    """Locate one prepared exact overview identity without reading any body."""
    if not isinstance(case_id, str) or not case_id or len(case_id) > 256:
        raise ValueError("case_id must be an exact model_case citation ID")
    result = validated_navigation(search_root, active)
    with closing(_read_only(result["path"])) as db:
        if result["meta"]["schema"] == LEGACY_SCHEMA:
            row = db.execute("SELECT id,library_id,case_group FROM overviews WHERE id=?", (case_id,)).fetchone()
            prepared = case_id in result["prepared_case_ids"]
        else:
            row = db.execute("""SELECT o.id,o.library_id,o.case_group,p.case_id
              FROM overviews o LEFT JOIN prepared_cases p ON p.case_id=o.id WHERE o.id=?""", (case_id,)).fetchone()
            prepared = row is not None and row[3] is not None
    if row is None:
        raise FileNotFoundError("The exact model_case citation is not in the current navigation generation")
    if not prepared:
        raise NavigationNotPrepared("Case implementation navigation is not_prepared for this model_case")
    return dict(result, id=row[0], case_id=row[0], library_id=row[1], case_group=row[2])


def invalidate_navigation(search_root):
    """Remove only disposable case metadata, including owned atomic temp files."""
    root = Path(search_root).resolve()
    runtime = _owned(root, root / "runtime")
    result = {"removed_files": 0, "removed_bytes": 0}
    if not runtime.exists():
        return result
    fixed = {name + suffix for name in (FILENAME, "case-guides.sqlite3")
             for suffix in ("", "-wal", "-shm", "-journal")} | {"case-guides.json"}
    temporary = re.compile(r"(?:case-navigation\.sqlite3|case-guides\.(?:json|sqlite3))\.[a-f0-9]{32}\.tmp(?:-wal|-shm|-journal)?\Z")
    for path in runtime.iterdir():
        if path.name not in fixed and not temporary.fullmatch(path.name):
            continue
        _owned(root, path)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError("Case metadata cleanup requires unshared regular files")
        size = path.stat().st_size
        path.unlink()
        result["removed_files"] += 1
        result["removed_bytes"] += size
    return result


def _compact_node_metadata(root, authority):
    """Read only node kinds/property names from one pinned compact artifact."""
    reference = authority.get("data", {}).get("compact_case")
    if not isinstance(reference, dict):
        return {}
    relative = Path(reference.get("path", ""))
    path = (root / relative).resolve()
    if (relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(root / "artifacts")
            or path.name != "compactcase.sqlite3" or not path.is_file()):
        raise ValueError("Pinned compact case metadata is missing or outside owned artifacts")
    with closing(_read_only(path)) as db:
        nodes = {}
        for row in db.execute("SELECT n.id,k.value,t.value FROM nodes n LEFT JOIN symbols k ON k.id=n.kind_id LEFT JOIN symbols t ON t.id=n.type_id"):
            check_cancelled()
            nodes[row[0]] = {"kind": row[1], "type": row[2], "properties": set(), "api_names": set(), "selection": False}
        for node, name in db.execute("SELECT p.node_id,s.value FROM properties p LEFT JOIN symbols s ON s.id=p.name_id"):
            check_cancelled()
            if node in nodes:
                nodes[node]["properties"].add(name)
        for (node,) in db.execute("SELECT DISTINCT node_id FROM selections"):
            check_cancelled()
            if node in nodes:
                nodes[node]["selection"] = True
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='api_observations'").fetchone():
            for node, name in db.execute("SELECT n.node_id,s.value FROM api_observations o JOIN api_nodes n ON n.id=o.api_node_id LEFT JOIN symbols s ON s.id=o.name_id"):
                check_cancelled()
                if node in nodes:
                    nodes[node]["api_names"].add(name)
    overlay = authority.get("data", {}).get("compact_api_overlay")
    if isinstance(overlay, dict):
        relative = Path(overlay.get("path", ""))
        path = (root / relative).resolve()
        if (relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(root / "artifacts")
                or path.name != "compactapi.json.gz" or not path.is_file()):
            raise ValueError("Pinned API metadata is missing or outside owned artifacts")
        with gzip.open(path, "rb") as source:
            body = source.read(64 * 1024 * 1024 + 1)
        if len(body) > 64 * 1024 * 1024:
            raise ValueError("Pinned API metadata exceeds its bounded size")
        for observation in json.loads(body).get("observations", []):
            check_cancelled()
            node = observation.get("node_id")
            if node in nodes:
                nodes[node]["api_names"].add(observation.get("name"))
    return nodes


def _classification(local_id, branch, node_metadata):
    if local_id == "model-case":
        return "overview", "overview", 1
    stage = branch if branch in FEATURE_BRANCHES else "misc"
    match = re.fullmatch(r"(?:compact-node|deep-api-node)-(\d+)-\d+", local_id)
    node = node_metadata.get(int(match[1])) if match else None
    if node is not None:
        names = node["properties"]
        display_saved = bool(names and names <= _DISPLAY_PROPERTIES and not node["selection"]
                             and local_id.startswith("compact-node-"))
        display_api = bool(node["api_names"] and node["api_names"] <= _DISPLAY_PROPERTIES
                           and local_id.startswith("deep-api-node-"))
        if (node["kind"] in _LOW_VALUE_KINDS or node["type"] in _LOW_VALUE_KINDS
                or display_saved or display_api):
            return "support", stage, 0
    return "implementation", stage, 1


def _pilot_rows(catalog, objects, root, authority, policy):
    """Merge exact API/saved groups and canonical tutorial identities only."""
    data = authority.get("data") or {}
    base_id, base_group = data.get("base_case_id"), data.get("base_case_group")
    if base_id and (policy.blocks_identity(base_id) or base_group in policy.case_groups):
        raise _NavigationExcluded("A held-out saved base cannot enter case navigation")
    base = pinned_base_authority(catalog, authority)
    if (base_id or base_group) and base is None:
        raise ValueError("Model-case navigation requires its exact pinned saved base")
    groups = {authority["case_group"]}
    if base is not None:
        if policy.blocks_object(base):
            raise _NavigationExcluded("A held-out saved base cannot enter case navigation")
        groups.add(base["case_group"])
    node_metadata = _compact_node_metadata(root, authority)
    rows = {}
    for group in sorted(groups):
        for local_id, identity in catalog.execute("SELECT local_id,citation_id FROM case_members WHERE group_id=? ORDER BY local_id", (group,)):
            check_cancelled()
            if policy.blocks_identity(identity):
                continue
            indexed = objects.execute("SELECT library_id,feature_branch,qmd_path FROM objects WHERE id=?", (identity,)).fetchone()
            if indexed is None:
                continue  # Historical authorities need not be active QMD documents.
            if indexed[2] in policy.qmd_paths:
                continue
            if indexed[0] != authority["library_id"]:
                raise ValueError("Case member belongs to a different library")
            role, stage, visible = _classification(local_id, indexed[1], node_metadata)
            rows[identity] = (authority["id"], identity, role, stage, visible)
    identities = [authority["id"], *([base["id"]] if base else [])]
    placeholders = ",".join("?" for _ in identities)
    tables = {row[0] for row in catalog.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"tutorial_model_documents", "tutorial_associations", "tutorial_document_sources"} <= tables:
        query = f"""WITH docs AS (
          SELECT source_sha256 FROM tutorial_model_documents WHERE source_id IN ({placeholders}) AND owner_library_id=?
          UNION SELECT source_sha256 FROM tutorial_associations WHERE source_id IN ({placeholders}) AND source_library_id=?
        ) SELECT DISTINCT d.citation_id,d.category FROM docs t
          JOIN tutorial_document_sources d ON d.source_sha256=t.source_sha256
          JOIN revisions r ON r.id=d.citation_id AND r.library_id=d.owner_library_id
          JOIN active a ON a.library_id=r.library_id AND a.object_id=r.object_id AND a.citation_id=r.id"""
        for identity, category in catalog.execute(query, [*identities, authority["library_id"], *identities, authority["library_id"]]):
            check_cancelled()
            indexed = objects.execute("SELECT qmd_path FROM objects WHERE id=?", (identity,)).fetchone()
            if not policy.blocks_identity(identity) and indexed and indexed[0] not in policy.qmd_paths:
                stage = category if category in FEATURE_BRANCHES else "reference"
                rows[identity] = (authority["id"], identity, "reference", stage, 1)
    return list(rows.values())


def build_navigation(store, output=None, *, case_ids=(), progress=None):
    """Compile overview identities and case scopes without source text.

    This internal metadata compiler does not enable libraries. Public consumers
    must apply the same live visibility/holdout gates as ordinary QMD searches.
    ``case_ids=None`` prepares every non-heldout configured/unknown overview;
    an explicit iterable prepares exactly those cases, with no pilot limit.
    The empty default is a lightweight, payload-free overview refresh.
    Membership and compact metadata are retained for at most one case at once.
    """
    root = Path(store.root).resolve()
    search = root / "search"
    output = Path(output).resolve() if output is not None else navigation_path(search)
    if output != navigation_path(search):
        raise ValueError("Case navigation must use its fixed owned runtime path")
    _owned(search, output)
    if output.exists() and output.stat().st_nlink != 1:
        raise ValueError("Case navigation refuses shared hard links")
    if isinstance(case_ids, (str, bytes)):
        raise ValueError("case_ids must be an iterable of exact model_case IDs or None")
    requested = None if case_ids is None else list(case_ids)
    if requested is not None:
        if any(not isinstance(value, str) or not value or len(value) > 256 for value in requested):
            raise ValueError("Case navigation requires exact model_case IDs")
        requested = list(dict.fromkeys(requested))
    if progress is not None and not callable(progress):
        raise TypeError("progress must be callable or None")
    build_scope = "all" if requested is None else "selected" if requested else "overviews"
    active = read_manifest(search / "active.json", summary=True, resolve_paths=True)
    if not active:
        raise NavigationStale("A current QMD generation is required before preparing navigation")
    identity = _identity(search, active)
    objects_path = _objects_path(search, active)
    live_policy = store.evaluation_policy()
    policy = live_policy
    registry = None
    if requested is None:
        # Opening evaluation retrieval in the web UI never opts held-out
        # assets into an offline full-library derivation.
        from .evaluation_sets import _load
        registry, closed_policy, _ = _load(root)
        if registry is not None:
            policy = closed_policy
    if requested is not None and any(policy.blocks_identity(value) for value in requested):
        raise PermissionError("Held-out case IDs cannot be prepared as navigation")
    catalog_signature = database_signature(store.db_path, change_counter=True)
    stage = output.with_name(output.name + "." + uuid.uuid4().hex + ".tmp")
    _owned(search, stage)
    output.parent.mkdir(parents=True, exist_ok=True)
    overview_count = prepared_count = member_count = excluded_count = processed = 0
    skipped_heldout = skipped_geometry = 0
    context_signature = context_file = None

    def report_progress(case_id=None):
        if progress is not None:
            progress({"stage": "navigation", "build_scope": build_scope, "current_case": case_id,
                      "processed_cases": processed, "overview_count": overview_count,
                      "prepared_case_count": prepared_count, "member_count": member_count,
                      "skipped_heldout": skipped_heldout, "skipped_geometry_only": skipped_geometry})

    try:
        check_cancelled()
        with ExitStack() as stack:
            catalog = stack.enter_context(closing(_read_only(store.db_path)))
            objects = stack.enter_context(closing(_read_only(objects_path)))
            catalog.execute("BEGIN")
            objects.execute("BEGIN")
            watermark = _watermark(catalog)
            if registry is not None and registry.get("catalog_revision_watermark") != watermark:
                from .evaluation_sets import EvaluationPolicyStale
                raise EvaluationPolicyStale("The evaluation holdout registry does not match the catalog watermark")
            if database_signature(store.db_path, change_counter=True) != catalog_signature:
                raise NavigationStale("Catalog changed while opening the navigation snapshot")
            capabilities = None
            if requested is None:
                from .case_context_store import validated_contexts
                contexts = validated_contexts(search, active)
                if contexts is not None:
                    context_file = Path(contexts["path"])
                    context_signature = database_signature(context_file, change_counter=True)
                    capabilities = stack.enter_context(closing(_read_only(context_file)))
                    capabilities.execute("BEGIN")
            with closing(sqlite3.connect(stage)) as target, target:
                target.executescript("""CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                  CREATE TABLE overviews(id TEXT PRIMARY KEY,library_id TEXT NOT NULL,case_group TEXT NOT NULL);
                  CREATE INDEX overviews_library ON overviews(library_id,id);
                  CREATE TABLE prepared_cases(case_id TEXT PRIMARY KEY);
                  CREATE TABLE members(case_id TEXT NOT NULL,object_id TEXT NOT NULL,role TEXT NOT NULL,
                    stage TEXT NOT NULL,global_visible INTEGER NOT NULL CHECK(global_visible IN (0,1)),PRIMARY KEY(case_id,object_id));
                  CREATE INDEX members_object ON members(object_id,global_visible,role);
                  CREATE INDEX members_scope ON members(case_id,global_visible,role,stage);""")
                # Scalar revision columns do not invoke the payload codec. This
                # single overview scan never decodes the full member catalog.
                for row in catalog.execute(_OVERVIEWS_SQL):
                    check_cancelled()
                    if objects.execute("SELECT 1 FROM objects WHERE id=?", (row[0],)).fetchone():
                        target.execute("INSERT INTO overviews VALUES(?,?,?)", tuple(row))
                        overview_count += 1
                report_progress()
                candidates = (target.execute("SELECT id,library_id,case_group FROM overviews ORDER BY library_id,id")
                              if requested is None else ((case_id,) for case_id in requested))
                for candidate in candidates:
                    check_cancelled()
                    authority = raw = None
                    case_id = candidate[0]
                    located = candidate if requested is None else target.execute(
                        "SELECT id,library_id,case_group FROM overviews WHERE id=?", (case_id,)).fetchone()
                    if located is None:
                        raise FileNotFoundError("Exact model_case is not an active indexed overview")
                    processed += 1
                    try:
                        if policy.blocks_identity(case_id) or located[2] in policy.case_groups:
                            raise _NavigationExcluded("Held-out cases cannot be prepared as navigation")
                        indexed = objects.execute("SELECT qmd_path FROM objects WHERE id=?", (case_id,)).fetchone()
                        if indexed[0] in policy.qmd_paths:
                            raise _NavigationExcluded("Held-out cases cannot be prepared as navigation")
                        raw = catalog.execute("SELECT payload FROM revisions WHERE id=?", (case_id,)).fetchone()
                        authority = json.loads(raw[0])
                        if (authority.get("id") != case_id or authority.get("type") != "model_case"
                                or authority.get("case_group") != located[2] or authority.get("library_id") != located[1]):
                            raise ValueError("Case overview differs from its immutable catalog identity")
                        if policy.blocks_object(authority):
                            raise _NavigationExcluded("Held-out cases cannot be prepared as navigation")
                        if requested is None:
                            from .case_context import classify_case_metadata
                            capability = (capabilities.execute("SELECT classification FROM capabilities WHERE case_id=?",
                                                               (case_id,)).fetchone() if capabilities else None)
                            classification = capability[0] if capability else classify_case_metadata(authority)["classification"]
                            if classification == "geometry_only":
                                skipped_geometry += 1
                                report_progress(case_id)
                                continue
                        # _pilot_rows uses group and exact revision probes. Its
                        # list and compact metadata die before the next case.
                        members = _pilot_rows(catalog, objects, root, authority, policy)
                    except _NavigationExcluded:
                        if requested is not None:
                            raise
                        skipped_heldout += 1
                        report_progress(case_id)
                        continue
                    target.executemany("INSERT INTO members VALUES(?,?,?,?,?)", members)
                    target.execute("INSERT INTO prepared_cases VALUES(?)", (case_id,))
                    prepared_count += 1
                    member_count += len(members)
                    excluded_count += sum(row[4] == 0 for row in members)
                    del members, authority, raw
                    report_progress(case_id)
                metadata = {"schema": SCHEMA, **identity, "catalog_revision_watermark": watermark,
                            "compiled_policy_token": policy.token, "build_scope": build_scope,
                            "prepared_case_count": prepared_count, "overview_count": overview_count,
                            "member_count": member_count, "source_text_stored": False,
                            "skipped_heldout": skipped_heldout, "skipped_geometry_only": skipped_geometry}
                target.executemany("INSERT INTO meta VALUES(?,?)", ((key, _json(value)) for key, value in metadata.items()))
            check_cancelled()
            with closing(_read_only(stage)) as target:
                target.set_progress_handler(lambda: (check_cancelled(), 0)[1], 10000)
                try:
                    integrity = target.execute("PRAGMA integrity_check").fetchone()
                except sqlite3.OperationalError:
                    # SQLite converts callback exceptions to "interrupted";
                    # preserve the cooperative cancellation exception for owners.
                    check_cancelled()
                    raise
                if integrity != ("ok",):
                    raise ValueError("Case navigation metadata failed SQLite integrity check")
        if database_signature(store.db_path, change_counter=True) != catalog_signature:
            raise NavigationStale("Catalog changed while preparing navigation")
        if context_file is not None and database_signature(context_file, change_counter=True) != context_signature:
            raise NavigationStale("Case classification changed while preparing navigation")
        if requested is None:
            current_registry, _, _ = _load(root)
            if current_registry != registry:
                raise NavigationStale("Evaluation holdouts changed while preparing navigation")
        current = read_manifest(search / "active.json", summary=True, resolve_paths=True)
        if current is None or _identity(search, current) != identity or store.evaluation_policy().token != live_policy.token:
            raise NavigationStale("QMD generation or exclusion policy changed while preparing navigation")
        check_cancelled()
        os.replace(stage, output)
        return {"path": str(output), "schema": SCHEMA, "generation": identity["generation"],
                "overview_count": overview_count, "build_scope": build_scope,
                "prepared_case_ids": requested, "prepared_case_count": prepared_count,
                "member_count": member_count, "pilot_member_count": member_count,
                "excluded_default_members": excluded_count, "skipped_heldout": skipped_heldout,
                "skipped_geometry_only": skipped_geometry,
                "source_text_stored": False, "qmd_rebuilt": False, "embedding_invoked": False}
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            stage.with_name(stage.name + suffix).unlink(missing_ok=True)
