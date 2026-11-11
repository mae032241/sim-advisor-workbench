"""Immutable API observations layered over an existing compact XML base.

An existing single-case SQLite snapshot is augmented only in temporary work.
The permanent gzip sidecar contains new canonical API facts, never a second
copy of saved nodes/properties/selections or their source XML.
"""
from __future__ import annotations

from contextlib import closing
import copy
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile

from .build_control import check_cancelled
from .compact_case import CompactCase, CompactCaseIntegrityError, _hash_file, _json, add_compact_api
from .compact_reclaim import _hash_rows, _quote

FORMAT = "compact-mph-api-overlay/v1"
DATABASE = OVERLAY_FILE = "compactapi.json.gz"
MAX_JSON_BYTES = 64 * 1024 * 1024
_POOLS = ("symbols", "value_pool", "metadata_pool")
_SAVED = ("model", "nodes", "properties", "selections", "documents", "passages", "text_pool")


def pinned_base_authority(db, source):
    """Resolve an explicit historical XML link only within the same asset."""
    authority = source
    if source.get("type") != "model_case" and source.get("case_group"):
        row = db.execute("""SELECT r.payload FROM case_members m JOIN revisions r ON r.id=m.citation_id
          WHERE m.group_id=? AND m.local_id='model-case'""", (source["case_group"],)).fetchone()
        if row:
            authority = json.loads(row[0])
    data = authority.get("data", {})
    identity, group = data.get("base_case_id"), data.get("base_case_group")
    if not identity or not group or identity == source.get("id"):
        return None
    row = db.execute("SELECT payload FROM revisions WHERE id=?", (identity,)).fetchone()
    if not row:
        return None
    base = json.loads(row[0])
    fields = ("library_id", "asset_key", "asset_hash")
    if (base.get("type") != "model_case" or base.get("case_group") != group
            or tuple(base.get(key) for key in fields) != tuple(source.get(key) for key in fields)):
        return None
    return base


def _ids(db, table):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return set()
    return {row[0] for row in db.execute("SELECT id FROM " + table)}


def _base_proof(db, limits=None):
    """Protect every saved row and all pre-existing pool/API row identities."""
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    tables = [*_SAVED, *_POOLS, *(name for name in ("api_runs", "api_nodes", "api_observations") if name in names)]
    limits = limits or {name: db.execute("SELECT COALESCE(max(id),0) FROM " + name).fetchone()[0] for name in tables}
    result = {}
    for name, bound in limits.items():
        check_cancelled()
        result[name] = _hash_rows(db.execute("SELECT * FROM " + _quote(name) + " WHERE id<=? ORDER BY id", (bound,)))
    return result, limits


def export_api_overlay(base_path, augmented_path, output, *, source_override=None):
    """Export a completed temporary API pilot after proving its base unchanged."""
    base_path = Path(base_path).expanduser().resolve(strict=True)
    output = Path(output).expanduser().resolve()
    if output.exists() or output == base_path:
        raise FileExistsError("API overlay requires a new output path")
    base = CompactCase(base_path, source_override=source_override)
    original = base.read_case()
    base_sha = _hash_file(base.db_path)
    with closing(base._connect()) as db:
        proof, limits = _base_proof(db)
        existing_runs = _ids(db, "api_runs")
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        augmented = CompactCase(augmented_path, source_override=source_override)
        with closing(augmented._connect()) as db:
            if _base_proof(db, limits)[0] != proof:
                raise CompactCaseIntegrityError("API augmentation changed the existing saved/base observations")
            mappings = [dict(row) for row in db.execute("SELECT id,node_id,path_id FROM api_nodes ORDER BY id")]
            for row in mappings:
                row["api_path"] = augmented._pool(db, "symbols", row.pop("path_id"))
        current = augmented.read_case()
        runs = [run for run in current["api"]["runs"] if run["id"] not in existing_runs]
        run_ids = {run["id"] for run in runs}
        observations = []
        for node in augmented.iter_nodes():
            check_cancelled()
            for row in node.get("api_observations", []):
                if row["run_id"] not in run_ids:
                    continue
                row = copy.deepcopy(row)
                if row.get("relation") == "corroborates_saved_xml" and row.get("saved_property_id") is not None:
                    row["base_value_property_id"] = row["saved_property_id"]
                    row.pop("value", None)
                observations.append(row)
        payload = {"format": FORMAT, "base_sha256": base_sha, "source_sha256": original["source_sha256"],
                   "runs": runs, "api_nodes": mappings, "observations": observations}
        encoded = _json(payload).encode("utf-8")
        if len(encoded) > MAX_JSON_BYTES:
            raise ValueError("API overlay exceeds its bounded decoded size")
        if _hash_file(base.db_path) != base_sha:
            raise CompactCaseIntegrityError("Compact base changed while API observations were staged")
        check_cancelled()
        with output.open("xb") as stream:
            created = True
            stream.write(gzip.compress(encoded, mtime=0))
        return {"path": str(output), "format": FORMAT, "sha256": _hash_file(output), "base_sha256": base_sha,
                "source_sha256": original["source_sha256"], "bytes": output.stat().st_size,
                "decoded_bytes": len(encoded), "api": current["api"], "new_runs": len(runs),
                "new_observations": len(observations), "base_unchanged": True,
                "saved_rows_unchanged": True}
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        check_cancelled()
        raise


def stage_api_overlay(base_path, output, *, source_override=None, bundle=None, extractor=None, force=False, **extractor_options):
    """Back up one base temporarily, validate injected/default API facts, export.

    ``bundle``, ``extractor`` and ``runner`` remain injectable; no XML parser or
    catalog constructor is invoked. Temporary complete databases are removed.
    """
    base = CompactCase(base_path, source_override=source_override)
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise FileExistsError("API overlay requires a new output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".api-work-", dir=output.parent) as temporary:
        from .compact_case import DATABASE as CASE_DATABASE
        work = Path(temporary)
        with closing(base._connect()) as old, closing(sqlite3.connect(work / CASE_DATABASE)) as staged:
            old.backup(staged, pages=128, progress=lambda *_: check_cancelled())
        result = add_compact_api(work, bundle, extractor=extractor, force=force,
                                 source_override=source_override, **extractor_options)
        exported = export_api_overlay(base.db_path, work, output, source_override=source_override)
        return dict(exported, api_cache_hit=result.get("api_cache_hit", False))


class OverlayCompactCase:
    """Read a pinned XML base plus one immutable, hash-bound API delta."""
    def __init__(self, base, overlay, *, source_override=None, base_sha256=None):
        self.base = base if isinstance(base, CompactCase) else CompactCase(base, source_override=source_override)
        self.db_path, self.root, self.source_override = self.base.db_path, self.base.root, self.base.source_override
        self.overlay_path = Path(overlay).expanduser().resolve(strict=True)
        if self.overlay_path.stat().st_size > MAX_JSON_BYTES:
            raise CompactCaseIntegrityError("API overlay exceeds its bounded size")
        try:
            with gzip.open(self.overlay_path, "rb") as stream:
                raw = stream.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise ValueError("Decoded API overlay exceeds its bounded size")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise CompactCaseIntegrityError("API overlay decoding failed") from exc
        self._base_case = self.base.read_case()
        actual_base_sha = base_sha256 or _hash_file(self.base.db_path)
        if (data.get("format") != FORMAT or data.get("base_sha256") != actual_base_sha
                or data.get("source_sha256") != self._base_case["source_sha256"]):
            raise CompactCaseIntegrityError("API overlay does not belong to this compact base/source")
        self.base_sha256 = actual_base_sha
        self.runs = data.get("runs", [])
        self.api_nodes = data.get("api_nodes", [])
        self._by_id, self._by_node = {}, {}
        with closing(self.base._connect()) as db:
            retained_nodes, old_observations = _ids(db, "nodes"), _ids(db, "api_observations")
            self._base_api_nodes = _ids(db, "api_nodes")
        run_ids = {run["id"] for run in self.runs}
        old_runs = {run["id"] for run in self._base_case["api"].get("runs", [])}
        if len(run_ids) != len(self.runs) or run_ids & old_runs:
            raise CompactCaseIntegrityError("API overlay has duplicate run identities")
        for observation in data.get("observations", []):
            row = copy.deepcopy(observation)
            if (row["id"] in self._by_id or row["id"] in old_observations or row["node_id"] not in retained_nodes
                    or row["run_id"] not in run_ids):
                raise CompactCaseIntegrityError("API overlay observation mapping is invalid")
            if row.get("base_value_property_id") is not None:
                prop = self.base.get_property(row["node_id"], property_id=row.pop("base_value_property_id"))
                if not prop.get("value_complete"):
                    raise CompactCaseIntegrityError("Incomplete saved value cannot supply API corroboration")
                row["value"] = prop["value"]
            self._by_id[row["id"]] = row
            self._by_node.setdefault(row["node_id"], []).append(row)

    def __getattr__(self, name):
        return getattr(self.base, name)

    def read_case(self):
        result = self.base.read_case()
        api = result["api"]
        runs = sorted([*api.get("runs", []), *copy.deepcopy(self.runs)], key=lambda row: row["id"])
        nodes = self._base_api_nodes | {row["id"] for row in self.api_nodes}
        result["api"] = {"status": "recorded" if runs else "not_requested", "runs": runs,
                         "counts": {"runs": len(runs), "nodes": len(nodes),
                                    "observations": api.get("counts", {}).get("observations", 0) + len(self._by_id)}}
        result["api_overlay"] = {"format": FORMAT, "base_sha256": self.base_sha256, "new_observations": len(self._by_id)}
        return result

    def read_node(self, node_id):
        node = self.base.read_node(node_id)
        node["api_observations"] = [*node.get("api_observations", []), *copy.deepcopy(self._by_node.get(node_id, []))]
        node["api_observations"].sort(key=lambda row: (row["run_id"], row["id"]), reverse=True)
        return node

    def iter_nodes(self):
        for node in self.base.iter_nodes():
            node["api_observations"] = [*node.get("api_observations", []), *copy.deepcopy(self._by_node.get(node["id"], []))]
            node["api_observations"].sort(key=lambda row: (row["run_id"], row["id"]), reverse=True)
            yield node

    def iter_overlay_observations(self):
        for identity in sorted(self._by_id):
            yield copy.deepcopy(self._by_id[identity])

    def get_property(self, node_id, name=None, *, ordinal=None, property_id=None, source="xml", run_id=None, getter=None):
        if source != "api":
            return self.base.get_property(node_id, name, ordinal=ordinal, property_id=property_id, source=source, run_id=run_id, getter=getter)
        rows = [row for row in self.read_node(node_id).get("api_observations", [])
                if (name is None or row["name"] == name) and (property_id is None or row["id"] == property_id)
                and (run_id is None or row["run_id"] == run_id) and (getter is None or row["getter"] == getter)]
        if rows and run_id is None:
            rows = [row for row in rows if row["run_id"] == rows[0]["run_id"]]
        if len(rows) != 1:
            raise KeyError("API property not found" if not rows else "API property is ambiguous; specify its getter or observation ID")
        return copy.deepcopy(rows[0])

    def read_api_evidence(self, observation_id):
        if observation_id not in self._by_id:
            return self.base.read_api_evidence(observation_id)
        row = self._by_id[observation_id]
        run = next(run for run in self.runs if run["id"] == row["run_id"])
        return {"status": "recorded", "evidence_kind": "api_getter_observation", "case_ref": row["case_ref"],
                "source_sha256": self._base_case["source_sha256"], "api_path": row["api_path"], "name": row["name"],
                "getter": row["getter"], "run_id": row["run_id"], "run": copy.deepcopy(run),
                "xml_evidence": False, "saved_property_id": row["saved_property_id"]}

    def add_api(self, *args, **kwargs):
        raise ValueError("API overlays are immutable; stage a new overlay version")
