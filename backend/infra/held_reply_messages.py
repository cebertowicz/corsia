"""What a held reply tells the sending agent, the asking agent and the person.

The sender learns each outcome as a later message, because its send tool returns once. The asker's
transcript gets a note while the reply waits and a receipt when it settles, as silent rows: the
person reads them, and the asker receives the reply itself only on approval.

The sentences an agent reads are fixed here, one per moment; the person's lines carry the same
facts for the transcript.
"""

from __future__ import annotations

import uuid
from typing import Any

from loguru import logger

from core.thread_identity import root_visible_thread_ids
import infra.held_replies as held_replies
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.queries import insert_message
from infra.send_broadcast import broadcast_message_rows

UNTITLED_CHAT = "Untitled chat"
NOTE_TYPE = "held_reply_note"
RECEIPT_PREFIX = "held_reply."
RESTART_ERROR = (
    "Couldn’t confirm the reply was sent because CORSIA restarted. "
    "Approve once to finish; it won’t be sent twice."
)
SCOPE_NOT_SENT = "scope_not_sent"


def held_text(recipient: str) -> str:
    """The sending tool's result while the reply waits."""
    return (
        f"Your reply to {recipient} is waiting for the person's approval, so do not send it "
        "again; stop here until you are told how it ended."
    )


_SENDER_TEXT = {
    held_replies.SENT: "The person approved and your reply to {recipient} was delivered, so there is nothing more to send.",
    held_replies.DECLINED: (
        "The person declined, so your reply to {recipient} was not sent; do not send it again or "
        "send a reworded version unless the person asks you to."
    ),
    held_replies.NOT_SENT: (
        "Your reply to {recipient} was not sent because {recipient} no longer exists, so do not "
        "retry; tell the person if the question still needs an answer."
    ),
    SCOPE_NOT_SENT: (
        "Your reply to {recipient} was not sent because {recipient} is no longer in a scope you "
        "can send to, so do not retry; tell the person if the question still needs an answer."
    ),
}

_ASKER_TEXT = {
    "waiting": (
        "{agent}'s reply is waiting for the person's approval; do not ask again, it will arrive "
        "here if the person approves it."
    ),
    held_replies.SENT: "{agent}'s reply was sent.",
    SCOPE_NOT_SENT: "{agent}'s reply could not be sent, so you will not receive it; continue without it, or tell the person if you can't.",
    held_replies.DECLINED: (
        "The person declined to share {agent}'s reply, so you will not receive it; do not ask "
        "again unless the person asks you to; continue without it, or tell the person if you can't."
    ),
}

# The lines the person reads in a transcript, the receipt slot and the drawer.
_LINE = {
    "waiting": "{agent}'s reply is waiting for approval.",
    held_replies.SENT: "{agent}'s reply was sent.",
    held_replies.DECLINED: "{agent}'s reply was not sent.",
    SCOPE_NOT_SENT: "{agent}'s reply was not sent.",
}


def sender_line(outcome: str, recipient: str) -> str:
    """The person's receipt line in the sender's chat."""
    if outcome == held_replies.SENT:
        return f"The reply to {recipient} was sent."
    if outcome == held_replies.NOT_SENT:
        return gone_line(recipient)
    if outcome == SCOPE_NOT_SENT:
        return scope_line(recipient)
    return f"The reply to {recipient} was not sent."


def gone_line(recipient: str) -> str:
    """The line for a recipient that no longer exists."""
    return f"Couldn’t send the reply because {recipient} no longer exists."


def scope_line(recipient: str) -> str:
    """The line for a recipient no longer reachable from the sender's scope."""
    return f"Couldn’t send the reply because {recipient} is no longer reachable from this chat."


def error_line(recipient: str) -> str:
    """The line when delivery failed for a reason that may pass; Approve once tries again."""
    return (
        f"Couldn’t send the reply to {recipient} because of an unexpected error. "
        "Approve once to try again."
    )


def visible_name(sender_thread_id: str, *, db: Any) -> str:
    """The name a person knows the sender by: its nearest visible chat, which for a worker is its parent."""
    root = root_visible_thread_ids([sender_thread_id], db=db).get(sender_thread_id) or sender_thread_id
    row = db.execute("SELECT title FROM threads WHERE id=?", (root,)).fetchone()
    return (row["title"] if row is not None else None) or UNTITLED_CHAT


def recipient_name(held: dict[str, Any], *, db: Any) -> str:
    """The recipient as the person reads it: its current name, or the one it had at the hold once it is gone."""
    row = db.execute("SELECT title FROM threads WHERE id=?", (held["recipient_thread_id"],)).fetchone()
    return held_replies.recipient_label(row["title"] if row is not None else held.get("recipient_title"))


def notify_changed(user_id: str, sender_thread_id: str, visible_thread_id: str | None = None) -> None:
    """Tell the person's open pages that the held-reply list moved; they re-read it."""
    from infra import state

    threads = [t for t in dict.fromkeys((sender_thread_id, visible_thread_id)) if t]
    state.broadcast_to_user(user_id, {"type": "held_reply_changed", "thread_ids": threads})


def _insert_asker_row(held: dict[str, Any], kind: str, *, recipient: str, agent: str) -> str | None:
    """One silent row in the asker's transcript; its id, or None when the asker is gone."""
    values = {"agent": agent, "recipient": recipient}
    note = kind == "waiting"
    metadata = {
        "type": NOTE_TYPE if note else "context_event",
        "summary": _LINE[kind].format(**values),
        "held_reply_id": held["id"],
    }
    if not note:
        metadata["event_type"] = RECEIPT_PREFIX + (held_replies.NOT_SENT if kind == SCOPE_NOT_SENT else kind)
    with write_transaction(None, scope="held_reply_asker_row") as db:
        if db.execute(
            "SELECT 1 FROM threads WHERE id=?", (held["recipient_thread_id"],),
        ).fetchone() is None:
            return None
        return insert_message(
            held["recipient_thread_id"], "system", _ASKER_TEXT[kind].format(**values), metadata,
            msg_id=str(uuid.uuid4()), db=db,
        )


async def note_asker(held: dict[str, Any], kind: str) -> None:
    """Write one silent row in the asker's transcript; best-effort, the reply's state is already stored."""
    try:
        def names() -> tuple[str, str]:
            db = get_db()
            try:
                return (
                    visible_name(held["sender_thread_id"], db=db),
                    recipient_name(held, db=db),
                )
            finally:
                db.close()

        agent, recipient = await run_in_db(names)
        row_id = await run_in_db(lambda: _insert_asker_row(held, kind, recipient=recipient, agent=agent))
        if row_id:
            await broadcast_message_rows(held["recipient_thread_id"], (row_id,))
    except Exception:
        logger.exception("held reply {}: asker row {} failed", held["id"], kind)


async def tell_sender(held: dict[str, Any], outcome: str, *, recipient: str) -> bool:
    """Deliver the outcome to the sending agent as a later message; best-effort once settled."""
    from events.delivery import deliver_to_thread

    try:
        delivered = await deliver_to_thread(
            held["sender_thread_id"],
            RECEIPT_PREFIX + (held_replies.NOT_SENT if outcome == SCOPE_NOT_SENT else outcome),
            _SENDER_TEXT[outcome].format(recipient=recipient),
            None,
            delivery_id=f"held-reply:{held['id']}:{outcome}",
            chip=sender_line(outcome, recipient),
        )
        if not delivered:
            logger.warning("held reply {}: telling the sender {} failed", held["id"], outcome)
        return delivered
    except Exception:
        logger.exception("held reply {}: telling the sender {} failed", held["id"], outcome)
        return False


async def notify_hold_changed(held: dict[str, Any]) -> None:
    """Refresh the sender and its visible chat after a hold changes."""
    def visible_id() -> str:
        db = get_db()
        try:
            return root_visible_thread_ids([held["sender_thread_id"]], db=db).get(
                held["sender_thread_id"],
            ) or held["sender_thread_id"]
        finally:
            db.close()

    notify_changed(held["user_id"], held["sender_thread_id"], await run_in_db(visible_id))


async def announce_hold(held: dict[str, Any]) -> None:
    """A new hold: tell the asker's transcript and the person's open pages."""
    await note_asker(held, "waiting")
    await notify_hold_changed(held)


async def hold_response(held: dict[str, Any]) -> dict[str, Any]:
    """What the sending tool or route returns for a hold, or for a retry of one already decided."""
    def recipient() -> str:
        db = get_db()
        try:
            return recipient_name(held, db=db)
        finally:
            db.close()

    name = await run_in_db(recipient)
    state = held["state"]
    outcome = held.get("error") if state == held_replies.NOT_SENT and held.get("error") == SCOPE_NOT_SENT else state
    message = _SENDER_TEXT[outcome].format(recipient=name) if outcome in _SENDER_TEXT else held_text(name)
    return {
        "ok": True,
        "status": state if outcome in _SENDER_TEXT else "held",
        "held_reply_id": held["id"],
        "message": message,
    }
