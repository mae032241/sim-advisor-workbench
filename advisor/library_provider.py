"""Compatibility facade joining the existing manuals and registered libraries."""
import os
import posixpath
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit
from .library_store import MANUAL_TYPES, LibraryStore
from .case_metadata import normalize_feature_branch
from .library_search import LibrarySearch, LibraryVisibilityError


class LibraryKnowledgeProvider:
    def __init__(self, legacy, data_dir):
        self.legacy = legacy
        self.store = LibraryStore(data_dir)

    def __getattr__(self, key):
        return getattr(self.legacy, key)

    def status_summary(self):
        return self.status(summary=True)

    def status(self, *, summary=False):
        status = self.legacy.status()
        libraries = self._libraries()
        enabled = [library for library in libraries if library["enabled"] and library["deleted_at"] is None]
        status["libraries"] = [library for library in libraries if library["deleted_at"] is None]
        status["library_visibility"] = {"enabled_library_count": len(enabled),
            "disabled_library_count": sum(not library["enabled"] and library["deleted_at"] is None for library in libraries),
            "deleted_library_count": sum(library["deleted_at"] is not None for library in libraries),
            "searchable_object_count": sum(library.get("object_count", 0) for library in enabled),
            "physical_object_count": sum(library.get("object_count", 0) for library in libraries)}
        try:
            from .library_search import LibrarySearch
            status["library_indexes"] = self.store.index_status(summary=True) if summary else self.store.index_status()
        except (ImportError, OSError, ValueError) as exc:
            status["library_indexes"] = {"ready": False, "detail": str(exc)}
        return status

    def _searchable(self, requested):
        try:
            return self.store.searchable_library_ids(requested)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise LibraryVisibilityError("Library lifecycle catalog is invalid") from exc

    def _libraries(self):
        try:
            return self.store.libraries(include_deleted=True)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise LibraryVisibilityError("Library lifecycle catalog is invalid") from exc

    def _legacy_exclusions(self):
        """Only managed manual roots can hide overlapping built-in manuals."""
        _, heldout_paths, _ = self._evaluation_legacy_scope()
        root = getattr(self.legacy, "doc_root", None)
        if root is None:
            return False, sorted(heldout_paths)
        root = Path(root).resolve()
        prefixes = set(heldout_paths)
        for library in self._libraries():
            if library["type"] not in MANUAL_TYPES or (library.get("enabled", True) and library.get("deleted_at") is None):
                continue
            for binding in library["roots"]:
                # Manual ingestion treats every registered root as
                # document input, including the default role="models".
                for location in [binding["path"], *binding.get("previous_paths", [])]:
                    directory = Path(location).resolve()
                    if root.is_relative_to(directory):
                        return True, []
                    if directory.is_relative_to(root):
                        prefixes.add(directory.relative_to(root).as_posix())
        return False, sorted(prefixes)

    def _evaluation_legacy_scope(self):
        """Resolve operator-owned source locators once per policy and doc root.

        Full-file prefixes reuse the native backend's existing SQL exclusion
        before candidate LIMIT. COMSOL tutorial packages are case-specific, so
        their other HTML sections stay closed as well. No source body is hashed
        or scanned during retrieval.
        """
        policy = self.store.evaluation_policy()
        root = getattr(self.legacy, "doc_root", None)
        root = Path(root).resolve() if root is not None else None
        key = (policy.token, str(root))
        cached = getattr(self, "_evaluation_legacy_cache", None)
        if cached is not None and cached[0] == key:
            return policy, cached[1], cached[2]
        paths = set(policy.document_relpaths)
        for value in policy.source_paths:
            parsed = urlsplit(value)
            path = unquote(parsed.path) if parsed.scheme == "file" else value
            if parsed.scheme == "file" and len(path) > 3 and path[0] == "/" and path[2] == ":":
                path = path[1:]
            candidate = Path(path)
            if root is not None and candidate.is_absolute():
                try:
                    paths.add(candidate.resolve().relative_to(root).as_posix())
                except ValueError:
                    pass
        paths = {path.replace("\\", "/").strip("/") for path in paths
                 if isinstance(path, str) and path and ":" not in path and ".." not in path.replace("\\", "/").split("/")}
        paths.update(path.split("/", 1)[0] for path in tuple(paths)
                     if path.split("/", 1)[0].startswith("com.comsol.help.models."))
        from .knowledge import source_id
        identities = frozenset(source_id(path) for path in paths)
        frozen = frozenset(paths)
        self._evaluation_legacy_cache = (key, frozen, identities)
        return policy, frozen, identities

    @staticmethod
    def _legacy_visible(item, blocked, prefixes):
        if blocked:
            return False
        if not prefixes:
            return True
        relative = item.get("relative_path")
        if not isinstance(relative, str):
            path = urlsplit(str(item.get("url", ""))).path
            relative = unquote(path.split("/doc/", 1)[1]) if "/doc/" in path else None
        if not isinstance(relative, str):
            return False  # An unidentified source cannot bypass a closed root.
        relative = relative.replace("\\", "/").casefold()
        return not any(relative == prefix.casefold() or relative.startswith(prefix.casefold() + "/") for prefix in prefixes)

    def search(self, query, limit=8, kind="all", filters=None):
        filters = dict(filters or {})
        libraries = filters.get("library_ids")
        if isinstance(libraries, str):
            libraries = [libraries]
        allowed = self._searchable(libraries)
        feature_branch = normalize_feature_branch(filters.get("feature_branch"))
        mode = filters.get("mode", os.environ.get("SIM_ADVISOR_LIBRARY_MODE", "native"))
        if filters.get("backend") == "qmd":
            mode = "qmd_keyword"
        modern = []
        has_objects = getattr(self.store, "has_objects", None)
        if allowed and (has_objects(allowed) if callable(has_objects) else self.store.objects(allowed)):
            # Fetch eligible library results independently of the large manual index.
            try:
                read_status = getattr(self.store, "index_status_summary", self.store.index_status)
                if not read_status().get("modes", {}).get(mode, {}).get("ready"):
                    raise RuntimeError(f"{mode} library index is unavailable or stale; rebuild required")
                hits = LibrarySearch(self.store.root / "search").search(query, mode=mode, library_ids=allowed,
                                                                      feature_branch=feature_branch, kind=kind, limit=40)
            except LibraryVisibilityError:
                raise
            except (ValueError, RuntimeError):
                if libraries is not None or feature_branch is not None or mode != "native":
                    raise
                hits = []
            for item in hits:
                if kind != "all" and item["kind"] != kind:
                    continue
                if any(filters.get(k) and filters[k] != item.get(k) for k in ("version", "branch", "package")):
                    continue
                modern.append(item)
        if libraries is not None or feature_branch is not None:
            visible = set(self._searchable(allowed))
            return [item for item in modern if item["library_id"] in visible][:limit]
        # A vector request must never quietly include lexical-only old documents.
        blocked, prefixes = self._legacy_exclusions() if mode == "native" else (False, [])
        legacy_filters = dict(filters)
        if prefixes:
            existing = legacy_filters.get("exclude_relpath_prefixes", [])
            if not isinstance(existing, list) or any(not isinstance(prefix, str) for prefix in existing):
                raise ValueError("exclude_relpath_prefixes must be a list of relative directory paths")
            legacy_filters["exclude_relpath_prefixes"] = list(dict.fromkeys([*existing, *prefixes]))
        legacy = self.legacy.search(query, limit=limit, kind=kind, filters=legacy_filters) if mode == "native" and not blocked else []
        # Fresh lifecycle reads happen after the possibly slow legacy search.
        # Do not return a library disabled/deleted while either backend ran.
        visible = set(self._searchable(allowed))
        modern = [item for item in modern if item["library_id"] in visible]
        blocked, prefixes = self._legacy_exclusions() if mode == "native" else (False, [])
        legacy = [item for item in legacy if self._legacy_visible(item, blocked, prefixes)]
        # Rank fusion avoids mixing incomparable BM25 and vector score scales.
        ranked = {}
        for items in (modern, legacy):
            for rank, item in enumerate(items, 1):
                score = 1.0 / (60 + rank)
                if item["id"] not in ranked:
                    ranked[item["id"]] = [score, item]
                else:
                    ranked[item["id"]][0] += score
        return [v[1] for v in sorted(ranked.values(), key=lambda v: v[0], reverse=True)[:limit]]

    def _source_allowed(self, document):
        library_id = document.get("library_id")
        if library_id is not None:
            return library_id in self._searchable([library_id])
        blocked, prefixes = self._legacy_exclusions()
        return self._legacy_visible(document, blocked, prefixes)

    def _read_document(self, citation_id):
        policy, _, heldout_ids = self._evaluation_legacy_scope()
        if policy.blocks_identity(citation_id) or citation_id in heldout_ids:
            raise PermissionError("该来源属于管理员保留的测试集，Agent 不能检索或读取。")
        reader = getattr(self.store, "read", None)
        document = reader(citation_id) if callable(reader) else self.store.get(citation_id)
        if document is None:
            document = self.legacy.get_document(citation_id)
        if document is not None and not self._source_allowed(document):
            raise PermissionError("资料库已关闭或删除，不能新读取该来源。")
        return document

    def source_available(self, document):
        """Authorize a new knowledge read, independently of historical ledgers."""
        citation_id = document.get("id") if isinstance(document, dict) else document
        if not isinstance(citation_id, str) or not citation_id:
            return False
        try:
            checker = getattr(self.store, "source_available", None)
            if citation_id.startswith("lo-") and callable(checker):
                return bool(checker(citation_id))
            return self._read_document(citation_id) is not None
        except PermissionError:
            return False

    def get_document(self, citation_id):
        document = self._read_document(citation_id)
        if document is None:
            return None
        document = dict(document)
        parent = document.get("parent")
        if parent and not self.source_available(parent):
            document.pop("parent", None)
        if isinstance(document.get("children"), list):
            document["children"] = [item for item in document["children"] if self.source_available(item)]
        # Legacy parent/child and hyperlink metadata must not advertise closed
        # manual roots through an otherwise readable document.
        blocked, prefixes = self._legacy_exclusions()
        if (blocked or prefixes) and isinstance(document.get("related_links"), list):
            def visible_link(link):
                href = urlsplit(str(link.get("href", "")))
                relative = document.get("relative_path")
                target = {"url": href.geturl()}
                if not href.scheme and not href.netloc and relative:
                    target["relative_path"] = posixpath.normpath(posixpath.join(posixpath.dirname(relative), unquote(href.path)))
                return self._legacy_visible(target, blocked, prefixes)
            document["related_links"] = [link for link in document["related_links"] if isinstance(link, dict) and visible_link(link)]
        if not self._source_allowed(document):
            raise PermissionError("资料库已关闭或删除，不能新读取该来源。")
        return document

    def related_sources(self, citation_id, *, limit=8):
        source = self._read_document(citation_id)
        if source is None or source.get("library_id") is None:
            return []
        related = self.store.related_sources(citation_id, limit=limit)
        related = [item for item in related if not item.get("id") or self.source_available(item)]
        if not self.source_available(source):
            raise PermissionError("资料库已关闭或删除，不能新读取该来源。")
        return related

    def validate_source(self, document):
        citation_id = document.get("id")
        if str(citation_id).startswith("lo-"):
            saved = self.store.get(citation_id)
            if not saved:
                return False
            if all(document.get(key) == value for key, value in saved.items()):
                return True  # Exact internal archive validation remains valid.
            try:
                visible = self.get_document(citation_id)
            except PermissionError:
                return False
            return bool(visible and all(document.get(key) == value for key, value in visible.items()) and
                        all(document.get(key) == visible.get(key) for key in ("parent", "children", "related_links")))
        saved = self.legacy.get_document(citation_id)
        return bool(saved and self._source_allowed(saved) and saved.get("url") == document.get("url") and saved.get("text") == document.get("text")
                    and str(saved.get("url", "")).startswith("https://doc.comsol.com/"))
