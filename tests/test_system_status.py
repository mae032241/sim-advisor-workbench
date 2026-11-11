"""Regression: a legacy receipt must not hide ready registered-library indexes."""
import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from advisor.server import App
from advisor.system_status import services_status


def corpus(keyword=False, vector=False, native=True, installed=True, vector_state=None):
    return {"backends": {"qmd": {"available": False, "installed": installed, "mode": "installed-unverified"},
                         "pageindex": {"available": False, "installed": False, "mode": "not-installed"}},
            "library_indexes": {"ready": native or keyword or vector, "building": False,
                "dependencies": {"qmd_available": installed},
                "modes": {"native": {"ready": native, "state": "ready" if native else "not_built"},
                          "qmd_keyword": {"ready": keyword, "state": "ready" if keyword else "not_built"},
                          "qmd_vector": {"ready": vector, "state": vector_state or ("ready" if vector else "not_built")}},
                "libraries": {"large": {"objects": list(range(10000))}}}}


def report(value):
    return services_status(value, {"available": True}, {"available": False, "mode": "disabled"})


def test_ready_keyword_and_interrupted_vector_override_missing_legacy_receipt():
    source = corpus(keyword=True, vector_state="interrupted")
    before = copy.deepcopy(source)
    result = report(source)
    qmd = result["qmd"]
    assert qmd["available"] and qmd["status_label"] == "关键词可用"
    assert qmd["legacy"]["available"] is False
    assert qmd["library_indexes"]["modes"]["qmd_vector"] == {"ready": False, "state": "interrupted"}
    assert "libraries" not in qmd["library_indexes"]
    assert source == before  # Retrieval still sees its original legacy receipt.


@pytest.mark.parametrize("keyword,vector,label", [(True, True, "关键词与向量可用"), (False, True, "向量可用")])
def test_modern_qmd_modes_are_reported_independently(keyword, vector, label):
    qmd = report(corpus(keyword=keyword, vector=vector))["qmd"]
    assert qmd["available"] and qmd["status_label"] == label
    assert qmd["semantic_available"] is vector


def test_native_index_alone_does_not_claim_qmd_is_ready():
    qmd = report(corpus())["qmd"]
    assert qmd["component_available"] and not qmd["available"]
    assert qmd["library_indexes"]["modes"]["native"]["ready"]


def test_missing_components_and_legacy_only_corpus_remain_distinct():
    qmd = report(corpus(keyword=True, installed=False))["qmd"]
    assert not qmd["available"] and qmd["status_label"] == "组件未配置"
    source = corpus()
    source["backends"]["qmd"].update(available=True, mode="qmd-keyword", document_count=5)
    qmd = report(source)["qmd"]
    assert qmd["available"] and qmd["status_label"] == "旧手册关键词可用"
    assert qmd["legacy"]["document_count"] == 5


def test_optional_pageindex_and_disabled_managed_agent_do_not_deny_external_agent():
    source = corpus()
    result = report(source)
    assert result["pageindex"]["status_label"] == "可选扩展未安装"
    assert result["planner"]["status_label"] == "托管后端已关闭"
    assert "外部" in result["planner"]["detail"]
    assert "connected" not in result["planner"]
    source["backends"]["pageindex"]["installed"] = True
    assert report(source)["pageindex"]["status_label"] == "待配置 / 核验"


def test_status_api_uses_registered_library_report_without_building_or_starting_agents():
    value = corpus(keyword=True, vector_state="interrupted")
    app = SimpleNamespace(kb=SimpleNamespace(status=lambda: value),
                          agents=SimpleNamespace(backend_status=lambda: {"available": False, "mode": "disabled"}),
                          sessions=SimpleNamespace(list=lambda: []))
    with patch("advisor.server.parser_status", return_value={"available": True}), patch("advisor.server.config.examples", return_value=[]):
        result = App.status(app)
    assert result["services"]["qmd"]["available"]
    assert result["corpus"]["backends"]["qmd"]["available"] is False
