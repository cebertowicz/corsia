"""Google sign-in: the agent that started it is woken with the outcome, once, never with a secret."""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import parse_qs, urlparse

import pytest

from connections.account_state import CONSENT_CANCELLED, PROVIDER_UNAVAILABLE
from connections.app_reach import AppReach
from connections.google import oauth
from connections.sign_in_notice import EXPIRED_MESSAGE, SHUT_DOWN_MESSAGE, UNAVAILABLE_MESSAGE, notify_starting_agent
from infra.database import get_db
from infra.internal_principal import internal_request
from infra.send_types import SendRejected
from sign_in_wake_helpers import (
    adopt, break_notice_write, parked_reports, record_drives, refuse_sign_in_wakes, reports_for, wakes,
)
from tests.test_google_sign_in import (  # noqa: F401  (the fixtures)
    EMAIL, REFRESH, SECRET, _accounts, _connect, _http, _start, client_file, conductor, google,
)

CALLBACK = "/api/google/oauth/callback"
EVERYWHERE = AppReach(mode="all", project_ids=frozenset(), outside_projects=False)


async def _callback(state: str, **query: str) -> tuple[int, str]:
    async with _http() as http:
        response = await http.get(CALLBACK, params={"state": state, **query})
    return response.status_code, response.text


async def _start_with_target(google, client_file: str, adding: str, starting: str) -> str:
    """Start an agent's add whose wake goes to ``starting``; returns its state."""
    url, _ = await oauth.start_sign_in(
        client_file, actor=("agent", adding), adding_thread_id=adding, starting_thread_id=starting,
    )
    query = parse_qs(urlparse(url).query)
    google.challenge = query["code_challenge"][0]
    return query["state"][0]


@pytest.fixture(autouse=True)
def drives(monkeypatch):
    return record_drives(monkeypatch)


def _stop_timer(state: str) -> None:
    oauth._expiries.pop(state).cancel()


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
    assert wake["chip"] == "Google sign-in finished"
    assert state not in oauth._expiries


async def test_an_agent_signing_an_account_in_again_wakes_that_thread_not_an_adding_one(google, conductor, client_file):
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
    pending = oauth._pending[query["state"][0]]
    assert (pending.adding_thread_id, pending.starting_thread_id) == (None, conductor)

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
    assert wake["chip"] == "Couldn’t finish Google sign-in"
    assert "Summary: Couldn’t finish Google sign-in: " in wake["content"]
    assert _accounts() == []


async def test_missing_scopes_wake_the_thread_with_the_cause_and_what_to_ask_for(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)
    google.scope = oauth.SCOPES[0]

    status, _page = await _callback(state, code="auth-code")

    assert status == 400
    [wake] = wakes()
    assert (wake["outcome"], wake["cause"], wake["message"]) == ("failed", "consent_cancelled", oauth.missing_scopes(oauth.OPTIONAL_SCOPES))
    assert _accounts() == []


async def test_a_fault_inside_corsia_wakes_the_thread_with_the_shared_provider_unavailable_facts(
    google, conductor, client_file, monkeypatch,
):
    _, state = await _start(google, conductor, client_file)

    def down(*_args):
        raise RuntimeError("canary-internal-detail")

    monkeypatch.setattr(oauth, "_store", down)
    status, _page = await _callback(state, code="auth-code")

    assert status == 500
    [wake] = wakes()
    assert (wake["outcome"], wake["cause"], wake["message"]) == ("failed", PROVIDER_UNAVAILABLE, UNAVAILABLE_MESSAGE)
    assert "canary-internal-detail" not in wake["content"]


async def test_a_replayed_callback_and_a_replayed_wake_add_no_second_input(google, conductor, client_file):
    _, state = await _start(google, conductor, client_file)

    first, _ = await _callback(state, code="auth-code")
    replay, _ = await _callback(state, code="auth-code")
    forged, _ = await _callback("forged", code="auth-code")
    [account] = _accounts()
    # A replay that reached the sender again is the same input, whatever it then reports.
    await notify_starting_agent(conductor, state, "google", account["id"], cause=CONSENT_CANCELLED)
    await notify_starting_agent(conductor, state, "google", account["id"], cause=CONSENT_CANCELLED)

    assert (first, replay, forged) == (200, 400, 400)
    assert len(wakes()) == 1


async def test_a_person_started_sign_in_wakes_no_one(google, conductor, client_file):
    url, _ = await oauth.start_sign_in(client_file, actor=("person", "person"), reach=EVERYWHERE)
    query = parse_qs(urlparse(url).query)
    google.challenge = query["code_challenge"][0]
    assert query["state"][0] in oauth._expiries

    status, _page = await _callback(query["state"][0], code="auth-code")

    assert status == 200 and len(_accounts()) == 1
    assert wakes() == []


async def test_an_unfinished_person_sign_in_expires_without_a_wake(google, conductor, client_file, monkeypatch):
    url, _ = await oauth.start_sign_in(client_file, actor=("person", "person"), reach=EVERYWHERE)
    state = parse_qs(urlparse(url).query)["state"][0]
    pending = oauth._pending[state]
    _stop_timer(state)
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0)

    await oauth._expire(pending)

    assert state not in oauth._pending and wakes() == []


async def test_expired_flow_counts_toward_cap_until_timer_removes_it(google, conductor, client_file, monkeypatch):
    monkeypatch.setattr(oauth, "PENDING_MAX", 1)
    _, state = await _start(google, conductor, client_file)
    pending = oauth._pending[state]
    _stop_timer(state)
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0)

    with pytest.raises(oauth.GoogleSignInError, match=oauth.TOO_MANY):
        await oauth.start_sign_in(client_file, actor=("person", "person"), reach=EVERYWHERE)
    await oauth._expire(pending)
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 600)
    await oauth.start_sign_in(client_file, actor=("person", "person"), reach=EVERYWHERE)


async def test_an_unfinished_sign_in_wakes_the_thread_when_it_expires(google, conductor, client_file, monkeypatch):
    _, state = await _start(google, conductor, client_file)
    pending = oauth._pending[state]
    _stop_timer(state)
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0)

    await oauth._expire(pending)

    [wake] = wakes()
    assert (wake["thread_id"], wake["outcome"], wake["cause"], wake["message"]) == (
        conductor, "failed", CONSENT_CANCELLED, EXPIRED_MESSAGE,
    )
    assert state not in oauth._pending and _accounts() == []


async def test_a_sign_in_the_callback_took_is_not_also_reported_expired(google, conductor, client_file, monkeypatch):
    _, state = await _start(google, conductor, client_file)
    pending = oauth._pending[state]
    assert (await _callback(state, code="auth-code"))[0] == 200
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0)

    await oauth._expire(pending)

    assert [wake["outcome"] for wake in wakes()] == ["connected"]


async def test_a_callback_after_deadline_leaves_expiry_to_the_timer(google, conductor, client_file, monkeypatch):
    _, state = await _start(google, conductor, client_file)
    pending = oauth._pending[state]
    _stop_timer(state)
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0)

    status, page = await _callback(state, code="auth-code")

    assert status == 400 and oauth.BROWSER_DENIED in page
    assert state in oauth._pending and wakes() == []
    await oauth._expire(pending)
    [wake] = wakes()
    assert (wake["thread_id"], wake["cause"], wake["message"]) == (conductor, CONSENT_CANCELLED, EXPIRED_MESSAGE)
    assert _accounts() == []


async def test_an_unavailable_target_keeps_the_account_and_is_logged(google, conductor, client_file, caplog):
    state = await _start_with_target(google, client_file, conductor, "no-such-thread")

    with caplog.at_level(logging.WARNING, logger="connections.sign_in_notice"):
        status, page = await _callback(state, code="auth-code")

    assert status == 200 and "Google is connected" in page
    [account] = _accounts()
    assert account["state"] == "connected"
    assert f"sign-in result for account {account['id']} not delivered to thread no-such-thread: ValueError" in caplog.text
    assert wakes() == []


async def test_a_connected_sign_in_drives_the_starting_thread_once_and_only_after_the_commit(
    google, conductor, client_file, drives,
):
    _, state = await _start(google, conductor, client_file)

    assert (await _callback(state, code="auth-code"))[0] == 200

    # The account row and the queued wake were both committed when the drive began.
    assert drives == [{"thread": conductor, "accounts": 1, "wakes": 1}]


async def test_a_failed_sign_in_drives_the_starting_thread_once(google, conductor, client_file, drives):
    _, state = await _start(google, conductor, client_file)

    assert (await _callback(state, error="access_denied"))[0] == 400

    assert drives == [{"thread": conductor, "accounts": 0, "wakes": 1}]


async def test_an_expired_sign_in_drives_the_starting_thread_once(google, conductor, client_file, drives, monkeypatch):
    _, state = await _start(google, conductor, client_file)
    pending = oauth._pending[state]
    _stop_timer(state)
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0)

    await oauth._expire(pending)

    assert drives == [{"thread": conductor, "accounts": 0, "wakes": 1}]


async def test_a_sign_in_with_no_starting_thread_drives_nothing(google, conductor, client_file, drives):
    url, _ = await oauth.start_sign_in(client_file, actor=("person", "person"), reach=EVERYWHERE)
    query = parse_qs(urlparse(url).query)
    google.challenge = query["code_challenge"][0]

    assert (await _callback(query["state"][0], code="auth-code"))[0] == 200
    assert (await _callback("forged", error="access_denied"))[0] == 400

    assert drives == []


async def test_a_fenced_thread_is_not_driven(google, conductor, client_file, drives, monkeypatch):
    from connections.sign_in_notice import drive_starting_agent

    _, state = await _start(google, conductor, client_file)
    assert (await _callback(state, code="auth-code"))[0] == 200
    drives.clear()
    monkeypatch.setattr("infra.admission_fence.is_fenced", lambda thread_id: thread_id == conductor)

    await drive_starting_agent(conductor, state)

    assert drives == []


async def test_a_drive_that_fails_changes_nothing_the_person_sees(google, conductor, client_file, monkeypatch, caplog):
    async def failing(_thread_id):
        raise RuntimeError("canary-drive")

    monkeypatch.setattr("infra.thread_execution.drive_thread", failing)
    _, state = await _start(google, conductor, client_file)

    with caplog.at_level(logging.ERROR, logger="connections.sign_in_notice"):
        status, page = await _callback(state, code="auth-code")

    assert status == 200 and "Google is connected" in page and len(_accounts()) == 1
    assert len(wakes()) == 1 and "driving thread" in caplog.text


async def test_a_wake_the_thread_refuses_is_reported_to_its_owner_and_that_owner_is_driven(
    google, conductor, client_file, monkeypatch, drives,
):
    from tests.registry_helpers import make_session

    db = get_db()
    try:
        child = make_session(db, prefix="child")
    finally:
        db.close()
    adopt(child, conductor)
    refuse_sign_in_wakes(monkeypatch)
    state = await _start_with_target(google, client_file, conductor, child)

    status, _page = await _callback(state, code="auth-code")

    assert status == 200 and [a["state"] for a in _accounts()] == ["connected"]
    [report] = reports_for(conductor)
    assert report.startswith("wake-failed:sign-in:") and wakes() == []
    # The refused thread has nothing queued to drive; its owner holds the report.
    assert [call["thread"] for call in drives] == [conductor]


async def test_a_wake_the_thread_refuses_and_no_owner_takes_is_parked(google, conductor, client_file):
    state = await _start_with_target(google, client_file, conductor, "no-such-thread")

    assert (await _callback(state, code="auth-code"))[0] == 200

    [parked] = parked_reports()
    assert parked["thread_id"] == "no-such-thread" and parked["event"] == "account.sign_in_finished"
    assert [a["state"] for a in _accounts()] == ["connected"]


async def test_a_refused_send_keeps_the_account_and_is_logged(google, conductor, client_file, monkeypatch, caplog):
    def refused(*_args, **_kwargs):
        raise SendRejected(409, "held")

    monkeypatch.setattr("events.delivery.enqueue_event_wake", refused)
    _, state = await _start(google, conductor, client_file)

    with caplog.at_level(logging.WARNING, logger="connections.sign_in_notice"):
        status, _page = await _callback(state, code="auth-code")

    assert status == 200 and len(_accounts()) == 1
    assert "not delivered to thread" in caplog.text and "SendRejected" in caplog.text


async def test_a_defect_in_the_wake_keeps_the_account_and_is_logged_with_its_traceback(
    google, conductor, client_file, monkeypatch, caplog,
):
    def defect(*_args, **_kwargs):
        raise RuntimeError("canary-defect")

    monkeypatch.setattr("events.delivery.enqueue_event_wake", defect)
    _, state = await _start(google, conductor, client_file)

    with caplog.at_level(logging.ERROR, logger="connections.sign_in_notice"):
        status, page = await _callback(state, code="auth-code")

    assert status == 200 and "Google is connected" in page
    assert [account["state"] for account in _accounts()] == ["connected"]
    [record] = [r for r in caplog.records if r.name == "connections.sign_in_notice"]
    assert record.exc_info is not None and "canary-defect" in str(record.exc_info[1])


async def test_a_failure_wake_to_an_unavailable_target_leaves_the_page_alone_and_is_logged(
    google, conductor, client_file, caplog,
):
    state = await _start_with_target(google, client_file, conductor, "no-such-thread")

    with caplog.at_level(logging.WARNING, logger="connections.sign_in_notice"):
        status, _page = await _callback(state, error="access_denied")

    assert status == 400 and wakes() == []
    assert "not delivered to thread no-such-thread" in caplog.text and "ValueError" in caplog.text


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


async def test_shutdown_queues_one_wake_for_the_waiting_agent_and_starts_no_turn(
    google, conductor, client_file, drives,
):
    _, state = await _start(google, conductor, client_file)
    timer = oauth._expiries[state]
    await oauth.start_sign_in(client_file, actor=("person", "person"), reach=EVERYWHERE)
    assert len(oauth._pending) == 2

    await oauth.stop_all_signins()
    await oauth.stop_all_signins()
    await asyncio.sleep(0)

    [wake] = wakes()
    assert (wake["thread_id"], wake["outcome"], wake["cause"], wake["message"]) == (
        conductor, "failed", CONSENT_CANCELLED, SHUT_DOWN_MESSAGE,
    )
    assert oauth._pending == {} and oauth._expiries == {} and timer.cancelled()
    assert drives == [] and _accounts() == []


async def test_backend_shutdown_stops_the_google_sign_ins(google, conductor, client_file, monkeypatch):
    from app_startup_runtime import RUNTIME_LOOP_TASKS
    from app_shutdown_owned_tasks import shutdown_owned_tasks
    from infra import state

    for name in ("_github_ownership_login_task", *(attr for _label, attr in RUNTIME_LOOP_TASKS)):
        monkeypatch.setattr(state, name, None, raising=False)
    await _start(google, conductor, client_file)

    await shutdown_owned_tasks([None] * 5, logging.getLogger("test"))

    assert [wake["message"] for wake in wakes()] == [SHUT_DOWN_MESSAGE] and oauth._pending == {}


async def test_the_expiry_the_agent_is_told_follows_the_constant(google, conductor, client_file, monkeypatch):
    from tools.google_account import GoogleConnectTool

    monkeypatch.setattr(oauth, "PENDING_SECONDS", 1200)

    out = await GoogleConnectTool().execute(_thread_id=conductor, client_file=client_file)

    assert "expires after 20 minutes" in json.loads(out)["next"]


async def test_a_notice_the_database_refuses_leaves_the_failure_page_alone(
    google, conductor, client_file, monkeypatch, caplog,
):
    break_notice_write(monkeypatch)
    _, state = await _start(google, conductor, client_file)

    with caplog.at_level(logging.ERROR, logger="connections.sign_in_notice"):
        status, page = await _callback(state, error="access_denied")

    assert status == 400 and oauth.BROWSER_DENIED in page and wakes() == []
    assert "the notice write for thread" in caplog.text


async def test_a_notice_the_database_refuses_never_escapes_the_expiry_task(
    google, conductor, client_file, monkeypatch,
):
    break_notice_write(monkeypatch)
    _, state = await _start(google, conductor, client_file)
    pending = oauth._pending[state]
    _stop_timer(state)
    monkeypatch.setattr(oauth, "PENDING_SECONDS", 0)

    await oauth._expire(pending)

    assert state not in oauth._pending and wakes() == []


async def test_a_notice_the_database_refuses_keeps_the_stored_account(google, conductor, client_file, monkeypatch):
    break_notice_write(monkeypatch)
    _, state = await _start(google, conductor, client_file)

    status, page = await _callback(state, code="auth-code")

    assert status == 200 and "Google is connected" in page
    assert [a["state"] for a in _accounts()] == ["connected"] and wakes() == []


def test_the_provider_unavailable_facts_are_the_same_on_every_provider():
    from connections.sign_in_notice import _wake

    google = _wake("google", "acct", cause=PROVIDER_UNAVAILABLE, message="Google's own text")
    microsoft = _wake("microsoft", "acct", cause=PROVIDER_UNAVAILABLE, message="Microsoft's own text")

    assert google[2] | {"provider": None} == microsoft[2] | {"provider": None}
    assert google[2]["message"] == UNAVAILABLE_MESSAGE
