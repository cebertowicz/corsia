from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from types import SimpleNamespace
import re

import pytest

from connections.mailhost import folders, guard_read, read


def message(mid, sender, date, *, refs='', html=False):
    msg = EmailMessage()
    msg['Message-ID'] = f'<{mid}>'
    msg['From'] = sender
    msg['Date'] = date
    msg['Subject'] = 'Topic'
    if refs:
        msg['References'] = refs
    msg.set_content('<font face="Garamond">hi</font>' if html else 'hello', subtype='html' if html else 'plain')
    return msg.as_bytes()


class FakeIMAP:
    def __init__(self):
        self.mail = {
            'INBOX': {1: (message('a', 'Alice <alice@example.org>', 'Mon, 26 Oct 2026 10:00:00 +0000'), b'\\Seen'),
                      2: (message('c', 'Bob <bob@example.org>', 'Mon, 26 Oct 2026 12:00:00 +0000', refs='<b>'), b''),
                      3: (message('other', 'Alice <alice@example.org>', 'Mon, 26 Oct 2026 13:00:00 +0000'), b''),
                      4: (message('other2', 'Alice <alice@example.org>', 'Mon, 26 Oct 2026 14:00:00 +0000'), b'')},
            'Sent Items': {5: (message('b', 'Me <me@example.org>', 'Mon, 26 Oct 2026 11:00:00 +0000', refs='<a>', html=True), b'\\Seen')},
        }
        self.folder = ''
        self.fail = None
        self.listing = b'(\\Sent) "/" "Sent Items"'
        self.commands = []

    def list(self):
        self.commands.append(('LIST',))
        return ('NO', [b'failure']) if self.fail == 'LIST' else ('OK', [self.listing])

    def select(self, folder, readonly=False):
        self.folder = folder.strip('"')
        self.commands.append(('SELECT', self.folder))
        return ('NO', []) if self.fail == 'SELECT' else ('OK', [b'1'])

    def response(self, name):
        return name, [b'42' if self.folder == 'INBOX' else b'43']

    def uid(self, command, *args):
        self.commands.append((command, self.folder, *args))
        if self.fail == command:
            return 'NO', [b'failure']
        if command == 'SEARCH':
            ids = self.mail[self.folder]
            if args[0] == 'FROM':
                ids = {uid: pair for uid, pair in ids.items() if b'alice@example.org' in pair[0]}
            elif args[0] != 'ALL':
                mids = re.findall(r'"(<[^>]+>)"', args[0])
                ids = {uid: pair for uid, pair in ids.items() if any(mid.encode() in pair[0].split(b'\n\n')[0] for mid in mids)}
            return 'OK', [' '.join(map(str, ids)).encode()]
        output = []
        for uid in map(int, args[0].split(',')):
            if uid not in self.mail[self.folder]:
                continue
            raw, flags = self.mail[self.folder][uid]
            if 'HEADER.FIELDS' in args[1]:
                raw = raw.split(b'\n\n')[0] + b'\n\n'
            arrival = parsedate_to_datetime(str(BytesParser(policy=policy.default).parsebytes(self.mail[self.folder][uid][0])['Date']))
            stamp = arrival.strftime('%d-%b-%Y %H:%M:%S %z').encode()
            output.extend([(f'1 (UID {uid} INTERNALDATE "'.encode() + stamp + b'" FLAGS (' + flags + b') BODY[]', raw), b')'])
        return 'OK', output


@pytest.fixture
def provider(monkeypatch):
    conn = FakeIMAP()
    ep = SimpleNamespace(imap=SimpleNamespace(host='mock'), imap_port=lambda: 993)
    folders.forget(('mock:993', 'user'))
    @contextmanager
    def session(*args):
        yield conn
    async def in_db(func, *args):
        return ep, 'user', 'secret', {}
    async def guarded(account, check, call):
        return call()
    monkeypatch.setattr(guard_read.imap, 'session', session)
    monkeypatch.setattr(guard_read, 'run_in_db', in_db)
    monkeypatch.setattr(read, 'guarded', guarded)
    yield conn
    folders.forget(('mock:993', 'user'))


@pytest.mark.asyncio
async def test_batched_search_thread_seen_sender_and_lazy_body(provider):
    target, thread, sender, count = await guard_read.guard_data('account', read.mint_message_id('account', 42, 1))
    assert count == 3
    assert len(thread) == 3
    assert len({m.thread_id for m in thread}) == 1
    assert len(sender) == 3
    assert next(m for m in thread if m.message_id.endswith('-2')).unread
    assert next(m for m in sender if m.message_id.endswith('-3')).thread_id != target.thread_id
    assert all(not m.body for m in thread + sender)
    assert target.date == datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
    searches = [c for c in provider.commands if c[0] == 'SEARCH']
    assert len(searches) == 8
    assert not any('ALL' in c[2:] for c in searches)
    sender_searches = [c for c in searches if c[2] == 'FROM' and c[4] == 'SINCE']
    assert len(sender_searches) == 4
    assert {c[5] for c in sender_searches} == {
        '25-Oct-2026', (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%d-%b-%Y')
    }
    assert all('References' in c[2] and 'In-Reply-To' in c[2] and 'Message-ID' in c[2]
               for c in searches if c[2] != 'FROM')
    fetches = [c for c in provider.commands if c[0] == 'FETCH']
    assert all('HEADER.FIELDS' in c[3] and '<0.500000>' in c[3] for c in fetches)
    assert all('ENVELOPE FLAGS' in c[3] for c in fetches[1:])
    assert any(',' in c[2] for c in fetches)
    assert len(fetches) == 4  # target; first-round Inbox/Sent; second-round Inbox
    assert sum(c[0] == 'SELECT' for c in provider.commands) == 5
    assert await guard_read.fetch_body('account', target.message_id) == 'hello'
    sent = next(m for m in thread if m.message_id.startswith('sent:'))
    assert await guard_read.fetch_body('account', sent.message_id) == 'hi'
    assert all('<0.500000>' in c[3] for c in provider.commands if c[0] == 'FETCH')


@pytest.mark.asyncio
async def test_sent_html_is_one_batch_and_target_binding(provider):
    provider.listing = b'() "/" "Sent Items"'
    for uid in range(6, 12):
        provider.mail['Sent Items'][uid] = (message(str(uid), 'Me <me@example.org>',
                                              'Mon, 26 Oct 2026 11:00:00 +0000', html=True), b'\\Seen')
    samples = await guard_read.sent_html('account')
    assert len(samples) == 5
    fetches = [c for c in provider.commands if c[0] == 'FETCH']
    assert len(fetches) == 1 and fetches[0][2] == '7,8,9,10,11'
    with pytest.raises(read.MessageNotFound):
        await guard_read.guard_data('different', read.mint_message_id('account', 42, 1))
    with pytest.raises(read.MessageNotFound):
        await guard_read.guard_data('account', read.mint_message_id('account', 41, 1))
    with pytest.raises(read.MessageNotFound):
        await guard_read.fetch_body('different', f'sent:{read.account_tag("account")}:43:5')


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['LIST', 'SELECT', 'SEARCH', 'FETCH'])
async def test_provider_failures_never_silently_pass(provider, failure):
    provider.fail = failure
    with pytest.raises(Exception):
        await guard_read.guard_data('account', read.mint_message_id('account', 42, 1))
    assert not any(c[0] == 'SEARCH' and c[2] == 'ALL' for c in provider.commands)
