"""Physical tutorial reclamation preserves pinned model/API semantics."""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from advisor import compact_case, compact_library
from advisor.compact_case import CompactCase, CompactCaseIntegrityError
from advisor.compact_reclaim import (compact_semantic_digest, ensure_storage_binding_schema,
                                     stage_compact_reclaim, storage_binding_matches)
from test_compact_case import fixture as saved_fixture


def fixture(tmp_path):
    source, parsed, parser, calls = saved_fixture(tmp_path)
    parsed["documents"][0]["figure_metadata"] = "TUTORIAL_METADATA_SECRET " * 1000
    for passage in parsed["passages"]:
        passage["text"] = "TUTORIAL_BODY_SECRET " * 30000
    root = tmp_path / "libraries"
    output = root / "artifacts/case"
    compact_case.build_compact_case(source, output, parser=parser, signature="fixture/v1")
    database = output / compact_case.DATABASE
    with closing(sqlite3.connect(database)) as db:
        compact_case._api_schema(db)
        with db:
            intern = lambda table, value: compact_case._intern_existing(db, table, value)
            metadata = intern("metadata_pool", {"status": "observed", "read_contract": "fixture-api/v1"})
            db.execute("INSERT INTO api_runs VALUES(1,'api-run',?)", (metadata,))
            db.execute("INSERT INTO api_nodes VALUES(1,3,?)", (intern("symbols", "model/component(comp1)/mesh(mesh1)"),))
            db.execute("""INSERT INTO api_observations(id,run_id,api_node_id,name_id,kind_id,getter_id,status_id,type_id,value_id,metadata_id)
              VALUES(1,1,1,?,?,?,?,?,?,?)""", (intern("symbols", "api_only"), intern("symbols", "property"),
                intern("symbols", "getDoubleArray"), intern("symbols", "observed"), intern("symbols", "array"),
                intern("value_pool", [1, 2, 3]), metadata))
            # Candidate tutorial metadata also referenced by API must survive.
            db.execute("UPDATE passages SET metadata_id=? WHERE id=2", (metadata,))
    authority = {"id": "unchanged-model-case", "type": "model_case", "title": "Saved model", "data": {
        "compact_case": {"path": database.relative_to(root).as_posix(), "sha256": compact_library._hash(database), "format": compact_case.FORMAT}}}
    child = {"id": "unchanged-feature", "type": "saved_configuration", "title": "Mesh", "text": "hmax",
             "data": {"compact_ref": {"node_id": 3}}, "evidence": [{"asset_path": str(source), "locator": "compact:node/3"}]}
    return source, root, database, authority, child, metadata


def bind(root, authority, result):
    with closing(sqlite3.connect(root / "catalog.sqlite3")) as db, db:
        ensure_storage_binding_schema(db)
        db.execute("INSERT INTO compact_storage_bindings VALUES(?,?,?)", (
            authority["data"]["compact_case"]["path"], result["original_sha256"], result["current_sha256"]))


def test_stage_removes_body_and_exclusive_metadata_preserving_models_api_fts_and_evidence(tmp_path):
    source, root, database, authority, child, shared_metadata = fixture(tmp_path)
    original = database.read_bytes()
    reader = CompactCase(database)
    nodes = list(reader.iter_nodes())
    raw = reader.read_raw_evidence(3)
    digest = compact_semantic_digest(database)
    staged = tmp_path / "staged" / compact_case.DATABASE
    with patch("advisor.compact_model.parse_compact_model", side_effect=AssertionError("No MPH parsing")):
        result = stage_compact_reclaim(database, staged)
    assert database.read_bytes() == original
    assert result["semantic_unchanged"] and result["semantic_sha256_before"] == result["semantic_sha256_after"]
    assert result["reclaimed_bytes"] > 500000 and result["removed_text_bytes"] > 500000
    assert result["removed_tutorial_metadata_rows"] >= 1
    assert compact_semantic_digest(staged) == digest
    new_reader = CompactCase(staged)
    assert list(new_reader.iter_nodes()) == nodes
    assert new_reader.read_raw_evidence(3) == raw
    assert new_reader.search("hmax") == reader.search("hmax")
    with closing(sqlite3.connect(staged)) as db:
        assert all(db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0 for table in ("documents", "passages", "text_pool"))
        assert db.execute("SELECT body_json FROM metadata_pool WHERE id=?", (shared_metadata,)).fetchone()
        assert not db.execute("SELECT 1 FROM metadata_pool WHERE body_json LIKE '%TUTORIAL_METADATA_SECRET%'").fetchone()
    assert b"TUTORIAL_BODY_SECRET" not in staged.read_bytes()
    assert b"TUTORIAL_METADATA_SECRET" not in staged.read_bytes()


def test_exact_catalog_binding_keeps_citations_configuration_and_raw_evidence_after_replace(tmp_path):
    source, root, database, authority, child, _ = fixture(tmp_path)
    frozen = copy.deepcopy(authority)
    saved = compact_library.hydrate_source(authority, child, library_root=root)
    configuration = compact_library.configuration(authority, library_root=root, source="xml")
    evidence = compact_library.read_evidence(authority, child, library_root=root)
    staged = tmp_path / "reclaimed.sqlite3"
    result = stage_compact_reclaim(database, staged)
    staged.replace(database)
    with pytest.raises(CompactCaseIntegrityError, match="hash"):
        compact_library.hydrate_source(authority, child, library_root=root)
    bind(root, authority, result)
    assert compact_library.hydrate_source(authority, child, library_root=root) == saved
    assert compact_library.configuration(authority, library_root=root, source="xml") == configuration
    assert compact_library.read_evidence(authority, child, library_root=root) == evidence
    assert authority == frozen
    source.unlink()
    assert compact_library.hydrate_source(authority, child, library_root=root)["data"]["saved_properties"]
    assert compact_library.read_evidence(authority, child, library_root=root)["status"] == "unavailable"
    assert storage_binding_matches(root / "catalog.sqlite3", authority["data"]["compact_case"]["path"],
                                   result["original_sha256"], result["current_sha256"])
    database.write_bytes(database.read_bytes() + b"tampered after binding")
    with pytest.raises(CompactCaseIntegrityError, match="hash"):
        compact_library.hydrate_source(authority, child, library_root=root)


def test_reclaim_needs_no_original_mph_and_never_overwrites_existing_paths(tmp_path):
    source, root, database, authority, child, _ = fixture(tmp_path)
    source.unlink()
    target = tmp_path / "staged" / compact_case.DATABASE
    result = stage_compact_reclaim(database, target)
    assert result["semantic_unchanged"]
    assert CompactCase(target).get_property(3, "hmax")["value"] == .05
    for output in (database, target):
        with pytest.raises(ValueError, match="new staged"):
            stage_compact_reclaim(database, output)


def test_failed_proof_cleans_stage_without_touching_input(tmp_path):
    _, _, database, _, _, _ = fixture(tmp_path)
    original = database.read_bytes()
    target = tmp_path / "failed.sqlite3"
    import advisor.compact_reclaim as reclaim
    digest = reclaim._semantic_digest
    calls = 0
    def changed(db):
        nonlocal calls
        result = digest(db)
        calls += 1
        if calls == 3:
            result["sha256"] = "changed"
        return result
    with patch.object(reclaim, "_semantic_digest", side_effect=changed), pytest.raises(ValueError, match="model/API"):
        stage_compact_reclaim(database, target)
    assert database.read_bytes() == original and not target.exists()
