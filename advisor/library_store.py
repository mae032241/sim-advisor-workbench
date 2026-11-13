"""Versioned local libraries and repeatable, bounded construction jobs.

Source files are never edited. Citation IDs include an immutable revision; the
catalog retains old payloads when a new generation becomes active.
"""
from __future__ import annotations

from contextlib import closing, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid

from .build_control import BuildCancelled, BuildControl, build_control, check_cancelled
from .catalog_storage import initialize_revisions, register_catalog_functions
from .workspace_paths import WorkspacePaths
from .case_metadata import link_case_objects, object_feature_branch
from .ui_status import status_summary


SCHEMA = "library/v1"
MANUAL_TYPES = {"official-manuals", "user-manuals"}
TYPES = MANUAL_TYPES | {"official-models", "user-models"}
PARTIAL_EXTRACTIONS = {"partial", "failed", "unavailable", "preview_only"}
ACTIVE_BUILDS = {"queued", "running", "cancel_requested"}


class LibraryLifecycleConflict(ValueError):
    """An active constructor/index task owns this library's current state."""


def extraction_coverage(item):
    """Summarize read completeness separately from task/physical validity."""
    coverage = item.get("coverage") or {}
    raw, deep = coverage.get("raw_xml") or {}, coverage.get("deep_api") or {}
    normalization = raw.get("normalization") or {}
    properties, selections, traversal = (deep.get(key) or {} for key in ("properties", "selections", "traversal"))
    reasons = []
    if not raw and ("raw_xml" in coverage or coverage.get("extraction_level")):
        reasons.append("saved_xml_manifest_missing")
    if raw:
        if raw.get("status") in PARTIAL_EXTRACTIONS | {"missing", "malformed"}:
            reasons.append("saved_xml_" + raw["status"])
        if raw.get("truncated"):
            reasons.append("saved_xml_truncated")
        if not normalization or any(key not in normalization for key in
                ("properties_total", "properties_unresolved", "properties_partial", "selection_statuses")):
            reasons.append("saved_xml_normalization_manifest_missing")
        if normalization.get("properties_unresolved"):
            reasons.append("saved_properties_unresolved")
        if normalization.get("properties_partial"):
            reasons.append("saved_properties_partial")
        if any(count for status, count in normalization.get("selection_statuses", {}).items()
               if status in {"unresolved", "partial", "unknown"}):
            reasons.append("saved_selections_unresolved")
    features = coverage.get("saved_features") or {}
    if any(features.get(key, 0) for key in ("omitted_nodes", "omitted_properties", "omitted_selections")):
        reasons.append("saved_features_omitted")
    if coverage.get("source_container", {}).get("status") == "preview_only":
        reasons.append("preview_only")
    if deep.get("status") in PARTIAL_EXTRACTIONS:
        reasons.append("deep_api_" + deep["status"])
    if deep.get("status") == "observed" and not deep.get("read_contract"):
        reasons.append("deep_api_read_manifest_missing")
    if deep.get("read_errors") or properties.get("unresolved") or selections.get("unresolved") or traversal.get("unresolved"):
        reasons.append("deep_api_unresolved")
    if properties.get("partial"):
        reasons.append("deep_api_properties_partial")
    if deep.get("truncated") or any(deep.get("truncation_reasons", {}).values()):
        reasons.append("deep_api_truncated")
    return {"status": "partial" if reasons else "complete" if raw or deep else "not_applicable",
            "raw_xml_status": raw.get("status"), "deep_api_status": deep.get("status", "not_requested"),
            "reasons": reasons,
            "properties_unresolved": normalization.get("properties_unresolved", 0) + properties.get("unresolved", 0),
            "properties_partial": normalization.get("properties_partial", 0) + properties.get("partial", 0)}


def partial_extraction(item):
    return extraction_coverage(item)["status"] == "partial"


def job_coverage(job):
    summaries = [extraction_coverage(item) for item in job.get("items", [])]
    return {"status": "partial" if any(item["status"] == "partial" for item in summaries) else
                     "complete" if any(item["status"] == "complete" for item in summaries) else "not_applicable",
            "partial_files": sum(item["status"] == "partial" for item in summaries),
            "deep_api_not_requested": sum(item["deep_api_status"] == "not_requested" and item["status"] != "not_applicable" for item in summaries),
            "properties_unresolved": sum(item["properties_unresolved"] for item in summaries),
            "properties_partial": sum(item["properties_partial"] for item in summaries)}


def job_counts(job):
    if job.get("kind") == "api_enrichment" and isinstance(job.get("api_counts"), dict):
        counts = job["api_counts"]
        return {"succeeded": counts.get("completed", 0), "partial": counts.get("partial", 0), "failed": counts.get("failed", 0)}
    items = job.get("items", [])
    partial = sum(partial_extraction(item) or item.get("enrichment", {}).get("status") == "failed" for item in items)
    return {"succeeded": len(items) - partial, "partial": partial, "failed": len(job.get("failed", []))}


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        hasher = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            check_cancelled()
            hasher.update(block)
        check_cancelled()
        return hasher.hexdigest()


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of death.
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _ordered_identity_hash(rows):
    """Canonical JSON-array hash of SQL-ordered IDs, without a Python list."""
    hasher, count = hashlib.sha256(), 0
    hasher.update(b"[")
    for row in rows:
        check_cancelled()
        if count:
            hasher.update(b",")
        hasher.update(encoded(row[0]).encode("utf-8"))
        count += 1
    hasher.update(b"]")
    check_cancelled()
    return hasher.hexdigest(), count


class _CatalogObjectStream:
    """Trusted, reiterable ID-ordered payloads from one read-only SQL snapshot.

    The caller holds the catalog build lease and keeps this context open for
    every indexing pass. Only one decoded source object is retained per cursor;
    all cursors and the snapshot close on success, failure or cancellation.
    """
    def __init__(self, path):
        self.path = Path(path)
        self._db, self._cursors = None, set()
        self.generation, self._count = None, 0

    def __enter__(self):
        if self._db is not None:
            raise RuntimeError("Catalog object stream is already open")
        try:
            if (self.path.parent / ".catalog-migration.lock").exists():
                raise RuntimeError("Catalog is undergoing offline maintenance")
            self._db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30)
            register_catalog_functions(self._db)
            self._db.execute("PRAGMA query_only=ON")

            def interrupted():
                try:
                    check_cancelled()
                except BuildCancelled:
                    return 1
                return 0

            self._db.set_progress_handler(interrupted, 10000)
            self._db.execute("BEGIN")
            with closing(self._db.execute("SELECT citation_id FROM active ORDER BY citation_id")) as rows:
                fingerprint, self._count = _ordered_identity_hash(rows)
            self.generation = fingerprint[:24]
            return self
        except Exception:
            self.close()
            check_cancelled()
            raise

    def __len__(self):
        if self._db is None:
            raise RuntimeError("Catalog object stream requires an open snapshot")
        return self._count

    def __iter__(self):
        if self._db is None:
            raise RuntimeError("Catalog object stream requires an open snapshot")
        # IN materializes only the small identity set. A payload JOIN followed
        # by ORDER BY can instead sort the complete multi-GB payload corpus.
        cursor = None
        try:
            check_cancelled()
            cursor = self._db.execute("SELECT id,library_id,payload FROM revisions "
                                      "WHERE id IN (SELECT citation_id FROM active) ORDER BY id")
            self._cursors.add(cursor)
            for identity, library_id, raw in cursor:
                check_cancelled()
                item = json.loads(raw)
                if not isinstance(item, dict) or item.get("id") != identity or item.get("library_id") != library_id:
                    raise ValueError("Catalog payload identity does not match its revision")
                yield item
                if self._db is None:
                    return
        except sqlite3.Error:
            check_cancelled()
            raise
        finally:
            if cursor is not None and cursor in self._cursors:
                self._cursors.remove(cursor)
                cursor.close()

    def close(self):
        cursors, self._cursors = tuple(self._cursors), set()
        db, self._db = self._db, None
        try:
            for cursor in cursors:
                try:
                    cursor.close()
                except sqlite3.Error:
                    pass  # Snapshot shutdown must not replace cancellation.
        finally:
            if db is not None:
                db.close()

    def __exit__(self, *_):
        self.close()


class LibraryStore:
    def __init__(self, data_dir, *, workspace_root=None):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.paths = WorkspacePaths(workspace_root or (self.root.parent if self.root.name == "libraries" else self.root))
        self.db_path = self.root / "catalog.sqlite3"
        self._build_lock = threading.Lock()
        with closing(self.connect()) as db, db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS libraries(id TEXT PRIMARY KEY, body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS active(library_id TEXT, object_id TEXT,
                citation_id TEXT NOT NULL, PRIMARY KEY(library_id,object_id));
              CREATE TABLE IF NOT EXISTS builds(id TEXT PRIMARY KEY, body TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS builds_status ON builds(json_extract(body,'$.status'));
              CREATE TABLE IF NOT EXISTS case_members(group_id TEXT NOT NULL, local_id TEXT NOT NULL,
                citation_id TEXT NOT NULL, PRIMARY KEY(group_id,local_id));
              CREATE INDEX IF NOT EXISTS case_members_citation ON case_members(citation_id,group_id);
            """)
            initialize_revisions(db)
            from .tutorial_catalog import ensure_schema
            ensure_schema(db)
            from .tutorial_aliases import ensure_alias_schema
            ensure_alias_schema(db)

    def connect(self):
        maintenance = self.root / ".catalog-migration.lock"
        if maintenance.exists():
            raise RuntimeError("Catalog is undergoing offline maintenance")
        db = sqlite3.connect(self.db_path, timeout=30)
        try:
            register_catalog_functions(db)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            if maintenance.exists():
                raise RuntimeError("Catalog is undergoing offline maintenance")
            return db
        except BaseException:
            db.close()
            raise

    def _roots(self, roots, *, encode=False):
        locate = self.paths.encode if encode else lambda path: str(self.paths.resolve(path))
        result = []
        for root in roots:
            value = dict(root, path=locate(root["path"]))
            if "previous_paths" in root:
                previous = root["previous_paths"]
                if not isinstance(previous, list) or any(not isinstance(path, str) or not path.strip() for path in previous):
                    raise ValueError("Invalid previous source directory locators")
                value["previous_paths"] = [locate(path) for path in previous]
            result.append(value)
        return result

    def _library(self, library, *, encode=False):
        enabled, deleted = library.get("enabled", True), library.get("deleted_at")
        if type(enabled) is not bool or deleted is not None and (not isinstance(deleted, str) or not deleted.strip()):
            raise ValueError("Invalid library lifecycle metadata")
        return dict(library, enabled=enabled, deleted_at=deleted, roots=self._roots(library["roots"], encode=encode))

    def register(self, name, type, roots, version="", library_id=None):
        if not isinstance(type, str) or type not in TYPES or not isinstance(name, str) or not name.strip():
            raise ValueError("Library needs a name and a supported type")
        if not isinstance(roots, list) or not roots:
            raise ValueError("At least one source directory is required")
        normalized = []
        for index, root in enumerate(roots):
            if not isinstance(root, dict) or not isinstance(root.get("path"), str):
                raise ValueError("Every root requires a directory path")
            path = Path(root["path"]).expanduser().resolve()
            if not path.is_dir():
                raise ValueError(f"Source directory does not exist: {path}")
            normalized.append({"id": str(root.get("id", f"root{index}")),
                               "role": root.get("role", "models"), "path": str(path)})
        if len({r["id"] for r in normalized}) != len(normalized):
            raise ValueError("Root IDs must be unique")
        lid = library_id or "lib-" + uuid.uuid4().hex[:20]
        if not all(c.isalnum() or c in "-_" for c in lid) or len(lid) > 100:
            raise ValueError("Invalid library ID")
        body = {"id": lid, "name": str(name).strip(), "type": type, "version": str(version),
                "roots": normalized, "schema": SCHEMA, "created_at": now(), "generation": None,
                "enabled": False, "deleted_at": None}
        with closing(self.connect()) as db, db:
            if db.execute("SELECT 1 FROM libraries WHERE id=?", (lid,)).fetchone():
                raise ValueError("Library already exists; use rebind to relocate it")
            db.execute("INSERT INTO libraries VALUES(?,?)", (lid, encoded(self._library(body, encode=True))))
        return body

    def library(self, library_id, include_deleted=False):
        with closing(self.connect()) as db:
            row = db.execute("SELECT body FROM libraries WHERE id=?", (library_id,)).fetchone()
        if not row:
            raise FileNotFoundError("Library not found")
        library = self._library(json.loads(row[0]))
        if library["deleted_at"] is not None and not include_deleted:
            raise FileNotFoundError("Library was deleted; restore it before using it")
        return library

    def libraries(self, include_deleted=False):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT body FROM libraries ORDER BY id").fetchall()
            counts = dict(db.execute("SELECT library_id,COUNT(*) FROM active GROUP BY library_id"))
        libraries = [self._library(json.loads(row[0])) for row in rows]
        return [dict(library, object_count=counts.get(library["id"], 0)) for library in libraries
                if include_deleted or library["deleted_at"] is None]

    def searchable_library_ids(self, requested=None):
        if requested is not None and (not isinstance(requested, list) or not all(isinstance(value, str) for value in requested)):
            raise ValueError("Requested library IDs must be a list of strings")
        from pathlib import PureWindowsPath
        anchors = (self.paths.root, *sorted(self.paths.previous_roots, key=lambda path: len(path.parts), reverse=True))

        def validate_locator(value):
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError("Invalid source directory locator")
            path = Path(value).expanduser()
            if path.is_absolute():
                for anchor in anchors:
                    if path.is_relative_to(anchor):
                        path = path.relative_to(anchor)
                        break
                else:
                    return  # External and offline absolute sources are valid.
            elif PureWindowsPath(value).drive:
                raise ValueError("Drive-relative workspace locators are not supported")
            # Preserve locator boundary checks without filesystem resolution,
            # which is unnecessary for a lifecycle permission decision.
            if not Path(os.path.normpath(self.paths.root / path)).is_relative_to(self.paths.root):
                raise ValueError("Workspace locator escapes the data directory")

        with closing(self.connect()) as db:
            rows = db.execute("SELECT id,body FROM libraries").fetchall()
        eligible = set()
        # Validate every record, even for an empty/explicit request: a malformed
        # catalog must not become an enable-all or partially accepted scope.
        for library_id, raw in rows:
            library = json.loads(raw)
            if not isinstance(library, dict) or not isinstance(library_id, str) or not library_id or library.get("id") != library_id:
                raise ValueError("Invalid library body/id")
            enabled, deleted = library.get("enabled", True), library.get("deleted_at")
            if type(enabled) is not bool or deleted is not None and (not isinstance(deleted, str) or not deleted.strip()):
                raise ValueError("Invalid library lifecycle metadata")
            roots = library.get("roots")
            if not isinstance(roots, list):
                raise ValueError("Invalid library source roots")
            for root in roots:
                if not isinstance(root, dict) or "path" not in root:
                    raise ValueError("Invalid library source root")
                validate_locator(root["path"])
                if "previous_paths" in root:
                    previous = root["previous_paths"]
                    if not isinstance(previous, list) or any(not isinstance(path, str) or not path.strip() for path in previous):
                        raise ValueError("Invalid previous source directory locators")
                    for path in previous:
                        validate_locator(path)
            if enabled and deleted is None:
                eligible.add(library_id)
        return sorted(eligible) if requested is None else list(dict.fromkeys(value for value in requested if value in eligible))

    @staticmethod
    def _scope_library(library):
        """Only catalog metadata needed to select a retrieval scope."""
        return {**{key: library.get(key, "") for key in ("id", "name", "type", "version")},
                "enabled": library["enabled"], "object_count": library.get("object_count", 0),
                "roots": [{key: root.get(key, "") for key in ("id", "role", "path")}
                          for root in library.get("roots", [])]}

    def library_scope(self, *, query=None, type=None):
        """List selectable libraries without loading source text, jobs or indexes."""
        if query is not None and (not isinstance(query, str) or len(query) > 2000):
            raise ValueError("query must be a bounded text string")
        if type is not None and (not isinstance(type, str) or type not in TYPES):
            raise ValueError("Unsupported library type")
        needle = (query or "").strip().casefold()
        catalog = self.libraries()
        libraries = []
        for library in catalog:
            item = self._scope_library(library)
            if type is not None and item["type"] != type:
                continue
            if needle and needle not in " ".join(
                    [item["id"], item["name"], item["version"], *(root["path"] for root in item["roots"])]).casefold():
                continue
            libraries.append(item)
        by_type = {kind: [item for item in libraries if item["type"] == kind] for kind in TYPES}
        return {"schema": "library-scope/v1", "libraries": libraries,
                "groups": [{"id": "official", "name": "官方资料", "groups": [
                    {"id": "official-manuals", "name": "手册", "libraries": by_type["official-manuals"]},
                    {"id": "official-models", "name": "模型案例", "libraries": by_type["official-models"]}]},
                    {"id": "custom", "name": "自定义资料",
                     "libraries": [item for item in libraries if item["type"] in {"user-manuals", "user-models"}],
                     "groups": [{"id": "user-manuals", "name": "手册", "libraries": by_type["user-manuals"]},
                                {"id": "user-models", "name": "模型案例", "libraries": by_type["user-models"]}]}],
                "enabled_library_ids": [item["id"] for item in libraries if item["enabled"]],
                "library_count": len(libraries), "enabled_library_count": sum(item["enabled"] for item in libraries),
                "global_library_count": len(catalog), "matched_library_count": len(libraries),
                "filter_applied": bool(needle or type is not None),
                "query": query, "type": type, "query_match_mode": "literal_substring",
                "next_action": ("当前筛选没有匹配资料库，不表示目录为空；调用 get_library_scope({}) 查看完整目录，再按研究需要选择库。不要自动启用全部库。"
                                if not libraries and (needle or type is not None) and catalog else
                                "目录为空，当前没有已登记且未删除的资料库。" if not catalog else
                                "按当前研究需要选择资料库；启停状态仅由显式 set_libraries_enabled 调用改变。")}

    def set_enabled_many(self, library_ids, enabled):
        """Atomically change one explicit scope; retained indexes stay untouched."""
        if (not isinstance(library_ids, list) or not library_ids or len(library_ids) > 1000
                or any(not isinstance(lid, str) or not lid or len(lid) > 100 for lid in library_ids)):
            raise ValueError("library_ids must be a nonempty list of at most 1000 bounded IDs")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        identities = list(dict.fromkeys(library_ids))
        selected = set(identities)
        self._recover_active_job_owners()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" for _ in identities)
            rows = db.execute(f"SELECT id,body FROM libraries WHERE id IN ({marks})", identities).fetchall()
            libraries = {row[0]: self._library(json.loads(row[1])) for row in rows}
            missing = [lid for lid in identities if lid not in libraries]
            if missing:
                raise FileNotFoundError("Library not found: " + ", ".join(missing))
            if any(item["deleted_at"] is not None for item in libraries.values()):
                raise FileNotFoundError("Library was deleted; restore it before changing its enabled state")
            for job in self._active_job_owners(db):
                if job.get("kind") == "api_enrichment":
                    continue  # Enrichment never rewrites saved enabled flags.
                if (job.get("library_id") in selected or selected.intersection(job.get("library_ids") or [])
                        or job.get("scope") == "all_libraries" or job.get("kind") == "reindex" and not job.get("library_ids")):
                    raise LibraryLifecycleConflict(f"所选资料库存在进行中的构建或索引任务，请先中断并等待结束后再操作（任务 {job['id']}）。")
            changed = 0
            for lid in identities:
                item = libraries[lid]
                if item["enabled"] != enabled:
                    item.update(enabled=enabled, updated_at=now())
                    db.execute("UPDATE libraries SET body=? WHERE id=?", (encoded(self._library(item, encode=True)), lid))
                    changed += 1
            counts = dict(db.execute(f"SELECT library_id,COUNT(*) FROM active WHERE library_id IN ({marks}) GROUP BY library_id", identities))
        return {"libraries": [self._scope_library(dict(libraries[lid], object_count=counts.get(lid, 0))) for lid in identities],
                "library_ids": identities, "enabled": enabled, "changed": changed, "index_rebuilt": False}

    def _change_lifecycle(self, library_id, operation, enabled=None):
        # Recover genuinely dead/moved workers before checking current ownership.
        # The write transaction then checks all active rows, not only UI history.
        self._recover_active_job_owners()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM libraries WHERE id=?", (library_id,)).fetchone()
            if not row:
                raise FileNotFoundError("Library not found")
            library = self._library(json.loads(row[0]))
            if operation == "enabled" and library["deleted_at"] is not None:
                raise FileNotFoundError("Library was deleted; restore it before enabling it")
            for job in self._active_job_owners(db):
                if operation == "enabled" and job.get("kind") == "api_enrichment":
                    continue  # API staging/publication can coexist with scope changes.
                if (job.get("library_id") == library_id or library_id in (job.get("library_ids") or [])
                        or job.get("scope") == "all_libraries" or job.get("kind") == "reindex" and not job.get("library_ids")):
                    raise LibraryLifecycleConflict(f"该资料库存在进行中的构建或索引任务，请先中断并等待结束后再操作（任务 {job['id']}）。")
            if operation == "enabled":
                library["enabled"] = enabled
            elif operation == "delete":
                library.update(enabled=False, deleted_at=library["deleted_at"] or now())
            elif operation == "restore":
                library.update(enabled=False, deleted_at=None)
            library["updated_at"] = now()
            db.execute("UPDATE libraries SET body=? WHERE id=?", (encoded(self._library(library, encode=True)), library_id))
        return library

    @staticmethod
    def _active_job_owners(db):
        """Project only lifecycle ownership; completed diagnostics stay on disk."""
        query = """SELECT json_object('id',id,
          'library_id',json_extract(body,'$.library_id'),'library_ids',json_extract(body,'$.library_ids'),
          'scope',json_extract(body,'$.scope'),'kind',json_extract(body,'$.kind'),
          'status',json_extract(body,'$.status'),'pid',json_extract(body,'$.pid'),
          'workspace_root',json_extract(body,'$.workspace_root'))
          FROM builds WHERE json_extract(body,'$.status') IN ('queued','running','cancel_requested')"""
        for row in db.execute(query):
            yield json.loads(row[0])

    def _recover_active_job_owners(self):
        with closing(self.connect()) as db:
            owners = list(self._active_job_owners(db))
        for job in owners:
            if self._owner_moved(job) or job.get('pid') and not process_alive(job['pid']):
                # Recovery is exceptional: preserve the complete old job when
                # persisting interruption rather than its ownership projection.
                self.job(job['id'])

    def set_enabled(self, library_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        return self._change_lifecycle(library_id, "enabled", enabled)

    def delete_library(self, library_id):
        return self._change_lifecycle(library_id, "delete")

    def restore_library(self, library_id):
        return self._change_lifecycle(library_id, "restore")

    def unfinished_vectors(self):
        from .unfinished_vectors import preview
        return preview(self)

    def clean_unfinished_vectors(self, *, confirmed=False, token=None):
        from .unfinished_vectors import clean
        return clean(self, confirmed=confirmed, token=token)

    def embedding_settings(self):
        from .retrieval_settings import read
        return read(self.root)

    def configure_embedding(self, *, enabled, model_path=None):
        from .retrieval_settings import update
        from .library_search import LibrarySearch
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if not self._build_lock.acquire(blocking=False):
            raise LibraryLifecycleConflict("请等待构建、索引或清理结束后再修改 embedding 设置。")
        search = LibrarySearch.for_maintenance(self.root / "search")
        acquired, handle = False, None
        try:
            lease = self.root / ".build.lock"
            if lease.exists():
                try:
                    owner = json.loads(lease.read_text(encoding="utf-8"))
                    busy = not self._owner_moved(owner) and process_alive(owner.get("pid"))
                except (ValueError, OSError, TypeError):
                    busy = True
                if busy:
                    raise LibraryLifecycleConflict("请等待构建、索引或清理结束后再修改 embedding 设置。")
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM builds WHERE json_extract(body,'$.status') IN ('queued','running','cancel_requested') LIMIT 1").fetchone():
                    raise LibraryLifecycleConflict("请等待资料库任务结束后再修改 embedding 设置。")
                acquired = search.lock.acquire(blocking=False)
                if not acquired:
                    raise LibraryLifecycleConflict("请等待索引构建结束后再修改 embedding 设置。")
                try:
                    handle, token = search._acquire_lease("embedding-settings")
                except RuntimeError:
                    raise LibraryLifecycleConflict("其它进程正在构建索引，请等待结束。") from None
                return update(self.root, enabled=enabled, model_path=model_path)
        finally:
            if handle is not None:
                search._release_lease(handle, token)
            if acquired:
                search.lock.release()
            self._build_lock.release()

    def _purge_file_plan(self, db, library, on_progress=None):
        """Prove ownership before deleting any file; never infer from a name."""
        lid = library["id"]
        last_progress = 0.0

        def planning(phase, completed=None, total=None, unit=None, *, force=False):
            nonlocal last_progress
            current = time.monotonic()
            if on_progress and (force or current - last_progress >= 0.25):
                on_progress({"stage": "planning", "phase": phase, "completed": completed,
                             "total": total, "unit": unit})
                last_progress = current

        planning("revisions", 0, unit="objects", force=True)
        # Project only ownership locators. Retained snapshots and extracted text
        # can make a single revision/job much larger than its cleanup metadata.
        revisions = db.execute(
            "SELECT json_extract(payload,'$.id'),json_extract(payload,'$.asset_key'),"
            "json_extract(payload,'$.asset_hash') FROM revisions WHERE library_id=?", (lid,))
        revision_ids, assets = set(), set()
        relative_paths, artifact_names = set(), {}
        root_ids = sorted((root["id"] for root in library["roots"]), key=len, reverse=True)
        for index, (identity, asset, fingerprint) in enumerate(revisions, 1):
            revision_ids.add(identity)
            planning("revisions", index, unit="objects")
            if (asset, fingerprint) in assets:
                continue
            assets.add((asset, fingerprint))
            asset = asset or ""
            root_id = next((key for key in root_ids if asset.startswith(key + ":")), None)
            if root_id:
                relative = asset[len(root_id)+1:]
                relative_paths.add(relative)
                if re.fullmatch(r"[a-f0-9]{64}", str(fingerprint)):
                    artifact_names[digest([lid, relative, fingerprint])[:24]] = (relative, fingerprint)
        planning("jobs", 0, unit="objects", force=True)
        jobs = [row[0] for row in db.execute(
            "SELECT json_extract(body,'$.id') FROM builds WHERE json_extract(body,'$.library_id')=?", (lid,))]
        for field in ("items", "failed"):
            for index, row in enumerate(db.execute(
                    "WITH owned(body) AS MATERIALIZED (SELECT body FROM builds WHERE json_extract(body,'$.library_id')=?) "
                    "SELECT json_extract(item.value,'$.path') FROM owned,json_each(owned.body,?) item "
                    "WHERE item.type='object' AND json_type(item.value,'$.path')='text'",
                    (lid, "$." + field)), 1):
                relative_paths.add(row[0])
                planning("jobs", index, unit="objects")
        planning("artifacts", 0, unit="directories", force=True)
        artifacts, unknown = [], 0
        directory = self.root / "artifacts"
        if directory.exists() and (directory.is_symlink() or getattr(directory, "is_junction", lambda: False)()):
            raise ValueError("提取缓存根含符号链接或联接；未跟随读取或删除。")
        for index, candidate in enumerate(directory.iterdir() if directory.is_dir() else [], 1):
            planning("artifacts", index, unit="directories")
            if not candidate.is_dir() or not re.fullmatch(r"[a-f0-9]{24}", candidate.name):
                continue
            if (candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)()
                    or not candidate.resolve().is_relative_to(self.root)):
                raise ValueError("提取缓存目录含符号链接或联接；未跟随读取或删除，请先检查。")
            owner = candidate / "library-owner.json"
            if owner.is_file():
                try:
                    info = json.loads(owner.read_text(encoding="utf-8"))
                    if not isinstance(info, dict):
                        raise ValueError("Invalid owner")
                except (OSError, ValueError):
                    if candidate.name in artifact_names:
                        raise ValueError("目标提取缓存归属标记损坏；未删除，请先修复元数据。") from None
                    unknown += 1
                    continue
                if info.get("library_id") != lid:
                    continue
                if (info.get("schema") != "library-artifact-owner/v1" or not isinstance(info.get("relative_path"), str)
                        or not re.fullmatch(r"[a-f0-9]{64}", str(info.get("content_hash", "")))
                        or digest([lid, info["relative_path"], info["content_hash"]])[:24] != candidate.name):
                    raise ValueError("提取缓存归属标记无效；未删除，请先修复缓存元数据。")
                artifacts.append(candidate)
                continue
            if candidate.name in artifact_names:
                artifacts.append(candidate)
                continue
            cache = candidate / "extraction.json"
            assigned = False
            if cache.is_file() and cache.stat().st_size <= 64 * 1024 * 1024:
                try:
                    value = json.loads(cache.read_text(encoding="utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError("Invalid extraction cache")
                except (OSError, ValueError):
                    unknown += 1
                    continue
                snapshot = value.get("result", {}).get("model_snapshot", {})
                fingerprint, source = snapshot.get("fingerprint"), snapshot.get("path")
                if re.fullmatch(r"[a-f0-9]{64}", str(fingerprint)) and isinstance(source, str):
                    source = self.paths.resolve(source)
                    for root in value.get("root_bindings", []):
                        location = self.paths.resolve(root["path"])
                        if source.is_relative_to(location):
                            relative = source.relative_to(location).as_posix()
                            if digest([lid, relative, fingerprint])[:24] == candidate.name:
                                artifact_names[candidate.name] = (relative, fingerprint)
                                artifacts.append(candidate)
                                assigned = True
                                break
            if assigned:
                continue
            # Legacy failed extractions may have a saved-state artifact but no
            # extraction.json/revision. Match BOTH known job-relative path and
            # recorded source fingerprint to the constructor's exact digest.
            for saved in candidate.glob("mph-*/saved-state.json"):
                with saved.open("r", encoding="utf-8") as stream:
                    header = stream.read(4096)
                fingerprint = re.search(r'"source_sha256"\s*:\s*"([a-f0-9]{64})"', header)
                relative = next((relative for relative in relative_paths if fingerprint and
                                 digest([lid, relative, fingerprint[1]])[:24] == candidate.name), None)
                if '"mph-saved-state/v1"' in header and relative is not None:
                    artifact_names[candidate.name] = (relative, fingerprint[1])
                    artifacts.append(candidate)
                    assigned = True
                    break
            if not assigned:
                unknown += 1

        planning("evidence", 0, unit="objects", force=True)
        resolved = {self.root: self.root}

        def resolve(path):
            path = Path(path)
            if path not in resolved:
                resolved[path] = path.resolve()
            return resolved[path]

        def blob_references(where, values):
            references = set()
            for index, row in enumerate(db.execute(
                "SELECT DISTINCT json_extract(e.value,'$.relative_path') FROM revisions r, json_each(r.payload,'$.evidence') e "
                "WHERE " + where + " AND json_extract(e.value,'$.root_id')='derived' "
                "AND json_type(e.value,'$.relative_path')='text'", values), 1):
                references.add(str(resolve(self.root / row[0])))
                planning("evidence", index, unit="objects")
            return references
        own = blob_references("r.library_id=?", (lid,))
        shared = blob_references("r.library_id<>?", (lid,))
        # A cancelled/failed constructor can own canonical assets before any
        # revision is published. Other caches also keep shared blobs alive.
        owned_artifacts = set(artifacts)
        for candidate in directory.iterdir() if directory.is_dir() else []:
            if not candidate.is_dir() or not re.fullmatch(r"[a-f0-9]{24}", candidate.name):
                continue
            if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
                raise ValueError("提取缓存目录含符号链接或联接；未跟随读取或删除。")
            manifest = candidate / "artifact-blobs.json"
            if not manifest.is_file():
                continue  # Legacy caches have no canonical cache-only assets.
            if manifest.is_symlink() or manifest.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("提取缓存无法安全检查共享证据；未删除任何文件。")
            value = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema") != "library-artifact-blobs/v1":
                raise ValueError("提取缓存证据清单无效；未删除任何文件。")
            references = own if candidate in owned_artifacts else shared
            for entry, _, blob in self._artifact_entries(value, candidate):
                references.add(str(resolve(blob)))
            for entry in value.get("retained_blobs", []):
                blob = self.paths.resolve(entry["blob"])
                if not blob.is_relative_to(self.root / "blobs") or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256", ""))) or blob.name[:64] != entry["sha256"]:
                    raise ValueError("提取缓存证据清单越过受控目录；未删除任何文件。")
                references.add(str(resolve(blob)))
        blobs = [Path(value) for value in own - shared if Path(value).is_file()]
        markers = [self._cancel_path(identity) for identity in jobs if re.fullmatch(r"[a-f0-9]{32}", str(identity)) and self._cancel_path(identity).is_file()]
        def components(path):
            return tuple(os.path.normcase(part) for part in path.parts)

        protected = {components(self.paths.resolve(root["path"])) for row in db.execute("SELECT body FROM libraries")
                     for root in json.loads(row[0])["roots"]}

        def safe(path, container):
            path, container = Path(path), Path(container)
            location, boundary = resolve(path), resolve(container)
            if not boundary.is_relative_to(self.root) or not location.is_relative_to(boundary):
                raise ValueError("待清理文件越过资料库受控目录；未删除原始资料。")
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError("待清理目录含符号链接或联接；请先检查，未跟随删除。")
            # Compare normalized components of already resolved paths, including
            # the drive/anchor. Path.is_relative_to rebuilds every parent chain
            # for every source root, which dominates large Windows purges.
            parts = components(location)
            if any(parts[:len(root)] == root or root[:len(parts)] == parts for root in protected):
                raise ValueError("资料库缓存与登记的原始资料目录重叠；不能安全彻底删除，请先修正绑定。")

        planning("validation", 0, unit="files", force=True)
        # The catalog and search helper also write inside owned locations.
        safe(self.db_path, self.root)
        if (self.root / "search").exists():
            safe(self.root / "search", self.root)
        inventory, examined = {}, 0
        # Delete canonical payloads before their ownership ledgers. A failed
        # unlink must leave cache-only blob ownership discoverable on retry.
        for path, container in [*((p, self.root / "blobs") for p in blobs), *((p, directory) for p in artifacts), *((p, self.root / "build-control") for p in markers)]:
            safe(path, container)
            files, size = 0, 0
            if path.is_dir():
                pending = [path]
                while pending:
                    with os.scandir(pending.pop()) as entries:
                        for child in entries:
                            info = child.stat(follow_symlinks=False)
                            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", None)
                                    == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)):
                                raise ValueError("提取缓存包含符号链接或联接；未跟随删除，请先检查。")
                            if stat.S_ISDIR(info.st_mode):
                                pending.append(child.path)
                            elif stat.S_ISREG(info.st_mode):
                                files += 1
                                size += info.st_size
                            examined += 1
                            planning("validation", examined, unit="files")
            else:
                files, size = 1, path.stat().st_size
                examined += 1
                planning("validation", examined, unit="files")
            inventory[path] = (files, size)
        return {"artifacts": artifacts, "blobs": blobs, "markers": markers, "unknown": unknown,
                "shared_blobs": len(own & shared), "revision_ids": revision_ids, "inventory": inventory,
                "artifact_identities": artifact_names}

    @staticmethod
    def _purge_shared_job(job, library_id, removed_ids):
        """Keep shared audit history, pruning only exact structured locators."""
        key = "lib_" + hashlib.sha256(library_id.encode()).hexdigest()[:20]
        def clean(value):
            if not isinstance(value, dict):
                return
            if isinstance(value.get("library_ids"), list):
                value["library_ids"] = [lid for lid in value["library_ids"] if lid != library_id]
            if isinstance(value.get("libraries"), dict):
                value["libraries"].pop(library_id, None)
            if isinstance(value.get("mapping"), dict):
                value["mapping"] = {name: identity for name, identity in value["mapping"].items()
                                    if not name.startswith(key + "/") and identity not in removed_ids}
            representation = value.get("representation")
            if isinstance(representation, dict) and isinstance(representation.get("objects"), dict):
                representation["objects"] = {identity: info for identity, info in representation["objects"].items() if identity not in removed_ids}
            for field in ("embedding_records", "progress"):
                if isinstance(value.get(field), dict):
                    value[field].pop(library_id, None)
            clean(value.get("last_build"))
            for mode in value.get("modes", {}).values() if isinstance(value.get("modes"), dict) else []:
                clean(mode)
        clean(job)
        clean(job.get("indexes"))
        return job

    def purge_progress(self, library_id):
        """A compact in-memory view; polling never waits for the catalog lock."""
        return dict(getattr(self, "_purge_states", {}).get(library_id,
                    {"library_id": library_id, "status": "idle", "stage": "planning",
                     "completed": None, "total": None, "unit": None, "updated_at": None}))

    def purge_library(self, library_id, confirmed=False):
        """Remove registered derived data, never the original source roots.

        Cleanup precedes registry deletion: a failed file/index operation leaves
        a soft-deleted record so this operation can be retried idempotently.
        This reclaims owned files/SQLite space; it is not forensic secure erasure.
        """
        if confirmed is not True:
            raise ValueError("彻底删除不可恢复，请明确确认后再操作。")
        library = self.library(library_id, include_deleted=True)
        if library["deleted_at"] is None:
            raise ValueError("请先将资料库移入已删除列表，再执行彻底删除。")
        if not self._build_lock.acquire(blocking=False):
            raise LibraryLifecycleConflict("存在进行中的构建或索引任务，请先中断并等待结束后再彻底删除。")
        if not hasattr(self, "_purge_states"):
            self._purge_states = {}
        state = {"library_id": library_id, "status": "running", "stage": "planning", "phase": "jobs",
                 "completed": None, "total": None, "unit": None, "started_at": now()}

        def progress(update=None, **fields):
            state.update(update or {}, **fields, updated_at=now())
            self._purge_states[library_id] = dict(state)

        progress()
        lease, token, owned = self.root / ".build.lock", uuid.uuid4().hex, False
        try:
            self.jobs_summary()  # Recover dead owners without loading retained index maps.
            if lease.is_file():
                prior = json.loads(lease.read_text(encoding="utf-8"))
                if not self._owner_moved(prior) and process_alive(prior.get("pid")):
                    raise LibraryLifecycleConflict("存在进行中的构建或索引任务，请先中断并等待结束后再彻底删除。")
                lease.unlink()
            try:
                with lease.open("x", encoding="utf-8") as stream:
                    stream.write(encoded({"pid": os.getpid(), "workspace_root": str(self.paths.root), "kind": "purge", "library_id": library_id, "token": token}))
                owned = True
            except FileExistsError:
                raise LibraryLifecycleConflict("其它进程正在构建、索引或清理资料库，请等待结束。") from None
            with closing(self.connect()) as db, db:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT body FROM libraries WHERE id=?", (library_id,)).fetchone()
                if not row or json.loads(row[0]).get("deleted_at") is None:
                    raise ValueError("资料库状态已变化；仅已软删除的库可彻底删除。")
                active = db.execute("SELECT id FROM builds WHERE json_extract(body,'$.status') IN ('queued','running','cancel_requested') LIMIT 1").fetchone()
                if active:
                    raise LibraryLifecycleConflict(f"存在进行中的构建或索引任务，请先中断并等待结束后再彻底删除（任务 {active[0]}）。")
                library = self._library(json.loads(row[0]))
                plan = self._purge_file_plan(db, library, on_progress=progress)
                remaining = [row[0] for row in db.execute("SELECT citation_id FROM active WHERE library_id<>?", (library_id,))]
                generation = digest(sorted(remaining))[:24]
                from .library_index_purge import purge_library_indexes
                progress(stage="indexes", phase=None, completed=None, total=None, unit="generations")
                indexes = purge_library_indexes(self.root / "search", library_id, generation=generation, on_progress=progress)
                counts = {"cache_directories": 0, "files": 0, "file_bytes": 0, "blobs": 0,
                          "shared_blobs_retained": plan["shared_blobs"], "shared_build_records_updated": 0}
                progress(stage="files", completed=0, total=None, unit="files")
                inventory = plan["inventory"]
                progress(total=sum(item[0] for item in inventory.values()))
                for path, (files, size) in inventory.items():
                    if path.is_dir():
                        owner = path / "library-owner.json"
                        if not owner.is_file():
                            relative, fingerprint = plan["artifact_identities"][path.name]
                            self._write_json(owner, {"schema": "library-artifact-owner/v1", "library_id": library_id,
                                                     "relative_path": relative, "content_hash": fingerprint})
                        # rmtree may remove metadata before a locked payload.
                        # Keep the proven root owner until every child is gone,
                        # including when adopting an old markerless cache.
                        for child in path.iterdir():
                            if child != owner:
                                shutil.rmtree(child) if child.is_dir() else child.unlink()
                        owner.unlink()
                        path.rmdir()
                        counts["cache_directories"] += 1
                    else:
                        path.unlink()
                        counts["blobs"] += int(path in plan["blobs"])
                    counts["files"] += files
                    counts["file_bytes"] += size
                    progress(completed=counts["files"])
                progress(stage="catalog", completed=None, total=None, unit=None)
                for row in db.execute("SELECT id,body FROM builds WHERE json_extract(body,'$.library_id') IS NULL").fetchall():
                    job = json.loads(row[1])
                    prior = row[1]
                    self._purge_shared_job(job, library_id, plan["revision_ids"])
                    updated = encoded(job)
                    if updated != prior:
                        db.execute("UPDATE builds SET body=? WHERE id=?", (updated, row[0]))
                        counts["shared_build_records_updated"] += 1
                counts["case_members"] = db.execute("DELETE FROM case_members WHERE citation_id IN (SELECT id FROM revisions WHERE library_id=?)", (library_id,)).rowcount
                counts["active"] = db.execute("DELETE FROM active WHERE library_id=?", (library_id,)).rowcount
                # SQLite INSTEAD OF view triggers report zero direct changes.
                counts["revisions"] = db.execute("SELECT COUNT(*) FROM revisions WHERE library_id=?", (library_id,)).fetchone()[0]
                db.execute("DELETE FROM revisions WHERE library_id=?", (library_id,))
                counts["builds"] = db.execute("DELETE FROM builds WHERE json_extract(body,'$.library_id')=?", (library_id,)).rowcount
                db.execute('DELETE FROM tutorial_associations WHERE source_library_id=? OR target_library_id=?', (library_id, library_id))
                db.execute('DELETE FROM tutorial_document_sources WHERE owner_library_id=?', (library_id,))
                db.execute('DELETE FROM tutorial_model_documents WHERE owner_library_id=?', (library_id,))
                db.execute('DELETE FROM tutorial_retired_sources WHERE owner_library_id=?', (library_id,))
                from .tutorial_aliases import purge_aliases
                counts['tutorial_aliases'] = purge_aliases(db, library_id)
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='compact_storage_bindings'").fetchone():
                    counts['compact_storage_bindings'] = 0
                    for artifact in plan['artifacts']:
                        prefix = artifact.relative_to(self.root).as_posix() + '/'
                        counts['compact_storage_bindings'] += db.execute(
                            'DELETE FROM compact_storage_bindings WHERE relative_path>=? AND relative_path<?',
                            (prefix, prefix + '\uffff')).rowcount
                db.execute("DELETE FROM libraries WHERE id=?", (library_id,))
            warnings = list(indexes.get("warnings", []))
            if plan["unknown"]:
                warnings.append(f"保留 {plan['unknown']} 个无法确定归属的旧提取缓存目录（可能属于其它库）；未猜测删除。")
            warnings.append("原始资料目录、其它独立语料库中的同源副本、既有方案/导出文件及共享构建日志中的自由文本未清除。")
            compacted = False
            progress(stage="compacting", completed=None, total=None, unit=None)
            try:
                with closing(self.connect()) as db:
                    # Readers can delay optional space reclamation; do not spend
                    # two default 30s busy waits after deletion already committed.
                    db.execute("PRAGMA busy_timeout=1000")
                    checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if not checkpoint or checkpoint[0] == 0:
                        db.execute("VACUUM")
                        result = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                        compacted = not result or result[0] == 0
            except sqlite3.Error:
                pass
            if not compacted:
                warnings.append("登记数据已删除；数据库压缩/日志回收未完成，关闭其它读取进程后可另行压缩；不承诺安全擦除。")
            from .evaluation_sets import refresh_registry
            if (self.root / "evaluation-sets.json").is_file():
                progress(stage="evaluation_holdout", completed=None, total=None, unit="revisions")
                refresh_registry(self.root, on_progress=lambda update: progress(completed=update["scanned_revisions"]))
            progress(status="completed", stage="finished", completed=counts["files"], total=counts["files"], unit="files")
            return {"library_id": library_id, "status": "purged", "counts": counts, "indexes": indexes,
                    "catalog_compacted": compacted, "remaining_unattributed": plan["unknown"], "warnings": warnings,
                    "original_sources": "untouched", "secure_erasure": False}
        except Exception as exc:
            progress(status="failed", stage="finished", error=f"{type(exc).__name__}: {str(exc)[:500]}")
            raise
        finally:
            if owned:
                try:
                    if json.loads(lease.read_text(encoding="utf-8")).get("token") == token:
                        lease.unlink()
                except (OSError, ValueError):
                    pass
            self._build_lock.release()

    def _buildable_library(self, library_id):
        library = self.library(library_id)
        if not library["enabled"]:
            raise ValueError("资料库已关闭，请先启用后再构建。")
        return library

    @staticmethod
    def _require_rebind_idle(db, library_id):
        for row in db.execute("SELECT body FROM builds WHERE json_extract(body,'$.status') IN ('queued','running','cancel_requested')"):
            job = json.loads(row[0])
            if (job.get("library_id") == library_id or library_id in (job.get("library_ids") or [])
                    or job.get("scope") == "all_libraries" or job.get("kind") == "reindex" and not job.get("library_ids")):
                raise LibraryLifecycleConflict("该资料库存在进行中的构建或索引任务，请先中断并等待结束后重新关联目录。")

    def rebind(self, library_id, root_id, path):
        library = self.library(library_id)
        self.jobs_summary()  # Recover dead owners before checking active work.
        with closing(self.connect()) as db:
            self._require_rebind_idle(db, library_id)
        target = Path(path).expanduser().resolve()
        if not target.is_dir():
            raise ValueError("新目录不存在，请检查路径 (Replacement root does not exist)")
        root = next((r for r in library["roots"] if r["id"] == root_id), None)
        if root is None:
            raise ValueError("Unknown root ID")
        # Verify registered sources before changing the local locator.
        checked = 0
        seen = set()
        for obj in self.objects([library_id]):
            for evidence in obj.get("evidence", []):
                if evidence.get("root_id") != root_id:
                    continue
                key = (evidence["relative_path"], evidence["asset_hash"])
                if key in seen:
                    continue
                seen.add(key)
                candidate = (target / evidence["relative_path"]).resolve()
                if not candidate.is_relative_to(target) or not candidate.is_file():
                    raise ValueError(f"新目录缺少已入库文件 (Replacement root is missing an indexed source): {evidence['relative_path']}")
                if file_hash(candidate) != evidence["asset_hash"]:
                    raise ValueError(f"新目录中的文件内容不一致 (Replacement root contains different source content): {evidence['relative_path']}")
                checked += 1
        root["path"] = str(target)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._require_rebind_idle(db, library_id)
            latest = db.execute("SELECT body FROM libraries WHERE id=?", (library_id,)).fetchone()
            library = self._library(json.loads(latest[0])) if latest else None
            if library is None or library["deleted_at"] is not None:
                raise FileNotFoundError("Library was deleted while verifying replacement sources")
            root = next((r for r in library["roots"] if r["id"] == root_id), None)
            if root is None:
                raise ValueError("Unknown root ID")
            # Old manual indexes retain their original doc_root. Keep the
            # verified root aliases so closing this library still hides that
            # cached source after relocation, without rewriting its index.
            if root["path"] != str(target):
                root["previous_paths"] = [path for path in dict.fromkeys([*root.get("previous_paths", []), root["path"]]) if path != str(target)]
            root["path"] = str(target)
            library["rebound_at"] = now()
            db.execute("UPDATE libraries SET body=? WHERE id=?", (encoded(self._library(library, encode=True)), library_id))
        return dict(library, verified_evidence=checked)

    def model_candidates(self, library_id, limit=None):
        """List all MPH choices by default, without hashing/parsing content."""
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("Model candidate limit must be a positive integer or None")
        library = self.library(library_id)
        output = {"library_id": library_id, "files": [], "limit": limit, "has_more": False,
                  "missing_roots": [], "status": "not_applicable"}
        if library["type"] in MANUAL_TYPES:
            return output
        roots = [root for root in library["roots"] if root.get("role", "models") == "models"]
        online = []
        for root in roots:
            if Path(root["path"]).is_dir():
                online.append(root)
            else:
                output["missing_roots"].append({"root_id": root["id"], "path": root["path"]})
        files, seen = [], set()

        def append(root, relative):
            directory = Path(root["path"])
            path = (directory / relative).resolve()
            if path in seen or not path.is_relative_to(directory) or path.suffix.lower() != ".mph" or not path.is_file():
                return
            seen.add(path)
            files.append({"path": str(path), "relative_path": path.relative_to(directory).as_posix(), "root_id": root["id"]})

        # Directory scope is authoritative. Do not parse model_case payloads or
        # prioritize old indexed selections over newly added source files.
        if limit is None or len(files) <= limit:
            for root in online:
                def unavailable(error):
                    if not any(item["root_id"] == root["id"] for item in output["missing_roots"]):
                        output["missing_roots"].append({"root_id": root["id"], "path": root["path"], "reason": "directory_enumeration_failed"})

                for directory, directories, names in os.walk(root["path"], followlinks=False, onerror=unavailable):
                    check_cancelled()
                    directories.sort()
                    for name in sorted(names):
                        if name.lower().endswith(".mph"):
                            append(root, (Path(directory) / name).relative_to(root["path"]))
                        if limit is not None and len(files) > limit:
                            break
                    if limit is not None and len(files) > limit:
                        break
                if limit is not None and len(files) > limit:
                    break
        output.update(files=files if limit is None else files[:limit], has_more=limit is not None and len(files) > limit,
                      status="offline" if roots and not online else "partial" if output["missing_roots"] else "ready" if files else "empty")
        return output

    def locate(self, library, path):
        path = self.paths.resolve(path)
        candidates = [r for r in library["roots"] if path.is_relative_to(Path(r["path"]))]
        if not candidates:
            raise ValueError(f"Source is outside registered directories: {path}")
        root = max(candidates, key=lambda r: len(r["path"]))
        return root, path.relative_to(Path(root["path"])).as_posix()

    def scan(self, library_id, paths=None, limit=None, on_progress=None):
        library = self.library(library_id)
        if paths is None:
            extensions = {".html", ".htm", ".md", ".txt", ".pdf"} if library["type"] in MANUAL_TYPES else {".mph"}
            files = []
            for root in library["roots"]:
                if root["role"] in {"docs", "attachments"} and library["type"] not in MANUAL_TYPES:
                    continue
                directory = Path(root["path"])
                if not directory.is_dir():
                    raise ValueError("Registered directory is offline; no deletion will be inferred")
                for path in directory.rglob("*"):
                    check_cancelled()
                    if path.is_file() and path.suffix.lower() in extensions:
                        files.append(path)
            files = sorted(set(files))
        else:
            files = [self.paths.resolve(p) for p in paths]
        if library["type"] in MANUAL_TYPES:
            from .tutorial_sources import choose_companion_html
            chosen = set()
            for path in files:
                check_cancelled()
                if path.suffix.lower() == ".pdf":
                    boundary, _ = self.locate(library, path)
                    html = choose_companion_html(path, root=boundary["path"])
                    if html is not None:
                        self.locate(library, html)
                        chosen.add(html)
                        continue
                chosen.add(path)
            files = sorted(chosen)
        if limit is not None:
            files = files[:max(0, int(limit))]
        records = []
        for index, path in enumerate(files):
            check_cancelled()
            root, relpath = self.locate(library, path)
            if not path.is_file():
                raise FileNotFoundError(str(path))
            if on_progress:
                on_progress({"completed": index, "total": len(files), "unit": "files", "current_file": relpath,
                             "detail": "Fingerprinting source files"})
            records.append({"root_id": root["id"], "relative_path": relpath,
                            "path": str(path), "size": path.stat().st_size,
                            "content_hash": file_hash(path)})
            if on_progress:
                on_progress({"completed": index + 1, "total": len(files), "unit": "files", "current_file": relpath,
                             "detail": "Fingerprinting source files"})
        return records

    @staticmethod
    def _write_json(path, value):
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(encoded(value), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _evidence_hash(path, hashes=None):
        # A construction pass may create hundreds of feature objects sharing a
        # snapshot. Reuse only while that exact file's stat identity is stable.
        before = path.stat()
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        cached = (hashes or {}).get(path)
        if cached and cached[0] == identity:
            return cached[1]
        fingerprint = file_hash(path)
        after = path.stat()
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Evidence changed while it was being archived")
        if hashes is not None:
            hashes[path] = (identity, fingerprint)
        return fingerprint

    def _archive_blob(self, path, fingerprint, hashes=None):
        saved = self.root / "blobs" / (fingerprint + path.suffix)
        saved.parent.mkdir(parents=True, exist_ok=True)
        if saved.is_symlink() or not saved.parent.resolve().is_relative_to(self.root):
            raise ValueError("Derived evidence blob escapes the controlled catalog")
        # Stage inside the owned artifact directory, so even an abrupt process
        # exit leaves temporary payloads covered by that directory's purge.
        temporary = path.with_name(path.name + ".archive.tmp")
        temporary.unlink(missing_ok=True)
        if saved.exists():
            if self._evidence_hash(saved, hashes) != fingerprint:
                raise ValueError("Archived evidence blob hash mismatch; existing evidence was preserved")
            return saved
        try:
            with path.open("rb") as source, temporary.open("xb") as target:
                shutil.copyfileobj(source, target)
            if self._evidence_hash(temporary, hashes) != fingerprint:
                raise ValueError("Derived evidence changed while being copied")
            os.replace(temporary, saved)
        finally:
            temporary.unlink(missing_ok=True)
        return saved

    def _artifact_entries(self, candidate, artifact_dir):
        entries = candidate.get("artifact_blobs", [])
        if not isinstance(entries, list):
            raise ValueError("Invalid extraction artifact redirects")
        result = []
        for entry in entries:
            if not isinstance(entry, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256", ""))):
                raise ValueError("Invalid extraction artifact redirect")
            relative = Path(entry.get("path", ""))
            work = (artifact_dir / relative).resolve()
            blob = self.paths.resolve(entry.get("blob", ""))
            if (relative.is_absolute() or not relative.parts or not work.is_relative_to(artifact_dir)
                    or not blob.is_relative_to(self.root / "blobs") or blob.name[:64] != entry["sha256"]):
                raise ValueError("Extraction artifact redirect escapes its controlled directory")
            result.append((entry, work, blob))
        return result

    def _restore_artifacts(self, cache, artifact_dir):
        """Give an invalidated extractor separate writable working copies.

        The canonical evidence is never hardlinked or supplied as an output
        filename. API manifests stay beside these lazily restored payloads.
        """
        if not cache.is_file():
            return
        candidate = json.loads(cache.read_text(encoding="utf-8"))
        manifest = artifact_dir / "artifact-blobs.json"
        if manifest.is_file():
            candidate = json.loads(manifest.read_text(encoding="utf-8"))
        source_roots = [Path(root["path"]) for library in self.libraries(include_deleted=True) for root in library["roots"]]
        for entry, work, blob in self._artifact_entries(candidate, artifact_dir):
            if any(work.is_relative_to(root) for root in source_roots):
                raise ValueError("Extraction output overlaps a registered source directory")
            check_cancelled()
            if work.exists():
                continue
            if not blob.is_file() or file_hash(blob) != entry["sha256"]:
                raise ValueError("Archived extraction dependency hash mismatch")
            work.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(blob, work)

    def _archive_extraction(self, cache, artifact_dir, result, hashes):
        """Persist canonical cache references before removing duplicate bytes."""
        candidate = json.loads(cache.read_text(encoding="utf-8"))
        entries = {entry["path"]: entry for entry, _, _ in self._artifact_entries(candidate, artifact_dir)}
        manifest = artifact_dir / "artifact-blobs.json"
        previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
        retained = {item["blob"]: item for item in previous.get("retained_blobs", [])}
        for entry in entries.values():
            retained[entry["blob"]] = {"blob": entry["blob"], "sha256": entry["sha256"]}
        if previous:
            entries.update((entry["path"], entry) for entry, _, _ in self._artifact_entries(previous, artifact_dir))
        for entry in entries.values():
            retained[entry["blob"]] = {"blob": entry["blob"], "sha256": entry["sha256"]}
        replacements, copies = {}, []
        source_roots = [Path(root["path"]) for library in self.libraries(include_deleted=True) for root in library["roots"]]

        def discover(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"path", "asset_path"} and isinstance(child, str):
                        try:
                            path = self.paths.resolve(child)
                        except (OSError, ValueError):
                            continue
                        if path.is_relative_to(artifact_dir) and path not in replacements and path.is_file():
                            # Registered sources remain read-only, even if a
                            # caller has bound a source root inside artifacts.
                            if any(path.is_relative_to(root) for root in source_roots):
                                raise ValueError("Extraction output overlaps a registered source directory")
                            fingerprint = self._evidence_hash(path, hashes)
                            saved = self.root / "blobs" / (fingerprint + path.suffix)
                            if any(saved.is_relative_to(root) for root in source_roots):
                                raise ValueError("Evidence archive overlaps a registered source directory")
                            copies.append((path, fingerprint))
                            relative = path.relative_to(artifact_dir).as_posix()
                            entries[relative] = {"path": relative, "blob": self.paths.encode(saved), "sha256": fingerprint}
                            replacements[path] = self.paths.encode(saved)
                    else:
                        discover(child)
            elif isinstance(value, list):
                for child in value:
                    discover(child)

        discover(result)
        if not entries:
            return result
        for entry in entries.values():
            retained[entry["blob"]] = {"blob": entry["blob"], "sha256": entry["sha256"]}
        redirects = {"schema": "library-artifact-blobs/v1", "artifact_blobs": list(entries.values()),
                     "retained_blobs": list(retained.values())}
        # Journal ownership before creating blobs. A crash during copy/cache
        # publication still leaves a small, purgeable reference for every blob.
        if not manifest.is_file() or json.loads(manifest.read_text(encoding="utf-8")) != redirects:
            self._write_json(manifest, redirects)
        for path, fingerprint in copies:
            check_cancelled()
            self._archive_blob(path, fingerprint, hashes)

        def rewrite(value):
            if isinstance(value, dict):
                result = {}
                for key, child in value.items():
                    if key in {"path", "asset_path"} and isinstance(child, str):
                        try:
                            child = replacements.get(self.paths.resolve(child), child)
                        except (OSError, ValueError):
                            pass
                    else:
                        child = rewrite(child)
                    result[key] = child
                return result
            return [rewrite(child) for child in value] if isinstance(value, list) else value

        result = rewrite(result)
        dependencies = {self.paths.resolve(item["path"]): item["hash"] for item in candidate.get("dependencies", [])}
        for path, target in replacements.items():
            dependencies.pop(path, None)
            blob = self.paths.resolve(target)
            dependencies[blob] = self._evidence_hash(blob, hashes)
        updated = dict(candidate, result=result, artifact_blobs=list(entries.values()),
                       dependencies=[{"path": self.paths.encode(path), "hash": fingerprint} for path, fingerprint in sorted(dependencies.items())])
        # Preserve valid cache bytes during relocation, including legacy absolute
        # extractor/source locators. Immutable citation payloads stay identical.
        if replacements or candidate.get("artifact_blobs") != updated["artifact_blobs"]:
            self._write_json(cache, updated)
        for entry, work, blob in self._artifact_entries(updated, artifact_dir):
            check_cancelled()
            if any(work.is_relative_to(root) for root in source_roots):
                raise ValueError("Extraction output overlaps a registered source directory")
            if work.is_file() and file_hash(work) == entry["sha256"]:
                work.unlink()
            work.with_name(work.name + ".archive.tmp").unlink(missing_ok=True)
        return result

    def _prepare(self, library, record, source, *, evidence_hashes=None):
        local_id = str(source["local_id"])
        logical = "lo-" + digest([library["id"], record["root_id"], record["relative_path"], local_id])[:24]
        evidence = []
        for item in source.get("evidence", []):
            check_cancelled()
            item = dict(item)
            path = self.paths.resolve(item.pop("asset_path", record["path"]))
            asset_hash = self._evidence_hash(path, evidence_hashes)
            if path.is_relative_to(self.root / "blobs"):
                if path.name[:64] != asset_hash:
                    raise ValueError("Archived evidence blob hash mismatch")
                root, relative = {"id": "derived"}, path.relative_to(self.root).as_posix()
            elif path.is_relative_to(self.root / "artifacts"):
                saved = self._archive_blob(path, asset_hash, evidence_hashes)
                root, relative = {"id": "derived"}, saved.relative_to(self.root).as_posix()
            else:
                root, relative = self.locate(library, path)
            evidence.append(dict(item, root_id=root["id"], relative_path=relative, asset_hash=asset_hash))
        if not evidence:
            raise ValueError(f"Object {local_id} has no source evidence")
        payload = {"schema": SCHEMA, "object_id": logical, "local_id": local_id,
                   "library_id": library["id"], "origin": library["type"],
                   "version": library["version"], "type": source["type"],
                   "title": source["title"], "text": source.get("text", ""),
                   "kind": source.get("kind", "case"), "data": source.get("data", {}),
                   "relations": source.get("relations", []), "evidence": evidence,
                   "asset_key": record["root_id"] + ":" + record["relative_path"],
                   "asset_hash": record["content_hash"], "coverage": source.get("coverage", {}),
                   "verification": {"numerical": "not_run", "original_files": "read_only"}}
        if isinstance(source.get("data", {}).get("compact_ref"), dict):
            payload.pop("coverage", None)
            payload["coverage_ref"] = "model-case"
        revision = digest(payload)
        citation = logical + "-" + revision[:12]
        return dict(payload, id=citation, revision_id=revision, source_uri=f"library://{library['id']}/{citation}",
                    url=f"library://{library['id']}/{citation}", section_path=[library["name"], source["title"]],
                    branch=library["type"], package=library["id"], physics=[], parent=None,
                    text_truncated=False, read_scope="complete_knowledge_object")

    def _cached_extraction(self, cache, library, deep, parser_hash):
        """Validate locators at their new location, retaining observed JSON bytes.

        Extractor source is a declared dependency, not an external source root.
        Its content hash permits using the identical installed extractor after
        the application package itself moves to a different machine/path.
        """
        candidate = json.loads(cache.read_text(encoding="utf-8"))
        if (candidate.get("deep") != deep or candidate.get("parser_hash") != parser_hash
                or self._roots(candidate.get("root_bindings", [])) != library["roots"]):
            return None
        from .library_model import JAVA_SOURCE
        known_extractors = {JAVA_SOURCE.name: JAVA_SOURCE,
                            "case_structure.py": Path(__file__).with_name("case_structure.py"),
                            "case_metadata.py": Path(__file__).with_name("case_metadata.py"),
                            "tutorial_sources.py": Path(__file__).with_name("tutorial_sources.py"),
                            "mph_saved_values.py": Path(__file__).with_name("mph_saved_values.py"),
                            "mph_saved_selections.py": Path(__file__).with_name("mph_saved_selections.py")}
        declared = {d["path"]: d.get("sha256") for d in candidate.get("result", {}).get("extractor_dependencies", [])}
        for dependency in candidate.get("dependencies", []):
            dep = self.paths.resolve(dependency["path"])
            if dependency["path"] in declared and declared[dependency["path"]] == dependency["hash"]:
                installed = known_extractors.get(Path(dependency["path"]).name)
                if installed and installed.is_file() and file_hash(installed) == dependency["hash"]:
                    continue
            if not dep.is_file() or file_hash(dep) != dependency["hash"]:
                return None
        return candidate.get("result")

    def objects(self, library_ids=None):
        if library_ids == []:
            return []
        with closing(self.connect()) as db:
            scope = "" if library_ids is None else " WHERE a.library_id IN (" + ",".join("?" for _ in library_ids) + ")"
            rows = db.execute("SELECT r.payload FROM active a JOIN revisions r ON r.id=a.citation_id" + scope + " ORDER BY a.rowid",
                              () if library_ids is None else tuple(library_ids)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def object_stream(self):
        """Open an indexing snapshot while the caller owns the build lease."""
        return _CatalogObjectStream(self.db_path)

    def has_objects(self, library_ids=None):
        if library_ids == []:
            return False
        with closing(self.connect()) as db:
            scope = "" if library_ids is None else " WHERE a.library_id IN (" + ",".join("?" for _ in library_ids) + ")"
            return db.execute("SELECT 1 FROM active a JOIN revisions r ON r.id=a.citation_id" + scope + " LIMIT 1",
                              () if library_ids is None else tuple(library_ids)).fetchone() is not None

    def current_generation(self):
        from .catalog_generation import cached_generation
        with closing(self.connect()) as db:
            return cached_generation(db, _ordered_identity_hash)

    def index_status(self, *, summary=False):
        from .library_search import LibrarySearch
        search = LibrarySearch(self.root / "search")
        state = search.status(summary=True) if summary else search.status()
        desired = self.current_generation()
        state["desired_generation"] = desired
        if state.get("generation") and state["generation"] != desired:
            for mode in state.get("modes", {}).values():
                if mode.get("state") == "disabled":
                    continue
                mode.update(ready=False, state="stale", error="Library objects changed; rebuild the retrieval index")
            state["ready"] = False
        return state

    def index_status_summary(self):
        return self.index_status(summary=True)

    def get(self, citation_id):
        with closing(self.connect()) as db:
            from .tutorial_aliases import lookup_alias, resolve_alias_source
            alias = lookup_alias(db, citation_id)
            row = db.execute("SELECT payload FROM revisions WHERE id=?", (citation_id,)).fetchone()
        if alias:
            return resolve_alias_source(self, alias)
        return json.loads(row[0]) if row else None

    def require_readable_library(self, library_id):
        if library_id not in self.searchable_library_ids([library_id]):
            raise PermissionError("资料库已关闭或删除，不能新读取该来源。")

    def evaluation_policy(self):
        from .evaluation_sets import exclusion_snapshot
        return exclusion_snapshot(self.root)

    def evaluation_set_summaries(self):
        from .evaluation_sets import evaluation_set_summaries
        return evaluation_set_summaries(self.root)

    def set_evaluation_retrieval_enabled(self, enabled):
        from .evaluation_sets import set_retrieval_enabled
        return set_retrieval_enabled(self.root, enabled)

    def require_not_heldout(self, citation_id, source=None):
        """Public retrieval gate, independent of Agent-editable library flags."""
        policy = self.evaluation_policy()
        if policy.blocks_identity(citation_id) or source is not None and policy.blocks_object(source):
            raise PermissionError("该来源属于管理员保留的测试集，Agent 不能检索或读取。")

    def source_available(self, citation_id):
        """Check current source ownership without decoding the document body."""
        policy = self.evaluation_policy()
        if policy.blocks_identity(citation_id):
            return False
        with closing(self.connect()) as db:
            from .tutorial_aliases import lookup_alias, alias_available
            alias = lookup_alias(db, citation_id)
            owner = db.execute("SELECT library_id,object_id FROM revisions WHERE id=?", (citation_id,)).fetchone()
            from .tutorial_catalog import retired_targets
            targets = retired_targets(db, citation_id) if owner else None
        if alias:
            return not policy.blocks_identity(alias["target_id"]) and alias_available(self, alias)
        visible = set(self.searchable_library_ids())
        return bool(owner and not policy.blocks_identity(citation_id, owner[1])
                    and owner[0] in visible and (targets is None or targets & visible))

    def read(self, citation_id):
        """Fresh public reads obey lifecycle; get() retains internal archives."""
        self.require_not_heldout(citation_id)
        with closing(self.connect()) as db:
            from .tutorial_aliases import lookup_alias, resolve_alias_source
            alias = lookup_alias(db, citation_id)
            owner = db.execute("SELECT library_id FROM revisions WHERE id=?", (citation_id,)).fetchone()
        if alias:
            source = resolve_alias_source(self, alias, reader=self.read)
            self.require_not_heldout(citation_id, source)
            return source
        if not owner:
            return None
        self.require_readable_library(owner[0])
        source = self.get(citation_id)
        if source is None:
            return None
        self.require_not_heldout(citation_id, source)
        if source.get("library_id") != owner[0]:
            raise ValueError("Source owner does not match its catalog record")
        from .tutorial_catalog import retired_targets
        with closing(self.connect()) as db:
            targets = retired_targets(db, citation_id)
        if targets is not None and not targets.intersection(self.searchable_library_ids()):
            raise PermissionError("教程的统一文档库已关闭，不能通过旧案例副本读取正文。")
        compact = self._compact_context(source)
        if compact is not None:
            from .compact_library import hydrate_source
            authority, source_override = compact
            source = hydrate_source(authority, source, library_root=self.root, source_override=source_override)
        self.require_readable_library(owner[0])
        if targets is not None and not targets.intersection(self.searchable_library_ids()):
            raise PermissionError("教程的统一文档库已关闭，不能通过旧案例副本读取正文。")
        if source['type'] == 'model_case':
            from .tutorial_catalog import links
            tutorial_links = [item for item in links(self, source, limit=40) if self.source_available(item['id'])]
            if tutorial_links or source.get('data', {}).get('tutorial_refs'):
                source = dict(source, data=dict(source.get('data') or {}))
                source['data']['tutorial_links'] = tutorial_links
        self.require_not_heldout(citation_id, source)
        return source

    def _compact_context(self, obj):
        """Resolve a child through its pinned immutable case, never latest state."""
        data = obj.get("data") or {}
        if not isinstance(data.get("compact_case"), dict) and not isinstance(data.get("compact_ref"), dict):
            return None
        authority = obj
        if not isinstance(data.get("compact_case"), dict):
            if not obj.get("case_group"):
                raise ValueError("Compact source has no pinned case group")
            with closing(self.connect()) as db:
                row = db.execute("SELECT r.payload FROM case_members m JOIN revisions r ON r.id=m.citation_id "
                                 "WHERE m.group_id=? AND m.local_id='model-case'", (obj["case_group"],)).fetchone()
            authority = json.loads(row[0]) if row else None
            if (not authority or not isinstance(authority.get("data", {}).get("compact_case"), dict)
                    or any(authority.get(key) != obj.get(key) for key in ("library_id", "asset_key", "asset_hash", "case_group"))):
                raise ValueError("Compact source authority is not pinned in this case revision")
        library = self.library(authority["library_id"], include_deleted=True)
        asset = authority.get("asset_key", "")
        root = next((root for root in sorted(library["roots"], key=lambda value: len(value["id"]), reverse=True)
                     if asset.startswith(root["id"] + ":")), None)
        if root is None:
            raise ValueError("Compact model source root is no longer registered")
        relative = asset[len(root["id"]) + 1:]
        boundary = Path(root["path"]).resolve()
        source_override = (boundary / relative).resolve()
        if not source_override.is_relative_to(boundary):
            raise ValueError("Compact original source escapes its registered directory")
        return authority, source_override

    def _pin_case_group(self, objects):
        """Resolve local relations within one immutable extraction, without hash cycles."""
        if not any(item.get("type") == "model_case" for item in objects):
            return objects
        if len({item["local_id"] for item in objects}) != len(objects):
            raise ValueError("Case extraction contains duplicate local object IDs")
        group = digest(sorted(item["id"] for item in objects))
        derived = {"id", "revision_id", "source_uri", "url", "section_path", "branch", "package", "physics", "parent", "text_truncated", "read_scope"}
        result = []
        for item in objects:
            item = dict(item, case_group=group)
            revision = digest({key: value for key, value in item.items() if key not in derived})
            citation = item["object_id"] + "-" + revision[:12]
            uri = f"library://{item['library_id']}/{citation}"
            result.append(dict(item, id=citation, revision_id=revision, source_uri=uri, url=uri))
        return result

    def related_sources(self, citation_id, *, limit=8):
        """Return references, never silently replace them with a newer case revision."""
        if type(limit) is not int or not 1 <= limit <= 40:
            raise ValueError("Related-source limit must be between 1 and 40")
        self.require_not_heldout(citation_id)

        def finish(items):
            visible = [item for item in items if not item.get('id') or self.source_available(item['id'])]
            self.require_not_heldout(citation_id)
            return visible

        with closing(self.connect()) as db:
            from .tutorial_aliases import lookup_alias, related_alias_sources
            alias = lookup_alias(db, citation_id)
        if alias:
            return finish(related_alias_sources(self, alias))
        source = self.get(citation_id)
        if not source:
            raise FileNotFoundError("Object not found")
        self.require_not_heldout(citation_id, source)
        self.require_readable_library(source["library_id"])
        from .tutorial_catalog import links, retired_targets
        tutorial_links = links(self, source, limit=limit)
        case_links, base_links = [], []
        with closing(self.connect()) as db:
            from .compact_api_overlay import pinned_base_authority
            authority = source
            if source.get("type") != "model_case" and source.get("case_group"):
                # Some thin API observations have no explicit same_case
                # relation. The existing immutable group, never active/latest,
                # supplies one overview reference without hydrating its body.
                row = db.execute("SELECT r.payload FROM case_members m JOIN revisions r ON r.id=m.citation_id "
                                 "WHERE m.group_id=? AND m.local_id='model-case'", (source["case_group"],)).fetchone()
                target = json.loads(row[0]) if row else None
                fields = ("library_id", "asset_key", "asset_hash", "case_group")
                if (target and target.get("type") == "model_case"
                        and tuple(target.get(key) for key in fields) == tuple(source.get(key) for key in fields)):
                    authority = target
                    case_links.append({"id": target["id"], "library_id": target["library_id"], "title": target["title"],
                                       "type": "model_case", "relation": "same_case", "status": "pinned_case_version"})
            base = pinned_base_authority(db, authority)
            if base:
                base_links.append({"id": base["id"], "library_id": base["library_id"], "title": base["title"], "type": "model_case",
                                   "relation": "saved_xml_base", "status": "pinned_saved_xml_version"})
                node_id = source.get("data", {}).get("compact_ref", {}).get("node_id")
                query = ("SELECT r.payload FROM case_members m JOIN revisions r ON r.id=m.citation_id "
                         "WHERE m.group_id=? AND json_extract(r.payload,'$.type')='saved_configuration'")
                parameters = [base["case_group"]]
                if node_id is not None:
                    query += " AND json_extract(r.payload,'$.data.compact_ref.node_id')=?"
                    parameters.append(node_id)
                for (raw,) in db.execute(query + " ORDER BY m.local_id LIMIT 2", parameters):
                    target = json.loads(raw)
                    if (target.get("library_id"), target.get("asset_key"), target.get("asset_hash")) == (
                            source.get("library_id"), source.get("asset_key"), source.get("asset_hash")):
                        base_links.append({"id": target["id"], "library_id": target["library_id"], "title": target["title"],
                                           "type": target["type"], "relation": "saved_xml_configuration",
                                           "status": "pinned_saved_xml_version", "feature_branch": object_feature_branch(target)})
        relations = list(source.get("relations", []))
        rank = {"requires": 0, "same_case": 1, "context": 2, "documented_by": 3, "follows_document_order": 4}
        relations.sort(key=lambda item: rank.get(item.get("type"), 4))
        output, seen = [*case_links, *base_links, *tutorial_links][:limit], set()
        if case_links:
            seen.add(("same_case", "model-case"))
        if len(output) >= limit:
            return finish(output)
        with closing(self.connect()) as db:
            from .tutorial_aliases import has_canonical_document_reference
            canonical_tutorial = has_canonical_document_reference(db, source)
            for relation in relations:
                local = relation.get("target_local_id")
                if not local or (relation.get("type"), local) in seen:
                    continue
                seen.add((relation.get("type"), local))
                row = db.execute("SELECT r.payload FROM case_members m JOIN revisions r ON r.id=m.citation_id WHERE m.group_id=? AND m.local_id=?",
                                 (source.get("case_group"), local)).fetchone() if source.get("case_group") else None
                target = json.loads(row[0]) if row else None
                if target is None and canonical_tutorial and relation.get('type') in {'context', 'documented_by', 'follows_document_order'}:
                    continue
                if target and retired_targets(db, target['id']) is not None:
                    continue
                if target and (target.get("library_id"), target.get("asset_key"), target.get("asset_hash"), target.get("case_group")) != (
                        source.get("library_id"), source.get("asset_key"), source.get("asset_hash"), source.get("case_group")):
                    target = None
                item = {"relation": relation.get("type"), "status": relation.get("status", "source_relation") if target else "unresolved"}
                if target:
                    item.update(id=target["id"], library_id=target["library_id"], title=target["title"], type=target["type"], feature_branch=object_feature_branch(target))
                else:
                    item.update(title=local, reason="Referenced object is not pinned in this case revision")
                if relation.get("alignment"):
                    item["alignment"] = relation["alignment"]
                output.append(item)
                if len(output) >= limit:
                    return finish(output)
        for reference in source.get("data", {}).get("references", []):
            if isinstance(reference, dict) and reference.get("status") == "unresolved":
                output.append({"relation": "requires", "status": "unresolved", "title": str(reference.get("path") or reference.get("description") or "Unknown prerequisite")[:300]})
                if len(output) >= limit:
                    break
        return finish(output)

    def configuration(self, citation_id, *, category=None, node_path=None, limit=20, source="api"):
        source = "xml" if source == "saved" else source
        if source not in {"xml", "api"}:
            raise ValueError("source must be saved/xml or api")
        # This read needs the exact saved authority, not a hydrated overview or
        # its tutorial navigation. Gate the archive before loading settings.
        self.require_not_heldout(citation_id)
        obj = self.get(citation_id)
        if obj is None:
            raise FileNotFoundError("Object not found")
        self.require_readable_library(obj["library_id"])
        self.require_not_heldout(citation_id, obj)
        if obj["type"] != "model_case":
            raise ValueError("Configuration reads require an exact model_case citation ID")
        compact = self._compact_context(obj)
        if compact is not None:
            from .compact_library import configuration
            authority, source_override = compact
            result = configuration(authority, library_root=self.root, source_override=source_override,
                                   category=category, node_path=node_path, limit=limit, source=source)
            self.require_readable_library(obj["library_id"])
            self.require_not_heldout(citation_id, obj)
            return result
        method = "comsol_standalone_api_snapshot" if source == "api" else "native_mph_xml_snapshot"
        evidence = next((e for e in obj["evidence"] if e.get("method") == method), None)
        if evidence is None:
            raise ValueError(f"This case has no {source} configuration snapshot")
        if evidence["root_id"] != "derived":
            raise ValueError("Configuration must come from a registered immutable artifact")
        artifact = (self.root / evidence["relative_path"]).resolve()
        if not artifact.is_relative_to(self.root / "blobs") or file_hash(artifact) != evidence["asset_hash"]:
            raise ValueError("Configuration artifact failed source hash validation")
        snapshot = json.loads(artifact.read_text(encoding="utf-8"))
        nodes = [n for n in snapshot.get("nodes", []) if (not category or category in {n.get("category"), n.get("node_kind")})
                 and (not node_path or node_path in str(n.get("path", n.get("node_path", ""))))]
        bound = max(1, min(100, int(limit)))
        extraction_coverage = dict(obj.get("coverage", {}).get("deep_api" if source == "api" else "raw_xml", {}))
        if source == "api":
            extraction_coverage.update(read_contract=snapshot.get("read_contract", "legacy_unknown"),
                                       snapshot_read_errors=snapshot.get("read_errors"),
                                       read_coverage=snapshot.get("read_coverage", {}),
                                       truncation_reasons=snapshot.get("truncation_reasons", []))
            if snapshot.get("read_errors") or snapshot.get("truncated"):
                extraction_coverage["status"] = "partial"
        self.require_readable_library(obj["library_id"])
        self.require_not_heldout(citation_id, obj)
        return {"id": citation_id, "revision_id": obj["revision_id"], "source": source, "evidence": evidence,
                "total_matches": len(nodes), "returned": min(bound, len(nodes)), "truncated": len(nodes) > bound,
                "nodes": nodes[:bound], "extraction_coverage": extraction_coverage,
                "physical_validation": "not_run", "effective_activation": "not_evaluated"}

    def read_evidence(self, citation_id, *, index=0, line=None, limit=120):
        self.require_not_heldout(citation_id)
        with closing(self.connect()) as db:
            from .tutorial_aliases import lookup_alias, read_alias_evidence
            alias = lookup_alias(db, citation_id)
        if alias:
            result = read_alias_evidence(self, alias, index=index, line=line, limit=limit)
            self.require_not_heldout(citation_id)
            return result
        obj = self.read(citation_id)
        if obj is None:
            raise FileNotFoundError("Object not found")
        compact = self._compact_context(obj)
        if compact is not None:
            from .compact_library import read_evidence
            authority, source_override = compact
            extra = {}
            if "passage_id" in obj.get("data", {}).get("compact_ref", {}):
                try:
                    evidence = obj["evidence"][int(index)]
                    library = self.library(obj["library_id"], include_deleted=True)
                    root = next(root for root in library["roots"] if root["id"] == evidence["root_id"])
                    boundary = Path(root["path"]).resolve()
                    evidence_path = (boundary / evidence["relative_path"]).resolve()
                except (IndexError, KeyError, ValueError, TypeError, StopIteration):
                    raise ValueError("Unknown compact document evidence locator") from None
                if not evidence_path.is_relative_to(boundary):
                    raise ValueError("Compact document evidence escapes its registered directory")
                extra["evidence_path"] = evidence_path
            result = read_evidence(authority, obj, library_root=self.root, source_override=source_override,
                                   index=index, line=line, limit=limit, **extra)
            self.require_readable_library(obj["library_id"])
            if not self.source_available(citation_id):
                raise PermissionError("教程的统一文档库已关闭，不能通过旧案例副本读取正文。")
            return result
        try:
            evidence = obj["evidence"][int(index)]
        except (IndexError, ValueError, TypeError):
            raise ValueError("Unknown evidence index") from None
        if evidence["root_id"] == "derived":
            root = self.root
        else:
            library = self.library(obj["library_id"], include_deleted=True)
            root = Path(next(r["path"] for r in library["roots"] if r["id"] == evidence["root_id"]))
        path = (root / evidence["relative_path"]).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("Evidence locator is unavailable")
        if path.suffix.lower() not in {".java", ".json", ".xml", ".html", ".htm", ".md", ".txt", ".csv"}:
            raise ValueError("Binary evidence is not a text source; use the configuration reader for MPH")
        if path.stat().st_size > 32 * 1024 * 1024 or file_hash(path) != evidence["asset_hash"]:
            raise ValueError("Evidence source changed or exceeds the bounded text limit")
        import re
        start = re.match(r"lines:(\d+)-", str(evidence.get("locator", "")))
        first = max(1, int(line if line is not None else start[1] if start else 1))
        count = max(1, min(300, int(limit)))
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[first-1:first-1+count]
        text = "\n".join(selected)
        self.require_readable_library(obj["library_id"])
        if not self.source_available(citation_id):
            raise PermissionError("教程的统一文档库已关闭，不能通过旧案例副本读取正文。")
        return {"id": citation_id, "revision_id": obj["revision_id"], "evidence": evidence,
                "line_start": first, "line_end": first+len(selected)-1, "total_lines": len(lines),
                "text": text[:50000], "truncated": first-1+len(selected) < len(lines) or len(text) > 50000,
                "read_scope": "bounded_original_source_lines", "source_hash_verified": True}

    def _save_job(self, job):
        if "indexes" in job:
            from .build_history import compact_index_diagnostic
            job["indexes"] = compact_index_diagnostic(job["indexes"])
        job.setdefault("workspace_root", str(self.paths.root))
        job["updated_at"] = now()
        job["counts"] = job_counts(job)
        job["coverage_summary"] = job_coverage(job)
        for item in job.get("items", []):
            item["coverage_summary"] = extraction_coverage(item)
        job["partial_extractions"] = sum(partial_extraction(item) for item in job.get("items", []))
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT json_extract(body,'$.cancel_requested_at') FROM builds WHERE id=?", (job["id"],)).fetchone()
            if previous and previous[0]:
                job["cancel_requested_at"] = previous[0]
                if job.get("status") in {"queued", "running"}:
                    job["status"] = "cancel_requested"
                elif job.get("status") in {"completed", "partial"}:
                    # The cancel request won the DB race with final publication.
                    # Published artifacts remain valid; this task must not report
                    # success after its cancellation was acknowledged.
                    job["status"] = "cancelled"
                    job.setdefault("cancelled_stage", job.get("stage"))
                    job["progress"] = dict(job.get("progress", {}), current_file=None,
                                           detail="Build cancelled; previously committed results were retained")
            db.execute("INSERT OR REPLACE INTO builds VALUES(?,?)", (job["id"], encoded(job)))

    def _cancel_path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("Invalid build job ID")
        return self.root / "build-control" / (job_id + ".cancel")

    def cancel_build(self, job_id):
        """Request this worker's cancellation, including workers in another CLI.

        The marker is task-specific. Never signal the shared workbench PID.
        The worker alone publishes cancelled after its subprocesses have exited.
        """
        marker = self._cancel_path(job_id)
        # Refresh stale ownership before taking the cancellation write lock.
        self.job(job_id)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM builds WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise FileNotFoundError("Build not found")
            job = json.loads(row[0])
            if job.get("status") in ACTIVE_BUILDS:
                if not job.get("cancel_supported"):
                    raise ValueError("This build was started by an older constructor and cannot acknowledge cancellation")
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch(exist_ok=True)
                job.update(status="cancel_requested", cancel_requested_at=job.get("cancel_requested_at") or now(), updated_at=now())
                db.execute("UPDATE builds SET body=? WHERE id=?", (encoded(job), job_id))
        return self._job_view(job)

    def _job_view(self, job, *, summary=False):
        moved = self._owner_moved(job)
        if job.get("status") in ACTIVE_BUILDS and (moved or (job.get("pid") and not process_alive(job["pid"]))):
            if summary:
                # Exceptional stale-owner recovery must persist the full job,
                # never overwrite it with the projected UI view.
                self.job(job["id"])
                return self.job(job["id"], summary=True)
            job.update(status="interrupted", stage="finished", finished_at=now(),
                       error="Data directory moved; the prior worker is not attached here" if moved else
                             "Constructor process exited; rerun reuses successful extraction and annotation artifacts")
            self._save_job(job)
        # SQL has already bounded this view's item preview. Preserve aggregates
        # calculated from the full record instead of recounting eight items.
        if not summary or not job.get("items_truncated"):
            job["counts"] = job_counts(job)
            job["coverage_summary"] = job_coverage(job)
            job["partial_extractions"] = sum(partial_extraction(item) for item in job.get("items", []))
        for item in job.get("items", []):
            item["coverage_summary"] = extraction_coverage(item)
        job["cancellable"] = bool(job.get("cancel_supported") and job.get("status") in ACTIVE_BUILDS)
        if job.get("status") == "completed" and job.get("partial_extractions"):
            # Old jobs may have omitted deep_api=partial from their aggregate.
            job["status"] = "partial"
        try:
            end = datetime.fromisoformat(job.get("finished_at") or now())
            start = datetime.fromisoformat(job["started_at"])
            job["elapsed_seconds"] = round(max(0, (end - start).total_seconds()), 1)
        except (KeyError, TypeError, ValueError):
            job["elapsed_seconds"] = None
        return job

    def _owner_moved(self, record):
        owner = record.get("workspace_root")
        return Path(owner).resolve() != self.paths.root if owner else bool(self.paths.previous_roots)

    @staticmethod
    def _job_body_sql(summary):
        # SQLite JSON projection keeps multi-MB retrieval manifests out of the
        # Python/UI status path. Full diagnostics retain the original record.
        if not summary:
            return "body"
        count = "COALESCE(json_array_length(body,'$.items'),0)"
        tail = "json_extract(body," + ",".join(f"'$.items[#-{offset}]'" for offset in range(8, 0, -1)) + ")"
        pruned = ("json_remove(body, '$.indexes.mapping', '$.indexes.representation', "
                  "'$.indexes.last_build.mapping', '$.indexes.last_build.representation')")
        return (f"CASE WHEN {count}>8 THEN json_set(json_remove({pruned}, '$.items'), "
                f"'$.items', json({tail}), '$.items_total', {count}, '$.items_truncated', json('true')) "
                f"ELSE {pruned} END")

    def job(self, job_id, *, summary=False):
        with closing(self.connect()) as db:
            row = db.execute(f"SELECT {self._job_body_sql(summary)} FROM builds WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise FileNotFoundError("Build not found")
        result = self._job_view(json.loads(row[0]), summary=summary)
        return status_summary(result) if summary else result

    def jobs(self, *, summary=False):
        with closing(self.connect()) as db:
            body = self._job_body_sql(summary)
            active = db.execute(f"SELECT {body} FROM builds WHERE json_extract(body,'$.status') IN ('running','queued','cancel_requested') ORDER BY rowid DESC").fetchall()
            recent = db.execute(f"SELECT {body} FROM builds ORDER BY rowid DESC LIMIT 30").fetchall()
        found = {}
        for row in [*active, *recent]:
            job = json.loads(row[0])
            if job["id"] not in found:
                found[job["id"]] = self._job_view(job, summary=summary)
        result = list(found.values())
        return status_summary(result) if summary else result

    def jobs_summary(self):
        return self.jobs(summary=True)

    def job_summary(self, job_id):
        return self.job(job_id, summary=True)

    def build(self, library_id, *, paths=None, limit=None, deep=True, enrich=False,
              modes=None, job_id=None, model_format="legacy"):
        # Direct SDK callers retain legacy extraction compatibility. Production
        # UI/CLI entry points select compact explicitly for model libraries.
        if not isinstance(model_format, str) or model_format not in {"legacy", "compact"}:
            raise ValueError("model_format must be legacy or compact")
        if modes:
            from .query_terms import normalize_modes
            modes = normalize_modes(modes)
        if modes and "qmd_vector" in modes:
            from .retrieval_settings import require_enabled
            require_enabled(self.root)
        self._buildable_library(library_id)
        job_id = job_id or uuid.uuid4().hex
        with build_control(BuildControl(self._cancel_path(job_id))):
            return self._build(library_id, paths=paths, limit=limit, deep=deep, enrich=enrich,
                               modes=modes, job_id=job_id, model_format=model_format)

    def _build(self, library_id, *, paths, limit, deep, enrich, modes, job_id, model_format="legacy"):
        if not self._build_lock.acquire(blocking=False):
            if job_id:
                self._save_job({"id": job_id, "library_id": library_id, "status": "failed", "stage": "finished", "error": "A library build is already running"})
            raise RuntimeError("A library build is already running")
        job = {"id": job_id, "library_id": library_id, "stage": "scanning", "pid": os.getpid(), "cancel_supported": True,
               "status": "running", "started_at": now(), "completed": 0, "failed": [], "items": [],
               "progress": {"completed": None, "total": None, "unit": "files", "current_file": None, "detail": "Enumerating source files"}}
        self._save_job(job)
        lease = self.root / ".build.lock"
        owns_lease = False
        extraction_session = ExitStack()
        try:
            check_cancelled()
            try:
                old = json.loads(lease.read_text(encoding="utf-8")) if lease.is_file() else None
                if old and (self._owner_moved(old) or not process_alive(old.get("pid"))):
                    lease.unlink()
            except (ValueError, OSError):
                pass
            try:
                with lease.open("x", encoding="utf-8") as stream:
                    stream.write(encoded({"pid": os.getpid(), "job_id": job["id"], "workspace_root": str(self.paths.root)}))
                owns_lease = True
            except FileExistsError:
                raise RuntimeError("Another process is constructing this catalog") from None
            library = self._buildable_library(library_id)
            job["model_format"] = "text" if library["type"] in MANUAL_TYPES else model_format
            if library["type"] in MANUAL_TYPES:
                from .library_text import text_extraction_session
                extraction_session.enter_context(text_extraction_session())
            last_scan_update = 0.0
            last_checkpoint = time.monotonic()
            checkpoint_stage = job["stage"]

            def checkpoint():
                nonlocal last_checkpoint, checkpoint_stage
                check_cancelled()
                stamp = time.monotonic()
                # Small builds keep per-file progress. Large builds checkpoint
                # at most every five seconds, plus every stage transition, so
                # growing diagnostic arrays are not encoded twice per source.
                if job.get("total", 0) <= 100 or job["stage"] != checkpoint_stage or stamp - last_checkpoint >= 5:
                    self._save_job(job)
                    last_checkpoint, checkpoint_stage = stamp, job["stage"]

            def scan_progress(progress):
                nonlocal last_scan_update
                check_cancelled()
                stamp = time.monotonic()
                if stamp - last_scan_update >= 0.3 or progress["completed"] == progress["total"]:
                    job["progress"] = progress
                    self._save_job(job)
                    last_scan_update = stamp

            records = self.scan(library_id, paths, limit, on_progress=scan_progress)
            job["total"] = len(records)
            job["stage"] = "extracting"
            job["progress"] = {"completed": 0, "total": len(records), "unit": "files", "current_file": None, "detail": "Reading and structuring sources"}
            self._save_job(job)
            if not records:
                raise ValueError("No supported source files were selected")
            output = []
            alternate_assets = set()
            for record in records:
                check_cancelled()
                record_alternates = set()
                job["progress"] = {"completed": job["completed"], "total": len(records), "unit": "files",
                                   "current_file": record["relative_path"], "detail": "Reading and structuring this source"}
                checkpoint()
                try:
                    artifact_dir = self.root / "artifacts" / digest([library_id, record["relative_path"], record["content_hash"]])[:24]
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    owner = {"schema": "library-artifact-owner/v1", "library_id": library_id, "root_id": record["root_id"],
                             "relative_path": record["relative_path"], "content_hash": record["content_hash"]}
                    ownership = artifact_dir / "library-owner.json"
                    if ownership.is_file() and json.loads(ownership.read_text(encoding="utf-8")) != owner:
                        raise ValueError("Extraction cache ownership does not match this source")
                    if not ownership.exists():
                        ownership.write_text(encoded(owner), encoding="utf-8")
                    evidence_hashes = {}
                    if library["type"] in MANUAL_TYPES:
                        from .library_text import extract_text_objects
                        root, _ = self.locate(library, record["path"])
                        result = extract_text_objects(record["path"], root=root["path"])
                        for alternate in result.get('tutorial', {}).get('alternate_sources', []):
                            try:
                                alternate_root, alternate_relative = self.locate(library, alternate['asset_path'])
                            except ValueError:
                                continue
                            record_alternates.add(alternate_root['id'] + ':' + alternate_relative)
                    elif model_format == "compact":
                        from .compact_library import build_case
                        docs = next((r["path"] for r in library["roots"] if r["role"] == "docs"), None)
                        result = build_case(Path(record["path"]), docs_root=Path(docs) if docs else None,
                                            artifact_dir=artifact_dir, library_root=self.root, deep=deep)
                    else:
                        from .library_model import extract_case
                        docs = next((r["path"] for r in library["roots"] if r["role"] == "docs"), None)
                        cache = artifact_dir / "extraction.json"
                        parser_hash = file_hash(Path(__file__).with_name("library_model.py"))
                        if cache.is_file():
                            result = self._cached_extraction(cache, library, deep, parser_hash)
                        else:
                            result = None
                        if result is None:
                            self._restore_artifacts(cache, artifact_dir)
                            result = extract_case(Path(record["path"]), docs_root=Path(docs) if docs else artifact_dir / "no-documents-bound",
                                                  artifact_dir=artifact_dir, deep=deep)
                            dependencies = {str(Path(e["asset_path"]).resolve()) for obj in result["objects"] for e in obj.get("evidence", []) if e.get("asset_path")}
                            dependencies.update(str(Path(d["path"]).resolve()) for d in result.get("extractor_dependencies", []) if d.get("path"))
                            dependencies.update(str(Path(a["path"]).resolve()) for a in result.get("assets", []) if a.get("path"))
                            previous = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else {}
                            self._write_json(cache, {"deep": deep, "parser_hash": parser_hash, "root_bindings": self._roots(library["roots"], encode=True), "result": result,
                                                      "artifact_blobs": previous.get("artifact_blobs", []),
                                                      "dependencies": [{"path": self.paths.encode(p), "hash": self._evidence_hash(Path(p), evidence_hashes)} for p in sorted(dependencies)]})
                        result = self._archive_extraction(cache, artifact_dir, result, evidence_hashes)
                    if enrich:
                        check_cancelled()
                        job["progress"]["detail"] = "Running requested semantic annotation"
                        checkpoint()
                        from .library_enrichment import enrich_case
                        result = enrich_case(result, artifact_dir)
                    check_cancelled()
                    if file_hash(record["path"]) != record["content_hash"]:
                        raise ValueError("Source changed while it was being parsed")
                    prepared = []
                    for obj in (result["objects"] if library['type'] in MANUAL_TYPES else link_case_objects(result["objects"])):
                        candidate = dict(obj) if isinstance(obj.get("data", {}).get("compact_ref"), dict) else dict(
                            obj, coverage=obj.get("coverage") or result.get("coverage", {}))
                        if obj["type"] == "model_case":
                            candidate["evidence"] = list(obj.get("evidence", []))
                            for asset in result.get("assets", []):
                                if asset.get("role") == "associated_tutorial_html":
                                    candidate["evidence"].append({"asset_path": asset["path"], "locator": "document",
                                        "method": "associated_tutorial_not_verified_model_agreement", "status": "documented"})
                        prepared.append(self._prepare(library, record, candidate, evidence_hashes=evidence_hashes))
                    prepared = self._pin_case_group(prepared)
                    if not prepared:
                        raise ValueError("Parser returned no knowledge objects")
                    output.extend(prepared)
                    alternate_assets.update(record_alternates)
                    item = {"path": record["relative_path"], "status": "completed", "objects": len(prepared),
                                         "coverage": result.get("coverage", {}), "warnings": result.get("warnings", []),
                                         "enrichment": result.get("enrichment", {"status": "not_requested"})}
                    if partial_extraction(item) or item["enrichment"].get("status") == "failed":
                        item["status"] = "partial"
                    job["items"].append(item)
                except BuildCancelled:
                    raise
                except Exception as exc:
                    job["failed"].append({"path": record["relative_path"], "error": f"{type(exc).__name__}: {str(exc)[:700]}"})
                job["completed"] += 1
                job["progress"]["completed"] = job["completed"]
                checkpoint()
            check_cancelled()
            if not output:
                raise RuntimeError("No source could be constructed; previous library remains active")
            # Selected-file builds replace those assets only, retaining all others.
            replaced = {obj["asset_key"] for obj in output} | alternate_assets
            retained = [o for o in self.objects([library_id]) if o["asset_key"] not in replaced]
            own_objects = retained + output
            with closing(self.connect()) as db:
                other_ids = [row[0] for row in db.execute("SELECT citation_id FROM active WHERE library_id<>?", (library_id,))]
            generation = digest(sorted(other_ids + [o["id"] for o in own_objects]))[:24]
            job["stage"] = "archiving"
            job["progress"] = {"completed": 0, "total": len(output), "unit": "objects", "current_file": None,
                               "detail": "Saving immutable knowledge objects"}
            self._save_job(job)
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                latest = db.execute("SELECT body FROM libraries WHERE id=?", (library_id,)).fetchone()
                if not latest:
                    raise FileNotFoundError("Library not found at publication")
                library = self._library(json.loads(latest[0]))
                for obj in output:
                    check_cancelled()
                    db.execute("INSERT OR IGNORE INTO revisions VALUES(?,?,?,?)", (obj["id"], obj["object_id"], library_id, encoded(obj)))
                    if obj.get("case_group"):
                        db.execute("INSERT OR REPLACE INTO case_members VALUES(?,?,?)", (obj["case_group"], obj["local_id"], obj["id"]))
                from .tutorial_catalog import register_objects, refresh_links
                register_objects(db, output)
                db.execute("DELETE FROM active WHERE library_id=?", (library_id,))
                db.executemany("INSERT INTO active VALUES(?,?,?)", [(library_id, o["object_id"], o["id"]) for o in own_objects])
                refresh_links(db)
                library["generation"] = generation
                library["last_build_at"] = now()
                db.execute("UPDATE libraries SET body=? WHERE id=?", (encoded(self._library(library, encode=True)), library_id))
                check_cancelled()
            job["catalog_committed"] = True
            from .evaluation_sets import refresh_registry
            if (self.root / "evaluation-sets.json").is_file():
                job["stage"] = "evaluation_holdout"
                job["progress"] = {"completed": None, "total": None, "unit": "revisions", "current_file": None,
                                   "detail": "Refreshing administrator holdout exclusions"}
                self._save_job(job)

                def holdout_progress(update):
                    check_cancelled()
                    job["progress"].update(completed=update["scanned_revisions"])
                    checkpoint()

                refresh_registry(self.root, on_progress=holdout_progress)
            job["stage"] = "indexing"
            job["progress"] = {"completed": None, "total": None, "unit": "indexes", "current_file": None,
                               "detail": "Preparing retrieval indexes"}
            job["generation"] = generation
            job["object_count"] = len(output)
            self._save_job(job)
            if modes:
                from .library_search import LibrarySearch

                def index_progress(update):
                    check_cancelled()
                    job["stage"] = update["stage"]
                    job["progress"] = update["progress"]
                    checkpoint()

                # Pure construction is followed by one global reindex during
                # bulk imports; do not hydrate other libraries' payloads here.
                with self.object_stream() as objects:
                    job["indexes"] = LibrarySearch(self.root / "search").build(objects, modes=list(modes), generation=generation, on_progress=index_progress)
                self._refresh_case_navigation(job["indexes"])
            check_cancelled()
            job["failed_annotations"] = sum(i.get("enrichment", {}).get("status") == "failed" for i in job["items"])
            mode_states = job.get("indexes", {}).get("modes", {})
            job["failed_indexes"] = [m for m in (modes or []) if not mode_states.get(m, {}).get("ready")]
            job["status"] = "partial" if job["failed"] or job["partial_extractions"] or job["failed_indexes"] or job["failed_annotations"] else "completed"
            job["stage"] = "finished"
            job["progress"] = {"completed": job["completed"], "total": job["total"], "unit": "files", "current_file": None,
                               "detail": "Processing finished; check partial and failed results"}
        except BuildCancelled:
            job.update(status="cancelled", cancelled_stage=job["stage"], stage="finished")
            job["progress"] = dict(job.get("progress", {}), current_file=None,
                                   detail="Build cancelled; saved artifacts and completed indexes were retained")
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {str(exc)[:1000]}"
        finally:
            if job["status"] == "failed":
                job["failed_stage"] = job["stage"]
                job["stage"] = "finished"
            job["finished_at"] = now()
            self._save_job(job)
            extraction_session.close()
            if owns_lease:
                lease.unlink(missing_ok=True)
            self._build_lock.release()
        return self._job_view(job)

    def _refresh_case_navigation(self, indexes):
        """Refresh derived overview scopes after publication under our build lease."""
        if not indexes.get("modes", {}).get("qmd_keyword", {}).get("ready"):
            return
        try:
            from .case_navigation import (NavigationNotPrepared, NavigationStale,
                                          build_navigation, validated_navigation)
            if indexes.get("unchanged"):
                from .index_manifest_cache import read_manifest
                active = read_manifest(self.root / "search" / "active.json", summary=True, resolve_paths=True)
                if active:
                    try:
                        current = validated_navigation(self.root / "search", active)
                    except (NavigationNotPrepared, NavigationStale):
                        pass
                    else:
                        indexes["case_navigation"] = {"state": "ready", "unchanged": True,
                            "overview_count": current["meta"]["overview_count"]}
                        return
            # A newly published corpus needs implementation scopes, not only
            # discoverable overviews. Derive these from the saved archives;
            # neither the original models nor QMD are rebuilt here.
            from .case_guides import build_case_guides
            indexes["case_navigation"] = dict(build_navigation(self, case_ids=None), state="ready")
            indexes["case_guides"] = build_case_guides(self, None)
        except BuildCancelled:
            raise
        except Exception as exc:
            # A disposable navigation failure cannot downgrade published QMD.
            indexes["case_navigation"] = {"state": "need_rebuild", "error": str(exc)[:700]}

    def start_build(self, library_id, **kwargs):
        if "qmd_vector" in (kwargs.get("modes") or []):
            from .retrieval_settings import require_enabled
            require_enabled(self.root)
        self._buildable_library(library_id)
        job_id = uuid.uuid4().hex
        self._save_job({"id": job_id, "library_id": library_id, "status": "queued", "stage": "queued", "pid": os.getpid(), "cancel_supported": True,
                        "started_at": now(), "completed": 0, "failed": [], "items": [],
                        "progress": {"completed": None, "total": None, "unit": "files", "current_file": None, "detail": "Waiting for constructor"}})
        def run():
            try:
                self.build(library_id, **dict(kwargs, job_id=job_id))
            except BuildCancelled:
                self._save_job({"id": job_id, "library_id": library_id, "status": "cancelled", "stage": "finished", "finished_at": now()})
            except Exception as exc:
                # A lifecycle edit can win after the initial check but before
                # the queued job is visible. It must fail visibly, never leave
                # a permanently queued task or bypass the worker's fresh gate.
                self._save_job({"id": job_id, "library_id": library_id, "status": "failed", "stage": "finished",
                                "finished_at": now(), "error": f"{type(exc).__name__}: {str(exc)[:700]}"})

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return self.job(job_id)

    def reindex(self, modes):
        from .query_terms import normalize_modes
        modes = normalize_modes(modes)
        if "qmd_vector" in modes:
            from .retrieval_settings import require_enabled
            require_enabled(self.root)
        from .library_search import LibrarySearch
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "library_id": None, "library_ids": [lib["id"] for lib in self.libraries(include_deleted=True)],
               "kind": "reindex", "scope": "all_libraries", "stage": "indexing", "status": "running",
               "pid": os.getpid(), "cancel_supported": True, "started_at": now(), "completed": 0,
               "items": [], "failed": [], "progress": {"completed": None, "total": None,
               "unit": "indexes", "current_file": None, "detail": "Preparing retrieval indexes"}}
        self._save_job(job)
        acquired = self._build_lock.acquire(blocking=False)
        lease = self.root / ".build.lock"
        owns_lease = False
        with build_control(BuildControl(self._cancel_path(job_id))):
            try:
                check_cancelled()
                if not acquired:
                    raise RuntimeError("A library build is already running")
                try:
                    old = json.loads(lease.read_text(encoding="utf-8")) if lease.is_file() else None
                    if old and (self._owner_moved(old) or not process_alive(old.get("pid"))):
                        lease.unlink()
                except (ValueError, OSError):
                    pass
                try:
                    with lease.open("x", encoding="utf-8") as stream:
                        stream.write(encoded({"pid": os.getpid(), "job_id": job_id, "workspace_root": str(self.paths.root)}))
                    owns_lease = True
                except FileExistsError:
                    raise RuntimeError("Another process is constructing this catalog") from None
                def progress(update):
                    check_cancelled()
                    job.update(stage=update["stage"], progress=update["progress"])
                    self._save_job(job)

                with self.object_stream() as objects:
                    check_cancelled()
                    generation = objects.generation
                    job["generation"] = generation
                    job["indexes"] = LibrarySearch(self.root / "search").build(objects, modes=list(modes),
                                                                            generation=generation, on_progress=progress)
                self._refresh_case_navigation(job["indexes"])
                check_cancelled()
                job["failed_indexes"] = [mode for mode in modes if not job["indexes"].get("modes", {}).get(mode, {}).get("ready")]
                job["status"] = "partial" if job["failed_indexes"] else "completed"
                job["stage"] = "finished"
            except BuildCancelled:
                job.update(status="cancelled", cancelled_stage=job["stage"], stage="finished")
                job["progress"] = dict(job["progress"], current_file=None,
                                       detail="Index build cancelled; completed indexes and partial files were retained")
            except Exception as exc:
                job.update(status="failed", failed_stage=job["stage"], stage="finished",
                           error=f"{type(exc).__name__}: {str(exc)[:1000]}")
            finally:
                job["finished_at"] = now()
                self._save_job(job)
                if owns_lease:
                    lease.unlink(missing_ok=True)
                if acquired:
                    self._build_lock.release()
        # Preserve the original synchronous CLI modes/ready result contract.
        return dict(job.get("indexes", {}), job_id=job_id, status=job["status"],
                    **({"error": job["error"]} if job.get("error") else {}))
