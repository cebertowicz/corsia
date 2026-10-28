from datetime import datetime, timedelta, timezone

import pytest

from connections.google import guard_read as gmail
from connections.mail.email_document import DraftRefused
from connections.mailhost import guard_read as host, read
from tests.test_mailhost_guard_read import provider  # noqa: F401


@pytest.mark.asyncio
async def test_gmail_send_uses_arrival_and_skips_count(monkeypatch):
    saved = datetime(2026, 10, 26, 12, 0, 0, 500000, tzinfo=timezone.utc)
    queries = []
    def raw(mid, arrival, sender='Alice <alice@example.org>'):
        return {'id': mid, 'threadId': 't', 'internalDate': str(int(arrival.timestamp() * 1000)),
                'payload': {'headers': [{'name': 'From', 'value': sender}, {'name': 'Date', 'value': 'broken'}]}}
    rows = {'target': raw('target', saved - timedelta(days=1)),
            'new': raw('new', saved.replace(microsecond=0)),
            'other': raw('other', saved, 'Alice <other@example.org>')}
    async def request(method, url, *, account_id, params):
        if url.endswith('/threads/t'):
            return {'messages': list(rows.values())}
        if url.endswith('/messages'):
            queries.append(params['q'])
            return {'messages': [{'id': key} for key in rows]}
        return rows[url.rsplit('/', 1)[-1]]
    monkeypatch.setattr(gmail.client, 'request_json', request)
    target, thread, sender, count = await gmail.guard_data('account', 'target', since=saved)
    assert target.message_id == 'target'
    assert {m.message_id for m in thread} == {'new', 'other'}
    assert [m.message_id for m in sender] == ['new']
    assert count == 0 and len(queries) == 1 and 'newer_than' not in queries[0]


@pytest.mark.asyncio
async def test_gmail_missing_thread_is_actionable(monkeypatch):
    async def metadata(*args):
        return {'id': 'target', 'internalDate': '1', 'payload': {'headers': []}}
    monkeypatch.setattr(gmail, '_metadata', metadata)
    with pytest.raises(DraftRefused, match='Send a new email'):
        await gmail.guard_data('account', 'target')


@pytest.mark.asyncio
async def test_imap_send_filters_at_saved_second_and_skips_count(provider):
    since = datetime(2026, 10, 26, 13, 0, 0, 900000, tzinfo=timezone.utc)
    target, thread, sender, count = await host.guard_data('account', read.mint_message_id('account', 42, 1), since)
    assert target.message_id.endswith('-1')
    assert not thread
    assert {item.message_id.rsplit('-', 1)[-1] for item in sender} == {'3', '4'}
    assert count == 0
    searches = [c for c in provider.commands if c[0] == 'SEARCH' and c[2] == 'FROM']
    assert len(searches) == 2


@pytest.mark.asyncio
async def test_imap_missing_message_id_and_sent_folder_are_actionable(provider):
    raw, flags = provider.mail['INBOX'][1]
    provider.mail['INBOX'][1] = (raw.replace(b'Message-ID: <a>\n', b''), flags)
    with pytest.raises(DraftRefused, match='Message-ID.*Send a new email'):
        await host.guard_data('account', read.mint_message_id('account', 42, 1))
    provider.mail['INBOX'][1] = (raw, flags)
    provider.listing = b'() "/" "Other"'
    with pytest.raises(DraftRefused, match='Sent folder.*Create a Sent folder'):
        await host.guard_data('account', read.mint_message_id('account', 42, 1))
