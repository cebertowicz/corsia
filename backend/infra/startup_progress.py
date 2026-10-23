"""Startup progress the starting service publishes before it can answer HTTP.

A schema migration runs in the lifespan, before the port binds, and on a large
database it can take minutes. Anything waiting on that startup (the update
shell's health wait, the desktop app's update screen) therefore cannot ask the
service over HTTP whether it is still working. This module is the service's own
answer: one small JSON file, ``<state root>/runtime/startup-progress.json``,
rewritten whole (temporary file, then rename) so a reader never sees half of it.

The object always leads with ``{"schema": 1, "pid": <int>, "seq": <int>, ...``
in that order and with ``json.dumps`` spacing: the update shell reads those
three fields with a bash pattern instead of starting an interpreter every poll.
``seq`` grows with every write, so a reader that sees it change knows the
process named by ``pid`` is still making progress. The other fields:

``phase``
    ``starting`` (process up, opening its data), ``backing_up`` (the
    pre-migration copy), ``migrating``, ``validating`` (post-migration checks),
    ``recovering`` (startup work after the data is open), ``serving`` (startup
    finished; HTTP answers next), or ``failed`` (startup raised; ``error`` says
    why).
``done`` / ``total``
    Counts within the phase, or ``null`` when the phase has none: pages copied
    of the database for ``backing_up``, migration steps finished for
    ``migrating``.
``from_version`` / ``to_version``
    The schema versions a backup or migration moves between, when one runs.
``started_at`` / ``updated_at``
    UTC ISO-8601 times of ``begin`` and of this write.

Heartbeats come from SQLite: every connection opened while startup publishes
(and the migration's own) runs ``sqlite_beat`` from its progress handler, so any
startup step doing database work keeps ``seq`` moving, in every phase. Work that
never touches the database between two marks does not beat.

Writes are no-ops until ``begin`` and after ``finish`` or ``fail``, so code
shared with tools and tests (``migrate_to_current``) publishes nothing outside
the service's own startup. A failed write is logged once and stops publishing;
it never fails startup.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

FILE_NAME = "startup-progress.json"
#: The longest a heartbeat waits before rewriting the file. Readers judge
#: liveness by ``seq`` changing, so this bounds how stale a live reading gets.
HEARTBEAT_SECONDS = 1.0
#: SQLite virtual-machine operations between progress-handler calls. A long
#: statement (a table rebuild, ``quick_check``) on a 4 GiB database measured
#: well under one second between calls at this value.
SQLITE_PROGRESS_OPS = 100_000

_state: dict | None = None
_path: Path | None = None
_last_write = 0.0
# Database threads beat concurrently; one writer at a time owns the file.
_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def path(runtime_dir: Path) -> Path:
    """Where the service under ``runtime_dir`` publishes its startup progress."""
    return Path(runtime_dir) / FILE_NAME


def begin(runtime_dir: Path | None = None) -> None:
    """Start publishing for this process, replacing any earlier process's file."""
    global _state, _path
    if runtime_dir is None:
        from infra.config import RUNTIME_DIR

        runtime_dir = RUNTIME_DIR
    with _lock:
        _path = path(runtime_dir)
        now = _now_iso()
        _state = {
            "schema": 1, "pid": os.getpid(), "seq": 0, "phase": "starting",
            "done": None, "total": None, "from_version": None, "to_version": None,
            "error": None, "started_at": now, "updated_at": now,
        }
        _write()


@contextmanager
def publishing(runtime_dir: Path | None = None) -> Iterator[None]:
    """``begin``, and publish an exception raised inside as ``failed`` with its message."""
    begin(runtime_dir)
    try:
        yield
    except BaseException as error:
        fail(error)
        raise


def watch(conn: sqlite3.Connection) -> None:
    """Let ``conn``'s statements beat while startup publishes. Harmless after it ends."""
    if _state is not None:
        conn.set_progress_handler(sqlite_beat, SQLITE_PROGRESS_OPS)


def mark(phase: str, *, done: int | None = None, total: int | None = None, **detail) -> None:
    """Publish a phase change or a new count now."""
    with _lock:
        if _state is None:
            return
        _state.update(phase=phase, done=done, total=total, **detail)
        _write()


def beat(*, done: int | None = None, total: int | None = None) -> None:
    """Publish that the current phase is still moving, at most once per heartbeat.

    Never waits on another writer: a beat that finds one is already redundant.
    """
    if _state is None or not _lock.acquire(blocking=False):
        return
    try:
        if _state is None:
            return
        if done is not None:
            _state.update(done=done, total=total)
        if time.monotonic() - _last_write >= HEARTBEAT_SECONDS:
            _write()
    finally:
        _lock.release()


def sqlite_beat() -> int:
    """A ``set_progress_handler`` callback: beat, and never interrupt the statement."""
    _beat_inside_sqlite()
    return 0


def backup_step(_status: int, remaining: int, total: int) -> None:
    """A ``Connection.backup`` progress callback: publish pages copied so far."""
    _beat_inside_sqlite(done=total - remaining, total=total)


def _beat_inside_sqlite(*, done: int | None = None, total: int | None = None) -> None:
    # An exception escaping a SQLite callback aborts the statement or backup it
    # reports on, which would fail the migration over a progress file. Stop
    # publishing instead; readers then judge this start by the process alone.
    global _state
    try:
        beat(done=done, total=total)
    except Exception:
        log.exception("Startup progress stopped publishing")
        _state = None


def finish() -> None:
    """Publish that startup finished, then stop publishing."""
    global _state
    with _lock:
        mark("serving")
        _state = None


def fail(error: BaseException | str) -> None:
    """Publish why startup stopped, then stop publishing. No-op once finished."""
    global _state
    with _lock:
        if _state is None:
            return
        mark("failed", error=str(error)[:500] or type(error).__name__)
        _state = None


def _write() -> None:
    global _state, _last_write
    assert _state is not None and _path is not None
    _state["seq"] += 1
    _state["updated_at"] = _now_iso()
    temporary = _path.with_name(f".{FILE_NAME}.{os.getpid()}.tmp")
    try:
        _path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(_state) + "\n")
        os.replace(temporary, _path)
    except OSError as error:
        log.warning("Startup progress stopped publishing: %s", error)
        _state = None
        return
    _last_write = time.monotonic()
