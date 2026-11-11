"""Versioned COMSOL source retrieval, with no model or network dependency.

The native engine reads COMSOL's *existing* HTML topic boundaries and Eclipse
TOC, rather than inventing chunks or summarising documentation. SQLite FTS5 is
only a candidate generator; titles, explicit terminology aliases, and the real
document hierarchy improve ranking. Optional engines are separate adapters.
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

from .retrieval_backends import QMDAdapter, backend_status

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
        db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS documents(
              id TEXT PRIMARY KEY, relpath TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
              text TEXT NOT NULL, context TEXT, headings TEXT, section_path TEXT,
              kind TEXT, version TEXT, branch TEXT, package TEXT, physics TEXT,
              parent_id TEXT, links TEXT, source_mtime_ns INTEGER, source_size INTEGER);
            CREATE INDEX IF NOT EXISTS doc_kind ON documents(kind);
            CREATE INDEX IF NOT EXISTS doc_package ON documents(package);
            CREATE VIRTUAL TABLE IF NOT EXISTS topics_fts USING fts5(
              title, context, headings, text, content='documents', content_rowid='rowid',
              tokenize='porter unicode61');
            CREATE TRIGGER IF NOT EXISTS doc_ai AFTER INSERT ON documents BEGIN
              INSERT INTO topics_fts(rowid,title,context,headings,text) VALUES(new.rowid,new.title,new.context,new.headings,new.text); END;
            CREATE TRIGGER IF NOT EXISTS doc_ad AFTER DELETE ON documents BEGIN
              INSERT INTO topics_fts(topics_fts,rowid,title,context,headings,text) VALUES('delete',old.rowid,old.title,old.context,old.headings,old.text); END;
            CREATE TRIGGER IF NOT EXISTS doc_au AFTER UPDATE ON documents BEGIN
              INSERT INTO topics_fts(topics_fts,rowid,title,context,headings,text) VALUES('delete',old.rowid,old.title,old.context,old.headings,old.text);
              INSERT INTO topics_fts(rowid,title,context,headings,text) VALUES(new.rowid,new.title,new.context,new.headings,new.text); END;
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
                "built_at": metadata.get("built_at"), "backend": "native-html-hierarchy-sqlite-fts5",
                "backends": backend_status(self.data_dir), "error": self._last_error,
                "limitations": ["Native retrieval uses real HTML TOC hierarchy, explicit Chinese/English aliases and lexical ranking; it does not perform LLM reasoning or vector search.", "Equation images and diagrams are not OCRed; inspect the official page for full mathematical notation.", "Application-library values are examples, not validated inputs for the user's model."]}

    def _result(self, row, terms=None, score=0.0, full=False):
        result = {"id": row["id"], "title": row["title"], "excerpt": _excerpt(row["text"], terms or []),
                  "relative_path": row["relpath"],
                  "text": row["text"] if full else _bounded_paragraphs(row["text"], 18000),
                  "url": OFFICIAL_BASE.format(version=row["version"]) + quote(row["relpath"], safe="/._-"),
                  "section_path": json.loads(row["section_path"]), "kind": row["kind"],
                  "version": row["version"], "branch": row["branch"], "score": round(score, 5),
                  "package": row["package"], "physics": json.loads(row["physics"]),
                  "parent_id": row["parent_id"], "headings": row["headings"].splitlines(),
                  "retrieval_backend": "native-html-hierarchy-sqlite-fts5"}
        if full:
            result["relative_path"] = row["relpath"]
            result["related_links"] = json.loads(row["links"])
            result["text_truncated"] = False
        return result

    def search(self, query: str, limit: int = 8, kind: str = "all", filters: dict | None = None) -> list[dict]:
        if not query.strip() or not self.db_path.exists():
            return []
        if kind not in ("all", "manual", "case", "code"):
            raise ValueError("kind must be all, manual, case or code")
        limit = max(1, min(int(limit), 40))
        expanded = expand_query(query)
        terms = expanded["terms"]
        if not terms:
            return []
        filters = filters or {}
        exclusions = filters.get("exclude_relpath_prefixes", [])
        if not isinstance(exclusions, list) or any(not isinstance(prefix, str) for prefix in exclusions):
            raise ValueError("exclude_relpath_prefixes must be a list of relative directory paths")
        normalized_exclusions = []
        for prefix in exclusions:
            prefix = prefix.replace("\\", "/")
            if prefix == "":
                return []
            if (prefix.startswith("/") or ":" in prefix or "\0" in prefix
                    or any(part in {"", ".", ".."} for part in prefix.rstrip("/").split("/"))):
                raise ValueError("exclude_relpath_prefixes must stay within the documentation root")
            normalized_exclusions.append(prefix.rstrip("/"))
        requested_backend = filters.get("backend", os.environ.get("SIM_ADVISOR_RETRIEVAL_MODE", "native"))
        if requested_backend == "pageindex":
            raise ValueError("PageIndex PDF reasoning requires an explicitly indexed PDF and configured model; use PageIndexAdapter. Native HTML retrieval is not PageIndex.")
        if requested_backend == "qmd":
            if normalized_exclusions:
                raise ValueError("Legacy source-prefix exclusions require native retrieval; no global QMD fallback")
            if not backend_status(self.data_dir)["qmd"]["available"]:
                raise ValueError("Actual QMD keyword index is unavailable; run scripts/ingest_docs.py --qmd first")
            candidates = QMDAdapter(self.data_dir).search(" ".join(terms), 160)
            results = []
            with self._connect() as db:
                for candidate in candidates:
                    row = db.execute("SELECT * FROM documents WHERE id=?", (candidate["id"],)).fetchone()
                    if not row or (kind != "all" and row["kind"] != kind):
                        continue
                    if any(filters.get(key) and filters[key] != row[key] for key in ("version", "package", "branch")):
                        continue
                    if filters.get("physics") and filters["physics"] not in json.loads(row["physics"]):
                        continue
                    title = row["title"].lower()
                    score = float(candidate["score"])
                    normalized_title = " ".join(re.findall(r"[a-z0-9]+", title))
                    normalized_query = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
                    if normalized_query and normalized_title == normalized_query:
                        score += 3
                    for phrase in expanded["phrases"]:
                        if phrase.lower() in title:
                            score += 0.3
                    for phrase in expanded["focus_phrases"]:
                        if phrase in title:
                            score += 1.5
                    score += 0.025 * sum(bool(re.search(r"\b" + re.escape(t) + r"\b", title)) for t in terms)
                    result = self._result(row, terms, score)
                    result["retrieval_backend"] = "qmd-keyword+source-hierarchy"
                    results.append(result)
            results.sort(key=lambda item: item["score"], reverse=True)
            return results[:limit]
        if requested_backend != "native":
            raise ValueError("backend must be native, qmd or pageindex")
        clauses, values = [], []
        for prefix in normalized_exclusions:
            escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(d.relpath<>? AND d.relpath NOT LIKE ? ESCAPE '\\')")
            values.extend([prefix, escaped + "/%"])
        if kind != "all":
            clauses.append("d.kind=?")
            values.append(kind)
        for key in ("version", "package", "branch"):
            if filters.get(key):
                clauses.append(f"d.{key}=?")
                values.append(str(filters[key]))
        if filters.get("physics"):
            clauses.append("d.physics LIKE ?")
            values.append('%"' + str(filters["physics"]) + '"%')
        match = " OR ".join('"' + term + '"' for term in terms)
        sql = "SELECT d.*,bm25(topics_fts,9.0,2.5,3.0,1.0) AS fts_rank FROM topics_fts JOIN documents d ON d.rowid=topics_fts.rowid WHERE topics_fts MATCH ?"
        if clauses:
            sql += " AND " + " AND ".join(clauses)
        sql += " ORDER BY fts_rank LIMIT 180"
        try:
            with self._connect() as db:
                rows = db.execute(sql, [match] + values).fetchall()
                ranked = []
                for row in rows:
                    title = row["title"].lower()
                    context = row["context"].lower()
                    body = row["text"].lower()
                    title_hits = sum(bool(re.search(r"\b" + re.escape(t) + r"\b", title)) for t in terms)
                    context_hits = sum(bool(re.search(r"\b" + re.escape(t) + r"\b", context)) for t in terms)
                    text_hits = sum(bool(re.search(r"\b" + re.escape(t) + r"\b", body)) for t in terms)
                    score = -float(row["fts_rank"]) + title_hits * 2.2 + context_hits * 0.45 + (text_hits / len(terms)) * 3.5
                    normalized_title = " ".join(re.findall(r"[a-z0-9]+", title))
                    normalized_query = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
                    if normalized_query and normalized_title == normalized_query:
                        score += 38
                    for phrase in expanded["phrases"]:
                        if phrase.lower() in title:
                            score += 7
                        elif phrase.lower() in context:
                            score += 2.5
                    for phrase in expanded["focus_phrases"]:
                        if phrase in title:
                            score += 28
                        elif phrase in context:
                            score += 12
                        elif phrase in body[:2500]:
                            score += 4
                    # Multiple terms together in a title/real TOC path disambiguate
                    # repeated "Heat Flux", "Inlet", "Fixed Constraint" topics.
                    if title_hits == len(terms):
                        score += 5
                    physics = json.loads(row["physics"])
                    score += 2.0 * len(set(physics) & set(expanded["physics"]))
                    preferred_packages = {"heat": "heat", "structure": "sme", "flow": "cfd", "electromagnetics": "acdc", "acoustics": "aco"}
                    if any(row["package"] == "com.comsol.help." + preferred_packages.get(physics_name, "!") for physics_name in expanded["physics"]):
                        score += 2.0
                    if expanded["physics"] and physics and not set(physics) & set(expanded["physics"]):
                        score *= 0.65
                    ranked.append((score, row))
                ranked.sort(key=lambda item: item[0], reverse=True)
                return [self._result(row, terms, score) for score, row in ranked[:limit]]
        except sqlite3.Error as exc:
            self._last_error = str(exc)
            return []

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
