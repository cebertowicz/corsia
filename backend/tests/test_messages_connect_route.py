"""Turning the Messages row on: one transaction, an audit record, lifecycle events, and Settings' Sign in."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException, Request

from api import messages as messages_route
from connections import account_state
from connections.account_actions import sign_out_account
from connections.connected_accounts import MESSAGES_PROVIDER, create_account, get_account, visible_accounts
from connections.messages import account
from infra.database import get_db
from tests.registry_helpers import make_session

ALL = {"mode": "all", "project_ids": [], "outside_projects": False}
PERSON = {"id": "person-1", "token_kind": "session"}


@pytest.fixture
def events(monkeypatch) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []

    def emit(category: str, action: str, **kw) -> bool:
        seen.append((category, action, kw["properties"]["provider"]))
        return True

    monkeypatch.setattr(account_state, "emit_product_event", emit)
    return seen


def _audit(db, account_id: str) -> list[tuple[str, str, str]]:
    return [tuple(row) for row in db.execute(
        "SELECT actor_kind, actor_id, action FROM connected_account_audit WHERE account_id=? ORDER BY id", (account_id,),
    )]


async def _post(user: dict, body: object):
    encoded = json.dumps(body).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": encoded, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/api/messages/connect", "headers": []}, receive)
    return await messages_route.connect(request, user)


async def test_agent_connect_looks_and_adds_in_one_transaction(isolated_db, monkeypatch) -> None:
    inside: list[tuple[str, bool]] = []
    looked, added = account.visible_accounts, account.create_account_for_thread

    def look(*args, **kwargs):
        inside.append(("look", kwargs["db"].in_transaction))
        return looked(*args, **kwargs)

    def add(*args, **kwargs):
        inside.append(("add", kwargs["db"].in_transaction))
        return added(*args, **kwargs)

    monkeypatch.setattr(account, "visible_accounts", look)
    monkeypatch.setattr(account, "create_account_for_thread", add)
    db = get_db()
    try:
        thread = make_session(db, prefix="corsia")
        first, created = await account.connect(thread)
        second, created_again = await account.connect(thread)
        assert created and not created_again and second["id"] == first["id"]
        assert inside and all(flag for _, flag in inside)
        assert [kind for kind, _ in inside].count("add") == 1
        assert len(visible_accounts(None, db=db, provider=MESSAGES_PROVIDER)) == 1
    finally:
        db.close()


async def test_agent_connect_writes_a_connect_audit_and_lifecycle_events(isolated_db, events) -> None:
    db = get_db()
    try:
        thread = make_session(db, prefix="corsia")
        row, _ = await account.connect(thread)
        assert _audit(db, str(row["id"])) == [("agent", thread, "connect")]
        assert events == [("connected_account", "connect_started", "messages"),
                          ("connected_account", "connect_succeeded", "messages")]
        await account.connect(thread)
        assert len(_audit(db, str(row["id"]))) == 1 and len(events) == 2
    finally:
        db.close()


async def test_settings_sign_in_turns_a_signed_out_row_back_on_at_its_reach(isolated_db, events) -> None:
    db = get_db()
    try:
        row = create_account(MESSAGES_PROVIDER, account.ADDRESS, reach=ALL, created_by=("person", "person-1"), db=db)
        db.commit()
        account_id = str(row["id"])
        await sign_out_account(account_id, actor=("person", "person-1"))
        assert get_account(account_id, db=db)["state"] == "signed_out"
        assert await _post(PERSON, {"account_id": account_id}) == {"url": "", "account_id": account_id}
        turned_on = get_account(account_id, db=db)
        assert turned_on["state"] == "connected" and turned_on["reach"] == row["reach"]
        assert _audit(db, account_id)[-1] == ("person", "person-1", "sign_in")
        assert events[-2:] == [("connected_account", "connect_started", "messages"),
                               ("connected_account", "connect_succeeded", "messages")]
    finally:
        db.close()


async def test_settings_sign_in_refuses_other_rows_unknown_ids_and_agents(isolated_db) -> None:
    db = get_db()
    try:
        row = create_account(MESSAGES_PROVIDER, account.ADDRESS, reach=ALL, created_by=("person", "person-1"), db=db)
        other = create_account("google", "g@example.test", reach=ALL, created_by=("system", "test"), db=db)
        db.commit()
        bad_bodies: tuple[object, ...] = ({"account_id": str(other["id"])}, {"account_id": "missing"}, {}, [])
        for bad in bad_bodies:
            with pytest.raises(HTTPException) as refused:
                await _post(PERSON, bad)
            assert refused.value.status_code == 404 and refused.value.detail == messages_route.NOT_FOUND
        with pytest.raises(HTTPException) as agent:
            await _post({"id": "thread-1", "token_kind": "agent"}, {"account_id": str(row["id"])})
        assert agent.value.status_code == 403 and agent.value.detail == messages_route.PERSON_ONLY
        assert get_account(str(other["id"]), db=db)["state"] == "connected"
    finally:
        db.close()
