from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from advisor import library_text
from advisor.build_control import BuildCancelled


def topic(root, package, name="topic", label="Original"):
    folder = root / package
    folder.mkdir(exist_ok=True)
    path = folder / f"{name}.html"
    path.write_text(f'<title>{name}</title><p>Documented text</p><img src="eq.png">', encoding="utf-8")
    (folder / "toc.xml").write_text(
        f'<toc label="Manual"><topic label="{label}" href="{name}.html"/></toc>', encoding="utf-8")
    return path


def section(path, root):
    return library_text.extract_text_objects(path, root=root)["objects"][0]["data"].get("section_path")


def test_build_session_reads_tocs_once_and_html_once(tmp_path):
    first = topic(tmp_path, "manual")
    second = first.with_name("another.html")
    second.write_text('<title>Another</title><p>Text</p>', encoding="utf-8")
    read_text = Path.read_text
    html_reads = []

    def counted_read(path, *args, **kwargs):
        if path.suffix == ".html":
            html_reads.append(path)
        return read_text(path, *args, **kwargs)

    with patch.object(library_text, "_read_tocs", wraps=library_text._read_tocs) as tocs:
        with patch.object(Path, "read_text", counted_read), library_text.text_extraction_session():
            assert section(first, tmp_path) == ["Manual", "Original"]
            library_text.extract_text_objects(second, root=tmp_path / ".")
        assert tocs.call_count == 1
    assert html_reads == [first, second]


def test_next_build_refreshes_changed_added_and_removed_tocs(tmp_path):
    first = topic(tmp_path, "manual")
    with library_text.text_extraction_session():
        assert section(first, tmp_path) == ["Manual", "Original"]
        topic(tmp_path, "manual", label="Changed")
        added = topic(tmp_path, "new_manual", label="Added")
        assert section(first, tmp_path) == ["Manual", "Original"]
        assert section(added, tmp_path) is None

    with library_text.text_extraction_session():
        assert section(first, tmp_path) == ["Manual", "Changed"]
        assert section(added, tmp_path) == ["Manual", "Added"]

    (first.parent / "toc.xml").unlink()
    with library_text.text_extraction_session():
        assert section(first, tmp_path) is None
        assert section(added, tmp_path) == ["Manual", "Added"]


def test_standalone_reads_refresh_and_failed_session_releases_cache(tmp_path):
    first = topic(tmp_path, "manual")
    with pytest.raises(RuntimeError), library_text.text_extraction_session():
        assert section(first, tmp_path) == ["Manual", "Original"]
        raise RuntimeError("failed build")
    topic(tmp_path, "manual", label="Updated")
    assert section(first, tmp_path) == ["Manual", "Updated"]
    topic(tmp_path, "manual", label="Latest")
    assert section(first, tmp_path) == ["Manual", "Latest"]


def test_registered_package_root_uses_its_own_toc(tmp_path):
    first = topic(tmp_path, "manual")
    with library_text.text_extraction_session():
        assert section(first, first.parent) == ["Manual", "Original"]
        assert section(first, tmp_path) == ["Manual", "Original"]


@pytest.mark.parametrize("failure", [None, "cancel", "extract"])
def test_pdf_reader_closes_on_success_cancellation_and_extraction_failure(tmp_path, monkeypatch, failure):
    closed = []

    class Page:
        def extract_text(self):
            if failure == "extract":
                raise ValueError("broken page")
            return "Documented page"

    class Reader:
        @property
        def pages(self):
            assert not closed
            return [Page(), Page()]

        def close(self):
            closed.append(True)

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=lambda path: Reader()))
    checks = 0

    def check():
        nonlocal checks
        checks += 1
        if failure == "cancel" and checks == 2:
            raise BuildCancelled()

    monkeypatch.setattr(library_text, "check_cancelled", check)
    if failure:
        with pytest.raises(BuildCancelled if failure == "cancel" else ValueError):
            library_text.extract_text_objects(tmp_path / "manual.pdf")
    else:
        result = library_text.extract_text_objects(tmp_path / "manual.pdf")
        assert result["coverage"]["pages"] == 2
        assert len(result["objects"]) == 2
    assert closed == [True]
