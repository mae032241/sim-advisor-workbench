"""Lifecycle and default directory scope at the public HTTP/CLI boundary."""
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from advisor.library_store import LibraryStore, LibraryLifecycleConflict
from advisor.server import make_server
import library_cli


@pytest.fixture
def library_server(tmp_path):
    sources = tmp_path / "registered"
    sources.mkdir()
    for number in range(14):
        (sources / f"case-{number:02}.mph").write_bytes(b"discovery fixture, not parsed")
    store = LibraryStore(tmp_path / "workspace/libraries")
    lib = store.register("Cases", "user-models", [{"id": "main", "path": str(sources), "role": "models"}], library_id="cases")
    lib = store.set_enabled(lib["id"], True)
    app = SimpleNamespace(libraries=store, kb=SimpleNamespace(status=lambda: {}))
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield store, f"http://127.0.0.1:{server.server_port}", sources
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request(base, route, body=None):
    request = Request(base + route, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def test_model_discovery_lists_every_registered_file_without_building_or_hashing(library_server):
    store, base, sources = library_server
    with patch("advisor.library_store.file_hash", side_effect=AssertionError("Discovery must not hash MPH files")), patch.object(store, "start_build", side_effect=AssertionError("Discovery is read-only")):
        code, result = request(base, "/api/libraries/cases/models")
    assert code == 200 and len(result["files"]) == 14
    assert result["limit"] is None and result["has_more"] is False
    assert all(Path(item["path"]).parent == sources for item in result["files"])
    assert store.jobs() == []


def test_ui_build_default_scope_has_neither_path_override_nor_ten_file_limit(library_server):
    store, base, _ = library_server
    with patch.object(store, "start_build", return_value={"id": "a" * 32, "status": "queued"}) as build:
        code, _ = request(base, "/api/libraries/cases/build", {"modes": ["native"], "deep": True, "enrich": False})
    assert code == 202
    assert build.call_args.kwargs["paths"] is None
    assert build.call_args.kwargs["limit"] is None
    assert build.call_args.kwargs["model_format"] == "compact"


@pytest.mark.parametrize("deep", [False, True])
def test_web_extraction_choice_reaches_constructor(library_server, deep):
    store, base, _ = library_server
    with patch.object(store, "start_build", return_value={"id": "b" * 32, "status": "queued"}) as build:
        code, _ = request(base, "/api/libraries/cases/build", {"deep": deep, "enrich": False, "modes": ["native"]})
    assert code == 202 and build.call_args.kwargs["deep"] is deep
    assert build.call_args.kwargs["paths"] is None and build.call_args.kwargs["limit"] is None


def test_disable_delete_restore_are_reversible_and_keep_sources(library_server):
    store, base, sources = library_server
    assert request(base, "/api/libraries/cases/enable", {"enabled": False})[1]["enabled"] is False
    assert request(base, "/api/libraries/cases/delete", {})[1]["deleted_at"]
    assert request(base, "/api/libraries")[1]["libraries"] == []
    assert len(request(base, "/api/libraries?include_deleted=1")[1]["libraries"]) == 1
    assert request(base, "/api/libraries/cases/models")[0] == 404
    restored = request(base, "/api/libraries/cases/restore", {})[1]
    assert restored["deleted_at"] is None and restored["enabled"] is False
    assert request(base, "/api/libraries/cases/enable", {"enabled": True})[1]["enabled"] is True
    assert len(list(sources.glob("*.mph"))) == 14
    assert request(base, "/api/libraries/cases/enable", {"enabled": "yes"})[0] == 422


def test_busy_library_change_returns_conflict(library_server):
    store, base, _ = library_server
    with patch.object(store, "delete_library", side_effect=LibraryLifecycleConflict("先中断构建")):
        code, body = request(base, "/api/libraries/cases/delete", {})
    assert code == 409 and "中断" in body["error"]


def test_scope_http_is_metadata_only_and_batch_enabled_state_persists(library_server):
    store, base, sources = library_server
    with patch.object(store, 'jobs', side_effect=AssertionError('Scope must not load jobs')), patch.object(store, 'index_status', side_effect=AssertionError('Scope must not load indexes')):
        code, scope = request(base, '/api/library-scope?query=Cases&type=user-models')
    assert code == 200 and scope['library_count'] == 1
    assert scope['groups'][1]['libraries'][0]['id'] == 'cases'
    code, result = request(base, '/api/libraries/enable', {'library_ids': ['cases', 'cases'], 'enabled': False})
    assert code == 200 and result['changed'] == 1 and result['library_ids'] == ['cases']
    assert result['index_rebuilt'] is False
    assert LibraryStore(store.root).searchable_library_ids() == []
    assert request(base, '/api/libraries/enable', {'library_ids': ['cases'], 'enabled': True})[1]['changed'] == 1
    assert len(list(sources.glob('*.mph'))) == 14 and store.jobs() == []


def test_scope_http_rejects_unknown_ids_invalid_payload_and_active_owner_atomically(library_server):
    import os
    store, base, _ = library_server
    assert request(base, '/api/libraries/enable', {'library_ids': ['cases', 'missing'], 'enabled': False})[0] == 404
    assert store.library('cases')['enabled'] is True
    assert request(base, '/api/libraries/enable', {'library_ids': ['cases'], 'enabled': 'false'})[0] == 422
    assert request(base, '/api/libraries/enable', {'library_ids': ['cases'], 'enabled': False, 'rebuild': True})[0] == 422
    assert request(base, '/api/library-scope?type=unknown')[0] == 422
    store._save_job({'id': 'active', 'library_id': 'cases', 'status': 'running', 'pid': os.getpid()})
    assert request(base, '/api/libraries/enable', {'library_ids': ['cases'], 'enabled': False})[0] == 409
    assert store.library('cases')['enabled'] is True


@pytest.mark.parametrize('library_type', ['user-models', 'user-manuals'])
def test_http_registration_starts_disabled_and_cannot_queue_build_until_enabled(library_server, library_type):
    store, base, sources = library_server
    code, library = request(base, '/api/libraries', {'name': 'New module', 'type': library_type,
                                                  'roots': [{'id': 'models', 'path': str(sources)}]})
    assert code == 201 and library['enabled'] is False
    assert store.library(library['id'])['enabled'] is False
    assert request(base, f"/api/libraries/{library['id']}/build", {'modes': ['native']})[0] == 422
    assert store.jobs() == []
    assert request(base, '/api/libraries/enable', {'library_ids': [library['id']], 'enabled': True})[0] == 200
    assert store.library(library['id'])['enabled'] is True


def test_purge_requires_confirmation_and_deleted_library(library_server):
    store, base, sources = library_server
    route = "/api/libraries/cases/purge"
    assert request(base, route, {})[0] == 422
    assert request(base, route, {"confirmed": "yes"})[0] == 422
    assert request(base, route, {"confirmed": True})[0] in (409, 422)
    request(base, "/api/libraries/cases/delete", {})
    code, result = request(base, route, {"confirmed": True})
    assert code == 200 and result["status"] == "purged"
    assert request(base, "/api/libraries?include_deleted=1")[1]["libraries"] == []
    assert request(base, "/api/libraries/cases/restore", {})[0] == 404
    assert len(list(sources.glob("*.mph"))) == 14


def test_cli_purge_requires_explicit_confirm_flag(tmp_path, capsys):
    store = Mock()
    store.purge_library.return_value = {"library_id": "cases", "status": "purged"}
    with patch("sys.argv", ["library_cli.py", "--data-dir", str(tmp_path), "purge", "cases", "--confirm"]), patch("library_cli.LibraryStore", return_value=store):
        assert library_cli.main() == 0
    store.purge_library.assert_called_once_with("cases", confirmed=True)


@pytest.mark.parametrize("action,method,args", [("disable", "set_enabled", ("cases", False)), ("enable", "set_enabled", ("cases", True)),
                                              ("delete", "delete_library", ("cases",)), ("restore", "restore_library", ("cases",))])
def test_cli_lifecycle_dispatch_is_explicit(action, method, args, tmp_path, capsys):
    store = Mock()
    getattr(store, method).return_value = {"id": "cases"}
    with patch("sys.argv", ["library_cli.py", "--data-dir", str(tmp_path), action, "cases"]), patch("library_cli.LibraryStore", return_value=store):
        assert library_cli.main() == 0
    getattr(store, method).assert_called_once_with(*args)
    assert json.loads(capsys.readouterr().out)["id"] == "cases"


@pytest.mark.parametrize("model_format", [None, "legacy"])
def test_cli_build_selects_compact_unless_legacy_explicit(model_format, tmp_path, capsys):
    store = Mock()
    store.build.return_value = {"status": "completed"}
    argv = ["library_cli.py", "--data-dir", str(tmp_path), "build", "cases", "--shallow"]
    if model_format:
        argv.extend(["--model-format", model_format])
    with patch("sys.argv", argv), patch("library_cli.LibraryStore", return_value=store):
        assert library_cli.main() == 0
    assert store.build.call_args.kwargs["model_format"] == (model_format or "compact")
    assert store.build.call_args.kwargs["deep"] is False
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
