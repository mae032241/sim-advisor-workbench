"""Operator-owned logical holdouts, independent of catalogs and search indexes.

Only the administrator compiles/writes this registry. Runtime consumers read an
immutable, mtime-cached exclusion snapshot; a new catalog revision closes access
until the existing selection has been refreshed. No source or index is rebuilt.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import unquote, urlsplit
import uuid

from .catalog_storage import register_catalog_functions, revision_storage


SCHEMA = "evaluation-sets/v1"
REGISTRY_FILENAME = "evaluation-sets.json"
ACCESS_FILENAME = "evaluation-access.json"
_CACHE = {}
_LOCK = threading.RLock()
_SUMMARY_KEYS = ("library_id", "asset_key", "title", "direction", "description", "source_relative_path")


class EvaluationPolicyError(PermissionError):
    """An invalid holdout policy cannot silently restore Agent access."""


class EvaluationPolicyStale(EvaluationPolicyError):
    """Catalog publication preceded refresh of the operator's selection."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    register_catalog_functions(db)
    # mode=ro protects the catalog while permitting connection-local TEMP
    # tables during offline compilation.
    return db


def _watermark(db):
    storage = revision_storage(db)
    table = "_revision_payloads" if storage == "catalog-revisions/zlib-v1" else "revisions"
    # Revisions are immutable and every constructor/API publication inserts a
    # fresh row. The rightmost leaf needs no corpus payload reads. Its citation
    # also detects rowid reuse after an administrator purges the newest rows.
    row = db.execute(f"SELECT rowid,id FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
    return {"storage": storage, "last_revision_rowid": row[0] if row else 0,
            "last_revision_id": row[1] if row else None}


@dataclass(frozen=True)
class HoldoutPolicy:
    citation_ids: frozenset = frozenset()
    object_ids: frozenset = frozenset()
    case_groups: frozenset = frozenset()
    source_hashes: frozenset = frozenset()
    asset_keys: frozenset = frozenset()
    qmd_paths: frozenset = frozenset()
    document_relpaths: frozenset = frozenset()
    source_paths: frozenset = frozenset()
    token: str = ""

    def blocks_identity(self, citation_id, object_id=None):
        return (citation_id in self.citation_ids or citation_id in self.object_ids
                or object_id is not None and object_id in self.object_ids
                # Normal revision IDs preserve their logical object prefix.
                or isinstance(citation_id, str) and len(citation_id) > 13
                and citation_id[-13] == "-" and citation_id[:-13] in self.object_ids)

    def blocks_object(self, item):
        data = item.get("data") or {}
        canonical = data.get("canonical_tutorial") or {}
        return (self.blocks_identity(item.get("id"), item.get("object_id"))
                or item.get("case_group") in self.case_groups
                or (item.get("library_id"), item.get("asset_key")) in self.asset_keys
                or item.get("asset_hash") in self.source_hashes
                or canonical.get("source_sha256") in self.source_hashes)


def _strings(value, name):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"Invalid evaluation exclusion {name}")
    return frozenset(value)


def _parse_registry(registry, *, selection_only=False):
    if not isinstance(registry, dict) or registry.get("schema") != SCHEMA:
        raise ValueError("Unsupported evaluation registry schema")
    sets = registry.get("sets")
    if not isinstance(sets, list):
        raise ValueError("Evaluation sets must be a list")
    identities, summaries = set(), []
    for group in sets:
        if (not isinstance(group, dict) or not isinstance(group.get("id"), str) or not group["id"]
                or group["id"] in identities or not isinstance(group.get("name"), str)
                or not isinstance(group.get("models"), list)):
            raise ValueError("Invalid evaluation set")
        identities.add(group["id"])
        models = []
        for model in group["models"]:
            if (not isinstance(model, dict) or any(not isinstance(model.get(key), str) or not model[key]
                    for key in ("library_id", "asset_key"))):
                raise ValueError("Evaluation models need library_id and asset_key")
            summary = {key: model[key] for key in _SUMMARY_KEYS if isinstance(model.get(key), str)}
            models.append(summary)
        summaries.append({"id": group["id"], "name": group["name"],
                          "description": str(group.get("description") or ""), "models": models, "model_count": len(models)})
    exclusions = registry.get("exclusions")
    if not isinstance(exclusions, dict):
        raise ValueError("Compiled evaluation exclusions are required")
    fields = {name: _strings(exclusions.get(name), name)
              for name in ("citation_ids", "object_ids", "case_groups", "source_hashes", "qmd_paths")}
    keys = exclusions.get("asset_keys")
    if not isinstance(keys, list) or any(not isinstance(pair, list) or len(pair) != 2
            or any(not isinstance(value, str) or not value for value in pair) for pair in keys):
        raise ValueError("Invalid evaluation asset keys")
    fields["asset_keys"] = frozenset(tuple(pair) for pair in keys)
    for name in ("document_relpaths", "source_paths"):
        fields[name] = _strings(exclusions.get(name, []), name)
    if any(group["models"] for group in sets) and not fields["citation_ids"] and not selection_only:
        raise ValueError("A nonempty evaluation selection requires compiled exclusions")
    policy = HoldoutPolicy(**fields, token=hashlib.sha256(_json(registry).encode()).hexdigest())
    summary = {"schema": "evaluation-set-summaries/v1", "sets": summaries, "set_count": len(summaries),
               "model_count": sum(group["model_count"] for group in summaries), "read_only": True, "index_rebuilt": False}
    return policy, summary


def _load(root):
    path = Path(root).resolve() / REGISTRY_FILENAME
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None, HoldoutPolicy(), {"schema": "evaluation-set-summaries/v1", "sets": [], "set_count": 0,
                                      "model_count": 0, "read_only": True, "index_rebuilt": False}
    signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    with _LOCK:
        cached = _CACHE.get(path)
        if cached is not None and cached[0] == signature:
            return cached[1:]
        try:
            registry = json.loads(path.read_text(encoding="utf-8"))
            policy, summary = _parse_registry(registry)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise EvaluationPolicyError("测试集隔离配置无效，已拒绝检索和读取；请由管理员修复。") from exc
        _CACHE[path] = (signature, registry, policy, summary)
        return registry, policy, summary


def exclusion_snapshot(libraries_root):
    """Read-only runtime API. An invalid/stale registry always fails closed."""
    root = Path(libraries_root).resolve()
    registry, policy, _ = _load(root)
    if registry is not None:
        expected = registry.get("catalog_revision_watermark")
        if not isinstance(expected, dict):
            raise EvaluationPolicyError("测试集隔离配置缺少版本水位，请由管理员重新编译。")
        try:
            with closing(_connect(root / "catalog.sqlite3")) as db:
                current = _watermark(db)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise EvaluationPolicyError("无法核对测试集隔离配置，已拒绝检索和读取。") from exc
        if current != expected:
            raise EvaluationPolicyStale("案例目录已更新，测试集隔离配置等待管理员刷新；当前拒绝检索和读取。")
    if registry is not None and _retrieval_enabled(root):
        # Only the web management action changes this gate. Library switches
        # and Agent tools cannot change it. A distinct token invalidates both
        # in-flight searches and cached legacy document exclusions.
        return HoldoutPolicy(token="web-open:" + policy.token)
    return policy


def _retrieval_enabled(libraries_root):
    path = Path(libraries_root).resolve() / ACCESS_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        raise EvaluationPolicyError("测试集检索开关不可读，已拒绝检索；请在网页检查。") from exc
    if (not isinstance(value, dict) or value.get("schema") != "evaluation-access/v1"
            or type(value.get("enabled")) is not bool):
        raise EvaluationPolicyError("测试集检索开关无效，已拒绝检索；请在网页检查。")
    return value["enabled"]


def set_retrieval_enabled(libraries_root, enabled):
    """Web management only; intentionally absent from all Agent tool lists."""
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    root = Path(libraries_root).resolve()
    registry, _, _ = _load(root)
    if registry is None or not registry["sets"]:
        raise ValueError("尚未配置测试集。")
    if enabled:
        # An explicit UI action must not open a stale/invalid selection.
        exclusion_snapshot(root)
    path = root / ACCESS_FILENAME
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".new")
    try:
        temporary.write_text(_json({"schema": "evaluation-access/v1", "enabled": enabled}) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return evaluation_set_summaries(root)


def evaluation_set_summaries(libraries_root):
    """User management metadata only; never expose this method as an Agent tool."""
    result = json.loads(_json(_load(libraries_root)[2]))
    result["retrieval_enabled"] = _retrieval_enabled(libraries_root)
    return result


def compile_registry(catalog_path, sets, *, on_progress=None, previous_registry=None, allow_missing=False):
    """Expand an operator's asset selection across immutable history, once.

    This administrator operation reads existing catalog rows, not MPH/doc files
    or indexes. Runtime requests never perform this scan. A sequential payload
    pass includes legacy/manual copies not recorded in tutorial link tables.
    """
    # Validate the selection before a potentially large offline read.
    placeholder = {"schema": SCHEMA, "sets": sets, "exclusions": {name: [] for name in
                   ("citation_ids", "object_ids", "case_groups", "source_hashes", "qmd_paths", "asset_keys")}}
    _parse_registry(placeholder, selection_only=True)
    wanted = {(model["library_id"], model["asset_key"]) for group in sets for model in group["models"]}
    if not wanted:
        raise ValueError("At least one evaluation model is required")
    selected_hashes = {model["asset_hash"] for group in sets for model in group["models"] if model.get("asset_hash")}
    keys, groups, citations, objects, hashes, paths = set(wanted), set(), set(), set(), set(), set()
    document_relpaths, source_paths = set(), set()
    if previous_registry is not None:
        prior, _ = _parse_registry(previous_registry)
        keys.update(prior.asset_keys)
        groups.update(prior.case_groups)
        citations.update(prior.citation_ids)
        objects.update(prior.object_ids)
        hashes.update(prior.source_hashes)
        selected_hashes.update(prior.source_hashes)
        paths.update(prior.qmd_paths)
        document_relpaths.update(prior.document_relpaths)
        source_paths.update(prior.source_paths)

    def document_path(value):
        if not isinstance(value, str) or not value:
            return
        value = unquote(value).replace("\\", "/")
        parsed = urlsplit(value)
        path = parsed.path if parsed.scheme and not re.match(r"^[a-zA-Z]:/", value) else value
        if not path.lower().endswith((".html", ".htm", ".pdf", ".txt", ".md")):
            return
        source_paths.add(value)
        packaged = re.search(r"(?:^|/)(com\.comsol\.help[^/]+/.*)$", path)
        if packaged:
            document_relpaths.add(packaged[1])
        elif "/doc/" in path.casefold():
            document_relpaths.add(path[path.casefold().rfind("/doc/") + 5:])
        elif not path.startswith("/") and ":" not in path and ".." not in path.split("/"):
            document_relpaths.add(path)

    with closing(_connect(catalog_path)) as db:
        db.execute("BEGIN")
        watermark = _watermark(db)
        libraries = {identity: json.loads(body) for identity, body in db.execute("SELECT id,body FROM libraries")}

        def object_document_paths(item):
            owner = libraries.get(item.get("library_id"), {})
            bindings = {binding["id"]: binding for binding in owner.get("roots", [])}

            def bound_path(root_id, relative):
                document_path(relative)
                binding = bindings.get(root_id)
                if binding:
                    for base in [binding["path"], *binding.get("previous_paths", [])]:
                        candidate = Path(base)
                        if not candidate.is_absolute():
                            candidate = Path(catalog_path).resolve().parent.parent / candidate
                        document_path((candidate / relative).as_posix())

            asset = item.get("asset_key") or ""
            for root_id in sorted(bindings, key=len, reverse=True):
                if asset.startswith(root_id + ":"):
                    bound_path(root_id, asset[len(root_id) + 1:])
                    break
            data = item.get("data") or {}
            for ref in data.get("tutorial_refs", []):
                document_path(ref.get("source_uri"))
            canonical = data.get("canonical_tutorial") or {}
            document_path(canonical.get("source_uri"))
            tutorial = data.get("tutorial") or {}
            for ref in [tutorial, *tutorial.get("alternate_sources", [])]:
                document_path(ref.get("source_uri"))
                document_path(ref.get("asset_path"))
                if ref.get("source_sha256"):
                    hashes.add(ref["source_sha256"])
            for alias in tutorial.get("aliases", []):
                document_path(alias)
            for evidence in item.get("evidence") or []:
                if evidence.get("root_id") and evidence.get("relative_path"):
                    bound_path(evidence["root_id"], evidence["relative_path"])
        # Authorities are a small subset. Retain identity metadata, never the
        # complete decoded corpus. Same-hash copies and all revisions of the
        # same logical asset form one holdout boundary.
        authorities = []
        for (raw,) in db.execute("SELECT payload FROM revisions WHERE id IN (SELECT citation_id FROM case_members WHERE local_id='model-case')"):
            item = json.loads(raw)
            authorities.append({key: item.get(key) for key in ("id", "library_id", "asset_key", "asset_hash", "case_group", "data", "evidence")})
        matched = set()
        changed = True
        while changed:
            changed = False
            for item in authorities:
                pair = (item["library_id"], item["asset_key"])
                if pair not in keys and item["asset_hash"] not in selected_hashes:
                    continue
                matched.add(pair)
                if pair not in keys:
                    keys.add(pair)
                    changed = True
                if item["asset_hash"] and item["asset_hash"] not in selected_hashes:
                    selected_hashes.add(item["asset_hash"])
                    changed = True
                if item["case_group"]:
                    groups.add(item["case_group"])
                citations.add(item["id"])
                object_document_paths(item)
                for ref in (item.get("data") or {}).get("tutorial_refs", []):
                    if ref.get("source_sha256"):
                        hashes.add(ref["source_sha256"])
                for evidence in item.get("evidence") or []:
                    if "associated_tutorial" in str(evidence.get("method", "")) and evidence.get("asset_hash"):
                        hashes.add(evidence["asset_hash"])
        if wanted - matched and not allow_missing:
            raise ValueError("Evaluation selection has no model-case authority: " + repr(sorted(wanted - matched)))
        hashes.update(selected_hashes)
        db.execute("CREATE TEMP TABLE heldout_sources(id TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO heldout_sources VALUES(?)", ((identity,) for identity in citations))
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column in (("tutorial_model_documents", "source_id"), ("tutorial_associations", "source_id")):
            if table in tables:
                hashes.update(row[0] for row in db.execute(f"SELECT DISTINCT source_sha256 FROM {table} WHERE {column} IN (SELECT id FROM heldout_sources)"))
        db.execute("CREATE TEMP TABLE heldout_hashes(hash TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO heldout_hashes VALUES(?)", ((value,) for value in hashes))
        if "tutorial_document_sources" in tables:
            citations.update(row[0] for row in db.execute("SELECT citation_id FROM tutorial_document_sources WHERE source_sha256 IN (SELECT hash FROM heldout_hashes)"))
        if "tutorial_retired_sources" in tables:
            citations.update(row[0] for row in db.execute("SELECT citation_id FROM tutorial_retired_sources WHERE source_sha256 IN (SELECT hash FROM heldout_hashes)"))
        # Canonical tutorial metadata records other formats (especially PDF)
        # with different source hashes. Read those few already-selected chunks
        # before the sequential pass so even an earlier PDF revision is closed.
        db.executemany("INSERT OR IGNORE INTO heldout_sources VALUES(?)", ((identity,) for identity in citations))
        for (raw,) in db.execute("SELECT payload FROM revisions WHERE id IN (SELECT id FROM heldout_sources)"):
            object_document_paths(json.loads(raw))
        count = 0
        for identity, object_id, owner, raw in db.execute("SELECT id,object_id,library_id,payload FROM revisions"):
            item = json.loads(raw)
            canonical = (item.get("data") or {}).get("canonical_tutorial") or {}
            if (identity in citations or item.get("case_group") in groups
                    or (owner, item.get("asset_key")) in keys or item.get("asset_hash") in hashes
                    or canonical.get("source_sha256") in hashes
                    or any(evidence.get("asset_hash") in hashes for evidence in item.get("evidence") or [])):
                citations.add(identity)
                objects.add(object_id)
                if item.get("asset_key"):
                    keys.add((owner, item["asset_key"]))
                object_document_paths(item)
                from .object_index import qmd_path
                paths.add(qmd_path({"id": identity, "library_id": owner}))
            count += 1
            if on_progress and count % 10000 == 0:
                on_progress({"scanned_revisions": count, "excluded_citations": len(citations)})
        # A logical object stays excluded through every historical revision,
        # including legacy aliases whose payload has already been reclaimed.
        db.executemany("INSERT OR IGNORE INTO heldout_sources VALUES(?)", ((identity,) for identity in citations))
        db.execute("CREATE TEMP TABLE heldout_objects(id TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO heldout_objects VALUES(?)", ((identity,) for identity in objects))
        from .object_index import qmd_path
        for identity, owner in db.execute("SELECT id,library_id FROM revisions WHERE object_id IN (SELECT id FROM heldout_objects)"):
            citations.add(identity)
            paths.add(qmd_path({"id": identity, "library_id": owner}))
        db.executemany("INSERT OR IGNORE INTO heldout_sources VALUES(?)", ((identity,) for identity in citations))
        if "tutorial_aliases" in tables:
            for identity, owner in db.execute("SELECT citation_id,owner_library_id FROM tutorial_aliases WHERE target_id IN (SELECT id FROM heldout_sources)"):
                citations.add(identity)
                paths.add(qmd_path({"id": identity, "library_id": owner}))
        if _watermark(db) != watermark:
            raise ValueError("Catalog changed during evaluation compilation")
    return {"schema": SCHEMA, "sets": sets, "compiled_at": datetime.now(timezone.utc).isoformat(),
            "catalog_revision_watermark": watermark,
            "exclusions": {"citation_ids": sorted(citations), "object_ids": sorted(objects), "case_groups": sorted(groups),
                           "asset_keys": [list(pair) for pair in sorted(keys)], "source_hashes": sorted(hashes), "qmd_paths": sorted(paths),
                           "document_relpaths": sorted(document_relpaths), "source_paths": sorted(source_paths)},
            "index_rebuilt": False}


def write_registry(libraries_root, registry):
    """Administrator-only atomic publication; intentionally absent from tools."""
    _parse_registry(registry)
    if not isinstance(registry.get("catalog_revision_watermark"), dict):
        raise ValueError("Compiled catalog revision watermark is required")
    path = Path(libraries_root).resolve() / REGISTRY_FILENAME
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".new")
    try:
        temporary.write_text(_json(registry) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return evaluation_set_summaries(libraries_root)


def refresh_registry(libraries_root, catalog_path=None, *, on_progress=None):
    """Refresh exclusions after publication, preserving the operator's sets."""
    root = Path(libraries_root).resolve()
    registry, _, _ = _load(root)
    if registry is None:
        return None
    catalog_path = catalog_path or root / "catalog.sqlite3"
    with closing(_connect(catalog_path)) as db:
        if _watermark(db) == registry.get("catalog_revision_watermark"):
            return registry
    updated = compile_registry(catalog_path, registry["sets"], on_progress=on_progress,
                               previous_registry=registry, allow_missing=True)
    write_registry(root, updated)
    return updated
