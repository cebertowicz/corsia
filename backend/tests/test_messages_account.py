"""The keyless Messages account row: reach, sign-out and remove work with no key-store entry."""

from __future__ import annotations

import asyncio

from api.connected_account_rows import account_row
from connections.account_actions import remove_account, sign_out_account
from connections.connected_accounts import (
    MESSAGES_PROVIDER, account_reaches_thread, create_account, create_account_for_thread, get_account,
    resolve_credentials, visible_accounts,
)
from infra.database import get_db
from infra.time import utc_now_iso
from tests.registry_helpers import make_session

ALL = {"mode": "all", "project_ids": [], "outside_projects": False}


def _key_rows(db) -> int:
    return db.execute("SELECT COUNT(*) FROM key_store_entries WHERE name LIKE '%connected%'").fetchone()[0]


def test_keyless_row_is_connected_and_writes_no_key_entry(isolated_db) -> None:
    db = get_db()
    try:
        before = db.execute("SELECT COUNT(*) FROM key_store_entries").fetchone()[0]
        account = create_account(
            MESSAGES_PROVIDER, "This Mac", reach=ALL, created_by=("person", "steve"), db=db,
        )
        assert account["state"] == "connected"
        assert resolve_credentials(str(account["id"]), db=db) == {}
        assert account_row(account, db=db)["state"] == "connected"
        assert db.execute("SELECT COUNT(*) FROM key_store_entries").fetchone()[0] == before
    finally:
        db.close()


def test_where_it_works_reaches_only_chosen_projects(isolated_db) -> None:
    db = get_db()
    try:
        now = utc_now_iso()
        db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)", ("project-a", "A", now, now))
        db.commit()
        inside = make_session(db, project_id="project-a", prefix="in")
        outside = make_session(db, prefix="out")
        account = create_account_for_thread(
            MESSAGES_PROVIDER, "This Mac", actor_thread_id=inside, created_by=("agent", inside), db=db,
        )
        assert account_reaches_thread(str(account["id"]), inside, db=db)
        assert not account_reaches_thread(str(account["id"]), outside, db=db)
        assert [row["id"] for row in visible_accounts(inside, db=db, provider=MESSAGES_PROVIDER)] == [account["id"]]
        assert visible_accounts(outside, db=db, provider=MESSAGES_PROVIDER) == []
    finally:
        db.close()


def test_sign_out_and_remove_work_without_a_key_entry(isolated_db) -> None:
    db = get_db()
    try:
        account = create_account(MESSAGES_PROVIDER, "This Mac", reach=ALL, created_by=("person", "steve"), db=db)
        db.commit()
        before = db.execute("SELECT COUNT(*) FROM key_store_entries").fetchone()[0]
        account_id = str(account["id"])
        result = asyncio.run(sign_out_account(account_id, actor=("person", "steve")))
        assert result["state"] == "signed_out" and result["revocation"] == "manual_only"
        assert get_account(account_id, db=db)["state"] == "signed_out"
        assert asyncio.run(remove_account(account_id, actor=("person", "steve")))["removed"] is True
        assert db.execute("SELECT 1 FROM connected_accounts WHERE id=?", (account_id,)).fetchone() is None
        assert db.execute("SELECT COUNT(*) FROM key_store_entries").fetchone()[0] == before
    finally:
        db.close()


async def test_connect_adds_one_row_at_the_adding_scope_and_never_a_second(isolated_db) -> None:
    from connections.messages import account

    db = get_db()
    try:
        thread = make_session(db, prefix="corsia")
        first, created = await account.connect(thread)
        again, created_again = await account.connect(thread)
        assert created and not created_again and again["id"] == first["id"]
        assert len(visible_accounts(None, db=db, provider=MESSAGES_PROVIDER)) == 1
        assert (await account.usable_for_thread(thread))["id"] == first["id"]
        assert await account.usable_for_thread(None) is None
    finally:
        db.close()


async def test_only_the_corsia_thread_can_call_messages_connect(isolated_db, monkeypatch) -> None:
    import tools.messages_texts as messages_texts
    from tools import messages_db
    from tools.messages_tools import MessagesConnectTool

    monkeypatch.setattr(messages_db, "is_mac", lambda: True)
    db = get_db()
    try:
        thread = make_session(db, prefix="plain")
    finally:
        db.close()
    assert await MessagesConnectTool().execute(_thread_id=thread) == messages_texts.ONLY_CORSIA
