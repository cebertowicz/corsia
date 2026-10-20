"""Shared mail lifecycle: Graph delta, scheduled delivery, reach and removal."""
from __future__ import annotations

import asyncio

import pytest

from connections.connected_accounts import get_account
from connections.mail import email_screen
from connections.microsoft import client, mail
from connections.connected_accounts import create_account
from connections.microsoft.provider import MICROSOFT
from events.connectors import gmail_feed, mail_poller as poller
from google_account_helpers import only_corsia_account
from infra.database import get_db


@pytest.fixture(autouse=True)
def isolated(isolated_db, monkeypatch):
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv(poller.DISABLE_ENV, raising=False)
    for name in ("_accounts", "_inflight", "_due"):
        monkeypatch.setattr(poller, name, {})
    monkeypatch.setattr(poller, "_status", poller._fresh_status())
    monkeypatch.setattr(email_screen, "sweep_expired", lambda: 0)


def connect(reach=None):
    return str(create_account(
        MICROSOFT.name, "person@example.test", reach=reach or {"mode": "all", "project_ids": [], "outside_projects": False},
        created_by=("system", "test"), account_kind="personal", provider_subject="tenant:object",
    )["id"])


def seed(account):
    assert poller._save_cursor(poller.Cursor(account, "floor", "old-delta", "then", "microsoft"))


def stored(account):
    db = get_db()
    try:
        return get_account(account, db=db)["cursor"]
    finally:
        db.close()


def message(mid="new", thread="conversation"):
    return mail.Message(mid, thread, {"from": "sender@example.test", "subject": "private subject", "date": "2026-10-19T00:00:00Z"}, "private body")


def graph_message(item: mail.Message) -> dict[str, object]:
    return {
        "id": item.message_id, "conversationId": item.thread_id,
        "from": {"emailAddress": {"address": item.headers["from"]}}, "toRecipients": [],
        "receivedDateTime": item.headers["date"], "subject": item.headers["subject"],
        "body": {"content": item.body},
    }


async def screened(*args):
    return email_screen.Screen("screened", "mail", "now", False, "none", "local")


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_missing_conversation_preserves_account_thread_key_contract(provider):
    screen = email_screen.Screen("unavailable")
    payload = poller.event_payload("account", message(thread=None), screen, "position", provider)
    assert payload["thread_id"] is None


async def test_microsoft_floor_preserves_projection_then_next_cycle_delivers_screenable_facts(monkeypatch):
    account = connect()
    calls = []
    pages = [
        {"value": [graph_message(message("existing"))], "@odata.nextLink": "floor-next?%24select=retained"},
        {"value": [], "@odata.deltaLink": "floor-delta"},
        {"value": [graph_message(message())], "@odata.deltaLink": "new-delta"},
    ]

    async def graph(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return pages.pop(0)

    monkeypatch.setattr(client, "graph_json", graph)
    await poller.poll_once(account)
    assert stored(account)["delta_link"] == "floor-delta"
    assert [call[1] for call in calls] == [
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta", "floor-next?%24select=retained",
    ]
    method, url, kwargs = calls[0]
    assert method == "GET" and url.endswith("/messages/delta")
    assert kwargs["params"]["$select"] == mail.MESSAGE_FIELDS
    assert kwargs["params"]["$filter"].startswith("receivedDateTime ge ")
    assert calls[1][2]["params"] is None
    delivered, screened_facts = [], []

    async def screen(_account, _message_id, headers, body):
        screened_facts.append((headers, body))
        return await screened()

    monkeypatch.setattr(poller, "deliver_platform_event_sync", lambda source, payload, **kw: delivered.append(payload) or [])
    monkeypatch.setattr(email_screen, "screen_message", screen)
    await poller.poll_once(account)
    assert [call[1] for call in calls] == [
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta", "floor-next?%24select=retained", "floor-delta",
    ]
    assert stored(account)["delta_link"] == "new-delta"
    assert screened_facts == [
        ({"from": "sender@example.test", "to": "", "date": "2026-10-19T00:00:00Z", "subject": "private subject"}, "private body"),
    ]
    assert delivered[0]["message_id"] == "new"
    assert "private body" not in str(delivered)


async def test_microsoft_empty_filtered_floor_follows_next_link_without_delivery(monkeypatch):
    account = connect()
    calls, delivered = [], []
    pages = [
        {"value": [], "@odata.nextLink": "floor-next?%24select=retained"},
        {"value": [], "@odata.deltaLink": "terminal-delta"},
    ]

    async def graph(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return pages.pop(0)

    monkeypatch.setattr(client, "graph_json", graph)
    monkeypatch.setattr(poller, "deliver_platform_event_sync", lambda source, payload, **kw: delivered.append(payload) or [])
    await poller.poll_once(account)
    assert stored(account)["delta_link"] == "terminal-delta"
    assert delivered == []
    assert [call[1] for call in calls] == [
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta", "floor-next?%24select=retained",
    ]
    assert calls[0][2]["params"]["$select"] == mail.MESSAGE_FIELDS
    assert calls[1][2]["params"] is None


async def test_scheduled_cycles_share_cadence_and_startup_loop(monkeypatch):
    import app_startup_runtime

    account = connect()
    seed(account)
    clock = [1000.0]
    calls = []
    monkeypatch.setattr(poller.time, "monotonic", lambda: clock[0])

    async def changes(link, *, account_id):
        calls.append(account_id)
        return [], "delta"

    monkeypatch.setattr(client, "graph_delta", changes)
    assert app_startup_runtime.mail_poll_loop is poller.mail_poll_loop
    await poller.run_cycle()
    await poller.run_cycle()
    assert calls == [account]
    clock[0] += poller.POLL_INTERVAL_S
    await poller.run_cycle()
    assert calls == [account, account]
    assert poller.poller_status(account)["state"] == "enabled"


async def test_shared_loop_sleeps_then_runs_again(monkeypatch):
    delays = iter([120, 120, None])
    slept = []

    async def cycle():
        return next(delays)

    async def sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(poller, "run_cycle", cycle)
    monkeypatch.setattr(poller.asyncio, "sleep", sleep)
    monkeypatch.setattr(poller.random, "uniform", lambda *_: 0)
    await poller.mail_poll_loop()
    assert slept == [120, 120]


@pytest.mark.parametrize("screen_state", ["screened", "unavailable"])
async def test_delivery_reach_filter_and_unscreened_payload(monkeypatch, screen_state):
    from registry import artifact_record as records
    from tests.event_routing_helpers import mk_session

    reached, excluded = mk_session("reached"), mk_session("excluded")
    from connections.app_reach import AppReach
    # A project account may serve only the reached project's thread.
    db = get_db()
    try:
        db.execute("INSERT INTO projects(id,name,path,created_at,updated_at) VALUES ('mail-project','mail-project','/tmp/mail-project','2026-10-19T00:00:00Z','2026-10-19T00:00:00Z')")
        db.execute("UPDATE threads SET project_id='mail-project' WHERE id=?", (reached,))
        db.commit()
    finally:
        db.close()
    account = connect(AppReach(mode="selected", project_ids=frozenset({"mail-project"}), outside_projects=False))
    seed(account)
    for thread, conversation in ((reached, "allowed"), (excluded, "blocked")):
        records.register_or_resolve(
            {"integration": "microsoft", "type": "email-thread", "external_id": f"microsoft:{account}:thread:{conversation}"},
            owner_intent=thread,
        )

    async def changes(*args, **kwargs):
        return [graph_message(message("a", "allowed")), graph_message(message("b", "blocked"))], "next-delta"

    real_screen = email_screen.screen_message

    async def screen_result(*args):
        if screen_state == "screened":
            return await screened()
        return await real_screen(*args)

    def unavailable():
        raise email_screen.ScreenUnavailable("no local model")

    monkeypatch.setattr(email_screen, "_resolve_local_target", unavailable)

    captured = []
    real_deliver = poller.deliver_platform_event_sync

    def deliver(source, payload, **kwargs):
        captured.append(payload)
        return real_deliver(source, payload, **kwargs)

    monkeypatch.setattr(client, "graph_delta", changes)
    monkeypatch.setattr(email_screen, "screen_message", screen_result)
    monkeypatch.setattr(poller, "deliver_platform_event_sync", deliver)
    await poller.poll_once(account)
    assert all(p["screen_status"] == screen_state for p in captured)
    assert all(("subject" in p) == (screen_state == "screened") for p in captured)
    status = poller.poller_status(account)["delivery"]
    assert status["delivered"] == 1 and status["excluded"] == 1
    db = get_db()
    try:
        wakes = db.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", (reached,)).fetchone()[0]
        assert wakes == (1 if screen_state == "screened" else 0)
        # Unscreened mail is informational: a digest note, not an actionable wake.
        assert db.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", (excluded,)).fetchone()[0] == 0
    finally:
        db.close()
    assert stored(account)["delta_link"] == "next-delta"


async def test_remove_during_poll_cancels_only_microsoft(monkeypatch):
    from connections.account_actions import remove_account

    microsoft = connect()
    google = str(only_corsia_account("google@example.test", created_by=("system", "test"), state="connected")["id"])
    seed(microsoft)
    assert poller._save_cursor(poller.Cursor(google, "floor", "gmail-old"))
    started = asyncio.Event()

    async def changes(*args, **kwargs):
        started.set()
        await asyncio.Future()

    async def gmail_changes(*args):
        return gmail_feed.HistoryPage((), "gmail-new")

    monkeypatch.setattr(client, "graph_delta", changes)
    monkeypatch.setattr(gmail_feed, "added_messages", gmail_changes)
    cycle = asyncio.create_task(poller.run_cycle())
    await asyncio.wait_for(started.wait(), 5)
    await remove_account(microsoft, actor=("person", "test"))
    await asyncio.wait_for(cycle, 5)
    assert microsoft not in poller._inflight and microsoft not in poller._due
    assert stored(google)["history_id"] == "gmail-new"
    db = get_db()
    try:
        assert db.execute("SELECT COUNT(*) FROM connected_accounts WHERE id=?", (microsoft,)).fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize("cause,state", [("provider_unavailable", "failing"), ("refresh_token_invalid", "disabled")])
async def test_microsoft_failure_does_not_delay_gmail(monkeypatch, cause, state):
    from connections.microsoft.errors import MicrosoftError

    microsoft = connect()
    google = str(only_corsia_account("google@example.test", created_by=("system", "test"), state="connected")["id"])
    seed(microsoft)
    assert poller._save_cursor(poller.Cursor(google, "floor", "gmail-old"))

    async def changes(*args, **kwargs):
        raise MicrosoftError(cause)

    async def gmail_changes(*args):
        return gmail_feed.HistoryPage((), "gmail-new")

    monkeypatch.setattr(client, "graph_delta", changes)
    monkeypatch.setattr(gmail_feed, "added_messages", gmail_changes)
    await poller.run_cycle()
    assert poller.poller_status(microsoft)["state"] == state
    assert poller.poller_status(google)["state"] == "enabled"
    assert stored(microsoft)["delta_link"] == "old-delta"
    assert stored(google)["history_id"] == "gmail-new"
    assert poller._due[microsoft] > poller._due[google]


async def test_remove_while_screening_prevents_delivery_and_cursor(monkeypatch):
    from connections.account_actions import remove_account

    account = connect()
    seed(account)

    async def changes(*args, **kwargs):
        return [graph_message(message())], "next"

    async def screen(*args):
        await remove_account(account, actor=("person", "test"))
        return await screened()

    def unexpected(*args, **kwargs):
        pytest.fail("removed mail must never be delivered")

    monkeypatch.setattr(client, "graph_delta", changes)
    monkeypatch.setattr(email_screen, "screen_message", screen)
    monkeypatch.setattr(poller, "deliver_platform_event_sync", unexpected)
    await poller.poll_once(account)
    assert poller._load_stored_cursor(account) is None


async def test_expired_delta_resets_floor_without_backfill(monkeypatch):
    account = connect()
    seed(account)
    calls = []

    async def changes(link, *, account_id, params=None):
        calls.append((link, params))
        if link == "old-delta":
            raise client.DeltaExpired()
        return [], "fresh-floor"

    monkeypatch.setattr(client, "graph_delta", changes)
    monkeypatch.setattr(email_screen, "screen_message", lambda *_: pytest.fail("no gap replay"))
    assert await poller._run_account(account) == poller.POLL_INTERVAL_S
    assert stored(account)["delta_link"] == "fresh-floor"
    assert calls[0] == ("old-delta", None)
    assert calls[1][0].endswith("/messages/delta")
    assert calls[1][1]["$select"] == mail.MESSAGE_FIELDS


async def test_failed_delivery_holds_delta(monkeypatch):
    account = connect()
    seed(account)

    async def changes(*args, **kwargs):
        return [graph_message(message())], "new-delta"

    def failed(*args, **kwargs):
        raise RuntimeError("delivery failed")

    monkeypatch.setattr(client, "graph_delta", changes)
    monkeypatch.setattr(email_screen, "screen_message", screened)
    monkeypatch.setattr(poller, "deliver_platform_event_sync", failed)
    assert await poller._run_account(account) == poller.BACKOFF_S
    assert stored(account)["delta_link"] == "old-delta"


async def test_delta_client_traverses_pages_and_skips_deletions(monkeypatch):
    pages = [
        {"value": [{"id": "deleted", "@removed": {"reason": "deleted"}}], "@odata.nextLink": "next-page"},
        {"value": [{"id": "new", "conversationId": "thread"}], "@odata.deltaLink": "terminal-delta"},
    ]
    calls = []

    async def graph(method, url, **kwargs):
        calls.append((url, kwargs.get("params")))
        return pages.pop(0)

    monkeypatch.setattr(client, "graph_json", graph)
    values, link = await client.graph_delta("previous-delta", account_id="account")
    assert [value["id"] for value in values] == ["deleted", "new"] and link == "terminal-delta"
    assert calls == [("previous-delta", None), ("next-page", None)]


async def test_delta_client_keeps_next_link_after_bounded_slice(monkeypatch):
    pages = [
        {"value": [{"id": str(index)}], "@odata.nextLink": f"next-{index}"}
        for index in range(client.DELTA_PAGES_PER_POLL)
    ]
    calls = []

    async def graph(method, url, **kwargs):
        calls.append((url, kwargs.get("params")))
        return pages.pop(0)

    monkeypatch.setattr(client, "graph_json", graph)
    values, link = await client.graph_delta("previous-delta", account_id="account", params={"$select": "id"})
    assert [value["id"] for value in values] == [str(index) for index in range(client.DELTA_PAGES_PER_POLL)]
    assert link == f"next-{client.DELTA_PAGES_PER_POLL - 1}"
    assert calls == [("previous-delta", {"$select": "id"})] + [(f"next-{index}", None) for index in range(client.DELTA_PAGES_PER_POLL - 1)]
