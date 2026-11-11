"""Interactive real-DSH acceptance operator for a packed workbench.

It loads the unpacked artifact as an ephemeral SDK overlay, never installs into
or edits a user's desktop/web profile. Visible responses and paths are safe to
report; native reasoning, launch secrets and credentials are never exported.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import threading

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from advisor.dsh_runtime import DshRuntime


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=APP / "dist/sim-advisor-workbench-0.2.0.tgz")
    args = parser.parse_args()
    audit = APP / "data/dsh-package-audit"
    audit.mkdir(parents=True, exist_ok=True)
    unpacked = Path(tempfile.mkdtemp(prefix="installed package ", dir=audit)).resolve()
    with tarfile.open(args.archive, "r:gz") as archive:
        archive.extractall(unpacked, filter="data")
    package = unpacked / "package"
    data = unpacked.parent / (unpacked.name + " data")
    workspace = unpacked.parent / (unpacked.name + " agent")
    manuals = unpacked.parent / (unpacked.name + " manuals")
    workspace.mkdir()
    manuals.mkdir()
    (manuals / "mesh-convergence.md").write_text(
        "# Package acceptance: mesh convergence\n\n"
        "Synthetic transport fixture, not COMSOL documentation. Compare at least three mesh sizes, "
        "record degrees of freedom and the pressure-drop target quantity. Check mesh quality and "
        "the relative change under refinement. Actual geometry, inputs and tolerance remain unknown.\n",
        encoding="utf-8")
    plugin_config = {"dataDir": str(data), "docRoot": str(manuals), "applications": str(manuals / "no-examples")}
    launcher = workspace / "launch-overlay.py"
    launcher.write_text(
        "import json,sys,subprocess\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(APP)!r})\n"
        "from advisor.dsh_command import discover_dsh_command,launch_command\n"
        "patch=Path(sys.argv[sys.argv.index('--patch')+1])\n"
        f"extra='- insert:\\n    - id: packed-sim-advisor-workbench\\n      name: '+json.dumps({(package / 'index.js').as_uri()!r})+'\\n      config: '+json.dumps({plugin_config!r})+'\\n'\n"
        "patch.write_text(patch.read_text(encoding='utf-8')+extra,encoding='utf-8')\n"
        "child=subprocess.Popen(launch_command(discover_dsh_command(),patch),shell=False)\n"
        "raise SystemExit(child.wait())\n", encoding="utf-8")
    events = []
    lock = threading.Lock()

    def on_event(event):
        if event.get("type") not in {"message", "turn", "error"}:
            return
        with lock:
            events.append(event)
        if event.get("type") == "message":
            print(json.dumps({"visible_reply": event.get("text", "")[:1800]}, ensure_ascii=False), flush=True)
        elif event.get("type") == "error":
            print(json.dumps({"runtime_error": event.get("message", "DSH turn error")}, ensure_ascii=False), flush=True)
        elif event.get("event") == "completed":
            print(json.dumps({"turn_completed": True}), flush=True)

    runtime = DshRuntime([sys.executable, str(launcher)], workspace, on_event, lambda *_: None)
    try:
        started = runtime.start(instructions=(
            "This is a local package integration test using a synthetic fixture. Use the installed "
            "sim-advisor-workbench tools when asked. Do not execute shell commands, change permissions, "
            "run COMSOL or claim physical validation. Never export reasoning or credentials."))
        metadata = {"package_root": str(package), "data_dir": str(data), "workspace": str(workspace),
                    "manuals": str(manuals), "thread_id": started["thread_id"], "pid": started["pid"],
                    "archive": str(args.archive.resolve()), "persistent_profile_modified": False}
        (audit / "active.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ready": True, **metadata, "tool_names": runtime.status()["tool_names"]}, ensure_ascii=False), flush=True)
        for line in sys.stdin:
            try:
                command = json.loads(line)
                if command.get("op") == "send":
                    print(json.dumps({"sent": runtime.send(command["text"])}, ensure_ascii=False), flush=True)
                elif command.get("op") == "status":
                    print(json.dumps({"runtime": runtime.status()}, ensure_ascii=False), flush=True)
                elif command.get("op") == "interrupt":
                    print(json.dumps({"interrupt": runtime.interrupt()}, ensure_ascii=False), flush=True)
                elif command.get("op") == "shutdown":
                    break
            except Exception as error:
                print(json.dumps({"operator_error": type(error).__name__, "message": str(error)[:300]}, ensure_ascii=False), flush=True)
    finally:
        runtime.close()
        print(json.dumps({"stopped": True}), flush=True)


if __name__ == "__main__":
    main()
