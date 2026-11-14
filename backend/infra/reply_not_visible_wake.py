"""The one Focus nudge for an unanswered person message or first-run wake.

Focus shows an agent's words only when it sends them with ``post_update``. When
a Turn ends while the person's newest message has no such reply after it, the agent
is woken once, for that message, with the text below; it answers with
``post_update`` or with exactly ``NO_REPLY``. If the woken Turn does neither,
its last words become the reply (``for_user``), so the person still sees them, and
any comment answers tagged in them are placed in the same write; a
woken Turn that leaves no words of its own (it errored first) shows the words it
was woken about instead.

Before any person message, the delivered first-run event can anchor the same rule.
Both run inside the terminal write (``automation.turn_end_effects``). A person's
Stop records no Turn end, so it never nudges; an error before any nudge is the
unanswered-input wake's. Stopping the woken Turn still shows the words it was
woken about (``show_reply_on_nudge_stop``, inside ``finish_turn``).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from core.message_history import NON_HUMAN_MARKERS, REPLY_VIA_POST_UPDATE_KEY
from infra.chat_comment_answers import (
    VISIBLE_CONTENT, VISIBLE_CONTENT_JOIN, focus_mode_on, place_flipped_answers,
)

EVENT = "turn.reply_not_visible"
NO_REPLY = "NO_REPLY"

#: Newest FIFO inputs searched for the person's message, as ``post_update_reminder``
#: does: every Turn end reads this window through the input-order index, never the
#: whole thread. A message further back has had that many Turn ends to be nudged.
_INPUT_WINDOW = 100

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


def _first_run_nudge_text(hidden: str | None) -> str:
    seen = f' Your last words, which they did not see: "{hidden}"' if hidden else ""
    return (
        "No reply is visible to the user after setup started." + seen +
        " If you intended to send a visible response, please use post_update now. "
        "If no reply was necessary or expected, DO NOT SEND AN UPDATE. "
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
    return focus_mode_on(db)


def _newest_person_input(
    db: sqlite3.Connection, thread_id: str, delivery_state: str,
) -> sqlite3.Row | None:
    return db.execute(
        "SELECT id, seq, created_at, active_turn_id FROM ("
        "SELECT id, seq, created_at, active_turn_id, role, metadata, delivery_state "
        "FROM messages WHERE thread_id=? AND input_order IS NOT NULL "
        f"ORDER BY input_order DESC LIMIT ?) WHERE delivery_state=? AND {_HUMAN} "
        "ORDER BY seq DESC LIMIT 1",
        (thread_id, _INPUT_WINDOW, delivery_state),
    ).fetchone()


def _anchor(db: sqlite3.Connection, thread_id: str, delivery_state: str) -> sqlite3.Row | None:
    person = _newest_person_input(db, thread_id, delivery_state)
    if person is not None or delivery_state != "delivered":
        return person
    from infra.onboarding_events import FIRST_RUN_EVENT, first_run_message_id

    return db.execute(
        "SELECT id, seq, created_at, active_turn_id FROM messages "
        "WHERE id=? AND thread_id=? AND role='system' AND delivery_state=? "
        "AND json_valid(metadata) AND json_extract(metadata,'$.event_type')=?",
        (first_run_message_id(thread_id), thread_id, delivery_state, FIRST_RUN_EVENT),
    ).fetchone()


def _unseen(db: sqlite3.Connection, thread_id: str, row: sqlite3.Row) -> str:
    """The row's words as chat shows them, without the answers placed in comment threads."""
    (visible,) = db.execute(
        f"SELECT {VISIBLE_CONTENT} FROM messages m {VISIBLE_CONTENT_JOIN} WHERE m.id=? AND m.thread_id=?",
        (row["id"], thread_id),
    ).fetchone()
    return str(visible).strip()


def _replied_after(
    db: sqlite3.Connection, thread_id: str, person: sqlite3.Row | dict[str, Any],
) -> bool:
    """A message sent for the person after theirs; a comment answer is one, as it is only placed from one."""
    return db.execute(
        f"SELECT 1 FROM messages WHERE thread_id=? AND seq>? AND {_FOR_USER} LIMIT 1",
        (thread_id, person["seq"]),
    ).fetchone() is not None


def nudge_reply_shown(db: sqlite3.Connection, thread_id: str, turn_id: str) -> bool:
    """True when ``turn_id`` ran a nudge and the message it was about now shows a reply."""
    nudge = db.execute(
        "SELECT json_extract(metadata,'$.person_message_id') FROM messages "
        "WHERE active_turn_id=? AND thread_id=? AND json_valid(metadata) "
        "AND json_extract(metadata,'$.kind')='reply_not_visible' LIMIT 1",
        (turn_id, thread_id),
    ).fetchone()
    if nudge is None or nudge[0] is None:
        return False
    person = db.execute("SELECT id, seq FROM messages WHERE id=?", (nudge[0],)).fetchone()
    if person is None or person[1] is None:
        return False
    return _replied_after(db, thread_id, {"id": person[0], "seq": person[1]})


def _turn_status(db: sqlite3.Connection, turn_id: str | None) -> str | None:
    row = db.execute("SELECT status FROM active_turns WHERE id=?", (turn_id,)).fetchone()
    return None if row is None else str(row["status"])


def _last_hidden(
    db: sqlite3.Connection, thread_id: str, after_seq: int, before_seq: int | None = None,
) -> sqlite3.Row | None:
    before = "" if before_seq is None else "AND seq<? "
    return db.execute(
        f"SELECT id, content, created_at FROM messages WHERE thread_id=? AND seq>? {before}"
        f"AND {_HIDDEN_TEXT} ORDER BY seq DESC LIMIT 1",
        (thread_id, after_seq) if before_seq is None else (thread_id, after_seq, before_seq),
    ).fetchone()


def _woken_about(db: sqlite3.Connection, thread_id: str, person: sqlite3.Row, nudge: sqlite3.Row):
    """The hidden words the nudge was sent about: the last ones between the message and it."""
    return _last_hidden(db, thread_id, person["seq"], nudge["seq"])


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
    place_flipped_answers(db, thread_id, str(row["id"]))
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
    db: sqlite3.Connection, thread_id: str, person: sqlite3.Row, nudge: sqlite3.Row,
) -> None:
    """At the nudged Turn's end: NO_REPLY stays hidden; any other last words show."""
    if _replied_after(db, thread_id, person):
        logger.info("focus.nudge_settled thread={} result=sent", thread_id)
        return
    # A woken Turn that wrote nothing (it errored first) leaves the words it was woken about.
    hidden = _last_hidden(db, thread_id, nudge["seq"]) or _woken_about(db, thread_id, person, nudge)
    if hidden is None or str(hidden["content"]).strip() == NO_REPLY:
        logger.info("focus.nudge_settled thread={} result=no_reply", thread_id)
        return
    _show_hidden_words(db, thread_id, hidden)
    logger.info("focus.nudge_settled thread={} result=shown", thread_id)


def _nudge_of(db: sqlite3.Connection, person: sqlite3.Row) -> sqlite3.Row | None:
    return db.execute(
        "SELECT seq, active_turn_id, delivery_state FROM messages WHERE id=?",
        (_nudge_id(str(person["id"])),),
    ).fetchone()


def _woke(nudge: sqlite3.Row, turn_id: str) -> bool:
    """``turn_id`` is the Turn this nudge woke."""
    return nudge["delivery_state"] == "delivered" and nudge["active_turn_id"] == turn_id


def _nudged_by(db: sqlite3.Connection, thread_id: str, turn_id: str):
    """(person, nudge) when ``turn_id`` is the Turn the newest message's nudge woke."""
    person = _anchor(db, thread_id, "delivered")
    if person is None or person["seq"] is None:
        return None
    nudge = _nudge_of(db, person)
    if nudge is None or not _woke(nudge, turn_id):
        return None
    return person, nudge


def _queue_nudge(db: sqlite3.Connection, thread_id: str, person: sqlite3.Row) -> bool:
    from events.delivery import enqueue_event_wake
    from infra.send_types import SendRejected

    hidden = _last_hidden(db, thread_id, person["seq"])
    from infra.onboarding_events import first_run_message_id

    words = (_unseen(db, thread_id, hidden) or None) if hidden is not None else None
    text = (
        _first_run_nudge_text(words) if person["id"] == first_run_message_id(thread_id)
        else nudge_text(_clock(str(person["created_at"])), words)
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
    logger.info("focus.nudge_queued thread={}", thread_id)
    return True


def _settle(db: sqlite3.Connection, thread_id: str, outcome: dict) -> bool:
    """Apply both rules; True when a nudge was queued."""
    ending_turn = outcome.get("turn_id")
    if not ending_turn or not _focus_on(db):
        return False
    person = _anchor(db, thread_id, "delivered")
    if person is None or person["seq"] is None:
        return False
    nudge = _nudge_of(db, person)
    if nudge is not None:  # Once per message; its own Turn's end settles it.
        if _woke(nudge, ending_turn):
            _settle_nudged_turn(db, thread_id, person, nudge)
        return False
    if outcome.get("outcome", "completed") != "completed":
        return False
    status = _turn_status(db, person["active_turn_id"])
    if status == "cancelled":
        return False  # The person stopped the Turn their message started.
    if status == "running" and person["active_turn_id"] != ending_turn:
        return False  # A later Turn is answering it; that Turn's end decides.
    if _replied_after(db, thread_id, person):
        return False
    if _newest_person_input(db, thread_id, "queued") is not None:
        return False  # Their next message starts the next Turn anyway.
    return _queue_nudge(db, thread_id, person)


def settle_reply_not_visible(
    db: sqlite3.Connection, thread_id: str, outcome: dict, pending,
) -> None:
    """Show a nudged Turn's hidden words, or nudge once for an unanswered input.

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


def show_reply_on_nudge_stop(db: sqlite3.Connection, thread_id: str, turn_id: str) -> None:
    """A nudged Turn that is stopped (the person's Stop, shutdown, a plan limit) still
    shows the words it was woken about.

    The stopped Turn's own words stay as they are; a NO_REPLY among them keeps the
    earlier words hidden too. Runs in its own savepoint inside ``finish_turn``, as
    ``settle_reply_not_visible`` does.
    """
    from infra.database import is_lock_refusal, write_transaction

    try:
        with write_transaction(db, scope="reply_not_visible_stop") as connection:
            nudged = _nudged_by(connection, thread_id, turn_id) if _focus_on(connection) else None
            if nudged is None or _replied_after(connection, thread_id, nudged[0]):
                return
            own = _last_hidden(connection, thread_id, nudged[1]["seq"])
            if own is not None and str(own["content"]).strip() == NO_REPLY:
                return
            hidden = _woken_about(connection, thread_id, *nudged)
            if hidden is None or str(hidden["content"]).strip() == NO_REPLY:
                return
            _show_hidden_words(connection, thread_id, hidden)
            logger.info("focus.nudge_settled thread={} result=shown_on_stop", thread_id)
    except Exception as error:
        if is_lock_refusal(error):
            raise
        logger.exception("reply-not-visible stop settle failed for {}", thread_id)
