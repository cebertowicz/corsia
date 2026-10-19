"""Persist local ONNX correction facts and select bounded pending candidates."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import TYPE_CHECKING, Any

from loguru import logger

from core.corrections_input import owner_text
from core.corrections_recording import (
    CORRECTION_EVENT,
    DECISION_SUBJECT_PREFIX,
    FACT_KEYS,
    LABEL_UNREADABLE,
    UNIT_KINDS,
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
    PositiveComment,
    ScanCursor,
    hydrate_candidates,
    pending_candidate_keys,
    pending_source_candidate_keys,
    positive_comment_keys,
)

if TYPE_CHECKING:
    from core.corrections_classifier import Classification

#: Comments ``recover_comment_notes`` reads per page.
COMMENT_PAGE = 20


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

    The notes it completes are written in the same transaction, so a failing
    note rolls the fact back and the source stays pending for the scan to retry.
    """
    from core.correction_notes import write_fact_notes
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
        if result.inserted and result.event_id:
            write_fact_notes(db, candidate, str(classification["label"]), version, result.event_id)
        return result.inserted


def write_unit(kind: str, unit_id: str) -> None:
    """Write the notes one completed review unit owes, in the worker's own transaction."""
    from core.correction_notes import write_unit_notes
    from infra.database import write_transaction

    with write_transaction(None, scope="write_correction_unit_notes") as db:
        write_unit_notes(db, kind, unit_id)


def _active_since() -> str | None:
    from core.corrections_recording import active_since
    from infra.database import get_db

    db = get_db()
    try:
        return active_since(db)
    finally:
        db.close()


def _write_recovered(version: ClassifierVersion, keys: list[PositiveComment]) -> None:
    """Write the notes of the units holding ``keys``' comments that the no-history rule admits."""
    from core.correction_notes import made_after_activation, write_comment_notes
    from infra.database import write_transaction

    with write_transaction(None, scope="recover_correction_comment_notes") as db:
        admitted = [key.comment_id for key in keys if made_after_activation(db, key.created_at, version)]
        write_comment_notes(db, admitted)


async def recover_comment_notes(window_start: str) -> None:
    """Write the note of each unit holding a comment the active version found a correction in.

    The scan's recovery for a unit key the worker never took or failed on. It
    selects the active version's live ``correction`` facts on comments made
    after that version became active (``correction_notes.made_after_activation``,
    the no-history rule a scan fact's note follows) and no earlier than the
    scan's ``window_start``. It hands those comments to ``write_comment_notes``,
    whose ``_unit_of`` is the one membership rule. A candidate version's
    verdicts are never selected, so its backfill cannot note a unit the active
    version found clean. A noted comment is read again on later passes; its
    ``note_key`` stops the write.
    """
    from core.corrections_recording import active_corrections_version

    version = await run_in_db(active_corrections_version)
    activated = await run_in_db(_active_since)
    if not isinstance(version, ClassifierVersion) or activated is None:
        return  # No readable active version: no note is owed.
    # Cut to the second: a superset of the later instants ``made_after_activation`` admits.
    since, after = max(activated, window_start)[:19], None
    while keys := await run_in_db(positive_comment_keys, version, since, COMMENT_PAGE, after):
        await run_in_db(_write_recovered, version, keys)
        after = keys[-1]


def arrival_candidate(arrival_kind: str, arrival_id: str) -> CorrectionCandidate | None:
    """Take one arrival: a completed review unit's key writes the notes it owes; a source is hydrated."""
    if arrival_kind in UNIT_KINDS:
        write_unit(arrival_kind, arrival_id)
        return None
    return candidate_for_source(arrival_kind, arrival_id)


def candidate_for_source(source_kind: str, source_id: str) -> CorrectionCandidate | None:
    """Hydrate one newly arrived correction source or return no human candidate."""
    if source_kind == "message":
        return candidate_for_message(source_id)
    candidate = source_candidate(source_kind, source_id)
    return replace(candidate, arrived=True) if candidate is not None else None


def source_candidate(source_kind: str, source_id: str) -> CorrectionCandidate | None:
    """Read one comment or decision-answer source as the scan sees it, or None."""
    from infra.database import get_db

    db = get_db()
    try:
        if source_kind == "doc_comment":
            from core.correction_targets import comment_target_sql

            row = db.execute(
                f"""SELECT c.id,c.anchor_text,c.body,c.created_at,{comment_target_sql('c')} AS target
                    FROM doc_comments c WHERE c.id=? AND c.author_kind='user'""", (source_id,),
            ).fetchone()
            if row is None or not row['target']:
                return None  # No agent was or will be handed it: the scan never selects it either.
            target = str(row['target'])
            project = db.execute("SELECT project_id FROM threads WHERE id=?", (target,)).fetchone()
            content = f"Artifact excerpt: {row['anchor_text'] or '(no anchor)'}\nOwner comment: {row['body']}"
            return CorrectionCandidate(
                "doc_comment", str(row['id']), target, project[0] if project else None,
                owner_text(content), str(row['created_at']),
            )
        if source_kind == "plan_decision_answer":
            from plans.public import answer_note, decision_identity_row

            row = decision_identity_row(db, source_id)
            if row is None:
                return None
            try:
                body = json.loads(row['metadata']).get('plan_decision', {})
            except (TypeError, ValueError):
                return None
            if not isinstance(body, dict):
                return None
            # Plans' own test of whether an answer carries the person's words.
            text = answer_note(body.get('answer'))
            thread_id, revision, answered_at = body.get('thread_id'), body.get('revision'), body.get('answered_at')
            if (
                body.get('state') != 'answered' or text is None
                or not isinstance(thread_id, str) or not thread_id or revision is None
                or not isinstance(answered_at, str) or not answered_at
            ):
                return None
            listed = body.get('options')
            options = listed if isinstance(listed, list) else []
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
                quote=text, source_ref=source_id,
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


def notify_if_owner(accepted: Any, *, role: str, content: str, metadata: object) -> None:
    """Hand the corrections worker an admitted owner message; best effort, never failing the send."""
    from core.corrections_recording import is_owner_authored_message
    from jobs.corrections_realtime import notify

    try:
        if not accepted.duplicate and is_owner_authored_message(role=role, content=content, metadata=metadata):
            notify("message", accepted.message_id)
    except Exception:
        logger.exception("corrections classifier admission notification failed")


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
