"""Saved items: which Settings › Keys entries an agent may ask to have filled, and their approvals.

An item is approved through the saved-login approvals (infra.saved_login_fills): the same store, decisions,
durations and ends. These check what is the item's own: which entries are items, the site its request
carries, that clearing or removing its entry ends its grant, its audit row, and that its value joins the
blanking set and never appears in a view, an event or a log.
"""
from __future__ import annotations

import logging
import uuid

import pytest

import infra.saved_login_fills as fills
from auth_fixtures import OWNER_USER as PERSON
import infra.saved_items as saved_items  # mypy resolves a new submodule only this way
from infra import key_store, key_store_values, saved_item_fills, saved_login_results, saved_logins
from infra.database import get_db
from infra.saved_fill_identity import FillApprovalError, FillDurationError

THREAD = "item-thread"
OTHER_THREAD = "other-item-thread"
SITE = "https://portal.example.org"


@pytest.fixture
def world(isolated_db, tmp_path, monkeypatch, caplog):
    """Two agent threads, an added entry with a value, and no approvals in memory."""
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.setattr(fills, "_approvals", {})
    from infra import product_events
    from infra.state import ws

    seen: list[str] = []
    monkeypatch.setattr(ws, "broadcast_to_user", lambda *args, **kwargs: seen.append(repr(args)))
    monkeypatch.setattr(product_events, "emit_product_event", lambda *a, **k: seen.append(repr((a, k))))
    db = get_db()
    db.executemany(
        "INSERT INTO threads (id,user_id,title,spawned_by,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        [(THREAD, PERSON, "Bookkeeper", None, "t", "t"), (OTHER_THREAD, PERSON, "Researcher", None, "t", "t")],
    )
    db.commit()
    db.close()
    caplog.set_level(logging.DEBUG)
    sentinel = f"Item-sentinel-{uuid.uuid4().hex}"
    key_store.add_entry("member-number", "Member number", "", added_by_thread_id=None, added_by_name=None)
    key_store.set_value("member-number", sentinel)
    return {"sentinel": sentinel, "seen": seen}


def _approve(row, seconds=600):
    return fills.approve_fill(row["id"], user_id=PERSON, revision=row["revision"],
                              application_hash=row["application_hash"], duration_seconds=seconds)


def _never_shown(world, caplog, *texts: object) -> None:
    held = [caplog.text, *world["seen"], *(repr(text) for text in texts), repr(fills._approvals)]
    assert not any(world["sentinel"] in text for text in held)


def test_items_are_the_entries_a_person_can_set_and_never_carry_a_value(world, caplog):
    key_store.ensure_managed_entry(f"{key_store.SERVER_PREFIX}box", "A server")
    key_store.set_managed_value(f"{key_store.SERVER_PREFIX}box", "server-value-0001")
    key_store.ensure_managed_entry(key_store.GOOGLE_CLIENT_ID, "Google")
    login = saved_logins.save_login(site="https://example.com", username="me", password="pw-0000001", saved_by=PERSON)
    items = {item.name: item for item in saved_items.list_items()}
    assert {"web-search", "github-webhook", "member-number"} <= set(items)
    assert items["member-number"].label == "Member number" and items["member-number"].is_set
    assert items["github-webhook"].label == "GitHub webhook secret" and not items["github-webhook"].is_set
    assert any(name.startswith(key_store.PROVIDER_PREFIX) for name in items)
    assert f"{key_store.PROVIDER_PREFIX}omlx" not in items
    refused = (f"{key_store.SERVER_PREFIX}box", key_store.GOOGLE_CLIENT_ID, saved_logins.entry_name(login), "nope", 3)
    for name in refused:
        assert name not in items
        with pytest.raises(key_store_values.KeyStoreError):
            saved_items.get_item(name)
        with pytest.raises(FillApprovalError):
            saved_item_fills.request_item_fill(THREAD, name, SITE)
    _never_shown(world, caplog, list(items.values()))


def test_an_item_value_joins_the_blanking_set(world):
    assert world["sentinel"] in saved_logins.blanking_values()
    assert world["sentinel"] in saved_items.item_blanking_values()


def test_a_request_carries_the_field_site_and_the_item_label_never_a_value(world, caplog):
    row = saved_item_fills.request_item_fill(THREAD, "member-number", SITE + "/", requested_seconds=900)
    assert row["kind"] == "fill" and row["state"] == "awaiting_user"
    assert row["site"] == SITE and row["item_label"] == "Member number" and row["username"] == ""
    assert row["requested_duration_seconds"] == 900 and row["agent"] == "Bookkeeper"
    again = saved_item_fills.request_item_fill(THREAD, "member-number", "https://elsewhere.example")
    assert again["id"] == row["id"] and again["site"] == SITE  # one per thread and item, as it is
    assert saved_item_fills.granted_site(THREAD, "member-number") is None
    granted = _approve(row)
    assert granted["state"] == "active"
    assert saved_item_fills.granted_site(THREAD, "member-number") == SITE
    assert saved_item_fills.granted_site(OTHER_THREAD, "member-number") is None
    assert saved_item_fills.granted_site(THREAD, "web-search") is None
    _never_shown(world, caplog, row, granted, fills.listed(PERSON))


def test_an_item_request_is_refused_off_https_unset_or_for_ever(world):
    with pytest.raises(FillApprovalError):
        saved_item_fills.request_item_fill(THREAD, "member-number", "http://portal.example.org")
    with pytest.raises(FillApprovalError) as unset:
        saved_item_fills.request_item_fill(THREAD, "github-webhook", SITE)
    assert "Settings › Keys" in str(unset.value)
    for asked in ("until_revoked", 0, "600", 10**9, True):
        with pytest.raises(FillDurationError):
            saved_item_fills.request_item_fill(THREAD, "member-number", SITE, requested_seconds=asked)
    assert fills.listed(PERSON) == []


def test_only_the_person_makes_an_item_grant_standing(world):
    row = saved_item_fills.request_item_fill(THREAD, "member-number", SITE)
    standing = fills.approve_fill(row["id"], user_id=PERSON, revision=row["revision"],
                                  application_hash=row["application_hash"], until_revoked=True)
    assert standing["standing"] is True
    assert saved_item_fills.granted_site(THREAD, "member-number") == SITE


@pytest.mark.parametrize("end", ["clear", "remove"])
def test_clearing_or_removing_the_entry_ends_its_grant(world, end):
    _approve(saved_item_fills.request_item_fill(THREAD, "member-number", SITE))
    _approve(saved_item_fills.request_item_fill(OTHER_THREAD, "member-number", SITE))
    if end == "clear":
        key_store.clear_value("member-number")
    else:
        key_store.remove_entry("member-number")
    assert fills._approvals == {}
    if end == "clear":
        key_store.set_value("member-number", world["sentinel"])  # setting it again revives nothing
    assert saved_item_fills.granted_site(THREAD, "member-number") is None


def test_a_grant_never_outlives_its_value_even_unannounced(world, monkeypatch):
    _approve(saved_item_fills.request_item_fill(THREAD, "member-number", SITE))
    monkeypatch.setattr(key_store, "_end_fills", lambda _name: None)
    key_store.clear_value("member-number")
    assert saved_item_fills.granted_site(THREAD, "member-number") is None
    assert fills._approvals == {}


def test_ending_an_item_leaves_a_login_grant_alone(world):
    login = saved_logins.save_login(site="https://example.com", username="me", password="pw-0000001", saved_by=PERSON)
    _approve(fills.request_fill(THREAD, login))
    _approve(saved_item_fills.request_item_fill(THREAD, "member-number", SITE))
    key_store.clear_value("member-number")
    assert fills.may_fill(THREAD, login) is True
    fills.end_for_login(login, deleted=True)
    assert fills.may_fill(THREAD, login) is False


def test_an_item_fill_writes_one_audit_row_with_its_name_and_never_its_value(world):
    saved_login_results.item_fill_recorded(THREAD, "member-number", SITE, "filled")
    db = get_db()
    try:
        rows = [dict(row) for row in db.execute("SELECT * FROM saved_login_fill_audit").fetchall()]
    finally:
        db.close()
    assert len(rows) == 1
    assert rows[0]["item"] == "member-number" and rows[0]["login_id"] == "" and rows[0]["site"] == SITE
    assert rows[0]["thread_id"] == THREAD and rows[0]["result"] == "filled" and rows[0]["created_at"]
    assert world["sentinel"] not in repr(rows)


def test_a_value_in_an_error_never_reaches_the_log(world, caplog, monkeypatch):
    """A failure while reading for a fill carries a secret in its message; only its type is logged."""
    from host_access import browser_item_fill

    def boom(*_a, **_k):
        raise RuntimeError(world["sentinel"])

    monkeypatch.setattr(browser_item_fill.saved_login_results, "item_fill_recorded", boom)
    import asyncio

    asyncio.run(browser_item_fill._record(THREAD, "member-number", SITE, "filled"))
    assert "RuntimeError" in caplog.text
    _never_shown(world, caplog)


def test_a_legacy_provider_is_an_item_only_while_it_holds_a_key_as_settings_shows_it(world):
    from infra import key_store_providers

    names = {item.name for item in saved_items.list_items()}
    assert "provider-openai" in names and "provider-groq" not in names
    key_store_providers.set_provider_key("groq", "groq-key-000001")
    assert "provider-groq" in {item.name for item in saved_items.list_items() if item.is_set}
