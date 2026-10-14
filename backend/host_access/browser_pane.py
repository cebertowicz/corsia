"""Browser-pane operations delegated through ProcessOwner's public surface."""
from __future__ import annotations

from host_access.processes import PROCESS_OWNER

__all__ = ["open_url", "stream_port"]


async def stream_port(thread_id: str):
    return await PROCESS_OWNER.browser_stream_port(thread_id)


async def open_url(thread_id: str, url: str) -> str:
    return await PROCESS_OWNER.open_browser_url(thread_id, url)
