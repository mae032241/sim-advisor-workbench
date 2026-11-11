"""The UI needs progress and readiness, not entire retrieval manifests."""
import json
import threading
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

from advisor.server import make_server
from advisor.ui_status import status_summary


def manifest():
    return {"ready": True, "building": False, "generation": "gen-1", "object_count": 300,
            "modes": {"qmd_keyword": {"ready": True}, "qmd_vector": {"ready": False, "state": "interrupted"}},
            "libraries": {"models": {"key": "models", "corpus": "relative/corpus"}},
            "mapping": {str(i): "reference" * 100 for i in range(300)},
            "representation": {str(i): "configuration" * 100 for i in range(300)}}


def test_summary_skips_bulk_before_traversal_and_keeps_status_without_mutation():
    class Unreadable(dict):
        def items(self):
            raise AssertionError("Bulk mapping should not be traversed or copied")
    value = manifest()
    value["mapping"] = Unreadable(value["mapping"])
    value["last_build"] = {"status": "partial", "errors": ["unverified property"],
                           "representation": Unreadable(), "progress": {"completed": 3, "total": 10}}
    compact = status_summary(value)
    assert compact["ready"] is True and compact["object_count"] == 300
    assert compact["modes"] == value["modes"] and compact["libraries"] == value["libraries"]
    assert compact["last_build"] == {"status": "partial", "errors": ["unverified property"],
                                     "progress": {"completed": 3, "total": 10}}
    assert "mapping" not in compact and "representation" not in compact
    assert "mapping" in value and "representation" in value["last_build"]


@pytest.fixture
def status_server():
    index = manifest()
    index["last_build"] = manifest()
    job = {"id": "a" * 32, "status": "partial", "indexes": index,
           "items": [{"status": "partial", "error": "unverified"}],
           "progress": {"completed": 3, "total": 10}}
    app = SimpleNamespace(status=lambda: {"ready": True, "corpus": {"library_indexes": index}, "sessions": []},
        libraries=SimpleNamespace(libraries=lambda **_: [{"id": "models", "enabled": True}],
                                  jobs=lambda: [job], job=lambda _: job),
        kb=SimpleNamespace(status=lambda: {"library_indexes": index}))
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("route", ["/api/status", "/api/libraries", "/api/library-jobs/" + "a" * 32])
def test_compact_http_view_preserves_full_api_and_reduces_payload(status_server, route):
    def read(suffix=""):
        with urlopen(status_server + route + suffix, timeout=5) as response:
            return response.read()
    original = read()
    compact = read("?view=summary")
    assert len(compact) < len(original) / 100
    assert json.loads(compact) == status_summary(json.loads(original))
    assert b'"mapping"' in original and b'"representation"' in original
    assert b'"mapping"' not in compact and b'"representation"' not in compact
    assert read() == original  # Summary must not mutate a reused manifest.
