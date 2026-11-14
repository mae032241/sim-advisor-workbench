"""Exact-case access to an on-demand, offline original MPH source snapshot.

No configuration artifacts, navigation trees, method cards or COMSOL runtime
are needed. Authorization is deliberately not cached with parsed XML.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import threading

from .raw_resource_tools import (MAX_BYTES, MAX_MPH_BYTES, RawResourceTools,
                                 _fingerprint, _path, tool_definitions)


TOOL_NAME = "inspect_case_source"
_BINDING_FIELDS = ("id", "type", "library_id", "object_id", "revision_id",
                   "asset_key", "asset_hash", "case_group")


def tool_definition():
    """Expose the native reader's small-page contract, with an exact case ID."""
    native = next(item for item in tool_definitions() if item["name"] == "inspect_original_mph")
    schema = copy.deepcopy(native["inputSchema"])
    schema["properties"].pop("path")
    schema["properties"]["case_id"] = {
        "type": "string", "minLength": 1, "maxLength": 256, "pattern": r"\S",
        "description": "Exact model_case ID returned by search_cases; not an asset_key or a file path.",
    }
    schema["required"] = ["case_id"]
    return {"name": TOOL_NAME, "description": (
        "After locating a case, inspect its registered original MPH on demand and offline, without "
        "COMSOL or a built compact/tree index. Use read_case_configuration for existing saved/API "
        "configuration. children follows unfiltered native XML; node_path / is the member root and "
        "/0/2 uses zero-based XML child positions, not compact node IDs. xml at / pages the original "
        "decoded member, while another node serializes that subtree. Only dmodel.xml/modelinfo.xml "
        "are read, with small offset pages. The current registered source must match this immutable "
        "case's asset_hash. Closed/deleted libraries and heldout cases remain unavailable. Results "
        "are original saved XML source snapshots with path/hash/range coordinates, not API "
        "observations, effective activation or solved/physical validation; they do not grant "
        "read_case_configuration evidence_refs or configuration-reading credit. In the workbench, "
        "nonempty xml pages receive separate original_mph_xml receipts for exactly the returned "
        "source text; children metadata alone grants no source-reading credit."
    ), "inputSchema": schema}


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


class CaseOriginalInspector:
    def __init__(self, store):
        self.store = store
        self._readers = OrderedDict()
        self._verified_hashes = OrderedDict()
        self._lock = threading.RLock()

    def _authorize(self, case_id):
        self.store.require_not_heldout(case_id)
        source = self.store.get(case_id)
        if not source or source.get("id") != case_id or source.get("type") != "model_case":
            raise ValueError("Original inspection requires an exact model_case source ID")
        self.store.require_not_heldout(case_id, source)
        self.store.require_readable_library(source["library_id"])
        if not self.store.source_available(case_id):
            raise PermissionError("This case source is no longer available for a new read")
        library = self.store.library(source["library_id"])
        roots = library.get("roots", [])
        asset = source.get("asset_key")
        digest = source.get("asset_hash")
        if not isinstance(asset, str) or not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            raise ValueError("The immutable case must pin its original asset_key and SHA256")
        root = next((item for item in sorted(roots, key=lambda item: len(item["id"]), reverse=True)
                     if asset.startswith(item["id"] + ":")), None)
        if root is None:
            raise PermissionError("The original case source root is no longer registered")
        relative = asset[len(root["id"]) + 1:]
        local, windows = Path(relative), PureWindowsPath(relative)
        if (not relative or "\x00" in relative or ":" in relative or local.is_absolute()
                or windows.anchor or ".." in local.parts or ".." in windows.parts):
            raise PermissionError("The original asset must stay inside its current registered root")
        boundary = _path(root["path"])
        path = _path(boundary / local)
        if not path.is_relative_to(boundary):
            raise PermissionError("The original asset escapes its current registered root")
        if path.suffix.casefold() != ".mph":
            raise ValueError("The case asset_key must identify its original MPH")
        return {"binding": tuple(source.get(key) for key in _BINDING_FIELDS),
                "roots": tuple((item["id"], item["path"], item.get("role")) for item in roots),
                "root": boundary, "path": path, "sha256": digest.lower()}

    def _reader(self, authority, budget):
        key = (authority["path"], authority["sha256"], authority["roots"], budget)
        with self._lock:
            reader = self._readers.get(key)
            if reader is None:
                # Roots locate the mount; only this exact MPH is authorized.
                reader = RawResourceTools(files=[authority["path"]], roots=[authority["root"]],
                                          strict_files=True, max_bytes=budget)
                self._readers[key] = reader
            self._readers.move_to_end(key)
            while len(self._readers) > 4:
                self._readers.popitem(last=False)
            return reader

    def _verify_original(self, reader, path, expected):
        """Hash a physical file version once; recheck path guards on every use.

        This cache records content identity only. The caller still authorizes
        the exact case and its currently registered roots before and after use.
        """
        path, before = reader._guard(path)
        if before.st_size > MAX_MPH_BYTES:
            raise ValueError("Original MPH exceeds the explicit on-demand size limit")
        fingerprint = _fingerprint(before)
        key = (path, fingerprint, expected)
        with self._lock:
            verified = key in self._verified_hashes
            if verified:
                self._verified_hashes.move_to_end(key)
        if verified:
            # Cached hash identity never skips current exact-file/reparse
            # guards, or the check for a version change during cache lookup.
            if _fingerprint(reader._guard(path)[1]) != fingerprint:
                with self._lock:
                    self._verified_hashes.pop(key, None)
                raise PermissionError("Original MPH changed during cached case verification")
            return fingerprint
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if _fingerprint(opened)[:4] != _fingerprint(before)[:4]:
                raise PermissionError("Original file identity changed before case verification")
            size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                if size > MAX_MPH_BYTES:
                    raise PermissionError("Original MPH changed during case verification")
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = reader._guard(path)[1]
        if (_fingerprint(before) != _fingerprint(current)
                or _fingerprint(opened) != _fingerprint(after)
                or _fingerprint(after)[:4] != _fingerprint(current)[:4]):
            raise PermissionError("Original MPH changed during case verification")
        if digest.hexdigest() != expected:
            raise PermissionError("Original MPH SHA256 does not match this immutable case revision")
        with self._lock:
            self._verified_hashes[key] = True
            self._verified_hashes.move_to_end(key)
            while len(self._verified_hashes) > 4:
                self._verified_hashes.popitem(last=False)
        return fingerprint

    def call(self, arguments):
        schema = tool_definition()["inputSchema"]
        properties = schema["properties"]
        if (not isinstance(arguments, dict) or set(arguments) - properties.keys()
                or "case_id" not in arguments):
            raise ValueError("Arguments do not match the case-original tool contract")
        for key, value in arguments.items():
            contract = properties[key]
            if contract["type"] == "integer":
                if (type(value) is not int or value < contract.get("minimum", 0)
                        or value > contract.get("maximum", 2**63 - 1)):
                    raise ValueError("Case-original page arguments are outside their contract")
            elif (not isinstance(value, str) or len(value) < contract.get("minLength", 0)
                    or len(value) > contract.get("maxLength", 2**31)
                    or "enum" in contract and value not in contract["enum"]):
                raise ValueError("Invalid case-original string argument")
        case_id = arguments["case_id"]
        if not case_id.strip():
            raise ValueError("An exact model case_id is required")
        route = arguments.get("node_path", "/") or "/"
        if not re.fullmatch(r"/(?:[0-9]+(?:/[0-9]+)*)?", route):
            raise ValueError("node_path must use zero-based original XML child positions")
        result = {"case_id": case_id, "basis": "original_saved_xml", "is_read_evidence": False,
                  "interpretation": "Source snapshot only; not API observations, effective activation, "
                                    "solve validation or read_case_configuration evidence_refs.",
                  "raw_result": {}}
        # JSON object braces are already counted in the wrapper's empty result.
        # Leave room for the product service's exact original-XML read receipt.
        budget = MAX_BYTES - _size(result) + 2 - 768
        authority = self._authorize(case_id)
        reader = self._reader(authority, budget)
        fingerprint = self._verify_original(reader, authority["path"], authority["sha256"])
        native_args = {key: value for key, value in arguments.items() if key != "case_id"}
        native_args["path"] = str(authority["path"])
        native = reader.call("inspect_original_mph", native_args)
        if native.get("sha256") != authority["sha256"]:
            raise PermissionError("Parsed original MPH does not match this immutable case revision")
        if self._authorize(case_id) != authority:
            raise PermissionError("Case identity or registered roots changed during original inspection")
        if _fingerprint(reader._guard(authority["path"])[1]) != fingerprint:
            raise PermissionError("Original MPH changed during case inspection")
        result["raw_result"] = native
        if _size(result) > MAX_BYTES:
            raise ValueError("Case-original response exceeds its explicit byte budget")
        return result
