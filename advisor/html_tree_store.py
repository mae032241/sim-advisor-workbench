"""Library-owned native HTML trees with atomic, relocatable publication.

The small registry is the only active-generation authority. Neither startup nor
navigation hashes a corpus; unchanged document fingerprints are reused only by
an explicit build. HTML bodies remain in the registered original source roots.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import threading
import uuid

from .build_control import check_cancelled
from .html_tree import build_html_tree, native_packages
from .index_manifest import _owned

SCHEMA = "html-tree-registry/v1"
_TREE = re.compile(r"tree-[a-f0-9]{32}\Z")
_GENERATION = re.compile(r"(?:gen-|\.building-)[a-f0-9]{32}\Z")
_CACHE = {}
_CACHE_LOCK = threading.RLock()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def tree_id(library_id, root_id):
    return "tree-" + hashlib.sha256(_json([library_id, root_id]).encode("utf-8")).hexdigest()[:32]


class HtmlTreeRepository:
    def __init__(self, store):
        self.store = store
        self.root = Path(store.root).absolute()
        self.directory = _owned(self.root, self.root / "html-trees")
        self.registry_path = _owned(self.root, self.directory / "registry.json")

    def _registry(self):
        _owned(self.root, self.registry_path)
        try:
            info = self.registry_path.stat()
        except FileNotFoundError:
            return {"schema": SCHEMA, "version": None, "trees": {}, "libraries": {}}
        stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        with _CACHE_LOCK:
            cached = _CACHE.get(str(self.registry_path))
            if cached and cached[0] == stamp:
                return deepcopy(cached[1])
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("schema") != SCHEMA
                    or not isinstance(value.get("trees"), dict) or not isinstance(value.get("libraries"), dict)):
                raise ValueError("Invalid HTML tree registry")
            for identity, entry in value["trees"].items():
                if (not _TREE.fullmatch(identity) or not isinstance(entry, dict)
                        or entry.get("tree_id") != identity
                        or identity != tree_id(entry.get("library_id"), entry.get("root_id"))):
                    raise ValueError("Invalid HTML tree ownership identity")
                self._index_path(entry)
            _CACHE[str(self.registry_path)] = (stamp, deepcopy(value))
            return value

    def _index_path(self, entry):
        relative = entry.get("index_path")
        parts = PurePosixPath(relative).parts if isinstance(relative, str) else ()
        if (len(parts) != 5 or parts[0] != "html-trees" or parts[1] != entry.get("tree_id")
                or parts[2] != "generations" or not re.fullmatch(r"gen-[a-f0-9]{32}", parts[3])
                or parts[4] != "index.sqlite3" or "\\" in relative or ":" in relative):
            raise ValueError("Invalid relative HTML tree generation locator")
        return _owned(self.root, self.root.joinpath(*parts))

    def _entry(self, value):
        result = deepcopy(value)
        result["index_path"] = self._index_path(value)
        return result

    def list_trees(self):
        """Fresh small-registry snapshot; authorization remains a live tool gate."""
        return [self._entry(entry) for _, entry in sorted(self._registry()["trees"].items())]

    def resolve(self, identity):
        if not isinstance(identity, str) or not _TREE.fullmatch(identity):
            raise ValueError("Use an exact registered tree_id")
        entry = self._registry()["trees"].get(identity)
        if entry is None:
            raise KeyError("HTML tree is not prepared")
        value = self._entry(entry)
        if not value["index_path"].is_file():
            raise FileNotFoundError("Registered HTML tree index is missing; rebuild this tree")
        return value

    def summary(self):
        registry = self._registry()
        entries = list(registry["trees"].values())
        enabled = {}
        available = []
        for entry in entries:
            lid = entry["library_id"]
            if lid not in enabled:
                try:
                    library = self.store.library(lid, include_deleted=True)
                    enabled[lid] = bool(library.get("enabled") and not library.get("deleted_at"))
                except FileNotFoundError:
                    enabled[lid] = False
            if enabled[lid] and self._index_path(entry).is_file():
                available.append(entry)
        unsupported = sum(item.get("state") == "not_supported" for item in registry["libraries"].values())
        state = "ready" if available else "disabled" if entries else "not_supported" if unsupported else "not_prepared"
        return {"state": state, "ready": bool(available), "registry_version": registry.get("version"),
                "tree_count": len(entries), "registered_tree_count": len(entries),
                "enabled_tree_count": sum(enabled.get(e["library_id"], False) for e in entries),
                "available_tree_count": len(available), "not_supported_library_count": unsupported,
                **{key: sum(e.get(key, 0) for e in entries) for key in ("node_count", "document_count", "package_count")}}

    def _write(self, path, value):
        path = _owned(self.root, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        stage = _owned(self.root, path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp"))
        try:
            with stage.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(_json(value))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(stage, path)
        finally:
            stage.unlink(missing_ok=True)

    @contextmanager
    def _writer(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = _owned(self.root, self.directory / ".registry.lock")
        if lock.exists():
            from .library_store import process_alive
            previous = json.loads(lock.read_text(encoding="utf-8"))
            if process_alive(previous.get("pid")):
                raise RuntimeError("Another HTML tree build or purge is active")
            lock.unlink()
        token = uuid.uuid4().hex
        try:
            with lock.open("x", encoding="utf-8") as handle:
                handle.write(_json({"pid": os.getpid(), "token": token}))
        except FileExistsError:
            raise RuntimeError("Another HTML tree build or purge is active") from None
        try:
            yield
        finally:
            if lock.exists() and json.loads(lock.read_text(encoding="utf-8")).get("token") == token:
                lock.unlink()

    def _remove(self, path):
        path = _owned(self.directory, path)
        if path == self.directory:
            raise ValueError("Refusing to remove the repository root")
        if path.exists():
            shutil.rmtree(path)

    def _fingerprints(self, entry):
        if not entry:
            return {}
        path = self._index_path(entry)
        if not path.is_file():
            return {}
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                return {row[0]: {"sha256": row[1], "bytes": row[2], "stat": json.loads(row[3])}
                        for row in db.execute("SELECT doc_path,sha256,bytes,stat_fingerprint FROM documents")}
        except (sqlite3.Error, ValueError):
            return {}  # Old index versions simply require one explicit rehash.

    def _cleanup(self, identities, registry):
        warnings = []
        for identity in identities:
            directory = _owned(self.root, self.directory / identity / "generations")
            if not directory.is_dir():
                continue
            active = registry["trees"].get(identity, {}).get("generation")
            for path in directory.iterdir():
                if _GENERATION.fullmatch(path.name) and path.name != active:
                    try:
                        self._remove(path)
                    except OSError as exc:
                        warnings.append({"tree_id": identity, "generation": path.name, "reason": type(exc).__name__})
        return warnings

    def build_library(self, library_id, on_progress=None):
        library = self.store._buildable_library(library_id)
        binding = [(r["id"], r["path"]) for r in library["roots"]]
        prepared, directories, root_states = [], [], []
        with self._writer():
            registry = self._registry()
            previous_ids = {identity for identity, entry in registry["trees"].items() if entry["library_id"] == library_id}
            try:
                if library["type"] in {"official-manuals", "user-manuals"}:
                    for root in library["roots"]:
                        check_cancelled()
                        source = Path(root["path"])
                        if not source.is_dir():
                            raise FileNotFoundError("Registered manual source root is unavailable")
                        packages = native_packages(source)
                        if not packages:
                            root_states.append({"root_id": root["id"], "state": "not_supported", "reason": "no_generic_native_toc"})
                            continue
                        identity = tree_id(library_id, root["id"])
                        own = _owned(self.root, self.directory / identity)
                        owner_path = _owned(self.root, own / "owner.json")
                        owner = {"schema": "html-tree-owner/v1", "tree_id": identity,
                                 "library_id": library_id, "root_id": root["id"]}
                        if owner_path.is_file() and json.loads(owner_path.read_text(encoding="utf-8")) != owner:
                            raise ValueError("HTML tree directory has a different owner")
                        self._write(owner_path, owner)
                        generation = "gen-" + uuid.uuid4().hex
                        stage = _owned(self.root, own / "generations" / (".building-" + uuid.uuid4().hex))
                        final = _owned(self.root, own / "generations" / generation)
                        directories.append(stage)

                        def progress(update, root_id=root["id"]):
                            check_cancelled()
                            if on_progress:
                                on_progress(dict(update, library_id=library_id, root_id=root_id))
                            check_cancelled()

                        build_html_tree(source, stage, library_id=library_id, root_id=root["id"],
                                        packages=packages, on_progress=progress,
                                        fingerprints=self._fingerprints(registry["trees"].get(identity)))
                        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
                        check_cancelled()
                        stage.rename(final)
                        directories[-1] = final
                        entry = {"tree_id": identity, "library_id": library_id, "root_id": root["id"],
                                 "generation": generation, "index_path": (final / "index.sqlite3").relative_to(self.root).as_posix(),
                                 "state": "ready", "ready": True,
                                 **{key: manifest[key] for key in ("index_sha256", "node_count", "document_count", "package_count", "hashing")}}
                        prepared.append(entry)
                        root_states.append({"root_id": root["id"], "state": "ready", "tree_id": identity})
                else:
                    root_states.append({"state": "not_supported", "reason": "not_a_manual_library"})
                check_cancelled()
                latest = self.store._buildable_library(library_id)
                if latest["type"] != library["type"] or [(r["id"], r["path"]) for r in latest["roots"]] != binding:
                    raise RuntimeError("Library root bindings changed during HTML tree construction")
                registry["trees"] = {identity: entry for identity, entry in registry["trees"].items() if entry["library_id"] != library_id}
                registry["trees"].update({entry["tree_id"]: entry for entry in prepared})
                state = "ready" if prepared else "not_supported"
                registry["libraries"][library_id] = {"state": state, "roots": root_states}
                registry["version"] = uuid.uuid4().hex
                self._write(self.registry_path, registry)  # The single publication point.
            except BaseException:
                for directory in directories:
                    try:
                        self._remove(directory)
                    except OSError:
                        pass  # Preserve the original cancellation/failure; a later build/purge retries cleanup.
                raise
            cleanup = self._cleanup(previous_ids | {e["tree_id"] for e in prepared}, registry)
        return {"state": state, "library_id": library_id, "trees": prepared, "roots": root_states,
                "cleanup_pending": cleanup, "fallback": "qmd" if not prepared else None}

    def purge_library(self, library_id):
        removed = []
        with self._writer():
            registry = self._registry()
            identities = {identity for identity, entry in registry["trees"].items() if entry["library_id"] == library_id}
            for directory in self.directory.glob("tree-*"):
                owner_path = _owned(self.root, directory / "owner.json")
                if owner_path.is_file():
                    owner = json.loads(owner_path.read_text(encoding="utf-8"))
                    if owner.get("library_id") == library_id:
                        if directory.name != tree_id(library_id, owner.get("root_id")):
                            raise ValueError("HTML tree directory ownership is invalid")
                        identities.add(directory.name)
            for identity in sorted(identities):
                self._remove(self.directory / identity)
                removed.append(identity)
            registry["trees"] = {k: v for k, v in registry["trees"].items() if v["library_id"] != library_id}
            registry["libraries"].pop(library_id, None)
            registry["version"] = uuid.uuid4().hex
            self._write(self.registry_path, registry)
        return {"tree_count": len(removed), "tree_ids": removed}
