"""Tutorial ownership follows exact COMSOL sources, never a shared basename."""
import hashlib
from pathlib import Path

import pytest

from advisor import tutorial_sources as tutorials
from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.library_search import _representation
from advisor.library_text import extract_text_objects


HTML = '''<html><head><title>Bracket tutorial</title></head><body>
<a href="models.sme.bracket.pdf"><img alt="PDF" src="pdf.png"/></a>
<div><div class="Head1"><a name="title">Bracket tutorial</a></div>
<div class="Head2_ML"><a name="intro">Introduction</a></div>
<div class="Body_text"><a name="intro-body">Unique introduction.</a></div>
<div class="Head2_ML"><a name="implementation">Notes About the COMSOL Implementation</a></div>
<div class="Body_text">Preserve arbitrary explanatory sections.</div>
<div class="Bullets-first_outer"><table><tr><td><div class="Bullets-first_inner">•</div></td>
<td><div class="Bullets-first_inner"><a name="coupled">One coupled-system explanation.</a></div></td></tr></table></div>
<table><tr><th>Parameter</th><th>Value</th></tr><tr><td>hmax</td><td>0.2[mm]</td></tr></table>
<div class="Head2_ML"><a name="instructions">Modeling Instructions</a></div>
<div class="Body_text">Start the model.</div>
<div class="Head3"><a name="mesh">Mesh</a></div>
<div class="Head4"><a name="size">Size</a></div>
<div class="Num_list-first_outer"><table><tr><td><div class="Num_list-first_inner">1</div></td>
<td><div class="Num_list-first_inner"><a name="set-size">Set the size once.</a></div></td></tr></table></div>
<p>Keep the final paragraph.</p><script>DO_NOT_INDEX_SCRIPT</script></div>
</body></html>'''


def fixture(root, *, namespace="sme", name="bracket", html=HTML):
    package = root / f"com.comsol.help.models.{namespace}.{name}"
    package.mkdir(parents=True, exist_ok=True)
    source = package / f"{name}.html"
    source.write_text(html, encoding="utf-8")
    pdf = package / f"models.{namespace}.{name}.pdf"
    pdf.write_bytes(b"PDF has independent source bytes; never parse these as a PDF")
    return source, pdf


@pytest.mark.parametrize("path,identity", [
    ("doc/com.comsol.help.models.sme.bracket/bracket.html", "models.sme.bracket"),
    (r"D:\COMSOL63\doc\com.comsol.help.models.sme.bracket\models.sme.bracket.pdf", "models.sme.bracket"),
    ("https://doc.comsol.com/6.3/doc/models.sme.bracket.pdf?download=1", "models.sme.bracket"),
    ("com.comsol.help.models.sme.bracket/bracket_geom_sequence.html", None),
    ("com.comsol.help.models.heat.bracket/models.sme.bracket.pdf", None),
    ("com.comsol.help.models.heat.bracket/bracket_thermal.html", None),
    ("com.comsol.help.sme/sme_ug.pdf", None),
    ("manuals/bracket.pdf", None),
])
def test_identity_requires_complete_namespace_and_canonical_filename(path, identity):
    assert tutorials.tutorial_identity(path) == identity


def test_exact_package_and_root_pdf_formats_have_one_existing_companion(tmp_path):
    source, pdf = fixture(tmp_path)
    assert tutorials.choose_companion_html(pdf, tmp_path) == source
    download = tmp_path / pdf.name
    download.write_bytes(b"alternate download copy")
    assert tutorials.choose_companion_html(download, tmp_path) == source
    assert tutorials.companion_pdf_paths(source, tmp_path) == [pdf, download]
    # A registered package root cannot silently reach outside its boundary.
    assert tutorials.companion_pdf_paths(source, source.parent) == [pdf]


def test_matching_never_crosses_language_version_module_or_record_boundary(tmp_path):
    source, pdf = fixture(tmp_path / "6.3/en/doc")
    foreign, _ = fixture(tmp_path / "6.4/zh/doc")
    other, _ = fixture(tmp_path / "6.3/en/doc", namespace="heat")
    records = [{"path": str(foreign)}, {"path": str(other)}]
    assert tutorials.choose_companion_html(pdf, tmp_path, records) is None
    assert tutorials.choose_companion_html(pdf, tmp_path / "6.4/zh/doc") is None
    assert tutorials.choose_companion_html(pdf, source.parent, [{"relative_path": source.name}]) == source
    source.unlink()
    assert tutorials.choose_companion_html(pdf, tmp_path) is None


@pytest.mark.parametrize("html", ["", "<html><title>Title only</title><body></body></html>", "<script>Only a script</script>"])
def test_empty_html_never_replaces_pdf(tmp_path, html):
    _, pdf = fixture(tmp_path, html=html)
    assert tutorials.choose_companion_html(pdf, tmp_path) is None


def test_ambiguous_html_extensions_do_not_replace_pdf(tmp_path):
    source, pdf = fixture(tmp_path)
    source.with_suffix(".htm").write_text(HTML, encoding="utf-8")
    assert tutorials.choose_companion_html(pdf, tmp_path) is None


def test_structured_sections_preserve_arbitrary_text_and_tables_once(tmp_path):
    source, pdf = fixture(tmp_path)
    before = source.read_bytes(), pdf.read_bytes(), source.stat().st_mtime_ns, pdf.stat().st_mtime_ns
    result = extract_text_objects(source, root=tmp_path)
    objects = result["objects"]
    text = "\n".join(item["text"] for item in objects)
    for marker in ("Unique introduction.", "Preserve arbitrary explanatory sections.", "One coupled-system explanation.",
                   "hmax", "0.2[mm]", "Start the model.", "Set the size once.", "Keep the final paragraph."):
        assert text.count(marker) == 1
    assert "DO_NOT_INDEX_SCRIPT" not in text
    assert "Parameter | Value" in text
    assert any(item["title"] == "Notes About the COMSOL Implementation" for item in objects)
    mesh = next(item for item in objects if item["title"] == "Mesh")
    assert mesh["type"] == "method_step" and mesh["data"]["feature_branch"] == "mesh"
    assert "Size" in mesh["text"]
    assert any(evidence["locator"] == "html:#set-size" for evidence in mesh["evidence"])
    assert not result["coverage"]["full_topic_fallback"]
    assert all(item["local_id"] != "topic" for item in objects)
    assert sum("tutorial" in item["data"] for item in objects) == 1
    assert before == (source.read_bytes(), pdf.read_bytes(), source.stat().st_mtime_ns, pdf.stat().st_mtime_ns)


def test_pdf_metadata_uses_its_own_hash_and_is_not_indexed_as_text(tmp_path):
    source, pdf = fixture(tmp_path)
    result = tutorials.extract_tutorial_objects(source)
    metadata = result["tutorial"]
    assert metadata["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    alternate = metadata["alternate_sources"][0]
    assert alternate["source_sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert alternate["source_sha256"] != metadata["source_sha256"]
    assert metadata["aliases"] == [pdf.as_uri()]
    assert "PDF has independent source bytes" not in "".join(item["text"] for item in result["objects"])


def test_long_chapters_and_unrecognized_pages_keep_every_character_in_search_budget(tmp_path):
    paragraph = " ".join(f"UNIQUE_TOKEN_{index}" for index in range(600))
    source, _ = fixture(tmp_path, html='<title>Long tutorial</title><h2 id="chapter">Unusual Chapter</h2><p id="long">' + paragraph + '</p>')
    structured = tutorials.extract_tutorial_objects(source)
    assert "".join(item["text"] for item in structured["objects"]) == paragraph
    assert not structured["coverage"]["full_topic_fallback"]
    source.write_text('<title>Long tutorial</title><article>' + paragraph + '</article>', encoding="utf-8")
    fallback = tutorials.extract_tutorial_objects(source)
    assert "".join(item["text"] for item in fallback["objects"]) == paragraph
    assert fallback["coverage"]["full_topic_fallback"]
    for result in (structured, fallback):
        assert len(result["objects"]) > 1
        assert len({item["local_id"] for item in result["objects"]}) == len(result["objects"])
        for item in result["objects"]:
            assert len(item["text"]) <= tutorials.MAX_SECTION_CHARS
            item = dict(item, version="6.3", asset_key="docs:" + source.relative_to(tmp_path).as_posix(), section_path=["Official manuals", item["title"]])
            assert not _representation(item)[1]["truncated"]


def test_referral_page_is_one_full_topic_and_anchor_ids_survive_new_lines(tmp_path):
    source, _ = fixture(tmp_path, html='<title>Geometry template</title><div class="Head1">Geometry template</div><p>Refer to the official module book for full instructions.</p>')
    referral = tutorials.extract_tutorial_objects(source)
    assert referral["coverage"]["full_topic_fallback"]
    assert len(referral["objects"]) == 1 and "Refer to the official module book" in referral["objects"][0]["text"]
    source.write_text(HTML, encoding="utf-8")
    original = tutorials.extract_tutorial_objects(source)
    source.write_text("\n\n\n" + HTML, encoding="utf-8")
    updated = tutorials.extract_tutorial_objects(source)
    assert [item["local_id"] for item in original["objects"]] == [item["local_id"] for item in updated["objects"]]


def test_reference_is_metadata_only_and_canonical_key_is_prefix_and_content_scoped(tmp_path):
    source, _ = fixture(tmp_path / "6.3/en/doc")
    other, _ = fixture(tmp_path / "6.4/en/doc")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    reference = tutorials.tutorial_reference(source, source_sha256=sha, title="Saved title", chapters=[{"title": "Mesh", "locator": "html:#mesh"}])
    assert reference["source_uri"] == source.as_uri()
    assert reference["document_identifier"] == "models.sme.bracket"
    assert reference["relation_status"] == "reference_not_verified_model_agreement"
    assert not {"text", "body", "instructions"} & reference.keys()
    assert tutorials.canonical_tutorial_key(source, sha) != tutorials.canonical_tutorial_key(other, sha)
    assert tutorials.canonical_tutorial_key(source, sha) != tutorials.canonical_tutorial_key(source, "changed")
    assert tutorials.canonical_tutorial_key(source, sha) == tutorials.canonical_tutorial_key(source.as_uri(), sha)
    assert tutorials.canonical_tutorial_key("https://one.example/6.3/doc/models.sme.bracket.pdf", sha) != tutorials.canonical_tutorial_key("https://two.example/6.3/doc/models.sme.bracket.pdf", sha)


def test_cancelled_matching_and_extraction_do_not_modify_sources(tmp_path):
    source, pdf = fixture(tmp_path)
    marker = tmp_path / "cancel"
    marker.touch()
    before = source.read_bytes(), pdf.read_bytes()
    with build_control(BuildControl(marker)):
        with pytest.raises(BuildCancelled):
            tutorials.choose_companion_html(pdf, tmp_path)
        with pytest.raises(BuildCancelled):
            tutorials.extract_tutorial_objects(source)
    assert before == (source.read_bytes(), pdf.read_bytes())
