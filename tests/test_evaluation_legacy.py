"""The independent legacy HTML corpus cannot reopen heldout tutorials."""
from contextlib import closing
import json
from unittest.mock import patch

import pytest

from advisor.evaluation_sets import EvaluationPolicyStale, exclusion_snapshot, write_registry
from advisor.knowledge import KnowledgeBase, source_id
from advisor.library_provider import LibraryKnowledgeProvider
from advisor.library_store import encoded
from test_evaluation_sets import fixture, insert, publish
from test_knowledge import write_topic


@pytest.fixture
def legacy_fixture(fixture, tmp_path):
    store, sets, _ = fixture
    docs = tmp_path / "docs"
    package = "com.comsol.help.models.cfd.held_case"
    for index in range(195):
        write_topic(docs, package, f"held-{index}.html", "Mesh sizing",
                    ["Mesh sizing mesh sizing tetrahedral mesh controls. SECRET_HELDOUT_ANSWER."])
    write_topic(docs, "com.comsol.help.comsol", "open.html", "Generic meshing guide",
                ["Mesh sizing is set using documented element sizes. Keep this generic reference available."])
    kb = KnowledgeBase(store.root.parent, docs)
    assert kb.build()["ready"]
    with closing(store.connect()) as db, db:
        obj = store.get("doc-one")
        obj["asset_key"] = "models:" + package + "/held-0.html"
        db.execute("UPDATE revisions SET payload=? WHERE id=?", (encoded(obj), obj["id"]))
    registry = publish(store, sets)
    return LibraryKnowledgeProvider(kb, store.root), kb, store, registry, package


def test_retired_legacy_search_cannot_bypass_registered_qmd_requirement(legacy_fixture):
    provider, kb, _store, _registry, _package = legacy_fixture
    with patch.object(kb, "search", side_effect=AssertionError("Legacy retrieval must not run")):
        with pytest.raises(RuntimeError, match="QMD index"):
            provider.search("mesh sizing", limit=1)


def test_old_legacy_ids_cannot_read_body_or_other_sections_in_same_tutorial(legacy_fixture):
    provider, kb, store, _registry, package = legacy_fixture
    identity = source_id(package + "/held-0.html")
    with patch.object(kb, "get_document", side_effect=AssertionError("Known heldout ID must be rejected before loading body")):
        with pytest.raises(PermissionError, match="测试集"):
            provider.get_document(identity)
        assert not provider.source_available(identity)
    # Other sections in the same case-specific tutorial package are held out,
    # even if that legacy page never appeared in the managed catalog.
    with pytest.raises(PermissionError):
        provider.get_document(source_id(package + "/held-194.html"))
    store.set_enabled_many(["models", "manuals", "copy"], True)
    assert not provider.source_available(identity)
    with pytest.raises(RuntimeError, match="QMD index"):
        provider.search("mesh sizing", limit=1)


def test_legacy_links_and_validation_cannot_expose_heldout_tutorial(legacy_fixture):
    provider, kb, _store, _registry, package = legacy_fixture
    open_id = source_id("com.comsol.help.comsol/open.html")
    old = kb.get_document(source_id(package + "/held-0.html"))
    assert not provider.validate_source(old)
    with kb._connect() as db:
        db.execute("UPDATE documents SET links=? WHERE id=?", (json.dumps([
            {"href": "../" + package + "/held-0.html", "title": "Held answer"},
            {"href": "open.html", "title": "Generic manual"}]), open_id))
    document = provider.get_document(open_id)
    assert document["related_links"] == [{"href": "open.html", "title": "Generic manual"}]


def test_policy_is_rechecked_after_legacy_body_read_and_stale_catalog_fails_closed(legacy_fixture):
    provider, kb, store, registry, _package = legacy_fixture
    original = kb.get_document
    identity = source_id("com.comsol.help.comsol/open.html")

    def change_policy(*args, **kwargs):
        result = original(*args, **kwargs)
        registry["exclusions"]["document_relpaths"].append("com.comsol.help.comsol/open.html")
        write_registry(store.root, registry)
        return result

    with patch.object(kb, "get_document", side_effect=change_policy):
        with pytest.raises(PermissionError):
            provider.get_document(identity)
    insert(store, "fresh", asset="models:ordinary-new.mph", source_hash="new", group="fresh-group")
    with pytest.raises(EvaluationPolicyStale):
        provider.get_document(identity)


def test_purging_other_library_refreshes_watermark_and_keeps_holdouts(fixture):
    store, sets, sources = fixture
    (sources / "generic.txt").write_text("Generic unrelated reference", encoding="utf-8")
    extra = store.register("Unrelated", "user-manuals", [{"id": "docs", "path": str(sources)}])
    store.set_enabled(extra["id"], True)
    assert store.build(extra["id"], modes=[])["status"] == "completed"
    publish(store, sets)
    store.delete_library(extra["id"])
    assert store.purge_library(extra["id"], confirmed=True)["status"] == "purged"
    assert exclusion_snapshot(store.root).blocks_identity("base")
    assert store.read("normal")["id"] == "normal"


def test_purging_selected_library_preserves_selection_and_old_denials(fixture):
    store, sets, _ = fixture
    publish(store, sets)
    store.delete_library("models")
    assert store.purge_library("models", confirmed=True)["status"] == "purged"
    assert exclusion_snapshot(store.root).blocks_identity("base")
    assert exclusion_snapshot(store.root).blocks_identity("copy-child")
    assert store.evaluation_set_summaries()["model_count"] == 1
