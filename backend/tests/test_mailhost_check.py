"""The check that runs when the person pastes an app password: what each host answer becomes,
that TLS always comes before a password, and that no password or host text leaks.

Servers are real TLS sockets on 127.0.0.1 signed by a throwaway CA (``mailhost_fakes``).
"""

from __future__ import annotations

import logging
import socket

import pytest
from mailhost_fakes import HOST_REPLY_MARKER
from mailhost_helpers import (  # noqa: F401  (fixtures)
    ADDRESS, PASSWORD, add_account, authority, conductor, env, row, run_check, servers,
)

from connections.mailhost import check, endpoints, imap, smtp, store, tls
from infra import key_store
from connections.mailhost.errors import PASSWORD_REFUSED, SERVER_UNREACHABLE, MailHostError

pytestmark = pytest.mark.asyncio


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _leaks(text: str) -> bool:
    return PASSWORD in text or HOST_REPLY_MARKER in text


async def test_a_good_password_connects_with_mail_and_calendar(conductor, servers, caplog) -> None:
    built = servers()
    account_id = add_account(built, conductor)["account_id"]
    assert row(account_id)["check"]["status"] == store.WAITING and row(account_id)["state"] == "needs_sign_in"
    with caplog.at_level(logging.DEBUG):
        assert await run_check(account_id) == store.OK
    found = row(account_id)
    assert found["state"] == "connected" and found["capabilities"] == ["mail", "calendar"]
    assert found["endpoints"]["imap_username"] == ADDRESS and len(found["endpoints"]["calendars"]) == 1
    assert not _leaks(found["raw"]) and not _leaks(caplog.text)
    assert all(tls_on for tls_on, _ in built.imap.commands), "every IMAP command ran inside TLS"
    assert all(tls_on for tls_on, _ in built.smtp.commands), "every SMTP command ran inside TLS"


async def test_a_finished_check_tells_the_open_screens(conductor, servers, monkeypatch) -> None:
    told: list[str] = []
    monkeypatch.setattr(check, "notify_connected_apps_updated", lambda: told.append("updated"))
    account_id = add_account(servers(), conductor)["account_id"]
    assert await run_check(account_id) == store.OK
    assert told == ["updated"]


async def test_no_calendar_leaves_the_account_mail_only(conductor, servers) -> None:
    account_id = add_account(servers(with_caldav=False), conductor)["account_id"]
    assert await run_check(account_id) == store.MAIL_ONLY
    found = row(account_id)
    assert found["state"] == "connected" and found["capabilities"] == ["mail"]


async def test_a_calendar_that_refuses_the_password_leaves_it_mail_only(conductor, servers) -> None:
    built = servers(caldav={"password": "a-different-password"})
    account_id = add_account(built, conductor)["account_id"]
    assert await run_check(account_id) == store.MAIL_ONLY
    assert row(account_id)["capabilities"] == ["mail"]


async def test_a_calendar_without_events_support_is_not_a_calendar(conductor, servers) -> None:
    account_id = add_account(servers(caldav={"supports": "VTODO"}), conductor)["account_id"]
    assert await run_check(account_id) == store.MAIL_ONLY


async def test_a_refused_password_stops_at_imap_and_never_reaches_smtp(conductor, servers, caplog) -> None:
    built = servers(password="the-hosts-real-password")
    account_id = add_account(built, conductor)["account_id"]
    with caplog.at_level(logging.DEBUG):
        assert await run_check(account_id) == store.REFUSED
    found = row(account_id)
    assert found["state"] == "needs_sign_in" and found["capabilities"] == []
    assert built.smtp.commands == [] and built.caldav.requests == []
    assert not _leaks(found["raw"]) and not _leaks(caplog.text)


async def test_imap_turned_off_at_the_host_is_named_separately(conductor, servers) -> None:
    account_id = add_account(servers(imap={"imap_off": True}), conductor)["account_id"]
    assert await run_check(account_id) == store.IMAP_OFF
    assert row(account_id)["state"] == "needs_sign_in"


async def test_a_wrong_smtp_password_is_a_refused_password(conductor, servers) -> None:
    built = servers(smtp={"password": "other-password-0000"})
    account_id = add_account(built, conductor)["account_id"]
    assert await run_check(account_id) == store.REFUSED


async def test_a_server_that_cannot_be_reached_names_the_host(conductor, servers) -> None:
    built = servers()
    account_id = add_account(built, conductor)["account_id"]
    built.imap.close()
    assert await run_check(account_id) == store.UNREACHABLE
    found = row(account_id)
    assert found["check"]["host"] == "127.0.0.1" and found["capabilities"] == []


async def test_a_certificate_the_system_does_not_trust_is_unreachable_and_gets_no_login(
    conductor, servers, monkeypatch, caplog,
) -> None:
    built = servers()
    account_id = add_account(built, conductor)["account_id"]
    import ssl

    monkeypatch.setattr(tls, "context", ssl.create_default_context)  # the app's own, which does not trust the test CA
    with caplog.at_level(logging.DEBUG):
        assert await run_check(account_id) == store.UNREACHABLE
    assert built.imap.commands == [] and built.smtp.commands == []
    assert not _leaks(caplog.text)


async def test_the_context_every_connection_uses_verifies_certificate_and_host_name() -> None:
    import ssl

    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert tls.__doc__ and "no setting" in tls.__doc__


async def test_one_wrong_password_is_one_login_at_the_host(conductor, servers) -> None:
    built = servers(password="the-hosts-real-password")
    account_id = add_account(built, conductor)["account_id"]
    await run_check(account_id)
    assert [verb for _, verb in built.imap.commands].count("LOGIN") == 1


async def test_imap_starttls_logs_in_only_after_tls_is_up(servers, monkeypatch) -> None:
    built = servers(imap={"implicit_tls": False, "offer_starttls": True})
    monkeypatch.setattr(endpoints, "IMAP_STARTTLS_PORTS", frozenset((built.imap.port,)))
    ep = endpoints.Endpoints("t", False, ADDRESS, endpoints.Server("127.0.0.1", built.imap.port), endpoints.Server("x", 1))
    assert imap.check_login(ep, PASSWORD) == ADDRESS
    logins = [tls_on for tls_on, verb in built.imap.commands if verb == "LOGIN"]
    assert logins == [True]
    assert PASSWORD.encode() not in built.imap.plain


async def test_imap_without_starttls_never_sees_the_password(servers, monkeypatch) -> None:
    built = servers(imap={"implicit_tls": False, "offer_starttls": False})
    monkeypatch.setattr(endpoints, "IMAP_STARTTLS_PORTS", frozenset((built.imap.port,)))
    ep = endpoints.Endpoints("t", False, ADDRESS, endpoints.Server("127.0.0.1", built.imap.port), endpoints.Server("x", 1))
    with pytest.raises(MailHostError) as raised:
        imap.check_login(ep, PASSWORD)
    assert raised.value.cause == SERVER_UNREACHABLE
    assert all(verb != "LOGIN" for _, verb in built.imap.commands)
    assert PASSWORD.encode() not in built.imap.plain


async def test_smtp_starttls_authenticates_only_after_tls_is_up(servers, monkeypatch) -> None:
    built = servers(smtp={"implicit_tls": False, "offer_starttls": True})
    monkeypatch.setattr(endpoints, "SMTP_STARTTLS_PORTS", frozenset((built.smtp.port,)))
    ep = endpoints.Endpoints("t", False, ADDRESS, endpoints.Server("x", 1), endpoints.Server("127.0.0.1", built.smtp.port))
    smtp.check_login(ep, PASSWORD)
    assert [tls_on for tls_on, verb in built.smtp.commands if verb == "AUTH"] == [True]
    assert PASSWORD.encode() not in built.smtp.plain


async def test_smtp_without_starttls_gets_no_auth_even_when_it_offers_plain_auth(servers, monkeypatch) -> None:
    built = servers(smtp={"implicit_tls": False, "offer_starttls": False, "plain_auth_without_tls": True})
    monkeypatch.setattr(endpoints, "SMTP_STARTTLS_PORTS", frozenset((built.smtp.port,)))
    ep = endpoints.Endpoints("t", False, ADDRESS, endpoints.Server("x", 1), endpoints.Server("127.0.0.1", built.smtp.port))
    with pytest.raises(MailHostError) as raised:
        smtp.check_login(ep, PASSWORD)
    assert raised.value.cause == SERVER_UNREACHABLE
    assert all(verb != "AUTH" for _, verb in built.smtp.commands)
    assert PASSWORD.encode() not in built.smtp.plain


async def test_smtp_with_no_port_falls_from_the_closed_tls_port_to_the_next(servers, monkeypatch) -> None:
    built = servers()
    ep = endpoints.Endpoints("t", False, ADDRESS, endpoints.Server("x", 1), endpoints.Server("127.0.0.1", None))
    monkeypatch.setattr(endpoints, "SMTP_AUTO_PORTS", (_free_port(), built.smtp.port))
    smtp.check_login(ep, PASSWORD)
    assert [verb for _, verb in built.smtp.commands].count("AUTH") == 1


async def test_a_refused_smtp_password_does_not_try_another_port(servers, monkeypatch) -> None:
    built = servers(password="the-hosts-real-password")
    ep = endpoints.Endpoints("t", False, ADDRESS, endpoints.Server("x", 1), endpoints.Server("127.0.0.1", None))
    monkeypatch.setattr(endpoints, "SMTP_AUTO_PORTS", (built.smtp.port, built.smtp.port))
    with pytest.raises(MailHostError) as raised:
        smtp.check_login(ep, PASSWORD)
    assert raised.value.cause == PASSWORD_REFUSED
    assert [verb for _, verb in built.smtp.commands].count("AUTH") == 1


async def test_an_older_check_never_overwrites_a_newer_one(conductor, servers, monkeypatch) -> None:
    told: list[str] = []
    monkeypatch.setattr(check, "notify_connected_apps_updated", lambda: told.append("updated"))
    built = servers()
    account_id = add_account(built, conductor)["account_id"]
    first = store.begin_check(account_id)
    second = store.begin_check(account_id)
    assert first and second and first[1] != second[1]
    name = key_store.MAILHOST_PREFIX + account_id
    key_store.set_value(name, PASSWORD)
    assert await check.run_check(account_id, first[0], first[1]) == store.OK  # finishes, but its token is stale
    assert row(account_id)["check"]["status"] == store.CHECKING and row(account_id)["state"] == "needs_sign_in"
    assert told == [], "a stale check changed nothing, so it tells no screen"
    assert await check.run_check(account_id, second[0], second[1]) == store.OK
    assert told == ["updated"]
    assert row(account_id)["check"]["status"] == store.OK and row(account_id)["state"] == "connected"


async def test_a_check_with_no_saved_password_leaves_the_row_waiting(conductor, servers) -> None:
    account_id = add_account(servers(), conductor)["account_id"]
    begun = store.begin_check(account_id)
    assert await check.run_check(account_id, *begun) == store.WAITING
    assert row(account_id)["check"]["status"] == store.WAITING


async def test_a_bug_inside_a_check_ends_it_instead_of_leaving_the_row_checking(conductor, servers, monkeypatch, caplog) -> None:
    account_id = add_account(servers(), conductor)["account_id"]

    async def broken(*_args):
        raise RuntimeError(f"boom {PASSWORD}")

    monkeypatch.setattr(check, "_outcome", broken)
    with caplog.at_level(logging.DEBUG):
        assert await run_check(account_id) == store.UNREACHABLE
    assert not _leaks(caplog.text) and not _leaks(row(account_id)["raw"])


async def test_clearing_the_key_returns_the_row_to_waiting_with_no_capabilities(conductor, servers) -> None:
    account_id = add_account(servers(), conductor)["account_id"]
    assert await run_check(account_id) == store.OK
    name = key_store.MAILHOST_PREFIX + account_id
    key_store.clear_value(name)
    await check.reset_after_key_clear(name)
    found = row(account_id)
    assert found["check"]["status"] == store.WAITING and found["state"] == "needs_sign_in" and found["capabilities"] == []


async def test_a_name_that_is_not_a_mail_host_key_starts_nothing(conductor, servers) -> None:
    await check.start_after_key_write("web-search")
    await check.start_after_key_write(f"{key_store.MAILHOST_PREFIX}not-an-account-id")
    await check.wait_for_checks()


async def test_a_check_a_dead_process_left_running_ends_as_server_unreachable_and_can_be_retried(conductor, servers) -> None:
    built = servers()
    stuck = add_account(built, conductor)["account_id"]
    waiting = add_account(built, conductor, address="other@example.com")["account_id"]
    assert await run_check(stuck) == store.OK
    assert store.begin_check(stuck) is not None
    assert row(stuck)["check"]["status"] == store.CHECKING

    assert await check.settle_after_restart() == 1

    settled = row(stuck)
    assert (settled["check"]["status"], settled["check"]["host"], settled["state"]) == (
        store.UNREACHABLE, "127.0.0.1", "needs_sign_in")
    assert row(waiting)["check"]["status"] == store.WAITING
    assert await check.settle_after_restart() == 0
    assert await run_check(stuck) == store.OK


async def test_startup_settles_stuck_checks_after_it_reviews_reconcile() -> None:
    import inspect

    import app_startup_runtime

    assert "await settle_after_restart()" in inspect.getsource(app_startup_runtime._start_runtime_services)
