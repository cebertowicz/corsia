"""Keep SQLite write transactions off the asyncio event-loop thread."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading

log = logging.getLogger(__name__)

LOOP_WRITE_STRICT_ENV = "CORSIA_DB_LOOP_WRITE_STRICT"
_loop_write_sites_lock = threading.Lock()
_loop_write_sites: set[tuple[str, int, str]] = set()  # (file, line, function) already logged


class LoopThreadWriteError(RuntimeError):
    """A write transaction was opened on a thread that is running an asyncio event loop."""


def _loop_write_caller(frame) -> tuple[tuple[str, int, str], str]:
    """The first caller outside this module and contextlib, plus a short chain naming who called it."""
    chain = []
    while frame is not None:
        filename = frame.f_code.co_filename
        if not filename.endswith(("/infra/db_loop_guard.py", "/infra/database.py", "/contextlib.py")):
            chain.append((filename, frame.f_lineno, frame.f_code.co_name))
        frame = frame.f_back
    if not chain:
        return ("<unknown>", 0, "<unknown>"), "<unknown>"
    rendered = " <- ".join(
        f"{name.split('/backend/', 1)[-1]}:{line} {func}" for name, line, func in chain[:8]
    )
    return chain[0], rendered


def guard_write_off_event_loop(scope: str) -> None:
    """Refuse (strict mode) or report (otherwise) an outermost write on an event-loop thread.

    ``BEGIN IMMEDIATE`` waits up to ``busy_timeout`` for another writer, and on the loop
    thread that wait freezes every request. Log only: never retry, recover or suppress.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    site, chain = _loop_write_caller(sys._getframe(1))
    if os.environ.get(LOOP_WRITE_STRICT_ENV) == "1":
        raise LoopThreadWriteError(
            f"write transaction {scope} opened on the event-loop thread: {chain}"
        )
    with _loop_write_sites_lock:
        if site in _loop_write_sites:
            return
        _loop_write_sites.add(site)
    log.error("SQLite write transaction on the event-loop thread: scope=%s caller=%s", scope, chain)
