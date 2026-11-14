"""Deterministic case-node -> native-manual navigation, never read evidence.

Only existing case-tree and TOC metadata are consumed. No MPH, property body,
HTML body, retrieval index, embedding, model or test task constructs a link.
Exact-name matches are inspectable associations, not validation of correctness.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import threading
import unicodedata
from urllib.parse import unquote, urlsplit

from .html_tree_tools import HtmlTreeTools
from .index_manifest import _owned

SCHEMA = "case-manual-links/v1"
ALGORITHM = "scoped-native-metadata/v2"
MAX_BYTES = 7000
_STOP = {"the", "interface", "interfaces", "node", "nodes", "feature", "features"}
_STRUCTURAL = {"physics", "field", "physicsfield", "model", "component", "group", "property", "settings", "selection"}
_LEXICAL_STOP = _STOP | _STRUCTURAL | {"and", "or", "in", "of", "for", "to", "a", "an", "from", "on", "at", "with", "by", "sequence", "sequences"}
_STAGES = {"geometry", "mesh", "study", "solver", "material", "selection", "parameters", "results"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def normalized_name(value):
    """Language-preserving generic typography/CamelCase normalization only."""
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    value = re.sub(r"\([^)]*\)\s*$", "", value)
    words = re.findall(r"[^\W_]+", value.casefold(), re.UNICODE)
    return " ".join(word for word in words if word not in _STOP)


def _names(node):
    values = {normalized_name(node.get(key)) for key in ("type", "label", "title")}
    return {value for value in values if value and not set(value.split()) <= _STRUCTURAL}


def _signature(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _words(value):
    result = []
    for word in normalized_name(value).split():
        if word in _LEXICAL_STOP:
            continue
        if word.isascii():
            if len(word) > 4 and word.endswith("ies"):
                word = word[:-3] + "y"
            elif len(word) > 5 and word.endswith("ing"):
                word = word[:-3]
            elif len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
                word = word[:-1]
        if word not in result:
            result.append(word)
    return tuple(result)


def _word_score(left, right):
    if left == right:
        return 1.0
    if left.isascii() and right.isascii() and min(len(left), len(right)) >= 3:
        if left.startswith(right) or right.startswith(left):
            return .75
        prefix = 0
        for a, b in zip(left, right):
            if a != b:
                break
            prefix += 1
        if prefix >= 5 and prefix / max(len(left), len(right)) >= .7:
            return .65
    return 0.0


def _lexical_score(query, title):
    if not query or not title:
        return 0.0
    used, total = set(), 0.0
    for word in query:
        ranked = sorted((_word_score(word, target), index) for index, target in enumerate(title) if index not in used)
        if not ranked or ranked[-1][0] == 0:
            continue
        score, index = ranked[-1]
        used.add(index)
        total += score
    return total / max(len(query), len(title))


class CaseManualLinks:
    def __init__(self, store, case_repository=None, html_tools=None, directory=None):
        self.store = store
        if case_repository is None:
            from .case_tree import CaseTreeRepository
            case_repository = CaseTreeRepository(store)
        self.cases = case_repository
        self.html = html_tools or HtmlTreeTools(store)
        self.directory = _owned(Path(store.root), Path(directory) if directory else Path(store.root) / "case-manual-links")
        self.path = _owned(Path(store.root), self.directory / "index.sqlite3")
        self._lock = threading.RLock()
        self._manual_cache = None

    def _db(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.executescript("""CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY,binding TEXT NOT NULL,
              binding_hash TEXT NOT NULL,manual_generation TEXT NOT NULL,summary TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS links(case_id TEXT NOT NULL,node_path TEXT NOT NULL,manual_node_id TEXT NOT NULL,
              body TEXT NOT NULL,PRIMARY KEY(case_id,node_path,manual_node_id));
          CREATE INDEX IF NOT EXISTS by_manual ON links(manual_node_id,case_id,node_path);""")
        return db

    def _case(self, case_id):
        self.store.require_not_heldout(case_id)
        source = self.store.get(case_id)
        if not source or source.get("type") != "model_case":
            raise ValueError("Use an exact registered model_case citation")
        self.store.require_readable_library(source["library_id"])
        self.store.require_not_heldout(case_id, source)
        if not self.store.source_available(case_id):
            raise PermissionError("Case source is no longer available")
        binding = self.cases.case(case_id)
        if not binding or binding.get("state") == "not_prepared":
            raise ValueError("Case node metadata is not prepared")
        if any(binding.get(key) is not None and binding.get(key) != source.get(key)
               for key in ("library_id", "asset_key", "asset_hash", "revision_id", "case_group")):
            raise PermissionError("Case tree binding differs from its exact source version")
        return {key: deepcopy(binding.get(key, source.get(key))) for key in
                ("case_id", "library_id", "asset_key", "asset_hash", "revision_id", "case_group", "generation")} | {"case_id": case_id}

    def _manuals(self):
        # The small registry/policy signature is checked on every request;
        # full TOC metadata is reused only while that signature is unchanged.
        records = self.html._records()
        policy = self.store.evaluation_policy()
        signature = _signature([ALGORITHM, [{key: row.get(key) for key in
            ("tree_id", "library_id", "root_id", "generation", "index_sha256")} for row in records],
            {key: sorted(getattr(policy, key, ())) for key in ("citation_ids", "object_ids", "case_groups", "asset_keys", "source_hashes", "document_relpaths", "source_paths")}])
        if self._manual_cache and self._manual_cache[0] == signature:
            return self._manual_cache[1]
        snapshot = self.html.navigation_metadata()
        snapshot["generation"] = signature
        snapshot["nodes"] = [node for node in snapshot["nodes"] if node.get("available") and node.get("doc_path")]
        for node in snapshot["nodes"]:
            node["_normalized_title"] = normalized_name(node.get("title"))
            node["_words"] = _words(node.get("title"))
        self._manual_cache = (signature, snapshot)
        return snapshot

    def _nodes(self, case_id):
        result, offset = [], 0
        while True:
            page = self.cases.list_nodes(case_id, offset=offset, limit=100)
            result.extend(page.get("items", []))
            following = page.get("next_offset")
            if following is None:
                return result
            if type(following) is not int or following <= offset:
                raise ValueError("Case-tree metadata did not advance its real cursor")
            offset = following

    @staticmethod
    def _doc_matches(node, manuals, *, scoped_relative=False):
        refs = node.get("doc_refs") or []
        if isinstance(refs, (str, dict)):
            refs = [refs]
        found = {}
        for ref in refs:
            value = ref if isinstance(ref, str) else next((ref.get(key) for key in
                ("node_id", "manual_node_id", "href", "url", "doc_path", "help", "value") if isinstance(ref.get(key), str)), None)
            if not value:
                continue
            for manual in manuals:
                if value == manual["node_id"]:
                    found[manual["node_id"]] = manual
            parsed = urlsplit(value)
            if parsed.scheme and (parsed.scheme not in {"http", "https"} or parsed.hostname != "doc.comsol.com"):
                continue
            path = unquote(parsed.path).replace("\\", "/")
            if "/doc/" in path:
                path = path.split("/doc/", 1)[1]
            path = path.lstrip("/")
            if not path or ".." in PurePosixPath(path).parts or ":" in path:
                continue
            anchor = unquote(parsed.fragment)
            for manual in manuals:
                candidates = {manual["doc_path"], manual["package"] + "/" + manual["doc_path"]}
                relative_match = scoped_relative and not parsed.scheme and not path.startswith("com.comsol.help.") and any(
                    candidate.endswith("/" + path) for candidate in candidates)
                if (path in candidates or relative_match) and (manual.get("anchor") or "") == anchor:
                    found[manual["node_id"]] = manual
        return list(found.values())

    @staticmethod
    def _best_candidates(queries, manuals, *, minimum=.7):
        ranked = [(max((_lexical_score(query, manual["_words"]) for query in queries), default=0), manual)
                  for manual in manuals]
        best = max((score for score, _ in ranked), default=0)
        if best < minimum:
            return []
        return [manual for score, manual in ranked if abs(score - best) < .000001]

    @staticmethod
    def _structural_stage(node, by_path):
        context = [node, *(by_path[path] for path in reversed(node.get("ancestor_paths", [])) if path in by_path)]
        for ancestor in context:
            stage = ancestor.get("stage")
            if stage in _STAGES:
                return stage
            kind = _words(ancestor.get("kind"))
            candidates = [stage for stage in _STAGES if kind and _lexical_score(kind, _words(stage)) >= .6]
            if len(candidates) == 1:
                return candidates[0]
        return None

    def _structural(self, node, by_path, manuals):
        stage = self._structural_stage(node, by_path)
        if not stage:
            return "unresolved", "none", "No explicit Physics scope or informative native structural stage is recorded", []
        if stage not in self._structure_cache:
            queries = [_words(stage)]
            containers = [manual for manual in manuals if manual.get("child_count", 0)]
            scopes = self._best_candidates(queries, containers, minimum=.35)
            if not scopes:
                scopes = self._best_candidates(queries, manuals, minimum=.35)
            ids = {scope["node_id"] for scope in scopes}
            pool = [manual for manual in manuals if manual["node_id"] in ids or ids.intersection(manual.get("ancestor_ids", []))]
            self._structure_cache[stage] = (scopes, pool)
        scopes, pool = self._structure_cache[stage]
        if not scopes:
            return "unresolved", "none", "No native TOC chapter scope conservatively matches the recorded structural stage/kind", []
        matches = []
        for key in ("type", "label"):
            if node.get(key):
                matches = self._best_candidates([_words(node[key])], pool)
                if matches:
                    break
        if matches:
            return ("ambiguous" if len(matches) > 1 else "candidate"), "structural_feature_candidate", \
                "Native stage/ancestor kind narrows the TOC chapter; lexical feature matches are candidates only", matches
        return ("ambiguous" if len(scopes) > 1 else "candidate"), "structural_scope_candidate", \
            "Only native structural chapter scope is suggested; no feature implementation match is claimed", scopes

    def _match(self, node, by_path, manuals):
        physics = node.get("nearest_physics")
        if not physics and node.get("kind", "").casefold() in {"physics", "physicsfield"}:
            physics = node
        if not physics:
            explicit = self._doc_matches(node, manuals)
            if explicit:
                return ("ambiguous" if len(explicit) > 1 else "candidate"), "explicit_unscoped_reference", \
                    "Existing doc reference is retained, but no parent Physics scope is established; no scoped feature match is claimed", explicit
            return self._structural(node, by_path, manuals)
        physics = by_path.get(physics.get("node_path"), physics)
        scope_key = physics.get("node_path") or _signature(physics)
        if scope_key in self._scope_cache:
            scopes, explicit_scope, descendants = self._scope_cache[scope_key]
        else:
            scopes = self._doc_matches(physics, manuals)
            explicit_scope = bool(scopes)
            if not scopes:
                # A customized label cannot override an exact native type, nor
                # turn a leaf feature named Wall into its parent interface.
                for field in ("type", "label", "title"):
                    names = _names({field: physics.get(field)})
                    scopes = [m for m in manuals if m["_normalized_title"] in names and
                              (m.get("child_count", 0) or re.search(r"\binterface\b", m.get("title", ""), re.I))]
                    if scopes:
                        break
            ids = {scope["node_id"] for scope in scopes}
            descendants = [m for m in manuals if ids.intersection(m.get("ancestor_ids", []))]
            self._scope_cache[scope_key] = (scopes, explicit_scope, descendants)
        if not scopes:
            if scope_key not in self._weak_physics_cache:
                headers = [manual for manual in manuals if manual.get("child_count", 0)
                           or re.search(r"\binterface\b", manual.get("title", ""), re.I)]
                candidates = []
                for key in ("type", "label"):
                    query = _words(physics.get(key))
                    if len(query) >= 2 or query and len(query[0]) >= 7:
                        candidates = self._best_candidates([query], headers, minimum=.6)
                        if candidates:
                            break
                self._weak_physics_cache[scope_key] = candidates
            candidates = self._weak_physics_cache[scope_key]
            if candidates:
                return ("ambiguous" if len(candidates) > 1 else "candidate"), "candidate_physics_scope", \
                    "Only conservative lexical parent Physics candidates exist; the feature is not linked across those interfaces", candidates
            return "unresolved", "none", "No reliable native-manual parent Physics scope matches the recorded type/label", []
        self_node = node.get("node_path") == physics.get("node_path")
        subtree = scopes if self_node else descendants
        explicit = self._doc_matches(node, manuals)
        scoped_explicit = self._doc_matches(node, subtree, scoped_relative=True)
        if scoped_explicit:
            matches, confidence, basis = scoped_explicit, "explicit_reference", "Existing native doc reference matches within the recorded parent Physics subtree"
        elif explicit:
            return "candidate", "scope_conflict", "Existing doc reference is outside the matched parent Physics subtree; applicability is unresolved", explicit
        else:
            names = _names(node)
            matches = scopes if self_node else []
            if not self_node:
                for field in ("type", "label", "title"):
                    field_names = _names({field: node.get(field)})
                    matches = [m for m in subtree if m["_normalized_title"] in field_names]
                    if matches:
                        break
            confidence = "explicit_reference" if self_node and explicit_scope else "exact_scoped_metadata"
            basis = "Native type/label and TOC title normalize exactly within the matched parent Physics subtree"
            if not matches and names:
                matches = [m for m in subtree if any(
                    len(set(name.split()) & set(m["_normalized_title"].split())) >= 2
                    and (set(name.split()) <= set(m["_normalized_title"].split())
                         or set(m["_normalized_title"].split()) <= set(name.split())) for name in names)]
                if matches:
                    return "candidate", "partial_scoped_name", "Only a partial native-name match exists within the parent Physics subtree", matches
        if not matches:
            return "unresolved", "none", "No feature title or explicit doc reference matches inside the parent Physics subtree", []
        if len(scopes) > 1 or len(matches) > 1:
            return "ambiguous", confidence, "Multiple parent-interface or feature matches remain; no target is selected as verified", matches
        return "matched", confidence, basis, matches

    def build(self, case_id):
        with self._lock:
            binding = self._case(case_id)
            nodes, snapshot = self._nodes(case_id), self._manuals()
            self._scope_cache = {}
            self._weak_physics_cache = {}
            self._structure_cache = {}
            by_path = {node["node_path"]: node for node in nodes}
            counts = {state: 0 for state in ("matched", "candidate", "ambiguous", "unresolved")}
            contexts = {group: {"node_count": 0, "counts": dict.fromkeys(counts, 0)}
                        for group in ("physics_subtree", "structural", "other")}
            physics_scope_counts = {"exact_parent_scope": 0, "candidate_parent_scope": 0, "unresolved_parent_scope": 0}
            rows = []
            for node in nodes:
                state, confidence, basis, matches = self._match(node, by_path, snapshot["nodes"])
                counts[state] += 1
                physics = node.get("nearest_physics")
                group = "physics_subtree" if physics else "structural" if self._structural_stage(node, by_path) else "other"
                contexts[group]["node_count"] += 1
                contexts[group]["counts"][state] += 1
                if group == "physics_subtree":
                    scope_key = physics.get("node_path") or _signature(physics)
                    scope_status = "exact_parent_scope" if self._scope_cache.get(scope_key, ([],))[0] else "candidate_parent_scope" if confidence == "candidate_physics_scope" else "unresolved_parent_scope"
                    physics_scope_counts[scope_status] += 1
                for manual in matches or [None]:
                    value = {"case_id": case_id, "node_path": node["node_path"], "case_library_id": binding["library_id"],
                             "native_type": node.get("type"), "native_label": node.get("label"),
                             "native_kind": node.get("kind"), "stage": node.get("stage"),
                             "source_doc_refs": deepcopy(node.get("doc_refs", [])),
                             "source_version": {key: binding.get(key) for key in ("asset_hash", "revision_id", "generation")},
                             "source_refs": deepcopy(node.get("source_refs", [])),
                             "configuration_requests": deepcopy(node.get("read_requests", [])),
                             "nearest_physics": deepcopy(node.get("nearest_physics")),
                             "mapping_state": state, "confidence": confidence, "basis": basis,
                             "target_role": "parent_physics_candidate" if confidence == "candidate_physics_scope" else "structural_candidate" if confidence.startswith("structural_") else "scoped_node_or_explicit_reference",
                             "verified": False, "is_read_evidence": False}
                    if manual:
                        value["manual"] = {key: deepcopy(manual.get(key)) for key in
                            ("node_id", "tree_id", "library_id", "root_id", "title", "ancestor_titles", "doc_path", "doc_sha256", "tree_generation")}
                    rows.append((case_id, node["node_path"], manual["node_id"] if manual else "", _json(value)))
            if _signature(binding) != _signature(self._case(case_id)):
                raise RuntimeError("Case metadata changed while associations were constructed")
            summary = {"state": "ready", "schema": SCHEMA, "case_id": case_id, "node_count": len(nodes),
                       "algorithm_version": ALGORITHM, "contexts": contexts, "physics_scope_counts": physics_scope_counts,
                       "relationship_count": len(rows), "counts": counts, "manual_generation": snapshot["generation"],
                       "matching_scope": "inspectable metadata association; coverage is not correctness or physical validation",
                       "is_read_evidence": False}
            with closing(self._db()) as db, db:
                db.execute("DELETE FROM links WHERE case_id=?", (case_id,))
                db.executemany("INSERT INTO links VALUES(?,?,?,?)", rows)
                db.execute("INSERT OR REPLACE INTO cases VALUES(?,?,?,?,?)", (case_id, _json(binding), _signature(binding), snapshot["generation"], _json(summary)))
            return summary

    def _refresh(self, case_id):
        binding, snapshot = self._case(case_id), self._manuals()
        with closing(self._db()) as db:
            previous = db.execute("SELECT binding_hash,manual_generation FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if previous is None or previous[0] != _signature(binding) or previous[1] != snapshot["generation"]:
            self.build(case_id)
        return binding

    def _source_permissions(self, identities, policy, visible):
        """Exact catalog metadata, sharing one live scope/policy per batch.

        Match source_available/get/require_not_heldout, including canonical
        aliases and retired tutorial ownership, without loading source bodies
        into Python or repeating catalog/policy queries for every reference.
        """
        identities = {identity for identity in identities if isinstance(identity, str)}
        sources, aliases, retired, targets = {}, {}, set(), {}
        with closing(self.store.connect()) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            pending = identities
            while pending:
                following = set()
                values = sorted(pending)
                for start in range(0, len(values), 400):
                    batch = values[start:start + 400]
                    marks = ",".join("?" for _ in batch)
                    # One multi-path extraction decodes a compressed revision
                    # once and returns only holdout-relevant identity metadata.
                    for row in db.execute("SELECT id,object_id,library_id,json_extract(payload,"
                            "'$.id','$.library_id','$.case_group','$.asset_key','$.asset_hash',"
                            "'$.data.canonical_tutorial.source_sha256') FROM revisions WHERE id IN (" + marks + ")", batch):
                        item = json.loads(row[3])
                        sources[row[0]] = {"id": item[0], "object_id": row[1], "library_id": item[1],
                            "case_group": item[2], "asset_key": item[3], "asset_hash": item[4],
                            "data": {"canonical_tutorial": {"source_sha256": item[5]}}, "owner_library_id": row[2]}
                    if "tutorial_aliases" in tables:
                        for row in db.execute("SELECT citation_id,owner_library_id,target_id,target_library_id "
                                              "FROM tutorial_aliases WHERE citation_id IN (" + marks + ")", batch):
                            aliases[row[0]] = tuple(row[1:])
                            if row[2] not in sources and row[2] not in pending:
                                following.add(row[2])
                    if "tutorial_retired_sources" in tables:
                        retired.update(row[0] for row in db.execute("SELECT citation_id FROM tutorial_retired_sources "
                            "WHERE reason='model_tutorial_copy' AND citation_id IN (" + marks + ")", batch))
                    if "tutorial_associations" in tables:
                        for row in db.execute("SELECT source_id,target_library_id FROM tutorial_associations "
                                              "WHERE source_id IN (" + marks + ")", batch):
                            targets.setdefault(row[0], set()).add(row[1])
                pending = following - set(sources) - set(aliases)
        result = {}
        for identity in identities:
            alias, source = aliases.get(identity), sources.get(identity)
            if alias:
                owner, target, target_library = alias
                source = sources.get(target)
                available = bool(source and target not in aliases and {owner, target_library} <= visible
                    and source["owner_library_id"] == target_library and source["id"] == target
                    and source["library_id"] == target_library and not policy.blocks_identity(target))
            else:
                available = bool(source and source["owner_library_id"] in visible
                    and (identity not in retired or targets.get(identity, set()) & visible)
                    and not policy.blocks_identity(identity, source["object_id"]))
            result[identity] = bool(available and not policy.blocks_identity(identity) and not policy.blocks_object(source))
        return result

    def _authority(self, values, bindings=None):
        # This snapshot is created anew both before counting and after paging.
        # Only immutable TOC metadata is cached between requests.
        policy = self.store.evaluation_policy()
        visible = set(self.store.searchable_library_ids())
        authority = {"cases": dict(bindings or {}), "sources": {}, "manuals": {}}
        identities = {ref.get("source_id") for value in values for ref in value.get("source_refs", [])}
        identities.update(value["case_id"] for value in values)
        authority["sources"] = self._source_permissions(identities, policy, visible)
        for case_id in {value["case_id"] for value in values}:
            if not authority["sources"].get(case_id):
                authority["cases"][case_id] = None
            elif case_id not in authority["cases"]:
                try:
                    authority["cases"][case_id] = self._case(case_id)
                except (KeyError, PermissionError, ValueError, OSError):
                    authority["cases"][case_id] = None
        authority["manuals"] = self.html.authorize_metadata_nodes(
            {value["manual"]["node_id"] for value in values if value.get("manual")}, policy=policy)
        return authority

    def _visible(self, value, authority):
        value = deepcopy(value)
        try:
            case_id = value["case_id"]
            binding = authority["cases"][case_id]
            if not binding or any(value["source_version"].get(key) != binding.get(key)
                                  for key in ("asset_hash", "revision_id", "generation")):
                return None
            original_modes = {ref.get("source_mode") for ref in value.get("source_refs", [])}
            refs = []
            for ref in value.get("source_refs", []):
                identity = ref.get("source_id")
                if authority["sources"].get(identity):
                    refs.append(ref)
            if value.get("source_refs") and not refs:
                return None
            value["source_refs"] = refs
            modes = {ref.get("source_mode") for ref in refs}
            blocked_modes = original_modes - modes
            requests = value.get("configuration_requests", [entry["arguments"] for entry in value.get("configuration_reads", [])])
            value["configuration_requests"] = [request for request in requests
                if request.get("source") not in blocked_modes]
            manual = value.get("manual")
            if manual:
                current = authority["manuals"].get(manual["node_id"])
                if (not current or current.get("asset_hash") != manual.get("doc_sha256")
                        or current.get("tree_generation") != manual.get("tree_generation")):
                    return None
                value["manual_read"] = {"tool": "read_html_section", "arguments": {"node_id": manual["node_id"]}}
            value["configuration_reads"] = [{"tool": "read_case_configuration", "arguments": deepcopy(request)}
                                            for request in value.pop("configuration_requests")]
            return value
        except (KeyError, PermissionError, ValueError, OSError):
            return None

    def _visible_rows(self, rows, bindings=None):
        values = [json.loads(row["body"]) for row in rows]
        authority = self._authority(values, bindings)
        return [value for row in values if (value := self._visible(row, authority)) is not None]

    def _final_gate(self, page):
        # Reauthorize the selected result once, after filtering/counting. These
        # permissions never survive a request; only TOC metadata is cached.
        authority = self._authority(page["items"])
        for item in page["items"]:
            checked = self._visible(item, authority)
            if (checked is None or checked["source_refs"] != item["source_refs"]
                    or checked["configuration_reads"] != item["configuration_reads"]):
                raise PermissionError("Association source authority changed during this request")
        return page

    @staticmethod
    def _page(items, offset, limit, **identity):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError("Association pages require offset>=0 and limit in 1..8")
        result = {"schema": SCHEMA, "state": "ready", **identity, "offset": offset, "limit": limit,
                  "total": len(items), "items": [], "returned": 0, "next_offset": None, "has_more": False,
                  "is_read_evidence": False, "note": "Associations and candidate confidence are navigation, not verified applicability, values, activation, or read evidence."}
        for item in items[offset:offset + limit]:
            result["items"].append(item)
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_BYTES - 100:
                result["items"].pop()
                break
        if offset < len(items) and not result["items"]:
            raise ValueError("One association metadata record exceeds the page budget; inspect the exact case node separately")
        end = offset + len(result["items"])
        result.update(returned=len(result["items"]), next_offset=end if end < len(items) else None, has_more=end < len(items))
        return result

    def links(self, case_id, node_path=None, offset=0, limit=8):
        binding = self._refresh(case_id)
        with closing(self._db()) as db:
            rows = db.execute("SELECT * FROM links WHERE case_id=?" + (" AND node_path=?" if node_path else "")
                              + " ORDER BY node_path,manual_node_id", (case_id, node_path) if node_path else (case_id,)).fetchall()
        visible = self._visible_rows(rows, {case_id: binding})
        page = self._page(visible, offset, limit, case_id=case_id, node_path=node_path)
        return self._final_gate(page)

    def reverse(self, manual_node_id, offset=0, limit=8):
        current = self.html.get_document(manual_node_id, metadata_only=True)
        if not current:
            raise PermissionError("Manual node is not currently readable")
        with closing(self._db()) as db:
            case_ids = [row[0] for row in db.execute("SELECT case_id FROM cases ORDER BY case_id")]
        for case_id in case_ids:
            try:
                self._refresh(case_id)
            except (KeyError, PermissionError, ValueError, OSError):
                continue
        with closing(self._db()) as db:
            rows = db.execute("SELECT * FROM links WHERE manual_node_id=? ORDER BY case_id,node_path", (manual_node_id,)).fetchall()
        visible = self._visible_rows(rows)
        page = self._page(visible, offset, limit, manual_node_id=manual_node_id,
                          coverage_scope="Only cases whose metadata associations have been constructed; no catalog-wide model scan")
        return self._final_gate(page)
