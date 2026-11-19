"""Permanently normalize pending project recipients and fold their open collisions."""

import logging
import time

from infra.comment_identity_batch_folds import FOLDS, _recount
from infra.feedback_recipient_folds import merge_open

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


def _repair_fold_counts(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='comment_identity_moves'").fetchone():
        return 0
    targets = db.execute(
        """SELECT DISTINCT b.id FROM doc_feedback_batches b
           JOIN comment_identity_moves m ON m.moved_to=b.id
           WHERE m.table_name=? AND b.state IN ('open','undelivered')""", (FOLDS,)
    ).fetchall()
    for (batch_id,) in targets:
        _recount(db, batch_id)
    return len(targets)


def migrate_201_to_202(db):
    started = time.monotonic()
    db.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) "
               "VALUES ('person_comment_recovery_pending','1',CURRENT_TIMESTAMP)")
    repaired = _repair_fold_counts(db)
    references = _reference_columns(db)
    rows = db.execute(
        """SELECT id,author_kind,author_id,artifact_id,delivery_policy,state,recipient_id
           FROM doc_feedback_batches WHERE recipient_id LIKE 'project:%'
           AND state IN ('open','undelivered') ORDER BY opened_at,created_at,id"""
    ).fetchall()
    merged = 0
    open_groups = {}
    for batch_id, author_kind, author_id, artifact_id, policy, state, legacy_recipient in rows:
        project = db.execute('SELECT conductor_thread_id FROM projects WHERE id=?',
                             (legacy_recipient.removeprefix('project:'),)).fetchone()
        recipient = project[0] if project and project[0] else 'ui'
        if state == 'open':
            key = (recipient, author_kind, author_id, artifact_id, policy)
            open_groups.setdefault(key, []).append(batch_id)
        else:
            db.execute('UPDATE doc_feedback_batches SET recipient_id=? WHERE id=?', (recipient, batch_id))
    for key, sources in open_groups.items():
        target = db.execute(
            """SELECT id FROM doc_feedback_batches WHERE state='open' AND recipient_id=?
               AND author_kind=? AND author_id=? AND artifact_id=? AND delivery_policy=?""", key,
        ).fetchone()
        if target is None:
            target_id = sources.pop(0)
            db.execute('UPDATE doc_feedback_batches SET recipient_id=? WHERE id=?', (key[0], target_id))
        else:
            target_id = target[0]
        if sources:
            merge_open(db, sources, target_id, references)
            merged += len(sources)
    log.info('Legacy feedback recipients: total=%d normalized=%d merged=%d fold_counts_repaired=%d elapsed_ms=%d',
             len(rows), len(rows) - merged, merged, repaired, int((time.monotonic() - started) * 1000))


def reverse_202_to_201(db):
    """Recipient and count repairs remain valid on downgrade; never restore legacy recipients."""
