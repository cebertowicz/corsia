"""Narrow, cursor-based candidate scan for corrections recording."""

from __future__ import annotations

from dataclasses import dataclass

from core.corrections_input import CONTEXT_HEAD_CHARS, CONTEXT_ROWS, CONTEXT_TAIL_CHARS, owner_text, shape_context
from core.corrections_recording import (
    COMMENT_SUBJECT_PREFIX,
    CORRECTION_EVENT,
    DECISION_SUBJECT_PREFIX,
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


def _before_params(before: tuple[str, ...] | None) -> dict[str, str | None]:
    """Normalize the old two-part cursor while scans move to typed sources."""
    if before is None:
        return {"before_created_at": None, "before_source_kind": None, "before_source_id": None}
    if len(before) == 2:
        return {
            "before_created_at": before[0], "before_source_kind": "message", "before_source_id": before[1],
        }
    return {
        "before_created_at": before[0], "before_source_kind": before[1], "before_source_id": before[2],
    }


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
    before: tuple[str, ...] | None = None, since: str | None = None,
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
    before: tuple[str, ...] | None = None, since: str | None = None,
) -> list[PendingCandidateKey]:
    """Read pending comments and free-text decision answers without their text.

    The source tables remain canonical.  This scan only selects identities, then
    ``candidate_for_source`` rechecks the source and builds the bounded input.
    """
    from infra.database import get_db
    from plans.decision_projection import DECISION_TYPE
    from registry.identity_bridge import OWNER_REGISTRATION_STATES

    db = get_db()
    try:
        version_filter, version_filter_params = version_match_sql("e", version)
        state_params = {f"owner_state_{index}": state for index, state in enumerate(OWNER_REGISTRATION_STATES)}
        params = {
            **version_filter_params,
            **_before_params(before),
            **state_params,
            "correction_event": CORRECTION_EVENT,
            "comment_prefix": COMMENT_SUBJECT_PREFIX,
            "decision_prefix": DECISION_SUBJECT_PREFIX,
            "decision_type": DECISION_TYPE,
            "scan_limit": batch_size,
            "since_created_at": since,
        }
        comment_rows = db.execute(
            f"""
            SELECT DISTINCT c.id,c.created_at
            FROM doc_comments c
            JOIN artifact_registrations r ON r.artifact_id=c.artifact_id
            WHERE c.author_kind='user'
              AND r.relationship_state IN ({','.join(':' + key for key in state_params)})
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
              AND typeof(json_extract(metadata,'$.plan_decision.answer.text'))='text'
              AND length(trim(json_extract(metadata,'$.plan_decision.answer.text'))) > 0
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
                from jobs.corrections_recording import candidate_for_source

                candidate = candidate_for_source(key.source_kind, key.source_id)
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
