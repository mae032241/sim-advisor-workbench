"""Read-only, auditable corpus boundary for geometry-only held-out challenges.

This is an evaluation boundary, not an index mutation.  Registered, active objects
use the requested engine; the separate legacy branch exposes generic manuals via
native retrieval only.  Every read is checked, including direct historical IDs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import posixpath
import re
import threading
import unicodedata
from urllib.parse import unquote


_IDENTITY_KEYS = {"relative_path", "source_path", "model_path", "tutorial_path", "asset_key", "url", "source_uri", "uri", "path", "package"}
_SOURCE_DATA_KEYS = {"source_path", "model_path", "tutorial_path", "relative_path", "asset_key", "source_uri"}
_DROP = object()


def _canonical(value):
    value = unicodedata.normalize("NFKC", str(value)).strip()
    for _ in range(3):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    value = value.replace("\\", "/").casefold()
    # Retain the package/hostname as well as the path; COMSOL packages identify
    # case families, whereas URLs alone can contain opaque library revision IDs.
    value = value.split("#", 1)[0].split("?", 1)[0]
    return posixpath.normpath(value)


def _family(path):
    value = _canonical(path).rstrip("/").rsplit("/", 1)[-1]
    value = re.sub(r"\.(?:mph|html?|pdf|java|m|py|zip|txt)$", "", value)
    if re.search(r"(?:^|\.)models\.", value):
        value = value.rsplit(".", 1)[-1]
    return re.sub(r"_(?:geom(?:etry)?(?:_sequence)?|solution|mesh|code)$", "", value)


class ChallengeKnowledgeProvider:
    """Pin eligible active libraries, exclude a case family, and audit real reads.

    ``excluded_paths`` accepts canonical case stems or source paths.  A stem
    excludes that tutorial package and its suffixed assets across doc versions.
    Caller filters can narrow the pinned libraries, never expand them or change
    the engine.  An explicit library filter omits the legacy branch, matching
    LibraryKnowledgeProvider's existing filter semantics.
    """

    def __init__(self, base, excluded_paths, excluded_terms=None, mode="native", library_ids=None):
        if mode not in {"native", "qmd_keyword", "qmd_vector"}:
            raise ValueError("Unsupported challenge retrieval mode")
        if not isinstance(excluded_paths, list) or not excluded_paths or any(not isinstance(p, str) or not p.strip() for p in excluded_paths):
            raise ValueError("A held-out challenge requires nonempty excluded source paths")
        if excluded_terms is not None and (not isinstance(excluded_terms, list) or any(not isinstance(t, str) or not t.strip() for t in excluded_terms)):
            raise ValueError("excluded_terms must be nonempty strings")
        if library_ids is not None and (not isinstance(library_ids, list) or any(not isinstance(x, str) for x in library_ids)):
            raise ValueError("library_ids must be a list of library IDs")
        self._base = base
        self.mode = mode
        self._lock = threading.RLock()
        self._events = []
        self._event_counter = 0
        self._audit_dropped = 0
        self._query_counter = 0
        self._seen_backends = set()
        self._last_query = {}
        self._paths = sorted({_canonical(p) for p in excluded_paths})
        self._families = sorted({_family(p) for p in excluded_paths})
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name) for name in self._families):
            raise ValueError("Excluded paths must identify a safe case stem")
        self._family_patterns = [re.compile(r"(?<![a-z0-9])" + re.escape(name) + r"(?:_[a-z0-9]+)*(?=$|[^a-z0-9_])") for name in self._families]
        # Human-readable cross-references are redacted too, but do not determine
        # a source's identity.  Generic words such as 'contact' are never added.
        self._text_patterns = self._family_patterns + [re.compile(r"(?<!\w)" + r"[\W_]+".join(map(re.escape, re.split(r"[_-]+", name))) + r"(?!\w)", re.I) for name in self._families]
        self._text_patterns += [re.compile(re.escape(unicodedata.normalize("NFKC", t)), re.I) for t in (excluded_terms or [])]
        store = getattr(base, "store", None)
        if store is None or not hasattr(base, "legacy"):
            raise TypeError("ChallengeKnowledgeProvider requires a LibraryKnowledgeProvider-compatible base")
        registered = {item["id"] for item in store.libraries()}
        self.library_ids = tuple(sorted(registered if library_ids is None else registered.intersection(library_ids)))
        self._active_ids = {item["id"] for item in store.objects(list(self.library_ids))}
        generation = getattr(store, "current_generation", None)
        self._generation = generation() if callable(generation) else None
        self._policy_id = hashlib.sha256(json.dumps({"paths": self._paths, "terms": excluded_terms or [], "mode": mode, "libraries": self.library_ids, "active_ids": sorted(self._active_ids)}, sort_keys=True).encode()).hexdigest()[:24]

    def _record(self, event, **values):
        with self._lock:
            self._event_counter += 1
            self._events.append(dict(sequence=self._event_counter, event=event, **values))
            if len(self._events) > 4000:
                del self._events[:1000]
                self._audit_dropped += 1000

    def audit_snapshot(self):
        with self._lock:
            return {"policy_id": self._policy_id, "events": copy.deepcopy(self._events), "dropped_events": self._audit_dropped}

    @property
    def audit(self):
        """Serializable event copy; callers cannot mutate the provider's audit."""
        return self.audit_snapshot()["events"]

    def _forbidden_locator(self, value):
        value = _canonical(value)
        return any(pattern.search(value) for pattern in self._family_patterns) or any(value == path or value.startswith(path + "/") for path in self._paths)

    def _forbidden_text(self, value):
        value = unicodedata.normalize("NFKC", str(value))
        for _ in range(3):
            value = unquote(value)
        return any(pattern.search(value.casefold()) for pattern in self._text_patterns)

    def _identity_values(self, document):
        values = [v for key, v in document.items() if key in _IDENTITY_KEYS and isinstance(v, str)]
        def evidence_values(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in _IDENTITY_KEYS and isinstance(item, str):
                        values.append(item)
                    elif isinstance(item, (dict, list)):
                        evidence_values(item)
            elif isinstance(value, list):
                for item in value:
                    evidence_values(item)
        evidence_values(document.get("evidence", []))
        data = document.get("data", {})
        if isinstance(data, dict):
            values.extend(v for key, v in data.items() if key in _SOURCE_DATA_KEYS and isinstance(v, str))
            evidence_values(data.get("evidence", []))
        return values

    def _reason(self, document):
        if not isinstance(document, dict) or not document.get("id"):
            return "missing_source"
        cid = str(document["id"])
        modern = cid.startswith("lo-") or bool(document.get("library_id"))
        if modern and (cid not in self._active_ids or document.get("library_id") not in self.library_ids):
            return "outside_active_library_boundary"
        if not modern and document.get("kind") != "manual":
            return "legacy_tutorial_or_code_outside_boundary"
        values = self._identity_values(document)
        if any(self._forbidden_locator(v) for v in values):
            return "excluded_source_family"
        # Opaque library URIs and content-hash blob paths do not establish the
        # original case provenance.  Require a resolvable source locator.
        origin = [v for v in values if v not in self.library_ids and not str(v).startswith("library://") and not re.search(r"(?:^|[/\\:])blobs[/\\]", str(v))]
        if not origin:
            return "missing_original_provenance"
        if any(self._forbidden_text(v) for v in document.get("section_path", []) if isinstance(v, str)):
            return "excluded_tutorial_section"
        return None

    def _clean_document(self, raw, seen=None):
        reason = self._reason(raw)
        if reason:
            return None, reason
        validator = getattr(self._base, "validate_source", None)
        if not callable(validator) or not validator(raw):
            return None, "unverified_original_source"
        seen = set(seen or ())
        if raw["id"] in seen or len(seen) >= 8:
            return None, "parent_cycle_or_depth"
        seen.add(raw["id"])
        removed = []

        def scrub(value, path=""):
            if isinstance(value, str):
                if not self._forbidden_text(value):
                    return value
                # COMSOL HTML extraction preserves block boundaries as newlines.
                # Delete complete referring blocks, never splice a purported quote.
                blocks = value.splitlines()
                kept = [line for line in blocks if not self._forbidden_text(line)]
                removed.append(path)
                return "\n".join(kept).strip() if kept else _DROP
            if isinstance(value, list):
                output = []
                for index, item in enumerate(value):
                    cleaned = scrub(item, f"{path}[{index}]")
                    if cleaned is not _DROP:
                        output.append(cleaned)
                return output
            if isinstance(value, dict):
                if any(isinstance(v, str) and self._forbidden_text(v) for k, v in value.items() if k in {"href", "url", "uri", "relative_path", "asset_key"}):
                    removed.append(path)
                    return _DROP
                if path and any(key in value for key in ("href", "url", "uri")) and any(isinstance(v, str) and self._forbidden_text(v) for v in value.values()):
                    removed.append(path)
                    return _DROP
                output = {}
                for key, item in value.items():
                    if key in {"parent", "parent_id"}:
                        continue
                    cleaned = scrub(item, f"{path}.{key}")
                    if cleaned is not _DROP:
                        output[key] = cleaned
                return output
            return copy.deepcopy(value)

        clean = scrub(raw)
        if clean is _DROP or not clean.get("text", "").strip() or not clean.get("title", "").strip():
            return None, "no_safe_source_content"
        parent_id = raw.get("parent_id") or (raw.get("parent") or {}).get("id")
        if parent_id:
            parent = self._base.get_document(parent_id)
            safe_parent, parent_reason = self._clean_document(parent, seen)
            self._record("context_check", source_id=raw["id"], context_id=parent_id, allowed=bool(safe_parent), reason=parent_reason)
            if safe_parent:
                clean["parent_id"] = parent_id
                clean["parent"] = {key: safe_parent[key] for key in ("id", "title", "excerpt", "section_path") if key in safe_parent}
            else:
                clean["parent_id"] = None
                removed.append("parent")
        elif "parent_id" in raw:
            clean["parent_id"] = None
        if not clean.get("excerpt") or self._forbidden_text(clean["excerpt"]):
            clean["excerpt"] = clean["text"][:1200]
        clean["challenge_filter"] = {"policy_id": self._policy_id, "redacted": bool(removed), "removed_fields": sorted(set(removed)), "source_content": "filtered_original" if removed else "original", "parent_context_checked": True}
        return clean, None

    def get_document(self, source_id):
        raw = self._base.get_document(source_id)
        clean, reason = self._clean_document(raw)
        with self._lock:
            query_id = self._last_query.get(source_id)
        self._record("read", source_id=source_id, query_id=query_id, allowed=bool(clean), reason=reason,
                     redacted=bool(clean and clean["challenge_filter"]["redacted"]))
        return copy.deepcopy(clean)

    def validate_source(self, document):
        cid = document.get("id") if isinstance(document, dict) else None
        raw = self._base.get_document(cid) if cid else None
        expected, reason = self._clean_document(raw)
        # Search score/backend/rank are operational metadata; all original and
        # filtered source fields, including evidence and text, remain immutable.
        operational = {"retrieval", "retrieval_backend", "score"}
        allowed = bool(expected and all(document.get(k) == v for k, v in expected.items() if k not in operational)
                       and not any(self._forbidden_text(str(v)) for k, v in document.items() if k not in operational and k not in expected))
        self._record("validate", source_id=cid, allowed=allowed, reason=None if allowed else (reason or "source_view_mismatch"))
        return allowed

    def search(self, query, limit=8, kind="all", filters=None):
        if kind not in {"all", "manual", "case", "code"}:
            raise ValueError("kind must be all, manual, case or code")
        filters = dict(filters or {})
        requested = filters.get("library_ids")
        if requested is not None and not isinstance(requested, list):
            raise ValueError("library_ids filter must be a list")
        libraries = list(self.library_ids) if requested is None else sorted(set(self.library_ids).intersection(requested))
        if "mode" in filters and filters["mode"] != self.mode:
            raise ValueError("Challenge retrieval mode is pinned")
        if "backend" in filters and filters["backend"] not in {self.mode, "native" if self.mode == "native" else "qmd"}:
            raise ValueError("Challenge retrieval backend is pinned")
        limit = max(0, min(int(limit), 40))
        with self._lock:
            self._query_counter += 1
            qid = self._query_counter
        self._record("query", query_id=qid, query=str(query), requested_mode=self.mode, library_ids=libraries, kind=kind,
                     legacy_manuals=requested is None and kind in {"all", "manual"})
        if not str(query).strip() or limit == 0:
            return []
        metadata = {k: filters[k] for k in ("version", "branch", "package", "physics") if k in filters}
        branches = []
        if libraries:
            branches.append(("registered_libraries", self.mode, self._base.search, dict(metadata, mode=self.mode, library_ids=libraries), kind))
        if requested is None and kind in {"all", "manual"}:
            branches.append(("legacy_manuals", "native", self._base.legacy.search, dict(metadata, backend="native"), "manual"))
        ranked = {}
        for branch, mode, run, branch_filters, branch_kind in branches:
            try:
                candidates = run(query, limit=40, kind=branch_kind, filters=branch_filters)
            except Exception as exc:
                self._record("search_error", query_id=qid, branch=branch, requested_backend=mode, error_type=type(exc).__name__)
                raise
            allowed_ids, denied, actuals = [], [], set()
            for rank, candidate in enumerate(candidates, 1):
                cid = candidate.get("id")
                actual = candidate.get("retrieval", {}).get("backend") or candidate.get("retrieval_backend")
                canonical_mode = "native" if actual == "native-html-hierarchy-sqlite-fts5" else actual
                if canonical_mode != mode:
                    self._record("backend_mismatch", query_id=qid, branch=branch, source_id=cid, expected=mode, actual=actual)
                    raise RuntimeError("Challenge source backend does not match the requested engine")
                actuals.add(actual)
                raw = self._base.get_document(cid)
                clean, reason = self._clean_document(raw)
                self._record("candidate_read", query_id=qid, source_id=cid, branch=branch, actual_backend=actual, allowed=bool(clean), reason=reason,
                             redacted=bool(clean and clean["challenge_filter"]["redacted"]))
                if not clean or (branch == "registered_libraries" and clean.get("library_id") not in libraries) or (branch == "legacy_manuals" and str(cid).startswith("lo-")):
                    denied.append({"id": cid, "reason": reason or "branch_scope_mismatch"})
                    continue
                clean["retrieval"] = dict(copy.deepcopy(candidate.get("retrieval", {})), backend=canonical_mode, challenge_branch=branch, policy_id=self._policy_id)
                clean["retrieval_backend"] = actual
                clean["score"] = candidate.get("score", candidate.get("retrieval", {}).get("score", 0))
                allowed_ids.append(cid)
                score = 1 / (60 + rank)
                if cid not in ranked:
                    ranked[cid] = [score, clean]
                else:
                    ranked[cid][0] += score
            with self._lock:
                self._seen_backends.update(actuals)
            self._record("search_branch", query_id=qid, branch=branch, requested_backend=mode, actual_backends=sorted(actuals), candidate_ids=[c.get("id") for c in candidates], allowed_ids=allowed_ids, rejected=denied,
                         candidate_limit=40, candidate_limit_reached=len(candidates) >= 40)
        output = [v[1] for v in sorted(ranked.values(), key=lambda item: item[0], reverse=True)[:limit]]
        with self._lock:
            for item in output:
                self._last_query[item["id"]] = qid
        self._record("query_result", query_id=qid, returned_ids=[item["id"] for item in output])
        return output

    def status(self):
        status = copy.deepcopy(self._base.status())
        status["libraries"] = [item for item in status.get("libraries", []) if item.get("id") in self.library_ids]
        status["challenge"] = {"policy_id": self._policy_id, "mode": self.mode, "library_ids": list(self.library_ids), "active_revision_count": len(self._active_ids),
                               "generation_at_start": self._generation, "legacy_mode": "native", "legacy_kind": "manual", "legacy_counts_are_unfiltered": True,
                               "excluded_family_count": len(self._families), "source_boundary": "pinned_active_libraries_and_filtered_legacy_manuals", "actual_backends_seen": sorted(self._seen_backends),
                               "audit_events": self._event_counter, "audit_dropped_events": self._audit_dropped,
                               "limitations": ["Legacy retrieval counts precede challenge filtering.", "Results are filtered within each backend's bounded candidate window; fewer than requested results may remain.", "Filtered sources omit held-out references and are not complete page reproductions."]}
        return status
