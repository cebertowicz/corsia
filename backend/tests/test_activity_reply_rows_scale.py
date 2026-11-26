"""Activity's Notifications read grows linearly with a document's comments, never quadratically.

Cost is counted in SQLite VM steps (a progress handler), not wall time, so the check is the
same on a loaded runner. Quadrupling one document's comments must cost well under the 16x a
per-comment rescan of the document would.
"""
import sqlite3

from doceditor.activity_reply_rows import document_rows
from infra.schema import SCHEMA_SQL

_STEP = 1000


def _document(comments: int, *, person_commented: bool):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO threads(id,user_id,title,created_at,updated_at) VALUES ('agent','u','Agent','0','0')")
    db.execute("INSERT INTO artifact_identities(id,integration,type,external_id,title,created_at,updated_at) "
               "VALUES ('doc','local','document','doc','Doc','0','0')")
    db.execute("INSERT INTO artifact_registrations(id,artifact_id,parent_thread_id,created_at,updated_at) "
               "VALUES ('reg','doc','agent','0','0')")
    rows: list[tuple[str, str | None, str, str | None, str]] = []
    for i in range(comments):
        at = f'{i:08}'
        if person_commented and i % 4 == 0:  # the person opens a thread; agents answer in it
            rows.append((f'c{i}', None, 'user', None, at))
        elif person_commented:
            rows.append((f'c{i}', f'c{i - i % 4}', 'agent', 'agent', at))
        else:  # agents comment on a document the person never commented in
            rows.append((f'c{i}', None, 'agent', 'agent', at))
    db.executemany("INSERT INTO doc_comments(id,artifact_id,parent_id,author,author_kind,author_thread_id,body,"
                   "created_at,updated_at) VALUES (?,'doc',?,'x',?,?,'b',?,?)",
                   [(cid, parent, kind, thread, at, at) for cid, parent, kind, thread, at in rows])
    db.commit()
    return db


def _steps(db) -> int:
    count = 0

    def tick():
        nonlocal count
        count += 1
        return 0

    db.set_progress_handler(tick, _STEP)
    document_rows(db, 'u')
    db.set_progress_handler(None, _STEP)
    return count


def test_replies_to_the_person_cost_linear_in_the_document():
    small, large = _document(400, person_commented=True), _document(1600, person_commented=True)
    assert document_rows(large, 'u')[0]['detail']['unread'] == 1200
    assert _steps(large) < 6 * _steps(small)


def test_a_document_the_person_never_commented_in_costs_linear():
    small, large = _document(400, person_commented=False), _document(1600, person_commented=False)
    assert document_rows(large, 'u') == []
    assert _steps(large) < 6 * _steps(small)
