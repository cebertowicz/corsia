"""The composed queue preserves explicit ranks and orders the rest by urgency."""
from tests.test_attention_queue import db  # noqa: F401


def test_queue_orders_ranks_then_urgency_and_age(db, monkeypatch):
    from api import attention

    monkeypatch.setattr(attention, '_authorized_raised', lambda *a, **k: [])
    monkeypatch.setattr(attention.public, 'annotate_attention', lambda *a, **k: [])
    monkeypatch.setattr(attention, '_queue_for', lambda *a, **k: [])
    monkeypatch.setattr(attention.public, 'activity_plan_rows', lambda *a: [
        row('review', 'review-new', '2026-01-03'),
        row('task', 'task-new', '2026-01-03'),
        row('review', 'review-old', '2026-01-01'),
        row('task', 'task-old', '2026-01-01'),
        row('task', 'ranked', '2026-01-04'),
    ])
    monkeypatch.setattr(attention.public, 'dedupe_activity_comment_tasks', lambda _db, _user, projected, _replies: projected)
    from doceditor import activity_reply_rows
    document = row('document', 'document-old', '2026-01-01')
    document['detail']['unread'] = 1
    monkeypatch.setattr(activity_reply_rows, 'document_rows', lambda *a, **k: [document])
    from infra import activity_rank_scope
    monkeypatch.setattr(activity_rank_scope, 'ranker_for', lambda *a: ('scope', None))
    db.execute('INSERT INTO activity_ranks VALUES (?,?,?,?,?,?,?)',
               ('user-1', 'scope', 'ranked', 1, 'First', 'root', '2026-01-01'))
    rows, notifications, _, _ = attention.activity_lists(db, 'user-1', view='activity')
    assert [r['key'] for r in rows] == [
        'ranked', 'task-old', 'task-new', 'review-old', 'review-new',
    ]
    assert [r['key'] for r in notifications] == ['document-old']


def row(renderer, key, raised_at):
    return {'renderer': renderer, 'kind': renderer, 'key': key, 'raised_at': raised_at,
            'source': {}, 'detail': {'artifact_id': key}}


def test_within_a_level_the_canonical_priority_comes_before_age():
    from infra.activity_urgency import sort_key
    rows = [
        {'renderer': 'decision', 'key': 'decision-late-plan-order', 'priority': 2, 'raised_at': '2026-01-01'},
        {'renderer': 'decision', 'key': 'decision-first-plan-order', 'priority': 0, 'raised_at': '2026-01-05'},
        {'renderer': 'held-reply', 'key': 'held-newest', 'priority': -1, 'raised_at': '2026-01-09'},
        {'renderer': 'review', 'key': 'review-oldest', 'raised_at': '2025-12-01'},
    ]
    assert [r['key'] for r in sorted(rows, key=sort_key)] == [
        'held-newest', 'decision-first-plan-order', 'decision-late-plan-order', 'review-oldest',
    ]


def test_renderer_levels_and_unknown_default():
    from infra.activity_urgency import sort_key
    assert [sort_key({'renderer': kind, 'key': kind})[0] for kind in
            ('decision', 'task', 'review', 'held-reply', 'reply', 'unknown')] == [1, 1, 2, 1, 3, 2]
