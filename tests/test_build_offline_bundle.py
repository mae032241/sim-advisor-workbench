import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/build_offline_bundle.py"
spec = importlib.util.spec_from_file_location("offline_bundle_builder", SOURCE)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def prepared(tmp_path, monkeypatch):
    app, root, dist, assets = (tmp_path / name for name in ("app", "prepared", "dist", "assets"))
    for directory in (app, root, dist, assets):
        directory.mkdir()
    files = {
        "python-minimal/python.exe": b"python", "qmd-minimal/node/node.exe": b"node",
        "qmd-minimal/node_modules/@tobilu/qmd/index.js": b"qmd",
        "licenses/gemma/TERMS.txt": b"model terms", "sources/source.txt": b"source",
        "prerequisites/vc_redist.x64.exe": b"redistributable",
        "python/manifest-minimal.json": b"{}", "python/build-report-minimal.json": b"{}",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    package = app / "integrations/dsh-sim-workbench/package.json"
    package.parent.mkdir(parents=True)
    package.write_text('{"version":"9.8.7"}', encoding="utf-8")
    verifier = app / "scripts/verify_offline_qmd.mjs"
    verifier.parent.mkdir()
    verifier.write_text("// verifier", encoding="utf-8")
    for name, value in (("APP", app), ("PREPARED", root), ("DIST", dist), ("ASSETS", assets)):
        monkeypatch.setattr(builder, name, value)
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout='{"version":"v24","platform":"win32","arch":"x64"}'))
    return app, root, dist


def test_default_base_has_one_python_no_weights_and_never_reads_model_receipt(tmp_path, monkeypatch):
    app, _, _ = prepared(tmp_path, monkeypatch)
    assert not (app / ".tools").exists()
    stage = builder.assemble(include_plugin=False)
    lock = json.loads((stage / "versions.lock.json").read_text())
    assert lock["bundle_version"] == "0.2.3"
    assert lock["workbench_plugin"] == "9.8.7"
    assert lock["embedding"]["weights_included"] is False
    assert lock["embedding"]["enabled_by_default"] is False
    assert list(stage.rglob("python.exe")) == [stage / "python/python.exe"]
    assert not list(stage.rglob("*.gguf"))
    assert not (stage / "qmd/node").exists()


def test_historical_stage_is_refused_before_any_modification(tmp_path, monkeypatch):
    _, root, _ = prepared(tmp_path, monkeypatch)
    stage = root / "legacy"
    stage.mkdir()
    path = stage / "versions.lock.json"
    path.write_text('{"workbench_plugin":"0.2.2"}', encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(builder, "refresh_plugin", lambda *_: pytest.fail("Historical stage must not be refreshed"))
    with pytest.raises(ValueError, match="historical"):
        builder.package(stage)
    assert path.read_bytes() == before


def test_optional_weights_archive_is_separate_and_does_not_enable_embedding(tmp_path, monkeypatch):
    _, _, dist = prepared(tmp_path, monkeypatch)
    model = tmp_path / "local.gguf"
    model.write_bytes(b"GGUF-local-test-weights")
    monkeypatch.setattr(builder, "MODEL_BYTES", model.stat().st_size)
    monkeypatch.setattr(builder, "MODEL_SHA", builder.sha(model))
    result = builder.package_embedding_addon(model)
    assert result["enabled_by_default"] is False
    with zipfile.ZipFile(dist / result["archive"]) as archive:
        names = archive.namelist()
        assert sum(name.endswith('.gguf') for name in names) == 1
        assert not any(name.endswith(('python.exe', 'node.exe')) for name in names)
        contents = json.loads(archive.read(builder.EMBEDDING_NAME + "/CONTENTS.json"))
        assert contents["enabled_by_default"] is False
        assert any(name.endswith('/licenses/gemma/TERMS.txt') for name in names)
