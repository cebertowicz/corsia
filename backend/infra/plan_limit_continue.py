"""The one continue input a limit-stopped or size-refused Turn leaves behind.

Three causes share it: CORSIA's own plan-limit Stop (#2477 amendment 12), a
vendor's usage-limit refusal, and a harness provider refusing the Turn for size.
Written inside the terminal transaction, before the successor claim, so the
claim rolls it to a usable account or the held scope keeps it queued until the
limit is lifted; a size refusal's successor starts in a fresh session whose
boundary folds what no longer fits.
"""

from __future__ import annotations

import sqlite3

from core.stop_causes import (
    CLAUDE_LOGIN_RACE_REASON, CONTEXT_OVERFLOW_REASON, PLAN_LIMIT_REASON, VENDOR_LIMIT_REFUSAL_REASON,
    ClaudeLoginRaceEnd, ContextOverflowEnd, PlanLimitStop,
)

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
_OVERFLOW_CONTENT = (
    "The provider refused your previous turn because the chat no longer fit its "
    "context window. Continue the work that turn was doing."
)
_LOGIN_RACE_CONTENT = (
    "Your previous turn ended when Claude Code briefly reported its login as failed; "
    "the login still works. Continue the work that turn was doing."
)
_CONTENT_BY_CAUSE = {
    PLAN_LIMIT_REASON: _CONTENT,
    VENDOR_LIMIT_REFUSAL_REASON: _VENDOR_CONTENT,
    CONTEXT_OVERFLOW_REASON: _OVERFLOW_CONTENT,
    CLAUDE_LOGIN_RACE_REASON: _LOGIN_RACE_CONTENT,
}


_CONTINUE_ID_PREFIX = "overage-continue:"
_OVERFLOW_ID_PREFIX = "overflow-continue:"
_LOGIN_RACE_ID_PREFIX = "login-race-continue:"
_UNANSWERED_ID_PREFIX = "overflow-unanswered:"


def continue_message_id(turn_id: str) -> str:
    """Stable id of the one continue input written for a limit-stopped turn."""
    return f"{_CONTINUE_ID_PREFIX}{turn_id}"


def overflow_continue_message_id(turn_id: str) -> str:
    """Stable id of the one continue input written for a size-refused turn."""
    return f"{_OVERFLOW_ID_PREFIX}{turn_id}"


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
    _queue_continue(db, turn_id, thread_id, cause=PLAN_LIMIT_REASON, summary=CONTINUE_CHIP)


def _queue_continue(
    db: sqlite3.Connection, turn_id: str, thread_id: str, *, cause: str, summary: str | None,
    message_id: str | None = None,
) -> None:
    """Queue one system continue input unless real input already restarts the thread.

    A held prefix is delivered as one batch, so a continue input beside real
    input adds nothing. The deterministic id makes a second Stop a duplicate.
    """
    if _turn_starting_input_queued(db, thread_id):
        return
    from infra.input_admission import accept_thread_input

    metadata = {
        "type": "context_event",
        "event_type": cause,
        "event_disposition": "wake",
        "sender_kind": "system",
        "cause": cause,
        "stopped_turn_id": turn_id,
    }
    if summary is not None:
        metadata["summary"] = summary
    accept_thread_input(
        thread_id,
        role="system",
        content=_CONTENT_BY_CAUSE[cause],
        intent="apply_now",
        message_id=message_id or continue_message_id(turn_id),
        metadata=metadata,
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


def _refusal_chain_reached_account_cap(db: sqlite3.Connection, turn_id: str) -> bool:
    """Bound one uninterrupted refusal chain to one continue per account of its provider.

    The chain counts the original Turn, so N accounts allow N continues: the
    last one is held when every account is limited and resumes at the reset.
    """
    from connections.account_policy import thread_scope
    from connections.account_rollover import rollover_order
    from connections.provider_policy import effective_policy
    from connections.vendor_accounts import get_account
    from infra.vendor_login import read_turn_receipt

    receipt = read_turn_receipt(turn_id, db=db)
    if receipt is None:
        return True
    provider = str(get_account(receipt.account_id, db=db)["provider"])
    turn = db.execute("SELECT thread_id FROM active_turns WHERE id=?", (turn_id,)).fetchone()
    if turn is None:
        return True
    project = thread_scope(str(turn["thread_id"]), db=db)["project_id"]
    accounts = rollover_order(provider, project, effective_policy(project, provider, db=db), db=db)
    if accounts is None:
        return False
    cap = len(accounts)
    previous = turn_id
    seen: set[str] = set()
    count = 0
    while previous not in seen:
        seen.add(previous)
        count += 1
        row = db.execute(
            "SELECT json_extract(metadata,'$.stopped_turn_id') AS stopped FROM messages "
            "WHERE active_turn_id=? AND id LIKE ?",
            (previous, f"{_CONTINUE_ID_PREFIX}%"),
        ).fetchone()
        if row is None or row["stopped"] is None:
            break
        previous = str(row["stopped"])
    return count > cap


def queue_limit_refusal_continue(db: sqlite3.Connection, turn_id: str, thread_id: str) -> None:
    """Queue the continue input for a Turn whose account the vendor refused for a usage limit.

    The caller has the account recorded limited, so the successor claim either
    rolls the continue to a usable sibling account or holds it on the limit.
    """
    if _repeats_refusal_on_same_login(db, turn_id) or _refusal_chain_reached_account_cap(db, turn_id):
        return
    _queue_continue(db, turn_id, thread_id, cause=VENDOR_LIMIT_REFUSAL_REASON, summary=CONTINUE_CHIP)


def overflow_rerun_queued(turn_id: str, *, db: sqlite3.Connection | None = None) -> bool:
    """True when the size-refused Turn left its re-run: its notice then waits on that run."""
    from infra.database import get_db

    connection = db or get_db()
    return connection.execute(
        "SELECT 1 FROM messages WHERE id=?", (overflow_continue_message_id(turn_id),),
    ).fetchone() is not None


def queue_overflow_continue(db: sqlite3.Connection, turn_id: str, thread_id: str) -> None:
    """Queue the one re-run of a Turn its provider refused for size; never for a re-run.

    A re-run the provider refuses again ends visibly: the fold did not make room,
    so another run would only repeat the refusal.
    """
    rerun = db.execute(
        "SELECT 1 FROM messages WHERE active_turn_id=? AND id LIKE ?",
        (turn_id, f"{_OVERFLOW_ID_PREFIX}%"),
    ).fetchone()
    if rerun is not None:
        return
    _queue_continue(
        db, turn_id, thread_id, cause=CONTEXT_OVERFLOW_REASON,
        message_id=overflow_continue_message_id(turn_id), summary=None,
    )


def queue_login_race_continue(db: sqlite3.Connection, turn_id: str, thread_id: str) -> None:
    """Queue one login-race re-run, never for a Turn that is itself a re-run."""
    rerun = db.execute(
        "SELECT 1 FROM messages WHERE active_turn_id=? AND id LIKE ?",
        (turn_id, f"{_LOGIN_RACE_ID_PREFIX}%"),
    ).fetchone()
    if rerun is None:
        _queue_continue(
            db, turn_id, thread_id, cause=CLAUDE_LOGIN_RACE_REASON,
            message_id=f"{_LOGIN_RACE_ID_PREFIX}{turn_id}", summary=None,
        )


def record_unanswered_overflow(db: sqlite3.Connection, message_id: str) -> None:
    """Write the size-refused Turn's notice once its re-run input ended failed.

    The refused Turn wrote no notice because its re-run would answer; a re-run
    refused before it ran answers nothing, so the notice the Turn held back is
    written now, in the settlement's transaction. The continue input itself
    stays undrawn.
    """
    if not message_id.startswith(_OVERFLOW_ID_PREFIX):
        return
    turn_id = message_id[len(_OVERFLOW_ID_PREFIX):]
    notice_id = f"{_UNANSWERED_ID_PREFIX}{turn_id}"
    if db.execute("SELECT 1 FROM messages WHERE id=?", (notice_id,)).fetchone():
        return
    turn = db.execute(
        "SELECT thread_id,model,error FROM active_turns WHERE id=?", (turn_id,),
    ).fetchone()
    if turn is None or not turn["error"]:
        # Raising here would undo the settlement and leave the input queued.
        from loguru import logger

        logger.error("size-refused turn {} has no recorded error; no notice written", turn_id)
        return
    from infra.queries import insert_message
    from infra.turn_errors import insert_turn_error
    from llm.error_cause import cause_kind

    metadata = {
        "type": "llm_error", "model": turn["model"],
        "cause": "context_window_exceeded", "cause_kind": cause_kind("context_window_exceeded"),
    }
    insert_turn_error(
        insert_message, str(turn["thread_id"]), str(turn["error"]), metadata,
        turn_id=turn_id, msg_id=notice_id, db=db,
    )


def unanswered_overflow_notices(message_ids: tuple[str, ...]) -> list[tuple[str, str]]:
    """(thread, notice) for each failed re-run in ``message_ids`` whose notice was written."""
    from infra.database import get_db

    found: list[tuple[str, str]] = []
    for message_id in message_ids:
        if not message_id.startswith(_OVERFLOW_ID_PREFIX):
            continue
        notice_id = f"{_UNANSWERED_ID_PREFIX}{message_id[len(_OVERFLOW_ID_PREFIX):]}"
        row = get_db().execute("SELECT thread_id FROM messages WHERE id=?", (notice_id,)).fetchone()
        if row is not None:
            found.append((str(row["thread_id"]), notice_id))
    return found


def queue_terminal_continue(
    db: sqlite3.Connection, turn_id: str, thread_id: str, *,
    outcome: str, error: object, limit_recorded: bool,
) -> None:
    """Queue the continue input a Turn's terminal cause leaves behind, if it leaves one."""
    if outcome == "cancelled" and isinstance(error, PlanLimitStop):
        queue_plan_limit_continue(db, turn_id, thread_id)
    elif outcome == "error" and limit_recorded:
        queue_limit_refusal_continue(db, turn_id, thread_id)
    elif outcome == "error" and isinstance(error, ContextOverflowEnd):
        queue_overflow_continue(db, turn_id, thread_id)
    elif outcome == "error" and isinstance(error, ClaudeLoginRaceEnd):
        queue_login_race_continue(db, turn_id, thread_id)
