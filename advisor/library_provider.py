"""Registered QMD retrieval with permission-checked legacy citation reads."""
import os
import posixpath
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlsplit
from .library_store import MANUAL_TYPES, LibraryStore
from .case_metadata import normalize_feature_branch
from .library_search import LibrarySearch, LibraryVisibilityError
from .query_terms import normalize_mode

_CONTEXT_UNSET = object()


def _navigation_identity(value):
    identity = value.get("id") if isinstance(value, dict) else value
    return identity if isinstance(identity, str) and identity else None


class LibraryKnowledgeProvider:
    def __init__(self, legacy, data_dir):
        self.legacy = legacy
        self.store = LibraryStore(data_dir)

    def __getattr__(self, key):
        return getattr(self.legacy, key)

    def status_summary(self):
        return self.status(summary=True)

    def build(self):
        """Compatibility rebuild button: rebuild the registered QMD index."""
        if not self._libraries():
            raise ValueError("请先登记资料库并构建资料，再生成 QMD 关键词索引。")
        return self.store.reindex(["qmd_keyword"])

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
        status["ready"] = bool(status["library_indexes"].get("modes", {}).get("qmd_keyword", {}).get("ready"))
        status["retrieval_backend"] = "qmd_keyword"
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

        Full-file prefixes protect historical citation reads.
        COMSOL tutorial packages are case-specific, so
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
        if kind not in {"all", "manual", "case", "code"}:
            raise ValueError("kind must be all, manual, case or code")
        filters = dict(filters or {})
        if "exclude_relpath_prefixes" in filters:
            raise ValueError("exclude_relpath_prefixes is retired; use registered library scope")
        case_id = filters.get("case_id")
        retrieval_view = filters.get("retrieval_view", "knowledge")
        include_geometry_only = filters.get("include_geometry_only", False)
        if type(include_geometry_only) is not bool:
            raise ValueError("include_geometry_only must be a boolean")
        stage = filters.get("stage")
        if case_id is not None:
            if not isinstance(case_id, str) or not case_id.strip() or len(case_id) > 256:
                raise ValueError("case_id must be an exact model_case citation ID")
            case = self._authorize_case(case_id)
        libraries = filters.get("library_ids")
        if isinstance(libraries, str):
            libraries = [libraries]
        allowed = self._searchable(libraries)
        feature_branch = normalize_feature_branch(filters.get("feature_branch"))
        requested_mode = filters.get("mode", os.environ.get("SIM_ADVISOR_LIBRARY_MODE", "qmd_keyword"))
        if filters.get("backend") is not None:
            backend = filters["backend"]
            if backend not in {"qmd", "native", "qmd_keyword", "qmd_vector"}:
                raise ValueError("Retrieval backend must be qmd_keyword or qmd_vector")
            requested_mode = backend
        mode = normalize_mode(requested_mode)
        requested = max(1, min(40, int(limit)))
        if not allowed:
            return []
        has_objects = getattr(self.store, "has_objects", None)
        if not (has_objects(allowed) if callable(has_objects) else self.store.objects(allowed)):
            return []
        engine = LibrarySearch(self.store.root / "search")
        snapshot = engine.query_snapshot()
        state = snapshot.status
        if (not state.get("modes", {}).get(mode, {}).get("ready") or
                state.get("generation") != self.store.current_generation()):
            raise RuntimeError(f"{mode} library index is unavailable or stale; build the QMD index")
        # Library/kind/feature predicates run before TopK. Only additional
        # metadata filters need a larger hydrated pool; no legacy rank fusion.
        post_filter = any(filters.get(key) for key in ("version", "branch", "package", "physics"))
        navigation_filters = ({"retrieval_view": retrieval_view, "case_id": case_id, "stage": stage}
                              if retrieval_view != "knowledge" or case_id is not None or stage is not None else {})
        if include_geometry_only:
            navigation_filters["include_geometry_only"] = True
        policy_reader = getattr(self.store, "evaluation_policy", None)
        query_policy = policy_reader() if callable(policy_reader) else None
        hits = engine.search(query, mode=mode, library_ids=allowed,
                             feature_branch=feature_branch, kind=kind,
                             limit=40 if post_filter else requested,
                             snapshot=snapshot, candidate_limit=40, **navigation_filters)
        from .case_context_store import load_contexts
        from .case_context_index import readable_contexts
        context_ids = [item['id'] for item in hits if item.get('type') == 'model_case']
        profiles = load_contexts(self.store.root / 'search', state, context_ids)
        readable = readable_contexts(self.store, profiles)
        modern = []
        for item in hits:
            if kind != "all" and item["kind"] != kind:
                continue
            if any(filters.get(key) and filters[key] != item.get(key)
                   for key in ("version", "branch", "package")):
                continue
            if filters.get("physics") and filters["physics"] not in item.get("physics", []):
                continue
            decorated = self._decorate_case_context(item, _profile=profiles.get(item['id']),
                                                    _readable=item['id'] in readable)
            if (item.get("retrieval", {}).get("representation", {}).get("case_context_included")
                    and not decorated.get("data", {}).get("search_context")):
                continue  # A context dependency closed after QMD's final gate.
            modern.append(decorated)
        # Re-read lifecycle after retrieval; disabling a library also wins
        # against an in-flight search. Legacy archives are read-only by ID.
        visible = set(self._searchable(allowed))
        if case_id is not None:
            # The selected model's lifecycle/holdout gate also governs linked
            # tutorial hits that live in a different, still enabled library.
            self._authorize_case(case_id, case)
        contextual = [item['id'] for item in modern if item.get('data', {}).get('search_context')]
        if contextual:
            from .case_context_index import _active
            from .query_scope_counts import database_signature
            catalog_path = self.store.root / 'catalog.sqlite3'
            catalog_signature = database_signature(catalog_path, change_counter=True)
            final_policy = self.store.evaluation_policy()
            fresh_active = _active(self.store.root / 'search')
            if any(fresh_active.get(key) != state.get(key) for key in ('generation', 'corpus_hash')):
                raise RuntimeError('Library index changed during case decoration; retry the query')
            # Do not reuse permission decisions from decoration. Reload the
            # bounded profiles and check current dependencies in one new batch.
            current_profiles = load_contexts(self.store.root / 'search', fresh_active, contextual)
            readable = readable_contexts(self.store, current_profiles)
            available = self._available_navigation_ids(contextual)
            if (database_signature(catalog_path, change_counter=True) != catalog_signature or
                    self.store.evaluation_policy() != final_policy):
                raise RuntimeError('Case source visibility changed during final authorization; retry the query')
        else:
            current_profiles, readable, available = {}, set(), set()
        # Decoration runs after the engine's final policy gate. A new holdout
        # must also win for ordinary hits, not only context-enriched cases.
        if callable(policy_reader) and policy_reader() != query_policy:
            raise RuntimeError('Source visibility changed during retrieval; retry the query')
        return [item for item in modern if item["library_id"] in visible
                and (not item.get("data", {}).get("search_context") or
                     item['id'] in readable and item['id'] in available and
                     current_profiles[item['id']]['content_hash'] == item['data']['search_context']['content_hash'])][:requested]

    def search_cases(self, query, library_ids=None, limit=8, include_geometry_only=False):
        """Search all indexed active model overviews with unchanged QMD ranking."""
        filters = {"mode": "qmd_keyword", "retrieval_view": "case_overviews"}
        if include_geometry_only:
            filters["include_geometry_only"] = True
        if library_ids is not None:
            filters["library_ids"] = library_ids
        return self.search(query, limit=limit, kind="case", filters=filters)

    def _decorate_case_context(self, source, *, full=False, _profile=_CONTEXT_UNSET, _readable=None):
        if source.get("type") != "model_case":
            return source
        from .case_context_index import _active, context_readable
        from .case_context_store import load_context
        if _profile is _CONTEXT_UNSET:
            if not (self.store.root / "search/active.json").is_file():
                return source
            try:
                profile = load_context(self.store.root / "search", _active(self.store.root / "search"), source["id"])
            except RuntimeError:
                # Underlying model archives remain independently readable.
                return source
        else:
            profile = _profile
        if not profile or not (_readable if _readable is not None else context_readable(self.store, profile)):
            return source
        result = dict(source, data=dict(source.get("data", {})))
        result["data"]["search_context"] = {"basis": profile["basis"], "content_hash": profile["content_hash"],
            "model_settings_agreement": "not_verified", "source_refs": profile["source_refs"],
            "lineage_ids": profile.get("lineage_ids", [source["id"]])}
        result["data"]["model_completeness"] = profile["model_completeness"]
        result["excerpt"] = profile["text"][:700]
        if full:
            sections = ["\n\nDOCUMENTED CASE CONTEXT — 教程摘录，与模型设置的一致性未核实："]
            for item in profile["excerpts"]:
                sections.append("[来源 " + item["source_id"] + " · " + item["section"] + "]\n" + item["text"])
            result["text"] = str(source.get("text", "")) + "\n\n".join(sections)
            result["read_scope"] = "model_metadata_and_bounded_documented_context"
        return result

    def get_case_authority(self, citation_id):
        """Read exact case identity/policy metadata without compact hydration."""
        return self._authorize_case(citation_id)

    def _authorize_case(self, citation_id, source=None):
        """Check one immutable case revision without reading settings/navigation.

        Scope authorization needs the catalog's exact identity and policy
        fields. It must not hydrate the compact model or enumerate tutorials.
        The post-query check reuses that immutable payload with fresh gates.
        """
        if not isinstance(citation_id, str) or not citation_id.strip() or len(citation_id) > 256:
            raise ValueError("case_id must be an exact model_case citation ID")
        self.store.require_not_heldout(citation_id)
        source = self.store.get(citation_id) if source is None else source
        if source is None:
            raise FileNotFoundError("Selected case overview not found")
        if source.get("id") != citation_id or source.get("type") != "model_case":
            raise ValueError("case_id must identify an exact model_case overview")
        self.store.require_readable_library(source["library_id"])
        self.store.require_not_heldout(citation_id, source)
        return source

    def _available_navigation_ids(self, references):
        """Filter ordinary registered IDs in bounded SQL batches, without bodies.

        Alias/retired-tutorial and built-in references retain their dedicated
        permission rules. No result is cached across reads or policy changes.
        """
        identities = list(dict.fromkeys(identity for value in references if (identity := _navigation_identity(value))))
        if not identities:
            return set()
        regular = [identity for identity in identities if identity.startswith("lo-")]
        special = set(identities) - set(regular)
        owners, special_registered = {}, set()
        if regular:
            with closing(self.store.connect()) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('tutorial_aliases','tutorial_retired_sources')")}
                for start in range(0, len(regular), 400):
                    batch = regular[start:start + 400]
                    placeholders = ','.join('?' for _ in batch)
                    owners.update((identity, (owner, logical)) for identity, owner, logical in db.execute(
                        "SELECT id,library_id,object_id FROM revisions WHERE id IN (" + placeholders + ")", batch))
                    if 'tutorial_aliases' in tables:
                        special_registered.update(row[0] for row in db.execute(
                            "SELECT citation_id FROM tutorial_aliases WHERE citation_id IN (" + placeholders + ")", batch))
                    if 'tutorial_retired_sources' in tables:
                        special_registered.update(row[0] for row in db.execute(
                            "SELECT citation_id FROM tutorial_retired_sources WHERE reason='model_tutorial_copy' AND citation_id IN (" + placeholders + ")", batch))
            # Sample after the SQL reads so a concurrent close or new holdout
            # wins over metadata collected earlier in this operation.
            visible, policy = set(self._searchable(None)), self.store.evaluation_policy()
            available = {identity for identity, (owner, logical) in owners.items()
                         if identity not in special_registered and owner in visible and not policy.blocks_identity(identity, logical)}
        else:
            available = set()
        special.update(special_registered)
        available.update(identity for identity in special if self.source_available(identity))
        return available

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
        return self._decorate_case_context(document, full=True) if document is not None else None

    def source_available(self, document):
        """Authorize a new knowledge read, independently of historical ledgers."""
        citation_id = document.get("id") if isinstance(document, dict) else document
        if not isinstance(citation_id, str) or not citation_id:
            return False
        try:
            if isinstance(document, dict):
                self.store.require_not_heldout(citation_id, document)
                context = document.get("data", {}).get("search_context")
                if context:
                    from .case_context_index import _active, context_readable
                    from .case_context_store import load_context
                    # A saved/candidate view may abbreviate the reference list.
                    # Authorize against its complete current source list.
                    try:
                        profile = load_context(self.store.root / "search", _active(self.store.root / "search"), citation_id)
                    except RuntimeError:
                        return False
                    if (not profile or profile["content_hash"] != context.get("content_hash")
                            or not context_readable(self.store, profile)):
                        return False
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
        parent, children = document.get("parent"), document.get("children")
        references = ([parent] if parent else []) + (children if isinstance(children, list) else [])
        available = self._available_navigation_ids(references)
        if parent and _navigation_identity(parent) not in available:
            document.pop("parent", None)
        if isinstance(children, list):
            document["children"] = [item for item in children
                                    if _navigation_identity(item) in available]
        # Compact registered objects normally have no HTML links. Do not scan
        # all library counts merely to filter a field that is absent or empty.
        if document.get("related_links"):
            blocked, prefixes = self._legacy_exclusions()
            if (blocked or prefixes) and isinstance(document["related_links"], list):
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
        self.store.require_not_heldout(citation_id, document)
        if document.get("data", {}).get("search_context") and not self.source_available(document):
            raise PermissionError("关联来源已关闭、删除或被隔离，不能读取案例上下文。")
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
