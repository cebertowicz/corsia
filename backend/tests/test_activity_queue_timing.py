"""Timed HTTP read of full Activity lists in a disposable in-memory database."""
import time

from plans import service
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_attention_queue import db  # noqa: F401
from tests.test_attention_queue_api import _attention_client


def _seed_lists(db, start, stop):
    plan = service.ensure_plan('thread', 'root', db=db)
    with raw_plan_seed(db):
        db.executemany(
            """INSERT INTO plan_nodes
               (id,plan_id,order_key,kind,title,acceptance_criteria,assignee,commitment_id,created_at,updated_at)
               VALUES (?,?,?,'task',?,'Done','user',?,'t','t')""",
            [(f'task-{i}', plan.id, i, f'Task {i}', f'task-{i}') for i in range(start, stop)],
        )
    db.executemany(
        """INSERT INTO artifact_identities (id,integration,type,external_id,title,created_at,updated_at)
           VALUES (?,'local','document',?,?, 't','t')""",
        [(f'doc-{i}', f'/workspace/doc-{i}', f'Document {i}') for i in range(start, stop)],
    )
    db.executemany(
        """INSERT INTO artifact_registrations (id,artifact_id,parent_thread_id,created_at,updated_at)
           VALUES (?,?,'root','t','t')""",
        [(f'reg-{i}', f'doc-{i}') for i in range(start, stop)],
    )
    db.executemany(
        """INSERT INTO doc_comments (id,artifact_id,author,author_kind,body,created_at,updated_at)
           VALUES (?,?,'Person','user','Question','2026-01-01','2026-01-01')""",
        [(f'root-{i}', f'doc-{i}') for i in range(start, stop)],
    )
    db.executemany(
        """INSERT INTO doc_comments
           (id,artifact_id,parent_id,author,author_kind,author_thread_id,body,created_at,updated_at)
           VALUES (?,?,?,'Agent','agent','root','Reply','2026-01-02','2026-01-02')""",
        [(f'reply-{i}', f'doc-{i}', f'root-{i}') for i in range(start, stop)],
    )
    db.commit()


def test_activity_full_lists_timing(db, monkeypatch):
    _seed_lists(db, 0, 3000)
    client = _attention_client(db, monkeypatch)
    url = '/api/attention/queue?view=activity'
    response = client.get(url)
    assert response.status_code == 200
    body = response.json()
    assert body['count'] == len(body['items']) == 3000
    assert body['notifications']['count'] == len(body['notifications']['items']) == 3000
    timings = []
    for _ in range(20):
        start = time.perf_counter()
        response = client.get(url)
        timings.append((time.perf_counter() - start) * 1000)
        assert response.status_code == 200
    p95 = sorted(timings)[18]
    print(f'Activity 3000 per list: p95={p95:.1f}ms max={max(timings):.1f}ms')
    # Local baseline: 338 ms p95 for 20 HTTP reads; allow ~4.4x for CI variance.
    assert p95 < 1500
