"""Verify the actual portable artifact contains code and excludes user state."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_dsh_workbench", APP / "scripts/build_dsh_workbench.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_html_tree_release_sources_are_complete_without_building_archive():
    sources = builder.collect_sources()
    required = {'runtime/advisor/html_tree.py', 'runtime/advisor/html_tree_tools.py',
                'runtime/advisor/html_tree_store.py', 'runtime/advisor/research_policy.py',
                'runtime/scripts/build_html_tree.py', 'runtime/docs/html-manual-tree.md',
                'runtime/prompts/comsol_planner.v1.txt', 'bridge/index.js'}
    assert required.issubset(sources)
    assert all(sources[name].is_file() for name in required)
    assert 'runtime/scripts/benchmark_html_tree.py' not in sources
    assert not any(name.endswith(('.sqlite3', '.mph', '.pdf', '.gguf')) for name in sources)
    manifest = json.loads((builder.BUNDLE / 'package.json').read_text(encoding='utf-8'))
    assert manifest['version'] == '0.2.68' and not manifest.get('dependencies')
    assert manifest['engines']['node'] == '>=24.9.0'


def test_case_tree_source_manifest_includes_algorithms_and_no_local_navigation_data():
    sources = builder.collect_sources()
    required = {'runtime/advisor/case_tree.py', 'runtime/advisor/case_manual_links.py',
                'runtime/scripts/build_case_tree.py', 'runtime/docs/case-tree-navigation.md'}
    assert required.issubset(sources)
    assert all(sources[name].is_file() for name in required)
    assert not any(name.endswith(('.sqlite3', '.mph', '.gguf')) for name in sources)
    assert 'runtime/scripts/benchmark_route_catalog.py' not in sources


def test_distribution_is_deterministic_complete_and_has_no_user_state(tmp_path):
    first = builder.build(tmp_path / "first")
    second = builder.build(tmp_path / "second")
    assert first["sha256"] == second["sha256"]
    assert first["verified_source_match"]
    with tarfile.open(tmp_path / "first" / first["archive"]) as archive:
        names = {member.name for member in archive.getmembers()}
        assert all(member.isfile() for member in archive.getmembers())
        assert all(not set(Path(name).parts).intersection({"data", ".tools", "__pycache__", ".model-env", ".venv"}) for name in names)
        vendor = json.load(archive.extractfile("package/vendor/VENDOR.json"))
        allowed_vendor = {"package/vendor/" + entry["path"]: entry for entry in vendor["files"]}
        actual_vendor = {name for name in names if "node_modules" in Path(name).parts}
        assert actual_vendor == set(allowed_vendor)
        for name, entry in allowed_vendor.items():
            value = archive.extractfile(name).read()
            assert len(value) == entry["bytes"]
            assert hashlib.sha256(value).hexdigest() == entry["sha256"]
        package = json.load(archive.extractfile("package/package.json"))
        assert package["name"] == "@local/sim-advisor-workbench"
        assert not package.get("dependencies")
        assert package["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
        assert ET.fromstring(archive.extractfile("package/icon.svg").read()).tag.endswith("svg")
        for locale in ("zh", "en"):
            meta = json.load(archive.extractfile(f"package/locale/{locale}.json"))["meta"]
            assert meta["title"] and meta["description"]
        contents = json.load(archive.extractfile("package/CONTENTS.json"))
        for entry in contents["files"]:
            value = archive.extractfile("package/" + entry["path"]).read()
            assert hashlib.sha256(value).hexdigest() == entry["sha256"]
        assert "package/runtime/scripts/mph_extract/ReadMph.java" in names
        assert "package/runtime/integrations/dsh-managed-host/index.js" in names


def test_relocated_runtime_imports_with_external_data_and_no_source_environment(tmp_path):
    result = builder.build(tmp_path / "archive")
    relocated = tmp_path / "another device with spaces"
    relocated.mkdir()
    with tarfile.open(tmp_path / "archive" / result["archive"]) as archive:
        archive.extractall(relocated, filter="data")
    runtime = relocated / "package/runtime"
    data = tmp_path / "persistent data"
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "SIM_ADVISOR_DATA": str(data),
                   "SIM_ADVISOR_AGENT_BACKEND": "none", "PYTHONPATH": str(runtime), "PYTHONUTF8": "1"}
    probe = subprocess.run([sys.executable, "-c", "import json,sqlite3; from advisor.config import ROOT,DATA; import advisor.server; "
        "db=sqlite3.connect(':memory:'); db.execute('create virtual table check_fts using fts5(body)'); "
        "print(json.dumps({'root':str(ROOT),'data':str(DATA)}))"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert probe.returncode == 0, probe.stderr
    observed = json.loads(probe.stdout)
    assert Path(observed["root"]) == runtime
    assert Path(observed["data"]) == data
    assert not (runtime / "data").exists()
    assert not list(runtime.rglob("__pycache__"))
