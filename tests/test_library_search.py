import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.library_search import BRIDGE_VERSION, LibrarySearch, _representation
from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.index_paths import PATH_SCHEMA, resolve_index_manifest
from advisor.index_manifest_cache import read_manifest
from advisor.object_index import pack
from qmd_fixtures import keyword_schema, keyword_insert
from advisor.retrieval_settings import read as read_embedding_settings, settings_root, update as update_embedding_settings


def enable_test_embedding(data_dir, model=None):
    """Explicit capability opt-in confined to the test's fixture data root."""
    root = settings_root(data_dir)
    if model is None and read_embedding_settings(data_dir)["enabled"]:
        return
    root.mkdir(parents=True, exist_ok=True)
    model = model or root / "test-embedding.gguf"
    if not Path(model).exists():
        Path(model).write_bytes(b"GGUFexplicit-unit-test-fixture")
    update_embedding_settings(data_dir, enabled=True, model_path=str(model))


def obj(key, library="cases", text="Laminar flow through a narrow valve passage gives pressure drop."):
    return {"id": "lo-" + key, "object_id": key, "revision_id": "revision-" + key, "library_id": library,
            "type": "case_overview", "title": key + " valve flow", "text": text,
            "data": {"nested": {"keep": [1, 2]}}, "evidence": [{"uri": "source://" + key}], "kind": "case", "version": "6.3", "source_uri": "library://" + key}


class FakeEngineSearch(LibrarySearch):
    """Unit orchestration fixture; real CPU integration is separate."""
    def __init__(self, data_dir, **kwargs):
        enable_test_embedding(data_dir)
        super().__init__(data_dir, **kwargs)
        self.calls = []
        self.model_sha = "a" * 64
        self.fail_embed = False
        self.observer = None

    def _model_info(self):
        return {"sha256": self.model_sha, "uri": self.model_uri, "size": 123, "available": True}

    def prepare_embedding(self, **kwargs):
        return self._model_info()

    def _build_keyword(self, inputs_path, output, libraries, *, on_progress=None):
        if self.observer:
            self.observer({'op': 'keyword_build', 'db_path': str(output)})
        from advisor.build_control import check_cancelled
        check_cancelled()
        with closing(sqlite3.connect(inputs_path)) as source, closing(sqlite3.connect(output)) as db, db:
            keyword_schema(db)
            for path, title, text in source.execute("SELECT qmd_path,title,text FROM keyword_inputs ORDER BY qmd_path"):
                collection, filename = path.split('/', 1)
                keyword_insert(db, collection, filename, title, text)
        if on_progress:
            on_progress({'completed': len(libraries), 'total': len(libraries), 'detail': 'Shared fixture ready'})

    def _engine(self, request, *, timeout=120):
        if request["op"] == "batch":
            return [{"library_key": item["library_key"], "results": self._engine(item, timeout=timeout)}
                    for item in request["requests"]]
        self.calls.append(copy.deepcopy(request))
        if self.observer:
            self.observer(request)
        if request["op"] == "shared_keyword":
            with closing(sqlite3.connect(request["db_path"], uri=True)) as db:
                scopes = {item['collection']: item for item in request['collections']}
                authoritative = request.get('authoritative_scope')
                if authoritative:
                    from advisor.object_index import scope_predicate
                    db.execute('ATTACH DATABASE ? AS authoritative', (Path(authoritative['db_path']).as_uri() + '?mode=ro',))
                    clause, params = scope_predicate(compact=True, library_ids=[item['library_key'] for item in scopes.values()],
                                                    kind=authoritative['kind'], feature_branch=authoritative['feature_branch'])
                    rows = db.execute("SELECT d.collection,d.path FROM documents d JOIN authoritative.objects o ON o.qmd_path=d.collection || '/' || d.path WHERE " + clause + " ORDER BY d.path", params).fetchall()
                else:
                    rows = db.execute("SELECT collection,path FROM documents ORDER BY path").fetchall()
                if request.get('holdout_db_path'):
                    db.execute('ATTACH DATABASE ? AS heldout_fixture',
                               (Path(request['holdout_db_path']).as_uri() + '?mode=ro',))
                    blocked = {row[0] for row in db.execute('SELECT path FROM heldout_fixture.paths')}
                    rows = [(collection, path) for collection, path in rows
                            if collection + '/' + path not in blocked]
            return [{'filepath': 'qmd://' + collection + '/' + path, 'score': .8, 'source': 'fts'}
                    for collection, path in rows if collection in scopes
                    and ('allowed_paths' not in scopes[collection] or path in scopes[collection]['allowed_paths'])][:request['limit']]
        if request["op"] == "update":
            with closing(sqlite3.connect(request["db_path"])) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS fixture(value TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS fixture_paths(value TEXT)")
                db.execute("DELETE FROM fixture_paths")
                db.executemany("INSERT INTO fixture_paths VALUES(?)", [(path.name,) for path in sorted(Path(request["corpus"]).glob("*.md"))])
            return {"indexed": 1}
        if request["op"] == "embed":
            if self.fail_embed:
                raise RuntimeError("actual embedding fixture failure")
            return {"state": {"needsEmbedding": 0, "hasVectorIndex": True}, "embedding_rows": [{"model": "fixture", "embed_fingerprint": "fixture", "chunks": 1}]}
        if request["op"] in {"keyword", "vector"}:
            with closing(sqlite3.connect(request["db_path"])) as db:
                paths = [row[0] for row in db.execute("SELECT value FROM fixture_paths ORDER BY value")]
            if "allowed_paths" in request:
                paths = [path for path in paths if path in request["allowed_paths"]]
            return [{"filepath": "qmd://objects/" + path, "score": 0.8, "source": "vec" if request["op"] == "vector" else "fts"}
                    for path in paths[:request["limit"]]]
        raise AssertionError(request)


def test_progress_reports_actual_stage_counts_and_unknown_embedding_total(tmp_path):
    progress = []
    search = FakeEngineSearch(tmp_path)
    search.build([obj('a', 'first'), obj('b', 'second')], modes=['qmd_keyword', 'qmd_vector'], generation='progress', on_progress=progress.append)
    assert {entry['stage'] for entry in progress} == {'indexing_objects', 'indexing_keyword', 'indexing_vector', 'cleaning_corpus'}
    cleanup = [entry['progress'] for entry in progress if entry['stage'] == 'cleaning_corpus']
    assert cleanup[-1]['completed'] == cleanup[-1]['total'] == 2
    assert cleanup[-1]['unit'] == 'files'
    for entry in progress:
        value = entry['progress']
        assert value['current_file'] is None
        assert value['unit'] in {'objects', 'indexes', 'chunks', 'files'}
        assert 'percent' not in value and 'overall' not in value
        if value['completed'] is not None:
            assert 0 <= value['completed'] <= value['total']
    native = [entry['progress'] for entry in progress if entry['stage'] == 'indexing_objects']
    assert native[0]['completed'] == 0 and native[-1]['completed'] == native[-1]['total'] == 2
    vector = [entry['progress'] for entry in progress if entry['stage'] == 'indexing_vector']
    assert vector[0]['completed'] is None and vector[0]['total'] is None
    assert vector[-1]['completed'] == vector[-1]['total'] == 2
    assert any('first' in entry['detail'] for entry in vector)
    assert any('second' in entry['detail'] for entry in vector)


def test_failed_embedding_progress_does_not_claim_completed_vector_indexes(tmp_path):
    search = FakeEngineSearch(tmp_path)
    search.fail_embed = True
    progress = []
    result = search.build([obj('a')], modes=['qmd_keyword', 'qmd_vector'], generation='failed-progress', on_progress=progress.append)
    assert result['modes']['qmd_keyword']['ready'] and not result['modes']['qmd_vector']['ready']
    last = [entry['progress'] for entry in progress if entry['stage'] == 'indexing_vector'][-1]
    assert last['completed'] == 0 and last['total'] == 1
    assert 'failed' in last['detail'].lower()


def test_keyword_only_request_does_not_resume_or_erase_interrupted_vectors(tmp_path):
    search = FakeEngineSearch(tmp_path)
    built = search.build([obj('kept')], modes=['qmd_keyword', 'qmd_vector'], generation='kept-generation')
    built['modes']['qmd_vector'].update(ready=False, state='building')
    search.active_path.write_text(json.dumps(built), encoding='utf-8')
    vector_db = Path(built['libraries']['cases']['vector_db_path'])
    progress_file = vector_db.with_suffix('.progress.json')
    progress_file.write_text('{"chunksEmbedded":3,"totalChunks":7}', encoding='utf-8')
    original_manifest = search.active_path.read_bytes()
    original_database = vector_db.read_bytes()
    original_progress = progress_file.read_bytes()
    search.calls.clear()
    result = search.build([obj('kept')], modes=['qmd_keyword'], generation='kept-generation')
    assert result['unchanged']
    assert result['modes']['qmd_vector']['state'] == 'interrupted'
    assert search.calls == [], 'No update, model load, or embedding may resume implicitly'
    assert search.active_path.read_bytes() == original_manifest
    assert vector_db.read_bytes() == original_database
    assert progress_file.read_bytes() == original_progress


def test_native_preserves_original_objects_provenance_and_filtered_recall(tmp_path):
    objects = [obj(f"large-{i}", "large", "valve pressure pressure pressure") for i in range(180)] + [obj("small", "small")]
    original = copy.deepcopy(objects)
    search = LibrarySearch(tmp_path)
    result = search.build(objects, modes=["qmd_keyword"], generation="g1")
    assert objects == original and result["modes"]["qmd_keyword"]["ready"]
    hits = search.search("valve pressure", library_ids=["small"], limit=1)
    assert len(hits) == 1 and hits[0]["id"] == "lo-small"
    assert {key: hits[0][key] for key in original[-1]} == original[-1]
    assert hits[0]["retrieval"]["generation"] == "g1" and hits[0]["retrieval"]["backend"] == "qmd_keyword"
    assert search.search("valve", library_ids=[]) == []
    assert search.search("valve", library_ids=["missing"]) == []


def test_all_modes_and_arbitrary_reference_id_mapping(tmp_path):
    item = obj("opaque/引用+revision")
    search = FakeEngineSearch(tmp_path)
    built = search.build([item], modes=["qmd_keyword", "qmd_vector"], generation="g1")
    assert all(built["modes"][mode]["ready"] for mode in ("qmd_keyword", "qmd_vector"))
    for mode in ("qmd_keyword", "qmd_vector"):
        hits = search.search("阀门压降如何随流量变化", mode=mode)
        assert hits[0]["id"] == item["id"] and hits[0]["evidence"] == item["evidence"]
        assert hits[0]["retrieval"]["backend"] == mode
    vector = [call for call in search.calls if call["op"] == "vector"][-1]
    assert vector["query"] == "阀门压降如何随流量变化"
    assert "qmd-vector.sqlite3" in vector["db_path"]


def test_qmd_limits_libraries_before_search_not_after_global_topk(tmp_path):
    search = FakeEngineSearch(tmp_path)
    search.build([obj("a", "large"), obj("b", "small")], modes=["qmd_keyword", "qmd_vector"], generation="g1")
    search.calls.clear()
    for mode in ("qmd_keyword", "qmd_vector"):
        assert search.search("valve", mode=mode, library_ids="small")[0]["library_id"] == "small"
    keyword = [call for call in search.calls if call['op'] == 'shared_keyword']
    assert len(keyword) == 1
    assert [item['library_key'] for item in keyword[0]['collections']] == ['small']
    small_path = search.status()["libraries"]["small"]["corpus"]
    assert all(call["corpus"] == small_path for call in search.calls if call['op'] != 'shared_keyword')


def test_native_and_keyword_publish_before_vector_and_fail_independently(tmp_path):
    search = FakeEngineSearch(tmp_path)
    observed = []
    def observe(request):
        if request["op"] == "embed":
            current = search.status()
            observed.append(current)
            assert current["modes"]["qmd_keyword"]["ready"]
            assert current["modes"]["qmd_keyword"]["ready"]
            assert not current["modes"]["qmd_vector"]["ready"]
            assert search.search("valve", mode="qmd_keyword")
    search.observer, search.fail_embed = observe, True
    result = search.build([obj("one")], modes=["qmd_keyword", "qmd_vector"], generation="g1")
    assert observed and result["ready"]
    assert result["modes"]["qmd_keyword"]["ready"] and result["modes"]["qmd_keyword"]["ready"]
    assert result["modes"]["qmd_vector"]["state"] == "failed"
    assert "embedding fixture failure" in result["modes"]["qmd_vector"]["error"]
    with pytest.raises(RuntimeError):
        search.search("valve", mode="qmd_vector")


def test_stale_content_and_model_are_explicit_and_generation_files_retained(tmp_path):
    search = FakeEngineSearch(tmp_path, preserve_history=True)
    first = search.build([obj("one")], modes=["qmd_keyword", "qmd_vector"], generation="g1")
    search.model_sha = "b" * 64
    assert search.status()["modes"]["qmd_vector"]["state"] == "stale"
    assert search.status()["modes"]["qmd_keyword"]["ready"]
    changed = obj("one", text="Changed source content with unchanged generation.")
    result = search.build([changed], modes=["qmd_keyword"], generation="g1")
    assert result["state"] == "stale" and not search.status()["modes"]["qmd_keyword"]["ready"]
    assert Path(first["directory"]).is_dir()
    second = search.build([obj("two")], modes=["qmd_keyword"], generation="g2")
    assert second["generation"] == "g2" and Path(first["directory"]).is_dir()


def test_identical_ready_generation_is_idempotent(tmp_path):
    search = FakeEngineSearch(tmp_path)
    first = search.build([obj("one")], modes=["qmd_keyword", "qmd_vector"], generation="g1")
    count = len(search.calls)
    second = search.build([obj("one")], modes=["qmd_keyword", "qmd_vector"], generation="g1")
    assert second["unchanged"] and second["directory"] == first["directory"]
    assert len(search.calls) == count


def test_bounded_representation_prioritizes_inferred_annotation_not_large_code(tmp_path):
    item = obj("code", text="model.component(\"comp1\").physics(\"spf\");\n" * 1000)
    item["type"] = "implementation_snippet"
    item["data"].update(code=item["text"], scope_hint="physics", annotation={"status": "inferred", "objective": "研究阀门流量与压降，目标是比较节流开度。", "summary": "带来源的推断注释", "checks": ["核对流量守恒"]})
    text, stats = _representation(item)
    assert len(text) <= 1400 and "研究阀门流量与压降" in text
    assert "INFERRED ANNOTATION (not observed model facts)" in text
    assert stats["truncated"] and stats["annotation_included"] and not stats["annotation_is_observed"]
    search = LibrarySearch(tmp_path)
    built = search.build([item], modes=["qmd_keyword"], generation="g1")
    assert built["representation"]["indexed_objects"] == 1 and built["representation"]["unindexed_objects"] == 0
    hit = search.search("阀门流量")[0]
    assert hit["data"]["code"] == item["data"]["code"] and hit["text"] == item["text"]


def test_malformed_inputs_do_not_replace_active_generation(tmp_path):
    search = LibrarySearch(tmp_path)
    search.build([obj("good")], modes=["qmd_keyword"], generation="g1")
    before = search.active_path.read_bytes()
    for objects, modes in (([obj("x"), obj("x")], ["qmd_keyword"]), ([], ["qmd_keyword"]), ([obj("x")], ["made_up"])):
        with pytest.raises(ValueError):
            search.build(objects, modes=modes, generation="invalid")
    assert search.active_path.read_bytes() == before
    with pytest.raises(ValueError):
        search.search("valve", mode="made_up")


def test_lexical_hits_never_count_as_vector_probe_success(tmp_path):
    class WrongSource(FakeEngineSearch):
        def _engine(self, request, *, timeout=120):
            result = super()._engine(request, timeout=timeout)
            if request["op"] == "vector":
                for item in result:
                    item["source"] = "fts"
            return result
    search = WrongSource(tmp_path)
    built = search.build([obj("one")], modes=["qmd_keyword", "qmd_vector"], generation="g1")
    assert built["modes"]["qmd_keyword"]["ready"] and built["modes"]["qmd_keyword"]["ready"]
    assert not built["modes"]["qmd_vector"]["ready"]
    assert "vector probe" in built["modes"]["qmd_vector"]["error"]


def test_same_directory_instances_share_thread_lock_and_live_lease(tmp_path):
    first, second = FakeEngineSearch(tmp_path), LibrarySearch(tmp_path)
    assert first.lock is second.lock
    observed = []
    def observe(request):
        if request["op"] == "embed":
            status = second.status()
            observed.append(status)
            assert status["building"] and status["lease"]["pid"] == os.getpid()
            with pytest.raises(RuntimeError, match="build is active"):
                second.build([obj("other")], modes=["qmd_keyword"], generation="race")
    first.observer = observe
    first.build([obj("one")], modes=["qmd_keyword", "qmd_vector"], generation="g1")
    assert observed and not second.status()["building"]
    assert not (tmp_path / "build-lease.json").exists()


def test_file_lock_excludes_another_process_and_recovers_after_exit(tmp_path):
    package = Path(__file__).resolve().parents[1]
    ready = tmp_path / "ready"
    script = "import sys,os;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from advisor.library_search import LibrarySearch;s=LibrarySearch(Path(sys.argv[2]));h,t=s._acquire_lease('child');Path(sys.argv[3]).write_text(str(os.getpid()));sys.stdin.read(1);s._release_lease(h,t)"
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    child = subprocess.Popen([sys.executable, "-c", script, str(package), str(tmp_path), str(ready)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
    try:
        until = time.monotonic() + 8
        while not ready.exists() and child.poll() is None and time.monotonic() < until:
            time.sleep(0.02)
        assert ready.exists()
        search = LibrarySearch(tmp_path)
        # Windows venv launchers can spawn the interpreter under a distinct PID.
        assert search.status()["building"] and search.status()["lease"]["pid"] == int(ready.read_text())
        with pytest.raises(RuntimeError, match="Another process"):
            search.build([obj("one")], modes=["qmd_keyword"], generation="g1")
    finally:
        child.communicate("\n", timeout=8)
    assert child.returncode == 0
    assert not search.status()["building"]
    assert search.build([obj("one")], modes=["qmd_keyword"], generation="g1")["ready"]


def test_interrupted_mode_does_not_hide_completed_native_index(tmp_path):
    enable_test_embedding(tmp_path)
    search = LibrarySearch(tmp_path)
    manifest = search.build([obj("one")], modes=["qmd_keyword"], generation="g1")
    manifest["modes"]["qmd_vector"] = {"ready": False, "state": "building"}
    search.active_path.write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "build-lease.json").write_text(json.dumps({"pid": -1, "token": "stale", "generation": "g1"}), encoding="utf-8")
    status = LibrarySearch(tmp_path).status()
    assert not status["building"] and status["modes"]["qmd_vector"]["state"] == "interrupted"
    assert status["modes"]["qmd_keyword"]["ready"] and search.search("valve")


def test_next_generation_rebuilds_shared_keyword_and_reuses_compatible_vectors(tmp_path):
    search = FakeEngineSearch(tmp_path)
    old = search.build([obj("one")], modes=["qmd_keyword", "qmd_vector"], generation="g1")
    with sqlite3.connect(old["libraries"]["cases"]["vector_db_path"]) as db:
        db.execute("INSERT INTO fixture VALUES('reusable vector marker')")
    search.calls.clear()
    new = search.build([obj("one"), obj("two")], modes=["qmd_keyword", "qmd_vector"], generation="g2")
    item = new["libraries"]["cases"]
    assert item["vector_reused_from_generation"] == "g1"
    assert "keyword_reused_from_generation" not in item
    assert new['keyword_storage'] == 'shared-collections-v1'
    assert Path(item['db_path']).parent == Path(new['directory'])
    with sqlite3.connect(item['db_path']) as db:
        assert db.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 2
    with sqlite3.connect(item["vector_db_path"]) as db:
        assert db.execute("SELECT value FROM fixture").fetchone()[0] == "reusable vector marker"
    assert any(call["op"] == "update" and call["db_path"] == item["vector_db_path"] for call in search.calls)


def test_cancel_before_build_has_no_side_effects(tmp_path):
    search = LibrarySearch(tmp_path / "search")
    marker = tmp_path / "cancel"
    marker.touch()
    with build_control(BuildControl(marker)), pytest.raises(BuildCancelled):
        search.build([obj("new")], modes=["qmd_keyword"], generation="new")
    assert not search.data_dir.exists()
    assert not search.lock.locked()


def test_native_cancellation_rolls_back_staging_and_preserves_old_index(tmp_path):
    search = LibrarySearch(tmp_path / "search")
    old = search.build([obj("old")], modes=["qmd_keyword"], generation="old")
    before = search.active_path.read_bytes()
    marker = tmp_path / "cancel"
    progress = []

    def cancel_mid_transaction(entry):
        progress.append(entry)
        if entry["progress"]["completed"] == 25:
            marker.touch()

    with build_control(BuildControl(marker)), pytest.raises(BuildCancelled):
        search.build([obj(f"new-{i}") for i in range(30)], modes=["qmd_keyword"], generation="new", on_progress=cancel_mid_transaction)
    assert search.active_path.read_bytes() == before
    assert search.search("valve")[0]["id"] == "lo-old"
    cancelled = search.status()["last_build"]
    assert cancelled["state"] == "cancelled" and not cancelled["ready"]
    assert cancelled["modes"]["qmd_keyword"]["state"] == "cancelled"
    assert not any(entry["progress"]["detail"] == "Native index completed" for entry in progress)
    with sqlite3.connect(Path(cancelled["directory"]) / "objects.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 0
    assert Path(old["directory"]).is_dir()
    assert not search.lock.locked() and not (search.data_dir / "build-lease.json").exists()
    # A different request can acquire both locks after cancellation.
    assert search.build([obj("next")], modes=["qmd_keyword"], generation="next")["ready"]


@pytest.mark.parametrize("cancel_op", ["keyword_build", "embed"])
def test_qmd_cancellation_retains_published_modes_and_old_vector_files(tmp_path, cancel_op):
    search = FakeEngineSearch(tmp_path / "search")
    old = search.build([obj("old")], modes=["qmd_keyword", "qmd_vector"], generation="old")
    old_vector = Path(old["libraries"]["cases"]["vector_db_path"])
    old_bytes = old_vector.read_bytes()
    marker = tmp_path / "cancel"
    partial = []
    progress = []

    def interrupt_engine(request):
        if request["op"] == cancel_op:
            if cancel_op == "embed":
                path = Path(request["progress_path"])
                path.write_text('{"chunksEmbedded":1,"totalChunks":2}', encoding="utf-8")
                partial.append(path)
            marker.touch()

    search.observer = interrupt_engine
    with build_control(BuildControl(marker)), pytest.raises(BuildCancelled):
        search.build([obj("new")], modes=["qmd_keyword", "qmd_vector"], generation="new", on_progress=progress.append)
    current = search.status()
    assert not current["building"] and current["lease"] is None
    assert current["modes"]["qmd_keyword"]["ready"]
    if cancel_op == "embed":
        assert current["state"] == "cancelled"
        assert search.search("valve", mode="qmd_keyword")[0]["id"] == "lo-new"
        assert partial[0].is_file()
        assert Path(current["libraries"]["cases"]["vector_db_path"]).is_file()
        assert current["modes"]["qmd_vector"]["state"] == "cancelled"
    else:
        assert current["generation"] == "old"
        assert search.search("valve", mode="qmd_keyword")[0]["id"] == "lo-old"
        assert read_manifest(search.last_build_path)["modes"]["qmd_keyword"]["state"] == "cancelled"
    assert old_vector.read_bytes() == old_bytes
    assert not any(entry["progress"]["detail"] in {"Vector phase completed", "Vector phase failed: Build cancelled by user."} for entry in progress)
    assert not search.lock.locked()


def test_progress_callback_cancellation_is_not_swallowed_as_mode_failure(tmp_path):
    search = FakeEngineSearch(tmp_path)

    def reject_progress(entry):
        if entry["stage"] == "indexing_keyword" and entry["progress"]["detail"] == "Shared fixture ready":
            raise BuildCancelled()

    with pytest.raises(BuildCancelled):
        search.build([obj("one")], modes=["qmd_keyword"], generation="g", on_progress=reject_progress)
    status = search.status()
    assert not status["modes"]["qmd_keyword"]["ready"]
    assert status["modes"]["qmd_keyword"]["state"] == "not_built"
    assert read_manifest(search.last_build_path)["modes"]["qmd_keyword"]["state"] == "cancelled"
    assert not status["building"]


def test_cancellation_between_serialization_and_atomic_publish_keeps_old_index(tmp_path, monkeypatch):
    import advisor.library_search as module
    search = LibrarySearch(tmp_path / "search")
    search.build([obj("old")], modes=["qmd_keyword"], generation="old")
    before = search.active_path.read_bytes()
    marker = tmp_path / "cancel"
    original_json = module._json

    def cancel_after_serialization(value):
        text = original_json(value)
        if isinstance(value, dict) and value.get("generation") == "new" and value.get("modes", {}).get("qmd_keyword", {}).get("ready"):
            marker.touch()
        return text

    monkeypatch.setattr(module, "_json", cancel_after_serialization)
    with build_control(BuildControl(marker)), pytest.raises(BuildCancelled):
        search.build([obj("new")], modes=["qmd_keyword"], generation="new")
    assert search.active_path.read_bytes() == before
    assert search.search("valve")[0]["id"] == "lo-old"
    assert read_manifest(search.last_build_path)["modes"]["qmd_keyword"]["state"] == "cancelled"
    assert not list(search.data_dir.rglob("*.tmp"))


def test_remote_prepare_does_not_download_or_publish_receipt(tmp_path, monkeypatch):
    enable_test_embedding(tmp_path / "search")
    search = LibrarySearch(tmp_path / "search", embed_model="hf:offline-test/model.gguf", cache_dir=tmp_path / "cache")
    marker = tmp_path / "cancel"
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUFfixture")

    def cancelled_pull(request, *, timeout):
        assert request["op"] == "pull"
        marker.touch()
        return [{"path": str(model)}]

    monkeypatch.setattr(search, "_engine", cancelled_pull)
    with build_control(BuildControl(marker)), pytest.raises(RuntimeError, match="no download attempted"):
        search.prepare_embedding()
    assert not list(search.cache_dir.glob("embedding-*.json"))


def test_cancelled_simulated_embedding_subprocess_releases_build_resources(tmp_path):
    from advisor.process_runner import run_text
    search = FakeEngineSearch(tmp_path / "search")
    marker, started = tmp_path / "cancel", tmp_path / "started"

    def long_embedding(request):
        if request["op"] == "embed":
            code = "import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)"
            run_text([sys.executable, "-c", code, str(started)], input_text="", cwd=tmp_path, timeout=15)

    def cancel_started_child():
        until = time.monotonic() + 8
        while not started.exists() and time.monotonic() < until:
            time.sleep(0.02)
        if started.exists():
            marker.touch()

    search.observer = long_embedding
    canceller = threading.Thread(target=cancel_started_child, daemon=True)
    canceller.start()
    try:
        with build_control(BuildControl(marker)), pytest.raises(BuildCancelled):
            search.build([obj("one")], modes=["qmd_keyword", "qmd_vector"], generation="g")
    finally:
        canceller.join(timeout=9)
    assert started.exists(), "This test must reach the real simulated child, not cancel during preparation"
    assert not search._pid_alive(int(started.read_text()))
    status = search.status()
    assert status["modes"]["qmd_keyword"]["ready"] and status["modes"]["qmd_keyword"]["ready"]
    assert status["modes"]["qmd_vector"]["state"] == "cancelled"
    assert not status["building"] and status["lease"] is None and not search.lock.locked()


@pytest.mark.parametrize("legacy", [False, True])
def test_moved_indexes_read_only_rebase_even_when_old_root_still_exists(tmp_path, legacy):
    old_root, new_root = tmp_path / "A" / "libraries/search", tmp_path / "B moved" / "libraries/search"
    old = FakeEngineSearch(old_root)
    item = obj("portable")
    built = old.build([item], modes=["qmd_keyword", "qmd_vector"], generation="portable-generation")
    if legacy:
        # This is the exact pre-schema on-disk form, with A still available.
        for path in (old.active_path, old.last_build_path, Path(built["directory"]) / "manifest.json"):
            path.write_text(json.dumps(built), encoding="utf-8")
    else:
        for path in (old.active_path, old.last_build_path, Path(built["directory"]) / "manifest.json"):
            stored = read_manifest(path)
            assert stored["path_schema"] == PATH_SCHEMA
            assert stored["directory"].startswith("generations/")
            assert str(old_root) not in stored["directory"]
            assert not Path(stored["libraries"]["cases"]["db_path"]).is_absolute()
    shutil.copytree(old_root, new_root)
    new = FakeEngineSearch(new_root)
    before_manifest = new.active_path.read_bytes()
    # Poison A's object data; a wrong absolute-path read is observable.
    with closing(sqlite3.connect(Path(built["directory"]) / "objects.sqlite3")) as db, db:
        poisoned = dict(item, title="OLD ROOT POISON", text="OLD ROOT POISON")
        db.execute("UPDATE objects SET source_json=?", (pack(poisoned),))
    current = new.status()
    assert current["generation"] == built["generation"] and current["corpus_hash"] == built["corpus_hash"]
    assert all(current["modes"][mode]["ready"] for mode in ("qmd_keyword", "qmd_vector"))
    assert Path(current["directory"]).is_relative_to(new_root)
    assert Path(current["last_build"]["directory"]).is_relative_to(new_root)
    vector = Path(current["libraries"]["cases"]["vector_db_path"])
    vector_before = vector.read_bytes()
    for mode in ("qmd_keyword", "qmd_vector"):
        assert new.search("valve", mode=mode)[0]["text"] == item["text"]
    assert new.calls and all(Path(call["db_path"]).is_relative_to(new_root) for call in new.calls)
    assert all(call["op"] in {"shared_keyword", "vector"} for call in new.calls)
    old_root.rename(old_root.with_name("search-offline"))
    assert new.search("valve", mode="qmd_keyword")[0]["id"] == item["id"]
    assert new.search("valve", mode="qmd_vector")[0]["id"] == item["id"]
    new.calls.clear()
    assert new.build([item], modes=["qmd_keyword", "qmd_vector"], generation="portable-generation")["unchanged"]
    assert not new.calls, "Moving a ready generation must not update or embed it again"
    assert new.active_path.read_bytes() == before_manifest, "Reading a legacy manifest must not rewrite it"
    assert vector.read_bytes() == vector_before


def test_model_path_relocation_requires_same_sha_and_keeps_original_query_identity(tmp_path):
    search = FakeEngineSearch(tmp_path)
    search.model_uri = "D:/old/embeddinggemma.gguf"
    built = search.build([obj("one")], modes=["qmd_keyword", "qmd_vector"], generation="g")
    search.calls.clear()
    search.model_uri = "D:/new/model-renamed.gguf"
    assert search.status()["modes"]["qmd_vector"]["ready"]
    assert search.search("阀门压降", mode="qmd_vector")
    assert search.calls[-1]["index_model_uri"] == built["modes"]["qmd_vector"]["model_uri"]
    search.model_sha = "b" * 64
    assert search.status()["modes"]["qmd_vector"]["state"] == "stale"


def test_legacy_hf_receipt_alias_uses_recorded_native_vector_model_identity(tmp_path):
    search = FakeEngineSearch(tmp_path)
    built = search.build([obj("one")], modes=["qmd_keyword", "qmd_vector"], generation="g")
    vector = built["modes"]["qmd_vector"]
    vector.pop("embedding_model_uri")
    vector["model_uri"] = "hf:original/embeddinggemma.gguf"
    vector["embedding_records"]["cases"]["embedding_rows"][0]["model"] = "D:/original/native/embeddinggemma.gguf"
    # A historical active manifest used the receipt alias at top level while
    # QMD's rows recorded the resolved native file path.
    search.active_path.write_text(json.dumps(built), encoding="utf-8")
    search.calls.clear()
    assert search.search("valve", mode="qmd_vector")
    assert search.calls[-1]["index_model_uri"] == "D:/original/native/embeddinggemma.gguf"


def test_moved_manifest_missing_local_db_never_falls_back_to_existing_old_root(tmp_path):
    old = FakeEngineSearch(tmp_path / "old")
    built = old.build([obj("one")], modes=["qmd_keyword", "qmd_vector"], generation="g")
    new = FakeEngineSearch(tmp_path / "new")
    new.data_dir.mkdir(exist_ok=True)
    new.active_path.write_text(json.dumps(built), encoding="utf-8")
    status = new.status()
    assert status["modes"]["qmd_keyword"]["state"] == "unavailable"
    assert status["modes"]["qmd_vector"]["state"] == "unavailable"
    assert Path(built["directory"]).is_dir()
    with pytest.raises(RuntimeError, match="former root is not used"):
        new.search("valve")
