"""A temporarily locked progress display must not stop archive construction."""
import json

from scripts import build_case_guides as cli


def test_atomic_progress_retries_transient_windows_reader(tmp_path, monkeypatch):
    path = tmp_path / 'progress.json'
    replace = cli.os.replace
    attempts = []

    def locked_once(source, target):
        attempts.append(True)
        if len(attempts) == 1:
            raise PermissionError('reader has not released delete-sharing')
        return replace(source, target)

    monkeypatch.setattr(cli.os, 'replace', locked_once)
    monkeypatch.setattr(cli.time, 'sleep', lambda _: None)
    cli._write(path, {'completed': 42})
    assert len(attempts) == 2
    assert json.loads(path.read_text(encoding='utf-8')) == {'completed': 42}
    assert list(tmp_path.iterdir()) == [path]


def test_unavailable_progress_display_keeps_work_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, '_write', lambda *_: (_ for _ in ()).throw(PermissionError('temporarily locked')))
    state = {'completed': 42}
    cli._progress_write(tmp_path / 'progress.json', state)
    assert state == {'completed': 42, 'progress_write_skips': 1}
