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


ISOLATED_RESULT_LABEL = "from an isolated worker; contains outside content"


def restrict_agent_read(mode: object, thread_id: str, caller_thread_id: str | None) -> bool:
    """Whether an agent must not read this thread's intermediate content.

    A None caller denotes the person's own API view; tool callers must pass their
    backend-injected thread id (or an empty string if it cannot be resolved).
    Any non-null confinement is treated as restricted if its record is damaged.
    """
    return caller_thread_id is not None and caller_thread_id != thread_id and mode is not None


def worker_error_for_agent(db: sqlite3.Connection, thread_id: str, error: str) -> str:
    """Keep an isolated worker's unclassified failure text out of a wake."""
    row = db.execute("SELECT confinement FROM threads WHERE id=?", (thread_id,)).fetchone()
    if row and restrict_agent_read(row[0], thread_id, ""):
        return "isolated worker turn failed"
    return error


def finished_agent_result(db: sqlite3.Connection, thread_id: str) -> dict | None:
    """Return a completed turn's final text only when its last step is an answer."""
    turn = db.execute(
        "SELECT id, status FROM active_turns WHERE thread_id=? ORDER BY turn_order DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if turn is None or turn[1] != "completed":
        return None
    row = db.execute(
        "SELECT id, role, content, metadata, created_at FROM messages "
        "WHERE thread_id=? AND json_valid(metadata) "
        "AND json_extract(metadata, '$.turn_id')=? AND seq IS NOT NULL "
        "ORDER BY seq DESC, rowid DESC LIMIT 1", (thread_id, turn[0]),
    ).fetchone()
    if row is None or row[1] != "assistant" or not str(row[2] or "").strip():
        return None
    metadata = json.loads(row[3])
    if metadata.get("llm_tool_calls") or metadata.get("tool_calls") or metadata.get("step_type") == "tool_calls":
        return None
    return {"id": row[0], "role": "assistant", "content": str(row[2]), "created_at": row[4]}


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
