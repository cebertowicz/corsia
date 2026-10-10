from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect


_CLOSE = object()


class _Socket:
    def __init__(self) -> None:
        self.inbound: asyncio.Queue[dict | object] = asyncio.Queue()
        self.sent: list[dict] = []

    async def accept(self) -> None:
        return None

    async def receive_json(self) -> dict:
        frame = await self.inbound.get()
        if frame is _CLOSE:
            raise WebSocketDisconnect()
        assert isinstance(frame, dict)
        return frame

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)


class _BlockedSocket(_Socket):
    def __init__(self) -> None:
        super().__init__()
        self.receipt_started = asyncio.Event()
        self.release_receipt = asyncio.Event()
        self.receipt_completed = asyncio.Event()

    async def send_json(self, frame: dict) -> None:
        self.receipt_started.set()
        await self.release_receipt.wait()
        await super().send_json(frame)
        self.receipt_completed.set()


def _message(input_id: str, thread_id: str, content: str | None = None) -> dict:
    return {
        "type": "message", "thread_id": thread_id, "input_id": input_id,
        "content": content or input_id,
    }


def _main_thread_row(main_db) -> dict:
    return dict(main_db.execute(
        "SELECT * FROM threads WHERE id='main-1'",
    ).fetchone())


def _queued_row(main_db, result) -> dict:
    return dict(main_db.execute(
        "SELECT input_order,delivery_state FROM messages WHERE id=?",
        (result.receipt.message_id,),
    ).fetchone())


@pytest.mark.asyncio
async def test_endpoint_serializes_admission_but_not_provider_work(monkeypatch) -> None:
    """Frames admit FIFO; completed providers cannot release another frame."""
    import ws as ws_mod

    socket = _Socket()
    first_started = asyncio.Event()
    release_first_admission = asyncio.Event()
    provider_started = asyncio.Event()
    release_first_provider = asyncio.Event()
    first_finished = asyncio.Event()
    second_started = asyncio.Event()
    release_second_admission = asyncio.Event()
    third_started = asyncio.Event()
    starts: list[str] = []

    async def handle_message(
        _ws, _user, _user_id, data, *, admission_complete=None,
    ) -> None:
        starts.append(data["input_id"])
        if data["input_id"] == "first":
            first_started.set()
            await release_first_admission.wait()
            if admission_complete is not None:
                admission_complete()
            provider_started.set()
            await release_first_provider.wait()
            first_finished.set()
            return
        if data["input_id"] == "second":
            second_started.set()
            await release_second_admission.wait()
        else:
            third_started.set()
        if admission_complete is not None:
            admission_complete()

    monkeypatch.setattr(
        ws_mod, "_authenticate", lambda _ws: asyncio.sleep(0, result={"id": "steve"}),
    )
    monkeypatch.setattr(ws_mod, "_cleanup_ws", lambda *_args: None)
    monkeypatch.setattr(ws_mod, "_handle_message", handle_message)

    endpoint = asyncio.create_task(ws_mod.websocket_endpoint(socket))
    try:
        for input_id, thread_id in (
            ("first", "blocked-thread"),
            ("second", "working-thread"),
            ("third", "blocked-thread"),
        ):
            await socket.inbound.put(_message(input_id, thread_id))

        await asyncio.wait_for(first_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not second_started.is_set()

        release_first_admission.set()
        await asyncio.wait_for(second_started.wait(), timeout=1)
        assert provider_started.is_set()
        assert starts == ["first", "second"]

        # The first detached task finishes while frame two is still admitting.
        # Its done callback must remain bound to frame one's Event.
        release_first_provider.set()
        await asyncio.wait_for(first_finished.wait(), timeout=1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not third_started.is_set()

        release_second_admission.set()
        await asyncio.wait_for(third_started.wait(), timeout=1)
        assert starts == ["first", "second", "third"]
    finally:
        release_first_admission.set()
        release_first_provider.set()
        release_second_admission.set()
        await socket.inbound.put(_CLOSE)
        await asyncio.wait_for(endpoint, timeout=1)


@pytest.mark.asyncio
async def test_handler_failure_releases_the_next_frame(monkeypatch) -> None:
    import ws as ws_mod

    socket = _Socket()
    second_started = asyncio.Event()
    logged: list[tuple[str, tuple[object, ...]]] = []

    async def handle_message(
        _ws, _user, _user_id, data, *, admission_complete=None,
    ) -> None:
        if data["input_id"] == "failed":
            raise RuntimeError("admission failed")
        second_started.set()
        if admission_complete is not None:
            admission_complete()

    monkeypatch.setattr(
        ws_mod, "_authenticate", lambda _ws: asyncio.sleep(0, result={"id": "steve"}),
    )
    monkeypatch.setattr(ws_mod, "_cleanup_ws", lambda *_args: None)
    monkeypatch.setattr(ws_mod, "_handle_message", handle_message)
    monkeypatch.setattr(
        ws_mod.logger,
        "error",
        lambda message, *args: logged.append((message, args)),
    )

    endpoint = asyncio.create_task(ws_mod.websocket_endpoint(socket))
    try:
        for input_id in ("failed", "next"):
            await socket.inbound.put(_message(input_id, f"{input_id}-thread"))

        await asyncio.wait_for(second_started.wait(), timeout=0.1)
        assert logged
        assert str(logged[0][1][0]) == "admission failed"
    finally:
        endpoint.cancel()
        await asyncio.gather(endpoint, return_exceptions=True)


@pytest.mark.asyncio
async def test_shared_send_receipts_durable_admission_before_provider_work(
    main_db, monkeypatch,
) -> None:
    """The durable receipt is observable before provider work can finish."""
    from infra import send_service
    from infra.send_types import InboxQueuedInput, SendResult

    thread_row = _main_thread_row(main_db)
    admitted = asyncio.Event()
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()
    order: list[str] = []
    monkeypatch.setattr(
        send_service, "broadcast_accepted_input",
        lambda *_args, **_kwargs: order.append("broadcast"),
    )

    async def target_for(*_args, **_kwargs):
        return SimpleNamespace(harness="claude-code", available=True)

    async def execute(batch, **_kwargs):
        order.append("provider")
        provider_started.set()
        await release_provider.wait()
        return SendResult(
            driver="test",
            message_id=batch.items[-1].message_id,
            created_at=batch.items[-1].created_at,
            turn_id=batch.identity.turn_id,
            turn_order=batch.identity.turn_order,
        )

    async def on_admitted(result: InboxQueuedInput) -> None:
        assert _queued_row(main_db, result) == {
            "input_order": 2, "delivery_state": "queued",
        }
        order.append("receipt")
        admitted.set()

    monkeypatch.setattr(send_service, "_target_for", target_for)
    monkeypatch.setattr(send_service, "execute_claimed_batch", execute)
    admission_wait = asyncio.create_task(admitted.wait())
    task = asyncio.create_task(send_service.send_thread_input(
        "main-1", thread_row, "accepted before provider",
        native_mode="event",
        input_id="input-before-provider",
        on_admitted=on_admitted,
    ))
    try:
        completed, _ = await asyncio.wait(
            (task, admission_wait), return_when=asyncio.FIRST_COMPLETED,
        )
        assert admission_wait in completed, task.exception()
        assert order == ["broadcast", "receipt", "provider"]
        assert provider_started.is_set()
        assert not task.done()
    finally:
        release_provider.set()
        await task
        if not admission_wait.done():
            admission_wait.cancel()
        await asyncio.gather(admission_wait, return_exceptions=True)


@pytest.mark.asyncio
async def test_shared_send_receipts_durable_admission_before_switch_work(
    main_db, monkeypatch,
) -> None:
    from infra import send_service
    from infra.send_types import InboxQueuedInput, SendResult
    from thread import model_pending

    thread_row = _main_thread_row(main_db)
    admitted = asyncio.Event()
    switch_started = asyncio.Event()
    release_switch = asyncio.Event()
    order: list[str] = []

    monkeypatch.setattr(
        send_service, "broadcast_accepted_input",
        lambda *_args, **_kwargs: order.append("broadcast"),
    )

    async def target_for(*_args, **_kwargs):
        return SimpleNamespace(harness="claude-code", available=True)

    async def execute(batch, **_kwargs):
        order.append("provider")
        return SendResult(
            driver="test", message_id=batch.items[-1].message_id,
            created_at=batch.items[-1].created_at,
            turn_id=batch.identity.turn_id, turn_order=batch.identity.turn_order,
        )

    async def drive_pending_model_switch(_thread_id: str):
        order.append("switch")
        switch_started.set()
        await release_switch.wait()
        return object()

    async def on_admitted(result: InboxQueuedInput) -> None:
        assert _queued_row(main_db, result) == {
            "input_order": 3, "delivery_state": "queued",
        }
        order.append("receipt")
        admitted.set()

    monkeypatch.setattr(send_service, "_target_for", target_for)
    monkeypatch.setattr(send_service, "execute_claimed_batch", execute)
    monkeypatch.setattr(
        model_pending, "drive_pending_model_switch", drive_pending_model_switch,
    )
    await send_service.send_thread_input(
        "main-1", thread_row, "create active turn",
        native_mode="event", input_id="active-input",
    )
    task = asyncio.create_task(send_service.send_thread_input(
        "main-1", thread_row, "accepted before switch",
        native_mode="event", input_id="input-before-switch",
        on_admitted=on_admitted,
    ))
    try:
        await asyncio.wait_for(admitted.wait(), timeout=1)
        await asyncio.wait_for(switch_started.wait(), timeout=1)
        assert order == ["broadcast", "provider", "broadcast", "receipt", "switch"]
        assert not task.done()
    finally:
        release_switch.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_ws_handler_releases_ingress_on_queued_receipt_without_duplicate_ack() -> None:
    """A blocked receipt cannot delay ingress release or provider dispatch."""
    from infra.send_types import InboxQueuedInput, InputReceipt, SendResult
    from ws.message_dispatch import handle_message

    socket = _BlockedSocket()
    admission_complete = asyncio.Event()
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()
    thread_row = {"id": "thread-1", "reasoning_effort": None}

    class _Db:
        def close(self) -> None:
            return None

    async def run_in_db(call):
        return call()

    async def send_thread_input(*_args, **kwargs):
        receipt = InboxQueuedInput(InputReceipt(
            "thread-1",
            "durable-message-1",
            7,
            1,
            "2026-09-03T06:00:00+00:00",
        ))
        await kwargs["on_admitted"](receipt)
        provider_started.set()
        await release_provider.wait()
        return SendResult(
            driver="test",
            message_id="durable-message-1",
            created_at="2026-09-03T06:00:00+00:00",
            seq=41,
            turn_id="turn-1",
            turn_order=9,
        )

    task = asyncio.create_task(handle_message(
        socket,
        {"id": "user-1"},
        "user-1",
        _message("client-input-1", "thread-1", "persist me first"),
        get_db_fn=_Db,
        run_in_db_fn=run_in_db,
        get_thread_for_user_fn=lambda *_args, **_kwargs: thread_row,
        touch_thread_fn=lambda *_args, **_kwargs: None,
        send_thread_input_fn=send_thread_input,
        handle_cc_main_message_fn=lambda *_args, **_kwargs: None,
        handle_codex_main_message_fn=lambda *_args, **_kwargs: None,
        admission_complete=admission_complete.set,
    ))
    try:
        await asyncio.wait_for(socket.receipt_started.wait(), timeout=1)
        await asyncio.wait_for(admission_complete.wait(), timeout=1)
        await asyncio.wait_for(provider_started.wait(), timeout=0.1)
        assert not task.done()
        assert socket.sent == []
        socket.release_receipt.set()
        await asyncio.wait_for(socket.receipt_completed.wait(), timeout=1)
        assert socket.sent == [{
            "type": "input_queued",
            "queue_kind": "messages",
            "thread_id": "thread-1",
            "input_id": "client-input-1",
            "message_id": "durable-message-1",
            "queue_order": 7,
            "created_at": "2026-09-03T06:00:00+00:00",
            "delivery_state": "queued",
            "delivery_error": None,
            "delivery_revision": 1,
            "duplicate": False,
        }]
    finally:
        socket.release_receipt.set()
        release_provider.set()
        await asyncio.wait_for(task, timeout=1)

    assert [frame["type"] for frame in socket.sent] == ["input_queued"]
