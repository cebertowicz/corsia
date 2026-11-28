"""Activity queue diagnostics expose the cost of each read step."""
from loguru import logger

from tests.test_attention_queue import db  # noqa: F401


def test_activity_queue_log_contains_read_timings(db):
    from api.attention import activity_lists

    messages = []
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        activity_lists(db, 'user-1', view='activity')
    finally:
        logger.remove(sink)
    lines = [message for message in messages if 'activity queue scope=' in message]
    assert len(lines) == 1
    for field in ('raised_ms', 'queue_ms', 'plan_ms', 'documents_ms', 'dedupe_ms',
                  'rank_ms', 'total_ms', 'person_documents_ms', 'person_documents_count',
                  'replies_ms', 'replies_chunks', 'replies_rows', 'access_ms',
                  'access_owners', 'thread_sources_ms'):
        assert field in lines[0], field


def test_title_timing_only_for_activity(monkeypatch):
    from api import attention
    from doceditor import activity_reply_rows

    class Connection:
        def close(self):
            pass

    monkeypatch.setattr(attention, 'get_db', Connection)
    monkeypatch.setattr(attention, 'activity_lists', lambda *args, **kwargs: ([], [{}], None, 'scope'))
    monkeypatch.setattr(activity_reply_rows, 'resolve_row_titles', lambda page: None)
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        attention._queue_response('user-1', None, None)
        assert not any('activity title resolution' in message for message in messages)
        attention._queue_response('user-1', None, 'activity')
    finally:
        logger.remove(sink)
    lines = [message for message in messages if 'activity title resolution' in message]
    assert len(lines) == 1
    assert 'ms=' in lines[0] and 'rows=1' in lines[0]
