"""Normalize pending legacy project feedback recipients without dropping feedback."""

import logging
import time

from infra.feedback_recipient_journal import record, reverse, snapshot
from infra.doc_feedback_schema import JOURNAL_SCHEMA_SQL

log = logging.getLogger(__name__)


def _reference_columns(db):
    references = []
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if table in {'doc_feedback_batch_items', 'doc_feedback_edit_items'}:
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        for fk in db.execute(f'PRAGMA foreign_key_list({quoted})').fetchall():
            if fk[2] == 'doc_feedback_batches':
                column = '"' + fk[3].replace('"', '""') + '"'
                references.append((quoted, column))
    return references


def _collision_reasons(db, source, target, references):
    reasons = set()
    for table, column in references:
        if db.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1', (source,)).fetchone():
            reasons.add('referenced')
            break
    edits = db.execute('SELECT * FROM doc_feedback_edit_items WHERE batch_id IN (?,?) ORDER BY batch_id',
                       (source, target)).fetchall()
    if len(edits) == 2 and tuple(edits[0])[1:] != tuple(edits[1])[1:]:
        reasons.add('edit_conflicts')
    if db.execute(
        """SELECT 1 FROM doc_feedback_batch_items a JOIN doc_feedback_batch_items b
           ON a.comment_id=b.comment_id WHERE a.batch_id=? AND b.batch_id=?
           AND (a.event IS NOT b.event OR a.event_at IS NOT b.event_at OR a.event_by IS NOT b.event_by)""",
        (source, target),
    ).fetchone():
        reasons.add('reopen_conflicts')
    return reasons


def _merge_open(db, source, target):
    db.execute(
        """INSERT OR IGNORE INTO doc_feedback_batch_items(batch_id,comment_id,event,event_at,event_by)
           SELECT ?,comment_id,event,event_at,event_by FROM doc_feedback_batch_items WHERE batch_id=?""",
        (target, source),
    )
    if not db.execute('SELECT 1 FROM doc_feedback_edit_items WHERE batch_id=?', (target,)).fetchone():
        db.execute('UPDATE doc_feedback_edit_items SET batch_id=? WHERE batch_id=?', (target, source))
    db.execute('DELETE FROM doc_feedback_edit_items WHERE batch_id=?', (source,))
    db.execute(
        """UPDATE doc_feedback_batches SET
           opened_at=MIN(opened_at,(SELECT opened_at FROM doc_feedback_batches WHERE id=?)),
           last_item_at=MAX(last_item_at,(SELECT last_item_at FROM doc_feedback_batches WHERE id=?)),
           closes_at=MIN(closes_at,(SELECT closes_at FROM doc_feedback_batches WHERE id=?)),
           created_at=MIN(created_at,(SELECT created_at FROM doc_feedback_batches WHERE id=?)),
           updated_at=MAX(updated_at,(SELECT updated_at FROM doc_feedback_batches WHERE id=?)),
           item_count=(SELECT COUNT(*) FROM doc_feedback_batch_items WHERE batch_id=?)
                      +(SELECT COUNT(*) FROM doc_feedback_edit_items WHERE batch_id=?) WHERE id=?""",
        (source, source, source, source, source, target, target, target),
    )
    db.execute('DELETE FROM doc_feedback_batch_items WHERE batch_id=?', (source,))
    db.execute('DELETE FROM doc_feedback_batches WHERE id=?', (source,))


def migrate_199_to_200(db):
    started = time.monotonic()
    db.execute(JOURNAL_SCHEMA_SQL)
    db.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) "
               "VALUES ('person_comment_recovery_pending','1',CURRENT_TIMESTAMP)")
    counts = dict(normalized=0, merged=0, reopen_conflicts=0, edit_conflicts=0, referenced=0)
    references = _reference_columns(db)
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
        ids = [batch_id] + ([target[0]] if target else [])
        before = snapshot(db, ids)
        if target is None:
            db.execute("UPDATE doc_feedback_batches SET recipient_id='ui' WHERE id=?", (batch_id,))
            counts['normalized'] += 1
        else:
            reasons = _collision_reasons(db, batch_id, target[0], references)
            if reasons:
                for reason in reasons:
                    counts[reason] += 1
            else:
                _merge_open(db, batch_id, target[0])
                counts['merged'] += 1
        record(db, ids, before)
    log.info('Legacy feedback recipients: total=%d normalized=%d merged=%d reopen_conflicts=%d '
             'edit_conflicts=%d referenced=%d elapsed_ms=%d', len(rows), counts['normalized'], counts['merged'],
             counts['reopen_conflicts'], counts['edit_conflicts'], counts['referenced'],
             int((time.monotonic() - started) * 1000))


def reverse_200_to_199(db):
    """Restore recorded feedback rows only if none changed after this migration."""
    reverse(db)
