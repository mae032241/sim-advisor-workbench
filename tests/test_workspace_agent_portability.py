"""Preserve portable conversation data while detaching old-host process identity."""
import json
import os
import shutil

from advisor.server import App


def test_copied_workspace_preserves_history_without_resuming_old_harness(tmp_path):
    old = tmp_path / 'workspace A'
    app = App(old, doc_root=tmp_path, runtime_factory='none')
    session = app.sessions.create('Portable simulation planning conversation fixture', None)
    sid = session['id']
    entry = app.agents._entry(sid)
    entry['record']['backend_threads'] = {'codex': 'old-host-thread'}
    entry['record']['thread_id'] = 'old-host-thread'
    entry['record']['messages'] = [{'role': 'user', 'text': 'Preserve my conversation'}]
    app.agents._persist(sid, entry)
    bound = app.agents.external.register(sid, {'name': 'test external harness', 'pid': os.getpid()})
    assert bound['alive']
    app.agents.close()
    original = (old / 'agent-sessions' / (sid + '.json')).read_bytes()
    target = tmp_path / 'workspace B'
    shutil.copytree(old, target)
    # Old workspace and same-number PID remain alive: neither may be adopted.
    moved = App(target, doc_root=tmp_path, runtime_factory='none')
    try:
        status = moved.agents.status(sid)
        assert status['thread_id'] is None and not status['connected']
        assert status['messages'] == [{'role': 'user', 'text': 'Preserve my conversation'}]
        assert moved.agents.entries[sid]['record']['backend_threads'] == {}
        external = moved.agents.external.status(sid)
        assert not external['alive'] and external['offline_reason'] == 'workspace_relocated'
        assert moved.sessions.get(sid)['goal'] == session['goal']
        assert (old / 'agent-sessions' / (sid + '.json')).read_bytes() == original
    finally:
        moved.agents.close()


def test_legacy_qmd_facade_uses_registered_engine_without_rewriting_old_corpus(tmp_path, monkeypatch):
    from advisor.retrieval_backends import QMDAdapter
    root = tmp_path / 'data'
    config = root / 'qmd/config/qmd/index.yml'
    config.parent.mkdir(parents=True)
    original = 'collections: historical archive only'
    config.write_text(original, encoding='utf-8')
    calls = []

    class Store:
        def __init__(self, path): calls.append(path)
        def reindex(self, modes):
            calls.append(modes)
            return {'modes': {'qmd_keyword': {'ready': True}}}

    monkeypatch.setattr('advisor.library_store.LibraryStore', Store)
    assert QMDAdapter(root).prepare()['modes']['qmd_keyword']['ready']
    assert calls == [root / 'libraries', ['qmd_keyword']]
    assert config.read_text(encoding='utf-8') == original


def test_legacy_proof_anchors_uploads_after_setup_receipt_has_already_moved(tmp_path):
    from advisor.workspace_paths import WorkspacePaths
    previous = tmp_path / 'old data'
    target = tmp_path / 'new data'
    (target / 'uploads').mkdir(parents=True)
    (target / 'uploads/part.step').write_text('geometry fixture', encoding='utf-8')
    (target / 'plugin-offline-setup-receipt.json').write_text(json.dumps({
        'schema': 'sim-advisor-plugin-offline-setup/v1', 'dataDir': str(target)}), encoding='utf-8')
    (target / 'offline-verification.json').write_text(json.dumps({
        'verified': True, 'singlePython': True, 'qmdReport': str(previous / 'offline-checks/run/report.json')}), encoding='utf-8')
    paths = WorkspacePaths(target)
    assert paths.resolve(previous / 'uploads/part.step') == target / 'uploads/part.step'
