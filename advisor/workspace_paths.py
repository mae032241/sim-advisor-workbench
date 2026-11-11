"""Operational locators for a stopped, copied workbench data directory.

Only explicit path fields use this codec. Historical source observations and
immutable citation payloads are deliberately not rewritten.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path, PureWindowsPath
import threading
import uuid

SCHEMA = "sim-advisor-workspace-location/v1"
_LOCK = threading.RLock()


class WorkspacePaths:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        if self.root.parent == self.root:
            raise ValueError("A filesystem root cannot be a workbench data directory")
        self.current_root = self.root
        self.root.mkdir(parents=True, exist_ok=True)
        self.anchor_path = self.root / "workspace-location.json"
        with _LOCK:
            previous = []
            if self.anchor_path.is_file():
                value = json.loads(self.anchor_path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("schema") != SCHEMA:
                    raise ValueError("Invalid workspace-location.json; preserve its historical roots before repairing it")
                if not isinstance(value.get("previous_roots", []), list):
                    raise ValueError("Workspace previous_roots must be a list")
                roots = [value.get("current_root"), *value.get("previous_roots", [])]
                if any(not isinstance(p, str) or not Path(p).is_absolute() or Path(p).resolve().parent == Path(p).resolve() for p in roots):
                    raise ValueError("Workspace location anchors must be absolute directories")
                previous.extend(Path(p).resolve() for p in roots)
            # Compatibility for data written before the explicit anchor existed.
            # This field has a known layout; an arbitrary filename cannot become
            # authority to redirect external sources.
            active = self.root / "libraries" / "search" / "active.json"
            try:
                value = json.loads(active.read_text(encoding="utf-8"))
                directory = Path(value["directory"])
                if (directory.is_absolute() and len(directory.parts) >= 5 and directory.name not in {".", ".."}
                        and tuple(p.lower() for p in directory.parts[-4:-1]) == ("libraries", "search", "generations")):
                    old = directory.parents[3].resolve()
                    if old.parent != old:
                        previous.append(old)
            except (OSError, ValueError, KeyError, TypeError):
                pass
            receipt = self.root / "plugin-offline-setup-receipt.json"
            try:
                value = json.loads(receipt.read_text(encoding="utf-8"))
                directory = Path(value["dataDir"])
                if (value.get("schema") == "sim-advisor-plugin-offline-setup/v1"
                        and directory.is_absolute() and directory.resolve().parent != directory.resolve()):
                    previous.append(directory.resolve())
            except (OSError, ValueError, KeyError, TypeError):
                pass
            # The setup receipt may already have been rebound by the JS host
            # before Python starts. Its hash-verified proof keeps the original
            # data-root locator and covers legacy workspaces without a modern
            # library/search manifest.
            try:
                value = json.loads((self.root / 'offline-verification.json').read_text(encoding='utf-8'))
                report = Path(value['qmdReport'])
                if value.get('verified') is True and value.get('singlePython') is True and report.is_absolute():
                    positions = [i for i, part in enumerate(report.parts) if part == 'offline-checks']
                    if positions and 0 < positions[-1] < len(report.parts) - 1:
                        old = Path(*report.parts[:positions[-1]]).resolve()
                        if old.parent != old:
                            previous.append(old)
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self.previous_roots = tuple(dict.fromkeys(p for p in previous if p != self.root))
            body = {"schema": SCHEMA, "current_root": str(self.root),
                    "previous_roots": [str(p) for p in self.previous_roots]}
            if not self.anchor_path.is_file() or json.loads(self.anchor_path.read_text(encoding="utf-8")) != body:
                temp = self.anchor_path.with_name(self.anchor_path.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    temp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(temp, self.anchor_path)
                finally:
                    temp.unlink(missing_ok=True)

    def resolve(self, value) -> Path:
        """Resolve data-relative paths and explicitly anchored old absolutes."""
        path = Path(value).expanduser()
        if path.is_absolute():
            # Prefer the current location if roots happen to overlap.
            for root in (self.root, *sorted(self.previous_roots, key=lambda p: len(p.parts), reverse=True)):
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    continue
                candidate = (self.root / relative).resolve()
                if not candidate.is_relative_to(self.root):
                    raise ValueError("Workspace locator escapes the data directory")
                return candidate
            return path.resolve()
        if PureWindowsPath(str(value)).drive:
            raise ValueError("Drive-relative workspace locators are not supported")
        candidate = (self.root / path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("Workspace locator escapes the data directory")
        return candidate

    def encode(self, value) -> str:
        path = self.resolve(value)
        return path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else str(path)

    def model_snapshot(self, snapshot, *, encode=False):
        """Copy the mutable intake snapshot; only its top-level file locator."""
        value = copy.deepcopy(snapshot)
        if isinstance(value, dict) and isinstance(value.get("path"), str):
            value["path"] = self.encode(value["path"]) if encode else str(self.resolve(value["path"]))
        return value
