"""Bound cleanup for the app-server process family owned by one session."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

import psutil


class CodexProcessGroup:
    """A private POSIX group; never signal a reused leader or a released group."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.released = False
        self.killed = False
        try:
            self.created: float | None = psutil.Process(pid).create_time()
        except psutil.NoSuchProcess:
            # A fast launcher can exit before binding; an absent leader is safe,
            # but any later occupant must be rejected because it is unbound.
            self.created = None

    def _assert_binding(self) -> None:
        try:
            leader = psutil.Process(self.pid)
            if self.created is None or leader.create_time() != self.created:
                raise RuntimeError("Codex process group leader identity changed")
            if os.getpgid(self.pid) != self.pid or os.getsid(self.pid) != self.pid:
                raise RuntimeError("Codex process group ownership changed")
        except (psutil.NoSuchProcess, ProcessLookupError):
            # POSIX retains this group ID while its original children remain.
            pass

    def send(self, selected_signal: signal.Signals) -> None:
        # A delivered SIGKILL is final; Darwin denies re-signalling the zombies.
        if self.released or self.killed:
            return
        self._assert_binding()
        try:
            os.killpg(self.pid, selected_signal)
        except ProcessLookupError:
            self.released = True
            return
        self.killed = selected_signal == signal.SIGKILL

    def _alive(self) -> bool:
        if self.released:
            return False
        self._assert_binding()
        # Query only the group we created; protected hosts can deny global
        # process enumeration. Permission denial is not proof of cleanup.
        try:
            os.killpg(self.pid, 0)
            return True
        except PermissionError:
            return True  # Still present or unverifiable; retain the deadline.
        except ProcessLookupError:
            pass
        self.released = True
        return False

    async def wait(self, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            while self._alive():
                await asyncio.sleep(.02)


def signal_process(proc, group: CodexProcessGroup | None, *, force: bool) -> None:
    if group is not None:
        group.send(signal.SIGKILL if force else signal.SIGTERM)
    elif proc is not None and proc.returncode is None:
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
        except ProcessLookupError:
            pass


async def cancel_reader_bounded(session, timeout: float, log: logging.Logger) -> None:
    reader = session._reader
    if reader is None or reader is asyncio.current_task():
        return
    caller = asyncio.current_task()
    if session._tool_requests is not None and caller is not None:
        # Cutover cannot join itself; external Stop still drains all tools.
        session._tool_requests.tasks.discard(caller)
    for _attempt in range(2):
        if reader.done():
            break
        reader.cancel()
        done, _ = await asyncio.wait({reader}, timeout=timeout)
        if done:
            break
    if not reader.done():
        raise RuntimeError("codex app-server reader resisted bounded cancellation")
    try:
        reader.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.warning("codex_app_server: reader stopped with error: %s", exc)
