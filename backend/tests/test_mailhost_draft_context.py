"""A mail-host reply draft in the draft view: its conversation in one IMAP session, and older drafts still sending.

Servers are the local TLS fakes in ``mailhost_fakes``; the route is called as the signed-in person reaches it.
"""

from __future__ import annotations

from email import message_from_bytes, policy

import pytest
from mailhost_helpers import authority, conductor, env, servers  # noqa: F401  (fixtures)
from test_mailhost_send import _draft, _send, make_host  # noqa: F401  (fixtures)
from test_mailhost_threads_folders_invites import ORIGINAL, _id

from api.email_drafts import draft_context
from tests.test_email_drafts_api import _artifact, _owner

pytestmark = pytest.mark.asyncio

ANSWER = (ORIGINAL.replace(b"Message-ID: <orig@example.com>", b"Message-ID: <answer@example.com>")
          .replace(b"References: <root@example.com>", b"In-Reply-To: <orig@example.com>\r\nReferences: <root@example.com> <orig@example.com>")
          .replace(b"From: Sam <sam@example.com>", b"From: Lee <lee@example.com>")
          .replace(b"Noon on Friday?", b"Friday works."))


async def test_the_conversation_is_read_in_one_session_with_one_search_and_one_fetch_per_folder(make_host) -> None:
    host = await make_host()
    # The fake answers every SEARCH with its whole inbox, so the inbox holds only this conversation.
    host.built.imap.messages.clear()
    host.built.imap.messages.update({5: ORIGINAL, 6: ANSWER})
    saved = await _draft(host, to=[], cc=[], subject="Lunch plans", reply_to_message_id=_id(host))
    imap = host.built.imap
    commands, searches, sent_folder = len(imap.commands), len(imap.searches), len(imap.uid_commands)
    context = await draft_context(_artifact(saved), _owner(host.thread))
    assert context["status"] == "ready" and context["replied_to"] == _id(host)
    assert [m["body"].strip() for m in context["conversation"]] == ["Noon on Friday?", "Friday works."]
    assert context["answered"] == ["sam@example.com", "you@example.com"]
    verbs = [verb for _, verb in imap.commands[commands:]]
    assert verbs.count("LOGIN") == 1
    # Inbox: one SEARCH for the draft's thread Message-IDs and one FETCH; Sent: one SEARCH that found nothing to fetch.
    [inbox_search] = imap.searches[searches:]
    assert "<orig@example.com>" in inbox_search and "<root@example.com>" in inbox_search
    assert verbs.count("UID") == 3
    assert [c.split(" ", 1)[0].upper() for c in imap.uid_commands[sent_folder:]] == ["SEARCH"]


async def test_a_reply_draft_saved_with_the_old_in_reply_to_line_still_sends_threaded(make_host) -> None:
    host = await make_host()
    host.built.imap.messages[5] = ORIGINAL
    saved = await _draft(host, to=[], cc=[], subject="Lunch plans", reply_to_message_id=_id(host))
    path = host.workspace / saved["path"]
    path.write_text(path.read_text().replace("Subject: Lunch plans\n", 'Subject: Lunch plans\nIn reply to: "Lunch plans" from Sam\n'))
    out = await _send(host, saved)
    assert not out.startswith("Error"), out
    [(_, raw)] = host.built.smtp.sent
    message = message_from_bytes(raw, policy=policy.default)
    assert message["In-Reply-To"] == "<orig@example.com>"
    body = message.get_body(("plain",))
    assert body is not None
    assert "In reply to" not in body.get_content()
