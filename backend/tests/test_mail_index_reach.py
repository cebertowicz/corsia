"""Account providers govern which account actions appear in the discovery index."""

import logging
import sqlite3

import pytest

from tools.registry import ToolRegistry
from tools.tool_surface import pack_index


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    path = tmp_path / "accounts.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE connected_accounts (provider TEXT)")
    monkeypatch.setattr("infra.database.get_db", lambda: sqlite3.connect(path))
    return path


def render():
    return pack_index(ToolRegistry())


def listed(index, name):
    return f"- {name}:" in index


@pytest.mark.parametrize(
    ("provider", "mail", "microsoft"),
    [(None, False, False), ("google", True, False), ("mailhost", True, False),
     ("microsoft", False, True)],
)
def test_provider_families(accounts, provider, mail, microsoft):
    if provider:
        with sqlite3.connect(accounts) as db:
            db.execute("INSERT INTO connected_accounts VALUES (?)", (provider,))
    index = render()
    for name in ("email_search", "email_send", "calendar_list", "calendar_send_invite"):
        assert listed(index, name) is mail
    for name in ("microsoft_mail_search", "microsoft_calendar_list", "microsoft_files_list"):
        assert listed(index, name) is microsoft
    for name in ("google_connect", "google_status", "microsoft_connect", "microsoft_status", "mail_host_connect"):
        assert listed(index, name)


@pytest.mark.parametrize("error", [OSError("unavailable"), sqlite3.OperationalError("unavailable")])
def test_store_error_fails_open_and_logs_type(monkeypatch, caplog, error):
    def fail():
        raise error
    monkeypatch.setattr("infra.database.get_db", fail)
    with caplog.at_level(logging.WARNING):
        index = render()
    assert listed(index, "email_search")
    assert listed(index, "calendar_send_invite")
    assert listed(index, "microsoft_files_list")
    assert type(error).__name__ in caplog.text


def test_account_add_remove_and_stability(accounts):
    before = render()
    assert before == render()
    with sqlite3.connect(accounts) as db:
        db.execute("INSERT INTO connected_accounts VALUES ('google')")
    after = render()
    assert after != before and after == render()
    with sqlite3.connect(accounts) as db:
        db.execute("DELETE FROM connected_accounts")
    assert render() == before


def test_index_reads_accounts_once(accounts, monkeypatch):
    connections = []

    def open_db():
        connections.append(1)
        return sqlite3.connect(accounts)

    monkeypatch.setattr("infra.database.get_db", open_db)
    render()
    assert len(connections) == 1
