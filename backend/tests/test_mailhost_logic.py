"""Mail hosts, the parts that need no server: settings, the known-host table, search translation,
message ids, message parsing and event expansion."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from mailhost_fakes import SAMPLE_EVENTS, message

from connections.mailhost import events, query, read
from connections.mailhost.endpoints import (
    BAD_ADDRESS, Endpoints, EndpointsRefused, Server, parse_address, parse_caldav, parse_server,
)
from connections.mailhost.known_hosts import KNOWN_HOSTS, known_host

TODAY = date(2026, 10, 21)


def test_known_hosts_cover_every_design_host_with_https_calendars_and_verifying_ports() -> None:
    names = {host.name for host in KNOWN_HOSTS}
    assert {"iCloud", "Yahoo", "Fastmail", "AOL", "Zoho", "GMX"} <= names
    for host in KNOWN_HOSTS:
        assert host.imap.port == 993 and host.smtp.port in (465, 587), host.name
        assert host.caldav is None or host.caldav.startswith("https://"), host.name
        assert host.app_password_url.startswith("https://") and host.click_path, host.name


def test_a_known_address_gets_the_tables_servers_and_icloud_tries_the_bare_name() -> None:
    host = known_host("Sam.Lee@iCloud.com")
    assert host is not None and host.name == "iCloud"
    found = host.endpoints("Sam.Lee@iCloud.com")
    assert (found.imap.host, found.smtp.host, found.known) == ("imap.mail.me.com", "smtp.mail.me.com", True)
    assert found.usernames(None) == ("Sam.Lee@iCloud.com", "Sam.Lee")
    assert known_host("pat@example.org") is None


def test_zoho_and_gmx_say_to_turn_imap_on_first() -> None:
    for address in ("a@zoho.eu", "b@gmx.net"):
        assert "IMAP" in (known_host(address).endpoints(address).enable_imap_step or "")


def test_settings_round_trip_and_reject_incomplete_json() -> None:
    original = Endpoints(
        name="example.test", known=False, username="sam", imap=Server("imap.example.test", 993),
        smtp=Server("smtp.example.test", None), caldav="https://cal.example.test/", calendars=("https://cal.example.test/a/",),
        imap_username="sam", caldav_username="sam",
    )
    assert Endpoints.from_dict(original.as_dict()) == original
    assert Endpoints.from_dict({"name": "x"}) is None
    assert Endpoints.from_dict("nope") is None
    assert original.smtp_ports() == (465, 587) and original.imap_port() == 993
    assert original.password_destinations() == ["cal.example.test", "imap.example.test", "smtp.example.test"]


@pytest.mark.parametrize("value", ["", "a b", "no-at-sign", "a@b c", '"q"@example.com', None, 5])
def test_bad_addresses_are_refused_with_one_fixed_line(value) -> None:
    with pytest.raises(EndpointsRefused) as raised:
        parse_address(value)
    assert str(raised.value) == BAD_ADDRESS


@pytest.mark.parametrize("value", ["", "https://mail.example.com", "mail.example.com/path", "mail..example.com", "mail.example.com:abc",
                                   "mail.example.com:0", "mail.example.com:70000", "-bad.example.com", "a b"])
def test_bad_servers_are_refused_without_echoing_them(value) -> None:
    with pytest.raises(EndpointsRefused) as raised:
        parse_server(value, field_name="imap")
    assert value.strip() == "" or value not in str(raised.value)


def test_servers_parse_with_and_without_a_port() -> None:
    assert parse_server("Mail.Example.com", field_name="imap") == Server("mail.example.com", None)
    assert parse_server("mail.example.com:143", field_name="imap") == Server("mail.example.com", 143)
    assert Server("m", 143).tls_starts_with_starttls(imap=True) and not Server("m", 993).tls_starts_with_starttls(imap=True)
    assert Server("m", 587).tls_starts_with_starttls(imap=False) and not Server("m", 465).tls_starts_with_starttls(imap=False)


@pytest.mark.parametrize("value", ["http://cal.example.com/", "https://user:pw@cal.example.com/", "cal.example.com", "https:///x", None])
def test_caldav_must_be_a_plain_https_address(value) -> None:
    with pytest.raises(EndpointsRefused):
        parse_caldav(value)
    assert parse_caldav("https://cal.example.com/dav/") == "https://cal.example.com/dav/"


def test_search_translates_gmail_style_terms_and_quotes_every_value() -> None:
    assert query.criteria("from:sam newer_than:7d", today=TODAY) == ["FROM", '"sam"', "SINCE", "14-Oct-2026"]
    assert query.criteria('"quarterly report" -subject:lunch is:unread', today=TODAY) == [
        "TEXT", '"quarterly report"', "NOT", "SUBJECT", '"lunch"', "UNSEEN"]
    assert query.criteria("after:2026/10/01 before:2026-10-15", today=TODAY) == [
        "SINCE", "01-Oct-2026", "BEFORE", "15-Oct-2026"]
    assert query.criteria("", today=TODAY) == ["ALL"]


def test_a_model_written_query_cannot_add_an_imap_command() -> None:
    found = query.criteria('x" ALL "DELETE', today=TODAY)
    assert all(part in ("TEXT",) or part.startswith('"') for part in found)
    assert '\\"' in found[1]


@pytest.mark.parametrize("text", ["a OR b", "has:attachment", "label:work", "tricky\r\nA1 LOGOUT", "is:important", "newer_than:soon",
                                  " ".join(f"w{i}" for i in range(13))])
def test_unsupported_searches_are_refused_with_the_list_of_what_works(text) -> None:
    with pytest.raises(query.QueryRefused) as raised:
        query.criteria(text, today=TODAY)
    assert str(raised.value) == query.SUPPORTED


def test_non_ascii_search_text_gets_its_own_line() -> None:
    with pytest.raises(query.QueryRefused) as raised:
        query.criteria("café", today=TODAY)
    assert str(raised.value) == query.NOT_ASCII


def test_message_ids_are_uidvalidity_dash_uid_and_nothing_else() -> None:
    assert read.parse_message_id("7-12") == (7, 12)
    assert read.check_message_id(" 007-0012 ") == "7-12"
    for bad in ("7", "7-12 ALL", "7-12;", "a-b", "", None, "-1-2", "1234567890123-1"):
        with pytest.raises(read.MessageNotFound) as raised:
            read.parse_message_id(bad)
        assert str(raised.value) == read.NOT_FOUND


def test_a_message_parses_to_headers_and_a_text_body_and_html_loses_its_tags() -> None:
    headers, body = read.parse(message("Hello  there", "plain body"), with_body=True)
    assert headers["subject"] == "Hello there" and headers["from"].endswith("<sam@example.com>") and body == "plain body"
    _, html_body = read.parse(message("h", "<p>Hi <b>there</b></p><script>alert(1)</script>", html=True), with_body=True)
    assert html_body == "Hi  there" or html_body.split() == ["Hi", "there"]
    assert read.parse(message("h", "body"), with_body=False)[1] == ""


def test_events_expand_repeats_inside_the_window_and_mark_all_day_and_free_time() -> None:
    window = (datetime(2026, 10, 22, tzinfo=timezone.utc), datetime(2026, 10, 28, tzinfo=timezone.utc))
    found = events.summaries([SAMPLE_EVENTS, "not a calendar"], window)
    assert [item["title"] for item in found] == ["Standup", "Holiday", "Weekly sync"]
    standup, holiday, weekly = found
    assert standup["attendees"] == ["sam@example.com"] and standup["free/busy"] == "busy"
    assert holiday["start"] == "2026-10-23" and holiday["free/busy"] == "free"
    assert weekly["id"].startswith("weekly-1@2026-10-26T18:00:00")
