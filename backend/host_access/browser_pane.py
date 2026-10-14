"""Browser-pane operations over the process owner's private browser owner."""
from __future__ import annotations

from host_access.browser_control import has_user_control, set_user_control
from host_access.processes import PROCESS_OWNER, ProcessOwnerError

__all__ = ["has_user_control", "open_url", "set_user_control", "stream_port"]


def _owner():
    owner = PROCESS_OWNER._browser_owner
    if owner is None:
        raise ProcessOwnerError("browser session owner is unavailable")
    return owner


async def stream_port(thread_id: str) -> int | None:
    return await _owner().stream_port(thread_id)


async def open_url(thread_id: str, url: str) -> str:
    return await _owner().run(thread_id, ("open", url), bypass_user_control=True)
