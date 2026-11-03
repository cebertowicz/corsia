"""The Messages app waits for the person: the agent's connect adds it off, and only the person's Sign in turns it on."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException, Request

from api import messages as messages_route
from connections import account_state
from connections.account_actions import sign_out_account
from connections.connected_accounts import MESSAGES_PROVIDER, create_account, get_account, visible_accounts
from connections.messages import account
from infra.database import get_db
from tests.messages_fixtures import make_chat_db
from tests.registry_helpers import make_session
from tools import messages_db
from tools import messages_texts as texts
from tools.messages_tools import MessagesConnectTool

ALL = {"mode": "all", "project_ids": [], "outside_projects": False}
PERSON = {"id": "person-1", "token_kind": "session"}


@pytest.fixture
def events(monkeypatch) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []

    def emit(category: str, action: str, **kw: Any) -> bool:
        seen.append((category, action, kw["properties"]["provider"]))
        return True

    monkeypatch.setattr(account_state, "emit_product_event", emit)
    return seen


@pytest.fixture
def readable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(messages_db, "CHAT_DB", make_chat_db(tmp_path / "chat.db"))
    monkeypatch.setattr(messages_db, "is_mac", lambda: True)


def _audit(db, account_id: str) -> list[tuple[str, str, str]]:
    return [tuple(row) for row in db.execute(
        "SELECT actor_kind, actor_id, action FROM connected_account_audit WHERE account_id=? ORDER BY id", (account_id,),
    )]


async def _post(user: dict, body: object, raw: bytes | None = None):
    encoded = json.dumps(body).encode() if raw is None else raw

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": encoded, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/api/messages/connect", "headers": []}, receive)
    return await messages_route.connect(request, user)


async def _refused(user: dict, body: object, raw: bytes | None = None) -> HTTPException:
    with pytest.raises(HTTPException) as refused:
        await _post(user, body, raw)
    return refused.value


async def test_connect_looks_and_adds_in_one_transaction(isolated_db, monkeypatch) -> None:
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


async def test_agent_connect_adds_the_app_off_with_an_audit_and_one_lifecycle_event(isolated_db, events) -> None:
    db = get_db()
    try:
        thread = make_session(db, prefix="corsia")
        row, created = await account.connect(thread)
        assert created and row["state"] == "needs_sign_in"
        assert await account.usable_for_thread(thread) is None
        assert _audit(db, str(row["id"])) == [("agent", thread, "connect")]
        assert events == [("connected_account", "connect_started", "messages")]
        await account.connect(thread)
        assert len(_audit(db, str(row["id"]))) == 1 and len(events) == 1
    finally:
        db.close()


async def test_the_connect_tool_tells_the_agent_the_person_turns_it_on(isolated_db, readable, monkeypatch) -> None:
    import tools.messages_tools as tools_module

    monkeypatch.setattr(tools_module, "conductor_user", lambda *_args: "user")
    db = get_db()
    try:
        thread = make_session(db, prefix="corsia")
        result = await MessagesConnectTool().execute(_thread_id=thread)
        assert result == texts.WAITING and "Full Disk Access" in result and "Sign in" in result
        # The person hears what Full Disk Access opens before granting it, on both paths.
        assert texts.FDA_OPENS in result and "protected files" in messages_route.NO_FDA
        assert await account.usable_for_thread(thread) is None
        assert await MessagesConnectTool().execute(_thread_id=thread) == texts.WAITING
    finally:
        db.close()


async def test_sign_in_turns_the_app_on_when_messages_is_readable(isolated_db, readable, events) -> None:
    db = get_db()
    try:
        thread = make_session(db, prefix="corsia")
        row, _ = await account.connect(thread)
        account_id = str(row["id"])
        assert await _post(PERSON, {"account_id": account_id}) == {"url": "", "account_id": account_id}
        assert get_account(account_id, db=db)["state"] == "connected"
        assert (await account.usable_for_thread(thread))["id"] == account_id
        assert _audit(db, account_id)[-1] == ("person", "person-1", "sign_in")
        assert events[-1] == ("connected_account", "connect_succeeded", "messages")
    finally:
        db.close()


async def test_sign_in_turns_a_signed_out_app_back_on_at_its_reach(isolated_db, readable, events) -> None:
    db = get_db()
    try:
        row = create_account(MESSAGES_PROVIDER, account.ADDRESS, reach=ALL, created_by=("person", "person-1"), db=db)
        db.commit()
        account_id = str(row["id"])
        await sign_out_account(account_id, actor=("person", "person-1"))
        assert await _post(PERSON, {"account_id": account_id}) == {"url": "", "account_id": account_id}
        turned_on = get_account(account_id, db=db)
        assert turned_on["state"] == "connected" and turned_on["reach"] == row["reach"]
        assert events[-2:] == [("connected_account", "connect_started", "messages"),
                               ("connected_account", "connect_succeeded", "messages")]
    finally:
        db.close()


@pytest.mark.parametrize(("missing", "line"), [
    ("denied", messages_route.NO_FDA), ("absent", messages_route.NO_HISTORY), ("busy", messages_route.BUSY),
])
async def test_sign_in_leaves_the_app_off_and_gives_the_next_step_when_messages_cannot_be_read(
    isolated_db, tmp_path, monkeypatch, missing, line,
) -> None:
    monkeypatch.setattr(messages_db, "is_mac", lambda: True)
    monkeypatch.setattr(messages_db, "CHAT_DB", tmp_path / "none.db")
    if missing == "denied":
        def deny(_path):
            raise PermissionError

        monkeypatch.setattr(messages_db.os, "stat", deny)
    if missing == "busy":
        def busy() -> None:
            raise messages_db.MessagesError(texts.UNREADABLE)

        monkeypatch.setattr(messages_db, "check_readable", busy)
    db = get_db()
    try:
        row = create_account(
            MESSAGES_PROVIDER, account.ADDRESS, reach=ALL, created_by=("agent", "t"), state="needs_sign_in", db=db,
        )
        db.commit()
        refused = await _refused(PERSON, {"account_id": str(row["id"])})
        assert refused.status_code == 409 and refused.detail == line
        assert get_account(str(row["id"]), db=db)["state"] == "needs_sign_in"
        assert not _audit(db, str(row["id"]))
    finally:
        db.close()


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'{"account_id": 5}', b"x" * 3000])
async def test_a_bad_body_is_a_400_of_its_own(isolated_db, raw) -> None:
    refused = await _refused(PERSON, None, raw)
    assert refused.status_code == 400 and refused.detail == messages_route.BAD_REQUEST


async def test_sign_in_refuses_other_apps_unknown_ids_and_agents(isolated_db, readable) -> None:
    db = get_db()
    try:
        row = create_account(MESSAGES_PROVIDER, account.ADDRESS, reach=ALL, created_by=("person", "person-1"), db=db)
        other = create_account("google", "g@example.test", reach=ALL, created_by=("system", "test"), db=db)
        db.commit()
        for bad in (str(other["id"]), "missing"):
            refused = await _refused(PERSON, {"account_id": bad})
            assert refused.status_code == 404 and refused.detail == messages_route.NOT_FOUND
        agent = await _refused({"id": "thread-1", "token_kind": "agent"}, {"account_id": str(row["id"])})
        assert agent.status_code == 403 and agent.detail == messages_route.PERSON_ONLY
        assert get_account(str(other["id"]), db=db)["state"] == "connected"
    finally:
        db.close()
