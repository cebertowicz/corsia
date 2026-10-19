"""Persist local ONNX correction facts and select bounded pending candidates."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import TYPE_CHECKING

from loguru import logger

from core.corrections_input import owner_text
from core.corrections_recording import (
    CORRECTION_EVENT,
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
    hydrate_candidates,
    pending_candidate_keys,
    pending_source_candidate_keys,
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
) -> tuple[bool, str | None]:
    """Write one versioned local-classifier fact in its own transaction.

    A new correction queues its triage note in the same transaction; a comment's
    fact also retries the round or batch note that waited for it.
    """
    from core.correction_notes import queue_correction_note, queue_late_comment_note
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
        note: tuple[str, str] | None = None
        if result.inserted and result.event_id:
            if candidate.source_kind == "doc_comment":
                try:
                    note = queue_late_comment_note(db, candidate, result.event_id)
                except Exception:  # A fact is never lost to its note.
                    logger.exception("late comment correction note failed")
            elif classification["label"] == LABEL_CORRECTION:
                note_key = queue_correction_note(db, candidate, version, result.event_id)
                note = None if note_key is None else (candidate.thread_id, note_key)
        if note is not None and candidate.source_kind != "message":
            from infra.passive_changes import materialize_pending_digest

            admitted = materialize_pending_digest(db, note[0], wake_id=f"correction-wake:{note[1]}")
            if admitted is None or admitted.duplicate:
                note = None
        return result.inserted, note[0] if note is not None else None


def candidate_for_source(source_kind: str, source_id: str) -> CorrectionCandidate | None:
    """Hydrate one newly arrived correction source or return no human candidate."""
    if source_kind == "message":
        return candidate_for_message(source_id)
    from infra.database import get_db

    db = get_db()
    try:
        if source_kind == "doc_comment":
            row = db.execute(
                """SELECT id,artifact_id,anchor_text,body,created_at FROM doc_comments
                   WHERE id=? AND author_kind='user'""", (source_id,),
            ).fetchone()
            from registry.models import get_artifact

            artifact = get_artifact(str(row['artifact_id']), db=db) if row is not None else None
            if row is None or artifact is None or not artifact.get('parent_thread_id'):
                return None
            target = str(artifact['parent_thread_id'])
            project = db.execute("SELECT project_id FROM threads WHERE id=?", (target,)).fetchone()
            content = f"Artifact excerpt: {row['anchor_text'] or '(no anchor)'}\nOwner comment: {row['body']}"
            return CorrectionCandidate(
                "doc_comment", str(row['id']), target, project[0] if project else None,
                owner_text(content), str(row['created_at']),
            )
        if source_kind == "plan_decision_answer":
            from plans.public import decision_identity_row

            row = decision_identity_row(db, source_id)
            if row is None:
                return None
            try:
                body = json.loads(row['metadata']).get('plan_decision', {})
            except (TypeError, ValueError):
                return None
            answer = body.get('answer') if isinstance(body, dict) else None
            text = answer.get('text') if isinstance(answer, dict) else None
            thread_id, revision, answered_at = (
                body.get('thread_id'), body.get('revision'), body.get('answered_at'),
            ) if isinstance(body, dict) else (None, None, None)
            if (
                body.get('state') != 'answered' or not isinstance(text, str) or not text.strip()
                or not isinstance(thread_id, str) or not thread_id or revision is None
                or not isinstance(answered_at, str) or not answered_at
            ):
                return None
            options = body.get('options') if isinstance(body.get('options'), list) else ()
            options_text = '; '.join(
                f"{item.get('id')} — {item.get('label')} — {item.get('detail', '')}"
                for item in options if isinstance(item, dict)
            )
            content = (f"Decision question: {body.get('title', '')}\nContext: {body.get('context', '')}\n"
                       f"Trade-off: {body.get('tradeoff', '')}\nOptions: {options_text}\nOwner answer: {text}")
            project = db.execute("SELECT project_id FROM threads WHERE id=?", (thread_id,)).fetchone()
            return CorrectionCandidate(
                "plan_decision_answer", f"{source_id}:{revision}", thread_id,
                project[0] if project else None, owner_text(content), answered_at,
            )
        return None
    finally:
        db.close()


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


async def _record_and_drive(
    candidate: CorrectionCandidate,
    classification: dict[str, str | int | bool],
    version: ClassifierVersion,
) -> bool:
    inserted, wake_target = await run_in_db(_record_fact, candidate, classification, version)
    if wake_target is not None:
        from infra.thread_execution import drive_idle_thread

        await drive_idle_thread(wake_target)
    return inserted


async def record_classification(candidate: CorrectionCandidate, result: Classification) -> bool:
    """Record a scored fact; ``failure_reason`` is ``none``."""
    return await _record_and_drive(candidate, {
        "label": result.label,
        "truncated": result.truncated,
        "original_tokens": result.original_tokens,
        "failure_reason": "none",
    }, result.version)


async def record_unreadable(candidate: CorrectionCandidate, version: ClassifierVersion, reason: str) -> bool:
    """Record an input a healthy model failed on twice, with that failure's kind."""
    return await _record_and_drive(candidate, {
        "label": LABEL_UNREADABLE, "truncated": False, "original_tokens": 0, "failure_reason": reason,
    }, version)


def unreadable_messages(db, version: ClassifierVersion) -> int:
    """Count served unreadable facts in the Measurement window, once each.

    Comment facts count alongside messages, including legacy facts that lack a
    source-kind attribute. Decision answers stay triage-only. The ``(event_name,
    ts)`` index bounds the read to the window's correction facts.
    """
    from jobs.corrections_activation import measurement_window_start

    version_filter, version_params = version_match_sql("e", version)
    row = db.execute(
        f"""SELECT COUNT(DISTINCT e.subject_key) FROM outcome_events e
           WHERE e.event_name=:event AND e.ts >= :since AND e.retracted_at IS NULL
             AND {version_filter} AND e.subject_key NOT LIKE 'plan-decision-answer:%'
             AND json_extract(e.attributes,'$.label')=:label""",
        {
            **version_params, "event": CORRECTION_EVENT, "label": LABEL_UNREADABLE,
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
    version: ClassifierVersion, batch_size: int, before: tuple[str, ...] | None,
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
