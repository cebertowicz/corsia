"""A thread's launch confinement, kept on its own row for every harness.

A confined spawn writes it once, in the transaction that creates the thread, so
no launch of that thread on any harness, by any later model switch, can find it
unconfined. The readers are the tool boundary (``tools.confined_reads``), the
Claude Code launch (``subagents.cc_confinement.launch_confinement``) and the
Codex launch (``subagents.codex_confinement``). A damaged record fails closed:
it could be the one that said ``untrusted``.
"""

from __future__ import annotations

import json
import sqlite3

from subagents.cc_invocation import CONFINEMENT_UNTRUSTED


class ConfinementRecordDamaged(ValueError):
    """A thread's stored confinement cannot be read, so no launch may run it."""


def record(db: sqlite3.Connection, thread_id: str, mode: str, read_dirs: tuple[str, ...]) -> None:
    """Store a new thread's confinement once; a second write is refused."""
    if mode != CONFINEMENT_UNTRUSTED or not read_dirs:
        raise ValueError("only an untrusted confinement with read dirs is stored")
    if db.execute(
        "UPDATE threads SET confinement=?, confined_read_dirs=? WHERE id=? AND confinement IS NULL",
        (mode, json.dumps(list(read_dirs)), thread_id),
    ).rowcount != 1:
        raise ValueError(f"thread {thread_id} is missing or already has a confinement")


def parse(mode: object, raw_dirs: object) -> tuple[str, ...] | None:
    """The confined read dirs a row's two columns hold, ``None`` if unconfined."""
    if mode is None:
        return None
    try:
        dirs = json.loads(raw_dirs) if isinstance(raw_dirs, str) else None
    except ValueError:
        dirs = None
    if mode != CONFINEMENT_UNTRUSTED or not isinstance(dirs, list) or not dirs or any(
        not isinstance(path, str) or not path for path in dirs
    ):
        raise ConfinementRecordDamaged("this thread's confinement record is damaged")
    return tuple(dirs)


def read_dirs(thread_id: str, db: sqlite3.Connection) -> tuple[str, ...] | None:
    """The thread's confined read dirs, ``None`` if it is unconfined (or gone)."""
    row = db.execute(
        "SELECT confinement, confined_read_dirs FROM threads WHERE id=?", (thread_id,),
    ).fetchone()
    return None if row is None else parse(row[0], row[1])
