"""Cross-provider-runtime invariants for subprocess lifecycle management.

Shared helpers for cc_runtime, codex_runtime, and any future provider runtime that
holds a live subprocess in a ``_LIVE: dict[str, <Live>]`` registry.

Invariant (conductor-blessed, 2026-06-14):
    Marking a session dead MUST terminate its subprocess — never pop-only.
    An orphaned subprocess is silent resource-death and violates the
    never-silent ethos. Use ``ensure_terminated`` before (or in place of)
    popping the registry entry.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


async def join_task_through_cancellation(
    task: asyncio.Task,
) -> asyncio.CancelledError | None:
    """Let one already-started lifecycle transition finish after caller cancel."""
    cancellation = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
    return cancellation


async def ensure_terminated(session) -> bool:
    """Force-terminate ``session``'s subprocess if it is still running.

    Returns True if ``terminate()`` was called, False if the session was
    already dead or None. Callers are responsible for popping their own
    registry and running provider-runtime cleanup (broadcasts, seed-file
    removal, etc.) — this helper enforces ONLY the subprocess-kill half of
    the invariant so runtimes can compose it with their own finalize logic.

    ``session`` must expose:
    - ``.returncode: int | None`` — None iff the subprocess is running.
    - ``async terminate() -> None`` — sends SIGKILL and awaits exit.
    """
    if session is None:
        return False
    if session.returncode is not None:
        return False
    await session.terminate()
    return True


async def terminate_and_pop(registry: dict, key: str) -> bool:
    """Pop ``key`` from ``registry`` and terminate its session's subprocess.

    Returns True if an entry was found, False if the key was absent.

    Use this when the runtime's finalize logic is a direct pop (no
    additional cleanup needed from the registry entry itself). If your
    finalize runs independently (e.g. via a ``finally: _finalize(...)``
    call), use ``ensure_terminated`` instead to avoid the double-pop
    preventing your finalize's broadcast/cleanup from seeing the entry.

    Both ``cc_runtime`` and ``codex_runtime`` carry their registry as
    ``_LIVE: dict[str, <_Live>]`` where ``_Live.session`` has
    ``returncode`` and ``terminate()``.
    """
    entry = registry.pop(key, None)
    if entry is None:
        return False
    await ensure_terminated(entry.session)
    return True
