"""Codex app-server error helpers."""

from __future__ import annotations

import re

_EXPECTED_TURN_MISMATCH_RE = re.compile(
    r"expected active turn id `[^`]+` but found `([^`]+)`"
)


class CodexWriteSurfaceError(RuntimeError):
    """Raised at launch when a task-bearing Codex session cannot write.

    A dispatched write-bearing subagent must fail fast and visibly at spawn if
    its harness has no sanctioned write path, rather than letting the worker
    discover read-only mid-task. Carries the observed launch posture so the
    dispatcher sees exactly what was missing.
    """

    def __init__(self, message: str, payload: dict | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}


class CodexAppServerError(RuntimeError):
    """Raised when the Codex app-server returns a JSON-RPC error."""

    def __init__(self, message: str, payload: dict | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}

    @property
    def is_missing_thread_rollout(self) -> bool:
        """Whether Codex reports that the persisted thread cannot be resumed."""
        message = str(self.payload.get("message") or self).lower()
        return (
            "thread not found" in message
            or "no rollout found" in message
            or "rollout not found" in message
        )

    @property
    def is_active_writer(self) -> bool:
        """Whether Codex refused a new Turn because the thread's writer is still live."""
        message = str(self.payload.get("message") or self).lower()
        return (
            self.payload.get("code") == -32600
            and "already has an active writer" in message
        )

    @property
    def is_not_steerable(self) -> bool:
        data = self.payload.get("data") or {}
        info = data.get("codexErrorInfo") or data.get("codex_error_info")
        return (
            isinstance(info, dict) and "activeTurnNotSteerable" in info
        ) or self.is_stale_active_turn

    @property
    def is_stale_active_turn(self) -> bool:
        return self.current_active_turn_id is not None

    @property
    def current_active_turn_id(self) -> str | None:
        message = str(self.payload.get("message") or self)
        match = _EXPECTED_TURN_MISMATCH_RE.search(message)
        return match.group(1) if match else None
