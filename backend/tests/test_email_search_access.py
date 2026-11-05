"""Search, access and retention coverage for the email screen."""
import json
from datetime import timedelta

import pytest

from test_email_screen_read import (
    ACCOUNT, BODY_CANARY, CLEAN, FLAGGED, SNIPPET_CANARY, SUBJECT_CANARY,
    _rows, conductor, mail,
)
from connections.google import gmail_read
from connections.mail import email_screen
from connections.mail_accounts import NONE_AVAILABLE
from google_account_helpers import only_corsia_account
from infra.corsia_agent import set_corsia_agent
from infra.database import get_db
from infra.time import utc_now
from tests.registry_helpers import make_session
from tools.email_read import EmailReadTool, EmailSearchTool


async def test_search_returns_headers_only_and_hides_the_subject_unless_clean(mail, conductor):
    for mid in ("clean", "flagged", "unscreened"):
        mail.gmail.add(mid)
    mail.model.answers = {"": None}
    await email_screen.screen_message(mail.account, "clean", {"from": "a"}, "b", invoke=lambda *a, **k: _ok(CLEAN))
    await email_screen.screen_message(mail.account, "flagged", {"from": "a"}, "b", invoke=lambda *a, **k: _ok(FLAGGED))
    out = await EmailSearchTool().execute(_thread_id=conductor, account_id=mail.account, query="from:sam", max=10)
    assert SNIPPET_CANARY not in out and BODY_CANARY not in out
    by_id = {m["message_id"]: m for m in json.loads(out)["messages"]}
    assert set(by_id) == {"clean", "flagged", "unscreened"}
    assert by_id["clean"]["subject"] == SUBJECT_CANARY
    assert by_id["clean"]["screen"]["state"] == "clean"
    assert "subject" not in by_id["flagged"] and by_id["flagged"]["screen"] == {"state": "flagged", "rule": "R1"}
    assert "subject" not in by_id["unscreened"] and by_id["unscreened"]["screen"] == {"state": "unscreened"}
    assert SUBJECT_CANARY not in json.dumps([by_id["flagged"], by_id["unscreened"]])
    assert set(by_id["clean"]) == {"message_id", "thread_id", "from", "date", "subject", "screen"}
    for request in mail.gmail.requests:
        fields = request.url.params.get("fields", "")
        assert "snippet" not in fields and request.url.params.get("format") in (None, "metadata")


async def _ok(value):
    return email_screen.validate_screen(value)


async def test_search_caps_the_result_count(mail, conductor):
    for i in range(30):
        mail.gmail.add(f"m{i}")
    out = json.loads(await EmailSearchTool().execute(_thread_id=conductor, account_id=mail.account, query="x", max=25))
    assert len(out["messages"]) == 25
    sent = next(r for r in mail.gmail.requests if str(r.url).startswith(gmail_read.API + "?"))
    assert sent.url.params["maxResults"] == "25"


async def test_a_caller_outside_reach_gets_the_exact_refusal_for_every_selector(mail):
    other = make_session(get_db(), prefix="other")
    for selector in (mail.account, ACCOUNT, ACCOUNT.upper(), "nobody@example.com", None):
        for tool, args in ((EmailSearchTool(), {"query": "x"}), (EmailReadTool(), {"message_id": "m1"})):
            assert await tool.execute(_thread_id=other, account_id=selector, **args) == f"Error: {NONE_AVAILABLE}"
    assert mail.gmail.requests == []


async def test_no_account_named_returns_the_clarification_never_a_default(mail, conductor):
    second = only_corsia_account("work@example.com", created_by=("system", "test"), state="connected")
    for tool, args in ((EmailSearchTool(), {"query": "x"}), (EmailReadTool(), {"message_id": "m1"})):
        out = await tool.execute(_thread_id=conductor, **args)
        assert out == (
            f"Error: Pass account_id to say which account to use: {ACCOUNT} (account_id "
            f"{mail.account}), work@example.com (account_id {second['id']}). If the person hasn't said "
            "which account, ask them; never pick one for them."
        )
    assert mail.gmail.requests == []
    assert (await EmailSearchTool().execute(_thread_id=conductor, account_id=ACCOUNT.upper(), query="x")).startswith("{")


async def test_not_connected_says_to_call_google_connect(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    db = get_db()
    thread_id = make_session(db, prefix="conductor")
    assert set_corsia_agent(thread_id, db=db)
    out = await EmailSearchTool().execute(_thread_id=thread_id, query="x")
    assert out == (
        "Error: No Gmail or other mail-host account is connected. For Microsoft mail, use microsoft_status, then "
        "microsoft_mail_search or microsoft_mail_read. To connect Gmail or Google Workspace, call google_connect; "
        "any other host follows mail-host-setup."
    )


def test_sweep_deletes_only_results_past_retention(isolated_db):
    db = get_db()
    old = (utc_now() - timedelta(days=email_screen.RETENTION_DAYS + 1)).isoformat()
    new = (utc_now() - timedelta(days=email_screen.RETENTION_DAYS - 1)).isoformat()
    for mid, at in (("old", old), ("new", new)):
        db.execute(
            "INSERT INTO email_screens (account, message_id, status, screened_at) VALUES (?, ?, 'failed', ?)",
            (ACCOUNT, mid, at),
        )
    db.commit()
    assert email_screen.sweep_expired() == 1
    assert [r["message_id"] for r in _rows()] == ["new"]


def test_the_evaluation_set_covers_every_rule_and_ordinary_mail():
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "email_screen" / "v1.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(rows) >= 40 and len({r["id"] for r in rows}) == len(rows)
    for row in rows:
        assert set(row) == {"id", "source", "expected_injection", "expected_rule", "headers", "body"}
        assert row["expected_injection"] == (row["expected_rule"] != "none")
    assert {r["expected_rule"] for r in rows} == set(email_screen.RULES)
    assert sum(1 for r in rows if not r["expected_injection"]) >= 10


def test_a_snake_case_kind_is_stored_as_words_and_other_characters_still_fail():
    assert email_screen.validate_screen({**CLEAN, "kind": "payment_request"})["kind"] == "payment request"
    for bad in ("payment2", "pay.ment", "Payment", "payment/request"):
        with pytest.raises(email_screen.ScreenOutputError):
            email_screen.validate_screen({**CLEAN, "kind": bad})
