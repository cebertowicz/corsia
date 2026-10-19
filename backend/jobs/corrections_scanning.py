"""Narrow, cursor-based candidate scan for corrections recording."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from core.corrections_input import CONTEXT_HEAD_CHARS, CONTEXT_ROWS, CONTEXT_TAIL_CHARS, owner_text, shape_context
from core.corrections_recording import (
    COMMENT_SUBJECT_PREFIX,
    CORRECTION_EVENT,
    DECISION_SUBJECT_PREFIX,
    LABEL_CORRECTION,
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
    """The ordering keys required before hydrating one classifier input."""

    rowid: int | None
    source_kind: str
    source_id: str
    thread_id: str | None
    project_id: str | None
    created_at: str


ScanCursor = tuple[str, str, str]
"""The last selected ``(created_at, source_kind, source_id)``; the scan reads strictly before it."""


def _before_params(before: ScanCursor | None) -> dict[str, str | None]:
    created_at, source_kind, source_id = before if before is not None else (None, None, None)
    return {"before_created_at": created_at, "before_source_kind": source_kind, "before_source_id": source_id}


def _before_clause(source_kind: str, id_expression: str, created_expression: str = "created_at") -> str:
    """One descending cursor definition for every correction source."""
    return f"""(:before_created_at IS NULL
                 OR {created_expression} < :before_created_at
                 OR ({created_expression}=:before_created_at AND (
                     :source_kind < :before_source_kind
                     OR (:source_kind=:before_source_kind AND {id_expression} < :before_source_id)
                 )))"""


def pending_candidate_keys(
    version: ClassifierVersion, batch_size: int, owner_user_id: str,
    before: ScanCursor | None = None, since: str | None = None,
) -> list[PendingCandidateKey]:
    """Read one owner's pending message keys without projecting message content."""
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
              AND {_before_clause('message', 'm.id', 'm.created_at')}
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
                **version_filter_params,
                **_before_params(before),
                "source_kind": "message",
                "user_id": owner_user_id,
                "scope_project": "all",
                "scope_agent": None,
                "correction_event": CORRECTION_EVENT,
                "message_prefix": MESSAGE_SUBJECT_PREFIX,
                "scan_limit": batch_size,
                "since_created_at": since,
            },
        ).fetchall()
        return [
            PendingCandidateKey(
                rowid=row[0], source_kind="message", source_id=row[1], thread_id=row[2], project_id=row[3], created_at=row[4],
            )
            for row in rows
        ]
    finally:
        db.close()


def pending_source_candidate_keys(
    version: ClassifierVersion, batch_size: int,
    before: ScanCursor | None = None, since: str | None = None,
) -> list[PendingCandidateKey]:
    """Read pending comments and free-text decision answers without their text.

    The source tables remain canonical.  This scan only selects identities, then
    ``source_candidate`` rechecks the source and builds the bounded input.
    """
    from core.correction_targets import comment_target_sql
    from infra.database import get_db
    from plans.public import DECISION_TYPE

    db = get_db()
    try:
        version_filter, version_filter_params = version_match_sql("e", version)
        params = {
            **version_filter_params,
            **_before_params(before),
            "correction_event": CORRECTION_EVENT,
            "comment_prefix": COMMENT_SUBJECT_PREFIX,
            "decision_prefix": DECISION_SUBJECT_PREFIX,
            "decision_type": DECISION_TYPE,
            "scan_limit": batch_size,
            "since_created_at": since,
        }
        comment_rows = db.execute(
            f"""
            SELECT c.id,c.created_at
            FROM doc_comments c
            WHERE c.author_kind='user'
              AND ({comment_target_sql('c')}) IS NOT NULL
              AND {_before_clause('doc_comment', 'c.id', 'c.created_at')}
              AND (:since_created_at IS NULL OR c.created_at >= :since_created_at)
              AND NOT EXISTS (
                SELECT 1 FROM outcome_events e
                WHERE e.event_name=:correction_event
                  AND e.subject_key=:comment_prefix || c.id AND e.retracted_at IS NULL
                  AND {version_filter}
              )
            ORDER BY c.created_at DESC,c.id DESC LIMIT :scan_limit
            """,
            {**params, "source_kind": "doc_comment"},
        ).fetchall()
        decision_created_at = "json_extract(metadata,'$.plan_decision.answered_at')"
        decision_rows = db.execute(
            f"""
            SELECT external_id,json_extract(metadata,'$.plan_decision.answered_at')
            FROM artifact_identities
            WHERE integration='local' AND type=:decision_type AND state='active'
              AND json_extract(metadata,'$.plan_decision.state')='answered'
              -- A coarse prefilter, never narrower than Plans' ``answer_note``, which
              -- ``source_candidate`` applies to each selected answer.
              AND COALESCE(json_extract(metadata,'$.plan_decision.answer.text'),'')<>''
              AND {_before_clause('plan_decision_answer', 'external_id', decision_created_at)}
              AND (:since_created_at IS NULL
                   OR json_extract(metadata,'$.plan_decision.answered_at') >= :since_created_at)
              AND NOT EXISTS (
                SELECT 1 FROM outcome_events e
                WHERE e.event_name=:correction_event
                  AND e.subject_key=:decision_prefix || external_id || ':'
                      || json_extract(metadata,'$.plan_decision.revision')
                  AND e.retracted_at IS NULL AND {version_filter}
              )
            ORDER BY json_extract(metadata,'$.plan_decision.answered_at') DESC,external_id DESC
            LIMIT :scan_limit
            """,
            {**params, "source_kind": "plan_decision_answer"},
        ).fetchall()
        return [
            *(PendingCandidateKey(None, "doc_comment", str(row[0]), None, None, str(row[1])) for row in comment_rows),
            *(PendingCandidateKey(None, "plan_decision_answer", str(row[0]), None, None, str(row[1])) for row in decision_rows),
        ]
    finally:
        db.close()


def hydrate_candidates(keys: list[PendingCandidateKey]) -> list[CorrectionCandidate]:
    """Fetch content only for globally selected candidate keys."""
    from infra.database import get_db

    db = get_db()
    try:
        candidates: list[CorrectionCandidate] = []
        for key in keys:
            if key.source_kind != "message":
                from jobs.corrections_recording import source_candidate

                candidate = source_candidate(key.source_kind, key.source_id)
                if candidate is not None:
                    candidates.append(candidate)
                continue
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
                    "thread": key.thread_id, "created": key.created_at, "message": key.source_id,
                },
            ).fetchall()
            candidates.append(CorrectionCandidate(
                source_kind=key.source_kind, source_id=key.source_id,
                thread_id=str(key.thread_id), project_id=key.project_id,
                content=owner_text(message[1]), created_at=key.created_at,
                context=shape_context(preceding),
            ))
        return candidates
    finally:
        db.close()


class CompletedUnit(NamedTuple):
    """A review unit the scan's recovery re-notes; the last one read is the next page's cursor."""

    completed_at: str
    kind: str
    unit_id: str


def completed_unit_keys(since: str, limit: int, after: CompletedUnit | None = None) -> list[CompletedUnit]:
    """Answered rounds and delivered batches completed since ``since`` with a positive comment and no note.

    Oldest first, strictly after ``after``. A unit whose facts are still pending
    or whose target is gone stays selected until the window passes it; writing
    its note is ``correction_notes.write_unit_notes``'s call.
    """
    from infra.database import get_db
    from plans.public import ANSWERED_ROUNDS_SQL

    positive = """EXISTS (SELECT 1 FROM outcome_events e INDEXED BY idx_outcome_events_subject
                            WHERE e.subject_key=:comment_prefix || {comment} AND e.event_name=:event
                              AND e.retracted_at IS NULL AND json_extract(e.attributes,'$.label')=:label)"""
    db = get_db()
    try:
        rows = db.execute(
            f"""WITH rounds AS ({ANSWERED_ROUNDS_SQL}),
               units AS (
                 SELECT r.cleared_at AS completed_at, 'review-round' AS kind, r.hold_id AS unit_id FROM rounds r
                  WHERE EXISTS (SELECT 1 FROM doc_comments c
                                 WHERE c.artifact_id=r.artifact_id AND c.author_kind='user'
                                   AND c.created_at>r.raised_at AND c.created_at<=r.cleared_at
                                   AND {positive.format(comment='c.id')})
                 UNION ALL
                 SELECT b.updated_at, 'doc-feedback', b.id FROM doc_feedback_batches b
                  WHERE b.author_kind='user' AND b.state='delivered' AND b.updated_at >= :since
                    AND EXISTS (SELECT 1 FROM doc_feedback_batch_items i
                                 WHERE i.batch_id=b.id AND i.event IS NULL
                                   AND {positive.format(comment='i.comment_id')}))
               SELECT completed_at, kind, unit_id FROM units u
                WHERE (:after_at IS NULL OR (completed_at, kind, unit_id) > (:after_at, :after_kind, :after_id))
                  AND NOT EXISTS (SELECT 1 FROM context_change_journal j  -- idx_context_changes_note_key
                                   WHERE j.object_kind='thread_context'
                                     AND json_extract(j.details_json, '$.note_key')=u.kind || ':' || u.unit_id)
                ORDER BY completed_at, kind, unit_id LIMIT :limit""",
            {
                "since": since, "limit": limit, "event": CORRECTION_EVENT, "label": LABEL_CORRECTION,
                "comment_prefix": COMMENT_SUBJECT_PREFIX,
                "after_at": after.completed_at if after else None,
                "after_kind": after.kind if after else None,
                "after_id": after.unit_id if after else None,
            },
        ).fetchall()
        return [CompletedUnit(str(row[0]), str(row[1]), str(row[2])) for row in rows]
    finally:
        db.close()
