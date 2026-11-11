"""Build relocatable Windows Python dependency payloads without changing source environments."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata as metadata
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

APP = Path(__file__).resolve().parents[1]
WORKSPACE = APP.parents[1]
DEFAULT_STAGE = APP / "data/offline-build/python"
DSH_PYTHON = Path("D:/software/dsh/resources/runtime/primary-runtime/dependencies/python")
ENVIRONMENTS = {"model": APP / ".model-env", "retrieval": APP / ".retrieval-env", "workspace": WORKSPACE / ".venv"}
ROOT_PACKAGES = ("sim-cli-core", "simparse", "gmsh", "pypdf", "PageIndex", "mcp", "httpx")
CORE_ROOTS = ("sim-cli-core==0.4.2", "simparse==0.4.0", "gmsh==4.15.2", "pypdf==7.0.0", "PageIndex==0.5.3", "mcp==2.3.0")
MCP_ROOTS = ("mcp==1.29.1", "httpx==0.28.1")
UNIFIED_ROOTS = ("sim-cli-core==0.4.2", "simparse==0.4.0", "gmsh==4.15.2", "pypdf==7.0.0", "PageIndex==0.5.3", *MCP_ROOTS)
MINIMAL_ROOTS = ("sim-cli-core==0.4.2", "simparse==0.4.0", "gmsh==4.15.2", "pypdf==7.0.0", *MCP_ROOTS)
PROFILES = {"split": (("core", CORE_ROOTS), ("mcp", MCP_ROOTS)), "unified": (("unified", UNIFIED_ROOTS),), "minimal": (("minimal", MINIMAL_ROOTS),)}


def canonical(name):
    return name.lower().replace("_", "-").replace(".", "-")


def directory_size(root, *, exclude_site=False):
    total = 0
    if not root.is_dir():
        return total
    for base, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in {"__pycache__", ".git"} and not (exclude_site and name == "site-packages")]
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                pass
    return total


def inventory(stage):
    result = {"schema": "offline-python-inventory/v1", "runtime": {"path": str(DSH_PYTHON),
        "exists": DSH_PYTHON.is_dir(), "base_bytes_excluding_site_packages": directory_size(DSH_PYTHON, exclude_site=True)}, "environments": {}}
    for label, root in ENVIRONMENTS.items():
        site = root / "Lib/site-packages"
        packages = []
        for distribution in metadata.distributions(path=[str(site)]):
            name = distribution.metadata.get("Name")
            if not name:
                continue
            size = 0
            for file in distribution.files or []:
                path = distribution.locate_file(file)
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except OSError:
                    pass
            packages.append({"name": name, "version": distribution.version, "bytes": size,
                "requires": distribution.requires or [], "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License", ""),
                "metadata_dir": str(distribution._path)})
        result["environments"][label] = {"path": str(root), "packages": sorted(packages, key=lambda item: canonical(item["name"]))}
    cache = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "pip/Cache"
    wheels = [{"name": p.name, "path": str(p), "bytes": p.stat().st_size} for p in cache.rglob("*.whl")] if cache.is_dir() else []
    result["pip_cache"] = {"path": str(cache), "wheel_count": len(wheels), "wheels": wheels,
        "http_cache_bytes": directory_size(cache / "http-v2")}
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "inventory.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"runtime_base_mib": round(result["runtime"]["base_bytes_excluding_site_packages"] / 2**20, 1),
        "environments": {label: {"package_count": len(value["packages"]), "package_mib": round(sum(p["bytes"] for p in value["packages"]) / 2**20, 1),
            "roots": [{k: package[k] for k in ("name", "version", "bytes", "requires")} for package in value["packages"] if canonical(package["name"]) in {canonical(name) for name in ROOT_PACKAGES}]}
            for label, value in result["environments"].items()},
        "cached_wheels": len(wheels), "pip_http_cache_mib": round(result["pip_cache"]["http_cache_bytes"] / 2**20, 1)}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return result


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def require_owned(path):
    """All runtime mutations are confined to the explicitly owned staging root."""
    path = path.resolve()
    root = (APP / "data/offline-build").resolve()
    if path == root or root not in path.parents:
        raise ValueError(f"Expected a child of owned staging directory: {root}")
    return path


def unpack_wheels(stage):
    """Materialize downloaded pure wheels as another read-only metadata source."""
    target = require_owned(stage / "wheel-source")
    site = target / "Lib/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for wheel in sorted((stage / "wheelhouse").glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            for item in archive.infolist():
                rel = Path(item.filename)
                if rel.is_absolute() or ".." in rel.parts or ":" in item.filename or "\\" in item.filename:
                    raise ValueError(f"Unsafe wheel member: {item.filename}")
                if ".data" in rel.parts[0]:
                    raise ValueError("Wheel .data relocation requires an explicit installer; unsupported download")
                destination = site / rel
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(item))
    return target


def resolve(stage, *, profile="split", runtime_source=DSH_PYTHON):
    """Resolve only activated requirements/extras against installed metadata.

    This is deliberately an offline installed-artifact resolver, not a replacement
    for pip's general dependency solver. A missing/conflicting dependency is fatal.
    """
    from packaging.markers import default_environment
    from packaging.requirements import Requirement

    wheel_source = unpack_wheels(stage)
    sources = dict(ENVIRONMENTS, wheel=wheel_source)
    catalog = {}
    for source, root in sources.items():
        for dist in metadata.distributions(path=[str(root / "Lib/site-packages")]):
            name = dist.metadata.get("Name")
            if name:
                catalog.setdefault(canonical(name), []).append((source, root, dist))
    runtime_version = subprocess.run([str(runtime_source / "python.exe"), "-I", "-B", "-c", "import sys;print('.'.join(map(str,sys.version_info[:3])))"],
                                     check=True, capture_output=True, text=True, timeout=15).stdout.strip()
    if not runtime_version.startswith("3.12."):
        raise ValueError(f"The supported runtime is Windows x64 Python 3.12, got {runtime_version}")
    environment = default_environment()
    environment.update(python_version="3.12", python_full_version=runtime_version, sys_platform="win32",
                       platform_system="Windows", platform_machine="AMD64", os_name="nt",
                       implementation_name="cpython", platform_python_implementation="CPython")
    fixed_versions = {}
    unified_plan = None
    if profile == "minimal":
        unified_plan = json.loads((stage / "plan-unified.json").read_text(encoding="utf-8"))
        fixed_versions = {canonical(item["name"]): item["version"] for item in unified_plan["runtimes"]["unified"]["packages"]}

    def closure(label, roots):
        constraints, extras, selected = {}, {}, {}
        pending = [Requirement(value) for value in roots]
        priorities = ["workspace", "retrieval", "model", "wheel"] if label == "mcp" else ["retrieval", "model", "workspace", "wheel"]
        edges = set()
        while pending:
            requirement = pending.pop(0)
            name = canonical(requirement.name)
            specifiers = constraints.setdefault(name, set())
            old = (set(specifiers), set(extras.get(name, set())))
            specifiers.add(str(requirement.specifier))
            extras.setdefault(name, set()).update(requirement.extras)
            previous = selected.get(name)
            if previous and old == (specifiers, extras[name]):
                continue
            candidates = sorted(catalog.get(name, []), key=lambda item: priorities.index(item[0]))
            candidates = [candidate for candidate in candidates if all(candidate[2].version in Requirement(name + spec).specifier for spec in specifiers)]
            if profile == "minimal":
                if name not in fixed_versions:
                    raise ValueError(f"Minimal dependency {name} is missing from the verified unified lock")
                candidates = [candidate for candidate in candidates if candidate[2].version == fixed_versions[name]]
            if not candidates:
                raise ValueError(f"{label}: unavailable installed dependency {name} {sorted(specifiers)}")
            source, root, dist = candidates[0]
            selected[name] = (source, root, dist)
            for dependency in dist.requires or []:
                child = Requirement(dependency)
                if child.marker and not any(child.marker.evaluate(dict(environment, extra=extra)) for extra in {"", *extras[name]}):
                    continue
                edges.add((name, str(child)))
                pending.append(child)
        # Recheck every active edge after source/version choices have settled.
        for origin, value in edges:
            dependency = Requirement(value)
            candidate = selected[canonical(dependency.name)][2]
            if candidate.version not in dependency.specifier:
                raise ValueError(f"{label}: {origin} requires {dependency}, selected {candidate.version}")
        return selected, sorted(edges)

    closures, plan = {}, {"schema": "offline-python-plan/v1", "profile": profile, "platform": "win_amd64", "python": runtime_version, "runtime_source": str(runtime_source), "runtimes": {}}
    for label, roots in PROFILES[profile]:
        selected, edges = closure(label, roots)
        closures[label] = selected
        packages = []
        for name, (source, root, dist) in sorted(selected.items()):
            files = []
            for rel in dist.files or []:
                path = Path(dist.locate_file(rel))
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    files.append(path)
            packages.append({"name": dist.metadata["Name"], "version": dist.version, "source": source,
                             "bytes": sum(path.stat().st_size for path in files),
                             "license": dist.metadata.get("License-Expression") or dist.metadata.get("License", "")})
        estimated = directory_size(runtime_source, exclude_site=True) + sum(item["bytes"] for item in packages)
        plan["runtimes"][label] = {"directory": f"python-{label}", "roots": list(roots), "package_count": len(packages),
                                  "estimated_bytes": estimated, "estimated_mib": round(estimated / 2**20, 1),
                                  "packages": packages, "active_requirements": [{"from": a, "requirement": b} for a, b in edges]}
    plan["downloaded_wheels"] = [{"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)} for path in (stage / "wheelhouse").glob("*.whl")]
    if profile == "minimal":
        base_names = set(closures["minimal"])
        addon = [item for item in unified_plan["runtimes"]["unified"]["packages"] if canonical(item["name"]) not in base_names]
        plan["unified_lock"] = {"path": str(stage / "plan-unified.json"), "sha256": sha256(stage / "plan-unified.json")}
        plan["addon_pageindex"] = {"directory": "addon-pageindex", "package_count": len(addon), "packages": addon,
                                   "estimated_bytes": sum(item["bytes"] for item in addon), "contains_python": False}
    write_json(stage / ("plan.json" if profile == "split" else f"plan-{profile}.json"), plan)
    return closures, plan


def copy_runtime_base(source, target):
    if (source / "pyvenv.cfg").exists() or list(source.glob("*._pth")):
        raise ValueError("Runtime base must be a full standalone Python, without venv/_pth configuration")
    target.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc", "pyvenv.cfg"))
    (target / "Lib/site-packages").mkdir(parents=True, exist_ok=True)


def copy_distribution(root, dist, target, *, launcher_python="..\\python.exe"):
    files, skipped = [], []
    if not dist.files:
        raise ValueError(f"Distribution has no RECORD inventory: {dist.metadata['Name']}")
    root = root.resolve()
    for item in dist.files:
        source = Path(dist.locate_file(item)).resolve()
        if not source.is_relative_to(root):
            raise ValueError(f"RECORD escapes source environment: {dist.metadata['Name']}: {item}")
        rel = source.relative_to(root)
        if "__pycache__" in rel.parts or source.suffix == ".pyc":
            continue
        # pip's Windows launchers embed the source interpreter. Recreate console
        # entries below; Gmsh's native data script is handled explicitly as well.
        if rel.parts[0].lower() == "scripts" and source.suffix.lower() != ".py":
            skipped.append(str(rel))
            continue
        if not source.is_file():
            raise ValueError(f"Missing recorded package file: {source}")
        content = source.read_bytes()
        if item.hash and item.hash.mode == "sha256":
            digest = hashlib.sha256(content)
            actual = base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")
            # Gmsh's upstream RECORD uses hexadecimal hashes rather than wheel's
            # URL-safe Base64 convention. Both encode the same SHA-256 digest.
            if item.hash.value not in {actual, digest.hexdigest()}:
                raise ValueError(f"Source RECORD hash mismatch: {source}")
        original_hash = hashlib.sha256(content).hexdigest()
        if rel.parts[0].lower() == "scripts" and content.startswith(b"#!"):
            content = b"#!python\n" + content.split(b"\n", 1)[-1]
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() != content:
            raise ValueError(f"Conflicting package file: {rel}")
        destination.write_bytes(content)
        record = {"path": rel.as_posix(), "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        if original_hash != record["sha256"]:
            record.update(source_sha256=original_hash, modification="absolute console-script shebang replaced by #!python")
        files.append(record)
    entrypoints = []
    for entry in dist.entry_points:
        if entry.group != "console_scripts":
            continue
        if not entry.name.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise ValueError(f"Unsafe console entry name: {entry.name}")
        scripts = target / "Scripts"
        scripts.mkdir(exist_ok=True)
        body = ("import importlib.metadata as m\nimport sys\n"
                f"entry = next(e for e in m.distribution({dist.metadata['Name']!r}).entry_points if e.group == 'console_scripts' and e.name == {entry.name!r})\n"
                "sys.exit(entry.load()())\n")
        (scripts / f"{entry.name}-script.py").write_text(body, encoding="utf-8")
        (scripts / f"{entry.name}.cmd").write_text(f'@echo off\r\n"%~dp0{launcher_python}" "%~dp0{entry.name}-script.py" %*\r\n', encoding="utf-8")
        entrypoints.append({"name": entry.name, "value": entry.value, "generated": f"Scripts/{entry.name}.cmd", "relative_python": launcher_python})
    if canonical(dist.metadata['Name']) == "gmsh":
        scripts = target / "Scripts"
        scripts.mkdir(exist_ok=True)
        native = root / "Scripts/gmsh"
        if not native.is_file():
            raise ValueError("Gmsh's recorded native launcher is missing")
        shutil.copy2(native, scripts / "gmsh")
        (scripts / "gmsh.cmd").write_text('@echo off\r\n"%~dp0..\\python.exe" "%~dp0gmsh" %*\r\n', encoding="utf-8")
    license_files = [item for item in files if any(word in item["path"].lower() for word in ("license", "licence", "notice", "copyright", "copying"))]
    return {"name": dist.metadata['Name'], "version": dist.version, "source_environment": str(root), "files": files,
            "license": dist.metadata.get("License-Expression") or dist.metadata.get("License", ""),
            "license_files": license_files, "entrypoints": entrypoints, "replaced_source_launchers": skipped}


def verify_probe(label, runtime, result_path, mph=None, step=None, addon_root=None):
    """Run by the copied interpreter with -I -B; no source environments imported."""
    import importlib
    import importlib.util
    import socket
    import sqlite3
    import traceback

    result = {"schema": "offline-python-verification/v1", "runtime": str(runtime), "label": label,
              "checks": {}, "network_attempts": [], "environment_switches": {"LITELLM_LOCAL_MODEL_COST_MAP": "True", "PYTHON_DOTENV_DISABLED": "1"},
              "llm_requests": "not executed; provider inference still requires API/network", "passed": False}
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    # Audit covers Python network access even if libraries catch a rejection.
    def audit(event, args):
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto"}:
            result["network_attempts"].append(event)
            raise RuntimeError("Network disabled by offline verification audit hook")
    sys.addaudithook(audit)
    def check(name, callback):
        try:
            result["checks"][name] = {"passed": True, "result": callback()}
        except Exception as exc:
            result["checks"][name] = {"passed": False, "error": f"{type(exc).__name__}: {str(exc)[:1000]}", "traceback": traceback.format_exc(limit=6)}

    allowed_roots = [runtime.resolve()] + ([addon_root.resolve()] if addon_root else [])
    def isolation():
        assert sys.version_info[:2] == (3, 12), sys.version
        assert Path(sys.prefix).resolve() == runtime.resolve(), sys.prefix
        assert Path(sys.base_prefix).resolve() == runtime.resolve(), sys.base_prefix
        assert not (runtime / "pyvenv.cfg").exists()
        for value in sys.path:
            assert any(Path(value).resolve().is_relative_to(root) for root in allowed_roots), sys.path
        return {"executable": sys.executable, "version": sys.version, "prefix": sys.prefix, "base_prefix": sys.base_prefix, "sys_path": list(sys.path), "allowed_roots": [str(root) for root in allowed_roots]}
    check("isolated_runtime", isolation)
    def import_modules():
        modules = ["httpx", "mcp", "mcp.server.lowlevel", "mcp.server.stdio", "win32api", "pythoncom"]
        if label in {"core", "unified", "minimal", "minimal-addon"}:
            modules += ["sim", "simparse", "gmsh", "pypdf", "fastapi", "lxml", "pywinauto"]
        if label in {"core", "unified", "minimal-addon"}:
            modules += ["pageindex", "agents", "litellm", "pypdfium2"]
        imported = {}
        for name in modules:
            module = importlib.import_module(name)
            imported[name] = str(getattr(module, "__file__", "namespace"))
            if getattr(module, "__file__", None):
                assert any(Path(module.__file__).resolve().is_relative_to(root) for root in allowed_roots), imported[name]
        assert metadata.version("mcp").startswith("2." if label == "core" else "1.")
        versions = ["mcp", "httpx"]
        if label != "mcp":
            versions += ["sim-cli-core", "simparse", "gmsh", "pypdf"]
        if label in {"core", "unified", "minimal-addon"}:
            versions += ["PageIndex"]
        if label == "minimal":
            for unrelated in ("pageindex", "agents", "litellm", "botocore", "boto3", "torch", "pytest", "pandas", "numpy"):
                assert importlib.util.find_spec(unrelated) is None, f"Minimal-only verification unexpectedly sees {unrelated}"
        return {"modules": imported, "versions": {name: metadata.version(name) for name in versions}}
    check("imports", import_modules)
    def sqlite():
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE VIRTUAL TABLE docs USING fts5(text)")
        connection.execute("DROP TABLE docs")
        connection.close()
        return sqlite3.sqlite_version
    check("sqlite_fts5", sqlite)
    if label in {"core", "unified", "minimal", "minimal-addon"}:
        def pdf():
            from pypdf import PdfReader, PdfWriter
            buffer = io.BytesIO()
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=144)
            writer.write(buffer)
            assert len(PdfReader(io.BytesIO(buffer.getvalue())).pages) == 1
            if label == "minimal":
                return {"pages": 1, "size_points": [72, 144], "render": "optional PageIndex/pypdfium2 addon not installed"}
            import pypdfium2
            document = pypdfium2.PdfDocument(buffer.getvalue())
            page = document[0]
            size = list(page.get_size())
            bitmap = page.render(scale=1)
            dimensions = [bitmap.width, bitmap.height]
            bitmap.close(); page.close(); document.close()
            return {"pages": 1, "size_points": size, "render_pixels": dimensions}
        check("pdf_create_read" if label == "minimal" else "pdf_create_read_render", pdf)
        def scan(path, format_name):
            from click.testing import CliRunner
            from sim.cli import main
            before = sha256(path)
            cli = CliRunner().invoke(main, ["--json", "--port", "7600", "scan", str(path), "--full", "--no-recursive", "--limit", "1", "--format", format_name])
            assert cli.exit_code == 0, str(cli.exception) + cli.output[:500]
            payload = json.loads(cli.output)
            assert payload.get("ok") and len(payload["assets"]) == 1 and payload["assets"][0].get("ok"), payload
            assert sha256(path) == before, "Read-only input changed"
            asset = payload["assets"][0]
            summary = asset.get("summary", {})
            data = summary.get("data", {})
            # Verification proves a real native scan; it is not a redistribution
            # of the vendor model's saved configuration or tutorial content.
            counts = {key: len(data[key]) for key in ("physics_tags", "study_tags", "material_tags", "entries", "entity_type_counts", "products", "length_units") if isinstance(data.get(key), list)}
            return {"sha256": before, "format": asset.get("format"), "engine": payload.get("engine"),
                    "summary_kind": summary.get("kind"), "observed_counts": counts,
                    "records_observed": data.get("records_observed"), "truncated": payload.get("truncated", False)}
        if mph:
            check("mph_sim_cli_scan", lambda: scan(mph, "comsol-mph"))
        if step:
            check("step_sim_cli_scan", lambda: scan(step, "step"))
            def topology():
                import gmsh
                before = sha256(step)
                gmsh.initialize(["offline-check"], readConfigFiles=False)
                try:
                    gmsh.option.setNumber("General.Terminal", 0)
                    gmsh.open(str(step))
                    solids = gmsh.model.getEntities(3)
                    assert solids, "No 3D solids imported"
                    facts = {"solid_count": len(solids), "surface_count": len(gmsh.model.getEntities(2)),
                             "bbox": list(gmsh.model.getBoundingBox(-1, -1)),
                             "volume_model_units_cubed": sum(gmsh.model.occ.getMass(3, tag) for _, tag in solids),
                             "build_info": gmsh.option.getString("General.BuildInfo")}
                    assert sha256(step) == before
                    return facts
                finally:
                    gmsh.finalize()
            check("gmsh_step_topology_no_mesh", topology)
    check("isolated_runtime_after_imports", isolation)
    result["third_party_import_notes"] = ["LiteLLM proxy_cli appends os.getcwd() to sys.path; verification cwd is the portable runtime itself, not an original environment or harness directory."]
    # Deliberate negative control proves the block is active, without contacting
    # the address. Do not count this control as an unintended package request.
    before_control = len(result["network_attempts"])
    try:
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)
    except RuntimeError:
        result["checks"]["network_negative_control"] = {"passed": True, "blocked": True}
    except Exception as exc:
        result["checks"]["network_negative_control"] = {"passed": False, "error": type(exc).__name__}
    result["negative_control_audit_events"] = result["network_attempts"][before_control:]
    result["network_attempts"] = result["network_attempts"][:before_control]
    result["passed"] = all(item["passed"] for item in result["checks"].values()) and not result["network_attempts"]
    write_json(result_path, result)
    return result


def run_verification(stage, label, target, mph, step, suffix, *, addon_root=None):
    output = stage / f"verification-{label}-{suffix}.json"
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE"}}
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", LITELLM_LOCAL_MODEL_COST_MAP="True", PYTHON_DOTENV_DISABLED="1")
    startup_code = "import json,sys;print(json.dumps({'executable':sys.executable,'version':sys.version,'prefix':sys.prefix,'base_prefix':sys.base_prefix,'sys_path':sys.path}))"
    startup = subprocess.run([str(target / "python.exe"), "-I", "-B", "-c", startup_code], cwd=target, env=env,
                             capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    startup_result = json.loads(startup.stdout)
    allowed_roots = [target.resolve()] + ([addon_root.resolve()] if addon_root else [])
    startup_result["allowed_roots"] = [str(root) for root in allowed_roots]
    startup_result["passed"] = startup.returncode == 0 and all(any(Path(value).resolve().is_relative_to(root) for root in allowed_roots) for value in startup_result["sys_path"])
    startup_result["method"] = "independent python.exe -I -B -c; no verification-script import"
    write_json(stage / f"isolated-startup-{label}-{suffix}.json", startup_result)
    if not startup_result["passed"]:
        raise RuntimeError(f"Portable {label} independent -c startup leaked an external sys.path")
    command = [str(target / "python.exe"), "-I", "-B", str(Path(__file__).resolve()), "_verify", "--label", label, "--target", str(target), "--result", str(output)]
    if addon_root:
        command += ["--addon-root", str(addon_root)]
    for flag, value in (("--mph", mph), ("--step", step)):
        if value:
            command += [flag, str(value)]
    process = subprocess.run(command, cwd=target, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=150,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not output.is_file():
        raise RuntimeError(f"Portable {label} verification failed before report: exit {process.returncode}: {process.stderr.decode('utf-8', 'replace')[-2000:]}")
    value = json.loads(output.read_text(encoding="utf-8"))
    value["independent_startup"] = startup_result
    write_json(output, value)
    if process.returncode or not value.get("passed"):
        failures = {name: item.get("error") for name, item in value["checks"].items() if not item["passed"]}
        raise RuntimeError(f"Portable {label} verification failed: {failures}; network_attempts={value.get('network_attempts')}; see {output}")
    if label in {"unified", "minimal", "minimal-addon"}:
        probe_script = Path(__file__).with_name("verify_unified_sdk.py")
        sdk_output = stage / f"verification-sdk-{label}-{suffix}.json"
        if not probe_script.is_file():
            raise RuntimeError(f"Unified API/protocol compatibility probe is required: {probe_script}")
        sdk_work = Path(tempfile.mkdtemp(prefix=f"sdk-proof-{suffix}-", dir=stage))
        sdk_command = [str(target / "python.exe"), "-I", "-B", str(probe_script),
                       "--output", str(sdk_output), "--work-dir", str(sdk_work)]
        if label == "minimal":
            sdk_command += ["--base-only"]
        sdk_process = subprocess.run(sdk_command,
                                     cwd=target, env=env, capture_output=True, timeout=150,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        sdk_result = json.loads(sdk_output.read_text(encoding="utf-8")) if sdk_output.is_file() else {}
        if sdk_process.returncode or not (sdk_result.get("passed") is True or sdk_result.get("ok") is True):
            raise RuntimeError(f"Unified API/protocol compatibility probe failed (exit {sdk_process.returncode}); see {sdk_output}")
        value["sdk_api_and_protocol_compatibility"] = {"passed": True, "report": str(sdk_output)}
        write_json(output, value)
    return value


def build_report_path(stage, profile):
    return stage / ("build-report.json" if profile == "split" else f"build-report-{profile}.json")


def record_savings(stage, summary):
    previous = stage / "build-report.json"
    if "unified" in summary["runtimes"] and previous.is_file():
        split = json.loads(previous.read_text(encoding="utf-8"))
        split_bytes = sum(item["bytes"] for item in split.get("runtimes", {}).values())
        unified_bytes = summary["runtimes"]["unified"]["bytes"]
        summary["comparison"] = {"split_bytes": split_bytes, "unified_bytes": unified_bytes,
                                 "saved_bytes": split_bytes - unified_bytes,
                                 "saved_percent": round(100 * (split_bytes - unified_bytes) / split_bytes, 2)}


def addon_pth(target, addon):
    site = target / "Lib/site-packages"
    relative = os.path.relpath(addon / "Lib/site-packages", site).replace("\\", "/")
    path = site / "sim_advisor_addons.pth"
    path.write_text(relative + "\n", encoding="utf-8")
    return {"path": path.relative_to(target).as_posix(), "relative_directory": relative,
            "behavior": "plain relative directory; ignored by Python while addon is absent; no executable .pth code"}


def pruning_reason(name, relative):
    """Explicit development-only exclusions; never trim stdlib or simparse."""
    if canonical(name) == "simparse":
        return None
    path = Path(relative)
    lower = path.as_posix().lower()
    if any(word in lower for word in ("license", "licence", "notice", "copying", "copyright", "credits")):
        return None
    if canonical(name) == "gmsh":
        if lower in {"lib/gmsh.dll.lib", "lib/gmsh.jl"}:
            return "Gmsh developer link library / Julia binding; Python runtime uses unchanged Lib/gmsh-4.15.dll and gmsh.py"
        if lower.startswith("include/gmsh") or lower.startswith("share/gmsh/") and lower.endswith(".cmake"):
            return "Gmsh compilation headers / CMake developer configuration"
        if lower.startswith(("share/doc/gmsh/examples/", "share/doc/gmsh/tutorials/")):
            return "Gmsh development examples / tutorials; corresponding complete upstream source is supplied separately"
        if lower == "share/doc/gmsh/readme.txt":
            return "Gmsh SDK developer README; license, credits and change log retained"
    if lower.startswith("lib/site-packages/") and any(part.lower() in {"test", "tests"} for part in path.parts[2:-1]):
        return "Third-party distribution test directory; not an activated runtime dependency"
    return None


def prune_payload(stage, label):
    payload_name = "addon-pageindex" if label == "addon-pageindex" else f"python-{label}"
    payload = require_owned(stage.parent / payload_name)
    manifest_path = stage / f"manifest-{label}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Guards also document boundaries for future policy changes.
    assert pruning_reason("simparse", "Lib/site-packages/simparse/tests/x.py") is None
    assert pruning_reason("gmsh", "Lib/gmsh-4.15.dll") is None
    assert pruning_reason("gmsh", "share/doc/gmsh/examples/LICENSE.txt") is None
    assert pruning_reason("pypdfium2", "Lib/site-packages/pypdfium2_raw/BUILD_LICENSES/thirdparty.txt") is None
    assert pruning_reason("httpx", "Lib/site-packages/httpx/runtime.pyi") is None
    removed = list(manifest.get("pruning", {}).get("files", []))
    for package in manifest["packages"]:
        retained = []
        for record in package["files"]:
            reason = pruning_reason(package["name"], record["path"])
            if not reason:
                retained.append(record)
                continue
            path = require_owned(payload / record["path"])
            if not path.is_relative_to(payload.resolve()) or sha256(path) != record["sha256"]:
                raise ValueError(f"Pruning target escaped payload or changed since copying: {path}")
            removed.append(dict(record, distribution=package["name"], reason=reason))
            path.unlink()
        package["files"] = retained
    for base, dirs, files in os.walk(payload, topdown=False):
        for name in dirs:
            path = require_owned(Path(base) / name)
            try:
                path.rmdir()  # Only empty directories; never a recursive delete.
            except OSError:
                pass
    manifest["pruning"] = {"policy": "Explicit developer artifacts / package test directories only; no stdlib files or simparse distribution files removed",
                           "files": removed, "removed_bytes": sum(item["bytes"] for item in removed),
                           "license_policy": "All LICENSE/NOTICE/COPYING/COPYRIGHT/CREDITS/BUILD_LICENSES files are retained"}
    write_json(manifest_path, manifest)
    return {"directory": str(payload), "bytes": directory_size(payload), "removed_bytes": manifest["pruning"]["removed_bytes"], "removed_files": len(removed)}


def audit_pruning(stage):
    results = {}
    for label, folder in (("minimal", "python-minimal"), ("addon-pageindex", "addon-pageindex")):
        target = require_owned(stage.parent / folder)
        manifest = json.loads((stage / f"manifest-{label}.json").read_text(encoding="utf-8"))
        licenses, native, base = 0, 0, 0
        for package in manifest["packages"]:
            for record in package["license_files"]:
                assert sha256(target / record["path"]) == record["sha256"], record["path"]
                licenses += 1
            for record in package["files"]:
                if Path(record["path"]).suffix.lower() in {".dll", ".pyd"} or canonical(package["name"]) == "simparse":
                    assert sha256(target / record["path"]) == record["sha256"], record["path"]
                    native += 1
        for record in manifest.get("runtime_base_files", []):
            assert sha256(target / record["path"]) == record["sha256"], record["path"]
            base += 1
        for record in manifest.get("pruning", {}).get("files", []):
            assert pruning_reason(record["distribution"], record["path"]) is not None, record["path"]
            assert not (target / record["path"]).exists(), record["path"]
        results[label] = {"passed": True, "license_files_hashed": licenses, "native_and_simparse_files_hashed": native,
                          "python_base_files_hashed": base, "removed_bytes": manifest.get("pruning", {}).get("removed_bytes", 0)}
    write_json(stage / "pruning-audit.json", results)
    return results


def refresh_addon_launchers(stage):
    """Use the final bundle's python folder, with the named staging fallback."""
    addon = require_owned(stage.parent / "addon-pageindex")
    manifest_path = stage / "manifest-addon-pageindex.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for package in manifest["packages"]:
        for entry in package["entrypoints"]:
            script = addon / entry["generated"]
            body = ("@echo off\r\nsetlocal\r\n"
                    'set "SIM_ADVISOR_ADDON_PYTHON=%~dp0..\\..\\python\\python.exe"\r\n'
                    'if not exist "%SIM_ADVISOR_ADDON_PYTHON%" set "SIM_ADVISOR_ADDON_PYTHON=%~dp0..\\..\\python-minimal\\python.exe"\r\n'
                    f'"%SIM_ADVISOR_ADDON_PYTHON%" "%~dp0{entry["name"]}-script.py" %*\r\n')
            script.write_text(body, encoding="utf-8")
            entry["relative_python"] = ["../../python/python.exe", "../../python-minimal/python.exe"]
            entry["generated_sha256"] = sha256(script)
    write_json(manifest_path, manifest)


def verify_addon(stage, target, addon, mph, step):
    pth = addon_pth(target, addon)
    run_verification(stage, "minimal-addon", target, mph, step, "original", addon_root=addon)
    layout = require_owned(stage.parent / "relocation test minimal addon layout")
    layout.mkdir(exist_ok=False)
    # Exercise the shipping name as well as spaces in the enclosing directory.
    moved_target, moved_addon = layout / "python", layout / addon.name
    try:
        require_owned(target).rename(require_owned(moved_target))
        require_owned(addon).rename(require_owned(moved_addon))
        run_verification(stage, "minimal-addon", moved_target, mph, step, "relocated", addon_root=moved_addon)
    finally:
        if moved_addon.exists():
            moved_addon.rename(require_owned(addon))
        if moved_target.exists():
            moved_target.rename(require_owned(target))
        layout.rmdir()
    manifest_path = stage / "manifest-addon-pageindex.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"path": str(addon), "bytes": directory_size(addon), "package_count": len(manifest["packages"]),
            "contains_python": False, "uses_runtime": str(target), "manifest": str(manifest_path),
            "relative_pth": pth, "verified": True, "relocated_layout_with_spaces": True}


def build_pageindex_addon(stage, target, mph, step, *, replace=False):
    """Copy only the locked distribution set difference, never overlay a site tree."""
    addon = require_owned(stage.parent / "addon-pageindex")
    if addon.exists():
        if not replace:
            raise ValueError(f"Addon output already exists: {addon}")
        shutil.rmtree(require_owned(addon))
    addon.mkdir()
    unified = json.loads((stage / "plan-unified.json").read_text(encoding="utf-8"))
    minimal = json.loads((stage / "plan-minimal.json").read_text(encoding="utf-8"))
    common = {canonical(item["name"]): item["version"] for item in minimal["runtimes"]["minimal"]["packages"]}
    packages = []
    for item in unified["runtimes"]["unified"]["packages"]:
        name = canonical(item["name"])
        if name in common:
            if item["version"] != common[name]:
                raise ValueError(f"Addon would replace a base dependency: {name}")
            continue
        source = ENVIRONMENTS.get(item["source"])
        if source is None:
            source = stage / "wheel-source" if item["source"] == "wheel" else None
        if source is None:
            raise ValueError(f"Unknown locked addon source: {item['source']}")
        matches = [dist for dist in metadata.distributions(path=[str(source / "Lib/site-packages")])
                   if canonical(dist.metadata.get("Name", "")) == name and dist.version == item["version"]]
        if len(matches) != 1:
            raise ValueError(f"Locked addon distribution unavailable or ambiguous: {item['name']} {item['version']}")
        package = copy_distribution(source, matches[0], addon, launcher_python="..\\..\\python-minimal\\python.exe")
        package["source_label"] = item["source"]
        packages.append(package)
    manifest = {"schema": "offline-python-manifest/v1", "label": "addon-pageindex", "contains_python": False,
                "packages": packages, "common_dependencies": common, "unified_lock_sha256": sha256(stage / "plan-unified.json"),
                "activation": addon_pth(target, addon),
                "console_entrypoints": "relative launchers use the sibling python-minimal interpreter; no second Python"}
    write_json(stage / "manifest-addon-pageindex.json", manifest)
    refresh_addon_launchers(stage)
    prune_payload(stage, "addon-pageindex")
    return verify_addon(stage, target, addon, mph, step)


def verify_existing(stage, mph, step, *, profile="split"):
    summary = {"schema": "offline-python-build/v1", "profile": profile, "runtimes": {}, "all_passed": False}
    addon = require_owned(stage.parent / "addon-pageindex")
    hidden_addon = require_owned(stage.parent / "addon-pageindex verification hidden")
    # Prove the base works when the optional extension is physically absent.
    if profile == "minimal" and addon.exists():
        if hidden_addon.exists():
            raise ValueError(f"Temporary addon destination exists: {hidden_addon}")
        addon.rename(hidden_addon)
    try:
        return _verify_existing(stage, mph, step, profile=profile, summary=summary)
    finally:
        if profile == "minimal" and hidden_addon.exists():
            hidden_addon.rename(addon)


def _verify_existing(stage, mph, step, *, profile, summary):
    for label, _ in PROFILES[profile]:
        target = require_owned(stage.parent / f"python-{label}")
        run_verification(stage, label, target, mph, step, "original")
        relocated = require_owned(stage.parent / f"relocation test {label}")
        if relocated.exists():
            raise ValueError(f"Relocation test destination already exists: {relocated}")
        target.rename(relocated)
        try:
            run_verification(stage, label, relocated, mph, step, "relocated")
        finally:
            relocated.rename(target)
        manifest = json.loads((stage / f"manifest-{label}.json").read_text(encoding="utf-8"))
        summary["runtimes"][label] = {"path": str(target), "bytes": directory_size(target), "package_count": len(manifest["packages"]),
                                       "manifest": str(stage / f"manifest-{label}.json"), "verified": True, "relocated_with_spaces": True}
    if profile == "minimal":
        hidden = require_owned(stage.parent / "addon-pageindex verification hidden")
        addon = require_owned(stage.parent / "addon-pageindex")
        if hidden.exists():
            hidden.rename(addon)
        summary["addons"] = {"pageindex": verify_addon(stage, require_owned(stage.parent / "python-minimal"), addon, mph, step)}
        summary["pruning_audit"] = audit_pruning(stage)
    summary["all_passed"] = True
    record_savings(stage, summary)
    write_json(build_report_path(stage, profile), summary)
    return summary


def build(stage, runtime_source, mph, step, *, replace=False, profile="split"):
    stage = require_owned(stage)
    closures, plan = resolve(stage, profile=profile, runtime_source=runtime_source)
    summary = {"schema": "offline-python-build/v1", "profile": profile, "runtimes": {}, "all_passed": False}
    if profile == "minimal":
        addon = require_owned(stage.parent / "addon-pageindex")
        if addon.exists():
            if not replace:
                raise ValueError(f"Addon output already exists; pass --replace to rebuild: {addon}")
            shutil.rmtree(require_owned(addon))
    for label, selected in closures.items():
        target = require_owned(stage.parent / f"python-{label}")
        if target.exists():
            if not replace:
                raise ValueError(f"Output already exists; pass --replace to rebuild owned staging runtime: {target}")
            # Never delete a computed path without revalidating its absolute root.
            shutil.rmtree(require_owned(target))
        copy_runtime_base(runtime_source, target)
        if label == "minimal":
            addon_pth(target, require_owned(stage.parent / "addon-pageindex"))
        packages = []
        for _, (source, root, dist) in sorted(selected.items()):
            item = copy_distribution(root, dist, target)
            item["source_label"] = source
            packages.append(item)
        manifest = {"schema": "offline-python-manifest/v1", "label": label, "runtime_source": str(runtime_source),
                    "packages": packages, "runtime_base_files": [],
                    "console_entrypoints": "relative .cmd + importlib.metadata loaders; source absolute launchers replaced"}
        for path in sorted(runtime_source.rglob("*")):
            if path.is_file() and "site-packages" not in path.parts and "__pycache__" not in path.parts and path.suffix != ".pyc":
                manifest["runtime_base_files"].append({"path": path.relative_to(runtime_source).as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path)})
        write_json(stage / f"manifest-{label}.json", manifest)
        if label == "minimal":
            prune_payload(stage, label)
        # Detect .pth source-environment leakage before executing any copied code.
        for path in (target / "Lib/site-packages").glob("*.pth"):
            text = path.read_text(encoding="utf-8-sig")
            for line in text.splitlines():
                if line.strip() and not line.startswith(("#", "import ", "import\t")) and Path(line).is_absolute():
                    raise ValueError(f"Absolute .pth entry in portable payload: {path.name}")
        run_verification(stage, label, target, mph, step, "original")
        relocated = require_owned(stage.parent / f"relocation test {label}")
        if relocated.exists():
            raise ValueError(f"Relocation test destination already exists: {relocated}")
        target.rename(relocated)
        try:
            run_verification(stage, label, relocated, mph, step, "relocated")
        finally:
            relocated.rename(target)
        summary["runtimes"][label] = {"path": str(target), "bytes": directory_size(target), "package_count": len(packages),
                                       "manifest": str(stage / f"manifest-{label}.json"), "verified": True, "relocated_with_spaces": True}
        print(json.dumps(summary["runtimes"][label], ensure_ascii=False), flush=True)
    if profile == "minimal":
        summary["addons"] = {"pageindex": build_pageindex_addon(stage, require_owned(stage.parent / "python-minimal"), mph, step, replace=replace)}
        summary["pruning_audit"] = audit_pruning(stage)
    summary["all_passed"] = True
    record_savings(stage, summary)
    write_json(build_report_path(stage, profile), summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inventory", "plan", "build", "verify", "refresh-addon-launchers", "prune-minimal", "audit-minimal", "_verify"])
    parser.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--runtime-source", type=Path, default=DSH_PYTHON)
    parser.add_argument("--mph", type=Path)
    parser.add_argument("--step", type=Path)
    parser.add_argument("--label", choices=["core", "mcp", "unified", "minimal", "minimal-addon"])
    parser.add_argument("--profile", choices=list(PROFILES), default="split", help="Select split, unified, or minimal roots; unselected runtime outputs are untouched")
    parser.add_argument("--target", type=Path)
    parser.add_argument("--addon-root", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--replace", action="store_true", help="Rebuild only the owned runtime directories selected by --profile")
    args = parser.parse_args()
    if args.action == "inventory":
        inventory(require_owned(args.stage))
    elif args.action == "plan":
        _, plan = resolve(require_owned(args.stage), profile=args.profile, runtime_source=args.runtime_source.resolve())
        print(json.dumps({label: {key: value for key, value in item.items() if key not in {"packages", "active_requirements"}} for label, item in plan["runtimes"].items()}, ensure_ascii=False, indent=2))
    elif args.action == "build":
        build(args.stage, args.runtime_source.resolve(), args.mph.resolve() if args.mph else None, args.step.resolve() if args.step else None, replace=args.replace, profile=args.profile)
    elif args.action == "verify":
        value = verify_existing(require_owned(args.stage), args.mph.resolve() if args.mph else None, args.step.resolve() if args.step else None, profile=args.profile)
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.action == "refresh-addon-launchers":
        refresh_addon_launchers(require_owned(args.stage))
    elif args.action == "prune-minimal":
        stage = require_owned(args.stage)
        print(json.dumps([prune_payload(stage, label) for label in ("minimal", "addon-pageindex")], ensure_ascii=False, indent=2))
    elif args.action == "audit-minimal":
        print(json.dumps(audit_pruning(require_owned(args.stage)), ensure_ascii=False, indent=2))
    elif args.action == "_verify":
        if not all((args.label, args.target, args.result)):
            parser.error("_verify requires --label, --target, --result")
        value = verify_probe(args.label, args.target.resolve(), args.result.resolve(), args.mph, args.step, args.addon_root)
        raise SystemExit(0 if value["passed"] else 1)


if __name__ == "__main__":
    main()
