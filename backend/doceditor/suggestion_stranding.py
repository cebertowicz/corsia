"""Tell an agent, once, when an edit it did not make strands its pending suggestion.

Every route that stores a document's bytes calls ``note_stranded_suggestions``
inside its write, so no save path is missed and a read never sends anything.
A suggestion's ``stranded_notified_at`` is the once-only mark: set when the
author is told or has made the edit itself, and cleared when the suggestion can
be applied again, so a later stranding is told again.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from doceditor import comments as store
from doceditor.comment_anchoring import anchored_threads
from doceditor.doc_text import read_doc_text
from doceditor.document_scope import UI_THREAD
from doceditor.feedback_suggestion_decisions import stranded_note
from doceditor.note_names import actor_name
from doceditor.quote_anchor import reanchor
from infra.database import write_transaction

log = logging.getLogger(__name__)

_PENDING_AGENT_SUGGESTION = (
    "SELECT 1 FROM doc_comments WHERE artifact_id=? AND parent_id IS NULL AND review=0 AND resolved=0 "
    "AND replacement IS NOT NULL AND decision IS NULL AND author_kind='agent' LIMIT 1"
)


def _applied(row: dict, text: str) -> bool:
    """Whether the text already holds the suggestion's replacement, as when this very write accepted it."""
    if row.get("insert_after"):
        # Accepting an insertion leaves its quoted anchor in place. A missing anchor
        # therefore cannot be explained by applying this suggestion.
        return False
    if row["replacement"]:
        return reanchor(
            text, row["replacement"], from16=row["from_off"], to16=row["to_off"],
            prefix=row.get("anchor_prefix"), suffix=row.get("anchor_suffix"),
        ) is not None
    around = (row.get("anchor_prefix") or "") + (row.get("anchor_suffix") or "")
    return bool(around) and around in text


def note_stranded_suggestions(db, artifact, *, actor_thread_id: str | None) -> None:
    """Tell each pending agent suggestion's author once when ``actor_thread_id``'s edit stranded it."""
    artifact_id = str(artifact["id"])
    if db.execute(_PENDING_AGENT_SUGGESTION, (artifact_id,)).fetchone() is None:
        return
    try:
        text = read_doc_text(artifact["path"])
    except (OSError, ValueError) as exc:  # a save is never failed by a note about it
        log.info("stranded suggestion check skipped for %s: %s", artifact_id, exc)
        return
    threads, _ = anchored_threads(artifact_id, artifact["path"], db, text=text, persist=False)
    pending = [
        row for row in threads
        if row.get("replacement") is not None and not row["resolved"]
        and not row["review"] and row.get("author_thread_id")
    ]
    editor = None if actor_thread_id in (None, UI_THREAD) else actor_thread_id
    for row in pending:
        stranded = bool(row.get("orphaned")) and not _applied(row, text)
        if stranded == bool(row["stranded_notified_at"]):
            continue
        # The mark and its note commit together, whoever owns the connection: a save with no
        # registration holds no transaction, and a note written outside one would be rolled back.
        with write_transaction(db, scope="note_stranded_suggestion") as connection:
            store.set_stranded_notice(
                row["id"], datetime.now(timezone.utc).isoformat() if stranded else None, db=connection,
            )
            if (
                stranded and editor != row["author_thread_id"]
                and connection.execute("SELECT 1 FROM threads WHERE id=?", (row["author_thread_id"],)).fetchone()
            ):
                stranded_note(connection, row, editor=actor_name(connection, editor))
