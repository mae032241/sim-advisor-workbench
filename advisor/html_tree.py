"""Native manual TOC navigation and complete, explicitly scoped HTML reads.

The index stores titles, hierarchy, relative paths and content hashes only.
It neither retrieves a corpus nor copies manual bodies or evaluates equations.
Live library/holdout authorization belongs to the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import closing
from collections import OrderedDict
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import threading
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from .build_control import check_cancelled
from .query_scope_counts import database_signature

SCHEMA = "html-manual-tree/v1"
_PACKAGE = re.compile(r"com\.comsol\.help\.[A-Za-z0-9_.-]+\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
_HTML = {".html", ".htm"}
_TITLE_SNAPSHOTS = OrderedDict()
_TITLE_LOCK = threading.RLock()


def title_query_terms(query):
    """Literal, whitespace-separated terms; all must occur in the title path."""
    return tuple(dict.fromkeys(query.casefold().split()))


def title_query_matches(query, title, ancestors=()):
    path = " ".join([*ancestors, title]).casefold()
    return all(term in path for term in title_query_terms(query))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def generic_package(name):
    """Only native generic-help packages; example models/add-ins stay separate."""
    return bool(isinstance(name, str) and _PACKAGE.fullmatch(name)
                and not any(part.casefold() in {"models", "addins"} for part in name.split(".")))


def _plain_local(path):
    text = str(path)
    if "\x00" in text or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text) or ".." in Path(text).parts:
        raise ValueError("Expected a local path without traversal or a URL")
    value = Path(path).absolute()
    for item in (value, *value.parents):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise ValueError("HTML paths cannot use filesystem aliases")
    return value


def _inside(root, relative):
    """Resolve one decoded relative locator without following aliases/escapes."""
    if not isinstance(relative, str) or not relative or "\x00" in relative or "\\" in relative:
        raise ValueError("Invalid relative HTML path")
    part = PurePosixPath(relative)
    if part.is_absolute() or ".." in part.parts or ":" in relative:
        raise ValueError("HTML path escapes its registered root")
    path = _plain_local(root.joinpath(*part.parts))
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("HTML path escapes its registered root")
    return path


def _href(root, package, href, parent_doc=None):
    """External/unsupported links are metadata; they are never opened."""
    if not isinstance(href, str) or "\x00" in href:
        raise ValueError("Invalid TOC href")
    parts = urlsplit(href)
    anchor = unquote(parts.fragment)
    if parts.scheme or parts.netloc:
        return None, anchor, "external_reference"
    path = unquote(parts.path).replace("\\", "/")
    if not path:
        return (parent_doc, anchor, None) if anchor and parent_doc else (None, anchor, "navigation_only")
    if path.startswith("/"):
        path = path.lstrip("/")
    elif path.split("/", 1)[0].startswith("com.comsol.help."):
        pass
    else:
        path = package + "/" + path
    # Native parent references may be valid within the HTML root. Normalize
    # lexically, rejecting an attempt to move above that root before any read.
    normalized = []
    for item in path.split("/"):
        if item in {"", "."}:
            continue
        if item == "..":
            if not normalized:
                raise ValueError("TOC href escapes the HTML root")
            normalized.pop()
        else:
            normalized.append(item)
    relative = "/".join(normalized)
    _inside(root, relative)
    if not normalized or not generic_package(normalized[0]):
        return None, anchor, "excluded_package"
    if PurePosixPath(relative).suffix.casefold() not in _HTML:
        return None, anchor, "non_html_reference"
    return relative, anchor, None


def native_packages(html_root):
    """Discover native generic TOCs without walking bodies or reading an index."""
    root = _plain_local(html_root)
    if not root.is_dir():
        return []
    if generic_package(root.name) and (root / "toc.xml").is_file():
        return [root.name]
    return sorted(path.name for path in root.iterdir() if path.is_dir() and generic_package(path.name)
                  and (path / "toc.xml").is_file())


def build_html_tree(html_root, output, *, library_id, root_id="help", packages=None,
                    on_progress=None, fingerprints=None):
    """Build a new metadata-only SQLite tree from unmodified native toc.xml files."""
    if not _IDENTITY.fullmatch(str(library_id)) or not _IDENTITY.fullmatch(str(root_id)):
        raise ValueError("library_id and root_id must be bounded simple identities")
    root, destination = _plain_local(html_root), _plain_local(output)
    if not root.is_dir():
        raise FileNotFoundError("HTML root does not exist")
    if destination.resolve().is_relative_to(root.resolve()):
        raise ValueError("The navigation index must be outside the original HTML root")
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError("HTML tree output must be a new empty directory")
    if isinstance(packages, str):
        raise ValueError("packages must be a sequence of exact native package names")
    discovered = native_packages(root)
    chosen = sorted(set(packages)) if packages is not None else discovered
    if not chosen or any(not generic_package(name) for name in chosen):
        raise ValueError("Select at least one generic manual package; model/add-in packages are excluded")
    nodes, documents, package_records, warnings = [], {}, [], []
    document_cache = {}
    package_root_layout = generic_package(root.name) and (root / "toc.xml").is_file()
    if package_root_layout and chosen != [root.name]:
        raise ValueError("A package-scoped root cannot include adjacent packages")
    toc_root = root.parent if package_root_layout else root
    prior_fingerprints = fingerprints or {}
    hashed, reused, processed = 0, 0, 0

    def progress(phase, current=None, *, finished=False):
        check_cancelled()
        if on_progress:
            on_progress({"stage": "html_tree", "progress": {
                "completed": processed, "total": processed if finished else None,
                "unit": "documents", "current_file": current, "phase": phase,
                "hashed": hashed, "reused": reused, "detail": "Building native HTML chapter metadata"}})
        check_cancelled()

    def stat_key(info):
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

    def document(relative):
        nonlocal hashed, reused, processed
        if relative not in document_cache:
            progress("hashing", relative)
            path = _inside(root, relative)
            if path.is_file():
                before = stat_key(path.stat())
                previous = prior_fingerprints.get(relative, {})
                if (isinstance(previous, dict) and previous.get("stat") == before
                        and re.fullmatch(r"[a-f0-9]{64}", str(previous.get("sha256", "")))):
                    digest, size = previous["sha256"], before[2]
                    reused += 1
                else:
                    value = hashlib.sha256()
                    with path.open("rb") as handle:
                        while chunk := handle.read(1024 * 1024):
                            check_cancelled()
                            value.update(chunk)
                    digest, size = value.hexdigest(), before[2]
                    hashed += 1
                if stat_key(path.stat()) != before:
                    raise ValueError("HTML source changed during fingerprinting")
                document_cache[relative] = (digest, size, before)
            else:
                document_cache[relative] = (None, 0, None)
            processed += 1
            progress("hashing", relative)
        digest, size, identity = document_cache[relative]
        if digest:
            documents[relative] = (digest, size, _json(identity))
        return digest

    for package in chosen:
        progress("toc", package + "/toc.xml")
        toc_path = _inside(toc_root, package + "/toc.xml")
        raw = toc_path.read_bytes()
        if len(raw) > 32 * 1024 * 1024 or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.I):
            raise ValueError("TOC must be bounded XML without document/entity declarations")
        tree = ET.fromstring(raw)
        toc_hash = hashlib.sha256(raw).hexdigest()
        label = tree.get("label") or package

        def add(title, href, parent, location, *, package_root=False):
            check_cancelled()
            parent_doc = parent["doc_path"] if parent else None
            if package_root_layout and parent_doc:
                parent_doc = package + "/" + parent_doc
            relative, anchor, blocked = (None, "", "navigation_only") if package_root else _href(toc_root, package, href, parent_doc)
            if package_root_layout and relative:
                if relative.startswith(package + "/"):
                    relative = relative[len(package) + 1:]
                else:
                    relative, blocked = None, "outside_registered_root"
            digest = document(relative) if relative else None
            if relative and not digest:
                blocked = "missing_html"
            if blocked and not package_root:
                warnings.append({"package": package, "href": href, "doc_path": relative, "reason": blocked})
            ancestor_ids = [*parent["ancestor_ids"], parent["node_id"]] if parent else []
            ancestor_titles = [*parent["ancestor_titles"], parent["title"]] if parent else []
            identity = "html-" + hashlib.sha256(_json([library_id, root_id, package, location, title, href, digest,
                                                       toc_hash if package_root else None]).encode("utf-8")).hexdigest()
            node = {"node_id": identity, "parent_id": parent["node_id"] if parent else None,
                    "package": package, "title": title, "href": href, "anchor": anchor,
                    "doc_path": relative, "doc_sha256": digest, "depth": len(ancestor_ids),
                    "sibling_order": location[-1] if location else chosen.index(package),
                    "tree_order": len(nodes), "ancestor_ids": ancestor_ids, "ancestor_titles": ancestor_titles,
                    "child_count": 0, "available": bool(digest), "blocked_reason": blocked}
            if parent:
                parent["child_count"] += 1
            nodes.append(node)
            return node

        top = add(label, "", None, (), package_root=True)
        package_records.append({"package": package, "toc_sha256": toc_hash, "root_node_id": top["node_id"]})

        def topics(element, parent, location):
            position = 0
            for child in element:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "topic":
                    current = add(child.get("label") or child.get("href") or "Untitled topic",
                                  child.get("href", ""), parent, (*location, position))
                    position += 1
                    topics(child, current, (*location, current["sibling_order"]))
                elif tag == "link":
                    warnings.append({"package": package, "reason": "external_toc_link_not_followed",
                                     "toc": child.get("toc", "")})
                elif list(child):
                    # Unknown XML wrappers are not external TOC includes.
                    # Preserve their descendant topics in source order.
                    for descendant in child.iter():
                        if descendant is not child and descendant.tag.rsplit("}", 1)[-1] == "topic":
                            raise ValueError("Unsupported TOC wrapper containing topics")
        topics(tree, top, ())

    destination.mkdir(parents=True, exist_ok=True)
    index_path = destination / "index.sqlite3"
    progress("writing")
    with closing(sqlite3.connect(index_path)) as db:
        db.executescript("""CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE documents(doc_path TEXT PRIMARY KEY,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,stat_fingerprint TEXT);
          CREATE TABLE nodes(node_id TEXT PRIMARY KEY,parent_id TEXT,package TEXT NOT NULL,title TEXT NOT NULL,
            href TEXT NOT NULL,anchor TEXT NOT NULL,doc_path TEXT,doc_sha256 TEXT,depth INTEGER NOT NULL,
            sibling_order INTEGER NOT NULL,tree_order INTEGER NOT NULL,ancestor_ids TEXT NOT NULL,
            ancestor_titles TEXT NOT NULL,child_count INTEGER NOT NULL,available INTEGER NOT NULL,blocked_reason TEXT);
          CREATE INDEX node_parent ON nodes(parent_id,sibling_order);
          CREATE INDEX node_document ON nodes(doc_path);
          CREATE UNIQUE INDEX node_preorder ON nodes(tree_order);""")
        db.executemany("INSERT INTO metadata VALUES(?,?)", [(key, value) for key, value in
                       {"schema": SCHEMA, "library_id": library_id, "root_id": root_id}.items()])
        db.executemany("INSERT INTO documents VALUES(?,?,?,?)", [(key, *value) for key, value in sorted(documents.items())])
        for node in nodes:
            check_cancelled()
            db.execute("INSERT INTO nodes VALUES(" + ",".join("?" for _ in range(16)) + ")", tuple(
                _json(node[key]) if key in {"ancestor_ids", "ancestor_titles"} else int(node[key]) if key == "available" else node[key]
                for key in ("node_id", "parent_id", "package", "title", "href", "anchor", "doc_path", "doc_sha256", "depth",
                            "sibling_order", "tree_order", "ancestor_ids", "ancestor_titles", "child_count", "available", "blocked_reason")))
        db.commit()
    manifest = {"schema": SCHEMA, "ready": True, "index_file": "index.sqlite3",
                "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
                "library_id": library_id, "root_id": root_id, "node_count": len(nodes),
                "document_count": len(documents), "package_count": len(chosen), "packages": package_records,
                "excluded_package_families": ["com.comsol.help.models.*", "com.comsol.help.addins*"],
                "unavailable_node_count": sum(not n["available"] for n in nodes), "warnings": warnings,
                "unavailable_reasons": {reason: sum(n["blocked_reason"] == reason for n in nodes)
                                        for reason in sorted({n["blocked_reason"] for n in nodes if n["blocked_reason"]})},
                "hashing": {"hashed": hashed, "reused": reused},
                "content_stored": "TOC metadata and relative document paths/hashes only; no HTML or body text"}
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    progress("finished", finished=True)
    return {"ready": True, "schema": SCHEMA, "directory": str(destination),
            "index_path": str(index_path), "manifest_path": str(destination / "manifest.json"),
            **{key: manifest[key] for key in ("index_sha256", "node_count", "document_count", "package_count")}}


class HtmlTreeIndex:
    """Read-only native-title navigation. No body, vector or keyword index."""
    def __init__(self, index_path):
        self.path = _plain_local(index_path)
        self._db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self._db.row_factory = sqlite3.Row
        self.meta = dict(self._db.execute("SELECT key,value FROM metadata"))
        if self.meta.get("schema") != SCHEMA:
            self.close()
            raise ValueError("Unsupported HTML tree schema")

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _view(self, row):
        value = dict(row)
        for key in ("ancestor_ids", "ancestor_titles"):
            value[key] = json.loads(value[key])
        value["available"] = bool(value["available"])
        value["readable"] = value["available"]
        value.update({key: self.meta[key] for key in ("library_id", "root_id")})
        parent = value["parent_id"]
        for key, op, ordering in (("previous_sibling_id", "<", "DESC"), ("next_sibling_id", ">", "ASC")):
            found = self._db.execute(f"SELECT node_id FROM nodes WHERE parent_id IS ? AND sibling_order {op} ? ORDER BY sibling_order {ordering} LIMIT 1",
                                     (parent, value["sibling_order"])).fetchone()
            value[key] = found[0] if found else None
        found = self._db.execute("SELECT node_id FROM nodes WHERE tree_order>? ORDER BY tree_order LIMIT 1", (value["tree_order"],)).fetchone()
        value["next_id"] = found[0] if found else None
        return value

    def node(self, node_id):
        if not isinstance(node_id, str) or not re.fullmatch(r"html-[a-f0-9]{64}", node_id):
            raise ValueError("Use an exact HTML tree node_id")
        row = self._db.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown HTML tree node_id")
        return self._view(row)

    def _title_rows(self):
        # Cache metadata by physical index version, never source permissions or
        # HTML bodies. Publication, relocation and WAL writes invalidate it.
        signature = database_signature(self.path, change_counter=True)
        key = (str(self.path), _json(signature))
        with _TITLE_LOCK:
            cached = _TITLE_SNAPSHOTS.get(key)
            if cached is not None:
                _TITLE_SNAPSHOTS.move_to_end(key)
                return cached
            rows = tuple((row["node_id"], tuple(json.loads(row["ancestor_ids"])),
                          " ".join([*json.loads(row["ancestor_titles"]), row["title"]]).casefold())
                         for row in self._db.execute(
                             "SELECT node_id,ancestor_ids,ancestor_titles,title FROM nodes ORDER BY tree_order"))
            if database_signature(self.path, change_counter=True) != signature:
                raise RuntimeError("Chapter index changed while reading title metadata; retry")
            _TITLE_SNAPSHOTS[key] = rows
            while len(_TITLE_SNAPSHOTS) > 4:
                _TITLE_SNAPSHOTS.popitem(last=False)
            return rows

    def list_children(self, node_id=None, offset=0, limit=8, query=""):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Expected offset>=0 and limit in 1..50")
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("query must be a bounded native-title navigation query")
        if node_id is not None:
            self.node(node_id)
        if query.strip():
            words = title_query_terms(query)
            identities = [identity for identity, ancestors, title_path in self._title_rows()
                          if (node_id is None or node_id in ancestors) and all(word in title_path for word in words)]
            selected = identities[offset:offset + limit]
            rows = [self._db.execute("SELECT * FROM nodes WHERE node_id=?", (identity,)).fetchone() for identity in selected]
            scope = "title_navigation"
            total = len(identities)
        else:
            rows = list(self._db.execute("SELECT * FROM nodes WHERE parent_id IS ? ORDER BY sibling_order", (node_id,)))
            scope = "children"
            total = len(rows)
            rows = rows[offset:offset + limit]
        items = [self._view(row) for row in rows]
        more = offset + len(items) < total
        return {"schema": SCHEMA, **self.meta, "parent_id": node_id, "query": query, "scope": scope,
                "query_match_mode": "all_whitespace_terms_in_title_path" if query.strip() else "children",
                "total": total, "offset": offset, "limit": limit, "returned": len(items), "items": items,
                "has_more": more, "next_offset": offset + len(items) if more else None}

    def resolve_source_path(self, relative_path):
        if not isinstance(relative_path, str) or "\\" in relative_path or "#" in relative_path or "?" in relative_path:
            raise ValueError("Use an exact relative HTML doc_path without a fragment/query")
        value = unquote(relative_path)
        parts = PurePosixPath(value)
        if not value or parts.is_absolute() or ".." in parts.parts or ":" in value or "\x00" in value:
            raise ValueError("Invalid relative HTML doc_path")
        rows = self._db.execute("SELECT * FROM nodes WHERE doc_path=? ORDER BY tree_order", (parts.as_posix(),))
        return {"doc_path": parts.as_posix(), "nodes": [self._view(row) for row in rows]}


@dataclass
class _Element:
    tag: str
    attrs: dict
    parent: object = None
    children: list = field(default_factory=list)
    start: int = 0
    end: int = 0
    line: int = 0
    column: int = 0


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_OMIT = {"head", "script", "style", "template"}
_BLOCK = {"address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "ul", *{"h" + str(n) for n in range(1, 7)}}


class _Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Element("document", {})
        self.stack, self.elements, self.events = [self.root], [], []

    def handle_starttag(self, tag, attrs):
        line, column = self.getpos()
        node = _Element(tag, {key: value if value is not None else "" for key, value in attrs},
                        self.stack[-1], start=len(self.events), line=line, column=column)
        self.stack[-1].children.append(node)
        self.elements.append(node)
        self.events.append(("start", node, node))
        if tag in _VOID:
            node.end = len(self.events)
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                while len(self.stack) > index:
                    node = self.stack.pop()
                    self.events.append(("end", node, node))
                    node.end = len(self.events)
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)
        self.events.append(("text", data, self.stack[-1]))

    def finish(self):
        self.close()
        while len(self.stack) > 1:
            node = self.stack.pop()
            self.events.append(("end", node, node))
            node.end = len(self.events)
        self.root.end = len(self.events)


def _ancestors(node):
    while node is not None:
        yield node
        node = node.parent


def _ignored(node):
    return any(element.tag in _OMIT for element in _ancestors(node))


def _text(node):
    values = []
    for child in node.children:
        if isinstance(child, str):
            values.append(child)
        elif child.tag not in _OMIT:
            values.append(child.attrs.get("alt", "") if child.tag == "img" else _text(child))
    return re.sub(r"\s+", " ", " ".join(values)).strip()


def _level(node):
    if re.fullmatch(r"h[1-6]", node.tag):
        return int(node.tag[1])
    match = re.search(r"(?:^|\s)(?:head|heading)[_-]?([1-6])(?:\b|_)", node.attrs.get("class", ""), re.I)
    return int(match[1]) if match else None


def _reference(target):
    parts = urlsplit(target)
    scheme = parts.scheme.casefold()
    blocked = bool(scheme in {"file", "javascript", "data", "vbscript"} or target.startswith("\\"))
    return {"target": target, "external": bool(parts.scheme or parts.netloc), "blocked": blocked,
            "followed": False, "reason": "reference_only_no_fetch"}


def read_html_text(path, anchor=""):
    """Read a complete page or exact native heading/anchor range, never a preview.

    References are metadata only. No linked URL, image, stylesheet or equation
    image is fetched; missing formula text stays explicitly missing.
    """
    source = _plain_local(path)
    if source.suffix.casefold() not in _HTML:
        raise ValueError("Expected one native HTML page")
    if not isinstance(anchor, str) or len(anchor) > 2048 or "\x00" in anchor:
        raise ValueError("Invalid HTML anchor")
    anchor = unquote(anchor.removeprefix("#"))
    raw = source.read_bytes()
    charset = re.search(br"charset\s*=\s*[\"']?\s*([A-Za-z0-9_-]+)", raw[:8192], re.I)
    encoding = charset[1].decode("ascii") if charset else "utf-8-sig"
    try:
        html = raw.decode(encoding)
    except (LookupError, UnicodeError) as exc:
        raise ValueError("HTML character encoding cannot be decoded losslessly") from exc
    doc = _Document()
    doc.feed(html)
    doc.finish()
    body = next((n for n in doc.elements if n.tag == "body"), doc.root)
    heading_nodes = [n for n in doc.elements if _level(n) and not _ignored(n) and body.start <= n.start < body.end]
    headings = [{"title": _text(n), "level": _level(n),
                 "anchor": n.attrs.get("id") or n.attrs.get("name") or next((c.attrs.get("id") or c.attrs.get("name") for c in doc.elements if n.start < c.start < n.end and (c.attrs.get("id") or c.attrs.get("name"))), "")} for n in heading_nodes]
    start, end, selected_heading, scope_kind = body.start, body.end, None, "html_full_page"
    if anchor:
        matches = [n for n in doc.elements if body.start <= n.start < body.end
                   and not _ignored(n) and anchor in {n.attrs.get("id"), n.attrs.get("name")}]
        if not matches:
            raise ValueError("HTML anchor was not found; no full-page fallback was read")
        target = matches[0]
        selected_heading = next((n for n in _ancestors(target) if _level(n)), None)
        if selected_heading is None and not _text(target):
            following = next((n for n in heading_nodes if n.start > target.start), None)
            between = [event for event in doc.events[target.end:following.start] if event[0] == "text" and event[1].strip()] if following else [True]
            if following and not between:
                selected_heading = following
        if selected_heading is not None:
            start = selected_heading.start
            end = next((n.start for n in heading_nodes if n.start > start and _level(n) <= _level(selected_heading)), body.end)
            scope_kind = "html_heading_section"
        elif target.children:
            start, end, scope_kind = target.start, target.end, "html_anchor_element"
        else:
            preceding = next((n for n in reversed(heading_nodes) if n.start < target.start), None)
            start = target.start
            end = next((n.start for n in heading_nodes if n.start > start and (preceding is None or _level(n) <= _level(preceding))), body.end)
            scope_kind = "html_anchor_range"
    parts, links, images, eqrefs = [], [], [], []
    text_length = 0

    def emit(value):
        nonlocal text_length
        parts.append(value)
        text_length += len(value)

    table_cells = []
    for kind, value, parent in doc.events[start:end]:
        if _ignored(parent):
            continue
        if kind == "text":
            if any(n.tag == "pre" for n in _ancestors(parent)):
                emit(value)
            else:
                emit(re.sub(r"\s+", " ", value))
            continue
        node = value
        tag = node.tag
        if kind == "start":
            if tag in _BLOCK or _level(node):
                emit("\n")
            if tag == "tr":
                emit("\n")
                table_cells.append(0)
            elif tag in {"td", "th"}:
                if table_cells and table_cells[-1]:
                    emit(" | ")
                if table_cells:
                    table_cells[-1] += 1
            elif tag == "br":
                emit("\n")
            elif tag == "li":
                emit("- ")
            elif tag == "a" and node.attrs.get("href"):
                links.append({**_reference(node.attrs["href"]), "href": node.attrs["href"], "text": _text(node),
                              "text_start": text_length, "html_line": node.line, "html_column": node.column})
            elif tag == "img":
                src, alt = node.attrs.get("src", ""), node.attrs.get("alt", "")
                equation = bool(re.search(r"(?:equation|formula|math|(?:^|[/_.-])eq(?:[\d_.-]|$))", src, re.I)
                                or any(re.search(r"(?:equation|eqn|formula)", n.attrs.get("class", ""), re.I) for n in _ancestors(node)))
                image_id = "img:" + str(len(images) + 1)
                image = {**_reference(src), "id": image_id, "src": src, "alt": alt,
                         "title": node.attrs.get("title", ""), "is_equation": equation,
                         "original_class": node.attrs.get("class", ""),
                         "parent_classes": [n.attrs["class"] for n in _ancestors(node.parent) if n.attrs.get("class")],
                         "html_line": node.line, "html_column": node.column,
                         "text_start": text_length, "image_content_not_transcribed": True}
                images.append(image)
                emit("[" + image_id + "]" + (" " + alt if alt else ""))
                image["text_end"] = text_length
                if equation:
                    eqrefs.append({"kind": "image", "image_id": image_id, "src": src, "alt": alt,
                                   "missing_formula_text": not bool(alt.strip()) or alt.strip().casefold() in {"equation", "formula", "image"},
                                   "image_fetched": False, "image_content_not_transcribed": True,
                                   "text_start": image["text_start"], "text_end": image["text_end"]})
            elif tag == "math":
                eqrefs.append({"kind": "mathml", "text": _text(node), "alttext": node.attrs.get("alttext", ""),
                               "text_start": text_length, "representation": "native_mathml_text_only", "semantic_rendering_verified": False})
        elif kind == "end":
            if tag == "tr":
                emit("\n")
                if table_cells:
                    table_cells.pop()
            elif tag in _BLOCK or _level(node):
                emit("\n")
    untrimmed = "".join(parts)
    leading = len(untrimmed) - len(untrimmed.lstrip())
    text = untrimmed.strip()
    for entry in [*links, *images, *eqrefs]:
        for key in ("text_start", "text_end"):
            if key in entry:
                entry[key] = min(len(text), max(0, entry[key] - leading))
    page_title = next((_text(n) for n in doc.elements if n.tag == "title"), "")
    return {"text": text, "links": links, "images": images, "eqrefs": eqrefs,
            "scope": {"kind": scope_kind, "anchor": anchor, "start_event": start, "end_event": end,
                      "ends_before_heading": next((_text(n) for n in heading_nodes if n.start == end), None)},
            "read_scope": scope_kind, "headings": [h for n, h in zip(heading_nodes, headings) if start <= n.start < end],
            "section_title": _text(selected_heading) if selected_heading else page_title,
            "anchor": anchor, "anchor_found": True, "full_page_available": True,
            "doc_sha256": hashlib.sha256(raw).hexdigest(), "encoding": encoding,
            "text_truncated": False, "references_fetched": False}
