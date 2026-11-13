# Local retrieval integration

All product search uses registered-library QMD. The public modes are:

| Mode | Behavior |
| --- | --- |
| `qmd_keyword` | Default for build, index and search. Expand project Chinese/English terminology once, combine the terms with OR, then execute one QMD FTS/BM25 query. |
| `qmd_vector` | Optional, disabled by default. Requires a configured local embedding model and an explicitly built vector index. |

The OR term policy belongs to this project; it is not a claim that upstream
QMD `searchLex` uses the same default query combination.

Historical `native` and `qmd` input names normalize to `qmd_keyword`. They do not
select another backend. Missing QMD, unsupported SDK versions, stale indexes and
unbuilt modes are reported explicitly. There is no native fallback, automatic
model download, LLM query expansion, generation or reranking in keyword search.

## Register and build

```powershell
python library_cli.py add "Manuals" --type official-manuals --path "D:/COMSOL/doc"
python library_cli.py build LIBRARY_ID --modes qmd_keyword
python library_cli.py enable LIBRARY_ID
python library_cli.py search "mesh convergence" --mode qmd_keyword --library LIBRARY_ID
```

`official-manuals`, `official-models`, `user-manuals` and `user-models` are declared
source categories; publisher identity is not inferred from paths. New libraries
remain disabled until explicitly enabled. Builds cover the registered directory
unless an explicit sample limit is supplied. `library_cli.py index` defaults to
`qmd_keyword`; vectors and semantic enrichment require explicit selection.

`scripts/ingest_docs.py` registers the installed COMSOL HTML documentation and
builds its QMD keyword index through this same library path. HTML titles,
paragraphs, Eclipse TOC paths, parent context and source URLs remain provenance;
they are not an independent retrieval engine. Existing HTML archives and source
IDs remain readable for historical citations, while fresh search uses registered
libraries only. No original COMSOL files are modified.

## Runtime and storage

QMD SDK 2.8.3 and Node are resolved from the configured local offline dependency
profile. Existing offline dependencies are reused; keyword retrieval needs no
embedding weights. Run `python library_cli.py runtime` to inspect the selected
paths without building or downloading anything.

Registered-library keywords share one QMD database with one collection per
library. Enabled libraries, source kind, feature branch and evaluation exclusions
are filtered before ranking and TopK. Keywords read the stored search
representation directly, without exporting a second Markdown corpus. Vectors
retain their separate per-library stores. Source results resolve to the unchanged
authoritative archive and disclose the actual QMD mode. Read full source text
before citing; a search candidate or related-source summary is not read evidence.

The optional `feature_branch` filters only explicitly labelled sources. General
manuals and case overviews may be unlabelled; omit the branch for a broad first
query. Closed or deleted libraries remain excluded from new searches and reads,
including old source IDs. Restoring visibility reuses available indexes.

## Optional PDF reasoning

PageIndex is a separate opt-in PDF adapter. It requires an explicitly selected
PDF, a configured reasoning model and provider credentials. It never silently
replaces QMD or uploads the COMSOL corpus. Installed SDK status alone does not
establish a verified reasoning result. Ordinary HTML hierarchy and PDF reading
are not described as PageIndex reasoning.

Equation images and diagrams are not OCRed. Open the original source to inspect
complete mathematical notation, figures and tutorial context.

## Case discovery and implementation navigation

Use `search_cases` for overview discovery, then `get_case_guide(case_id)` for a small flow map; expand one `stage` at a time. `search_knowledge` accepts `case_id`/`stage` to scope settings and linked tutorials; `read_case_configuration` reads actual saved/API values. Navigation is not read evidence. Closed libraries and held-out cases remain blocked. See [case navigation](../docs/case-navigation.md) for preparation and CLI usage. No embedding or QMD rebuild is needed.
