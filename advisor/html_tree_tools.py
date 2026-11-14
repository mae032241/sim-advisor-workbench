"""Live-gated native manual navigation and original HTML section reads.

The tree is metadata, never citation authority. Every request resolves the
current registered library/root and checks the original file, with no body
cache, catalog writes, or fallback to tutorial/model packages.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from urllib.parse import unquote, urlsplit, quote
from .html_tree import title_query_matches, title_query_terms

MAX_OUTPUT_BYTES = 7000
TOOL_NAMES = {"get_html_navigation", "read_html_section"}
_NATIVE_ID = re.compile(r"html-[a-f0-9]{64}$")
_PUBLIC_ID = re.compile(r"(tree-[a-f0-9]{32}):(html-[a-f0-9]{64})$")


def is_html_source_id(identity):
    return isinstance(identity, str) and bool(_PUBLIC_ID.fullmatch(identity) or _NATIVE_ID.fullmatch(identity))


def _tree_id(library_id, root_id):
    value = json.dumps([library_id, root_id], separators=(",", ":"))
    return "tree-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


class _RoutedIndex:
    """Public identities wrap native metadata; SQLite IDs stay unchanged."""
    def __init__(self, index, record, *, prefix=True):
        self.index, self.record, self.prefix = index, record, prefix
        self.meta = dict(index.meta, tree_id=record["tree_id"])

    def _public(self, identity):
        return self.record["tree_id"] + ":" + identity if self.prefix and identity else identity

    def _native(self, identity):
        match = _PUBLIC_ID.fullmatch(identity) if isinstance(identity, str) else None
        if match:
            if match[1] != self.record["tree_id"]:
                raise PermissionError("HTML node belongs to another registered tree")
            return match[2]
        if self.prefix and identity is not None:
            raise ValueError("Use a complete tree_id:html-node public identity")
        return identity

    def _view(self, node):
        value = deepcopy(node)
        value["native_node_id"] = node["node_id"]
        for key in ("node_id", "parent_id", "previous_sibling_id", "next_sibling_id", "next_id"):
            if key in value:
                value[key] = self._public(value[key])
        value["ancestor_ids"] = [self._public(identity) for identity in value.get("ancestor_ids", [])]
        value["tree_id"] = self.record["tree_id"]
        return value

    def node(self, identity):
        return self._view(self.index.node(self._native(identity)))

    def list_children(self, node_id=None, offset=0, limit=8, query=""):
        page = self.index.list_children(self._native(node_id), offset=offset, limit=limit, query=query)
        return dict(page, items=[self._view(node) for node in page["items"]])

    def resolve_source_path(self, path):
        page = self.index.resolve_source_path(path)
        return dict(page, nodes=[self._view(node) for node in page["nodes"]])


def tool_definitions():
    return [
        {"name": "get_html_navigation", "description":
         "Locate original manual chapters by their native title path. query splits on whitespace and requires EVERY literal term in title/ancestors, case-insensitive; it is not semantic or body search. Use short distinctive interface/feature terms; split different questions instead of pasting a task sentence. With no node_id/query, list package roots; node_id narrows to descendants. Empty results include refinement guidance. source_id bridges an exact existing search citation to its registered HTML nodes using the SAME query semantics; source_id and node_id are mutually exclusive. Follow parent/children/siblings, then read_html_section for original text. Navigation is not read evidence. Only enabled registered generic manual packages are accessible; model tutorials/addins and heldout paths are excluded.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": [], "properties": {
             "node_id": {"type": "string", "minLength": 1, "maxLength": 256},
             "tree_id": {"type": "string", "pattern": "^tree-[a-f0-9]{32}$", "maxLength": 37},
             "source_id": {"type": "string", "minLength": 1, "maxLength": 30000},
             "query": {"type": "string", "maxLength": 300, "default": ""},
             "offset": {"type": "integer", "minimum": 0, "default": 0},
             "limit": {"type": "integer", "minimum": 1, "maximum": 8, "default": 8}}}},
        {"name": "read_html_section", "description":
         "Read original text from an exact native manual tree node, with its parent interface and complete ancestor titles. Unicode offset/next_offset page the selected anchor section; no anchor means the original HTML page, not a whole manual chapter. Images and equation images are references, not visually interpreted formulas. Nonempty actual text grants the returned native source_id and exact html_section evidence (file SHA256, anchor, range) to the bound planning session; use that source_id in save_plan and copy its locator for setting source_refs. Navigation and legacy citations receive no credit. Follow next_offset until null and retain extraction/scope limits.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["node_id"], "properties": {
             "node_id": {"type": "string", "minLength": 1, "maxLength": 256},
             "offset": {"type": "integer", "minimum": 0, "default": 0},
             "length": {"type": "integer", "minimum": 1, "maximum": 3200, "default": 1800}}}},
    ]


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _relative(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PermissionError("Native HTML document needs a registered relative path")
    value = unquote(value).replace("\\", "/")
    if urlsplit(value).scheme or value.startswith("/") or PureWindowsPath(value).drive:
        raise PermissionError("Absolute paths and URLs are not native document identities")
    path = PurePosixPath(value)
    if ".." in path.parts or ":" in value or not path.parts:
        raise PermissionError("Native document path escapes its registered root")
    return path.as_posix()


def _generic(package):
    return (isinstance(package, str) and bool(re.fullmatch(r"com\.comsol\.help\.[A-Za-z0-9_.-]+", package))
            and not re.match(r"com\.comsol\.help\.(?:models|addins)(?:\.|$)", package, re.I))


class HtmlTreeTools:
    def __init__(self, store, index_path=None):
        self.store = store
        chosen = index_path or os.environ.get("SIM_ADVISOR_HTML_TREE_INDEX")
        self.index_path = Path(chosen) if chosen else None
        self._repository = None
        if self.index_path is not None and (self.index_path.is_dir() or not self.index_path.suffix):
            self.index_path = self.index_path / "index.sqlite3"

    def call(self, name, arguments):
        if name not in TOOL_NAMES or not isinstance(arguments, dict):
            raise ValueError("Unknown HTML tree tool or invalid argument object")
        allowed = {"node_id", "source_id", "tree_id", "query", "offset", "limit"} if name == "get_html_navigation" else {"node_id", "offset", "length"}
        if set(arguments) - allowed:
            raise ValueError("HTML tools accept node/source identities, not free paths or URLs")
        offset = arguments.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative Unicode/item offset")
        count = arguments.get("limit", 8) if name == "get_html_navigation" else arguments.get("length", 1800)
        if type(count) is not int or not 1 <= count <= (8 if name == "get_html_navigation" else 3200):
            raise ValueError("HTML page range is outside the published contract")
        for key in ("node_id", "source_id"):
            if key in arguments and (not isinstance(arguments[key], str) or not arguments[key]):
                raise ValueError(key + " must be a nonempty exact identity")
        if arguments.get("node_id") and arguments.get("source_id"):
            raise ValueError("node_id and source_id are mutually exclusive")
        query = arguments.get("query", "")
        if not isinstance(query, str) or len(query) > 300:
            raise ValueError("query must be at most 300 characters")
        if name == "read_html_section" and not arguments.get("node_id"):
            raise ValueError("read_html_section requires node_id")
        if self.store is None or self.index_path is not None and not self.index_path.is_file():
            return {"state": "not_prepared", "is_read_evidence": False,
                    "detail": "Native HTML chapter index is not prepared for the registered manual library."}
        if name == "get_html_navigation" and not arguments.get("node_id") and not arguments.get("source_id"):
            if self.index_path is None:
                return self._root_navigation(arguments, offset, count, query)
        record = self._record_for(arguments.get("node_id"), arguments.get("source_id"), arguments.get("tree_id"))
        if record is None:
            return {"state": "not_prepared", "is_read_evidence": False,
                    "detail": "No active registered native HTML tree maps this source/root."}
        with self._open(record) as index:
            if name == "get_html_navigation":
                return self._navigation(index, arguments, offset, count, query)
            return self._read(index, arguments["node_id"], offset, count)

    def _repo(self):
        if self._repository is None:
            from .html_tree_store import HtmlTreeRepository
            self._repository = HtmlTreeRepository(self.store)
        return self._repository

    @contextmanager
    def _open(self, record):
        from .html_tree import HtmlTreeIndex
        with HtmlTreeIndex(record["index_path"]) as native:
            if any(native.meta.get(key) != record.get(key) for key in ("library_id", "root_id")):
                raise PermissionError("Registered HTML tree binding differs from its index")
            self._binding(native.meta)
            yield _RoutedIndex(native, record, prefix=self.index_path is None)

    def _records(self):
        if self.index_path is not None:
            from .html_tree import HtmlTreeIndex
            with HtmlTreeIndex(self.index_path) as native:
                return [dict(native.meta, tree_id=_tree_id(native.meta["library_id"], native.meta["root_id"]), index_path=self.index_path)]
        records = []
        for record in self._repo().list_trees():
            try:
                self._binding(record)
            except (PermissionError, FileNotFoundError, ValueError):
                continue
            records.append(record)
        return sorted(records, key=lambda row: row["tree_id"])

    def navigation_metadata(self):
        """Trusted construction view of active TOCs, not a model tool result.

        Only native SQLite title/identity metadata is inspected. This neither
        opens HTML bodies nor grants read evidence. Callers must reauthorize
        any selected node before exposing a relation to an Agent.
        """
        trees, nodes = [], []
        policy = self.store.evaluation_policy()
        for record in self._records():
            trees.append({key: record.get(key) for key in ("tree_id", "library_id", "root_id", "generation", "index_sha256")})
            with self._open(record) as index:
                _, root = self._binding(index.meta)
                for row in index.index._db.execute("SELECT node_id FROM nodes ORDER BY tree_order"):
                    node = index.node(index._public(row[0]))
                    try:
                        self._authorize(index, node, verify_hash=False, _authority=(root, policy))
                    except (PermissionError, ValueError, FileNotFoundError):
                        continue
                    node["tree_generation"] = record.get("generation")
                    nodes.append(node)
                self._binding(index.meta)
        policy_stamp = {key: sorted(getattr(policy, key, ())) for key in
                        ("citation_ids", "object_ids", "case_groups", "source_hashes", "asset_keys", "document_relpaths", "source_paths")}
        signature = hashlib.sha256(json.dumps([trees, policy_stamp], sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return {"generation": signature, "trees": trees, "nodes": nodes,
                "is_read_evidence": False, "scope": "native_toc_metadata_only"}

    def authorize_metadata_nodes(self, identities, *, policy=None):
        """Request-local batch gate for server-side navigation relations.

        No body is parsed or credited. Each current tree/root is opened once,
        and each referenced original file is hash-checked once in this batch.
        Neither permissions nor file digests survive the call.
        """
        policy = policy if policy is not None else self.store.evaluation_policy()
        groups, allowed = {}, {}
        for identity in set(identities):
            match = _PUBLIC_ID.fullmatch(identity) if isinstance(identity, str) else None
            if match or self.index_path is not None and is_html_source_id(identity):
                groups.setdefault(match[1] if match else None, []).append(identity)
        for group in groups.values():
            try:
                record = self._record_for(node_id=group[0])
                with self._open(record) as index:
                    _, root = self._binding(index.meta)
                    digests, tree_allowed = {}, {}
                    for identity in group:
                        try:
                            node = index.node(identity)
                            path = self._authorize(index, node, body=True, verify_hash=False,
                                                   _authority=(root, policy))
                            if path not in digests:
                                digests[path] = hashlib.sha256(path.read_bytes()).hexdigest()
                            if digests[path] == node.get("doc_sha256"):
                                tree_allowed[identity] = {"asset_hash": digests[path],
                                                         "tree_generation": record.get("generation")}
                        except (KeyError, PermissionError, ValueError, OSError):
                            continue
                    self._binding(index.meta)
                    allowed.update(tree_allowed)
            except (KeyError, PermissionError, ValueError, OSError):
                continue
        return allowed

    def _record_for(self, node_id=None, source_id=None, tree_id=None):
        identity = node_id or (source_id if is_html_source_id(source_id) else None)
        match = _PUBLIC_ID.fullmatch(identity) if isinstance(identity, str) else None
        if match:
            if tree_id and tree_id != match[1]:
                raise PermissionError("tree_id conflicts with the selected native node")
            tree_id = match[1]
        if self.index_path is not None:
            records = self._records()
            if tree_id and records[0]["tree_id"] != tree_id:
                raise PermissionError("Native node is outside the selected compatibility tree")
            return records[0]
        if identity and not match:
            raise ValueError("Use the complete registered tree_id:html-node identity")
        if tree_id:
            try:
                record = self._repo().resolve(tree_id)
            except KeyError as error:
                raise PermissionError("Native HTML tree is no longer active") from error
            self._binding(record)
            return record
        if source_id:
            self.store.require_not_heldout(source_id)
            source = self.store.get(source_id)
            if not source:
                raise ValueError("Unknown exact source citation")
            self.store.require_readable_library(source.get("library_id"))
            self.store.require_not_heldout(source_id, source)
            if not self.store.source_available(source_id):
                raise PermissionError("Source citation is no longer available")
            asset = source.get("asset_key", "")
            for record in self._records():
                if source.get("library_id") == record["library_id"] and isinstance(asset, str) and asset.startswith(record["root_id"] + ":"):
                    return record
        return None

    def _root_navigation(self, args, offset, limit, query):
        records = self._records()
        if args.get("tree_id"):
            record = self._record_for(tree_id=args["tree_id"])
            with self._open(record) as index:
                return self._navigation(index, args, offset, limit, query)
        if not records:
            return {"state": "not_prepared", "is_read_evidence": False,
                    "detail": "No built and enabled registered manual chapter tree is available."}
        result = {"state": "ready", "scope": "registered_title_navigation" if query else "registered_package_roots",
                  "query": query, "offset": offset, "limit": limit, "total": 0, "returned": 0,
                  "items": [], "next_offset": None, "has_more": False, "available_tree_count": len(records),
                  "is_read_evidence": False, **self._query_info(query),
                  "note": "Native TOC navigation only; body reads grant source evidence."}
        segments = []
        for record in records:
            with self._open(record) as index:
                total = index.list_children(limit=1, query=query)["total"]
            segments.append((record, total))
            result["total"] += total
        cursor, stop = 0, False
        for record, total in segments:
            if offset >= cursor + total:
                cursor += total
                continue
            local = max(0, offset - cursor)
            with self._open(record) as index:
                page = index.list_children(offset=local, limit=limit - len(result["items"]), query=query)
                for node in page["items"]:
                    position = cursor + local
                    try:
                        self._authorize(index, node)
                    except (PermissionError, ValueError, FileNotFoundError):
                        local += 1
                        continue
                    result["items"].append(self._view(node))
                    if _size(result) > MAX_OUTPUT_BYTES - 100:
                        result["items"].pop()
                        if not result["items"]:
                            raise ValueError("Native navigation metadata exceeds the page budget")
                        result["next_offset"], stop = position, True
                        break
                    local += 1
                    if len(result["items"]) == limit:
                        result["next_offset"], stop = cursor + local, True
                        break
                if stop:
                    break
                # Hidden/stale candidates consume positions as well.
                if page.get("next_offset") is not None:
                    result["next_offset"], stop = cursor + local, True
                    break
            cursor += total
        if result["next_offset"] is not None and result["next_offset"] >= result["total"]:
            result["next_offset"] = None
        result.update(returned=len(result["items"]), has_more=result["next_offset"] is not None)
        self._empty_query_hint(result, args, query)
        return result

    @staticmethod
    def _query_info(query):
        return {"query_match_mode": "all_whitespace_terms_in_title_path" if query.strip() else "children",
                "body_searched": False}

    @staticmethod
    def _empty_query_hint(result, args, query):
        if query.strip() and result.get("total") == 0:
            arguments = {key: args[key] for key in ("node_id", "tree_id", "source_id") if args.get(key)}
            result["refinement"] = {
                "reason": "No title path contains all query terms; this does not mean no relevant source exists.",
                "query_term_count": len(title_query_terms(query)),
                "suggestion": "Use one short interface/feature phrase, or browse this scope and select a parent before searching its children. Use search_knowledge for body terms.",
                "browse": {"tool": "get_html_navigation", "arguments": {**arguments, "limit": 4}}}

    def _binding(self, meta):
        library_id, root_id = meta.get("library_id"), meta.get("root_id")
        if not isinstance(library_id, str) or not isinstance(root_id, str):
            raise PermissionError("HTML index has no registered library/root binding")
        self.store.require_readable_library(library_id)
        library = self.store.library(library_id)
        if library.get("id") != library_id or library.get("type") not in {"official-manuals", "user-manuals"}:
            raise PermissionError("HTML navigation requires its registered manual library")
        binding = next((root for root in library.get("roots", []) if root.get("id") == root_id), None)
        if binding is None:
            raise PermissionError("The HTML index root is no longer registered")
        root = Path(binding["path"]).resolve(strict=True)
        if not root.is_dir():
            raise PermissionError("Registered HTML root is unavailable")
        return library, root

    def _authorize(self, index, node, *, body=False, verify_hash=True, _authority=None):
        for key in ("library_id", "root_id"):
            if node.get(key) != index.meta.get(key):
                raise PermissionError("Native node does not belong to the index's registered binding")
        root = _authority[0] if _authority is not None else self._binding(index.meta)[1]
        package = node.get("package")
        if not _generic(package):
            raise PermissionError("Only generic native manual packages may be read; model tutorials/addins are excluded")
        relative = node.get("doc_path")
        if not relative:
            if body:
                raise ValueError("This native TOC node has no readable document; browse its children")
            return None
        relative = _relative(relative)
        path = (root / Path(relative)).resolve(strict=True)
        if not path.is_relative_to(root) or path.suffix.lower() not in {".html", ".htm"}:
            raise PermissionError("Native HTML file is outside its registered root")
        packages = [part for part in (*root.parts, *Path(relative).parts) if part.casefold().startswith("com.comsol.help.")]
        if any(not _generic(part) for part in packages) or packages and packages[-1].casefold() != package.casefold():
            raise PermissionError("Native file package conflicts with its generic manual identity")
        policy = _authority[1] if _authority is not None else self.store.evaluation_policy()
        candidates = {relative.casefold(), path.as_posix().casefold()}
        for value in frozenset(getattr(policy, "document_relpaths", ())) | frozenset(getattr(policy, "source_paths", ())):
            parsed = urlsplit(value)
            blocked = unquote(parsed.path if parsed.scheme == "file" else value).replace("\\", "/").strip("/").casefold()
            if any(candidate.strip("/") == blocked or candidate.strip("/").endswith("/" + blocked) for candidate in candidates):
                raise PermissionError("Native HTML source is held out by the current evaluation policy")
        if node.get("doc_sha256") in getattr(policy, "source_hashes", ()):
            raise PermissionError("Native HTML source hash is held out")
        source = {"id": node["node_id"], "library_id": node["library_id"],
                  "asset_key": node["root_id"] + ":" + relative, "asset_hash": node.get("doc_sha256")}
        if _authority is None:
            self.store.require_not_heldout(node["node_id"], source)
            if node.get("native_node_id") != node["node_id"]:
                self.store.require_not_heldout(node.get("native_node_id"))
        elif (policy.blocks_object(source) or policy.blocks_identity(node.get("native_node_id"))):
            raise PermissionError("Native HTML metadata is held out")
        if verify_hash:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != node.get("doc_sha256"):
                raise ValueError("Native HTML source changed; rebuild the chapter index before reading")
        return path

    @staticmethod
    def _view(node):
        return {key: deepcopy(node[key]) for key in ("node_id", "tree_id", "library_id", "root_id", "title", "package", "doc_path", "anchor", "parent_id",
            "ancestor_ids", "ancestor_titles", "child_count", "available", "blocked_reason") if key in node}

    def _brief(self, index, identity):
        if not identity:
            return None
        try:
            node = index.node(identity)
            self._authorize(index, node)
        except (KeyError, ValueError, PermissionError, FileNotFoundError):
            return None
        return {key: node[key] for key in ("node_id", "title") if key in node}

    def _context(self, index, node):
        return {"parent": self._brief(index, node.get("parent_id")),
                "siblings": {"previous": self._brief(index, node.get("previous_sibling_id")),
                             "next": self._brief(index, node.get("next_sibling_id"))},
                "children": {"count": node.get("child_count", 0), "tool": "get_html_navigation", "arguments": {"node_id": node["node_id"]}},
                "next_reading_node": self._brief(index, node.get("next_id"))}

    def _source_nodes(self, index, source_id):
        if is_html_source_id(source_id):
            try:
                node = index.node(source_id)
                self._authorize(index, node, body=True)
                return [node]
            except KeyError:
                return []
        self.store.require_not_heldout(source_id)
        source = self.store.get(source_id)
        if not source:
            raise ValueError("Unknown exact source citation")
        self.store.require_readable_library(source.get("library_id"))
        self.store.require_not_heldout(source_id, source)
        if not self.store.source_available(source_id):
            raise PermissionError("Source citation is no longer available")
        asset = source.get("asset_key", "")
        prefix = index.meta["root_id"] + ":"
        if source.get("library_id") != index.meta["library_id"] or not isinstance(asset, str) or not asset.startswith(prefix):
            return []
        relative = _relative(asset[len(prefix):])
        if PurePosixPath(relative).suffix.lower() not in {".htm", ".html"}:
            return []
        nodes = index.resolve_source_path(relative).get("nodes", [])
        return [node for node in nodes if not source.get("asset_hash") or node.get("doc_sha256") == source["asset_hash"]]

    def _navigation(self, index, args, offset, limit, query):
        current = index.node(args["node_id"]) if args.get("node_id") else None
        if current:
            self._authorize(index, current)
        bridge = bool(args.get("source_id"))
        if bridge:
            nodes = self._source_nodes(index, args["source_id"])
            if not nodes:
                return {"state": "not_prepared", "source_id": args["source_id"], "is_read_evidence": False,
                        "detail": "This exact citation has no registered native HTML tree node (PDF/non-HTML and other roots are not guessed)."}
            if query:
                nodes = [n for n in nodes if title_query_matches(query, n.get("title", ""), n.get("ancestor_titles", []))]
            page = {"items": nodes[offset:offset + limit], "total": len(nodes), "next_offset": offset + limit if offset + limit < len(nodes) else None,
                    "scope": "exact_source_document_nodes"}
        else:
            page = index.list_children(node_id=args.get("node_id"), offset=offset, limit=limit, query=query)
        items = []
        consumed = 0
        result = {"state": "ready", "library_id": index.meta["library_id"], "root_id": index.meta["root_id"],
                  "scope": page.get("scope", "native_toc"), "query": query, "offset": offset, "limit": limit,
                  "total": page.get("total"), "items": items, "returned": 0, "next_offset": page.get("next_offset"),
                  "is_read_evidence": False, **self._query_info(query),
                  "note": "Native chapter navigation only; titles and links do not grant body-read evidence."}
        if bridge:
            result["source_id"] = args["source_id"]
            result.pop("items")
            result["matches"] = items
        if current:
            result["node"] = self._view(current)
            result.update(self._context(index, current))
        for node in page.get("items", []):
            try:
                self._authorize(index, node)
            except (PermissionError, FileNotFoundError, ValueError):
                consumed += 1
                continue
            items.append(self._view(node))
            if _size(result) > MAX_OUTPUT_BYTES - 100:
                items.pop()
                if not consumed:
                    raise ValueError("Native navigation metadata exceeds the tool page budget")
                result["next_offset"] = offset + consumed
                break
            consumed += 1
        result["returned"] = len(items)
        result["has_more"] = result["next_offset"] is not None
        self._empty_query_hint(result, args, query)
        self._binding(index.meta)
        if _size(result) > MAX_OUTPUT_BYTES:
            raise ValueError("Native navigation context exceeds the tool page budget")
        return result

    def _references(self, index, node, parsed, start, end):
        output = {}
        for kind in ("links", "images", "eqrefs"):
            refs = parsed.get(kind, [])
            positioned = [ref for ref in refs if isinstance(ref, dict)
                          and type(ref.get("text_start")) is int and type(ref.get("text_end")) is int]
            visible = [ref for ref in positioned if ref["text_start"] < end and ref["text_end"] > start]
            unpositioned = [ref for ref in refs if not (isinstance(ref, dict)
                            and type(ref.get("text_start")) is int and type(ref.get("text_end")) is int)]
            # Unpositioned references are explicitly page-level samples. They
            # cannot be mistaken for an image marker on the returned text page.
            selected = visible if positioned else unpositioned
            samples = []
            for ref in selected[:3]:
                original = ref if isinstance(ref, str) else ref.get("href", ref.get("src", ref.get("target", "")))
                if kind == "images" and isinstance(ref, dict):
                    item = {key: ref[key] for key in ("id", "src", "text_start", "text_end", "is_equation") if key in ref}
                    if ref.get("alt"):
                        item["alt"] = ref["alt"][:160]
                elif kind == "eqrefs" and isinstance(ref, dict):
                    item = {key: ref[key] for key in ("kind", "image_id", "text_start", "text_end", "missing_formula_text") if key in ref}
                else:
                    label = ref.get("text", ref.get("alt", "")) if isinstance(ref, dict) else ""
                    item = {"label": str(label)[:160], "reference": str(original)[:300], "accessible_by_text_tool": False}
                    if len(str(original)) > 300:
                        item["reference_preview"] = True
                target = urlsplit(str(original))
                if kind == "links" and not target.scheme and not target.netloc and (target.path or target.fragment):
                    link_path = unquote(target.path).replace("\\", "/")
                    if link_path.startswith("/com.comsol.help."):
                        relative = link_path.lstrip("/")
                        _, root = self._binding(index.meta)
                        if root.name == node["package"] and relative.startswith(node["package"] + "/"):
                            relative = relative[len(node["package"]) + 1:]
                    else:
                        relative = (PurePosixPath(node["doc_path"]).parent / link_path).as_posix() if link_path else node["doc_path"]
                    # Resolve internal ../ links only within the registered root.
                    normalized = os.path.normpath(relative).replace("\\", "/")
                    try:
                        _relative(normalized)
                        mapped = index.resolve_source_path(normalized).get("nodes", [])
                        chosen = next((n for n in mapped if (n.get("anchor") or "") == unquote(target.fragment)), None)
                        if chosen:
                            self._authorize(index, chosen)
                            item.update(node_id=chosen["node_id"], accessible_by_text_tool=True)
                    except (KeyError, ValueError, PermissionError, FileNotFoundError):
                        pass
                samples.append(item)
            output[kind] = {"document_count": len(refs), "range_count": len(visible),
                            "samples": samples,
                            "samples_scope": "returned_text_range" if positioned else "page_level_unpositioned",
                            "remaining_in_range": max(0, len(visible) - len(samples))}
            if unpositioned:
                output[kind].update(unpositioned_count=len(unpositioned),
                                    remaining_unpositioned=max(0, len(unpositioned) - (len(samples) if not positioned else 0)))
        return output

    def _read(self, index, identity, offset, length):
        from .html_tree import read_html_text
        node = index.node(identity)
        path = self._authorize(index, node, body=True)
        parsed = read_html_text(path, anchor=node.get("anchor") or "")
        if parsed.get("doc_sha256") != node["doc_sha256"]:
            raise ValueError("Native HTML file changed during the read")
        text = parsed.get("text")
        if not isinstance(text, str) or offset >= len(text) or not text[offset:offset + length].strip():
            raise ValueError("This native HTML section has no text in the requested range")
        result = {"state": "ready", "node": {key: node[key] for key in ("node_id", "title", "doc_path", "anchor", "package") if key in node},
                  "ancestor_ids": deepcopy(node.get("ancestor_ids", [])), "ancestor_titles": deepcopy(node.get("ancestor_titles", [])),
                  **self._context(index, node),
                  "library_id": node["library_id"], "root_id": node["root_id"], "doc_sha256": node["doc_sha256"],
                  "anchor": node.get("anchor") or "", "read_scope": parsed.get("read_scope", parsed.get("scope")),
                  "section_title": parsed.get("section_title"), "anchor_found": parsed.get("anchor_found"),
                  "full_page_available": parsed.get("full_page_available"),
                  "scope_note": "One HTML section/page, not a whole chapter. Images/formula images are references only, not transcribed or rendered.",
                  "references": {}, "offset": offset,
                  "non_text_content": {"image_not_transcribed": bool(parsed.get("images")),
                                       "equation_refs_not_visually_verified": bool(parsed.get("eqrefs")),
                                       "text_only": True, "scope": "source_section"},
                  "total_characters": len(text), "text": "", "next_offset": None, "evidence_refs": [],
                  "source_id": node["node_id"],
                  "citation_scope": "Native source_id plus exact html_section range; navigation/legacy IDs receive no read credit."}
        requested_end = min(len(text), offset + length)

        def render(end):
            locator = "sha256:" + node["doc_sha256"] + ":anchor:" + quote(node.get("anchor") or "", safe="") + f":text:{offset}:{end}"
            result.update(text=text[offset:end], returned_characters=end - offset,
                          next_offset=end if end < len(text) else None,
                          evidence_refs=[{"source_id": node["node_id"], "context": "html_section", "locator": locator}])
            result["references"] = self._references(index, node, parsed, offset, end)
            result["text_delivery"] = {"start": offset, "end": end,
                                       "complete_section": offset == 0 and end == len(text)}
            return _size(result)

        if render(requested_end) > MAX_OUTPUT_BYTES:
            # Fit the largest declared Unicode page rather than repeatedly
            # halving text and leaving most of the actual byte budget unused.
            low, high, best = offset + 1, requested_end - 1, None
            while low <= high:
                end = (low + high) // 2
                if render(end) <= MAX_OUTPUT_BYTES:
                    best = deepcopy(result)
                    low = end + 1
                else:
                    high = end - 1
            if best is None:
                raise ValueError("Native section metadata leaves no body budget")
            result = best
        self._authorize(index, node, body=True)
        if not result["text"].strip():
            raise ValueError("This page has no nonempty native body text")
        return result

    def get_document(self, identity, *, metadata_only=False):
        """Dynamic virtual source; never insert it into the library catalog."""
        if not is_html_source_id(identity) or self.store is None:
            return None
        record = self._record_for(node_id=identity)
        if record is None:
            return None
        with self._open(record) as index:
            try:
                node = index.node(identity)
            except KeyError:
                return None
            path = self._authorize(index, node, body=True)
            native = {"tree_id": record["tree_id"], "node_id": node.get("native_node_id", node["node_id"]),
                      "root_id": node["root_id"], "doc_path": node["doc_path"],
                      "doc_sha256": node["doc_sha256"], "anchor": node.get("anchor") or ""}
            library = self.store.library(node["library_id"])
            version = library.get("version") or "6.3"
            url_path = node["doc_path"] if node["doc_path"].startswith(node["package"] + "/") else node["package"] + "/" + node["doc_path"]
            document = {"id": node["node_id"], "title": node["title"], "kind": "manual", "type": "native_html_section",
                        "library_id": node["library_id"], "package": node["package"], "version": version,
                        "asset_key": node["root_id"] + ":" + node["doc_path"], "asset_hash": node["doc_sha256"],
                        "relative_path": node["doc_path"], "section_path": [*node.get("ancestor_titles", []), node["title"]],
                        "url": "https://doc.comsol.com/" + quote(version, safe=".") + "/doc/" + quote(url_path, safe="/._-") + ("#" + quote(native["anchor"], safe="") if native["anchor"] else ""),
                        "native_html": native, "provenance_kind": "native_html_virtual_source",
                        "read_scope": "native_html_anchor_section" if native["anchor"] else "native_html_page",
                        "source_context": {"role": "general_manual", "applicability": "not_verified",
                                           "package": node["package"], "ancestor_titles": deepcopy(node.get("ancestor_titles", []))},
                        "text_storage": "Original registered HTML; no body copy stored in the catalog or plan."}
            if not metadata_only:
                from .html_tree import read_html_text
                parsed = read_html_text(path, anchor=native["anchor"])
                if parsed.get("doc_sha256") != node["doc_sha256"]:
                    raise PermissionError("Native HTML changed during the source read")
                document.update(text=parsed["text"], text_truncated=False,
                                read_scope=parsed.get("read_scope", parsed.get("scope")),
                                images=parsed.get("images", []), eqrefs=parsed.get("eqrefs", []),
                                image_content_not_transcribed=bool(parsed.get("images")))
                self._authorize(index, node, body=True)
            return document

    def source_available(self, source):
        identity = source.get("id") if isinstance(source, dict) else source
        try:
            current = self.get_document(identity, metadata_only=True)
            if not current:
                return False
            return (not isinstance(source, dict) or all(source.get(key) == current[key]
                    for key in ("id", "library_id", "asset_key", "asset_hash", "native_html")))
        except (KeyError, ValueError, OSError):
            return False


def html_locator_matches(source, reference):
    """A structural check, never a replacement for the session read ledger."""
    native = source.get("native_html") or {}
    prefix = "sha256:" + str(native.get("doc_sha256", "")) + ":anchor:" + quote(native.get("anchor") or "", safe="") + ":text:"
    match = re.fullmatch(re.escape(prefix) + r"([0-9]+):([0-9]+)", reference.get("locator", ""))
    return bool(reference.get("source_id") == source.get("id") and reference.get("context") == "html_section"
                and match and int(match[2]) > int(match[1]))
