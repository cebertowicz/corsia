"""Persist local ONNX correction facts and select bounded pending candidates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from loguru import logger

from core.corrections_recording import (
    CORRECTION_EVENT,
    DECISION_SUBJECT_PREFIX,
    FACT_KEYS,
    LABEL_CORRECTION,
    LABEL_UNREADABLE,
    ClassifierVersion,
    CorrectionCandidate,
    dedupe_key,
    fact_attributes,
    subject_key,
    version_match_sql,
)
from infra.db_async import run_in_db
from jobs.corrections_scanning import (
    PendingCandidateKey,
    ScanCursor,
    hydrate_candidates,
    pending_candidate_keys,
    pending_source_candidate_keys,
    source_candidate,
)

if TYPE_CHECKING:
    from core.corrections_classifier import Classification


def owner_user_ids() -> list[str]:
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
    """Write one versioned local-classifier fact in its own transaction.

    A new correction queues its triage note in the same transaction.
    """
    from core.correction_notes import queue_correction_note
    from infra.database import write_transaction
    from infra.outcome_events import record_outcome_event

    attributes = fact_attributes(candidate, classification, version)
    if set(attributes) != set(FACT_KEYS):
        raise RuntimeError("correction fact attributes are incomplete")
    with write_transaction(None, scope="record_correction_fact") as db:
        result = record_outcome_event(
            db,
            dedupe_key=dedupe_key(candidate, version),
            event_name=CORRECTION_EVENT,
            source="corsia",
            provenance="agent_recorded",
            ts=candidate.created_at,
            subject_key=subject_key(candidate.source_kind, candidate.source_id),
            project_id=candidate.project_id,
            agent_thread_id=candidate.thread_id,
            attributes=attributes,
        )
        if result.dropped_keys:
            raise RuntimeError("correction fact attributes were dropped")
        if result.inserted and result.event_id and classification["label"] == LABEL_CORRECTION:
            queue_correction_note(db, candidate, version, result.event_id)
        return result.inserted


def candidate_for_source(source_kind: str, source_id: str) -> CorrectionCandidate | None:
    """Hydrate one newly arrived correction source, or return no human candidate."""
    from infra.database import get_db

    if source_kind == "message":
        return candidate_for_message(source_id)
    db = get_db()
    try:
        candidate = source_candidate(db, source_kind, source_id)
    finally:
        db.close()
    return replace(candidate, arrived=True) if candidate is not None else None


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
        key = PendingCandidateKey(int(row[0]), "message", str(row[1]), str(row[2]), row[3], str(row[4]))
    finally:
        db.close()
    candidates = hydrate_candidates([key])
    return replace(candidates[0], arrived=True) if candidates else None


def notify_if_owner(accepted: Any, *, role: str, content: str, metadata: object) -> None:
    """Hand the corrections worker an admitted owner message; best effort, never failing the send."""
    from core.corrections_recording import is_owner_authored_message
    from jobs.corrections_realtime import notify

    try:
        if not accepted.duplicate and is_owner_authored_message(role=role, content=content, metadata=metadata):
            notify("message", accepted.message_id)
    except Exception:
        logger.exception("corrections classifier admission notification failed")


def notify_doc_comment(comment_id: str) -> None:
    """Hand the worker a committed user comment; best effort, never failing the comment."""
    from jobs.corrections_realtime import notify

    try:
        notify("doc_comment", comment_id)
    except Exception:
        logger.exception("corrections classifier comment notification failed")


def notify_decision_answer(decision_id: str, other_text: object) -> None:
    """Hand the worker a committed decision answer that carries free text; best effort, never failing the answer.

    An Other answer and a pick-any note both arrive as ``other_text``.
    """
    from jobs.corrections_realtime import notify

    try:
        if isinstance(other_text, str) and other_text.strip():
            notify("plan_decision_answer", decision_id)
    except Exception:
        logger.exception("corrections classifier decision-answer notification failed")


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


def unreadable_facts(db, version: ClassifierVersion) -> int:
    """Count served unreadable facts in the Measurement window, once per source.

    Message and document-comment facts count, including legacy facts that lack a
    source-kind attribute. Decision answers stay triage-only. The ``(event_name,
    ts)`` index bounds the read to the window's correction facts.
    """
    from jobs.corrections_activation import measurement_window_start

    version_filter, version_params = version_match_sql("e", version)
    row = db.execute(
        f"""SELECT COUNT(DISTINCT e.subject_key) FROM outcome_events e
           WHERE e.event_name=:event AND e.ts >= :since AND e.retracted_at IS NULL
             AND {version_filter} AND e.subject_key NOT LIKE :decision_prefix || '%'
             AND json_extract(e.attributes,'$.label')=:label""",
        {
            **version_params, "event": CORRECTION_EVENT, "label": LABEL_UNREADABLE,
            "decision_prefix": DECISION_SUBJECT_PREFIX,
            "since": measurement_window_start().isoformat(),
        },
    ).fetchone()
    return int(row[0])


@dataclass(slots=True)
class _PendingBatch:
    """One selected page and its hydrated owner-authored candidates."""

    selected_keys: list[PendingCandidateKey]
    candidates: list[CorrectionCandidate]


async def next_pending_batch(
    version: ClassifierVersion, batch_size: int, before: ScanCursor | None,
    *, since: str | None = None,
) -> _PendingBatch:
    """Globally select keys before reading the bounded classifier inputs."""
    keys: list[PendingCandidateKey] = await run_in_db(
        pending_source_candidate_keys, version, batch_size, before, since,
    )
    for owner_user_id in await run_in_db(owner_user_ids):
        keys.extend(
            await run_in_db(
                pending_candidate_keys, version, batch_size, owner_user_id, before, since,
            )
        )
    selected = sorted(
        keys, key=lambda key: (key.created_at, key.source_kind, key.source_id), reverse=True,
    )[:batch_size]
    return _PendingBatch(selected, await run_in_db(hydrate_candidates, selected))
