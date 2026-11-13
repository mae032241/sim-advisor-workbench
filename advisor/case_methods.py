"""Source-grounded method notes for a bounded, opt-in experiment.

This module is deliberately outside production planning and retrieval. Its files
contain short explanations and exact read references, never a source-text cache.
An explanation is navigation, not new evidence credit for a planning session.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .index_manifest import _owned
from .plan_drafts import schema_errors

SCHEMA = "case-method/v1"
PACK_SCHEMA = "case-method-pack/v1"
MAX_PROFILE_BYTES = 24 * 1024
MAX_FILE_BYTES = 64 * 1024
SECTIONS = (
    "objective_outputs", "method_and_assumptions", "applicability_conditions",
    "failure_conditions", "domains_and_interfaces", "transfer_and_setting_dependencies",
    "migration_boundaries", "evidence_coverage_unknowns",
)
_ORIGINS = ("source_text", "saved_configuration", "api_configuration", "llm_inference", "unknown")
_CASE_ID = {"type": "string", "minLength": 1, "maxLength": 256, "pattern": r"^[^\s/\\:]+$",
            "description": "Exact model_case citation ID returned by the existing service; never a filename or asset path."}


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _text(maximum=500):
    return {"type": "string", "minLength": 1, "maxLength": maximum, "pattern": r"\S"}


def profile_schema():
    """The complete constructor contract, shared with the actual validator."""
    reference = _object({"source_id": _text(256),
                         "context": {"type": "string", "enum": ["source_text", "case_configuration"]},
                         "locator": _text(1024)})
    claim = _object({
        "explanation": dict(_text(), description="Brief method explanation, not copied tutorial/configuration text. One origin per statement; separate inference from what was read."),
        "origin": {"type": "string", "enum": list(_ORIGINS), "description":
                   "source_text: explicitly documented in a real text page; saved_configuration/api_configuration: only the native configuration returned for that mode; llm_inference: interpretation supported by reads, not an observation; unknown: the source has not established this, with no evidence_refs. No origin proves physical validity or applicability to a new problem."},
        "evidence_refs": {"type": "array", "items": reference, "maxItems": 6, "uniqueItems": True,
                          "description": "Copy exact references from this constructor's successful service reads. Other than origin=unknown, at least one reference is required. Text and native configuration scopes cannot substitute for one another."},
        "limitations": {"type": "array", "items": _text(240), "maxItems": 4, "uniqueItems": True},
    })
    descriptions = {
        "objective_outputs": "Case objective and outputs that the documented method targets.",
        "method_and_assumptions": "Method, modeling choices, and assumptions; keep reasoning distinct from observations.",
        "applicability_conditions": "Conditions under which the documented method is applicable; unknown conditions remain unknown.",
        "failure_conditions": "Documented failure or invalidity conditions; do not invent a physics rule when absent.",
        "domains_and_interfaces": "Participating domains and physical interfaces, and their roles.",
        "transfer_and_setting_dependencies": "Quantities passed between stages/interfaces and dependencies needed for settings to make sense.",
        "migration_boundaries": "What can be reused as a method and what must be checked or replaced for a different problem.",
        "evidence_coverage_unknowns": "Read coverage, extraction limitations and unresolved questions. Missing evidence is not an observed result.",
    }
    return _object({"schema": {"type": "string", "const": SCHEMA}, "case_id": deepcopy(_CASE_ID),
                    "sections": _object({name: {"type": "array", "items": deepcopy(claim), "minItems": 1,
                                                 "maxItems": 6, "description": descriptions[name]} for name in SECTIONS})})


def tool_definitions():
    """Same optional read tool for both experiment conditions."""
    return [{"name": "get_case_method", "description":
             "Read an experimental case method and its applicability notes by exact case_id. ready notes are short source-derived navigation with explicit inference/unknown boundaries; read their original sources before using them as evidence. not_prepared/stale provides no method. Does not grant source-read credit or prove applicability.",
             "inputSchema": _object({"case_id": deepcopy(_CASE_ID)})}]


def constructor_tool_definitions():
    """Only the isolated constructor gets submission, never production tools."""
    return [{"name": "submit_case_method", "description":
             "Submit one short method profile derived solely from this constructor's real service reads. Use the full exact schema; source_text and native saved/API configuration scopes are distinct. State llm_inference and unknown explicitly. Do not include evaluation questions/answers or copy source bodies. Service-issued references are checked against a private read ledger.",
             "inputSchema": _object({"profile": profile_schema()})}]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _shape(value, schema, where):
    errors = schema_errors(value, schema)
    if errors:
        raise ValueError(where + " does not match its published schema: " + _json(errors))


def _ref_key(ref):
    return (ref["source_id"], ref["context"], ref["locator"])


def _reference_scope(ref, origin):
    if origin == "source_text":
        match = re.fullmatch(r"text:([0-9]+):([0-9]+)", ref["locator"])
        return bool(ref["context"] == "source_text" and match and int(match[2]) > int(match[1]))
    prefix = "saved:" if origin == "saved_configuration" else "api:" if origin == "api_configuration" else None
    return bool(prefix and ref["context"] == "case_configuration" and ref["locator"].startswith(prefix)
                and len(ref["locator"]) > len(prefix))


def _claims(profile):
    return (claim for section in SECTIONS for claim in profile["sections"][section])


def _validate_claims(profile, references):
    _shape(profile, profile_schema(), "profile")
    if len(_json(profile).encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ValueError("Method profile exceeds the brief explanation limit")
    grounded = False
    for claim in _claims(profile):
        origin, refs = claim["origin"], claim["evidence_refs"]
        if origin == "unknown":
            if refs:
                raise ValueError("unknown is a gap explanation, not an observed/documented fact; evidence_refs must be empty")
            continue
        if not refs:
            raise ValueError("A method explanation needs real read evidence; use origin=unknown for a gap")
        kinds = []
        for ref in refs:
            kind = references.get(_ref_key(ref))
            if kind is None or not _reference_scope(ref, kind):
                raise ValueError("Unissued or forged read reference: " + _json(ref))
            kinds.append(kind)
        if origin != "llm_inference" and any(kind != origin for kind in kinds):
            raise ValueError("Text summaries, native saved configuration and API observations are distinct evidence scopes")
        grounded |= origin != "llm_inference"
    if not grounded:
        raise ValueError("A method profile requires at least one source-documented or native configuration statement")
    return deepcopy(profile)


class _ReadLedger:
    """Only CaseMethodBuilder's own successful service calls populate this."""
    def __init__(self):
        self.references = {}
        self.bindings = {}


def validate_profile(profile, *, receipts=None):
    """Strict shape and provenance validation; model-supplied receipts fail closed.

    Use CaseMethodBuilder.call for real reads and submission. A dict containing
    plausible evidence_refs is never a trusted receipt ledger.
    """
    if not isinstance(receipts, _ReadLedger):
        raise ValueError("Validation requires the constructor's private real-read ledger; supplied receipts are not evidence")
    value = _validate_claims(profile, receipts.references)
    if value["case_id"] not in receipts.bindings:
        raise ValueError("The exact source case must be read by this constructor")
    return value


class CaseMethodStale(RuntimeError):
    """The exact source revision/model binding is no longer current."""


def _base_store(provider):
    seen = set()
    while id(provider) not in seen:
        seen.add(id(provider))
        if getattr(provider, "store", None) is not None:
            return provider.store
        provider = getattr(provider, "_base", None)
        if provider is None:
            break
    raise TypeError("Case methods require an existing library provider/store with live holdout gates")


class CaseMethodStore:
    """An experiment-owned file store; no shared-library writes or indexing."""
    def __init__(self, experiment_root, provider):
        self.root = Path(experiment_root).absolute()
        self.provider = provider
        self.source_store = _base_store(provider)
        source_root = Path(self.source_store.root).resolve()
        if self.root.resolve().is_relative_to(source_root):
            raise ValueError("Method files must be in an experiment-owned directory outside shared libraries")
        _owned(Path(self.root.anchor), self.root)
        self.directory = self.root / "case-methods"

    def _path(self, case_id):
        _shape(case_id, _CASE_ID, "case_id")
        digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
        return _owned(self.root, self.directory / (digest + ".json"))

    def _authority(self, source_id, *, case=False):
        """Use fresh existing policy gates; archived payloads never authorize reads."""
        store = self.source_store
        store.require_not_heldout(source_id)
        if not store.source_available(source_id):
            raise PermissionError("Source library is disabled, removed, held out, or unavailable")
        # get() provides immutable identity metadata without hydrating a compact
        # model. Wrapped challenge providers must additionally enforce their own
        # source boundary; their _base's policy alone would not be sufficient.
        source = store.get(source_id)
        if source is None:
            raise PermissionError("Source is no longer available")
        store.require_not_heldout(source_id, source)
        available = getattr(self.provider, "source_available", None)
        if callable(available) and not available(source):
            raise PermissionError("Provider denies this source under its current library policy")
        if getattr(self.provider, "store", None) is None:
            visible = self.provider.get_document(source_id)
            if not visible:
                raise PermissionError("Provider excludes this source")
        if source.get("id") != source_id or case and source.get("type") != "model_case":
            raise ValueError("case_id must identify an exact model_case source")
        # Current citation lookup is one indexed scalar query per dependency.
        # Old archived IDs can remain readable for history, but cannot make a
        # prepared method look current after a source/model revision changes.
        if source.get("object_id") and source.get("library_id"):
            with closing(store.connect()) as db:
                row = db.execute("SELECT citation_id FROM active WHERE library_id=? AND object_id=?",
                                 (source["library_id"], source["object_id"])).fetchone()
            if row is None or row[0] != source_id:
                raise CaseMethodStale("Source revision is no longer the active source")
        binding = {key: source.get(key) for key in
                   ("id", "object_id", "revision_id", "library_id", "type", "asset_key", "asset_hash", "case_group")}
        binding["evidence_hashes"] = sorted({str(ref["asset_hash"]) for ref in source.get("evidence", [])
                                             if isinstance(ref, dict) and ref.get("asset_hash")})
        store.require_not_heldout(source_id, source)
        if not store.source_available(source_id) or callable(available) and not available(source):
            raise PermissionError("Source policy changed during method access")
        return binding

    def _check_bindings(self, dependencies):
        for identity, expected in dependencies.items():
            if self._authority(identity) != expected:
                raise CaseMethodStale("Source revision or model hash changed; rebuild the method profile")

    def save(self, profile, *, receipts=None):
        """Only a real-read constructor ledger can publish a short profile."""
        profile = validate_profile(profile, receipts=receipts)
        self._authority(profile["case_id"], case=True)
        used = {profile["case_id"]} | {ref["source_id"] for claim in _claims(profile) for ref in claim["evidence_refs"]}
        dependencies = {identity: deepcopy(receipts.bindings[identity]) for identity in sorted(used)}
        self._check_bindings(dependencies)
        refs = sorted({_ref_key(ref) for claim in _claims(profile) for ref in claim["evidence_refs"]})
        envelope = {"schema": PACK_SCHEMA, "profile": profile, "dependencies": dependencies,
                    "reads": [{"source_id": key[0], "context": key[1], "locator": key[2],
                               "origin": receipts.references[key]} for key in refs],
                    "profile_sha256": hashlib.sha256(_json(profile).encode("utf-8")).hexdigest()}
        raw = _json(envelope).encode("utf-8")
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("Method pack exceeds the bounded profile limit")
        path = self._path(profile["case_id"])
        if os.path.lexists(path):
            raise FileExistsError("A prepared method profile already exists; use a fresh experiment directory")
        self.directory.mkdir(parents=True, exist_ok=True)
        stage = _owned(self.root, path.with_suffix("." + uuid.uuid4().hex + ".tmp"))
        try:
            with stage.open("xb") as handle:
                handle.write(raw)
            self._check_bindings(dependencies)
            # Windows rename fails if the destination exists. POSIX rename
            # replaces it, so publish there with an atomic create-if-absent link.
            # Never replace a frozen profile after a competing publication.
            if os.name == "nt":
                os.rename(stage, path)
            else:
                os.link(stage, path)
        finally:
            stage.unlink(missing_ok=True)
        return {"saved": True, "state": "ready", "case_id": profile["case_id"], "path": str(path),
                "is_read_evidence": False}

    def get_case_method(self, case_id, *, enabled=True):
        """A and B share this tool. Disabled/held-out dependencies fail closed."""
        path = self._path(case_id)
        try:
            self._authority(case_id, case=True)
            if not enabled or not path.exists():
                return {"state": "not_prepared", "case_id": case_id, "is_read_evidence": False}
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
                raise ValueError("Method pack must be an owned, bounded regular file")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or set(value) != {"schema", "profile", "dependencies", "reads", "profile_sha256"} or value["schema"] != PACK_SCHEMA:
                raise ValueError("Unknown method pack schema")
            profile, dependencies, reads = value["profile"], value["dependencies"], value["reads"]
            if (not isinstance(dependencies, dict) or not 1 <= len(dependencies) <= 64
                    or not isinstance(reads, list) or len(reads) > 288):
                raise ValueError("Invalid method dependency manifest")
            references = {}
            for read in reads:
                if not isinstance(read, dict) or set(read) != {"source_id", "context", "locator", "origin"}:
                    raise ValueError("Invalid stored read reference")
                ref = {key: read[key] for key in ("source_id", "context", "locator")}
                if read["origin"] not in _ORIGINS[:3] or read["source_id"] not in dependencies:
                    raise ValueError("Invalid stored read provenance")
                references[_ref_key(ref)] = read["origin"]
            _validate_claims(profile, references)
            used = {case_id} | {ref["source_id"] for claim in _claims(profile) for ref in claim["evidence_refs"]}
            if (profile["case_id"] != case_id or used != set(dependencies)
                    or value["profile_sha256"] != hashlib.sha256(_json(profile).encode("utf-8")).hexdigest()):
                raise ValueError("Method profile binding is invalid")
            self._check_bindings(dependencies)
            self._authority(case_id, case=True)
            return {"state": "ready", "case_id": case_id, "profile": deepcopy(profile), "is_read_evidence": False,
                    "note": "Experimental method explanation. Original refs are navigation, not this planning session's read evidence. Applicability and physical validity remain unverified."}
        except CaseMethodStale as exc:
            return {"state": "stale", "case_id": case_id, "is_read_evidence": False, "reason": str(exc)}

    get = get_case_method

    def list(self):
        """List only profiles whose case and complete dependency set are readable."""
        if not self.directory.exists():
            return []
        _owned(self.root, self.directory)
        result = []
        for path in sorted(self.directory.glob("*.json")):
            _owned(self.root, path)
            if not re.fullmatch(r"[a-f0-9]{64}\.json", path.name):
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Method pack exceeds the bounded profile limit")
            value = json.loads(path.read_text(encoding="utf-8"))
            identity = value.get("profile", {}).get("case_id")
            if path != self._path(identity):
                raise ValueError("Method filename does not bind its exact case ID")
            try:
                view = self.get_case_method(identity)
            except PermissionError:
                continue
            result.append({"case_id": identity, "state": view["state"], "is_read_evidence": False})
        return result


class CaseMethodBuilder:
    """Read through the existing service; keep receipts out of model arguments."""
    def __init__(self, method_store, read_tool, *, case_id=None):
        self.store = method_store
        self._read_tool = read_tool
        self._receipts = _ReadLedger()
        self._read_count = 0
        if case_id is not None:
            _shape(case_id, _CASE_ID, "case_id")
        self.case_id = case_id

    def tool_definitions(self):
        return constructor_tool_definitions()

    def call(self, name, arguments):
        if name == "get_tool_contract":
            if not isinstance(arguments, dict) or set(arguments) - {"tool", "path"} or arguments.get("tool") != "submit_case_method":
                raise ValueError("This constructor contract describes submit_case_method only")
            schema = constructor_tool_definitions()[0]["inputSchema"]
            path = arguments.get("path", "")
            if not isinstance(path, str) or path and not path.startswith("/"):
                raise ValueError("Contract path must be an exact JSON pointer")
            for part in path.split("/")[1:] if path else []:
                part = part.replace("~1", "/").replace("~0", "~")
                if part in schema.get("properties", {}):
                    schema = schema["properties"][part]
                elif schema.get("type") == "array" and (part == "items" or part.isdecimal()):
                    schema = schema["items"]
                else:
                    raise ValueError("Unknown contract field")
            return {"tool": "submit_case_method", "path": path, "schema": deepcopy(schema),
                    "semantics": "Service-issued real reads only; unknown has no refs, inference is not an observation; text and native configuration are distinct scopes."}
        if name == "submit_case_method":
            _shape(arguments, constructor_tool_definitions()[0]["inputSchema"], "arguments")
            if self.case_id is not None and arguments["profile"]["case_id"] != self.case_id:
                raise PermissionError("Constructor submission is bound to one exact case ID")
            return self.store.save(arguments["profile"], receipts=self._receipts)
        if name not in {"read_source", "read_case_configuration"}:
            raise ValueError("The method constructor records only real text/configuration reads")
        if self._read_count >= 128:
            raise ValueError("Method construction requires a bounded real-read budget")
        arguments = deepcopy(arguments)
        identity = arguments.get("id") if name == "read_source" else arguments.get("case_id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("A real service read needs an exact source identity")
        if name == "read_case_configuration" and self.case_id is not None and identity != self.case_id:
            raise PermissionError("Native configuration reads are bound to the constructor's case ID")
        before = self.store._authority(identity, case=name == "read_case_configuration")
        self._read_count += 1
        result = self._read_tool(name, arguments)
        after = self.store._authority(identity, case=name == "read_case_configuration")
        if before != after:
            raise CaseMethodStale("Source changed during the service read")
        if not isinstance(result, dict):
            raise ValueError("Read service did not return a source page")
        references = self._returned_references(name, identity, arguments, result)
        if references:
            previous = self._receipts.bindings.get(identity)
            if previous is not None and previous != after:
                raise CaseMethodStale("Source changed between constructor reads")
            self._receipts.bindings[identity] = after
            self._receipts.references.update(references)
        return deepcopy(result)

    @staticmethod
    def _returned_references(name, identity, arguments, result):
        references = {}
        if name == "read_source":
            text, offset = result.get("text"), result.get("offset")
            if result.get("id") != identity or not isinstance(text, str) or not text.strip() or type(offset) is not int:
                return references
            expected = {"source_id": identity, "context": "source_text", "locator": f"text:{offset}:{offset + len(text)}"}
            if expected in result.get("evidence_refs", []):
                references[_ref_key(expected)] = "source_text"
            return references
        if result.get("case_id") != identity or not result.get("returned") or not result.get("nodes"):
            return references
        mode = arguments.get("source", "saved")
        expected_sources = {"saved", "xml"} if mode == "saved" else {"api"}
        if result.get("source") not in expected_sources:
            raise ValueError("Native configuration returned a different source mode")
        for node in result["nodes"]:
            path = node.get("path")
            if not isinstance(path, str) or not path:
                continue
            locator = mode + ":" + path
            expected = {"source_id": identity, "context": "case_configuration", "locator": locator}
            if expected not in result.get("evidence_refs", []):
                continue
            origin = "saved_configuration" if mode == "saved" else "api_configuration"
            references[_ref_key(expected)] = origin
            for prop in node.get("properties", []):
                property_locator = prop.get("evidence_locator")
                if isinstance(property_locator, str) and property_locator.startswith(locator + "::property/"):
                    references[(identity, "case_configuration", property_locator)] = origin
        return references
