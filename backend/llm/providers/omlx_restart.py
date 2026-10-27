"""What the supervisor does when its oMLX server's process exits without being asked to.

The ``Life`` of each server is held by ``omlx_supervisor`` in one dict; this module only reads and
writes the one it is handed, and imports nothing from the supervisor (it is passed the start and
log-tail calls it needs). The trigger is the OS's process-exit event, so nothing polls, and a server
that hangs is not this module's concern.

What each event does, by where the server is:

==================  ==========================  ===============================  =================
event               running (watched)           restart in flight                stop asked for
==================  ==========================  ===============================  =================
exit, not clean     restart it (bounded)        that restart is cancelled and    no restart
                                                a new one begins
exit, code 0        logged, not restarted       -                                no restart
                    (an exit with no readable status is treated as unexpected)
stop (the person,   exit watch dropped, never   restart cancelled and awaited;   -
CORSIA, Quit)       restarted                   what it spawned is then stopped
stop fails          stop flag cleared: a later  -                                -
                    exit is a crash again
start by the        bound reset                 restart carries on, bound kept   flag cleared,
person                                                                           bound reset
backend shutdown    watches dropped             restart cancelled                -
==================  ==========================  ===============================  =================

A stop is announced (``Life.announce_stop``) before any signal is sent, so the exit it causes is
never read as a crash. The pid record is replaced when the new process spawns, with no await between
the spawn and the write; before that no process of this server exists, and a stop reaches a server
whose restart has not spawned yet through its ``Life``. An exit CORSIA was not told about (a Force
Quit, ``kill``) looks like a crash and is restarted; after three restarts in ten minutes it gives up.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from infra.product_events import emit_product_event
from llm.providers import omlx_process as process

log = logging.getLogger("llm.providers.omlx.restart")

MAX_RESTARTS = 3
RESTART_WINDOW_S = 600.0


@dataclass
class Life:
    """What the supervisor tracks about one server beyond its pid record."""

    unwatch: Callable[[], None] | None = None
    stopping: bool = False  # a stop was asked for: the exit it causes is not a crash
    restart: asyncio.Task | None = None
    restarts: deque[float] = field(default_factory=deque)  # when each restart in the window began

    def watch(self, pid: int, on_exit: Callable[[tuple[str, bool]], None]) -> None:
        """The server is up and wanted running: have the OS tell us when ``pid`` exits.

        A start by the person resets the restart bound and any earlier stop; a restart's own start
        keeps both. Where the OS cannot tell us, nothing is ever restarted.
        """
        if self.restart is None:
            self.stopping = False
            self.restarts.clear()
        self.drop_watch()
        self.unwatch = process.watch_exit(pid, on_exit)

    def drop_watch(self) -> None:
        if self.unwatch:
            self.unwatch()
            self.unwatch = None

    def shutdown(self) -> None:
        """The backend is shutting down: an exit from here on is not a crash."""
        self.announce_stop()
        self.drop_watch()

    def announce_stop(self) -> asyncio.Task | None:
        """A stop is about to be asked for: call before any signal. A restart in flight is cancelled and returned."""
        self.stopping = True
        if self.restart:
            self.restart.cancel()
        return self.restart


async def stop(
    lives: dict[str, Life], server_id: str,
    spawned: Callable[[], bool], stop_process: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Stop a server that is running or has a restart in flight, and never restart it for that.

    ``spawned`` says whether a pid record exists. A stop that fails leaves the server running, so a
    later exit is a crash again; one that works forgets the server's ``Life``.
    """
    life = lives.setdefault(server_id, Life())
    task = life.announce_stop()
    if task is not None:
        await asyncio.wait({task})  # it spawned a process only if a pid record now exists
    if task is not None and not spawned():
        result: dict[str, Any] = {"ok": True, "already_stopped": True, "message": "Stopped before the restart finished"}
    else:
        result = await stop_process()
    if result.get("ok"):
        life.drop_watch()
        lives.pop(server_id, None)
    else:
        life.stopping = False
    return result


def restarting(lives: dict[str, Life]) -> set[str]:
    """Servers with a restart in flight: they have no pid record until it has spawned one."""
    return {server_id for server_id, life in lives.items() if life.restart}


def emit(server_id: str, action: str, **properties: Any) -> None:
    emit_product_event("model_server", action, properties={"provider": "omlx", "server_id": server_id, **properties})


def on_exit(
    server_id: str, life: Life, pid: int,
    start: Callable[[dict], Awaitable[dict[str, Any]]], tail_log: Callable[[str], str],
    observed: tuple[str, bool],
) -> None:
    """Restart the server unless the exit was asked for or was a clean exit 0."""
    from llm.server_observation import forget_loaded_on_exit

    life.unwatch = None
    loaded = sorted(forget_loaded_on_exit(server_id))
    status, clean = observed
    if life.stopping:
        log.info("oMLX %s (pid %s) stopped as asked; not restarting", server_id, pid)
        return
    if clean:
        log.info("oMLX %s (pid %s) exited with code 0 without being asked; not restarting", server_id, pid)
        return
    emit(server_id, "exit.unexpected", status=status, loaded_models=len(loaded))
    if life.restart:  # it exited again while a restart was running
        life.restart.cancel()
    task = asyncio.get_running_loop().create_task(_restart(server_id, life, status, loaded, start, tail_log))
    life.restart = task
    task.add_done_callback(lambda done: _done(server_id, life, done))


def _done(server_id: str, life: Life, task: asyncio.Task) -> None:
    if life.restart is task:
        life.restart = None
    error = None if task.cancelled() else task.exception()
    if error is not None:
        log.error("oMLX restart stopped on an error", exc_info=error)
        emit(server_id, "restart.failed", error=type(error).__name__, message=str(error)[:100])


async def _restart(
    server_id: str, life: Life, status: str, loaded: list[str],
    start: Callable[[dict], Awaitable[dict[str, Any]]], tail_log: Callable[[str], str],
) -> None:
    from llm.local_keep_loaded import reload_after_restart

    while life.restarts and time.monotonic() - life.restarts[0] > RESTART_WINDOW_S:
        life.restarts.popleft()
    log.warning("oMLX server %s exited unexpectedly: %s; %d restarts in the last 10 minutes",
                server_id, status, len(life.restarts))
    while len(life.restarts) < MAX_RESTARTS:
        life.restarts.append(time.monotonic())
        emit(server_id, "restart.attempted", attempt=len(life.restarts), status=status)
        result = await start({"server_id": server_id})
        if life.stopping:
            return
        if result.get("ok"):
            emit(server_id, "restart.completed", attempt=len(life.restarts))
            await reload_after_restart(server_id, loaded)
            return
        log.warning("oMLX server %s restart failed: %s", server_id, result.get("message"))
    emit(server_id, "restart.gave_up", status=status, restarts=len(life.restarts))
    log.error("oMLX server %s not restarted again: %d restarts in 10 minutes (last exit %s). Last log lines:\n%s",
              server_id, len(life.restarts), status, tail_log(server_id) or "(empty log)")
