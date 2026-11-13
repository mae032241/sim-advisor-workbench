"""Source contracts preserve exact page ranges and live source permissions."""
from contextlib import closing
import copy
from types import SimpleNamespace

import pytest

from advisor import case_guides as guides
from advisor import library_provider as provider_module
from advisor.evaluation_sets import HoldoutPolicy
from advisor.library_provider import LibraryKnowledgeProvider
from advisor.library_store import LibraryStore, encoded
from advisor.planning_service import PlanningService
from test_case_guides import prepared
from test_case_context_provider import contextual_provider
from test_case_navigation_search import navigation, release_workers
from test_library_search import obj


@pytest.fixture
def registered_source(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    store = LibraryStore(tmp_path / "libraries")
    store.register("Models", "user-models", [{"id": "models", "path": str(sources)}], library_id="models")
    store.set_enabled("models", True)
    source = obj("case", "models", "Exact saved source text.")
    source.update(type="model_case", case_group="case-group")
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (source["id"], source["object_id"], source["library_id"], encoded(source)))
        db.execute("INSERT INTO active VALUES(?,?,?)", (source["library_id"], source["object_id"], source["id"]))
    provider = LibraryKnowledgeProvider(SimpleNamespace(get_document=lambda _identity: None), store.root)
    return provider, source


@pytest.mark.parametrize("method", ["search", "search_cases"])
@pytest.mark.parametrize("gate", ["closed", "heldout"])
def test_search_cannot_return_sources_disabled_after_engine_authorization(registered_source, monkeypatch, method, gate):
    provider, source = registered_source
    policy = [HoldoutPolicy(token="clear")]
    monkeypatch.setattr(provider.store, "evaluation_policy", lambda: policy[0])
    engine = SimpleNamespace(
        query_snapshot=lambda: SimpleNamespace(status={"generation": provider.store.current_generation(),
            "modes": {"qmd_keyword": {"ready": True}}}),
        search=lambda *_args, **_kwargs: [copy.deepcopy(source)])
    monkeypatch.setattr(provider_module, "LibrarySearch", lambda *_args: engine)

    def disable_after_engine(item, **_kwargs):
        if gate == "closed":
            provider.store.set_enabled("models", False)
        else:
            policy[0] = HoldoutPolicy(case_groups=frozenset([source["case_group"]]), token="new-holdout")
        return item

    monkeypatch.setattr(provider, "_decorate_case_context", disable_after_engine)
    if gate == "closed":
        assert getattr(provider, method)("saved settings") == []
    else:
        with pytest.raises(RuntimeError, match="visibility changed"):
            getattr(provider, method)("saved settings")


@pytest.mark.parametrize("gate", ["closed", "heldout"])
def test_document_reauthorizes_after_navigation_without_decoding_again(registered_source, monkeypatch, gate):
    provider, source = registered_source
    policy = [HoldoutPolicy(token="clear")]
    monkeypatch.setattr(provider.store, "evaluation_policy", lambda: policy[0])
    read = provider.store.get
    decoded = []

    def tracked(identity):
        decoded.append(identity)
        return read(identity)

    def disable_during_navigation(_references):
        if gate == "closed":
            provider.store.set_enabled("models", False)
        else:
            policy[0] = HoldoutPolicy(case_groups=frozenset([source["case_group"]]), token="new-holdout")
        return set()

    monkeypatch.setattr(provider.store, "get", tracked)
    monkeypatch.setattr(provider, "_available_navigation_ids", disable_during_navigation)
    with pytest.raises(PermissionError):
        provider.get_document(source["id"])
    assert decoded == [source["id"]]


def test_source_availability_honors_object_holdouts_without_reading_body(registered_source, monkeypatch):
    provider, source = registered_source
    policy = HoldoutPolicy(case_groups=frozenset([source["case_group"]]), token="held-group")
    monkeypatch.setattr(provider.store, "evaluation_policy", lambda: policy)
    monkeypatch.setattr(provider.store, "get", lambda *_args: pytest.fail("Availability must not decode the source body"))
    assert not provider.source_available(source)


@pytest.mark.parametrize("state", ["not_prepared", "need_rebuild", "ready"])
@pytest.mark.parametrize("gate", ["closed", "heldout"])
def test_all_case_guide_states_reauthorize_before_returning_overview(prepared, monkeypatch, state, gate):
    store, _navigation = prepared
    guides.build_case_guides(store, ["case"])
    cached = guides._cached_guide

    def disable_during_cache_read(*args):
        result = cached(*args)
        if gate == "closed":
            store.enabled["models"] = False
        else:
            store.blocked.add("case")
        if state == "not_prepared":
            return None, result[1]
        if state == "need_rebuild":
            raise RuntimeError("Stale guide cache")
        return result

    monkeypatch.setattr(guides, "_cached_guide", disable_during_cache_read)
    with pytest.raises(PermissionError):
        guides.get_case_guide(store, "case")


@pytest.mark.parametrize("gate", ["closed", "heldout"])
def test_context_dependency_disabled_after_body_decoration_blocks_direct_read(contextual_provider, monkeypatch, gate):
    fixture = contextual_provider
    original = fixture.provider._available_navigation_ids

    def disable_during_navigation(references):
        result = original(references)
        if gate == "closed":
            fixture.store.set_enabled("manuals", False)
        else:
            policy = HoldoutPolicy(citation_ids=frozenset([fixture.tutorial["id"]]), token="held-tutorial")
            monkeypatch.setattr(fixture.provider.store, "evaluation_policy", lambda: policy)
        return result

    monkeypatch.setattr(fixture.provider, "_available_navigation_ids", disable_during_navigation)
    with pytest.raises(PermissionError):
        fixture.provider.get_document(fixture.case["id"])


def test_source_pages_use_real_character_ranges_and_allow_a_short_tail(tmp_path):
    text = "測試α🙂\n" * 203
    source = {"id": "exact-source-id", "kind": "manual", "type": "manual_topic", "text": text}
    tools = PlanningService(tmp_path, None, SimpleNamespace(get_document=lambda _identity: copy.deepcopy(source)))
    pages = []
    offset = 0
    while offset is not None:
        page = tools.call_public("read_source", {"id": source["id"], "offset": offset, "length": 500})
        pages.append(page)
        assert page["offset"] == offset and page["length"] == 500
        assert page["returned_characters"] == len(page["text"])
        assert page["total_characters"] == len(text)
        assert page["text"] == text[offset:offset + 500]
        assert page["evidence_refs"] == [{"source_id": source["id"], "context": "source_text",
            "locator": f"text:{offset}:{offset + len(page['text'])}"}]
        offset = page["next_offset"]
    assert "".join(page["text"] for page in pages) == text
    assert 0 < pages[-1]["returned_characters"] < 500
    assert pages[-1]["next_offset"] is None
    with pytest.raises(ValueError):
        tools.call_public("read_source", {"id": source["id"], "offset": len(text), "length": 500})
    with pytest.raises(ValueError):
        tools.call_public("read_source", {"id": source["id"], "offset": 1000, "length": 15})
