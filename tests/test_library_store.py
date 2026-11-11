import json
from pathlib import Path
import shutil

import pytest

from advisor.library_store import LibraryStore
from advisor.library_text import extract_text_objects
from advisor.library_enrichment import enrich_case


def registered(tmp_path, name="library"):
    root = tmp_path / name
    root.mkdir()
    path = root / "same.md"
    path.write_text("# A source\nThermal expansion with a documented reference temperature.", encoding="utf-8")
    store = LibraryStore(tmp_path / "catalog")
    library = store.register(name, "official-manuals", [{"id": "main", "path": str(root)}], "6.3")
    library = store.set_enabled(library["id"], True)
    return store, library, path


def test_namespaces_and_immutable_old_citations(tmp_path):
    store, first, path = registered(tmp_path)
    assert store.build(first["id"], modes=[]) ["status"] == "completed"
    old = store.objects([first["id"]])[0]
    path.write_text("Changed reference temperature and boundary assumptions.", encoding="utf-8")
    store.build(first["id"], modes=[])
    current = store.objects([first["id"]])[0]
    assert current["object_id"] == old["object_id"]
    assert current["id"] != old["id"]
    assert store.get(old["id"])["text"] == old["text"]
    second = store.register("other", "official-manuals", [{"id": "main", "path": str(path.parent)}], "6.4")
    second = store.set_enabled(second["id"], True)
    store.build(second["id"], modes=[])
    assert store.objects([second["id"]])[0]["object_id"] != current["object_id"]


def test_rebind_checks_content_and_preserves_ids(tmp_path):
    store, library, path = registered(tmp_path)
    store.build(library["id"], modes=[])
    old_id = store.objects()[0]["id"]
    target = tmp_path / "moved"
    shutil.copytree(path.parent, target)
    assert store.rebind(library["id"], "main", target)["verified_evidence"] == 1
    store.build(library["id"], modes=[])
    assert store.objects()[0]["id"] == old_id
    (target / path.name).write_text("different content", encoding="utf-8")
    with pytest.raises(ValueError, match="different"):
        store.rebind(library["id"], "main", target)


def test_failed_or_offline_scan_does_not_erase_sources(tmp_path):
    store, library, path = registered(tmp_path)
    store.build(library["id"], modes=[])
    old = store.objects()
    assert store.build(library["id"], paths=[path.parent / "missing.mph"], modes=[])["status"] == "failed"
    assert store.objects() == old
    path.unlink()
    assert store.build(library["id"], modes=[])["status"] == "failed"
    assert store.objects() == old


def test_source_outside_registered_roots_is_rejected(tmp_path):
    store, library, path = registered(tmp_path)
    other = tmp_path / "outside.md"
    other.write_text("Not in the source directory", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        store.scan(library["id"], [other])


def test_html_keeps_structure_and_equation_location(tmp_path):
    path = tmp_path / "topic.html"
    path.write_text('<html><title>Heat</title><h1>Heat flux</h1><p>Energy balance.</p><img src="eq.png"><script>ignore_me()</script></html>', encoding="utf-8")
    obj = extract_text_objects(path)["objects"][0]
    assert "Energy balance" in obj["text"]
    assert "ignore_me" not in obj["text"]
    assert obj["data"]["images"] == ["eq.png"]


class FakeBackend:
    model = "test-only"
    engine = "injected-test-backend"
    def __init__(self, bad=False):
        self.bad = bad
    def generate(self, *args, **kwargs):
        return {"summary": "fixture explanation", "objective": "unknown", "applicability": [], "limitations": [],
                "steps": [{"local_id": "invented" if self.bad else "step1", "purpose": "check",
                           "required_inputs": [], "depends_on": [], "source_local_ids": ["step1"],
                           "checks": [], "reuse_cautions": []}]}


def test_annotation_does_not_change_extracted_facts_and_rejects_fake_ids(tmp_path):
    source = {"objects": [{"local_id": "step1", "type": "method_step", "title": "Heat flux", "text": "Documented text", "data": {"value": "10[W]"}, "evidence": []}]}
    good = enrich_case(source, tmp_path / "good", FakeBackend())
    assert good["enrichment"]["status"] == "completed"
    assert good["objects"][0]["data"]["value"] == "10[W]"
    assert "annotation" not in source["objects"][0]["data"]
    bad = enrich_case(source, tmp_path / "bad", FakeBackend(True))
    assert bad["enrichment"]["status"] == "failed"
    assert bad["objects"] == source["objects"]


def test_registered_user_evidence_is_accepted_and_forgery_rejected(tmp_path):
    from advisor.library_provider import LibraryKnowledgeProvider
    from advisor.research_planner import _registered_source, _evidence_for_model
    store, library, path = registered(tmp_path)
    store.build(library["id"], modes=[])
    class Legacy:
        def get_document(self, citation_id):
            return None
    provider = LibraryKnowledgeProvider(Legacy(), store.root)
    source = store.objects()[0]
    assert source["url"].startswith("library://")
    assert _registered_source(provider, source)
    assert not _registered_source(provider, dict(source, text="forged source text"))
    assert not _registered_source(provider, dict(source, library_id="another-library"))
    assert not _registered_source(provider, dict(source, verification={"numerical": "passed"}))
    transported = _evidence_for_model([source])[0]
    assert transported["revision_id"] == source["revision_id"]
    assert transported["evidence"] == source["evidence"]


def test_stale_process_lease_is_recovered(tmp_path):
    store, library, path = registered(tmp_path)
    # An impossible PID supplies a deterministic interrupted-worker fixture.
    (store.root / ".build.lock").write_text(json.dumps({"pid": 2147483647}), encoding="utf-8")
    assert store.build(library["id"], modes=[])["status"] == "completed"
    assert not (store.root / ".build.lock").exists()


def test_configuration_is_filtered_and_bound_to_archived_hash(tmp_path):
    from contextlib import closing
    from advisor.library_store import encoded, file_hash
    store, library, path = registered(tmp_path)
    artifact = store.root / "artifacts" / "case" / "api.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({"nodes": [{"path": "model/physics(ht)/feature(hf1)", "category": "physics", "properties": {"h": "12[W/(m^2*K)]"}},
                                              {"path": "model/sol(sol1)", "category": "sol"}]}), encoding="utf-8")
    record = store.scan(library["id"], [path])[0]
    obj = store._prepare(library, record, {"local_id": "model-case", "type": "model_case", "title": "Synthetic API fixture", "text": "fixture",
        "evidence": [{"asset_path": str(artifact), "method": "comsol_standalone_api_snapshot", "locator": "json:/nodes", "status": "observed"}]})
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (obj["id"], obj["object_id"], library["id"], encoded(obj)))
    answer = store.configuration(obj["id"], node_path="hf1")
    assert answer["returned"] == 1
    assert answer["nodes"][0]["properties"]["h"] == "12[W/(m^2*K)]"
    assert answer["physical_validation"] == "not_run"
    blob = store.root / obj["evidence"][0]["relative_path"]
    blob.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        store.configuration(obj["id"])


def test_original_evidence_is_bounded_and_does_not_read_replaced_content(tmp_path):
    store, library, path = registered(tmp_path)
    store.build(library["id"], modes=[])
    obj = store.objects()[0]
    answer = store.read_evidence(obj["id"], line=1, limit=1)
    assert answer["source_hash_verified"] and answer["line_end"] == 1
    assert "Thermal" not in answer["text"]
    path.write_text("Replaced source", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        store.read_evidence(obj["id"])
