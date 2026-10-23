"""A mail-host row on screen: what the list says about each state, and the row's own Try again and
Edit server, which run only behind the person's session and CORSIA password (H17).

Servers are local TLS fakes (``mailhost_fakes``). The routes are driven over ASGI like the app's own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from types import SimpleNamespace

import httpx
import pytest
from auth_fixtures import OWNER_SESSION_TOKEN, OWNER_TOKEN, OWNER_USER, TEST_PASSWORD, seed_account
from fastapi import FastAPI
from mailhost_fakes import HOST_REPLY_MARKER
from mailhost_helpers import (  # noqa: F401  (fixtures)
    ADDRESS, PASSWORD, add_account, authority, conductor, env, row, run_check, servers,
)

from api import key_write_guard
from api import keys as keys_api
from api import mcp as mcp_api
from connections.mailhost import check, endpoints, store
from infra import accounts, key_store
from infra.database import get_db

pytestmark = pytest.mark.asyncio
WRONG = "not-the-password-0123"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _auth(token: str = OWNER_SESSION_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _no_leak(*texts: str) -> None:
    for text in texts:
        assert PASSWORD not in text and TEST_PASSWORD not in text and WRONG not in text and HOST_REPLY_MARKER not in text


@pytest.fixture
def client(env, monkeypatch):
    seed_account(OWNER_USER, "second-session-credential-for-row-controls", kind="session")
    monkeypatch.setattr(key_write_guard, "_key_write_lock", asyncio.Lock())
    monkeypatch.setattr(key_write_guard, "_pending", {})
    app = FastAPI()
    app.include_router(keys_api.router)
    app.include_router(mcp_api.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def unreachable(conductor, servers, client):
    """A row whose IMAP server nothing listens on, so its check ended Server unreachable."""
    built = servers()
    account_id = add_account(built, conductor, imap=f"127.0.0.1:{_free_port()}")["account_id"]
    key = key_store.MAILHOST_PREFIX + account_id
    key_store.set_value(key, PASSWORD)
    return SimpleNamespace(built=built, account_id=account_id, key=key, client=client)


async def _listed(client, account_id: str) -> dict:
    listed = await client.get("/api/mcp/servers", headers=_auth())
    assert listed.status_code == 200, listed.text
    _no_leak(listed.text)
    return next(found for found in listed.json()["accounts"] if found["id"] == account_id)


async def _check(client, account_id, *, token=OWNER_SESSION_TOKEN, body=None):
    """Try again: one press, so no body unless a test sends one."""
    return await client.post(f"/api/mcp/accounts/{account_id}/mailhost/check", headers=_auth(token), json=body)


async def _servers(client, account_id, servers, *, password=TEST_PASSWORD, token=OWNER_SESSION_TOKEN):
    body = {"imap": servers.imap, "smtp": servers.smtp, "caldav": servers.caldav, "password": password}
    return await client.put(f"/api/mcp/accounts/{account_id}/mailhost/servers", headers=_auth(token), json=body)


def _good(built) -> SimpleNamespace:
    return SimpleNamespace(imap=f"127.0.0.1:{built.imap.port}", smtp=f"127.0.0.1:{built.smtp.port}", caldav=built.caldav.url)


# -- what the list row says ---------------------------------------------------------------------


async def test_a_waiting_row_names_its_host_and_where_the_key_is(conductor, servers, client) -> None:
    account_id = add_account(servers(), conductor)["account_id"]
    found = await _listed(client, account_id)
    detail = found["mailhost"]
    assert found["provider"] == "mailhost" and found["provider_display_name"] == "mail.test"
    assert detail["problem"] == "waiting" and detail["calendar_missing"] is False
    assert detail["key_refs"] == [{
        "entry": key_store.MAILHOST_PREFIX + account_id, "name": f"mail.test key: {ADDRESS}", "set": False,
    }]
    assert detail["destination_hosts"] == ["127.0.0.1"]
    assert detail["imap"].startswith("127.0.0.1:") and detail["smtp"].startswith("127.0.0.1:")
    assert "manage_url" not in detail and "server" not in detail and "failing" not in detail


async def test_connected_has_no_problem_and_mail_only_says_the_calendar_is_missing(conductor, servers, client) -> None:
    both = add_account(servers(), conductor)["account_id"]
    assert await run_check(both) == store.OK
    mail_only = add_account(servers(with_caldav=False), conductor, address="pat@other.test", username=ADDRESS)["account_id"]
    assert await run_check(mail_only) == store.MAIL_ONLY
    ok, partial = await _listed(client, both), await _listed(client, mail_only)
    assert ok["mailhost"]["problem"] is None and ok["mailhost"]["calendar_missing"] is False
    assert ok["state"] == "connected" and ok["capabilities"] == ["mail", "calendar"]
    assert partial["mailhost"]["problem"] is None and partial["mailhost"]["calendar_missing"] is True
    assert partial["capabilities"] == ["mail"]
    assert ok["mailhost"]["key_refs"][0]["set"] is True


async def test_refused_and_imap_off_and_unreachable_are_named(conductor, servers, client, unreachable) -> None:
    refused = add_account(servers(password="[REDACTED]"), conductor, address="pat@other.test")["account_id"]
    assert await run_check(refused) == store.REFUSED
    assert (await _listed(client, refused))["mailhost"]["problem"] == "refused"
    db = get_db()
    try:
        endpoints, _ = store.read(unreachable.account_id, db=db)
        store.write(unreachable.account_id, endpoints, status=store.IMAP_OFF, db=db)
    finally:
        db.close()
    assert (await _listed(client, unreachable.account_id))["mailhost"]["problem"] == "imap_off"
    assert await check.start_check(unreachable.account_id)
    await check.wait_for_checks()
    found = (await _listed(client, unreachable.account_id))["mailhost"]
    assert found["problem"] == "unreachable" and found["server"] == "127.0.0.1" and found["failing"] == "imap"
    assert found["imap"].startswith("127.0.0.1:")


async def test_a_known_host_row_carries_its_manage_page_and_no_destination_line(client, conductor, env) -> None:
    from connections.mailhost.known_hosts import known_host

    db = get_db()
    try:
        from connections.account_state import emit_lifecycle_event  # noqa: F401
        from connections.connected_accounts import MAILHOST_PROVIDER, create_account_for_thread

        account = create_account_for_thread(
            MAILHOST_PROVIDER, "you@icloud.com", actor_thread_id=conductor, created_by=("agent", conductor),
            provider_subject="you@icloud.com", state="needs_sign_in", db=db,
        )
        store.write(str(account["id"]), known_host("you@icloud.com").endpoints("you@icloud.com"), status=store.WAITING, db=db)
        account_id = str(account["id"])
    finally:
        db.close()
    found = await _listed(client, account_id)
    assert found["provider_display_name"] == "iCloud"
    assert found["mailhost"]["manage_url"] == "https://account.apple.com/"
    assert "destination_hosts" not in found["mailhost"]


# -- Keys card actions (F2) ----------------------------------------------------------------------


async def test_a_mail_host_entry_can_be_cleared_but_the_card_is_told_it_cannot_be_removed(conductor, servers, client) -> None:
    account_id = add_account(servers(), conductor)["account_id"]
    key = key_store.MAILHOST_PREFIX + account_id
    assert key_store.entry_metadata(key)["removable"] is False
    assert key_store.entry_metadata("web-search")["removable"] is False
    added = await client.post("/api/keys", headers=_auth(), json={"name": "maps", "label": "Maps"})
    assert added.json()["key"]["removable"] is True
    refused = await client.post(f"/api/keys/{key}/remove", headers=_auth(), json={"password": TEST_PASSWORD})
    assert refused.status_code == 400 and refused.json()["detail"]["message"] == key_store.removal_refusal(key)


# -- Try again ------------------------------------------------------------------------------------
# One press: no CORSIA password, no body. It is the person's alone by session provenance (an agent or
# API token is refused), takes only the row id and reads the stored servers when it runs.


async def test_try_again_is_one_press_with_no_password_and_returns_the_checking_row(unreachable) -> None:
    edited = await _servers(unreachable.client, unreachable.account_id, _good(unreachable.built))
    assert edited.status_code == 200
    await check.wait_for_checks()
    assert row(unreachable.account_id)["check"]["status"] == store.OK
    unreachable.built.imap.password = "changed-at-the-host-0000"
    again = await _check(unreachable.client, unreachable.account_id)
    assert again.status_code == 200
    assert again.json()["row"]["mailhost"]["problem"] == "checking"
    await check.wait_for_checks()
    assert (await _listed(unreachable.client, unreachable.account_id))["mailhost"]["problem"] == "refused"
    _no_leak(again.text)


async def test_try_again_is_the_persons_alone_an_agent_or_api_token_is_refused_403(unreachable) -> None:
    before = row(unreachable.account_id)["raw"]
    refused = await _check(unreachable.client, unreachable.account_id, token=OWNER_TOKEN)
    assert refused.status_code == 403
    assert (await _check(unreachable.client, unreachable.account_id, token="nobody")).status_code == 401
    query = await unreachable.client.post(
        f"/api/mcp/accounts/{unreachable.account_id}/mailhost/check?token={OWNER_SESSION_TOKEN}",
    )
    assert query.status_code == 401
    await check.wait_for_checks()
    assert row(unreachable.account_id)["raw"] == before and unreachable.built.imap.commands == []
    _no_leak(refused.text)


async def test_try_again_on_another_providers_or_unknown_account_is_not_found(client, env) -> None:
    for account_id in ("00000000-0000-0000-0000-000000000000", "nope"):
        missing = await _check(client, account_id)
        assert missing.status_code == 404 and missing.json()["detail"] == "That account isn't connected."


async def test_try_again_takes_only_the_row_id_and_never_goes_anywhere_but_the_stored_servers(unreachable) -> None:
    good = _good(unreachable.built)
    await _servers(unreachable.client, unreachable.account_id, good)
    await check.wait_for_checks()
    stored = row(unreachable.account_id)["endpoints"]
    elsewhere = {"imap": "attacker.example:993", "smtp": "attacker.example:465", "caldav": "https://attacker.example/",
                 "host": "attacker.example", "port": 993, "tls": False, "username": "evil", "password": "whatever"}
    again = await _check(unreachable.client, unreachable.account_id, body=elsewhere)
    assert again.status_code == 200
    await check.wait_for_checks()
    after = row(unreachable.account_id)
    assert after["endpoints"]["imap"] == stored["imap"] and after["endpoints"]["smtp"] == stored["smtp"]
    assert after["endpoints"]["caldav"] == stored["caldav"] and after["check"]["status"] == store.OK
    logins = [verb for _, verb in unreachable.built.imap.commands].count("LOGIN")
    assert logins == 2, "both checks logged in at the stored server, and nowhere else"
    assert "attacker" not in after["raw"] and "evil" not in after["raw"]


async def test_try_again_goes_through_the_connect_check_so_the_password_never_leaves_before_tls(conductor, servers, client, monkeypatch) -> None:
    built = servers(imap={"implicit_tls": False, "offer_starttls": False})
    monkeypatch.setattr(endpoints, "IMAP_STARTTLS_PORTS", frozenset((built.imap.port,)))
    account_id = add_account(built, conductor)["account_id"]
    key_store.set_value(key_store.MAILHOST_PREFIX + account_id, PASSWORD)
    seen: list[str] = []
    real = check.run_check

    async def watched(account, *args):
        seen.append(account)
        return await real(account, *args)

    monkeypatch.setattr(check, "run_check", watched)
    assert (await _check(client, account_id)).status_code == 200
    await check.wait_for_checks()
    assert seen == [account_id]
    assert row(account_id)["check"]["status"] == store.UNREACHABLE
    assert all(verb != "LOGIN" for _, verb in built.imap.commands)
    assert PASSWORD.encode() not in built.imap.plain


async def test_a_second_try_again_while_one_runs_joins_it_and_starts_no_second_check(unreachable, monkeypatch) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    runs: list[str] = []

    async def slow(account_id, ep, password):
        runs.append(account_id)
        started.set()
        await release.wait()
        return ep, store.UNREACHABLE, ep.imap.host, ()

    monkeypatch.setattr(check, "_outcome", slow)
    first = await _check(unreachable.client, unreachable.account_id)
    assert first.status_code == 200 and first.json()["row"]["mailhost"]["problem"] == "checking"
    await asyncio.wait_for(started.wait(), 5)
    second, third = await asyncio.gather(
        _check(unreachable.client, unreachable.account_id), _check(unreachable.client, unreachable.account_id),
    )
    for joined in (second, third):
        assert joined.status_code == 200 and joined.json()["row"]["mailhost"]["problem"] == "checking"
    release.set()
    await check.wait_for_checks()
    assert runs == [unreachable.account_id], "one check ran for three presses"
    assert row(unreachable.account_id)["check"]["status"] == store.UNREACHABLE
    again = await _check(unreachable.client, unreachable.account_id)
    assert again.status_code == 200
    await check.wait_for_checks()
    assert len(runs) == 2, "once the first ended, Try again starts a new check"


# -- Edit server ----------------------------------------------------------------------------------


async def test_edit_server_checks_against_the_new_names_and_marks_the_row_typed_in(unreachable) -> None:
    good = _good(unreachable.built)
    saved = await _servers(unreachable.client, unreachable.account_id, good)
    assert saved.status_code == 200 and saved.json()["row"]["mailhost"]["problem"] == "checking"
    await check.wait_for_checks()
    found = row(unreachable.account_id)
    assert found["check"]["status"] == store.OK and found["state"] == "connected"
    assert found["endpoints"]["imap"]["port"] == unreachable.built.imap.port and found["endpoints"]["known"] is False
    _no_leak(saved.text, found["raw"])


async def test_edit_server_needs_the_password_and_a_wrong_one_changes_nothing(unreachable) -> None:
    before = row(unreachable.account_id)["raw"]
    good = _good(unreachable.built)
    missing = await unreachable.client.put(
        f"/api/mcp/accounts/{unreachable.account_id}/mailhost/servers", headers=_auth(),
        json={"imap": good.imap, "smtp": good.smtp, "caldav": good.caldav},
    )
    assert missing.status_code == 400 and missing.json()["detail"]["message"] == key_write_guard.NO_PASSWORD
    wrong = await _servers(unreachable.client, unreachable.account_id, good, password=WRONG)
    assert wrong.status_code == 403 and wrong.json()["detail"]["message"] == key_write_guard.MISMATCH
    await check.wait_for_checks()
    assert row(unreachable.account_id)["raw"] == before and unreachable.built.imap.commands == []
    _no_leak(missing.text, wrong.text)


async def test_edit_server_is_the_persons_alone(unreachable) -> None:
    before = row(unreachable.account_id)["raw"]
    for token in (OWNER_TOKEN, "nobody"):
        refused = await _servers(unreachable.client, unreachable.account_id, _good(unreachable.built), token=token)
        assert refused.status_code in (401, 403)
    await check.wait_for_checks()
    assert row(unreachable.account_id)["raw"] == before


async def test_three_wrong_passwords_on_edit_server_end_that_session_and_the_busy_answer_is_429(unreachable) -> None:
    good = _good(unreachable.built)
    for _ in range(2):
        assert (await _servers(unreachable.client, unreachable.account_id, good, password=WRONG)).status_code == 403
    answers = await asyncio.gather(*(
        _servers(unreachable.client, unreachable.account_id, good, password=WRONG) for _ in range(4)
    ))
    codes = sorted(answer.status_code for answer in answers)
    assert codes.count(429) >= 3 and 401 in codes
    assert accounts.resolve_token(OWNER_SESSION_TOKEN) is None
    assert (await _servers(unreachable.client, unreachable.account_id, good)).status_code == 401
    assert unreachable.built.imap.commands == []


async def test_edit_server_refuses_a_bad_name_with_its_fixed_line_before_the_password_is_counted(unreachable) -> None:
    good = _good(unreachable.built)
    bad = SimpleNamespace(imap="https://mail.example.com/x", smtp=good.smtp, caldav=good.caldav)
    refused = await _servers(unreachable.client, unreachable.account_id, bad, password=WRONG)
    assert refused.status_code == 400
    assert refused.json()["detail"]["message"].startswith("imap must be a server name such as mail.example.com")
    insecure = SimpleNamespace(imap=good.imap, smtp=good.smtp, caldav="http://caldav.example.com/")
    assert (await _servers(unreachable.client, unreachable.account_id, insecure)).status_code == 400
    # Neither refusal counted a wrong password: three more wrong ones are still needed to end the session.
    assert accounts.resolve_token(OWNER_SESSION_TOKEN) is not None
    assert (await _servers(unreachable.client, unreachable.account_id, good, password=WRONG)).status_code == 403


async def test_an_empty_calendar_address_clears_it_and_the_row_ends_mail_only(unreachable) -> None:
    good = _good(unreachable.built)
    saved = await _servers(unreachable.client, unreachable.account_id, SimpleNamespace(**{**good.__dict__, "caldav": "  "}))
    assert saved.status_code == 200
    await check.wait_for_checks()
    assert row(unreachable.account_id)["check"]["status"] == store.MAIL_ONLY
    assert row(unreachable.account_id)["endpoints"]["caldav"] is None


# -- H14 over the new routes ------------------------------------------------------------------------


async def test_no_password_reaches_a_response_a_log_or_the_stored_row_on_either_route(unreachable, caplog) -> None:
    good = _good(unreachable.built)
    with caplog.at_level(logging.DEBUG):
        texts = [
            (await _servers(unreachable.client, unreachable.account_id, good, password=WRONG)).text,
            (await _servers(unreachable.client, unreachable.account_id, good)).text,
        ]
        await check.wait_for_checks()
        unreachable.built.imap.password = "[REDACTED]"
        texts.append((await _check(unreachable.client, unreachable.account_id)).text)
        await check.wait_for_checks()
        texts.append(json.dumps(await _listed(unreachable.client, unreachable.account_id)))
    _no_leak(*texts, caplog.text, row(unreachable.account_id)["raw"])
