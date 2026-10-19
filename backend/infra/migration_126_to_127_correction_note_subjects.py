"""Schema 126 -> 127: every correction note names its corrections one way, and two read indexes.

A chat-correction note journaled before notes covered comments and decision
answers stored one ``correction_subject`` string. Notes now store a
``note_key`` and a ``correction_subjects`` list, and only that is read, so this
step rewrites the old shape once wherever it is stored: the journal rows not
yet delivered (or offered again until their triage is written), and the
context-digest inputs that already carry them. An old note's key is its
subject, which is the key a chat-message note is written under today.

It then adds the index on a note's key and the index on an artifact's user
asks, which the note path reads on the answer and delivery requests.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import json
import sqlite3

from infra.context_change_schema import NOTE_KEY_INDEX_SQL
from infra.plan_ask_schema import ASK_ROUNDS_INDEX_SQL

# Frozen at this step: the note kind and the key it replaces, as written before 127.
_NOTE_KIND = "correction_note"
_OLD_KEY = "correction_subject"
_OLD_KEY_PATTERN = f'"{_OLD_KEY}"'  # the quoted key, so the plural never matches


def _rewritten(details: object) -> bool:
    """Move one note's singular subject to the plural contract in place; False when not an old note."""
    if not isinstance(details, dict) or details.get("kind") != _NOTE_KIND or _OLD_KEY not in details:
        return False
    subject = details.pop(_OLD_KEY)
    subjects = [subject] if isinstance(subject, str) and subject else []
    details["correction_subjects"] = subjects
    if subjects:
        details.setdefault("note_key", subject)
    return True


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _rewrite_journal(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT seq,details_json FROM context_change_journal "
        "WHERE object_kind='thread_context' AND json_valid(details_json) AND instr(details_json,?)>0",
        (_OLD_KEY_PATTERN,),
    ).fetchall()
    for seq, raw in rows:
        details = json.loads(raw)
        if _rewritten(details):
            conn.execute("UPDATE context_change_journal SET details_json=? WHERE seq=?", (json.dumps(details), seq))


def _rewrite_digests(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT rowid,metadata FROM messages "
        "WHERE role='system' AND json_valid(metadata) AND instr(metadata,?)>0",
        (_OLD_KEY_PATTERN,),
    ).fetchall()
    for rowid, raw in rows:
        metadata = json.loads(raw)
        events = metadata.get("context_events") if isinstance(metadata, dict) else None
        changed = [
            _rewritten(event.get("details")) for event in events or () if isinstance(event, dict)
        ]
        if any(changed):
            conn.execute("UPDATE messages SET metadata=? WHERE rowid=?", (json.dumps(metadata), rowid))


def migrate_126_to_127(conn: sqlite3.Connection) -> None:
    """Rewrite old-shape correction notes, then add the note-key and user-ask indexes."""
    if _has_table(conn, "context_change_journal"):
        _rewrite_journal(conn)
        conn.execute(NOTE_KEY_INDEX_SQL)
    if _has_table(conn, "messages"):
        _rewrite_digests(conn)
    if _has_table(conn, "plan_node_holds"):
        conn.execute(ASK_ROUNDS_INDEX_SQL)
