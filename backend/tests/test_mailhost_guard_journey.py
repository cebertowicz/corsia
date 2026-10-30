"""A stale reply journey through the mail-host tools and local TLS fakes."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from tests.test_google_email import _row
from infra.database import get_db

from mailhost_helpers import authority, conductor, env, servers  # noqa: F401
from test_mailhost_send import _draft, _send, make_host  # noqa: F401
from test_mailhost_threads_folders_invites import ORIGINAL, _id
from tools import mail_guard_runtime
from tools.google_email import EmailSendTool


@pytest.mark.asyncio
async def test_newer_host_message_stops_then_new_draft_sends(make_host, monkeypatch):
    host = await make_host()
    host.built.imap.messages[5] = ORIGINAL
    now = datetime.now(timezone.utc)
    dates = {5: now - timedelta(days=1), 6: now - timedelta(seconds=5)}
    _with_internal_dates(host.built.imap, monkeypatch, dates)
    saved = await _draft(host, to=[], cc=[], subject="Lunch", reply_to_message_id=_id(host))
    db = get_db()
    target = json.loads(_row(saved['draft_id'])['target_json'])
    target['guard_read_at'] = (now - timedelta(seconds=10)).isoformat()
    db.execute('UPDATE action_drafts SET target_json=? WHERE id=?',
               (json.dumps(target), saved['draft_id']))
    db.commit()
    host.built.imap.messages[6] = (ORIGINAL.replace(b"<orig@example.com>", b"<new@example.com>")
                                   .replace(b"09:00:00", b"10:00:00")
                                   .replace(b"Noon on Friday?", b"New details"))

    async def screen(account, message):
        return json.dumps({"subject": message.headers["subject"], "screen": {"state": "clean"},
                           "body": f"<untrusted-email>{message.body}</untrusted-email>"})

    monkeypatch.setattr(mail_guard_runtime, "screened_message", screen)
    stopped = await _send(host, saved)
    assert "Not sent" in stopped and "New details" in stopped
    assert not host.built.smtp.sent
    assert 'seen_newer' not in EmailSendTool().parameters['properties']
    replacement = await _draft(host, to=[], cc=[], subject='Lunch', reply_to_message_id=_id(host, uid=6))
    result = await _send(host, replacement)
    assert not result.startswith("Error"), result
    assert len(host.built.smtp.sent) == 1


def _with_internal_dates(imap, monkeypatch, dates):
    original = imap._uid
    def uid(conn, tag, args):
        send = conn.send
        def with_dates(*lines):
            changed = []
            for line in lines:
                if isinstance(line, bytes) and b' FETCH (UID ' in line:
                    for number, arrival in dates.items():
                        marker = f'FETCH (UID {number}'.encode()
                        if marker in line:
                            stamp = arrival.astimezone(timezone.utc).strftime('%d-%b-%Y %H:%M:%S +0000')
                            line = line.replace(marker, marker + f' INTERNALDATE "{stamp}"'.encode(), 1)
                changed.append(line)
            return send(*changed)
        conn.send = with_dates
        try:
            return original(conn, tag, args)
        finally:
            conn.send = send
    monkeypatch.setattr(imap, '_uid', uid)


@pytest.mark.asyncio
async def test_host_malformed_date_header_uses_provider_arrival(make_host, monkeypatch):
    host = await make_host()
    host.built.imap.messages[5] = ORIGINAL.replace(b'Wed, 21 Oct 2026 09:00:00 -0700', b'not a date')
    _with_internal_dates(host.built.imap, monkeypatch, {5: datetime.now(timezone.utc) - timedelta(days=1)})
    saved = await _draft(host, to=[], cc=[], subject='Lunch', reply_to_message_id=_id(host))
    result = await _send(host, saved)
    assert not result.startswith('Error'), result
    assert len(host.built.smtp.sent) == 1


@pytest.mark.asyncio
async def test_host_same_second_new_arrival_is_shown_and_stops_send(make_host, monkeypatch):
    host = await make_host()
    host.built.imap.messages[5] = ORIGINAL
    dates = {5: datetime.now(timezone.utc) - timedelta(days=1)}
    _with_internal_dates(host.built.imap, monkeypatch, dates)
    saved = await _draft(host, to=[], cc=[], subject='Lunch', reply_to_message_id=_id(host))
    recorded = datetime.fromisoformat(_row(saved['draft_id'])['recorded_at'])
    dates[6] = recorded.replace(microsecond=0)
    host.built.imap.messages[6] = (ORIGINAL.replace(b'<orig@example.com>', b'<new@example.com>')
                                   .replace(b'Noon on Friday?', b'New details'))
    async def screen(account, message):
        return json.dumps({'subject': message.headers['subject'], 'screen': {'state': 'clean'}, 'body': message.body})
    monkeypatch.setattr(mail_guard_runtime, 'screened_message', screen)
    result = await _send(host, saved)
    assert 'Not sent' in result and 'New details' in result
    assert not host.built.smtp.sent
