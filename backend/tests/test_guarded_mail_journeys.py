"""Guarded mail journeys through the Gmail tool and mocked HTTP provider."""
from __future__ import annotations

import base64
import json
from copy import deepcopy

import httpx
import pytest

from tests.test_google_email import (  # fixtures and existing provider fake
    GMAIL, THREADED, _draft, _mime, _row, approve, client_file, conductor, google, mail,
)
from tools import mail_guard_runtime
from tools.google_email import EmailDraftTool, EmailSendTool


def newer():
    result = deepcopy(THREADED)
    result['id'] = 'm-101'
    result['internalDate'] = str(int(THREADED['internalDate']) + 1000)
    result['payload']['headers'][0]['value'] = '<new@mail.example>'
    result['payload']['mimeType'] = 'text/plain'
    result['payload']['body'] = {'data': base64.urlsafe_b64encode(b'New details from Pat').decode()}
    return result


def provider_state(mail, messages, *, sent_html=None):
    inner = mail.fake.handler
    mail.messages.update({m['id']: m for m in messages})

    def handle(request):
        url = str(request.url).split('?')[0]
        if url == f'{GMAIL}/threads/t-9':
            return httpx.Response(200, json={'messages': messages})
        if sent_html is not None and url == f'{GMAIL}/messages' and request.url.params.get('q') == 'in:sent':
            return httpx.Response(200, json={'messages': [{'id': 'sent-sample'}]})
        if sent_html is not None and url == f'{GMAIL}/messages/sent-sample':
            data = base64.urlsafe_b64encode(sent_html.encode()).decode()
            return httpx.Response(200, json={'id': 'sent-sample', 'payload': {'mimeType': 'text/html', 'body': {'data': data}}})
        return inner(request)

    mail.fake.handler = handle


@pytest.fixture
def clean_screen(monkeypatch):
    async def screen(account_id, message):
        return json.dumps({'subject': message.headers['subject'], 'body': f'<untrusted-email>{message.body}</untrusted-email>'})
    monkeypatch.setattr(mail_guard_runtime, 'screened_message', screen)


async def send(mail, draft, **kwargs):
    return await EmailSendTool().execute(_thread_id=mail.thread, draft_id=draft['draft_id'], revision=draft['revision'], **kwargs)


async def test_stale_at_draft_refused_then_retarget_succeeds(mail, approve, clean_screen):
    fresh = newer()
    provider_state(mail, [THREADED, fresh])
    stopped = await EmailDraftTool().execute(_thread_id=mail.thread, account_id=mail.account, subject='Lunch', body='Yes', reply_to_message_id='m-100')
    assert stopped.startswith('Error:') and 'New details from Pat' in stopped
    assert '<untrusted-email>' in stopped and not mail.sends
    saved = await EmailDraftTool().execute(_thread_id=mail.thread, account_id=mail.account, subject='Lunch', body='Yes', reply_to_message_id='m-101')
    draft = json.loads(saved)
    assert json.loads(_row(draft['draft_id'])['target_json'])['reply_to'] == 'm-101'
    result = await send(mail, draft)
    assert not result.startswith('Error'), result
    assert _mime(mail.sends[0])['In-Reply-To'] == '<new@mail.example>'


async def test_new_mail_after_draft_stops_send_until_seen_newer(mail, approve, clean_screen):
    saved = await _draft(mail, to=[], reply_to_message_id='m-100')
    provider_state(mail, [THREADED, newer()])
    stopped = await send(mail, saved)
    assert 'Not sent' in stopped and 'New details from Pat' in stopped
    assert not mail.sends
    result = await send(mail, saved, seen_newer=True)
    assert not result.startswith('Error'), result
    assert len(mail.sends) == 1
    message = _mime(mail.sends[0])
    assert message['To'] == 'pat@mail.example'
    assert message['In-Reply-To'] == '<abc@mail.example>'
    assert mail.sends[0]['threadId'] == 't-9'


async def test_font_and_attachment_in_sent_mime_and_missing_file_refused(mail, approve):
    provider_state(mail, [THREADED], sent_html='<span style="font-family: Garamond; font-size: small">sent</span>')
    (mail.workspace / 'report.txt').write_text('attachment contents')
    bad = await EmailDraftTool().execute(_thread_id=mail.thread, account_id=mail.account, to=['pat@mail.example'], subject='Report', body='Here', attachments=['missing.txt'])
    assert 'missing' in bad.lower() and not mail.sends
    saved = await _draft(mail, to=['pat@mail.example'], attachments=['report.txt'])
    document = (mail.workspace / saved['path']).read_text()
    assert 'report.txt' in document and '19 bytes' in document
    result = await send(mail, saved)
    assert not result.startswith('Error'), result
    message = _mime(mail.sends[0])
    html = message.get_body(preferencelist=('html',)).get_content()
    assert 'Garamond' in html and 'small' in html
    assert 'Noon works?' in message.get_body(preferencelist=('plain',)).get_content()
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == 'report.txt'
    assert attachment.get_payload(decode=True) == b'attachment contents'
