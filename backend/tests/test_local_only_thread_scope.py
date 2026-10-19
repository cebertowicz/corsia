"""Local-only thread scope admission, inheritance, route, and send contracts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import httpx
import pytest
from fastapi import FastAPI


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    return db


def _thread(
    db,
    thread_id: str,
    *,
    user_id: str = "u",
    model: str | None = "local",
    local_only: int = 0,
    inherited_from: str | None = None,
    spawned_by: str | None = None,
    pending_model: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO threads("
        "id,user_id,model,pending_model,local_only,"
        "local_only_inherited_from_thread_id,spawned_by,created_at,updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?)",
        (
            thread_id,
            user_id,
            model,
            pending_model,
            local_only,
            inherited_from,
            spawned_by,
            "t",
            "t",
        ),
    )


def test_create_fresh_thread_refuses_cloud_child_without_writing(monkeypatch) -> None:
    from infra import fresh_threads

    db = _db()
    _thread(db, "root", local_only=1)
    monkeypatch.setattr(
        fresh_threads,
        "local_only_model_refusal",
        lambda model, **_kwargs: "local_only_requires_local_model" if model == "cloud" else None,
    )

    with pytest.raises(ValueError, match="local_only_requires_local_model"):
        fresh_threads.create_fresh_thread(
            user_id="u",
            thread_id="refused-child",
            model="cloud",
            spawned_by="root",
            db=db,
        )

    assert db.execute("SELECT id FROM threads WHERE id='refused-child'").fetchone() is None


def test_request_switch_refuses_cloud_for_local_only_thread(monkeypatch) -> None:
    from infra import model_switches

    db = _db()
    _thread(db, "thread", local_only=1)
    monkeypatch.setattr(
        model_switches,
        "thread_model_refusal",
        lambda *_args, **_kwargs: "local_only_requires_local_model",
    )
    monkeypatch.setattr(model_switches, "bind_model_account", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="local_only_requires_local_model"):
        model_switches.request_switch(
            "thread", "cloud", operation_id="op", requested_at="t", db=db,
        )

    assert db.execute(
        "SELECT pending_model FROM threads WHERE id='thread'",
    ).fetchone()[0] is None


def test_activation_rechecks_local_only_and_returns_unstarted_suffix(monkeypatch) -> None:
    from infra import model_switches
    from infra.database import write_transaction

    db = _db()
    _thread(db, "thread", model="source")
    monkeypatch.setattr(model_switches, "bind_model_account", lambda *_args, **_kwargs: None)
    model_switches.request_switch(
        "thread", "cloud", operation_id="op", requested_at="t", db=db,
    )
    db.execute(
        "INSERT INTO messages("
        "id,thread_id,role,content,delivery_state,target_model,"
        "target_execution_epoch,input_order,delivery_revision,created_at"
        ") VALUES ('m','thread','user','x','queued','cloud',1,1,1,'t')",
    )
    db.execute("UPDATE threads SET local_only=1,switch_status='ready' WHERE id='thread'")
    monkeypatch.setattr(
        model_switches,
        "thread_model_refusal",
        lambda *_args, **_kwargs: "local_only_requires_local_model",
    )

    with write_transaction(db, scope="test") as connection:
        result = model_switches.activate_ready_switch(
            connection, "thread", committed_at="t2",
        )

    row = db.execute("SELECT switch_status,model FROM threads WHERE id='thread'").fetchone()
    message = db.execute(
        "SELECT target_model,target_execution_epoch FROM messages WHERE id='m'",
    ).fetchone()
    assert result is not None and result.committed is False
    assert tuple(row) == ("failed", "source")
    assert tuple(message) == ("source", 0)


def test_adopt_first_model_refuses_cloud_for_local_only_thread(monkeypatch) -> None:
    from infra import model_switch_inputs
    from infra.database import write_transaction

    db = _db()
    _thread(db, "thread", model=None, local_only=1)
    monkeypatch.setattr(
        "infra.local_only.local_only_model_refusal",
        lambda *_args, **_kwargs: "local_only_requires_local_model",
    )

    with write_transaction(db, scope="test") as connection:
        with pytest.raises(ValueError, match="local_only_requires_local_model"):
            model_switch_inputs.adopt_first_model(connection, "thread", "cloud", now="t")

    assert db.execute("SELECT model FROM threads WHERE id='thread'").fetchone()[0] is None


def test_inheritance_uses_root_and_every_creator_candidate(monkeypatch) -> None:
    from infra import fresh_threads

    db = _db()
    _thread(db, "root", local_only=1)
    _thread(db, "child", local_only=1, inherited_from="root", spawned_by="root")
    _thread(db, "ordinary")
    monkeypatch.setattr(fresh_threads, "local_only_model_refusal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "connections.account_selection.bind_model_account",
        lambda *_args, **_kwargs: None,
    )

    grandchild = fresh_threads.create_fresh_thread(
        user_id="u", thread_id="grandchild", model="local", spawned_by="child", db=db,
    )
    inherited_later = fresh_threads.create_fresh_thread(
        user_id="u",
        thread_id="later",
        model="local",
        parent_thread_id="ordinary",
        actor_thread_id="root",
        db=db,
    )
    unaffected = fresh_threads.create_fresh_thread(
        user_id="u", thread_id="unaffected", model="cloud", spawned_by="ordinary", db=db,
    )

    assert (
        grandchild["local_only"],
        grandchild["local_only_inherited_from_thread_id"],
    ) == (1, "root")
    assert (
        inherited_later["local_only"],
        inherited_later["local_only_inherited_from_thread_id"],
    ) == (1, "root")
    assert unaffected["local_only"] == 0


def test_non_local_thread_can_switch_to_a_cloud_model(monkeypatch) -> None:
    from infra import model_switches

    db = _db()
    _thread(db, "ordinary")
    monkeypatch.setattr(model_switches, "bind_model_account", lambda *_args, **_kwargs: None)

    request = model_switches.request_switch(
        "ordinary", "cloud", operation_id="op", requested_at="t", db=db,
    )

    assert request.destination_model == "cloud"
    assert db.execute(
        "SELECT pending_model FROM threads WHERE id='ordinary'",
    ).fetchone()[0] == "cloud"


@pytest.mark.asyncio
async def test_local_only_route_requires_a_person_and_owns_the_thread(
    isolated_db,
    monkeypatch,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OTHER_TOKEN, OWNER_SESSION_TOKEN, OWNER_TOKEN
    from infra.database import get_db

    db = get_db()
    try:
        _thread(db, "root", user_id="steve")
        _thread(
            db,
            "child",
            user_id="steve",
            local_only=1,
            inherited_from="root",
            spawned_by="root",
        )
        _thread(db, "foreign", user_id="leksi")
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        thread_mutations,
        "local_only_model_refusal",
        lambda *_args, **_kwargs: None,
    )
    app = FastAPI()
    app.include_router(thread_mutations.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        assert (await client.put(
            "/api/threads/root/local-only?token=" + OWNER_TOKEN,
            json={"local_only": True},
        )).status_code == 403
        assert (await client.put(
            "/api/threads/root/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": True},
        )).json()["local_only"] is True
        assert (await client.put(
            "/api/threads/root/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": False},
        )).json()["local_only"] is False
        inherited = await client.put(
            "/api/threads/child/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": False},
        )
        invalid = await client.put(
            "/api/threads/root/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": "yes"},
        )
        foreign = await client.put(
            "/api/threads/foreign/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": True},
        )

    assert inherited.status_code == 409
    assert "root" in inherited.json()["detail"]
    assert invalid.status_code == 422
    assert foreign.status_code == 404
    assert OTHER_TOKEN  # Keep the seeded foreign-account contract explicit.


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["model", "pending_model"])
async def test_local_only_route_refuses_cloud_current_or_pending_model(
    isolated_db,
    monkeypatch,
    column,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OWNER_SESSION_TOKEN
    from infra.database import get_db

    db = get_db()
    try:
        _thread(
            db,
            "root",
            user_id="steve",
            model="cloud" if column == "model" else "local",
            pending_model="cloud" if column == "pending_model" else None,
        )
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        thread_mutations,
        "local_only_model_refusal",
        lambda model, **_kwargs: (
            "local_only_requires_local_model: choose local" if model == "cloud" else None
        ),
    )
    app = FastAPI()
    app.include_router(thread_mutations.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.put(
            "/api/threads/root/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": True},
        )

    assert response.status_code == 409
    assert "local_only_requires_local_model" in response.json()["detail"]


@pytest.mark.asyncio
async def test_model_switch_route_returns_local_only_refusal_as_409(
    isolated_db,
    monkeypatch,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OWNER_SESSION_TOKEN
    from infra.database import get_db

    db = get_db()
    try:
        _thread(db, "root", user_id="steve", local_only=1)
        db.commit()
    finally:
        db.close()

    async def refuse(*_args, **_kwargs):
        raise ValueError("local_only_requires_local_model: choose local")

    async def prepared(*_args):
        return None

    monkeypatch.setattr("connections.harness_refresh.prepare_selection", prepared)
    monkeypatch.setattr(thread_mutations, "is_dispatchable_model", lambda *_args: True)
    monkeypatch.setattr(thread_mutations, "switch_model", refuse)
    app = FastAPI()
    app.include_router(thread_mutations.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.patch(
            "/api/threads/root?token=" + OWNER_SESSION_TOKEN,
            json={"model": "cloud"},
        )

    assert response.status_code == 409
    assert "local_only_requires_local_model" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sender_local_only", "recipient_model", "confirmed", "refused"),
    [
        (True, "cloud", False, True),
        (True, "cloud", True, False),
        (True, "local", False, False),
        (False, "cloud", False, False),
    ],
)
async def test_delegate_send_enforces_cloud_confirmation_at_send_time(
    isolated_db,
    monkeypatch,
    sender_local_only,
    recipient_model,
    confirmed,
    refused,
) -> None:
    from infra.database import get_db
    from tools import delegate_send

    db = get_db()
    try:
        _thread(db, "sender", user_id="steve", local_only=int(sender_local_only))
        _thread(
            db,
            "recipient",
            user_id="steve",
            model=recipient_model,
            spawned_by="sender",
        )
        db.commit()
    finally:
        db.close()
    sent: list[str] = []

    async def deliver(*_args, **_kwargs):
        sent.append("delivered")
        return {"ok": True}

    async def no_plan(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "infra.local_only.local_only_model_refusal",
        lambda model, **_kwargs: "cloud" if model == "cloud" else None,
    )
    monkeypatch.setattr(delegate_send, "native_send_scope_error", lambda **_kwargs: None)
    monkeypatch.setattr(delegate_send, "_prepare_plan_task", no_plan)
    monkeypatch.setattr(delegate_send, "_send", deliver)

    result = await delegate_send.DelegateSendTool().execute(
        "recipient",
        "safe task",
        _thread_id="sender",
        no_sensitive_content=confirmed,
    )

    assert ("no_sensitive_content: true" in result) is refused
    assert sent == ([] if refused else ["delivered"])


def test_person_recipient_is_exempt_from_send_confirmation(monkeypatch) -> None:
    from infra import local_only

    db = _db()
    monkeypatch.setattr(
        local_only,
        "local_only_model_refusal",
        lambda *_args, **_kwargs: "cloud",
    )

    assert local_only.send_confirmation_refusal(
        {"local_only": True},
        {"model": "cloud", "agent_enabled": 0, "spawned_by": None},
        no_sensitive_content=False,
        db=db,
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_local_only", "model", "confirmed", "refused"),
    [
        (True, "cloud", False, True),
        (True, "cloud", True, False),
        (True, "local", False, False),
        (False, "cloud", False, False),
    ],
)
async def test_delegate_task_first_message_enforces_cloud_confirmation(
    isolated_db,
    monkeypatch,
    parent_local_only,
    model,
    confirmed,
    refused,
) -> None:
    from infra.database import get_db
    from tools import delegate_task

    db = get_db()
    try:
        _thread(db, "parent", user_id="steve", local_only=int(parent_local_only))
        db.commit()
    finally:
        db.close()
    spawned: list[str] = []

    async def create(**_kwargs):
        spawned.append("child")
        return {"id": "child"}

    async def send(*_args, **_kwargs):
        return type("Sent", (), {"driver": "turn", "duplicate": False})()

    monkeypatch.setattr(
        "infra.local_only.local_only_model_refusal",
        lambda selected, **_kwargs: "cloud" if selected == "cloud" else None,
    )
    monkeypatch.setattr(delegate_task, "eligible_delegation_model", lambda selected, **_kwargs: (selected, None))
    monkeypatch.setattr(delegate_task.delegation, "_is_cc_model", lambda _model: False)
    monkeypatch.setattr(delegate_task.delegation, "_is_delisted", lambda _model: False)
    monkeypatch.setattr(delegate_task, "create_visible_fresh_thread", create)
    monkeypatch.setattr(delegate_task, "send_thread_input", send)

    result = await delegate_task._spawn(
        "parent",
        {"user_id": "steve", "local_only": parent_local_only, "project_id": None},
        "safe task",
        {"model": model, "no_sensitive_content": confirmed},
    )

    assert isinstance(result, str) is refused
    if refused:
        assert "no_sensitive_content: true" in result
        assert spawned == []
    else:
        assert result["ok"] is True
        assert spawned == ["child"]
