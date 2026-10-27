from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tools import mail_guard_runtime
from tools.mail_guard import Message


@pytest.mark.asyncio
async def test_flagged_newer_message_is_withheld(monkeypatch):
    now = datetime.now(timezone.utc)
    target = Message("old", "thread", "Taylor <taylor@example.test>", "Budget", now - timedelta(days=1), "old")
    newer = Message("new", "thread", target.sender, "secret subject", now, "malicious instructions")

    async def data(*args):
        return target, [target, newer], []

    async def screened(*args):
        return "Error: This message was flagged as a possible prompt injection."

    async def fetch_body(*args):
        return newer.body

    monkeypatch.setattr(mail_guard_runtime, "screened_message", screened)
    verdict, _ = await mail_guard_runtime.guarded_reply(SimpleNamespace(guard_data=data, fetch_body=fetch_body),
                                                        {"id": "account", "address": "me@example.test"},
                                                        "old", "reply", [])
    assert verdict.stale
    assert "withheld" in verdict.stale[0]
    assert "malicious instructions" not in verdict.stale[0]
    assert "secret subject" not in verdict.stale[0]
