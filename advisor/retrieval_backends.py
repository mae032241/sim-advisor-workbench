"""QMD compatibility and the explicit optional PageIndex adapter.

QMD's keyword command requires no GGUF downloads. Its own configuration/cache
are isolated below data_dir. PageIndex is an opt-in PDF SDK adapter and requires
an explicitly configured reasoning model and credentials. Neither sends local
files to a hosted indexing service automatically.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from .offline_profile import load_offline_profile, retrieval_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def qmd_command(data_dir: Path | None = None) -> list[str] | None:
    configured = os.environ.get("SIM_ADVISOR_QMD")
    if configured:
        candidate = Path(configured).expanduser()
        command = str(candidate.resolve()) if candidate.is_file() else shutil.which(configured) if not any(c in configured for c in '/\\') else None
        return [command] if command else None
    # Prefer the project installation to a user's unrelated global index.
    runtime = retrieval_runtime(data_dir or PROJECT_ROOT / "data", PROJECT_ROOT)
    package = Path(runtime["qmd_dir"]) if runtime["qmd_dir"] else None
    local = package / "dist/cli/qmd.js" if package else None
    node = runtime["node"]
    if local and local.is_file() and node:
        return [node, str(local.resolve())]
    if runtime["profile"]["present"] or runtime["sources"]["qmd_dir"] != "default" or runtime["sources"]["node"] != "default":
        return None
    binary = shutil.which("qmd")
    return [binary] if binary else None


@lru_cache(maxsize=8)
def _pageindex_sdk_available(interpreter: str, mtime_ns: int) -> bool:
    """A Python executable alone does not establish that the optional SDK exists."""
    try:
        result = subprocess.run([interpreter, "-I", "-B", "-c",
            "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('pageindex') else 2)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _pageindex_python(data_dir: Path | None = None) -> Path | None:
    configured = os.environ.get("SIM_ADVISOR_PAGEINDEX_PYTHON")
    if not configured and data_dir is not None:
        profile = load_offline_profile(data_dir)
        if profile["present"]:
            configured = profile["paths"].get("pageindexPython") or profile["paths"].get("pythonPath")
            if not configured:
                return None
    if configured:
        candidate = Path(configured).expanduser().resolve()
        # An explicit interpreter is never silently replaced by a different one.
        return candidate if candidate.is_file() and _pageindex_sdk_available(str(candidate), candidate.stat().st_mtime_ns) else None
    candidates = [PROJECT_ROOT / ".retrieval-env/Scripts/python.exe", PROJECT_ROOT / ".retrieval-env/bin/python"]
    for candidate in candidates:
        if candidate.is_file() and (PROJECT_ROOT / ".retrieval-env/Lib/site-packages/pageindex/__init__.py").is_file():
            return candidate
    return Path(sys.executable) if importlib.util.find_spec("pageindex") else None


def backend_status(data_dir: Path) -> dict:
    data_dir = Path(data_dir)
    qmd = qmd_command(data_dir)
    runtime = retrieval_runtime(data_dir, PROJECT_ROOT)
    qmd_manifest = data_dir / "qmd/verified.json"
    verification = {}
    if qmd_manifest.is_file():
        try:
            verification = json.loads(qmd_manifest.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    qmd_ready = bool(qmd and verification.get("ready"))
    pi = _pageindex_python(data_dir)
    pi_verification = {}
    pi_manifest = data_dir / "pageindex/verified.json"
    if pi_manifest.is_file():
        try:
            pi_verification = json.loads(pi_manifest.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    configured = bool(os.environ.get("SIM_ADVISOR_PAGEINDEX_MODEL"))
    credentials = any(os.environ.get(name) for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_API_KEY"))
    return {
        "qmd": {"available": False, "installed": bool(qmd), "mode": "registered-library-index-required" if qmd else "not-installed",
                "detail": "QMD installed; register source libraries and build qmd_keyword. Readiness is reported under library_indexes." if qmd else "QMD unavailable: " + "; ".join(runtime["errors"]),
                "dependencies": runtime,
                "document_count": 0, "semantic_available": False,
                "archived_legacy_keyword": {"verified": qmd_ready, "document_count": verification.get("document_count", 0), "searchable": False},
                "limitations": "Only registered-library QMD keyword/vector modes are supported. Legacy verification receipts are historical; unavailable QMD never falls back to another engine."},
        "pageindex": {"available": bool(pi and pi_verification.get("reasoning_verified") and configured), "installed": bool(pi), "mode": "pageindex-pdf-reasoning" if pi_verification.get("reasoning_verified") and configured else "awaiting-model-configuration" if pi else "not-installed",
                      "detail": pi_verification.get("detail") or ("PageIndex SDK installed. PDF tree indexing/reasoning is opt-in; set SIM_ADVISOR_PAGEINDEX_MODEL and provider credentials, then explicitly index a PDF." if pi else "PageIndex SDK not installed. Native HTML hierarchy is not PageIndex."),
                      "model_configured": configured, "provider_credentials_present": bool(credentials),
                      "limitations": "No PageIndex reasoning/indexing run has been verified. The native TOC is deterministic and must not be presented as model reasoning."},
    }


class QMDAdapter:
    """Compatibility facade for the single registered-library QMD engine."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.dependencies = retrieval_runtime(self.data_dir, PROJECT_ROOT)
        self.command = qmd_command(self.data_dir)

    def prepare(self, knowledge_base=None) -> dict:
        """Reindex registered source libraries without exporting a second corpus."""
        from .library_store import LibraryStore
        return LibraryStore(self.data_dir / "libraries").reindex(["qmd_keyword"])

    def search(self, query: str, limit: int = 8) -> list[dict]:
        from .knowledge import KnowledgeBase
        from .library_provider import LibraryKnowledgeProvider
        provider = LibraryKnowledgeProvider(KnowledgeBase(self.data_dir), self.data_dir / "libraries")
        return provider.search(query, limit=limit, filters={"mode": "qmd_keyword"})


class PageIndexAdapter:
    """SDK boundary for explicit PDF experiments, never a silent fallback.

    A PDF must be explicitly selected: this adapter does not upload the COMSOL
    corpus and does not turn a native HTML TOC into a claimed PageIndex result.
    """
    def __init__(self, data_dir: Path):
        self.root = (Path(data_dir) / "pageindex").resolve()

    def _run(self, action: str, **payload):
        interpreter = _pageindex_python(self.root.parent)
        if not interpreter:
            raise RuntimeError("PageIndex SDK is not installed")
        model = os.environ.get("SIM_ADVISOR_PAGEINDEX_MODEL")
        if not model:
            raise RuntimeError("Set SIM_ADVISOR_PAGEINDEX_MODEL explicitly; no reasoning model is configured")
        self.root.mkdir(parents=True, exist_ok=True)
        message = {"action": action, "storage_path": str(self.root.resolve()), "model": model, **payload}
        script = PROJECT_ROOT / "scripts/pageindex_bridge.py"
        proc = subprocess.run([str(interpreter), str(script)], input=json.dumps(message), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=240)
        if proc.returncode:
            # Provider exception messages sometimes contain URL parameters; return
            # a bounded generic failure instead of logging secrets.
            raise RuntimeError(f"PageIndex SDK {action} failed (exit {proc.returncode}); check provider/model configuration")
        try:
            return json.loads(proc.stdout)
        except ValueError as exc:
            raise RuntimeError("PageIndex SDK did not return valid JSON") from exc

    def index_pdf(self, path: Path):
        path = Path(path).resolve()
        if path.suffix.lower() != ".pdf" or not path.is_file():
            raise ValueError("PageIndex PDF adapter requires an existing PDF file")
        return self._run("index", path=str(path))

    def search(self, query: str, doc_id: str):
        result = self._run("search", query=query, doc_id=doc_id)
        verification = {"reasoning_verified": True, "detail": "Actual PageIndex SDK PDF reasoning query succeeded for an explicitly indexed PDF. Native HTML search remains a separate backend."}
        (self.root / "verified.json").write_text(json.dumps(verification), encoding="utf-8")
        return result
