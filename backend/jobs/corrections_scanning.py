"""Narrow, cursor-based candidate scan for corrections recording."""

from __future__ import annotations

from dataclasses import dataclass

from core.corrections_recording import (
    CORRECTION_EVENT,
    CheckerVersion,
    CorrectionCandidate,
    is_owner_authored_message,
    owner_message_sql_predicate,
)
from core.measurement_scope import scope_cte

MAX_PENDING_SCAN_MULTIPLIER = 8


@dataclass(frozen=True)
class PendingCandidateKey:
    """The indexed ordering keys required before hydrating one classifier input."""

    rowid: int
    message_id: str
    thread_id: str
    project_id: str | None
    created_at: str


def pending_candidate_keys(
    version: CheckerVersion, batch_size: int, owner_user_id: str,
    before: tuple[str, str] | None = None,
) -> list[PendingCandidateKey]:
    """Read one owner's pending ordering keys without projecting message content."""
    from infra.database import get_db

    db = get_db()
    try:
        owner_filter, owner_filter_params = owner_message_sql_predicate()
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
              AND NOT EXISTS (
                SELECT 1 FROM outcome_events e
                WHERE e.event_name=:correction_event
                  AND e.subject_key=:message_prefix || m.id AND e.retracted_at IS NULL
                  AND json_extract(e.attributes,'$.checker_model_id')=:model_id
                  AND json_extract(e.attributes,'$.rubric_sha256')=:rubric_sha256
                  AND json_extract(e.attributes,'$.prompt_sha256')=:prompt_sha256
              )
            ORDER BY m.created_at DESC,m.id DESC LIMIT :scan_limit
            """,
            {
                **owner_filter_params,
                "user_id": owner_user_id,
                "scope_project": "all",
                "scope_agent": None,
                "correction_event": CORRECTION_EVENT,
                "message_prefix": "message:",
                "model_id": version.model_id,
                "rubric_sha256": version.rubric_sha256,
                "prompt_sha256": version.prompt_sha256,
                "scan_limit": batch_size * MAX_PENDING_SCAN_MULTIPLIER,
                "before_created_at": before[0] if before else None,
                "before_message_id": before[1] if before else None,
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
    """Fetch content only for the globally selected candidate keys."""
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
                """SELECT content FROM messages
                   WHERE thread_id=? AND role='assistant'
                     AND (created_at < ? OR (created_at=? AND rowid < ?))
                   ORDER BY created_at DESC,rowid DESC LIMIT 1""",
                (key.thread_id, key.created_at, key.created_at, key.rowid),
            ).fetchone()
            candidates.append(CorrectionCandidate(
                message_id=key.message_id, thread_id=key.thread_id, project_id=key.project_id,
                content=message[1], created_at=key.created_at,
                preceding_agent=preceding[0] if preceding else "",
            ))
        return candidates
    finally:
        db.close()


def pending_candidates(
    version: CheckerVersion, batch_size: int, owner_user_id: str,
    before: tuple[str, str] | None = None,
) -> list[CorrectionCandidate]:
    """Compatibility helper for callers that need one owner's hydrated batch."""
    keys = pending_candidate_keys(version, batch_size, owner_user_id, before)
    return hydrate_candidates(keys[:batch_size])
