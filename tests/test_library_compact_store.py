"""Compact registry integration and bounded indexing snapshot ownership."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.library_store import LibraryStore, encoded


def stream_fixture(tmp_path):
    store = LibraryStore(tmp_path / "libraries")
    with closing(store.connect()) as db, db:
        for number in range(4):
            identity = f"object-{number}"
            value = {"id": identity, "library_id": "fixture", "title": identity}
            db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (identity, identity, "fixture", encoded(value)))
            db.execute("INSERT INTO active VALUES(?,?,?)", ("fixture", identity, identity))
    return store


def test_catalog_stream_closes_partial_cursors_and_held_generators(tmp_path):
    store = stream_fixture(tmp_path)
    with store.object_stream() as stream:
        first = iter(stream)
        assert next(first)["id"] == "object-0" and len(stream._cursors) == 1
        first.close()
        assert not stream._cursors
        second = iter(stream)
        next(second)
        assert len(stream._cursors) == 1
        stream.close()
        assert stream._db is None and not stream._cursors
        assert list(second) == []
    stream.close()


def test_catalog_stream_cancellation_releases_cursor_without_replacing_exception(tmp_path):
    store = stream_fixture(tmp_path)
    marker = tmp_path / "cancel"
    with build_control(BuildControl(marker)):
        with store.object_stream() as stream:
            iterator = iter(stream)
            next(iterator)
            marker.touch()
            with pytest.raises(BuildCancelled):
                next(iterator)
            assert not stream._cursors
    assert stream._db is None


def compact_fixture(tmp_path):
    from test_compact_model import mph
    sources = tmp_path / "sources"
    model = mph(sources / "case.mph", '''<Model><ModelNode tag="comp1" name="Component"/>
<GeomSequence tag="geom1"><storedParent>/modelNode/comp1</storedParent></GeomSequence>
<MeshSequence tag="mesh1" name="Mesh"><geom>/geom/geom1</geom><MeshFeature tag="size" op="Size" name="Size">
<propertyValue name="p:hmax" value="0.05"/><propertyValue name="p:custom" value="on"/>
<selection T="32" selType="GEOMDIM"><explicit geom="geom1" dim="2" hDim="2" entities="10,1,3"/></selection>
</MeshFeature></MeshSequence></Model>''')
    store = LibraryStore(tmp_path / "data/libraries")
    library = store.register("Compact model fixture", "user-models", [{"id": "models", "role": "models", "path": str(sources)}])
    library = store.set_enabled(library["id"], True)
    return store, library, model


def build_compact(store, library):
    result = store.build(library["id"], model_format="compact", deep=False, modes=["native"])
    assert result["status"] in {"completed", "partial"}, result
    return result


def test_compact_registry_is_thin_and_public_reads_hydrate_typed_values(tmp_path):
    store, library, model = compact_fixture(tmp_path)
    build_compact(store, library)
    raw = store.objects([library["id"]])
    authority = next(obj for obj in raw if obj["type"] == "model_case")
    children = [obj for obj in raw if obj["data"].get("compact_ref")]
    assert children and len(raw) < 20
    assert all(not child.get("coverage") and child.get("coverage_ref") == "model-case" for child in children)
    assert all("compact_case" not in child["data"] and "properties" not in child["data"] for child in children)
    compact = authority["data"]["compact_case"]
    path = store.root / compact["path"]
    assert path.is_file() and path.name == "compactcase.sqlite3"
    assert not list(store.root.rglob("extraction.json")) and not list(store.root.rglob("saved-state.json"))
    assert not list(store.root.rglob("*.xml")) and not list(store.root.rglob("*.mph"))
    with closing(sqlite3.connect(path)) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'native_fts%'").fetchall()
    assert store.get(children[0]["id"]) == children[0]
    hydrated = store.read(children[0]["id"])
    assert hydrated["id"] == children[0]["id"] and hydrated != children[0]
    configuration = store.configuration(authority["id"], source="xml", limit=20)
    assert configuration["nodes"] and configuration["physical_validation"] == "not_run"
    assert model.is_file()


def test_closed_compact_library_blocks_hydration_before_opening_case_database(tmp_path, monkeypatch):
    from advisor import compact_library
    store, library, _ = compact_fixture(tmp_path)
    build_compact(store, library)
    source = next(obj for obj in store.objects() if obj["data"].get("compact_ref"))
    store.set_enabled(library["id"], False)
    monkeypatch.setattr(compact_library, "hydrate_source", lambda *a, **kw: pytest.fail("Closed source must not open its compact database"))
    with pytest.raises(PermissionError):
        store.read(source["id"])
    assert store.get(source["id"]) == source


def test_rebind_uses_current_original_without_mutating_compact_history(tmp_path):
    store, library, original = compact_fixture(tmp_path)
    build_compact(store, library)
    sources = store.objects()
    authority = next(obj for obj in sources if obj["type"] == "model_case")
    compact = store.root / authority["data"]["compact_case"]["path"]
    before = compact.read_bytes()
    destination = tmp_path / "rebound-models"
    shutil.copytree(original.parent, destination)
    store.rebind(library["id"], "models", destination)
    original.unlink()
    node = next(obj for obj in sources if obj["data"].get("compact_ref", {}).get("node_id"))
    evidence = store.read_evidence(node["id"])
    assert evidence and compact.read_bytes() == before
    assert store.read(node["id"])["id"] == node["id"]


def test_unchanged_compact_build_reuses_ids_database_and_purge_owns_only_artifact(tmp_path):
    store, library, original = compact_fixture(tmp_path)
    build_compact(store, library)
    before = store.objects()
    authority = next(obj for obj in before if obj["type"] == "model_case")
    compact = store.root / authority["data"]["compact_case"]["path"]
    database = compact.read_bytes()
    build_compact(store, library)
    assert store.objects() == before and compact.read_bytes() == database
    source_bytes = original.read_bytes()
    store.delete_library(library["id"])
    result = store.purge_library(library["id"], confirmed=True)
    assert result["status"] == "purged" and not compact.exists()
    assert original.read_bytes() == source_bytes
