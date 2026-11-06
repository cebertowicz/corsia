"""Agent comment writes waiting in line for a thread's writer turn, and the wake that serves them.

An agent write on a thread whose turn someone else holds is saved here with its full arguments and
not run. One row per agent per thread while it waits; a second write by the same agent joins its row.
When the turn frees, the oldest row is woken once with what was posted since it was saved, its saved
text, and two answers: post it as is (``doc_comment_update(post_as_is=true)``), or rewrite it with an
ordinary call. The row settles as posted when that agent's next write on the thread is accepted, and
the next row is woken at once. A woken agent whose turn ends without either drops out of line, is
told, and the next row is woken. Nothing here runs on a timer.

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

from doceditor import comments as store, writer_turns
from doceditor.note_names import document_name
from infra.database import write_transaction
from infra.passive_changes import append_thread_context_note
from infra.write_effects import after_commit
from registry import models as registry_models

EVENT = "doc_comment.writer_turn"
#: Items posted since the save that a wake quotes; the rest are counted.
SINCE_LIMIT = 20
_ACTIVE = "state IN ('waiting','woken')"
#: Turn states still able to answer a wake.
_LIVE = "('running','waiting_retry')"


@dataclass(frozen=True)
class Admitted:
    root_id: str
    entry_id: str | None


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
        f"SELECT * FROM doc_comment_writer_line WHERE root_id=? AND {_ACTIVE} ORDER BY saved_at,id", (root_id,),
    ).fetchall()


def count(db, root_id: str) -> int:
    return db.execute(
        f"SELECT COUNT(*) FROM doc_comment_writer_line WHERE root_id=? AND {_ACTIVE}", (root_id,),
    ).fetchone()[0]


def saved_entry(db, root_id: str, thread_id: str) -> sqlite3.Row | None:
    return db.execute(
        f"SELECT * FROM doc_comment_writer_line WHERE root_id=? AND agent_thread_id=? AND {_ACTIVE}",
        (root_id, thread_id),
    ).fetchone()


def _title(db, thread_id: str | None) -> str:
    row = db.execute("SELECT title FROM threads WHERE id=?", (thread_id,)).fetchone() if thread_id else None
    return str(row["title"]) if row is not None and row["title"] else "another agent"


def admit(db, *, root_id: str, artifact_id: str, thread_id: str, user_id: str,
          op: dict[str, Any] | None) -> Admitted | Queued:
    """Let this agent's write take the turn, or save ``op`` in line.

    The turn is free for an agent when no person is on the thread, no other agent is mid-write, and
    the agent is first in line or the line is empty. A woken agent whose write finds the turn taken
    again goes back to waiting in its place; an ordinary call then replaces its saved text, since it
    is that agent's rewrite. ``op`` None is ``post_as_is``: nothing new to save.
    """
    line = active(db, root_id)
    mine = next((row for row in line if row["agent_thread_id"] == thread_id), None)
    person = writer_turns.person_holding(root_id)
    agent = writer_turns.agent_writing(root_id)
    if person is None and agent in (None, thread_id) and (not line or line[0]["agent_thread_id"] == thread_id):
        writer_turns.agent_enter(root_id, thread_id)
        return Admitted(root_id, mine["id"] if mine else None)
    if mine is None:
        db.execute(
            """INSERT INTO doc_comment_writer_line(id,root_id,artifact_id,agent_thread_id,user_id,ops,saved_at)
               VALUES (?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, root_id, artifact_id, thread_id, user_id, json.dumps([op]), _now()),
        )
    else:
        ops = json.loads(mine["ops"])
        if op is not None:
            ops = [op] if mine["state"] == "woken" else [*ops, op]
        db.execute("UPDATE doc_comment_writer_line SET ops=?, state='waiting' WHERE id=?", (json.dumps(ops), mine["id"]))
    line = active(db, root_id)
    position = next(index for index, row in enumerate(line, 1) if row["agent_thread_id"] == thread_id)
    if person is not None:
        holder = person.name
    elif agent is not None and agent != thread_id:
        holder = _title(db, agent)
    else:
        holder = _title(db, line[0]["agent_thread_id"])
    _publish_after_commit(db, root_id, artifact_id)
    return Queued(root_id, position, holder)


def finish(db, *, root_id: str, thread_id: str, entry_id: str | None, posted: bool, as_is: bool = False) -> list[str]:
    """The agent's write call ended: release its turn, settle its saved row, serve the next.

    Returns the agent threads woken, to drive after commit.
    """
    writer_turns.agent_exit(root_id, thread_id)
    if posted and entry_id is not None:
        db.execute(
            f"UPDATE doc_comment_writer_line SET state='posted', settled_at=?, settled_reason=? WHERE id=? AND {_ACTIVE}",
            (_now(), "posted" if as_is else "rewritten", entry_id),
        )
        _publish_after_commit(db, root_id, None)
    return serve(db, root_id)


def serve(db, root_id: str) -> list[str]:
    """Wake the first agent in line once the turn is free and no one in line is already woken."""
    from events.delivery import enqueue_event_wake
    from infra.send_types import SendRejected

    if not writer_turns.is_free(root_id):
        return []
    while True:
        line = active(db, root_id)
        if not line or any(row["state"] == "woken" for row in line):
            return []
        head = line[0]
        if store.get_comment(root_id, db=db) is None:
            _drop(db, head, "the thread was deleted")
            continue
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
        _publish_after_commit(db, root_id, head["artifact_id"])
        return [str(head["agent_thread_id"])]


def settle_turn_end(db, thread_id: str, turn_id: str | None) -> list[str]:
    """Drop this agent's woken rows whose wake ended unanswered, stopped or never written included; serve each next.

    A wake still queued is yet to be read, and one inside another live turn may still be answered there.
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
        if row["delivery_state"] == "queued":
            continue
        if row["active_turn_id"] != turn_id and row["turn_status"] in ("running", "waiting_retry"):
            continue
        _drop(db, row, "your turn ended without posting it as is or rewriting it")
        drives.extend(serve(db, str(row["root_id"])))
    return drives


def recover(db) -> list[str]:
    """After a restart every turn is free: re-wake rows whose wake will not reach a live turn, serve every line.

    A wake still queued is delivered after the restart, and one inside a turn that is still live is
    answered there; every other woken row goes back to waiting in its place and is woken again.
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
    for row in db.execute(f"SELECT DISTINCT root_id FROM doc_comment_writer_line WHERE {_ACTIVE}").fetchall():
        drives.extend(serve(db, str(row["root_id"])))
    return drives


def _drop(db, row, reason: str) -> None:
    db.execute(
        "UPDATE doc_comment_writer_line SET state='dropped', settled_at=?, settled_reason=? WHERE id=?",
        (_now(), reason, row["id"]),
    )
    artifact = registry_models.get_artifact(str(row["artifact_id"]), db=db)
    drafts = "\n".join(describe(op) for op in json.loads(row["ops"]))
    append_thread_context_note(
        db, thread_id=str(row["agent_thread_id"]),
        summary=(f"Your saved write on a comment thread in {document_name(artifact)} dropped out of line and was "
                 f"not posted: {reason}. What you had saved:\n{drafts}"),
        details={"comment_id": row["root_id"], "artifact_id": row["artifact_id"]},
    )
    _publish_after_commit(db, str(row["root_id"]), str(row["artifact_id"]))


def describe(op: dict[str, Any]) -> str:
    """One saved write in the words a wake shows it."""
    kind = op.get("kind")
    if kind == "reply":
        return f'Reply: "{op["input"]["body"]}"'
    if kind == "decide":
        verb = "Accept" if op["decision"] == "accept" else "Reject"
        reason = f' Reason: "{op["reason"]}"' if op.get("reason") else ""
        return f"{verb} the suggestion.{reason}"
    fields = op.get("fields", {})
    parts = []
    if fields.get("resolved") is True:
        parts.append("Resolve the thread.")
    if fields.get("resolved") is False:
        parts.append("Re-open the thread.")
    if fields.get("acked") is False:
        parts.append("Take back your Agree.")
    if "body" in fields:
        parts.append(f'Edit your comment to: "{fields["body"]}"')
    if any(key in fields for key in ("from_off", "to_off", "anchor_text", "region")):
        parts.append("Move your comment.")
    return " ".join(parts) or "Update the comment."


def wake_text(db, row) -> str:
    root_id = str(row["root_id"])
    root = store.get_comment(root_id, db=db) or {}
    artifact = registry_models.get_artifact(str(row["artifact_id"]), db=db)
    where = f'the comment thread on "{root.get("anchor_text")}"' if root.get("anchor_text") else "a comment thread"
    since = db.execute(
        """SELECT author, body, created_at FROM doc_comments WHERE (id=? OR parent_id=?) AND created_at>?
           ORDER BY created_at, id""",
        (root_id, root_id, row["saved_at"]),
    ).fetchall()
    lines = [f"The writer turn is yours on {where} in {document_name(artifact)}."]
    if since:
        lines.append("Posted on the thread since you saved your write:")
        lines += [f'- {item["author"]}: "{item["body"]}"' for item in since[:SINCE_LIMIT]]
        if len(since) > SINCE_LIMIT:
            lines.append(f'- and {len(since) - SINCE_LIMIT} more; read them with doc_comments_list(comment_id="{root_id}").')
    else:
        lines.append("Nothing was posted on the thread since you saved your write.")
    if root.get("decision"):
        lines.append(f"The suggestion is now {root['decision']} by {root.get('decided_by') or 'someone'}.")
    elif root.get("resolved"):
        lines.append("The thread is now resolved.")
    lines.append("Your saved write:")
    lines += [f"- {describe(op)}" for op in json.loads(row["ops"])]
    lines += [
        "Answer one way:",
        f'- Post it as is: doc_comment_update(comment_id="{root_id}", post_as_is=true)',
        "- Or rewrite it: make the call again with what you want to say now.",
        "If this turn ends with neither, your write drops out of line and the next agent goes.",
    ]
    return "\n".join(lines)


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
