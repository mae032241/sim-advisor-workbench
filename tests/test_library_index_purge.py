import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.library_index_purge import purge_library_indexes
from advisor.library_search import LibrarySearch, _hash, _json
from advisor.index_paths import resolve_index_manifest, serialize_index_manifest
from advisor.index_manifest_cache import read_manifest
from test_library_search import FakeEngineSearch, obj
from qmd_fixtures import keyword_collection, keyword_insert, keyword_schema


def fixture(tmp_path):
    root = tmp_path / "search"
    search = FakeEngineSearch(root, preserve_history=True)
    kept = obj("kept", "retained", text="retained mesh sizing and laminar flow")
    manifests = []
    for number in (1, 2):
        deleted = obj("deleted-" + str(number), "deleted", text="PURGE_UNIQUE_SECRET_A_7159360_" + str(number))
        manifests.append(search.build([deleted, kept], modes=["native", "qmd_keyword", "qmd_vector"], generation="generation-" + str(number)))
    desired = _hash(_json([kept["id"]]))[:24]
    return root, search, manifests, kept, desired


def hashes(directory):
    return {file.relative_to(directory).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in directory.rglob("*") if file.is_file()}


def test_purge_cleans_current_and_history_native_fts_and_qmd_without_touching_other_library(tmp_path):
    root, search, manifests, kept, desired = fixture(tmp_path)
    preserved = {item["directory"]: hashes(Path(item["libraries"]["retained"]["corpus"]).parent) for item in manifests}
    keyword_preserved = {}
    for item in manifests:
        with closing(sqlite3.connect(item['libraries']['retained']['db_path'])) as db:
            keyword_preserved[item['directory']] = keyword_collection(db, item['libraries']['retained']['collection'])
    result = purge_library_indexes(root, "deleted", generation=desired)
    assert result["active_generation_retargeted"] and result["native_objects_removed"] == 2
    assert result["generations_cleaned"] == 2 and not result["embedding_invoked"]
    assert result["scope"] == "registered-library-search-indexes-current-and-historical"
    for old in manifests:
        directory = Path(old["directory"])
        assert not Path(old["libraries"]["deleted"]["corpus"]).parent.exists()
        assert hashes(Path(old["libraries"]["retained"]["corpus"]).parent) == preserved[old["directory"]]
        keyword = directory / 'qmd-keyword.sqlite3'
        assert keyword.is_file() and b'PURGE_UNIQUE_SECRET_A' not in keyword.read_bytes()
        with closing(sqlite3.connect(keyword)) as db:
            retained_collection = old['libraries']['retained']['collection']
            assert keyword_collection(db, retained_collection) == keyword_preserved[old['directory']]
            assert db.execute('SELECT DISTINCT collection FROM documents').fetchall() == [(retained_collection,)]
            assert not db.execute("SELECT 1 FROM documents_fts WHERE documents_fts MATCH 'PURGE_UNIQUE_SECRET_A'").fetchall()
        native = directory / "objects.sqlite3"
        assert b"PURGE_UNIQUE_SECRET_A" not in native.read_bytes(), "Deleted payload must not remain in SQLite free pages/FTS segments"
        with closing(sqlite3.connect(native)) as db:
            assert db.execute("SELECT id FROM objects").fetchall() == [(kept["id"],)]
            assert db.execute("SELECT o.id FROM object_fts f JOIN objects o ON o.rowid=f.rowid").fetchall() == [(kept["id"],)]
        value = resolve_index_manifest(json.loads((directory / "manifest.json").read_text()), root)
        assert set(value["libraries"]) == {"retained"} and value["object_count"] == 1
        assert "mapping" not in value and "objects" not in value["representation"]
        assert value["representation"]["indexed_objects"] == 1
        assert set(value["modes"]["qmd_vector"]["embedding_records"]) == {"retained"}
    current = search._load_active()
    assert current["generation"] == desired and current["corpus_hash"] == _hash(_json([kept]))
    assert search.search("laminar", mode="native")[0]["id"] == kept["id"]
    assert all(current["modes"][mode]["ready"] for mode in ("native", "qmd_keyword", "qmd_vector"))
    assert not (root / "purge-pending.json").exists() and not (root / "build-lease.json").exists()


def test_stale_remaining_generation_is_not_promoted_to_requested_generation(tmp_path):
    root, _, manifests, _, _ = fixture(tmp_path)
    purge_library_indexes(root, "deleted", generation="different-desired-generation")
    current = LibrarySearch(root)._load_active()
    assert current["generation"] == manifests[-1]["generation"]
    assert current["generation"] != "different-desired-generation"


def test_failure_after_manifest_cleanup_is_retryable_and_keeps_other_library_ready(tmp_path, monkeypatch):
    import advisor.library_index_purge as module
    root, search, manifests, kept, desired = fixture(tmp_path)
    original = module._remove_library_directory
    failed = False

    def locked_once(root, path, *, validate_only=False, validated_bytes=None):
        nonlocal failed
        if not validate_only and path.exists() and not failed:
            failed = True
            raise PermissionError("simulated target QMD reader")
        return original(root, path, validate_only=validate_only, validated_bytes=validated_bytes)

    monkeypatch.setattr(module, "_remove_library_directory", locked_once)
    with pytest.raises(PermissionError):
        purge_library_indexes(root, "deleted", generation=desired)
    assert (root / "purge-pending.json").exists()
    assert not search.lock.locked() and not (root / "build-lease.json").exists()
    # Retrying rereads all generations, including ones created after a failed
    # request; it must not trust only the journal's original directory list.
    latest = search.build([obj("deleted-later", "deleted"), kept], modes=["native"], generation="later")
    result = purge_library_indexes(root, "deleted", generation=desired)
    assert result["active_generation_retargeted"]
    assert not (root / "purge-pending.json").exists()
    for old in [*manifests, latest]:
        assert not Path(old["libraries"]["deleted"]["corpus"]).parent.exists()
    assert search.search("laminar")[0]["id"] == kept["id"]


def test_busy_search_directory_is_rejected_without_deleting_anything(tmp_path):
    root, search, manifests, _, desired = fixture(tmp_path)
    before = search.active_path.read_bytes()
    assert search.lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="active"):
            purge_library_indexes(root, "deleted", generation=desired)
    finally:
        search.lock.release()
    assert search.active_path.read_bytes() == before
    assert all(Path(item["libraries"]["deleted"]["db_path"]).is_file() for item in manifests)


def test_nested_link_in_target_index_is_rejected_without_touching_original_source(tmp_path):
    root, _, manifests, _, desired = fixture(tmp_path)
    original = tmp_path / "original-source"
    original.mkdir()
    (original / "keep.mph").write_bytes(b"original model must remain")
    link = Path(manifests[0]["libraries"]["deleted"]["corpus"]) / "external"
    try:
        link.symlink_to(original, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is not permitted on this host")
    with pytest.raises(ValueError, match="symlink|junction"):
        purge_library_indexes(root, "deleted", generation=desired)
    assert (original / "keep.mph").read_bytes() == b"original model must remain"


def test_unrelated_generation_is_not_rewritten_and_manifests_are_read_once(tmp_path, monkeypatch):
    import advisor.library_index_purge as module
    root = tmp_path / "search"
    search = FakeEngineSearch(root, preserve_history=True)
    kept = obj("kept", "retained")
    untouched = search.build([kept], modes=["native"], generation="unrelated")
    search.build([kept, obj("deleted", "deleted")], modes=["native"], generation="target")
    preserved = hashes(Path(untouched["directory"]))
    reads = {}
    original = module.read_manifest

    def counted(path, *args, **kwargs):
        if path.name in {"active.json", "last-build.json", "manifest.json"}:
            reads[path] = reads.get(path, 0) + 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(module, "read_manifest", counted)
    purge_library_indexes(root, "deleted", generation="desired")
    assert reads and all(count == 1 for count in reads.values())
    assert hashes(Path(untouched["directory"])) == preserved


def test_manifest_locator_conversion_does_not_copy_representation_and_mapping_trees(tmp_path, monkeypatch):
    import advisor.library_index_purge as module
    root, _, manifests, kept, desired = fixture(tmp_path)
    calls = []

    def observed(convert):
        def convert_locators(value, search_root):
            assert set(value) <= {"directory", "libraries", "path_schema", "keyword_storage"}
            calls.append(convert.__name__)
            return convert(value, search_root)
        return convert_locators

    monkeypatch.setattr(module, "resolve_index_manifest", observed(module.resolve_index_manifest))
    monkeypatch.setattr(module, "serialize_index_manifest", observed(module.serialize_index_manifest))
    purge_library_indexes(root, "deleted", generation=desired)
    assert {"resolve_index_manifest", "serialize_index_manifest"} <= set(calls)
    for manifest in manifests:
        value = json.loads((Path(manifest["directory"]) / "manifest.json").read_text())
        assert "objects" not in value["representation"] and "mapping" not in value
        with closing(sqlite3.connect(Path(manifest["directory"]) / "objects.sqlite3")) as db:
            assert db.execute("SELECT id FROM objects").fetchall() == [(kept["id"],)]
        assert value["directory"].startswith("generations/")
        assert value["libraries"]["retained"]["corpus"].startswith(value["directory"] + "/")


@pytest.mark.parametrize("legacy", [False, True])
def test_purge_accepts_relative_and_old_absolute_locators_without_rewriting_provenance(tmp_path, legacy):
    root, _, manifests, kept, desired = fixture(tmp_path)
    paths = [root / "active.json", root / "last-build.json", *(Path(item["directory"]) / "manifest.json" for item in manifests)]
    source_uri = "file:///Z:/original-source/model.mph"
    model_uri = "Z:/external-models/embedding.gguf"
    for path in paths:
        value = read_manifest(path)
        value["source_uri"] = source_uri
        value["modes"]["qmd_vector"]["model_uri"] = model_uri
        if legacy:
            value.pop("path_schema")
            value["directory"] = "Z:/old-search/" + value["directory"]
            for library in value["libraries"].values():
                for field in ("corpus", "db_path", "vector_db_path"):
                    library[field] = "Z:/old-search/" + library[field]
        path.write_text(json.dumps(value), encoding="utf-8")
    purge_library_indexes(root, "deleted", generation=desired)
    for path in paths:
        value = read_manifest(path)
        assert value["source_uri"] == source_uri
        assert value["modes"]["qmd_vector"]["model_uri"] == model_uri
        assert value["path_schema"] == "search-root-relative-v1"
        assert value["directory"].startswith("generations/")
        assert value["object_count"] == 1 and "mapping" not in value


@pytest.mark.parametrize('legacy', [False, True])
def test_old_per_library_keyword_databases_remain_purgeable_after_shared_storage_upgrade(tmp_path, legacy):
    root, search, manifests, kept, desired = fixture(tmp_path)
    retained = {}
    for old in manifests:
        directory = Path(old['directory'])
        authority = directory / 'manifest.json'
        value = resolve_index_manifest(read_manifest(authority), root)
        shared = directory / 'qmd-keyword.sqlite3'
        with closing(sqlite3.connect(shared)) as source:
            for library, item in value['libraries'].items():
                per_library = directory / item['key'] / 'qmd-keyword.sqlite3'
                with closing(sqlite3.connect(per_library)) as target, target:
                    keyword_schema(target)
                    for filename, title, body in source.execute(
                            'SELECT d.path,d.title,c.doc FROM documents d JOIN content c ON c.hash=d.hash WHERE d.collection=?',
                            (item['collection'],)):
                        keyword_insert(target, 'objects', filename, title, body)
                item['db_path'] = str(per_library)
                item.pop('collection')
        shared.unlink()
        value.pop('keyword_storage')
        stored = serialize_index_manifest(value, root)
        if legacy:
            stored.pop('path_schema')
            stored['directory'] = 'Z:/previous-search/' + stored['directory']
            for item in stored['libraries'].values():
                for field in ('corpus', 'db_path', 'vector_db_path'):
                    item[field] = 'Z:/previous-search/' + item[field]
        authority.write_text(json.dumps(stored), encoding='utf-8')
        retained[str(directory)] = hashes(directory / value['libraries']['retained']['key'])
    result = purge_library_indexes(root, 'deleted', generation=desired)
    assert result['native_objects_removed'] == 2
    for old in manifests:
        directory = Path(old['directory'])
        assert not (directory / old['libraries']['deleted']['key']).exists()
        assert hashes(directory / old['libraries']['retained']['key']) == retained[str(directory)]
        value = resolve_index_manifest(read_manifest(directory / 'manifest.json'), root)
        assert 'keyword_storage' not in value
        assert Path(value['libraries']['retained']['db_path']).parent.name == old['libraries']['retained']['key']
    assert search.search('laminar', mode='native')[0]['id'] == kept['id']


def test_index_tree_is_inventoried_once_without_resolving_every_child(tmp_path, monkeypatch):
    import advisor.library_index_purge as module
    root = tmp_path / "search"
    target = root / "generations/gen/lib_0123456789abcdef0123"
    target.mkdir(parents=True)
    for number in range(250):
        (target / (str(number) + ".md")).write_bytes(b"retained cache bytes")
    calls = []
    original = module._owned

    def counted(root, path):
        calls.append(path)
        return original(root, path)

    monkeypatch.setattr(module, "_owned", counted)
    total = module._remove_library_directory(root, target, validate_only=True)
    assert module._remove_library_directory(root, target, validated_bytes=total) == total
    assert total == 250 * len(b"retained cache bytes") and len(calls) == 3
    assert not target.exists()


def test_failed_vacuum_is_retried_even_after_native_delete_committed(tmp_path, monkeypatch):
    import advisor.library_index_purge as module
    root, _, manifests, _, desired = fixture(tmp_path)
    connect = sqlite3.connect
    failed = False
    vacuums = []

    class Connection:
        def __init__(self, db, path):
            self.db = db
            self.path = str(path)
        def __getattr__(self, name):
            return getattr(self.db, name)
        def __enter__(self):
            self.db.__enter__()
            return self
        def __exit__(self, *args):
            return self.db.__exit__(*args)
        def execute(self, sql, *args):
            nonlocal failed
            if sql == "VACUUM":
                vacuums.append(self.path)
                if not failed and self.path.endswith('objects.sqlite3'):
                    failed = True
                    raise sqlite3.OperationalError("fixture interrupted compaction")
            return self.db.execute(sql, *args)

    monkeypatch.setattr(module.sqlite3, "connect", lambda *args, **kwargs: Connection(connect(*args, **kwargs), args[0]))
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        purge_library_indexes(root, "deleted", generation=desired)
    pending = json.loads((root / "purge-pending.json").read_text())
    assert any(entry["native_pending"] for entry in pending["entries"].values())
    before_retry = len(vacuums)
    purge_library_indexes(root, "deleted", generation=desired)
    assert sum(path.endswith('objects.sqlite3') for path in vacuums[before_retry:]) == 2, "Committed DELETE still needs the interrupted VACUUM"
    for manifest in manifests:
        assert b"PURGE_UNIQUE_SECRET_A" not in (Path(manifest["directory"]) / "objects.sqlite3").read_bytes()


def test_shared_keyword_compaction_failure_keeps_a_collection_retry_after_delete_commits(tmp_path, monkeypatch):
    import advisor.library_index_purge as module
    root, _, manifests, _, desired = fixture(tmp_path)
    connect = sqlite3.connect
    purge = module.qmd_shared.purge_collection
    failed = False
    retries = []

    class Connection:
        def __init__(self, db):
            self.db = db
        def __getattr__(self, name):
            return getattr(self.db, name)
        def __enter__(self):
            self.db.__enter__()
            return self
        def __exit__(self, *args):
            return self.db.__exit__(*args)
        def execute(self, sql, *args):
            nonlocal failed
            if sql == 'PRAGMA incremental_vacuum' and not failed:
                failed = True
                raise sqlite3.OperationalError('shared keyword compaction interrupted')
            return self.db.execute(sql, *args)

    def connected(path, *args, **kwargs):
        db = connect(path, *args, **kwargs)
        return Connection(db) if str(path).endswith('qmd-keyword.sqlite3') else db

    def tracked(path, collection, *, force_compact=False):
        retries.append((str(path), force_compact))
        return purge(path, collection, force_compact=force_compact)

    monkeypatch.setattr(module.qmd_shared.sqlite3, 'connect', connected)
    monkeypatch.setattr(module.qmd_shared, 'purge_collection', tracked)
    with pytest.raises(sqlite3.OperationalError, match='shared keyword'):
        purge_library_indexes(root, 'deleted', generation=desired)
    journal = json.loads((root / 'purge-pending.json').read_text())
    pending = [(relative, entry) for relative, entry in journal['entries'].items() if entry['shared_pending']]
    assert len(pending) == 1
    relative, entry = pending[0]
    with closing(connect(root / relative / 'qmd-keyword.sqlite3')) as db:
        assert not db.execute('SELECT 1 FROM documents WHERE collection=?',
                              (entry['shared_pending'][0],)).fetchall()
    purge_library_indexes(root, 'deleted', generation=desired)
    assert len(retries) == 3
    assert [retry for path, retry in retries if Path(path).parent == root / relative] == [False, True]
    assert sum(retry for _, retry in retries) == 1
    assert not (root / 'purge-pending.json').exists()
    for manifest in manifests:
        assert b'PURGE_UNIQUE_SECRET_A' not in (Path(manifest['directory']) / 'qmd-keyword.sqlite3').read_bytes()


def test_shared_keyword_sidecar_alias_is_rejected_before_sqlite_mutation(tmp_path):
    root, _, manifests, _, desired = fixture(tmp_path)
    database = Path(manifests[0]['directory']) / 'qmd-keyword.sqlite3'
    before = database.read_bytes()
    original = tmp_path / 'original.sqlite3'
    original.write_bytes(b'Original outside shared DB lifecycle')
    sidecar = database.with_name(database.name + '-wal')
    try:
        sidecar.symlink_to(original)
    except OSError:
        pytest.skip('Creating file symlinks is not permitted on this host')
    with pytest.raises(ValueError, match='escapes|symlink|junction'):
        purge_library_indexes(root, 'deleted', generation=desired)
    assert database.read_bytes() == before
    assert original.read_bytes() == b'Original outside shared DB lifecycle'


def only_library_fixture(tmp_path, *, builds=3):
    from advisor.library_search import _NODE_BRIDGE
    from advisor.qmd_shared import _WORKER
    root = tmp_path / "search"
    search = FakeEngineSearch(root, preserve_history=True)
    manifests = [search.build([obj("deleted-" + str(number), "deleted", "PURGE_LAST_LIBRARY_SECRET_" + str(number))],
                              modes=["native", "qmd_keyword", "qmd_vector"], generation="only-" + str(number))
                 for number in range(builds)]
    runtime = root / "runtime"
    runtime.mkdir()
    script = runtime / ("qmd_bridge_" + _hash(_NODE_BRIDGE)[:16] + ".mjs")
    script.write_text(_NODE_BRIDGE, encoding="utf-8")
    (runtime / ('qmd_shared_' + _hash(_WORKER)[:16] + '.mjs')).write_text(_WORKER, encoding='utf-8')
    return root, search, manifests, script


def test_last_library_purge_reclaims_empty_generations_pointers_and_generated_runtime(tmp_path):
    root, search, manifests, script = only_library_fixture(tmp_path)
    lock = root / "build.lock"
    lock_identity = lock.stat().st_ino
    original = tmp_path / "original.mph"
    original.write_bytes(b"Original sources remain outside the index lifecycle")
    custom = root / "custom-corpus.sqlite3"
    custom.write_bytes(b"Independent corpus remains intact")

    result = purge_library_indexes(root, "deleted", generation="empty-library")

    assert result["native_objects_removed"] == 3
    assert result["empty_generations_removed"] == 3
    assert result["search_metadata_bytes_removed"] > 3 * 32768
    assert result["runtime_bytes_removed"] > 0 and result["warnings"] == []
    assert all(not Path(manifest["directory"]).exists() for manifest in manifests)
    assert not (root / "generations").exists() and not script.parent.exists()
    assert all(not (root / name).exists() for name in ("active.json", "last-build.json", "purge-pending.json", "build-lease.json"))
    assert lock.stat().st_ino == lock_identity and root.is_dir()
    assert custom.read_bytes() == b"Independent corpus remains intact"
    assert original.read_bytes() == b"Original sources remain outside the index lifecycle"
    status = search.status()
    assert not status["ready"] and not status["modes"]["native"]["ready"]
    assert status["generation"] is None
    with pytest.raises(RuntimeError, match="not ready"):
        search.search("valve")
    assert not (root / "generations").exists(), "Reading empty status must not create indexes"


def test_last_library_purge_preserves_shared_database_with_independent_global_context(tmp_path):
    root, _, manifests, script = only_library_fixture(tmp_path, builds=1)
    database = Path(manifests[0]['directory']) / 'qmd-keyword.sqlite3'
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT INTO store_config VALUES('global_context','Independent global search context')")
    result = purge_library_indexes(root, 'deleted', generation='empty-library')
    assert result['empty_generations_removed'] == 0 and result['warnings']
    assert database.is_file() and script.is_file()
    with closing(sqlite3.connect(database)) as db:
        assert db.execute('SELECT count(*) FROM documents').fetchone() == (0,)
        assert db.execute('SELECT count(*) FROM store_collections').fetchone() == (0,)
        assert db.execute("SELECT value FROM store_config WHERE key='global_context'").fetchone() == ('Independent global search context',)
    assert b'PURGE_LAST_LIBRARY_SECRET' not in database.read_bytes()


@pytest.mark.parametrize("failed_name", ["objects.sqlite3", "qmd-keyword.sqlite3", "manifest.json", "runtime"])
def test_empty_generation_and_runtime_unlink_failure_requires_retry(tmp_path, monkeypatch, failed_name):
    root, search, manifests, script = only_library_fixture(tmp_path, builds=1)
    directory = Path(manifests[0]["directory"])
    target = script if failed_name == "runtime" else directory / failed_name
    original = Path.unlink
    failed = False

    def locked_once(path, *args, **kwargs):
        nonlocal failed
        if path == target and not failed:
            failed = True
            raise PermissionError("Fixture reader still holds a disposable index file")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_once)
    with pytest.raises(PermissionError, match="reader"):
        purge_library_indexes(root, "deleted", generation="empty-library")
    pending = json.loads((root / "purge-pending.json").read_text())
    assert pending["entries"][directory.relative_to(root).as_posix()]["empty_removal"] is True
    assert target.exists() and not search.lock.locked()
    assert (root / "build.lock").exists() and not (root / "build-lease.json").exists()

    result = purge_library_indexes(root, "deleted", generation="empty-library")

    assert not target.exists() and not directory.exists() and not script.parent.exists()
    assert not (root / "purge-pending.json").exists()
    assert result["warnings"] == [] and not search.status()["ready"]


@pytest.mark.parametrize("custom", ["generation-file", "native-table", "runtime-script", "runtime-subdirectory", "bridge-name-collision"])
def test_empty_cleanup_keeps_unrecognized_generation_and_runtime_content(tmp_path, custom):
    root, _, manifests, script = only_library_fixture(tmp_path, builds=1)
    directory = Path(manifests[0]["directory"])
    if custom == "generation-file":
        retained = directory / "custom-notes.txt"
        retained.write_bytes(b"Custom generated-directory attachment")
    elif custom == "native-table":
        retained = directory / "objects.sqlite3"
        with closing(sqlite3.connect(retained)) as db, db:
            db.execute("CREATE TABLE custom_corpus(payload TEXT)")
            db.execute("INSERT INTO custom_corpus VALUES('Independent table remains')")
    elif custom == "runtime-subdirectory":
        retained = script.parent / "custom" / "notes.txt"
        retained.parent.mkdir()
        retained.write_bytes(b"Custom runtime attachment")
    else:
        retained = script if custom == "bridge-name-collision" else script.parent / "custom.mjs"
        retained.write_bytes(b"Custom runtime script")
    content = retained.read_bytes()

    result = purge_library_indexes(root, "deleted", generation="empty-library")

    if custom == "native-table":
        with closing(sqlite3.connect(retained)) as db:
            assert db.execute("SELECT payload FROM custom_corpus").fetchall() == [("Independent table remains",)]
            assert db.execute("SELECT count(*) FROM objects").fetchone() == (0,)
        assert b"PURGE_LAST_LIBRARY_SECRET" not in retained.read_bytes()
    else:
        assert retained.read_bytes() == content
    if custom in {"generation-file", "native-table"}:
        assert result["empty_generations_removed"] == 0 and result["warnings"]
        assert script.exists(), "Runtime is retained while any generation remains"
    else:
        assert not directory.exists() and script.parent.is_dir()
        assert result["warnings"] == []


@pytest.mark.parametrize("dependency", [False, True])
def test_empty_history_is_reclaimed_unless_referenced_by_a_retained_library(tmp_path, dependency):
    root = tmp_path / "search"
    search = FakeEngineSearch(root, preserve_history=True)
    old = search.build([obj("deleted", "deleted")], modes=["native"], generation="old")
    keeper = obj("keeper", "retained", "Retained laminar valve flow")
    current = search.build([keeper], modes=["native", "qmd_keyword"], generation="retained")
    if dependency:
        for path in (root / "active.json", root / "last-build.json", Path(current["directory"]) / "manifest.json"):
            value = read_manifest(path)
            value["libraries"]["retained"]["keyword_reused_from_directory"] = Path(old["directory"]).relative_to(root).as_posix()
            path.write_text(json.dumps(value), encoding="utf-8")
    retained_bytes = hashes(Path(current["directory"]))

    result = purge_library_indexes(root, "deleted", generation=_hash(_json([keeper["id"]]))[:24])

    assert result["empty_generations_removed"] == (0 if dependency else 1)
    assert Path(old["directory"]).is_dir() is dependency
    if dependency:
        assert b"lo-deleted" not in (Path(old["directory"]) / "objects.sqlite3").read_bytes()
    assert hashes(Path(current["directory"])) == retained_bytes
    assert search.search("laminar", mode="native")[0]["id"] == keeper["id"]
    assert search.search("laminar", mode="qmd_keyword")[0]["id"] == keeper["id"]


def test_empty_cleanup_holds_the_same_process_lease_and_keeps_lock_inode(tmp_path, monkeypatch):
    import advisor.library_index_purge as module
    root, _, _, _ = only_library_fixture(tmp_path, builds=1)
    lock = root / "build.lock"
    identity = lock.stat().st_ino
    inventory = module._empty_inventory
    checked = []

    def while_leased(root, directory):
        assert lock.stat().st_ino == identity
        with pytest.raises(RuntimeError, match="Another process"):
            LibrarySearch.for_maintenance(root)._acquire_lease("racing-build")
        checked.append(directory)
        return inventory(root, directory)

    monkeypatch.setattr(module, "_empty_inventory", while_leased)
    purge_library_indexes(root, "deleted", generation="empty-library")

    assert checked and lock.stat().st_ino == identity and root.is_dir()
    search = LibrarySearch.for_maintenance(root)
    handle, token = search._acquire_lease("later-build")
    search._release_lease(handle, token)
    assert lock.stat().st_ino == identity


def test_empty_cleanup_retry_preserves_a_newer_generation_published_after_failure(tmp_path, monkeypatch):
    root, search, manifests, _ = only_library_fixture(tmp_path, builds=1)
    old = Path(manifests[0]["directory"])
    original = Path.unlink
    failed = False

    def interrupted(path, *args, **kwargs):
        nonlocal failed
        if path == old / "manifest.json" and not failed:
            failed = True
            raise PermissionError("Fixture crash after empty native database removal")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises(PermissionError, match="crash"):
        purge_library_indexes(root, "deleted", generation="empty-library")
    assert not (old / "objects.sqlite3").exists()
    keeper = obj("keeper", "retained", "Retained laminar flow")
    current = search.build([keeper], modes=["native", "qmd_keyword"], generation="newer")
    preserved = hashes(Path(current["directory"]))
    pointers = {name: (root / name).read_bytes() for name in ("active.json", "last-build.json")}

    purge_library_indexes(root, "deleted", generation=_hash(_json([keeper["id"]]))[:24])

    assert not old.exists() and not (root / "purge-pending.json").exists()
    assert hashes(Path(current["directory"])) == preserved
    assert all((root / name).read_bytes() == content for name, content in pointers.items())
    assert search.search("laminar")[0]["id"] == keeper["id"]
