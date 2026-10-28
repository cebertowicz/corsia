from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from connections.google import guard_read
from tools import mail_guard_runtime


@pytest.mark.asyncio
async def test_guard_reads_are_repeated_per_draft_and_send(monkeypatch):
    now = datetime.now(timezone.utc)
    calls = []
    def raw(mid):
        date = now - timedelta(days=2 if mid == 'target' else 1)
        return {'id': mid, 'threadId': 'thread' if mid == 'target' else 'other',
                'internalDate': str(int(date.timestamp() * 1000)), 'labelIds': ['UNREAD'],
                'payload': {'headers': [{'name': 'From', 'value': 'Alice <alice@example.org>'},
                                        {'name': 'Subject', 'value': 'Topic'}]}}

    async def request(method, url, *, account_id, params):
        calls.append((url, params))
        if url.endswith('/threads/thread'):
            return {'messages': [raw('target')]}
        if url.endswith('/messages'):
            return {'messages': [{'id': 'target'}, {'id': 'other'}]}
        return raw(url.rsplit('/', 1)[-1])

    async def fetch_body(account_id, message_id):
        calls.append(('body', message_id))
        return 'evidence'

    async def screen(account_id, source):
        return '{"subject":"Topic","body":"evidence"}'

    monkeypatch.setattr(guard_read.client, 'request_json', request)
    monkeypatch.setattr(mail_guard_runtime, 'screened_message', screen)
    backend = SimpleNamespace(guard_data=guard_read.guard_data, fetch_body=fetch_body)
    for _ in range(2):  # draft and send each run a fresh guard
        verdict, _ = await mail_guard_runtime.guarded_reply(
            backend, {'id': 'account', 'address': 'me@example.org'}, 'target', 'reply', [])
        assert verdict.stale
    assert len([c for c in calls if c[0].endswith('/messages')]) == 8
    assert len([c for c in calls if '/messages/' in c[0]]) == 4
    assert len([c for c in calls if '/threads/' in c[0]]) == 2
    assert [c for c in calls if c[0] == 'body'] == [('body', 'other'), ('body', 'other')]
