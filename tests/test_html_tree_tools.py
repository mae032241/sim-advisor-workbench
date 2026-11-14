"""Synthetic native-tree transport and authorization; no service/model calls."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from advisor.evaluation_sets import HoldoutPolicy
from advisor.html_tree_tools import HtmlTreeTools, tool_definitions
from advisor.library_store import LibraryStore, encoded
from advisor.planning_service import PlanningService, tool_definitions as planning_definitions


def output_bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


@pytest.fixture
def native(tmp_path, monkeypatch):
    root = tmp_path / "original-docs"
    package = root / "com.comsol.help.aco"
    package.mkdir(parents=True)
    store = LibraryStore(tmp_path / "libraries")
    store.register("Synthetic native manuals", "official-manuals", [{"id": "manuals", "path": str(root), "role": "manuals"}], library_id="manuals")
    store.set_enabled("manuals", True)
    index_path = tmp_path / "html-tree" / "index.sqlite3"
    index_path.parent.mkdir()
    index_path.touch()
    monkeypatch.setenv("SIM_ADVISOR_HTML_TREE_INDEX", str(index_path.parent))
    nodes = {}

    def node(identity, title, parent=None, *, body="native manual text", ancestors=None, filename=None):
        path = package / (filename or identity + ".html")
        path.write_text(body, encoding="utf-8")
        nodes[identity] = {"node_id": identity, "title": title, "parent_id": parent, "library_id": "manuals", "root_id": "manuals",
            "package": "com.comsol.help.aco", "doc_path": path.relative_to(root).as_posix(),
            "doc_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "anchor": "", "ancestor_titles": ancestors or [],
            "ancestor_ids": [] if parent is None else [parent], "child_count": 0, "available": True}
        if parent:
            nodes[parent]["child_count"] += 1
        return nodes[identity]

    node("package", "Acoustics Module")
    nodes["package"].update(doc_path=None, doc_sha256=None, available=False)
    node("paas", "Pressure Acoustics, Asymptotic Scattering", "package", ancestors=["Acoustics Module"])
    node("pabe", "Pressure Acoustics, Boundary Elements", "package", ancestors=["Acoustics Module"])
    node("wall-a", "Wall", "paas", ancestors=["Acoustics Module", "Pressure Acoustics, Asymptotic Scattering"])
    node("wall-b", "Wall", "pabe", ancestors=["Acoustics Module", "Pressure Acoustics, Boundary Elements"])
    nodes["wall-a"].update(next_sibling_id=None, next_id="pabe")
    nodes["paas"].update(next_sibling_id="pabe", next_id="wall-a")
    meta = {"library_id": "manuals", "root_id": "manuals", "schema": "synthetic-native-tree"}

    class FakeIndex:
        def __init__(self, _path):
            self.meta = meta

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def node(self, identity):
            return deepcopy(nodes[identity])

        def list_children(self, node_id=None, offset=0, limit=8, query=""):
            items = [n for n in nodes.values() if n["parent_id"] == node_id] if not query else [n for n in nodes.values()
                if query.casefold() in " ".join([n["title"], *n["ancestor_titles"]]).casefold()
                and (node_id is None or node_id in n.get("ancestor_ids", []) or n["node_id"] == node_id)]
            return {"items": deepcopy(items[offset:offset + limit]), "total": len(items),
                    "next_offset": offset + limit if offset + limit < len(items) else None, "scope": "title_navigation" if query else "native_toc"}

        def resolve_source_path(self, relative_path):
            return {"doc_path": relative_path, "nodes": [deepcopy(n) for n in nodes.values() if n.get("doc_path") == relative_path]}

    refs = {"links": [], "images": [], "eqrefs": []}

    def read_html_text(path, anchor=""):
        if anchor not in {"", "wall"}:
            raise ValueError("Unknown exact HTML anchor")
        return {"text": Path(path).read_text(encoding="utf-8"), "doc_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                "read_scope": "html_anchor_section" if anchor else "html_page", "section_title": "Original native title",
                "anchor_found": bool(anchor), "full_page_available": bool(anchor), **deepcopy(refs)}

    monkeypatch.setitem(sys.modules, "advisor.html_tree", SimpleNamespace(HtmlTreeIndex=FakeIndex, read_html_text=read_html_text))
    tools = HtmlTreeTools(store)
    return SimpleNamespace(root=root, package=package, store=store, nodes=nodes, meta=meta, refs=refs,
                           tools=tools, index_path=index_path, node=node, read_html_text=read_html_text)


def add_source(fixture, identity="exact-citation", *, relative=None, asset_hash=None):
    node = fixture.nodes["wall-a"]
    obj = {"id": identity, "object_id": "synthetic-native-source", "library_id": "manuals", "type": "manual_topic",
           "asset_key": "manuals:" + (relative or node["doc_path"]), "asset_hash": asset_hash or node["doc_sha256"]}
    with closing(fixture.store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (identity, obj["object_id"], "manuals", encoded(obj)))
    return obj


def test_missing_index_is_not_prepared_and_schemas_reject_paths(tmp_path):
    tools = HtmlTreeTools(SimpleNamespace(root=tmp_path), index_path=tmp_path / "absent")
    assert tools.call("get_html_navigation", {})["state"] == "not_prepared"
    with pytest.raises(ValueError):
        tools.call("read_html_section", {"node_id": "x", "path": "secret.html"})
    schemas = {tool["name"]: tool["inputSchema"] for tool in tool_definitions()}
    assert schemas["read_html_section"]["properties"]["length"]["default"] == 1800
    assert schemas["read_html_section"]["properties"]["length"]["maximum"] == 3200


def test_same_title_keeps_multiple_full_ancestor_interface_paths(native):
    result = native.tools.call("get_html_navigation", {"query": "Wall"})
    assert result["is_read_evidence"] is False
    assert len(result["items"]) == 2
    paths = [node["ancestor_titles"] for node in result["items"]]
    assert paths[0] != paths[1]
    assert paths[0][-1] == "Pressure Acoustics, Asymptotic Scattering"
    body = native.tools.call("read_html_section", {"node_id": "wall-a"})
    assert body["ancestor_titles"] == paths[0]
    assert "ancestor_titles" not in body["node"]
    assert body["parent"] == {"node_id": "paas", "title": "Pressure Acoustics, Asymptotic Scattering"}
    assert body["next_reading_node"]["node_id"] == "pabe"
    assert body["evidence_refs"][0]["context"] == "html_section"


@pytest.mark.parametrize("lifecycle", ["disabled", "deleted"])
def test_old_node_ids_cannot_read_after_library_closes(native, lifecycle):
    native.tools.call("read_html_section", {"node_id": "wall-a"})
    if lifecycle == "disabled":
        native.store.set_enabled("manuals", False)
    else:
        native.store.delete_library("manuals")
    with pytest.raises((PermissionError, FileNotFoundError)):
        native.tools.call("read_html_section", {"node_id": "wall-a"})
    with pytest.raises((PermissionError, FileNotFoundError)):
        native.tools.call("get_html_navigation", {})


@pytest.mark.parametrize("kind", ["path", "absolute", "hash", "node"])
def test_native_reads_obey_current_holdout_paths_hashes_and_node_ids(native, monkeypatch, kind):
    n = native.nodes["wall-a"]
    policy = {"path": HoldoutPolicy(document_relpaths=frozenset([n["doc_path"]])),
              "absolute": HoldoutPolicy(source_paths=frozenset([(native.root / n["doc_path"]).as_posix()])),
              "hash": HoldoutPolicy(source_hashes=frozenset([n["doc_sha256"]])),
              "node": HoldoutPolicy(citation_ids=frozenset(["wall-a"]))}[kind]
    monkeypatch.setattr(native.store, "evaluation_policy", lambda: policy)
    with pytest.raises(PermissionError):
        native.tools.call("read_html_section", {"node_id": "wall-a"})
    nav = native.tools.call("get_html_navigation", {"query": "Wall"})
    assert all(node["node_id"] != "wall-a" for node in nav["items"])


@pytest.mark.parametrize("package", ["com.comsol.help.models.aco", "com.comsol.help.addins", "com.comsol.help.addins.test"])
def test_tutorial_and_addin_packages_cannot_be_read_even_with_generic_label(native, package):
    n = native.nodes["wall-a"]
    forbidden = native.root / package / "hidden.html"
    forbidden.parent.mkdir()
    forbidden.write_text("native tutorial body must stay closed", encoding="utf-8")
    n.update(doc_path=forbidden.relative_to(native.root).as_posix(), doc_sha256=hashlib.sha256(forbidden.read_bytes()).hexdigest())
    with pytest.raises(PermissionError):
        native.tools.call("read_html_section", {"node_id": "wall-a"})


@pytest.mark.parametrize("relative", ["../outside.html", "/outside.html", "C:/outside.html", "https://example.com/a.html", "..%2foutside.html"])
def test_index_paths_cannot_escape_the_registered_root(native, relative):
    native.nodes["wall-a"]["doc_path"] = relative
    with pytest.raises(PermissionError):
        native.tools.call("read_html_section", {"node_id": "wall-a"})


def test_stale_source_sha_and_index_binding_fail_closed(native):
    (native.root / native.nodes["wall-a"]["doc_path"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        native.tools.call("read_html_section", {"node_id": "wall-a"})
    native.nodes["wall-b"]["root_id"] = "other-root"
    with pytest.raises(PermissionError, match="binding"):
        native.tools.call("read_html_section", {"node_id": "wall-b"})


def test_registered_root_relocation_preserves_node_ids_without_copying_html_to_data(native, tmp_path):
    relocated = tmp_path / "relocated-originals"
    shutil.move(str(native.root), relocated)
    native.store.rebind("manuals", "manuals", relocated)
    body = native.tools.call("read_html_section", {"node_id": "wall-a"})
    assert body["text"] == "native manual text"
    assert body["node"]["node_id"] == "wall-a"
    assert not list(native.store.root.rglob("*.html"))


def test_long_unicode_text_pages_reconstruct_exactly_with_truthful_ranges(native):
    text = "原始正文🙂 \"quoted\"\\ " * 2500
    n = native.nodes["wall-a"]
    path = native.root / n["doc_path"]
    path.write_text(text, encoding="utf-8")
    n["doc_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    parts, offset = [], 0
    while True:
        body = native.tools.call("read_html_section", {"node_id": "wall-a", "offset": offset, "length": 3200})
        assert output_bytes(body) <= 7000
        end = offset + len(body["text"])
        ref = body["evidence_refs"][0]
        assert ref["context"] == "html_section" and ref["locator"].endswith(f":text:{offset}:{end}")
        assert n["doc_sha256"] in ref["locator"]
        parts.append(body["text"])
        if body["next_offset"] is None:
            break
        assert body["next_offset"] == end > offset
        offset = body["next_offset"]
    assert "".join(parts) == text


def test_source_id_bridge_maps_exact_registered_html_and_preserves_scope(native, monkeypatch):
    add_source(native)
    bridge = native.tools.call("get_html_navigation", {"source_id": "exact-citation"})
    assert bridge["matches"][0]["node_id"] == "wall-a"
    assert bridge["matches"][0]["ancestor_titles"][-1].endswith("Asymptotic Scattering")
    assert bridge["is_read_evidence"] is False
    # Word order is immaterial in BOTH the normal and exact-source entrances.
    ordered = native.tools.call("get_html_navigation", {"source_id": "exact-citation", "query": "Wall Scattering"})
    assert ordered["matches"][0]["node_id"] == "wall-a"
    assert ordered["query_match_mode"] == "all_whitespace_terms_in_title_path"
    with pytest.raises(ValueError):
        native.tools.call("get_html_navigation", {"node_id": "wall-a", "source_id": "exact-citation"})
    monkeypatch.setattr(native.store, "evaluation_policy", lambda: HoldoutPolicy(citation_ids=frozenset(["exact-citation"])))
    with pytest.raises(PermissionError):
        native.tools.call("get_html_navigation", {"source_id": "exact-citation"})


def test_empty_title_query_explains_matching_and_keeps_refinement_in_scope(native):
    result = native.tools.call("get_html_navigation", {"node_id": "paas", "query": "several unrelated body words"})
    assert result["total"] == 0 and not result["body_searched"]
    assert result["refinement"]["browse"]["arguments"]["node_id"] == "paas"
    assert result["query_match_mode"] == "all_whitespace_terms_in_title_path"
    assert output_bytes(result) <= 7000


def test_source_id_bridge_returns_matches_for_same_document_anchors_and_no_pdf_guesses(native):
    add_source(native)
    n = deepcopy(native.nodes["wall-a"])
    n.update(node_id="wall-anchor", anchor="wall")
    native.nodes["wall-anchor"] = n
    bridge = native.tools.call("get_html_navigation", {"source_id": "exact-citation"})
    assert {item["node_id"] for item in bridge["matches"]} == {"wall-a", "wall-anchor"}
    add_source(native, "pdf-citation", relative="manual.pdf")
    assert native.tools.call("get_html_navigation", {"source_id": "pdf-citation"})["state"] == "not_prepared"
    add_source(native, "stale-citation", asset_hash="0" * 64)
    assert native.tools.call("get_html_navigation", {"source_id": "stale-citation"})["state"] == "not_prepared"


def test_image_and_equation_refs_are_explicitly_untranscribed_and_links_cannot_open_tutorials(native):
    native.refs.update(images=[{"src": "formula.png", "alt": ""}], eqrefs=[{"href": "#eq1"}],
                       links=[{"href": "../com.comsol.help.models.aco/hidden.html", "text": "Tutorial"},
                              {"href": "wall-b.html", "text": "Other wall"}])
    body = native.tools.call("read_html_section", {"node_id": "wall-a"})
    assert body["non_text_content"]["image_not_transcribed"] is True
    assert body["non_text_content"]["equation_refs_not_visually_verified"] is True
    assert body["references"]["links"]["samples"][0]["accessible_by_text_tool"] is False
    assert body["references"]["links"]["samples"][1]["node_id"] == "wall-b"


def test_image_refs_match_current_text_range_and_show_remaining_count(native):
    n = native.nodes["wall-a"]
    text = "a" * 100 + " [image:late] " + "b" * 100
    path = native.root / n["doc_path"]
    path.write_text(text, encoding="utf-8")
    n["doc_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    native.refs["images"] = [{"id": "early", "src": "early.png", "text_start": 0, "text_end": 10},
        {"id": "late", "src": "late.png", "text_start": 100, "text_end": 114},
        *[{"id": f"nearby-{i}", "src": f"nearby-{i}.png", "text_start": 116 + i, "text_end": 117 + i} for i in range(5)]]
    early = native.tools.call("read_html_section", {"node_id": "wall-a", "offset": 0, "length": 50})
    assert [ref["id"] for ref in early["references"]["images"]["samples"]] == ["early"]
    later = native.tools.call("read_html_section", {"node_id": "wall-a", "offset": 100, "length": 100})
    refs = later["references"]["images"]
    assert refs["range_count"] == 6 and refs["remaining_in_range"] == 3
    assert refs["samples_scope"] == "returned_text_range"
    assert refs["samples"][0]["id"] == "late" and refs["samples"][0]["src"] == "late.png"
    assert all(ref["id"] != "early" for ref in refs["samples"])
    assert later["non_text_content"]["image_not_transcribed"] is True
    assert output_bytes(later) <= 7000


def test_planning_service_exposes_bound_and_unbound_tools_without_legacy_read_credit(native, tmp_path):
    definitions = {item["name"]: item for item in planning_definitions()}
    for name in ("get_html_navigation", "read_html_section"):
        assert name in definitions
        assert "session_id" not in definitions[name]["inputSchema"]["required"]
    service = PlanningService(tmp_path / "workspace", SimpleNamespace(get=lambda sid: {"id": sid, "revision": 1}), SimpleNamespace(store=native.store))
    assert service.call_public("get_html_navigation", {})["state"] == "ready"
    page = service.call_public("read_html_section", {"session_id": "task", "node_id": "wall-a"})
    assert page["evidence_refs"][0]["context"] == "html_section"
    assert service.read_sources("task") == []


def test_heldout_path_is_denied_before_original_body_is_opened(native, monkeypatch):
    n = native.nodes["wall-a"]
    monkeypatch.setattr(native.store, "evaluation_policy", lambda: HoldoutPolicy(document_relpaths=frozenset([n["doc_path"]])))
    original_read_bytes = Path.read_bytes

    def read_bytes(path):
        assert path != native.root / n["doc_path"], "heldout original body must not be opened"
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(PermissionError):
        native.tools.call("read_html_section", {"node_id": "wall-a"})


def test_real_native_index_and_original_html_preserve_interface_section_and_image_scope(tmp_path):
    from advisor.html_tree import build_html_tree
    root = tmp_path / "native"
    package = root / "com.comsol.help.aco"
    package.mkdir(parents=True)
    (package / "toc.xml").write_text('<toc label="Acoustics"><topic label="Asymptotic Interface" href="paas.html#intro">'
        '<topic label="Wall" href="paas.html#wall"/></topic></toc>', encoding="utf-8")
    (package / "paas.html").write_text('<body><h1 id="intro">Asymptotic Interface</h1><p>Introduction.</p>'
        '<h2 id="wall">Wall</h2><p>Angle-dependent boundary text.</p><img src="formula1.png" alt="">'
        '<h2 id="next">Next section</h2><p>Other body must not leak into Wall.</p></body>', encoding="utf-8")
    store = LibraryStore(tmp_path / "libraries")
    store.register("Original manuals", "official-manuals", [{"id": "help", "path": str(root)}], library_id="native-manuals")
    store.set_enabled("native-manuals", True)
    built = build_html_tree(root, tmp_path / "tree", library_id="native-manuals", root_id="help")
    tools = HtmlTreeTools(store, index_path=built["index_path"])
    nav = tools.call("get_html_navigation", {"query": "Wall"})
    page = tools.call("read_html_section", {"node_id": nav["items"][0]["node_id"]})
    assert page["ancestor_titles"] == ["Acoustics", "Asymptotic Interface"]
    assert "Angle-dependent boundary text." in page["text"] and "Other body" not in page["text"]
    assert page["read_scope"] == "html_heading_section"
    image = page["references"]["images"]["samples"][0]
    assert "[" + image["id"] + "]" in page["text"] and image["src"] == "formula1.png"
    assert page["references"]["images"]["samples_scope"] == "returned_text_range"
    assert page["non_text_content"]["image_not_transcribed"] is True
    metadata = dict(page, text="")
    assert output_bytes(metadata) <= 3000
