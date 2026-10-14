"""Prepare a browser session's private stream through its lifecycle owner."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from host_access.browser_session_state import (
    BrowserSessionCleanupError,
    BrowserSessionError,
    BrowserSetupError,
)


@dataclass(frozen=True, slots=True)
class StreamPortResult:
    """The stream preparation outcome, keeping absence distinct from failure."""
    port: int | None = None
    error: str | None = None

    @property
    def no_session(self) -> bool:
        return self.port is None and self.error is None


async def prepare_stream_port(owner: Any, thread_id: str) -> StreamPortResult:
    """Use the owner's admission, native, observation, and command invariants."""
    async with owner._owner_lock:
        runtime = owner._sessions.get(thread_id)
    if runtime is None:
        return StreamPortResult()
    try:
        await owner._pin_native()
        async with runtime.command_lock:
            owner._assert_admitted(runtime, runtime.record.lease_id)
            before = await owner._observe(runtime)
            if runtime.record.binding is not None and not before.alive:
                return StreamPortResult(error="browser daemon is not running")
            if runtime.record.binding is None and not before.artifacts_absent:
                return StreamPortResult(error="browser startup ownership is unresolved")
            # The daemon may already stream (it can enable streaming at launch), and
            # `stream enable` refuses a second enable, so read the status first.
            status = await _stream_status(owner, runtime)
            if status.no_session:
                enabled = await owner._invoke(owner._session_argv(runtime, ("stream", "enable")), timeout=10.0)
                if not _succeeded(enabled):
                    return StreamPortResult(error="browser stream could not be enabled")
                status = await _stream_status(owner, runtime)
                if status.no_session:
                    return StreamPortResult(error="browser stream has no port")
            return status
    except (BrowserSessionError, BrowserSessionCleanupError, BrowserSetupError) as exc:
        return StreamPortResult(error=str(exc))


async def _stream_status(owner: Any, runtime: Any) -> StreamPortResult:
    """Read the live stream port; neither port nor error means streaming is off."""
    result = await owner._invoke(owner._session_argv(runtime, ("stream", "status")), timeout=10.0)
    if not _succeeded(result):
        return StreamPortResult(error="browser stream status is unavailable")
    try:
        port = json.loads(result.stdout).get("data", {}).get("port")
    except (AttributeError, TypeError, ValueError):
        return StreamPortResult(error="browser stream returned invalid status")
    return StreamPortResult(port=port if isinstance(port, int) and port > 0 else None)


def _succeeded(result: Any) -> bool:
    return result.completion == "exited" and not result.timed_out and result.returncode == 0
