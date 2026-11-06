"""Agent comment writes waiting in line for a thread's writer turn, and the wake that serves them.

An agent write on a thread whose turn someone else holds is saved here with its full arguments and
not run. One row per agent per thread while it waits; a second write by the same agent joins its row.
When the turn frees, the oldest row is woken once with what was posted since it was saved, its saved
text, and two answers: post it as is (``doc_comment_update(post_as_is=true)``), or rewrite it with an
ordinary call. A row lives only while it waits: ``settle`` deletes it when it posts or drops out of
line, and the next row is woken at once. A rewrite settles in the same transaction as the comment
write that lands it (``landed``), so one never commits without the other. A woken agent whose turn
ends without either drops out of line, is told, and the next row is woken; so does one whose thread
can no longer take input. The continuation sweep serves any line left waiting (``serve_waiting``).

Every function runs inside the caller's write transaction; ``BEGIN IMMEDIATE`` makes a line read
and its write one step.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from doceditor import comments as store, live_events, writer_turns
from doceditor.note_names import document_name
from doceditor.writer_line_text import describe, wake_text
from infra.database import write_transaction
from infra.passive_changes import append_thread_context_note
from infra.write_effects import after_commit
from registry import models as registry_models

__all__ = ["Admitted", "Queued", "describe", "wake_text"]

EVENT = "doc_comment.writer_turn"
#: Turn states still able to answer a wake.
_LIVE = "('running','waiting_retry')"


@dataclass(frozen=True)
class Admitted:
    root_id: str
    ops: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Queued:
    root_id: str
    position: int
    holder: str

    @property
    def text(self) -> str:
        return (f"Saved, {ordinal(self.position)} in line behind {self.holder}. You'll be woken with what changed. "
                "Don't act on this anywhere else until it posts.")


def ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def active(db, root_id: str) -> list[sqlite3.Row]:
    """The thread's line, first in line first."""
    return db.execute(
        "SELECT * FROM doc_comment_writer_line WHERE root_id=? ORDER BY saved_at,id", (root_id,),
    ).fetchall()


def count(db, root_id: str) -> int:
    return counts(db, [root_id]).get(root_id, 0)


def counts(db, root_ids) -> dict[str, int]:
    """How many writes wait on each of these threads, in one grouped read."""
    roots = sorted(set(root_ids))
    if not roots:
        return {}
    rows = db.execute(
        f"""SELECT root_id, COUNT(*) AS waiting FROM doc_comment_writer_line
            WHERE root_id IN ({",".join("?" * len(roots))}) GROUP BY root_id""",
        roots,
    ).fetchall()
    return {str(row["root_id"]): int(row["waiting"]) for row in rows}


def saved_entry(db, root_id: str, thread_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM doc_comment_writer_line WHERE root_id=? AND agent_thread_id=?", (root_id, thread_id),
    ).fetchone()


def _title(db, thread_id: str | None) -> str:
    row = db.execute("SELECT title FROM threads WHERE id=?", (thread_id,)).fetchone() if thread_id else None
    return str(row["title"]) if row is not None and row["title"] else "another agent"


def _claim_turn(line, root_id: str, thread_id: str) -> bool:
    """Take the turn when this agent is first in line or none wait, and no person or other agent holds it."""
    return (not line or line[0]["agent_thread_id"] == thread_id) and writer_turns.agent_claim(root_id, thread_id)


def admit(db, *, root_id: str, artifact_id: str, thread_id: str, user_id: str, op: dict[str, Any]) -> Admitted | Queued:
    """Let this agent's write take the turn, or save ``op`` in line.

    A woken agent whose write finds the turn taken again goes back to waiting in its place; an
    ordinary call then replaces its saved text, since it is that agent's rewrite.
    """
    line = active(db, root_id)
    if _claim_turn(line, root_id, thread_id):
        return Admitted(root_id)
    mine = next((row for row in line if row["agent_thread_id"] == thread_id), None)
    if mine is None:
        line_id = uuid.uuid4().hex
        db.execute(
            """INSERT INTO doc_comment_writer_line(id,root_id,artifact_id,agent_thread_id,user_id,ops,saved_at)
               VALUES (?,?,?,?,?,?,?)""",
            (line_id, root_id, artifact_id, thread_id, user_id, json.dumps([op]), _now()),
        )
        _event(db, "queued", root_id=root_id, artifact_id=artifact_id, line_id=line_id)
    else:
        ops = [op] if mine["state"] == "woken" else [*json.loads(mine["ops"]), op]
        db.execute("UPDATE doc_comment_writer_line SET ops=?, state='waiting' WHERE id=?", (json.dumps(ops), mine["id"]))
    return _queued(db, root_id, artifact_id, thread_id)


def admit_as_is(db, *, root_id: str, thread_id: str) -> Admitted | Queued | None:
    """``post_as_is``: take the turn with this agent's saved writes, read in this transaction.

    None when nothing is saved. Admitted settles the row here, so a second call finds nothing to post.
    """
    mine = saved_entry(db, root_id, thread_id)
    if mine is None:
        return None
    if _claim_turn(active(db, root_id), root_id, thread_id):
        settle(db, mine, "posted_as_is")
        return Admitted(root_id, tuple(json.loads(mine["ops"])))
    db.execute("UPDATE doc_comment_writer_line SET state='waiting' WHERE id=?", (mine["id"],))
    return _queued(db, root_id, str(mine["artifact_id"]), thread_id)


def _queued(db, root_id: str, artifact_id: str, thread_id: str) -> Queued:
    line = active(db, root_id)
    position = next(index for index, row in enumerate(line, 1) if row["agent_thread_id"] == thread_id)
    person, agent = writer_turns.person_holding(root_id), writer_turns.agent_writing(root_id)
    if person is not None:
        holder = person.name
    elif agent is not None and agent != thread_id:
        holder = _title(db, agent)
    else:
        holder = _title(db, line[0]["agent_thread_id"])
    _publish_after_commit(db, root_id, artifact_id)
    return Queued(root_id, position, holder)


def landed(db, root_id: str, thread_id: str | None) -> None:
    """An agent's write on this thread commits in ``db``'s transaction: settle its saved row with it.

    Only a write made holding the turn settles a row; an Agree, which never waits, leaves it in line.
    """
    if not thread_id or writer_turns.agent_writing(root_id) != thread_id:
        return
    mine = saved_entry(db, root_id, thread_id)
    if mine is not None:
        settle(db, mine, "rewritten")


def settle(db, row, outcome: str) -> None:
    """The one end of a saved row: posted as is, rewritten, or dropped. The row is deleted."""
    db.execute("DELETE FROM doc_comment_writer_line WHERE id=?", (row["id"],))
    waited = (datetime.now(timezone.utc) - datetime.fromisoformat(str(row["saved_at"]))).total_seconds()
    _event(db, outcome, root_id=str(row["root_id"]), artifact_id=str(row["artifact_id"]), line_id=str(row["id"]),
           wakes=int(row["wakes"]), waited_seconds=round(waited, 3))
    _publish_after_commit(db, str(row["root_id"]), str(row["artifact_id"]))


def _event(db, outcome: str, **properties) -> None:
    """One product event per line step, once its write commits: identities and counts only."""
    after_commit(db, lambda: live_events.emit("comment_writer_line", outcome=outcome, **properties))


def finish(db, *, root_id: str, thread_id: str) -> list[str]:
    """The agent's write call ended: release its turn and serve the next. Returns the agent threads woken."""
    writer_turns.agent_exit(root_id, thread_id)
    return serve(db, root_id)


def _unable(db, row) -> str | None:
    """Why this row's agent can no longer take its wake, as a send to it would be refused; None when it can."""
    from infra.send_target import model_admission

    thread = db.execute("SELECT model FROM threads WHERE id=?", (row["agent_thread_id"],)).fetchone()
    if thread is None:
        return "your thread was deleted"
    if row["state"] == "woken":
        wake = db.execute("SELECT delivery_state FROM messages WHERE id=?", (row["wake_message_id"],)).fetchone()
        if wake is not None and wake["delivery_state"] in ("failed", "cancelled"):
            return "your thread could not take the wake"
    if thread["model"] and model_admission(thread["model"], db=db, thread_id=str(row["agent_thread_id"])) == "fail":
        return "your thread's model cannot take input"
    return None


def serve(db, root_id: str) -> list[str]:
    """Wake the first agent in line once the turn is free and no one in line is already woken.

    A woken or first row whose agent can no longer take input drops out of line first.
    """
    from events.delivery import enqueue_event_wake
    from infra.send_types import SendRejected

    if not writer_turns.is_free(root_id):
        return []
    while True:
        line = active(db, root_id)
        if not line:
            return []
        woken = next((row for row in line if row["state"] == "woken"), None)
        head = woken or line[0]
        if store.get_comment(root_id, db=db) is None:
            _drop(db, head, "the thread was deleted")
            continue
        if reason := _unable(db, head):
            _drop(db, head, reason)
            continue
        if woken is not None:
            return []
        wakes = head["wakes"] + 1
        message_id = f"writer-line-{head['id']}-{wakes}"
        try:
            with write_transaction(db, scope="writer_line_wake"):
                db.execute(
                    "UPDATE doc_comment_writer_line SET state='woken', wakes=?, wake_message_id=? WHERE id=?",
                    (wakes, message_id, head["id"]),
                )
                enqueue_event_wake(
                    head["agent_thread_id"], EVENT, wake_text(db, head),
                    metadata={"kind": "comment_writer_turn", "comment_id": root_id, "line_id": head["id"]},
                    message_id=message_id, db=db,
                )
        except (ValueError, SendRejected) as error:
            logger.warning("writer line {} could not wake {}: {}", head["id"], head["agent_thread_id"], error)
            _drop(db, head, "your thread could not be woken")
            continue
        _event(db, "woken", root_id=root_id, artifact_id=str(head["artifact_id"]), line_id=str(head["id"]), wakes=wakes)
        _publish_after_commit(db, root_id, head["artifact_id"])
        return [str(head["agent_thread_id"])]


def serve_waiting(db) -> list[str]:
    """The continuation sweep's pass: serve every line with a write waiting, so none waits on a lost serve."""
    drives: list[str] = []
    for row in db.execute("SELECT DISTINCT root_id FROM doc_comment_writer_line WHERE state='waiting'").fetchall():
        root_id = str(row["root_id"])
        drives.extend(_contained(db, root_id, lambda: serve(db, root_id)))
    return drives


def settle_turn_end(db, thread_id: str, turn_id: str | None) -> list[str]:
    """Drop this agent's woken rows whose wake ended unanswered, stopped or never written included; serve each next.

    A wake still queued is yet to be read unless the thread can no longer take it, and one inside
    another live turn may still be answered there. Each row's step is contained, so a failure here
    never rolls back the turn's own terminal settlement; that row is re-woken by restart recovery.
    """
    drives: list[str] = []
    rows = db.execute(
        """SELECT line.*, m.delivery_state, m.active_turn_id, t.status AS turn_status
           FROM doc_comment_writer_line line
           LEFT JOIN messages m ON m.id=line.wake_message_id
           LEFT JOIN active_turns t ON t.id=m.active_turn_id
           WHERE line.agent_thread_id=? AND line.state='woken'""",
        (thread_id,),
    ).fetchall()
    for row in rows:
        drives.extend(_contained(db, str(row["root_id"]), lambda row=row: _settle_ended(db, row, turn_id)))
    return drives


def _settle_ended(db, row, turn_id: str | None) -> list[str]:
    if row["delivery_state"] == "queued":
        if reason := _unable(db, row):
            _drop(db, row, reason)
            return serve(db, str(row["root_id"]))
        return []
    if row["active_turn_id"] != turn_id and row["turn_status"] in ("running", "waiting_retry"):
        return []
    _drop(db, row, "your turn ended without posting it as is or rewriting it")
    return serve(db, str(row["root_id"]))


def _contained(db, root_id: str, step) -> list[str]:
    """Run one line's step in its own savepoint: a failure is logged and undone, never raised into the caller's work."""
    try:
        with write_transaction(db, scope="writer_line_step"):
            return step()
    except Exception:
        logger.exception("comment writer line on {} failed; left as it was", root_id)
        return []


def recover(db) -> list[str]:
    """After a restart every turn is free: re-wake rows whose wake will not reach a live turn, serve every line.

    A wake still queued is delivered after the restart, and one inside a turn that is still live is
    answered there; every other woken row goes back to waiting in its place and is woken again. A row
    whose agent can no longer take input drops out of line when its line is served.
    """
    db.execute(
        f"""UPDATE doc_comment_writer_line SET state='waiting'
           WHERE state='woken' AND NOT EXISTS (
               SELECT 1 FROM messages m WHERE m.id=doc_comment_writer_line.wake_message_id
               AND (m.delivery_state='queued'
                    OR (m.delivery_state='delivered' AND EXISTS (
                        SELECT 1 FROM active_turns t WHERE t.id=m.active_turn_id AND t.status IN {_LIVE}))))""",
    )
    drives: list[str] = []
    for row in db.execute("SELECT DISTINCT root_id FROM doc_comment_writer_line").fetchall():
        drives.extend(serve(db, str(row["root_id"])))
    return drives


def _drop(db, row, reason: str) -> None:
    """Drop a row out of line and tell its agent what it had saved; a deleted agent has no one to tell."""
    if db.execute("SELECT 1 FROM threads WHERE id=?", (row["agent_thread_id"],)).fetchone() is not None:
        artifact = registry_models.get_artifact(str(row["artifact_id"]), db=db)
        drafts = "\n".join(describe(op) for op in json.loads(row["ops"]))
        append_thread_context_note(
            db, thread_id=str(row["agent_thread_id"]),
            summary=(f"Your saved write on a comment thread in {document_name(artifact)} dropped out of line and was "
                     f"not posted: {reason}. What you had saved:\n{drafts}"),
            details={"comment_id": row["root_id"], "artifact_id": row["artifact_id"]},
        )
    settle(db, row, "dropped")


def _publish_after_commit(db, root_id: str, artifact_id: str | None) -> None:
    """Tell the people on the thread how many agents wait, once this write commits."""
    after_commit(db, lambda: publish(root_id, artifact_id))


def publish(root_id: str, artifact_id: str | None, *, user_ids: set[str] | None = None) -> None:
    from infra import state
    from infra.database import get_db

    users = user_ids if user_ids is not None else writer_turns.people_on(root_id)
    if not users:
        return
    db = get_db()
    try:
        waiting = count(db, root_id)
        if artifact_id is None:
            row = db.execute("SELECT artifact_id FROM doc_comments WHERE id=?", (root_id,)).fetchone()
            artifact_id = row["artifact_id"] if row else None
    finally:
        db.close()
    for user_id in users:
        state.broadcast_to_user(user_id, {
            "type": "comment_line", "artifact_id": artifact_id, "comment_id": root_id, "waiting": waiting,
        })
