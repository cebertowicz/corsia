"""FIFO admission to one database file's write lock for this process's writers.

SQLite's busy handler is not a queue: a waiter sleeps and polls, so a writer that
arrives later can take the lock first, and a waiter can be passed over until its
``busy_timeout`` expires while no single hold ever came near it. Every outermost
``write_transaction`` therefore takes its database file's ticket before
``BEGIN IMMEDIATE`` and gives it back after ``COMMIT`` or ``ROLLBACK``. The
ticket belongs to the file, not the connection, so pooled and private
connections on one file share it. Waiters are served in arrival order and the
ticket passes straight to the next one, so ``busy_timeout`` is left guarding
only writers outside this process.

This is admission, not retry: a waiter that times out raises the same
``database is locked`` error SQLite would, naming the holder, and nothing runs
again. In-memory and temporary databases have no file, take no ticket, and keep
SQLite's own contention handling.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import sqlite3
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# How long an outermost write waits for its turn before it is refused. The
# event-loop thread keeps today's five-second ceiling because its wait freezes
# every request; a worker thread may wait out a long hold ahead of it instead.
WORKER_WAIT_SECONDS = 30.0
LOOP_WAIT_SECONDS = 5.0
SLOW_WAIT_SECONDS = 1.0

# Weak values: a ticket lives only while a holder or waiter references it, so
# per-document databases opened and closed over a long run leave nothing behind.
_tickets_lock = threading.Lock()
_tickets: weakref.WeakValueDictionary[str, WriteTicket] = weakref.WeakValueDictionary()


ADMISSION_TIMEOUT = "CORSIA_ADMISSION_TIMEOUT"
ADMISSION_NESTED = "CORSIA_ADMISSION_NESTED"


def _synthetic_refusal(message: str, errorname: str) -> sqlite3.OperationalError:
    """A ``database is locked`` error SQLite did not raise, marked so a log reader can tell."""
    error = sqlite3.OperationalError(message)
    error.sqlite_errorname = errorname  # type: ignore[attr-defined]  # the slot a real SQLite error fills; code stays None
    return error


def sqlite_errorname_tag(error: BaseException) -> str:
    """`` [SQLITE_BUSY_SNAPSHOT]`` for a sqlite3 error that carries a name, else ``""``."""
    name = getattr(error, "sqlite_errorname", None) if isinstance(error, sqlite3.Error) else None
    return f" [{name}]" if name else ""


@dataclass(eq=False)
class Admission:
    """One writer's place in a ticket's queue, and then its turn."""

    ticket: WriteTicket
    scope: str
    thread_name: str
    thread_ident: int
    requested_at: float
    acquired_at: float | None = None
    granted: threading.Event = field(default_factory=threading.Event, repr=False)

    def describe(self, now: float) -> str:
        """Name this holder for a refusal or a log line."""
        held = now - self.acquired_at if self.acquired_at is not None else 0.0
        return f"scope={self.scope} thread={self.thread_name} held={held:.3f}s"


class WriteTicket:
    """Strict FIFO admission with immediate hand-off to the next waiter.

    Invariant: a waiter is queued only while someone holds the ticket, so a
    newcomer can never take it ahead of a queued writer.
    """

    def __init__(self, database: str) -> None:
        self.database = database
        self._lock = threading.Lock()
        self._holder: Admission | None = None
        self._waiters: deque[Admission] = deque()

    def acquire(self, scope: str) -> Admission:
        """Wait for this thread's turn, or raise ``database is locked`` naming the holder."""
        thread = threading.current_thread()
        me = Admission(self, scope, thread.name, threading.get_ident(), time.monotonic())
        with self._lock:
            holder = self._holder
            if holder is None:
                self._grant(me)
                return me
            if holder.thread_ident == me.thread_ident:
                raise _synthetic_refusal(
                    f"database is locked: {scope} opened a second outermost write while this "
                    f"thread already holds the write lock ({holder.describe(time.monotonic())}); "
                    "pass that transaction's connection as db= to nest instead",
                    ADMISSION_NESTED,
                )
            self._waiters.append(me)
            queued = len(self._waiters)
        timeout = _wait_bound()
        try:
            me.granted.wait(timeout)
        except BaseException:
            self._withdraw(me)
            raise
        with self._lock:
            if self._holder is me:
                _log_slow_wait(me)
                return me
            self._waiters.remove(me)
            holder = self._holder
        raise _synthetic_refusal(
            f"database is locked: write admission for {scope} waited {timeout:g}s "
            f"(queued behind {queued - 1}) and {_describe(holder)} still holds the write lock",
            ADMISSION_TIMEOUT,
        )

    def release(self, admission: Admission) -> None:
        """Give the ticket to the next waiter, or free it."""
        with self._lock:
            if self._holder is not admission:
                raise RuntimeError(f"write admission released by a non-holder: {admission.scope}")
            self._hand_off()

    def holder_summary(self) -> str | None:
        """Name the current holder and queue length, or None when the ticket is free."""
        with self._lock:
            holder, queued = self._holder, len(self._waiters)
        if holder is None:
            return None
        return f"admission {holder.describe(time.monotonic())} queued={queued}"

    def _grant(self, admission: Admission) -> None:
        admission.acquired_at = time.monotonic()
        self._holder = admission
        admission.granted.set()

    def _hand_off(self) -> None:
        if self._waiters:
            self._grant(self._waiters.popleft())
        else:
            self._holder = None

    def _withdraw(self, admission: Admission) -> None:
        """A waiter is leaving on an exception: give back a turn it was just handed."""
        with self._lock:
            if self._holder is admission:
                self._hand_off()
            elif admission in self._waiters:
                self._waiters.remove(admission)


def ticket_for(path: str) -> WriteTicket | None:
    """The process-wide ticket for the database file at ``path``; None when there is no file."""
    if not path:
        return None
    key = _canonical(path)
    with _tickets_lock:
        ticket = _tickets.get(key)
        if ticket is None:
            ticket = _tickets[key] = WriteTicket(key)
        return ticket


@functools.lru_cache(maxsize=1024)
def _canonical(path: str) -> str:
    """One spelling per file; cached because ``realpath`` costs more than the rest of admission."""
    return os.path.realpath(path)


def ticket_of(connection: sqlite3.Connection) -> WriteTicket | None:
    """The ticket for the file behind ``connection``'s main database, as SQLite reports it."""
    cursor = connection.execute("PRAGMA database_list")
    cursor.row_factory = None  # plain tuples, whatever the connection's row factory
    for _seq, name, path in cursor.fetchall():
        if name == "main":
            return ticket_for(path)
    return None


def admit(connection: sqlite3.Connection, scope: str) -> Admission | None:
    """Take the ticket for ``connection``'s database file, if it has one, before ``BEGIN IMMEDIATE``.

    The caller holds it until its ``COMMIT`` or ``ROLLBACK``, including a failed
    ``BEGIN``, and gives it back with :func:`release` on every exit.
    """
    ticket = ticket_of(connection)
    return ticket.acquire(scope) if ticket is not None else None


def release(admission: Admission | None) -> None:
    """Give back a ticket taken by :func:`admit`; a no-op for a connection without one."""
    if admission is not None:
        admission.ticket.release(admission)


def holders_summary() -> str:
    """Every database ticket currently held in this process, for a lock refusal's log line."""
    with _tickets_lock:
        tickets = list(_tickets.values())
    held = [summary for ticket in tickets if (summary := ticket.holder_summary())]
    return "; ".join(held) or "none"


def _describe(holder: Admission | None) -> str:
    return holder.describe(time.monotonic()) if holder is not None else "no holder"


def _wait_bound() -> float:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return WORKER_WAIT_SECONDS
    return LOOP_WAIT_SECONDS


# Temporary acceptance evidence, with the slow-hold warning and lock-refusal log
# in infra.database: delete in the change that settles Phase B's production
# acceptance (24 h with zero in-process lock refusals after deploy), and no later
# than 2026-10-23.
def _log_slow_wait(admission: Admission) -> None:
    waited = (admission.acquired_at or admission.requested_at) - admission.requested_at
    if waited >= SLOW_WAIT_SECONDS:
        log.warning("SQLite write admission waited: scope=%s waited=%.3fs", admission.scope, waited)
