"""The fresh-read record: each read, the passed bytes, and what each document shows the person.

Every write here is a short row write; no read ever runs inside one (the read is a
model call that can take minutes, and it would hold the write lock that long).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from infra.database import get_db, write_transaction
from infra.time import utc_now_iso

PASSED, FAILED, HELD, PERSON_EDIT = "passed", "failed", "held", "person_edit"


def sha256_text(text: str) -> str:
    """The binding hash: sha256 of the exact text the person sees, as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ReadRecord:
    """One read's outcome as stored; ``documents`` are (artifact_id, sha256, bytes) it passed."""

    kind: str
    item_ref: str
    sha256: str
    verdict: str
    writer_thread_id: str | None = None
    node_id: str | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    reader_model: str | None = None
    input_cap: int | None = None
    messages_cut_from: str | None = None
    elapsed_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    cost_source: str | None = None
    documents: tuple[tuple[str, str, bytes], ...] = ()


def record(read: ReadRecord, db=None) -> str:
    """Store one read; on a pass, move each document it passed to the passed version. Returns its id.

    With ``db`` in a transaction, the record lands inside it (a person's live edit, with its registration).
    """
    read_id = uuid.uuid4().hex
    now = utc_now_iso()
    with write_transaction(db, scope="fresh_read_record") as db:
        db.execute(
            """INSERT INTO fresh_reads (id, kind, item_ref, sha256, verdict, findings, attempts,
                   writer_thread_id, node_id, reader_model, input_cap, messages_cut_from, elapsed_ms,
                   prompt_tokens, completion_tokens, cost_usd, cost_source, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (read_id, read.kind, read.item_ref, read.sha256, read.verdict,
             json.dumps(read.findings) if read.findings else None,
             json.dumps(read.attempts) if read.attempts else None,
             read.writer_thread_id, read.node_id, read.reader_model, read.input_cap,
             read.messages_cut_from, read.elapsed_ms, read.prompt_tokens, read.completion_tokens,
             read.cost_usd, read.cost_source, now),
        )
        if read.verdict in (PASSED, PERSON_EDIT):
            for artifact_id, sha, body in read.documents:
                move_view(db, artifact_id, sha, body, read_id, read.writer_thread_id, read.node_id, now)
    return read_id


def record_person_edit(artifact_id: str, sha: str, body: bytes, db=None) -> str:
    """Move a document's pass to the person's own bytes; returns the record that now holds it.

    A run of the person's writes is one record: when the view already rests on a person edit,
    that record takes the new bytes instead of a new row per live-typing batch. An agent's pass
    in between starts a new run.
    """
    with write_transaction(db, scope="fresh_read_person_edit") as db:
        row = db.execute(
            """SELECT r.id FROM fresh_read_views v JOIN fresh_reads r ON r.id = v.fresh_read_id
               WHERE v.artifact_id=? AND r.verdict=?""", (artifact_id, PERSON_EDIT),
        ).fetchone()
        if row is None:
            return record(ReadRecord(kind="document", item_ref=artifact_id, sha256=sha, verdict=PERSON_EDIT,
                                     documents=((artifact_id, sha, body),)), db=db)
        now = utc_now_iso()
        db.execute("UPDATE fresh_reads SET sha256=?, created_at=? WHERE id=?", (sha, now, row["id"]))
        move_view(db, artifact_id, sha, body, str(row["id"]), None, None, now)
        return str(row["id"])


def move_view(db, artifact_id: str, sha: str, body: bytes, read_id: str,
              holder: str | None, node_id: str | None, now: str) -> None:
    """Point a document's view at a passed version, keeping only bytes a view still names."""
    old = db.execute("SELECT sha256, holder_thread_id, node_id FROM fresh_read_views WHERE artifact_id=?",
                     (artifact_id,)).fetchone()
    db.execute("INSERT OR IGNORE INTO fresh_read_bytes (sha256, body, created_at) VALUES (?,?,?)", (sha, body, now))
    # A person's edit keeps the agent that holds the document's pass, and the words it was read against.
    holder = holder if holder is not None or old is None else old["holder_thread_id"]
    node_id = node_id if node_id is not None or old is None else old["node_id"]
    db.execute(
        """INSERT INTO fresh_read_views (artifact_id, sha256, fresh_read_id, holder_thread_id, node_id,
               recheck_sha256, recheck_verdict, updated_at) VALUES (?,?,?,?,?,NULL,NULL,?)
           ON CONFLICT(artifact_id) DO UPDATE SET sha256=excluded.sha256, fresh_read_id=excluded.fresh_read_id,
               holder_thread_id=excluded.holder_thread_id, node_id=excluded.node_id,
               recheck_sha256=NULL, recheck_verdict=NULL, updated_at=excluded.updated_at""",
        (artifact_id, sha, read_id, holder, node_id, now),
    )
    if old is not None and old["sha256"] != sha:
        db.execute(
            "DELETE FROM fresh_read_bytes WHERE sha256=? AND NOT EXISTS "
            "(SELECT 1 FROM fresh_read_views WHERE sha256=?)", (old["sha256"], old["sha256"]),
        )


def forget_thread(db, thread_id: str) -> None:
    """A purged or deleted thread's reads go with it; a view keeps showing the version it names."""
    db.execute("DELETE FROM fresh_reads WHERE writer_thread_id=?", (thread_id,))


def forget_document(db, artifact_id: str) -> None:
    """A retired document's view, the bytes no other view names, and the person's edits to it.

    If it comes back, it has no view: it is served as the file is, like any document no
    agent has handed over, and the next hand-over reads it.
    """
    old = db.execute("SELECT sha256 FROM fresh_read_views WHERE artifact_id=?", (artifact_id,)).fetchone()
    db.execute("DELETE FROM fresh_read_views WHERE artifact_id=?", (artifact_id,))
    if old is not None:
        db.execute(
            "DELETE FROM fresh_read_bytes WHERE sha256=? AND NOT EXISTS "
            "(SELECT 1 FROM fresh_read_views WHERE sha256=?)", (old["sha256"], old["sha256"]),
        )
    db.execute("DELETE FROM fresh_reads WHERE kind='document' AND item_ref=? AND verdict=?",
               (artifact_id, PERSON_EDIT))


def note_recheck(artifact_id: str, sha: str, verdict: str) -> None:
    """Record that the background read of a newer version did not pass, so the passed copy opens editable."""
    with write_transaction(None, scope="fresh_read_recheck") as db:
        db.execute(
            "UPDATE fresh_read_views SET recheck_sha256=?, recheck_verdict=?, updated_at=? WHERE artifact_id=?",
            (sha, verdict, utc_now_iso(), artifact_id),
        )


def view(artifact_id: str, db=None) -> dict[str, Any] | None:
    """The document's view row with its passed bytes, or None when no pass governs it."""
    own = db is None
    db = db or get_db()
    try:
        row = db.execute(
            """SELECT v.*, b.body FROM fresh_read_views v
               LEFT JOIN fresh_read_bytes b ON b.sha256 = v.sha256 WHERE v.artifact_id=?""",
            (artifact_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        if own:
            db.close()
