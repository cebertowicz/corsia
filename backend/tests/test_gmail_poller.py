"""Gmail poller cursor, delivery, status, and no-backfill guarantees."""

from __future__ import annotations

import asyncio

import pytest

from connections.google import email_screen
from connections.google import gmail_read
from events.connectors import gmail_feed as feed
from events.connectors import gmail_poller as poller
from infra.settings import get_setting, set_setting

ACCOUNT = "person@example.com"
OLD_CURSOR = {
    "account": ACCOUNT,
    "floor_at": "2026-10-01T00:00:00+00:00",
    "history_id": "old-history",
    "last_success_at": "2026-10-01T00:00:00+00:00",
}


@pytest.fixture(autouse=True)
def _poller_state(isolated_db, monkeypatch):
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv(poller.TEST_ENABLE_ENV, raising=False)
    monkeypatch.delenv(poller.DISABLE_ENV, raising=False)
    monkeypatch.setattr(poller, "_status", {
        "state": "starting", "reason": None, "last_success_at": None, "last_error": None,
    })


def _message(message_id: str = "m1") -> gmail_read.Message:
    return gmail_read.Message(message_id, "thread-1", {
        "from": "Sam <sam@example.com>",
        "to": ACCOUNT,
        "date": "Wed, 15 Oct 2026 09:12:00 -0700",
        "subject": "Next week",
    }, "This is the mail body.")


async def _answer(value):
    return value


async def test_first_run_sets_a_profile_floor_without_backfill(monkeypatch):
    calls: list[str] = []

    async def profile():
        calls.append("profile")
        return "profile-history"

    async def unexpected(_history: str):
        calls.append("history")
        raise AssertionError("a first run must not backfill Gmail history")

    monkeypatch.setattr(feed, "profile_history_id", profile)
    monkeypatch.setattr(feed, "added_messages", unexpected)

    await poller.poll_once(ACCOUNT)

    stored = get_setting(poller.CURSOR_SETTING)
    assert stored["account"] == ACCOUNT
    assert stored["history_id"] == "profile-history"
    assert stored["floor_at"]
    assert stored["last_success_at"]
    assert calls == ["profile"]


async def test_delivery_uses_flat_facts_and_advances_only_after_handling(monkeypatch):
    set_setting(poller.CURSOR_SETTING, OLD_CURSOR)
    delivered: list[dict] = []

    async def deliver(connector: str, raw: dict):
        assert connector == "gmail"
        delivered.append(raw)
        return []  # designation=None is handled, not a Gmail polling error.

    monkeypatch.setattr(feed, "added_messages", lambda _history: _answer(feed.HistoryPage(("m1",), "new-history")))
    monkeypatch.setattr(feed, "metadata", lambda message_id: _answer(_message(message_id)))
    monkeypatch.setattr(feed, "full_message", lambda message_id: _answer(_message(message_id)))
    monkeypatch.setattr(
        email_screen,
        "screen_message",
        lambda *_args: _answer(email_screen.Screen("screened", "scheduling", "now", False, "none", "local")),
    )
    monkeypatch.setattr(poller, "deliver_platform_event", deliver)
    monkeypatch.setattr(email_screen, "sweep_expired", lambda: 0)

    await poller.poll_once(ACCOUNT)

    assert delivered == [{
        "message_id": "m1", "thread_id": "thread-1", "history_id": "new-history",
        "from": "Sam <sam@example.com>", "to": ACCOUNT, "label": "INBOX",
        "date": "2026-10-15T09:12:00-07:00", "subject": "Next week",
        "screen_status": "screened", "needs_person": "now", "injection": False,
        "injection_rule": "none", "kind": "scheduling", "screen_model": "local",
    }]
    assert get_setting(poller.CURSOR_SETTING)["history_id"] == "new-history"


async def test_failed_delivery_holds_the_cursor(monkeypatch):
    set_setting(poller.CURSOR_SETTING, OLD_CURSOR)
    monkeypatch.setattr(feed, "added_messages", lambda _history: _answer(feed.HistoryPage(("m1",), "new-history")))
    monkeypatch.setattr(feed, "metadata", lambda message_id: _answer(_message(message_id)))
    monkeypatch.setattr(feed, "full_message", lambda message_id: _answer(_message(message_id)))
    monkeypatch.setattr(email_screen, "screen_message", lambda *_args: _answer(email_screen.Screen("unavailable")))

    async def failed_delivery(*_args):
        raise RuntimeError("delivery unavailable")

    monkeypatch.setattr(poller, "deliver_platform_event", failed_delivery)
    with pytest.raises(RuntimeError, match="delivery unavailable"):
        await poller.poll_once(ACCOUNT)
    assert get_setting(poller.CURSOR_SETTING) == OLD_CURSOR


async def test_expired_history_resets_the_floor_without_a_gap_digest(monkeypatch):
    set_setting(poller.CURSOR_SETTING, OLD_CURSOR)
    monkeypatch.setattr(poller, "disabled_reason", lambda: _answer(None))
    monkeypatch.setattr(poller.oauth, "account_status", lambda: {"email": ACCOUNT, "status": "connected"})

    async def expired(_history: str):
        raise feed.GmailHistoryExpired()

    async def unexpected_delivery(*_args):
        raise AssertionError("scope cut forbids a Gmail history gap digest")

    monkeypatch.setattr(feed, "added_messages", expired)
    monkeypatch.setattr(feed, "profile_history_id", lambda: _answer("new-floor"))
    monkeypatch.setattr(poller, "deliver_platform_event", unexpected_delivery)

    assert await poller.run_cycle() == poller.POLL_INTERVAL_S
    stored = get_setting(poller.CURSOR_SETTING)
    assert stored["history_id"] == "new-floor"
    assert stored["floor_at"] != OLD_CURSOR["floor_at"]
    assert poller.poller_status()["state"] == "enabled"


async def test_needs_sign_in_is_off_and_test_mode_is_off(monkeypatch):
    monkeypatch.setattr(poller.oauth, "account_status", lambda: {"email": ACCOUNT, "status": "needs_sign_in"})
    assert await poller.run_cycle() == poller.RECHECK_S
    assert poller.poller_status()["state"] == "disabled"
    assert poller.poller_status()["reason"] == "needs_sign_in"

    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.setattr(feed, "profile_history_id", lambda: (_ for _ in ()).throw(AssertionError("test mode must not poll")))
    assert await poller.run_cycle() is None
    assert poller.poller_status()["reason"] == "test_mode"


def test_manifest_keeps_payload_facts_flat_and_hides_subject_when_unavailable():
    payload = poller.event_payload(_message(), email_screen.Screen("unavailable"), "history")
    from events.envelope import normalize

    envelope = normalize("gmail", payload)
    assert envelope.facts["screen_status"] == "unavailable"
    assert envelope.facts["injection"] is False
    assert envelope.facts["history_id"] == "history"
    assert "subject" not in envelope.facts
