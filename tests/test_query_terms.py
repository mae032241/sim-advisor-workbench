"""Public retrieval names and the established multilingual keyword recipe."""
import pytest

from advisor.query_terms import MODES, RETRIEVAL_MODES, cjk_tokens, lex_terms, normalize_mode, normalize_modes


def test_public_modes_aliases_and_order_are_canonical():
    assert MODES == RETRIEVAL_MODES == ("qmd_keyword", "qmd_vector")
    assert normalize_modes(["native", "qmd_vector", "qmd", "qmd_keyword"]) == ["qmd_keyword", "qmd_vector"]
    for value in (None, "native", "qmd", "qmd_keyword"):
        assert normalize_mode(value) == "qmd_keyword"
    assert normalize_mode("qmd_vector") == "qmd_vector"


@pytest.mark.parametrize("value", ["qmd_hybrid", "vector", "", 3, {}, []])
def test_unsupported_backend_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_mode(value)


@pytest.mark.parametrize("value", [None, [], "native", {"qmd_keyword"}, ["unknown"]])
def test_build_requires_explicit_nonempty_mode_list(value):
    with pytest.raises(ValueError):
        normalize_modes(value)


def test_established_terminology_and_cjk_bigrams_are_preserved():
    assert lex_terms("阀门压降") == ["valve", "pressure", "drop", "阀门", "门压", "压降"]
    assert lex_terms("動網格壓降簡化") == ["moving", "mesh", "pressure", "drop", "simplification",
                                        "動網", "網格", "格壓", "壓降", "降簡", "簡化"]
    assert cjk_tokens("阀 阀门压降 阀门") == ["阀", "阀门", "门压", "压降"]
    assert lex_terms("mesh mesh -thermal") == ["mesh", "thermal"]
    assert lex_terms('" OR * ()') == []
    terms = lex_terms(" ".join("word" + str(index) for index in range(90)) + " " + "".join(chr(0x4e00 + index) for index in range(80)))
    assert len(terms) == 60 and len(set(terms)) == 60
