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
        row('reply', 'ranked', '2026-01-04'),
    ])
    monkeypatch.setattr(attention.public, 'dedupe_activity_comment_tasks', lambda _db, _user, projected, _replies: projected)
    from doceditor import activity_reply_rows
    monkeypatch.setattr(activity_reply_rows, 'reply_rows', lambda *a, **k: [row('reply', 'reply-old', '2026-01-01')])
    from infra import activity_rank_scope
    monkeypatch.setattr(activity_rank_scope, 'ranker_for', lambda *a: ('scope', None))
    db.execute('INSERT INTO activity_ranks VALUES (?,?,?,?,?,?,?)',
               ('user-1', 'scope', 'ranked', 1, 'First', 'root', '2026-01-01'))
    rows, _, _ = attention.activity_queue(db, 'user-1', view='activity')
    assert [r['key'] for r in rows] == [
        'ranked', 'task-old', 'task-new', 'review-old', 'review-new', 'reply-old',
    ]


def row(renderer, key, raised_at):
    return {'renderer': renderer, 'kind': renderer, 'key': key, 'raised_at': raised_at,
            'source': {}, 'detail': {'artifact_id': key}}


def test_renderer_levels_and_unknown_default():
    from infra.activity_urgency import sort_key
    assert [sort_key({'renderer': kind, 'key': kind})[0] for kind in
            ('decision', 'task', 'review', 'held-reply', 'reply', 'unknown')] == [1, 1, 2, 1, 3, 2]
