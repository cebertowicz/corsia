"""Agent-only correction triage notes: what each note covers. Only the corrections worker writes them.

A chat message or a decision answer gets its own note. A comment is noted with
its review unit, written once every comment in the unit has a fact:

- ``review-round:<hold>``: the comments made while an ask was open, once the
  person answered it (``plans.public.round_at`` picks the one round);
- ``doc-feedback:<batch>``: any other comment, with the delivered batch that
  handed it to its target (``core.correction_targets``).

A unit's note goes to each target its comments were handed to. The worker
writes in its own write transaction, on one of two events, and re-derives the
unit here each time, so whichever lands last writes the note (``note_key``
makes it once):

- a fact lands (``write_fact_notes``, in the fact's transaction);
- a unit completes (``write_unit_notes``): the unit key an answer or a
  delivery enqueues after its own commit, or one the scan recovers.

A failing note fails that worker step, so it has one failure policy: the
fact rolls back and the scan retries it; a unit key's scan recovers it.
No note starts a turn. It rides its recipient's next admitted input as a
passive digest, exactly as a chat note does.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from core.correction_targets import comment_target_sql
from core.corrections_recording import (
    CORRECTION_EVENT,
    LABEL_CORRECTION,
    ClassifierVersion,
    CorrectionCandidate,
    active_since,
    is_owner_authored_message,
    owner_message_sql_predicate,
    subject_key,
)

NOTE_KIND = "correction_note"
QUOTE_CHARS = 160


def _quote(content: str) -> str:
    """The person's words on one line, cut to ``QUOTE_CHARS``."""
    line = " ".join(content.split())
    return line if len(line) <= QUOTE_CHARS else line[: QUOTE_CHARS - 1].rstrip() + "…"


def _instant(stamp: str) -> datetime:
    """A stored timestamp as an aware instant; a naive one is UTC."""
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _local_clock(created_at: str) -> str:
    """When the correction was sent, in local time like the digest clock."""
    try:
        sent = _instant(created_at)
    except ValueError:
        return created_at
    return f"{sent.astimezone():%Y-%m-%d %H:%M %Z}"


def note_text(message_id: str, content: str, created_at: str) -> str:
    """The note for one correction the person sent as a chat message."""
    return (
        f"Correction recorded on your work (message {message_id}, sent "
        f'{_local_clock(created_at)}): "{_quote(content)}"\n'
        "Proposed tier: none yet; you set it.\n"
        "Fix the work first. Then triage it with skill `memory-placement` section 4: "
        "set the tier, and for a Miss or Serious answer why it was missed, then watch or act. "
        "The triage is working state, not part of your reply. "
        "If you already fixed and triaged it when the message arrived, do nothing more."
    )


def _decision_note_text(candidate: CorrectionCandidate) -> str:
    return (
        f"Correction recorded on your plan decision {candidate.source_ref}, sent "
        f"{_local_clock(candidate.created_at)}: “{_quote(candidate.quote or '')}”\n"
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


def _scan_fact_notes(db: sqlite3.Connection, candidate: CorrectionCandidate, version: ClassifierVersion) -> bool:
    """Whether a scan's fact may note: its source was made after its version became active.

    So neither an activation backfill nor the first scan after an upgrade notes
    history. A message must also still be the person's latest word.
    """
    since = active_since(db, version)
    try:
        if since is None or _instant(candidate.created_at) <= _instant(since):
            return False
    except ValueError:
        return False  # An unreadable time is never proof the source is new.
    return candidate.source_kind != "message" or not _newer_owner_message(db, candidate)


def _append_note(
    db: sqlite3.Connection, *, thread_id: str, note_key: str, summary: str, subjects: list[str],
) -> bool:
    """Append one note per durable note key; True only on first insertion."""
    if not _has_target(db, thread_id):
        return False
    exists = db.execute(  # one row of idx_context_changes_note_key
        """SELECT 1 FROM context_change_journal
           WHERE object_kind='thread_context' AND object_id=?
             AND json_extract(details_json, '$.note_key')=? LIMIT 1""",
        (thread_id, note_key),
    ).fetchone()
    if exists is not None:
        return False
    from infra.passive_changes import REOFFER_UNWRITTEN, append_thread_context_note

    append_thread_context_note(db, thread_id=thread_id, summary=summary, details={
        "chip": None, "kind": NOTE_KIND, "note_key": note_key, "correction_subjects": subjects, REOFFER_UNWRITTEN: True,
    })
    return True


def write_fact_notes(
    db: sqlite3.Connection, candidate: CorrectionCandidate, label: str, version: ClassifierVersion,
    event_id: str,
) -> None:
    """Write the notes a just-recorded fact completes, in the fact's transaction."""
    if not candidate.arrived and not _scan_fact_notes(db, candidate, version):
        return
    if candidate.source_kind == "doc_comment":
        write_comment_notes(db, [candidate.source_id])
        return
    if label != LABEL_CORRECTION or not _first_record(db, candidate, event_id):
        return
    if candidate.source_kind == "plan_decision_answer":
        note_key = f"decision-answer:{candidate.source_id}"
        summary = _decision_note_text(candidate)
    else:
        note_key = f"message:{candidate.source_id}"
        summary = note_text(candidate.source_id, candidate.content, candidate.created_at)
    subjects = [subject_key(candidate.source_kind, candidate.source_id)]
    _append_note(db, thread_id=candidate.thread_id, note_key=note_key, summary=summary, subjects=subjects)


@dataclass(frozen=True)
class _Comment:
    """One user comment as the note path reads it."""

    id: str
    artifact_id: str
    anchor_text: str | None
    body: str
    created_at: str
    target: str | None
    batch_id: str | None  # the delivered batch that handed it to ``target``
    recorded: bool
    positive: bool


def _fact_states(db: sqlite3.Connection, comment_ids: Sequence[str]) -> dict[str, tuple[bool, bool]]:
    """Each comment's ``(recorded, positive)`` from its live facts, in one indexed read."""
    keys = {subject_key("doc_comment", comment_id): comment_id for comment_id in comment_ids}
    rows = db.execute(  # by subject: the planner would otherwise read every correction fact by name
        """SELECT e.subject_key, MAX(json_extract(e.attributes,'$.label')=:label)
             FROM outcome_events e INDEXED BY idx_outcome_events_subject
            WHERE e.subject_key IN (SELECT value FROM json_each(:keys))
              AND e.event_name=:event AND e.retracted_at IS NULL
            GROUP BY e.subject_key""",
        {"keys": json.dumps(list(keys)), "event": CORRECTION_EVENT, "label": LABEL_CORRECTION},
    ).fetchall()
    return {keys[str(row[0])]: (True, bool(row[1])) for row in rows}


def _comments(db: sqlite3.Connection, where: str, params: dict) -> list[_Comment]:
    """The user comments ``where`` selects, oldest first, with target, handing batch and facts."""
    rows = db.execute(
        f"""WITH picked AS (
              SELECT c.id,c.artifact_id,c.anchor_text,c.body,c.created_at,
                     {comment_target_sql('c')} AS target
                FROM doc_comments c WHERE c.author_kind='user' AND {where})
            SELECT p.*,
                   (SELECT b.id FROM doc_feedback_batch_items i
                      JOIN doc_feedback_batches b ON b.id=i.batch_id
                     WHERE i.comment_id=p.id AND i.event IS NULL AND b.author_kind='user'
                       AND b.state='delivered' AND b.recipient_id=p.target
                     ORDER BY b.updated_at DESC, b.id DESC LIMIT 1) AS batch_id
              FROM picked p ORDER BY p.created_at,p.id""",
        params,
    ).fetchall()
    facts = _fact_states(db, [str(row["id"]) for row in rows])
    return [
        _Comment(
            str(row["id"]), str(row["artifact_id"]), row["anchor_text"], str(row["body"]),
            str(row["created_at"]), row["target"], row["batch_id"], *facts.get(str(row["id"]), (False, False)),
        )
        for row in rows
    ]


Unit = tuple[str, str]
"""A review unit as ``(kind, id)``: ``("review-round", hold)`` or ``("doc-feedback", batch)``."""


def _unit_of(comment: _Comment, rounds: list) -> Unit | None:
    """The one membership rule: a comment's review unit, or None while it has none to note yet.

    It belongs to the round of the ask open when it was made; with none open, or
    one withdrawn rather than answered, to the batch that handed it to its target.
    """
    from plans.public import round_at

    asked = round_at(rounds, comment.created_at)
    if asked is not None and asked["cleared_at"] is None:
        return None  # Its round is still open: the answer notes it.
    if asked is not None and asked["answered"]:
        return ("review-round", str(asked["hold_id"]))
    return ("doc-feedback", comment.batch_id) if comment.batch_id else None


def write_comment_notes(db: sqlite3.Connection, comment_ids: Iterable[str]) -> None:
    """Write the note of each review unit these comments belong to, once it is complete."""
    from plans.public import ask_rounds

    seeds = _comments(db, "c.id IN (SELECT value FROM json_each(:ids))", {"ids": json.dumps(list(comment_ids))})
    for artifact_id in dict.fromkeys(comment.artifact_id for comment in seeds):
        rounds = ask_rounds(db, artifact_id)
        units = {_unit_of(comment, rounds) for comment in seeds if comment.artifact_id == artifact_id}
        _write_units(db, artifact_id, sorted(unit for unit in units if unit), rounds)


def write_unit_notes(db: sqlite3.Connection, kind: str, unit_id: str) -> None:
    """Write the notes one completed unit owes: an answered round (a hold) or a delivered batch."""
    from plans.public import ask_rounds, asked_artifact

    if kind == "doc-feedback":
        sql = "SELECT comment_id FROM doc_feedback_batch_items WHERE batch_id=? AND event IS NULL"
        write_comment_notes(db, [str(row[0]) for row in db.execute(sql, (unit_id,)).fetchall()])
        return
    if kind != "review-round":
        raise ValueError(f"unknown review unit kind: {kind}")
    artifact_id = asked_artifact(db, unit_id)
    if artifact_id is not None:
        rounds = ask_rounds(db, artifact_id)
        if any(r["hold_id"] == unit_id and r["answered"] for r in rounds):
            _write_units(db, artifact_id, [(kind, unit_id)], rounds)


def _write_units(db: sqlite3.Connection, artifact_id: str, units: Iterable[Unit], rounds: list) -> None:
    for unit in units:
        kind, unit_id = unit
        if kind == "review-round":
            hold = next((r for r in rounds if r["hold_id"] == unit_id), None)
            if hold is None:
                continue  # Older than the rounds read: its comments are noted with their batch.
            members = _comments(
                db, "c.artifact_id=:artifact AND c.created_at>:since AND c.created_at<=:until",
                {"artifact": artifact_id, "since": hold["raised_at"], "until": hold["cleared_at"]},
            )
        else:
            members = _comments(
                db, "c.id IN (SELECT comment_id FROM doc_feedback_batch_items WHERE batch_id=:batch AND event IS NULL)",
                {"batch": unit_id},
            )
        members = [comment for comment in members if _unit_of(comment, rounds) == unit]
        if not members or not all(comment.recorded for comment in members):
            continue  # A fact is pending: that fact retries.
        note_key = f"{kind}:{unit_id}"
        for target in dict.fromkeys(comment.target for comment in members if comment.target):
            handed = [comment for comment in members if comment.target == target]
            positive = [comment for comment in handed if comment.positive]
            if not any(comment.batch_id for comment in handed):
                continue  # Not handed to the target yet: that delivery retries.
            if positive:
                _write_comment_note(db, artifact_id, positive, note_key, target)


def _write_comment_note(
    db: sqlite3.Connection, artifact_id: str, comments: list[_Comment], note_key: str, recipient_id: str,
) -> bool:
    """Append the unit note naming ``comments`` to ``recipient_id`` once per ``note_key``."""
    from registry.models import get_artifact

    artifact = get_artifact(artifact_id, db=db) or {}
    path = artifact.get("path") or artifact_id
    lines = [
        f"Correction batch recorded on your artifact {artifact_id} ({path}; {len(comments)} comment{'' if len(comments) == 1 else 's'}, "
        f"sent {_local_clock(comments[0].created_at)}–{_local_clock(comments[-1].created_at)}):"
    ]
    lines.extend(f"- “{_quote(comment.anchor_text or '(no anchor)')}” — “{_quote(comment.body)}”" for comment in comments)
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
        subjects=[subject_key("doc_comment", comment.id) for comment in comments],
    )


def noted_subjects(metadata: object) -> tuple[str, ...]:
    """The corrections a passive digest's notes name, from their ``correction_subjects``."""
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
