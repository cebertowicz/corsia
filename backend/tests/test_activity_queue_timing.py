"""Timed read of a 300-item disposable Activity queue."""
import time
from api.attention import activity_queue
from plans import service
from tests.test_attention_queue import db  # noqa: F401


def test_read_300_items(db, monkeypatch):
    plan = service.ensure_plan('thread', 'root', db=db)
    for index in range(300):
        service.add_node(plan.id, kind='task', title=f'Task {index}',
                         acceptance_criteria='Done', assignee='user', db=db)
    db.commit()
    rows, _, _ = activity_queue(db, 'user-1', view='activity')
    assert len(rows) == 300
    timings = []
    for _ in range(20):
        start = time.perf_counter()
        rows, _, _ = activity_queue(db, 'user-1', view='activity')
        timings.append((time.perf_counter() - start) * 1000)
    p95 = sorted(timings)[18]
    print(f'Activity 300 items: p95={p95:.1f}ms max={max(timings):.1f}ms')
    assert p95 < 150
    service.add_node(plan.id, kind='task', title='One more',
                     acceptance_criteria='Done', assignee='user', db=db)
    db.commit()
    from tests.test_attention_queue_api import _attention_client
    import api.attention
    # The cap is 3,000 per list; pin a smaller one so the uncapped count is still proven here.
    monkeypatch.setattr(api.attention, 'QUEUE_ITEMS_CAP', 300)
    response = _attention_client(db, monkeypatch).get('/api/attention/queue?view=activity').json()
    assert response['count'] == 301
    assert len(response['items']) == 300
    assert response['current'] == response['items'][0]
