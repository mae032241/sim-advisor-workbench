"""Assemble prepared, version-locked Windows x64 dependencies without installs."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

APP = Path(__file__).resolve().parents[1]
PREPARED = APP / "data/offline-build"
DIST = APP / "dist"
ASSETS = APP / "integrations/offline-dependencies"
VERSION = "0.2.3"
NAME = "sim-advisor-offline-win-x64-" + VERSION
ADDON_NAME = "sim-advisor-pageindex-addon-win-x64-" + VERSION
EMBEDDING_NAME = "sim-advisor-embedding-weights-win-x64-" + VERSION
MODEL_BYTES = 333590944
MODEL_SHA = "b5ce9d77a3fc4b3b39ccb5643c36777911cc4eb46a66962eadfa3f5f60490d63"


def sha(filename):
    digest = hashlib.sha256()
    with filename.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_tree(source, target, ignore=None):
    if not source.is_dir():
        raise ValueError(f"Prepared dependency directory missing: {source}")
    for item in source.rglob("*"):
        if item.is_symlink() or item.is_junction():
            raise ValueError(f"Dependency is not materialized: {item}")
    shutil.copytree(source, target, ignore=ignore)


def refresh_plugin(stage):
    version = json.loads((APP / "integrations/dsh-sim-workbench/package.json").read_text(encoding="utf-8"))["version"]
    plugin = DIST / f"sim-advisor-workbench-{version}.tgz"
    if not plugin.is_file():
        raise ValueError("Build the current plugin first")
    (stage / "plugins").mkdir(exist_ok=True)
    for suffix in ("", ".sha256", ".manifest.json"):
        shutil.copy2(Path(str(plugin) + suffix), stage / "plugins" / (plugin.name + suffix))
    with tarfile.open(plugin, "r:gz") as packed:
        for member in packed.getmembers():
            if not member.name.startswith("package/runtime/") or not member.isfile():
                continue
            relative = Path(member.name.removeprefix("package/runtime/"))
            target = stage / "workbench" / relative
            if relative.is_absolute() or not target.resolve().is_relative_to(stage.resolve()):
                raise ValueError("Invalid runtime member")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(packed.extractfile(member).read())


def assemble(*, include_plugin=True):
    stage = Path(tempfile.mkdtemp(prefix="complete bundle ", dir=PREPARED))
    print(json.dumps({"stage": "copy-single-minimal-python"}), flush=True)
    copy_tree(PREPARED / "python-minimal", stage / "python")
    if (stage / "python/pyvenv.cfg").exists():
        raise ValueError("A source virtualenv was copied instead of a portable runtime")
    print(json.dumps({"stage": "copy-node-qmd-and-licenses-without-weights"}), flush=True)
    copy_tree(PREPARED / "qmd-minimal" / "node", stage / "node")
    qmd_source = (PREPARED / "qmd-minimal").resolve()
    copy_tree(qmd_source, stage / "qmd", ignore=lambda directory, names: {"node"} if Path(directory).resolve() == qmd_source else set())
    for directory in ("licenses", "sources", "prerequisites"):
        copy_tree(PREPARED / directory, stage / directory)
    (stage / "manifests/python").mkdir(parents=True)
    for name in ("manifest-minimal.json", "build-report-minimal.json"):
        shutil.copy2(PREPARED / "python" / name, stage / "manifests/python" / name)
    # A source copy also exposes the independent MCP entry without locating DSH's profile.
    if include_plugin:
        refresh_plugin(stage)
    (stage / "scripts").mkdir()
    shutil.copy2(APP / "scripts/verify_offline_qmd.mjs", stage / "scripts/verify_offline_qmd.mjs")
    for source in ASSETS.iterdir():
        if source.is_file() and source.suffix in {".mjs", ".ps1", ".txt"}:
            shutil.copy2(source, stage / source.name)
    node = subprocess.run([str(stage / "node/node.exe"), "-p", "JSON.stringify({version:process.version,abi:process.versions.modules,platform:process.platform,arch:process.arch})"], capture_output=True, text=True, check=True)
    plugin_version = json.loads((APP / "integrations/dsh-sim-workbench/package.json").read_text(encoding="utf-8"))["version"]
    versions = {"schema": "sim-advisor-offline-versions/v1", "bundle_version": VERSION, "workbench_plugin": plugin_version, "python_profile": "minimal-single-runtime",
                "node": json.loads(node.stdout), "qmd": "2.8.3", "node-llama-cpp": "3.20.0",
                "better-sqlite3": "13.0.3", "sqlite-vec": "0.1.9", "python": "3.12",
                "core_roots": {"sim-cli-core": "0.4.2", "simparse": "0.4.0", "gmsh": "4.15.2", "pypdf": "7.0.0", "mcp": "1.29.1", "httpx": "0.28.1"},
                "mcp_runtime": "python/python.exe", "optional_addons": {"pageindex": {"version": "0.5.3", "archive": ADDON_NAME + ".zip", "same_python": True}},
                "embedding": {"enabled_by_default": False, "weights_included": False,
                              "optional_archive": EMBEDDING_NAME + ".zip", "bytes": MODEL_BYTES, "sha256": MODEL_SHA},
                "python_manifests": sorted(path.relative_to(stage).as_posix() for path in (stage / "manifests/python").glob("manifest-*.json")),
                "qmd_manifest": ["qmd/MANIFEST.json"],
                "qmd_preparation_layout_mapping": {"node/": "node/", "node_modules/": "qmd/node_modules/"},
                "platform_prerequisites": {"msvc_x64": {"installer": "prerequisites/vc_redist.x64.exe", "installed_automatically": False}},
                "excluded": ["COMSOL binaries", "official manuals/models", "user libraries and sessions", "credentials", "CUDA/Vulkan/ARM backend payloads", "online planning model weights", "PageIndex-only optional SDK dependencies", "audited development/build/test payloads"]}
    (stage / "versions.lock.json").write_text(json.dumps(versions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return stage


def package_embedding_addon(model_source=None):
    """Package existing local weights separately; never download or enable them."""
    if model_source is None:
        receipt = json.loads((APP / ".tools/library-search-cache/embedding-8e650b0ebff011e0.json").read_text(encoding="utf-8"))
        model_source = Path(receipt["path"])
    model_source = Path(model_source).resolve(strict=True)
    if model_source.stat().st_size != MODEL_BYTES or sha(model_source) != MODEL_SHA:
        raise ValueError("Embedding model does not match the tested version")
    stage = Path(tempfile.mkdtemp(prefix="embedding weights ", dir=PREPARED))
    (stage / "models").mkdir()
    shutil.copy2(model_source, stage / "models/embeddinggemma-300M-Q8_0.gguf")
    copy_tree(PREPARED / "licenses/gemma", stage / "licenses/gemma")
    (stage / "INSTALL.txt").write_text(
        "Optional local embedding weights. Extract anywhere, open the workbench retrieval controls, "
        "select models/embeddinggemma-300M-Q8_0.gguf, then explicitly enable embedding. "
        "Extraction and offline setup do not enable embedding. No Python, Node or installer is included.\n", encoding="utf-8")
    files = [{"path": source.relative_to(stage).as_posix(), "bytes": source.stat().st_size, "sha256": sha(source)}
             for source in sorted(stage.rglob("*")) if source.is_file()]
    manifest = {"schema": "sim-advisor-embedding-weights/v1", "name": EMBEDDING_NAME, "version": VERSION,
                "enabled_by_default": False, "files": files, "model_sha256": MODEL_SHA, "model_bytes": MODEL_BYTES}
    (stage / "CONTENTS.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = DIST / (EMBEDDING_NAME + ".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5, allowZip64=True) as output:
        for source in sorted(stage.rglob("*")):
            if source.is_file():
                output.write(source, EMBEDDING_NAME + "/" + source.relative_to(stage).as_posix())
    report = {"archive": archive.name, "bytes": archive.stat().st_size, "sha256": sha(archive),
              "uncompressed_bytes": sum(source.stat().st_size for source in stage.rglob("*") if source.is_file()),
              "files": len(files) + 1, "stage": str(stage), "enabled_by_default": False}
    Path(str(archive) + ".manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(str(archive) + ".sha256").write_text(report["sha256"] + "  " + archive.name + "\n", encoding="ascii")
    return report


def package_addon(base_stage):
    """An optional incremental SDK payload, with no second interpreter."""
    stage = Path(tempfile.mkdtemp(prefix="pageindex addon ", dir=PREPARED))
    payload = stage / "addon-pageindex"
    copy_tree(PREPARED / "addon-pageindex", payload)
    shutil.copy2(PREPARED / "python/manifest-addon-pageindex.json", payload / "PACKAGE-MANIFEST.json")
    files = [{"path": source.relative_to(payload).as_posix(), "bytes": source.stat().st_size, "sha256": sha(source)}
             for source in sorted(payload.rglob("*")) if source.is_file()]
    manifest = {"schema": "sim-advisor-offline-addon/v1", "name": "pageindex", "version": VERSION,
                "python_runtime": "../python/python.exe", "no_interpreter_included": True,
                "requires_base_python_manifest_sha256": sha(base_stage / "manifests/python/manifest-minimal.json"),
                "files": files}
    (payload / "CONTENTS.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = DIST / (ADDON_NAME + ".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5, allowZip64=True) as output:
        for source in sorted(payload.rglob("*")):
            if source.is_file():
                output.write(source, NAME + "/addon-pageindex/" + source.relative_to(payload).as_posix())
    report = {"archive": archive.name, "bytes": archive.stat().st_size, "sha256": sha(archive),
              "uncompressed_bytes": sum(source.stat().st_size for source in payload.rglob("*") if source.is_file()),
              "files": len(files) + 1, "stage": str(stage), "no_interpreter_included": True}
    Path(str(archive) + ".manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(str(archive) + ".sha256").write_text(report["sha256"] + "  " + archive.name + "\n", encoding="ascii")
    return report


def package(stage):
    stage = stage.resolve(strict=True)
    if not stage.is_relative_to(PREPARED.resolve()):
        raise ValueError("Stage must remain inside the task's offline-build directory")
    versions_path = stage / "versions.lock.json"
    versions = json.loads(versions_path.read_text(encoding="utf-8"))
    if versions.get("bundle_version") != VERSION or versions.get("embedding", {}).get("weights_included") is not False:
        raise ValueError("Repackaging historical full bundles is refused; assemble a new weight-free stage")
    if any(source.suffix.lower() == ".gguf" for source in stage.rglob("*")):
        raise ValueError("The default offline base cannot contain GGUF weights")
    refresh_plugin(stage)
    shutil.copy2(APP / "scripts/verify_offline_qmd.mjs", stage / "scripts/verify_offline_qmd.mjs")
    versions["qmd_manifest"] = ["qmd/MANIFEST.json"]
    versions["qmd_preparation_layout_mapping"] = {"node/": "node/", "node_modules/": "qmd/node_modules/"}
    versions["platform_prerequisites"] = {"msvc_x64": {"installer": "prerequisites/vc_redist.x64.exe", "installed_automatically": False,
        "sha256": sha(stage / "prerequisites/vc_redist.x64.exe"), "authenticode_preparation_check": "Valid / Microsoft Corporation"}}
    versions_path.write_text(json.dumps(versions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for source in ASSETS.iterdir():
        if source.is_file() and source.suffix in {".mjs", ".ps1", ".txt"}:
            shutil.copy2(source, stage / source.name)
    for source, target in ((DIST / "INSTALL-OFFLINE.txt", stage / "INSTALL-OFFLINE.txt"),
                           (APP / "docs/offline-dependencies.md", stage / "OFFLINE-DEPENDENCIES.md")):
        shutil.copy2(source, target)
    print(json.dumps({"stage": "hash-distribution-files", "directory": str(stage)}), flush=True)
    files = []
    for filename in sorted(stage.rglob("*")):
        if filename.is_symlink() or filename.is_junction():
            raise ValueError("Offline bundle must contain materialized files only")
        if filename.is_file() and filename.name != "CONTENTS.json":
            files.append({"path": filename.relative_to(stage).as_posix(), "bytes": filename.stat().st_size, "sha256": sha(filename)})
    manifest = {"schema": "sim-advisor-offline-bundle/v1", "name": NAME, "platform": "win32-x64",
                "file_count": len(files), "uncompressed_bytes": sum(row["bytes"] for row in files), "files": files,
                "conditions": "Component-specific licenses and model conditions are retained; no common restriction is applied to unrelated components."}
    (stage / "CONTENTS.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = DIST / (NAME + ".zip")
    print(json.dumps({"stage": "write-zip", "file_count": len(files), "bytes": manifest["uncompressed_bytes"]}), flush=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5, allowZip64=True) as output:
        for filename in sorted(stage.rglob("*")):
            if filename.is_file():
                entry = zipfile.ZipInfo(NAME + "/" + filename.relative_to(stage).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                with filename.open("rb") as source, output.open(entry, "w", force_zip64=True) as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
    report = {key: manifest[key] for key in ("schema", "name", "platform", "file_count", "uncompressed_bytes")}
    report.update(archive=archive.name, bytes=archive.stat().st_size, sha256=sha(archive),
                  contents_sha256=sha(stage / "CONTENTS.json"), stage=str(stage))
    Path(str(archive) + ".manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(str(archive) + ".sha256").write_text(report["sha256"] + "  " + archive.name + "\n", encoding="ascii")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, help="Repackage an already assembled stage after documentation updates")
    parser.add_argument("--with-addon", action="store_true", help="Also create the separately installed PageIndex incremental archive")
    parser.add_argument("--with-embedding-addon", action="store_true", help="Also package existing local GGUF weights in a separate optional archive")
    parser.add_argument("--assemble-only", action="store_true", help="Stage prepared dependencies without waiting for the final plugin artifact")
    args = parser.parse_args()
    if args.assemble_only:
        print(json.dumps({"stage": str(assemble(include_plugin=False)), "weights_included": False}), flush=True)
        raise SystemExit(0)
    stage = args.stage if args.stage else assemble()
    print(json.dumps(package(stage), ensure_ascii=False), flush=True)
    if args.with_addon:
        print(json.dumps(package_addon(stage), ensure_ascii=False), flush=True)
    if args.with_embedding_addon:
        print(json.dumps(package_embedding_addon(), ensure_ascii=False), flush=True)
