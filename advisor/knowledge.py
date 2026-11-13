"""COMSOL HTML parsing and permission-checked historical source archives.

The registered library catalog and QMD are the only retrieval path. Existing
HTML hierarchy and source IDs remain available for historical citation reads.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .retrieval_backends import backend_status

DEFAULT_DOC_ROOT = Path("D:/comsol/COMSOL63/Multiphysics/doc/help/wtpwebapps/ROOT/doc")
SCHEMA_VERSION = "3"
OFFICIAL_BASE = "https://doc.comsol.com/{version}/doc/"

# Explicit, inspectable terminology translation. These are query expansion
# rules, never evidence or substituted document text.
ALIASES = {
    "傳熱": "heat transfer", "传热": "heat transfer", "熱傳": "heat transfer",
    "热传": "heat transfer", "熱通量": "heat flux", "热通量": "heat flux",
    "熱源": "heat source", "热源": "heat source", "熱傳導": "heat conduction",
    "熱膨脹": "thermal expansion", "热膨胀": "thermal expansion",
    "熱應力": "thermal stress", "热应力": "thermal stress",
    "热传导": "heat conduction", "對流": "convection", "对流": "convection",
    "輻射": "radiation", "辐射": "radiation", "散熱器": "heat sink",
    "散热器": "heat sink", "溫度": "temperature", "温度": "temperature",
    "強制對流": "forced convection", "强制对流": "forced convection",
    "共軛傳熱": "conjugate heat transfer", "共轭传热": "conjugate heat transfer",
    "固體力學": "solid mechanics", "固体力学": "solid mechanics",
    "結構": "structural mechanics", "结构": "structural mechanics",
    "應力": "stress", "应力": "stress", "應變": "strain", "应变": "strain",
    "懸臂梁": "cantilever beam", "悬臂梁": "cantilever beam",
    "支架": "bracket", "靜力": "static analysis", "静力": "static analysis",
    "頻率響應": "frequency response", "频率响应": "frequency response",
    "模態": "eigenfrequency", "模态": "eigenfrequency", "屈曲": "buckling",
    "疲勞": "fatigue", "疲劳": "fatigue", "振動": "vibration", "振动": "vibration",
    "固定約束": "fixed constraint", "固定约束": "fixed constraint",
    "位移": "displacement", "接觸": "contact", "接触": "contact",
    "層流": "laminar flow", "层流": "laminar flow", "紊流": "turbulent flow",
    "湍流": "turbulent flow", "流體": "fluid flow", "流体": "fluid flow",
    "壓力": "pressure", "压力": "pressure", "入口": "inlet", "出口": "outlet",
    "不可壓縮": "incompressible", "不可压缩": "incompressible",
    "流固耦合": "fluid structure interaction", "閥": "valve", "阀": "valve",
    "隔膜": "diaphragm", "薄隙": "thin gap", "動網格": "moving mesh ALE",
    "动网格": "moving mesh ALE", "變形網格": "deformed mesh", "变形网格": "deformed mesh",
    "網格": "mesh", "网格": "mesh", "邊界層": "boundary layer",
    "边界层": "boundary layer", "掃掠": "swept mesh", "扫掠": "swept mesh",
    "收斂": "convergence solver", "收敛": "convergence solver",
    "穩態": "stationary", "稳态": "stationary", "暫態": "time dependent",
    "瞬態": "time dependent", "瞬态": "time dependent", "時間相依": "time dependent",
    "參數掃描": "parametric sweep", "参数扫描": "parametric sweep",
    "電磁": "electromagnetic", "电磁": "electromagnetic",
    "電流": "electric currents", "电流": "electric currents",
    "壓電": "piezoelectric", "压电": "piezoelectric", "聲學": "acoustics",
    "声学": "acoustics", "擴散": "diffusion", "扩散": "diffusion",
    "稀物質傳遞": "transport diluted species", "稀物质传递": "transport diluted species",
    "程式": "programming API Java", "代码": "code API Java", "代碼": "code API Java",
    "範例": "example", "案例": "example", "教程": "tutorial",
    "cht": "conjugate heat transfer", "fsi": "fluid structure interaction",
    "ale": "moving mesh ALE", "spf": "laminar flow", "ht": "heat transfer",
}
STOP_WORDS = set("a an and are as at be by can comsol do for from how i in into is it model modeling modelling of on or please simulation the this to use using want with you your set setup simulate need create add example tutorial study".split())
PHYSICS_PACKAGES = {
    "heat": "heat", "cfd": "flow", "mfl": "flow", "spf": "flow",
    "sme": "structure", "mbd": "structure", "nsm": "structure", "fatigue": "structure",
    "acdc": "electromagnetics", "rf": "electromagnetics", "woptics": "optics",
    "aco": "acoustics", "chem": "chemical", "chemsptrans": "chemical",
    "bfc": "flow", "mixer": "flow", "polymer": "flow", "porous": "flow",
}
PHYSICS_TERMS = {
    "heat": ("heat transfer", "heat flux", "heat sink", "convection", "temperature", "radiation"),
    "flow": ("laminar flow", "turbulent", "fluid flow", "inlet", "outlet", "pressure", "valve"),
    "structure": ("solid mechanics", "structural", "stress", "cantilever", "displacement", "strain", "thermal expansion", "thermoelastic"),
    "mesh": ("mesh", "ale", "moving mesh"),
    "electromagnetics": ("electric", "magnetic", "electromagnetic"),
    "chemical": ("diffusion", "species", "reaction"), "acoustics": ("acoustic", "sound"),
}


def source_id(relative_path: str) -> str:
    return "cs-" + hashlib.sha256(relative_path.replace("\\", "/").encode()).hexdigest()[:24]


def expand_query(query: str) -> dict:
    lower = query.lower()
    expansions = [value for key, value in ALIASES.items()
                  if (key in lower if not key.isascii() else re.search(r"\b" + re.escape(key) + r"\b", lower))]
    # Keep complete technical phrases in addition to their individual tokens.
    phrases = list(dict.fromkeys(expansions))
    english_query = " ".join(re.findall(r"[a-z][a-z0-9_-]*", lower))
    if 1 < len(english_query.split()) <= 7 and len(english_query) < 100:
        phrases.append(english_query)
    expanded = lower + " " + " ".join(phrases)
    terms = list(dict.fromkeys(t for t in re.findall(r"[a-z][a-z0-9_]{1,40}", expanded)
                               if t not in STOP_WORDS))[:40]
    physics = [key for key, values in PHYSICS_TERMS.items() if any(v in expanded for v in values)]
    focus = [phrase for phrase in ("heat sink", "bracket", "cantilever", "valve", "diaphragm", "busbar", "microchannel", "heat exchanger") if phrase in expanded]
    return {"original": query, "expanded": expanded, "terms": terms, "phrases": phrases, "physics": physics, "focus_phrases": focus}


class TopicParser(HTMLParser):
    """Extract real paragraphs and headings; ignore executable and style content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.headings: list[str] = []
        self.links: list[dict] = []
        self.title = ""
        self._ignored = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._heading: list[str] | None = None
        self._heading_depth = 0
        self._depth = 0
        self.has_code = False

    def handle_starttag(self, tag, attrs):
        self._depth += 1
        attr = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._ignored += 1
        if tag == "title":
            self._in_title = True
        if self._ignored:
            return
        classes = attr.get("class", "")
        if tag in ("div", "p", "tr", "li", "pre", "h1", "h2", "h3", "h4", "h5", "br"):
            self.parts.append("\n")
        if tag in ("td", "th"):
            self.parts.append(" | ")
        if tag in ("pre", "code") or re.search(r"(?:Code|Java|Monospace|Program)", classes):
            self.has_code = True
        if self._heading is None and (re.match(r"^h[1-6]$", tag) or re.search(r"(?:^|\s)(?:Head\d|ChapterTitle|Title)", classes)):
            self._heading = []
            self._heading_depth = self._depth
        if tag == "a" and attr.get("href") and attr.get("title"):
            self.links.append({"href": attr["href"], "title": attr["title"]})

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            self.title = " ".join("".join(self._title_parts).split())
        if self._heading is not None and self._depth == self._heading_depth:
            heading = " ".join("".join(self._heading).split())
            if heading and heading not in self.headings:
                self.headings.append(heading)
            self._heading = None
        if tag in ("script", "style", "noscript"):
            self._ignored = max(0, self._ignored - 1)
        if not self._ignored and tag in ("div", "p", "tr", "li", "pre", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")
        self._depth = max(0, self._depth - 1)

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
        elif not self._ignored:
            self.parts.append(data)
            if self._heading is not None:
                self._heading.append(data)

    def content(self) -> str:
        paragraphs = [" ".join(p.split()).strip(" |") for p in "".join(self.parts).splitlines()]
        return "\n\n".join(p for p in paragraphs if p and p != "•")


def _read_tocs(root: Path) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for path in sorted([root / "toc.xml", *root.glob("*/toc.xml")]):
        try:
            tree = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        root_label = tree.attrib.get("label", path.parent.name)

        def visit(node, labels, parent_rel=None):
            label = node.attrib.get("label", "")
            labels = labels + ([label] if label and (not labels or labels[-1] != label) else [])
            href = node.attrib.get("href", "")
            next_parent = parent_rel
            if href and ".html" in href:
                parsed = unquote(urlsplit(href).path)
                rel = parsed.lstrip("/") if parsed.startswith("/") else (path.parent.relative_to(root) / parsed).as_posix()
                if not rel.startswith("../"):
                    existing = mapping.get(rel)
                    info = {"section_path": labels, "parent_rel": parent_rel}
                    if not existing or len(labels) > len(existing["section_path"]):
                        mapping[rel] = info
                    next_parent = rel
            for child in node:
                if child.tag.rsplit("}", 1)[-1] == "topic":
                    visit(child, labels, next_parent)
        for topic in tree:
            if topic.tag.rsplit("}", 1)[-1] == "topic":
                visit(topic, [root_label])
    return mapping


def _excerpt(text: str, terms: list[str], max_chars: int = 850) -> str:
    paragraphs = text.split("\n\n")
    best = 0
    best_score = -1
    for i, para in enumerate(paragraphs):
        if len(para) < 45:
            continue
        lower = para.lower()
        score = sum(1 for term in terms if re.search(r"\b" + re.escape(term) + r"\b", lower))
        if score > best_score:
            best, best_score = i, score
    selected = paragraphs[best] if paragraphs else ""
    # Add useful adjacent paragraph only when it fits; never cut in mid-sentence.
    if best + 1 < len(paragraphs) and len(selected) + len(paragraphs[best + 1]) < max_chars:
        selected += "\n\n" + paragraphs[best + 1]
    if len(selected) > max_chars:
        sentences = re.split(r"(?<=[.!?])\s+", selected)
        complete = []
        for sentence in sentences:
            if complete and len(" ".join(complete + [sentence])) > max_chars:
                break
            complete.append(sentence)
        selected = " ".join(complete)
        if len(selected) > max_chars * 2:
            selected = selected[:max_chars].rsplit(" ", 1)[0] + " …"
    return selected


class KnowledgeBase:
    def __init__(self, data_dir: Path, doc_root: Path | None = None):
        self.data_dir = Path(data_dir)
        self.doc_root = Path(doc_root or os.environ.get("COMSOL_DOC_ROOT", DEFAULT_DOC_ROOT))
        self.db_path = self.data_dir / "knowledge.sqlite3"
        self._build_lock = threading.Lock()
        self._last_error: str | None = None
        self._building = False
        self.version = os.environ.get("COMSOL_DOC_VERSION", "6.3")
        inferred = re.search(r"COMSOL(\d)(\d)(?:[/\\]|$)", str(self.doc_root), re.I)
        if inferred:
            self.version = inferred[1] + "." + inferred[2]

    def _connect(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _schema(self, db):
        # Explicit archive rebuilds retain document IDs and hierarchy, while
        # retiring postings maintained by pre-QMD archive triggers.
        db.executescript("""
            DROP TRIGGER IF EXISTS doc_ai;
            DROP TRIGGER IF EXISTS doc_ad;
            DROP TRIGGER IF EXISTS doc_au;
            DROP TABLE IF EXISTS topics_fts;
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS documents(
              id TEXT PRIMARY KEY, relpath TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
              text TEXT NOT NULL, context TEXT, headings TEXT, section_path TEXT,
              kind TEXT, version TEXT, branch TEXT, package TEXT, physics TEXT,
              parent_id TEXT, links TEXT, source_mtime_ns INTEGER, source_size INTEGER);
            CREATE INDEX IF NOT EXISTS doc_kind ON documents(kind);
            CREATE INDEX IF NOT EXISTS doc_package ON documents(package);
        """)

    def build(self) -> dict:
        if not self.doc_root.is_dir():
            self._last_error = f"COMSOL documentation directory not found: {self.doc_root}"
            return self.status()
        with self._build_lock:
            self._building = True
            started = time.monotonic()
            failures = []
            indexed = skipped = 0
            try:
                hierarchy = _read_tocs(self.doc_root)
                with self._connect() as db:
                    self._schema(db)
                    old = {row["relpath"]: row for row in db.execute("SELECT relpath,source_mtime_ns,source_size FROM documents")}
                    previous = dict(db.execute("SELECT key,value FROM metadata"))
                    rebuild = previous.get("schema_version") not in (None, SCHEMA_VERSION) or previous.get("doc_root") not in (None, str(self.doc_root.resolve()))
                    # Save the checkpoint before parsing: an interrupted first
                    # build resumes unchanged topics rather than reparsing them.
                    db.executemany("INSERT OR REPLACE INTO metadata VALUES(?,?)", {"schema_version": SCHEMA_VERSION, "doc_root": str(self.doc_root.resolve()), "build_state": "building"}.items())
                    db.commit()
                    seen = set()
                    for path in sorted(self.doc_root.glob("*/*.html"), key=_ingest_priority):
                        rel = path.relative_to(self.doc_root).as_posix()
                        if path.name.startswith(("html_", "index.")):
                            continue
                        seen.add(rel)
                        stat = path.stat()
                        before = old.get(rel)
                        if not rebuild and before and before["source_mtime_ns"] == stat.st_mtime_ns and before["source_size"] == stat.st_size:
                            skipped += 1
                            continue
                        try:
                            parser = TopicParser()
                            parser.feed(path.read_text(encoding="utf-8-sig", errors="replace"))
                            text = parser.content()
                            title = parser.title or (parser.headings[0] if parser.headings else path.stem)
                            if len(text) < 50:
                                continue
                            package = path.parent.name
                            toc = hierarchy.get(rel, {})
                            section_path = toc.get("section_path", [package, title])
                            if section_path[-1] != title:
                                section_path = section_path + [title]
                            is_case = ".help.models." in package
                            # API manuals contain real source/API text. They form a
                            # separate code branch, with exact provenance retained.
                            is_code = not is_case and (path.name.startswith(("comsol_api", "llmatlab_", "comsol_prog")) or (parser.has_code and "api" in rel.lower()))
                            kind = "case" if is_case else "code" if is_code else "manual"
                            branch = {"manual": "manual-tree", "case": "application-library", "code": "api-examples"}[kind]
                            package_key = package.replace("com.comsol.help.models.", "").replace("com.comsol.help.", "").split(".")[0]
                            physics = [PHYSICS_PACKAGES[package_key]] if package_key in PHYSICS_PACKAGES else []
                            title_context = (title + " " + " ".join(section_path)).lower()
                            for name, terms in PHYSICS_TERMS.items():
                                if any(term in title_context for term in terms) and name not in physics:
                                    physics.append(name)
                            row = (source_id(rel), rel, title, text, " > ".join(section_path), "\n".join(parser.headings), json.dumps(section_path, ensure_ascii=False), kind, self.version, branch, package, json.dumps(physics), source_id(toc["parent_rel"]) if toc.get("parent_rel") else None, json.dumps(parser.links[:80], ensure_ascii=False), stat.st_mtime_ns, stat.st_size)
                            db.execute("""INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                ON CONFLICT(id) DO UPDATE SET relpath=excluded.relpath,title=excluded.title,text=excluded.text,
                                context=excluded.context,headings=excluded.headings,section_path=excluded.section_path,
                                kind=excluded.kind,version=excluded.version,branch=excluded.branch,package=excluded.package,
                                physics=excluded.physics,parent_id=excluded.parent_id,links=excluded.links,
                                source_mtime_ns=excluded.source_mtime_ns,source_size=excluded.source_size""", row)
                            indexed += 1
                            if indexed % 60 == 0:
                                db.commit()
                        except (OSError, ValueError, sqlite3.Error) as exc:
                            failures.append({"path": rel, "error": str(exc)[:160]})
                    for removed in old.keys() - seen:
                        db.execute("DELETE FROM documents WHERE relpath=?", (removed,))
                    metadata = {"schema_version": SCHEMA_VERSION, "doc_root": str(self.doc_root.resolve()), "build_state": "complete", "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "version": self.version, "failures": json.dumps(failures[:30]), "last_build_seconds": str(round(time.monotonic() - started, 2))}
                    db.executemany("INSERT OR REPLACE INTO metadata VALUES(?,?)", metadata.items())
                    db.commit()
                self._last_error = None
            except (OSError, sqlite3.Error) as exc:
                self._last_error = str(exc)
            finally:
                self._building = False
        result = self.status()
        result["last_build"] = {"indexed": indexed, "unchanged": skipped, "errors": failures[:30], "seconds": round(time.monotonic() - started, 2)}
        return result

    def status(self) -> dict:
        counts = {"manual": 0, "case": 0, "code": 0}
        packages = 0
        metadata = {}
        if self.db_path.exists():
            try:
                with self._connect() as db:
                    counts.update({row[0]: row[1] for row in db.execute("SELECT kind,count(*) FROM documents GROUP BY kind")})
                    packages = db.execute("SELECT count(DISTINCT package) FROM documents").fetchone()[0]
                    metadata = dict(db.execute("SELECT key,value FROM metadata"))
            except sqlite3.Error as exc:
                self._last_error = str(exc)
        count = sum(counts.values())
        return {"ready": count > 0, "building": self._building or metadata.get("build_state") == "building", "index_complete": metadata.get("build_state") == "complete", "count": count, "document_count": count,
                "by_kind": counts, "packages": packages, "version": metadata.get("version", self.version),
                "doc_root": str(self.doc_root), "doc_root_available": self.doc_root.is_dir(),
                "built_at": metadata.get("built_at"), "backend": "registered-qmd", "archive_backend": "legacy-html-archive",
                "backends": backend_status(self.data_dir), "error": self._last_error,
                "limitations": ["HTML archives preserve original hierarchy and historical citation reads; current retrieval uses registered QMD indexes.", "Equation images and diagrams are not OCRed; inspect the official page for full mathematical notation.", "Application-library values are examples, not validated inputs for the user's model."]}

    def _result(self, row, terms=None, score=0.0, full=False):
        result = {"id": row["id"], "title": row["title"], "excerpt": _excerpt(row["text"], terms or []),
                  "relative_path": row["relpath"],
                  "text": row["text"] if full else _bounded_paragraphs(row["text"], 18000),
                  "url": OFFICIAL_BASE.format(version=row["version"]) + quote(row["relpath"], safe="/._-"),
                  "section_path": json.loads(row["section_path"]), "kind": row["kind"],
                  "version": row["version"], "branch": row["branch"], "score": round(score, 5),
                  "package": row["package"], "physics": json.loads(row["physics"]),
                  "parent_id": row["parent_id"], "headings": row["headings"].splitlines(),
                  "retrieval_backend": "legacy-html-archive"}
        if full:
            result["relative_path"] = row["relpath"]
            result["related_links"] = json.loads(row["links"])
            result["text_truncated"] = False
        return result

    def search(self, query: str, limit: int = 8, kind: str = "all", filters: dict | None = None) -> list[dict]:
        """Compatibility entry point; all searches use registered QMD indexes."""
        from .library_provider import LibraryKnowledgeProvider
        return LibraryKnowledgeProvider(self, self.data_dir / "libraries").search(
            query, limit=limit, kind=kind, filters=filters)

    def get_document(self, source_id: str) -> dict | None:
        if not re.fullmatch(r"cs-[a-f0-9]{24}", source_id) or not self.db_path.exists():
            return None
        try:
            with self._connect() as db:
                row = db.execute("SELECT * FROM documents WHERE id=?", (source_id,)).fetchone()
                if row is None:
                    return None
                result = self._result(row, full=True)
                if row["parent_id"]:
                    parent = db.execute("SELECT * FROM documents WHERE id=?", (row["parent_id"],)).fetchone()
                    if parent:
                        result["parent"] = {"id": parent["id"], "title": parent["title"], "excerpt": _excerpt(parent["text"], []), "section_path": json.loads(parent["section_path"])}
                result["children"] = [{"id": child["id"], "title": child["title"]} for child in
                    db.execute("SELECT id,title FROM documents WHERE parent_id=? ORDER BY relpath LIMIT 80", (source_id,))]
                return result
        except sqlite3.Error as exc:
            self._last_error = str(exc)
            return None


def _bounded_paragraphs(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    paragraphs, length = [], 0
    for para in text.split("\n\n"):
        if paragraphs and length + len(para) > limit:
            break
        paragraphs.append(para)
        length += len(para) + 2
    return "\n\n".join(paragraphs)


def _ingest_priority(path: Path):
    """Make a useful multi-physics corpus available during the first build."""
    package = path.parent.name
    filename = path.name
    if any(name in package for name in ("models.heat.heat_sink", "models.sme.bracket", "models.mph.bracket")):
        return (-2, package, filename)
    if filename in ("heat_ug_ht_features.09.051.html", "heat_ug_ht_features.09.053.html", "sme_ug_solid.07.002.html", "sme_ug_solid.07.063.html", "cfd_ug_fluidflow_single.06.008.html"):
        return (-1, package, filename)
    for prefix, priority in (("com.comsol.help.heat", 0), ("com.comsol.help.sme", 1), ("com.comsol.help.cfd", 2), ("com.comsol.help.models.heat", 3), ("com.comsol.help.models.sme", 4), ("com.comsol.help.models.cfd", 5), ("com.comsol.help.comsol", 6)):
        if package == prefix or package.startswith(prefix + "."):
            return (priority, package, filename)
    return (7, package, filename)
