"""Gmail poller cursor, delivery, status, per-account isolation, and no-backfill guarantees."""

from __future__ import annotations

import asyncio

import pytest

from connections.connected_accounts import get_account
from connections.account_state import set_state
from connections.google import client as google_client
from connections.mail import email_screen
from connections.google import gmail_read
from events.connectors import gmail_feed as feed
from events.connectors import mail_poller as poller
from google_account_helpers import only_corsia_account
from infra.database import get_db

ADDRESS = "person@example.com"
OTHER_ADDRESS = "work@example.com"
OLD_FLOOR = "2026-10-01T00:00:00+00:00"


def _connect(address: str) -> str:
    return str(only_corsia_account(address, created_by=("system", "test"), state="connected")["id"])


@pytest.fixture(autouse=True)
def account(isolated_db, monkeypatch) -> str:
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv(poller.TEST_ENABLE_ENV, raising=False)
    monkeypatch.delenv(poller.DISABLE_ENV, raising=False)
    monkeypatch.setattr(poller, "_status", poller._fresh_status())
    monkeypatch.setattr(poller, "_accounts", {})
    monkeypatch.setattr(poller, "_inflight", {})
    monkeypatch.setattr(poller, "_due", {})
    return _connect(ADDRESS)


def _message(message_id: str = "m1", to: str = ADDRESS) -> gmail_read.Message:
    return gmail_read.Message(message_id, "thread-1", {
        "from": "Sam <sam@example.com>",
        "to": to,
        "date": "Wed, 15 Oct 2026 09:12:00 -0700",
        "subject": "Next week",
    }, "This is the mail body.")


async def _answer(value):
    return value


def _clean_screen(*_args):
    return _answer(email_screen.Screen("screened", "scheduling", "now", False, "none", "local"))


def _seed_cursor(account_id: str, history_id: str = "old-history") -> dict:
    assert poller._save_cursor(poller.Cursor(account_id, OLD_FLOOR, history_id, OLD_FLOOR))
    return _stored_cursor(account_id)


def _stored_cursor(account_id: str) -> dict:
    cursor = poller._load_stored_cursor(account_id)
    assert cursor is not None
    return {
        "account_id": cursor.account_id, "floor_at": cursor.floor_at,
        "history_id": cursor.history_id, "last_success_at": cursor.last_success_at,
    }


def _capture_deliveries(monkeypatch) -> list[dict]:
    delivered: list[dict] = []

    def deliver(connector: str, raw: dict, *, target_filter=None, db=None):
        assert connector == "gmail"
        assert target_filter is not None
        delivered.append(raw)
        return []  # designation=None is handled, not a Gmail polling error.

    monkeypatch.setattr(poller, "deliver_platform_event_sync", deliver)
    return delivered


async def test_first_run_sets_a_profile_floor_without_backfill(monkeypatch, account):
    calls: list[str] = []

    async def profile(account_id: str):
        assert account_id == account
        calls.append("profile")
        return "profile-history"

    async def unexpected(*_args):
        calls.append("history")
        raise AssertionError("a first run must not backfill Gmail history")

    monkeypatch.setattr(feed, "profile_history_id", profile)
    monkeypatch.setattr(feed, "added_messages", unexpected)

    await poller.poll_once(account)

    stored = _stored_cursor(account)
    assert stored["account_id"] == account
    assert stored["history_id"] == "profile-history"
    assert stored["floor_at"]
    assert stored["last_success_at"]
    assert calls == ["profile"]


async def test_delivery_uses_flat_facts_and_advances_only_after_handling(monkeypatch, account):
    _seed_cursor(account)
    delivered = _capture_deliveries(monkeypatch)
    monkeypatch.setattr(feed, "added_messages", lambda _a, _h: _answer(feed.HistoryPage(("m1",), "new-history")))
    monkeypatch.setattr(feed, "metadata", lambda _a, message_id: _answer(_message(message_id)))
    monkeypatch.setattr(feed, "full_message", lambda _a, message_id: _answer(_message(message_id)))
    monkeypatch.setattr(email_screen, "screen_message", _clean_screen)
    monkeypatch.setattr(email_screen, "sweep_expired", lambda: 0)

    await poller.poll_once(account)

    assert delivered == [{
        "account_id": account, "message_id": "m1", "thread_id": f"google:{account}:thread:thread-1",
        "history_id": "new-history", "from": "Sam <sam@example.com>", "to": ADDRESS, "label": "INBOX",
        "date": "2026-10-15T09:12:00-07:00", "subject": "Next week",
        "screen_status": "screened", "needs_person": "now", "injection": False,
        "injection_rule": "none", "kind": "scheduling", "screen_model": "local",
    }]
    assert _stored_cursor(account)["history_id"] == "new-history"


async def test_failed_delivery_holds_the_cursor(monkeypatch, account):
    old = _seed_cursor(account)
    monkeypatch.setattr(feed, "added_messages", lambda _a, _h: _answer(feed.HistoryPage(("m1",), "new-history")))
    monkeypatch.setattr(feed, "metadata", lambda _a, message_id: _answer(_message(message_id)))
    monkeypatch.setattr(feed, "full_message", lambda _a, message_id: _answer(_message(message_id)))
    monkeypatch.setattr(email_screen, "screen_message", lambda *_args: _answer(email_screen.Screen("unavailable")))

    def failed_delivery(*_args, **_kwargs):
        raise RuntimeError("delivery unavailable")

    monkeypatch.setattr(poller, "deliver_platform_event_sync", failed_delivery)
    with pytest.raises(RuntimeError, match="delivery unavailable"):
        await poller.poll_once(account)
    assert _stored_cursor(account) == old


async def test_a_message_deleted_after_listing_is_skipped(monkeypatch, account):
    _seed_cursor(account)
    delivered = _capture_deliveries(monkeypatch)

    async def metadata(_account_id: str, message_id: str):
        if message_id == "gone":
            raise feed.GmailMessageGone()
        return _message(message_id)

    monkeypatch.setattr(feed, "added_messages", lambda _a, _h: _answer(feed.HistoryPage(("gone", "m1"), "new-history")))
    monkeypatch.setattr(feed, "metadata", metadata)
    monkeypatch.setattr(feed, "full_message", lambda _a, message_id: _answer(_message(message_id)))
    monkeypatch.setattr(email_screen, "screen_message", lambda *_args: _answer(email_screen.Screen("unavailable")))
    monkeypatch.setattr(email_screen, "sweep_expired", lambda: 0)

    await poller.poll_once(account)

    assert [raw["message_id"] for raw in delivered] == ["m1"]
    assert _stored_cursor(account)["history_id"] == "new-history"


async def test_retry_after_a_partial_page_wakes_each_message_once(monkeypatch, account):
    from connections import binder
    from infra.conductor import WORKSPACE_SCOPE, set_scope_coordinator
    from registry import artifact_record as records
    from tests.event_routing_helpers import mk_session

    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    workspace = mk_session("ws")
    assert set_scope_coordinator(WORKSPACE_SCOPE, workspace)
    assert binder.ensure_designation_rule(connector="gmail", fact="label", value="INBOX") is not None
    for message_id in ("a", "b"):
        records.register_or_resolve(
            {"integration": "gmail", "type": "email-thread",
             "external_id": f"google:{account}:thread:thread-{message_id}"},
            owner_intent=workspace,
        )
    old = _seed_cursor(account)
    failing = {"b"}

    def message(message_id: str) -> gmail_read.Message:
        return gmail_read.Message(message_id, f"thread-{message_id}", _message().headers, "body")

    async def full_message(_account_id: str, message_id: str):
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

    monkeypatch.setattr(feed, "added_messages", lambda _a, _h: _answer(feed.HistoryPage(("a", "b"), "new-history")))
    monkeypatch.setattr(feed, "metadata", lambda _a, message_id: _answer(message(message_id)))
    monkeypatch.setattr(feed, "full_message", full_message)
    monkeypatch.setattr(email_screen, "screen_message", _clean_screen)
    monkeypatch.setattr(email_screen, "sweep_expired", lambda: 0)

    with pytest.raises(feed.GmailFeedError):
        await poller.poll_once(account)
    assert wakes() == 1
    assert _stored_cursor(account) == old

    failing.clear()
    await poller.poll_once(account)
    assert wakes() == 2
    assert _stored_cursor(account)["history_id"] == "new-history"


async def test_expired_history_resets_the_floor_without_a_gap_digest(monkeypatch, account):
    _seed_cursor(account)

    async def expired(*_args):
        raise feed.GmailHistoryExpired()

    def unexpected_delivery(*_args, **_kwargs):
        raise AssertionError("scope cut forbids a Gmail history gap digest")

    monkeypatch.setattr(feed, "added_messages", expired)
    monkeypatch.setattr(feed, "profile_history_id", lambda _a: _answer("new-floor"))
    monkeypatch.setattr(poller, "deliver_platform_event_sync", unexpected_delivery)

    delay = await poller.run_cycle()
    assert poller.POLL_INTERVAL_S - 1 <= delay <= poller.POLL_INTERVAL_S
    stored = _stored_cursor(account)
    assert stored["history_id"] == "new-floor"
    assert stored["floor_at"] != OLD_FLOOR
    assert poller.poller_status()["state"] == "enabled"
    assert poller.poller_status(account)["state"] == "enabled"


async def test_no_connected_account_is_off_and_test_mode_is_off(monkeypatch, account):
    set_state(account, "needs_sign_in")
    monkeypatch.setattr(feed, "profile_history_id", lambda _a: (_ for _ in ()).throw(AssertionError("not connected")))
    assert await poller.run_cycle() == poller.RECHECK_S
    assert poller.poller_status()["state"] == "disabled"
    assert poller.poller_status()["reason"] == "not_connected"

    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    assert await poller.run_cycle() is None
    assert poller.poller_status()["reason"] == "test_mode"


async def test_signed_out_account_is_forgotten_from_poller_status(monkeypatch, account):
    monkeypatch.setattr(feed, "profile_history_id", lambda _a: _answer("history"))
    await poller.run_cycle()
    assert poller.poller_status(account)["state"] == "enabled"

    set_state(account, "needs_sign_in")
    assert await poller.run_cycle() == poller.RECHECK_S
    assert poller.poller_status(account)["state"] != "enabled"


def test_manifest_keeps_payload_facts_flat_and_hides_subject_when_unavailable(account):
    payload = poller.event_payload(account, _message(), email_screen.Screen("unavailable"), "history")
    from events.envelope import normalize

    envelope = normalize("gmail", payload)
    assert envelope.facts["screen_status"] == "unavailable"
    assert envelope.facts["injection"] is False
    assert envelope.facts["history_id"] == "history"
    assert envelope.facts["account_id"] == account
    assert "subject" not in envelope.facts


async def test_two_mailboxes_keep_their_own_cursors_and_thread_ids(monkeypatch, account):
    other = _connect(OTHER_ADDRESS)
    _seed_cursor(account, "a-old")
    _seed_cursor(other, "b-old")
    delivered = _capture_deliveries(monkeypatch)
    pages = {account: ("a-old", "a-new"), other: ("b-old", "b-new")}

    async def added(account_id: str, start: str):
        assert start == pages[account_id][0]
        return feed.HistoryPage(("m1",), pages[account_id][1])

    monkeypatch.setattr(feed, "added_messages", added)
    # Both mailboxes list the same Gmail message and thread ids: they must stay distinct.
    monkeypatch.setattr(feed, "metadata", lambda _a, message_id: _answer(_message(message_id)))
    monkeypatch.setattr(feed, "full_message", lambda _a, message_id: _answer(_message(message_id)))
    monkeypatch.setattr(email_screen, "screen_message", _clean_screen)
    monkeypatch.setattr(email_screen, "sweep_expired", lambda: 0)

    await poller.run_cycle()

    assert _stored_cursor(account)["history_id"] == "a-new"
    assert _stored_cursor(other)["history_id"] == "b-new"
    by_account = {raw["account_id"]: raw["thread_id"] for raw in delivered}
    assert by_account == {
        account: f"google:{account}:thread:thread-1", other: f"google:{other}:thread:thread-1",
    }


async def test_one_account_signed_out_by_google_leaves_the_other_polling(monkeypatch, account):
    other = _connect(OTHER_ADDRESS)
    _seed_cursor(account, "a-old")
    _seed_cursor(other, "b-old")
    _capture_deliveries(monkeypatch)

    async def added(account_id: str, _start: str):
        if account_id == account:
            set_state(account_id, google_client.STATUS_NEEDS_SIGN_IN)
            raise google_client.GoogleSignInRejected()
        return feed.HistoryPage((), "b-new")

    monkeypatch.setattr(feed, "added_messages", added)
    monkeypatch.setattr(email_screen, "sweep_expired", lambda: 0)

    await poller.run_cycle()

    assert poller.poller_status(account)["reason"] == google_client.STATUS_NEEDS_SIGN_IN
    assert poller.poller_status(other)["state"] == "enabled"
    assert _stored_cursor(account)["history_id"] == "a-old"
    assert _stored_cursor(other)["history_id"] == "b-new"


async def test_a_remove_during_a_poll_stops_every_later_write(monkeypatch, account):
    from connections.account_actions import _remove_locally

    _seed_cursor(account)
    delivered = _capture_deliveries(monkeypatch)
    monkeypatch.setattr(feed, "added_messages", lambda _a, _h: _answer(feed.HistoryPage(("m1", "m2"), "new-history")))
    monkeypatch.setattr(feed, "metadata", lambda _a, message_id: _answer(_message(message_id)))
    monkeypatch.setattr(feed, "full_message", lambda _a, message_id: _answer(_message(message_id)))
    real_screen = email_screen.screen_message

    async def screen_then_remove(account_id: str, message_id: str, headers, body):
        # The person removes the account while the model screens this message.
        _remove_locally(account_id, ("person", "steve"))
        return await real_screen(account_id, message_id, headers, body, invoke=None)

    monkeypatch.setattr(email_screen, "_resolve_local_target", lambda: (_ for _ in ()).throw(
        email_screen.ScreenUnavailable("no local model")))
    monkeypatch.setattr(email_screen, "screen_message", screen_then_remove)

    await poller.poll_once(account)

    assert delivered == []
    db = get_db()
    try:
        assert db.execute("SELECT COUNT(*) FROM connected_accounts WHERE id=?", (account,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM email_screens WHERE account=?", (account,)).fetchone()[0] == 0
    finally:
        db.close()


async def test_cancel_account_stops_its_in_flight_poll_only(monkeypatch, account):
    other = _connect(OTHER_ADDRESS)
    _seed_cursor(account, "a-old")
    _seed_cursor(other, "b-old")
    _capture_deliveries(monkeypatch)
    started = asyncio.Event()

    async def added(account_id: str, _start: str):
        if account_id == account:
            started.set()
            await asyncio.sleep(3600)
        return feed.HistoryPage((), "b-new")

    monkeypatch.setattr(feed, "added_messages", added)
    monkeypatch.setattr(email_screen, "sweep_expired", lambda: 0)

    cycle = asyncio.create_task(poller.run_cycle())
    await asyncio.wait_for(started.wait(), 5)
    poller.cancel_account(account)
    await asyncio.wait_for(cycle, 5)

    assert account not in poller._accounts and account not in poller._due
    assert _stored_cursor(account)["history_id"] == "a-old"
    assert _stored_cursor(other)["history_id"] == "b-new"
    db = get_db()
    try:
        assert get_account(other, db=db)["state"] == "connected"
    finally:
        db.close()


def test_inbox_delivery_only_reaches_the_account_scope_and_counts_the_drop(account):
    """A resolved owner outside an account's reach receives no wake or fallback delivery."""
    from infra.conductor import WORKSPACE_SCOPE, set_scope_coordinator
    from registry import artifact_record as records
    from tests.event_routing_helpers import mk_session

    outside = mk_session("outside")
    blocked_thread = f"google:{account}:thread:thread-1"
    records.register_or_resolve(
        {"integration": "gmail", "type": "email-thread", "external_id": blocked_thread},
        owner_intent=outside,
    )
    payload = poller.event_payload(
        account, _message("blocked"), email_screen.Screen("screened", "mail", "now", False, "none", "local"),
        "history-blocked",
    )

    assert poller._deliver(account, payload)
    db = get_db()
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", (outside,),
        ).fetchone()[0] == 0
    finally:
        db.close()
    assert poller.poller_status(account)["delivery"] == {
        "delivered": 0, "excluded": 1, "outcomes": {"filtered": 1},
    }

    workspace = mk_session("workspace")
    assert set_scope_coordinator(WORKSPACE_SCOPE, workspace)
    allowed_thread = f"google:{account}:thread:thread-2"
    records.register_or_resolve(
        {"integration": "gmail", "type": "email-thread", "external_id": allowed_thread},
        owner_intent=workspace,
    )
    allowed_message = gmail_read.Message("allowed", "thread-2", _message().headers, "body")
    allowed_payload = poller.event_payload(
        account, allowed_message, email_screen.Screen("screened", "mail", "now", False, "none", "local"),
        "history-allowed",
    )

    assert poller._deliver(account, allowed_payload)
    db = get_db()
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", (workspace,),
        ).fetchone()[0] == 1
    finally:
        db.close()
    assert poller.poller_status(account)["delivery"] == {
        "delivered": 1, "excluded": 1, "outcomes": {"filtered": 1, "woke": 1},
    }
