"""Additional guarded mail journeys through the real tools and fake providers."""
import json
from email import message_from_bytes, policy

import httpx
import pytest

from tests.test_guarded_mail_journeys import provider_state
from tests.test_google_email import GMAIL, THREADED, _draft as gmail_draft, approve, client_file, google, mail
from mailhost_helpers import authority, env, servers  # noqa: F401
from test_mailhost_send import _draft as host_draft, _send, make_host  # noqa: F401
from test_mailhost_threads_folders_invites import ORIGINAL, _id
from tools.google_email import EmailDraftTool, EmailSendTool
from tools.reviewed_drafts import working_root

pytestmark = pytest.mark.asyncio


@pytest.fixture
def conductor(isolated_db):
    from infra.database import get_db
    from tests.registry_helpers import make_session
    from infra.corsia_agent import set_corsia_agent
    db = get_db()
    thread_id = make_session(db, prefix='conductor')
    assert set_corsia_agent(thread_id, db=db)
    return thread_id


async def test_gmail_warning_is_in_saved_draft(mail):
    provider_state(mail, [THREADED])
    saved = await gmail_draft(mail, to=['alternate@example.com'], reply_to_message_id='m-100')
    document = (mail.workspace / saved['path']).read_text()
    assert 'differs' in document and 'alternate@example.com' in document
    assert not mail.sends


async def test_gmail_reports_returned_thread_mismatch(mail, approve):
    provider_state(mail, [THREADED])
    saved = await gmail_draft(mail, to=[], reply_to_message_id='m-100')
    inner = mail.fake.handler
    def handler(request):
        if str(request.url).split('?')[0] == f'{GMAIL}/messages/send':
            # Preserve the fixture's capture of the outgoing request.
            inner(request)
            return httpx.Response(200, json={'id': 'sent-1', 'threadId': 'another-thread'})
        return inner(request)
    mail.fake.handler = handler
    result = json.loads(await EmailSendTool().execute(_thread_id=mail.thread, draft_id=saved['draft_id'], revision=saved['revision']))
    assert 'different thread' in result['note']
    assert len(mail.sends) == 1 and mail.sends[0]['threadId'] == 't-9'


async def test_host_warning_is_in_saved_draft(make_host):
    host = await make_host()
    host.built.imap.messages[5] = ORIGINAL
    saved = await host_draft(host, to=['alternate@example.com'], cc=[], reply_to_message_id=_id(host))
    document = (host.workspace / saved['path']).read_text()
    assert 'differs' in document and 'alternate@example.com' in document
    assert not host.built.smtp.sent


async def test_host_missing_and_advertised_oversize_attachments_refused(make_host, monkeypatch):
    host = await make_host()
    smtp = host.built.smtp
    original_session = smtp.session
    def session(conn):
        original_send = conn.send
        def advertise(*lines):
            if lines and lines[0] == '250-localhost':
                lines = (*lines[:-1], '250-SIZE 8', lines[-1])
            return original_send(*lines)
        conn.send = advertise
        return original_session(conn)
    monkeypatch.setattr(smtp, 'session', session)
    common = dict(_thread_id=host.thread, account_id=host.account_id, to=['pat@example.com'], subject='Files', body='Here')
    missing = await EmailDraftTool().execute(**common, attachments=['missing.txt'])
    assert 'missing' in missing.lower() and not smtp.sent
    working_root(host.thread).mkdir(parents=True, exist_ok=True)
    (working_root(host.thread) / 'large.txt').write_bytes(b'0123456789')
    too_large = await EmailDraftTool().execute(**common, attachments=['large.txt'])
    assert ('large' in too_large.lower() or 'size' in too_large.lower()) and too_large.startswith('Error'), too_large
    assert not smtp.sent and not host.built.imap.appended


async def test_host_detected_font_plain_alternative_and_attachment_send(make_host, monkeypatch):
    host = await make_host()
    imap = host.built.imap
    sample = (b'From: person@example.com\r\nSubject: prior\r\nMIME-Version: 1.0\r\n'
              b'Content-Type: text/html; charset=utf-8\r\n\r\n'
              b'<div dir="ltr"><font face="garamond, times new roman, serif" style="font-size:small">Earlier</font></div>')
    imap.copies[77] = ['Sent', sample, '']
    original = imap._copy_uid
    def copy_uid(conn, tag, args, folder):
        if folder == 'Sent' and args.upper() == 'SEARCH ALL':
            return conn.send('* SEARCH 77', f'{tag} OK done')
        if folder == 'Sent' and args.upper().startswith('FETCH 77 '):
            return conn.send(f'* 77 FETCH (UID 77 BODY[]<0> {{{len(sample)}}}'.encode() + b'\r\n' + sample + b')', f'{tag} OK done')
        return original(conn, tag, args, folder)
    monkeypatch.setattr(imap, '_copy_uid', copy_uid)
    working_root(host.thread).mkdir(parents=True, exist_ok=True)
    (working_root(host.thread) / 'report.txt').write_text('attached report')
    saved = await host_draft(host, attachments=['report.txt'])
    document = (host.workspace / saved['path']).read_text()
    assert 'report.txt' in document and '15 bytes' in document
    result = await _send(host, saved)
    assert not result.startswith('Error'), result
    assert len(host.built.smtp.sent) == 1
    message = message_from_bytes(host.built.smtp.sent[0][1], policy=policy.default)
    html = message.get_body(preferencelist=('html',)).get_content().lower()
    assert 'garamond' in html and 'small' in html
    assert 'Noon works?' in message.get_body(preferencelist=('plain',)).get_content()
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == 'report.txt' and attachment.get_payload(decode=True) == b'attached report'
