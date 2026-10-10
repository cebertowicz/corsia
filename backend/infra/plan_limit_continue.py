"""The one continue input a limit-stopped Turn leaves behind.

Two causes share it: CORSIA's own plan-limit Stop (#2477 amendment 12) and a
vendor's usage-limit refusal. Written inside the terminal transaction, before
the successor claim, so the claim rolls it to a usable account or the held
scope keeps it queued until the limit is lifted.
"""

from __future__ import annotations

import sqlite3

from core.stop_causes import PLAN_LIMIT_REASON, VENDOR_LIMIT_REFUSAL_REASON

#: The transcript chip line for the continue input (approved copy).
CONTINUE_CHIP = "Resumed after plan limit pause"
_CONTENT = (
    "CORSIA paused your previous turn because the plan limit was reached and "
    "spending past it is off. Continue the work that turn was doing."
)
_VENDOR_CONTENT = (
    "CORSIA stopped your previous turn because its account reached a usage limit. "
    "Continue the work that turn was doing."
)
_CONTENT_BY_CAUSE = {PLAN_LIMIT_REASON: _CONTENT, VENDOR_LIMIT_REFUSAL_REASON: _VENDOR_CONTENT}


_CONTINUE_ID_PREFIX = "overage-continue:"


def continue_message_id(turn_id: str) -> str:
    """Stable id of the one continue input written for a limit-stopped turn."""
    return f"{_CONTINUE_ID_PREFIX}{turn_id}"


def _turn_starting_input_queued(db: sqlite3.Connection, thread_id: str) -> bool:
    from infra.thread_input_rows import starts_turn

    rows = db.execute(
        "SELECT role,metadata FROM messages WHERE thread_id=? AND delivery_state='queued' "
        "AND active_turn_id IS NULL AND input_order IS NOT NULL",
        (thread_id,),
    ).fetchall()
    return any(starts_turn(row) for row in rows)


def queue_plan_limit_continue(db: sqlite3.Connection, turn_id: str, thread_id: str) -> None:
    """Queue the continue input for a Turn CORSIA stopped at the plan limit with overage off."""
    _queue_continue(db, turn_id, thread_id, cause=PLAN_LIMIT_REASON)


def _queue_continue(db: sqlite3.Connection, turn_id: str, thread_id: str, *, cause: str) -> None:
    """Queue one system continue input unless real input already restarts the thread.

    A held prefix is delivered as one batch, so a continue input beside real
    input adds nothing. The deterministic id makes a second Stop a duplicate.
    """
    if _turn_starting_input_queued(db, thread_id):
        return
    from infra.input_admission import accept_thread_input

    accept_thread_input(
        thread_id,
        role="system",
        content=_CONTENT_BY_CAUSE[cause],
        intent="apply_now",
        message_id=continue_message_id(turn_id),
        metadata={
            "type": "context_event",
            "event_type": cause,
            "event_disposition": "wake",
            "summary": CONTINUE_CHIP,
            "sender_kind": "system",
            "cause": cause,
            "stopped_turn_id": turn_id,
        },
        delivery_options={"native_mode": "event", "attachments": []},
        db=db,
    )


def _repeats_refusal_on_same_login(db: sqlite3.Connection, turn_id: str) -> bool:
    """True when a continue started this Turn on the very login its stopped Turn was refused on.

    Admission judged that login usable and the vendor refused it again, so a
    further continue would only cycle: the thread stops visibly instead. A
    refusal on a different login (a rollover moved the work) is not a repeat.
    """
    from infra.vendor_login import read_turn_receipt

    row = db.execute(
        "SELECT json_extract(metadata,'$.stopped_turn_id') AS stopped FROM messages "
        "WHERE active_turn_id=? AND id LIKE ?",
        (turn_id, f"{_CONTINUE_ID_PREFIX}%"),
    ).fetchone()
    if row is None or row["stopped"] is None:
        return False
    refused = read_turn_receipt(str(row["stopped"]), db=db)
    return refused is not None and refused == read_turn_receipt(turn_id, db=db)


def queue_limit_refusal_continue(db: sqlite3.Connection, turn_id: str, thread_id: str) -> None:
    """Queue the continue input for a Turn whose account the vendor refused for a usage limit.

    The caller has the account recorded limited, so the successor claim either
    rolls the continue to a usable sibling account or holds it on the limit.
    """
    if _repeats_refusal_on_same_login(db, turn_id):
        return
    _queue_continue(db, turn_id, thread_id, cause=VENDOR_LIMIT_REFUSAL_REASON)
