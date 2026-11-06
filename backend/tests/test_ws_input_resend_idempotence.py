"""A resent WebSocket input frame is admitted once, on both input paths.

After a reconnect the client puts every input it holds no receipt for back on
the new socket, marked ``recovery``. These tests drive both WebSocket entry
points with the frame the client actually sends and pin what each resend does:
a stored input is answered with its row, an absent one is admitted, and an
unmarked second copy goes through the duplicate check.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_MODEL = "anthropic/claude-3-5-sonnet"
INPUT_ID = "4f1a0e2c-7c31-4c41-8f0a-0d0b0e0c0a01"
CONTENT = "resend me exactly"


def _thread_row(thread_id: str) -> dict:
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, 'user-1', ?, 't0', 't0')",
        (thread_id, _MODEL),
    )
    db.commit()
    row = dict(db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone())
    db.close()
    return row


def _stored(message_id: str) -> sqlite3.Row | None:
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(
            "SELECT content,metadata,delivery_options,delivery_intent,created_at,"
            "input_order FROM messages WHERE id=?",
            (message_id,),
        ).fetchone()
    finally:
        db.close()


def _user_inputs(thread_id: str) -> int:
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='user'",
            (thread_id,),
        ).fetchone()[0]
    finally:
        db.close()


def _stub_delivery(monkeypatch):
    """Real admission, fake provider execution, and every receipt recorded."""
    import infra.send_service as service
    import ws as ws_module
    import ws.ingress as ingress

    receipts: list[dict] = []
    errors: list[dict] = []

    async def _execute(batch, **_kwargs):
        return service.SendResult(
            driver="fake", message_id=batch.items[0].message_id,
            turn_id=batch.turn_id, turn_order=batch.turn_order,
        )

    def _record(_ws, payload):
        # Admission receipts are reserved writes; only the error path is awaited.
        (errors if payload.get("type") == "error" else receipts).append(payload)
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return done

    monkeypatch.setattr(service, "execute_claimed_batch", _execute)
    monkeypatch.setattr(service, "request_safe_boundary", AsyncMock())
    monkeypatch.setattr(
        "jobs.corrections_recording.notify_if_owner", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(ingress, "send_to_client", _record)
    monkeypatch.setattr("infra.state.send_to_client", _record)
    monkeypatch.setattr(ws_module, "send_thread_input", service.send_thread_input)
    return service, receipts, errors


async def _send_frame(
    service, path: str, row: dict, content: str, *, recovery: bool = False,
) -> None:
    """Deliver one composer input, as its WebSocket frame, through its handler."""
    import ws as ws_module
    import ws.message_dispatch as dispatch

    async def run(callback):
        return callback()

    if path == "message":
        await dispatch.handle_message(
            SimpleNamespace(), {"id": "user-1"}, "user-1",
            {
                "type": "message", "thread_id": row["id"], "content": content,
                "input_id": INPUT_ID, "reasoning_effort": "high",
                "reply_context": {"in_view": [[1, 3, 2]]},
                **({"recovery": True} if recovery else {}),
            },
            get_db_fn=lambda: SimpleNamespace(close=lambda: None),
            run_in_db_fn=run,
            get_thread_for_user_fn=lambda *_a, **_k: row,
            touch_thread_fn=lambda *_a, **_k: None,
            send_thread_input_fn=service.send_thread_input,
            handle_cc_main_message_fn=AsyncMock(),
            handle_codex_main_message_fn=AsyncMock(),
        )
        return

    await ws_module._handle_subagent_feed(
        SimpleNamespace(), {"id": "user-1"}, row["id"], content,
        [], False, INPUT_ID, None, None, {"in_view": [[1, 3, 2]]}, recovery,
    )


def _as_subagent(monkeypatch, row: dict) -> None:
    """Classify the target as a model-backed subagent, the feed path's condition."""
    import ws as ws_module

    monkeypatch.setattr("infra.execution_class.execution_class", lambda _row: "native")
    monkeypatch.setattr("infra.execution_class.is_subagent", lambda _row: True)
    monkeypatch.setattr(ws_module, "_owned_thread", AsyncMock(return_value=row))


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["message", "subagent_feed"])
async def test_a_resent_frame_is_one_input(isolated_db, monkeypatch, path):
    service, receipts, errors = _stub_delivery(monkeypatch)
    row = _thread_row(f"{path}-thread")
    if path == "subagent_feed":
        _as_subagent(monkeypatch, row)

    await _send_frame(service, path, row, CONTENT)
    await _send_frame(service, path, row, CONTENT)

    assert not errors, errors
    assert len(receipts) == 2, receipts
    # The first admission earned a FIFO position; the replay was answered with
    # that same row's facts and took no new one.
    assert receipts[0].get("duplicate", False) is False
    assert receipts[1]["duplicate"] is True
    assert receipts[1]["message_id"] == INPUT_ID
    stored = _stored(INPUT_ID)
    assert stored is not None
    assert _user_inputs(row["id"]) == 1, "the resent frame stored a second input"
    assert receipts[1]["created_at"] == str(stored["created_at"])
    assert int(stored["input_order"]) == receipts[1]["queue_order"]
    # A duplicate keeps the payload it was stored with: nothing the server mints
    # per receipt (a timestamp, a resolved document note) entered either column,
    # so the replay's payload still matched and no second input was admitted.
    assert json.loads(stored["metadata"] or "{}") == {"reply_via_post_update": True}
    assert set(json.loads(stored["delivery_options"] or "{}")) == {
        "native_mode", "attachments", "reasoning_effort", "accepted_effects",
        "broadcast_input", "touch_session_on_insert",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["message", "subagent_feed"])
async def test_the_same_input_with_other_content_is_refused(
    isolated_db, monkeypatch, path
):
    """The duplicate check is a same-payload check, not an id-only one.

    Delivery writes to a stored row, so a resent copy can stop matching it: the
    reason a recovery resend is answered from the row instead (below).
    """
    service, receipts, errors = _stub_delivery(monkeypatch)
    row = _thread_row(f"{path}-refused-thread")
    if path == "subagent_feed":
        _as_subagent(monkeypatch, row)

    await _send_frame(service, path, row, CONTENT)
    await _send_frame(service, path, row, f"{CONTENT} reworded")

    assert receipts and receipts[0]["input_id"] == INPUT_ID
    assert _user_inputs(row["id"]) == 1
    assert any(
        "input identity already has a different payload" in payload.get("message", "")
        for payload in errors
    ), errors


def _rewrite_stored_metadata(message_id: str) -> None:
    """Write to the stored row the way delivery does (an installed-context snapshot)."""
    from infra.database import get_db

    db = get_db()
    db.execute(
        "UPDATE messages SET metadata=json_set(COALESCE(metadata,'{}'),"
        "'$.installed_context','snapshot') WHERE id=?",
        (message_id,),
    )
    db.commit()
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["message", "subagent_feed"])
async def test_a_recovery_resend_of_a_stored_input_is_answered_not_admitted(
    isolated_db, monkeypatch, path
):
    """The answer comes from the stored row, so a row delivery wrote to still settles."""
    service, receipts, errors = _stub_delivery(monkeypatch)
    row = _thread_row(f"{path}-recovered-thread")
    if path == "subagent_feed":
        _as_subagent(monkeypatch, row)

    await _send_frame(service, path, row, CONTENT)
    _rewrite_stored_metadata(INPUT_ID)
    await _send_frame(service, path, row, CONTENT, recovery=True)

    assert not errors, errors
    assert _user_inputs(row["id"]) == 1
    recovered = [r for r in receipts if r.get("type") == "input_recovered"]
    assert len(recovered) == 1, receipts
    stored = _stored(INPUT_ID)
    assert stored is not None
    assert recovered[0]["thread_id"] == row["id"]
    assert recovered[0]["input_id"] == INPUT_ID
    assert recovered[0]["message"]["id"] == INPUT_ID
    assert recovered[0]["message"]["created_at"] == str(stored["created_at"])
    assert "delivery_state" in recovered[0]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["message", "subagent_feed"])
async def test_a_recovery_resend_of_an_absent_input_is_admitted(
    isolated_db, monkeypatch, path
):
    service, receipts, errors = _stub_delivery(monkeypatch)
    row = _thread_row(f"{path}-lost-thread")
    if path == "subagent_feed":
        _as_subagent(monkeypatch, row)

    await _send_frame(service, path, row, CONTENT, recovery=True)

    assert not errors, errors
    assert _user_inputs(row["id"]) == 1
    assert not [r for r in receipts if r.get("type") == "input_recovered"]
    assert receipts and receipts[0]["input_id"] == INPUT_ID
    assert receipts[0].get("duplicate", False) is False


@pytest.mark.asyncio
async def test_a_recovery_resend_reads_only_an_owned_thread(isolated_db, monkeypatch):
    service, receipts, errors = _stub_delivery(monkeypatch)
    row = _thread_row("owned-thread")
    await _send_frame(service, "message", row, CONTENT)
    receipts.clear()

    import ws.message_dispatch as dispatch

    async def run(callback):
        return callback()

    await dispatch.handle_message(
        SimpleNamespace(), {"id": "user-2"}, "user-2",
        {
            "type": "message", "thread_id": row["id"], "content": CONTENT,
            "input_id": INPUT_ID, "recovery": True,
        },
        get_db_fn=lambda: SimpleNamespace(close=lambda: None),
        run_in_db_fn=run,
        get_thread_for_user_fn=lambda *_a, **_k: None,
        touch_thread_fn=lambda *_a, **_k: None,
        send_thread_input_fn=service.send_thread_input,
        handle_cc_main_message_fn=AsyncMock(),
        handle_codex_main_message_fn=AsyncMock(),
    )

    assert not [r for r in receipts if r.get("type") == "input_recovered"]
    assert [e["message"] for e in errors] == ["Thread not found"]
