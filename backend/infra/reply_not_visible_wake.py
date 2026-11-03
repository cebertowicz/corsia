"""The one Focus nudge a person's message gets when a Turn ends with nothing they can see.

Focus shows an agent's words only when it sends them with ``post_update``. When
a Turn ends while the person's newest message has no such reply after it, the
agent is woken once, for that message, with the text below; it answers with
``post_update`` or with exactly ``NO_REPLY``. If the woken Turn does neither,
its last words become the reply (``for_user``), so the person still sees them.

Both run inside the terminal write (``automation.turn_end_effects``). A person's
Stop records no Turn end, so it never nudges and never shows hidden words; an
error before any nudge is the unanswered-input wake's.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from loguru import logger

from core.message_history import NON_HUMAN_MARKERS, REPLY_VIA_POST_UPDATE_KEY

EVENT = "turn.reply_not_visible"
NO_REPLY = "NO_REPLY"

_HUMAN = (
    f"role='user' AND json_valid(metadata) "
    f"AND json_extract(metadata,'$.{REPLY_VIA_POST_UPDATE_KEY}')=1 "
    + "".join(f"AND json_extract(metadata,'$.{key}') IS NULL " for key in NON_HUMAN_MARKERS)
)
_FOR_USER = "role='assistant' AND json_valid(metadata) AND json_extract(metadata,'$.for_user')=1"
_HIDDEN_TEXT = (
    "role='assistant' AND TRIM(COALESCE(content,''))<>'' "
    "AND COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata,'$.for_user') END,0)=0"
)


def nudge_text(hhmm: str, hidden: str | None) -> str:
    """The approved #1904 wording; the quote sentence only when there are hidden words."""
    seen = f' Your last words, which they did not see: "{hidden}"' if hidden else ""
    return (
        f"No reply is visible to the user after their {hhmm} message.{seen} If you intended to "
        "send a visible response, please use post_update now. If no reply to their last "
        "message was necessary or expected, DO NOT SEND AN UPDATE. "
        f"Instead, answer with exactly {NO_REPLY} and nothing else."
    )


def _clock(created_at: str) -> str:
    """H:MM on the host's clock, 12-hour with no AM/PM."""
    moment = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    local = (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone()
    return f"{local.hour % 12 or 12}:{local.minute:02d}"


def _nudge_id(person_message_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"corsia-reply-not-visible:{person_message_id}"))


def _focus_on(db: sqlite3.Connection) -> bool:
    from infra.settings import get_setting

    # As ``get_feature_flags`` and the settings API read it: Focus is on unless saved off.
    return (get_setting("features", db=db) or {}).get("focus_mode", True) is not False


def _owed_message(db: sqlite3.Connection, thread_id: str) -> sqlite3.Row | None:
    """The person's newest taken-in message when no reply for them follows it."""
    person = db.execute(
        f"SELECT id, seq, created_at, active_turn_id FROM messages WHERE thread_id=? "
        f"AND seq IS NOT NULL AND delivery_state='delivered' AND {_HUMAN} "
        "ORDER BY seq DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if person is None or db.execute(
        f"SELECT 1 FROM messages WHERE thread_id=? AND seq>? AND {_FOR_USER} LIMIT 1",
        (thread_id, person["seq"]),
    ).fetchone() is not None:
        return None
    return person


def _turn_status(db: sqlite3.Connection, turn_id: str | None) -> str | None:
    row = db.execute("SELECT status FROM active_turns WHERE id=?", (turn_id,)).fetchone()
    return None if row is None else str(row["status"])


def _ending_turn(db: sqlite3.Connection, thread_id: str, outcome: dict) -> str | None:
    """The Turn this end records: named by the harness, else the thread's newest."""
    if outcome.get("turn_id"):
        return str(outcome["turn_id"])
    row = db.execute(
        "SELECT id FROM active_turns WHERE thread_id=? ORDER BY turn_order DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    return None if row is None else str(row["id"])


def _last_hidden(db: sqlite3.Connection, thread_id: str, after_seq: int) -> sqlite3.Row | None:
    return db.execute(
        f"SELECT id, content, created_at FROM messages WHERE thread_id=? AND seq>? AND {_HIDDEN_TEXT} "
        "ORDER BY seq DESC LIMIT 1",
        (thread_id, after_seq),
    ).fetchone()


def _show_hidden_words(db: sqlite3.Connection, thread_id: str, row: sqlite3.Row) -> None:
    """Make the nudged Turn's last words its reply, as if it had sent them."""
    from infra import thread_updates
    from infra.send_broadcast import broadcast_input_rows, read_input_broadcast_row
    from infra.write_effects import after_commit

    db.execute(
        "UPDATE messages SET metadata=json_set(COALESCE(metadata,'{}'),'$.for_user',json('true')) "
        "WHERE id=?",
        (row["id"],),
    )
    thread_updates.record_marked(
        db, thread_id=thread_id, message_id=str(row["id"]), node_id=None,
        created_at=str(row["created_at"]),
    )
    projected = read_input_broadcast_row(str(row["id"]), db=db)
    owner = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()

    def publish() -> None:
        if owner is not None:
            thread_updates.notify_changed(str(owner["user_id"]), [thread_id])
        if projected is not None:
            broadcast_input_rows(thread_id, (projected,))

    after_commit(db, publish)


def _settle_nudged_turn(
    db: sqlite3.Connection, thread_id: str, person: sqlite3.Row, ending_turn: str | None,
) -> None:
    """At the nudged Turn's end: NO_REPLY stays hidden; any other last words show."""
    nudge = db.execute(
        "SELECT seq, active_turn_id FROM messages WHERE id=? AND delivery_state='delivered'",
        (_nudge_id(str(person["id"])),),
    ).fetchone()
    if nudge is None or nudge["active_turn_id"] is None or nudge["active_turn_id"] != ending_turn:
        return
    hidden = _last_hidden(db, thread_id, nudge["seq"])
    if hidden is not None and str(hidden["content"]).strip() != NO_REPLY:
        _show_hidden_words(db, thread_id, hidden)


def _queue_nudge(db: sqlite3.Connection, thread_id: str, person: sqlite3.Row) -> bool:
    from events.delivery import enqueue_event_wake
    from infra.send_types import SendRejected

    hidden = _last_hidden(db, thread_id, person["seq"])
    text = nudge_text(
        _clock(str(person["created_at"])),
        str(hidden["content"]).strip() if hidden is not None else None,
    )
    try:
        enqueue_event_wake(
            thread_id, EVENT, text,
            metadata={"kind": "reply_not_visible", "person_message_id": str(person["id"])},
            message_id=_nudge_id(str(person["id"])),
            db=db,
        )
    # A thread being deleted refuses it (SendRejected); the Turn-end record still commits.
    except (ValueError, SendRejected) as error:
        logger.warning("reply-not-visible nudge skipped for {}: {}", thread_id, error)
        return False
    return True


def _settle(db: sqlite3.Connection, thread_id: str, outcome: dict) -> bool:
    """Apply both rules; True when a nudge was queued."""
    if not _focus_on(db):
        return False
    person = _owed_message(db, thread_id)
    if person is None:
        return False
    _settle_nudged_turn(db, thread_id, person, _ending_turn(db, thread_id, outcome))
    if outcome.get("outcome", "completed") != "completed" or _owed_message(db, thread_id) is None:
        return False
    if _turn_status(db, person["active_turn_id"]) == "cancelled":
        return False  # The person stopped the Turn their message started.
    if db.execute(
        "SELECT 1 FROM messages WHERE id=?", (_nudge_id(str(person["id"])),),
    ).fetchone() is not None:
        return False  # Once per message.
    if db.execute(
        f"SELECT 1 FROM messages WHERE thread_id=? AND delivery_state='queued' AND {_HUMAN} LIMIT 1",
        (thread_id,),
    ).fetchone() is not None:
        return False  # Their next message starts the next Turn anyway.
    return _queue_nudge(db, thread_id, person)


def settle_reply_not_visible(
    db: sqlite3.Connection, thread_id: str, outcome: dict, pending,
) -> None:
    """Show a nudged Turn's hidden words, or nudge once for a message nothing visible answered.

    Runs in its own savepoint: a failure here is logged and undone, never the
    Turn end's, except a lock refusal, which the caller retries whole.
    """
    from infra.database import is_lock_refusal, write_transaction

    try:
        with write_transaction(db, scope="reply_not_visible") as connection:
            queued = _settle(connection, thread_id, outcome)
    except Exception as error:
        if is_lock_refusal(error):
            raise
        logger.exception("reply-not-visible settle failed for {}", thread_id)
        return
    if queued:
        pending.drives.append(thread_id)
