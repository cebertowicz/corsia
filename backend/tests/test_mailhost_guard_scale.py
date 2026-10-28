import pytest

from connections.mailhost import guard_read, read
from tests.test_mailhost_guard_read import message, provider  # noqa: F401


@pytest.mark.asyncio
async def test_large_sender_set_uses_batched_headers(provider):
    for uid in range(100, 700):
        provider.mail['INBOX'][uid] = (message(f'large{uid}', 'Alice <alice@example.org>',
                                              'Mon, 26 Oct 2026 14:00:00 +0000'), b'')
    _, _, sender, count = await guard_read.guard_data('account', read.mint_message_id('account', 42, 1))
    assert count == 603
    assert len(sender) == 603
    fetches = [c for c in provider.commands if c[0] == 'FETCH']
    assert len(fetches) == 4
    assert all('HEADER.FIELDS' in c[3] for c in fetches)
    assert all(not m.body for m in sender)
