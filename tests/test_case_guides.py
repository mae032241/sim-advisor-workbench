"""Case guides are bounded hints with fresh permission and generation gates."""
import copy
import json
from types import SimpleNamespace

import pytest

from advisor import case_guides as guides
from advisor.case_metadata import FEATURE_BRANCHES


class Store:
    def __init__(self, root):
        self.root = root
        self.enabled = {"models": True, "manuals": True}
        self.blocked = set()
        self.gets = []
        self.sources = {
            "case": {"id": "case", "type": "model_case", "library_id": "models", "title": "Saved pilot", "asset_key": "models:pilot.mph",
                     "coverage": {"raw_xml": {"status": "bounded_saved_state", "truncated": True},
                                  "deep_api": {"status": "partial", "read_errors": 2}}},
            "other": {"id": "other", "type": "model_case", "library_id": "models", "title": "Other case"},
            "mesh": {"id": "mesh", "type": "saved_configuration", "title": "Saved mesh", "library_id": "models", "data": {"compact_ref": {"node_id": 1}}},
            "api": {"id": "api", "type": "api_configuration", "title": "API solver", "library_id": "models", "data": {"compact_ref": {"node_id": 2}}},
            "tutorial": {"id": "tutorial", "type": "method_step", "title": "Mesh tutorial", "library_id": "manuals", "text": "Documented mesh step. " * 5000},
        }

    def get(self, identity):
        self.gets.append(identity)
        return copy.deepcopy(self.sources.get(identity))

    def require_not_heldout(self, identity, source=None):
        if identity in self.blocked:
            raise PermissionError("heldout")

    def source_available(self, identity):
        return identity in self.sources and identity not in self.blocked and self.enabled[self.sources[identity]["library_id"]]

    def require_readable_library(self, library):
        if not self.enabled[library]:
            raise PermissionError("closed library")


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    store = Store(tmp_path)
    navigation = {"path": str(tmp_path / "navigation.sqlite3"),
                  "meta": {"generation": "g1", "corpus_hash": "h1", "objects_signature": [[1, 2, 3], None, None]}}
    monkeypatch.setattr(guides, "_navigation", lambda _: ({"generation": "g1"}, copy.deepcopy(navigation)))
    def scope(_, active, identity):
        if identity != "case":
            raise RuntimeError("not prepared")
    monkeypatch.setattr(guides, "_scope", scope)
    monkeypatch.setattr(guides, "_available_references", lambda store, refs: {ref["id"] for ref in refs if store.source_available(ref["id"])})
    monkeypatch.setattr(guides, "_members", lambda *_: iter([(store.get("mesh"), "mesh", "implementation"),
        (store.get("api"), "solver", "implementation"), (store.get("tutorial"), "mesh", "reference")]))
    nodes = [{"id": 1, "feature_branch": "mesh", "type": "Size", "label": "Size 1", "tag": "size1",
              "properties": [{"name": "hmax", "value": "lambda/6", "status": "decoded_saved", "value_complete": True}]},
             {"id": 2, "feature_branch": "solver", "type": "Time", "label": "Time getter", "tag": "t1",
              "properties": [{"name": "method", "value": "MUST_NOT_CLAIM_SAVED", "value_complete": False}],
              "api_observations": [{"name": "method", "value": "getter"}]},
             {"id": 3, "feature_branch": "physics", "type": "Frame", "label": "DISPLAY_ONLY"}]
    monkeypatch.setattr(guides, "_reader", lambda *_: SimpleNamespace(iter_nodes=lambda: iter(copy.deepcopy(nodes))))
    return store, navigation


def test_build_retains_references_and_bounded_facts_without_copying_sources_or_claiming_effectiveness(prepared):
    store, _ = prepared
    store.enabled["models"] = False  # Explicit preparation may inspect an ordinary closed archive.
    built = guides.build_case_guides(store, ["case"])
    assert built["count"] == 1
    import sqlite3
    with sqlite3.connect(guides.database_path(store)) as db:
        raw = db.execute("SELECT guide_json FROM guides WHERE case_id='case'").fetchone()[0]
    assert len(raw) < 20000 and "DISPLAY_ONLY" not in raw and "MUST_NOT_CLAIM_SAVED" not in raw
    assert raw.count("Documented mesh step.") < 10
    store.enabled["models"] = True
    result = guides.get_case_guide(store, "case")
    assert result["state"] == "ready" and not result["is_read_evidence"]
    assert {stage["id"] for stage in result["stages"]} == set(FEATURE_BRANCHES)
    observed = [fact for stage in result["stages"] for fact in stage["observed"]]
    requests = [request for stage in result["stages"] for request in stage["configuration_requests"]]
    assert all(fact["effective_activation"] == "not_evaluated" for fact in observed)
    api = next(fact for fact in observed if fact["basis"] == "api_getter_observation")
    assert api["source_ids"] == ["api"] and "property_hints" not in api
    assert {gap["code"] for gap in result["gaps"]} >= {"api_coverage_incomplete", "saved_state_coverage_incomplete"}
    assert next(stage for stage in result["stages"] if stage["id"] == "mesh")["guidance"][0]["basis"] == "framework_guidance"
    assert {request["node_path"] for request in requests} == {"compact:node/1", "compact:node/2"}
    assert {(request['node_path'], request['source']) for request in requests} == {
        ('compact:node/1', 'saved'), ('compact:node/2', 'api')}
    assert all(request["limit"] == 1 for request in requests)
    assert not {"source_refs", "observed", "configuration_requests"} & result.keys()
    old_response = dict(result, observed=observed, configuration_requests=requests,
                        source_refs=[ref for stage in result["stages"] for ref in stage["source_refs"]])
    assert len(json.dumps(result).encode()) < len(json.dumps(old_response).encode())


def test_read_rechecks_closed_and_heldout_boundaries_before_returning_cached_metadata(prepared):
    store, _ = prepared
    guides.build_case_guides(store, ["case"])
    store.gets.clear()
    store.enabled["models"] = False
    with pytest.raises(PermissionError):
        guides.get_case_guide(store, "case")
    assert store.gets == []
    store.enabled["models"] = True
    store.blocked.add("case")
    with pytest.raises(PermissionError, match="heldout"):
        guides.get_case_guide(store, "case")


def test_closed_tutorial_source_hides_its_cached_hint_and_reference(prepared):
    store, _ = prepared
    guides.build_case_guides(store, ["case"])
    store.enabled["manuals"] = False
    result = guides.get_case_guide(store, "case")
    assert "Documented mesh step" not in json.dumps(result)
    assert all(ref["id"] != "tutorial" for stage in result["stages"] for ref in stage["source_refs"])
    assert any(gap["code"] == "linked_sources_unavailable" for gap in result["gaps"])


def test_changed_objects_generation_returns_need_rebuild_without_stale_facts(prepared):
    store, navigation = prepared
    guides.build_case_guides(store, ["case"])
    navigation["meta"]["objects_signature"][0][2] += 1
    result = guides.get_case_guide(store, "case")
    assert result["state"] == "need_rebuild" and "observed" not in result


def test_nonpilot_returns_not_prepared_with_only_its_readable_overview(prepared):
    store, _ = prepared
    guides.build_case_guides(store, ["case"])
    result = guides.get_case_guide(store, "other")
    assert result["state"] == "not_prepared" and result["overview"]["id"] == "other"
    assert "observed" not in result and not result["is_read_evidence"]


def test_heldout_cases_cannot_be_prepared_and_invalid_batches_do_not_build(prepared):
    store, _ = prepared
    store.blocked.add("case")
    with pytest.raises(PermissionError):
        guides.build_case_guides(store, ["case"])
    assert not guides._path(store).exists()
    for values in ([], [None], "case"):
        with pytest.raises(ValueError):
            guides.build_case_guides(store, values)


def test_cli_explicitly_builds_only_requested_navigation_and_guides(monkeypatch, capsys):
    from scripts import build_case_guides as cli
    from contextlib import nullcontext
    from pathlib import Path
    store, calls = SimpleNamespace(root=Path('existing/libraries')), []
    monkeypatch.setattr(cli, "ReadOnlyLibraryStore", lambda _: store)
    monkeypatch.setattr(cli, '_offline_build_lease', lambda _: nullcontext())
    monkeypatch.setattr(cli, 'suspend_workers', lambda _: nullcontext())
    monkeypatch.setattr(cli, '_write', lambda *_: None)
    monkeypatch.setattr(cli, "build_navigation", lambda value, **kwargs: calls.append(("navigation", value, kwargs)) or {})
    monkeypatch.setattr(cli, "build_case_guides", lambda value, ids, **kwargs: calls.append(("guides", value, ids)) or {"count": len(ids)})
    assert cli.main(["--data-dir", "existing", "--case", "one", "--case", "two"]) == 0
    assert calls[0][:2] == ('navigation', store)
    assert calls[0][2]['case_ids'] == ['one', 'two'] and callable(calls[0][2]['progress'])
    assert calls[1] == ('guides', store, ['one', 'two'])
    assert json.loads(capsys.readouterr().out)["library_visibility_changed"] is False


def test_reference_visibility_is_batched_without_hydrating_payloads(tmp_path):
    import sqlite3
    path = tmp_path / "owners.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE revisions(id TEXT,library_id TEXT,object_id TEXT)")
        db.executemany("INSERT INTO revisions VALUES(?,?,?)", [(str(n), "open" if n % 2 == 0 else "closed", "logical-" + str(n)) for n in range(900)])
    queries, scopes = [], []
    def connect():
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        db.set_trace_callback(queries.append)
        return db
    store = SimpleNamespace(connect=connect, searchable_library_ids=lambda: scopes.append(True) or ["open"],
                            evaluation_policy=lambda: SimpleNamespace(blocks_identity=lambda identity, logical: identity == "2"))
    result = guides._available_references(store, [{"id": str(n)} for n in range(900)])
    assert result == {str(n) for n in range(900) if n % 2 == 0 and n != 2}
    assert len(scopes) == 2 and len(queries) == 4
    assert all("payload" not in query for query in queries)


def test_summary_prefers_solver_routes_and_critical_mesh_properties():
    nodes = [{"id": n, "type": "Direct", "properties": [{"name": "itol", "value": None}]} for n in range(20)]
    nodes.extend([{"id": 90, "type": "Time", "properties": [{"name": "rtol", "value": "0.001"}]},
                  {"id": 91, "type": "Direct", "properties": [{"name": "linsolver", "value": "pardiso"}]}])
    selected = guides._select_nodes(nodes, "solver", 3)
    assert selected[0]["id"] == 90 and selected[1]["id"] == 91
    mesh = {"properties": [{"name": "table", "value": "cfd"}, {"name": "hmin", "value": "0.1"}, {"name": "hmax", "value": "lambda/6"}]}
    assert [prop["name"] for prop in guides._properties(mesh, "mesh")][:2] == ["hmax", "hmin"]


def test_context_references_follow_model_definition_and_introduction_not_random_ids(prepared, monkeypatch):
    store, _ = prepared
    for identity, section, order in [("a-noise", "Results", 5), ("z-goal", "Model Definition", 3), ("y-intro", "Introduction", 1)]:
        store.sources[identity] = {"id": identity, "library_id": "manuals", "type": "case_context", "title": section,
                                   "text": "Copyright all rights reserved\n" + section + " meaningful source text.",
                                   "data": {"section": section, "tutorial_order": order}}
    monkeypatch.setattr(guides, "_members", lambda *_: iter([(store.get(identity), "misc", "reference") for identity in ("a-noise", "z-goal", "y-intro")]))
    guides.build_case_guides(store, ["case"])
    result = guides.get_case_guide(store, "case")
    stage = next(stage for stage in result["stages"] if stage["id"] == "misc")
    assert [fact["source_ids"][0] for fact in stage["observed"]] == ["z-goal", "y-intro"]
    assert "Copyright" not in json.dumps(stage)
