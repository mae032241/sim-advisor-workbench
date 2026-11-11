"""Explicit, data-root-bound opt-in for local QMD embedding capability."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import uuid

SCHEMA = "retrieval-settings/v1"
DISABLED = "QMD embedding is disabled. Enable it explicitly in retrieval settings and select a local GGUF."


def settings_root(data_dir):
    path = Path(data_dir).expanduser().resolve()
    if path.name == "search":
        path = path.parent
    if path.name == "libraries":
        path = path.parent
    return path


def read(data_dir):
    """Read configuration only; never stat, resolve, or open its model path."""
    result = {"enabled": False, "configured": False, "model_path": "", "detail": DISABLED}
    path = settings_root(data_dir) / "retrieval-settings.json"
    try:
        if not path.is_file():
            return result
        if path.stat().st_size > 64 * 1024:
            raise ValueError("Retrieval settings exceeds 64 KiB")
        value = json.loads(path.read_text(encoding="utf-8"))
        embedding = value["embedding"]
        if value.get("schema") != SCHEMA or type(embedding.get("enabled")) is not bool or not isinstance(embedding.get("model_path", ""), str):
            raise ValueError("Invalid retrieval settings")
        model = embedding.get("model_path", "")
        identity = embedding.get("identity", {})
        if embedding["enabled"] and (not Path(model).is_absolute() or Path(model).suffix.lower() != ".gguf"
                or not isinstance(identity, dict) or identity.get("path") != model
                or type(identity.get("size")) is not int or identity["size"] < 4
                or type(identity.get("mtime_ns")) is not int
                or not re.fullmatch(r"[a-f0-9]{64}", str(identity.get("sha256", "")))):
            raise ValueError("Enabled embedding settings require a validated local GGUF identity")
        result.update(enabled=embedding["enabled"], model_path=model, configured=bool(model),
                      detail="Local QMD embedding explicitly enabled; no automatic downloads." if embedding["enabled"] else DISABLED)
        if isinstance(embedding.get("identity"), dict):
            result["identity"] = embedding["identity"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result["error"] = str(exc)
    return result


def require_enabled(data_dir):
    value = read(data_dir)
    if not value["enabled"]:
        raise ValueError(DISABLED)
    return value


def update(data_dir, *, enabled, model_path=None):
    """The caller owns build locks. Enabling validates an existing local file."""
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    if model_path is not None and (not isinstance(model_path, str) or len(model_path) > 4096 or "\0" in model_path):
        raise ValueError("model_path must be a bounded local GGUF path")
    previous = read(data_dir)
    selected = previous["model_path"] if model_path is None else model_path.strip()
    identity = previous.get("identity") if selected == previous["model_path"] else None
    if enabled:
        from .offline_profile import load_offline_profile, local_model_path
        if not selected:
            selected = load_offline_profile(data_dir, include_embedding=True)["paths"].get("embedModelPath", "")
        if not selected or not (selected.startswith("file:") or Path(selected).is_absolute()):
            raise ValueError("Select an existing absolute local .gguf file; no download attempted")
        model = local_model_path(selected)
        if model is None or model.suffix.lower() != ".gguf" or not model.is_file():
            raise ValueError("Select an existing local .gguf file; no download attempted")
        before = model.stat()
        digest = hashlib.sha256()
        with model.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                raise ValueError("Embedding model is not a GGUF file")
            stream.seek(0)
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = model.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Embedding model changed during validation; retry")
        selected = str(model)
        identity = {"path": selected, "size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest.hexdigest()}
    embedding = {"enabled": enabled, "model_path": selected}
    if identity:
        embedding["identity"] = identity
    destination = settings_root(data_dir) / "retrieval-settings.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps({"schema": SCHEMA, "embedding": embedding}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return read(data_dir)
