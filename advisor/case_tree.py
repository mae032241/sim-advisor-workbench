"""Metadata navigation over pinned, already parsed compact case configurations.

Edges describe observable configuration references, never execution order or
physical causality. Values remain in the compact reader; this index stores only
their locators and hashes. A navigation result is not configuration read evidence.
"""
from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from .build_control import check_cancelled
from .compact_library import _reader
from .index_manifest import _owned

SCHEMA = "case-configuration-tree/v1"
DEPENDENCY_KINDS = ("expression_symbol", "function_call", "named_selection", "node_tag",
                    "geometry_ref", "physics_ref", "study_ref", "solver_ref", "unknown")
_PATH = re.compile(r"compact:node/([1-9][0-9]*)\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*")
_EXPRESSION_NAMES = {"expr", "expression", "expressions", "f", "fun", "formula", "source", "q", "q0",
                     "t0", "p0", "u0", "v0", "w0", "value", "rho", "k", "cp", "hmax", "hmin"}
# These are language constructs/common native fields, not inferred user symbols.
_BUILTINS = set("sin cos tan asin acos atan atan2 sinh cosh tanh exp log log10 sqrt abs min max if real imag conj arg floor ceil round sign mod rem int d dt pd integrate sum with at prev time range linspace random pi i j e inf nan true false x y z r t freq omega T p u v w nx ny nz s K Pa m kg mol A V W N Hz J C rad deg mm cm um nm km g bar atm sccm from_mat userdef userdefined manual auto on off all none spatial material comp1 root".split())
_REF_NAMES = {"geom": "geometry_ref", "geometry": "geometry_ref", "geomtag": "geometry_ref",
              "physics": "physics_ref", "phys": "physics_ref", "physicstag": "physics_ref",
              "study": "study_ref", "studyref": "study_ref", "studystep": "study_ref",
              "sol": "solver_ref", "solver": "solver_ref", "solref": "solver_ref",
              "solution": "solver_ref", "control": "node_tag", "selection": "named_selection",
              "named": "named_selection", "input": "node_tag", "input2": "node_tag",
              "objects": "node_tag", "features": "node_tag", "feature": "node_tag"}
_VERIFIED = OrderedDict()
_VERIFY_LOCK = threading.RLock()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _path(identity):
    return "compact:node/" + str(identity)


def _artifact_stamp(store, authority):
    stamps = []
    for name in ("compact_case", "compact_api_overlay"):
        ref = (authority.get("data") or {}).get(name)
        if ref:
            relative = Path(ref["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Compact binding must use an owned relative artifact path")
            path = _owned(store.root, Path(store.root) / relative)
            info = path.stat()
            stamps.append((name, str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return tuple(stamps)


def _authority(store, case_id, *, load_reader=True):
    """Authorize the exact current case without hydrating any configuration/body."""
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("An exact model case_id is required")
    store.require_not_heldout(case_id)
    obj = store.get(case_id)
    if not obj or obj.get("type") != "model_case":
        raise ValueError("Case navigation requires an exact model_case source ID")
    store.require_not_heldout(case_id, obj)
    store.require_readable_library(obj["library_id"])
    with closing(store.connect()) as db:
        current = db.execute("SELECT citation_id FROM active WHERE library_id=? AND object_id=?",
                             (obj["library_id"], obj["object_id"])).fetchone()
    if not current or current[0] != case_id:
        raise ValueError("Case version is no longer current; rebuild navigation for its new case_id")
    context = store._compact_context(obj)
    if context is None:
        raise ValueError("Case tree is not supported for this source: no pinned compact configuration")
    authority, source_override = context
    binding = {key: obj.get(key) for key in
               ("id", "library_id", "object_id", "revision_id", "asset_key", "asset_hash", "case_group")}
    data = authority.get("data") or {}
    binding.update(compact_case=data.get("compact_case"), compact_api_overlay=data.get("compact_api_overlay"),
                   base_case_id=data.get("base_case_id"), base_case_group=data.get("base_case_group"))
    # _reader validates the owned artifact hash (including supported reclaim
    # bindings) and the immutable API overlay. No original MPH is opened.
    stamp = _artifact_stamp(store, authority)
    key = (str(Path(store.root).resolve()), case_id, _digest(binding), stamp)
    with _VERIFY_LOCK:
        verified = key in _VERIFIED
    reader = None
    if load_reader or not verified:
        reader = _reader(authority, store.root, source_override)
        if _artifact_stamp(store, authority) != stamp:
            raise ValueError("Compact configuration changed during integrity verification")
        with _VERIFY_LOCK:
            _VERIFIED[key] = True
            _VERIFIED.move_to_end(key)
            while len(_VERIFIED) > 128:
                _VERIFIED.popitem(last=False)
    return obj, reader if load_reader else None, binding


def _source_map(store, authority):
    """Read only scalar ref mappings, including an API case's pinned saved base."""
    groups = {authority.get("case_group")}
    with closing(store.connect()) as db:
        from .compact_api_overlay import pinned_base_authority
        base = pinned_base_authority(db, authority)
        if base:
            groups.add(base["case_group"])
        marks = ",".join("?" for _ in groups)
        rows = db.execute("SELECT r.id,json_extract(r.payload,'$.type'),"
                          "json_extract(r.payload,'$.data.compact_ref'),"
                          "json_extract(r.payload,'$.library_id'),json_extract(r.payload,'$.asset_key'),"
                          "json_extract(r.payload,'$.asset_hash') FROM case_members m JOIN revisions r "
                          "ON r.id=m.citation_id WHERE m.group_id IN (" + marks + ") ORDER BY r.id", tuple(groups)).fetchall()
    result = defaultdict(list)
    for identity, kind, raw, library, asset, sha in rows:
        if (library, asset, sha) != tuple(authority.get(key) for key in ("library_id", "asset_key", "asset_hash")):
            continue
        if kind not in {"saved_configuration", "api_configuration"} or not raw:
            continue
        ref = json.loads(raw)
        if type(ref.get("node_id")) is not int:
            continue
        # Policies may reserve an individual child even when the case is public.
        try:
            store.require_not_heldout(identity)
        except PermissionError:
            continue
        mode = "api" if kind == "api_configuration" or ref.get("api_only") else "saved"
        result[ref["node_id"]].append({"source_id": identity, "node_path": _path(ref["node_id"]),
                                       "source_mode": mode, "compact_ref": ref})
    return result


def _stage(node):
    branch = node.get("feature_branch")
    if branch and branch != "misc":
        return branch
    kind = str(node.get("kind") or "").lower()
    for name, words in (("parameters", ("param", "variable", "expr", "function", "analytic", "interpolation")),
                        ("geometry", ("geom",)), ("selection", ("selection",)), ("physics", ("physics",)),
                        ("material", ("material",)), ("mesh", ("mesh",)), ("study", ("study",)),
                        ("solver", ("solver", "solverfeature", "solversequence")), ("results", ("result", "numerical", "probe"))):
        if any(word in kind for word in words):
            return name
    return "other"


def _physics(node):
    return str(node.get("kind") or "").lower() in {"physics", "physicsinterface", "physicssequence"}


def _doc_refs(node):
    refs = []
    for source in (node, node.get("attributes") or {}):
        if not isinstance(source, dict):
            continue
        for key in ("href", "url", "doc_path", "anchor", "help", "helpref", "help_ref", "documentation"):
            if isinstance(source.get(key), str) and source[key]:
                refs.append({"field": key, "value": source[key]})
    return refs


def _node_metadata(nodes, mappings, case_id):
    by_id = {node["id"]: node for node in nodes}
    child_counts = Counter(node.get("parent_id") for node in nodes)
    if len(by_id) != len(nodes):
        raise ValueError("Compact node identities are not unique")
    results, stack = {}, set()

    def visit(identity):
        if identity in results:
            return results[identity]
        if identity in stack:
            raise ValueError("Compact parent hierarchy contains a cycle")
        stack.add(identity)
        node = by_id[identity]
        parent_id = node.get("parent_id")
        if parent_id is not None and parent_id not in by_id:
            raise ValueError("Compact parent hierarchy has a missing parent")
        parent = visit(parent_id) if parent_id is not None else None
        paths = [*parent["ancestor_paths"], parent["node_path"]] if parent else []
        titles = [*parent["ancestor_titles"], parent["label"] or parent["tag"] or parent["kind"]] if parent else []
        nearest = ({"node_path": _path(identity), **{key: node.get(key) or "" for key in ("tag", "type", "label")}}
                   if _physics(node) else parent["nearest_physics"] if parent else None)
        refs = [{key: ref[key] for key in ("source_id", "node_path", "source_mode")} for ref in mappings[identity]]
        modes = {ref["source_mode"] for ref in refs}
        # Saved nodes can have zero retrieval chunks (empty structural nodes),
        # yet remain addressable by the exact case + compact node path.
        modes.add("saved")
        if node.get("api_observations"):
            modes.add("api")
        result = {"node_path": _path(identity), "parent_path": _path(parent_id) if parent_id is not None else None,
                  **{key: node.get(key) or "" for key in ("tag", "kind", "type", "label")},
                  "stage": _stage(node), "depth": len(paths), "order": identity,
                  "child_count": child_counts[identity], "has_children": bool(child_counts[identity]),
                  "ancestor_paths": paths, "ancestor_titles": titles, "nearest_physics": nearest,
                  "source_refs": refs, "read_requests": [{"case_id": case_id, "node_path": _path(identity), "source": mode}
                                                           for mode in sorted(modes)],
                  "doc_refs": _doc_refs(node), "metadata_truncated_upstream": bool(node.get("truncated"))}
        results[identity] = result
        stack.remove(identity)
        return result

    for identity in sorted(by_id):
        visit(identity)
    return results


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, dict):
        for key in ("rows", "values", "value", "expression", "expr"):
            if key in value:
                yield from _strings(value[key])


def _definition_kind(node):
    kind = str(node.get("kind") or "").lower()
    typ = str(node.get("type") or "").lower()
    if kind in {"modelparam", "modelparamgroup", "param", "parameters", "variable", "variables", "expr", "variablefeature"} or typ == "variables":
        return "variable"
    if any(word in kind + " " + typ for word in ("analytic", "interpolation", "function")):
        return "function"
    return None


def _properties(node):
    for mode, rows in (("saved", node.get("properties", [])), ("api", node.get("api_observations", []))):
        for prop in rows:
            yield mode, prop
    for name, value in (node.get("attributes") or {}).items():
        if name.lower() in _REF_NAMES:
            yield "saved", {"id": name, "name": name, "value": value, "value_complete": True,
                            "value_type": "string", "_attribute": True}


def _property_ref(node, prop, mode, mappings, *, selection=False):
    path = _path(node["id"])
    # Configuration API exposes API observations as properties with native IDs.
    locator = mode + ":" + path + ("::selection/" if selection else "::attribute/" if prop.get("_attribute") else "::property/") + str(prop["id"])
    refs = []
    for ref in mappings[node["id"]]:
        if ref["source_mode"] != mode:
            continue
        compact = ref["compact_ref"]
        key = "selection_ordinals" if selection else "api_observation_ids" if mode == "api" else "property_ordinals"
        identity = prop.get("id") if mode == "api" else prop.get("ordinal")
        if prop.get("_attribute") or key not in compact or identity in compact[key]:
            refs.append({"source_id": ref["source_id"], "source_mode": mode, "node_path": path})
    return {"node_path": path, "source_mode": mode, "property_locator": locator, "source_refs": refs,
            "expression_sha256": _digest(prop.get("value")),
            **({"run_id": prop["run_id"]} if mode == "api" and prop.get("run_id") is not None else {})}


def _dependencies(nodes, mappings, metadata, case_id):
    definitions, tags = defaultdict(list), defaultdict(list)
    by_path = {_path(node["id"]): node for node in nodes}

    def qualified(node, prefixes):
        if not prefixes:
            return True
        chain = [by_path[path].get("tag") for path in metadata[node["id"]]["ancestor_paths"]]
        chain = [tag for tag in chain if tag]
        return any(len(candidate) >= len(prefixes) and candidate[-len(prefixes):] == prefixes
                   for candidate in (chain, [*chain, node.get("tag")]))

    def tag_targets(token, kind):
        # Keep native qualified paths intact. Splitting "comp1.geom1" into
        # two edges would invent a direct reference to comp1 itself.
        parts = re.split(r"[./]", token.strip("/"))
        if kind == "named_selection" and parts[:1] == ["selection"]:
            parts = parts[1:]
        if not parts:
            return []
        exact = tags.get(token, [])
        candidates = exact or [target for target in tags.get(parts[-1], []) if qualified(target, parts[:-1])]
        wanted = {"geometry_ref": "geometry", "physics_ref": "physics", "study_ref": "study",
                  "solver_ref": "solver", "named_selection": "selection"}.get(kind)
        return [target for target in candidates if wanted is None or _stage(target) == wanted]

    def symbol_targets(mode, token):
        result = definitions.get((mode, token), [])
        if not result and "." in token:
            parts = token.split(".")
            result = [(target, proof) for target, proof in definitions.get((mode, parts[-1]), [])
                      if qualified(target, parts[:-1])]
        return result

    for node in nodes:
        if node.get("tag"):
            tags[node["tag"]].append(node)
        definition = _definition_kind(node)
        for mode, prop in _properties(node):
            if not prop.get("value_complete"):
                continue
            if definition == "variable" and _IDENTIFIER.fullmatch(str(prop.get("name") or "")):
                name = prop["name"]
            elif definition == "function" and str(prop.get("name")).lower() in {"funcname", "functionname"}:
                name = prop.get("value")
            else:
                continue
            if isinstance(name, str) and _IDENTIFIER.fullmatch(name):
                definitions[(mode, name)].append((node, _property_ref(node, prop, mode, mappings)))
        if definition == "function" and node.get("tag"):
            for mode in {mode for mode, _ in _properties(node)} or {"saved"}:
                definitions[(mode, node["tag"])].append((node, {"node_path": _path(node["id"]), "source_mode": mode}))

    edges = {}

    def add(node, prop, mode, kind, token, candidates=(), *, selection=False, reason=None):
        origin = _property_ref(node, prop, mode, mappings, selection=selection)
        # Resolve within the nearest shared model/component context. Distinct
        # namespaces remain ambiguous; never select a target by phase order.
        candidates = list(candidates)
        if candidates:
            source_ancestors = metadata[node["id"]]["ancestor_paths"] + [_path(node["id"])]
            ranks = [len(set(source_ancestors) & set(metadata[target[0]["id"]]["ancestor_paths"] + [_path(target[0]["id"])]))
                     for target in candidates]
            candidates = [candidate for candidate, rank in zip(candidates, ranks) if rank == max(ranks)]
        unique = {}
        for target, proof in candidates:
            unique.setdefault(_path(target["id"]), []).append(proof)
        status = "confirmed" if len(unique) == 1 else "ambiguous" if unique else "unresolved"
        for target_path, proofs in sorted(unique.items()) if unique else [(None, [])]:
            row = {"from_node_path": _path(node["id"]), "target_node_path": target_path,
                   "kind": kind, "status": status, "token": token,
                   "origin": "api_configuration" if mode == "api" else "saved_configuration",
                   "property_locator": origin["property_locator"], "source_mode": mode,
                   "expression_sha256": origin["expression_sha256"], "source_refs": origin["source_refs"],
                   "target_refs": proofs, "candidate_count": len(unique),
                   "reason": reason or ("exact_scoped_configuration_reference" if unique else "resolution_unknown"),
                   "read_requests": [{"case_id": case_id, "node_path": _path(node["id"]), "source": mode}],
                   "physical_causality": "not_inferred", "effective_activation": "not_evaluated"}
            if target_path:
                for target_mode in sorted({proof.get("source_mode", mode) for proof in proofs}):
                    request = {"case_id": case_id, "node_path": target_path, "source": target_mode}
                    if request not in row["read_requests"]:
                        row["read_requests"].append(request)
            row["dependency_id"] = "dep-" + _digest(row)
            edges[row["dependency_id"]] = row

    for node in nodes:
        check_cancelled()
        local_arguments = set()
        if _definition_kind(node) == "function":
            for _mode, declaration in _properties(node):
                if declaration.get("value_complete") and str(declaration.get("name")).lower() in {"args", "arguments"}:
                    for text in _strings(declaration.get("value")):
                        local_arguments.update(_IDENTIFIER.findall(text))
        for mode, prop in _properties(node):
            name = str(prop.get("name") or "").lower()
            value = prop.get("value")
            if not prop.get("value_complete"):
                add(node, prop, mode, "unknown", None, reason="property_not_completely_decoded")
                continue
            if name in _REF_NAMES:
                for text in _strings(value):
                    for token in re.findall(r"[A-Za-z_][A-Za-z_0-9]*(?:[./][A-Za-z_][A-Za-z_0-9]*)*", text):
                        if token.lower() in {"all", "none", "auto", "manual", "on", "off"}:
                            continue
                        targets = tag_targets(token, _REF_NAMES[name])
                        add(node, prop, mode, _REF_NAMES[name], token,
                            [(target, {"node_path": _path(target["id"]), "source_mode": "saved"}) for target in targets])
                continue
            definition = _definition_kind(node)
            for text in _strings(value):
                if not (prop.get("value_type") == "expression" or definition == "variable" or name in _EXPRESSION_NAMES
                        or (re.search(r"[+*/^()]", text) and re.search(r"[A-Za-z_]", text))):
                    continue
                # Opaque labels/paths, quoted literals and unit annotations are
                # not declarations of missing user variables.
                if name in {"name", "label", "description", "filename", "file", "path", "unit", "funcname", "args"}:
                    continue
                clean = re.sub(r"\[[^\]]*\]|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'", " ", text)
                for match in _IDENTIFIER.finditer(clean):
                    token = match.group()
                    called = bool(re.match(r"\s*\(", clean[match.end():]))
                    if token in local_arguments:
                        continue
                    candidates = symbol_targets(mode, token)
                    if not candidates and mode == "api":
                        candidates = symbol_targets("saved", token)
                    if not candidates and (token in _BUILTINS or token.lower() in _BUILTINS or "." in token):
                        continue
                    if not candidates and re.fullmatch(r"[eE][0-9]+", token):
                        continue
                    add(node, prop, mode, "function_call" if called else "expression_symbol", token, candidates)
        for selection in node.get("selections", []):
            value = selection.get("value") or {}
            if not isinstance(value, dict):
                add(node, selection, "saved", "unknown", None, selection=True, reason="selection_structure_unknown")
                continue
            for token in value.get("named_references", []):
                token = str(token)
                targets = tag_targets(token, "named_selection")
                add(node, selection, "saved", "named_selection", token,
                    [(target, {"node_path": _path(target["id"]), "source_mode": "saved"}) for target in targets], selection=True)
            for explicit in value.get("explicit", []):
                if isinstance(explicit, dict) and explicit.get("geom"):
                    token = explicit["geom"]
                    add(node, selection, "saved", "geometry_ref", token,
                        [(target, {"node_path": _path(target["id"]), "source_mode": "saved"}) for target in tag_targets(token, "geometry_ref")], selection=True)
            if selection.get("status") == "unresolved":
                add(node, selection, "saved", "unknown", None, selection=True, reason="selection_structure_unknown")
    return sorted(edges.values(), key=lambda row: (metadata[int(_PATH.fullmatch(row["from_node_path"]).group(1))]["order"],
                                                   row["property_locator"], row["kind"], row["token"] or "", row["target_node_path"] or ""))


class CaseTreeRepository:
    def __init__(self, store):
        self.store = store
        self.directory = _owned(store.root, Path(store.root) / "case-trees")
        self.index_path = _owned(store.root, self.directory / "index.sqlite3")

    def _connect(self):
        path = _owned(self.store.root, self.index_path)
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        if db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] != SCHEMA:
            db.close()
            raise ValueError("Unsupported case tree schema; rebuild required")
        return db

    def _case(self, case_id):
        _, _, binding = _authority(self.store, case_id, load_reader=False)
        if not self.index_path.is_file():
            return None
        with closing(self._connect()) as db:
            row = db.execute("SELECT body FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            return None
        result = json.loads(row[0])
        if result["binding"] != binding:
            raise ValueError("Case configuration binding changed; rebuild case navigation")
        return result

    def case(self, case_id):
        result = self._case(case_id)
        if result is None:
            return {"state": "not_prepared", "case_id": case_id, "is_read_evidence": False}
        return {key: value for key, value in result.items() if key != "binding"}

    def node(self, case_id, node_path):
        if not isinstance(node_path, str) or not _PATH.fullmatch(node_path):
            raise ValueError("Use an exact compact:node/N path")
        case = self._case(case_id)
        if case is None:
            raise KeyError("Case tree is not prepared")
        with closing(self._connect()) as db:
            row = db.execute("SELECT body FROM nodes WHERE case_id=? AND node_path=?", (case_id, node_path)).fetchone()
        if not row:
            raise KeyError("Node is not present in this exact case tree")
        return json.loads(row[0])

    def list_nodes(self, case_id, offset=0, limit=100):
        _pagination(offset, limit, 100)
        case = self._case(case_id)
        if case is None:
            return _page(None, case_id, "nodes", None, offset, limit, 0, [])
        with closing(self._connect()) as db:
            rows = db.execute("SELECT body FROM nodes WHERE case_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
                              (case_id, limit, offset)).fetchall()
        return _page(case, case_id, "nodes", None, offset, limit, case["node_count"], [json.loads(row[0]) for row in rows])

    @contextmanager
    def _writer(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = _owned(self.store.root, self.directory / ".build.lock")
        if lock.exists():
            from .library_store import process_alive
            previous = json.loads(lock.read_text(encoding="utf-8"))
            if process_alive(previous.get("pid")):
                raise RuntimeError("Another case tree build is active")
            lock.unlink()
        token = uuid.uuid4().hex
        try:
            with lock.open("x", encoding="utf-8") as stream:
                stream.write(_json({"pid": os.getpid(), "token": token}))
        except FileExistsError:
            raise RuntimeError("Another case tree build is active") from None
        try:
            # Only owned temporary files from interrupted builds are removed.
            for path in self.directory.glob(".building-*.sqlite3*"):
                if re.fullmatch(r"\.building-[a-f0-9]{32}\.sqlite3(?:-(?:journal|wal|shm))?", path.name):
                    _owned(self.store.root, path).unlink()
            yield
        finally:
            if lock.exists() and json.loads(lock.read_text(encoding="utf-8")).get("token") == token:
                lock.unlink()


def build_case_trees(store, case_ids, progress=None):
    identities = list(dict.fromkeys(case_ids))
    if not identities or any(not isinstance(identity, str) or not identity for identity in identities):
        raise ValueError("At least one exact case_id is required")
    repo = CaseTreeRepository(store)
    generation = uuid.uuid4().hex
    prepared = []

    def report(completed, total, detail, case_id=None):
        if progress:
            progress({"stage": "case_tree", "progress": {"completed": completed, "total": total, "unit": "cases",
                                                        "current_file": case_id, "detail": detail}})
        check_cancelled()

    with repo._writer():
        stage = _owned(store.root, repo.directory / (".building-" + uuid.uuid4().hex + ".sqlite3"))
        try:
            with closing(sqlite3.connect(stage)) as db:
                if repo.index_path.is_file():
                    with closing(repo._connect()) as previous:
                        previous.backup(db)
                db.executescript("""
                  CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                  CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY,body TEXT NOT NULL);
                  CREATE TABLE IF NOT EXISTS nodes(case_id TEXT,node_path TEXT,parent_path TEXT,ordinal INTEGER,
                    search_text TEXT,body TEXT NOT NULL,PRIMARY KEY(case_id,node_path));
                  CREATE INDEX IF NOT EXISTS nodes_parent ON nodes(case_id,parent_path,ordinal);
                  CREATE TABLE IF NOT EXISTS dependencies(case_id TEXT,dependency_id TEXT,from_path TEXT,target_path TEXT,
                    ordinal INTEGER,search_text TEXT,body TEXT NOT NULL,PRIMARY KEY(case_id,dependency_id));
                  CREATE INDEX IF NOT EXISTS dependency_from ON dependencies(case_id,from_path,ordinal);
                  CREATE INDEX IF NOT EXISTS dependency_to ON dependencies(case_id,target_path,ordinal);
                """)
                for number, case_id in enumerate(identities):
                    report(number, len(identities), "Reading already saved compact node metadata", case_id)
                    obj, reader, binding = _authority(store, case_id)
                    mappings = _source_map(store, obj)
                    nodes = []
                    for node in reader.iter_nodes():
                        check_cancelled()
                        nodes.append(node)
                        if len(nodes) % 50 == 0:
                            report(number, len(identities), "Read " + str(len(nodes)) + " saved compact nodes", case_id)
                    metadata = _node_metadata(nodes, mappings, case_id)
                    deps = _dependencies(nodes, mappings, metadata, case_id)
                    summary = {"state": "ready", "schema": SCHEMA, "case_id": case_id, "generation": generation,
                               **{key: obj.get(key) for key in ("library_id", "asset_key", "asset_hash", "case_group", "revision_id")},
                               "node_count": len(nodes), "dependency_count": len(deps), "binding": binding,
                               "coverage": "all_retained_compact_nodes", "is_read_evidence": False,
                               "physical_causality": "not_inferred", "effective_activation": "not_evaluated"}
                    for table in ("cases", "nodes", "dependencies"):
                        db.execute("DELETE FROM " + table + " WHERE case_id=?", (case_id,))
                    db.execute("INSERT INTO cases VALUES(?,?)", (case_id, _json(summary)))
                    for node in metadata.values():
                        search = " ".join(str(node.get(key) or "") for key in ("tag", "kind", "type", "label", "stage"))
                        search += " " + " ".join(node["ancestor_titles"])
                        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?)", (case_id, node["node_path"], node["parent_path"],
                                                                           node["order"], search.casefold(), _json(node)))
                    for ordinal, edge in enumerate(deps):
                        db.execute("INSERT INTO dependencies VALUES(?,?,?,?,?,?,?)", (case_id, edge["dependency_id"], edge["from_node_path"],
                                   edge["target_node_path"], ordinal, " ".join(str(edge[key] or "") for key in ("kind", "token", "status")).casefold(), _json(edge)))
                    prepared.append(summary)
                for case in prepared:
                    if _authority(store, case["case_id"], load_reader=False)[2] != case["binding"]:
                        raise ValueError("Case changed during tree construction; previous index retained")
                db.execute("INSERT OR REPLACE INTO metadata VALUES('schema',?)", (SCHEMA,))
                db.execute("INSERT OR REPLACE INTO metadata VALUES('generation',?)", (generation,))
                db.commit()
                # Remove replaced rows from unused SQLite pages; values were
                # never persisted, and no old metadata generation is retained.
                db.execute("VACUUM")
            report(len(identities), len(identities), "Publishing complete case navigation")
            for case in prepared:
                if _authority(store, case["case_id"], load_reader=False)[2] != case["binding"]:
                    raise ValueError("Case changed before publication; previous index retained")
            os.replace(stage, repo.index_path)
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                leftover = _owned(store.root, stage.with_name(stage.name + suffix))
                if leftover.exists():
                    leftover.unlink()
    return {"state": "ready", "schema": SCHEMA, "generation": generation, "case_count": len(prepared),
            "node_count": sum(case["node_count"] for case in prepared),
            "dependency_count": sum(case["dependency_count"] for case in prepared), "prepared_case_ids": identities}


def _pagination(offset, limit, maximum=8):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError("offset must be nonnegative and limit between 1 and " + str(maximum))


def _page(case, case_id, view, parent, offset, limit, total, items):
    following = offset + len(items)
    return {"state": "ready" if case else "not_prepared", "case_id": case_id,
            "generation": case["generation"] if case else None, "view": view, "parent_path": parent,
            "total": total, "offset": offset, "limit": limit, "returned": len(items),
            "next_offset": following if following < total else None, "has_more": following < total,
            "items": items, "is_read_evidence": False}


def _public_node(node, minimal=False):
    fields = ("node_path", "parent_path", "tag", "kind", "type", "label", "stage", "depth", "order", "child_count", "has_children",
              "ancestor_paths", "ancestor_titles", "nearest_physics", "read_requests", "metadata_truncated_upstream")
    result = {key: node[key] for key in fields}
    if minimal:
        # Titles are hints, not source values. Explicitly mark shortened hints;
        # exact native values remain at the configuration read entrance.
        for key in ("label", "tag", "kind", "type"):
            if len(result[key]) > 120:
                result[key] = result[key][:120]
                result["hint_truncated"] = True
        result["ancestor_paths"] = node["ancestor_paths"][-3:]
        result["ancestor_titles"] = [str(title)[:80] for title in node["ancestor_titles"][-3:]]
        result["ancestor_count"] = len(node["ancestor_paths"])
        result["ancestor_hints_complete"] = len(node["ancestor_paths"]) <= 3 and all(len(str(title)) <= 80 for title in node["ancestor_titles"])
        if result.get("nearest_physics"):
            result["nearest_physics"] = {key: (value[:120] if isinstance(value, str) and key != "node_path" else value)
                                          for key, value in result["nearest_physics"].items()}
        result["metadata_view"] = "minimal_navigation"
    return result


def _public_dependency(edge):
    # The exact origin locator and endpoint read actions suffice for navigation.
    # Catalog chunk mapping/proof arrays stay in metadata storage rather than
    # being repeated on every edge page or mistaken for observed read evidence.
    result = {key: value for key, value in edge.items() if key not in {"source_refs", "target_refs"}}
    result.update(source_ref_count=len(edge["source_refs"]), target_ref_count=len(edge["target_refs"]),
                  metadata_view="configuration_reference")
    return result


def get_case_navigation(store, case_id, *, node_path=None, view="children", offset=0, limit=8, query=None, direction="both"):
    _pagination(offset, limit)
    if view not in {"children", "dependencies"} or direction not in {"both", "incoming", "outgoing"}:
        raise ValueError("Unsupported case navigation view/direction")
    if query is not None and (not isinstance(query, str) or len(query) > 500):
        raise ValueError("query must be a string up to 500 characters")
    if node_path is not None and (not isinstance(node_path, str) or not _PATH.fullmatch(node_path)):
        raise ValueError("Use an exact compact:node/N path")
    repo = CaseTreeRepository(store)
    case = repo._case(case_id)
    if case is None:
        return _page(None, case_id, view, node_path, offset, limit, 0, [])
    if node_path is not None:
        repo.node(case_id, node_path)
    params = [case_id]
    clause = "case_id=?"
    table = "nodes" if view == "children" else "dependencies"
    if view == "children" and not (node_path is None and query and query.strip()):
        clause += " AND parent_path IS NULL" if node_path is None else " AND parent_path=?"
        if node_path is not None:
            params.append(node_path)
    elif node_path is not None:
        clause += " AND " + {"outgoing": "from_path=?", "incoming": "target_path=?", "both": "(from_path=? OR target_path=?)"}[direction]
        params.extend([node_path] * (2 if direction == "both" else 1))
    if query and query.strip():
        clause += " AND instr(search_text,?)>0"
        params.append(query.strip().casefold())
    with closing(repo._connect()) as db:
        total = db.execute("SELECT count(*) FROM " + table + " WHERE " + clause, params).fetchone()[0]
        rows = db.execute("SELECT body FROM " + table + " WHERE " + clause + " ORDER BY ordinal LIMIT ? OFFSET ?",
                          [*params, limit, offset]).fetchall()
    items = []

    def envelope(values):
        page = _page(case, case_id, view, node_path, offset, limit, total, values)
        if view == "dependencies":
            page.update(direction=direction, physical_causality="not_inferred", effective_activation="not_evaluated")
        if view == "children" and query and query.strip():
            page["search_scope"] = "all_retained_nodes" if node_path is None else "direct_children"
        return page

    def fits(values):
        return len(json.dumps(envelope(values), ensure_ascii=False).encode("utf-8")) <= 7000

    for row in rows:
        original = json.loads(row[0])
        item = _public_node(original) if view == "children" else _public_dependency(original)
        if not fits([*items, item]):
            if items:
                break
            item = _public_node(original, minimal=True) if view == "children" else _public_dependency(original)
            if not fits([item]):
                raise ValueError("A dependency record exceeds the metadata page budget; use its exact configuration read entrance")
        items.append(item)
    return envelope(items)
