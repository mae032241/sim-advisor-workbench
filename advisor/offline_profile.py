"""Read dependency paths belonging to the selected data directory, without writes.

There is deliberately no LocalAppData/home search: an explicit library/data root
must not borrow another workbench's profile. The standard descendants are
``libraries`` and ``libraries/search``; standalone search roots may carry their
own profile or use their immediate library parent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit
from urllib.request import url2pathname

SCHEMA = "sim-advisor-offline-dependencies/v1"


def load_offline_profile(data_dir: Path, *, include_embedding=False) -> dict:
    directory = Path(data_dir).expanduser().resolve()
    roots = [directory]
    if directory.name in {"search", "qmd", "pageindex", "libraries"}:
        roots.append(directory.parent)
    if directory.name == "search" and directory.parent.name == "libraries":
        roots.append(directory.parent.parent)
    result = {"present": False, "path": None, "data_root": str(directory), "bundle_root": None, "paths": {}, "errors": []}
    candidate = next((root / "dependency-profile.json" for root in roots if (root / "dependency-profile.json").is_file()), None)
    if candidate is None:
        return result
    result.update(present=True, path=str(candidate), data_root=str(candidate.parent))
    try:
        if candidate.stat().st_size > 1024 * 1024:
            raise ValueError("dependency-profile.json exceeds 1 MiB")
        profile = json.loads(candidate.read_text(encoding="utf-8-sig"))
        if not isinstance(profile, dict) or profile.get("schema") != SCHEMA:
            raise ValueError(f"dependency profile schema must be {SCHEMA}")
        root_text = profile.get("bundleRoot")
        if not isinstance(root_text, str) or "\0" in root_text or not Path(root_text).expanduser().is_absolute():
            raise ValueError("dependency profile bundleRoot must be an absolute directory")
        bundle = Path(root_text).expanduser().resolve()
        result["bundle_root"] = str(bundle)
        for collection, external in ((profile.get("paths", {}), False), (profile.get("externalPaths", {}), True)):
            if not isinstance(collection, dict):
                raise ValueError("dependency profile paths/externalPaths must be objects")
            for key, value in collection.items():
                if key == "embedModelPath" and not include_embedding:
                    continue
                if not isinstance(value, str) or not value.strip() or "\0" in value:
                    result["errors"].append(f"Invalid profile path: {key}")
                    continue
                path = Path(value).expanduser()
                if external:
                    if not path.is_absolute():
                        result["errors"].append(f"externalPaths.{key} must be absolute")
                        continue
                    resolved = path.resolve()
                else:
                    resolved = (bundle / path).resolve()
                    if not resolved.is_relative_to(bundle):
                        result["errors"].append(f"paths.{key} escapes bundleRoot; use externalPaths explicitly")
                        continue
                result["paths"][key] = str(resolved)
    except (OSError, ValueError, TypeError) as exc:
        result["errors"].append(str(exc))
    return result


def local_model_path(value: str) -> Path | None:
    """Normalize file: or native paths before the QMD SDK sees them."""
    if value.startswith("file:"):
        parsed = urlsplit(value)
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError("Embedding file URI must refer to a local file")
        return Path(url2pathname(parsed.path)).expanduser().resolve()
    if value.startswith(("hf:", "http://", "https://")):
        return None
    return Path(value).expanduser().resolve()


def retrieval_runtime(data_dir: Path, project_root: Path, *, qmd_dir=None, node=None,
                      embed_model=None, cache_dir=None) -> dict:
    from .retrieval_settings import read as embedding_settings
    settings = embedding_settings(data_dir)
    profile = load_offline_profile(data_dir, include_embedding=settings["enabled"])
    paths, sources = profile["paths"], {}

    def choose(key, argument, env_name, profile_names, default):
        if argument is not None:
            sources[key] = "argument"
            return str(argument)
        configured = os.environ.get(env_name, "").strip()
        if configured:
            sources[key] = f"env:{env_name}"
            return configured
        for name in profile_names:
            if paths.get(name):
                sources[key] = f"profile:{name}"
                return paths[name]
        if profile["present"] and profile_names:
            sources[key] = "profile:missing"
            return None
        sources[key] = "default"
        return default

    qmd = choose("qmd_dir", qmd_dir, "SIM_ADVISOR_QMD_DIR", ("qmdDir",), str(project_root / ".tools/qmd/node_modules/@tobilu/qmd"))
    configured_node = choose("node", node, "SIM_ADVISOR_NODE", ("nodePath", "node"), shutil.which("node"))
    if settings["enabled"]:
        model = choose("embed_model", embed_model if embed_model is not None else settings["model_path"] or None,
                       "SIM_ADVISOR_EMBED_MODEL", ("embedModelPath",), "")
        if embed_model is None and settings["model_path"]:
            sources["embed_model"] = "settings:model_path"
    else:
        model = ""
        sources["embed_model"] = "disabled"
    cache_default = Path(profile["data_root"]) / "cache/library-search" if profile["present"] else project_root / ".tools/library-search-cache"
    cache = choose("cache_dir", cache_dir, "SIM_ADVISOR_SEARCH_CACHE", (), str(cache_default))
    node_path = Path(configured_node).expanduser().resolve() if configured_node else None
    qmd_path = Path(qmd).expanduser().resolve() if qmd else None
    errors = [error for error in profile["errors"] if "embedModelPath" not in error]
    if not node_path or not node_path.is_file():
        errors.append(f"Node executable unavailable: {node_path or 'nodePath missing from associated profile'}")
    if not qmd_path or not (qmd_path / "dist/index.js").is_file():
        errors.append(f"QMD SDK unavailable: {qmd_path or 'qmdDir missing from associated profile'}")
    model_path = None
    embedding_errors = [error for error in profile["errors"] if "embedModelPath" in error]
    if model:
        try:
            model_path = local_model_path(model)
            if model_path is not None:
                model = str(model_path)
        except ValueError as exc:
            embedding_errors.append(str(exc))
    elif settings["enabled"]:
        embedding_errors.append("Embedding model unavailable: select a local GGUF in embedding settings")
    return {"data_dir": str(Path(data_dir).resolve()), "profile": profile, "sources": sources,
            "qmd_dir": str(qmd_path) if qmd_path else None,
            "node": str(node_path) if node_path and node_path.is_file() else None,
            "node_requested": str(node_path) if node_path else None,
            "model_uri": model or "", "model_path": str(model_path) if model_path else None,
            "cache_dir": str(Path(cache).expanduser().resolve()), "errors": errors,
            "embedding_enabled": settings["enabled"], "embedding_errors": embedding_errors}
