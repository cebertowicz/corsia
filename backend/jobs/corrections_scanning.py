"""Narrow, cursor-based candidate scan for corrections recording."""

from __future__ import annotations

from dataclasses import dataclass

from core.corrections_input import CONTEXT_HEAD_CHARS, CONTEXT_ROWS, CONTEXT_TAIL_CHARS, owner_text, shape_context
from core.corrections_recording import (
    CORRECTION_EVENT,
    MESSAGE_SUBJECT_PREFIX,
    ClassifierVersion,
    CorrectionCandidate,
    is_owner_authored_message,
    owner_message_sql_predicate,
    version_match_sql,
)
from core.measurement_scope import scope_cte


@dataclass(frozen=True)
class PendingCandidateKey:
    """The indexed ordering keys required before hydrating one classifier input."""

    rowid: int
    message_id: str
    thread_id: str
    project_id: str | None
    created_at: str


def pending_candidate_keys(
    version: ClassifierVersion, batch_size: int, owner_user_id: str,
    before: tuple[str, str] | None = None, since: str | None = None,
) -> list[PendingCandidateKey]:
    """Read one owner's pending ordering keys without projecting message content."""
    from infra.database import get_db

    db = get_db()
    try:
        owner_filter, owner_filter_params = owner_message_sql_predicate()
        version_filter, version_filter_params = version_match_sql("e", version)
        rows = db.execute(
            scope_cte() + f"""
            SELECT m.rowid,m.id,m.thread_id,t.project_id,m.created_at
            FROM messages m
            JOIN scope_threads scoped ON scoped.id=m.thread_id
            JOIN threads t ON t.id=m.thread_id
            WHERE {owner_filter}
              AND (:before_created_at IS NULL
                   OR m.created_at < :before_created_at
                   OR (m.created_at=:before_created_at AND m.id < :before_message_id))
              AND (:since_created_at IS NULL OR m.created_at >= :since_created_at)
              AND NOT EXISTS (
                SELECT 1 FROM outcome_events e
                WHERE e.event_name=:correction_event
                  AND e.subject_key=:message_prefix || m.id AND e.retracted_at IS NULL
                  AND {version_filter}
              )
            ORDER BY m.created_at DESC,m.id DESC LIMIT :scan_limit
            """,
            {
                **owner_filter_params,
                # The same identity the dedupe key hashes, filtered before LIMIT.
                **version_filter_params,
                "user_id": owner_user_id,
                "scope_project": "all",
                "scope_agent": None,
                "correction_event": CORRECTION_EVENT,
                "message_prefix": MESSAGE_SUBJECT_PREFIX,
                "scan_limit": batch_size,
                "before_created_at": before[0] if before else None,
                "before_message_id": before[1] if before else None,
                "since_created_at": since,
            },
        ).fetchall()
        return [
            PendingCandidateKey(
                rowid=row[0], message_id=row[1], thread_id=row[2], project_id=row[3], created_at=row[4],
            )
            for row in rows
        ]
    finally:
        db.close()


def hydrate_candidates(keys: list[PendingCandidateKey]) -> list[CorrectionCandidate]:
    """Fetch content only for the globally selected candidate keys.

    Context rows are read as a bounded head and tail, never in full, and shaped by the
    input policy the model was trained on (``core.corrections_input``).
    """
    from infra.database import get_db

    db = get_db()
    try:
        candidates: list[CorrectionCandidate] = []
        for key in keys:
            message = db.execute(
                "SELECT role,content,metadata FROM messages WHERE rowid=?", (key.rowid,)
            ).fetchone()
            if not message or not is_owner_authored_message(
                role=message[0], content=message[1], metadata=message[2],
            ):
                continue
            preceding = db.execute(
                """SELECT role,substr(content,1,:head),substr(content,-:tail),metadata FROM messages
                   WHERE thread_id=:thread AND role IN ('user','assistant')
                     AND (created_at < :created OR (created_at=:created AND id < :message))
                   ORDER BY created_at DESC,id DESC LIMIT :rows""",
                {
                    "head": CONTEXT_HEAD_CHARS, "tail": CONTEXT_TAIL_CHARS, "rows": CONTEXT_ROWS,
                    "thread": key.thread_id, "created": key.created_at, "message": key.message_id,
                },
            ).fetchall()
            candidates.append(CorrectionCandidate(
                message_id=key.message_id, thread_id=key.thread_id, project_id=key.project_id,
                content=owner_text(message[1]), created_at=key.created_at,
                context=shape_context(preceding),
            ))
        return candidates
    finally:
        db.close()
