"""Browser-pane operations over the process owner's private browser owner."""
from __future__ import annotations

from host_access.processes import PROCESS_OWNER, ProcessOwnerError


def _owner():
    owner = PROCESS_OWNER._browser_owner
    if owner is None:
        raise ProcessOwnerError("browser session owner is unavailable")
    return owner


async def stream_port(thread_id: str) -> int | None:
    return await _owner().stream_port(thread_id)


def has_user_control(thread_id: str) -> bool:
    return _owner().has_user_control(thread_id)


async def set_user_control(thread_id: str, has_control: bool) -> None:
    await _owner().set_user_control(thread_id, has_control)


async def open_url(thread_id: str, url: str) -> str:
    return await _owner().run(thread_id, ("open", url), bypass_user_control=True)
