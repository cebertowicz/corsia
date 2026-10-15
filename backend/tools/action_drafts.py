"""Drafts of outbound actions (email, calendar invites): create, re-record, claim, mark sent.

The draft's text is a workspace document the person can open. This module records
the revision (sha256 of the document's bytes) and server time of each save made
through the draft tools; ``tools.action_gate`` refuses a send whose document no
longer hashes to the recorded revision, or whose recorded time the person's
newest message does not follow. Nothing else writes these columns.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from doceditor.document_scope import revision as document_revision
from infra.database import write_transaction
from infra.time import utc_now_iso

KINDS = ("email", "invite")


class DraftError(ValueError):
    """A draft call the stored state refuses."""


def create_draft(
    thread_id: str,
    kind: str,
    path: str,
    data: bytes,
    target: dict | None = None,
    *,
    db: sqlite3.Connection | None = None,
) -> dict:
    """Open a draft for ``path`` (workspace-relative) holding ``data``; records its revision."""
    if kind not in KINDS:
        raise DraftError(f"Unknown draft kind: {kind!r}")
    draft_id = uuid.uuid4().hex
    now = utc_now_iso()
    with write_transaction(db, scope="action_draft_create") as conn:
        conn.execute(
            "INSERT INTO action_drafts (id, thread_id, kind, path, target_json,"
            " recorded_revision, recorded_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (draft_id, thread_id, kind, path, json.dumps(target or {}),
             document_revision(data), now, now),
        )
    return {"draft_id": draft_id, "revision": document_revision(data), "recorded_at": now}


def record_draft(
    draft_id: str, data: bytes, *, db: sqlite3.Connection | None = None,
) -> dict:
    """Record ``data`` as the open draft's current bytes, at server time now."""
    revision = document_revision(data)
    now = utc_now_iso()
    with write_transaction(db, scope="action_draft_record") as conn:
        cursor = conn.execute(
            "UPDATE action_drafts SET recorded_revision=?, recorded_at=?"
            " WHERE id=? AND state='open'",
            (revision, now, draft_id),
        )
        if cursor.rowcount != 1:
            raise DraftError("There is no open draft with that id.")
    return {"draft_id": draft_id, "revision": revision, "recorded_at": now}


def _transition(draft_id: str, before: str, after: str, db, **columns) -> None:
    """Move one draft ``before`` -> ``after``; any other current state is refused."""
    sets = ", ".join(["state=?", *(f"{name}=?" for name in columns)])
    with write_transaction(db, scope="action_draft_transition") as conn:
        cursor = conn.execute(
            f"UPDATE action_drafts SET {sets} WHERE id=? AND state=?",
            (after, *columns.values(), draft_id, before),
        )
        if cursor.rowcount != 1:
            raise DraftError(f"The draft is not {before}.")


def claim_for_send(draft_id: str, *, db: sqlite3.Connection | None = None) -> None:
    """Take an open draft for one send, so a parallel or retried call cannot also send it."""
    _transition(draft_id, "open", "sending", db)


def release_claim(draft_id: str, *, db: sqlite3.Connection | None = None) -> None:
    """Return a claimed draft to open after the send provably did not happen."""
    _transition(draft_id, "sending", "open", db)


def mark_sent(
    draft_id: str, external_id: str | None, *, db: sqlite3.Connection | None = None,
) -> None:
    """Close a claimed draft as sent, once, keeping the provider's id for it."""
    _transition(draft_id, "sending", "sent", db, sent_at=utc_now_iso(), external_id=external_id)
