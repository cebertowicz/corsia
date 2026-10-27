"""A stale reply journey through the mail-host tools and local TLS fakes."""
import json

import pytest

from mailhost_helpers import authority, conductor, env, servers  # noqa: F401
from test_mailhost_send import _draft, _send, make_host  # noqa: F401
from test_mailhost_threads_folders_invites import ORIGINAL, _id
from tools import mail_guard_runtime
from tools.google_email import EmailSendTool


@pytest.mark.asyncio
async def test_newer_host_message_stops_then_acknowledgement_sends(make_host, monkeypatch):
    host = await make_host()
    host.built.imap.messages[5] = ORIGINAL
    saved = await _draft(host, to=[], cc=[], subject="Lunch", reply_to_message_id=_id(host))
    host.built.imap.messages[6] = (ORIGINAL.replace(b"<orig@example.com>", b"<new@example.com>")
                                   .replace(b"09:00:00", b"10:00:00")
                                   .replace(b"Noon on Friday?", b"New details"))

    async def screen(account, message):
        return json.dumps({"subject": message.headers["subject"],
                           "body": f"<untrusted-email>{message.body}</untrusted-email>"})

    monkeypatch.setattr(mail_guard_runtime, "screened_message", screen)
    stopped = await _send(host, saved)
    assert "Not sent" in stopped and "New details" in stopped
    assert not host.built.smtp.sent
    result = await EmailSendTool().execute(_thread_id=host.thread, draft_id=saved["draft_id"],
                                           revision=saved["revision"], seen_newer=True)
    assert not result.startswith("Error"), result
    assert len(host.built.smtp.sent) == 1
