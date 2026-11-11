"""Experimental single-MPH storage; existing plugin libraries do not use it.

Typed saved values are canonical SQLite rows. Feature evidence is a structural
pointer into the unchanged original MPH, opened only when evidence is requested.
Optional compact API observations use the same canonical values. This storage
module never builds geometry, meshes, solvers, embeddings or model copies.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid
import xml.etree.ElementTree as ET
import zipfile

FORMAT = "compact-mph-case/prototype-v1"
DATABASE = "compactcase.sqlite3"
_BRANCH_TERMS = {"mesh": "mesh meshing 网格 網格", "geometry": "geometry 几何 幾何",
                 "physics": "physics 物理场 物理場", "material": "material 材料",
                 "selection": "selection 选择 選擇", "study": "study 研究",
                 "solver": "solver 求解器", "result": "result 结果 結果",
                 "param": "parameter 参数 參數", "multiphysics": "multiphysics 多物理场 多物理場"}


class CompactCaseIntegrityError(ValueError):
    """The prototype's canonical XML or format cannot be verified."""


class RawEvidenceUnavailable(FileNotFoundError):
    """Normalized information remains readable, but its original is offline."""

    def __init__(self, message, *, reason="original_mph_missing"):
        super().__init__(message)
        self.reason = reason


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _signature(parser, explicit):
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit:
            raise ValueError("signature must be a nonempty string")
        return explicit
    module = importlib.import_module(parser.__module__)
    declared = getattr(module, "parser_signature", None)
    if callable(declared):
        return declared()
    source = inspect.getsourcefile(parser)
    if not source:
        raise ValueError("A custom parser without source requires an explicit signature")
    return _hash_file(source) + ":" + parser.__qualname__


def _preview(value, maximum=80):
    """A small lexical hint; the complete typed value stays in value_pool."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(_preview(child, maximum) for child in list(value.values())[:4])[:maximum]
    if isinstance(value, (list, tuple)):
        return " ".join(_preview(child, maximum) for child in value[:4])[:maximum]
    return str(value)[:maximum]


def _selection_names(value, named=False):
    if isinstance(value, dict):
        return " ".join(_selection_names(child, named or bool(re.search(r"name|reference|object|refs", key, re.I)))
                        for key, child in value.items())
    if isinstance(value, list):
        return " ".join(_selection_names(child, named) for child in value)
    return str(value) if named and isinstance(value, str) else ""


def _schema(db, *, native_index=True):
    db.executescript("""
      PRAGMA foreign_keys=ON;
      CREATE TABLE symbols(id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE);
      CREATE TABLE value_pool(id INTEGER PRIMARY KEY, value_json TEXT NOT NULL UNIQUE);
      CREATE TABLE metadata_pool(id INTEGER PRIMARY KEY, body_json TEXT NOT NULL UNIQUE);
      CREATE TABLE model(id INTEGER PRIMARY KEY CHECK(id=1), format TEXT NOT NULL,
        source_path TEXT NOT NULL, source_sha256 TEXT NOT NULL, source_size_bytes INTEGER NOT NULL,
        parser_signature TEXT NOT NULL, xml_member TEXT,
        xml_sha256 TEXT, xml_size_bytes INTEGER NOT NULL,
        metadata_id INTEGER REFERENCES metadata_pool, coverage_id INTEGER REFERENCES metadata_pool,
        contract_id INTEGER REFERENCES metadata_pool);
      CREATE TABLE nodes(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES nodes DEFERRABLE INITIALLY DEFERRED,
        xml_route TEXT NOT NULL, tag_id INTEGER REFERENCES symbols, kind_id INTEGER REFERENCES symbols,
        label_id INTEGER REFERENCES symbols, type_id INTEGER REFERENCES symbols, branch_id INTEGER REFERENCES symbols,
        metadata_id INTEGER REFERENCES metadata_pool);
      CREATE TABLE properties(id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL REFERENCES nodes,
        ordinal INTEGER NOT NULL, child_index INTEGER NOT NULL CHECK(child_index>=0),
        name_id INTEGER REFERENCES symbols, status_id INTEGER REFERENCES symbols,
        type_id INTEGER REFERENCES symbols, value_id INTEGER REFERENCES value_pool,
        metadata_id INTEGER REFERENCES metadata_pool, UNIQUE(node_id,ordinal));
      CREATE TABLE selections(id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL REFERENCES nodes,
        ordinal INTEGER NOT NULL, child_index INTEGER NOT NULL CHECK(child_index>=0),
        kind_id INTEGER REFERENCES symbols, status_id INTEGER REFERENCES symbols,
        value_id INTEGER NOT NULL REFERENCES value_pool, metadata_id INTEGER REFERENCES metadata_pool,
        UNIQUE(node_id,ordinal));
      CREATE TABLE documents(id INTEGER PRIMARY KEY, source_uri TEXT, source_sha256 TEXT,
        kind_id INTEGER REFERENCES symbols, metadata_id INTEGER REFERENCES metadata_pool);
      CREATE TABLE text_pool(id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE);
      CREATE TABLE passages(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents,
        ordinal INTEGER NOT NULL, title_id INTEGER REFERENCES symbols, kind_id INTEGER REFERENCES symbols,
        text_id INTEGER NOT NULL REFERENCES text_pool, metadata_id INTEGER REFERENCES metadata_pool);
      CREATE INDEX properties_node ON properties(node_id);
      CREATE INDEX selections_node ON selections(node_id);
      CREATE INDEX passages_document ON passages(document_id);
    """)
    if native_index:
        db.execute("CREATE VIRTUAL TABLE native_fts USING fts5(title,keywords,content='',tokenize='unicode61')")


def _populate(db, parsed, source, source_hash, signature, *, native_index=True):
    pools = {"symbols": {}, "value_pool": {}, "metadata_pool": {}, "text_pool": {}}

    def intern(table, value):
        if value is None:
            return None
        column = {"symbols": "value", "value_pool": "value_json", "metadata_pool": "body_json", "text_pool": "text"}[table]
        text = _json(value) if table in {"value_pool", "metadata_pool"} else str(value)
        if text not in pools[table]:
            cursor = db.execute(f"INSERT INTO {table}({column}) VALUES(?)", (text,))
            pools[table][text] = cursor.lastrowid
        return pools[table][text]

    symbol = lambda value: intern("symbols", value)
    metadata = lambda value: intern("metadata_pool", value) if value else None
    case = parsed["case"]
    details = {key: value for key, value in case.items() if key not in {
        "source_path", "source_sha256", "source_size_bytes", "source_member", "coverage", "parser_contract", "parser_signature"}}
    raw = parsed["raw_xml"]
    db.execute("INSERT INTO model VALUES(1,?,?,?,?,?,?,?,?,?,?,?)", (
        FORMAT, str(source), source_hash, source.stat().st_size, signature, "dmodel.xml" if raw else None,
        hashlib.sha256(raw).hexdigest() if raw else None, len(raw), metadata(details), metadata(case.get("coverage")), metadata(case.get("parser_contract"))))
    index = {}
    for node in parsed["nodes"]:
        route = node.get("xml_route", [])
        if not isinstance(route, list) or any(type(part) is not int or part < 0 for part in route):
            raise ValueError("Node xml_route must contain zero-based child indices")
        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?)", (
            node["id"], node.get("parent_id"), _json(route), symbol(node.get("tag")), symbol(node.get("kind")),
            symbol(node.get("label")), symbol(node.get("type")), symbol(node.get("feature_branch")),
            metadata({**({"attributes": node["attributes"]} if node.get("attributes") else {}),
                      **({"truncated": True} if node.get("truncated") else {})})))
        branch = node.get("feature_branch", "")
        title = " ".join(str(node.get(key) or "") for key in ("label", "tag", "kind", "type"))
        index[node["id"]] = [title, [_BRANCH_TERMS.get(branch, str(branch or ""))]]
    for prop in parsed["properties"]:
        # The parser contract intentionally excludes raw XML, coverage, source
        # hashes and invariant warnings from these per-property metadata rows.
        extra = {key: prop[key] for key in ("issues", "truncated", "value_omitted") if prop.get(key)}
        value = prop.get("value")
        db.execute("INSERT INTO properties(node_id,ordinal,child_index,name_id,status_id,type_id,value_id,metadata_id) VALUES(?,?,?,?,?,?,?,?)", (
            prop["node_id"], prop["ordinal"], prop["child_index"], symbol(prop.get("name")), symbol(prop.get("status")),
            symbol(prop.get("value_type")), intern("value_pool", value), metadata(extra)))
        # Every actual name is included, even on nodes with many properties.
        index[prop["node_id"]][1].append(str(prop.get("name") or "") + " " + _preview(value))
    for selection in parsed["selections"]:
        value = selection.get("value")
        if value is None or value == {} or value == [] or value == "":
            continue
        db.execute("INSERT INTO selections(node_id,ordinal,child_index,kind_id,status_id,value_id,metadata_id) VALUES(?,?,?,?,?,?,?)", (
            selection["node_id"], selection["ordinal"], selection["child_index"], symbol(selection.get("kind")), symbol(selection.get("status")),
            intern("value_pool", value), metadata({"truncated": True} if selection.get("truncated") else {})))
        index[selection["node_id"]][1].append(str(selection.get("kind") or "") + " " + _selection_names(value) + " " + _preview(value))
    for document in parsed.get("documents", []):
        extra = {key: value for key, value in document.items() if key not in {"id", "source_uri", "source_sha256", "kind", "text"}}
        db.execute("INSERT INTO documents VALUES(?,?,?,?,?)", (document["id"], document.get("source_uri"), document.get("source_sha256"),
                   symbol(document.get("kind")), metadata(extra)))
    for passage in parsed.get("passages", []):
        extra = {key: value for key, value in passage.items() if key not in {"id", "document_id", "ordinal", "title", "kind", "text"}}
        db.execute("INSERT INTO passages VALUES(?,?,?,?,?,?,?)", (passage["id"], passage["document_id"], passage["ordinal"],
                   symbol(passage.get("title")), symbol(passage.get("kind")), intern("text_pool", passage.get("text", "")), metadata(extra)))
    if native_index:
        db.executemany("INSERT INTO native_fts(rowid,title,keywords) VALUES(?,?,?)", ((identity, title, " ".join(terms)) for identity, (title, terms) in index.items()))
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Parser returned a dangling compact-case reference")


def _has_api(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_runs'").fetchone() is not None


def _api_schema(db):
    # These tables are optional. Existing offline prototype files stay readable.
    db.executescript("""
      PRAGMA foreign_keys=ON;
      CREATE TABLE IF NOT EXISTS api_runs(id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
        metadata_id INTEGER REFERENCES metadata_pool);
      CREATE TABLE IF NOT EXISTS api_nodes(id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL REFERENCES nodes,
        path_id INTEGER NOT NULL UNIQUE REFERENCES symbols);
      CREATE TABLE IF NOT EXISTS api_observations(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES api_runs,
        api_node_id INTEGER NOT NULL REFERENCES api_nodes, name_id INTEGER NOT NULL REFERENCES symbols,
        kind_id INTEGER REFERENCES symbols,
        getter_id INTEGER REFERENCES symbols, status_id INTEGER REFERENCES symbols, type_id INTEGER REFERENCES symbols,
        value_id INTEGER REFERENCES value_pool, metadata_id INTEGER REFERENCES metadata_pool,
        saved_property_id INTEGER REFERENCES properties, comparison_id INTEGER REFERENCES symbols,
        relation_id INTEGER REFERENCES symbols, xml_attribute_id INTEGER REFERENCES symbols,
        UNIQUE(run_id,api_node_id,name_id,getter_id));
      CREATE INDEX IF NOT EXISTS api_observations_node ON api_observations(api_node_id);
    """)


def _intern_existing(db, table, value):
    if value is None:
        return None
    column = {"symbols": "value", "value_pool": "value_json", "metadata_pool": "body_json"}[table]
    text = _json(value) if table != "symbols" else str(value)
    db.execute(f"INSERT OR IGNORE INTO {table}({column}) VALUES(?)", (text,))
    return db.execute(f"SELECT id FROM {table} WHERE {column}=?", (text,)).fetchone()[0]


def _api_complete(observation):
    value = observation.get("value")
    return (observation.get("status") in {"observed", "decoded", "decoded_saved"}
            and not observation.get("value_omitted") and not observation.get("truncated")
            and not (isinstance(value, dict) and value.get("source_only"))
            and observation.get("value_complete", True) is not False)


def _refresh_native(db):
    """Rebuild only lexical postings; contentless FTS retains no copied body."""
    pool = CompactCase._pool
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='native_fts'").fetchone():
        return
    db.execute("INSERT INTO native_fts(native_fts) VALUES('delete-all')")
    for node in db.execute("SELECT * FROM nodes ORDER BY id"):
        title = " ".join(str(pool(db, "symbols", node[key + "_id"]) or "") for key in ("label", "tag", "kind", "type"))
        branch = pool(db, "symbols", node["branch_id"]) or ""
        terms = [_BRANCH_TERMS.get(branch, branch)]
        for prop in db.execute("SELECT * FROM properties WHERE node_id=?", (node["id"],)):
            terms.append(str(pool(db, "symbols", prop["name_id"]) or "") + " " + _preview(pool(db, "value_pool", prop["value_id"])))
        for selection in db.execute("SELECT * FROM selections WHERE node_id=?", (node["id"],)):
            value = pool(db, "value_pool", selection["value_id"])
            terms.append(str(pool(db, "symbols", selection["kind_id"]) or "") + " " + _selection_names(value) + " " + _preview(value))
        if _has_api(db):
            for observation in db.execute("SELECT o.* FROM api_observations o JOIN api_nodes n ON n.id=o.api_node_id WHERE n.node_id=?", (node["id"],)):
                terms.append(str(pool(db, "symbols", observation["name_id"]) or "") + " " + _preview(pool(db, "value_pool", observation["value_id"])))
        db.execute("INSERT INTO native_fts(rowid,title,keywords) VALUES(?,?,?)", (node["id"], title, " ".join(dict.fromkeys(terms))))


def add_compact_api(output, bundle=None, *, extractor=None, force=False, source_override=None, **extractor_options):
    """Attach selected getter facts atomically, without replacing saved values.

    An extractor receives the CompactCase reader and returns a compact bundle.
    Getter execution and selection finish before any database write is staged.
    """
    reader = CompactCase(output, source_override=source_override)
    original = reader.read_case()
    if type(force) is not bool:
        raise ValueError("force must be a boolean")
    if bundle is not None and extractor is not None:
        raise ValueError("Supply a compact API bundle or an extractor, not both")
    if bundle is None:
        if extractor is None:
            from . import compact_api
            # Only the normal getter path may reuse a previous read before
            # execution. An explicitly supplied extractor/runner always runs.
            if not force and extractor_options.get("runner") is None:
                policy = compact_api.current_runtime_policy()
                signature = compact_api.parser_signature()
                matching = [run for run in original["api"]["runs"] if run.get("status") in {"observed", "partial"}
                            and run.get("selector_signature") == signature and run.get("runtime_policy") == policy]
                if matching:
                    source = Path(original["source_path"])
                    if source.is_file() and _hash_file(source) == original["source_sha256"]:
                        return dict(original, api_cache_hit=True, api_extraction_reused=True)
            extractor = compact_api.extract_compact_api
        bundle = extractor(reader, **extractor_options)
    if not isinstance(bundle, dict) or bundle.get("source_sha256") != original["source_sha256"]:
        raise CompactCaseIntegrityError("API observations do not belong to this original MPH")
    if (bundle.get("run") or {}).get("status") in {"failed", "cancelled", "interrupted"}:
        raise ValueError("API extractor did not complete; the compact case was preserved")
    nodes, observations = bundle.get("nodes", []), bundle.get("observations", [])
    if not isinstance(nodes, list) or not isinstance(observations, list):
        raise ValueError("API nodes and observations must be compact row lists")
    # A timing-only change is not a new set of facts or a reason to duplicate
    # rows. Forced getter execution can still reuse identical canonical facts.
    timing = {"started_at", "finished_at", "updated_at", "observed_at", "timestamp", "elapsed_seconds", "duration_seconds"}
    identity = dict(bundle, run={key: value for key, value in (bundle.get("run") or {}).items() if key not in timing})
    fingerprint = hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()
    with closing(reader._connect()) as db:
        if _has_api(db) and db.execute("SELECT 1 FROM api_runs WHERE fingerprint=?", (fingerprint,)).fetchone():
            return dict(original, api_cache_hit=True, api_extraction_reused=False)
    source = Path(original["source_path"])
    if source.is_file() and _hash_file(source) != original["source_sha256"]:
        raise CompactCaseIntegrityError("Original MPH changed before API observations were attached")
    stage = reader.root / (".a-" + uuid.uuid4().hex[:12])
    stage.mkdir()
    try:
        with closing(reader._connect()) as old, closing(sqlite3.connect(stage / DATABASE)) as db:
            old.backup(db)
            db.row_factory = sqlite3.Row
            _api_schema(db)
            with db:
                metadata = dict(bundle.get("run") or {})
                for key in ("source_sha256", "source_path", "source_size_bytes"):
                    metadata.pop(key, None)
                run_id = db.execute("INSERT INTO api_runs(fingerprint,metadata_id) VALUES(?,?)", (
                    fingerprint, _intern_existing(db, "metadata_pool", metadata) if metadata else None)).lastrowid
                node_map = {}
                paths = {}
                for node in nodes:
                    node_id, api_path = node["compact_node_id"], node["api_path"]
                    if not db.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone() or not isinstance(api_path, str) or not api_path:
                        raise ValueError("API node mapping is not a retained compact feature")
                    if api_path in paths and paths[api_path] != node_id:
                        raise ValueError("Ambiguous API path mapping")
                    paths[api_path] = node_id
                    path_id = _intern_existing(db, "symbols", api_path)
                    existing = db.execute("SELECT id,node_id FROM api_nodes WHERE path_id=?", (path_id,)).fetchone()
                    if existing and existing["node_id"] != node_id:
                        raise ValueError("API path was previously mapped to another compact feature")
                    if node["id"] in node_map:
                        raise ValueError("Duplicate API node identity")
                    node_map[node["id"]] = existing["id"] if existing else db.execute(
                        "INSERT INTO api_nodes(node_id,path_id) VALUES(?,?)", (node_id, path_id)).lastrowid
                for observation in observations:
                    mapped = node_map.get(observation["api_node_id"])
                    name = observation.get("name")
                    if mapped is None or not isinstance(name, str) or not name:
                        raise ValueError("API observation has no unambiguous selected node/property")
                    node_id = db.execute("SELECT node_id FROM api_nodes WHERE id=?", (mapped,)).fetchone()[0]
                    name_id = _intern_existing(db, "symbols", name)
                    saved = db.execute("SELECT * FROM properties WHERE node_id=? AND name_id=?", (node_id, name_id)).fetchall()
                    if observation.get("xml_property_ordinal") is not None:
                        saved = [row for row in saved if row["ordinal"] == observation["xml_property_ordinal"]]
                    if "value" not in observation:
                        if observation.get("relation") == "corroborates_saved_xml":
                            if len(saved) != 1:
                                raise ValueError("A corroborated API observation must reference one saved property")
                            saved_details = reader._pool(db, "metadata_pool", saved[0]["metadata_id"]) or {}
                            if (reader._pool(db, "symbols", saved[0]["status_id"]) not in {"decoded", "decoded_saved"}
                                    or saved_details.get("value_omitted") or saved_details.get("truncated")):
                                raise ValueError("An incomplete saved value cannot supply a complete API observation")
                            value_id = saved[0]["value_id"]
                        elif not _api_complete(observation):
                            value_id = None
                        else:
                            raise ValueError("An observed API value is missing its typed payload")
                    else:
                        value_id = _intern_existing(db, "value_pool", observation["value"])
                    saved_id, comparison = None, "api_only"
                    if len(saved) == 1:
                        saved_id = saved[0]["id"]
                        saved_metadata = reader._pool(db, "metadata_pool", saved[0]["metadata_id"]) or {}
                        saved_status = reader._pool(db, "symbols", saved[0]["status_id"])
                        if (_api_complete(observation) and saved_status in {"decoded", "decoded_saved"}
                                and not saved_metadata.get("value_omitted") and not saved_metadata.get("truncated")):
                            comparison = "same_saved_value" if saved[0]["value_id"] == value_id else "differs_from_saved"
                        else:
                            comparison = "not_comparable"
                    elif len(saved) > 1:
                        comparison = "ambiguous_saved_property"
                    extra = {key: observation[key] for key in ("issues", "truncated", "value_omitted", "read_error", "reason") if observation.get(key)}
                    if observation.get("value_complete") is False:
                        extra["value_complete"] = False
                    db.execute("INSERT INTO api_observations(run_id,api_node_id,name_id,kind_id,getter_id,status_id,type_id,value_id,metadata_id,saved_property_id,comparison_id,relation_id,xml_attribute_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        run_id, mapped, name_id, _intern_existing(db, "symbols", observation.get("kind", "property")), _intern_existing(db, "symbols", observation.get("getter")),
                        _intern_existing(db, "symbols", observation.get("status")), _intern_existing(db, "symbols", observation.get("value_type")),
                        value_id, _intern_existing(db, "metadata_pool", extra) if extra else None, saved_id, _intern_existing(db, "symbols", comparison),
                        _intern_existing(db, "symbols", observation.get("relation")), _intern_existing(db, "symbols", observation.get("xml_attribute"))))
                _refresh_native(db)
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Dangling API observation reference")
        if source.is_file() and _hash_file(source) != original["source_sha256"]:
            raise CompactCaseIntegrityError("Original MPH changed while API observations were attached")
        os.replace(stage / DATABASE, reader.db_path)
        return dict(reader.read_case(), api_cache_hit=False, api_extraction_reused=False)
    finally:
        if stage.parent != reader.root or not stage.name.startswith(".a-"):
            raise RuntimeError("Refusing to remove API staging outside the case output")
        shutil.rmtree(stage)


def build_compact_case(source, output_dir, *, docs_root=None, parser=None, signature=None, native_index=True):
    """Build this experimental format, or reuse a verified unchanged case."""
    source, output = Path(source).resolve(strict=True), Path(output_dir).resolve()
    if not source.is_file() or source.suffix.lower() != ".mph":
        raise ValueError("source must be an original MPH file")
    if source.is_relative_to(output):
        raise ValueError("Use a separate output directory that does not contain the MPH source")
    if type(native_index) is not bool:
        raise ValueError("native_index must be a boolean")
    if parser is None:
        from .compact_model import parse_compact_model
        parser = parse_compact_model
    signature = _signature(parser, signature)
    source_hash = _hash_file(source)
    if (output / DATABASE).is_file():
        try:
            current = CompactCase(output).read_case()
            if (current["source_sha256"] == source_hash and current["parser_signature"] == signature
                    and current["native_index"] == native_index):
                return dict(current, cache_hit=True)
        except (CompactCaseIntegrityError, sqlite3.DatabaseError, OSError):
            pass
    parsed = parser(source, docs_root=Path(docs_root).resolve() if docs_root is not None else None)
    if not isinstance(parsed.get("raw_xml"), bytes):
        raise ValueError("Parser must provide raw XML bytes, empty only for a genuine preview model")
    if not parsed["raw_xml"]:
        preview = parsed["case"].get("coverage", {}).get("source_container", {}).get("status") == "preview_only"
        if not preview or any(parsed.get(key) for key in ("nodes", "properties", "selections")):
            raise ValueError("Missing model XML is supported only for a preview without settings")
        with zipfile.ZipFile(source) as container:
            info = container.getinfo("modelinfo.xml")
            if ("dmodel.xml" in container.namelist() or info.file_size > 1024 * 1024
                    or ET.fromstring(container.read(info)).get("nodeType", "").lower() != "preview"):
                raise ValueError("Original MPH is not a genuine preview without model settings")
    if parsed["case"].get("source_sha256", source_hash) != source_hash or _hash_file(source) != source_hash:
        raise ValueError("MPH source changed while being parsed")
    if parsed["case"].get("parser_signature", signature) != signature:
        raise ValueError("Parser signature changed while parsing")
    if parsed["raw_xml"]:
        ET.fromstring(parsed["raw_xml"])
    output.mkdir(parents=True, exist_ok=True)
    stage = output / (".b-" + uuid.uuid4().hex[:12])
    stage.mkdir()
    try:
        with closing(sqlite3.connect(stage / DATABASE)) as db, db:
            _schema(db, native_index=native_index)
            _populate(db, parsed, source, source_hash, signature, native_index=native_index)
        # Validate every structural pointer before exposing the new database.
        staged = CompactCase(stage)
        staged.validate_evidence_routes(parsed["raw_xml"])
        os.replace(stage / DATABASE, output / DATABASE)
        return dict(CompactCase(output).read_case(), cache_hit=False)
    finally:
        if stage.parent != output or not stage.name.startswith(".b-"):
            raise RuntimeError("Refusing to remove a staging directory outside the case output")
        shutil.rmtree(stage)


class CompactCase:
    """Read the prototype independently of the original MPH's availability."""
    def __init__(self, output, *, source_override=None):
        path = Path(output).resolve()
        self.root = path.parent if path.name == DATABASE else path
        self.db_path = self.root / DATABASE
        self.source_override = Path(source_override).resolve() if source_override is not None else None

    def _connect(self):
        db = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    def _model(self, db):
        row = db.execute("SELECT * FROM model WHERE id=1").fetchone()
        if not row or row["format"] != FORMAT:
            raise CompactCaseIntegrityError("Not a supported compact-case prototype")
        if row["xml_member"] is None:
            coverage = self._pool(db, "metadata_pool", row["coverage_id"]) or {}
            if (coverage.get("source_container", {}).get("status") != "preview_only" or row["xml_size_bytes"] != 0
                    or any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in ("nodes", "properties", "selections"))):
                raise CompactCaseIntegrityError("A missing XML member is valid only for a settings-free preview")
        elif row["xml_member"] != "dmodel.xml":
            raise CompactCaseIntegrityError("Unexpected MPH evidence member")
        return row

    @staticmethod
    def _pool(db, table, identity):
        if identity is None:
            return None
        column = {"symbols": "value", "value_pool": "value_json", "metadata_pool": "body_json", "text_pool": "text"}[table]
        row = db.execute(f"SELECT {column} FROM {table} WHERE id=?", (identity,)).fetchone()
        if not row:
            raise CompactCaseIntegrityError("A canonical value reference is missing")
        return json.loads(row[0]) if table in {"value_pool", "metadata_pool"} else row[0]

    @staticmethod
    def _ref(model):
        return "compact-case:" + model["source_sha256"][:24]

    def read_case(self):
        with closing(self._connect()) as db:
            model = self._model(db)
            result = dict(self._pool(db, "metadata_pool", model["metadata_id"]) or {})
            result.update({key: model[key] for key in ("format", "source_path", "source_sha256", "source_size_bytes", "parser_signature",
                          "xml_member", "xml_sha256", "xml_size_bytes")})
            result.update(case_ref=self._ref(model), coverage=self._pool(db, "metadata_pool", model["coverage_id"]) or {},
                          parser_contract=self._pool(db, "metadata_pool", model["contract_id"]) or {},
                          counts={table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                                  for table in ("nodes", "properties", "selections", "documents", "passages")},
                          source_available=(self.source_override or Path(model["source_path"])).is_file(),
                          native_index=db.execute("SELECT 1 FROM sqlite_master WHERE name='native_fts'").fetchone() is not None)
            if self.source_override is not None:
                result["source_path"] = str(self.source_override)
            result["api"] = self._api_summary(db)
            return result

    def _api_summary(self, db):
        if not _has_api(db):
            return {"status": "not_requested", "runs": [], "counts": {"runs": 0, "nodes": 0, "observations": 0}}
        runs = []
        for row in db.execute("SELECT * FROM api_runs ORDER BY id"):
            runs.append(dict(self._pool(db, "metadata_pool", row["metadata_id"]) or {}, id=row["id"],
                             observations=db.execute("SELECT count(*) FROM api_observations WHERE run_id=?", (row["id"],)).fetchone()[0]))
        return {"status": "recorded", "runs": runs,
                "counts": {"runs": len(runs), "nodes": db.execute("SELECT count(*) FROM api_nodes").fetchone()[0],
                           "observations": db.execute("SELECT count(*) FROM api_observations").fetchone()[0]}}

    def _node(self, db, node_id, model):
        row = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not row:
            raise KeyError("Node not found")
        result = {"id": row["id"], "parent_id": row["parent_id"], "xml_route": json.loads(row["xml_route"]), "case_ref": self._ref(model)}
        for key in ("tag", "kind", "label", "type", "branch"):
            result["feature_branch" if key == "branch" else key] = self._pool(db, "symbols", row[key + "_id"])
        result.update(self._pool(db, "metadata_pool", row["metadata_id"]) or {})
        return result

    def _property(self, db, row, model):
        result = {key: row[key] for key in ("id", "node_id", "ordinal", "child_index")}
        result.update(name=self._pool(db, "symbols", row["name_id"]), status=self._pool(db, "symbols", row["status_id"]),
                      value_type=self._pool(db, "symbols", row["type_id"]), value=self._pool(db, "value_pool", row["value_id"]), case_ref=self._ref(model))
        result.update(self._pool(db, "metadata_pool", row["metadata_id"]) or {})
        result["decoded"] = result["status"] in {"decoded", "decoded_saved"} and not result.get("value_omitted")
        result["value_complete"] = result["decoded"] and not result.get("truncated", False)
        return result

    def _selection(self, db, row, model):
        result = {key: row[key] for key in ("id", "node_id", "ordinal", "child_index")}
        result.update(kind=self._pool(db, "symbols", row["kind_id"]), status=self._pool(db, "symbols", row["status_id"]),
                      value=self._pool(db, "value_pool", row["value_id"]), case_ref=self._ref(model))
        result.update(self._pool(db, "metadata_pool", row["metadata_id"]) or {})
        return result

    def _api_observation(self, db, row, model):
        mapping = db.execute("SELECT * FROM api_nodes WHERE id=?", (row["api_node_id"],)).fetchone()
        result = {key: row[key] for key in ("id", "run_id", "api_node_id", "saved_property_id")}
        result.update(node_id=mapping["node_id"], api_path=self._pool(db, "symbols", mapping["path_id"]),
                      case_ref=self._ref(model), source="api_getter_observation", value=self._pool(db, "value_pool", row["value_id"]))
        for key, column in (("name", "name_id"), ("kind", "kind_id"), ("getter", "getter_id"), ("status", "status_id"),
                            ("value_type", "type_id"), ("comparison", "comparison_id"), ("relation", "relation_id"), ("xml_attribute", "xml_attribute_id")):
            result[key] = self._pool(db, "symbols", row[column])
        result.update(self._pool(db, "metadata_pool", row["metadata_id"]) or {})
        result["value_complete"] = _api_complete(result)
        result["decoded"] = result["value_complete"]
        return result

    def read_node(self, node_id):
        with closing(self._connect()) as db:
            model = self._model(db)
            result = self._node(db, node_id, model)
            result["properties"] = [self._property(db, row, model) for row in db.execute("SELECT * FROM properties WHERE node_id=? ORDER BY ordinal", (node_id,))]
            result["selections"] = [self._selection(db, row, model) for row in db.execute("SELECT * FROM selections WHERE node_id=? ORDER BY ordinal", (node_id,))]
            result["children"] = [row[0] for row in db.execute("SELECT id FROM nodes WHERE parent_id=? ORDER BY id", (node_id,))]
            result["api_observations"] = ([self._api_observation(db, row, model) for row in db.execute(
                "SELECT o.* FROM api_observations o JOIN api_nodes n ON n.id=o.api_node_id WHERE n.node_id=? ORDER BY o.run_id DESC,o.id", (node_id,))]
                if _has_api(db) else [])
            return result

    def get_property(self, node_id, name=None, *, ordinal=None, property_id=None, source="xml", run_id=None, getter=None):
        with closing(self._connect()) as db:
            model = self._model(db)
            if source == "api":
                if not _has_api(db):
                    raise KeyError("No API observations have been recorded")
                query = ("SELECT o.* FROM api_observations o JOIN api_nodes n ON n.id=o.api_node_id "
                         "JOIN symbols s ON s.id=o.name_id LEFT JOIN symbols g ON g.id=o.getter_id WHERE n.node_id=?")
                parameters = [node_id]
                for condition, value in (("s.value", name), ("o.id", property_id), ("o.run_id", run_id), ("g.value", getter)):
                    if value is not None:
                        query += " AND " + condition + "=?"
                        parameters.append(value)
                rows = db.execute(query + " ORDER BY o.run_id DESC,o.id", parameters).fetchall()
                if rows and run_id is None:
                    rows = [row for row in rows if row["run_id"] == rows[0]["run_id"]]
                if len(rows) != 1:
                    raise KeyError("API property not found" if not rows else "API property is ambiguous; specify its getter or observation ID")
                return self._api_observation(db, rows[0], model)
            if source != "xml":
                raise ValueError("source must be xml or api")
            query, parameters = "SELECT p.* FROM properties p LEFT JOIN symbols s ON s.id=p.name_id WHERE p.node_id=?", [node_id]
            for condition, value in (("s.value", name), ("p.ordinal", ordinal), ("p.id", property_id)):
                if value is not None:
                    query += " AND " + condition + "=?"
                    parameters.append(value)
            rows = db.execute(query, parameters).fetchall()
            if len(rows) != 1:
                raise KeyError("Property not found" if not rows else "Property name is ambiguous; specify ordinal or property_id")
            return self._property(db, rows[0], model)

    def add_api(self, bundle=None, *, extractor=None, force=False, **kwargs):
        return add_compact_api(self.root, bundle, extractor=extractor, force=force, source_override=self.source_override, **kwargs)

    def read_api_evidence(self, observation_id):
        with closing(self._connect()) as db:
            model = self._model(db)
            if not _has_api(db):
                raise KeyError("No API observations have been recorded")
            row = db.execute("SELECT * FROM api_observations WHERE id=?", (observation_id,)).fetchone()
            if not row:
                raise KeyError("API observation not found")
            observation = self._api_observation(db, row, model)
            run = db.execute("SELECT metadata_id FROM api_runs WHERE id=?", (row["run_id"],)).fetchone()
            return {"status": "recorded", "evidence_kind": "api_getter_observation", "case_ref": self._ref(model),
                    "source_sha256": model["source_sha256"], "api_path": observation["api_path"], "name": observation["name"],
                    "getter": observation["getter"], "run_id": row["run_id"], "run": self._pool(db, "metadata_pool", run[0]) or {},
                    "xml_evidence": False, "saved_property_id": row["saved_property_id"]}

    def iter_nodes(self):
        # Keep only node identities while yielding independently readable rows.
        with closing(self._connect()) as db:
            self._model(db)
            identities = [row[0] for row in db.execute("SELECT id FROM nodes ORDER BY id")]
        for identity in identities:
            yield self.read_node(identity)

    def read_document(self, document_id):
        with closing(self._connect()) as db:
            model = self._model(db)
            row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if not row:
                raise KeyError("Document not found")
            result = {key: row[key] for key in ("id", "source_uri", "source_sha256")}
            result.update(kind=self._pool(db, "symbols", row["kind_id"]), case_ref=self._ref(model))
            result.update(self._pool(db, "metadata_pool", row["metadata_id"]) or {})
            result["passages"] = []
            for passage in db.execute("SELECT * FROM passages WHERE document_id=? ORDER BY ordinal", (document_id,)):
                item = {key: passage[key] for key in ("id", "document_id", "ordinal")}
                item.update(title=self._pool(db, "symbols", passage["title_id"]), kind=self._pool(db, "symbols", passage["kind_id"]),
                            text=self._pool(db, "text_pool", passage["text_id"]))
                item.update(self._pool(db, "metadata_pool", passage["metadata_id"]) or {})
                result["passages"].append(item)
            return result

    def read_passage(self, passage_id):
        with closing(self._connect()) as db:
            model = self._model(db)
            row = db.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
            if not row:
                raise KeyError("Passage not found")
            result = {key: row[key] for key in ("id", "document_id", "ordinal")}
            result.update(title=self._pool(db, "symbols", row["title_id"]), kind=self._pool(db, "symbols", row["kind_id"]),
                          text=self._pool(db, "text_pool", row["text_id"]), case_ref=self._ref(model))
            result.update(self._pool(db, "metadata_pool", row["metadata_id"]) or {})
            return result

    def iter_passages(self):
        with closing(self._connect()) as db:
            self._model(db)
            identities = [row[0] for row in db.execute("SELECT id FROM passages ORDER BY id")]
        for identity in identities:
            yield self.read_passage(identity)

    def read_raw_xml(self):
        with closing(self._connect()) as db:
            model = self._model(db)
            if model["xml_member"] is None:
                raise RawEvidenceUnavailable("Preview model has no saved settings XML", reason="preview_model_no_settings")
            source = self.source_override or Path(model["source_path"])
            if not source.is_file():
                raise RawEvidenceUnavailable("Original MPH is unavailable; normalized information is still readable")
            if _hash_file(source) != model["source_sha256"]:
                raise CompactCaseIntegrityError("Original MPH changed; raw evidence was refused")
            try:
                with zipfile.ZipFile(source) as container:
                    info = container.getinfo(model["xml_member"])
                    if info.file_size != model["xml_size_bytes"]:
                        raise CompactCaseIntegrityError("Original XML member size changed")
                    raw = container.read(info)
            except (OSError, KeyError, zipfile.BadZipFile) as exc:
                raise CompactCaseIntegrityError("Original MPH XML cannot be read") from exc
            if len(raw) != model["xml_size_bytes"] or hashlib.sha256(raw).hexdigest() != model["xml_sha256"]:
                raise CompactCaseIntegrityError("Original XML content does not match its case record")
            if _hash_file(source) != model["source_sha256"]:
                raise CompactCaseIntegrityError("Original MPH changed while evidence was read")
            return raw

    def rebind_source(self, new_path):
        """Relocate the original; only an exact original SHA256 is accepted."""
        candidate = Path(new_path).resolve(strict=True)
        with closing(self._connect()) as db:
            model = self._model(db)
            expected = model["source_sha256"]
        if not candidate.is_file() or _hash_file(candidate) != expected:
            raise CompactCaseIntegrityError("Replacement MPH is not the original source")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE model SET source_path=? WHERE id=1 AND source_sha256=?", (str(candidate), expected))
        return self.read_case()

    def _route(self, db, node_id):
        parts, seen = [], set()
        while node_id is not None:
            if node_id in seen:
                raise CompactCaseIntegrityError("Cyclic node evidence route")
            seen.add(node_id)
            row = db.execute("SELECT parent_id,xml_route FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not row:
                raise CompactCaseIntegrityError("Missing parent in evidence route")
            parts.append(json.loads(row["xml_route"]))
            node_id = row["parent_id"]
        return [part for segment in reversed(parts) for part in segment]

    @staticmethod
    def _element(root, route):
        try:
            for child in route:
                root = root[child]
        except (IndexError, TypeError) as exc:
            raise CompactCaseIntegrityError("Evidence route does not resolve in canonical XML") from exc
        return root

    def read_raw_evidence(self, node_id=None, *, child_index=None, property_id=None, selection_id=None):
        try:
            raw = self.read_raw_xml()
        except RawEvidenceUnavailable as exc:
            with closing(self._connect()) as db:
                model = self._model(db)
                return {"case_ref": self._ref(model), "status": "unavailable", "reason": exc.reason, "detail": str(exc)}
        with closing(self._connect()) as db:
            model = self._model(db)
            for table, identity in (("properties", property_id), ("selections", selection_id)):
                if identity is not None:
                    row = db.execute(f"SELECT node_id,child_index FROM {table} WHERE id=?", (identity,)).fetchone()
                    if not row:
                        raise KeyError("Evidence reference not found")
                    if node_id is not None and node_id != row["node_id"]:
                        raise ValueError("Evidence reference belongs to another node")
                    node_id, child_index = row["node_id"], row["child_index"]
            route = self._route(db, node_id) if node_id is not None else []
            if child_index is not None:
                if type(child_index) is not int or child_index < 0:
                    raise ValueError("child_index must be a zero-based child index")
                route.append(child_index)
            element = self._element(ET.fromstring(raw), route)
            return {"case_ref": self._ref(model), "status": "available", "node_id": node_id, "xml_route": route,
                    "raw_xml_sha256": model["xml_sha256"], "xml": ET.tostring(element, encoding="unicode"),
                    "serialization": "reconstructed_element; read_raw_xml returns the exact original bytes"}

    def validate_evidence_routes(self, raw_xml=None):
        with closing(self._connect()) as db:
            if self._model(db)["xml_member"] is None:
                if raw_xml not in (None, b""):
                    raise CompactCaseIntegrityError("A preview cannot validate invented settings XML")
                return True
        root = ET.fromstring(self.read_raw_xml() if raw_xml is None else raw_xml)
        with closing(self._connect()) as db:
            self._model(db)
            for node in db.execute("SELECT id,tag_id,kind_id FROM nodes"):
                route = self._route(db, node["id"])
                element = self._element(root, route)
                expected_tag = self._pool(db, "symbols", node["tag_id"])
                expected_kind = self._pool(db, "symbols", node["kind_id"])
                if ((expected_tag is not None and element.get("tag") != expected_tag)
                        or expected_kind != str(element.tag).rsplit("}", 1)[-1]):
                    raise CompactCaseIntegrityError("Node evidence route resolves to another saved feature")
                for table in ("properties", "selections"):
                    for row in db.execute(f"SELECT * FROM {table} WHERE node_id=?", (node["id"],)):
                        child = self._element(root, route + [row["child_index"]])
                        if table == "properties":
                            actual = child.get("param") if str(child.tag).rsplit("}", 1)[-1] == "param" else child.get("name")
                            if actual and str(child.tag).rsplit("}", 1)[-1] == "propertyValue" and actual.startswith("p:"):
                                actual = actual[2:]
                            expected = self._pool(db, "symbols", row["name_id"])
                            if expected is not None and actual != expected:
                                raise CompactCaseIntegrityError("Property evidence route resolves to another saved property")
                        elif str(child.tag).rsplit("}", 1)[-1] != self._pool(db, "symbols", row["kind_id"]):
                            raise CompactCaseIntegrityError("Selection evidence route resolves to another saved selection")
        return True

    def search(self, query, *, limit=10):
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("query must be a string of at most 2000 characters")
        terms = list(dict.fromkeys(re.findall(r"[\w]+", query, re.UNICODE)))[:64]
        if not terms:
            return []
        match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        with closing(self._connect()) as db:
            model = self._model(db)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='native_fts'").fetchone():
                raise ValueError("This compact case uses the library's shared native index")
            hits = db.execute("SELECT rowid,bm25(native_fts,4,1) AS score FROM native_fts WHERE native_fts MATCH ? ORDER BY score,rowid LIMIT ?",
                              (match, max(1, min(100, int(limit))))).fetchall()
            return [dict(self._node(db, row["rowid"], model), score=row["score"], backend="compact-native-fts5") for row in hits]


def read_case(output):
    return CompactCase(output).read_case()


def read_node(output, node_id):
    return CompactCase(output).read_node(node_id)


def get_property(output, node_id, name=None, **kwargs):
    return CompactCase(output).get_property(node_id, name, **kwargs)


def read_raw_evidence(output, node_id=None, **kwargs):
    return CompactCase(output).read_raw_evidence(node_id, **kwargs)


def search(output, query, **kwargs):
    return CompactCase(output).search(query, **kwargs)
