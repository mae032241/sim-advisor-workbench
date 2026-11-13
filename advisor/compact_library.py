"""Thin library/search records backed by one immutable compact case database.

The catalog contains retrieval text and canonical row references. Complete
typed values are hydrated only for explicit source/configuration reads.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
import hashlib
import json
import re
from pathlib import Path
import threading
import tempfile
from urllib.parse import urlsplit
from urllib.request import url2pathname
import xml.etree.ElementTree as ET

from .build_control import check_cancelled
from . import compact_case, compact_model
from .compact_case import CompactCase, CompactCaseIntegrityError, DATABASE, FORMAT, add_compact_api, build_compact_case
from .library_model import _tutorial, _tutorial_reference
from .model_reader import _safe_container

ADAPTER_VERSION = "compact-library/v1"
MAX_RETRIEVAL_CHARS = 720
_MESH_PRIORITY = {name: index for index, name in enumerate(("hmax", "hauto", "hmin", "custom", "hmaxactive", "hminactive", "hgrad", "hcurve", "hnarrow"))}
_BRANCH_WORDS = {"geometry": "geometry 几何", "mesh": "mesh 网格", "physics": "physics 物理场", "material": "material 材料",
                 "multiphysics": "multiphysics 多物理场", "selection": "selection 选择", "study": "study 研究",
                 "solver": "solver 求解器", "result": "result 结果", "param": "parameter 参数", "func": "function 函数", "variable": "variable 变量"}
_HASHES, _HASH_LOCK = OrderedDict(), threading.Lock()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            check_cancelled()
            digest.update(block)
    return digest.hexdigest()


def _cached_hash(path):
    stat = path.stat()
    key = (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    with _HASH_LOCK:
        value = _HASHES.get(key)
        if value is None:
            value = _hash(path)
            _HASHES[key] = value
        _HASHES.move_to_end(key)
        while len(_HASHES) > 128:
            _HASHES.popitem(last=False)
    return value


def _document_references(source, docs_root):
    metadata, _ = _safe_container(source)
    info = {}
    if metadata.get("modelinfo.xml"):
        try:
            info = dict(ET.fromstring(metadata["modelinfo.xml"]).attrib)
        except ET.ParseError:
            pass
    html, tutorial, _, _ = _tutorial(source, docs_root, info, metadata_only=True)
    return [_tutorial_reference(html, tutorial)] if html is not None else []


def _version(source_hash, documents, *, deep):
    value = {"adapter": ADAPTER_VERSION, "adapter_sha256": _hash(Path(__file__)),
             "parser_signature": compact_model.parser_signature(), "storage_sha256": _hash(Path(compact_case.__file__)),
             "source_sha256": source_hash, "document_hashes": documents, "mode": "api" if deep else "offline"}
    if deep:
        from .compact_api import current_runtime_policy
        value["runtime_policy"] = current_runtime_policy()
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _coverage(case, *, deep_requested=False):
    """Keep the existing job completeness fields without copying them to children."""
    value = dict(case["coverage"])
    raw = dict(value.get("raw_xml", {}))
    statuses = raw.get("property_statuses", {})
    raw.update(node_count=raw.get("nodes_retained", 0), normalization={
        "properties_total": raw.get("properties_retained", 0), "properties_unresolved": statuses.get("unresolved", 0),
        "properties_partial": statuses.get("partial", 0), "selection_statuses": raw.get("selection_statuses", {})})
    value.update(raw_xml=raw, extraction_level="compact_saved_xml_inventory", deep_api={"status": "not_requested", "node_count": 0})
    if deep_requested and value.get("source_container", {}).get("status") == "preview_only":
        value["deep_api"] = {"status": "preview_only", "node_count": 0}
    api = case.get("api", {})
    if api.get("runs"):
        last = api["runs"][-1]
        value["deep_api"] = {"status": last.get("status", "partial"), "read_contract": last.get("read_contract"),
                             "node_count": api.get("counts", {}).get("nodes", 0), "read_errors": last.get("errors", {}).get("all_getter_read_errors", 0)}
    return value


def _object(local_id, type_, title, text, data, evidence, *, coverage=None):
    result = {"local_id": local_id, "type": type_, "title": title, "text": text,
              "data": data, "evidence": evidence, "kind": "case", "relations": []}
    if coverage is not None:
        result["coverage"] = coverage
    return result


def _evidence(source, locator, method="compact_mph_row_reference", status="saved_observed"):
    return {"asset_path": str(source), "locator": locator, "method": method, "status": status}


def _ranges(ordinals):
    values = sorted(set(ordinals))
    result, start, previous = [], None, None
    for value in values:
        if previous is not None and value != previous + 1:
            result.append(str(start) if start == previous else f"{start}-{previous}")
            start = None
        if start is None:
            start = value
        previous = value
    if start is not None:
        result.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(result)


def _selection_terms(value, named=False):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _selection_terms(child, named or key in {"named_references", "output_references", "geometry_ref", "feature_ref", "object"})
    elif isinstance(value, list):
        for child in value:
            yield from _selection_terms(child, named)
    elif named and isinstance(value, str):
        yield value


def _value_hint(item):
    if item.get("value_omitted"):
        return " [source-only descriptor]"
    if item.get("status") not in {"decoded", "observed", "decoded_saved"}:
        return " [" + str(item.get("status") or "unresolved") + "]"
    value = item.get("value")
    if isinstance(value, dict) and value.get("source_only"):
        return " [source-only descriptor]"
    text = _json(value)
    if len(text) > 96:
        text = text[:48] + " … " + text[-45:]
    return " hint=" + text


def _node_objects(reader, source):
    objects = []
    for node in reader.iter_nodes():
        check_cancelled()
        branch = node.get("feature_branch") or "misc"
        prefix = "Saved " + _BRANCH_WORDS.get(branch, branch) + "; effectiveness unverified. "
        entries = []
        properties = node.get("properties", [])
        if branch == "mesh":
            properties = sorted(properties, key=lambda item: _MESH_PRIORITY.get(item["name"], len(_MESH_PRIORITY)))
        for prop in properties:
            entries.append(("properties", prop["ordinal"], str(prop.get("name") or "[unnamed]") + _value_hint(prop)))
        for selection in node.get("selections", []):
            terms = list(dict.fromkeys(_selection_terms(selection.get("value", {}))))
            entries.append(("selections", selection["ordinal"], "selection " + " ".join(terms)))
        property_names = {item.get("name") for item in properties}
        for observation in node.get("api_observations", []):
            if observation.get("name") not in property_names:
                entries.append(("api", observation["id"], "API " + observation["name"] + _value_hint(observation)))
        batches, batch, size = [], [], len(prefix)
        for kind, ordinal, text in entries:
            # A hostile long identifier/reference is split, retaining every
            # character and its exact row reference without a huge object.
            pieces = [text[index:index + 500] for index in range(0, len(text), 500)] or [""]
            for piece in pieces:
                if batch and size + len(piece) + 2 > MAX_RETRIEVAL_CHARS:
                    batches.append(batch)
                    batch, size = [], len(prefix)
                batch.append((kind, ordinal, piece))
                size += len(piece) + 2
        if batch or not batches:
            batches.append(batch)
        title = "Saved " + branch + " · " + str(node.get("type") or node["kind"])[:64] + " · " + str(node.get("label") or node.get("tag") or "")[:64]
        for part, items in enumerate(batches, 1):
            ref = {"node_id": node["id"]}
            if len(batches) > 1:
                ref.update(property_ordinals=sorted({item[1] for item in items if item[0] == "properties"}),
                           selection_ordinals=sorted({item[1] for item in items if item[0] == "selections"}),
                           api_observation_ids=sorted({item[1] for item in items if item[0] == "api"}))
            locator = f"compact:node/{node['id']}"
            if len(batches) > 1:
                locator += ";properties=" + _ranges(ref["property_ordinals"]) + ";selections=" + _ranges(ref["selection_ordinals"])
            text = prefix + "; ".join(item[2] for item in items)
            objects.append(_object(f"compact-node-{node['id']}-{part}", "saved_configuration", title + (f" · {part}" if len(batches) > 1 else ""), text,
                                   {"compact_ref": ref, "feature_branch": branch, "feature_type": node.get("type") or node["kind"]},
                                   [_evidence(source, locator)]))
    return objects


def _passage_objects(reader, document_paths):
    result = []
    documents = {}
    for passage in reader.iter_passages():
        if passage["document_id"] not in documents:
            documents[passage["document_id"]] = reader.read_document(passage["document_id"])
        document = documents[passage["document_id"]]
        source = document_paths.get(document["source_sha256"])
        if source is None:
            raise CompactCaseIntegrityError("Associated tutorial no longer matches the current document binding")
        text = passage.get("text", "")
        for part, start in enumerate(range(0, max(1, len(text)), MAX_RETRIEVAL_CHARS), 1):
            ref = {"passage_id": passage["id"], "text_start": start, "text_end": min(start + MAX_RETRIEVAL_CHARS, len(text))}
            data = {"compact_ref": ref}
            if passage.get("feature_branch"):
                data["feature_branch"] = passage["feature_branch"]
            result.append(_object(f"compact-passage-{passage['id']}-{part}", passage["kind"], passage["title"], text[start:start + MAX_RETRIEVAL_CHARS], data,
                                  [_evidence(source, (passage.get("locators") or ["document"])[0], "compact_tutorial_reference", "documented")]))
    return result


def build_case(path, *, docs_root=None, artifact_dir, library_root, deep=False):
    """Build/reuse one versioned database and return thin legacy-shaped objects."""
    if type(deep) is not bool:
        raise ValueError("deep must be a boolean")
    source = Path(path).expanduser().resolve(strict=True)
    root, artifact = Path(library_root).resolve(), Path(artifact_dir).resolve()
    if not artifact.is_relative_to(root / "artifacts"):
        raise ValueError("Compact case must remain under the owned library artifacts directory")
    fingerprint = _hash(source)
    tutorial_refs = _document_references(source, docs_root)
    documents = {item["source_sha256"]: _file_uri(item["source_uri"]) for item in tutorial_refs}
    version = _version(fingerprint, sorted(documents), deep=deep)
    # The full signature is verified inside SQLite; a shorter directory token
    # keeps ordinary Windows workspaces below the legacy MAX_PATH boundary.
    output = artifact / "compact" / version[:24]
    database = output / DATABASE
    cache_hit = database.is_file()
    if cache_hit:
        case = CompactCase(output, source_override=source).read_case()
        if case["source_sha256"] != fingerprint or case["parser_signature"] != version or case["native_index"]:
            raise CompactCaseIntegrityError("Immutable compact version does not match its source/signature")
    else:
        def parser(original, docs_root=None):
            parsed = compact_model.parse_compact_model(original, docs_root=docs_root)
            parsed["case"] = dict(parsed["case"], source_parser_signature=parsed["case"]["parser_signature"], parser_signature=version)
            return parsed
        build_compact_case(source, output, docs_root=docs_root, parser=parser, signature=version, native_index=False)
    if deep:
        current = CompactCase(output, source_override=source).read_case()
        if current["coverage"].get("source_container", {}).get("status") != "preview_only" and current.get("api", {}).get("counts", {}).get("runs", 0) == 0:
            add_compact_api(output, source_override=source)
    reader = CompactCase(output, source_override=source)
    case = reader.read_case()
    coverage = _coverage(case, deep_requested=deep)
    preview = coverage.get("source_container", {}).get("status") == "preview_only"
    summary = ("PREVIEW ONLY: no saved model settings; tutorial content is a separate reference." if preview else
               "Saved simulation definitions; effective activation, selection membership and physical validity are not verified.")
    authority = _object("model-case", "model_case", case.get("title") or source.stem, summary,
                        {"compact_case": {"path": database.relative_to(root).as_posix(), "sha256": _hash(database), "format": FORMAT},
                         "tutorial_refs": tutorial_refs,
                         "extractor_version": ADAPTER_VERSION},
                        [_evidence(source, "compact:case", "compact_mph_case_reference")], coverage=coverage)
    # Historical compact databases can contain document passages. They remain
    # readable by explicit old citations, but a current model index contains
    # only its saved MPH configuration and external tutorial relation metadata.
    objects = [authority, *_node_objects(reader, source)]
    if _hash(source) != fingerprint:
        raise ValueError("Original MPH changed during compact library construction")
    if any(_hash(path) != digest for digest, path in documents.items()):
        raise ValueError("Associated tutorial changed during compact library construction")
    return {"title": authority["title"], "version": case.get("version"), "objects": objects, "assets": [],
            "coverage": coverage, "warnings": ["Preview contains no saved model settings"] if preview else [],
            "extractor_version": ADAPTER_VERSION, "model_format": "compact", "cache_hit": cache_hit}


extract_compact_case = build_case


def _reader(authority, library_root, source_override):
    reference = authority.get("data", {}).get("compact_case")
    if not isinstance(reference, dict) or reference.get("format") != FORMAT:
        raise CompactCaseIntegrityError("Model case has no supported compact database reference")
    root = Path(library_root).resolve()
    relative = Path(reference.get("path", ""))
    database = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not database.is_relative_to(root / "artifacts") or database.name != DATABASE:
        raise CompactCaseIntegrityError("Compact database reference escapes owned artifacts")
    if not database.is_file():
        raise CompactCaseIntegrityError("Compact database hash verification failed")
    current_hash = _cached_hash(database)
    if current_hash != reference.get("sha256"):
        from .compact_reclaim import storage_binding_matches
        if not storage_binding_matches(root / "catalog.sqlite3", database.relative_to(root).as_posix(),
                                       reference.get("sha256"), current_hash):
            raise CompactCaseIntegrityError("Compact database hash verification failed")
    base = CompactCase(database, source_override=source_override)
    overlay = authority.get("data", {}).get("compact_api_overlay")
    if overlay is not None:
        from .compact_api_overlay import FORMAT as OVERLAY_FORMAT, OVERLAY_FILE, OverlayCompactCase
        if not isinstance(overlay, dict) or overlay.get("format") != OVERLAY_FORMAT:
            raise CompactCaseIntegrityError("Unsupported compact API overlay reference")
        overlay_relative = Path(overlay.get("path", ""))
        overlay_path = (root / overlay_relative).resolve()
        if (overlay_relative.is_absolute() or ".." in overlay_relative.parts or not overlay_path.is_relative_to(root / "artifacts")
                or overlay_path.name != OVERLAY_FILE or not overlay_path.is_file()
                or _cached_hash(overlay_path) != overlay.get("sha256") or overlay.get("base_sha256") != current_hash):
            raise CompactCaseIntegrityError("Compact API overlay failed owned-path/hash/base verification")
        return OverlayCompactCase(base, overlay_path, base_sha256=current_hash)
    return base


def _api_objects(reader, source):
    """New API facts have distinct logical IDs and never repeat saved XML rows."""
    grouped = {}
    for observation in reader.iter_overlay_observations():
        grouped.setdefault(observation["node_id"], []).append(observation)
    objects = []
    for node_id, observations in grouped.items():
        node = reader.read_node(node_id)
        branch = node.get("feature_branch") or "misc"
        title = "API " + branch + " · " + str(node.get("label") or node.get("tag") or node_id)[:128]
        prefix = "Observed API getter values; effectiveness unverified. "
        batch, size, parts = [], len(prefix), []
        for observation in observations:
            hint = observation["name"] + _value_hint(observation) + " [" + str(observation.get("relation") or "api_observed") + "]"
            if batch and size + len(hint) + 2 > MAX_RETRIEVAL_CHARS:
                parts.append(batch)
                batch, size = [], len(prefix)
            batch.append((observation["id"], hint[:600]))
            size += min(600, len(hint)) + 2
        if batch:
            parts.append(batch)
        for part, entries in enumerate(parts, 1):
            ids = [identity for identity, _ in entries]
            objects.append(_object(f"deep-api-node-{node_id}-{part}", "api_configuration", title,
                prefix + "; ".join(text for _, text in entries),
                {"compact_ref": {"node_id": node_id, "api_only": True, "property_ordinals": [],
                                 "selection_ordinals": [], "api_observation_ids": ids},
                 "feature_branch": branch, "source": "comsol_api_getter_observation"},
                [_evidence(source, f"compact:api-node/{node_id};observations=" + _ranges(ids), "compact_api_getter_reference", "observed")]))
    return objects


def enrich_case(authority, *, library_root, artifact_dir, source_override=None, bundle=None, extractor=None,
                augmented_path=None, force=False, on_progress=None, **extractor_options):
    """Reuse an exact compact base and return only a new authority/API delta.

    Publication, lifecycle flags and the old saved-configuration objects are
    outside this function. Completed default reads reuse their pinned overlay.
    """
    from .compact_api_overlay import FORMAT as OVERLAY_FORMAT, OVERLAY_FILE, OverlayCompactCase, stage_api_overlay, export_api_overlay
    root, artifact = Path(library_root).resolve(), Path(artifact_dir).resolve()
    if not artifact.is_relative_to(root / "artifacts"):
        raise ValueError("API enrichment artifacts must remain under owned library artifacts")
    existing = _reader(authority, root, source_override)
    base = existing.base if isinstance(existing, OverlayCompactCase) else existing
    case = base.read_case()
    if case["coverage"].get("source_container", {}).get("status") == "preview_only":
        return {"objects": [], "assets": [], "coverage": _coverage(case, deep_requested=True),
                "api_status": "preview_only", "cache_hit": False, "warnings": ["Preview contains no saved model settings"]}
    source = Path(source_override or case["source_path"]).resolve()
    base_sha = _cached_hash(base.db_path)
    artifact.mkdir(parents=True, exist_ok=True)
    cache = artifact / "api-overlay-manifest.json"
    if augmented_path is not None and (bundle is not None or extractor is not None or extractor_options):
        raise ValueError("A completed augmented snapshot cannot be combined with a new API extractor/bundle")
    default_read = augmented_path is None and bundle is None and extractor is None and extractor_options.get("runner") is None
    policy = signature = None
    if default_read:
        from .compact_api import current_runtime_policy, parser_signature
        policy, signature = current_runtime_policy(), parser_signature()
    def progress(phase, **details):
        check_cancelled()
        if on_progress:
            on_progress({"stage": "deep_api", "phase": phase, **details})
        check_cancelled()
    reference, cache_hit = None, False
    if default_read and not force and cache.is_file() and source.is_file() and _hash(source) == case["source_sha256"]:
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            candidate = saved["overlay"]
            path = (root / candidate["path"]).resolve()
            if (saved["base_sha256"] == base_sha and saved["selector_signature"] == signature and saved["runtime_policy"] == policy
                    and saved.get("api_status") in {"observed", "partial"} and path.is_relative_to(artifact)
                    and path.is_file() and _cached_hash(path) == candidate["sha256"]):
                reference, cache_hit = candidate, True
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if reference is None:
        progress("reading_existing_compact_base", source_sha256=case["source_sha256"])
        with tempfile.TemporaryDirectory(prefix=".api-overlay-", dir=artifact) as temporary:
            staged_path = Path(temporary) / OVERLAY_FILE
            progress("reading_comsol_api")
            staged = (export_api_overlay(base.db_path, augmented_path, staged_path, source_override=source) if augmented_path is not None else
                      stage_api_overlay(base.db_path, staged_path, source_override=source, bundle=bundle,
                                        extractor=extractor, force=force, **extractor_options))
            final = artifact / "api" / staged["sha256"][:24] / OVERLAY_FILE
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                if _cached_hash(final) != staged["sha256"]:
                    raise CompactCaseIntegrityError("Immutable API overlay version already has another payload")
                staged_path.unlink()
            else:
                staged_path.replace(final)
        reference = {"format": OVERLAY_FORMAT, "path": final.relative_to(root).as_posix(),
                     "sha256": staged["sha256"], "base_sha256": base_sha}
    reader = OverlayCompactCase(base, root / reference["path"], base_sha256=base_sha)
    enhanced = reader.read_case()
    coverage = _coverage(enhanced, deep_requested=True)
    runs = enhanced["api"].get("runs", [])
    api_status = (runs[-1].get("status") or "observed") if runs else "unavailable"
    if default_read and api_status in {"observed", "partial"}:
        saved = {"base_sha256": base_sha, "source_sha256": case["source_sha256"], "selector_signature": signature,
                 "runtime_policy": policy, "api_status": api_status, "overlay": reference}
        temporary_cache = cache.with_name(cache.name + ".tmp")
        temporary_cache.write_text(_json(saved), encoding="utf-8")
        temporary_cache.replace(cache)
    old_data = authority.get("data", {})
    base_case_id = old_data.get("base_case_id") or authority.get("id")
    base_case_group = old_data.get("base_case_group") or authority.get("case_group")
    data = {**old_data, "compact_api_overlay": reference, "base_case_id": base_case_id, "base_case_group": base_case_group,
            "deep_enrichment": {"schema": "deep-api-overlay/v1", "base_case_id": base_case_id,
                                "base_case_group": base_case_group, "api_status": api_status}, "extractor_version": ADAPTER_VERSION}
    model = _object("model-case", "model_case", authority.get("title") or case.get("title") or source.stem,
                    "Saved XML definitions with separate API getter observations; effective activation and physical validity are unverified.",
                    data, [_evidence(source, "compact:case+api-overlay", "compact_mph_case_reference")], coverage=coverage)
    objects = [model, *_api_objects(reader, source)]
    progress("api_overlay_ready", api_status=api_status, api_objects=len(objects)-1, cache_hit=cache_hit)
    return {"title": model["title"], "version": case.get("version"), "objects": objects, "assets": [],
            "coverage": coverage, "api_status": api_status, "cache_hit": cache_hit, "model_format": "compact",
            "extractor_version": ADAPTER_VERSION, "overlay": reference, "warnings": []}


def _file_uri(uri):
    value = urlsplit(uri)
    if value.scheme != "file" or value.netloc not in {"", "localhost"}:
        raise CompactCaseIntegrityError("Compact document requires a local file URI")
    return Path(url2pathname(value.path)).resolve()


def hydrate_source(authority, obj, *, library_root, source_override=None):
    """Hydrate an explicit read, preserving the pinned catalog citation fields."""
    reader = _reader(authority, library_root, source_override)
    result = dict(obj, data=dict(obj.get("data", {})))
    ref = result["data"].get("compact_ref", {})
    if obj.get("type") == "model_case":
        case = reader.read_case()
        result["coverage"] = _coverage(case)
        result["data"]["model_snapshot"] = {key: case[key] for key in ("source_path", "source_sha256", "version", "counts", "source_available") if key in case}
        result["data"]["parser_contract"] = case["parser_contract"]
        result["read_scope"] = "complete_compact_case_metadata"
    elif "node_id" in ref:
        node = reader.read_node(ref["node_id"])
        details = {key: value for key, value in node.items() if key not in {"properties", "selections", "api_observations"}}
        result["data"]["node"] = details
        chunked = "property_ordinals" in ref or "selection_ordinals" in ref
        result["data"]["saved_properties"] = [item for item in node["properties"] if not chunked or item["ordinal"] in ref.get("property_ordinals", [])]
        result["data"]["saved_selections"] = [item for item in node["selections"] if not chunked or item["ordinal"] in ref.get("selection_ordinals", [])]
        api_values = [item for item in node.get("api_observations", []) if not chunked or item["id"] in ref.get("api_observation_ids", [])]
        if api_values:
            result["data"]["api_observations"] = api_values
        result["read_scope"] = ("complete_compact_api_feature_part" if ref.get("api_only") else
                                "complete_compact_feature_part" if chunked else "complete_compact_feature")
    elif "passage_id" in ref:
        passage = reader.read_passage(ref["passage_id"])
        result["text"] = passage["text"]
        result["data"]["locators"] = passage.get("locators", [])
        document = reader.read_document(passage["document_id"])
        result["data"]["document"] = {key: value for key, value in document.items() if key != "passages"}
        result["read_scope"] = "bounded_compact_passage" if passage.get("truncated") else "complete_compact_passage"
        result["text_truncated"] = bool(passage.get("truncated"))
    else:
        raise CompactCaseIntegrityError("Compact child has no canonical row reference")
    result.setdefault("text_truncated", False)
    return result


def configuration(authority, *, library_root, source_override=None, category=None, node_path=None, limit=20, source="xml"):
    if source not in {"xml", "api"}:
        raise ValueError("source must be xml or api")
    reader = _reader(authority, library_root, source_override)
    case = reader.read_case()
    if source == "api" and not case.get("api", {}).get("counts", {}).get("runs"):
        raise ValueError("This compact case has no API observations; use source=xml for saved settings")
    bound, nodes, total = max(1, min(100, int(limit))), [], 0
    # A token-delimited node locator is exact. Do not hydrate every unrelated
    # node and all its properties just to select one primary key.
    exact = re.fullmatch(r"compact:node/([0-9]+) ", node_path) if isinstance(node_path, str) else None
    if exact:
        try:
            candidates = (reader.read_node(int(exact[1])),)
        except KeyError:
            candidates = ()
    else:
        candidates = reader.iter_nodes()
    for node in candidates:
        if category and category not in {node["kind"], node.get("feature_branch")}:
            continue
        locator = f"compact:node/{node['id']} " + " ".join(str(node.get(key) or "") for key in ("tag", "kind", "label", "type"))
        if node_path and str(node_path) not in locator:
            continue
        if source == "api" and not node.get("api_observations"):
            continue
        total += 1
        if len(nodes) < bound:
            if source == "api":
                node = {key: value for key, value in node.items() if key not in {"properties", "selections"}}
            nodes.append(dict(node, path=f"compact:node/{node['id']}", node_kind=node["kind"], category=node.get("feature_branch")))
    return {"id": authority.get("id"), "revision_id": authority.get("revision_id"), "source": source,
            "evidence": (authority.get("evidence") or [None])[0], "total_matches": total, "returned": len(nodes), "truncated": total > bound,
            "nodes": nodes, "extraction_coverage": case.get("api") if source == "api" else case["coverage"].get("raw_xml", {}),
            "physical_validation": "not_run", "effective_activation": "not_evaluated"}


def read_evidence(authority, obj, *, library_root, source_override=None, index=0, line=None, limit=120, evidence_path=None):
    reader = _reader(authority, library_root, source_override)
    if int(index) != 0:
        raise ValueError("Unknown evidence index")
    ref = obj.get("data", {}).get("compact_ref", {})
    if ref.get("api_only"):
        observations = [reader.read_api_evidence(identity) for identity in ref.get("api_observation_ids", [])]
        text = json.dumps(observations, ensure_ascii=False, sort_keys=True, indent=2)
        return {"id": obj.get("id"), "revision_id": obj.get("revision_id"), "status": "available",
                "evidence": (obj.get("evidence") or [None])[0], "evidence_kind": "api_getter_observation",
                "xml_evidence": False, "observations": observations, "text": text[:50000], "truncated": len(text) > 50000,
                "read_scope": "recorded_hash_bound_api_observation_metadata", "hash_bound_overlay": True}
    if "passage_id" in ref:
        passage = reader.read_passage(ref["passage_id"])
        document = reader.read_document(passage["document_id"])
        path = Path(evidence_path).resolve() if evidence_path is not None else _file_uri(document["source_uri"])
        if not path.is_file():
            return {"id": obj.get("id"), "status": "unavailable", "reason": "original_document_missing"}
        if _hash(path) != document["source_sha256"] or path.stat().st_size > 8 * 1024 * 1024:
            raise CompactCaseIntegrityError("Original tutorial changed or exceeds the bounded text limit")
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        evidence = reader.read_raw_evidence(ref.get("node_id"))
        if evidence["status"] != "available":
            return dict(evidence, id=obj.get("id"), revision_id=obj.get("revision_id"))
        text = evidence["xml"]
    lines = text.splitlines()
    first, count = max(1, int(line or 1)), max(1, min(300, int(limit)))
    selected = "\n".join(lines[first - 1:first - 1 + count])
    return {"id": obj.get("id"), "revision_id": obj.get("revision_id"), "status": "available",
            "evidence": (obj.get("evidence") or [None])[0], "line_start": first, "line_end": first - 1 + min(count, max(0, len(lines) - first + 1)),
            "total_lines": len(lines), "text": selected[:50000], "truncated": first - 1 + count < len(lines) or len(selected) > 50000,
            "read_scope": "bounded_original_source_lines", "source_hash_verified": True}
