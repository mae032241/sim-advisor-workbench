"""Deterministic, source-backed context derived from canonical tutorial sections.

This is an offline constructor, not a query summarizer. It neither opens MPH
files nor calls an embedding service. Ordinary closed archives may be prepared;
public consumers must separately enforce current source visibility. Extracts
remain documentation, not proof of the saved model's settings or validity.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import hashlib
import json
import re

from .build_control import check_cancelled


SCHEMA = "case-context/v1"
MAX_CONTEXT_CHARS = 3600
MAX_EXCERPTS = 14
MAX_CANONICAL_SECTIONS = 512
_IDENTITY = ("id", "object_id", "library_id", "asset_key", "asset_hash", "case_group")
_CONFIGURATION_BRANCHES = ("physics", "materials", "studies", "solvers")
_EXCLUDED_SECTION = re.compile(r"^(?:results?(?:\b|$)|discussion\b|references?\b|bibliography\b|"
                               r"model(?:l)?ing instructions?\b|copyright\b)", re.I)
_ROUTE_SECTION = re.compile(r"^(?:model(?:l)?ing (?:approach|strategy)|model (?:formulation|implementation)|"
                           r"simulation (?:setup|approach)|notes about (?:the )?comsol implementation)\b", re.I)
_BOILERPLATE = re.compile(r"copyright|all rights reserved|licensed under|license agreement|"
                          r"^application library path\s*:|^(?:figure|table|equation)\s+\d+\s*[:.]", re.I)
_ABBREVIATIONS = {"ref", "refs", "fig", "figs", "eq", "eqs", "e.g", "i.e", "etc", "dr", "mr", "mrs", "vs"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def classify_case_metadata(source):
    """Classify only observed saved inventory; missing counts remain unknown.

    Positive configuration evidence survives partial property extraction.
    Negative geometry-only inference additionally requires explicit zero counts
    and an unomitted saved-node inventory. A filename never establishes scope.
    This helper is deliberately usable in a metadata-only corpus scan.
    """
    coverage = source.get("coverage") or {}
    sections = coverage.get("sections") or {}
    raw = coverage.get("raw_xml") or {}
    container = coverage.get("source_container") or {}
    counts = {name: value for name, value in sections.items()
              if name in {"geometry", *_CONFIGURATION_BRANCHES}
              and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
    saved = container.get("status") not in {"preview_only", "missing", "failed"} and raw.get("status") in {
        "bounded_saved_state", "complete", "observed", "saved_state"}
    positive = {name: counts[name] for name in _CONFIGURATION_BRANCHES if counts.get(name, 0) > 0}
    no_omissions = (raw.get("nodes_omitted_by_limit") == 0
                    and not (coverage.get("saved_features") or {}).get("omitted_nodes"))
    if saved and positive:
        classification, reason = "configured", "saved_configuration_branches_observed"
    elif (saved and no_omissions and counts.get("geometry", 0) > 0
          and all(counts.get(name) == 0 for name in _CONFIGURATION_BRANCHES)):
        classification, reason = "geometry_only", "explicit_zero_configuration_counts_in_saved_inventory"
    else:
        classification, reason = "unknown", "saved_inventory_does_not_establish_scope"
    return {"classification": classification, "basis": "saved_configuration", "reason": reason,
            "evidence": {"source_id": source.get("id"), "library_id": source.get("library_id"),
                         "section_counts": counts, "nodes_omitted_by_limit": raw.get("nodes_omitted_by_limit"),
                         "source_container_status": container.get("status")},
            "saved_status": raw.get("status"), "api_status": (coverage.get("deep_api") or {}).get("status", "not_requested"),
            "physical_validation": "not_run", "effective_activation": "not_evaluated"}


def _authority(store, case_id):
    if not isinstance(case_id, str) or not case_id or len(case_id) > 256:
        raise ValueError("case_id must be an exact model_case citation ID")
    store.require_not_heldout(case_id)
    source = store.get(case_id)
    if source is None:
        raise FileNotFoundError("Case overview not found")
    store.require_not_heldout(case_id, source)
    if source.get("id") != case_id or source.get("type") != "model_case":
        raise ValueError("Case context requires an exact immutable model_case citation ID")
    return source


def _lineage(store, authority):
    """Follow only an explicit same-asset saved base, checking policy first."""
    data = authority.get("data") or {}
    identity, group = data.get("base_case_id"), data.get("base_case_group")
    if not identity and not group:
        return [authority]
    if not identity or not group or identity == authority["id"]:
        raise ValueError("Case context requires an exact pinned saved-base identity")
    policy = store.evaluation_policy()
    store.require_not_heldout(identity)
    if group in policy.case_groups:
        raise PermissionError("Held-out saved bases cannot enter case context")
    base = store.get(identity)
    if base is None:
        raise FileNotFoundError("Pinned saved base is unavailable")
    store.require_not_heldout(identity, base)
    fields = ("library_id", "asset_key", "asset_hash")
    if (base.get("id") != identity or base.get("type") != "model_case" or base.get("case_group") != group
            or any(base.get(key) != authority.get(key) for key in fields)):
        raise ValueError("Pinned saved base differs from the immutable case asset")
    return [authority, base]


def _canonical_sources(store, lineage, gaps):
    """Resolve exact document-hash edges; never find tutorials by title/path."""
    identities, owner = [value["id"] for value in lineage], lineage[0]["library_id"]
    placeholders = ",".join("?" for _ in identities)
    with closing(store.connect()) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"tutorial_document_sources", "tutorial_model_documents", "tutorial_associations", "active"} <= tables:
            gaps.append({"code": "canonical_tutorial_catalog_unavailable"})
            return []
        # Scalar ownership and digest reads precede all tutorial body hydration.
        rows = db.execute(f"""WITH docs AS (
          SELECT source_sha256 FROM tutorial_model_documents
            WHERE source_id IN ({placeholders}) AND owner_library_id=?
          UNION SELECT source_sha256 FROM tutorial_associations
            WHERE source_id IN ({placeholders}) AND source_library_id=?
        ) SELECT DISTINCT r.id,r.library_id,r.object_id,d.source_sha256,d.anchor
          FROM docs t CROSS JOIN tutorial_document_sources d ON d.source_sha256=t.source_sha256
          CROSS JOIN revisions r ON r.id=d.citation_id AND r.library_id=d.owner_library_id
          CROSS JOIN active a ON a.library_id=r.library_id AND a.object_id=r.object_id AND a.citation_id=r.id
          ORDER BY d.source_sha256,r.id LIMIT ?""",
            [*identities, owner, *identities, owner, MAX_CANONICAL_SECTIONS + 1]).fetchall()
    if len(rows) > MAX_CANONICAL_SECTIONS:
        gaps.append({"code": "canonical_section_limit_reached", "limit": MAX_CANONICAL_SECTIONS})
        rows = rows[:MAX_CANONICAL_SECTIONS]
    sources, seen = [], set()
    for identity, library, logical, source_sha, anchor in rows:
        check_cancelled()
        policy = store.evaluation_policy()
        if (policy.blocks_identity(identity, logical) or source_sha in policy.source_hashes):
            gaps.append({"code": "canonical_source_excluded"})
            continue
        store.require_not_heldout(identity)
        source = store.get(identity)
        if source is None:
            gaps.append({"code": "canonical_source_missing"})
            continue
        store.require_not_heldout(identity, source)
        canonical = (source.get("data") or {}).get("canonical_tutorial") or {}
        text = source.get("text")
        if (source.get("id") != identity or source.get("library_id") != library
                or source.get("object_id") != logical or canonical.get("source_sha256") != source_sha
                or not isinstance(text, str)):
            gaps.append({"code": "canonical_source_identity_mismatch"})
            continue
        text_sha = _digest(text)
        if canonical.get("text_sha256") and canonical["text_sha256"] != text_sha:
            gaps.append({"code": "canonical_source_text_hash_mismatch"})
            continue
        if identity in seen:
            continue
        seen.add(identity)
        source = dict(source, _source_sha256=source_sha, _source_text_sha256=text_sha, _catalog_anchor=anchor)
        sources.append(source)
    return sorted(sources, key=_source_order)


def _source_order(source):
    data = source.get("data") or {}
    order = data.get("tutorial_order")
    return (source["_source_sha256"], order if isinstance(order, (int, float)) else 100000,
            data.get("part") if isinstance(data.get("part"), int) else 1, source["id"])


def _section_role(source):
    data = source.get("data") or {}
    # Structured parser provenance is essential: a whole-page fallback must not
    # bring a duplicate tutorial body into the model overview.
    if source.get("type") != "case_context" or not isinstance(data.get("tutorial_order"), (int, float)):
        return None
    section = " ".join(str(data.get("section") or "").split()).strip(" :.-").casefold()
    if section in {"introduction", "overview"}:
        return "objective_and_scope"
    if section in {"model definition", "model description", "problem definition"}:
        return "conditions_and_mechanism"
    if _ROUTE_SECTION.search(section):
        return "model_route"
    if _EXCLUDED_SECTION.search(section):
        return None
    return "opening_context"


def _sentence_ends(text):
    for match in re.finditer(r"[.!?。！？][\"'’”\)\]]*(?=\s|$)", text):
        punctuation = text[match.start()]
        if punctuation == ".":
            token = re.search(r"([\w.]+)$", text[:match.start()])
            if token and token[1].casefold() in _ABBREVIATIONS:
                continue
            # A decimal point cannot match the whitespace lookahead above.
            # A single-letter initial also does not terminate a sentence.
            if token and len(token[1]) == 1 and token[1].isalpha():
                continue
        yield match.end()


def _meaningful(text):
    return (not _BOILERPLATE.search(text) and not re.fullmatch(r"[\W\d_]+", text)
            and (len(re.findall(r"\b\w+\b", text)) >= 7 or len(re.findall(r"[\u3400-\u9fff]", text)) >= 15)
            and not (len(text) < 100 and text.isupper()))


def _paragraph_spans(source):
    """Yield exact complete-sentence spans, never an arbitrary word prefix."""
    text = source["text"]
    for match in re.finditer(r"[^\r\n]+", text):
        raw = match[0]
        left, right = len(raw) - len(raw.lstrip()), len(raw.rstrip())
        start, end = match.start() + left, match.start() + right
        paragraph = text[start:end]
        if not paragraph or not _meaningful(paragraph):
            continue
        ends = list(_sentence_ends(paragraph))
        if not ends:
            continue
        # Canonical chunks may split a paragraph at a word. Drop a leading
        # lower-case continuation and a trailing incomplete sentence.
        if paragraph[0].islower():
            start += ends[0]
            while start < end and text[start].isspace():
                start += 1
            paragraph = text[start:end]
            ends = list(_sentence_ends(paragraph))
        if not ends:
            continue
        end = start + ends[-1]
        # Small paragraphs retain their complete relationship. Long paragraphs
        # are split only at sentence boundaries, preserving exact offsets.
        cursor = start
        while cursor < end:
            available = [value for value in _sentence_ends(text[cursor:end]) if value <= 850]
            if not available:
                # A very long sentence may precede useful short sentences.
                # Skip the long sentence whole rather than cutting its words.
                cursor += next(iter(_sentence_ends(text[cursor:end])), end - cursor)
                while cursor < end and text[cursor].isspace():
                    cursor += 1
                continue
            stop = cursor + available[-1]
            if _meaningful(text[cursor:stop]):
                yield cursor, stop
            cursor = stop
            while cursor < end and text[cursor].isspace():
                cursor += 1


def _fit_span(source, start, end, budget):
    if end - start <= budget:
        return start, end
    ends = [value for value in _sentence_ends(source["text"][start:end]) if value <= budget]
    if ends and _meaningful(source["text"][start:start + ends[-1]]):
        return start, start + ends[-1]
    return None


def _reference(source):
    data = source.get("data") or {}
    locators = list(dict.fromkeys(str(item["locator"]) for item in source.get("evidence", []) if item.get("locator")))
    if not locators and source.get("_catalog_anchor"):
        locators = [source["_catalog_anchor"]]
    return {"id": source["id"], "source_id": source["id"], "object_id": source.get("object_id"),
            "library_id": source["library_id"], "source_sha256": source["_source_sha256"],
            "source_text_sha256": source["_source_text_sha256"], "section": data.get("section"),
            "tutorial_order": data.get("tutorial_order"), "locators": locators[:8],
            "locators_scope": "source_section", "locators_truncated": len(locators) > 8,
            "basis": "documented_not_model_validated", "is_read_evidence": False}


def _extract(sources):
    buckets = defaultdict(list)
    known = any(_section_role(source) in {"objective_and_scope", "conditions_and_mechanism"} for source in sources)
    for source in sources:
        role = _section_role(source)
        if role is None or role == "opening_context" and known:
            continue
        # Unfamiliar tutorials may have another opening chapter title. Keep
        # only early narrative chapters, never arbitrary later results.
        if role == "opening_context" and (source.get("data") or {}).get("chapter_order", 100000) > 2:
            continue
        buckets[role].extend((source, start, end) for start, end in _paragraph_spans(source))
    roles = ("objective_and_scope", "conditions_and_mechanism", "model_route", "opening_context")
    quotas = {"objective_and_scope": 1550, "conditions_and_mechanism": 1750, "model_route": 500, "opening_context": 2800}
    used, selected, seen_text = defaultdict(int), [], set()
    total = 0
    while len(selected) < MAX_EXCERPTS:
        progress = False
        for role in roles:
            while buckets[role]:
                source, start, end = buckets[role].pop(0)
                budget = min(quotas[role] - used[role], MAX_CONTEXT_CHARS - total - (2 if selected else 0))
                fitted = _fit_span(source, start, end, budget)
                if fitted is None:
                    continue
                start, end = fitted
                excerpt = source["text"][start:end]
                key = " ".join(excerpt.casefold().split())
                if key in seen_text:
                    continue
                seen_text.add(key)
                ref = _reference(source)
                selected.append({"source_id": source["id"], "library_id": source["library_id"],
                                 "source_sha256": source["_source_sha256"], "section": ref["section"],
                                 "tutorial_order": ref["tutorial_order"], "locators": ref["locators"],
                                 "locators_scope": "source_section", "source_text_start": start, "source_text_end": end,
                                 "text": excerpt, "role": role, "basis": "documented_not_model_validated"})
                total += len(excerpt) + (2 if len(selected) > 1 else 0)
                used[role] += len(excerpt)
                progress = True
                break
            if len(selected) >= MAX_EXCERPTS:
                break
        if not progress:
            break
    selected_ids = {excerpt["source_id"] for excerpt in selected}
    refs = [_reference(source) for source in sources if source["id"] in selected_ids]
    return selected, refs


def build_case_context(store, case_id):
    """Build bounded, query-independent context from existing canonical text.

    No persistent writes, library enabling, model parsing, or retrieval rebuild
    occur here. Returned text is entirely exact source slices with offsets.
    """
    authority = _authority(store, case_id)
    lineage = _lineage(store, authority)
    gaps = []
    sources = _canonical_sources(store, lineage, gaps)
    excerpts, refs = _extract(sources)
    if not sources:
        gaps.append({"code": "canonical_tutorial_not_found"})
    elif not excerpts:
        gaps.append({"code": "meaningful_structured_context_not_found"})
    for source in lineage:
        store.require_not_heldout(source["id"], source)
    for source in sources:
        if source["id"] in {ref["id"] for ref in refs}:
            store.require_not_heldout(source["id"], source)
    result = {"schema": SCHEMA, "state": "ready" if excerpts else "degraded", "case_id": case_id,
              **{key: authority.get(key) for key in _IDENTITY if key != "id"},
              "title": authority.get("title"), "text": "\n\n".join(excerpt["text"] for excerpt in excerpts),
              "excerpts": excerpts, "source_refs": refs, "lineage_ids": [value["id"] for value in lineage],
              "model_completeness": classify_case_metadata(authority), "gaps": gaps,
              "basis": "documented_not_model_validated", "model_settings_agreement": "not_verified",
              "physical_validation": "not_run", "effective_activation": "not_evaluated", "is_read_evidence": False}
    result["content_hash"] = _digest(_json(result))
    return result
