"""The note that asks a corrected agent to triage, queued with the correction fact.

A recorded correction journals one passive note for the thread it was aimed at,
inside the transaction that writes the fact, so the note exists exactly when the
fact does: one note per flagged fact, whether the person corrected the agent in
chat, in a document comment or in a free-text decision answer. The note rides the thread's next passive digest and never starts a
turn; the digest's own delivery state records whether a turn received it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
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
EARLIER_CORRECTIONS_LIMIT = 3
_REPEAT_SEARCH_LIMIT = 12
_REPEAT_CANDIDATE_LIMIT = 16
_REPEAT_TERM_LIMIT = 1
_STOPWORDS = frozenset({
    "a", "about", "after", "again", "all", "also", "an", "and", "any", "are", "as", "at",
    "be", "because", "been", "being", "but", "by", "can", "could", "did", "do", "does", "for",
    "from", "get", "got", "had", "has", "have", "here", "how", "i", "if", "in", "into", "is",
    "it", "its", "just", "like", "me", "more", "my", "no", "not", "of", "on", "or", "our", "out",
    "so", "still", "that", "the", "their", "them", "then", "there", "these", "they", "this", "to",
    "too", "up", "was", "we", "were", "what", "when", "which", "who", "why", "will", "with", "would",
    "you", "your",
})
# Several comment or answer notes can arrive in one digest (a round of review); each of those asks for one triage.
TRIAGE_TOGETHER = (
    "If several correction notes reach you together, triage them as one: look for one general cause "
    "and re-check the whole artifact or decision, not each comment separately."
)


def _quote(content: str) -> str:
    """The person's words on one line, cut to ``QUOTE_CHARS``."""
    line = " ".join(content.split())
    return line if len(line) <= QUOTE_CHARS else line[: QUOTE_CHARS - 1].rstrip() + "…"


@dataclass(frozen=True)
class EarlierCorrection:
    """One matching prior chat correction, formatted only for the target agent."""

    thread_title: str
    created_at: str
    content: str


def _distinctive_words(content: str) -> tuple[str, ...]:
    """Return stable FTS terms without generic correction language."""
    return tuple(
        word for word in dict.fromkeys(re.findall(r"[\w]+", content.casefold()))
        if len(word) > 2 and word not in _STOPWORDS
    )


def earlier_chat_corrections(db: sqlite3.Connection, candidate: CorrectionCandidate) -> tuple[EarlierCorrection, ...]:
    """Find a few similarly-worded earlier chat corrections for this install.

    Comments and decision answers do not retain their original words in an FTS
    source, so this slice deliberately searches chat corrections only.
    """
    if candidate.source_kind != "message":
        return ()
    words = sorted(_distinctive_words(candidate.content), key=lambda word: (-len(word), word))[:_REPEAT_TERM_LIMIT]
    if not words:
        return ()
    from infra.db_fts import fts5_escape

    query = " OR ".join(fts5_escape(word) for word in words)
    owner_filter, owner_params = owner_message_sql_predicate("m")
    rows = db.execute(
        f"""WITH ranked AS (
                   SELECT rowid,bm25(messages_fts) AS rank FROM messages_fts
                   WHERE messages_fts MATCH :query ORDER BY rank LIMIT :candidate_limit
               )
               SELECT t.title,m.created_at,m.content
               FROM ranked
               JOIN messages m ON m.rowid=ranked.rowid
               JOIN threads t ON t.id=m.thread_id
               WHERE EXISTS (
                     SELECT 1 FROM outcome_events e
                     WHERE e.subject_key='message:' || m.id
                       AND e.event_name=:event AND e.retracted_at IS NULL
                 )
                 AND t.user_id=(SELECT user_id FROM threads WHERE id=:thread)
                 AND (m.created_at < :created OR (m.created_at=:created AND m.id < :message))
                 AND {owner_filter}
               ORDER BY ranked.rank,m.created_at DESC,m.id DESC
               LIMIT :limit""",
        {
            **owner_params,
            "query": query,
            "candidate_limit": _REPEAT_CANDIDATE_LIMIT,
            "event": CORRECTION_EVENT,
            "thread": candidate.thread_id,
            "created": candidate.created_at,
            "message": candidate.source_id,
            "limit": _REPEAT_SEARCH_LIMIT,
        },
    ).fetchall()
    return tuple(
        EarlierCorrection(str(row[0] or "Untitled thread"), str(row[1]), str(row[2]))
        for row in rows[:EARLIER_CORRECTIONS_LIMIT]
    )


def local_clock(created_at: str) -> str:
    """An ISO timestamp in local time like the digest clock; unparseable input unchanged."""
    try:
        sent = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return created_at
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=UTC)
    return f"{sent.astimezone():%Y-%m-%d %H:%M %Z}"


def _note(where: str, words: str, arrived_as: str, earlier: Iterable[EarlierCorrection] = ()) -> str:
    note = (
        f'Correction recorded on your {where}: "{_quote(words)}"\n'
        "Proposed tier: none yet; you set it.\n"
        "Fix the work first. Then triage it with skill `memory-placement` section 4: "
        "set the tier, and for a Miss or Serious answer why it was missed, then watch or act. "
        "The triage is working state, not part of your reply. "
        f"If you already fixed and triaged it when the {arrived_as} arrived, do nothing more."
    )
    earlier = tuple(earlier)
    if not earlier:
        return note
    entries = "\n".join(
        f'- {match.thread_title} ({local_clock(match.created_at)}): "{_quote(match.content)}"'
        for match in earlier
    )
    return (
        f"{note}\nEarlier corrections that may be the same miss:\n{entries}\n"
        "If one is the same kind of miss, this is a repeat: propose a project-wide rule to your "
        "project's conductor rather than only your own file."
    )


def note_text(
    message_id: str, content: str, created_at: str, earlier: Iterable[EarlierCorrection] = (),
) -> str:
    """The note one chat correction puts in its target's next turn."""
    return _note(f"work (message {message_id}, sent {local_clock(created_at)})", content, "message", earlier)


def _source_note_text(candidate: CorrectionCandidate) -> str:
    """The note for a comment (its artifact) or a decision answer (its decision title)."""
    clock = local_clock(candidate.created_at)
    if candidate.source_kind == "doc_comment":
        where = f"artifact {candidate.about} (comment {candidate.source_id}, sent {clock})"
        note = _note(where, candidate.quote or "", "comment")
    else:
        note = _note(f'plan decision "{candidate.about}" (answered {clock})', candidate.quote or "", "answer")
    return note + "\n" + TRIAGE_TOGETHER


def _has_target(db: sqlite3.Connection, thread_id: str) -> bool:
    from doceditor.document_scope import UI_THREAD
    from infra.queries import thread_is_available

    if not thread_id or thread_id == UI_THREAD:
        return False
    row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    return row is not None and thread_is_available(row)


def _first_record(db: sqlite3.Connection, subject: str, event_id: str) -> bool:
    """No other fact names this source, under any classifier version or label."""
    return db.execute(
        "SELECT 1 FROM outcome_events WHERE event_name=? AND subject_key=? AND id<>? LIMIT 1",
        (CORRECTION_EVENT, subject, event_id),
    ).fetchone() is None


def _is_active(db: sqlite3.Connection, version: ClassifierVersion) -> bool:
    from infra.settings import get_setting

    active = get_setting(ACTIVE_VERSION_SETTING, db=db)
    return isinstance(active, dict) and active.get("key") == version_key(version)


def _newer_owner_message(db: sqlite3.Connection, candidate: CorrectionCandidate) -> bool:
    """Whether the person has written in this thread since the correction."""
    owner_filter, params = owner_message_sql_predicate()
    rows = db.execute(
        f"""SELECT m.role,m.content,m.metadata FROM messages m
            WHERE m.thread_id=:thread AND {owner_filter}
              AND (m.created_at > :created OR (m.created_at=:created AND m.id > :message))
            ORDER BY m.created_at,m.id""",
        {**params, "thread": candidate.thread_id, "created": candidate.created_at,
         "message": candidate.source_id},
    )
    return any(
        is_owner_authored_message(role=row[0], content=row[1], metadata=row[2]) for row in rows
    )


def queue_correction_note(
    db: sqlite3.Connection,
    candidate: CorrectionCandidate,
    version: ClassifierVersion,
    event_id: str,
    earlier: Iterable[EarlierCorrection] = (),
) -> int | None:
    """Journal the note for a newly recorded correction, inside the fact's transaction.

    A live arrival always notes. A scan's catch-up notes only a chat message,
    only for the active version and only when the person has not written in that
    thread since, so a backfill never floods an agent with old corrections; a
    scan never notes a comment or a decision answer. Returns the journal seq, or
    None when a gate refuses.
    """
    if not _has_target(db, candidate.thread_id):
        return None
    subject = subject_key(candidate.source_kind, candidate.source_id)
    if not _first_record(db, subject, event_id):
        return None
    if not candidate.arrived and (
        candidate.source_kind != "message" or not _is_active(db, version) or _newer_owner_message(db, candidate)
    ):
        return None
    if candidate.source_kind == "message":
        row = db.execute("SELECT content FROM messages WHERE id=?", (candidate.source_id,)).fetchone()
        summary = note_text(
            candidate.source_id, str(row[0] if row else candidate.content), candidate.created_at, earlier,
        )
    else:
        summary = _source_note_text(candidate)
    from infra.passive_changes import REOFFER_UNWRITTEN, append_thread_context_note

    return append_thread_context_note(
        db,
        thread_id=candidate.thread_id,
        summary=summary,
        # Agent-only: no chip, so the person's transcript shows nothing for it.
        details={
            "chip": None,
            "kind": NOTE_KIND,
            "correction_subject": subject,
            REOFFER_UNWRITTEN: True,
        },
    )


def noted_subjects(metadata: object) -> tuple[str, ...]:
    """The corrections a passive digest's notes name, from its stored metadata."""
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
            subject = details.get("correction_subject")
            if isinstance(subject, str) and subject:
                subjects.append(subject)
    return tuple(subjects)
