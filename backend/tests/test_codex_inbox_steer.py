"""Codex true-steer integration over the canonical message FIFO."""

from __future__ import annotations

import pytest

from subagents.codex_inbox_batch import deliver_live_inbox_batch


def _seed_live_turn(thread_id: str):
    from core.turn_core import BOOT_EPOCH
    from infra.database import get_db
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    with get_db() as db:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?, ?, 'codex/gpt-5-codex', 't0', 't0')",
            (thread_id, "u-test"),
        )
        db.commit()
        admission = admit_input(
            thread_id,
            role="user",
            content="running input",
            intent="apply_now",
            message_id=f"initial-{thread_id}",
            turn_id=f"turn-{thread_id}",
            boot_epoch=BOOT_EPOCH,
            admitted_at="t1",
            db=db,
        )
        assert admission.batch is not None
        begin_batch_write(
            admission.batch,
            attempt_id=f"initial-write-{thread_id}",
            started_at="t1",
            db=db,
        )
        settle_batch_write(
            f"initial-write-{thread_id}", outcome="accepted", db=db,
        )
        return admission.batch.identity


def _enqueue(
    thread_id: str,
    *,
    attachments: list[dict] | None = None,
    accepted_effects: dict | None = None,
    content: str = "durable steer input",
    message_id: str | None = None,
    delivery_intent: str = "apply_now",
):
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input

    message_id = message_id or f"message-{thread_id}"
    admission = admit_input(
        thread_id,
        role="user",
        content=content,
        intent=delivery_intent,
        message_id=message_id,
        turn_id=f"unused-{message_id}",
        boot_epoch=BOOT_EPOCH,
        admitted_at="t2",
        metadata={
            "sender_kind": "agent",
            "from_thread": "sender-thread",
            "from_title": "Sender",
        },
        delivery_options={
            "native_mode": "delegation",
            "attachments": attachments or [],
            "reasoning_effort": None,
            "accepted_effects": accepted_effects,
            "broadcast_input": True,
            "touch_session_on_insert": None,
        },
    )
    assert admission.batch is None
    return admission.accepted


def _install_live(thread_id: str, identity, session):
    from subagents import codex_runtime
    from core.fork_codex_context import CodexForkContext

    session.tool_context = CodexForkContext(None, 0)
    peek = getattr(session, "peek_active_usage", None)
    usage = peek() if callable(peek) else None
    if usage is not None:
        counts = {"inputTokens": usage["input_tokens"], "outputTokens": 0}
        session.tool_context.record_receipt({"last": counts, "total": counts})

    live = codex_runtime._LiveCodex(
        codex_thread_id=f"provider-{thread_id}",
        session=session,
        parent_thread_id=None,
        name="codex",
        model="gpt-5-codex",
        catalog_model_id="codex/gpt-5-codex",
        expected_reported_model="gpt-5-codex",
        provider_reported_model="gpt-5-codex",
        corsia_turn=identity,
    )
    codex_runtime._LIVE.put(thread_id, live)
    return live


def _clear_live(thread_id: str) -> None:
    from subagents import codex_runtime

    codex_runtime._LIVE.pop(thread_id, None)
    codex_runtime._APP_SESSIONS.pop(thread_id, None)
    codex_runtime._CODEX_THREADS.pop(thread_id, None)
    codex_runtime._PROVIDER_CONTEXT_UNTRUSTED.discard(thread_id)


def _input_row(message_id: str):
    from infra.database import get_db

    with get_db() as db:
        return db.execute(
            "SELECT id,target_model,active_turn_id,delivery_state,"
            "delivery_attempt_id,write_started_at,write_outcome,delivery_error,seq "
            "FROM messages WHERE id=?",
            (message_id,),
        ).fetchone()


@pytest.mark.asyncio
async def test_joined_wakes_deliver_message_b_after_a_ack(isolated_db, monkeypatch):
    import asyncio

    from infra.db_async import run_in_db
    from infra.send_service import request_safe_boundary
    from subagents import codex_inbox_batch, codex_runtime_event_delivery as delivery

    thread_id = "codex-joined-wake"
    identity = await run_in_db(_seed_live_turn, thread_id)
    receipt_a = await run_in_db(
        lambda: _enqueue(thread_id, content="message A", message_id="joined-a"),
    )
    awaiting_ack = asyncio.Event()
    release_ack = asyncio.Event()
    wrote_b = asyncio.Event()
    calls = []
    deliveries = []

    class Session:
        async def steer(self, text, *, on_write_observed, on_drain_confirmed, input_context=None):
            calls.append(text)
            on_write_observed()
            on_drain_confirmed()
            if len(calls) == 1:
                awaiting_ack.set()
                await release_ack.wait()
            else:
                wrote_b.set()
            return True

    async def deliver(sid):
        deliveries.append(asyncio.current_task())
        return await deliver_live_inbox_batch(sid)

    monkeypatch.setattr(codex_inbox_batch, "deliver_live_inbox_batch", deliver)
    _install_live(thread_id, identity, Session())
    try:
        await request_safe_boundary(thread_id, "codex")
        await asyncio.wait_for(awaiting_ack.wait(), 2)
        first = delivery._SAFE_BOUNDARY_TASKS[thread_id]
        receipt_b = await run_in_db(
            lambda: _enqueue(thread_id, content="message B", message_id="joined-b"),
        )
        for _ in range(20):
            await request_safe_boundary(thread_id, "codex")
        assert delivery._SAFE_BOUNDARY_TASKS[thread_id] is first
        assert len(deliveries) == 1
        release_ack.set()
        assert await asyncio.wait_for(asyncio.shield(first), 2) is True
        await asyncio.wait_for(wrote_b.wait(), 2)
        assert len(deliveries) == 2
        assert deliveries[1] is not first
        assert await asyncio.wait_for(asyncio.shield(deliveries[1]), 2) is True
        await asyncio.sleep(0)
        assert thread_id not in delivery._SAFE_BOUNDARY_TASKS
        assert len(deliveries) == 2
        assert len(calls) == 2 and "message A" in calls[0] and "message B" in calls[1]
        for receipt in (receipt_a, receipt_b):
            row = await run_in_db(_input_row, receipt.message_id)
            assert row["delivery_state"] == "delivered"
    finally:
        release_ack.set()
        task = delivery._SAFE_BOUNDARY_TASKS.get(thread_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        _clear_live(thread_id)


@pytest.mark.asyncio
async def test_live_steer_cannot_write_past_selected_window(isolated_db):
    from infra.database import get_db

    thread_id = "codex-steer-at-cap"
    identity = _seed_live_turn(thread_id)
    with get_db() as db:
        db.execute("INSERT INTO model_profiles(model_id,context_window,updated_at) "
                   "VALUES ('codex/gpt-5-codex',32768,'t') "
                   "ON CONFLICT(model_id) DO UPDATE SET context_window=32768")
        db.commit()
    receipt = _enqueue(thread_id)
    writes = []

    class Session:
        def peek_active_usage(self):
            return {"input_tokens": 32769, "model_context_window": 1_000_000}

        async def steer(self, text, *, on_write_observed, on_drain_confirmed, input_context=None):
            writes.append(text)
            on_write_observed()
            return True

    live = _install_live(thread_id, identity, Session())
    live.context_window = 200
    try:
        assert await deliver_live_inbox_batch(thread_id) is False
    finally:
        _clear_live(thread_id)
    assert writes == []
    row = _input_row(receipt.message_id)
    assert (row["delivery_state"], row["active_turn_id"], row["delivery_attempt_id"], row["seq"]) == (
        "queued", None, None, None,
    )


@pytest.mark.asyncio
async def test_live_codex_steer_settles_exact_fifo_input(
    isolated_db, monkeypatch,
) -> None:
    from subagents import codex_runtime

    thread_id = "codex-steer-success"
    identity = _seed_live_turn(thread_id)
    receipt = _enqueue(thread_id)
    calls: list[str] = []
    broadcasts: list[dict] = []

    class Session:
        async def steer(self, text, *, on_write_observed, on_drain_confirmed, input_context=None):
            calls.append(text)
            on_write_observed()
            on_drain_confirmed()
            return True

    monkeypatch.setattr(
        "infra.state.broadcast_to_thread",
        lambda _sid, payload, **_kwargs: broadcasts.append(payload),
    )
    _install_live(thread_id, identity, Session())
    try:
        assert await deliver_live_inbox_batch(thread_id) is True
    finally:
        _clear_live(thread_id)

    assert len(calls) == 1
    assert "durable steer input" in calls[0]
    assert "AGENT-SEND" in calls[0]
    row = _input_row(receipt.message_id)
    assert (row["delivery_state"], row["delivery_error"], row["seq"]) == (
        "delivered", None, 3,
    )
    projections = [
        payload["message"] for payload in broadcasts
        if payload.get("type") == "message_added"
        and payload["message"]["id"] == receipt.message_id
    ]
    assert [
        (row["id"], row["delivery_state"], row["active_turn_id"])
        for row in projections
    ] == [
        (receipt.message_id, "queued", identity.turn_id),
        (receipt.message_id, "delivered", identity.turn_id),
    ]


@pytest.mark.asyncio
async def test_declined_codex_steer_releases_input_to_fifo(isolated_db) -> None:
    from subagents import codex_runtime

    thread_id = "codex-steer-declined"
    identity = _seed_live_turn(thread_id)
    receipt = _enqueue(thread_id)

    class Session:
        async def steer(self, _text, *, on_write_observed, on_drain_confirmed, input_context=None):
            on_drain_confirmed()
            return False

    _install_live(thread_id, identity, Session())
    try:
        assert await deliver_live_inbox_batch(thread_id) is False
    finally:
        _clear_live(thread_id)

    row = _input_row(receipt.message_id)
    assert row["delivery_state"] == "queued"
    assert row["active_turn_id"] is None
    assert row["delivery_attempt_id"] is None
    assert row["write_started_at"] is None
    assert row["write_outcome"] is None


@pytest.mark.asyncio
async def test_dead_codex_live_generation_settles_before_steering_fifo(
    isolated_db, monkeypatch,
) -> None:
    """A known-dead owner must terminalize before its queued FIFO is retried."""
    from core import turn_core
    from subagents import codex_runtime

    thread_id = "codex-steer-dead"
    identity = _seed_live_turn(thread_id)
    receipt = _enqueue(thread_id)
    settlements = []

    class Session:
        returncode = -9

        async def steer(self, *_args, **_kwargs):
            raise AssertionError("a dead Codex session must not be steered")

    async def _drive(settlement):
        settlements.append(settlement)
        return settlement.result

    monkeypatch.setattr(turn_core, "drive_turn_settlement", _drive)
    monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *_args: None)
    live = _install_live(thread_id, identity, Session())
    codex_runtime._APP_SESSIONS[thread_id] = live.session
    try:
        assert await deliver_live_inbox_batch(thread_id) is False
    finally:
        _clear_live(thread_id)

    assert len(settlements) == 1
    settlement = settlements[0]
    assert settlement.successor is not None
    assert settlement.successor.turn_id != identity.turn_id

    from infra.database import get_db

    with get_db() as db:
        assert db.execute(
            "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
        ).fetchone()[0] == "error"
        row = db.execute(
            "SELECT active_turn_id,delivery_state,delivery_error FROM messages "
            "WHERE id=?", (receipt.message_id,),
        ).fetchone()
    assert tuple(row) == (settlement.successor.turn_id, "queued", None)


@pytest.mark.asyncio
async def test_codex_tool_handler_keeps_new_input_queued(isolated_db) -> None:
    """A busy JSON-RPC tool handler is not a steer-safe provider boundary."""
    from subagents import codex_runtime
    from subagents.codex_app_server import CodexAppServerSession

    thread_id = "codex-steer-tool-handler-busy"
    identity = _seed_live_turn(thread_id)
    receipt = _enqueue(thread_id)
    session = CodexAppServerSession(binary="codex", task=None)
    session.thread_id = "provider-thread"
    session._track_active_turn("provider-turn")
    session._dynamic_tool_requests_idle.clear()

    _install_live(thread_id, identity, session)
    try:
        assert await deliver_live_inbox_batch(thread_id) is False
    finally:
        _clear_live(thread_id)

    row = _input_row(receipt.message_id)
    assert row["delivery_state"] == "queued"
    assert row["active_turn_id"] is None
    assert row["delivery_attempt_id"] is None
    assert row["write_outcome"] is None


@pytest.mark.asyncio
async def test_model_switch_destination_never_steers_source_turn(isolated_db) -> None:
    from infra.model_switches import request_switch
    from subagents import codex_runtime

    thread_id = "codex-steer-model-boundary"
    identity = _seed_live_turn(thread_id)
    request_switch(
        thread_id,
        "codex/gpt-5.1",
        operation_id="switch-1",
        requested_at="t2",
    )
    receipt = _enqueue(thread_id)
    calls: list[str] = []

    class Session:
        async def steer(self, _text, **_kwargs):
            calls.append("steer")
            return True

    _install_live(thread_id, identity, Session())
    try:
        assert await deliver_live_inbox_batch(thread_id) is False
    finally:
        _clear_live(thread_id)

    row = _input_row(receipt.message_id)
    assert calls == []
    assert row["target_model"] == "codex/gpt-5.1"
    assert row["delivery_state"] == "queued"
    assert row["active_turn_id"] is None


@pytest.mark.asyncio
async def test_image_steer_forwards_projection_and_public_effects(
    isolated_db, monkeypatch, tmp_path,
) -> None:
    from PIL import Image

    from core.media_delivery import (
        bind_projection_to_descriptor,
        build_delivery_projection,
    )
    from infra import config, send_effects
    from infra.database import get_db
    from llm.resolution import resolve_execution_target
    from registry import models as registry_models
    from subagents import codex_runtime

    thread_id = "codex-steer-image"
    identity = _seed_live_turn(thread_id)
    with get_db() as db:
        db.execute(
            "UPDATE models SET supports_vision=1 WHERE id='codex/gpt-5-codex'"
        )
        db.commit()
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    image_path = tmp_path / ".threads" / thread_id / "uploads" / "image" / "x.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), color="orange").save(image_path, format="PNG")
    registry_models.register_artifact(
        integration="upload",
        type="upload",
        external_id="image",
        parent_thread_id=thread_id,
        path=str(image_path),
    )
    public = [{
        "file_id": "image",
        "filename": "x.png",
        "mime_type": "image/png",
        "size": image_path.stat().st_size,
        "url": "/api/uploads/u-test/image/x.png",
    }]
    target = resolve_execution_target("codex/gpt-5-codex")
    public, projection = build_delivery_projection(
        public,
        owner_id="u-test",
        thread_id=thread_id,
        target=target,
    )
    receipt = _enqueue(
        thread_id,
        attachments=bind_projection_to_descriptor(public, projection),
        accepted_effects={"uploads": True},
    )
    received = []
    effect_attachments = []

    async def run_effects(**kwargs):
        effect_attachments.extend(kwargs["attachments"])

    monkeypatch.setattr(send_effects, "run_send_accepted_effects", run_effects)

    class Session:
        async def steer(
            self,
            _text,
            *,
            media_projection,
            on_write_observed,
            on_drain_confirmed,
            input_context=None,
        ):
            received.append(media_projection)
            on_write_observed()
            on_drain_confirmed()
            return True

    _install_live(thread_id, identity, Session())
    try:
        assert await deliver_live_inbox_batch(thread_id) is True
    finally:
        _clear_live(thread_id)

    assert received[0].digests == projection.digests
    assert received[0].images[0].data == projection.images[0].data
    assert effect_attachments == public
    assert "_corsia_validated_image_sha256" not in str(effect_attachments)
    assert _input_row(receipt.message_id)["delivery_state"] == "delivered"


@pytest.mark.asyncio
async def test_queue_then_send_steers_one_contiguous_fifo_batch(
    isolated_db, monkeypatch,
) -> None:
    from subagents import codex_runtime

    thread_id = "codex-steer-batch"
    identity = _seed_live_turn(thread_id)
    first = _enqueue(
        thread_id,
        content="first correction",
        message_id="codex-batch-1",
        delivery_intent="queue_after_turn",
    )
    second = _enqueue(
        thread_id,
        content="second correction",
        message_id="codex-batch-2",
    )
    calls: list[str] = []
    input_ids: list[str | None] = []
    broadcasts: list[dict] = []

    class Session:
        async def steer(self, text, *, on_write_observed, on_drain_confirmed, input_context=None):
            calls.append(text)
            input_ids.append(input_context[0] if input_context else None)
            on_write_observed()
            on_drain_confirmed()
            return True

    monkeypatch.setattr(
        "infra.state.broadcast_to_thread",
        lambda _sid, payload, **_kwargs: broadcasts.append(payload),
    )
    _install_live(thread_id, identity, Session())
    try:
        assert await deliver_live_inbox_batch(thread_id) is True
    finally:
        _clear_live(thread_id)

    assert len(calls) == 1
    assert calls[0].index("first correction") < calls[0].index("second correction")
    # clientUserMessageId is the batch's last row: the anchor it settles at.
    assert input_ids == [second.message_id]
    batch_events = [event for event in broadcasts if event["type"] == "messages_added"]
    assert len(batch_events) == 2
    assert [
        [
            (row["id"], row["delivery_state"], row["active_turn_id"])
            for row in event["messages"]
        ]
        for event in batch_events
    ] == [
        [
            (first.message_id, "queued", identity.turn_id),
            (second.message_id, "queued", identity.turn_id),
        ],
        [
            (first.message_id, "delivered", identity.turn_id),
            (second.message_id, "delivered", identity.turn_id),
        ],
    ]
    assert [_input_row(item.message_id)["seq"] for item in (first, second)] == [3, 4]


@pytest.mark.asyncio
async def test_explicit_queue_never_steers_without_later_send(isolated_db) -> None:
    from subagents import codex_runtime

    thread_id = "codex-steer-queue"
    identity = _seed_live_turn(thread_id)
    receipt = _enqueue(
        thread_id,
        content="wait until this turn ends",
        delivery_intent="queue_after_turn",
    )
    calls: list[str] = []

    class Session:
        async def steer(self, text, **_kwargs):
            calls.append(text)
            return True

    _install_live(thread_id, identity, Session())
    try:
        assert await deliver_live_inbox_batch(thread_id) is False
    finally:
        _clear_live(thread_id)

    row = _input_row(receipt.message_id)
    assert calls == []
    assert row["delivery_state"] == "queued"
    assert row["active_turn_id"] is None
