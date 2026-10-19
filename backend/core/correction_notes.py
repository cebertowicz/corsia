"""Agent-only correction triage notes: the one owner of what each note covers and whether it wakes.

A chat message or a decision answer gets its own note. A comment is noted with
its review unit, written once every comment in the unit has a fact:

- ``review-round:<hold>``: the comments made while an ask was open, once the
  person answered it (``plans.public.round_at`` picks the one round);
- ``doc-feedback:<batch>``: any other comment, with its newest delivered batch.

Every trigger (a fact landing by arrival or scan, a batch delivered, an ask
answered) re-derives the unit here, so whichever lands last writes the note.
A new note wakes its recipient unless the caller will not drive it. When this
transaction already queued the recipient an input, the note joins that input's
batch as a passive digest instead of starting a turn of its own.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from core.corrections_recording import (
    ACTIVE_VERSION_SETTING,
    CORRECTION_EVENT,
    LABEL_CORRECTION,
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


def input_floor(db: sqlite3.Connection) -> int:
    """The newest message row; take it when a transaction starts and pass it to the writers below."""
    return int(db.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0])


def _wake(
    db: sqlite3.Connection, target: str, note_key: str, *, floor: int, wakes: list[str] | None,
) -> None:
    """The one wake rule for a new note; ``wakes`` is None when the caller will not drive."""
    from infra.passive_changes import materialize_pending_digest

    if db.execute(
        "SELECT 1 FROM messages WHERE rowid>? AND thread_id=? AND delivery_state='queued' LIMIT 1",
        (floor, target),
    ).fetchone() is not None:
        # That input's digest was cut before this note; ride its batch, whoever drives it.
        materialize_pending_digest(db, target)
        return
    if wakes is None:
        return
    admitted = materialize_pending_digest(db, target, wake_id=f"correction-wake:{note_key}")
    if admitted is not None and not admitted.duplicate:
        wakes.append(target)


def _append_note(
    db: sqlite3.Connection, *, thread_id: str, note_key: str, summary: str, subjects: list[str],
) -> bool:
    """Append one note per durable note key; True only on first insertion."""
    if not _has_target(db, thread_id):
        return False
    # Linear in this thread's journal rows (16 ms at 50k); see the PR's scale budget.
    exists = db.execute(
        """SELECT 1 FROM context_change_journal
           WHERE object_kind='thread_context' AND object_id=?
             AND json_extract(details_json, '$.note_key')=? LIMIT 1""",
        (thread_id, note_key),
    ).fetchone()
    if exists is not None:
        return False
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
    return True


def queue_fact_notes(
    db: sqlite3.Connection, candidate: CorrectionCandidate, label: str, version: ClassifierVersion,
    event_id: str, *, floor: int, wakes: list[str] | None,
) -> None:
    """Write the notes a just-recorded fact completes, in the fact's transaction.

    A scan's fact notes only under the active version, so the activation
    backfill never notes, and a message only while it is still the person's latest word.
    """
    if not candidate.arrived and (
        not _is_active(db, version)
        or (candidate.source_kind == "message" and _newer_owner_message(db, candidate))
    ):
        return
    if candidate.source_kind == "doc_comment":
        queue_comment_notes(db, [candidate.source_id], floor=floor, wakes=wakes)
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
    if _append_note(db, thread_id=candidate.thread_id, note_key=note_key, summary=summary, subjects=subjects):
        # The person's chat message is itself the thread's input; its note rides that conversation.
        if candidate.source_kind != "message":
            _wake(db, candidate.thread_id, note_key, floor=floor, wakes=wakes)


_COMMENT_COLUMNS = """c.id,c.artifact_id,c.anchor_text,c.body,c.created_at,
    (SELECT COUNT(*) FROM outcome_events e WHERE e.event_name=:event
       AND e.subject_key='doc-comment:' || c.id AND e.retracted_at IS NULL) AS facts,
    EXISTS (SELECT 1 FROM outcome_events e WHERE e.event_name=:event
       AND e.subject_key='doc-comment:' || c.id AND e.retracted_at IS NULL
       AND json_extract(e.attributes,'$.label')='correction') AS positive"""


def _comments(db: sqlite3.Connection, where: str, params: dict) -> list[sqlite3.Row]:
    return db.execute(
        f"""SELECT {_COMMENT_COLUMNS} FROM doc_comments c
            WHERE c.author_kind='user' AND {where} ORDER BY c.created_at,c.id""",
        {"event": CORRECTION_EVENT, **params},
    ).fetchall()


def _delivered_batches(db: sqlite3.Connection, comment_ids: Sequence[str]) -> dict[str, sqlite3.Row]:
    """Each comment's newest delivered user batch."""
    rows = db.execute(
        """SELECT i.comment_id,b.id,b.recipient_id,b.updated_at
           FROM doc_feedback_batch_items i JOIN doc_feedback_batches b ON b.id=i.batch_id
           WHERE i.comment_id IN (SELECT value FROM json_each(?)) AND i.event IS NULL
             AND b.state='delivered' AND b.author_kind='user'
           ORDER BY b.updated_at,b.id""",
        (json.dumps(list(comment_ids)),),
    ).fetchall()
    return {str(row["comment_id"]): row for row in rows}


Unit = tuple[str, str]
"""A review unit as ``(kind, id)``: ``("review-round", hold)`` or ``("doc-feedback", batch)``."""


def _unit_of(comment: sqlite3.Row, rounds: list, batches: dict[str, sqlite3.Row]) -> Unit | None:
    """The one membership rule: a comment's review unit, or None while it has none to note yet.

    It belongs to the round of the ask open when it was made; with none open, or
    one withdrawn rather than answered, to its delivered batch.
    """
    from plans.public import round_at

    asked = round_at(rounds, str(comment["created_at"]))
    if asked is not None and asked["cleared_at"] is None:
        return None  # Its round is still open: the answer notes it.
    if asked is not None and asked["answered"]:
        return ("review-round", str(asked["hold_id"]))
    batch = batches.get(str(comment["id"]))
    return ("doc-feedback", str(batch["id"])) if batch is not None else None


def queue_comment_notes(
    db: sqlite3.Connection, comment_ids: Iterable[str], *, floor: int, wakes: list[str] | None,
) -> None:
    """Write the note of each review unit these comments belong to, once it is complete."""
    from plans.public import ask_rounds

    seeds = _comments(db, "c.id IN (SELECT value FROM json_each(:ids))", {"ids": json.dumps(list(comment_ids))})
    batches = _delivered_batches(db, [str(row["id"]) for row in seeds])
    for artifact_id in dict.fromkeys(str(row["artifact_id"]) for row in seeds):
        rounds = ask_rounds(db, artifact_id)
        units = {_unit_of(row, rounds, batches) for row in seeds if row["artifact_id"] == artifact_id}
        _queue_units(db, artifact_id, sorted(unit for unit in units if unit), rounds, floor=floor, wakes=wakes)


def queue_round_notes(
    db: sqlite3.Connection, artifact_id: str, hold_ids: Iterable[str], *, floor: int,
    wakes: list[str] | None,
) -> None:
    """Write the note of each round these just-answered asks ended."""
    from plans.public import ask_rounds

    rounds = ask_rounds(db, artifact_id)
    answered = set(hold_ids)
    units = [("review-round", str(r["hold_id"])) for r in rounds if r["hold_id"] in answered and r["answered"]]
    _queue_units(db, artifact_id, units, rounds, floor=floor, wakes=wakes)


def queue_batch_notes(
    db: sqlite3.Connection, batch_id: str, *, floor: int, wakes: list[str] | None,
) -> None:
    """Write the notes a just-delivered batch completes."""
    rows = db.execute(
        "SELECT comment_id FROM doc_feedback_batch_items WHERE batch_id=? AND event IS NULL", (batch_id,),
    ).fetchall()
    queue_comment_notes(db, [str(row[0]) for row in rows], floor=floor, wakes=wakes)


def _queue_units(
    db: sqlite3.Connection, artifact_id: str, units: Iterable[Unit], rounds: list, *, floor: int,
    wakes: list[str] | None,
) -> None:
    for unit in units:
        kind, unit_id = unit
        if kind == "review-round":
            hold = next(r for r in rounds if r["hold_id"] == unit_id)
            members = _comments(
                db, "c.artifact_id=:artifact AND c.created_at>:since AND c.created_at<=:until",
                {"artifact": artifact_id, "since": hold["raised_at"], "until": hold["cleared_at"]},
            )
        else:
            members = _comments(
                db, "c.id IN (SELECT comment_id FROM doc_feedback_batch_items WHERE batch_id=:batch AND event IS NULL)",
                {"batch": unit_id},
            )
        batches = _delivered_batches(db, [str(row["id"]) for row in members])
        members = [row for row in members if _unit_of(row, rounds, batches) == unit]
        delivered = [batches[str(row["id"])] for row in members if str(row["id"]) in batches]
        if not members or not delivered or any(not row["facts"] for row in members):
            continue  # Not delivered yet, or a fact is pending: that delivery or fact retries.
        recipient = str(max(delivered, key=lambda row: (row["updated_at"], row["id"]))["recipient_id"] or "")
        positive = [row for row in members if row["positive"]]
        note_key = f"{kind}:{unit_id}"
        if positive and _write_comment_note(db, artifact_id, positive, note_key, recipient):
            _wake(db, recipient, note_key, floor=floor, wakes=wakes)


def _write_comment_note(
    db: sqlite3.Connection, artifact_id: str, rows: list[sqlite3.Row], note_key: str, recipient_id: str,
) -> bool:
    """Append the unit note naming ``rows`` to ``recipient_id`` once per ``note_key``."""
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
