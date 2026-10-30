import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tools import mail_guard_runtime

from tools.mail_guard import FLAGGED, UNCHECKED, Message


@pytest.mark.asyncio
async def test_flagged_newer_message_is_withheld(monkeypatch):
    now = datetime.now(timezone.utc)
    target = Message("old", "thread", "Taylor <taylor@example.test>", "Budget", now - timedelta(days=1), "old")
    newer = Message("new", "thread", target.sender, "secret subject", now, "malicious instructions")

    async def data(*args):
        return target, [target, newer], [], 0

    async def screened(*args):
        return json.dumps({"screen": {"state": "flagged", "rule": "R1"}, "body": "malicious instructions"})

    async def fetch_body(*args):
        return newer.body

    monkeypatch.setattr(mail_guard_runtime, "screened_message", screened)
    verdict, screened_target = await mail_guard_runtime.guarded_reply(SimpleNamespace(guard_data=data, fetch_body=fetch_body),
                                                                      {"id": "account", "address": "me@example.test"},
                                                                      "old", "reply", [])
    target_evidence = mail_guard_runtime.evidence(screened_target)
    assert FLAGGED in target_evidence
    assert "R1" not in target_evidence and "prompt injection" not in target_evidence
    assert "malicious instructions" not in target_evidence
    assert verdict.stale
    assert FLAGGED in verdict.stale[0]
    assert "malicious instructions" not in verdict.stale[0]
    assert "secret subject" not in verdict.stale[0]


@pytest.mark.asyncio
async def test_unscreened_message_says_it_was_not_screened(monkeypatch):
    now = datetime.now(timezone.utc)
    target = Message("old", "thread", "Taylor <taylor@example.test>", "Budget", now, "old")

    async def screened(*args):
        return json.dumps({"screen": {"state": "unscreened"}, "body": "old"})

    async def fetch_body(*args):
        return target.body

    monkeypatch.setattr(mail_guard_runtime, "screened_message", screened)
    visible = await mail_guard_runtime._visible(SimpleNamespace(fetch_body=fetch_body), "account", target)
    text = mail_guard_runtime.evidence(visible)
    assert UNCHECKED in text and FLAGGED not in text and "Try again later" not in text
