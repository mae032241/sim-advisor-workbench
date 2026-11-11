"""The launcher probe is independent of expensive or broken corpus status."""
import json
import os
from types import SimpleNamespace
import threading
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from advisor.server import make_server


@pytest.fixture
def health_server(tmp_path):
    forbidden = Mock(side_effect=AssertionError("Health must not inspect application state"))
    app = SimpleNamespace(data_dir=tmp_path / "data with spaces", status=forbidden,
                          kb=SimpleNamespace(status=forbidden, build=forbidden),
                          sessions=SimpleNamespace(list=forbidden))
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    with patch("advisor.server.parser_status", forbidden):
        thread.start()
        try:
            yield app, forbidden, f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


def request(base, headers=None):
    try:
        with urlopen(Request(base + "/api/health", headers=headers or {}), timeout=3) as response:
            return response.status, response.read(), response.headers
    except HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def test_health_is_small_and_never_reads_status_or_builds(health_server):
    app, forbidden, base = health_server
    code, raw, headers = request(base)
    assert code == 200
    assert len(raw) < 1024
    assert json.loads(raw) == {"ready": True, "pid": os.getpid(), "data_dir": str(app.data_dir.resolve()),
                               "service": "sim-advisor-workbench", "protocol_version": 1}
    assert headers["Cache-Control"] == "no-store"
    forbidden.assert_not_called()
    assert not app.data_dir.exists()


@pytest.mark.parametrize("headers", [{"Host": "remote.example"}, {"Origin": "https://remote.example"}])
def test_health_keeps_loopback_host_and_origin_policy(health_server, headers):
    _, forbidden, base = health_server
    assert request(base, headers)[0] == 403
    forbidden.assert_not_called()


def test_health_accepts_same_origin(health_server):
    _, forbidden, base = health_server
    assert request(base, {"Origin": base})[0] == 200
    forbidden.assert_not_called()
