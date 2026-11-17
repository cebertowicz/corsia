"""One-time recovery of project-owned documents' person comment tasks."""

import asyncio
import logging

from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from plans import person_comment_task

log = logging.getLogger(__name__)
MARKER = 'person_comment_recovery_pending'
BATCH_SIZE = 25


def _pending():
    return get_db().execute('SELECT 1 FROM settings WHERE key=?', (MARKER,)).fetchone() is not None


def _page(after):
    # Include delivered batches: attention depends on comment watermarks, not delivery state.
    return [row[0] for row in get_db().execute(
        """SELECT DISTINCT r.artifact_id FROM artifact_registrations r
           JOIN artifact_identities i ON i.id=r.artifact_id AND i.state='active'
           WHERE r.parent_thread_id LIKE 'project:%'
             AND r.relationship_state IN ('owner-pinned','owner-active')
             AND r.artifact_id > ? AND EXISTS (
                 SELECT 1 FROM doc_comments c
                 JOIN doc_feedback_batch_items bi ON bi.comment_id=c.id
                 JOIN doc_feedback_batches b ON b.id=bi.batch_id
                 WHERE c.artifact_id=r.artifact_id AND b.artifact_id=c.artifact_id
                   AND b.recipient_id='ui')
           ORDER BY r.artifact_id LIMIT ?""", (after, BATCH_SIZE),
    ).fetchall()]


def _reconcile(artifact_id):
    with write_transaction(None, scope='person_comment_recovery') as db:
        before, _ = person_comment_task._open_task(db, artifact_id)
        result = person_comment_task.reconcile(db, artifact_id)
        return bool(result and result != before)


def _clear():
    with write_transaction(None, scope='person_comment_recovery_clear') as db:
        db.execute('DELETE FROM settings WHERE key=?', (MARKER,))


async def run():
    """Leave the marker on any failure or cancellation; the next startup retries."""
    candidates = created = failed = 0
    pending = False
    try:
        pending = await run_in_db(_pending)
        if not pending:
            return
        after = ''
        while True:
            page = await run_in_db(_page, after)
            if not page:
                break
            for artifact_id in page:
                candidates += 1
                try:
                    created += await run_in_db(_reconcile, artifact_id)
                except Exception:
                    failed += 1
                    log.exception('Person comment recovery failed for a document')
            after = page[-1]
            await asyncio.sleep(0)
        if not failed:
            await run_in_db(_clear)
    except Exception:
        failed += 1
        log.exception('Person comment recovery pass failed')
    finally:
        if pending or failed:
            log.info('Person comment recovery: candidates=%d created=%d failed=%d', candidates, created, failed)
