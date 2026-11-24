"""Saved website logins: which sites and passwords the store takes, and what saving again does.

A login is saved for one https origin. Its password may be any non-empty text
without control characters, spaces and Unicode included, and is kept exactly
as given. One site and username is one login: saving it again replaces the
password and marks it ok. A view never carries the password.
"""

from __future__ import annotations

import dataclasses

import pytest

import infra.saved_logins as saved_logins
from infra import key_store
from infra.key_store_values import KeyStoreError

PASSWORD = "correct horse battery staple ü"
PERSON = "person-user-id"


@pytest.fixture
def state(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    return root


def _save(site="https://example.com", username="me@example.com", password=PASSWORD, saved_by=PERSON) -> str:
    return saved_logins.save_login(site=site, username=username, password=password, saved_by=saved_by)


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("https://example.com", "https://example.com"),
        ("https://Example.COM/", "https://example.com"),
        ("HTTPS://login.example.com:443", "https://login.example.com"),
        ("https://example.com:8443", "https://example.com:8443"),
        ("https://example.com:08443/", "https://example.com:8443"),
        ("https://10.0.0.1", "https://10.0.0.1"),
        ("https://[0:0:0:0:0:0:0:1]:9443", "https://[::1]:9443"),
        ("https://intranet", "https://intranet"),
    ],
)
def test_a_site_is_kept_as_its_https_origin(given, kept):
    assert saved_logins.normalized_site(given) == kept


@pytest.mark.parametrize(
    "given",
    [
        "http://example.com",
        "ftp://example.com",
        "example.com",
        "https://",
        "https://example.com/login",
        "https://example.com/?next=1",
        "https://example.com?",
        "https://example.com#top",
        "https://someone" + "@example.com",
        "https://example.com\n",
        " https://example.com",
        "https://example.com.",
        "https://exa mple.com",
        "https://-example.com",
        "https://example-.com",
        "https://example..com",
        "https://exämple.com",
        "https://999.1.1.1",
        "https://example.com:0",
        "https://example.com:65536",
        "https://[not-an-address]",
        "https://" + "a" * 64 + ".com",
        "https://" + ".".join(["abcdefghi"] * 30),
        "",
        None,
        42,
    ],
)
def test_anything_but_an_https_origin_is_refused(given):
    with pytest.raises(KeyStoreError) as refused:
        saved_logins.normalized_site(given)
    assert str(refused.value) == "A saved login's site is an https address, such as https://example.com."


def test_the_longest_host_name_is_kept():
    host = ".".join(["a" * 63] * 3 + ["b" * 61])
    assert len(host) == 253
    assert saved_logins.normalized_site(f"https://{host}:8443") == f"https://{host}:8443"
    with pytest.raises(KeyStoreError):
        saved_logins.normalized_site(f"https://{host}b")


@pytest.mark.parametrize(
    "password",
    [
        "with spaces in it",
        " leading and trailing ",
        " ",
        "ünïcödé-パスワード-🔑",
        "x",
        "p" * key_store.VALUE_MAX,
        "zero​width",
    ],
)
def test_any_non_empty_password_without_control_characters_is_kept_exactly(state, password):
    login_id = _save(password=password)
    assert saved_logins.password_for_fill(login_id) == password


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("", "Type the password first."),
        ("nul\x00inside", None),
        ("tab\tinside", None),
        ("new\nline", None),
        ("delete\x7f", None),
        ("c1\x85control", None),
        ("lone\ud800surrogate", None),
        ("p" * (key_store.VALUE_MAX + 1), None),
        (None, None),
        (b"bytes", None),
    ],
)
def test_an_empty_or_control_character_password_is_refused_and_nothing_is_saved(state, password, message):
    with pytest.raises(KeyStoreError) as refused:
        _save(password=password)
    expected = message or f"A password is 1-{key_store.VALUE_MAX} characters with no control characters. Nothing was saved."
    assert str(refused.value) == expected
    assert saved_logins.logins_for_site("https://example.com") == []


def test_the_api_key_rules_are_unchanged_for_other_entries(state):
    with pytest.raises(KeyStoreError):
        key_store.validated_value("with spaces")
    with pytest.raises(KeyStoreError):
        key_store.validated_value("ünïcödé")
    assert key_store.validated_value("  plain-key  ") == "plain-key"


@pytest.mark.parametrize("username", ["", "a\x00b", "a\nb", "u" * (saved_logins.USERNAME_MAX + 1), None])
def test_a_bad_username_is_refused(state, username):
    with pytest.raises(KeyStoreError) as refused:
        _save(username=username)
    assert str(refused.value) == f"A username is 1-{saved_logins.USERNAME_MAX} characters with no control characters."


def test_a_username_is_kept_as_typed(state):
    login_id = _save(username=" Me@Example.com ")
    assert saved_logins.get_login(login_id).username == " Me@Example.com "


def test_saving_needs_the_person_who_saved_it(state):
    with pytest.raises(KeyStoreError, match="needs the person who saved it"):
        _save(saved_by="")


def test_a_new_login_is_ok_unused_and_saved_by_the_person(state):
    login = saved_logins.get_login(_save(site="https://Example.com/"))
    assert login.site == "https://example.com"
    assert login.username == "me@example.com"
    assert login.status == saved_logins.STATUS_OK
    assert login.saved_by == PERSON
    assert login.created_at
    assert (login.last_used_at, login.last_used_by_thread_id) == (None, None)


def test_saving_the_same_site_and_username_again_replaces_the_password_and_marks_it_ok(state):
    first = _save()
    saved_logins.mark_out_of_date(first)
    created = saved_logins.get_login(first).created_at
    again = _save(site="https://EXAMPLE.com:443/", password="the new one", saved_by="someone-else")
    assert again == first
    login = saved_logins.get_login(first)
    assert (login.status, login.saved_by, login.created_at) == (saved_logins.STATUS_OK, "someone-else", created)
    assert saved_logins.password_for_fill(first) == "the new one"
    assert len(saved_logins.logins_for_site("https://example.com")) == 1


def test_another_username_on_the_same_site_is_another_login(state):
    first = _save(username="me@example.com")
    second = _save(username="Me@example.com")
    assert first != second
    assert [login.username for login in saved_logins.logins_for_site("https://example.com")] == [
        "Me@example.com", "me@example.com",
    ]
    assert saved_logins.logins_for_site("https://example.com:8443") == []


def test_mark_out_of_date_and_record_use(state):
    login_id = _save()
    saved_logins.mark_out_of_date(login_id)
    saved_logins.record_use(login_id, "thread-1")
    login = saved_logins.get_login(login_id)
    assert login.status == saved_logins.STATUS_OUT_OF_DATE
    assert login.last_used_by_thread_id == "thread-1"
    assert login.last_used_at
    with pytest.raises(KeyStoreError, match="needs the thread"):
        saved_logins.record_use(login_id, "")


def test_delete_removes_the_login_and_its_entry(state):
    login_id = _save()
    saved_logins.delete_login(login_id)
    assert saved_logins.logins_for_site("https://example.com") == []
    for action in (
        saved_logins.get_login,
        saved_logins.password_for_fill,
        saved_logins.mark_out_of_date,
        saved_logins.delete_login,
    ):
        with pytest.raises(KeyStoreError) as refused:
            action(login_id)
        assert str(refused.value) == "No saved login has that id."
    from infra.database import get_db

    assert get_db().execute(
        "SELECT 1 FROM key_store_entries WHERE name = ?", (saved_logins.entry_name(login_id),),
    ).fetchone() is None


@pytest.mark.parametrize("login_id", ["", "not-an-id", "A" * 32, "0" * 31, "0" * 32 + "\n", None])
def test_an_unknown_or_malformed_id_is_refused(state, login_id):
    with pytest.raises(KeyStoreError) as refused:
        saved_logins.get_login(login_id)
    assert str(refused.value) == "No saved login has that id."


def test_blanking_values_holds_every_saved_password_of_eight_or_more_characters(state):
    _save(username="a", password="12345678")
    _save(username="b", password="1234567")
    stale = _save(username="c", password="out of date but still blanked")
    saved_logins.mark_out_of_date(stale)
    assert saved_logins.blanking_values() == frozenset({"12345678", "out of date but still blanked"})


def test_a_view_has_no_password_field(state):
    fields = {field.name for field in dataclasses.fields(saved_logins.SavedLogin)}
    assert fields == {
        "id", "site", "username", "status", "created_at", "saved_by", "last_used_at", "last_used_by_thread_id",
    }
    assert not any("pass" in name or "value" in name or "secret" in name for name in fields)
