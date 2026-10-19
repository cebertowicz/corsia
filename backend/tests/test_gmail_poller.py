"""Gmail poller cursor, delivery, status, and no-backfill guarantees."""

from __future__ import annotations

import asyncio

import pytest

from connections.connected_accounts import corsia_only_reach, create_google_account
from connections.google import email_screen
from connections.google import gmail_read
from events.connectors import gmail_feed as feed
from events.connectors import gmail_poller as poller

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
    create_google_account(
        ACCOUNT, reach=corsia_only_reach(), created_by=("system", "test"), state="connected",
    )


def _message(message_id: str = "m1") -> gmail_read.Message:
    return gmail_read.Message(message_id, "thread-1", {
        "from": "Sam <sam@example.com>",
        "to": ACCOUNT,
        "date": "Wed, 15 Oct 2026 09:12:00 -0700",
        "subject": "Next week",
    }, "This is the mail body.")


async def _answer(value):
    return value


def _seed_cursor(value: dict) -> None:
    poller._save_cursor(poller.Cursor(
        value["account"], value["floor_at"], value["history_id"], value.get("last_success_at"),
    ))


def _stored_cursor() -> dict:
    cursor = poller._load_stored_cursor(ACCOUNT)
    assert cursor is not None
    return {
        "account": cursor.account, "floor_at": cursor.floor_at,
        "history_id": cursor.history_id, "last_success_at": cursor.last_success_at,
    }


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

    stored = _stored_cursor()
    assert stored["account"] == ACCOUNT
    assert stored["history_id"] == "profile-history"
    assert stored["floor_at"]
    assert stored["last_success_at"]
    assert calls == ["profile"]


async def test_delivery_uses_flat_facts_and_advances_only_after_handling(monkeypatch):
    _seed_cursor(OLD_CURSOR)
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
    assert _stored_cursor()["history_id"] == "new-history"


async def test_failed_delivery_holds_the_cursor(monkeypatch):
    _seed_cursor(OLD_CURSOR)
    monkeypatch.setattr(feed, "added_messages", lambda _history: _answer(feed.HistoryPage(("m1",), "new-history")))
    monkeypatch.setattr(feed, "metadata", lambda message_id: _answer(_message(message_id)))
    monkeypatch.setattr(feed, "full_message", lambda message_id: _answer(_message(message_id)))
    monkeypatch.setattr(email_screen, "screen_message", lambda *_args: _answer(email_screen.Screen("unavailable")))

    async def failed_delivery(*_args):
        raise RuntimeError("delivery unavailable")

    monkeypatch.setattr(poller, "deliver_platform_event", failed_delivery)
    with pytest.raises(RuntimeError, match="delivery unavailable"):
        await poller.poll_once(ACCOUNT)
    assert _stored_cursor() == OLD_CURSOR


async def test_a_message_deleted_after_listing_is_skipped(monkeypatch):
    _seed_cursor(OLD_CURSOR)
    delivered: list[str] = []

    async def metadata(message_id: str):
        if message_id == "gone":
            raise feed.GmailMessageGone()
        return _message(message_id)

    async def deliver(_connector: str, raw: dict):
        delivered.append(raw["message_id"])
        return []

    monkeypatch.setattr(feed, "added_messages", lambda _history: _answer(feed.HistoryPage(("gone", "m1"), "new-history")))
    monkeypatch.setattr(feed, "metadata", metadata)
    monkeypatch.setattr(feed, "full_message", lambda message_id: _answer(_message(message_id)))
    monkeypatch.setattr(email_screen, "screen_message", lambda *_args: _answer(email_screen.Screen("unavailable")))
    monkeypatch.setattr(poller, "deliver_platform_event", deliver)
    monkeypatch.setattr(email_screen, "sweep_expired", lambda: 0)

    await poller.poll_once(ACCOUNT)

    assert delivered == ["m1"]
    assert _stored_cursor()["history_id"] == "new-history"


async def test_retry_after_a_partial_page_wakes_each_message_once(monkeypatch):
    from connections import binder
    from infra.conductor import WORKSPACE_SCOPE, set_scope_coordinator
    from infra.database import get_db
    from tests.event_routing_helpers import mk_session

    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    workspace = mk_session("ws")
    assert set_scope_coordinator(WORKSPACE_SCOPE, workspace)
    assert binder.ensure_designation_rule(connector="gmail", fact="label", value="INBOX") is not None
    _seed_cursor(OLD_CURSOR)
    failing = {"b"}

    def message(message_id: str) -> gmail_read.Message:
        return gmail_read.Message(message_id, f"thread-{message_id}", _message().headers, "body")

    async def full_message(message_id: str):
        if message_id in failing:
            raise feed.GmailFeedError("message_unreachable")
        return message(message_id)

    def wakes() -> int:
        db = get_db()
        try:
            return db.execute(
                "SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'",
                (workspace,),
            ).fetchone()[0]
        finally:
            db.close()

    monkeypatch.setattr(feed, "added_messages", lambda _history: _answer(feed.HistoryPage(("a", "b"), "new-history")))
    monkeypatch.setattr(feed, "metadata", lambda message_id: _answer(message(message_id)))
    monkeypatch.setattr(feed, "full_message", full_message)
    monkeypatch.setattr(
        email_screen,
        "screen_message",
        lambda *_args: _answer(email_screen.Screen("screened", "scheduling", "now", False, "none", "local")),
    )
    monkeypatch.setattr(email_screen, "sweep_expired", lambda: 0)

    with pytest.raises(feed.GmailFeedError):
        await poller.poll_once(ACCOUNT)
    assert wakes() == 1
    assert _stored_cursor() == OLD_CURSOR

    failing.clear()
    await poller.poll_once(ACCOUNT)
    assert wakes() == 2
    assert _stored_cursor()["history_id"] == "new-history"


async def test_expired_history_resets_the_floor_without_a_gap_digest(monkeypatch):
    _seed_cursor(OLD_CURSOR)
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
    stored = _stored_cursor()
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
