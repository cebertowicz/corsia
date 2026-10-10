from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest


class _Socket:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)


class _Db:
    def close(self) -> None:
        pass


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/test','t0','t0')"
    )
    connection.commit()
    return connection


async def _persist_queue(
    db: sqlite3.Connection,
    thread_id: str,
    _thread_row,
    content: str,
    *,
    input_id: str,
    delivery_intent: str,
    **_kwargs,
):
    from infra.send_types import InboxQueuedInput, InputReceipt
    from infra.input_admission import accept_thread_input

    accepted = accept_thread_input(
        thread_id,
        role="user",
        content=content,
        intent=delivery_intent,
        message_id=f"message-{input_id}",
        created_at=f"created-{input_id}",
        db=db,
    )
    receipt = InputReceipt(
        accepted.thread_id,
        accepted.message_id,
        accepted.input_order,
        accepted.revision,
        accepted.created_at,
    )
    return InboxQueuedInput(receipt=receipt)


@pytest.mark.asyncio
async def test_main_ws_protocol_intent_controls_canonical_contract_not_metadata():
    from ws.message_dispatch import handle_message

    db = _db()
    socket = _Socket()
    session = {"id": "thread-1", "reasoning_effort": None}
    try:
        async def run_in_db(call):
            return call()

        await handle_message(
            socket,
            {"id": "user-1"},
            "user-1",
            {
                "thread_id": "thread-1",
                "input_id": "queue-1",
                "content": "queue intentionally",
                "delivery_intent": "queue_after_turn",
                "metadata": {"delivery_intent": "apply_now"},
            },
            get_db_fn=_Db,
            run_in_db_fn=run_in_db,
            get_thread_for_user_fn=lambda *_args, **_kwargs: session,
            touch_thread_fn=lambda *_args, **_kwargs: None,
            send_thread_input_fn=lambda *args, **kwargs: _persist_queue(
                db, *args, **kwargs,
            ),
            handle_cc_main_message_fn=lambda *_args, **_kwargs: None,
            handle_codex_main_message_fn=lambda *_args, **_kwargs: None,
        )

        contract = db.execute(
            "SELECT delivery_intent FROM messages WHERE id=?",
            ("message-queue-1",),
        ).fetchone()
        assert contract["delivery_intent"] == "queue_after_turn"
        metadata = db.execute(
            "SELECT metadata FROM messages WHERE id=?", ("message-queue-1",)
        ).fetchone()["metadata"]
        assert metadata is None or "delivery_intent" not in metadata
        assert socket.frames[-1]["type"] == "input_queued"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_subagent_ws_protocol_defaults_invalid_or_missing_intent_to_apply_now(
    monkeypatch,
):
    from infra import execution_class
    from ws.subagent_control import handle_subagent_feed

    db = _db()
    socket = _Socket()
    session = {"id": "thread-1", "model": "codex/gpt-5-codex", "spawned_by": "parent"}
    monkeypatch.setattr(execution_class, "execution_class", lambda _session: "codex_sub")
    try:
        async def owned_thread(_user, _thread_id):
            return session

        await handle_subagent_feed(
            socket,
            {"id": "user-1"},
            "thread-1",
            "apply normally",
            input_id="apply-1",
            delivery_intent="metadata-spoof-is-not-a-protocol-value",
            owned_thread_fn=owned_thread,
            send_thread_input_fn=lambda *args, **kwargs: _persist_queue(
                db, *args, **kwargs,
            ),
        )

        contract = db.execute(
            "SELECT delivery_intent FROM messages WHERE id=?",
            ("message-apply-1",),
        ).fetchone()
        assert contract["delivery_intent"] == "apply_now"
        metadata = db.execute(
            "SELECT metadata FROM messages WHERE id=?", ("message-apply-1",)
        ).fetchone()["metadata"]
        assert metadata is None or "delivery_intent" not in metadata
        assert socket.frames[-1]["type"] == "input_queued"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_subagent_feed_rejects_removed_model_without_dispatch(monkeypatch):
    from infra import execution_class
    from llm.resolution import ModelUnavailableError
    from ws.subagent_control import handle_subagent_feed

    removed_model = "omlx@local/qwen-flash-next"
    session = {
        "id": "stale-child",
        "model": removed_model,
        "spawned_by": "parent",
    }

    async def owned_thread(_user, _thread_id):
        return session

    def missing_model(model):
        raise ModelUnavailableError(model, f"Model '{model}' not found in catalog")

    monkeypatch.setattr(execution_class, "resolve_execution_target", missing_model)
    send = AsyncMock()
    socket = _Socket()
    try:
        await handle_subagent_feed(
            socket,
            {"id": "user-1"},
            "stale-child",
            "continue",
            input_id="stale-input",
            owned_thread_fn=owned_thread,
            send_thread_input_fn=send,
        )
    except ModelUnavailableError as exc:
        pytest.fail(f"subagent feed did not return a rejection frame: {exc}")

    send.assert_not_awaited()
    assert socket.frames == [{
        "type": "error",
        "thread_id": "stale-child",
        "message": (
            f"Model '{removed_model}' not found in catalog. Choose another model."
        ),
        "input_id": "stale-input",
    }]


def test_explicit_queue_never_preempts_the_live_turn():
    from infra.thread_driver import admit_input

    db = _db()
    try:
        live = admit_input(
            "thread-1", role="user", content="already running",
            intent="apply_now", message_id="message-live",
            turn_id="live-turn", boot_epoch="boot-1", admitted_at="created-live",
            db=db,
        )
        queued = admit_input(
            "thread-1", role="user", content="wait for the live turn",
            intent="queue_after_turn", message_id="message-queue-2",
            turn_id="queued-turn", boot_epoch="boot-1", admitted_at="created-queue-2",
            db=db,
        )

        assert live.batch is not None
        assert queued.batch is None
        active = db.execute(
            "SELECT id FROM active_turns WHERE thread_id=? AND status='running'",
            ("thread-1",),
        ).fetchone()
        assert active["id"] == "live-turn"
        intent = db.execute(
            "SELECT delivery_intent FROM messages WHERE id=?",
            ("message-queue-2",),
        ).fetchone()
        assert intent["delivery_intent"] == "queue_after_turn"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_native_subagent_feed_delivers_through_delegation(monkeypatch):
    from infra import execution_class
    from ws.subagent_control import handle_subagent_feed

    db = _db()
    socket = _Socket()
    session = {"id": "thread-1", "model": "native/test", "spawned_by": "parent"}
    monkeypatch.setattr(execution_class, "execution_class", lambda _session: "native")

    async def persist(*args, **kwargs):
        return await _persist_queue(db, *args, **kwargs)

    send = AsyncMock(side_effect=persist)
    try:
        async def owned_thread(_user, _thread_id):
            return session

        await handle_subagent_feed(
            socket,
            {"id": "user-1"},
            "thread-1",
            "continue on the local model",
            input_id="native-1",
            owned_thread_fn=owned_thread,
            send_thread_input_fn=send,
        )

        send.assert_awaited_once()
        assert send.await_args.kwargs["native_mode"] == "delegation"
        assert [frame["type"] for frame in socket.frames] == ["input_queued"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_native_main_thread_refuses_subagent_feed(monkeypatch):
    from infra import execution_class
    from ws.subagent_control import handle_subagent_feed

    session = {"id": "thread-1", "model": "native/test", "spawned_by": None}
    monkeypatch.setattr(execution_class, "execution_class", lambda _session: "native")
    send = AsyncMock()
    socket = _Socket()

    async def owned_thread(_user, _thread_id):
        return session

    await handle_subagent_feed(
        socket,
        {"id": "user-1"},
        "thread-1",
        "not a subagent",
        input_id="main-1",
        owned_thread_fn=owned_thread,
        send_thread_input_fn=send,
    )

    send.assert_not_awaited()
    assert socket.frames == [{
        "type": "error",
        "thread_id": "thread-1",
        "message": "This conversation can't be continued as a subagent.",
        "input_id": "main-1",
    }]
