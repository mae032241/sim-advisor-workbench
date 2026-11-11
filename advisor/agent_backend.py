"""A bounded JSON-only Codex CLI boundary for requirement-driven research.

This reuses the user's existing CLI authentication. It does not read credentials,
load user MCP configuration, or expose raw CLI logs. Model execution is optional
and explicit; failure never becomes a fabricated or rule-generated agent plan.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .config import codex_command
from .process_runner import ProcessOutputLimitError, run_text


class AgentBackendError(RuntimeError):
    pass


def _bounded_seconds(value, default=600, minimum=5, maximum=600):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


class CodexJSONBackend:
    def __init__(self, data_dir: Path, *, runner: Callable | None = None,
                 command: str | None = None, model: str | None = None):
        self.data_dir = Path(data_dir)
        self.command = command or codex_command()
        self.runner = runner or run_text
        self.injected_runner = runner is not None and runner is not run_text
        self.model = model or os.environ.get("SIM_ADVISOR_MODEL", "gpt-6.1-sol")
        self.reasoning_effort = "xhigh"
        self.timeout = _bounded_seconds(os.environ.get("SIM_ADVISOR_AGENT_TIMEOUT", "600"))

    @property
    def engine(self):
        return ("injected-runner" if self.injected_runner else "codex-cli-research") + " / " + self.model + " / xhigh"

    def status(self) -> dict:
        return {"available": bool(self.command), "mode": "injected-runner" if self.injected_runner else "codex-cli-research" if self.command else "unavailable",
                "detail": "需求驱动研究：先由 Agent 提出候选机制和检索问题，控制器读取真实资料，再由 Agent 制定方案；最多一次补充研究。使用既有 Codex 登录，不加载用户 MCP 配置。" if self.command else "未发现 Codex CLI；需求可保存，但不会生成假 Agent 方案。",
                "model": self.model, "reasoning_effort": self.reasoning_effort,
                "max_stage_calls": 3, "per_process_timeout_seconds": self.timeout}

    def generate(self, prompt: str, schema: dict, *, stage: str, timeout: int | None = None) -> dict:
        if not self.command:
            raise AgentBackendError("未发现可用 Codex CLI；请检查本机安装或 SIM_ADVISOR_CODEX。")
        if stage not in {"formulate", "design", "design_followup", "simplification", "library_annotation"}:
            raise ValueError("Unknown research stage")
        runs = self.data_dir / "research-agent-runs"
        runs.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=stage + "-", dir=runs) as temporary:
            directory = Path(temporary)
            schema_file = directory / "schema.json"
            result_file = directory / "result.json"
            schema_file.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            args = [self.command, "exec", "--ephemeral", "--ignore-user-config", "--sandbox", "read-only",
                    "--skip-git-repo-check", "--color", "never", "-m", self.model,
                    "-c", 'model_reasoning_effort="xhigh"', "--output-schema", str(schema_file),
                    "-o", str(result_file), "-"]
            seconds = min(self.timeout, _bounded_seconds(timeout, default=self.timeout))
            try:
                proc = self.runner(args, input_text=prompt, cwd=directory, timeout=seconds, max_output=1024 * 1024)
            except subprocess.TimeoutExpired as exc:
                raise AgentBackendError(f"研究阶段 {stage} 超过 {seconds} 秒限制；未替换原方案。") from exc
            except ProcessOutputLimitError as exc:
                raise AgentBackendError(f"研究阶段 {stage} 的 CLI 输出超过限制；未替换原方案。") from exc
            except OSError as exc:
                raise AgentBackendError("无法启动研究 Agent；请检查本机 Codex CLI。") from exc
            if proc.returncode or not result_file.is_file():
                diagnostic = (proc.stderr or "").lower()
                reason = "认证不可用" if any(word in diagnostic for word in ("unauthorized", "authentication", "401", "login")) else "CLI 未成功返回结构化结果"
                raise AgentBackendError(f"研究阶段 {stage} 未完成：{reason}（退出码 {proc.returncode}）；未替换原方案。")
            if result_file.stat().st_size > 512 * 1024:
                raise AgentBackendError("研究 Agent 结果超过大小限制；未替换原方案。")
            try:
                result = json.loads(result_file.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise AgentBackendError("研究 Agent 未返回有效 JSON；未替换原方案。") from exc
            if not isinstance(result, dict):
                raise AgentBackendError("研究 Agent 结果必须是 JSON 对象；未替换原方案。")
            return result
