"""Agent-only correction triage notes and their idempotency rules."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime

from core.corrections_recording import (
    ACTIVE_VERSION_SETTING,
    CORRECTION_EVENT,
    ClassifierVersion,
    CorrectionCandidate,
    is_owner_authored_message,
    owner_message_sql_predicate,
    subject_key,
    version_key,
)

NOTE_KIND = "correction_note"
QUOTE_CHARS = 160


def _quote(content: str) -> str:
    """The person's words on one line, cut to ``QUOTE_CHARS``."""
    line = " ".join(content.split())
    return line if len(line) <= QUOTE_CHARS else line[: QUOTE_CHARS - 1].rstrip() + "…"


def _local_clock(created_at: str) -> str:
    """When the correction was sent, in local time like the digest clock."""
    try:
        sent = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return created_at
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=UTC)
    return f"{sent.astimezone():%Y-%m-%d %H:%M %Z}"


def note_text(message_id: str, content: str, created_at: str) -> str:
    """The existing singular chat-correction note."""
    return (
        f"Correction recorded on your work (feedback {message_id}, sent "
        f'{_local_clock(created_at)}): "{_quote(content)}"\n'
        "Proposed tier: none yet; you set it.\n"
        "Fix the work first. Then triage it with skill `memory-placement` section 4: "
        "set the tier, and for a Miss or Serious answer why it was missed, then watch or act. "
        "The triage is working state, not part of your reply. "
        "If you already fixed and triaged it when the message arrived, do nothing more."
    )


def _decision_note_text(candidate: CorrectionCandidate) -> str:
    decision_id = candidate.source_id.rsplit(":", 1)[0]
    answer = candidate.content.rsplit("Owner answer:", 1)[-1]
    return (
        f"Correction recorded on your plan decision {decision_id}, sent "
        f"{_local_clock(candidate.created_at)}: “{_quote(answer)}”\n"
        "Proposed tier: none yet; you set it.\n"
        "Fix the work first. Then triage it with skill `memory-placement` section 4: "
        "set the tier, and for a Miss or Serious answer why it was missed, then watch or act. "
        "The triage is working state, not part of your reply. If you already fixed and triaged it "
        "when the answer arrived, do nothing more."
    )


def _has_target(db: sqlite3.Connection, thread_id: str) -> bool:
    from doceditor.document_scope import UI_THREAD
    from infra.queries import thread_is_available

    if not thread_id or thread_id == UI_THREAD:
        return False
    row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    return row is not None and thread_is_available(row)


def _first_record(db: sqlite3.Connection, candidate: CorrectionCandidate, event_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM outcome_events WHERE event_name=? AND subject_key=? AND id<>? LIMIT 1",
        (CORRECTION_EVENT, subject_key(candidate.source_kind, candidate.source_id), event_id),
    ).fetchone() is None


def _is_active(db: sqlite3.Connection, version: ClassifierVersion) -> bool:
    from infra.settings import get_setting

    active = get_setting(ACTIVE_VERSION_SETTING, db=db)
    return isinstance(active, dict) and active.get("key") == version_key(version)


def _newer_owner_message(db: sqlite3.Connection, candidate: CorrectionCandidate) -> bool:
    owner_filter, params = owner_message_sql_predicate()
    rows = db.execute(
        f"""SELECT m.role,m.content,m.metadata FROM messages m
            WHERE m.thread_id=:thread AND {owner_filter}
              AND (m.created_at > :created OR (m.created_at=:created AND m.id > :source))
            ORDER BY m.created_at,m.id""",
        {**params, "thread": candidate.thread_id, "created": candidate.created_at,
         "source": candidate.source_id},
    )
    return any(is_owner_authored_message(role=row[0], content=row[1], metadata=row[2]) for row in rows)


def _append_note(
    db: sqlite3.Connection, *, thread_id: str, note_key: str, summary: str, subjects: list[str],
) -> str | None:
    """Append one note per durable note key, returning it only on first insertion."""
    if not _has_target(db, thread_id):
        return None
    exists = db.execute(
        """SELECT 1 FROM context_change_journal
           WHERE object_kind='thread_context' AND object_id=?
             AND json_extract(details_json, '$.note_key')=? LIMIT 1""",
        (thread_id, note_key),
    ).fetchone()
    if exists is not None:
        return None
    from infra.passive_changes import REOFFER_UNWRITTEN, append_thread_context_note

    append_thread_context_note(
        db,
        thread_id=thread_id,
        summary=summary,
        details={
            "chip": None,
            "kind": NOTE_KIND,
            "note_key": note_key,
            "correction_subjects": subjects,
            REOFFER_UNWRITTEN: True,
        },
    )
    return note_key


def queue_correction_note(
    db: sqlite3.Connection,
    candidate: CorrectionCandidate,
    version: ClassifierVersion,
    event_id: str,
) -> str | None:
    """Journal a singular note for chat or decision sources, never per comment."""
    if candidate.source_kind == "doc_comment" or not _first_record(db, candidate, event_id):
        return None
    if candidate.source_kind != "message" and not candidate.arrived:
        return None  # A scan never notes a decision answer; only its arrival does.
    if not candidate.arrived and (not _is_active(db, version) or _newer_owner_message(db, candidate)):
        return None
    subject = subject_key(candidate.source_kind, candidate.source_id)
    if candidate.source_kind == "plan_decision_answer":
        return _append_note(
            db,
            thread_id=candidate.thread_id,
            note_key=f"decision-answer:{candidate.source_id}",
            summary=_decision_note_text(candidate),
            subjects=[subject],
        )
    return _append_note(
        db,
        thread_id=candidate.thread_id,
        note_key=f"message:{candidate.source_id}",
        summary=note_text(candidate.source_id, candidate.content, candidate.created_at),
        subjects=[subject],
    )


def queue_review_round_note(
    db: sqlite3.Connection, *, artifact_id: str, note_key: str, raised_at: str, recipient_id: str,
) -> str | None:
    """Write one note for every positive user comment after ``raised_at``, once per ``note_key``."""
    rows = db.execute(
        """SELECT c.id,c.anchor_text,c.body,c.created_at
           FROM doc_comments c
           WHERE c.artifact_id=? AND c.author_kind='user' AND c.created_at>?
             AND EXISTS (
                 SELECT 1 FROM outcome_events e WHERE e.event_name=?
                   AND e.subject_key='doc-comment:' || c.id AND e.retracted_at IS NULL
                   AND json_extract(e.attributes,'$.label')='correction'
             ) ORDER BY c.created_at,c.id""",
        (artifact_id, raised_at, CORRECTION_EVENT),
    ).fetchall()
    if not rows:
        return None
    from registry.models import get_artifact

    artifact = get_artifact(artifact_id, db=db) or {}
    path = artifact.get("path") or artifact_id
    lines = [
        f"Correction batch recorded on your artifact {artifact_id} ({path}; {len(rows)} comments, "
        f"sent {_local_clock(str(rows[0]['created_at']))}–{_local_clock(str(rows[-1]['created_at']))}):"
    ]
    lines.extend(f"- “{_quote(str(row['anchor_text'] or '(no anchor)'))}” — “{_quote(str(row['body']))}”" for row in rows)
    lines.extend((
        "Proposed tier: none yet; you set it.",
        "Fix the work first. Then triage the general cause across these comments with skill `memory-placement` section 4. "
        "Re-check the whole artifact for untagged instances of that cause, not only the quoted spans. "
        "The triage is working state, not part of your reply.",
    ))
    return _append_note(
        db,
        thread_id=recipient_id,
        note_key=note_key,
        summary="\n".join(lines),
        subjects=[f"doc-comment:{row['id']}" for row in rows],
    )


def queue_feedback_batch_note(db: sqlite3.Connection, batch) -> str | None:
    """Write the delivered lone-comment batch note for its stored owner recipient."""
    values = dict(batch)
    if values.get("author_kind") != "user" or not values.get("recipient_id"):
        return None
    if values.get("recipient_id") == "ui":
        return None
    rows = db.execute(
        """SELECT c.id,c.anchor_text,c.body,c.created_at
           FROM doc_feedback_batch_items i JOIN doc_comments c ON c.id=i.comment_id
           WHERE i.batch_id=? AND i.event IS NULL AND c.author_kind='user'
             AND EXISTS (
                 SELECT 1 FROM outcome_events e WHERE e.event_name=?
                   AND e.subject_key='doc-comment:' || c.id AND e.retracted_at IS NULL
                   AND json_extract(e.attributes,'$.label')='correction'
             ) ORDER BY c.created_at,c.id""",
        (values["id"], CORRECTION_EVENT),
    ).fetchall()
    if not rows:
        return None
    # A raised ask owns this feedback as a review round; its answer writes that note.
    from plans.public import raised_asks
    if raised_asks(db, str(values["artifact_id"])):
        return None
    return queue_review_round_note(
        db,
        artifact_id=str(values["artifact_id"]),
        note_key=f"doc-feedback:{values['id']}",
        raised_at="",
        recipient_id=str(values["recipient_id"]),
    )


def noted_subjects(metadata: object) -> tuple[str, ...]:
    """The corrections a passive digest's notes name from its plural contract."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return ()
    if not isinstance(metadata, dict) or metadata.get("event_type") != "context.digest":
        return ()
    events: Iterable[object] = metadata.get("context_events") or ()
    subjects: list[str] = []
    for event in events:
        details = event.get("details") if isinstance(event, dict) else None
        if isinstance(details, dict) and details.get("kind") == NOTE_KIND:
            plural = details.get("correction_subjects")
            if isinstance(plural, list):
                subjects.extend(subject for subject in plural if isinstance(subject, str) and subject)
    return tuple(subjects)
