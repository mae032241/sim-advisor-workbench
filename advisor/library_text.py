"""Deterministic document extraction for registered library sources."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from .build_control import check_cancelled
from .knowledge import TopicParser, _read_tocs
from .tutorial_sources import extract_tutorial_objects, tutorial_identity


_SESSION_TOCS: ContextVar[dict | None] = ContextVar("sim_advisor_text_tocs", default=None)


@contextmanager
def text_extraction_session():
    """Reuse document hierarchies within one build, then discard them.

    COMSOL roots contain thousands of TOCs. Reading all of them for every
    HTML topic makes a full build quadratic. A build takes one hierarchy
    snapshot per root; a later build sees changed, added, or removed TOCs.
    Standalone extraction remains uncached and sees current files immediately.
    """
    token = _SESSION_TOCS.set({})
    try:
        yield
    finally:
        _SESSION_TOCS.reset(token)


def _topic_hierarchy(root):
    cache = _SESSION_TOCS.get()
    if cache is None:
        return _read_tocs(root)
    if root not in cache:
        check_cancelled()
        cache[root] = _read_tocs(root)
    return cache[root]


def extract_text_objects(path, root=None):
    check_cancelled()
    path = Path(path)
    suffix = path.suffix.lower()
    evidence = {"asset_path": str(path.resolve()), "method": "plain-text", "status": "documented"}
    if suffix in {".html", ".htm"}:
        if tutorial_identity(path):
            result = extract_tutorial_objects(path, root=root)
            if root is not None:
                boundary = Path(root).resolve()
                relative = path.resolve().relative_to(boundary).as_posix()
                hierarchy = _topic_hierarchy(boundary).get(relative, {})
                for item in result["objects"]:
                    item["data"].update(hierarchy)
            return result
        html = path.read_text(encoding="utf-8", errors="replace")
        parser = TopicParser()
        parser.feed(html)
        title = parser.title.strip() or path.stem
        text = parser.content()
        evidence.update(method="html-topic-parser", locator="document")
        data = {"headings": parser.headings, "links": parser.links}
        if root is not None:
            root = Path(root).resolve()
            relative = path.resolve().relative_to(root).as_posix()
            data.update(_topic_hierarchy(root).get(relative, {}))
        # Image locations remain available even when equations are image-only.
        import re
        data["images"] = re.findall(r'<img\b[^>]*\bsrc=[\"\']([^\"\']+)', html, re.I)
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError("PDF parser unavailable; install pypdf or use the matching HTML source") from None
        reader = PdfReader(path)
        try:
            objects = []
            page_count = len(reader.pages)
            for index, page in enumerate(reader.pages):
                check_cancelled()
                text = page.extract_text() or ""
                if text.strip():
                    objects.append({"local_id": f"page-{index+1}", "type": "manual_topic", "kind": "manual",
                                    "title": f"{path.stem} · {index+1}", "text": text, "data": {"page": index+1},
                                    "evidence": [dict(evidence, method="pypdf", locator=f"page:{index+1}")]})
            return {"objects": objects, "coverage": {"text": True, "ocr": False, "pages": page_count}, "warnings": ["Equation images and diagrams are preserved in the source; no OCR is performed."]}
        finally:
            reader.close()
    elif suffix in {".md", ".txt"}:
        title, text, data = path.stem, path.read_text(encoding="utf-8"), {}
        evidence["locator"] = "lines:1-end"
    else:
        raise ValueError("Unsupported document format")
    api = "_api_" in path.name or ".help.comsol/api/" in path.as_posix()
    return {"objects": [{"local_id": "topic", "type": "api_reference" if api else "manual_topic", "kind": "code" if api else "manual", "title": title,
                         "text": text, "data": data, "evidence": [evidence]}],
            "coverage": {"text": True, "ocr": False}, "warnings": []}
