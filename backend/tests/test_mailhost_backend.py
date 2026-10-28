"""The one mail backend seam: host Drafts copies kept by Message-ID, named refusal causes, and connect off the DB pool.

Same fixtures as ``test_mailhost_send``: local TLS fakes, the real draft and send tools, the approved-draft gate stubbed.
"""

from __future__ import annotations

import json
import threading
from email import message_from_bytes, policy

import pytest
from mailhost_helpers import ADDRESS, authority, conductor, env, servers  # noqa: F401  (fixtures)
from test_mailhost_send import _draft, _send, make_host  # noqa: F401  (fixtures and helpers)
from test_mailhost_threads_folders_invites import ORIGINAL, _id

from connections.account_errors import AccountError
from connections.mailhost import account, dns, imap, store
from connections.mailhost.endpoints import Endpoints, Server
from connections.mailhost.errors import (
    COPY_REFUSED, MESSAGE_REFUSED, PASSWORD_REFUSED, RECIPIENTS_REFUSED, SENDER_REFUSED, MailHostError,
)
from infra.database import get_db
from tools import mailhost_backend
from tools.google_email import EmailDraftTool
from tools.mail_backends import NO_BACKEND, backend_for

pytestmark = pytest.mark.asyncio


def _message_id(raw: bytes) -> str:
    return str(message_from_bytes(raw, policy=policy.default)["Message-ID"])


def _host_name(host) -> str:
    db = get_db()
    try:
        return store.read(host.account_id, db=db)[0].name
    finally:
        db.close()


async def _update(host, saved: dict, **fields) -> dict:
    return await _draft(host, draft_id=saved["draft_id"], **fields)


async def test_drafts_and_sent_copies_carry_the_flags_a_mail_app_would_set(make_host) -> None:
    host = await make_host()
    await _send(host, await _draft(host))
    assert host.built.imap.append_flags == [("Drafts", "\\Draft \\Seen"), ("Sent", "\\Seen")]


async def test_replacing_a_draft_removes_its_earlier_host_copy(make_host) -> None:
    host = await make_host(imap={"uidplus": True})
    first = await _draft(host)
    second = await _update(host, first, body="One o'clock instead?")
    [old, new] = [raw for folder, raw in host.built.imap.appended if folder == "Drafts"]
    assert _message_id(old) != _message_id(new)
    assert host.built.imap.kept("Drafts") == [new]
    assert "note" not in second


async def test_a_confirmed_send_removes_the_drafts_copy(make_host) -> None:
    host = await make_host(imap={"uidplus": True})
    out = json.loads(await _send(host, await _draft(host)))
    assert host.built.imap.kept("Drafts") == [] and len(host.built.imap.kept("Sent")) == 1
    assert "draft_copy_note" not in out


async def test_an_unconfirmed_send_leaves_the_drafts_copy(make_host) -> None:
    host = await make_host(imap={"uidplus": True}, smtp={"drop_after_data": True})
    saved = await _draft(host)
    out = await _send(host, saved)
    assert out == f"Error: {mailhost_backend.EMAIL_UNCONFIRMED}"
    assert len(host.built.imap.kept("Drafts")) == 1
    assert not any(command.upper().startswith("STORE") for command in host.built.imap.uid_commands)


@pytest.mark.parametrize("uidplus", [False, True])
async def test_a_copy_is_never_purged_by_plain_expunge_and_by_uid_only_with_uidplus(make_host, uidplus) -> None:
    host = await make_host(imap={"uidplus": uidplus})
    await _send(host, await _draft(host))
    assert "EXPUNGE" not in {verb for _tls, verb in host.built.imap.commands}
    expunged = [command for command in host.built.imap.uid_commands if command.upper().startswith("EXPUNGE")]
    assert bool(expunged) is uidplus
    left = [flags for folder, _raw, flags in host.built.imap.copies.values() if folder == "Drafts"]
    assert left == ([] if uidplus else ["\\Draft \\Seen \\Deleted"])


async def test_a_failed_removal_is_a_note_and_never_an_error(make_host, monkeypatch) -> None:
    host = await make_host(imap={"uidplus": True})
    first = await _draft(host)

    def refuse(*_args, **_kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(imap, "remove_copy", refuse)
    second = await _update(host, first, body="One o'clock instead?")
    assert second["note"] == mailhost_backend.EARLIER_LEFT
    out = json.loads(await _send(host, second))
    assert out["draft_copy_note"] == "The email was sent, but its draft is still in the account's Drafts folder."
    assert len(host.built.smtp.sent) == 1


async def test_a_refused_append_names_the_copy_refused_cause(make_host) -> None:
    host = await make_host(imap={"refuse_append": True})
    saved = await _draft(host)
    expected = MailHostError(COPY_REFUSED, name=_host_name(host))
    assert saved["note"] == f"{mailhost_backend.NOT_COPIED} {expected}"
    assert host.built.imap.appended == []


@pytest.mark.parametrize(("tuning", "cause"), [
    ({"refuse_sender": True}, SENDER_REFUSED),
    ({"refused_recipients": {"pat@example.com", "lee@example.com"}}, RECIPIENTS_REFUSED),
    ({"refuse_data": True}, MESSAGE_REFUSED),
])
async def test_each_smtp_refusal_has_its_own_cause_and_reopens_the_draft(make_host, tuning, cause) -> None:
    host = await make_host(smtp=tuning)
    saved = await _draft(host)
    out = await _send(host, saved)
    assert out == f"Error: Not sent: {MailHostError(cause, host=_host_name(host))}"
    assert host.built.smtp.sent == []
    assert get_db().execute("SELECT state FROM action_drafts WHERE id=?", (saved["draft_id"],)).fetchone()[0] == "open"


@pytest.mark.parametrize("failure", ["unreachable", "password"])
async def test_an_update_stops_when_the_reply_target_cant_be_read(make_host, failure) -> None:
    host = await make_host()
    host.built.imap.messages[5] = ORIGINAL
    saved = await _draft(host, to=[], cc=[], subject="Lunch plans", reply_to_message_id=_id(host))
    if failure == "unreachable":
        host.built.imap.close()
    else:
        host.built.imap.password = "not-the-app-password"
    updated = await EmailDraftTool().execute(_thread_id=host.thread, account_id=host.account_id,
                                             draft_id=saved["draft_id"], to=["team@example.com"], cc=[],
                                             subject="Re: Lunch plans", body="Changed")
    assert updated.startswith("Error:")
    assert "Changed" not in (host.workspace / saved["path"]).read_text()


async def test_a_new_reply_the_host_refuses_returns_the_named_cause(make_host) -> None:
    host = await make_host()
    host.built.imap.messages[5] = ORIGINAL
    host.built.imap.password = "not-the-app-password"
    out = await EmailDraftTool().execute(
        _thread_id=host.thread, account_id=host.account_id, subject="Re: x", body="Yes", reply_to_message_id=_id(host),
    )
    assert out == f"Error: The draft was not saved. {MailHostError(PASSWORD_REFUSED, name=_host_name(host))}"


async def test_backend_for_refuses_a_provider_no_backend_serves() -> None:
    with pytest.raises(AccountError) as refused:
        backend_for({"id": "acct", "provider": "microsoft"})
    assert str(refused.value) == NO_BACKEND
    assert backend_for({"provider": "mailhost"}) is mailhost_backend.MAIL_HOST


async def test_connecting_an_address_that_has_a_row_does_no_dns_lookup(make_host, monkeypatch) -> None:
    host = await make_host()

    def no_lookup(*_args, **_kwargs):
        raise AssertionError("DNS looked up for an address that already has a row")

    monkeypatch.setattr(dns, "discover", no_lookup)
    again = await account.connect(ADDRESS, thread_id=host.thread)
    assert again["account_id"] == host.account_id and again["already_existed"] is True


async def test_srv_discovery_runs_off_the_db_pool_and_says_where_the_password_goes(conductor, monkeypatch) -> None:
    threads: list[str] = []

    def discover(domain, username, **_kwargs):
        threads.append(threading.current_thread().name)
        return Endpoints(name=domain, known=False, username=username, imap=Server(f"imap.{domain}", 993),
                         smtp=Server(f"smtp.{domain}", 587), caldav=None)

    monkeypatch.setattr(dns, "discover", discover)
    described = await account.connect("pat@srv.test", thread_id=conductor)
    assert len(threads) == 1 and not threads[0].startswith("db")
    assert described["already_existed"] is False
    assert described["password_goes_to"]
