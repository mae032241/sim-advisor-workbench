"""Synthetic automatic builds, atomic generations, relocation and lifecycle."""
import json
from pathlib import Path
import shutil
import threading
import time

import pytest

from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.html_tree import HtmlTreeIndex, read_html_text
from advisor.html_tree_store import HtmlTreeRepository, tree_id
from advisor.library_store import LibraryStore


PACKAGE = "com.comsol.help.synthetic"


def source(root, *, package_root=False):
    directory = root if package_root else root / PACKAGE
    directory.mkdir(parents=True)
    (directory / "toc.xml").write_text(f'<toc xmlns="urn:Eclipse-TOC-Schema" label="Synthetic manual"><topic label="First" href="/{PACKAGE}/first.html"/><topic label="Second" href="second.html#part"/></toc>', encoding="utf-8")
    (directory / "first.html").write_text('<body><h1>First</h1><p>Complete synthetic first body.</p></body>', encoding="utf-8")
    (directory / "second.html").write_text('<body><h1 id="part">Second</h1><p>Complete synthetic second body.</p></body>', encoding="utf-8")
    return root


def library(tmp_path, *, roots=None, lid="manuals"):
    store = LibraryStore(tmp_path / "catalog")
    if roots is None:
        roots = [{"id": "main", "path": str(source(tmp_path / "source")), "role": "manuals"}]
    value = store.register("Synthetic manuals", "user-manuals", roots, library_id=lid)
    store.set_enabled(value["id"], True)
    return store, value


def test_normal_manual_build_automatically_adds_tree_without_search_modes(tmp_path, monkeypatch):
    store, lib = library(tmp_path)
    monkeypatch.setattr('advisor.library_search.LibrarySearch.build', lambda *a, **kw: pytest.fail("QMD/embedding construction must not run"))
    snapshots = []
    original = store._save_job
    def save(job):
        snapshots.append(json.loads(json.dumps(job)))
        original(job)
    monkeypatch.setattr(store, '_save_job', save)
    job = store.build(lib["id"], modes=None)
    assert job["status"] == "completed", job
    assert job["html_tree"]["state"] == "ready"
    assert any(j["stage"] == "html_tree" and j["progress"]["unit"] == "documents" for j in snapshots)
    repo = HtmlTreeRepository(store)
    assert repo.summary()["tree_count"] == 1 and repo.summary()["available_tree_count"] == 1
    entry = repo.list_trees()[0]
    assert entry["tree_id"] == tree_id(lib["id"], "main")
    assert isinstance(entry["index_path"], Path) and entry["document_count"] == 2
    with HtmlTreeIndex(entry["index_path"]) as index:
        assert index.meta["library_id"] == lib["id"]


def test_no_native_toc_is_explicitly_unsupported_and_keeps_qmd_fallback(tmp_path):
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "notes.md").write_text('# Notes\nA synthetic manual without a native TOC.', encoding="utf-8")
    store, lib = library(tmp_path, roots=[{"id": "plain", "path": str(directory), "role": "manuals"}])
    job = store.build(lib["id"], modes=[])
    assert job["status"] == "completed"
    assert job["html_tree"]["state"] == "not_supported" and job["html_tree"]["fallback"] == "qmd"
    assert HtmlTreeRepository(store).summary()["state"] == "not_supported"


def test_multiple_libraries_roots_and_package_scoped_registration(tmp_path):
    first = source(tmp_path / "first")
    scoped = source(tmp_path / "scoped" / PACKAGE, package_root=True)
    store, lib = library(tmp_path, roots=[{"id": "full", "path": str(first)}, {"id": "package", "path": str(scoped)}])
    second = source(tmp_path / "second")
    other = store.register("Other manual", "official-manuals", [{"id": "other", "path": str(second)}], library_id="other")
    store.set_enabled(other["id"], True)
    repo = HtmlTreeRepository(store)
    assert len(repo.build_library(lib["id"])["trees"]) == 2
    repo.build_library(other["id"])
    entries = repo.list_trees()
    assert len(entries) == 3 and len({e["tree_id"] for e in entries}) == 3
    assert repo.summary()["document_count"] == 6
    package_entry = repo.resolve(tree_id(lib["id"], "package"))
    with HtmlTreeIndex(package_entry["index_path"]) as index:
        node = index.resolve_source_path("second.html")["nodes"][0]
        assert node["package"] == PACKAGE
        assert "synthetic second body" in read_html_text(scoped / node["doc_path"], node["anchor"])["text"]


def test_unchanged_fingerprints_are_reused_and_obsolete_generations_cleaned(tmp_path, monkeypatch):
    store, lib = library(tmp_path)
    repo = HtmlTreeRepository(store)
    first = repo.build_library(lib["id"])
    old = repo.list_trees()[0]["index_path"]
    assert first["trees"][0]["hashing"] == {"hashed": 2, "reused": 0}
    calls = []
    original = Path.open
    def opened(path, mode='r', *args, **kwargs):
        if path.suffix == '.html' and mode == 'rb':
            calls.append(path)
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', opened)
    second = repo.build_library(lib["id"])
    assert second["trees"][0]["hashing"] == {"hashed": 0, "reused": 2} and calls == []
    assert not old.exists()
    changed = Path(lib["roots"][0]["path"]) / PACKAGE / "first.html"
    changed.write_text('<body>Changed synthetic document content.</body>', encoding='utf-8')
    third = repo.build_library(lib["id"])
    assert third["trees"][0]["hashing"] == {"hashed": 1, "reused": 1}
    assert calls == [changed]
    active = repo.list_trees()[0]["index_path"].parent
    assert [p for p in active.parent.iterdir() if p.is_dir()] == [active]


def test_failure_and_cancellation_leave_previous_active_registry_untouched(tmp_path, monkeypatch):
    store, lib = library(tmp_path)
    repo = HtmlTreeRepository(store)
    repo.build_library(lib["id"])
    before = repo.registry_path.read_bytes()
    prior = repo.list_trees()[0]["index_path"]
    from advisor import html_tree_store as module
    original = module.build_html_tree
    def failed(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Synthetic publication failure")
    monkeypatch.setattr(module, 'build_html_tree', failed)
    with pytest.raises(RuntimeError, match="publication failure"):
        repo.build_library(lib["id"])
    assert repo.registry_path.read_bytes() == before and prior.is_file()
    monkeypatch.setattr(module, 'build_html_tree', original)
    control = BuildControl(tmp_path / 'cancel.request', threading.Event())
    def cancel(update):
        if update['progress']['phase'] == 'hashing':
            control._event.set()
    with build_control(control), pytest.raises(BuildCancelled):
        repo.build_library(lib["id"], on_progress=cancel)
    assert repo.registry_path.read_bytes() == before and prior.is_file()
    assert not list(repo.directory.rglob('.building-*'))
    assert not (repo.directory / '.registry.lock').exists()


def test_two_root_failure_is_one_atomic_library_publication(tmp_path, monkeypatch):
    one, two = source(tmp_path / 'one'), source(tmp_path / 'two')
    store, lib = library(tmp_path, roots=[{'id': 'one', 'path': str(one)}, {'id': 'two', 'path': str(two)}])
    repo = HtmlTreeRepository(store)
    repo.build_library(lib['id'])
    before = repo.registry_path.read_bytes()
    from advisor import html_tree_store as module
    original = module.build_html_tree
    def failed(root, *args, **kwargs):
        if root == two:
            raise ValueError('Synthetic second root failed')
        return original(root, *args, **kwargs)
    monkeypatch.setattr(module, 'build_html_tree', failed)
    with pytest.raises(ValueError, match='second root'):
        repo.build_library(lib['id'])
    assert repo.registry_path.read_bytes() == before
    assert all(e['index_path'].is_file() for e in repo.list_trees())
    assert not list(repo.directory.rglob('.building-*'))


def test_summary_and_resolve_do_not_hash_or_open_html_and_follow_live_enable(tmp_path, monkeypatch):
    store, lib = library(tmp_path)
    repo = HtmlTreeRepository(store)
    repo.build_library(lib['id'])
    original = Path.open
    def no_body(path, *args, **kwargs):
        if path.suffix == '.html':
            pytest.fail('Navigation/summary must not reopen a body')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', no_body)
    identity = repo.list_trees()[0]['tree_id']
    assert repo.resolve(identity)['ready']
    assert repo.summary()['available_tree_count'] == 1
    store.set_enabled(lib['id'], False)
    assert repo.summary()['available_tree_count'] == 0 and repo.summary()['state'] == 'disabled'
    store.set_enabled(lib['id'], True)
    assert repo.summary()['available_tree_count'] == 1
    with pytest.raises(KeyError):
        repo.resolve('tree-' + '0' * 32)


def test_copying_workspace_relocates_registry_and_source_bindings(tmp_path):
    old = tmp_path / 'old'
    old.mkdir()
    store = LibraryStore(old)
    manual = source(old / 'sources')
    lib = store.register('Relocatable manuals', 'user-manuals', [{'id':'main','path':str(manual)}], library_id='move')
    store.set_enabled(lib['id'], True)
    repo = HtmlTreeRepository(store)
    repo.build_library(lib['id'])
    registry_bytes = repo.registry_path.read_bytes()
    assert str(old).encode() not in registry_bytes
    moved = tmp_path / 'moved'
    shutil.copytree(old, moved)
    relocated_store = LibraryStore(moved)
    relocated = HtmlTreeRepository(relocated_store)
    entry = relocated.list_trees()[0]
    assert entry['index_path'].is_relative_to(moved)
    binding = Path(relocated_store.library(lib['id'])['roots'][0]['path'])
    assert binding.is_relative_to(moved)
    with HtmlTreeIndex(entry['index_path']) as index:
        node = index.resolve_source_path(PACKAGE + '/first.html')['nodes'][0]
        assert 'synthetic first body' in read_html_text(binding / node['doc_path'])['text']


def test_hard_delete_removes_only_library_tree_files_and_metadata(tmp_path):
    store, lib = library(tmp_path)
    other_source = source(tmp_path / 'other-source')
    other = store.register('Other', 'user-manuals', [{'id':'main','path':str(other_source)}], library_id='other')
    store.set_enabled(other['id'], True)
    repo = HtmlTreeRepository(store)
    repo.build_library(lib['id'])
    repo.build_library(other['id'])
    own = repo.resolve(tree_id(lib['id'], 'main'))['index_path']
    keep = repo.resolve(tree_id(other['id'], 'main'))['index_path']
    store.delete_library(lib['id'])
    assert repo.summary()['available_tree_count'] == 1
    result = store.purge_library(lib['id'], confirmed=True)
    assert result['counts']['html_trees'] == 1
    assert not own.exists() and keep.is_file()
    assert [e['library_id'] for e in repo.list_trees()] == [other['id']]
    assert (Path(lib['roots'][0]['path']) / PACKAGE / 'first.html').is_file()
    assert lib['id'] not in json.loads(repo.registry_path.read_text('utf-8'))['libraries']


def test_tree_only_job_and_cli_never_scan_extract_or_build_qmd(tmp_path, monkeypatch, capsys):
    store, lib = library(tmp_path)
    monkeypatch.setattr(store, 'scan', lambda *a, **kw: pytest.fail('Tree-only must not scan/extract catalog sources'))
    monkeypatch.setattr('advisor.library_search.LibrarySearch.build', lambda *a, **kw: pytest.fail('Tree-only must not construct QMD'))
    job = store.build_tree(lib['id'])
    assert job['status'] == 'completed' and job['kind'] == 'html_tree' and job['html_tree']['state'] == 'ready'
    assert store.objects([lib['id']]) == []
    import library_cli
    monkeypatch.setattr(library_cli, 'LibraryStore', lambda path: store)
    monkeypatch.setattr('sys.argv', ['library_cli.py', 'build-tree', lib['id']])
    assert library_cli.main() == 0
    assert json.loads(capsys.readouterr().out)['html_tree']['state'] == 'ready'


def test_existing_cancel_api_stops_hash_stage_and_preserves_active_tree(tmp_path, monkeypatch):
    store, lib = library(tmp_path)
    store.build_tree(lib['id'])
    repo = HtmlTreeRepository(store)
    before = repo.registry_path.read_bytes()
    from advisor import html_tree_store as module
    original = module.build_html_tree
    def wrapped(*args, **kwargs):
        progress = kwargs['on_progress']
        def report(update):
            progress(update)
            if update['progress']['phase'] == 'hashing':
                active = next(j for j in store.jobs_summary() if j['kind'] == 'html_tree' and j['status'] in {'running','cancel_requested'})
                store.cancel_build(active['id'])
        return original(*args, **dict(kwargs, on_progress=report))
    monkeypatch.setattr(module, 'build_html_tree', wrapped)
    result = store.build_tree(lib['id'])
    assert result['status'] == 'cancelled' and result['cancelled_stage'] == 'html_tree'
    assert repo.registry_path.read_bytes() == before
    assert not (store.root / '.build.lock').exists()


def test_async_tree_only_job_uses_the_same_job_contract(tmp_path):
    store, lib = library(tmp_path)
    job = store.start_build_tree(lib['id'])
    deadline = time.monotonic() + 5
    while job['status'] in {'queued','running','cancel_requested'} and time.monotonic() < deadline:
        time.sleep(0.01)
        job = store.job(job['id'])
    assert job['status'] == 'completed' and job['kind'] == 'html_tree'


def test_tutorial_only_root_is_not_supported_and_model_library_is_not_built(tmp_path):
    root = tmp_path / 'tutorials'
    directory = root / 'com.comsol.help.models.synthetic'
    directory.mkdir(parents=True)
    (directory / 'toc.xml').write_text('<toc><topic label="Example" href="example.html"/></toc>', encoding='utf-8')
    store, lib = library(tmp_path, roots=[{'id':'main','path':str(root)}])
    assert HtmlTreeRepository(store).build_library(lib['id'])['state'] == 'not_supported'
    model = store.register('Models', 'user-models', [{'id':'main','path':str(root)}], library_id='models')
    store.set_enabled(model['id'], True)
    assert HtmlTreeRepository(store).build_library(model['id'])['state'] == 'not_supported'


def test_registry_locator_escape_is_rejected_without_removing_anything(tmp_path):
    store, lib = library(tmp_path)
    repo = HtmlTreeRepository(store)
    repo.build_library(lib['id'])
    data = json.loads(repo.registry_path.read_text('utf-8'))
    entry = next(iter(data['trees'].values()))
    entry['index_path'] = '../outside/index.sqlite3'
    repo.registry_path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='locator'):
        repo.list_trees()
