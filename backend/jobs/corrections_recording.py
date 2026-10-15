"""Persist local ONNX correction facts and select bounded pending candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.corrections_recording import (
    CORRECTION_EVENT,
    FACT_KEYS,
    LABEL_UNREADABLE,
    ClassifierVersion,
    CorrectionCandidate,
    dedupe_key,
    fact_attributes,
    subject_key,
)
from infra.db_async import run_in_db
from jobs.corrections_scanning import PendingCandidateKey, hydrate_candidates, pending_candidate_keys

if TYPE_CHECKING:
    from core.corrections_classifier import Classification


def _owner_user_ids() -> list[str]:
    """Return each durable account owner whose messages can be scoped independently."""
    from infra.database import get_db

    db = get_db()
    try:
        return [
            str(row[0])
            for row in db.execute(
                "SELECT DISTINCT user_id FROM threads "
                "WHERE user_id IS NOT NULL AND trim(user_id)<>'' ORDER BY user_id"
            ).fetchall()
        ]
    finally:
        db.close()


def _record_fact(
    candidate: CorrectionCandidate,
    classification: dict[str, str | int | bool],
    version: ClassifierVersion,
) -> bool:
    """Write one versioned local-classifier fact in its own transaction."""
    from infra.database import write_transaction
    from infra.outcome_events import record_outcome_event

    attributes = fact_attributes(candidate, classification, version)
    if set(attributes) != set(FACT_KEYS):
        raise RuntimeError("correction fact attributes are incomplete")
    with write_transaction(None, scope="record_correction_fact") as db:
        result = record_outcome_event(
            db,
            dedupe_key=dedupe_key(candidate.message_id, version),
            event_name=CORRECTION_EVENT,
            source="corsia",
            provenance="agent_recorded",
            ts=candidate.created_at,
            subject_key=subject_key(candidate.message_id),
            project_id=candidate.project_id,
            agent_thread_id=candidate.thread_id,
            attributes=attributes,
        )
        if result.dropped_keys:
            raise RuntimeError("correction fact attributes were dropped")
        return result.inserted


def candidate_for_message(message_id: str) -> CorrectionCandidate | None:
    """Read one arrived message as a candidate; None when it is not an owner candidate."""
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            """SELECT m.rowid,m.id,m.thread_id,t.project_id,m.created_at
               FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.id=?""", (message_id,),
        ).fetchone()
        if row is None:
            return None
        key = PendingCandidateKey(int(row[0]), str(row[1]), str(row[2]), row[3], str(row[4]))
    finally:
        db.close()
    candidates = hydrate_candidates([key])
    return candidates[0] if candidates else None


async def record_classification(candidate: CorrectionCandidate, result: Classification) -> bool:
    """Record a scored fact; ``failure_reason`` is ``none``."""
    classification = {
        "label": result.label,
        "truncated": result.truncated,
        "original_tokens": result.original_tokens,
        "failure_reason": "none",
    }
    return await run_in_db(_record_fact, candidate, classification, result.version)


async def record_unreadable(candidate: CorrectionCandidate, version: ClassifierVersion, reason: str) -> bool:
    """Record an input a healthy model failed on twice, with that failure's kind."""
    classification = {
        "label": LABEL_UNREADABLE, "truncated": False, "original_tokens": 0, "failure_reason": reason,
    }
    return await run_in_db(_record_fact, candidate, classification, version)


@dataclass(slots=True)
class _PendingBatch:
    """One selected page and its hydrated owner-authored candidates."""

    selected_keys: list[PendingCandidateKey]
    candidates: list[CorrectionCandidate]


async def _next_pending_batch(
    version: ClassifierVersion, batch_size: int, before: tuple[str, str] | None,
    *, since: str | None = None,
) -> _PendingBatch:
    """Globally select keys before reading the bounded classifier inputs."""
    keys: list[PendingCandidateKey] = []
    for owner_user_id in await run_in_db(_owner_user_ids):
        keys.extend(
            await run_in_db(
                pending_candidate_keys, version, batch_size, owner_user_id, before, since,
            )
        )
    selected = sorted(
        keys, key=lambda key: (key.created_at, key.message_id), reverse=True,
    )[:batch_size]
    return _PendingBatch(selected, await run_in_db(hydrate_candidates, selected))
