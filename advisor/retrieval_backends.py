"""Explicit optional retrieval adapters. Native search is never called PageIndex.

QMD's keyword command requires no GGUF downloads. Its own configuration/cache
are isolated below data_dir. PageIndex is an opt-in PDF SDK adapter and requires
an explicitly configured reasoning model and credentials. Neither sends local
files to a hosted indexing service automatically.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from functools import lru_cache
from pathlib import Path
from .offline_profile import load_offline_profile, retrieval_runtime
from .retrieval_settings import read as read_embedding_settings

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
        "native": {"available": True, "mode": "native-html-hierarchy-sqlite-fts5", "detail": "COMSOL HTML topic boundaries and Eclipse TOC, explicit Chinese/English aliases, SQLite FTS5/BM25. No model required."},
        "qmd": {"available": qmd_ready, "installed": bool(qmd), "mode": "qmd-keyword" if qmd_ready else "installed-unverified" if qmd else "not-installed",
                "detail": verification.get("detail") or ("QMD installed; run scripts/ingest_docs.py --qmd to build and verify its isolated keyword index." if qmd else "QMD unavailable: " + "; ".join(runtime["errors"])),
                "dependencies": runtime,
                "document_count": verification.get("document_count", 0), "semantic_available": False,
                "limitations": "This legacy full-manual corpus is keyword-only. Registered-library vector status is reported separately under library_indexes; its embedding cache does not imply vectors exist for this corpus."},
        "pageindex": {"available": bool(pi and pi_verification.get("reasoning_verified") and configured), "installed": bool(pi), "mode": "pageindex-pdf-reasoning" if pi_verification.get("reasoning_verified") and configured else "awaiting-model-configuration" if pi else "not-installed",
                      "detail": pi_verification.get("detail") or ("PageIndex SDK installed. PDF tree indexing/reasoning is opt-in; set SIM_ADVISOR_PAGEINDEX_MODEL and provider credentials, then explicitly index a PDF." if pi else "PageIndex SDK not installed. Native HTML hierarchy is not PageIndex."),
                      "model_configured": configured, "provider_credentials_present": bool(credentials),
                      "limitations": "No PageIndex reasoning/indexing run has been verified. The native TOC is deterministic and must not be presented as model reasoning."},
    }


class QMDAdapter:
    def __init__(self, data_dir: Path):
        self.root = (Path(data_dir) / "qmd").resolve()
        self.dependencies = retrieval_runtime(data_dir, PROJECT_ROOT)
        self.command = qmd_command(data_dir)

    def _env(self):
        env = os.environ.copy()
        env.pop("QMD_EMBED_MODEL", None)
        env["XDG_CACHE_HOME"] = str(self.root / "cache")
        env["XDG_CONFIG_HOME"] = str(self.root / "config")
        env["QMD_CONFIG_DIR"] = str(self.root / "config/qmd")
        env["QMD_FORCE_CPU"] = "1"
        env["QMD_LLAMA_GPU"] = "false"
        env["QMD_EMBED_PARALLELISM"] = "1"
        if read_embedding_settings(self.root.parent)["enabled"] and self.dependencies["model_uri"]:
            env["QMD_EMBED_MODEL"] = self.dependencies["model_uri"]
        return env

    def _run(self, args, timeout=60):
        if not self.command:
            raise RuntimeError("QMD CLI unavailable: " + "; ".join(self.dependencies["errors"]))
        self.root.mkdir(parents=True, exist_ok=True)
        self._portable_configuration()
        return subprocess.run(self.command + list(args), cwd=self.root, env=self._env(),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)

    def _portable_configuration(self):
        """Rebase only the legacy adapter-owned collection, never reindex it."""
        config = self.root / "config/qmd/index.yml"
        if not config.is_file():
            return
        text = config.read_text(encoding="utf-8")
        if not re.search(r"(?m)^  comsol63:\s*$", text):
            return
        # This adapter runs all QMD commands with cwd=self.root. QMD persists
        # the collection metadata from this local config on open; lexical
        # search itself never calls update or document embedding.
        updated = re.sub(r"(?m)^(  comsol63:\s*\n)    path:[^\n]*", r'\1    path: "corpus"', text, count=1)
        if updated != text:
            temporary = config.with_name(config.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_text(updated, encoding='utf-8')
                os.replace(temporary, config)
            finally:
                temporary.unlink(missing_ok=True)

    def prepare(self, knowledge_base) -> dict:
        """Export *real source text* with identifiers, build QMD FTS, and probe it."""
        corpus = self.root / "corpus"
        corpus.mkdir(parents=True, exist_ok=True)
        count = 0
        exported_ids = set()
        with knowledge_base._connect() as db:
            for row in db.execute("SELECT * FROM documents ORDER BY relpath"):
                metadata = {"source_id": row["id"], "kind": row["kind"], "version": row["version"], "branch": row["branch"], "package": row["package"]}
                # JSON values are valid YAML scalars; preserve source text exactly.
                frontmatter = "---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in metadata.items()) + "\n---\n"
                document = knowledge_base._result(row, full=True)
                body = frontmatter + f"# {row['title']}\n\nSource: {document['url']}\n\nTOC: {row['context']}\n\n" + row["text"]
                target = corpus / (row["id"] + ".md")
                if not target.exists() or target.read_text(encoding="utf-8") != body:
                    target.write_text(body, encoding="utf-8")
                count += 1
                exported_ids.add(row["id"])
        # Only this adapter's generated leaf files are eligible for removal.
        for stale in corpus.glob("cs-*.md"):
            if re.fullmatch(r"cs-[a-f0-9]{24}\.md", stale.name) and stale.stem not in exported_ids:
                stale.unlink()
        config = self.root / "config/qmd/index.yml"
        config.parent.mkdir(parents=True, exist_ok=True)
        # Direct configuration avoids changing any user/global QMD collections.
        config.write_text('collections:\n  comsol63:\n    path: "corpus"\n    pattern: "**/*.md"\n    context:\n      "/": "Official local COMSOL 6.3 manuals, tutorials, and API reference. Preserve source_id provenance."\n', encoding="utf-8")
        updated = self._run(["update"], timeout=240)
        if updated.returncode:
            result = {"ready": False, "document_count": count, "detail": (updated.stderr or updated.stdout)[-1800:]}
        else:
            try:
                probe = self.search("heat transfer", 3)
                result = {"ready": bool(probe), "document_count": count, "detail": f"Actual QMD keyword index built from {count} official HTML topics; heat transfer probe returned {len(probe)} source IDs. No GGUF/model download."}
            except RuntimeError as exc:
                result = {"ready": False, "document_count": count, "detail": str(exc)[-1800:]}
        (self.root / "verified.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def search(self, query: str, limit: int = 8) -> list[dict]:
        proc = self._run(["search", query, "--json", "-n", str(max(1, min(limit, 180))), "-c", "comsol63"], timeout=30)
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout)[-1500:])
        output = proc.stdout.strip()
        try:
            values = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError("QMD did not return JSON") from exc
        if isinstance(values, dict):
            values = values.get("results", [])
        results = []
        for item in values:
            match = re.search(r"cs-[a-f0-9]{24}", str(item.get("file", "")))
            if match:
                results.append({"id": match[0], "score": item.get("score", 0), "retrieval_backend": "qmd-keyword"})
        return results


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
