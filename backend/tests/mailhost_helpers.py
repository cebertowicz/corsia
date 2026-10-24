"""Fixtures for the mail-host tests: a throwaway CA, fake IMAP/SMTP/CalDAV servers, a conductor thread.

``tls.context`` is replaced with a context that trusts only the test CA; the app's own context is
never loosened, and the tests that prove an untrusted certificate is refused put it back.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mailhost_fakes import SAMPLE_EVENTS, FakeCalDav, FakeImap, FakeSmtp, make_authority, message

from connections.mailhost import account, check, folders, store, tls
from infra import key_store
from infra.corsia_agent import set_corsia_agent
from infra.database import get_db
from tests.registry_helpers import make_session

ADDRESS = "sam@mail.test"
PASSWORD = "mailhost-canary-pw-0123456789"
BODY_CANARY = "canary-body-text-7731"
MESSAGES = {1: message("Lunch plans", "Noon on Friday?"), 2: message("Invoice 42", BODY_CANARY, sender="billing@example.com")}


@pytest.fixture
def authority(tmp_path_factory):
    return make_authority(tmp_path_factory.mktemp("mailhost-ca"))


@pytest.fixture
def env(isolated_db, tmp_path, monkeypatch, authority):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.setattr(tls, "context", authority.client_context)
    folders._cache.clear()
    return authority


@pytest.fixture
def conductor(env):
    db = get_db()
    thread_id = make_session(db, prefix="conductor")
    assert set_corsia_agent(thread_id, db=db)
    return thread_id


@pytest.fixture
def servers(env):
    made: list[SimpleNamespace] = []

    def build(*, imap=None, smtp=None, caldav=None, with_caldav=True, password=PASSWORD):
        login = {"user": ADDRESS, "password": password}
        built = SimpleNamespace(
            imap=FakeImap(env, messages=MESSAGES, **{**login, **(imap or {})}),
            smtp=FakeSmtp(env, **{**login, **(smtp or {})}),
            caldav=FakeCalDav(env, events=SAMPLE_EVENTS, **{**login, **(caldav or {})}) if with_caldav else None,
        )
        made.append(built)
        return built

    yield build
    for built in made:
        for fake in (built.imap, built.smtp, built.caldav):
            if fake is not None:
                fake.close()


def add_account(built, conductor_thread, *, address=ADDRESS, **extra) -> dict:
    """The agent's ``mail_host_connect`` against the fake servers (an address the table doesn't know)."""
    arguments = {
        "imap": f"127.0.0.1:{built.imap.port}", "smtp": f"127.0.0.1:{built.smtp.port}",
        "caldav": built.caldav.url if built.caldav else None, **extra,
    }
    clean = account.checked_address(address)
    db = get_db()
    try:
        return account.existing(clean, db=db) or account.create(
            clean, account.settings(clean, **arguments), thread_id=conductor_thread, db=db,
        )
    finally:
        db.close()


async def run_check(account_id: str, *, value: str = PASSWORD) -> str:
    """Set the app password the way the Keys write path does, then run its check to the end."""
    db = get_db()
    try:
        name = db.execute("SELECT credential_key FROM connected_accounts WHERE id=?", (account_id,)).fetchone()[0]
    finally:
        db.close()
    key_store.set_value(name, value)
    await check.start_after_key_write(name)
    await check.wait_for_checks()
    return row(account_id)["check"].get("status", "")


def row(account_id: str) -> dict:
    """The stored row: shared state, capabilities and the check object."""
    db = get_db()
    try:
        found = db.execute(
            "SELECT state, capabilities_json, endpoints_json FROM connected_accounts WHERE id=?", (account_id,),
        ).fetchone()
        stored = json.loads(found["endpoints_json"])
        return {
            "state": found["state"], "capabilities": json.loads(found["capabilities_json"]),
            "check": stored.get("check", {}), "endpoints": stored, "raw": found["endpoints_json"],
        }
    finally:
        db.close()


def count_rows() -> int:
    db = get_db()
    try:
        return int(db.execute("SELECT COUNT(*) FROM connected_accounts WHERE provider='mailhost'").fetchone()[0])
    finally:
        db.close()


__all__ = ["ADDRESS", "BODY_CANARY", "PASSWORD", "add_account", "authority", "conductor", "count_rows", "env",
           "row", "run_check", "servers", "store"]
