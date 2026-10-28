"""Sent-copy outcomes after an SMTP submission has succeeded."""

import logging
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from connections.mailhost import outbound
from connections.mailhost.errors import MailHostError, SERVER_UNREACHABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("search", "append", "missing", "append_calls"),
    [(True, True, False, 0), (False, True, False, 1), ("error", True, False, 1),
     ("error", False, True, 1), (False, "error", True, 1)],
)
async def test_sent_copy_check_and_fallback(monkeypatch, caplog, search, append, missing, append_calls):
    account = outbound.Sender("account", "sender@example.com", SimpleNamespace(username="sender"), "sender", "secret", {})
    message = EmailMessage()
    message["To"] = "recipient@example.com"
    calls = []

    async def guarded(_account_id, _check, call):
        return call()

    def has_copy(*_args):
        if search == "error":
            raise MailHostError(SERVER_UNREACHABLE)
        return search

    async def keep_copy(*_args):
        calls.append(True)
        if append == "error":
            raise MailHostError(SERVER_UNREACHABLE)
        return append

    monkeypatch.setattr(outbound.read, "_guarded", guarded)
    monkeypatch.setattr(outbound.smtp, "send_message", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(outbound.imap, "has_copy", has_copy)
    monkeypatch.setattr(outbound, "_keep_copy", keep_copy)
    with caplog.at_level(logging.WARNING):
        result = await outbound.send(account, message)
    assert result.sent_copy_missing is missing
    assert len(calls) == append_calls
    assert ("Sent-copy check failed" in caplog.text) is (search == "error")
    if append == "error":
        assert "Sent copy was not saved" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["search", "append"])
async def test_programming_errors_are_not_silenced(monkeypatch, phase):
    account = outbound.Sender("account", "sender@example.com", SimpleNamespace(username="sender"), "sender", "secret", {})
    message = EmailMessage()

    async def guarded(_account_id, _check, call):
        return call()

    def has_copy(*_args):
        if phase == "search":
            raise ValueError("bug")
        return False

    async def keep_copy(*_args):
        raise ValueError("bug")

    monkeypatch.setattr(outbound.read, "_guarded", guarded)
    monkeypatch.setattr(outbound.smtp, "send_message", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(outbound.imap, "has_copy", has_copy)
    monkeypatch.setattr(outbound, "_keep_copy", keep_copy)
    with pytest.raises(ValueError, match="bug"):
        await outbound.send(account, message)
