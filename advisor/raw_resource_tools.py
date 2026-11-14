"""Allowlisted original-file tools; no catalog, index, route inference or execution.

Roots locate the mount. With the default strict_files=True they grant no file
access beyond the explicit files list. All permissions are rechecked on use.
"""
from __future__ import annotations

from collections import OrderedDict
import codecs
import hashlib
import io
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import threading
import time
import xml.etree.ElementTree as ET

MAX_BYTES = 7000
MAX_TEXT_BYTES = 32 * 1024 * 1024
MAX_MPH_BYTES = 1024 * 1024 * 1024
TOOL_NAMES = frozenset({"list_original_files", "search_original_files", "read_original_file", "inspect_original_mph"})
_TEXT = frozenset({".html", ".htm", ".xml", ".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".css", ".js", ".java", ".m", ".log", ".yaml", ".yml", ".ini", ".cfg"})


def tool_definitions():
    path = {"type": "string", "minLength": 1, "maxLength": 4096}
    offset = {"type": "integer", "minimum": 0, "default": 0}
    limit = {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}
    length = {"type": "integer", "minimum": 1, "maximum": 3200, "default": 1800}
    definitions = [
        ("list_original_files", "List immediate approved original files/directories at an original path; omit path to see mount roots. Roots are navigation only with the default exact file allowlist. Offset pages actual visible directory entries. No source-reading credit.", [],
         {"path": path, "offset": offset, "limit": limit}),
        ("search_original_files", "Search approved original filenames or ordinary text, without a retrieval index. Each hit represents one file, with a short first-match sample. Offset/next_offset index the current approved FILE list, not hit count; an empty page can have a next_offset. Path mode searches the complete in-memory allowlist; text mode scans at most 200 files per call, so narrow path for efficiency. Regex supports literals, groups, alternatives, classes and anchors; repetition, lookaround and backreferences are rejected. MPH binary content is inspected separately.", ["query"],
         {"query": {"type": "string", "minLength": 1, "maxLength": 300}, "path": path,
          "regex": {"type": "boolean", "default": False}, "case_sensitive": {"type": "boolean", "default": False},
          "search_in": {"type": "string", "enum": ["text", "path"], "default": "text"}, "offset": offset, "limit": limit}),
        ("read_original_file", "Read an approved original text file as Unicode source, or one PDF page's ordinary extracted text (page is 1-based). Offset/next_offset page the exact text representation. HTML is source text, not a generated chapter tree. PDF images/formulas are not OCR'd; PDF page text extraction is not visual reading. Path/hash/page/range locate evidence independently of any indexed-source ledger.", ["path"],
         {"path": path, "offset": offset, "length": length, "page": {"type": "integer", "minimum": 1, "default": 1},
          "encoding": {"type": "string", "enum": ["auto", "utf-8", "utf-16", "utf-32", "cp1252", "gb18030"], "default": "auto"}}),
        ("inspect_original_mph", "Read and parse the CURRENT approved original MPH offline, without COMSOL or any built compact/catalog cache. Default children view follows the unfiltered saved XML hierarchy. node_path is a zero-based XML child route such as /0/2; / is the member root. XML view at / pages the original decoded member text; at another node it pages an explicitly serialized XML subtree. No stage/route inference or effective-activation/physical validation. The first parse time and session-local stat-cache reuse are reported; binary mesh/solution members are not opened.", ["path"],
         {"path": path, "node_path": {"type": "string", "maxLength": 2048, "default": "/"},
          "member": {"type": "string", "enum": ["dmodel.xml", "modelinfo.xml"], "default": "dmodel.xml"},
          "view": {"type": "string", "enum": ["children", "xml"], "default": "children"},
          "offset": offset, "limit": limit, "length": length}),
    ]
    return [{"name": name, "description": description, "inputSchema": {"type": "object", "additionalProperties": False,
            "required": required, "properties": properties}} for name, description, required, properties in definitions]


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _path(value):
    if not isinstance(value, (str, os.PathLike)) or not str(value) or "\x00" in str(value):
        raise ValueError("Use an original local path")
    value = str(value)
    candidate = Path(value)
    windows = PureWindowsPath(value)
    if ".." in candidate.parts or ".." in windows.parts or value.startswith(("\\\\", "//")):
        raise PermissionError("Traversal and network paths are not allowed")
    if windows.drive and not windows.is_absolute() or ":" in value.removeprefix(windows.drive):
        raise PermissionError("Drive-relative paths, URLs and alternate streams are not allowed")
    return candidate.absolute()


def _fingerprint(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _decode(data, encoding="auto"):
    if encoding == "auto":
        encoding = next((name for bom, name in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
            (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"), (codecs.BOM_UTF8, "utf-8-sig")) if data.startswith(bom)), None)
        if encoding is None:
            declaration = re.search(br"(?:charset\s*=\s*[\"']?|encoding\s*=\s*[\"'])([A-Za-z0-9_.-]+)", data[:8192], re.I)
            encoding = declaration[1].decode("ascii") if declaration else "utf-8"
    if not isinstance(encoding, str) or len(encoding) > 80:
        raise ValueError("Unsupported text encoding")
    try:
        text = data.decode(encoding, errors="strict")
    except (LookupError, UnicodeError) as exc:
        raise ValueError("Original text cannot be decoded losslessly with this encoding") from exc
    if "\x00" in text:
        raise ValueError("Binary content is not ordinary text")
    return text, encoding


class RawResourceTools:
    def __init__(self, files=(), roots=(), excluded=(), *, strict_files=True, max_bytes=MAX_BYTES, denied_hashes=()):
        if type(strict_files) is not bool or type(max_bytes) is not int or not 1500 <= max_bytes <= MAX_BYTES:
            raise ValueError("Use a boolean strict_files and a response budget in 1500..7000 bytes")
        self.files = frozenset(_path(path) for path in files)
        self.roots = tuple(dict.fromkeys(_path(path) for path in roots))
        self.excluded = tuple(_path(path) for path in excluded)
        self.denied_hashes = frozenset(str(value).lower() for value in denied_hashes)
        if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in self.denied_hashes):
            raise ValueError("denied_hashes must contain complete SHA256 digests")
        self.strict_files, self.max_bytes = strict_files, max_bytes
        root_set = set(self.roots)
        self._mounts = self.roots + tuple(sorted({path.parent for path in self.files
            if path not in root_set and root_set.isdisjoint(path.parents)}, key=str))
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self.audit = []

    @staticmethod
    def tool_definitions():
        return tool_definitions()

    def _excluded(self, path):
        return any(path.is_relative_to(excluded) for excluded in self.excluded)

    def _guard(self, value, *, directory=False):
        path = _path(value)
        if self._excluded(path):
            raise PermissionError("Original path is excluded")
        permitted = (path in self.files or not self.strict_files and any(path.is_relative_to(root) for root in self.roots))
        if directory:
            permitted = (path in self._mounts or any(file.is_relative_to(path) for file in self.files)
                         and any(path.is_relative_to(root) for root in self._mounts)
                         or not self.strict_files and any(path.is_relative_to(root) for root in self.roots))
        if not permitted:
            raise PermissionError("Original path is outside the approved file scope")
        for component in (*reversed(path.parents), path):
            attributes = component.lstat()
            if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise PermissionError("Symbolic links, junctions and reparse paths are excluded")
        attributes = path.stat()
        if directory and not stat.S_ISDIR(attributes.st_mode) or not directory and not stat.S_ISREG(attributes.st_mode):
            raise ValueError("Expected an original directory" if directory else "Expected an original regular file")
        return path, attributes

    def _read_bytes(self, value, *, maximum=MAX_TEXT_BYTES):
        path, before = self._guard(value)
        if before.st_size > maximum:
            raise ValueError("Original file exceeds this reader's explicit size limit")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            # Windows path stat and fstat expose different historical ctime
            # semantics. Compare file identity/size/mtime across those APIs;
            # compare full fingerprints within each API before/after the read.
            if _fingerprint(opened)[:4] != _fingerprint(before)[:4]:
                raise PermissionError("Original file identity changed before reading")
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        current = self._guard(path)[1]
        if (len(data) > maximum or _fingerprint(before) != _fingerprint(current)
                or _fingerprint(opened) != _fingerprint(after) or _fingerprint(after)[:4] != _fingerprint(current)[:4]):
            raise PermissionError("Original file changed during reading")
        if hashlib.sha256(data).hexdigest() in self.denied_hashes:
            raise PermissionError("Original content is excluded by its SHA256")
        return path, data, before

    def _files(self, value=None):
        selected = _path(value) if value is not None else None
        if selected is not None:
            self._guard(selected, directory=selected not in self.files and selected.is_dir())
        candidates = self.files
        if not self.strict_files:
            candidates = set(candidates)
            for root in self.roots:
                self._guard(root, directory=True)
                for current, directories, names in os.walk(root, followlinks=False):
                    directories[:] = [name for name in directories if self._visible(Path(current) / name, directory=True)]
                    candidates.update(Path(current) / name for name in names)
        return sorted((path for path in candidates if not self._excluded(path)
            and (selected is None or path == selected or path.is_relative_to(selected))), key=lambda item: str(item).casefold())

    def _visible(self, path, *, directory=False):
        try:
            self._guard(path, directory=directory)
            return True
        except (PermissionError, ValueError, OSError):
            return False

    def _base(self, **values):
        return {"schema": "raw-resource-tools/v1", **values, "is_read_evidence": False,
                "evidence_system": "original_path_ranges; independent of indexed-source read ledgers"}

    def _page(self, items, offset, limit, **values):
        result = self._base(**values, offset=offset, limit=limit, total=len(items), items=[], returned=0,
                            next_offset=None, has_more=False)
        for item in items[offset:offset + limit]:
            result["items"].append(item)
            if _size(result) > self.max_bytes - 100:
                result["items"].pop()
                break
        if offset < len(items) and not result["items"]:
            raise ValueError("One original-file metadata item exceeds the response budget")
        end = offset + len(result["items"])
        result.update(returned=len(result["items"]), next_offset=end if end < len(items) else None, has_more=end < len(items))
        return result

    def _text_page(self, text, offset, length, **values):
        if offset > len(text):
            raise ValueError("Text offset is beyond the original representation")
        def result(count):
            end = offset + count
            return self._base(**values, offset=offset, end_offset=end, total_characters=len(text),
                text=text[offset:end], returned_length=count, requested_length=length,
                next_offset=end if end < len(text) else None, partial=end < len(text) or offset > 0,
                range={"unit": "Unicode characters", "start": offset, "end": end,
                       "start_line": text.count("\n", 0, offset) + 1, "end_line": text.count("\n", 0, max(offset, end - 1)) + 1})
        low, high = 0, min(length, len(text) - offset)
        while low < high:
            middle = (low + high + 1) // 2
            if _size(result(middle)) <= self.max_bytes:
                low = middle
            else:
                high = middle - 1
        page = result(low)
        if _size(page) > self.max_bytes or low == 0 and offset < len(text):
            raise ValueError("Original-file identity metadata exceeds the response budget")
        return page

    def _list(self, args):
        selected = args.get("path")
        if selected is None:
            candidates = set(self._mounts)
        else:
            selected = self._guard(selected, directory=True)[0]
            candidates = set()
            for path in self._files(selected):
                relative = path.relative_to(selected)
                if relative.parts:
                    candidates.add(selected / relative.parts[0])
        items = []
        for candidate in sorted(candidates, key=lambda path: str(path).casefold()):
            directory = candidate.is_dir()
            try:
                path, attributes = self._guard(candidate, directory=directory)
            except (PermissionError, ValueError, OSError):
                continue
            items.append({"path": str(path), "kind": "directory" if directory else "file",
                          "size_bytes": None if directory else attributes.st_size})
        return self._page(items, args.get("offset", 0), args.get("limit", 8),
                          path=str(selected) if selected is not None else None, scope="approved_immediate_directory_entries")

    def _cached(self, path, kind, loader):
        path, before = self._guard(path)
        key = (kind, path)
        with self._lock:
            previous = self._cache.get(key)
            if previous and previous[0] == _fingerprint(before):
                self._cache.move_to_end(key)
                return previous[1], True
            start = time.perf_counter()
            value = loader(path)
            after = self._guard(path)[1]
            if _fingerprint(before) != _fingerprint(after):
                raise PermissionError("Original file changed during on-demand parsing")
            value["first_parse_elapsed_ms"] = round((time.perf_counter() - start) * 1000, 3)
            value["_fingerprint"] = _fingerprint(after)
            self._cache[key] = (_fingerprint(after), value)
            self._cache.move_to_end(key)
            while len(self._cache) > 4:
                self._cache.popitem(last=False)
            return value, False

    def _pdf(self, path, page):
        def load(source):
            _, data, _ = self._read_bytes(source)
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise ValueError("Encrypted PDFs are not supported by ordinary original-file reading")
            return {"reader": reader, "sha256": hashlib.sha256(data).hexdigest(), "pages": {}}
        try:
            parsed, reused = self._cached(path, "pdf", load)
        except ImportError as exc:
            raise ValueError("Ordinary PDF text reading requires the installed pypdf runtime") from exc
        if page > len(parsed["reader"].pages):
            raise ValueError("PDF page is beyond the original document")
        start = time.perf_counter()
        with self._lock:
            if page not in parsed["pages"]:
                parsed["pages"][page] = parsed["reader"].pages[page - 1].extract_text() or ""
            text = parsed["pages"][page]
        if _fingerprint(self._guard(path)[1]) != parsed["_fingerprint"]:
            raise PermissionError("Original PDF changed during text extraction")
        return text, {"sha256": parsed["sha256"], "page": page, "page_count": len(parsed["reader"].pages),
            "next_page": page + 1 if page < len(parsed["reader"].pages) else None,
            "read_scope": "ordinary_pdf_page_text", "image_content_not_transcribed": True,
            "extraction_limit": "pypdf text only; images, layout and unencoded formulas are not interpreted",
            "first_parse_elapsed_ms": parsed["first_parse_elapsed_ms"], "cache_reused": reused,
            "page_text_elapsed_ms": round((time.perf_counter() - start) * 1000, 3)}

    def _read(self, args):
        path = self._guard(args["path"])[0]
        if path.suffix.casefold() == ".pdf":
            text, metadata = self._pdf(path, args.get("page", 1))
        else:
            if args.get("page", 1) != 1:
                raise ValueError("page is only meaningful for PDF originals")
            if path.suffix.casefold() not in _TEXT:
                raise ValueError("Use inspect_original_mph for MPH; this reader supports ordinary text and PDF pages")
            _, data, _ = self._read_bytes(path)
            text, encoding = _decode(data, args.get("encoding", "auto"))
            metadata = {"sha256": hashlib.sha256(data).hexdigest(), "encoding": encoding, "read_scope": "original_text_source"}
        return self._text_page(text, args.get("offset", 0), args.get("length", 1800), path=str(path), **metadata)

    @staticmethod
    def _matcher(query, regex, case_sensitive):
        if regex:
            # A repetition-free regex has bounded backtracking. Do not expose
            # catastrophic nested quantifiers through an otherwise read tool.
            escaped, in_class = False, False
            for char in query:
                if escaped:
                    if char.isdigit() or char in {"g", "k"}:
                        raise ValueError("Regex backreferences are not supported")
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "[":
                    in_class = True
                elif char == "]":
                    in_class = False
                elif not in_class and char in "*+?{}":
                    raise ValueError("Use repetition-free regex, or a literal text query")
            try:
                compiled = re.compile(query, 0 if case_sensitive else re.I)
            except re.error as exc:
                raise ValueError("Invalid bounded regular expression") from exc
            return lambda text: next(((match.start(), match.end()) for match in compiled.finditer(text)), None)
        if case_sensitive:
            return lambda text: (index, index + len(query)) if (index := text.find(query)) >= 0 else None
        # re.I preserves original Unicode character offsets; casefold does not.
        compiled = re.compile(re.escape(query), re.I)
        return lambda text: (match.start(), match.end()) if (match := compiled.search(text)) else None

    def _search(self, args):
        matcher = self._matcher(args["query"], args.get("regex", False), args.get("case_sensitive", False))
        paths, offset, limit = self._files(args.get("path")), args.get("offset", 0), args.get("limit", 8)
        if offset > len(paths):
            raise ValueError("Search file cursor is beyond the approved file list")
        result = self._base(query=args["query"], search_in=args.get("search_in", "text"), regex=args.get("regex", False),
            offset=offset, limit=limit, cursor_scope="approved_file_list", total_files=len(paths), items=[], returned=0,
            scanned_files=0, unavailable_files=0, unsupported_files=0, next_offset=None, has_more=False)
        cursor = offset
        path_search = args.get("search_in", "text") == "path"
        while cursor < len(paths) and (path_search or result["scanned_files"] < 200) and len(result["items"]) < limit:
            path = paths[cursor]
            try:
                hit, metadata = None, {}
                if path_search:
                    text = str(path)
                    hit = matcher(text)
                    if not hit:
                        cursor += 1
                        result["scanned_files"] += 1
                        continue
                self._guard(path)
                if not path_search and path.suffix.casefold() in _TEXT:
                    _, data, _ = self._read_bytes(path)
                    text, _ = _decode(data)
                    hit = matcher(text)
                elif not path_search and path.suffix.casefold() == ".pdf":
                    page = 1
                    while True:
                        text, metadata = self._pdf(path, page)
                        hit = matcher(text)
                        if hit or metadata["next_page"] is None:
                            break
                        page += 1
                elif not path_search:
                    result["unsupported_files"] += 1
                if hit:
                    start, end = hit
                    left, right = max(0, start - 90), min(len(text), start + 230)
                    item = {"path": str(path), "match_start": start, "match_end": end,
                        "line": text.count("\n", 0, start) + 1, "excerpt": text[left:right],
                        "excerpt_range": {"start": left, "end": right}, "view": "first_match_sample_only"}
                    if "page" in metadata:
                        item["page"] = metadata["page"]
                    result["items"].append(item)
                    if _size(result) > self.max_bytes - 100:
                        result["items"].pop()
                        if not result["items"]:
                            raise ValueError("One search metadata item exceeds the response budget")
                        break
            except (PermissionError, FileNotFoundError):
                result["unavailable_files"] += 1
            cursor += 1
            result["scanned_files"] += 1
        result.update(returned=len(result["items"]), next_offset=cursor if cursor < len(paths) else None, has_more=cursor < len(paths))
        return result

    def _mph(self, path):
        def load(source):
            from .model_reader import _safe_container
            before = self._guard(source)[1]
            if before.st_size > MAX_MPH_BYTES:
                raise ValueError("Original MPH exceeds the explicit on-demand size limit")
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() in self.denied_hashes:
                raise PermissionError("Original content is excluded by its SHA256")
            metadata, names = _safe_container(source)
            roots = {}
            for member, data in metadata.items():
                try:
                    roots[member] = ET.fromstring(data)
                except ET.ParseError as exc:
                    raise ValueError("Original saved XML is malformed") from exc
            return {"sha256": digest.hexdigest(), "members": metadata, "roots": roots,
                    "member_count": len(names), "source_size_bytes": before.st_size}
        return self._cached(path, "mph", load)

    @staticmethod
    def _summary(element, route):
        identity = {key: value for key, value in element.attrib.items() if key in {"tag", "name", "op"}}
        item = {"node_path": route, "element": element.tag, "identity_attributes": identity,
            "attribute_count": len(element.attrib), "child_count": len(element), "text_characters": len(element.text or ""),
            "content_view": "inspect_original_mph with this node_path and view=xml"}
        if _size(item) > 1400:
            item = {"node_path": route, "child_count": len(element), "attribute_count": len(element.attrib),
                    "identity_omitted_from_navigation": True, "content_view": "inspect_original_mph view=xml"}
        return item

    def _inspect(self, args):
        path = self._guard(args["path"])[0]
        if path.suffix.casefold() != ".mph":
            raise ValueError("Original MPH inspection requires an approved .mph file")
        parsed, reused = self._mph(path)
        member, route = args.get("member", "dmodel.xml"), args.get("node_path", "/") or "/"
        if not re.fullmatch(r"/(?:[0-9]+(?:/[0-9]+)*)?", route):
            raise ValueError("node_path must be a zero-based XML child path such as /0/2")
        if member not in parsed["roots"]:
            raise ValueError("This original MPH has no requested saved XML member")
        element = parsed["roots"][member]
        try:
            for index in route.strip("/").split("/") if route != "/" else ():
                element = element[int(index)]
        except IndexError as exc:
            raise ValueError("Saved XML node_path is outside the original member") from exc
        metadata = {"path": str(path), "sha256": parsed["sha256"], "member": member, "node_path": route,
            "first_parse_elapsed_ms": parsed["first_parse_elapsed_ms"], "cache_reused": reused,
            "parse_elapsed_ms": 0 if reused else parsed["first_parse_elapsed_ms"],
            "read_scope": "saved_native_xml_only", "physical_validation": "not_evaluated",
            "effective_activation": "not_evaluated", "binary_mesh_or_solution_read": False}
        if args.get("view", "children") == "xml":
            if route == "/":
                text, encoding = _decode(parsed["members"][member])
                metadata.update(representation="original_decoded_xml_member", encoding=encoding)
            else:
                text = ET.tostring(element, encoding="unicode")
                metadata.update(representation="serialized_xml_subtree", original_markup_layout_preserved=False)
            result = self._text_page(text, args.get("offset", 0), args.get("length", 1800), **metadata)
        else:
            children = [self._summary(child, route.rstrip("/") + "/" + str(index)) for index, child in enumerate(element)]
            result = self._page(children, args.get("offset", 0), args.get("limit", 8),
                node=self._summary(element, route), scope="unfiltered_native_xml_children_metadata_only", **metadata)
        if _fingerprint(self._guard(path)[1]) != parsed["_fingerprint"]:
            raise PermissionError("Original MPH changed during inspection")
        return result

    def call(self, name, arguments):
        if name not in TOOL_NAMES or not isinstance(arguments, dict):
            raise ValueError("Unknown original-resource tool or invalid arguments")
        definition = next(item for item in tool_definitions() if item["name"] == name)
        properties = definition["inputSchema"]["properties"]
        if set(arguments) - properties.keys() or any(key not in arguments for key in definition["inputSchema"]["required"]):
            raise ValueError("Arguments do not match the original-resource tool contract")
        for key, value in arguments.items():
            contract = properties[key]
            if contract["type"] == "integer":
                if type(value) is not int or value < contract.get("minimum", 0) or value > contract.get("maximum", 2**63 - 1):
                    raise ValueError("Original-resource page arguments are outside their contract")
            elif contract["type"] == "boolean" and type(value) is not bool:
                raise ValueError("Expected a boolean original-resource argument")
            elif contract["type"] == "string" and (not isinstance(value, str) or len(value) < contract.get("minLength", 0)
                    or len(value) > contract.get("maxLength", 2**31) or "enum" in contract and value not in contract["enum"]):
                raise ValueError("Invalid original-resource string argument")
        start = time.perf_counter()
        result = {"list_original_files": self._list, "search_original_files": self._search,
                  "read_original_file": self._read, "inspect_original_mph": self._inspect}[name](arguments)
        if _size(result) > self.max_bytes:
            raise ValueError("Original-resource response exceeds its explicit byte budget")
        with self._lock:
            self.audit.append({"tool": name, "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
                "output_bytes": _size(result), "path": arguments.get("path"),
                "cache_reused": result.get("cache_reused"), "first_parse_elapsed_ms": result.get("first_parse_elapsed_ms")})
        return result
