"""Shared keyword terms and compatibility names for QMD retrieval.

Keyword search expands explicit terminology once, quotes each resulting term,
and evaluates one OR/BM25 query against the QMD FTS index.
"""
from __future__ import annotations

import re

from .knowledge import expand_query

MODES = ("qmd_keyword", "qmd_vector")
RETRIEVAL_MODES = MODES


def normalize_mode(mode):
    """Accept the historical input name without exposing a third backend."""
    mode = "qmd_keyword" if mode is None or mode in ("native", "qmd") else mode
    if mode not in MODES:
        raise ValueError("mode must be qmd_keyword or qmd_vector")
    return mode


def normalize_modes(modes):
    if not isinstance(modes, (list, tuple)) or not modes:
        raise ValueError("modes must contain qmd_keyword and/or qmd_vector")
    return list(dict.fromkeys(normalize_mode(mode) for mode in modes))


def cjk_tokens(text):
    output = []
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        output.extend(run[i:i+2] for i in range(len(run)-1))
        if len(run) == 1:
            output.append(run)
    return list(dict.fromkeys(output))


def lex_terms(query):
    expanded = expand_query(query)["terms"]
    local = {"压降": "pressure drop", "壓降": "pressure drop", "压差": "pressure difference", "简化": "simplification", "簡化": "simplification"}
    for key, value in local.items():
        if key in query:
            expanded.extend(value.split())
    return list(dict.fromkeys(expanded + cjk_tokens(query)))[:60]

