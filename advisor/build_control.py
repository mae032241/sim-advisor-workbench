"""Cooperative cancellation for one build, scoped to its calling context.

The owner creates the marker (possibly from another CLI process). This module
never deletes it or changes job state. New threads must enter build_control()
inside their worker; a caller's ContextVar is not implicitly inherited.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import threading


class BuildCancelled(Exception):
    """The user requested cancellation of this build."""

    def __init__(self):
        super().__init__("Build cancelled by user.")


class BuildControl:
    def __init__(self, cancel_path: Path, event: threading.Event | None = None):
        self.cancel_path = Path(cancel_path)
        self._event = event
        self._observed = threading.Event()

    @property
    def cancelled(self) -> bool:
        if not self._observed.is_set() and ((self._event is not None and self._event.is_set()) or self.cancel_path.exists()):
            self._observed.set()
        return self._observed.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise BuildCancelled()


_CURRENT: ContextVar[BuildControl | None] = ContextVar("sim_advisor_build_control", default=None)


@contextmanager
def build_control(control: BuildControl | None):
    if control is not None and not isinstance(control, BuildControl):
        raise TypeError("control must be a BuildControl or None")
    token = _CURRENT.set(control)
    try:
        yield control
    finally:
        _CURRENT.reset(token)


def current_control() -> BuildControl | None:
    return _CURRENT.get()


def check_cancelled() -> None:
    control = current_control()
    if control is not None:
        control.check()
