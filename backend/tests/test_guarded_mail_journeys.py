"""Guarded mail journeys through the Gmail tool and mocked HTTP provider."""
from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from infra.database import get_db
from tests.test_action_gate import _admit, _bind

import httpx
import pytest

from tests.test_google_email import (  # fixtures and existing provider fake
    GMAIL, THREADED, _draft, _mime, _row, approve, client_file, conductor, google, mail,
)
from tools import google_email, mail_guard_runtime
from tools.google_email import EmailDraftTool, EmailSendTool
from tools.reviewed_drafts import working_root


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


async def test_stale_at_draft_can_be_retargeted(mail, approve, clean_screen):
    fresh = newer()
    provider_state(mail, [THREADED, fresh])
    saved = await EmailDraftTool().execute(_thread_id=mail.thread, account_id=mail.account, subject='Lunch', body='Yes', reply_to_message_id='m-100')
    draft = json.loads(saved)
    document = (mail.workspace / draft['path']).read_text()
    assert 'New details from Pat' in document and '<untrusted-email>' in document
    assert 'quoted below the reply' in document
    saved = await EmailDraftTool().execute(_thread_id=mail.thread, account_id=mail.account, subject='Lunch', body='Yes', reply_to_message_id='m-101')
    retargeted = json.loads(saved)
    document = (mail.workspace / retargeted['path']).read_text()
    assert '<untrusted-email>New details from Pat</untrusted-email>' in document
    assert document.index('New details from Pat') < document.index('quoted below the reply')
    assert json.loads(_row(retargeted['draft_id'])['target_json'])['reply_to'] == 'm-101'
    result = await send(mail, retargeted)
    assert not result.startswith('Error'), result
    assert _mime(mail.sends[-1])['In-Reply-To'] == '<new@mail.example>'


async def test_new_mail_after_draft_stops_send_until_new_draft(mail, approve, clean_screen):
    saved = await _draft(mail, to=[], reply_to_message_id='m-100')
    now = datetime.now(timezone.utc)
    db = get_db()
    _backdate(saved, now - timedelta(seconds=10))
    provider_state(mail, [THREADED, _arrival(newer(), now - timedelta(seconds=5))])
    stopped = await send(mail, saved)
    assert 'Not sent' in stopped and 'New details from Pat' in stopped
    assert not mail.sends
    assert 'seen_newer' not in EmailSendTool().parameters['properties']
    replacement = await _draft(mail, to=[], reply_to_message_id='m-101')
    result = await send(mail, replacement)
    assert not result.startswith('Error'), result
    assert len(mail.sends) == 1
    message = _mime(mail.sends[0])
    assert message['To'] == 'pat@mail.example'
    assert message['In-Reply-To'] == '<new@mail.example>'
    assert mail.sends[0]['threadId'] == 't-9'


async def test_font_and_attachment_in_sent_mime_and_missing_file_refused(mail, approve):
    provider_state(mail, [THREADED], sent_html='<span style="font-family: Garamond; font-size: small">sent</span>')
    working_root(mail.thread).mkdir(parents=True, exist_ok=True)
    (working_root(mail.thread) / 'report.txt').write_text('attachment contents')
    bad = await EmailDraftTool().execute(_thread_id=mail.thread, account_id=mail.account, to=['pat@mail.example'], subject='Report', body='Here', attachments=['missing.txt'])
    assert 'missing' in bad.lower() and not mail.sends
    saved = await _draft(mail, to=['pat@mail.example'], attachments=['report.txt'])
    document = (mail.workspace / saved['path']).read_text()
    assert 'Attachment: report.txt (0.019 KB)' in document
    result = await send(mail, saved)
    assert not result.startswith('Error'), result
    message = _mime(mail.sends[0])
    html = message.get_body(preferencelist=('html',)).get_content()
    assert 'Garamond' in html and 'small' in html
    assert 'Noon works?' in message.get_body(preferencelist=('plain',)).get_content()
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == 'report.txt'
    assert attachment.get_payload(decode=True) == b'attachment contents'


def _arrival(message, when):
    item = deepcopy(message)
    item['internalDate'] = str(int(when.timestamp() * 1000))
    return item


def _backdate(saved, when):
    target = json.loads(_row(saved['draft_id'])['target_json'])
    target['guard_read_at'] = when.isoformat()
    db = get_db()
    db.execute('UPDATE action_drafts SET recorded_at=?, target_json=? WHERE id=?',
               (when.isoformat(), json.dumps(target), saved['draft_id']))
    db.commit()


def _person_reply(thread, turn):
    db = get_db()
    _admit(db, turn, thread_id=thread)
    _bind(db, turn, thread_id=thread, call_id=f'call-{turn}')


async def test_arrival_after_approval_requires_new_draft_and_new_person_reply(mail, clean_screen):
    saved = await _draft(mail, to=[], reply_to_message_id='m-100')
    # Make the recorded save earlier than the arrival, without sleeping.
    db = get_db()
    prior = datetime.now(timezone.utc) - timedelta(seconds=10)
    _backdate(saved, prior)
    _person_reply(mail.thread, 'reply-before-arrival')
    arrived = _arrival(newer(), datetime.now(timezone.utc) - timedelta(seconds=2))
    provider_state(mail, [THREADED, arrived])
    stopped = await send(mail, saved)
    assert 'Not sent' in stopped and 'New details from Pat' in stopped
    assert not mail.sends
    replacement = await _draft(mail, to=[], reply_to_message_id='m-101')
    refused = await send(mail, replacement)
    assert 'hasn\'t replied' in refused and not mail.sends
    db.execute("UPDATE active_turns SET status='completed' WHERE id=?", ('reply-before-arrival',))
    db.commit()
    _person_reply(mail.thread, 'reply-after-new-draft')
    result = await send(mail, replacement)
    assert not result.startswith('Error'), result
    assert len(mail.sends) == 1
    assert _mime(mail.sends[0])['In-Reply-To'] == '<new@mail.example>'
    assert 'seen_newer' not in EmailSendTool().parameters['properties']


async def test_reading_other_thread_after_draft_does_not_stop_send(mail, approve, clean_screen):
    other = _arrival(newer(), datetime.now(timezone.utc) - timedelta(seconds=5))
    other['threadId'] = 't-other'
    other['labelIds'] = ['UNREAD']
    mail.messages[other['id']] = other
    inner = mail.fake.handler
    def handler(request):
        if str(request.url).split('?')[0] == f'{GMAIL}/messages' and request.url.params.get('q', '').startswith('from:'):
            return httpx.Response(200, json={'messages': [{'id': other['id']}]})
        return inner(request)
    mail.fake.handler = handler
    saved = await _draft(mail, to=[], reply_to_message_id='m-100')
    other['labelIds'] = []
    result = await send(mail, saved)
    assert not result.startswith('Error'), result
    assert len(mail.sends) == 1


async def test_malformed_date_header_does_not_prevent_draft_or_send(mail, approve):
    target = deepcopy(THREADED)
    target['payload']['headers'].append({'name': 'Date', 'value': 'not a date'})
    provider_state(mail, [target])
    saved = await _draft(mail, to=[], reply_to_message_id='m-100')
    result = await send(mail, saved)
    assert not result.startswith('Error'), result
    assert len(mail.sends) == 1


async def test_missing_message_id_refuses_reply_with_specific_next_step(mail):
    target = deepcopy(THREADED)
    target['payload']['headers'] = [h for h in target['payload']['headers'] if h['name'] != 'Message-ID']
    provider_state(mail, [target])
    result = await EmailDraftTool().execute(_thread_id=mail.thread, account_id=mail.account,
                                            subject='Lunch', body='Yes', reply_to_message_id='m-100')
    assert 'message-id' in result.lower() and 'new email' in result.lower(), result
    assert not mail.sends


async def test_missing_sender_with_reply_to_refuses_with_next_step(mail):
    target = deepcopy(THREADED)
    target['payload']['headers'] = [h for h in target['payload']['headers'] if h['name'] != 'From']
    target['payload']['headers'].append({'name': 'Reply-To', 'value': 'reply@mail.example'})
    provider_state(mail, [target])
    result = await EmailDraftTool().execute(_thread_id=mail.thread, account_id=mail.account,
                                            subject='Lunch', body='Yes', reply_to_message_id='m-100')
    assert 'no sender address' in result.lower() and 'reply@mail.example' in result
    assert 'new email' in result.lower() and not mail.sends


async def test_font_read_failure_still_saves_and_sends_with_default(mail, approve):
    inner = mail.fake.handler
    def handler(request):
        if str(request.url).split('?')[0] == f'{GMAIL}/messages' and request.url.params.get('q') == 'in:sent':
            raise httpx.ConnectError('font sample unavailable', request=request)
        return inner(request)
    mail.fake.handler = handler
    saved = await _draft(mail, to=['pat@mail.example'])
    note = "Your usual font couldn't be read; your mail's default font will be used if it still can't be read when sending."
    assert note in (mail.workspace / saved['path']).read_text()
    assert saved['font_note'] == note
    result = await send(mail, saved)
    assert not result.startswith('Error'), result
    assert len(mail.sends) == 1
    assert _mime(mail.sends[0]).get_body(preferencelist=('html',)) is not None


async def test_twenty_megabyte_gmail_attachment_is_refused_when_drafting(mail):
    working_root(mail.thread).mkdir(parents=True, exist_ok=True)
    (working_root(mail.thread) / 'large.bin').write_bytes(b'x' * (20 * 1024 * 1024))
    result = await EmailDraftTool().execute(_thread_id=mail.thread, account_id=mail.account,
                                            to=['pat@mail.example'], subject='File', body='Here',
                                            attachments=['large.bin'])
    assert 'draft' in result.lower() and ('large' in result.lower() or 'size' in result.lower()), result
    assert not mail.sends


async def test_draft_has_no_tool_names_or_provider_ids(mail, clean_screen):
    provider_state(mail, [THREADED, newer()])
    saved = await _draft(mail, to=[], reply_to_message_id='m-100')
    document = (mail.workspace / saved['path']).read_text()
    for internal in ('email_send', 'email_draft', 'email_read', 'seen_newer', 'm-100', 'm-101', 't-9'):
        assert internal not in document


async def test_arrival_during_draft_save_stops_send(mail, approve, clean_screen, monkeypatch):
    save = google_email.save_draft

    async def save_after_arrival(**kwargs):
        cutoff = datetime.fromisoformat(kwargs['target']['guard_read_at'])
        arrived = _arrival(newer(), cutoff + timedelta(seconds=2))
        provider_state(mail, [THREADED, arrived])
        saved = await save(**kwargs)
        db = get_db()
        db.execute('UPDATE action_drafts SET recorded_at=? WHERE id=?',
                   ((cutoff + timedelta(seconds=4)).isoformat(), saved['draft_id']))
        db.commit()
        return saved

    monkeypatch.setattr(google_email, 'save_draft', save_after_arrival)
    saved = await _draft(mail, to=[], reply_to_message_id='m-100')
    result = await send(mail, saved)
    assert 'Not sent' in result and 'New details from Pat' in result
    assert not mail.sends


async def test_older_draft_uses_saved_time_for_arrival_check(mail, approve, clean_screen):
    saved = await _draft(mail, to=[], reply_to_message_id='m-100')
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=10)
    target = json.loads(_row(saved['draft_id'])['target_json'])
    target.pop('guard_read_at')
    db = get_db()
    db.execute('UPDATE action_drafts SET recorded_at=?, target_json=? WHERE id=?',
               (cutoff.isoformat(), json.dumps(target), saved['draft_id']))
    db.commit()
    provider_state(mail, [THREADED, _arrival(newer(), cutoff + timedelta(seconds=5))])
    result = await send(mail, saved)
    assert 'Not sent' in result and 'New details from Pat' in result
    assert not mail.sends


async def test_same_second_arrival_stops_but_target_itself_does_not(mail, approve, clean_screen):
    saved = await _draft(mail, to=[], reply_to_message_id='m-100')
    recorded = datetime.fromisoformat(_row(saved['draft_id'])['recorded_at'])
    same_second = _arrival(newer(), recorded.replace(microsecond=0))
    for header in same_second['payload']['headers']:
        if header['name'] == 'From':
            header['value'] = 'Another person <other@mail.example>'
    provider_state(mail, [THREADED, same_second])
    result = await send(mail, saved)
    assert 'Not sent' in result and 'New details from Pat' in result
    assert not mail.sends


async def test_thirty_day_sender_count_counts_messages_without_fetching_each(mail):
    inner = mail.fake.handler
    count_only_ids = {'count-1', 'count-2', 'count-3'}
    count_queries = []
    count_fetches = []
    def handler(request):
        url = str(request.url).split('?')[0]
        query = request.url.params.get('q', '')
        if url == f'{GMAIL}/messages' and 'newer_than:30d' in query:
            count_queries.append(query)
            return httpx.Response(200, json={'messages': [{'id': mid, 'threadId': 'one-thread'} for mid in sorted(count_only_ids)]})
        if url.rsplit('/', 1)[-1] in count_only_ids:
            count_fetches.append(url)
        return inner(request)
    mail.fake.handler = handler
    saved = await _draft(mail, to=[], reply_to_message_id='m-100')
    document = (mail.workspace / saved['path']).read_text().lower()
    assert count_queries and 'from:pat@mail.example' in count_queries[0]
    assert re.search(r'\b3\s+emails?\b', document), document
    assert not count_fetches


async def test_same_second_target_is_not_a_new_arrival(mail, approve):
    saved = await _draft(mail, to=[], reply_to_message_id='m-100')
    recorded = datetime.fromisoformat(_row(saved['draft_id'])['recorded_at'])
    target = _arrival(THREADED, recorded.replace(microsecond=0))
    provider_state(mail, [target])
    result = await send(mail, saved)
    assert not result.startswith('Error'), result
    assert len(mail.sends) == 1
