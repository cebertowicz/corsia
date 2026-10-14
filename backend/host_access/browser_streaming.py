"""Turn agent-browser's stream commands into a pane stream port."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence

from host_access.browser_cli import CommandResult

# Runs one command through the session owner's command path, reporting whether it
# left the session unfinished (the owner has then already reset it).
RunCommand = Callable[[Sequence[str]], Awaitable[tuple[CommandResult, bool]]]


@dataclass(frozen=True, slots=True)
class StreamPortResult:
    """The stream preparation outcome, keeping absence distinct from failure."""
    port: int | None = None
    error: str | None = None

    @property
    def no_session(self) -> bool:
        return self.port is None and self.error is None


async def prepare_stream_port(run: RunCommand) -> StreamPortResult:
    """Reuse the daemon's stream, enabling it only when it is off."""
    # The daemon may already stream (it can enable streaming at launch), and
    # `stream enable` refuses a second enable, so read the status first.
    status = await _stream_status(run)
    if status.no_session:
        enabled, unfinished = await run(("stream", "enable"))
        if unfinished or not _succeeded(enabled):
            return StreamPortResult(error="browser stream could not be enabled")
        status = await _stream_status(run)
        if status.no_session:
            return StreamPortResult(error="browser stream has no port")
    return status


async def _stream_status(run: RunCommand) -> StreamPortResult:
    """Read the live stream port; neither port nor error means streaming is off."""
    result, unfinished = await run(("stream", "status"))
    if unfinished or not _succeeded(result):
        return StreamPortResult(error="browser stream status is unavailable")
    try:
        port = json.loads(result.stdout).get("data", {}).get("port")
    except (AttributeError, TypeError, ValueError):
        return StreamPortResult(error="browser stream returned invalid status")
    return StreamPortResult(port=port if isinstance(port, int) and port > 0 else None)


def _succeeded(result: CommandResult) -> bool:
    return result.completion == "exited" and not result.timed_out and result.returncode == 0
