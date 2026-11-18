"""Lossless, guarded reversal of the legacy feedback recipient migration."""

import json

TABLES = ('doc_feedback_batches', 'doc_feedback_batch_items', 'doc_feedback_edit_items')


def snapshot(db, batch_ids):
    result = {}
    for table in TABLES:
        key = 'id' if table == TABLES[0] else 'batch_id'
        marks = ','.join('?' for _ in batch_ids)
        cursor = db.execute(f'SELECT * FROM {table} WHERE {key} IN ({marks}) ORDER BY {key}', batch_ids)
        columns = [column[0] for column in cursor.description]
        rows = [list(row) for row in cursor.fetchall()]
        if table == 'doc_feedback_batch_items':
            rows.sort(key=lambda row: (row[0], row[1]))
        result[table] = {'columns': columns, 'rows': rows}
    return result


def record(db, batch_ids, before):
    db.execute('INSERT INTO feedback_recipient_migration_journal(batch_ids,before_rows,after_rows) VALUES (?,?,?)',
               (json.dumps(batch_ids), json.dumps(before), json.dumps(snapshot(db, batch_ids))))


def reverse(db):
    """Restore in reverse operation order, atomically refusing any subsequent change."""
    db.execute('SAVEPOINT reverse_feedback_recipients')
    try:
        rows = db.execute('SELECT batch_ids,before_rows,after_rows FROM feedback_recipient_migration_journal ORDER BY seq DESC').fetchall()
        for ids_json, before_json, after_json in rows:
            ids, before, after = json.loads(ids_json), json.loads(before_json), json.loads(after_json)
            if snapshot(db, ids) != after:
                raise RuntimeError('feedback batch changed after migration; reversal refused')
            _restore(db, ids, before)
        db.execute('DROP TABLE feedback_recipient_migration_journal')
        db.execute('RELEASE reverse_feedback_recipients')
    except Exception:
        db.execute('ROLLBACK TO reverse_feedback_recipients')
        db.execute('RELEASE reverse_feedback_recipients')
        raise


def _restore(db, ids, before):
    marks = ','.join('?' for _ in ids)
    db.execute(f'DELETE FROM doc_feedback_edit_items WHERE batch_id IN ({marks})', ids)
    db.execute(f'DELETE FROM doc_feedback_batch_items WHERE batch_id IN ({marks})', ids)
    batch = before[TABLES[0]]
    columns = batch['columns']
    updates = ','.join(f'{column}=excluded.{column}' for column in columns if column != 'id')
    for row in batch['rows']:
        db.execute(f"INSERT INTO doc_feedback_batches ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                   f'ON CONFLICT(id) DO UPDATE SET {updates}', row)
    items = before['doc_feedback_batch_items']
    columns = items['columns']
    db.executemany(f"INSERT INTO doc_feedback_batch_items ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", items['rows'])
    edits = before['doc_feedback_edit_items']
    columns = edits['columns']
    db.executemany(f"INSERT INTO doc_feedback_edit_items ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", edits['rows'])
