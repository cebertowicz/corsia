"""Microsoft sign-in: the agent that started it is woken with the outcome, once, never with a secret."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, urlparse

import pytest

from connections.microsoft import account_store, oauth
from connections.sign_in_notice import notify_starting_agent, wake_on_connect
from infra.database import get_db, write_transaction
from sign_in_wake_helpers import wakes
from tests.registry_helpers import make_session
from tests.test_microsoft_sign_in import (  # noqa: F401  (the fixtures)
    CLIENT_ID, EVERYWHERE, PERSON, TENANT_A, _accounts, _connect, _finish, agent_adds, microsoft,
)

ADDRESS = "same@example.test"


def _agent() -> str:
    db = get_db()
    try:
        return make_session(db, prefix="agent")
    finally:
        db.close()


async def _agent_flow(thread_id: str, **kwargs):
    return await oauth.start_sign_in(
        CLIENT_ID, "work", TENANT_A, actor=("agent", thread_id), adding_thread_id=thread_id, **kwargs,
    )


async def test_an_agent_added_account_wakes_the_starting_thread_with_the_address(microsoft, isolated_db, agent_adds):
    thread_id = _agent()
    url, account_id = await _agent_flow(thread_id)

    status, _page = await _finish(url)

    assert status == 200
    [wake] = wakes()
    assert wake["thread_id"] == thread_id
    assert (wake["provider"], wake["outcome"], wake["address"], wake["account_id"]) == (
        "microsoft", "connected", ADDRESS, account_id,
    )
    assert "cause" not in wake
    assert "authorization-code" not in wake["content"] and oauth._pending == {}


async def test_an_agent_signing_an_account_in_again_wakes_that_thread(microsoft, isolated_db, agent_adds):
    account_id = await _connect(microsoft)
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id, account_id=account_id)

    status, _page = await _finish(url)

    assert status == 200
    assert [(wake["thread_id"], wake["outcome"], wake["address"], wake["account_id"]) for wake in wakes()] == [
        (thread_id, "connected", ADDRESS, account_id),
    ]


async def test_a_cancelled_sign_in_wakes_the_thread_with_the_cause(microsoft, isolated_db, agent_adds):
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id)

    status, _page = await _finish(url, error="access_denied", error_subcode="cancel")

    assert status == 400
    [wake] = wakes()
    assert (wake["thread_id"], wake["outcome"], wake["cause"]) == (thread_id, "failed", "consent_cancelled")
    assert wake["message"] == "Microsoft sign-in was cancelled. Call microsoft_connect for a new link."
    assert "address" not in wake and _accounts() == []


async def test_a_refused_code_exchange_wakes_the_thread_without_the_providers_text(microsoft, isolated_db, agent_adds):
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id)
    microsoft.token_error = True

    status, _page = await _finish(url)

    assert status == 400
    [wake] = wakes()
    assert (wake["thread_id"], wake["outcome"]) == (thread_id, "failed") and wake["cause"]
    assert "token-error-canary" not in wake["content"]


async def test_a_fault_inside_corsia_wakes_the_thread_with_provider_unavailable(
    microsoft, isolated_db, agent_adds, monkeypatch,
):
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id)

    def broken(*_args):
        raise RuntimeError("canary-internal-detail")

    monkeypatch.setattr(oauth.account_store, "store", broken)

    status, _page = await _finish(url)

    assert status == 500
    [wake] = wakes()
    assert (wake["outcome"], wake["cause"]) == ("failed", "provider_unavailable")
    assert "canary-internal-detail" not in wake["content"]


async def test_one_sign_in_is_one_event_whatever_arrives_after_it(microsoft, isolated_db, agent_adds):
    thread_id = _agent()
    url, account_id = await _agent_flow(thread_id)
    await _finish(url)
    state = parse_qs(urlparse(url).query)["state"][0]

    # A replayed callback that raced the first one would report success again, or a late failure.
    with write_transaction(None, scope="test_replayed_success") as conn:
        wake_on_connect(conn, thread_id, state, "microsoft", account_id, ADDRESS)
    await notify_starting_agent(thread_id, state, "microsoft", account_id, cause="consent_cancelled")

    assert len(wakes()) == 1


async def test_a_person_started_sign_in_wakes_no_one(microsoft, isolated_db):
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=PERSON, reach=EVERYWHERE)

    status, _page = await _finish(url)

    assert status == 200 and len(_accounts()) == 1
    assert wakes() == []


async def test_a_person_started_failure_wakes_no_one(microsoft, isolated_db):
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=PERSON, reach=EVERYWHERE)

    status, _page = await _finish(url, error="access_denied", error_subcode="cancel")

    assert status == 400 and wakes() == []


async def test_a_delivery_failure_leaves_the_account_and_the_page_alone_and_is_logged(
    microsoft, isolated_db, agent_adds, monkeypatch, caplog,
):
    def broken(*_args, **_kwargs):
        raise ValueError("event wake target is unavailable")

    monkeypatch.setattr("events.platform.deliver_platform_event_sync", broken)
    thread_id = _agent()
    url, account_id = await _agent_flow(thread_id)

    with caplog.at_level(logging.WARNING, logger="connections.sign_in_notice"):
        status, page = await _finish(url)

    assert status == 200 and b"Microsoft is connected" in page
    [account] = _accounts()
    assert account["state"] == "connected"
    assert f"sign-in result for account {account_id} not delivered to thread {thread_id}: ValueError" in caplog.text
    assert wakes() == []


async def test_the_success_wake_commits_and_rolls_back_with_the_stored_account(
    microsoft, isolated_db, agent_adds, monkeypatch,
):
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id)
    real = account_store.wake_on_connect

    def wake_then_fail(*args):
        real(*args)
        raise RuntimeError("store fails after the wake was written")

    monkeypatch.setattr(account_store, "wake_on_connect", wake_then_fail)
    status, _page = await _finish(url)

    # Neither the account nor its success wake survives; only the failure is reported.
    assert status == 500 and _accounts() == []
    assert [wake["outcome"] for wake in wakes()] == ["failed"]


async def test_a_cancelled_callback_after_the_store_still_leaves_one_wake(
    microsoft, isolated_db, agent_adds, monkeypatch,
):
    thread_id = _agent()
    url, account_id = await _agent_flow(thread_id)

    def cancelled(_account_id):
        raise asyncio.CancelledError

    monkeypatch.setattr("events.connectors.mail_poller.schedule_account", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await oauth._complete_sign_in("authorization-code", parse_qs(urlparse(url).query)["state"][0])

    [wake] = wakes()
    assert (wake["thread_id"], wake["outcome"], wake["account_id"]) == (thread_id, "connected", account_id)
    assert [account["id"] for account in _accounts()] == [account_id]
