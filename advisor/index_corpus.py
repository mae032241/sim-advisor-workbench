"""Remove owned QMD export files after the persisted keyword index is ready."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .build_control import check_cancelled
from .index_paths import resolve_index_manifest

_FILE = re.compile(r"o-[a-f0-9]{32}\.md\Z")


def _owned(root, path):
    path = Path(path).absolute()
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Generated corpus escapes the search root")
    current = root
    for part in (None, *path.relative_to(root).parts):
        if part is not None:
            current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Generated corpus refuses symlink/junction paths")
    return path


def _source_roots(search_root):
    catalog = search_root.parent / "catalog.sqlite3"
    if not catalog.is_file():
        return []
    data = search_root.parent.parent
    previous = []
    anchor = data / "workspace-location.json"
    if anchor.is_file():
        value = json.loads(anchor.read_text(encoding="utf-8"))
        previous = [Path(p) for p in [value.get("current_root"), *value.get("previous_roots", [])] if p]
    def resolve(value):
        path = Path(value)
        if not path.is_absolute():
            return (data / path).resolve()
        for old in previous:
            if path.is_relative_to(old):
                return (data / path.relative_to(old)).resolve()
        return path.resolve()
    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as db:
        libraries = [json.loads(r[0]) for r in db.execute("SELECT body FROM libraries")]
    return [resolve(r["path"]) for lib in libraries for r in lib.get("roots", [])]


def cleanup_generated_corpus(search_root, manifest, *, on_progress=None):
    """Validate all exports before deletion; no source/parser/QMD update runs.

    The caller owns the catalog/index maintenance lease. A cancelled/failed
    cleanup can be repeated; already removed exports are not needed by queries.
    """
    root = Path(search_root).absolute()
    _owned(root, root)
    locators = {key: manifest[key] for key in ("directory", "libraries", "path_schema", "keyword_storage") if key in manifest}
    resolved = resolve_index_manifest(locators, root)
    ready = {mode: manifest.get("modes", {}).get(mode, {}).get("ready") for mode in ("qmd_keyword", "qmd_vector")}
    if not any(ready.values()):
        return {"status": "not_ready", "removed_files": 0, "removed_bytes": 0}
    directory = _owned(root, Path(resolved["directory"]))
    database = _owned(root, directory / "objects.sqlite3")
    sources = _source_roots(root)
    plan = []
    total = total_bytes = 0
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(objects)")}
        if "qmd_path" not in columns:
            raise ValueError("Corpus cleanup requires the validated compact object lookup")
        for library, item in resolved["libraries"].items():
            check_cancelled()
            corpus = _owned(root, Path(item["corpus"]))
            if corpus != directory / item["key"] / "corpus":
                raise ValueError("Unexpected generated corpus layout")
            if any(corpus.is_relative_to(source) or source.is_relative_to(corpus) for source in sources):
                raise ValueError("Generated corpus overlaps a registered source directory")
            if ready["qmd_keyword"] and not _owned(root, Path(item["db_path"])).is_file():
                raise ValueError("Persisted QMD keyword database is unavailable")
            if ready["qmd_vector"] and (not item.get("vector_db_path")
                                       or not _owned(root, Path(item["vector_db_path"])).is_file()):
                raise ValueError("Persisted QMD vector database is unavailable")
            if not corpus.exists():
                continue
            expected = {Path(r[0]).name for r in db.execute("SELECT qmd_path FROM objects WHERE library_id=?", (library,))}
            count = size = 0
            with os.scandir(corpus) as entries:
                for entry in entries:
                    check_cancelled()
                    info = entry.stat(follow_symlinks=False)
                    if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                            or not _FILE.fullmatch(entry.name) or entry.name not in expected):
                        raise ValueError("Unexpected/unowned generated corpus entry: " + entry.name)
                    count += 1
                    size += info.st_size
            plan.append((corpus, expected))
            total += count
            total_bytes += size
    completed = removed_bytes = 0
    last = 0.0
    def progress(phase):
        nonlocal last
        stamp = time.monotonic()
        if on_progress and (phase != "removing_corpus" or stamp - last >= 1 or completed == total):
            on_progress({"phase": phase, "completed": completed, "total": total, "removed_bytes": removed_bytes})
            last = stamp
    progress("corpus_validated")
    for corpus, expected in plan:
        _owned(root, corpus)
        with os.scandir(corpus) as entries:
            for entry in entries:
                check_cancelled()
                info = entry.stat(follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or not _FILE.fullmatch(entry.name) or entry.name not in expected):
                    raise ValueError("Generated corpus changed during cleanup")
                os.unlink(entry.path)
                completed += 1
                removed_bytes += info.st_size
                progress("removing_corpus")
        corpus.rmdir()
    progress("corpus_removed")
    return {"status": "cleaned", "removed_files": completed, "removed_bytes": removed_bytes,
            "validated_files": total, "validated_bytes": total_bytes, "qmd_update_invoked": False}
