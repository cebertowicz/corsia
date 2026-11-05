"""What the supervisor does when its oMLX server's process exits without being asked to.

The ``Life`` of each server is held by ``omlx_supervisor`` in one dict; this module only reads and
writes the one it is handed, and imports nothing from the supervisor (it is passed the start and
log-tail calls it needs). The trigger is the OS's process-exit event, so nothing polls, and a server
that hangs is not this module's concern.

What each event does, by where the server is:

==================  ==========================  ===============================  =================
event               running (watched)           restart in flight                stop asked for
==================  ==========================  ===============================  =================
exit, not clean     restart it (bounded)        same owner recovers            no restart
exit, code 0        logged, not restarted       same owner recovers            no restart
                    (an exit with no readable status is treated as unexpected)
exit, code 0 after  restarted, bound reset      -                                no restart
oMLX's own Restart  (its dashboard asked)
exit, code 0 with   logged and emitted as unknown, -
the log unreadable  not restarted
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
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from infra.product_events import emit_product_event
from llm.providers import omlx_process as process

log = logging.getLogger("llm.providers.omlx.restart")

MAX_RESTARTS = 3
RESTART_WINDOW_S = 600.0
# What oMLX logs when its own Restart is pressed, before it exits 0 for its supervisor to respawn it.
RESTART_REQUESTED = "Server restart requested (supervisor=corsia)"


@dataclass
class Life:
    """What the supervisor tracks about one server beyond its pid record."""

    unwatch: Callable[[], None] | None = None
    stopping: bool = False  # a stop was asked for: the exit it causes is not a crash
    restart: asyncio.Task | None = None
    hold_requests: bool = False  # only an upgrade drains calls before replacing the process
    exit_generation: int = 0  # incremented for every observed exit during an operation
    exit_status: str = ""
    restarts: deque[float] = field(default_factory=deque)  # when each restart in the window began
    requests: set[asyncio.Future[None]] = field(default_factory=set)  # admitted provider calls, not whole Turns

    def watch(self, pid: int, on_exit: Callable[[tuple[str, bool] | None], None]) -> None:
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


@asynccontextmanager
async def request_slot(lives: dict[str, Life], server_id: str | None) -> AsyncIterator[None]:
    """Keep concurrent calls admitted before replacement alive; later calls wait through restoration."""
    life = lives.get(server_id or "")
    while life is not None and life.hold_requests and life.restart is not None:
        task = life.restart
        await asyncio.wait({task})  # caller cancellation must not cancel the restart
        if task.cancelled():
            raise RuntimeError("Couldn’t upgrade the local model server because the upgrade was cancelled. Check the server in Settings and try again.")
        try:
            result = task.result()
        except Exception as exc:
            raise RuntimeError("Couldn’t upgrade the local model server. Check the server in Settings and try again.") from exc
        if isinstance(result, dict) and result.get("reason") != "already_current":
            if not result.get("restarted") or result.get("reason"):
                raise RuntimeError("Couldn’t upgrade the local model server. Check the server in Settings and try again.")
        life = lives.get(server_id or "")
    if life is None:
        yield  # unmanaged and other providers have no supervisor admission
        return
    finished = asyncio.get_running_loop().create_future()
    life.requests.add(finished)  # no await between observing the restart and claiming admission
    try:
        yield
    finally:
        life.requests.remove(finished)
        finished.set_result(None)


async def require_stopped(stop_process: Callable[[], Awaitable[dict[str, Any]]]) -> None:
    """Fail the owning operation if cleanup cannot stop its process."""
    from app_shutdown_join import join_cleanup_owner

    result, cancelled = await join_cleanup_owner(stop_process())
    if isinstance(result, BaseException):
        raise RuntimeError("Local model server cleanup failed") from result
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Local model server cleanup failed: {result}")
    if cancelled is not None:
        raise cancelled


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


def restart_logged_from_now(runtime_dir: Path, server_id: str) -> Callable[[], bool | None]:
    """Whether the server's log gains oMLX's restart line after this call: an earlier process's never counts.

    None when the log can't be read, which is not an answer: the caller says so rather than calling it a plain exit.
    CORSIA launches oMLX with ``OMLX_SUPERVISED=corsia``, so oMLX's own Restart logs that line and exits 0.
    """
    offset = process.log_size(runtime_dir, server_id)
    return lambda: process.logged_since(runtime_dir, server_id, offset, RESTART_REQUESTED)


def emit(server_id: str, action: str, **properties: Any) -> None:
    emit_product_event("model_server", action, properties={"provider": "omlx", "server_id": server_id, **properties})


def on_exit(
    server_id: str, life: Life, pid: int,
    start: Callable[[dict], Awaitable[dict[str, Any]]], tail_log: Callable[[str], str],
    observed: tuple[str, bool] | None = None, restart_requested: Callable[[], bool | None] = lambda: False,
    *, timeout: float | None = None,
) -> None:
    """Restart the server unless CORSIA stopped it, or it exited 0 without oMLX's own Restart asking."""
    from llm.server_observation import forget_loaded_on_exit

    life.unwatch = None
    loaded = sorted(forget_loaded_on_exit(server_id))
    status, clean = observed if observed is not None else process.reap_status(pid)
    if life.stopping:
        log.info("oMLX %s (pid %s) stopped as asked; not restarting", server_id, pid)
        return
    if life.restart is not None and not life.restart.done():
        life.exit_generation += 1
        life.exit_status = status
        return
    if clean:
        asked = restart_requested()
        if asked is None:  # the log is unreadable, so the seat stays down and the reason is said out loud
            log.warning("oMLX %s (pid %s) exited with code 0 and its log could not be read; "
                      "whether oMLX's own Restart asked for it is unknown, so it is not restarted", server_id, pid)
            emit(server_id, "exit.intent_unknown")
            return
        if not asked:
            log.info("oMLX %s (pid %s) exited with code 0 without being asked; not restarting", server_id, pid)
            return
        status = "restart requested in oMLX"  # a person's restart, like a person's start, resets the bound
        life.restarts.clear()
        emit(server_id, "restart.requested", loaded_models=len(loaded))
    else:
        emit(server_id, "exit.unexpected", status=status, loaded_models=len(loaded))
    prune_restart_window(life)
    generation = life.exit_generation
    schedule(server_id, life, recover(server_id, life, status, loaded, generation, start, tail_log, timeout=timeout))


def schedule(server_id: str, life: Life, operation: Coroutine[Any, Any, Any], *, hold_requests: bool = False) -> asyncio.Task:
    """Own one restart through the same Life, so Stop and shutdown can cancel and await it."""
    from infra.state.ws import broadcast_all

    task = asyncio.get_running_loop().create_task(operation)
    life.restart = task
    life.hold_requests = hold_requests
    task.add_done_callback(lambda done: _done(server_id, life, done))
    try:
        broadcast_all({"type": "models_updated", "server_id": server_id})
    except Exception:
        log.warning("oMLX lifecycle notification failed for %s", server_id, exc_info=True)
    return task


def _done(server_id: str, life: Life, task: asyncio.Task) -> None:
    from infra.state.ws import notify_models_changed

    if life.restart is task:
        life.restart = None
        life.hold_requests = False
        notify_models_changed(server_id=server_id)
    error = None if task.cancelled() else task.exception()
    if error is not None:
        log.error("oMLX restart stopped on an error", exc_info=error)
        emit(server_id, "restart.failed", error=type(error).__name__, message=str(error)[:100])


@dataclass
class Recovery:
    """Coroutine-local snapshot and exit baseline, captured before scheduling."""

    models: list[str]
    generation: int
    version: str | None = None

    def exited(self, life: Life) -> bool:
        return life.exit_generation != self.generation


def prune_restart_window(life: Life) -> None:
    while life.restarts and time.monotonic() - life.restarts[0] > RESTART_WINDOW_S:
        life.restarts.popleft()


async def recover(
    server_id: str, life: Life, status: str, loaded: list[str], generation: int,
    start: Callable[[dict], Awaitable[dict[str, Any]]], tail_log: Callable[[str], str],
    *, timeout: float | None = None,
    prepare: Callable[[Recovery], Awaitable[dict[str, Any] | None]] | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """Own preparation, every start and one restoration sequence until completion."""
    from llm.local_keep_loaded import reload_after_restart
    from llm.providers.omlx_lifecycle import read_loaded

    context = Recovery(loaded, generation, version)
    if prepare is not None and not context.exited(life):
        try:
            result = await prepare(context)
        except Exception:
            if not context.exited(life):
                raise
            result = None
        if not context.exited(life) and result is not None:
            return result

    while len(life.restarts) < MAX_RESTARTS and not life.stopping:
        if context.exited(life):
            context.generation = life.exit_generation
            status = life.exit_status
        life.restarts.append(time.monotonic())
        emit(server_id, "restart.attempted", attempt=len(life.restarts), status=status)
        try:
            async with asyncio.timeout(None if timeout is None else timeout * (MAX_RESTARTS + 1)):
                started = await start({"server_id": server_id})
            if context.exited(life):
                continue
            if not started.get("ok"):
                log.warning("oMLX server %s restart failed: %s", server_id, started.get("message"))
                continue
            async with asyncio.timeout(None if timeout is None else timeout * (len(context.models) + 1)):
                await reload_after_restart(server_id, context.models)
            if context.exited(life):
                continue
            async with asyncio.timeout(timeout):
                restored = await read_loaded({"server_id": server_id})
            if context.exited(life):
                continue
        except Exception:
            if context.exited(life):
                continue
            raise
        missing = sorted(set(context.models) - {row["identifier"] for row in restored or []})
        if restored is None or missing:
            missing = missing or context.models
            emit(server_id, "restart.failed", status=status, reason="reload_failed", missing=missing)
            return {"restarted": True, "reason": "reload_failed", "missing": missing}
        properties = {"status": "upgrade", "version": context.version} if context.version else {"attempt": len(life.restarts)}
        emit(server_id, "restart.completed", **properties)
        return {"restarted": True, "loaded": context.models}
    emit(server_id, "restart.gave_up", status=status, restarts=len(life.restarts))
    log.error("oMLX server %s not restarted again: %d restarts in 10 minutes (last exit %s). Last log lines:\n%s",
              server_id, len(life.restarts), status, tail_log(server_id) or "(empty log)")
    return {"restarted": False, "reason": "start_failed"}
