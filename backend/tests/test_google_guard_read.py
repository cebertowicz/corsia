from datetime import datetime, timedelta, timezone

import pytest

from connections.google import guard_read
from tools.mail_guard import check_reply


@pytest.mark.asyncio
@pytest.mark.parametrize('target_age', [2, 60])
async def test_guard_searches_both_windows_and_uses_only_sequential_metadata(monkeypatch, target_age):
    now = datetime.now(timezone.utc)
    target_date = now - timedelta(days=target_age)
    recent = now - timedelta(days=1)
    count_only = now - timedelta(days=20)
    dates = {'target': target_date, 'recent': recent, 'count': count_only,
             'older': now - timedelta(days=45)}
    dates.update({f'm{i}': recent for i in range(600)})
    calls = []
    active = 0
    max_active = 0

    def raw(mid):
        return {'id': mid, 'threadId': 'target-thread' if mid == 'target' else mid,
                'internalDate': str(int(dates[mid].timestamp() * 1000)),
                'labelIds': ['UNREAD'],
                'payload': {'headers': [{'name': 'From', 'value': 'Alice <alice@example.org>'},
                                        {'name': 'Subject', 'value': 'Topic'}]}}

    async def request(method, url, *, account_id, params):
        nonlocal active, max_active
        import asyncio
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        calls.append((url, dict(params)))
        try:
            if url.endswith('/threads/target-thread'):
                return {'messages': [raw('target')]}
            if url.endswith('/messages'):
                cutoff = (int((now - timedelta(days=30)).timestamp()) if 'newer_than:30d' in params['q']
                          else int(params['q'].rsplit('after:', 1)[1]))
                ids = [mid for mid, date in dates.items() if date.timestamp() > cutoff]
                start = int(params.get('pageToken', 0))
                page = ids[start:start + 500]
                result = {'messages': [{'id': mid} for mid in page]}
                if start + 500 < len(ids):
                    result['nextPageToken'] = str(start + 500)
                return result
            return raw(url.rsplit('/', 1)[-1])
        finally:
            active -= 1

    monkeypatch.setattr(guard_read.client, 'request_json', request)
    target, thread, sender, count = await guard_read.guard_data('account', 'target')
    verdict = check_reply(target, thread, sender, 'me@example.org', 'reply', [], now, count)
    expected_count = len({mid for mid, date in dates.items() if date >= now - timedelta(days=30)})
    assert count == expected_count
    assert any(message.message_id == 'recent' for message in sender)
    assert any(message.message_id == 'older' for message in sender) == (target_age == 60)
    assert verdict.stale
    assert all(not m.body for m in thread + sender)
    queries = [params['q'] for url, params in calls if url.endswith('/messages')]
    cutoffs = {int(q.rsplit('after:', 1)[1]) for q in queries if 'after:' in q}
    assert int(target_date.timestamp()) - 1 in cutoffs
    assert any('newer_than:30d' in q for q in queries)
    message_reads = [params for url, params in calls if '/messages/' in url]
    expected_reads = 1 + sum(mid != 'target' and date.timestamp() > int(target_date.timestamp()) - 1
                             for mid, date in dates.items())
    assert len(message_reads) == expected_reads
    assert all(params['format'] == 'metadata' for _, params in calls if 'format' in params)
    assert max_active == 1
    assert len(queries) == 4  # address-only searches for arrivals and the separate count
