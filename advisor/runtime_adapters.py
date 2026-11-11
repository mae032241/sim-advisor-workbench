"""Replaceable Agent debug backends; planning tools do not depend on Codex.

Factories are keyword-only callables taking cwd, on_event, on_tool, model and
reasoning_effort. Creating a factory/status is separate from connecting it.
Only the Codex factory's connection call reads the existing CLI configuration.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import tomllib
from typing import Any, Callable, Protocol

from .codex_runtime import CodexRuntime
from .config import codex_command


class RuntimeUnavailableError(RuntimeError):
    """A selected debug backend cannot connect; tool services remain independent."""


class Runtime(Protocol):
    backend_id: str

    def start(self, thread_id=None, tools=None, instructions="") -> dict: ...
    def send(self, text: str) -> dict: ...
    def interrupt(self) -> dict: ...
    def close(self) -> None: ...
    def status(self) -> dict: ...


class RuntimeFactory(Protocol):
    backend_id: str

    def __call__(self, *, cwd: Path, on_event: Callable, on_tool: Callable,
                 model: str = "gpt-6.1-sol", reasoning_effort: str = "xhigh") -> Runtime: ...
    def status(self) -> dict: ...


def runtime_command(command: str) -> list[str]:
    """Keep desktop CLI login; disable named user MCP servers for this child.

    Read configuration only when connecting. Never copy auth/configuration into
    a temporary home, and never include configuration values in error messages.
    """
    if not isinstance(command, str) or not command:
        raise RuntimeUnavailableError("未找到 Codex CLI；请配置 SIM_ADVISOR_CODEX。")
    args = [command, "app-server", "--stdio"]
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    path = home / "config.toml"
    if path.is_file():
        try:
            servers = tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers", {})
            if not isinstance(servers, dict):
                raise ValueError("invalid MCP table")
        except (OSError, ValueError):
            raise RuntimeUnavailableError("无法读取 Codex MCP 配置；请检查本机 config.toml。") from None
        for name in servers:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise RuntimeUnavailableError("当前原型只支持名称由字母、数字、下划线或连字符组成的 Codex MCP 配置。")
            # The local CLI treats quoted dotted override segments as literal
            # server names. Bare validated segments preserve the actual names.
            args.extend(["-c", "mcp_servers." + name + ".enabled=false"])
    return args


class CodexDebugRuntime(CodexRuntime):
    backend_id = "codex-app-server"

    def status(self):
        return {**super().status(), "backend_id": self.backend_id}


class CodexRuntimeFactory:
    backend_id = "codex-app-server"

    def status(self):
        available = bool(codex_command())
        return {"available": available, "mode": "app-server-stdio", "backend_id": self.backend_id,
                "detail": "Codex 调试适配器；登录与启动配置在连接时检查。" if available else "未找到 Codex CLI；请配置 SIM_ADVISOR_CODEX。"}

    def __call__(self, *, cwd, on_event, on_tool, model="gpt-6.1-sol", reasoning_effort="xhigh"):
        command = codex_command()
        if not command:
            raise RuntimeUnavailableError("未找到 Codex CLI；请配置 SIM_ADVISOR_CODEX。")
        return CodexDebugRuntime(command=runtime_command(command), cwd=cwd, on_event=on_event, on_tool=on_tool,
                                 model=model, reasoning_effort=reasoning_effort)


class DshRuntimeFactory:
    backend_id = "dsh"

    def status(self):
        try:
            from .dsh_command import dsh_status
            return {**dsh_status(), "backend_id": self.backend_id}
        except ImportError:
            return {"available": False, "mode": "dsh-sdk", "backend_id": self.backend_id,
                    "detail": "DSH 运行时适配器尚不可用。"}

    def __call__(self, *, cwd, on_event, on_tool, model="deepseek-flash", reasoning_effort="high"):
        from .dsh_command import discover_dsh_command
        command = discover_dsh_command()
        if not command:
            raise RuntimeUnavailableError("未找到 DSH CLI；请配置 SIM_ADVISOR_DSH。")
        from .dsh_runtime import DshRuntime
        return DshRuntime(command=command, cwd=cwd, on_event=on_event, on_tool=on_tool,
                          model=model, reasoning_effort=reasoning_effort)


class DisabledRuntimeFactory:
    backend_id = "none"
    _detail = "Agent 调试后端已禁用（SIM_ADVISOR_AGENT_BACKEND=none）；独立工具服务仍可使用。"

    def status(self):
        return {"available": False, "mode": "disabled", "backend_id": self.backend_id, "detail": self._detail}

    def __call__(self, *, cwd, on_event, on_tool, model="gpt-6.1-sol", reasoning_effort="xhigh"):
        raise RuntimeUnavailableError(self._detail)


class _InjectedRuntimeFactory:
    """Give a plain injected callable factory status without a registry."""

    def __init__(self, factory):
        self._factory = factory
        self.backend_id = getattr(factory, "backend_id", "custom")

    def status(self):
        return {"available": True, "mode": "custom", "backend_id": self.backend_id,
                "detail": "使用调用方提供的 Agent 调试适配器；连接能力由该适配器确认。"}

    def __call__(self, *, cwd, on_event, on_tool, model="gpt-6.1-sol", reasoning_effort="xhigh"):
        return self._factory(cwd=cwd, on_event=on_event, on_tool=on_tool,
                             model=model, reasoning_effort=reasoning_effort)


def get_runtime_factory(backend: str | RuntimeFactory | Callable[..., Any] | None = None) -> RuntimeFactory:
    """Select codex/dsh/none, or accept an injected factory without registration.

    Unknown names are errors: selecting a backend never falls back to Codex.
    A factory object already implementing status/backend_id is returned intact;
    plain functions/classes receive a small metadata wrapper.
    """
    if backend is None:
        backend = os.environ.get("SIM_ADVISOR_AGENT_BACKEND", "codex")
    if callable(backend):
        if not isinstance(backend, type) and callable(getattr(backend, "status", None)) and isinstance(getattr(backend, "backend_id", None), str):
            return backend
        return _InjectedRuntimeFactory(backend)
    if not isinstance(backend, str):
        raise TypeError("backend must be codex, dsh, none, or a runtime factory")
    selected = backend.strip().lower()
    if selected == "codex":
        return CodexRuntimeFactory()
    if selected == "dsh":
        return DshRuntimeFactory()
    if selected == "none":
        return DisabledRuntimeFactory()
    raise ValueError("不支持的 Agent 后端；请选择 codex、dsh、none 或传入自定义工厂。")


MANAGED_BACKENDS = {
    "codex": {"label": "Codex", "default_model": "gpt-6.1-sol", "default_effort": "xhigh",
              "effort_options": ["low", "medium", "high", "xhigh"]},
    "dsh": {"label": "DSH", "default_model": "deepseek-flash", "default_effort": "high",
            "effort_options": ["off", "low", "high", "max"]},
}


def managed_backend_key(factory):
    name = getattr(factory, "backend_id", "custom")
    return "codex" if name == "codex-app-server" else name


def managed_defaults(backend):
    info = MANAGED_BACKENDS.get(backend, MANAGED_BACKENDS["codex"])
    model = os.environ.get("SIM_ADVISOR_MODEL", info["default_model"]) if backend != "dsh" else info["default_model"]
    return {"model": model, "reasoning_effort": info["default_effort"]}
