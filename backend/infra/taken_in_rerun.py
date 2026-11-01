"""The one re-run a failed Turn owes a person whose input it took in but never answered.

Written inside the terminal transaction, before the successor claim, so the
claim starts exactly one successor Turn. The person's message is never
re-written: it stays delivered in the transcript, and the successor's only new
input is this model-only row, which the transcript never draws.
"""

from __future__ import annotations

import sqlite3

from infra.thread_input_rows import turn_starting_work_pending
from llm.error_cause import ProviderTurnFailure

#: Failure kinds a re-run cannot answer: the limit, sign-in or context size that
#: refused this Turn would refuse the next one the same way.
_NO_RERUN_KINDS = frozenset({"usage_limit", "sign_in_expired", "auth_rejected", "context_overflow"})
_RERUN_ID_PREFIX = "turn-rerun:"
_CONTENT = (
    "Your previous turn ended with an error before it answered the input above. "
    "Answer that input now."
)


def rerun_message_id(turn_id: str) -> str:
    """Stable id of the one re-run input written for a failed Turn."""
    return f"{_RERUN_ID_PREFIX}{turn_id}"


def _rerun_excluded(error: str | BaseException | None) -> bool:
    from connections.account_policy import AccountAdmissionError
    from core.checkpoint_types import CheckpointContinuationError

    if isinstance(error, (AccountAdmissionError, CheckpointContinuationError)):
        return True
    return isinstance(error, ProviderTurnFailure) and error.cause_kind in _NO_RERUN_KINDS


def _was_rerun(db: sqlite3.Connection, turn_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM messages WHERE active_turn_id=? AND id LIKE ? LIMIT 1",
        (turn_id, f"{_RERUN_ID_PREFIX}%"),
    ).fetchone() is not None


def _unanswered_input(db: sqlite3.Connection, turn_id: str) -> bool:
    """Whether the Turn took in a person's input that no assistant reply after it answered."""
    row = db.execute(
        "SELECT MAX(seq) AS seq FROM messages WHERE active_turn_id=? AND role='user' "
        "AND delivery_state='delivered' "
        "AND COALESCE(json_extract(metadata,'$.sender_kind'),'')<>'agent'",
        (turn_id,),
    ).fetchone()
    if row is None or row["seq"] is None:
        return False
    return db.execute(
        "SELECT 1 FROM messages WHERE role='assistant' AND seq>? AND TRIM(COALESCE(content,''))<>'' "
        "AND json_extract(metadata,'$.turn_id')=? LIMIT 1",
        (row["seq"], turn_id),
    ).fetchone() is None


def queue_taken_in_rerun(
    db: sqlite3.Connection, turn_id: str, thread_id: str, error: str | BaseException | None,
) -> None:
    """Queue the re-run input for a Turn that ended ``error`` with a person's input unanswered.

    A Turn that was itself a re-run never chains another; queued work that
    already starts a Turn answers the person on its own.
    """
    if (
        _rerun_excluded(error)
        or _was_rerun(db, turn_id)
        or not _unanswered_input(db, turn_id)
        or turn_starting_work_pending(db, thread_id)
    ):
        return
    from infra.input_admission import accept_thread_input

    accept_thread_input(
        thread_id,
        role="system",
        content=_CONTENT,
        intent="apply_now",
        message_id=rerun_message_id(turn_id),
        metadata={"type": "turn_rerun", "sender_kind": "system", "failed_turn_id": turn_id},
        delivery_options={"native_mode": "event", "attachments": []},
        db=db,
    )
