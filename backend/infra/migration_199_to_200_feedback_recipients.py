"""Normalize pending legacy project feedback recipients without dropping feedback."""

import logging
import sqlite3
import time

log = logging.getLogger(__name__)


class _Unmergeable(Exception):
    pass


def _merge_open(db, source, target):
    # The migration runner disables FK enforcement, so detect external references
    # before deleting a batch; post-migration validation must still pass.
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if table in {'doc_feedback_batch_items', 'doc_feedback_edit_items'}:
            continue
        quoted_table = '"' + table.replace('"', '""') + '"'
        for fk in db.execute(f'PRAGMA foreign_key_list({quoted_table})').fetchall():
            if fk[2] == 'doc_feedback_batches':
                column = '"' + fk[3].replace('"', '""') + '"'
                if db.execute(f'SELECT 1 FROM {quoted_table} WHERE {column}=? LIMIT 1', (source,)).fetchone():
                    raise _Unmergeable
    # Coalesced edit snapshots cannot be combined without reconstructing their history.
    if db.execute("SELECT 1 FROM doc_feedback_edit_items WHERE batch_id IN (?,?)", (source, target)).fetchone():
        raise _Unmergeable
    # Preserve distinct reopen events instead of silently choosing one.
    if db.execute(
        """SELECT 1 FROM doc_feedback_batch_items a JOIN doc_feedback_batch_items b
           ON a.comment_id=b.comment_id WHERE a.batch_id=? AND b.batch_id=?
           AND (a.event IS NOT b.event OR a.event_at IS NOT b.event_at OR a.event_by IS NOT b.event_by)""",
        (source, target),
    ).fetchone():
        raise _Unmergeable
    db.execute(
        """INSERT OR IGNORE INTO doc_feedback_batch_items(batch_id,comment_id,event,event_at,event_by)
           SELECT ?,comment_id,event,event_at,event_by FROM doc_feedback_batch_items WHERE batch_id=?""",
        (target, source),
    )
    db.execute(
        """UPDATE doc_feedback_batches SET
           opened_at=MIN(opened_at,(SELECT opened_at FROM doc_feedback_batches WHERE id=?)),
           last_item_at=MAX(last_item_at,(SELECT last_item_at FROM doc_feedback_batches WHERE id=?)),
           closes_at=MIN(closes_at,(SELECT closes_at FROM doc_feedback_batches WHERE id=?)),
           created_at=MIN(created_at,(SELECT created_at FROM doc_feedback_batches WHERE id=?)),
           updated_at=MAX(updated_at,(SELECT updated_at FROM doc_feedback_batches WHERE id=?)),
           item_count=(SELECT COUNT(*) FROM doc_feedback_batch_items WHERE batch_id=?) WHERE id=?""",
        (source, source, source, source, source, target, target),
    )
    db.execute("DELETE FROM doc_feedback_batch_items WHERE batch_id=?", (source,))
    db.execute("DELETE FROM doc_feedback_batches WHERE id=?", (source,))


def migrate_199_to_200(db):
    started = time.monotonic()
    normalized = merged = retained = 0
    rows = db.execute(
        """SELECT id,author_kind,author_id,artifact_id,delivery_policy,state
           FROM doc_feedback_batches WHERE recipient_id LIKE 'project:%'
           AND state IN ('open','undelivered') ORDER BY id"""
    ).fetchall()
    for batch_id, author_kind, author_id, artifact_id, policy, state in rows:
        target = None
        if state == 'open':
            target = db.execute(
                """SELECT id FROM doc_feedback_batches WHERE state='open' AND recipient_id='ui'
                   AND author_kind=? AND author_id=? AND artifact_id=? AND delivery_policy=?""",
                (author_kind, author_id, artifact_id, policy),
            ).fetchone()
        if target is None:
            db.execute("UPDATE doc_feedback_batches SET recipient_id='ui' WHERE id=?", (batch_id,))
            normalized += 1
            continue
        db.execute('SAVEPOINT feedback_recipient_collision')
        try:
            _merge_open(db, batch_id, target[0])
            merged += 1
        except (_Unmergeable, sqlite3.IntegrityError) as error:
            if isinstance(error, sqlite3.IntegrityError) and error.sqlite_errorcode != sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY:
                raise
            db.execute('ROLLBACK TO feedback_recipient_collision')
            retained += 1
            log.warning('Legacy feedback recipient collision could not be merged; retained both batches')
        finally:
            db.execute('RELEASE feedback_recipient_collision')
    log.info('Legacy feedback recipients: total=%d normalized=%d merged=%d retained=%d elapsed_ms=%d',
             len(rows), normalized, merged, retained, int((time.monotonic() - started) * 1000))
