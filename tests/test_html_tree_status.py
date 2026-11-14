from types import SimpleNamespace
from unittest.mock import patch

from advisor.system_status import html_tree_status, services_status
from advisor.server import html_tree_library_summaries


def test_native_tree_status_is_small_and_independent_of_optional_engines():
    tree = {"ready": True, "tree_count": 2, "enabled_tree_count": 1, "node_count": 100,
            "document_count": 90, "nodes": list(range(20000)), "directory": "private"}
    result = services_status({"html_tree": tree}, {}, {})["html_tree"]
    assert result["available"] and result["status_label"] == "主导航就绪"
    assert result["node_count"] == 100
    assert "nodes" not in result and "directory" not in result
    assert tree["ready"] is True


def test_closed_manual_trees_are_not_reported_available():
    result = html_tree_status({"ready": True, "tree_count": 1, "enabled_tree_count": 0})
    assert not result["available"]
    assert result["status_label"] == "已有目录 · 资料库未启用"
    assert html_tree_status({})["status_label"] == "尚未构造章节树"
    assert html_tree_status({"state": "error"})["status_label"] == "目录待修复"
    assert html_tree_status({"tree_count": 1, "enabled_tree_count": 1, "available_tree_count": 0})["status_label"] == "目录待修复"


def test_library_navigation_status_contains_counts_only(tmp_path):
    entries = [{"library_id": "manuals", "tree_id": "a", "index_path": "private", "nodes": ["private"],
                "node_count": 3, "document_count": 2},
               {"library_id": "manuals", "tree_id": "b", "node_count": 7, "document_count": 6}]
    with patch("advisor.html_tree_store.HtmlTreeRepository") as factory:
        factory.return_value.list_trees.return_value = entries
        result = html_tree_library_summaries(SimpleNamespace(root=tmp_path))
    assert result == {"manuals": {"tree_count": 2, "node_count": 10, "document_count": 8}}
