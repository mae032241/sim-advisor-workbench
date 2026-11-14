"""Build a dependency-free npm bundle using an explicit source allowlist.

No user data, model/manual corpus, binary environment, credentials or caches are
included. The packed artifact, not just the source directory, is verified.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

APP = Path(__file__).resolve().parents[1]
BUNDLE = APP / "integrations" / "dsh-sim-workbench"
TOP_LEVEL = ("run.py", "agent_cli.py", "agent_tools.py", "external_agent_client.py",
             "library_cli.py", "tools_cli.py", "workbench_mcp.py", "requirements-mcp.txt",
             "README.md", "CONTRACT.md", "MODEL_INTEGRATION.md")
PLUGIN_FILES = ("package.json", "index.js", "service.js", "dependency-profile.js", "offline-setup.js",
                "config.js", "config-ui.js", "client.js", "cordis.patch.yml", "icon.svg", "README.md")
RUNTIME_SCRIPTS = ("ingest_docs.py", "pageindex_bridge.py", "compare_offline_mph.py", "mph_extract/ReadMph.java", "mph_extract/ReadMphBatch.java", "enrich_model_api.py",
                   "verify_offline_qmd.mjs", "remove_legacy_object_fts.py", "slim_object_index.py", "normalize_tutorials.py", "verify_tutorial_normalization.py", "reclaim_tutorial_storage.py", "merge_qmd_indexes.py",
                   "RESEARCH_PLANNER.md", "RETRIEVAL.md", "select_evaluation_cases.py", "configure_evaluation_set.py", "build_html_tree.py", "build_case_tree.py", "build_case_guides.py", "enrich_case_contexts.py", "enrich_all_case_contexts.py")


def collect_sources() -> dict[str, Path]:
    files = {name: BUNDLE / name for name in PLUGIN_FILES}
    files["offline-initialize.mjs"] = APP / "integrations" / "offline-dependencies" / "initialize.mjs"
    for source in sorted((BUNDLE / "locale").glob("*.json")):
        files[source.relative_to(BUNDLE).as_posix()] = source
    for source in sorted((BUNDLE / "vendor").rglob("*")):
        if source.is_file():
            files[source.relative_to(BUNDLE).as_posix()] = source
    for name in TOP_LEVEL:
        files["runtime/" + name] = APP / name
    for directory, suffixes in (("advisor", {".py"}), ("web", {".js", ".css", ".html", ".svg"}),
                                ("prompts", {".txt", ".json"}), ("docs", {".md"})):
        for source in sorted((APP / directory).rglob("*")):
            if source.is_file() and source.suffix in suffixes and not any(
                    part.startswith(".") or part == "__pycache__" for part in source.relative_to(APP).parts):
                files["runtime/" + source.relative_to(APP).as_posix()] = source
    for name in RUNTIME_SCRIPTS:
        files["runtime/scripts/" + name] = APP / "scripts" / name
    for name in ("index.js", "http.js"):
        files["bridge/" + name] = APP / "integrations" / "dsh-workbench-bridge" / name
    for name in ("index.js", "package.json", "README.md"):
        files["runtime/integrations/dsh-managed-host/" + name] = APP / "integrations" / "dsh-managed-host" / name
    return files


def build(destination: Path) -> dict:
    manifest = json.loads((BUNDLE / "package.json").read_text(encoding="utf-8"))
    files = collect_sources()
    required = {"runtime/run.py", "runtime/library_cli.py", "runtime/web/index.html",
                "runtime/prompts/comsol_planner.v1.txt", "runtime/scripts/mph_extract/ReadMph.java",
                "runtime/advisor/library_store.py", "runtime/advisor/html_tree.py", "runtime/advisor/html_tree_tools.py",
                "runtime/advisor/html_tree_store.py", "runtime/advisor/research_policy.py", "runtime/scripts/build_html_tree.py",
                "runtime/advisor/case_tree.py", "runtime/advisor/case_manual_links.py", "runtime/scripts/build_case_tree.py",
                "runtime/advisor/case_original_tools.py", "runtime/advisor/raw_resource_tools.py",
                "runtime/docs/case-tree-navigation.md",
                "runtime/docs/html-manual-tree.md", "bridge/index.js", "service.js", "dependency-profile.js", "offline-setup.js",
                "config.js", "config-ui.js", "client.js", "locale/zh.json"}
    if not required.issubset(files):
        raise ValueError("Incomplete runtime resource list")
    resources = {}
    for name, source in sorted(files.items()):
        resolved = source.resolve(strict=True)
        if source.is_symlink() or not resolved.is_relative_to(APP):
            raise ValueError(f"Resource escapes application: {name}")
        resources[name] = resolved.read_bytes()
    inventory = {"name": manifest["name"], "version": manifest["version"],
                 "files": [{"path": name, "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                           for name, value in resources.items()],
                 "excluded": ["data", ".venv", ".model-env", ".retrieval-env", ".tools", "development node_modules (except audited vendor source closure)",
                              "tests", "COMSOL manuals and models", "local evaluation selections", "credentials", "session history"],
                 "external_dependencies": ["Python 3.12+ (DSH embedded or explicit)",
                                           "COMSOL documents/models supplied by user",
                                           "Node and QMD 2.8.3 for keyword retrieval",
                                           "Optional sim-cli, PageIndex, COMSOL installation"]}
    resources["CONTENTS.json"] = (json.dumps(inventory, ensure_ascii=False, indent=2) + "\n").encode()
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / f"sim-advisor-workbench-{manifest['version']}.tgz"
    with archive_path.open("wb") as output, gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, value in sorted(resources.items()):
                entry = tarfile.TarInfo("package/" + name)
                entry.size = len(value)
                entry.mode = 0o644
                entry.mtime = entry.uid = entry.gid = 0
                archive.addfile(entry, io.BytesIO(value))
    with tarfile.open(archive_path, "r:gz") as archive:
        packed = {member.name.removeprefix("package/"): archive.extractfile(member).read()
                  for member in archive.getmembers() if member.isfile()}
    if packed != resources:
        raise ValueError("Packed resources differ from reviewed sources")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    report = {**inventory, "archive": archive_path.name, "bytes": archive_path.stat().st_size,
              "sha256": digest, "packed_file_count": len(packed), "verified_source_match": True}
    archive_path.with_suffix(archive_path.suffix + ".manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive_path.with_suffix(archive_path.suffix + ".sha256").write_text(
        f"{digest}  {archive_path.name}\n", encoding="ascii")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=APP / "dist")
    args = parser.parse_args()
    report = build(args.out_dir.resolve())
    print(json.dumps({key: report[key] for key in ("archive", "bytes", "sha256", "packed_file_count", "verified_source_match")}, ensure_ascii=False))
