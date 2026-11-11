"""Read-only discovery of the official DSH CLI; no profile/auth inspection."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


def _registered_paths():
    if os.name != "nt":
        return []
    import winreg
    values = []
    for hive, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value = winreg.QueryValueEx(handle, "Path")[0]
                if isinstance(value, str):
                    values.extend(os.path.expandvars(part.strip().strip('"')) for part in value.split(os.pathsep) if part.strip())
        except OSError:
            pass
    return values


def discover_dsh_command() -> list[str] | None:
    configured = os.environ.get("SIM_ADVISOR_DSH")
    if configured:
        candidate = Path(configured).expanduser()
        found = str(candidate.resolve()) if candidate.is_file() else shutil.which(configured)
        return [found] if found else None  # Explicit configuration never silently falls back.
    for directory in _registered_paths():
        for filename in ("dsh.cmd", "dsh.exe", "dsh"):
            candidate = Path(directory) / filename
            if candidate.is_file():
                return [str(candidate.resolve())]
    for filename in (("dsh.cmd", "dsh.exe", "dsh") if os.name == "nt" else ("dsh",)):
        found = shutil.which(filename)
        if found:
            return [found]
    # A final known desktop-shim candidate supplements PATH; it is not the sole discovery route.
    for candidate in (Path(os.environ.get("LOCALAPPDATA", "")) / "DSH/resources/runtime/cli/bin/dsh.cmd",
                      Path("D:/software/dsh/resources/runtime/cli/bin/dsh.cmd")):
        if os.name == "nt" and candidate.is_file():
            return [str(candidate.resolve())]
    return None


def dsh_status() -> dict:
    available = discover_dsh_command() is not None
    return {"available": available, "mode": "dsh-managed-host", "backend_id": "dsh",
            "detail": "本机 DSH SDK Host；会话与模型配置在连接时校验。" if available else "未找到 DSH CLI；请配置 SIM_ADVISOR_DSH。"}


def launch_command(command: list[str], patch: Path) -> list[str] | str:
    args = [*command, "sdk", "--patch", str(patch)]
    if os.name == "nt" and Path(args[0]).suffix.lower() in {".cmd", ".bat"}:
        # Only CLI/patch paths enter cmd.exe. User text, tools, instructions and
        # the private token use the callback channel, never shell interpolation.
        if any(any(character in argument for character in '\r\n&|<>^%!') for argument in args):
            raise ValueError("DSH shim/patch paths contain unsupported Windows shell characters.")
        # Pass the complete CreateProcess line as a string. Placing an already
        # quoted /c payload in an argv list makes Popen escape its quotes again.
        interpreter = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe")])
        return interpreter + ' /d /s /c "' + subprocess.list2cmdline(args) + '"'
    return args
