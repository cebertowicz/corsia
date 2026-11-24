"""Saved-login fill results persist atomically without exposing the password."""

import logging
import sqlite3

import pytest
from loguru import logger

from infra import saved_login_results, saved_logins
from infra.database import get_db
from infra.key_store_values import KeyStoreError


@pytest.fixture
def login(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    return saved_logins.save_login(
        site="https://example.com", username="alice", password="sentinel-unique-for-test",  # gitleaks:allow
        saved_by="person",
    )


def test_agent_facing_wording():
    assert saved_login_results.FILLED == "Signed in to {site} as {username} with the saved login."
    assert saved_login_results.REJECTED == (
        "The saved login for {site} ({username}) didn't work, so it's now marked out of date. "
        "The person has a step to sign in to {site} themselves."
    )


def rows():
    return [tuple(row) for row in get_db().execute(
        "SELECT created_at, login_id, site, username, thread_id, result FROM saved_login_fill_audit"
    )]


@pytest.mark.parametrize("succeeded", [True, False])
def test_result_is_recorded_without_password_and_survives_deletion(login, caplog, succeeded):
    logs = []
    sink = logger.add(logs.append)
    try:
        with caplog.at_level(logging.DEBUG):
            result = (saved_login_results.fill_succeeded if succeeded else saved_login_results.fill_failed)("agent", login)
        login_view = saved_logins.get_login(login)
        audit = rows()
        assert len(audit) == 1
        assert audit[0][0]
        assert audit[0][1:] == (login, "https://example.com", "alice", "agent", "filled" if succeeded else "rejected")
        assert result == (saved_login_results.FILLED if succeeded else saved_login_results.REJECTED).format(
            site="https://example.com", username="alice"
        )
        if succeeded:
            assert login_view.last_used_at and login_view.last_used_by_thread_id == "agent"
        else:
            assert login_view.status == saved_logins.STATUS_OUT_OF_DATE
        saved_logins.delete_login(login)
        assert rows() == audit
        assert "sentinel-unique-for-test" not in repr((audit, result, logs, caplog.text))  # gitleaks:allow
    finally:
        logger.remove(sink)


@pytest.mark.parametrize("operation", [saved_login_results.fill_succeeded, saved_login_results.fill_failed])
def test_unknown_login_and_invalid_thread_write_nothing(login, operation):
    with pytest.raises(KeyStoreError):
        operation("agent", "0" * 32)
    with pytest.raises(KeyStoreError):
        operation("", login)
    assert rows() == []
    assert saved_logins.get_login(login).status == saved_logins.STATUS_OK


@pytest.mark.parametrize("operation", [saved_login_results.fill_succeeded, saved_login_results.fill_failed])
def test_audit_failure_rolls_back_login_update(login, operation):
    db = get_db()
    db.execute("CREATE TRIGGER fail_fill BEFORE INSERT ON saved_login_fill_audit BEGIN SELECT RAISE(ABORT, 'failed'); END")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="failed"):
        operation("agent", login)
    view = saved_logins.get_login(login)
    assert view.status == saved_logins.STATUS_OK
    assert view.last_used_at is None
    assert rows() == []
