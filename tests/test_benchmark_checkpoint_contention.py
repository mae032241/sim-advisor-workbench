from pathlib import Path

from scripts import benchmark_product_batch as batch
from scripts import benchmark_route_selection as experiment
from test_benchmark_route_selection import Product, arguments


def test_atomic_benchmark_write_retries_transient_windows_reader_lock(tmp_path, monkeypatch):
    original = Path.replace
    calls = []
    def locked_once(self, target):
        calls.append(target)
        if len(calls) == 1:
            raise PermissionError('reader temporarily holds target')
        return original(self, target)
    monkeypatch.setattr(Path, 'replace', locked_once)
    batch.write(tmp_path / 'state.json', {'complete': True})
    assert len(calls) == 2 and (tmp_path / 'state.json').is_file()


def test_checkpoint_permission_failure_never_interrupts_a_healthy_model(tmp_path, monkeypatch):
    product = Product()
    original = experiment.write
    remaining = [3]
    def unavailable_checkpoint(path, value):
        if path.name == 'run.json' and remaining[0]:
            remaining[0] -= 1
            raise PermissionError('progress file is temporarily unavailable')
        return original(path, value)
    monkeypatch.setattr(experiment, 'write', unavailable_checkpoint)
    result = experiment.run_turn(arguments(tmp_path), 'Same original task request', runner=product.runner, http_reader=product.read)
    assert result['status'] == 'complete' and result['natural_termination']
    assert len(result['checkpoint_warnings']) == 3
    assert 'interrupt' not in product.calls
