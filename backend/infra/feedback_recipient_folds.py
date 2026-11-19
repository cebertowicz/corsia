"""Chronological folding for the one-time legacy feedback recipient repair."""

from pathlib import Path

from infra.comment_identity_batch_folds import _recount
from registry.shared_edit_classification import capped_unified_diff, combine_capped_diffs


def _rows(db, sql, params):
    cursor = db.execute(sql, params)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _merge_comments(db, batch_ids, target):
    marks = ','.join('?' for _ in batch_ids)
    # add_comment_item inserts only once: the first queued event for a comment wins.
    items = db.execute(
        f"""SELECT i.comment_id,i.event,i.event_at,i.event_by
           FROM doc_feedback_batch_items i JOIN doc_feedback_batches b ON b.id=i.batch_id
           LEFT JOIN doc_comments c ON c.id=i.comment_id WHERE b.id IN ({marks})
           ORDER BY COALESCE(i.event_at,c.created_at,b.opened_at),b.opened_at,b.created_at,b.id""",
        batch_ids,
    ).fetchall()
    db.execute(f'DELETE FROM doc_feedback_batch_items WHERE batch_id IN ({marks})', batch_ids)
    db.executemany(
        """INSERT OR IGNORE INTO doc_feedback_batch_items(batch_id,comment_id,event,event_at,event_by)
           VALUES (?,?,?,?,?)""", [(target, *item) for item in items],
    )


def _coalesce(earlier, later, path):
    """Apply record_edit_item's segment rule to chronological saved snapshots."""
    latest = later.copy()
    continuous = (earlier['last_at'] <= later['first_at']
                  and earlier['latest_sha256'] == later['baseline_sha256']
                  and not later['prior_segments_diff'])
    if continuous:
        latest['baseline_sha256'] = earlier['baseline_sha256']
        latest['baseline_text'] = earlier['baseline_text']
        latest['prior_segments_diff'] = earlier['prior_segments_diff']
    else:
        closed = capped_unified_diff(path, earlier['baseline_text'].encode('utf-8'),
                                     earlier['latest_text'].encode('utf-8'))
        latest['prior_segments_diff'] = combine_capped_diffs(
            earlier['prior_segments_diff'], closed, later['prior_segments_diff'])
    return latest


def _merge_edits(db, batch_ids, target, artifact_id):
    marks = ','.join('?' for _ in batch_ids)
    edits = _rows(db, f'SELECT * FROM doc_feedback_edit_items WHERE batch_id IN ({marks}) '
                  'ORDER BY last_at,first_at,batch_id', batch_ids)
    if not edits:
        return
    identity = db.execute(
        "SELECT COALESCE(NULLIF(path,''),NULLIF(external_id,''),NULLIF(title,''),'document') "
        "FROM artifact_identities WHERE id=?",
        (artifact_id,),
    ).fetchone()
    path = Path(identity[0] if identity else 'document')
    latest = edits[0].copy()
    for later in edits[1:]:
        latest = _coalesce(latest, later, path)
    earliest = min(edits, key=lambda edit: (edit['first_at'], edit['batch_id']))
    latest.update(batch_id=target, actor_display=earliest['actor_display'],
                  first_at=earliest['first_at'], save_count=sum(edit['save_count'] for edit in edits))
    db.execute(f'DELETE FROM doc_feedback_edit_items WHERE batch_id IN ({marks})', batch_ids)
    columns = ','.join(latest)
    db.execute(f'INSERT INTO doc_feedback_edit_items({columns}) VALUES ({",".join("?" for _ in latest)})',
               tuple(latest.values()))


def merge_open(db, sources, target):
    """Fold the whole collision group once, retaining the canonical destination id.

    Pairwise folds lose chronology once an intervening snapshot is flattened into a
    newer destination's prior diff, so gather all payloads before coalescing any.
    """
    batch_ids = [target, *sources]
    marks = ','.join('?' for _ in batch_ids)
    batches = _rows(db, f'SELECT * FROM doc_feedback_batches WHERE id IN ({marks})', batch_ids)
    newest = max(batches, key=lambda batch: (batch['last_item_at'], batch['updated_at'], batch['id']))
    _merge_comments(db, batch_ids, target)
    _merge_edits(db, batch_ids, target, newest['artifact_id'])
    # open_batch owns first/open timestamps; touch_batch owns the latest event and its
    # deadline (not MIN/MAX deadline: an edit and a comment have different quiet times).
    db.execute(
        """UPDATE doc_feedback_batches SET opened_at=?,last_item_at=?,closes_at=?,
           created_at=?,updated_at=?,closed_reason=NULL,attempts=? WHERE id=?""",
        (min(batch['opened_at'] for batch in batches), newest['last_item_at'], newest['closes_at'],
         min(batch['created_at'] for batch in batches), max(batch['updated_at'] for batch in batches),
         sum(batch['attempts'] for batch in batches), target),
    )
    db.executemany('DELETE FROM doc_feedback_batches WHERE id=?', [(source,) for source in sources])
    _recount(db, target)
