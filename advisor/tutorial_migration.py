"""Staged, offline removal of repeated tutorial retrieval objects.

Planning reads the immutable catalog and compact case databases and writes only
the explicitly named SQLite plan.  It does not parse MPH/PDF files, mutate a
library, build embeddings, or redirect an old citation.  Publication is a
separate catalog transaction, owned by the offline maintenance coordinator.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .build_control import check_cancelled


FORMAT = "tutorial-migration/sqlite-v1"
ASSOCIATION_TABLE = "tutorial_associations"
TUTORIAL_TYPES = {"case_context", "method_step"}


def ensure_tutorial_association_schema(db):
    """Create compact association/source lookup tables in the caller's transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS tutorial_associations(
      source_id TEXT NOT NULL,source_library_id TEXT NOT NULL,target_id TEXT NOT NULL,
      target_library_id TEXT NOT NULL,relation TEXT NOT NULL,source_sha256 TEXT NOT NULL,
      migration_id TEXT NOT NULL,PRIMARY KEY(source_id,target_id,relation))""")
    db.execute("CREATE INDEX IF NOT EXISTS tutorial_associations_source ON tutorial_associations(source_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS tutorial_document_sources(
      source_sha256 TEXT NOT NULL,identity TEXT,owner_library_id TEXT NOT NULL,
      citation_id TEXT NOT NULL,anchor TEXT NOT NULL,category TEXT NOT NULL,
      PRIMARY KEY(source_sha256,owner_library_id,citation_id,anchor))""")
    db.execute("CREATE INDEX IF NOT EXISTS tutorial_document_sources_identity ON tutorial_document_sources(identity,owner_library_id)")
    db.execute("CREATE INDEX IF NOT EXISTS tutorial_document_sources_citation ON tutorial_document_sources(citation_id)")
    db.execute('''CREATE TABLE IF NOT EXISTS tutorial_retired_sources(
      citation_id TEXT PRIMARY KEY,owner_library_id TEXT NOT NULL,reason TEXT NOT NULL,source_sha256 TEXT NOT NULL)''')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            check_cancelled()
            digest.update(block)
    return digest.hexdigest()


def _generation(db):
    digest, count = hashlib.sha256(b"["), 0
    for row in db.execute("SELECT citation_id FROM active ORDER BY citation_id"):
        check_cancelled()
        if count:
            digest.update(b",")
        digest.update(_json(row[0]).encode("utf-8"))
        count += 1
    digest.update(b"]")
    return digest.hexdigest()[:24]


def _connect(path, *, read_only=False):
    path = Path(path).resolve()
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) if read_only else sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    if read_only:
        db.execute("PRAGMA query_only=ON")
    return db


def _schema(db):
    db.executescript("""
      CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE owners(library_id TEXT PRIMARY KEY, body_sha256 TEXT NOT NULL,
        body TEXT NOT NULL, protected INTEGER NOT NULL);
      CREATE TABLE cases(id TEXT PRIMARY KEY, library_id TEXT NOT NULL, case_group TEXT,
        payload TEXT NOT NULL);
      CREATE INDEX cases_group ON cases(case_group);
      CREATE TABLE tutorial_sources(id TEXT PRIMARY KEY, object_id TEXT NOT NULL,
        library_id TEXT NOT NULL, case_group TEXT, passage_id INTEGER, locators TEXT NOT NULL,
        source_sha256 TEXT);
      CREATE INDEX tutorial_sources_group ON tutorial_sources(case_group,passage_id);
      CREATE TABLE manual_sources(id TEXT PRIMARY KEY, object_id TEXT NOT NULL,
        library_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, path TEXT NOT NULL,
        root_id TEXT NOT NULL, relative_path TEXT NOT NULL, suffix TEXT NOT NULL,
        evidence TEXT NOT NULL, title TEXT NOT NULL);
      CREATE INDEX manual_sources_hash ON manual_sources(source_sha256,suffix);
      CREATE INDEX manual_sources_path ON manual_sources(path);
      CREATE TABLE compact_documents(case_id TEXT NOT NULL, document_id INTEGER NOT NULL,
        source_sha256 TEXT NOT NULL, source_uri TEXT, PRIMARY KEY(case_id,document_id));
      CREATE INDEX compact_documents_hash ON compact_documents(source_sha256);
      CREATE TABLE compact_passages(case_id TEXT NOT NULL, passage_id INTEGER NOT NULL,
        document_id INTEGER NOT NULL, source_sha256 TEXT NOT NULL, text_sha256 TEXT NOT NULL,
        text TEXT NOT NULL, truncated INTEGER NOT NULL, PRIMARY KEY(case_id,passage_id));
      CREATE INDEX compact_passages_hash ON compact_passages(source_sha256);
      CREATE TABLE passage_anchors(case_id TEXT NOT NULL, passage_id INTEGER NOT NULL,
        source_sha256 TEXT NOT NULL, locator TEXT NOT NULL,
        PRIMARY KEY(case_id,passage_id,locator));
      CREATE INDEX passage_anchors_lookup ON passage_anchors(source_sha256,locator);
      CREATE TABLE selected_html(source_sha256 TEXT PRIMARY KEY, path TEXT NOT NULL,
        source_id TEXT NOT NULL, canonical_count INTEGER NOT NULL DEFAULT 0,
        completeness TEXT NOT NULL, tutorial_identity TEXT);
      CREATE TABLE canonical_objects(id TEXT PRIMARY KEY, object_id TEXT NOT NULL,
        library_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, ordinal INTEGER NOT NULL,
        text_sha256 TEXT NOT NULL, payload TEXT NOT NULL);
      CREATE INDEX canonical_document_order ON canonical_objects(source_sha256,ordinal,id);
      CREATE TABLE canonical_anchors(target_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
        locator TEXT NOT NULL, PRIMARY KEY(target_id,locator));
      CREATE INDEX canonical_anchors_lookup ON canonical_anchors(source_sha256,locator);
      CREATE TABLE compact_text_reuse(target_id TEXT NOT NULL, case_id TEXT NOT NULL,
        passage_id INTEGER NOT NULL, text_sha256 TEXT NOT NULL, passage_text_sha256 TEXT NOT NULL,
        text_start INTEGER NOT NULL, text_end INTEGER NOT NULL,
        PRIMARY KEY(target_id,case_id,passage_id));
      CREATE TABLE retirements(id TEXT PRIMARY KEY, object_id TEXT NOT NULL,
        library_id TEXT NOT NULL, reason TEXT NOT NULL, source_sha256 TEXT NOT NULL);
      CREATE TABLE relations(source_id TEXT NOT NULL, source_library_id TEXT NOT NULL,
        target_id TEXT NOT NULL, target_library_id TEXT NOT NULL, relation TEXT NOT NULL,
        source_sha256 TEXT NOT NULL, PRIMARY KEY(source_id,target_id,relation));
      CREATE TABLE alternate_sources(source_sha256 TEXT NOT NULL, path TEXT NOT NULL,
        asset_sha256 TEXT NOT NULL, source_id TEXT NOT NULL, format TEXT NOT NULL,
        PRIMARY KEY(source_id,source_sha256));
      CREATE TABLE warnings(code TEXT NOT NULL, source_id TEXT, detail TEXT NOT NULL);
    """)


def _meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, _json(value)))


def _warning(db, code, source, detail):
    db.execute("INSERT INTO warnings VALUES(?,?,?)", (code, source, str(detail)[:2000]))


def _asset(library, obj):
    key = obj.get("asset_key", "")
    root = next((item for item in sorted(library["roots"], key=lambda item: len(item["id"]), reverse=True)
                 if key.startswith(item["id"] + ":")), None)
    if root is None:
        raise ValueError("Tutorial source has no registered asset root")
    relative = key[len(root["id"]) + 1:]
    boundary = Path(root["path"]).resolve()
    path = (boundary / relative).resolve()
    if not path.is_relative_to(boundary):
        raise ValueError("Tutorial source escapes its registered asset root")
    return path, root["id"], relative


def _locators(obj):
    return sorted({str(item["locator"]) for item in obj.get("evidence", []) if item.get("locator")})


def _progress(callback, **value):
    check_cancelled()
    if callback:
        callback(value)
    check_cancelled()


def _plan_identity(db):
    digest = hashlib.sha256()
    for table in ("retirements", "canonical_objects"):
        digest.update(table.encode("ascii") + b"\0")
        for row in db.execute("SELECT id FROM " + table + " ORDER BY id"):
            check_cancelled()
            digest.update(_json(row[0]).encode("utf-8") + b"\n")
    return digest.hexdigest()


class TutorialMigrationPlan:
    """Reiterable bounded deltas; large ID/payload collections stay in SQLite."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        with closing(_connect(self.path, read_only=True)) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='format'").fetchone()
            if not row or json.loads(row[0]) != FORMAT:
                raise ValueError("Not a supported tutorial migration plan")

    def metadata(self):
        with closing(_connect(self.path, read_only=True)) as db:
            return {row[0]: json.loads(row[1]) for row in db.execute("SELECT key,value FROM metadata")}

    def _rows(self, sql, params=()):
        with closing(_connect(self.path, read_only=True)) as db:
            for row in db.execute(sql, params):
                check_cancelled()
                yield dict(row)

    def iter_retirements(self, library_id=None):
        where = " WHERE library_id=?" if library_id else ""
        return self._rows("SELECT * FROM retirements" + where + " ORDER BY library_id,id",
                          (library_id,) if library_id else ())

    def iter_retirement_ids(self):
        for row in self._rows("SELECT id FROM retirements ORDER BY id"):
            yield row["id"]

    def iter_canonical_objects(self, library_id=None, *, order_by_id=False):
        where = " WHERE library_id=?" if library_id else ""
        order = "id" if order_by_id else "source_sha256,ordinal,id"
        for row in self._rows("SELECT payload FROM canonical_objects" + where + " ORDER BY " + order,
                              (library_id,) if library_id else ()):
            yield json.loads(row["payload"])

    def iter_relations(self):
        return self._rows("SELECT * FROM relations ORDER BY source_id,target_id,relation")

    def iter_alternate_sources(self):
        return self._rows("SELECT * FROM alternate_sources ORDER BY source_sha256,path,source_id")

    def iter_compact_text_reuse(self):
        return self._rows("SELECT * FROM compact_text_reuse ORDER BY target_id,case_id,passage_id")

    def iter_document_sources(self):
        """Yield one primary heading/anchor per canonical chunk for future builds."""
        return self._rows("""SELECT c.source_sha256,h.tutorial_identity identity,
          c.library_id owner_library_id,c.id citation_id,
          COALESCE(json_extract(c.payload,'$.evidence[0].locator'),'document') anchor,
          COALESCE(json_extract(c.payload,'$.data.feature_branch'),
                   CASE json_extract(c.payload,'$.type') WHEN 'method_step' THEN 'procedure' ELSE 'context' END) category
          FROM canonical_objects c JOIN selected_html h ON h.source_sha256=c.source_sha256
          ORDER BY c.source_sha256,c.ordinal,c.id""")

    def iter_warnings(self):
        return self._rows("SELECT * FROM warnings ORDER BY rowid")

    def summary(self):
        result = self.metadata()
        with closing(_connect(self.path, read_only=True)) as db:
            result["counts"] = {key: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for key, table in (
                ("retirements", "retirements"), ("canonical_objects", "canonical_objects"),
                ("relations", "relations"), ("selected_html", "selected_html"),
                ("compact_text_reuse", "compact_text_reuse"), ("warnings", "warnings"),
                ("alternate_sources", "alternate_sources"))}
            result["retirements_by_reason"] = dict(db.execute("SELECT reason,COUNT(*) FROM retirements GROUP BY reason"))
            result["protected_owner_libraries"] = [row[0] for row in db.execute("SELECT library_id FROM owners WHERE protected=1 ORDER BY library_id")]
            result["affected_libraries"] = [dict(row) for row in db.execute("""
              SELECT library_id,SUM(retired) retired,SUM(inserted) inserted FROM (
                SELECT library_id,COUNT(*) retired,0 inserted FROM retirements GROUP BY library_id
                UNION ALL SELECT library_id,0,COUNT(*) FROM canonical_objects GROUP BY library_id)
              GROUP BY library_id ORDER BY library_id""")]
        return result


def _stage_catalog(store, db, canonical_library_id, on_progress):
    libraries = {item["id"]: item for item in store.libraries(include_deleted=True)}
    if canonical_library_id not in libraries:
        raise ValueError("Canonical tutorial owner library is not registered")
    canonical = libraries[canonical_library_id]
    if canonical["type"] != "official-manuals":
        raise ValueError("Canonical tutorial owner must be an official-manuals library")
    with closing(store.connect()) as source:
        for identity, body in source.execute("SELECT id,body FROM libraries ORDER BY id"):
            library = libraries[identity]
            protected = not library.get("enabled", True) or library.get("deleted_at") is not None
            db.execute("INSERT INTO owners VALUES(?,?,?,?)", (identity, _sha(body), body, int(protected)))
    with store.object_stream() as objects:
        _meta(db, "source_generation", objects.generation)
        _meta(db, "source_active_count", len(objects))
        for count, obj in enumerate(objects, 1):
            library = libraries.get(obj.get("library_id"))
            if not library or not library.get("enabled", True) or library.get("deleted_at") is not None:
                continue
            data = obj.get("data") or {}
            if obj.get("type") == "model_case" and isinstance(data.get("compact_case"), dict):
                db.execute("INSERT INTO cases VALUES(?,?,?,?)", (obj["id"], obj["library_id"], obj.get("case_group"), _json(obj)))
            elif obj.get("type") in TUTORIAL_TYPES and library["type"] in {"official-models", "user-models"}:
                evidence_hashes = {item.get("asset_hash") for item in obj.get("evidence", []) if item.get("asset_hash")}
                source_sha = next(iter(evidence_hashes)) if len(evidence_hashes) == 1 else None
                ref = data.get("compact_ref") or {}
                db.execute("INSERT INTO tutorial_sources VALUES(?,?,?,?,?,?,?)", (
                    obj["id"], obj["object_id"], obj["library_id"], obj.get("case_group"), ref.get("passage_id"), _json(_locators(obj)), source_sha))
            elif obj.get("library_id") == canonical_library_id and not data.get("canonical_tutorial"):
                try:
                    path, root_id, relative = _asset(library, obj)
                except ValueError as exc:
                    _warning(db, "manual_asset_unresolved", obj.get("id"), exc)
                    continue
                if path.suffix.lower() not in {".html", ".htm", ".pdf"}:
                    continue
                db.execute("INSERT INTO manual_sources VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    obj["id"], obj["object_id"], obj["library_id"], obj["asset_hash"], str(path), root_id, relative,
                    path.suffix.lower(), _json(obj.get("evidence", [])), obj.get("title", "")))
            if count % 2000 == 0:
                _progress(on_progress, phase="scan_catalog", completed=count, total=len(objects))
    db.commit()
    return libraries


def _stage_compact(store, db, on_progress):
    from .compact_library import _reader
    total = db.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    for number, row in enumerate(db.execute("SELECT * FROM cases ORDER BY id"), 1):
        check_cancelled()
        authority = json.loads(row["payload"])
        try:
            reader = _reader(authority, store.root, None)
            with closing(reader._connect()) as source:
                reader._model(source)
                for document in source.execute("SELECT id,source_uri,source_sha256 FROM documents ORDER BY id"):
                    if document["source_sha256"]:
                        db.execute("INSERT INTO compact_documents VALUES(?,?,?,?)", (
                            row["id"], document["id"], document["source_sha256"], document["source_uri"]))
                for passage in source.execute("""
                  SELECT p.id,p.document_id,t.text,d.source_sha256,m.body_json
                  FROM passages p JOIN documents d ON d.id=p.document_id
                  JOIN text_pool t ON t.id=p.text_id LEFT JOIN metadata_pool m ON m.id=p.metadata_id
                  ORDER BY p.id"""):
                    metadata = json.loads(passage["body_json"]) if passage["body_json"] else {}
                    if not passage["source_sha256"]:
                        continue
                    db.execute("INSERT INTO compact_passages VALUES(?,?,?,?,?,?,?)", (
                        row["id"], passage["id"], passage["document_id"], passage["source_sha256"],
                        _sha(passage["text"]), passage["text"], int(bool(metadata.get("truncated")))))
                    for locator in set(metadata.get("locators", [])):
                        db.execute("INSERT INTO passage_anchors VALUES(?,?,?,?)", (
                            row["id"], passage["id"], passage["source_sha256"], str(locator)))
        except (OSError, ValueError, sqlite3.Error) as exc:
            # One broken old case never licenses retirement of its children.
            db.execute("DELETE FROM compact_documents WHERE case_id=?", (row["id"],))
            db.execute("DELETE FROM compact_passages WHERE case_id=?", (row["id"],))
            db.execute("DELETE FROM passage_anchors WHERE case_id=?", (row["id"],))
            _warning(db, "compact_case_unavailable", row["id"], exc)
        _progress(on_progress, phase="read_compact_tutorials", completed=number, total=total)
    db.commit()


def _canonical_payload(library, manual, source, ordinal, source_sha):
    """Prepare new manual objects without touching immutable source payloads."""
    source = dict(source, data=dict(source.get("data") or {}))
    # Keep exactly the ordinary Store._prepare logical identity. A subsequent
    # build of an unchanged manual must reuse these canonical citations.
    local = str(source["local_id"])
    logical = "lo-" + _sha(_json([library["id"], manual["root_id"], manual["relative_path"], local]))[:24]
    evidence = []
    for item in source.get("evidence", []) or [{"locator": "document"}]:
        item = {name: value for name, value in item.items() if name != "asset_path"}
        evidence.append(dict(item, root_id=manual["root_id"], relative_path=manual["relative_path"], asset_hash=source_sha))
    source["data"].update(canonical_tutorial={"schema": "canonical-tutorial/v1", "source_sha256": source_sha,
                                              "ordinal": ordinal, "text_sha256": _sha(source.get("text", ""))})
    payload = {"schema": "library/v1", "object_id": logical, "local_id": local,
               "library_id": library["id"], "origin": library["type"], "version": library.get("version", ""),
               "type": source.get("type", "manual_topic"), "title": source.get("title", manual["title"]),
               "text": source.get("text", ""), "kind": "manual", "data": source["data"], "relations": [],
               "evidence": evidence, "asset_key": manual["root_id"] + ":" + manual["relative_path"],
               "asset_hash": source_sha, "coverage": source.get("coverage", {}),
               "verification": {"numerical": "not_run", "original_files": "read_only"}}
    revision = _sha(_json(payload))
    citation = logical + "-" + revision[:12]
    uri = "library://" + library["id"] + "/" + citation
    return dict(payload, id=citation, revision_id=revision, source_uri=uri, url=uri,
                section_path=[library["name"], payload["title"]], branch=library["type"], package=library["id"],
                physics=[], parent=None, text_truncated=False, read_scope="complete_knowledge_object")


def _canonical_html(store, db, library, on_progress):
    from .tutorial_sources import extract_tutorial_objects, tutorial_identity
    candidates = db.execute("""SELECT m.* FROM manual_sources m WHERE m.suffix IN ('.html','.htm')
      ORDER BY m.source_sha256,m.id""")
    count = 0
    for manual in candidates:
        check_cancelled()
        source_sha, path = manual["source_sha256"], Path(manual["path"])
        if db.execute("SELECT 1 FROM selected_html WHERE source_sha256=?", (source_sha,)).fetchone():
            continue
        identity = tutorial_identity(path)
        associated = db.execute("SELECT 1 FROM compact_documents WHERE source_sha256=? LIMIT 1", (source_sha,)).fetchone()
        if not identity and not associated:
            continue
        try:
            if not path.is_file() or _file_sha(path) != source_sha:
                raise ValueError("HTML no longer matches its immutable manual source hash")
            root = next(item for item in library["roots"] if item["id"] == manual["root_id"])
            parsed = extract_tutorial_objects(path, root=Path(root["path"]), source_sha256=source_sha)
            objects = parsed.get("objects") or []
            if not objects or any(not isinstance(item.get("text"), str) for item in objects):
                raise ValueError("HTML extractor did not produce a complete canonical source")
            if _file_sha(path) != source_sha:
                raise ValueError("HTML changed while canonical sections were extracted")
            completeness = str(parsed.get("tutorial", {}).get("completeness", "complete_html_extraction"))
            db.execute("INSERT INTO selected_html VALUES(?,?,?,?,?,?)", (
                source_sha, str(path), manual["id"], len(objects), completeness, identity))
            for ordinal, item in enumerate(objects, 1):
                item = dict(item, coverage=item.get("coverage") or parsed.get("coverage", {}))
                target = _canonical_payload(library, manual, item, ordinal, source_sha)
                text, target_id = target["text"], target["id"]
                db.execute("INSERT INTO canonical_objects VALUES(?,?,?,?,?,?,?)", (
                    target_id, target["object_id"], library["id"], source_sha, ordinal, _sha(text), _json(target)))
                for locator in _locators(item) or ["document"]:
                    db.execute("INSERT OR IGNORE INTO canonical_anchors VALUES(?,?,?)", (target_id, source_sha, locator))
                # Compare canonical blocks with already retained typed text;
                # ranges refer to original complete passages, never fragments.
                for passage in db.execute("SELECT * FROM compact_passages WHERE source_sha256=? ORDER BY case_id,passage_id", (source_sha,)):
                    start = passage["text"].find(text) if text else -1
                    if start >= 0:
                        db.execute("INSERT OR IGNORE INTO compact_text_reuse VALUES(?,?,?,?,?,?,?)", (
                            target_id, passage["case_id"], passage["passage_id"], _sha(text), passage["text_sha256"], start, start + len(text)))
            count += 1
            _progress(on_progress, phase="canonicalize_tutorial_html", completed=count, total=None)
        except (OSError, ValueError, sqlite3.Error) as exc:
            # A failed/ambiguous parse keeps all three old representations.
            targets = "SELECT id FROM canonical_objects WHERE source_sha256=?"
            db.execute("DELETE FROM compact_text_reuse WHERE target_id IN (" + targets + ")", (source_sha,))
            db.execute("DELETE FROM canonical_anchors WHERE source_sha256=?", (source_sha,))
            db.execute("DELETE FROM canonical_objects WHERE source_sha256=?", (source_sha,))
            db.execute("DELETE FROM selected_html WHERE source_sha256=?", (source_sha,))
            _warning(db, "tutorial_html_preserved", manual["id"], exc)
    db.commit()


def _relation(db, source, owner, target, target_owner, relation, source_sha):
    db.execute("INSERT OR IGNORE INTO relations VALUES(?,?,?,?,?,?)", (source, owner, target, target_owner, relation, source_sha))


def _derive_deltas(db, canonical_library_id):
    # Original whole HTML revisions are archived once canonical sections exist.
    db.execute("""INSERT OR IGNORE INTO retirements
      SELECT m.id,m.object_id,m.library_id,'whole_tutorial_html',m.source_sha256
      FROM manual_sources m JOIN selected_html h USING(source_sha256)
      WHERE m.suffix IN ('.html','.htm') AND h.canonical_count>0""")
    for case in db.execute("SELECT * FROM cases ORDER BY id"):
        for document in db.execute("SELECT DISTINCT source_sha256 FROM compact_documents WHERE case_id=?", (case["id"],)):
            source_sha = document[0]
            for target in db.execute("SELECT id FROM canonical_objects WHERE source_sha256=? ORDER BY ordinal,id LIMIT 1", (source_sha,)):
                _relation(db, case["id"], case["library_id"], target[0], canonical_library_id, "documented_by", source_sha)
    for source in db.execute("SELECT * FROM tutorial_sources ORDER BY id"):
        # Match the pinned case AND passage. Identical MPH bytes are not a
        # document identity: separate preview cases can reference other HTML.
        passage = db.execute("""SELECT p.* FROM compact_passages p JOIN cases c ON c.id=p.case_id
          WHERE c.case_group=? AND p.passage_id=?""", (source["case_group"], source["passage_id"])).fetchone()
        if passage is None:
            continue
        source_sha = passage["source_sha256"]
        selected = db.execute("SELECT 1 FROM selected_html WHERE source_sha256=? AND canonical_count>0", (source_sha,)).fetchone()
        if not selected:
            continue
        db.execute("INSERT INTO retirements VALUES(?,?,?,?,?)", (
            source["id"], source["object_id"], source["library_id"], "model_tutorial_copy", source_sha))
        # Shared anchors are explicit source associations, not content redirects.
        targets = db.execute("""SELECT a.target_id FROM canonical_anchors a
          JOIN passage_anchors p ON p.source_sha256=a.source_sha256 AND p.locator=a.locator
          JOIN canonical_objects c ON c.id=a.target_id
          WHERE p.case_id=? AND p.passage_id=? ORDER BY c.ordinal,a.target_id LIMIT 1""", (passage["case_id"], passage["passage_id"]))
        matched = False
        for target in targets:
            matched = True
            _relation(db, source["id"], source["library_id"], target[0], canonical_library_id, "canonical_tutorial", source_sha)
        if not matched:
            for target in db.execute("SELECT id FROM canonical_objects WHERE source_sha256=? ORDER BY ordinal,id LIMIT 1", (source_sha,)):
                _relation(db, source["id"], source["library_id"], target[0], canonical_library_id, "canonical_tutorial", source_sha)
    db.commit()


def _pdf_deltas(db, canonical_library_id, library):
    from .tutorial_sources import choose_companion_html
    for pdf in db.execute("SELECT * FROM manual_sources WHERE suffix='.pdf' GROUP BY path,source_sha256 ORDER BY path"):
        path = Path(pdf["path"])
        root = next(item for item in library["roots"] if item["id"] == pdf["root_id"])
        html = choose_companion_html(path, root=Path(root["path"]))
        if html is None:
            continue
        selected = db.execute("""SELECT h.* FROM selected_html h JOIN manual_sources m
          ON m.source_sha256=h.source_sha256 WHERE m.path=? AND h.canonical_count>0""",
                              (str(Path(html).resolve()),)).fetchone()
        if not selected:
            continue
        # Filename/package identity alone is insufficient. Require both saved
        # source hashes still present and the chosen HTML already canonical.
        try:
            if not path.is_file() or _file_sha(path) != pdf["source_sha256"] or _file_sha(html) != selected["source_sha256"]:
                raise ValueError("PDF/HTML companion no longer matches its saved source")
        except OSError as exc:
            _warning(db, "pdf_companion_unverified", pdf["id"], exc)
            continue
        except ValueError as exc:
            _warning(db, "pdf_companion_unverified", pdf["id"], exc)
            continue
        for page in db.execute("SELECT * FROM manual_sources WHERE path=? AND source_sha256=? ORDER BY id", (str(path), pdf["source_sha256"])):
            db.execute("INSERT OR IGNORE INTO retirements VALUES(?,?,?,?,?)", (
                page["id"], page["object_id"], page["library_id"], "verified_html_companion", selected["source_sha256"]))
            db.execute("INSERT OR IGNORE INTO alternate_sources VALUES(?,?,?,?,?)", (
                selected["source_sha256"], str(path), pdf["source_sha256"], page["id"], "pdf"))
    # One representative identifies the whole canonical document. Full chapter
    # browsing uses its hash in tutorial_document_sources, avoiding a page x
    # chapter Cartesian expansion for long PDF tutorials.
    for source in db.execute("SELECT * FROM retirements WHERE reason!='model_tutorial_copy' ORDER BY id"):
        for target in db.execute("SELECT id FROM canonical_objects WHERE source_sha256=? ORDER BY ordinal,id LIMIT 1", (source["source_sha256"],)):
            _relation(db, source["id"], source["library_id"], target[0], canonical_library_id, "canonical_tutorial", source["source_sha256"])
    db.commit()


def build_tutorial_migration_plan(store, plan_path, *, canonical_library_id="cs63-documents", on_progress=None):
    """Create a reviewable plan without modifying catalog, indexes or sources.

    Memory is bounded by one catalog object/compact passage/tutorial HTML at a
    time. Catalog candidates, deduplication keys, ID maps and index deltas are
    staged in SQLite. Existing plan paths are never overwritten.
    """
    plan_path = Path(plan_path).resolve()
    if plan_path.exists():
        raise FileExistsError("Tutorial migration plan already exists")
    if plan_path == Path(store.db_path).resolve():
        raise ValueError("Plan cannot replace the catalog")
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        # Reserve the path before SQLite opens it; do not follow existing links.
        with plan_path.open("xb"):
            created = True
        with closing(_connect(plan_path)) as db:
            _schema(db)
            _meta(db, "format", FORMAT)
            _meta(db, "status", "building")
            _meta(db, "source_catalog", str(Path(store.db_path).resolve()))
            _meta(db, "created_at", datetime.now(timezone.utc).isoformat())
            _meta(db, "canonical_library_id", canonical_library_id)
            _meta(db, "archive_policy", "keep_immutable_revisions_and_citations")
            _meta(db, "index_policy", {"native": "incremental", "qmd": "affected_libraries_only", "embed": False})
            libraries = _stage_catalog(store, db, canonical_library_id, on_progress)
            canonical = libraries[canonical_library_id]
            if canonical.get("enabled", True) and canonical.get("deleted_at") is None:
                _stage_compact(store, db, on_progress)
                _canonical_html(store, db, canonical, on_progress)
                _derive_deltas(db, canonical_library_id)
                _pdf_deltas(db, canonical_library_id, canonical)
            else:
                _warning(db, "canonical_owner_protected", canonical_library_id, "Closed/deleted owner blocks cross-library canonicalization")
            _meta(db, "status", "ready")
            _meta(db, "migration_id", _sha(_json([
                FORMAT, str(store.db_path), db.execute("SELECT value FROM metadata WHERE key='source_generation'").fetchone()[0],
                canonical_library_id, _plan_identity(db)]))[:24])
            db.commit()
        plan = TutorialMigrationPlan(plan_path)
        _progress(on_progress, phase="plan_ready", **plan.summary()["counts"])
        return plan
    except BaseException:
        if created:
            plan_path.unlink(missing_ok=True)
        raise


def validate_tutorial_plan_sources(plan):
    """Refuse publication if any selected HTML/PDF changed since planning."""
    plan = plan if isinstance(plan, TutorialMigrationPlan) else TutorialMigrationPlan(plan)
    with closing(_connect(plan.path, read_only=True)) as staged:
        sources = staged.execute("""SELECT path,source_sha256 FROM selected_html
          UNION SELECT path,asset_sha256 FROM alternate_sources ORDER BY path""")
        for source in sources:
            check_cancelled()
            path = Path(source[0])
            if not path.is_file() or _file_sha(path) != source[1]:
                raise ValueError("Tutorial source changed after migration planning: " + str(path))


def apply_plan_to_catalog(db, plan, *, verify_sources=True):
    """Apply catalog deltas inside the caller's existing offline transaction.

    The coordinator must own the application/build lease and stage incremental
    native/QMD updates before committing. This function never commits, embeds,
    deletes revisions, mutates case_members, or changes citation payloads.
    A rollback restores every active row and association together.
    """
    if not db.in_transaction:
        raise ValueError("Tutorial publication requires a caller-owned catalog transaction")
    plan = plan if isinstance(plan, TutorialMigrationPlan) else TutorialMigrationPlan(plan)
    metadata = plan.metadata()
    if metadata.get("status") != "ready":
        raise ValueError("Tutorial migration plan is not ready")
    database = next((row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"), None)
    if not database or Path(database).resolve() != Path(metadata["source_catalog"]).resolve():
        raise ValueError("Tutorial plan belongs to a different catalog")
    if _generation(db) != metadata["source_generation"]:
        raise ValueError("Catalog changed after the tutorial migration plan was built")
    with closing(_connect(plan.path, read_only=True)) as staged:
        for owner in staged.execute("SELECT * FROM owners ORDER BY library_id"):
            current = db.execute("SELECT body FROM libraries WHERE id=?", (owner["library_id"],)).fetchone()
            if not current or _sha(current[0]) != owner["body_sha256"]:
                raise ValueError("Library ownership/lifecycle changed after tutorial planning")
        if verify_sources:
            validate_tutorial_plan_sources(plan)
        ensure_tutorial_association_schema(db)
        for item in plan.iter_retirements():
            check_cancelled()
            result = db.execute("DELETE FROM active WHERE library_id=? AND object_id=? AND citation_id=?",
                                (item["library_id"], item["object_id"], item["id"]))
            if result.rowcount != 1:
                raise ValueError("Planned retirement is no longer the active revision")
            db.execute('INSERT OR REPLACE INTO tutorial_retired_sources VALUES(?,?,?,?)',
                       (item['id'], item['library_id'], item['reason'], item['source_sha256']))
        for item in plan.iter_canonical_objects():
            existing = db.execute("SELECT payload FROM revisions WHERE id=?", (item["id"],)).fetchone()
            payload = _json(item)
            if existing and existing[0] != payload:
                raise ValueError("Canonical citation identity already has another payload")
            if not existing:
                db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (item["id"], item["object_id"], item["library_id"], payload))
            db.execute("INSERT INTO active VALUES(?,?,?)", (item["library_id"], item["object_id"], item["id"]))
        for relation in plan.iter_relations():
            db.execute("INSERT OR REPLACE INTO tutorial_associations VALUES(?,?,?,?,?,?,?)", (
                relation["source_id"], relation["source_library_id"], relation["target_id"], relation["target_library_id"],
                relation["relation"], relation["source_sha256"], metadata["migration_id"]))
        for source in plan.iter_document_sources():
            db.execute("INSERT OR REPLACE INTO tutorial_document_sources VALUES(?,?,?,?,?,?)", (
                source["source_sha256"], source["identity"], source["owner_library_id"], source["citation_id"],
                source["anchor"], source["category"]))
    return plan.summary()
