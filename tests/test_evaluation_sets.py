"""Holdouts remain closed through old IDs, source copies and scope toggles."""
from contextlib import closing
import json
from unittest.mock import patch

import pytest

from advisor.evaluation_sets import (
    EvaluationPolicyError, EvaluationPolicyStale, compile_registry,
    evaluation_set_summaries, exclusion_snapshot, refresh_registry, write_registry,
)
from advisor.library_store import LibraryStore, encoded


def insert(store, identity, *, owner="models", asset="models:held.mph", source_hash="model-hash",
           group=None, local="model-case", kind="model_case", data=None, object_id=None, active=True):
    obj = {"id": identity, "object_id": object_id or identity, "library_id": owner, "asset_key": asset,
           "asset_hash": source_hash, "type": kind, "kind": "case", "local_id": local,
           "revision_id": identity, "title": identity, "text": "Secret answer " + identity,
           "data": data or {}, "evidence": [], "relations": []}
    if group:
        obj["case_group"] = group
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (identity, obj["object_id"], owner, encoded(obj)))
        if group:
            db.execute("INSERT INTO case_members VALUES(?,?,?)", (group, local, identity))
        if active:
            db.execute("INSERT OR REPLACE INTO active VALUES(?,?,?)", (owner, obj["object_id"], identity))
    return obj


@pytest.fixture
def fixture(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    store = LibraryStore(tmp_path / "data/libraries")
    for lid, kind in (("models", "official-models"), ("copy", "user-models"), ("manuals", "official-manuals")):
        store.register(lid, kind, [{"id": "models", "role": "models", "path": str(sources)}], library_id=lid)
        store.set_enabled(lid, True)
    # Current API authority has no tutorial_refs; the historical XML authority
    # and association tables retain them. Both groups must stay closed.
    insert(store, "base", group="base-group", active=False, object_id="held-case")
    insert(store, "base-child", group="base-group", local="saved-node", kind="saved_configuration")
    insert(store, "api", group="api-group", object_id="held-case",
           data={"base_case_id": "base", "base_case_group": "base-group"})
    insert(store, "api-child", group="api-group", local="deep-api-node", kind="api_configuration")
    insert(store, "older", group="old-group", source_hash="older-source", object_id="held-case", active=False)
    insert(store, "older-child", group="old-group", local="saved-node", source_hash="older-source", kind="saved_configuration")
    insert(store, "copy-case", owner="copy", asset="models:renamed.mph", group="copy-group")
    insert(store, "copy-child", owner="copy", asset="models:renamed.mph", group="copy-group", local="saved-node", kind="saved_configuration")
    insert(store, "doc-one", owner="manuals", asset="models:tutorial.html", source_hash="doc-hash", kind="method_step")
    insert(store, "doc-two", owner="manuals", asset="models:tutorial.pdf", source_hash="doc-hash", kind="manual_topic")
    insert(store, "normal", asset="models:normal.mph", source_hash="normal-hash", group="normal-group")
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO tutorial_model_documents VALUES(?,?,?,?)", ("base", "models", "doc-hash", "example"))
        db.execute("INSERT INTO tutorial_document_sources VALUES(?,?,?,?,?,?)", ("doc-hash", "example", "manuals", "doc-one", "document", "physics"))
        db.execute("INSERT INTO tutorial_aliases VALUES(?,?,?,?,?)", ("old-doc-id", "models", "doc-one", "manuals", "model_tutorial_copy"))
    sets = [{"id": "held-out", "name": "Evaluation", "description": "Directions", "models": [
        {"library_id": "models", "asset_key": "models:held.mph", "title": "Held case", "direction": "Fluid dynamics",
         "object_id": "held-case", "text": "Do not expose answers in summaries"}]}]
    return store, sets, sources


def publish(store, sets):
    registry = compile_registry(store.db_path, sets)
    write_registry(store.root, registry)
    return registry


def test_web_gate_opens_and_restores_policy_without_changing_libraries_or_catalog(fixture):
    from advisor.evaluation_sets import set_retrieval_enabled, ACCESS_FILENAME
    store, sets, _ = fixture
    publish(store, sets)
    generation = store.current_generation()
    flags = [(lib['id'], lib['enabled']) for lib in store.libraries()]
    closed = exclusion_snapshot(store.root)
    assert closed.blocks_identity('api-child')
    assert evaluation_set_summaries(store.root)['retrieval_enabled'] is False
    with pytest.raises(PermissionError):
        store.read('api-child')
    set_retrieval_enabled(store.root, True)
    opened = exclusion_snapshot(store.root)
    assert opened.token != closed.token and not opened.blocks_identity('api-child')
    assert store.read('api-child')['id'] == 'api-child'
    # Opening the gate does not open an otherwise closed ordinary library.
    store.set_enabled('models', False)
    with pytest.raises(PermissionError):
        store.read('api-child')
    store.set_enabled('models', True)
    set_retrieval_enabled(store.root, False)
    assert exclusion_snapshot(store.root).token == closed.token
    with pytest.raises(PermissionError):
        store.read('api-child')
    assert store.current_generation() == generation
    assert [(lib['id'], lib['enabled']) for lib in store.libraries()] == flags
    assert (store.root / ACCESS_FILENAME).is_file()


def test_corrupt_web_gate_fails_closed_and_rejects_non_boolean(fixture):
    from advisor.evaluation_sets import set_retrieval_enabled, ACCESS_FILENAME
    store, sets, _ = fixture
    publish(store, sets)
    with pytest.raises(ValueError):
        set_retrieval_enabled(store.root, 1)
    (store.root / ACCESS_FILENAME).write_text('{"schema":"evaluation-access/v1","enabled":"yes"}')
    with pytest.raises(EvaluationPolicyError):
        exclusion_snapshot(store.root)
    # The same web entry can always restore isolation after invalid state.
    set_retrieval_enabled(store.root, False)
    assert exclusion_snapshot(store.root).blocks_identity('api-child')


def test_compilation_expands_versions_overlay_children_hash_copies_tutorials_and_aliases(fixture):
    store, sets, _ = fixture
    generation = store.current_generation()
    marker = store.root / "search-kept.marker"
    marker.write_bytes(b"Existing index is unchanged")
    registry = publish(store, sets)
    policy = exclusion_snapshot(store.root)
    expected = {"base", "base-child", "api", "api-child", "older", "older-child", "copy-case", "copy-child",
                "doc-one", "doc-two", "old-doc-id"}
    assert expected <= policy.citation_ids
    assert not policy.blocks_identity("normal")
    assert {"doc-hash", "model-hash", "older-source"} <= policy.source_hashes
    assert ("copy", "models:renamed.mph") in policy.asset_keys
    assert len(policy.qmd_paths) == len(policy.citation_ids)
    assert policy.blocks_object({"id": "future", "library_id": "models", "asset_key": "models:held.mph"})
    assert policy.blocks_object({"id": "future-copy", "asset_hash": "model-hash"})
    assert marker.read_bytes() == b"Existing index is unchanged"
    assert store.current_generation() == generation
    assert registry["index_rebuilt"] is False


@pytest.mark.parametrize("identity", ["base", "base-child", "api", "api-child", "older", "older-child", "copy-child", "doc-two", "old-doc-id"])
def test_agent_public_reads_refuse_known_historical_ids_and_cannot_enable_holdouts(fixture, identity):
    store, sets, _ = fixture
    publish(store, sets)
    original = (store.root / "evaluation-sets.json").read_bytes()
    store.set_enabled_many(["models", "copy", "manuals"], False)
    store.set_enabled_many(["models", "copy", "manuals"], True)
    assert (store.root / "evaluation-sets.json").read_bytes() == original
    assert not store.source_available(identity)
    for action in (lambda: store.read(identity), lambda: store.related_sources(identity),
                   lambda: store.read_evidence(identity), lambda: store.configuration(identity)):
        with pytest.raises(PermissionError, match="测试集"):
            action()
    # Internal archive access remains available to constructors/audit validation.
    assert store.get("base")["text"] == "Secret answer base"
    assert store.read("normal")["id"] == "normal"


def test_ordinary_disabled_library_rule_is_still_enforced(fixture):
    store, sets, _ = fixture
    publish(store, sets)
    store.set_enabled("models", False)
    assert not store.source_available("normal")
    with pytest.raises(PermissionError, match="关闭|删除"):
        store.read("normal")
    store.set_enabled("models", True)
    assert store.read("normal")["id"] == "normal"
    assert not store.source_available("base")


def test_links_from_allowed_case_do_not_advertise_heldout_tutorial(fixture):
    store, sets, _ = fixture
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO tutorial_model_documents VALUES(?,?,?,?)", ("normal", "models", "doc-hash", "example"))
    publish(store, sets)
    assert store.related_sources("normal") == []
    assert store.read("normal")["data"].get("tutorial_links", []) == []


def test_canonical_metadata_closes_alternate_pdf_with_a_different_hash(fixture):
    store, sets, _ = fixture
    insert(store, "alternate-pdf", owner="manuals", asset="models:alternate.pdf", source_hash="pdf-sha", kind="manual_topic")
    with closing(store.connect()) as db, db:
        source = store.get("doc-one")
        source["data"]["tutorial"] = {"source_sha256": "doc-hash", "source_uri": "file:///D:/doc/example.html",
             "alternate_sources": [{"source_sha256": "pdf-sha", "source_uri": "file:///D:/doc/example.pdf"}]}
        db.execute("UPDATE revisions SET payload=? WHERE id=?", (encoded(source), source["id"]))
    registry = publish(store, sets)
    assert "alternate-pdf" in registry["exclusions"]["citation_ids"]
    assert "pdf-sha" in registry["exclusions"]["source_hashes"]
    assert "example.pdf" in registry["exclusions"]["document_relpaths"]


def test_summaries_use_allowlist_and_missing_registry_is_ordinary_empty_policy(fixture):
    store, sets, _ = fixture
    assert exclusion_snapshot(store.root).token == ""
    assert evaluation_set_summaries(store.root)["sets"] == []
    publish(store, sets)
    summary = store.evaluation_set_summaries()
    assert summary["model_count"] == 1 and summary["read_only"]
    model = summary["sets"][0]["models"][0]
    assert model["title"] == "Held case"
    assert "object_id" not in model and "text" not in model
    assert "exclusions" not in summary
    summary["sets"].clear()
    assert evaluation_set_summaries(store.root)["model_count"] == 1


def test_snapshot_cache_does_not_redecode_catalog_or_registry(fixture):
    store, sets, _ = fixture
    publish(store, sets)
    expected = exclusion_snapshot(store.root)
    with patch("advisor.evaluation_sets._parse_registry", side_effect=AssertionError("Registry should be cached")), \
         patch("advisor.catalog_storage._unpack", side_effect=AssertionError("No runtime payload decode")):
        assert exclusion_snapshot(store.root) is expected
        assert not store.source_available("api-child")
        assert store.evaluation_policy() is expected


def test_new_revision_fails_closed_until_refresh_and_refresh_skips_unchanged_catalog(fixture):
    store, sets, _ = fixture
    publish(store, sets)
    insert(store, "new-child", group="api-group", local="brand-new-api-node", kind="api_configuration")
    with pytest.raises(EvaluationPolicyStale):
        exclusion_snapshot(store.root)
    with pytest.raises(EvaluationPolicyStale):
        store.read("new-child")
    updated = refresh_registry(store.root)
    assert "new-child" in updated["exclusions"]["citation_ids"]
    with pytest.raises(PermissionError, match="测试集"):
        store.read("new-child")
    with patch("advisor.evaluation_sets.compile_registry", side_effect=AssertionError("Unchanged catalog must not scan")):
        assert refresh_registry(store.root) == updated


def test_constructor_refreshes_existing_selection_once_after_catalog_commit(fixture):
    store, sets, sources = fixture
    publish(store, sets)
    (sources / "ordinary.txt").write_text("Useful generic material reference", encoding="utf-8")
    library = store.register("Additional manuals", "user-manuals", [{"id": "docs", "path": str(sources)}])
    store.set_enabled(library["id"], True)
    with patch("advisor.evaluation_sets.compile_registry", wraps=compile_registry) as compiled:
        result = store.build(library["id"], modes=[])
    assert result["status"] == "completed", result
    assert compiled.call_count == 1
    assert "base" in exclusion_snapshot(store.root).citation_ids
    ordinary = store.objects([library["id"]])[0]
    assert store.read(ordinary["id"])["text"] == "Useful generic material reference"


def test_corrupted_registry_fails_closed_and_rejects_unknown_selections(fixture):
    store, sets, _ = fixture
    with pytest.raises(ValueError, match="no model-case authority"):
        compile_registry(store.db_path, [{"id": "bad", "name": "Bad", "models": [{"library_id": "models", "asset_key": "models:missing.mph"}]}])
    (store.root / "evaluation-sets.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(EvaluationPolicyError):
        store.read("normal")
    publish(store, sets)
    assert exclusion_snapshot(store.root).blocks_identity("base")
    incomplete = json.loads((store.root / "evaluation-sets.json").read_text(encoding="utf-8"))
    incomplete["exclusions"].pop("citation_ids")
    (store.root / "evaluation-sets.json").write_text(json.dumps(incomplete), encoding="utf-8")
    with pytest.raises(EvaluationPolicyError):
        store.read("normal")
