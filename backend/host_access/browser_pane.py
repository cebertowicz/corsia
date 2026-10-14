"""Browser-pane operations on the composed browser session owner."""
from __future__ import annotations

from host_access.browser_streaming import StreamPortResult
from host_access.processes import PROCESS_OWNER

__all__ = ["open_url", "stream_port"]


async def stream_port(thread_id: str) -> StreamPortResult:
    return await PROCESS_OWNER.browser_sessions().stream_port(thread_id)


async def open_url(thread_id: str, url: str, connection_token: str | None = None) -> str:
    """Open a pane URL: the holding stream navigates; without one, only a new session starts."""
    return await PROCESS_OWNER.browser_sessions().run(
        thread_id, ("open", url), control_token=connection_token, new_session_only=connection_token is None,
    )
