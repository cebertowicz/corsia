"""One-time recovery of unanswered person comments on project-owned documents.

A recovery batch is durable feedback, so its task and comment links use the normal
plan writers and settlement path. UI history remains history; it is not evidence
that a conductor answered a thread. Existing pending conductor feedback or an open
conductor task covers its exact latest message, but a finished task does not.
"""

import asyncio
import logging
import uuid
from collections import Counter
from contextlib import closing
from pathlib import Path

from doceditor.feedback_batch_core import utc_now
from infra.conductor import get_project_conductor
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.queries import thread_is_available
from plans import feedback_nodes, node_states
from plans.feedback_items import load_feedback_items
from registry import models as registry_models

log = logging.getLogger(__name__)
MARKER = 'person_comment_recovery_pending'
BATCH_SIZE = 25


def _pending():
    with closing(get_db()) as db:
        return db.execute('SELECT 1 FROM settings WHERE key=?', (MARKER,)).fetchone() is not None


def _page(after):
    with closing(get_db()) as db:
        return [row[0] for row in db.execute(
            """SELECT DISTINCT r.artifact_id FROM artifact_registrations r
               JOIN artifact_identities i ON i.id=r.artifact_id AND i.state='active'
               WHERE r.parent_thread_id LIKE 'project:%'
                 AND r.relationship_state IN ('owner-pinned','owner-active')
                 AND r.artifact_id > ? AND EXISTS (
                     SELECT 1 FROM doc_comments c WHERE c.artifact_id=r.artifact_id)
               ORDER BY r.artifact_id LIMIT ?""", (after, BATCH_SIZE),
        ).fetchall()]


def _unanswered(db, artifact_id, counts):
    rows = db.execute('SELECT * FROM doc_comments WHERE artifact_id=? ORDER BY created_at,id',
                      (artifact_id,)).fetchall()
    counts['comments'] += len(rows)
    roots = {row['id']: row for row in rows if row['parent_id'] is None}
    latest = {row['id']: row for row in roots.values()}
    for row in rows:
        if row['parent_id'] in latest:
            root = row['parent_id']
            if (row['created_at'], row['id']) > (latest[root]['created_at'], latest[root]['id']):
                latest[root] = row
    counts['threads'] += len(roots)
    eligible = []
    for root, message in latest.items():
        if roots[root]['resolved']:
            counts['skipped_resolved'] += 1
        elif message['author_kind'] != 'user':
            counts['skipped_answered'] += 1
        else:
            eligible.append(message)
    return eligible


def _coverage(db, artifact_id, conductor):
    """Exact comment ids with pending delivery or a still-open conductor task.

    A held batch stays held; recovery must not send it early. A delivered batch
    lacking a live task (including historical UI/empty outcomes) covers nothing.
    """
    rows = db.execute(
        f"""SELECT i.comment_id,b.state,n.id AS node_id,
                   CASE WHEN {node_states.open_sql('n')} THEN 1 ELSE 0 END AS open_task
            FROM doc_feedback_batches b
            JOIN doc_feedback_batch_items i ON i.batch_id=b.id
            LEFT JOIN plan_nodes n ON n.id=b.delivered_node_id
            LEFT JOIN plans p ON p.id=n.plan_id
            WHERE b.artifact_id=? AND b.recipient_id=?
              AND (b.state IN ('open','undelivered') OR
                   (p.owner_kind='thread' AND p.owner_id=?))""",
        (artifact_id, conductor, conductor),
    ).fetchall()
    covered = {}
    for row in rows:
        if row['state'] in ('open', 'undelivered'):
            covered[row['comment_id']] = 'skipped_pending'
        elif row['open_task']:
            covered[row['comment_id']] = 'skipped_open_task'
    return covered


def _deliver(db, artifact_id, conductor, comments):
    """Group recovery into one batch/task atomically using the canonical plan path.

    Normal reply delivery can suppress a recipient who acknowledged or left a
    thread. Recovery deliberately follows unanswered-thread eligibility instead:
    the project conductor is responsible even when previously unsubscribed.
    """
    # plans.public exports recovery while feedback_delivery imports that facade.
    from doceditor import comment_recipients, feedback_delivery

    problem = feedback_delivery.recipient_problem(db, conductor)
    if problem:
        raise RuntimeError(f'Conductor cannot receive recovered feedback: {problem}')
    batch_id, now = str(uuid.uuid4()), utc_now()
    db.execute(
        """INSERT INTO doc_feedback_batches
           (id,artifact_id,author_kind,author_id,recipient_id,state,opened_at,last_item_at,
            closes_at,closed_reason,item_count,attempts,created_at,updated_at)
           VALUES (?,?,'user','ui',?,'delivered',?,?,?,'send',?,1,?,?)""",
        (batch_id, artifact_id, conductor, now, now, now, len(comments), now, now),
    )
    db.executemany('INSERT INTO doc_feedback_batch_items(batch_id,comment_id) VALUES (?,?)',
                   [(batch_id, row['id']) for row in comments])
    batch = db.execute('SELECT * FROM doc_feedback_batches WHERE id=?', (batch_id,)).fetchone()
    items = load_feedback_items(db, batch)
    comment_recipients.prepare_notification(db, items, conductor)
    created = feedback_nodes.create_feedback_node(db, conductor, batch, items=items)
    db.execute("UPDATE doc_feedback_batches SET delivered_node_id=?,delivery_outcome='node' WHERE id=?",
               (created.node_id, batch_id))
    if not created.woke:
        feedback_delivery.queued_note(db, conductor, batch, items=items)
    return created.created


def _reconcile(artifact_id):
    counts = Counter()
    with write_transaction(None, scope='person_comment_recovery') as db:
        artifact = registry_models.get_artifact(artifact_id, db=db)
        owner = artifact.get('parent_thread_id') if artifact else None
        if (not artifact or artifact.get('state') != 'active' or not owner
                or not owner.startswith('project:')
                or artifact.get('relationship_state') not in ('owner-pinned', 'owner-active')):
            counts['skipped_changed_document'] += 1
            return counts
        comments = _unanswered(db, artifact_id, counts)
        if not comments:
            return counts
        path = artifact.get('path') or artifact.get('external_id')
        if not path or not Path(path).is_file():
            counts['skipped_missing_file'] += 1
            counts['comments_missing_file'] += len(comments)
            return counts
        conductor = get_project_conductor(owner.removeprefix('project:'), db=db)
        thread = db.execute('SELECT * FROM threads WHERE id=?', (conductor,)).fetchone() if conductor else None
        if thread is None or not thread_is_available(thread):
            counts['skipped_missing_conductor'] += 1
            counts['comments_missing_conductor'] += len(comments)
            return counts
        covered = _coverage(db, artifact_id, conductor)
        recover = []
        for comment in comments:
            if reason := covered.get(comment['id']):
                counts[reason] += 1
            else:
                recover.append(comment)
        if recover:
            counts['created'] += int(_deliver(db, artifact_id, conductor, recover))
            counts['documents_recovered'] += 1
            counts['comments_recovered'] += len(recover)
    return counts


def _clear():
    with write_transaction(None, scope='person_comment_recovery_clear') as db:
        db.execute('DELETE FROM settings WHERE key=?', (MARKER,))


async def run():
    """Leave the marker on any failure or cancellation; the next startup retries."""
    counts = Counter({key: 0 for key in (
        'documents', 'comments', 'threads', 'created', 'failed', 'documents_recovered', 'comments_recovered',
        'skipped_resolved', 'skipped_answered', 'skipped_missing_file', 'skipped_missing_conductor',
        'skipped_pending', 'skipped_open_task',
    )})
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
                counts['documents'] += 1
                try:
                    counts.update(await run_in_db(_reconcile, artifact_id))
                except Exception:
                    counts['failed'] += 1
                    log.exception('Person comment recovery failed for a document')
            after = page[-1]
            await asyncio.sleep(0)
        if not counts['failed']:
            await run_in_db(_clear)
    except Exception:
        counts['failed'] += 1
        log.exception('Person comment recovery pass failed')
    finally:
        if pending or counts['failed']:
            log.info('Person comment recovery: %s', ' '.join(f'{key}={value}' for key, value in counts.items()))
