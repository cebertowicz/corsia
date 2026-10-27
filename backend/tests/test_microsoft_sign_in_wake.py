"""Microsoft sign-in: the agent that started it is woken with the outcome, once, never with a secret."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, urlparse

import pytest

from connections.account_state import CONSENT_CANCELLED, PROVIDER_UNAVAILABLE
from connections.microsoft import account_store, oauth
from connections.sign_in_notice import (
    ACCOUNT_REMOVED_MESSAGE, CLOSED_BY_CAP_MESSAGE, SHUT_DOWN_MESSAGE,
)
from connections.sign_in_notice import EXPIRED_MESSAGE, UNAVAILABLE_MESSAGE, notify_starting_agent, wake_on_connect
from infra.database import get_db, write_transaction
from infra.send_types import SendRejected
from sign_in_wake_helpers import (
    adopt, break_notice_write, parked_reports, record_drives, refuse_sign_in_wakes, reports_for, wakes,
)
from tests.registry_helpers import make_session
from tools.mcp_oauth_listener import REQUEST_CAP
from tests.test_microsoft_sign_in import (  # noqa: F401  (the fixtures)
    CLIENT_ID, EVERYWHERE, PERSON, TENANT_A, _accounts, _callback, _connect, _finish, _query, agent_adds, microsoft,
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


def _state(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


@pytest.fixture(autouse=True)
def drives(monkeypatch):
    return record_drives(monkeypatch)


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
    assert wake["chip"] == "Microsoft sign-in finished"
    assert "authorization-code" not in wake["content"] and oauth._pending == {}


async def test_an_agent_signing_an_account_in_again_wakes_that_thread(microsoft, isolated_db, agent_adds):
    account_id = await _connect(microsoft)
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id, account_id=account_id)
    assert oauth._pending[_state(url)].adding_thread_id == thread_id

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
    assert wake["chip"] == "Couldn’t finish Microsoft sign-in"
    assert "Summary: Couldn’t finish Microsoft sign-in: " in wake["content"]
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


async def test_a_fault_inside_corsia_wakes_the_thread_with_the_shared_provider_unavailable_facts(
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
    assert (wake["outcome"], wake["cause"], wake["message"]) == ("failed", PROVIDER_UNAVAILABLE, UNAVAILABLE_MESSAGE)
    assert "canary-internal-detail" not in wake["content"]


async def test_repeat_callback_during_exchange_does_not_end_the_flow(microsoft, isolated_db, agent_adds):
    thread_id = _agent()
    url, account_id = await _agent_flow(thread_id)
    entered, release = asyncio.Event(), asyncio.Event()
    handler = microsoft.handler

    async def delayed_exchange(request):
        if request.method == "POST":
            entered.set()
            await release.wait()
        return handler(request)

    microsoft.handler = delayed_exchange
    first = asyncio.create_task(_finish(url))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await _finish(url))[0] == 400
    finally:
        release.set()
    assert (await first)[0] == 200
    assert [(wake["outcome"], wake["account_id"]) for wake in wakes()] == [("connected", account_id)]
    assert [(account["id"], account["state"]) for account in _accounts()] == [(account_id, "connected")]
    assert "latest_failed_add" not in oauth.accounts_status(thread_id)


async def test_one_sign_in_is_one_input_whatever_arrives_after_it(microsoft, isolated_db, agent_adds):
    thread_id = _agent()
    url, account_id = await _agent_flow(thread_id)
    await _finish(url)
    state = _state(url)

    # A replayed callback that raced the first one would report success again, or a late failure.
    with write_transaction(None, scope="test_replayed_success") as conn:
        wake_on_connect(conn, thread_id, state, "microsoft", account_id, ADDRESS)
    await notify_starting_agent(thread_id, state, "microsoft", account_id, cause=CONSENT_CANCELLED)
    await notify_starting_agent(thread_id, state, "microsoft", account_id, cause=CONSENT_CANCELLED)

    assert len(wakes()) == 1


async def test_a_person_started_sign_in_wakes_no_one(microsoft, isolated_db):
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=PERSON, reach=EVERYWHERE)
    assert oauth._pending[_state(url)].adding_thread_id is None

    status, _page = await _finish(url)

    assert status == 200 and len(_accounts()) == 1
    assert wakes() == []


async def test_a_person_started_failure_wakes_no_one(microsoft, isolated_db):
    url, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=PERSON, reach=EVERYWHERE)

    status, _page = await _finish(url, error="access_denied", error_subcode="cancel")

    assert status == 400 and wakes() == []


async def test_a_connected_sign_in_drives_the_starting_thread_once_and_only_after_the_commit(
    microsoft, isolated_db, agent_adds, drives,
):
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id)

    assert (await _finish(url))[0] == 200

    # The account row and the queued wake were both committed when the drive began.
    assert drives == [{"thread": thread_id, "accounts": 1, "wakes": 1}]


async def test_a_failed_sign_in_drives_the_starting_thread_once(microsoft, isolated_db, agent_adds, drives):
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id)

    assert (await _finish(url, error="access_denied", error_subcode="cancel"))[0] == 400

    assert drives == [{"thread": thread_id, "accounts": 0, "wakes": 1}]


async def test_an_expired_sign_in_drives_the_starting_thread_once(
    microsoft, isolated_db, agent_adds, drives, monkeypatch,
):
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id)
    state = _state(url)
    pending = oauth._pending[state]
    oauth._pending_expiries[state].cancel()
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0)

    await oauth._expire(pending)

    assert drives == [{"thread": thread_id, "accounts": 0, "wakes": 1}]


async def test_a_sign_in_with_no_starting_thread_drives_nothing(microsoft, isolated_db, drives):
    ok, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=PERSON, reach=EVERYWHERE)
    refused, _ = await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=PERSON, reach=EVERYWHERE)

    assert (await _finish(ok))[0] == 200
    assert (await _finish(refused, error="access_denied", error_subcode="cancel"))[0] == 400

    assert drives == []


async def test_a_wake_the_thread_refuses_is_reported_to_its_owner_and_that_owner_is_driven(
    microsoft, isolated_db, agent_adds, monkeypatch, drives,
):
    owner, child = _agent(), _agent()
    adopt(child, owner)
    refuse_sign_in_wakes(monkeypatch)
    url, _ = await _agent_flow(child)

    assert (await _finish(url))[0] == 200

    assert [account["state"] for account in _accounts()] == ["connected"]
    [report] = reports_for(owner)
    assert report.startswith("wake-failed:sign-in:") and wakes() == []
    # The refused thread has nothing queued to drive; its owner holds the report.
    assert [call["thread"] for call in drives] == [owner]


async def test_a_wake_the_thread_refuses_and_no_owner_takes_is_parked(
    microsoft, isolated_db, agent_adds, monkeypatch,
):
    orphan = _agent()
    refuse_sign_in_wakes(monkeypatch)
    url, _ = await _agent_flow(orphan)

    assert (await _finish(url))[0] == 200

    [parked] = parked_reports()
    assert parked["thread_id"] == orphan and parked["event"] == "account.sign_in_finished"
    assert [account["state"] for account in _accounts()] == ["connected"]


async def test_an_unfinished_sign_in_wakes_the_thread_when_it_expires(microsoft, isolated_db, agent_adds, monkeypatch):
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id)
    state = _state(url)
    pending = oauth._pending[state]
    oauth._pending_expiries[state].cancel()
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0)

    await oauth._expire(pending)

    [wake] = wakes()
    assert (wake["thread_id"], wake["outcome"], wake["cause"], wake["message"]) == (
        thread_id, "failed", CONSENT_CANCELLED, EXPIRED_MESSAGE,
    )
    assert oauth._pending == {} and pending.listener.bound_addresses() == [] and _accounts() == []


async def test_a_sign_in_found_expired_when_the_next_one_starts_wakes_its_thread(
    microsoft, isolated_db, agent_adds, monkeypatch,
):
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id)
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0)

    await _agent_flow(_agent())

    [wake] = wakes()
    assert (wake["thread_id"], wake["cause"], wake["message"]) == (thread_id, CONSENT_CANCELLED, EXPIRED_MESSAGE)
    assert _state(url) not in oauth._pending and _state(url) not in oauth._pending_expiries


async def test_a_sign_in_the_callback_took_is_not_also_reported_expired(
    microsoft, isolated_db, agent_adds, monkeypatch,
):
    url, _ = await _agent_flow(_agent())
    pending = oauth._pending[_state(url)]
    assert (await _finish(url))[0] == 200
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0)

    await oauth._expire(pending)

    assert [wake["outcome"] for wake in wakes()] == ["connected"]


async def test_an_unavailable_target_keeps_the_account_and_is_logged(microsoft, isolated_db, agent_adds, caplog):
    url, account_id = await _agent_flow("no-such-thread")

    with caplog.at_level(logging.WARNING, logger="connections.sign_in_notice"):
        status, page = await _finish(url)

    assert status == 200 and b"Microsoft is connected" in page
    [account] = _accounts()
    assert account["state"] == "connected"
    assert f"sign-in result for account {account_id} not delivered to thread no-such-thread: ValueError" in caplog.text
    assert wakes() == []


async def test_a_refused_send_keeps_the_account_and_is_logged(microsoft, isolated_db, agent_adds, monkeypatch, caplog):
    def refused(*_args, **_kwargs):
        raise SendRejected(409, "held")

    monkeypatch.setattr("events.delivery.enqueue_event_wake", refused)
    url, _ = await _agent_flow(_agent())

    with caplog.at_level(logging.WARNING, logger="connections.sign_in_notice"):
        status, _page = await _finish(url)

    assert status == 200 and len(_accounts()) == 1
    assert "not delivered to thread" in caplog.text and "SendRejected" in caplog.text


async def test_a_defect_in_the_wake_keeps_the_account_and_is_logged_with_its_traceback(
    microsoft, isolated_db, agent_adds, monkeypatch, caplog,
):
    def defect(*_args, **_kwargs):
        raise RuntimeError("canary-defect")

    monkeypatch.setattr("events.delivery.enqueue_event_wake", defect)
    url, _ = await _agent_flow(_agent())

    with caplog.at_level(logging.ERROR, logger="connections.sign_in_notice"):
        status, page = await _finish(url)

    assert status == 200 and b"Microsoft is connected" in page
    assert [account["state"] for account in _accounts()] == ["connected"]
    [record] = [r for r in caplog.records if r.name == "connections.sign_in_notice"]
    assert record.exc_info is not None and "canary-defect" in str(record.exc_info[1])


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
        await oauth._complete_sign_in("authorization-code", _state(url))

    [wake] = wakes()
    assert (wake["thread_id"], wake["outcome"], wake["account_id"]) == (thread_id, "connected", account_id)
    assert [account["id"] for account in _accounts()] == [account_id]


def _ended(thread_id: str, message: str) -> bool:
    return [(wake["thread_id"], wake["outcome"], wake["cause"], wake["message"]) for wake in wakes()] == [
        (thread_id, "failed", CONSENT_CANCELLED, message),
    ]


async def test_a_flow_the_request_cap_closes_wakes_the_thread_once(microsoft, isolated_db, agent_adds, drives):
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id)
    state = _state(url)

    for _ in range(REQUEST_CAP):
        await _callback(_query(url)["redirect_uri"], path="/not-the-callback")
    # The listener answers first, then closes the flow, queues the wake and drives the thread.
    for _ in range(250):
        if drives:
            break
        await asyncio.sleep(0.02)

    assert state not in oauth._pending and _ended(thread_id, CLOSED_BY_CAP_MESSAGE)
    assert drives == [{"thread": thread_id, "accounts": 0, "wakes": 1}]


async def test_removing_the_account_wakes_the_thread_that_was_signing_it_in_again(
    microsoft, isolated_db, agent_adds, drives,
):
    account_id = await _connect(microsoft)
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id, account_id=account_id)

    await oauth.clear_pending(account_id)

    assert _state(url) not in oauth._pending and _ended(thread_id, ACCOUNT_REMOVED_MESSAGE)
    assert drives == [{"thread": thread_id, "accounts": 1, "wakes": 1}]


async def test_shutdown_queues_one_wake_for_each_waiting_agent_and_starts_no_turn(
    microsoft, isolated_db, agent_adds, drives,
):
    thread_id = _agent()
    await _agent_flow(thread_id)
    await oauth.start_sign_in(CLIENT_ID, "work", TENANT_A, actor=PERSON, reach=EVERYWHERE)

    await oauth.stop_all_signins()
    await oauth.stop_all_signins()

    assert oauth._pending == {} and _ended(thread_id, SHUT_DOWN_MESSAGE)
    assert drives == []


async def test_a_flow_the_callback_already_took_is_not_woken_again_when_closed(
    microsoft, isolated_db, agent_adds,
):
    account_id = await _connect(microsoft)
    thread_id = _agent()
    url, _ = await _agent_flow(thread_id, account_id=account_id)
    assert (await _finish(url))[0] == 200

    await oauth.clear_pending(account_id)
    await oauth.stop_all_signins()

    assert [wake["outcome"] for wake in wakes()] == ["connected"]


async def test_a_notice_the_database_refuses_leaves_the_failure_page_alone(
    microsoft, isolated_db, agent_adds, monkeypatch, caplog,
):
    break_notice_write(monkeypatch)
    url, _ = await _agent_flow(_agent())

    with caplog.at_level(logging.ERROR, logger="connections.sign_in_notice"):
        status, page = await _finish(url, error="access_denied", error_subcode="cancel")

    assert status == 400 and b"cancelled" in page.lower() and wakes() == []
    assert "the notice write for thread" in caplog.text


async def test_a_notice_the_database_refuses_never_escapes_the_expiry_task(
    microsoft, isolated_db, agent_adds, monkeypatch,
):
    break_notice_write(monkeypatch)
    url, _ = await _agent_flow(_agent())
    state = _state(url)
    pending = oauth._pending[state]
    oauth._pending_expiries[state].cancel()
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0)

    await oauth._expire(pending)

    assert state not in oauth._pending and wakes() == []


async def test_a_notice_the_database_refuses_keeps_the_stored_account(
    microsoft, isolated_db, agent_adds, monkeypatch,
):
    break_notice_write(monkeypatch)
    url, _ = await _agent_flow(_agent())

    status, page = await _finish(url)

    assert status == 200 and b"Microsoft is connected" in page
    assert [account["state"] for account in _accounts()] == ["connected"] and wakes() == []
