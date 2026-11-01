"""Mark a turn-ending error row `recovered` once the agent starts its next turn.

Focus shows such an error on its own until the agent moves past it: while
nothing runs after it, it ends the work block; once the next turn starts with
nothing the user must read in between, it is a line inside the block. The mark
is one durable write (metadata `recovered: true`), so a Focus page and the client
read the same answer on any page. Two callers share one rule:

* `stamp_at_turn_start` runs inside the claim that starts a turn.
* `stamped_on_arrival` applies it to an error written after its Turn's
  successor already started (a harness writes its error row after the
  terminal commit that claims that successor).
* `backfill` applies the rule to existing rows.

A row the user must read (their message, an update for them, any other notice)
between the error and the next turn's first row keeps the error visible.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from infra.focus_rows import ERROR_SYSTEM_TYPES, FOCUS_VISIBLE_SQL, is_focus_visible

_ERROR_TYPE_LIST = ",".join(f"'{kind}'" for kind in ERROR_SYSTEM_TYPES)
_TYPE = "CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.type') END"
_OPEN_ERROR = (
    f"role='system' AND seq IS NOT NULL AND {_TYPE} IN ({_ERROR_TYPE_LIST}) "
    "AND COALESCE(CASE WHEN json_valid(metadata) THEN json_type(metadata, '$.recovered') END, '') != 'true'"
)
# A later row the user must read. Another open error is not one: it is stamped by the
# same turn start, so it never holds an earlier error visible.
_BLOCKING = f"({FOCUS_VISIBLE_SQL}) = 1 AND NOT (role='system' AND COALESCE({_TYPE}, '') IN ({_ERROR_TYPE_LIST}))"
_STAMP = "metadata=json_set(metadata, '$.recovered', json('true'))"


def stamp_at_turn_start(
    db: sqlite3.Connection, thread_id: str, claimed: Iterable[sqlite3.Row],
) -> list[str]:
    """Stamp the thread's open errors when a turn starts; returns their ids.

    `claimed` are the inputs the turn starts with. If any of them is a row Focus
    shows on its own (a message from the user), the user was addressed after the
    error and nothing is stamped. The caller publishes the stamped rows, so a live
    client reads `recovered` without a reload.
    """
    if any(is_focus_visible(dict(row)) for row in claimed):
        return []
    # An error at or before the newest blocking row can never be stamped, so find that
    # row walking back from the tail and touch only the rows after it. Cost follows the
    # rows since the user was last addressed, not the thread's length.
    newest_blocking = db.execute(
        f"""SELECT seq FROM messages
             WHERE thread_id=? AND seq IS NOT NULL AND ({_BLOCKING})
             ORDER BY seq DESC LIMIT 1""",
        (thread_id,),
    ).fetchone()
    return [str(row[0]) for row in db.execute(
        f"""UPDATE messages SET {_STAMP}
             WHERE thread_id=? AND seq > ? AND {_OPEN_ERROR} RETURNING id""",
        (thread_id, newest_blocking[0] if newest_blocking else 0),
    )]


def stamped_on_arrival(db: sqlite3.Connection, thread_id: str, turn_id: str | None) -> bool:
    """Whether an error of `turn_id` written now is already past: a later Turn started.

    That Turn's claim ran `stamp_at_turn_start` before this row existed, so the
    same rule decides here: the error is past unless that Turn started with a
    row Focus shows on its own.
    """
    if turn_id is None:
        return False
    successor = db.execute(
        """SELECT later.id FROM active_turns later
             JOIN active_turns failed ON failed.id=? AND failed.thread_id=later.thread_id
            WHERE later.thread_id=? AND later.turn_order > failed.turn_order
            ORDER BY later.turn_order LIMIT 1""",
        (turn_id, thread_id),
    ).fetchone()
    if successor is None:
        return False
    claimed = db.execute(
        "SELECT role, content, metadata FROM messages WHERE thread_id=? AND active_turn_id=?",
        (thread_id, successor[0]),
    ).fetchall()
    return not any(
        is_focus_visible({"role": role, "content": content, "metadata": metadata})
        for role, content, metadata in claimed
    )


def backfill(db: sqlite3.Connection) -> int:
    """Stamp every existing open error that the agent moved past; returns how many.

    The agent moved past an error when the next row after it that is either an
    assistant row or one the user must read is an assistant row the user need not
    read. An assistant row shown on its own (an update for the user) is a row to
    read, so it blocks here as it does in `stamp_at_turn_start`.
    """
    return db.execute(
        f"""UPDATE messages SET {_STAMP}
             WHERE {_OPEN_ERROR}
               AND (SELECT following.role='assistant' AND NOT ({_BLOCKING}) FROM messages following
                     WHERE following.thread_id=messages.thread_id AND following.seq > messages.seq
                       AND (following.role='assistant' OR ({_BLOCKING}))
                     ORDER BY following.seq LIMIT 1) = 1"""
    ).rowcount
