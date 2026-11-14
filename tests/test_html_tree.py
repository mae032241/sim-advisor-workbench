"""Synthetic native TOC navigation, complete scoped bodies and path boundaries."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from advisor.html_tree import HtmlTreeIndex, SCHEMA, build_html_tree, read_html_text
from scripts.build_html_tree import main


ACO = "com.comsol.help.aco"
OTHER = "com.comsol.help.other"


def package(root, name=ACO, topics=None):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "toc.xml").write_text('<toc xmlns="urn:Eclipse-TOC-Schema" label="Native ' + name + '">' + (topics or "") + '</toc>', encoding="utf-8")
    return directory


@pytest.fixture
def manuals(tmp_path):
    root = tmp_path / "native-html"
    directory = package(root, topics=f'''
      <topic label="Pressure interface" href="/{ACO}/pressure.html#intro">
        <topic label="Wall" href="pressure.html#wall"/>
        <topic label="Second child" href="pressure.html#second"/>
      </topic>
      <topic label="Wave interface" href="wave.html#intro"><topic label="Wall" href="wave.html#wall"/></topic>
      <topic label="Shared page" href="pressure.html#wall"/>
      <topic label="Navigation only"/>
      <topic label="Missing page" href="missing.html"/>
      <topic label="External" href="https://example.invalid/manual.html"/>
      <topic label="Excluded tutorial" href="/com.comsol.help.models.demo/demo.html"/>
    ''')
    directory.joinpath("pressure.html").write_text('''<html><head><title>Pressure page</title><script>HEAD_SECRET</script></head><body>
      <h1 id="intro">Pressure interface</h1><p>Introduction only.</p>
      <div class="Head2_ML"><a name="wall"></a>Wall</div><p>Wall complete body.</p>
      <h3 id="sub">Subtopic</h3><p>Nested body retained.</p>
      <div class="Head2_ML"><a name="second"></a>Second heading</div><p>Other section body.</p>
      </body></html>''', encoding="utf-8")
    directory.joinpath("wave.html").write_text('<body><h1 id="intro">Wave interface</h1><h2 id="wall">Wall</h2><p>Different wall body.</p></body>', encoding="utf-8")
    generic = package(root, OTHER, '<topic label="Other manual" href="other.html"/>')
    generic.joinpath("other.html").write_text('<body><h1>Other manual</h1>Another body.</body>', encoding="utf-8")
    package(root, "com.comsol.help.models.demo", '<topic label="Private example" href="model.html"/>')
    package(root, "com.comsol.help.addins.demo", '<topic label="Add-in" href="addin.html"/>')
    return root


def build(manuals, tmp_path, **kwargs):
    result = build_html_tree(manuals, tmp_path / "tree", library_id="synthetic-manuals", **kwargs)
    return result, HtmlTreeIndex(result["index_path"])


def test_native_namespace_tree_order_siblings_and_same_wall_names(manuals, tmp_path):
    result, index = build(manuals, tmp_path)
    with index:
        roots = index.list_children()
        assert result["package_count"] == 2 and roots["total"] == 2
        assert all(item["parent_id"] is None and item["doc_path"] is None for item in roots["items"])
        top = roots["items"][0]
        children = index.list_children(top["node_id"], limit=2)
        assert [item["title"] for item in children["items"]] == ["Pressure interface", "Wave interface"]
        assert children["has_more"] and children["next_offset"] == 2
        pressure, wave = children["items"]
        assert pressure["next_sibling_id"] == wave["node_id"]
        assert wave["previous_sibling_id"] == pressure["node_id"]
        wall = index.list_children(pressure["node_id"])["items"][0]
        wave_wall = index.list_children(wave["node_id"])["items"][0]
        assert wall["title"] == wave_wall["title"] == "Wall"
        assert wall["node_id"] != wave_wall["node_id"] and wall["doc_path"] != wave_wall["doc_path"]
        assert wall["ancestor_titles"] == [top["title"], "Pressure interface"]
        assert wall["ancestor_ids"] == [top["node_id"], pressure["node_id"]]
        assert wall["depth"] == 2 and wall["sibling_order"] == 0
        assert pressure["next_id"] == wall["node_id"]
        assert index.node(wall["node_id"]) == wall
        with pytest.raises(sqlite3.OperationalError):
            index._db.execute("DELETE FROM nodes")


def test_title_ancestor_navigation_is_scoped_and_never_body_search(manuals, tmp_path):
    _, index = build(manuals, tmp_path)
    with index:
        result = index.list_children(query="Pressure Wall")
        assert result["scope"] == "title_navigation" and result["returned"] == 1
        assert result["items"][0]["ancestor_titles"][-1] == "Pressure interface"
        assert index.list_children(query="Different wall body")["total"] == 0
        pressure = index.list_children(query="Pressure interface")["items"][0]
        assert index.list_children(pressure["node_id"], query="Wall")["total"] == 1
        assert index.list_children(pressure["node_id"], query="Wave")["total"] == 0
        assert index.list_children(offset=99)["items"] == []


def test_title_metadata_cache_reuses_index_version_and_invalidates_on_change(manuals, tmp_path):
    _, index = build(manuals, tmp_path)
    path = index.path
    with index:
        first = index.list_children(query="Pressure Wall")
        wall = first["items"][0]
        statements = []
        index._db.set_trace_callback(statements.append)
        assert index.list_children(query="Wall Pressure")["items"][0]["node_id"] == wall["node_id"]
        assert not any("SELECT node_id,ancestor_ids,ancestor_titles,title FROM nodes" in sql for sql in statements)
        assert first["query_match_mode"] == "all_whitespace_terms_in_title_path"
    # A new metadata version must not keep the previous title search result.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE nodes SET title='Renamed surface' WHERE node_id=?", (wall["node_id"],))
    with HtmlTreeIndex(path) as fresh:
        assert fresh.list_children(query="Pressure Wall")["total"] == 0
        assert fresh.list_children(query="Renamed Pressure")["total"] == 1


def test_missing_external_empty_and_excluded_topics_stay_explicit(manuals, tmp_path):
    result, index = build(manuals, tmp_path)
    manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
    assert manifest["schema"] == SCHEMA and manifest["ready"]
    assert set(record["package"] for record in manifest["packages"]) == {ACO, OTHER}
    with index:
        top = index.list_children()["items"][0]
        children = index.list_children(top["node_id"], limit=50)["items"]
        blocked = {n["title"]: n for n in children if not n["readable"]}
        assert blocked["Navigation only"]["doc_path"] is None
        assert blocked["Missing page"]["blocked_reason"] == "missing_html"
        assert blocked["Missing page"]["doc_path"] == ACO + "/missing.html"
        assert blocked["External"]["blocked_reason"] == "external_reference"
        assert blocked["Excluded tutorial"]["blocked_reason"] == "excluded_package"
        assert all(n["doc_sha256"] is None for n in blocked.values())
    assert manifest["unavailable_reasons"]["missing_html"] == 1
    assert manifest["unavailable_reasons"]["external_reference"] == 1
    assert any(w["reason"] == "navigation_only" for w in manifest["warnings"])


def test_index_is_relocatable_metadata_only_and_ids_bind_content(manuals, tmp_path):
    result, index = build(manuals, tmp_path)
    with index:
        before = index.resolve_source_path(ACO + "/pressure.html")
        assert len(before["nodes"]) == 4
        assert {n["anchor"] for n in before["nodes"]} == {"intro", "wall", "second"}
        assert set(index.meta) == {"schema", "library_id", "root_id"}
    for path in (Path(result["index_path"]), Path(result["manifest_path"])):
        raw = path.read_bytes()
        assert str(manuals).encode() not in raw and b"Wall complete body" not in raw
    assert len(list((tmp_path / "tree").iterdir())) == 2
    relocated = tmp_path / "relocated"
    shutil.copytree(manuals, relocated)
    second = build_html_tree(relocated, tmp_path / "tree2", library_id="synthetic-manuals")
    with HtmlTreeIndex(second["index_path"]) as other:
        assert other.resolve_source_path(ACO + "/pressure.html") == before
    (relocated / ACO / "pressure.html").write_text('<body>Changed revision.</body>', encoding="utf-8")
    third = build_html_tree(relocated, tmp_path / "tree3", library_id="synthetic-manuals")
    with HtmlTreeIndex(third["index_path"]) as changed:
        after = changed.resolve_source_path(ACO + "/pressure.html")
        assert {n["node_id"] for n in before["nodes"]}.isdisjoint(n["node_id"] for n in after["nodes"])
        assert after["nodes"][0]["doc_sha256"] == hashlib.sha256((relocated / ACO / "pressure.html").read_bytes()).hexdigest()


def test_heading_anchor_stops_at_next_same_level_and_keeps_nested_headings(manuals):
    value = read_html_text(manuals / ACO / "pressure.html", "wall")
    assert value["section_title"] == "Wall" and value["read_scope"] == "html_heading_section"
    assert "Wall complete body." in value["text"] and "Nested body retained." in value["text"]
    assert "Introduction only." not in value["text"] and "Other section body." not in value["text"]
    assert value["scope"]["ends_before_heading"] == "Second heading"
    assert [h["title"] for h in value["headings"]] == ["Wall", "Subtopic"]
    assert value["anchor_found"] and value["full_page_available"] and not value["text_truncated"]
    with pytest.raises(ValueError, match="no full-page fallback"):
        read_html_text(manuals / ACO / "pressure.html", "not-present")


def test_empty_preheading_anchor_and_element_anchor_have_exact_ranges(tmp_path):
    p = tmp_path / "page.html"
    p.write_text('<body><h1>Topic</h1><p>Before.</p><a name="empty"></a><h2>Chosen</h2><p>Keep me.</p><h2>Next</h2><div id="box"><p>Only box.</p></div><p>Outside box.</p></body>', encoding="utf-8")
    chosen = read_html_text(p, "#empty")
    assert chosen["section_title"] == "Chosen" and "Keep me." in chosen["text"]
    assert "Before." not in chosen["text"] and "Outside box." not in chosen["text"]
    box = read_html_text(p, "box")
    assert box["read_scope"] == "html_anchor_element" and box["text"] == "Only box."


def test_full_long_body_tables_entities_and_preformatted_text_are_not_previewed(tmp_path):
    p = tmp_path / "long.html"
    tokens = ["TOKEN_" + str(i) for i in range(1600)]
    p.write_text('<html><head><title>Long native page</title></head><body><h1>Complete page</h1><p>' + " ".join(tokens) + '</p><table><tr><th>Quantity</th><th>Value</th></tr><tr><td>density</td><td>1 &amp; 2</td></tr></table><pre>A  B\n  C</pre><script>DO_NOT_READ_SCRIPT</script><p>LAST_BODY_MARKER</p></body></html>', encoding="utf-8")
    value = read_html_text(p)
    assert len(value["text"]) > 12000 and all(token in value["text"] for token in tokens)
    assert "Quantity | Value" in value["text"] and "density | 1 & 2" in value["text"]
    assert "A  B\n  C" in value["text"].replace("\r\n", "\n") and "LAST_BODY_MARKER" in value["text"]
    assert "DO_NOT_READ_SCRIPT" not in value["text"] and value["read_scope"] == "html_full_page"


def test_images_alt_equation_limits_offsets_and_reference_only_links(tmp_path, monkeypatch):
    p = tmp_path / "images.html"
    p.write_text('''<body><h1 id="one">Images</h1><p>Before <img src="figure.png" alt=""> after.</p>
      <div class="Equation"><img class="EquationImage" src="native17.png" alt=""></div>
      <img src="icon.svg" alt="Next &amp; Previous"><img src="eq_3.png" alt="E = m c²">
      <math alttext="x squared"><msup><mi>x</mi><mn>2</mn></msup></math>
      <a href="https://example.invalid/external">Remote reference</a>
      <a href="file:///private/secret.html">File reference</a><a href="../other.html#one">Local reference</a>
      <h1 id="two">Other scope</h1><img src="outside.png" alt="Outside image"></body>''', encoding="utf-8")
    original = Path.read_bytes
    reads = []
    def observed(path):
        reads.append(path)
        assert path == p
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", observed)
    value = read_html_text(p, "one")
    assert reads == [p] and len(value["images"]) == 4
    assert value["images"][0]["is_equation"] is False
    assert value["images"][1]["is_equation"] is True and value["images"][1]["original_class"] == "EquationImage"
    assert all(i["image_content_not_transcribed"] for i in value["images"])
    assert "Next & Previous" in value["text"] and "E = m c²" in value["text"]
    assert "outside.png" not in json.dumps(value)
    for image in value["images"]:
        assert value["text"][image["text_start"]:image["text_end"]].startswith("[" + image["id"] + "]")
        assert image["html_line"] >= 1 and image["followed"] is False
    equations = [e for e in value["eqrefs"] if e["kind"] == "image"]
    assert equations[0]["missing_formula_text"] is True and equations[1]["missing_formula_text"] is False
    assert next(e for e in value["eqrefs"] if e["kind"] == "mathml")["semantic_rendering_verified"] is False
    assert all(link["followed"] is False for link in value["links"])
    assert next(link for link in value["links"] if link["href"].startswith("file:"))["blocked"]


@pytest.mark.parametrize("href", ["../../escape.html", "/../escape.html", "/%2e%2e/escape.html", "../../../outside.html"])
def test_toc_path_escape_is_rejected_before_any_outside_file_read(tmp_path, href):
    root = tmp_path / "html"
    package(root, topics='<topic label="Escape" href="' + href + '"/>')
    with pytest.raises(ValueError, match="escapes"):
        build_html_tree(root, tmp_path / "tree", library_id="manuals")


def test_cross_package_native_href_keeps_parent_and_anchor(tmp_path):
    root = tmp_path / "html"
    package(root, topics=f'<topic label="Local parent" href="page.html"><topic label="Cross" href="../{OTHER}/other.html#part"/><topic label="Same page anchor" href="#part"/></topic>')
    (root / ACO / "page.html").write_text('<body><h1 id="part">Local part</h1></body>', encoding="utf-8")
    package(root, OTHER)
    (root / OTHER / "other.html").write_text('<body><h1 id="part">Cross part</h1></body>', encoding="utf-8")
    result = build_html_tree(root, tmp_path / "tree", library_id="manuals", packages=[ACO])
    with HtmlTreeIndex(result["index_path"]) as index:
        top = index.list_children()["items"][0]
        parent = index.list_children(top["node_id"])["items"][0]
        cross, same = index.list_children(parent["node_id"])["items"]
        assert cross["parent_id"] == parent["node_id"] and cross["doc_path"] == OTHER + "/other.html"
        assert cross["anchor"] == same["anchor"] == "part" and same["doc_path"] == ACO + "/page.html"


def test_source_lookup_and_direct_read_reject_traversal_urls_and_bad_types(manuals, tmp_path):
    result, index = build(manuals, tmp_path)
    with index:
        for value in ("../secret.html", "/absolute.html", "file:///secret.html", "https://example.invalid/secret.html", "x\\y.html", ACO + "/pressure.html#wall"):
            with pytest.raises(ValueError):
                index.resolve_source_path(value)
        with pytest.raises(ValueError):
            index.list_children(offset=True)
        with pytest.raises(ValueError):
            index.list_children(limit=0)
        with pytest.raises(KeyError):
            index.node("html-" + "0" * 64)
    for path in ("https://example.invalid/a.html", "file:///private/a.html", tmp_path / ".." / "a.html"):
        with pytest.raises(ValueError):
            read_html_text(path)
    with pytest.raises(ValueError):
        read_html_text(Path(result["manifest_path"]))


def test_fresh_output_package_gates_and_xml_entity_declarations(manuals, tmp_path):
    build(manuals, tmp_path)[1].close()
    with pytest.raises(FileExistsError):
        build_html_tree(manuals, tmp_path / "tree", library_id="synthetic-manuals")
    with pytest.raises(ValueError, match="outside"):
        build_html_tree(manuals, manuals / "generated", library_id="synthetic-manuals")
    for name in ("com.comsol.help.models.demo", "com.comsol.help.addins.demo", "../escape"):
        with pytest.raises(ValueError, match="generic"):
            build_html_tree(manuals, tmp_path / "excluded", library_id="synthetic-manuals", packages=[name])
    (manuals / ACO / "toc.xml").write_text('<!DOCTYPE toc [<!ENTITY secret SYSTEM "file:///secret">]><toc/>', encoding="utf-8")
    with pytest.raises(ValueError, match="entity"):
        build_html_tree(manuals, tmp_path / "bad-xml", library_id="synthetic-manuals", packages=[ACO])


def test_index_and_body_refuse_symlink_aliases_when_supported(manuals, tmp_path):
    target = manuals / ACO / "pressure.html"
    alias = manuals / ACO / "alias.html"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Creating a local symlink is unavailable")
    with pytest.raises(ValueError, match="aliases"):
        read_html_text(alias)
    (manuals / ACO / "toc.xml").write_text('<toc><topic label="Alias" href="alias.html"/></toc>', encoding="utf-8")
    with pytest.raises(ValueError, match="aliases"):
        build_html_tree(manuals, tmp_path / "alias-tree", library_id="manuals", packages=[ACO])


def test_cli_builds_only_requested_generic_package(manuals, tmp_path, capsys):
    assert main(["--html-root", str(manuals), "--output", str(tmp_path / "cli-tree"),
                 "--library-id", "manuals", "--root-id", "native", "--package", ACO]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["package_count"] == 1 and summary["ready"]
    with HtmlTreeIndex(summary["index_path"]) as index:
        assert index.meta["root_id"] == "native"
