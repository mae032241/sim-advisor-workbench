"""Bounded case hints in a resumable, generation-bound SQLite sidecar.

Each case is derived and committed independently. This cache never grants read
permission, and navigation hints never count as source reads.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from .build_control import BuildCancelled, check_cancelled
from .case_metadata import FEATURE_BRANCHES
from .compact_library import _reader
from .index_manifest import _owned
from .index_manifest_cache import read_manifest

SCHEMA = "case-guides/v2"
LEGACY_SCHEMA = "case-guides/v1"
FILENAME = "case-guides.sqlite3"
_MAX_LEGACY_BYTES = 8 * 1024 * 1024
_MAX_GUIDE_BYTES = 1024 * 1024
_REPORT_LIMIT = 100
_BINDING = ("generation", "corpus_hash", "objects_signature")
_LABELS = {"param": "参数", "geometry": "几何", "selection": "选择集", "material": "材料",
           "physics": "物理接口", "multiphysics": "耦合与算子", "mesh": "网格", "study": "研究",
           "solver": "求解器", "result": "结果", "func": "函数", "variable": "变量", "misc": "目标与适用条件"}
_PRIORITY = {"size", "freetet", "freetri", "swept", "map", "boundarylayer", "distribution",
             "stationary", "time", "frequency", "direct", "fullycoupled", "segregated", "iterative", "multigrid"}
_FRAMEWORK = {"mesh": "按目标量检查网格质量与独立性；本导航不提供已验证的质量阈值。",
              "solver": "核对残差、守恒和时间步或参数敏感性；保存的求解器树不证明本次计算收敛。"}
_PROPERTY_ORDER = {
    "mesh": ("hmax", "hmin", "hauto", "hgrad", "hcurve", "hnarrow", "custom", "scale", "blnlayers", "blfirstlayer", "blstretch"),
    "solver": ("rtol", "atolglobal", "atol", "tstepsbdf", "maxorder", "initialstepbdf", "maxstepbdf", "tlist", "linsolver", "prefun", "pretype", "dtech", "maxiter", "maxlinit"),
    "study": ("tlist", "plist", "pname", "rtol", "mmamaxiter", "gradientStep"),
}


def _path(store):
    """Historical JSON location, read only when the SQLite store is absent."""
    return Path(store.root) / "search" / "runtime" / "case-guides.json"


def database_path(store):
    return Path(store.root).absolute() / "search" / "runtime" / FILENAME


def _owned_path(store, path):
    root = Path(store.root).absolute()
    _owned(Path(root.anchor), root)
    _owned(root, root / "search")
    path = _owned(root / "search", path)
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        _owned(root / "search", candidate)
        if os.path.lexists(candidate):
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Case guides require owned, unshared regular files")
    return path


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _read_only(path):
    db = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=30)
    db.execute("PRAGMA query_only=ON")
    return db


def _navigation(store):
    from .case_navigation import validated_navigation
    from .evaluation_sets import _watermark
    root = Path(store.root) / "search"
    active = read_manifest(root / "active.json", summary=True, resolve_paths=True)
    if not active:
        raise RuntimeError("Case navigation is unavailable; rebuild navigation first")
    navigation = validated_navigation(root, active)
    with closing(store.connect()) as db:
        if navigation["meta"].get("catalog_revision_watermark") != _watermark(db):
            raise RuntimeError("Catalog revisions changed; rebuild retrieval and case navigation")
    return active, navigation


def _scope(store, active, case_id):
    from .case_navigation import case_scope
    return case_scope(Path(store.root) / "search", active, case_id)


def _authority(store, case_id, *, reading):
    if (not isinstance(case_id, str) or not case_id or len(case_id) > 256
            or any(character in case_id for character in ("/", "\\", ":"))
            or case_id.lower().endswith(".mph")):
        raise ValueError("case_id must be an exact model_case citation ID")
    store.require_not_heldout(case_id)
    if reading and not store.source_available(case_id):
        raise PermissionError("案例所属资料库已关闭、删除或不可用，不能读取案例导航。")
    value = store.get(case_id)
    if value is None:
        raise FileNotFoundError("Case overview not found")
    store.require_not_heldout(case_id, value)
    if value.get("type") != "model_case":
        raise ValueError("Case guides require an exact model_case citation ID")
    if reading:
        store.require_readable_library(value["library_id"])
    return value


def _hint(value, limit=160):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return " ".join(text.split())[:limit]


def _reference(source, stage, role):
    return {"id": source["id"], "library_id": source["library_id"], "title": _hint(source.get("title", "")),
            "type": source.get("type"), "stage": stage, "role": role,
            "is_read_evidence": False}


def _properties(node, stage):
    priority = {name.lower(): index for index, name in enumerate(_PROPERTY_ORDER.get(stage, ()))}
    return sorted(node.get("properties", []), key=lambda prop: (
        prop.get("value") is None, priority.get(str(prop.get("name", "")).lower(), 100), prop.get("ordinal", 0)))[:3 if stage in {"mesh", "solver"} else 2]


def _select_nodes(nodes, stage, cap):
    solver_types = {name: index for index, name in enumerate(("time", "stationary", "fullycoupled", "segregated", "direct", "iterative", "multigrid", "parametric"))}
    def score(node):
        kind, feature = str(node.get("kind", "")).lower(), str(node.get("type", "")).lower()
        useful = sum(prop.get("value") is not None for prop in _properties(node, stage))
        rank = solver_types.get(feature, 20) if stage == "solver" else (
            0 if stage == "physics" and kind == "physics" else 1 if feature in _PRIORITY else 2)
        return rank, -useful, node["id"]
    ordered = sorted(nodes, key=score)
    selected, types = [], set()
    # Include distinct routes/interfaces before repeated solver defaults.
    for node in ordered:
        feature = node.get("type") or node.get("kind")
        if feature not in types:
            selected.append(node)
            types.add(feature)
        if len(selected) == cap:
            return selected
    return (selected + [node for node in ordered if node not in selected])[:cap]


def _excerpt(source):
    lines, size = [], 0
    for match in re.finditer(r"[^\r\n]+", source.get("text", "")):
        line = match[0].strip()
        if not line or any(word in line.lower() for word in ("copyright", "licensed under", "license agreement", "all rights reserved")):
            continue
        lines.append(line[:160])
        size += len(lines[-1])
        if size >= 160:
            break
    return _hint(" ".join(lines))


def _select_references(entries, cap):
    ordered = sorted(entries, key=lambda row: row[0])
    selected, sections = [], set()
    for row in ordered:
        if row[1] not in sections:
            selected.append(row)
            sections.add(row[1])
        if len(selected) == cap:
            return selected
    return (selected + [row for row in ordered if row not in selected])[:cap]


def _members(store, navigation, case_id):
    """Stream scalar implementation routes; decode only chosen node sources.

The catalog's immutable local IDs identify compact nodes without decompressing
their retrieval text. Tutorial excerpts are streamed and bounded by _guide.
"""
    with closing(_read_only(navigation["path"])) as db, closing(store.connect()) as catalog:
        rows = db.execute("SELECT object_id,role,stage FROM members WHERE case_id=? "
                          "AND global_visible=1 AND role IN ('implementation','reference') ORDER BY object_id", (case_id,))
        while batch := rows.fetchmany(400):
            check_cancelled()
            identities = [row[0] for row in batch]
            owners = {}
            query = ("SELECT r.id,r.library_id,r.object_id,m.local_id FROM revisions r "
                     "LEFT JOIN case_members m ON m.citation_id=r.id WHERE r.id IN (" +
                     ",".join("?" for _ in identities) + ")")
            for identity, owner, logical, local_id in catalog.execute(query, identities):
                previous = owners.get(identity)
                if previous is None or re.fullmatch(r"(?:compact-node|deep-api-node)-\d+-\d+", local_id or ""):
                    owners[identity] = (owner, logical, local_id)
            policy = store.evaluation_policy()
            for identity, role, stage in batch:
                check_cancelled()
                store.require_not_heldout(identity)
                metadata = owners.get(identity)
                if metadata is None:
                    raise RuntimeError("A prepared navigation source is missing; rebuild navigation")
                owner, logical, local_id = metadata
                if policy.blocks_identity(identity, logical):
                    raise PermissionError("A prepared navigation source is held out")
                match = re.fullmatch(r"(compact-node|deep-api-node)-(\d+)-\d+", local_id or "")
                if role == "implementation":
                    if match is None:
                        continue
                    source = {"id": identity, "library_id": owner,
                              "type": "saved_configuration" if match[1] == "compact-node" else "api_configuration",
                              "data": {"compact_ref": {"node_id": int(match[2])}}, "_guide_lazy": True}
                else:
                    source = store.get(identity)
                    if source is None:
                        raise RuntimeError("A prepared navigation source is missing; rebuild navigation")
                    store.require_not_heldout(identity, source)
                yield source, stage if stage in FEATURE_BRANCHES else "misc", role


def _available_references(store, references):
    """Batched scalar owner reads with start/end visibility checks, no bodies."""
    visible, policy = set(store.searchable_library_ids()), store.evaluation_policy()
    identities = list(dict.fromkeys(ref["id"] for ref in references))
    owners, retired, aliases = [], {}, {}
    with closing(store.connect()) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for start in range(0, len(identities), 400):
            batch = identities[start:start + 400]
            if not batch:
                continue
            rows = db.execute("SELECT id,library_id,object_id FROM revisions WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch)
            owners.extend(rows)
            placeholders = ",".join("?" for _ in batch)
            if {"tutorial_retired_sources", "tutorial_associations"} <= tables:
                for identity, target in db.execute(
                        "SELECT r.citation_id,a.target_library_id FROM tutorial_retired_sources r "
                        "LEFT JOIN tutorial_associations a ON a.source_id=r.citation_id "
                        "WHERE r.reason='model_tutorial_copy' AND r.citation_id IN (" + placeholders + ")", batch):
                    retired.setdefault(identity, set())
                    if target is not None:
                        retired[identity].add(target)
            if "tutorial_aliases" in tables:
                for identity, owner, target, target_owner, logical, found in db.execute(
                        "SELECT a.citation_id,a.owner_library_id,a.target_id,a.target_library_id,r.object_id,r.id "
                        "FROM tutorial_aliases a LEFT JOIN revisions r ON r.id=a.target_id "
                        "AND r.library_id=a.target_library_id WHERE a.citation_id IN (" + placeholders + ")", batch):
                    aliases[identity] = (owner, target, target_owner, logical, found is not None)
    visible.intersection_update(store.searchable_library_ids())
    current_policy = store.evaluation_policy()
    allowed = {identity for identity, owner, logical in owners if owner in visible
               and not policy.blocks_identity(identity, logical) and not current_policy.blocks_identity(identity, logical)
               and (identity not in retired or retired[identity] & visible)}
    for identity, (owner, target, target_owner, logical, found) in aliases.items():
        if (found and {owner, target_owner} <= visible and not policy.blocks_identity(identity)
                and not current_policy.blocks_identity(identity)
                and not policy.blocks_identity(target, logical) and not current_policy.blocks_identity(target, logical)):
            allowed.add(identity)
        else:
            allowed.discard(identity)
    return allowed


def _guide(store, authority, navigation):
    by_node, references = defaultdict(list), defaultdict(list)
    for source, stage, role in _members(store, navigation, authority["id"]):
        check_cancelled()
        data = source.get("data", {})
        compact = data.get("compact_ref", {})
        node_id = compact.get("node_id")
        if role == "implementation" and node_id is not None:
            by_node[node_id].append(source)
        elif role == "reference":
            ref = _reference(source, stage, role)
            section = str(data.get("section") or source.get("title", ""))
            rank = 0 if section.lower() == "model definition" else 1 if section.lower() == "introduction" else 2
            order = data.get("tutorial_order")
            order = order if isinstance(order, (int, float)) else 100000
            references[stage].append(((rank, order, source["id"]), section, ref, _excerpt(source)))
            references[stage] = _select_references(references[stage], 3 if stage in {"mesh", "solver"} else 2)
    reader = _reader(authority, store.root, None)
    nodes, node_counts = defaultdict(list), defaultdict(int)
    for node in reader.iter_nodes():
        check_cancelled()
        stage = node.get("feature_branch") or "misc"
        if (stage in FEATURE_BRANCHES and by_node.get(node["id"])
                and (node.get("type") or node.get("properties") or node.get("selections")
                     or node.get("kind") in {"Physics", "Mesh", "Study", "SolverSequence"})):
            node_counts[stage] += 1
            nodes[stage] = _select_nodes([*nodes[stage], node], stage, 8 if stage in {"mesh", "solver"} else 4)
    stages, source_refs = [], {}
    order = ("param", "geometry", "selection", "material", "physics", "multiphysics", "mesh", "study", "solver", "result", "func", "variable", "misc")
    for stage in order:
        cap = 8 if stage in {"mesh", "solver"} else 4
        selected = _select_nodes(nodes[stage], stage, cap)
        facts, refs, configuration_requests = [], [], []
        for node in selected:
            properties = _properties(node, stage)
            candidates = []
            ordered_sources = sorted(by_node[node["id"]], key=lambda source: source.get("type") == "api_configuration")
            for source in ordered_sources:
                check_cancelled()
                is_api = source.get("type") == "api_configuration"
                saved_candidates = [(ref, ordinals) for ref, ordinals in candidates if ref.get("type") != "api_configuration"]
                covered = saved_candidates and all(any(ordinals is None or prop.get("ordinal") in ordinals
                                                      for _, ordinals in saved_candidates) for prop in properties)
                if ((is_api and any(ref.get("type") == "api_configuration" for ref, _ in candidates))
                        or (not is_api and covered)):
                    continue
                if source.get("_guide_lazy"):
                    original = source
                    source = store.get(original["id"])
                    if (source is None or source.get("id") != original["id"]
                            or source.get("library_id") != original["library_id"]
                            or source.get("type") != original["type"]
                            or source.get("data", {}).get("compact_ref", {}).get("node_id") != node["id"]):
                        raise RuntimeError("A prepared compact source differs from its immutable navigation route")
                    store.require_not_heldout(source["id"], source)
                compact = source.get("data", {}).get("compact_ref", {})
                candidates.append((_reference(source, stage, "implementation"), compact.get("property_ordinals")))
            saved = [(ref, ordinals) for ref, ordinals in candidates if ref.get("type") != "api_configuration"]
            linked, property_hints = [], []
            for prop in properties or [None]:
                match = next((ref for ref, ordinals in saved if ordinals is None or prop is None or prop.get("ordinal") in ordinals), None)
                if match is not None and match not in linked:
                    linked.append(match)
                if prop is not None and match is not None:
                    property_hints.append({"name": prop.get("name"), "hint": _hint(prop.get("value"), 96),
                                           "status": prop.get("status"), "complete": prop.get("value_complete") is True,
                                           "hint_truncated": len(_hint(prop.get("value"), 97)) > 96,
                                           "source_ids": [match["id"]]})
            if not linked and saved:
                linked.append(saved[0][0])
            api_ref = next((ref for ref, _ in candidates if ref.get("type") == "api_configuration"), None)
            if api_ref is not None:
                linked.append(api_ref)
            refs.extend(linked)
            saved_ids = [ref["id"] for ref in linked if ref.get("type") != "api_configuration"]
            api_ids = [ref["id"] for ref in linked if ref.get("type") == "api_configuration"]
            if saved_ids:
                facts.append({"stage": stage, "hint": _hint(" · ".join(str(node.get(key) or "") for key in ("type", "label", "tag"))),
                              "source_ids": saved_ids, "basis": "saved_configuration", "node_id": node["id"],
                              "property_hints": property_hints, "effective_activation": "not_evaluated"})
            if api_ids and node.get("api_observations"):
                facts.append({"stage": stage, "hint": "已保存 getter 观测；不是当前已生效配置。", "source_ids": api_ids,
                              "basis": "api_getter_observation", "node_id": node["id"], "effective_activation": "not_evaluated"})
            for mode, identities in (("saved", saved_ids), ("api", api_ids)):
                if identities:
                    configuration_requests.append({"case_id": authority["id"], "category": stage,
                                                   "node_path": f"compact:node/{node['id']}", "source": mode,
                                                   "limit": 1, "source_ids": identities})
        for _, _, ref, text in _select_references(references[stage], 3 if stage in {"mesh", "solver"} else 2):
            refs.append(ref)
            facts.append({"stage": stage, "hint": text, "source_ids": [ref["id"]], "basis": "linked_tutorial_excerpt",
                          "model_settings_agreement": "not_verified", "effective_activation": "not_evaluated"})
        for ref in refs:
            source_refs[ref["id"]] = ref
        gaps = [] if selected else [{"stage": stage, "code": "saved_configuration_not_found",
                                     "detail": "当前导航未找到该环节的保存配置；不据此推断默认设置。"}]
        guidance = [{"basis": "framework_guidance", "text": _FRAMEWORK[stage], "source_ids": []}] if stage in _FRAMEWORK else []
        stages.append({"id": stage, "title": _LABELS[stage], "source_refs": refs, "observed": facts,
                       "gaps": gaps, "configuration_requests": configuration_requests, "guidance": guidance,
                       "total_node_count": node_counts[stage], "selected_node_count": len(selected),
                       "summary_truncated": node_counts[stage] > len(selected), "detail_scope": "bounded_stage_summary"})
    coverage = authority.get("coverage", {})
    raw, api, tutorial = (coverage.get(key, {}) for key in ("raw_xml", "deep_api", "tutorial"))
    gaps = [{"code": "physical_validity_not_evaluated", "detail": "未执行几何、网格或求解；导航不证明物理有效性或收敛。"},
            {"code": "tutorial_settings_agreement_not_verified", "detail": "关联教程与当前保存配置的一致性未核实。"},
            {"code": "assumptions_require_source_read", "detail": "目标、假设与适用条件须阅读关联概览和教程核对，不能仅凭标题或配置推断。"}]
    if api.get("status") != "observed" or api.get("read_errors"):
        gaps.append({"code": "api_coverage_incomplete", "status": api.get("status", "not_requested"),
                     "read_errors": api.get("read_errors"), "detail": "getter 观测不代表有效成员、默认值或实际激活状态。"})
    if (raw.get("truncated") or raw.get("normalization", {}).get("properties_unresolved")
            or any(raw.get("property_statuses", {}).get(name) for name in ("partial", "unresolved"))
            or raw.get("selection_statuses", {}).get("unresolved")):
        gaps.append({"code": "saved_state_coverage_incomplete", "truncated": bool(raw.get("truncated")),
                     "property_statuses": raw.get("property_statuses", {}), "selection_statuses": raw.get("selection_statuses", {})})
    overview = _reference(authority, "misc", "overview")
    source_refs[authority["id"]] = overview
    return {"state": "ready", "case_id": authority["id"], "title": authority.get("title"), "library_id": authority["library_id"],
            "asset_key": authority.get("asset_key"), "overview": overview, "stages": stages,
            "source_refs": list(source_refs.values()), "gaps": gaps, "is_read_evidence": False,
            "coverage": {"saved_status": raw.get("status"), "api_status": api.get("status"),
                         "tutorial_status": tutorial.get("status"), "physical_validation": "not_run", "effective_activation": "not_evaluated"}}


def _binding(navigation):
    return {key: navigation["meta"].get(key) for key in _BINDING}


def _database_meta(db):
    meta = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM meta")}
    if meta.get("schema") != SCHEMA or not isinstance(meta.get("binding"), dict):
        raise ValueError("Case guide storage has an unsupported schema; rebuild its metadata")
    if not {"case_id", "guide_json"} <= {row[1] for row in db.execute("PRAGMA table_info(guides)")}:
        raise ValueError("Case guide storage is incomplete; rebuild its metadata")
    return meta


def _open_database(store, binding):
    path = _owned_path(store, database_path(store))
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    _owned_path(store, path)
    db = sqlite3.connect(path, timeout=30)
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not existed:
                db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                db.execute("CREATE TABLE guides(case_id TEXT PRIMARY KEY,guide_json TEXT NOT NULL)")
                db.execute("CREATE TABLE failures(case_id TEXT PRIMARY KEY,code TEXT NOT NULL,detail TEXT NOT NULL)")
                db.executemany("INSERT INTO meta VALUES(?,?)", (("schema", _json(SCHEMA)), ("binding", _json(binding))))
            meta = _database_meta(db)
            if meta["binding"] != binding:
                db.execute("DELETE FROM guides")
                db.execute("DELETE FROM failures")
                db.execute("UPDATE meta SET value=? WHERE key='binding'", (_json(binding),))
        return db
    except BaseException:
        db.close()
        raise


def build_case_guides(store, case_ids=None, *, progress=None, continue_on_error=True, resume=True):
    """Derive selected cases, or stream all prepared navigation cases.

    Ordinary closed archives may be prepared explicitly. Successful cases are
    committed independently and resumed only under the exact same binding.
    Failures are persisted for inspection and retried on the next invocation.
    The callback receives one small dictionary per completed/failed case.
    """
    if case_ids is not None and (not isinstance(case_ids, (list, tuple)) or not case_ids
            or any(not isinstance(value, str) or not value or len(value) > 256 for value in case_ids)):
        raise ValueError("Select exact model_case citation IDs, or use None for all prepared cases")
    active, navigation = _navigation(store)
    binding = _binding(navigation)
    requested = list(dict.fromkeys(case_ids)) if case_ids is not None else None
    if requested is None:
        from .case_navigation import iter_prepared_case_ids
        identities = iter_prepared_case_ids(navigation)
        total = navigation["meta"].get("prepared_case_count", len(navigation.get("prepared_case_ids", ())))
    else:
        identities, total = iter(requested), len(requested)
    result = {"state": "ready", "count": 0, "built_count": 0, "skipped_count": 0, "failed_count": 0,
              "path": str(database_path(store)), "binding": binding, "build_scope": "all" if requested is None else "selected",
              "requested_count": total, "failures": [], "failures_truncated": False,
              "qmd_rebuilt": False, "embedding_invoked": False}
    if requested is not None and len(requested) <= _REPORT_LIMIT:
        result["case_ids"] = requested
    check_cancelled()
    # Explicit requests must fail before creating metadata when an authority
    # is held out or invalid. Full runs isolate ordinary per-case failures.
    if requested is not None:
        for identity in requested:
            check_cancelled()
            _authority(store, identity, reading=False)
    with closing(_open_database(store, binding)) as db:
        for identity in identities:
            check_cancelled()
            try:
                authority = _authority(store, identity, reading=False)
                _scope(store, active, identity)
                if resume and db.execute("SELECT 1 FROM guides WHERE case_id=?", (identity,)).fetchone():
                    result["skipped_count"] += 1
                    state = "skipped"
                else:
                    guide = _guide(store, authority, navigation)
                    encoded = _json(guide)
                    if len(encoded.encode("utf-8")) > _MAX_GUIDE_BYTES:
                        raise ValueError("Derived guide exceeds its per-case size bound")
                    # Binding checks and cancellation happen before the atomic
                    # case commit, so an interrupted case is simply retried.
                    _, current = _navigation(store)
                    if _binding(current) != binding:
                        raise RuntimeError("Case navigation changed while guides were built; rebuild guides")
                    _owned_path(store, database_path(store))
                    check_cancelled()
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        if _database_meta(db)["binding"] != binding:
                            raise RuntimeError("Another guide build changed the storage binding")
                        db.execute("INSERT OR REPLACE INTO guides VALUES(?,?)", (identity, encoded))
                        db.execute("DELETE FROM failures WHERE case_id=?", (identity,))
                    result["built_count"] += 1
                    state = "built"
                result["count"] += 1
            except BuildCancelled:
                raise
            except (PermissionError, FileNotFoundError, RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
                # A changing generation invalidates the whole run; ordinary
                # corrupt/missing cases remain actionable per-case failures.
                _, current = _navigation(store)
                if _binding(current) != binding:
                    raise RuntimeError("Case navigation changed while guides were built; rebuild guides") from exc
                failure = {"case_id": identity, "code": type(exc).__name__, "detail": _hint(str(exc), 400)}
                _owned_path(store, database_path(store))
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    if _database_meta(db)["binding"] != binding:
                        raise RuntimeError("Another guide build changed the storage binding") from exc
                    db.execute("INSERT OR REPLACE INTO failures VALUES(?,?,?)", (identity, failure["code"], failure["detail"]))
                    db.execute("DELETE FROM guides WHERE case_id=?", (identity,))
                result["failed_count"] += 1
                if len(result["failures"]) < _REPORT_LIMIT:
                    result["failures"].append(failure)
                else:
                    result["failures_truncated"] = True
                state = "failed"
                if not continue_on_error:
                    raise
            if progress is not None:
                progress({"case_id": identity, "state": state, "completed": result["count"] + result["failed_count"],
                          "total": total, "built_count": result["built_count"], "skipped_count": result["skipped_count"],
                          "failed_count": result["failed_count"]})
        _, current = _navigation(store)
        if _binding(current) != binding:
            raise RuntimeError("Case navigation changed while guides were built; rebuild guides")
        result["stored_count"] = db.execute("SELECT count(*) FROM guides").fetchone()[0]
    if result["failed_count"]:
        result["state"] = "partial"
    return result


def _cached_guide(store, case_id):
    """Look up only one SQLite row; legacy JSON has a strict historical bound."""
    path = _owned_path(store, database_path(store))
    if path.exists():
        with closing(_read_only(path)) as db:
            db.execute("BEGIN")
            meta = _database_meta(db)
            row = db.execute("SELECT guide_json FROM guides WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            return None, meta["binding"]
        if len(row[0].encode("utf-8")) > _MAX_GUIDE_BYTES:
            raise ValueError("Cached guide exceeds its per-case size bound")
        return json.loads(row[0]), meta["binding"]
    path = _owned_path(store, _path(store))
    if not path.exists():
        return None, None
    if path.stat().st_size > _MAX_LEGACY_BYTES:
        raise ValueError("Historical guide storage exceeds its bounded size; rebuild guides")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or payload.get("schema") != LEGACY_SCHEMA
            or not isinstance(payload.get("cases"), dict) or not isinstance(payload.get("binding"), dict)):
        raise ValueError("Historical case guide cache is invalid; rebuild guides")
    return copy.deepcopy(payload["cases"].get(case_id)), payload["binding"]


def _filter_guide(store, result):
    references = result.get("source_refs", [])
    available = _available_references(store, references)
    if any(ref["id"] not in available for ref in references):
        result.setdefault("gaps", []).append({"code": "linked_sources_unavailable", "detail": "部分关联来源当前不可读取，其导航提示已隐藏。"})
    for stage in result["stages"]:
        original_refs = stage.get("source_refs", [])
        stage["source_refs"] = [ref for ref in original_refs if ref["id"] in available]
        facts = []
        for fact in stage.get("observed", []):
            dependencies = set(fact.get("source_ids", ()))
            if not dependencies or not dependencies <= available:
                continue
            fact["property_hints"] = [prop for prop in fact.get("property_hints", [])
                                      if set(prop.get("source_ids", fact["source_ids"])) <= available]
            if not fact["property_hints"]:
                fact.pop("property_hints", None)
            facts.append(fact)
        stage["observed"] = facts
        requests = []
        for request in stage.get("configuration_requests", []):
            dependencies = request.get("source_ids")
            if dependencies is None:
                # Historical caches omitted route provenance. Infer it only
                # from a matching node's facts and mode, never stage alone.
                node = request.get("node_path", "").removeprefix("compact:node/")
                basis = "api_getter_observation" if request.get("source") == "api" else "saved_configuration"
                dependencies = [identity for fact in stage.get("observed", [])
                                if str(fact.get("node_id")) == node and fact.get("basis") == basis
                                for identity in fact.get("source_ids", [])]
            if dependencies and set(dependencies) <= available:
                request = dict(request)
                request.pop("source_ids", None)  # Keep it directly callable.
                requests.append(request)
        stage["configuration_requests"] = requests
    for field in ("source_refs", "observed", "configuration_requests"):
        result.pop(field, None)
    return result


def get_case_guide(store, case_id):
    """Read fresh, currently permitted hints without recording a source read."""
    authority = _authority(store, case_id, reading=True)
    base = {"case_id": case_id, "overview": _reference(authority, "misc", "overview"), "is_read_evidence": False}
    try:
        result, saved_binding = _cached_guide(store, case_id)
        if result is None:
            return _authorized_guide(store, case_id, authority, {
                **base, "state": "not_prepared", "detail": "此案例尚未准备导航；仍可读取案例概览。"})
        active, navigation = _navigation(store)
        _scope(store, active, case_id)
        if saved_binding != _binding(navigation):
            raise ValueError("资料或对象目录已变化，请重新构建案例导航。")
        if (not isinstance(result, dict) or result.get("case_id") != case_id or result.get("state") != "ready"
                or not isinstance(result.get("stages"), list) or not isinstance(result.get("source_refs"), list)):
            raise ValueError("案例导航缓存格式无效，请重新构建。")
        _, current_navigation = _navigation(store)
        if saved_binding != _binding(current_navigation):
            raise ValueError("资料或对象目录已变化，请重新构建案例导航。")
        result = _filter_guide(store, result)
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        return _authorized_guide(store, case_id, authority, {**base, "state": "need_rebuild", "detail": str(exc)})
    return _authorized_guide(store, case_id, authority, result)


def _authorized_guide(store, case_id, authority, result):
    # Status-only responses still contain the overview's identity and title.
    # Recheck their permission just as for a prepared guide's cached hints.
    if not store.source_available(case_id):
        raise PermissionError("案例所属资料库已关闭、删除或不可用，不能读取案例导航。")
    store.require_readable_library(authority["library_id"])
    store.require_not_heldout(case_id, authority)
    return result
