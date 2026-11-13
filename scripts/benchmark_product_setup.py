"""Create benchmark tasks through the workbench's ordinary upload/session API.

Only public user inputs are read. Geometry facts remain the product's real
inspection result; this helper never constructs a model or session snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


TIMEOUT = 120
TEXT_SUFFIXES = {".csv", ".json", ".txt", ".md"}
DERIVED_NAMES = {"geometry.json", "topology.json"}


def _sha256(blob):
    return hashlib.sha256(blob).hexdigest()


def _case_directory(root, case_id):
    if not isinstance(case_id, str) or not re.fullmatch(r"case-[0-9]{2}", case_id):
        raise ValueError("Expected a public case ID such as case-01")
    root = Path(root).resolve()
    directory = root / "public" / "cases" / case_id
    _public_file(root, directory, directory=True)
    return root, directory


def _public_file(root, path, *, directory=False):
    public = root / "public"
    if not path.is_relative_to(public):
        raise ValueError("Input must remain inside the benchmark public directory")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("Public inputs cannot be filesystem aliases")
    if not path.resolve().is_relative_to(public.resolve()):
        raise ValueError("Input escapes the benchmark public directory")
    if not (path.is_dir() if directory else path.is_file()):
        raise FileNotFoundError(f"Missing public input: {path.relative_to(root).as_posix()}")
    return path


def _public_inputs(root, case_id):
    root, directory = _case_directory(root, case_id)
    problem = _public_file(root, directory / "problem.md")
    files = [(problem, "problem", problem.read_bytes())]
    attachments = []

    def walk_error(error):
        raise error  # Inaccessible attachments must not silently disappear.

    for current, directories, filenames in os.walk(directory, topdown=True, onerror=walk_error, followlinks=False):
        current = Path(current)
        # Validate each child before os.walk descends. In particular, do not
        # traverse a junction/alias and try to validate its descendants later.
        for name in directories:
            _public_file(root, current / name, directory=True)
        directories[:] = sorted(name for name in directories if name.casefold() != "inputs")
        for name in filenames:
            path = _public_file(root, current / name)
            if path == problem or name.casefold() in DERIVED_NAMES or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            attachments.append(path)
    for path in sorted(attachments, key=lambda item: item.relative_to(directory).as_posix()):
        files.append((path, "user_attachment", path.read_bytes()))
    sections = []
    for path, role, blob in files:
        # Decode bytes directly so original CRLFs, whitespace and data rows are
        # preserved instead of undergoing universal-newline conversion.
        content = blob.decode("utf-8")
        label = "用户需求" if role == "problem" else "用户附件"
        sections.append(f"### {label}：{path.relative_to(directory).as_posix()}\n\n{content}\n\n")
    goal = "".join(sections) + "--- 公开用户输入结束 ---"
    if not 8 <= len(goal) <= 12000:
        raise ValueError(f"{case_id}: complete public goal has {len(goal)} characters; expected 8..12000, no input was truncated")
    return root, directory, goal, files


def public_goal(root, case_id):
    """Return complete problem/attachment text, or fail rather than truncate."""
    return _public_inputs(root, case_id)[2]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_json(url, *, data, headers, timeout=TIMEOUT):
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    message = Request(url, data=data, headers=headers, method="POST")
    with opener.open(message, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Workbench response must be a JSON object")
    return result


def _base_url(url):
    if not isinstance(url, str):
        raise ValueError("Workbench URL must be a loopback HTTP URL")
    parts = urlsplit(url)
    if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parts.username is not None or parts.password is not None
            or parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise ValueError("Workbench URL must be a loopback HTTP origin")
    # Validate a malformed/out-of-range port before any upload occurs.
    parts.port
    return url.rstrip("/")


def create_task(url, root, case_id, *, request=None):
    """Upload geometry, then create a task with the unmodified public text.

    A test transport may be supplied as
    ``request(url, *, data: bytes, headers: dict, timeout: int) -> dict``.
    Both calls are POSTs. No response/model fields are synthesized.
    """
    base = _base_url(url)
    root, directory, goal, inputs = _public_inputs(root, case_id)
    geometry = _public_file(root, directory / "inputs" / "geometry_only.mph")
    geometry_bytes = geometry.read_bytes()
    inputs.append((geometry, "initial_geometry", geometry_bytes))
    manifest = {
        "schema": "product-benchmark-inputs/v1", "case_id": case_id,
        "goal_sha256": _sha256(goal.encode("utf-8")), "goal_chars": len(goal),
        "files": [{"path": path.relative_to(root).as_posix(), "role": role,
                   "bytes": len(blob), "sha256": _sha256(blob)}
                  for path, role, blob in inputs],
    }
    manifest["sha256"] = _sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8"))
    post = request or _post_json
    model = post(base + "/api/models/upload", data=geometry_bytes,
                 headers={"Content-Type": "application/octet-stream", "X-File-Name": "geometry_only.mph"},
                 timeout=TIMEOUT)
    if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]:
        raise ValueError("Geometry upload did not return a model ID")
    session = post(base + "/api/sessions",
                   data=json.dumps({"goal": goal, "model_id": model["id"]}, ensure_ascii=False).encode("utf-8"),
                   headers={"Content-Type": "application/json"}, timeout=TIMEOUT)
    if not isinstance(session, dict) or not isinstance(session.get("id"), str) or not session["id"]:
        raise ValueError("Task creation did not return a session ID")
    return {"session": session, "model": model, "input_manifest": manifest, "goal_chars": len(goal)}
