"""Google sign-in: the agent that started it is woken with the outcome, once, never with a secret."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, urlparse

import pytest

from connections.app_reach import AppReach
from connections.google import oauth
from events.platform import DEAD_LETTERED, PlatformDelivery
from infra.database import get_db
from infra.internal_principal import internal_request
from sign_in_wake_helpers import EVENT, wakes
from tests.test_google_sign_in import (  # noqa: F401  (the fixtures)
    EMAIL, REFRESH, SECRET, _accounts, _connect, _http, _start, client_file, conductor, google,
)

CALLBACK = "/api/google/oauth/callback"
EVERYWHERE = AppReach(mode="all", project_ids=frozenset(), outside_projects=False)


async def _callback(state: str, **query: str) -> tuple[int, str]:
    async with _http() as http:
        response = await http.get(CALLBACK, params={"state": state, **query})
    return response.status_code, response.text


async def test_an_agent_added_account_wakes_the_starting_thread_with_the_address(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)

    status, _page = await _callback(state, code="auth-code")

    assert status == 200
    [account] = _accounts()
    [wake] = wakes()
    assert wake["thread_id"] == conductor
    assert (wake["provider"], wake["outcome"], wake["address"]) == ("google", "connected", EMAIL)
    assert wake["account_id"] == account["id"] and "cause" not in wake
    for secret in (REFRESH, SECRET, "auth-code", state):
        assert secret not in wake["content"]


async def test_an_agent_signing_an_account_in_again_wakes_that_thread(google, conductor, client_file):
    account_id = await _connect(google, conductor, client_file)
    assert len(wakes()) == 1
    db = get_db()
    try:
        db.execute("UPDATE connected_accounts SET state='needs_sign_in' WHERE id=?", (account_id,))
        db.commit()
    finally:
        db.close()
    async with _http() as http:
        with internal_request("steve", conductor):
            started = await http.post("/api/google/connect", json={"account_id": account_id})
    assert started.status_code == 200, started.text
    query = parse_qs(urlparse(started.json()["url"]).query)
    google.challenge = query["code_challenge"][0]

    status, _page = await _callback(query["state"][0], code="auth-code")

    assert status == 200
    again = wakes()[1:]
    assert [(wake["thread_id"], wake["outcome"], wake["address"], wake["account_id"]) for wake in again] == [
        (conductor, "connected", EMAIL, account_id),
    ]


async def test_a_refused_sign_in_wakes_the_thread_with_the_cause(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)

    status, _page = await _callback(state, error="access_denied")

    assert status == 400
    [wake] = wakes()
    assert (wake["thread_id"], wake["outcome"], wake["cause"]) == (conductor, "failed", "consent_cancelled")
    assert wake["message"] == oauth.DENIED and "address" not in wake
    assert _accounts() == []


async def test_missing_scopes_wake_the_thread_with_the_cause_and_what_to_ask_for(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)
    google.scope = oauth.SCOPES[0]

    status, _page = await _callback(state, code="auth-code")

    assert status == 400
    [wake] = wakes()
    assert (wake["outcome"], wake["cause"], wake["message"]) == ("failed", "consent_cancelled", oauth.MISSING_SCOPES)
    assert _accounts() == []


async def test_a_provider_outage_wakes_the_thread_with_provider_unavailable(google, conductor, client_file, monkeypatch):
    _, state = await _start(google, conductor, client_file)

    def down(*_args):
        raise RuntimeError("canary-internal-detail")

    monkeypatch.setattr(oauth, "_store", down)
    status, _page = await _callback(state, code="auth-code")

    assert status == 500
    [wake] = wakes()
    assert (wake["outcome"], wake["cause"]) == ("failed", "provider_unavailable")
    assert "canary-internal-detail" not in wake["content"]


async def test_a_replayed_callback_sends_no_second_event(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)

    first, _ = await _callback(state, code="auth-code")
    replay, _ = await _callback(state, code="auth-code")
    forged, _ = await _callback("forged", code="auth-code")

    assert (first, replay, forged) == (200, 400, 400)
    assert len(wakes()) == 1


async def test_a_person_started_sign_in_wakes_no_one(google, conductor, client_file):
    url, _ = await oauth.start_sign_in(client_file, actor=("person", "person"), reach=EVERYWHERE)
    query = parse_qs(urlparse(url).query)
    google.challenge = query["code_challenge"][0]

    status, _page = await _callback(query["state"][0], code="auth-code")

    assert status == 200 and len(_accounts()) == 1
    assert wakes() == []


async def test_a_delivery_failure_leaves_the_account_and_the_page_alone_and_is_logged(
    google, conductor, client_file, monkeypatch, caplog,
):
    def broken(*_args, **_kwargs):
        raise ValueError("event wake target is unavailable")

    monkeypatch.setattr("events.platform.deliver_platform_event_sync", broken)
    _, state = await _start(google, conductor, client_file)

    with caplog.at_level(logging.WARNING, logger="connections.sign_in_notice"):
        status, page = await _callback(state, code="auth-code")

    assert status == 200 and "Google is connected" in page
    [account] = _accounts()
    assert account["state"] == "connected"
    assert f"sign-in result for account {account['id']} not delivered to thread {conductor}: ValueError" in caplog.text
    assert wakes() == []


async def test_a_dead_lettered_result_is_logged_with_its_reason(google, conductor, client_file, monkeypatch, caplog):
    def parked(*_args, **_kwargs):
        return [PlatformDelivery("account:event", None, DEAD_LETTERED, "owner_inactive")]

    monkeypatch.setattr("events.platform.deliver_platform_event_sync", parked)
    _, state = await _start(google, conductor, client_file)

    with caplog.at_level(logging.WARNING, logger="connections.sign_in_notice"):
        status, _page = await _callback(state, code="auth-code")

    assert status == 200
    assert "not delivered to thread" in caplog.text and "owner_inactive" in caplog.text


async def test_a_failure_delivery_error_leaves_the_page_alone_and_is_logged(
    google, conductor, client_file, monkeypatch, caplog,
):
    async def broken(*_args, **_kwargs):
        raise ValueError("event wake target is unavailable")

    monkeypatch.setattr("events.platform.deliver_platform_event", broken)
    _, state = await _start(google, conductor, client_file)

    with caplog.at_level(logging.WARNING, logger="connections.sign_in_notice"):
        status, _page = await _callback(state, error="access_denied")

    assert status == 400 and wakes() == []
    assert "not delivered to thread" in caplog.text and "ValueError" in caplog.text


async def test_the_success_wake_commits_and_rolls_back_with_the_stored_account(
    google, conductor, client_file, monkeypatch,
):
    _, state = await _start(google, conductor, client_file)
    real = oauth.wake_on_connect

    def wake_then_fail(*args):
        real(*args)
        raise RuntimeError("store fails after the wake was written")

    monkeypatch.setattr(oauth, "wake_on_connect", wake_then_fail)
    status, _page = await _callback(state, code="auth-code")

    # Neither the account nor its success wake survives; only the failure is reported.
    assert status == 500 and _accounts() == []
    assert [wake["outcome"] for wake in wakes()] == ["failed"]


async def test_a_cancelled_callback_after_the_store_still_leaves_one_wake(google, conductor, client_file, monkeypatch):
    _, state = await _start(google, conductor, client_file)

    def cancelled(_account_id):
        raise asyncio.CancelledError

    monkeypatch.setattr("events.connectors.mail_poller.schedule_account", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await oauth.complete_sign_in("auth-code", state)

    [account] = _accounts()
    [wake] = wakes()
    assert (wake["thread_id"], wake["outcome"], wake["account_id"]) == (conductor, "connected", account["id"])


def test_the_event_type_is_declared_actionable_with_the_sign_in_as_its_identity():
    from events.connectors import get_manifest

    decl = get_manifest("account").decl(EVENT)
    assert (decl.event_class, decl.identity_facts) == ("actionable", ("signin_id",))
