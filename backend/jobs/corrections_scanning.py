"""Narrow, cursor-based candidate scan for corrections recording."""

from __future__ import annotations

from dataclasses import dataclass

from core.corrections_recording import (
    CORRECTION_EVENT,
    CheckerVersion,
    CorrectionCandidate,
    dedupe_key,
    is_owner_authored_message,
    owner_message_sql_predicate,
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
              AND 1=1
            ORDER BY m.created_at DESC,m.id DESC LIMIT :scan_limit
            """,
            {
                **owner_filter_params,
                "user_id": owner_user_id,
                "scope_project": "all",
                "scope_agent": None,
                # The exact asset version is checked against the dedupe key below.
                "scan_limit": batch_size,
                "before_created_at": before[0] if before else None,
                "before_message_id": before[1] if before else None,
            },
        ).fetchall()
        keys = [
            PendingCandidateKey(
                rowid=row[0], message_id=row[1], thread_id=row[2], project_id=row[3], created_at=row[4],
            )
            for row in rows
        ]
        return [
            key for key in keys
            if db.execute(
                "SELECT 1 FROM outcome_events WHERE dedupe_key=? AND retracted_at IS NULL",
                (dedupe_key(key.message_id, version),),
            ).fetchone() is None
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
                """SELECT role,content FROM messages
                   WHERE thread_id=? AND (created_at < ? OR (created_at=? AND rowid < ?))
                   ORDER BY created_at DESC,rowid DESC LIMIT 6""",
                (key.thread_id, key.created_at, key.created_at, key.rowid),
            ).fetchall()
            context = tuple((str(row[0]), str(row[1])) for row in reversed(preceding))
            candidates.append(CorrectionCandidate(
                message_id=key.message_id, thread_id=key.thread_id, project_id=key.project_id,
                content=message[1], created_at=key.created_at,
                preceding_agent=next((text for role, text in reversed(context) if role == "assistant"), ""),
                context=context,
            ))
        return candidates
    finally:
        db.close()
