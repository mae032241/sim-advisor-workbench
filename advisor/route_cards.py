"""Experimental, paged method navigation grounded in actual constructor reads.

Cards are short explanations, never a source cache or planner citation credit.
The existing case-method authority and constructor read ledger remain the only
source/provenance authority. No library or retrieval index is built here.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from .case_methods import (
    CaseMethodBuilder, CaseMethodStale, CaseMethodStore, _CASE_ID, _ORIGINS,
    _ReadLedger, _json, _object, _ref_key, _reference_scope, _shape, _text,
)
from .index_manifest import _owned

SCHEMA = "route-method-card/v1"
PACK_SCHEMA = "route-method-card-pack/v1"
CATALOG_SCHEMA = "route-method-catalog/v1"
MANIFEST_SCHEMA = "route-method-manifest/v1"
SECTIONS = (
    "goals", "mechanism", "applicability", "exclusions", "alternatives",
    "required_inputs", "coupling_chain", "implementation", "gaps",
)
MAX_PAGE_BYTES = 6000
MAX_CARD_BYTES = 32 * 1024
MAX_FILE_BYTES = 64 * 1024
_METHOD_ID = {"type": "string", "pattern": r"^method-[a-f0-9]{64}$", "maxLength": 71,
              "description": "Exact server-issued method_id from list_route_methods; never invent an ID."}
_LOCK = threading.RLock()
_NOTE = ("Experimental method navigation. Original references must be read in the planning session "
         "before citation; the card grants no source-read credit or physical validation.")


def card_schema():
    """The complete, strict schema used by constructor submission and disk reads."""
    reference = _object({"source_id": _text(256),
                         "context": {"type": "string", "enum": ["source_text", "case_configuration"]},
                         "locator": _text(512)})
    claim = _object({
        "explanation": dict(_text(500), description="One short method claim. Separate documented facts, native configuration and inference; never copy raw tutorial/configuration bodies."),
        "origin": {"type": "string", "enum": list(_ORIGINS), "description":
                   "source_text is explicitly documented in a returned text page; saved_configuration/api_configuration is only the returned native mode; llm_inference is interpretation supported by actual reads; unknown is an unestablished gap and has no evidence_refs. None proves applicability or physical validity."},
        "evidence_refs": {"type": "array", "items": reference, "maxItems": 3, "uniqueItems": True,
                          "description": "Copy exact references from this constructor's successful reads, including the full locator without abbreviation. Every non-unknown claim needs at least one real-ledger reference. A node reference does not establish an unread property."},
        "limitations": {"type": "array", "items": _text(180), "maxItems": 3, "uniqueItems": True},
    })
    descriptions = {
        "goals": "Documented outputs and goals targeted by this method.",
        "mechanism": "Physical/computational mechanism and its stated assumptions; distinguish interpretation from observations.",
        "applicability": "Source-established conditions for choosing this method; leave unestablished conditions unknown.",
        "exclusions": "Documented exclusions or invalidity conditions; absence of evidence is not an invented exclusion rule.",
        "alternatives": "Documented alternatives and their decision conditions; unsupported alternatives remain unknown.",
        "required_inputs": "Quantities, material/geometry/operating inputs and variable dependencies required by the method.",
        "coupling_chain": "Quantities passed between interfaces or stages, with direction and variable dependencies when established.",
        "implementation": "Observed or documented implementation stages and settings; native configuration is not proof of effective activation.",
        "gaps": "Unknown selection conditions, unread dependencies, extraction limits and transfer checks still required.",
    }
    return _object({
        "schema": {"type": "string", "const": SCHEMA},
        "title": _text(100), "family": _text(100),
        "summary": dict(_text(350), description="Short navigation summary derived from the claims below, with no task/question IDs or evaluation answers. Not a new evidence claim."),
        "source_case_ids": {"type": "array", "items": deepcopy(_CASE_ID), "minItems": 1, "maxItems": 1,
                            "uniqueItems": True, "description": "Exactly the constructor's bound model_case citation ID. A case may yield multiple independently scoped cards."},
        "sections": _object({name: {"type": "array", "items": deepcopy(claim), "minItems": 1,
                                      "maxItems": 3, "description": descriptions[name]} for name in SECTIONS}),
    })


def tool_definitions():
    """Optional planner navigation tools; neither changes its evidence ledger."""
    listing = _object({
        "query": {"type": "string", "maxLength": 350, "default": "", "description": "Optional metadata words, all matched against title, family and summary; empty lists the catalog."},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 3, "default": 3},
    })
    listing["required"] = []
    reading = _object({
        "method_id": deepcopy(_METHOD_ID),
        "section": {"type": "string", "enum": ["overview", *SECTIONS], "default": "overview"},
        "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "Claim offset within a selected section; overview requires 0."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1,
                  "description": "Requested claims, default one. Follow next_offset until null; byte bounds may reduce returned count, never claim content. Overview requires 1."},
    })
    reading["required"] = ["method_id"]
    return [
        {"name": "list_route_methods", "description": "List a short experimental method directory: ID, title, family, summary and expandable section count only. Metadata search is navigation, not source evidence or proof of applicability. Default at most 3 entries and 6000 UTF-8 bytes; follow next_offset for every remaining entry. Disabled, held-out and stale dependencies invalidate entries.", "inputSchema": listing},
        {"name": "read_method_card", "description": "Read an experimental method overview or a single named section, default one complete atomic claim. There is no full-card view. Follow next_offset; original references are exact navigation locators and grant no planning-session read/citation credit. All dependencies must remain current and permitted. Every page is at most 6000 UTF-8 bytes; an indivisible oversized claim is an explicit error.", "inputSchema": reading},
    ]


def constructor_tool_definitions():
    return [{"name": "submit_method_card", "description":
             "Submit one short route method card from this constructor's actual service reads. The server generates a stable method ID; do not supply one. Keep documented text, saved/API configuration, inference and unknown separate. Use 1-3 claims in each of the nine sections; preserve conditions, exclusions and input/coupling dependencies only when established. Missing rules remain unknown. No task IDs, evaluation answers, raw source bodies or MPH copies. References are checked against the private real-read ledger; a node cannot authorize an unread property. Cards only provide navigation, never planner citation credit.",
             "inputSchema": _object({"card": card_schema()})}]


def _claims(card):
    return (claim for name in SECTIONS for claim in card["sections"][name])


def _method_id(card):
    return "method-" + hashlib.sha256(_json(card).encode("utf-8")).hexdigest()


def _bounded(value):
    if _page_bytes(value) > MAX_PAGE_BYTES:
        raise ValueError("A complete atomic method page exceeds 6000 UTF-8 bytes; shorten the claim or reduce its references before publication")
    return value


def _page_bytes(value):
    # The native runtime uses ordinary JSON serialization, including spaces.
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _metadata(identity, card):
    return {"method_id": identity, "title": card["title"], "family": card["family"],
            "summary": card["summary"], "section_count": len(SECTIONS)}


def _section_page(identity, card, section, offset, limit, claims):
    total = len(card["sections"][section])
    return {"state": "ready", "method_id": identity, "title": card["title"], "family": card["family"],
            "section": section, "offset": offset, "limit": limit, "total": total,
            "returned": len(claims), "next_offset": offset + len(claims) if offset + len(claims) < total else None,
            "claims": deepcopy(claims), "is_read_evidence": False, "note": _NOTE}


def _validate_card(card, references):
    _shape(card, card_schema(), "card")
    if len(_json(card).encode("utf-8")) > MAX_CARD_BYTES:
        raise ValueError("Method card exceeds the brief explanation limit")
    grounded = False
    for claim in _claims(card):
        origin, refs = claim["origin"], claim["evidence_refs"]
        if origin == "unknown":
            if refs:
                raise ValueError("unknown is a gap explanation; evidence_refs must be empty")
            continue
        if not refs:
            raise ValueError("A non-unknown claim requires real read evidence")
        kinds = []
        for ref in refs:
            kind = references.get(_ref_key(ref))
            if kind is None or not _reference_scope(ref, kind):
                raise ValueError("Unissued or forged read reference: " + _json(ref))
            kinds.append(kind)
        if origin != "llm_inference" and any(kind != origin for kind in kinds):
            raise ValueError("Text, native saved configuration and API observations are distinct evidence scopes")
        grounded |= origin != "llm_inference"
    if not grounded:
        raise ValueError("A method card requires at least one source-documented or native configuration claim")
    identity = _method_id(card)
    for section in SECTIONS:
        for offset, claim in enumerate(card["sections"][section]):
            _bounded(_section_page(identity, card, section, offset, 1, [claim]))
    return deepcopy(card)


def validate_card(card, *, receipts=None):
    """Plausible model-supplied receipt dicts are never trusted read ledgers."""
    if not isinstance(receipts, _ReadLedger):
        raise ValueError("Validation requires the constructor's private real-read ledger")
    value = _validate_card(card, receipts.references)
    for identity in value["source_case_ids"]:
        if identity not in receipts.bindings:
            raise ValueError("The exact source case must be read by this constructor")
    return value


class RouteCardStore(CaseMethodStore):
    """Experiment-owned cards, sharing all existing live source policy gates."""
    def __init__(self, root, provider):
        super().__init__(root, provider)
        self.directory = self.root / "route-method-cards"

    def _path(self, method_id):
        _shape(method_id, _METHOD_ID, "method_id")
        return _owned(self.root, self.directory / (method_id + ".json"))

    def _publish(self, path, raw):
        _owned(self.root, path)
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(path):
            raise FileExistsError("A frozen method artifact already exists; use a fresh experiment directory")
        stage = _owned(self.root, path.with_suffix("." + uuid.uuid4().hex + ".tmp"))
        try:
            with stage.open("xb") as handle:
                handle.write(raw)
            if os.name == "nt":
                os.rename(stage, path)
            else:
                os.link(stage, path)
        finally:
            stage.unlink(missing_ok=True)

    def save(self, card, *, receipts=None):
        card = validate_card(card, receipts=receipts)
        for case_id in card["source_case_ids"]:
            self._authority(case_id, case=True)
        used = set(card["source_case_ids"]) | {ref["source_id"] for claim in _claims(card) for ref in claim["evidence_refs"]}
        if len(used) > 64:
            raise ValueError("A method card supports at most 64 exact source dependencies")
        dependencies = {identity: deepcopy(receipts.bindings[identity]) for identity in sorted(used)}
        self._check_bindings(dependencies)
        refs = sorted({_ref_key(ref) for claim in _claims(card) for ref in claim["evidence_refs"]})
        identity = _method_id(card)
        envelope = {"schema": PACK_SCHEMA, "method_id": identity, "card": card, "dependencies": dependencies,
                    "reads": [{"source_id": key[0], "context": key[1], "locator": key[2],
                               "origin": receipts.references[key]} for key in refs],
                    "card_sha256": identity.removeprefix("method-")}
        raw = _json(envelope).encode("utf-8")
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("Method pack exceeds the bounded file limit")
        with _LOCK:
            if os.path.lexists(_owned(self.root, self.directory / "manifest.json")):
                raise FileExistsError("The method catalog is frozen; use a fresh experiment directory")
            self._check_bindings(dependencies)
            path = self._path(identity)
            self._publish(path, raw)
        return {"saved": True, "state": "ready", "method_id": identity,
                "path": str(path), "is_read_evidence": False}

    def _load(self, identity):
        path = self._path(identity)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
            raise ValueError("Method pack must be an owned, bounded regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or set(value) != {"schema", "method_id", "card", "dependencies", "reads", "card_sha256"}
                or value["schema"] != PACK_SCHEMA or value["method_id"] != identity):
            raise ValueError("Invalid route method pack schema or identity")
        card, dependencies, reads = value["card"], value["dependencies"], value["reads"]
        if (not isinstance(dependencies, dict) or not 1 <= len(dependencies) <= 64
                or not isinstance(reads, list) or len(reads) > 81):
            raise ValueError("Invalid route method dependency manifest")
        references = {}
        for read in reads:
            if not isinstance(read, dict) or set(read) != {"source_id", "context", "locator", "origin"}:
                raise ValueError("Invalid stored read reference")
            ref = {key: read[key] for key in ("source_id", "context", "locator")}
            if read["origin"] not in _ORIGINS[:3] or read["source_id"] not in dependencies:
                raise ValueError("Invalid stored read provenance")
            key = _ref_key(ref)
            if key in references:
                raise ValueError("Duplicate stored read reference")
            references[key] = read["origin"]
        _validate_card(card, references)
        used = set(card["source_case_ids"]) | {ref["source_id"] for claim in _claims(card) for ref in claim["evidence_refs"]}
        used_refs = {_ref_key(ref) for claim in _claims(card) for ref in claim["evidence_refs"]}
        if (_method_id(card) != identity or value["card_sha256"] != identity.removeprefix("method-")
                or used != set(dependencies) or used_refs != set(references)):
            raise ValueError("Method card content or source binding is invalid")
        for case_id in card["source_case_ids"]:
            self._authority(case_id, case=True)
        self._check_bindings(dependencies)
        return value

    def _identities(self):
        if not self.directory.exists():
            return []
        _owned(self.root, self.directory)
        return [path.stem for path in sorted(self.directory.glob("method-*.json"))
                if re.fullmatch(r"method-[a-f0-9]{64}\.json", path.name)]

    def list_methods(self, query="", offset=0, limit=3, *, enabled=True):
        _shape({"query": query, "offset": offset, "limit": limit}, tool_definitions()[0]["inputSchema"], "arguments")
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        entries = []
        if enabled:
            words = query.casefold().split()
            for identity in self._identities():
                try:
                    value = self._load(identity)
                except (PermissionError, CaseMethodStale):
                    continue
                metadata = _metadata(identity, value["card"])
                haystack = " ".join(metadata[key] for key in ("title", "family", "summary")).casefold()
                if all(word in haystack for word in words):
                    entries.append(metadata)
        entries.sort(key=lambda item: (item["family"].casefold(), item["title"].casefold(), item["method_id"]))
        page = entries[offset:offset + limit]
        while True:
            result = {"state": "ready" if enabled else "not_prepared", "query": query, "offset": offset, "limit": limit,
                      "total": len(entries), "returned": len(page),
                      "next_offset": offset + len(page) if offset + len(page) < len(entries) else None,
                      "methods": page, "is_read_evidence": False}
            if _page_bytes(result) <= MAX_PAGE_BYTES:
                return result
            if len(page) <= 1:
                return _bounded(result)
            page.pop()

    def read_method(self, method_id, section="overview", offset=0, limit=1, *, enabled=True):
        _shape({"method_id": method_id, "section": section, "offset": offset, "limit": limit}, tool_definitions()[1]["inputSchema"], "arguments")
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        if section == "overview" and (offset != 0 or limit != 1):
            raise ValueError("overview is one bounded metadata page; use offset=0 and limit=1")
        if not enabled:
            return {"state": "not_prepared", "method_id": method_id, "is_read_evidence": False}
        try:
            value = self._load(method_id)
        except FileNotFoundError:
            return {"state": "not_prepared", "method_id": method_id, "is_read_evidence": False}
        except CaseMethodStale as exc:
            return {"state": "stale", "method_id": method_id, "reason": str(exc), "is_read_evidence": False}
        card = value["card"]
        if section == "overview":
            return _bounded({"state": "ready", "method_id": method_id, "title": card["title"], "family": card["family"],
                             "summary": card["summary"], "source_case_ids": deepcopy(card["source_case_ids"]),
                             "sections": [{"section": name, "claim_count": len(card["sections"][name])} for name in SECTIONS],
                             "is_read_evidence": False, "note": _NOTE})
        page = card["sections"][section][offset:offset + limit]
        while True:
            result = _section_page(method_id, card, section, offset, limit, page)
            if _page_bytes(result) <= MAX_PAGE_BYTES:
                return result
            if len(page) <= 1:
                return _bounded(result)
            page.pop()

    def freeze_catalog(self):
        """Write deterministic catalog + manifest; immutable packs remain source-gated."""
        with _LOCK:
            values = [self._load(identity) for identity in self._identities()]
            entries = [_metadata(value["method_id"], value["card"]) for value in values]
            entries.sort(key=lambda item: (item["family"].casefold(), item["title"].casefold(), item["method_id"]))
            catalog = {"schema": CATALOG_SCHEMA, "methods": entries, "is_read_evidence": False}
            catalog_raw = _json(catalog).encode("utf-8")
            manifest = {"schema": MANIFEST_SCHEMA, "catalog_sha256": hashlib.sha256(catalog_raw).hexdigest(),
                        "methods": [{"method_id": value["method_id"],
                                     "pack_sha256": hashlib.sha256(self._path(value["method_id"]).read_bytes()).hexdigest(),
                                     "dependencies": value["dependencies"]} for value in values]}
            for filename, raw in (("catalog.json", catalog_raw), ("manifest.json", _json(manifest).encode("utf-8"))):
                path = _owned(self.root, self.directory / filename)
                if os.path.lexists(path):
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.read_bytes() != raw:
                        raise FileExistsError("A different frozen catalog already exists; use a fresh experiment directory")
                else:
                    self._publish(path, raw)
            return {"frozen": True, "method_count": len(values), "catalog_path": str(self.directory / "catalog.json"),
                    "manifest_path": str(self.directory / "manifest.json"), "catalog_sha256": manifest["catalog_sha256"]}


class RouteCardBuilder(CaseMethodBuilder):
    """Uses CaseMethodBuilder's actual successful-read ledger without a second index."""
    def __init__(self, store, read_tool, *, case_id):
        if case_id is None:
            raise ValueError("Route method construction requires one bound case_id")
        super().__init__(store, read_tool, case_id=case_id)

    def authorize_read(self, name, arguments):
        """Trusted wrapper callback, including before every cached page delivery.

        This is deliberately absent from model tool definitions. Other wrapper
        operations have no source identity and do not create any read credit.
        """
        if name not in {"read_source", "read_case_configuration"}:
            return None
        if not isinstance(arguments, dict):
            raise ValueError("A real service read requires argument fields")
        identity = arguments.get("id") if name == "read_source" else arguments.get("case_id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("A real service read needs an exact source identity")
        if name == "read_case_configuration" and identity != self.case_id:
            raise PermissionError("Native configuration reads are bound to the constructor's case ID")
        return self.store._authority(identity, case=name == "read_case_configuration")

    def record_delivery(self, name, arguments, result):
        """Trusted observer: credit only complete facts in this delivered page.

        The bounded service calls this with its original read tool/arguments,
        including for cached structured pages. Generic JSON fragments have no
        complete source/node/property reference and receive no ledger credit.
        Never expose this callback as an LLM tool or accept model receipts.
        """
        binding = self.authorize_read(name, arguments)
        if binding is None:
            return
        if not isinstance(result, dict):
            raise ValueError("Read delivery must be a service result object")
        identity = arguments.get("id") if name == "read_source" else arguments.get("case_id")
        references = self._returned_references(name, identity, arguments, result)
        if not references:
            return
        previous = self._receipts.bindings.get(identity)
        if previous is not None and previous != binding:
            raise CaseMethodStale("Source changed between constructor page deliveries")
        self._receipts.bindings[identity] = binding
        self._receipts.references.update(references)

    def tool_definitions(self):
        from .planning_service import tool_definitions as planning_definitions
        reads = [tool for tool in planning_definitions(bound=True)
                 if tool["name"] in {"read_source", "read_case_configuration"}]
        for tool in reads:
            if tool["name"] == "read_source":
                tool["inputSchema"]["properties"]["length"].update(maximum=2000, default=1000)
            else:
                tool["inputSchema"]["properties"]["limit"].update(maximum=1, default=1)
            tool["description"] += " Constructor-only actual read: small complete service pages; only returned exact refs enter the private ledger."
        contract = {"name": "get_tool_contract", "description": "Read the exact current constructor tool schema or one JSON-pointer field; never grants source read credit.",
                    "inputSchema": _object({"tool": {"type": "string", "enum": ["read_source", "read_case_configuration", "submit_method_card"]},
                                            "path": {"type": "string", "maxLength": 512, "default": ""}})}
        contract["inputSchema"]["required"] = ["tool"]
        return reads + constructor_tool_definitions() + [contract]

    def call(self, name, arguments):
        definitions = {tool["name"]: tool for tool in self.tool_definitions()}
        if name not in definitions:
            raise ValueError("The route constructor only reads sources/configuration, describes contracts, or submits method cards")
        _shape(arguments, definitions[name]["inputSchema"], "arguments")
        if name == "get_tool_contract":
            schema = definitions[arguments["tool"]]["inputSchema"]
            path = arguments.get("path", "")
            if path and not path.startswith("/"):
                raise ValueError("Contract path must be an exact JSON pointer")
            for part in path.split("/")[1:] if path else []:
                part = part.replace("~1", "/").replace("~0", "~")
                if part in schema.get("properties", {}):
                    schema = schema["properties"][part]
                elif schema.get("type") == "array" and (part == "items" or part.isdecimal()):
                    schema = schema["items"]
                else:
                    raise ValueError("Unknown contract field")
            return {"tool": arguments["tool"], "path": path, "schema": deepcopy(schema),
                    "semantics": "Real constructor reads only; preserve exact locators; unknown has no refs; inference is not observation; cards do not grant planner evidence credit."}
        if name == "submit_method_card":
            if arguments["card"]["source_case_ids"] != [self.case_id]:
                raise PermissionError("Constructor submission is bound to one exact source case")
            return self.store.save(arguments["card"], receipts=self._receipts)
        arguments = deepcopy(arguments)
        arguments.setdefault("length" if name == "read_source" else "limit", 1000 if name == "read_source" else 1)
        return super().call(name, arguments)
