"""Transaction-owned, byte-verified storage for document anchor versions."""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import cast

from doceditor.quote_anchor import _cp_at_u16
from infra.write_effects import after_commit

log = logging.getLogger(__name__)


def invalid_reason(text: str, start, end, quote: str | None) -> str | None:
    """Explain why a quote and UTF-16 range cannot identify these bytes."""
    length = len(text.encode('utf-16-le')) // 2
    if (not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int)
            or isinstance(end, bool) or start < 0 or end < start or end > length):
        return 'offset_out_of_range'
    first, last = _cp_at_u16(text, start), _cp_at_u16(text, end)
    if first is None or last is None:
        return 'boundary'
    return None if quote is not None and text[first:last] == quote else 'quote_mismatch'


def attach_on_create(db: sqlite3.Connection, comment_id: str, text: str | None,
                     invalid_reason: str | None) -> None:
    """Record the version a new anchor was measured against, inside the creating transaction.

    Nothing is stored when the comment carries no verifiable selection (a reply, a
    review, a read that failed): the comment is still created, and the reason is
    logged once after the commit, because it is a lost optimization, not a lost comment.
    """
    if not db.in_transaction:
        raise ValueError("snapshot changes require an owning transaction")
    if text is not None:
        db.execute('UPDATE doc_comments SET snapshot_id=? WHERE id=?', (intern(db, text), comment_id))
        artifact_id = db.execute('SELECT artifact_id FROM doc_comments WHERE id=?', (comment_id,)).fetchone()[0]
        adopt_legacy(db, artifact_id, text)
    if invalid_reason:
        after_commit(db, lambda: log.info('comment snapshot omitted id=%s reason=%s',
                                          comment_id, invalid_reason))


def intern(db: sqlite3.Connection, text: str) -> int:
    """Return the ID of these exact UTF-8 bytes, inserting inside the caller's transaction.

    The digest narrows the lookup; it is not an identity. A collision keeps a
    separate row. The caller must serialize writes or retry the entire owning
    transaction after SQLite reports a conflicting deferred transaction.
    """
    if not db.in_transaction:
        raise ValueError("snapshot changes require an owning transaction")
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).digest()
    for row in db.execute("SELECT id, text FROM doc_comment_snapshots WHERE digest=?", (digest,)):
        if row[1] == data:
            return row[0]
    return cast(int, db.execute("INSERT INTO doc_comment_snapshots (digest, text) VALUES (?, ?)", (digest, data)).lastrowid)


def read_many(db: sqlite3.Connection, snapshot_ids) -> dict[int, str]:
    """Load each exact version once, in bounded batches; missing references are errors."""
    ids = list(set(snapshot_ids))
    result = {}
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        placeholders = ','.join('?' for _ in batch)
        for snapshot_id, data in db.execute(
            f"SELECT id, text FROM doc_comment_snapshots WHERE id IN ({placeholders})", batch,
        ):
            result[snapshot_id] = data.decode('utf-8')
    missing = set(ids) - result.keys()
    if missing:
        raise LookupError(f"document comment snapshots missing: {sorted(missing)}")
    return result


def read(db: sqlite3.Connection, snapshot_id: int) -> str:
    """Read an exact version; a broken reference is an error, not a legacy row."""
    return read_many(db, [snapshot_id])[snapshot_id]


def adopt_before_write(path, *, before: bytes | str | None = None, artifact_id: str | None = None,
                       verify_text=None) -> None:
    """Best-effort adoption before a file or position write; never refuse the write."""
    db = None
    try:
        from infra.database import get_db, write_transaction
        from registry import service

        db = get_db()
        if artifact_id is None:
            artifact = service.resolve_file_identity(str(path), db=db)
            if artifact is None:
                return
            artifact_id = artifact['id']
        row = db.execute(
            'SELECT artifact_id FROM doc_comments WHERE artifact_id=? AND parent_id IS NULL '
            'AND review=0 AND snapshot_id IS NULL LIMIT 1', (artifact_id,),
        ).fetchone()
        if row is None:
            return
        data = before if before is not None else path.read_bytes()
        text = data.decode('utf-8') if isinstance(data, bytes) else data
        with write_transaction(db, scope='adopt_comment_before_write'):
            if verify_text is not None:
                verify_text()
            adopt_legacy(db, row[0], text)
    except Exception:
        log.warning('Could not adopt legacy comment anchors before write', exc_info=True)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                log.warning('Could not close legacy comment adoption connection', exc_info=True)


def adopt_legacy(db: sqlite3.Connection, artifact_id: str, text: str, *, eligible=None) -> None:
    """Adopt only root selections whose stored range exactly matches these bytes."""
    if not db.in_transaction:
        raise ValueError('snapshot changes require an owning transaction')
    rows = db.execute(
        'SELECT id,from_off,to_off,anchor_text FROM doc_comments '
        'WHERE artifact_id=? AND parent_id IS NULL AND review=0 AND snapshot_id IS NULL',
        (artifact_id,),
    ).fetchall()
    valid = [identifier for identifier, start, end, quote in rows
             if (eligible is None or identifier in eligible) and quote
             and invalid_reason(text, start, end, quote) is None]
    if valid:
        snapshot_id = intern(db, text)
        db.executemany('UPDATE doc_comments SET snapshot_id=? WHERE id=?',
                       [(snapshot_id, identifier) for identifier in valid])


def pair_published_positions(db: sqlite3.Connection, artifact_id: str, text: str, *, preserve=(), eligible=None) -> None:
    """Pair surviving roots with publication bytes; keep unplaced sources intact."""
    if not db.in_transaction:
        raise ValueError('snapshot changes require an owning transaction')
    rows = db.execute(
        'SELECT id,snapshot_id,from_off,to_off,anchor_text FROM doc_comments '
        'WHERE artifact_id=? AND parent_id IS NULL AND review=0 AND snapshot_id IS NOT NULL',
        (artifact_id,),
    ).fetchall()
    new_id = None
    previous = []
    for identifier, old_id, start, end, quote in rows:
        if identifier in preserve or (eligible is not None and identifier not in eligible):
            continue
        reason = invalid_reason(text, start, end, quote)
        if reason is None:
            if new_id is None:
                new_id = intern(db, text)
            target = new_id
        else:
            target = None
        db.execute('UPDATE doc_comments SET snapshot_id=? WHERE id=?', (target, identifier))
        previous.append(old_id)
    collect_candidates(db, previous)


def collect_candidates(db: sqlite3.Connection, snapshot_ids) -> int:
    """Collect only prior references made potentially unreachable by this transaction."""
    if not db.in_transaction:
        raise ValueError("snapshot changes require an owning transaction")
    count = 0
    for snapshot_id in set(snapshot_ids):
        if snapshot_id is not None:
            count += db.execute(
                "DELETE FROM doc_comment_snapshots WHERE id=? AND NOT EXISTS "
                "(SELECT 1 FROM doc_comments WHERE snapshot_id=doc_comment_snapshots.id)",
                (snapshot_id,),
            ).rowcount
    return count
