import json
import subprocess

import pytest

from advisor.agent_backend import AgentBackendError
from scripts.run_geometry_challenge import AuditedBackend, check_geometry_input


def test_geometry_guard_rejects_saved_physics_and_private_identity():
    model = {"asset_kind": "cad_geometry", "simulation_state": "not_defined", "format": "step", "fingerprint": "fixture"}
    assert check_geometry_input(model, ["private_case"])["physics"] == 0
    with pytest.raises(ValueError, match="simulation information"):
        check_geometry_input(dict(model, physics=[{"type": "SolidMechanics"}]), [])
    with pytest.raises(ValueError, match="identity"):
        check_geometry_input(dict(model, file_name="private_case.step"), ["private_case"])


def test_execution_audit_rejects_extra_tools_and_does_not_log_reasoning(tmp_path, monkeypatch):
    backend = AuditedBackend(tmp_path)
    from scripts import run_geometry_challenge as runner
    def completed(command, **kwargs):
        assert "--json" in command
        return subprocess.CompletedProcess(command, 0, json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "must not be persisted"}})+"\n"+json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "never executed in this test"}}), "")
    monkeypatch.setattr(runner, "run_text", completed)
    with pytest.raises(AgentBackendError, match="audit"):
        backend._run_audited(["codex", "exec"])
    saved = (tmp_path / "stages/execution-audit.json").read_text(encoding="utf-8")
    assert "command_execution" in saved
    assert "must not be persisted" not in saved
    assert "never executed" not in saved
