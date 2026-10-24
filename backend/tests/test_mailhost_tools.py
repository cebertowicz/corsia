"""Mail hosts through the surfaces agents and people use: ``mail_host_connect``, Settings › Keys,
``email_search``, ``email_read`` and ``calendar_list``. Servers are local TLS fakes (``mailhost_fakes``)."""

from __future__ import annotations

import asyncio
import imaplib
import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from auth_fixtures import OWNER_SESSION_TOKEN, OWNER_USER, TEST_PASSWORD, seed_account
from fastapi import FastAPI
from mailhost_fakes import HOST_REPLY_MARKER
from mailhost_helpers import (  # noqa: F401  (fixtures)
    ADDRESS, BODY_CANARY, PASSWORD, add_account, authority, conductor, count_rows, env, row, run_check, servers,
)

from api import key_write_guard
from api import keys as keys_api
from api import mcp as mcp_api
from api.connected_account_rows import list_account_rows
from connections import account_actions, mail_accounts
from connections.account_errors import AccountError
from connections.google import accounts as google_accounts
from connections.google.client import GoogleError
from connections.mail import email_screen
from connections.mailhost import check, imap, read as mailhost_read, store
from connections.mailhost.errors import IMAP_OFF, PASSWORD_REFUSED, SERVER_UNREACHABLE, MailHostError
from infra import key_store
from infra.database import get_db
from tests.registry_helpers import make_session
from tools.calendar_list import CalendarListTool
from tools.email_read import EmailReadTool, EmailSearchTool
from tools.mail_host_connect import MailHostConnectTool

pytestmark = pytest.mark.asyncio
WINDOW = {"time_min": "2026-10-22T00:00:00-07:00", "time_max": "2026-10-28T00:00:00-07:00"}


def _no_leak(*texts: str) -> None:
    for text in texts:
        assert PASSWORD not in text and HOST_REPLY_MARKER not in text and TEST_PASSWORD not in text


async def _connect(conductor_thread: str, **arguments) -> Any:
    out = await MailHostConnectTool().execute(_thread_id=conductor_thread, **arguments)
    return out if out.startswith("Error") else json.loads(out)


def _typed(built, **extra) -> dict:
    return {"imap": f"127.0.0.1:{built.imap.port}", "smtp": f"127.0.0.1:{built.smtp.port}", "caldav": built.caldav.url, **extra}


@pytest.fixture
def client(env, monkeypatch):
    seed_account(OWNER_USER, "second-session-credential-for-mailhost-tests", kind="session")
    monkeypatch.setattr(key_write_guard, "_key_write_lock", asyncio.Lock())
    monkeypatch.setattr(key_write_guard, "_pending", {})
    app = FastAPI()
    app.include_router(keys_api.router)
    app.include_router(mcp_api.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _auth() -> dict:
    return {"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"}


async def _put(client, name: str, value: str = PASSWORD):
    return await client.put(f"/api/keys/{name}/value", headers=_auth(), json={"value": value, "password": TEST_PASSWORD})


@pytest.fixture
def connected(conductor, servers, client):
    """A connected account, set through the real Keys route."""
    built = servers()
    account_id = add_account(built, conductor)["account_id"]
    return SimpleNamespace(built=built, account_id=account_id, thread=conductor, client=client,
                           key=key_store.MAILHOST_PREFIX + account_id)


async def _set_through_keys(found) -> None:
    saved = await _put(found.client, found.key)
    assert saved.status_code == 200, saved.text
    _no_leak(saved.text)
    await check.wait_for_checks()
    assert row(found.account_id)["state"] == "connected"


@pytest.fixture
def clean_screen(monkeypatch):
    async def screened(*_args, **_kwargs):
        return email_screen.Screen(status=email_screen.STATUS_SCREENED, kind="invoice", needs_person="today")

    monkeypatch.setattr(email_screen, "screen_message", screened)


# -- mail_host_connect ----------------------------------------------------------------------------


async def test_connect_adds_one_waiting_row_and_its_key_and_never_asks_for_a_password(conductor, servers) -> None:
    built = servers()
    result = await _connect(conductor, address=ADDRESS, **_typed(built))
    assert result["status"] == "waiting" and result["already_existed"] is False
    assert result["key_name"].startswith(key_store.MAILHOST_PREFIX) and result["link"].endswith(result["account_id"])
    assert sorted(result["password_goes_to"]) == ["127.0.0.1"]
    assert "never ask for, read or repeat the password" in result["next"]
    entry = key_store.entry_metadata(result["key_name"])
    assert entry["is_set"] is False and entry["label"] == ADDRESS
    assert row(result["account_id"])["state"] == "needs_sign_in" and count_rows() == 1
    _no_leak(json.dumps(result))


async def test_a_second_call_for_the_same_address_changes_no_server_and_adds_no_row(conductor, servers) -> None:
    built = servers()
    first = await _connect(conductor, address=ADDRESS, **_typed(built))
    before = row(first["account_id"])["endpoints"]
    again = await _connect(conductor, address=ADDRESS.upper(), imap="attacker.example:993", smtp="attacker.example:465",
                           caldav="https://attacker.example/")
    assert again["account_id"] == first["account_id"] and again["already_existed"] is True
    assert row(first["account_id"])["endpoints"] == before and count_rows() == 1
    assert "attacker" not in json.dumps(again)


async def test_a_known_host_takes_its_servers_from_the_table_and_ignores_typed_ones(conductor) -> None:
    result = await _connect(conductor, address="pat@icloud.com", imap="attacker.example", smtp="attacker.example")
    stored = row(result["account_id"])["endpoints"]
    assert (stored["imap"]["host"], stored["smtp"]["host"]) == ("imap.mail.me.com", "smtp.mail.me.com")
    assert result["host"] == "iCloud" and result["app_password_url"].startswith("https://account.apple.com")
    assert "password_goes_to" not in result


async def test_zoho_says_to_turn_imap_on_before_the_password(conductor) -> None:
    result = await _connect(conductor, address="pat@zoho.com")
    assert "IMAP" in result["first_turn_on_imap"] and "IMAP" in result["next"]


async def test_gmail_microsoft_and_unknown_hosts_are_refused_without_adding_a_row(conductor) -> None:
    assert "google_connect" in await _connect(conductor, address="pat@gmail.com")
    assert "microsoft_connect" in await _connect(conductor, address="pat@Outlook.com")
    assert "doesn't know this address's mail servers" in await _connect(conductor, address="pat@unknown.example")
    refused = await _connect(conductor, address="pat@unknown.example", imap="https://evil.example/x", smtp="smtp.unknown.example")
    assert refused.startswith("Error") and "evil" not in refused
    assert (await _connect(conductor, address="not an address")).startswith("Error")
    assert count_rows() == 0


async def test_only_the_corsia_thread_may_connect(conductor, servers) -> None:
    other = make_session(get_db(), prefix="worker")
    refused = await _connect(other, address=ADDRESS, **_typed(servers()))
    assert refused == "Error: Only the designated CORSIA thread can use mail host tools."
    assert (await MailHostConnectTool().execute(address=ADDRESS)).startswith("Error: current thread identity")
    assert count_rows() == 0


# -- Settings › Keys --------------------------------------------------------------------------------


async def test_keys_cannot_create_an_entry_under_the_managed_prefix(client) -> None:
    for name in (f"{key_store.MAILHOST_PREFIX}{uuid.uuid4()}", f"{key_store.MAILHOST_PREFIX}x"):
        made = await client.post("/api/keys", headers=_auth(), json={"name": name, "label": "x", "description": ""})
        assert made.status_code == 400, made.text


async def test_pasting_the_password_runs_the_check_and_the_account_connects(connected, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        await _set_through_keys(connected)
    found = row(connected.account_id)
    assert found["capabilities"] == ["mail", "calendar"] and found["check"]["status"] == store.OK
    listed = await connected.client.get("/api/keys", headers=_auth())
    _no_leak(listed.text, caplog.text, found["raw"])


async def test_keys_list_discloses_typed_server_destinations_but_not_known_hosts(conductor, servers, client) -> None:
    typed = add_account(servers(), conductor)
    known = await _connect(conductor, address="pat@icloud.com")
    listing = await client.get("/api/keys", headers=_auth())
    assert listing.status_code == 200, listing.text
    entries = {entry["name"]: entry for entry in listing.json()["keys"]}
    assert entries[typed["key_name"]]["destination_hosts"] == ["127.0.0.1"]
    assert "destination_hosts" not in entries[known["key_name"]]


async def test_clearing_the_key_through_keys_returns_the_account_to_waiting(connected) -> None:
    await _set_through_keys(connected)
    cleared = await connected.client.post(f"/api/keys/{connected.key}/clear", headers=_auth(), json={"password": TEST_PASSWORD})
    assert cleared.status_code == 200, cleared.text
    found = row(connected.account_id)
    assert found["state"] == "needs_sign_in" and found["capabilities"] == [] and found["check"]["status"] == store.WAITING


async def test_keys_will_not_remove_a_mail_accounts_entry_but_removing_the_account_takes_it(connected) -> None:
    url = f"/api/keys/{connected.key}/remove"
    removed = await connected.client.post(url, headers=_auth(), json={"password": TEST_PASSWORD})
    assert removed.status_code == 400 and "cleared but not removed" in removed.text
    assert key_store.entry_metadata(connected.key)["label"] and count_rows() == 1
    with pytest.raises(key_store.KeyStoreError, match="cleared but not removed"):
        key_store.remove_entry(connected.key)
    await account_actions.remove_account(connected.account_id, actor=("person", "test"))
    assert count_rows() == 0
    with pytest.raises(key_store.KeyStoreError):
        key_store.entry_metadata(connected.key)


async def test_a_known_hosts_pasted_password_loses_all_whitespace_and_a_typed_hosts_is_kept_or_refused(conductor, servers) -> None:
    icloud = (await _connect(conductor, address="pat@icloud.com"))["key_name"]
    key_store.set_value(icloud, " abcd efgh\tijkl\nmnop ")
    assert key_store.resolve_names([icloud])[icloud] == "abcdefghijklmnop"
    typed = (await _connect(conductor, address=ADDRESS, **_typed(servers())))["key_name"]
    key_store.set_value(typed, "  no-spaces-here  ")
    assert key_store.resolve_names([typed])[typed] == "no-spaces-here"
    with pytest.raises(key_store.KeyStoreError) as refused:
        key_store.set_value(typed, "secret word")
    assert "mail.test" in str(refused.value) and "secret" not in str(refused.value)


def _account_row_selects() -> list[str]:
    db = get_db()
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        list_account_rows(None, db=db)
    finally:
        db.set_trace_callback(None)
        db.close()
    return [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]


def _mailhost_detail_reads(statements: list[str]) -> list[str]:
    return [
        statement for statement in statements
        if "FROM CONNECTED_ACCOUNTS WHERE PROVIDER='MAILHOST' AND ID IN" in statement.upper()
        or "SELECT NAME,LABEL FROM KEY_STORE_ENTRIES WHERE NAME IN" in statement.upper()
        or "SELECT NAME, VALUE_REF FROM KEY_STORE_ENTRIES WHERE NAME IN" in statement.upper()
    ]


async def test_account_rows_batch_mailhost_details_for_one_or_three_accounts(conductor, servers) -> None:
    add_account(servers(), conductor)
    one = _account_row_selects()
    add_account(servers(), conductor, address="second@mail.test")
    add_account(servers(), conductor, address="third@mail.test")
    three = _account_row_selects()

    assert (len(one), len(three)) == (8, 16)
    assert len(_mailhost_detail_reads(one)) == len(_mailhost_detail_reads(three)) == 3


async def test_account_rows_do_not_fall_back_to_per_row_details_when_batch_omits_a_row(conductor, servers) -> None:
    account_id = add_account(servers(), conductor)["account_id"]
    db = get_db()
    try:
        db.execute("UPDATE connected_accounts SET endpoints_json=NULL WHERE id=?", (account_id,))
        db.commit()
    finally:
        db.close()

    statements = _account_row_selects()
    assert len(_mailhost_detail_reads(statements)) == 3


@pytest.mark.parametrize(
    ("reply", "cause"),
    [
        ("[UNAVAILABLE] busy", SERVER_UNREACHABLE), ("[SERVERBUG] retry", SERVER_UNREACHABLE),
        ("[LIMIT] retry", SERVER_UNREACHABLE), ("[INUSE] busy", SERVER_UNREACHABLE),
        ("too many connections", SERVER_UNREACHABLE), ("try again later", SERVER_UNREACHABLE),
        ("NO login refused", PASSWORD_REFUSED), ("IMAP is disabled", IMAP_OFF),
    ],
)
async def test_login_refusals_keep_temporary_and_host_states_distinct(connected, monkeypatch, reply, cause) -> None:
    endpoints = store.read(connected.account_id, db=get_db())[0]
    assert endpoints is not None

    class RefusingLogin:
        def login(self, *_args):
            raise imaplib.IMAP4.error(reply)

        def logout(self):
            return None

    monkeypatch.setattr(imap, "_open", lambda _endpoints: RefusingLogin())
    with pytest.raises(MailHostError) as error:
        with imap.session(endpoints, endpoints.username, PASSWORD):
            pass
    assert error.value.cause == cause


async def test_login_abort_is_unreachable_and_does_not_demote_the_row(connected, monkeypatch) -> None:
    endpoints, before = store.read(connected.account_id, db=get_db())
    assert endpoints is not None

    class AbortOnLogin:
        def login(self, *_args):
            raise imaplib.IMAP4.abort("BYE")

        def logout(self):
            return None

    monkeypatch.setattr(imap, "_open", lambda _endpoints: AbortOnLogin())
    with pytest.raises(MailHostError, match="couldn’t reach") as error:
        with imap.session(endpoints, endpoints.username, PASSWORD):
            pass
    assert error.value.cause == SERVER_UNREACHABLE

    def unreachable():
        raise MailHostError(SERVER_UNREACHABLE, host=endpoints.imap.host)

    with pytest.raises(MailHostError):
        await mailhost_read._guarded(connected.account_id, before, unreachable)
    assert store.read(connected.account_id, db=get_db())[1] == before


# -- reading mail and calendar --------------------------------------------------------------------


async def test_search_and_read_work_on_a_connected_mail_host_account(connected, clean_screen) -> None:
    await _set_through_keys(connected)
    found = json.loads(await EmailSearchTool().execute(_thread_id=connected.thread, query="from:sam", max=5))
    assert connected.built.imap.searches[-1] == 'FROM "sam"'
    ids = [item["message_id"] for item in found["messages"]]
    assert ids == ["7-2", "7-1"] and found["messages"][0]["from"].endswith("<billing@example.com>")
    assert all("body" not in item for item in found["messages"])
    read = json.loads(await EmailReadTool().execute(_thread_id=connected.thread, message_id="7-2"))
    assert read["subject"] == "Invoice 42" and BODY_CANARY in read["body"] and read["body"].startswith("<untrusted-email>")
    _no_leak(json.dumps(found), json.dumps(read))


async def test_a_message_from_before_the_mailbox_was_rebuilt_reads_as_not_found(connected, clean_screen) -> None:
    await _set_through_keys(connected)
    for stale in ("8-2", "7-99", "7-2; DELETE", "abc"):
        out = await EmailReadTool().execute(_thread_id=connected.thread, message_id=stale)
        assert out.startswith("Error") and "can't be found" in out and "DELETE" not in out.split("found")[-1]


async def test_search_refuses_what_imap_cannot_do_without_touching_the_host(connected) -> None:
    await _set_through_keys(connected)
    before = len(connected.built.imap.searches)
    out = await EmailSearchTool().execute(_thread_id=connected.thread, query="a OR b")
    assert out.startswith("Error: This account searches") and len(connected.built.imap.searches) == before


async def test_calendar_list_returns_the_hosts_events_in_the_google_shape(connected) -> None:
    await _set_through_keys(connected)
    found = json.loads(await CalendarListTool().execute(_thread_id=connected.thread, **WINDOW))
    assert [event["title"] for event in found["events"]] == ["Standup", "Holiday", "Weekly sync"]
    assert set(found["events"][0]) == {"id", "start", "end", "title", "attendees", "free/busy"}
    refused = await CalendarListTool().execute(_thread_id=connected.thread, calendar_id="work", **WINDOW)
    assert refused.startswith("Error: This account lists all its calendars together")


async def test_a_mail_only_account_says_so_for_the_calendar(conductor, servers) -> None:
    built = servers(with_caldav=False)
    account_id = add_account(built, conductor)["account_id"]
    assert await run_check(account_id) == store.MAIL_ONLY
    out = await CalendarListTool().execute(_thread_id=conductor, **WINDOW)
    assert out == "Error: This account's host didn't offer a calendar to CORSIA, so only its email can be used."
    assert json.loads(await EmailSearchTool().execute(_thread_id=conductor, query="lunch"))["messages"]


async def test_a_waiting_or_refused_account_is_not_used_and_says_what_to_do(conductor, servers) -> None:
    built = servers(password="the-hosts-real-password")
    account_id = add_account(built, conductor)["account_id"]
    waiting = await EmailSearchTool().execute(_thread_id=conductor, query="x")
    assert "waiting for an app password" in waiting and built.imap.commands == []
    assert await run_check(account_id) == store.REFUSED
    refused = await EmailSearchTool().execute(_thread_id=conductor, query="x")
    assert "didn't accept its app password" in refused
    _no_leak(waiting, refused)


async def test_a_password_the_host_stops_accepting_turns_the_account_to_revoked(connected, monkeypatch) -> None:
    told: list[str] = []
    monkeypatch.setattr(mailhost_read, "notify_connected_apps_updated", lambda: told.append("updated"))
    await _set_through_keys(connected)
    connected.built.imap.password = "revoked-at-the-host"
    out = await EmailSearchTool().execute(_thread_id=connected.thread, query="x")
    assert out.startswith("Error: mail.test didn’t accept this app password")
    found = row(connected.account_id)
    assert found["check"]["status"] == store.REVOKED and found["state"] == "needs_sign_in" and found["capabilities"] == []
    assert told == ["updated"]
    _no_leak(out, found["raw"])
    assert "stopped accepting its app password" in await EmailSearchTool().execute(_thread_id=connected.thread, query="x")


async def test_refusal_does_not_revoke_a_key_replaced_after_credentials_were_read(connected) -> None:
    await _set_through_keys(connected)
    _endpoints, _username, _password, snapshot = mailhost_read.credentials(connected.account_id)
    replaced = await _put(connected.client, connected.key)
    assert replaced.status_code == 200, replaced.text

    def refused():
        raise MailHostError(PASSWORD_REFUSED, name="mail.test")

    with pytest.raises(MailHostError):
        await mailhost_read._guarded(connected.account_id, snapshot, refused)
    await check.wait_for_checks()
    _saved, current = store.read(connected.account_id, db=get_db())
    assert current != snapshot and current["status"] == store.OK


async def test_refusal_does_not_restore_servers_saved_after_credentials_were_read(connected, servers) -> None:
    await _set_through_keys(connected)
    _endpoints, _username, _password, snapshot = mailhost_read.credentials(connected.account_id)
    replacement = servers()
    saved = await connected.client.put(
        f"/api/mcp/accounts/{connected.account_id}/mailhost/servers", headers=_auth(),
        json={**_typed(replacement), "password": TEST_PASSWORD},
    )
    assert saved.status_code == 200, saved.text

    def refused():
        raise MailHostError(PASSWORD_REFUSED, name="mail.test")

    with pytest.raises(MailHostError):
        await mailhost_read._guarded(connected.account_id, snapshot, refused)
    await check.wait_for_checks()
    endpoints, current = store.read(connected.account_id, db=get_db())
    assert endpoints is not None and endpoints.imap.host == "127.0.0.1" and endpoints.imap.port == replacement.imap.port
    assert current != snapshot and current["status"] == store.OK


async def test_two_accounts_need_an_account_id_and_it_names_the_choices(conductor, servers) -> None:
    first = add_account(servers(), conductor)["account_id"]
    second = add_account(servers(), conductor, address="pat@other.test")["account_id"]
    await run_check(first)
    out = await EmailSearchTool().execute(_thread_id=conductor, query="x")
    assert "Pass account_id" in out and first in out and second in out
    named = json.loads(await EmailSearchTool().execute(_thread_id=conductor, query="x", account_id=ADDRESS.upper()))
    assert named["messages"]


async def test_with_no_email_account_the_resolver_names_every_connection_path(env, conductor) -> None:
    with pytest.raises(AccountError) as refusal:
        mail_accounts.resolve_for_thread(None, conductor)
    assert str(refusal.value) == mail_accounts.NO_EMAIL_ACCOUNT
    output = await EmailSearchTool().execute(_thread_id=conductor, query="x")
    assert "google-setup" in output and "microsoft_status" in output and "microsoft_mail_search" in output and "microsoft_mail_read" in output and "mail-host-setup" in output


async def test_mail_account_parameters_describe_every_supported_mail_account() -> None:
    expected = mail_accounts.MAIL_ACCOUNT_PARAM
    assert EmailSearchTool().parameters["properties"]["account_id"] == expected
    assert EmailReadTool().parameters["properties"]["account_id"] == expected
    assert CalendarListTool().parameters["properties"]["account_id"] == expected


async def test_mail_host_connect_description_uses_the_connected_apps_key_route() -> None:
    assert "Settings › Connected apps" in MailHostConnectTool().description
    assert "Settings › Keys" not in MailHostConnectTool().description


async def test_missing_mail_host_password_uses_the_connected_apps_key_route() -> None:
    assert mailhost_read.NEEDS_PASSWORD == (
        "This email account needs an app password. Ask the person to set it on its row in Connected apps."
    )
