"""Only the same-origin management UI can change the evaluation retrieval gate."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from advisor.planning_service import tool_definitions
from advisor.chat_bridge_tools import external_tool_definitions
from advisor.server import make_server


@pytest.fixture
def evaluation_server():
    summary = {"schema": "evaluation-set-summaries/v1", "sets": [{"id": "diverse", "name": "跨方向测试集",
        "model_count": 1, "models": [{"title": "换热器", "direction": "传热与流动"}]}],
        "set_count": 1, "model_count": 1, "read_only": True, "index_rebuilt": False}
    store = Mock(spec=["evaluation_set_summaries", "set_evaluation_retrieval_enabled", "set_enabled", "start_build", "delete_library", "register"])
    store.evaluation_set_summaries.return_value = summary
    store.set_evaluation_retrieval_enabled.side_effect = lambda enabled: dict(summary, retrieval_enabled=enabled)
    server = make_server(SimpleNamespace(libraries=store), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield store, summary, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request(base, suffix="", body=None, *, origin=None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if origin is not None:
        headers["Origin"] = origin
    req = Request(base + "/api/libraries/evaluation-sets" + suffix,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers=headers)
    try:
        with urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def test_evaluation_ui_get_reads_only_the_dedicated_summary(evaluation_server):
    store, summary, base = evaluation_server
    status, result = request(base)
    assert status == 200 and {key: value for key, value in result.items() if key != 'access_token'} == summary
    assert len(result['access_token']) == 64
    assert store.method_calls == [("evaluation_set_summaries", (), {})]


@pytest.mark.parametrize("suffix", ["", "/enable", "/build", "/delete", "/restore", "/purge", "/rebind"])
def test_evaluation_ui_paths_cannot_mutate_or_dispatch_to_library_actions(evaluation_server, suffix):
    store, _, base = evaluation_server
    status, result = request(base, suffix, {"enabled": True, "library_ids": ["diverse"], "confirmed": True})
    assert status == 405 and "只读" in result["error"]
    assert store.method_calls == []


def test_evaluation_summary_does_not_add_an_agent_tool():
    names = {item["name"] for item in tool_definitions() + external_tool_definitions()}
    assert not any("evaluation" in name for name in names)


def test_only_the_web_access_route_can_toggle_with_same_origin_and_token(evaluation_server):
    store, _, base = evaluation_server
    _, summary = request(base)
    token = summary['access_token']
    store.reset_mock()
    for body, origin in (({'enabled': True}, base),
                         ({'enabled': True, 'access_token': '0' * 64}, base),
                         ({'enabled': True, 'access_token': token}, None),
                         ({'enabled': True, 'access_token': token}, 'https://example.com')):
        assert request(base, '/access', body, origin=origin)[0] == 403
    assert store.method_calls == []
    for enabled in (True, False):
        status, value = request(base, '/access', {'enabled': enabled, 'access_token': token}, origin=base)
        assert status == 200 and value['retrieval_enabled'] is enabled
    assert store.method_calls == [('set_evaluation_retrieval_enabled', (True,), {}),
                                  ('set_evaluation_retrieval_enabled', (False,), {})]
