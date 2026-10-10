"""Optional stage observer for ``ProcessOwner.spawn``.

A caller sets ``SPAWN_STAGE_HOOK`` around one spawn to learn when the owner
lock is held (``lock_wait``) and when the process exists (``exec``). It is
timing-only: the hook must not raise, and with no hook set ``stage`` is a
single context-variable read.
"""

from __future__ import annotations

from contextlib import suppress
from contextvars import ContextVar
from typing import Callable

SPAWN_STAGE_HOOK: ContextVar[Callable[[str], None] | None] = ContextVar(
    "process_owner_spawn_stage_hook", default=None
)


def stage(name: str) -> None:
    """Report a spawn stage to the current hook, if any."""
    hook = SPAWN_STAGE_HOOK.get()
    if hook is not None:
        # A timing observer cannot abort a real process launch.
        with suppress(Exception):
            hook(name)
