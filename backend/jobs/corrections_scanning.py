"""Narrow, cursor-based candidate scan for corrections recording."""

from __future__ import annotations

import json
from dataclasses import dataclass

from core.corrections_input import (
    CONTEXT_HEAD_CHARS, CONTEXT_ROWS, CONTEXT_TAIL_CHARS, agent_context, owner_text, shape_context,
)
from core.corrections_recording import (
    COMMENT_SUBJECT_PREFIX,
    CORRECTION_EVENT,
    MESSAGE_SUBJECT_PREFIX,
    SOURCE_KIND_DECISION_ANSWER,
    SOURCE_KIND_DOC_COMMENT,
    SOURCE_KIND_MESSAGE,
    ClassifierVersion,
    CorrectionCandidate,
    decision_answer_arrived_at_sql,
    decision_answer_source_id,
    decision_answer_subject_key_sql,
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
                "source_kind": SOURCE_KIND_MESSAGE,
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
                rowid=row[0], source_kind=SOURCE_KIND_MESSAGE, source_id=row[1], thread_id=row[2], project_id=row[3], created_at=row[4],
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
            "decision_type": DECISION_TYPE,
            "source_kind": SOURCE_KIND_DECISION_ANSWER,
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
              -- A range on idx_doc_comments_user_created: the read stops at the window's start.
              AND c.created_at >= COALESCE(:since_created_at, '')
              AND NOT EXISTS (
                SELECT 1 FROM outcome_events e
                WHERE e.event_name=:correction_event
                  AND e.subject_key=:comment_prefix || c.id AND e.retracted_at IS NULL
                  AND {version_filter}
              )
            ORDER BY c.created_at DESC,c.id DESC LIMIT :scan_limit
            """,
            {**params, "source_kind": SOURCE_KIND_DOC_COMMENT},
        ).fetchall()
        decision_created_at = decision_answer_arrived_at_sql("d")
        decision_rows = db.execute(
            f"""
            SELECT d.external_id,{decision_created_at}
            FROM artifact_identities d
            WHERE d.integration='local' AND d.type=:decision_type AND d.state='active'
              AND json_extract(d.metadata,'$.plan_decision.state')='answered'
              -- A coarse prefilter, never narrower than Plans' ``answer_note``, which
              -- ``source_candidate`` applies to each selected answer.
              AND COALESCE(json_extract(d.metadata,'$.plan_decision.answer.text'),'')<>''
              AND {_before_clause(SOURCE_KIND_DECISION_ANSWER, 'd.external_id', decision_created_at)}
              AND (:since_created_at IS NULL OR {decision_created_at} >= :since_created_at)
              AND NOT EXISTS (
                SELECT 1 FROM outcome_events e
                WHERE e.event_name=:correction_event
                  AND e.subject_key={decision_answer_subject_key_sql('d')}
                  AND e.retracted_at IS NULL AND {version_filter}
              )
            ORDER BY {decision_created_at} DESC,d.external_id DESC LIMIT :scan_limit
            """,
            params,
        ).fetchall()
        return [
            *(PendingCandidateKey(None, SOURCE_KIND_DOC_COMMENT, str(row[0]), None, None, str(row[1])) for row in comment_rows),
            *(PendingCandidateKey(None, SOURCE_KIND_DECISION_ANSWER, str(row[0]), None, None, str(row[1])) for row in decision_rows),
        ]
    finally:
        db.close()


def hydrate_candidates(keys: list[PendingCandidateKey]) -> list[CorrectionCandidate]:
    """Fetch content only for globally selected candidate keys."""
    from infra.database import get_db

    db = get_db()
    try:
        candidates = (
            _message_candidate(db, key) if key.source_kind == SOURCE_KIND_MESSAGE else source_candidate(db, key.source_kind, key.source_id)
            for key in keys
        )
        return [candidate for candidate in candidates if candidate is not None]
    finally:
        db.close()


def _message_candidate(db, key: PendingCandidateKey) -> CorrectionCandidate | None:
    message = db.execute(
        "SELECT role,content,metadata FROM messages WHERE rowid=?", (key.rowid,)
    ).fetchone()
    if not message or not is_owner_authored_message(
        role=message[0], content=message[1], metadata=message[2],
    ):
        return None
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
    return CorrectionCandidate(
        source_kind=key.source_kind, source_id=key.source_id,
        thread_id=str(key.thread_id), project_id=key.project_id,
        content=owner_text(message[1]), created_at=key.created_at,
        context=shape_context(preceding),
    )


def source_candidate(db, source_kind: str, source_id: str) -> CorrectionCandidate | None:
    """Read one comment or decision answer as classifier input; None when it is no candidate.

    The owner segment is the person's words alone. What the agent put to them
    (the commented excerpt, or the decision's question and options) is one
    ``Agent`` context row before it, the shape the classifier was trained on.
    """
    if source_kind == SOURCE_KIND_DOC_COMMENT:
        return _comment_candidate(db, source_id)
    if source_kind == SOURCE_KIND_DECISION_ANSWER:
        return _decision_candidate(db, source_id)
    raise ValueError(f"unknown correction source kind: {source_kind}")


def _project_of(db, thread_id: str) -> str | None:
    row = db.execute("SELECT project_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    return row[0] if row else None


def _comment_candidate(db, comment_id: str) -> CorrectionCandidate | None:
    from core.correction_targets import comment_target_sql
    from registry.models import get_artifact

    row = db.execute(
        f"""SELECT c.id,c.artifact_id,c.anchor_text,c.body,c.created_at,{comment_target_sql('c')} AS target
            FROM doc_comments c WHERE c.id=? AND c.author_kind='user'""", (comment_id,),
    ).fetchone()
    if row is None or not row["target"]:
        return None  # No agent was or will be handed it: the scan never selects it either.
    target, body = str(row["target"]), str(row["body"])
    artifact = get_artifact(str(row["artifact_id"]), db=db) or {}
    return CorrectionCandidate(
        SOURCE_KIND_DOC_COMMENT, str(row["id"]), target, _project_of(db, target), owner_text(body), str(row["created_at"]),
        context=agent_context(row["anchor_text"] or ""),
        quote=body, about=str(artifact.get("title") or artifact.get("path") or row["artifact_id"]),
    )


def _decision_candidate(db, decision_id: str) -> CorrectionCandidate | None:
    from plans.public import answer_note, decision_identity_row

    row = decision_identity_row(db, decision_id)
    try:
        body = json.loads(row["metadata"]).get("plan_decision") if row is not None else None
    except (TypeError, ValueError, AttributeError):
        return None
    if not isinstance(body, dict) or body.get("state") != "answered":
        return None
    text = answer_note(body.get("answer"))  # Plans' own test of whether an answer carries the person's words.
    thread_id, revision, answered_at = body.get("thread_id"), body.get("revision"), body.get("answered_at")
    if not text or not thread_id or not isinstance(thread_id, str) or revision is None or not answered_at:
        return None
    return CorrectionCandidate(
        SOURCE_KIND_DECISION_ANSWER, decision_answer_source_id(decision_id, revision), thread_id, _project_of(db, thread_id),
        owner_text(text), str(answered_at), context=agent_context(_question(body)),
        quote=text, about=str(body.get("title") or decision_id),
    )


def _question(body: dict) -> str:
    """The decision as the agent put it to the person: title, context, trade-off, then each option."""
    listed = body.get("options")
    options = [option for option in listed if isinstance(option, dict)] if isinstance(listed, list) else []
    lines = [str(body.get(field) or "") for field in ("title", "context", "tradeoff")]
    for option in options:
        said = ": ".join(str(part) for part in (option.get("label"), option.get("detail")) if part)
        lines.append(f"- {said}" if said else "")
    return "\n".join(line for line in lines if line)
