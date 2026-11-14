"""Real native-tree -> read receipt -> saved plan, using synthetic originals."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from advisor.evaluation_sets import HoldoutPolicy
from advisor.html_tree import HtmlTreeIndex
from advisor.html_tree_store import HtmlTreeRepository
from advisor.html_tree_tools import HtmlTreeTools
from advisor.library_provider import LibraryKnowledgeProvider
from advisor.library_store import encoded
from advisor.planning_service import PlanningService
from advisor.sessions import SessionStore
from test_agent_sessions import payload
from test_workflow import FakePlanner, GOAL


PACKAGE = "com.comsol.help.synthetic"
BODY = "Original native setting applies alpha = 0.3. " * 120


def original(root, label, *, package_root=False):
    folder = root if package_root else root / PACKAGE
    folder.mkdir(parents=True)
    (folder / "toc.xml").write_text('<toc label="' + label + '"><topic label="Interface" href="interface.html">'
        '<topic label="Wall" href="wall.html"/></topic></toc>', encoding="utf-8")
    (folder / "interface.html").write_text('<body><h1>Interface</h1>Interface context.</body>', encoding="utf-8")
    (folder / "wall.html").write_text('<body><h1>Wall</h1><p>' + BODY + '</p>'
        '<a href="/' + PACKAGE + '/interface.html">Interface</a></body>', encoding="utf-8")
    return root


@pytest.fixture
def formal(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ADVISOR_HTML_TREE_INDEX", raising=False)
    legacy = SimpleNamespace(get_document=lambda identity: None, status=lambda: {"ready": False}, doc_root=tmp_path / "unused")
    provider = LibraryKnowledgeProvider(legacy, tmp_path / "libraries")
    store = provider.store
    roots = [{"id": "full", "path": str(original(tmp_path / "first", "First manual"))},
             {"id": "package", "path": str(original(tmp_path / "scoped" / PACKAGE, "Scoped manual", package_root=True))}]
    store.register("First", "official-manuals", roots, library_id="first")
    store.register("Second", "user-manuals", [{"id": "full", "path": str(original(tmp_path / "second", "Second manual"))}], library_id="second")
    store.set_enabled_many(["first", "second"], True)
    repo = HtmlTreeRepository(store)
    repo.build_library("first")
    repo.build_library("second")
    sessions = SessionStore(tmp_path / "workspace", provider, FakePlanner())
    task = sessions.create(GOAL, None)
    service = PlanningService(tmp_path / "workspace", sessions, provider)
    return SimpleNamespace(store=store, provider=provider, repo=repo, sessions=sessions, task=task,
                           sid=task["id"], service=service, tmp_path=tmp_path)


def nodes(fixture):
    return fixture.service.call_public("get_html_navigation", {"query": "Wall", "limit": 8})["items"]


def plan_args(fixture, identity):
    args = payload(fixture.sessions.get(fixture.sid))
    for step in args["plan"]["steps"]:
        step["source_ids"] = [identity]
    return dict(args, session_id=fixture.sid)


def read(fixture, identity, offset=0, length=500):
    return fixture.service.call_public("read_html_section", {"session_id": fixture.sid, "node_id": identity,
                                                           "offset": offset, "length": length})


def setting(args, page):
    step = args["plan"]["steps"][0]
    step["inputs"].append("alpha = 0.3")
    return {"id": "native-alpha", "requirement": "Native boundary", "setting": "alpha", "proposed_value": "alpha = 0.3",
            "status": "adapted", "reason": "Read the original native section", "transfer_rationale": "Recheck applicability",
            "gap": "", "step_ids": [step["id"]], "implementation": {"step_id": step["id"], "field": "inputs"},
            "source_refs": [dict(page["evidence_refs"][0], stated_value="alpha = 0.3")]}


def test_registry_discovers_multiple_libraries_roots_without_env_and_uses_public_ids(formal):
    found = nodes(formal)
    assert len(found) == 3
    assert {(n["library_id"], n["root_id"]) for n in found} == {("first", "full"), ("first", "package"), ("second", "full")}
    assert all(n["node_id"].startswith(n["tree_id"] + ":html-") for n in found)
    assert len({n["node_id"] for n in found}) == 3
    for n in found:
        assert all(identity.startswith(n["tree_id"] + ":html-") for identity in n["ancestor_ids"])
    first = formal.service.call_public("get_html_navigation", {"limit": 1})
    pages, offset = [], 0
    while True:
        page = formal.service.call_public("get_html_navigation", {"limit": 1, "offset": offset})
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= 7000
        pages.extend(n["node_id"] for n in page["items"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(pages) == len(set(pages)) == first["total"] == 3


def test_real_business_chain_html_read_can_save_and_website_gets_dynamic_original(formal):
    node = nodes(formal)[0]
    identity = node["node_id"]
    assert formal.service.read_sources(formal.sid) == []
    with pytest.raises(ValueError):
        formal.service.call_public("save_plan", plan_args(formal, identity))
    page = read(formal, identity)
    assert page["source_id"] == identity and page["evidence_refs"][0]["context"] == "html_section"
    args = plan_args(formal, identity)
    args["plan_settings"] = [setting(args, page)]
    saved = formal.service.call_public("save_plan", args)
    assert saved["saved"] and saved["revision"] == 2
    current = formal.sessions.get(formal.sid)
    source = current["sources"][0]
    assert source["id"] == identity and source["provenance_kind"] == "native_html_virtual_source"
    assert "text" not in source and "excerpt" not in source
    assert source["read_evidence_refs"] == page["evidence_refs"]
    assert formal.provider.validate_source(source)
    document = formal.provider.get_document(identity)
    assert document["text"].startswith("Wall") and BODY in document["text"]
    assert formal.provider.validate_source(document)
    assert not formal.store.get(identity), "virtual HTML sources must not become fake catalog revisions"
    # Planning snapshots and read receipts contain no duplicated source body.
    persisted = formal.sessions.path(formal.sid).read_text(encoding="utf-8")
    assert BODY not in persisted
    assert not list(formal.store.root.rglob("*.html"))
    review = formal.service.call_public("review_plan", {"session_id": formal.sid})
    assert not any(w["code"] in {"source_not_in_read_ledger", "source_context_not_in_read_ledger", "source_unavailable"}
                   for w in review["settings_review"]["warnings"])


@pytest.mark.parametrize("mode", ["disabled", "deleted", "changed", "heldout", "purged"])
def test_old_native_ids_cannot_read_or_save_when_current_authority_changes(formal, monkeypatch, mode):
    node = next(n for n in nodes(formal) if n["library_id"] == "second")
    identity = node["node_id"]
    page = read(formal, identity)
    if mode == "disabled":
        formal.store.set_enabled("second", False)
    elif mode == "deleted":
        formal.store.delete_library("second")
    elif mode == "purged":
        formal.repo.purge_library("second")
    elif mode == "changed":
        root = Path(formal.store.library("second")["roots"][0]["path"])
        (root / node["doc_path"]).write_text("changed native original", encoding="utf-8")
    else:
        monkeypatch.setattr(formal.store, "evaluation_policy", lambda: HoldoutPolicy(source_hashes=frozenset([page["doc_sha256"]])))
    assert not formal.provider.source_available(identity)
    with pytest.raises((PermissionError, ValueError, FileNotFoundError, KeyError)):
        read(formal, identity)
    with pytest.raises((PermissionError, ValueError)):
        formal.service.call_public("save_plan", plan_args(formal, identity))
    nav = formal.service.call_public("get_html_navigation", {"query": "Wall"})
    assert all(n["node_id"] != identity for n in nav.get("items", []))


@pytest.mark.parametrize("tamper", ["range", "hash", "context", "other_node"])
def test_native_setting_refs_must_match_the_exact_delivered_range(formal, tamper):
    found = nodes(formal)
    page = read(formal, found[0]["node_id"])
    args = plan_args(formal, found[0]["node_id"])
    args["plan_settings"] = [setting(args, page)]
    ref = args["plan_settings"][0]["source_refs"][0]
    if tamper == "range":
        ref["locator"] = ref["locator"].rsplit(":", 1)[0] + ":999999"
    elif tamper == "hash":
        ref["locator"] = ref["locator"].replace(page["doc_sha256"], "0" * 64)
    elif tamper == "context":
        ref["context"] = "source_text"
    else:
        ref["source_id"] = found[1]["node_id"]
    with pytest.raises(PermissionError):
        formal.service.call_public("save_plan", args)
    assert formal.sessions.get(formal.sid)["revision"] == 1


def test_source_bridge_routes_exact_catalog_library_and_root(formal):
    node = next(n for n in nodes(formal) if n["library_id"] == "first" and n["root_id"] == "package")
    source = formal.provider.html_trees.get_document(node["node_id"], metadata_only=True)
    obj = {"id": "lo-synthetic-native", "object_id": "original-wall", "library_id": "first", "type": "manual_topic",
           "asset_key": source["asset_key"], "asset_hash": source["asset_hash"]}
    with closing(formal.store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (obj["id"], obj["object_id"], obj["library_id"], encoded(obj)))
    bridge = formal.service.call_public("get_html_navigation", {"source_id": obj["id"]})
    assert [n["node_id"] for n in bridge["matches"]] == [node["node_id"]]
    page = read(formal, node["node_id"])
    linked = page["references"]["links"]["samples"][0]
    assert linked["accessible_by_text_tool"] and linked["node_id"].startswith(node["tree_id"] + ":html-")


def test_unknown_or_cross_tree_id_cannot_become_a_native_source(formal):
    found = nodes(formal)
    first, second = found[:2]
    forged = first["tree_id"] + ":" + second["node_id"].split(":", 1)[1]
    assert not formal.provider.source_available(forged)
    with pytest.raises((KeyError, ValueError, PermissionError)):
        read(formal, forged)
    with pytest.raises(ValueError):
        formal.service.call_public("save_plan", plan_args(formal, forged))
    with pytest.raises(ValueError):
        formal.service.call_public("read_html_section", {"node_id": first["tree_id"] + ":../secret.html"})


def test_metadata_status_ready_when_native_tree_is_available_but_qmd_is_not(formal, monkeypatch):
    monkeypatch.setattr(formal.store, "index_status", lambda **kw: {"modes": {"qmd_keyword": {"ready": False}}})
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("status must not hash original HTML or indexes"))
    status = formal.provider.status_summary()
    assert status["ready"] is True and status["html_tree"]["available_tree_count"] == 3
    assert status["library_indexes"]["modes"]["qmd_keyword"]["ready"] is False
    assert "index_path" not in json.dumps(status["html_tree"])


def test_persistent_router_follows_published_registry_generation_without_old_index_cache(formal):
    old = next(n for n in nodes(formal) if n["library_id"] == "second")
    read(formal, old["node_id"])
    root = Path(formal.store.library("second")["roots"][0]["path"])
    document = root / old["doc_path"]
    document.write_text('<body><h1>Wall</h1>New published original setting.</body>', encoding="utf-8")
    formal.repo.build_library("second")
    current = next(n for n in nodes(formal) if n["library_id"] == "second")
    assert current["tree_id"] == old["tree_id"] and current["node_id"] != old["node_id"]
    assert not formal.provider.source_available(old["node_id"])
    with pytest.raises((KeyError, PermissionError, ValueError)):
        read(formal, old["node_id"])
    page = read(formal, current["node_id"])
    assert "New published original setting." in page["text"]
    assert formal.service.call_public("save_plan", plan_args(formal, current["node_id"]))["saved"]
