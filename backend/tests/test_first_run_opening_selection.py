"""Real app account, first-model transaction and live opening ordering."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture
def fresh_app(isolated_db, monkeypatch):
    from infra.database import write_transaction

    with write_transaction(None, scope="fresh_app_opening") as db:
        db.execute("DELETE FROM auth_tokens")
        db.execute("DELETE FROM users")
    monkeypatch.setattr("onboarding.first_run_wake.app_reference_served", lambda: True)
    monkeypatch.setattr("onboarding.app_first_run._pre_pick_facts", lambda **_: {
        "agents_found": [], "connected_app_count": 0, "email_calendar_ready": False,
    })
    return isolated_db


def _rows(sql, *args):
    from infra.database import get_db

    db = get_db()
    try:
        return [dict(row) for row in db.execute(sql, args)]
    finally:
        db.close()


async def _account():
    from api.setup import router
    from infra.corsia_agent import get_corsia_agent
    from infra.default_port import DEFAULT_BACKEND_PORT

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50100)),
        base_url=f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}",
    ) as client:
        response = await client.post("/api/setup/first-account", json={
            "name": "Pat Example", "username": "pat", "password": "long-enough-passphrase",
        })
    assert response.status_code == 200
    conductor = get_corsia_agent()
    assert conductor
    return conductor


def _ready(monkeypatch):
    from connections import account_selection
    from llm import resolution

    monkeypatch.setattr(resolution, "resolve_execution_target", lambda *_, **__: type(
        "Target", (), {"available": True, "availability_reason": None},
    )())
    monkeypatch.setattr(account_selection, "bind_model_account", lambda *_, **__: None)


@pytest.mark.asyncio
async def test_app_api_account_lands_with_plan_and_first_pick_publishes_before_drive(fresh_app, monkeypatch):
    from infra import state
    from thread.model import _apply_first_selection

    thread = await _account()
    opening_id = f"first-run-opening:{thread}"
    assert _rows("SELECT id FROM messages WHERE id=?", opening_id) == []
    plan_before = _rows("SELECT id,state,revision FROM plan_nodes WHERE workflow_ref='first-run-app' ORDER BY id")
    assert plan_before
    # New requests/relaunch reads observe the same pending plan and no greeting.
    assert _rows("SELECT model FROM threads WHERE id=?", thread) == [{"model": None}]
    assert _rows("SELECT id FROM messages WHERE id=?", opening_id) == []
    _ready(monkeypatch)
    monkeypatch.setattr("thread.model._inline_first_run_wake", AsyncMock(return_value="First applicable setup step"))
    events = []
    monkeypatch.setattr(state, "broadcast_to_thread", lambda _thread, event, **_: events.append(event))

    async def drive(thread_id):
        opening_events = [event for event in events if event["type"] == "message_added"
                          and event["message"]["id"] == opening_id]
        assert len(opening_events) == 1
        message = opening_events[0]["message"]
        assert message["content"].startswith("Hi Pat.\n\nI'm CORSIA.")
        assert message["metadata"]["first_run_opening"] is True
        assert message["seq"] > 0
        [stored] = _rows("SELECT content,seq FROM messages WHERE id=?", opening_id)
        assert stored == {"content": message["content"], "seq": message["seq"]}
        events.append({"type": "drive"})

    monkeypatch.setattr("infra.thread_execution.drive_thread", drive)
    result = await _apply_first_selection(thread, "test/model", user_id="pat", now="2026-10-22T00:00:00")
    assert result.changed
    assert _rows("SELECT id,state,revision FROM plan_nodes WHERE workflow_ref='first-run-app' ORDER BY id") == plan_before
    assert await _apply_first_selection(thread, "test/model", user_id="pat", now="2026-10-22T00:00:01") is None
    assert len([event for event in events if event["type"] == "drive"]) == 1
    assert len(_rows("SELECT id FROM messages WHERE id=?", opening_id)) == 1


@pytest.mark.asyncio
async def test_binding_failure_after_opening_insert_rolls_back_entire_pick(fresh_app, monkeypatch):
    from connections import account_selection
    from thread.model_row import select_first_model

    thread = await _account()
    _ready(monkeypatch)
    plan_before = _rows("SELECT id,state,revision FROM plan_nodes WHERE workflow_ref='first-run-app' ORDER BY id")

    def fail(*_, db, **__):
        # Prove the greeting was already inserted on this exact transaction.
        assert db.execute("SELECT 1 FROM messages WHERE id=?", (f"first-run-opening:{thread}",)).fetchone()
        db.execute("UPDATE threads SET model='partial-binding' WHERE id=?", (thread,))
        raise RuntimeError("binding failed")

    monkeypatch.setattr(account_selection, "bind_model_account", fail)
    with pytest.raises(RuntimeError, match="binding failed"):
        select_first_model(thread, "test/model", user_id="pat", now="t", first_run_wake="First step")
    assert _rows("SELECT model FROM threads WHERE id=?", thread) == [{"model": None}]
    assert _rows("SELECT id FROM messages WHERE id=?", f"first-run-opening:{thread}") == []
    [wake] = _rows("SELECT delivery_state,delivery_error FROM messages WHERE thread_id=? AND delivery_error='no_model_selected'", thread)
    assert wake == {"delivery_state": "queued", "delivery_error": "no_model_selected"}
    assert _rows("SELECT id,state,revision FROM plan_nodes WHERE workflow_ref='first-run-app' ORDER BY id") == plan_before


@pytest.mark.asyncio
async def test_restored_history_excludes_both_warm_opening_and_selection_seed(fresh_app):
    from infra.database import write_transaction
    from infra.queries import insert_message
    from onboarding.app_first_run import pending_warm_opening, seed_first_model_opening

    thread = await _account()
    with write_transaction(None, scope="restore_while_held") as db:
        # A held app wake exists, but genuine restored history makes it ineligible.
        insert_message(thread, "user", "Earlier conversation", db=db)
        assert pending_warm_opening(thread, db=db) is None
        seed_first_model_opening(thread, db=db)
        assert db.execute("SELECT 1 FROM messages WHERE id=?", (f"first-run-opening:{thread}",)).fetchone() is None
