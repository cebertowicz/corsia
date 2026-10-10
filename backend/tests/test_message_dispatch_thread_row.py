from __future__ import annotations

from types import SimpleNamespace

import pytest


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_native_dispatch_fetches_and_forwards_thread_row(monkeypatch):
    import ws.ingress as ingress
    import ws.message_dispatch as message_dispatch

    thread_row = {"id": "thread-1", "reasoning_effort": "high"}
    rows = iter((thread_row, None))
    connections: list[_Connection] = []
    callback_names: list[str] = []
    lookups: list[tuple[str, str, _Connection]] = []
    sends: list[tuple[tuple, dict]] = []
    frames: list[dict] = []

    def get_db() -> _Connection:
        connection = _Connection()
        connections.append(connection)
        return connection

    def get_thread_for_user(
        thread_id: str,
        user_id: str,
        *,
        db: _Connection,
    ):
        lookups.append((thread_id, user_id, db))
        return next(rows)

    async def run_in_db(callback):
        callback_names.append(callback.__name__)
        return callback()

    async def send_thread_input(*args, **kwargs):
        sends.append((args, kwargs))
        return SimpleNamespace(
            message_id="message-1",
            created_at="t",
            turn_id="turn-1",
            turn_order=1,
        )

    async def send_to_client(_ws, payload):
        frames.append(payload)

    monkeypatch.setattr(message_dispatch.state, "send_to_client", send_to_client)
    monkeypatch.setattr(ingress, "send_to_client", send_to_client)
    common = {
        "get_db_fn": get_db,
        "run_in_db_fn": run_in_db,
        "get_thread_for_user_fn": get_thread_for_user,
        "touch_thread_fn": lambda *_args, **_kwargs: None,
        "send_thread_input_fn": send_thread_input,
        "handle_cc_main_message_fn": lambda *_args, **_kwargs: None,
        "handle_codex_main_message_fn": lambda *_args, **_kwargs: None,
    }

    await message_dispatch.handle_message(
        object(),
        {"id": "user-1"},
        "user-1",
        {
            "thread_id": "thread-1",
            "input_id": "input-owned",
            "content": "hello",
        },
        **common,
    )
    await message_dispatch.handle_message(
        object(),
        {"id": "user-1"},
        "user-1",
        {
            "thread_id": "thread-1",
            "input_id": "input-missing",
            "content": "hello",
        },
        **common,
    )

    assert callback_names == ["_fetch_thread_row", "_fetch_thread_row"]
    assert lookups == [
        ("thread-1", "user-1", connections[0]),
        ("thread-1", "user-1", connections[1]),
    ]
    assert all(connection.closed for connection in connections)
    assert len(sends) == 1
    assert sends[0][0][1] is thread_row
    assert sends[0][1]["reasoning_effort"] == "high"
    assert frames[-1] == {
        "type": "error",
        "thread_id": "thread-1",
        "message": "Thread not found",
        "input_id": "input-missing",
    }
