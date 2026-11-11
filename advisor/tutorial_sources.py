"""One canonical COMSOL tutorial, with verified alternate source formats.

Identity is a COMSOL package identifier, never a loose model basename. Source
matching also retains the complete directory prefix, so different installations,
languages and document versions cannot provide each other's missing companion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote, urlsplit

from .build_control import check_cancelled
from .case_metadata import object_feature_branch
from .knowledge import TopicParser


PARSER_VERSION = "canonical-tutorial/v1"
MAX_SECTION_CHARS = 850
_IDENTIFIER = r"models\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_.-]+)"
_PACKAGE = re.compile(r"com\.comsol\.help\.(" + _IDENTIFIER + r")", re.I)
_PDF = re.compile(r"(" + _IDENTIFIER + r")\.pdf", re.I)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_BLOCK = {"div", "p", "li", "pre", "table", "tr", "td", "th", "blockquote", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6"}
_IGNORED = {"head", "title", "script", "style", "noscript", "nav"}
_INSTRUCTIONS = re.compile(r"^(?:model(?:l)?ing instructions|model instructions|建模说明|建模說明|建模步[骤驟])\b", re.I)


def _source_parts(path):
    raw = str(path).replace("\\", "/")
    # A Windows drive is not a URL scheme for these source-name rules.
    parsed = urlsplit(raw)
    value = unquote(parsed.path) if parsed.scheme and len(parsed.scheme) > 1 else raw.split("#", 1)[0].split("?", 1)[0]
    if parsed.scheme.lower() == "file":
        if parsed.netloc:
            value = "//" + parsed.netloc + value
        elif re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
    return PurePosixPath(value)


def tutorial_identity(path):
    """Return ``models.<namespace>.<model>`` only for canonical source names.

    Auxiliary geometry pages, generic PDFs and mismatching package/file names
    do not identify a complete tutorial. This helper performs no filesystem IO.
    """
    source = _source_parts(path)
    package = _PACKAGE.fullmatch(source.parent.name)
    pdf = _PDF.fullmatch(source.name)
    if pdf:
        identity = pdf[1].lower()
        if package and package[1].lower() != identity:
            return None
        # A PDF in an unrelated tutorial package is not a root-level companion.
        if source.parent.name.lower().startswith("com.comsol.help.models.") and not package:
            return None
        return identity
    if source.suffix.lower() in {".html", ".htm"} and package and source.stem.lower() == package[3].lower():
        return package[1].lower()
    return None


def _root_path(root):
    if isinstance(root, dict):
        root = root.get("path")
    return Path(root).expanduser().resolve() if root is not None else None


def _registered_paths(records, root):
    if records is None:
        return None
    paths = set()
    for record in records:
        check_cancelled()
        value = record.get("path") if isinstance(record, dict) else record
        if value is None and isinstance(record, dict) and root is not None:
            value = root / record["relative_path"]
        if value is not None:
            candidate = Path(value)
            if not candidate.is_absolute() and root is not None:
                candidate = root / candidate
            paths.add(candidate.resolve())
    return paths


def _eligible(path, root, registered):
    candidate = Path(path).resolve()
    return (root is None or candidate.is_relative_to(root)) and (registered is None or candidate in registered) and candidate.is_file()


def _has_text(path):
    """A broken/empty export cannot replace an otherwise valid PDF source."""
    parser = TopicParser()
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
            while chunk := stream.read(65536):
                check_cancelled()
                parser.feed(chunk)
                if parser.content().strip():
                    return True
        parser.close()
    except (OSError, ValueError):
        return False
    return bool(parser.content().strip())


def choose_companion_html(path, root=None, records=None):
    """Find one existing exact HTML companion inside the registered boundary.

    Both COMSOL's package-local PDF and its root-relative download filename are
    supported. ``records``, when given, is an additional registration boundary;
    a file outside it never causes a PDF to be omitted from indexing.
    """
    check_cancelled()
    source, boundary = Path(path).expanduser().resolve(), _root_path(root)
    identity = tutorial_identity(source)
    if identity is None:
        return None
    registered = _registered_paths(records, boundary)
    if source.suffix.lower() in {".html", ".htm"}:
        return source if _eligible(source, boundary, registered) and _has_text(source) else None
    package = "com.comsol.help." + identity
    parent = source.parent if source.parent.name.lower() == package else source.parent / package
    name = identity.split(".", 2)[2]
    candidates = [parent / (name + suffix) for suffix in (".html", ".htm")]
    found = [candidate.resolve() for candidate in candidates if _eligible(candidate, boundary, registered) and _has_text(candidate)]
    return found[0] if len(found) == 1 else None


def companion_pdf_paths(path, root=None, records=None):
    """Return only exact existing PDF alternates, never generic attachments."""
    check_cancelled()
    source, boundary = Path(path).expanduser().resolve(), _root_path(root)
    identity = tutorial_identity(source)
    if identity is None or source.suffix.lower() not in {".html", ".htm"}:
        return []
    registered = _registered_paths(records, boundary)
    name = identity + ".pdf"
    return [candidate.resolve() for candidate in (source.parent / name, source.parent.parent / name)
            if _eligible(candidate, boundary, registered) and choose_companion_html(candidate, boundary) == source]


def canonical_tutorial_key(path, source_sha256):
    """Identify exact source content within its installation/language prefix."""
    identity = tutorial_identity(path)
    if identity is None:
        return None
    source = _source_parts(path)
    prefix = source.parent.parent if _PACKAGE.fullmatch(source.parent.name) else source.parent
    parsed = urlsplit(str(path).replace("\\", "/"))
    scope = str(prefix)
    if parsed.scheme.lower() not in {"", "file"} and len(parsed.scheme) > 1:
        scope = parsed.scheme.lower() + "://" + parsed.netloc.lower() + scope
    value = scope + "\0" + identity + "\0" + str(source_sha256)
    return "tutorial-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def tutorial_reference(path, *, source_sha256=None, title=None, chapters=None, document_identifier=None):
    """Create metadata-only source association; no tutorial text is copied."""
    source = Path(path).expanduser().resolve()
    identity = tutorial_identity(source)
    return {"source_uri": source.as_uri(), "source_sha256": source_sha256,
            "document_identifier": document_identifier or identity,
            "title": title or source.stem, "chapters": list(chapters or []),
            "identity": identity, "status": "unresolved_external_document",
            "relation_status": "reference_not_verified_model_agreement"}


def _hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check_cancelled()
            value.update(chunk)
    check_cancelled()
    return value.hexdigest()


@dataclass
class _Element:
    tag: str
    attrs: dict
    line: int
    children: list = field(default_factory=list)


class _TutorialDOM(HTMLParser):
    """Small DOM used to emit disjoint text blocks, including table cells."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Element("root", {}, 1)
        self.stack = [self.root]
        self.links, self.images = [], []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        node = _Element(tag, attributes, self.getpos()[0])
        self.stack[-1].children.append(node)
        if tag == "a" and attributes.get("href"):
            self.links.append({"href": attributes["href"], "title": attributes.get("title", "")})
        if tag == "img" and attributes.get("src"):
            self.images.append(attributes["src"])
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def _heading_level(node):
    match = re.fullmatch(r"h([1-6])", node.tag)
    if match:
        return int(match[1])
    match = re.search(r"(?:^|\s)head([1-6])(?:\b|_)", node.attrs.get("class", ""), re.I)
    return int(match[1]) if match else None


def _flatten(node):
    if isinstance(node, str):
        return node
    if node.tag in _IGNORED:
        return ""
    if node.tag == "br":
        return "\n"
    if node.tag == "img":
        # Empty alt text represents image-only evidence; its source is metadata.
        return node.attrs.get("alt", "") if node.attrs.get("alt") != "PDF" else ""
    if node.tag == "tr":
        return " | ".join(value.strip() for child in node.children if (value := _flatten(child)).strip())
    return "".join(("\n" + _flatten(child) + "\n") if isinstance(child, _Element) and child.tag in _BLOCK
                   else _flatten(child) for child in node.children)


def _anchor(node):
    if not isinstance(node, _Element):
        return ""
    own = node.attrs.get("id") or node.attrs.get("name")
    return own or next((anchor for child in node.children if (anchor := _anchor(child))), "")


def _has_block(node):
    return isinstance(node, _Element) and (node.tag in _BLOCK or any(_has_block(child) for child in node.children))


def _blocks(node):
    """Walk DOM order while assigning every visible text node at most once."""
    if isinstance(node, str) or node.tag in _IGNORED:
        return
    check_cancelled()
    children = [child for child in node.children if isinstance(child, _Element)]
    # A table row retains cell relationships. Presentation tables contain the
    # bullet number and its text only once, rather than an outer/inner copy.
    leaf = _heading_level(node) is not None or node.tag == "tr" and not any(_contains(child, "tr") for child in children)
    if leaf or node.tag in _BLOCK and not any(_has_block(child) for child in children):
        text = " ".join(_flatten(node).split()).strip(" |")
        if text and text not in {"•", "·", "PDF"}:
            yield {"text": text, "level": _heading_level(node), "anchor": _anchor(node), "line": node.line}
        return
    run = []

    def flush():
        text = " ".join("".join(_flatten(child) for child in run).split()).strip(" |")
        run.clear()
        return {"text": text, "level": None, "anchor": _anchor(node), "line": node.line} if text and text not in {"•", "·", "PDF"} else None

    for child in node.children:
        if _has_block(child):
            block = flush()
            if block:
                yield block
            yield from _blocks(child)
        else:
            run.append(child)
    block = flush()
    if block:
        yield block


def _contains(node, tag):
    return isinstance(node, _Element) and (node.tag == tag or any(_contains(child, tag) for child in node.children))


def _locator(block):
    return "html:#" + block["anchor"] if block["anchor"] else f"html:line={block['line']}"


def _text_chunks(text):
    """Disjoint character spans preserve the complete text within search budget."""
    start = 0
    while start < len(text):
        end = min(start + MAX_SECTION_CHARS, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if boundary >= start + MAX_SECTION_CHARS // 2:
                end = boundary + 1
        yield start, end, text[start:end]
        start = end


def extract_tutorial_objects(path, html=None, *, root=None, source_sha256=None):
    """Extract one nonoverlapping sequence of tutorial chapters and GUI steps.

    No full-document object is added alongside those sections. Unrecognized or
    short referral pages fall back to one complete topic, so valid text survives
    even when COMSOL's usual section conventions are absent.
    """
    check_cancelled()
    path = Path(path).expanduser().resolve()
    identity = tutorial_identity(path)
    if identity is None or path.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("Canonical tutorial extraction requires a COMSOL tutorial HTML source")
    if html is None:
        raw = path.read_bytes()
        html = raw.decode("utf-8", errors="replace")
        source_sha256 = source_sha256 or hashlib.sha256(raw).hexdigest()
    source_sha256 = source_sha256 or _hash(path)
    parser, topic = _TutorialDOM(), TopicParser()
    for start in range(0, len(html), 65536):
        check_cancelled()
        parser.feed(html[start:start + 65536])
        topic.feed(html[start:start + 65536])
    parser.close()
    topic.close()
    title = topic.title.strip() or path.stem
    blocks = list(_blocks(parser.root))
    groups, current, instructions, title_seen = [], None, False, False
    structured = any(block["level"] in {2, 3} for block in blocks)
    for block in blocks if structured else []:
        check_cancelled()
        if block["level"] == 1 and not title_seen and block["text"] == title:
            title_seen = True
            continue
        starts_instructions = bool(block["level"] and _INSTRUCTIONS.search(block["text"]))
        if starts_instructions:
            instructions = True
        boundary = starts_instructions or block["level"] in ({1, 2, 3} if instructions else {1, 2})
        if boundary:
            current = {"title": block["text"], "heading": block, "blocks": [], "type": "method_step" if instructions else "case_context"}
            groups.append(current)
        else:
            if current is None:
                current = {"title": title, "heading": block, "blocks": [], "type": "case_context"}
                groups.append(current)
            current["blocks"].append(block)
    objects, chapters = [], []
    for group in groups:
        check_cancelled()
        heading = group["heading"]
        local_id = "tutorial-" + hashlib.sha256((identity + "\0" + group["type"] + "\0" + _locator(heading)).encode()).hexdigest()[:24]
        # Repeated unanchored headings on one physical HTML line still need
        # separate, deterministic identities.
        if any(chapter["local_id"] == local_id for chapter in chapters):
            local_id += "-" + str(len(chapters) + 1)
        if not group["blocks"]:
            chapters.append({"title": group["title"], "category": "procedure" if group["type"] == "method_step" else "context",
                             "locator": _locator(heading), "local_id": local_id, "local_ids": []})
            continue
        text = "\n".join(block["text"] for block in group["blocks"])
        spans, offset = [], 0
        for block in group["blocks"]:
            spans.append((offset, offset + len(block["text"]), block))
            offset += len(block["text"]) + 1
        chunks = list(_text_chunks(text))
        local_ids, branch = [], None
        for part, (start, end, chunk) in enumerate(chunks, 1):
            check_cancelled()
            chunk_id = local_id + f"-part-{part}"
            locators = list(dict.fromkeys([_locator(heading), *[_locator(block) for first, last, block in spans if first < end and last > start]]))
            item = {"local_id": chunk_id, "type": group["type"], "kind": "manual", "title": group["title"], "text": chunk,
                    "data": {"tutorial_identity": identity, "section": group["title"], "tutorial_order": len(objects) + 1,
                             "chapter_order": len(chapters) + 1, "part": part, "parts": len(chunks),
                             "status": "documented", "model_agreement": "not_verified"},
                    "evidence": [{"asset_path": str(path), "locator": locator, "method": "tutorial_html_section", "status": "documented"} for locator in locators],
                    "relations": []}
            branch = object_feature_branch(item)
            if branch:
                item["data"]["feature_branch"] = branch
            objects.append(item)
            local_ids.append(chunk_id)
        chapters.append({"title": group["title"], "category": branch or ("procedure" if group["type"] == "method_step" else "context"),
                         "locator": _locator(heading), "local_id": local_id, "local_ids": local_ids})
    fallback = not objects
    if fallback:
        chunks = list(_text_chunks(topic.content())) or [(0, 0, "")]
        objects = [{"local_id": "topic" if len(chunks) == 1 else f"topic-part-{part}", "type": "manual_topic", "kind": "manual", "title": title, "text": text,
                    "data": {"tutorial_identity": identity, "part": part, "parts": len(chunks)},
                    "evidence": [{"asset_path": str(path), "method": "html-topic-parser", "locator": "document", "status": "documented"}], "relations": []}
                   for part, (_, _, text) in enumerate(chunks, 1)]
    alternate_sources = [{"source_uri": pdf.as_uri(), "asset_path": str(pdf), "source_sha256": _hash(pdf), "format": "pdf", "relation": "alternate_format"}
                         for pdf in companion_pdf_paths(path, root=root)]
    metadata = tutorial_reference(path, source_sha256=source_sha256, title=title, chapters=chapters)
    metadata.update(status="canonical_document", parser_version=PARSER_VERSION, alternate_sources=alternate_sources,
                    aliases=[item["source_uri"] for item in alternate_sources], links=parser.links, images=list(dict.fromkeys(parser.images)))
    # Document-level metadata lives once, not in every indexed section.
    objects[0]["data"]["tutorial"] = metadata
    for ordinal, item in enumerate(objects, 1):
        item["data"]["canonical_tutorial"] = {"schema": PARSER_VERSION, "source_sha256": source_sha256,
                                                "ordinal": ordinal, "text_sha256": hashlib.sha256(item["text"].encode("utf-8")).hexdigest()}
    return {"objects": objects, "tutorial": metadata,
            "coverage": {"text": True, "ocr": False, "canonical_tutorial": True, "section_count": len(objects),
                         "full_topic_fallback": fallback, "alternate_source_count": len(alternate_sources)}, "warnings": []}
