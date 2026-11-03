from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.turn_core import TurnIdentity

from shared_send_fixtures import (
    _empty_tail,
    _insert_session,
    _install_fake_native_context_agent,
    _checkpoint_tail_preflight,
    _messages,
    _pending_compaction,
    _thread_row,
    _set_pending_compaction,
    prepared_context_with_fit_ledger,
)


@pytest.fixture(autouse=True)
def _native_context_agent(monkeypatch):
    _install_fake_native_context_agent(monkeypatch)


def _broadcast_messages(payload: dict) -> list[dict]:
    if payload.get("type") == "messages_added":
        return list(payload["messages"])
    message = payload.get("message")
    return [message] if message is not None else []


@pytest.mark.asyncio
async def test_persist_rechecks_lifecycle_inside_write_transaction(
    isolated_db, monkeypatch
):
    _insert_session(
        "race-target",
        user_id="user-1",
        model="opus",
        spawned_by=None,
    )
    from infra.database import get_db
    from infra.send_service import SendRejected, send_thread_input

    stale_thread_row = _thread_row("race-target")

    async def _archive_during_preflight(**_kwargs):
        db = get_db()
        try:
            db.execute(
                "UPDATE threads SET lifecycle_state='archived', "
                "archived_reason='user' WHERE id='race-target'"
            )
            db.commit()
        finally:
            db.close()
        return {
            "status": "allowed",
            "continuity_mode": "full",
            "model_context_window": 1_000_000,
            "total_tokens": 1,
            "prepared_context": prepared_context_with_fit_ledger(_kwargs["target"]),
        }

    provider_dispatch = AsyncMock()
    monkeypatch.setattr("infra.send_preflight.context_preflight", _archive_during_preflight)
    monkeypatch.setattr("infra.send_dispatch.dispatch_native_ws", provider_dispatch)

    with pytest.raises(SendRejected, match="archived"):
        await send_thread_input(
            "race-target",
            stale_thread_row,
            "must not persist",
            user={"id": "user-1"},
            ws=SimpleNamespace(),
            native_mode="ws",
        )
    provider_dispatch.assert_not_awaited()
    # Admission preserves the user's thought before the lifecycle race. The
    # definitive no-write rejection remains visible as a failed input and can
    # never enter provider history.
    rows = [row for row in _messages("race-target") if row["role"] == "user"]
    assert len(rows) == 1
    assert rows[0]["delivery_state"] == "failed"
    assert "archived" in rows[0]["delivery_error"]


@pytest.mark.asyncio
async def test_shared_send_rejects_missing_target_before_preflight(
    isolated_db, monkeypatch
):
    from infra.send_service import SendRejected, send_thread_input

    preflight = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr("infra.send_preflight.context_preflight", preflight)
    monkeypatch.setattr("infra.send_dispatch.dispatch", dispatch)

    with pytest.raises(SendRejected) as exc_info:
        await send_thread_input(
            "missing-target",
            {},
            "must not persist",
            native_mode="ws",
            ws=SimpleNamespace(),
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "thread does not exist"
    preflight.assert_not_awaited()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_ws_persists_broadcasts_and_starts_browser_turn(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    _insert_session(
        "native-ws",
        user_id="user-1",
        model="oMLX/qwen",
        spawned_by=None,
    )

    from infra import config, state
    from infra.send_service import send_thread_input
    from registry import models

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    path = tmp_path / ".threads" / "native-ws" / "uploads" / "fid" / "proof.txt"
    path.parent.mkdir(parents=True)
    path.write_text("proof")
    models.register_artifact(
        integration="upload",
        type="upload",
        external_id="fid",
        parent_thread_id="native-ws",
        path=str(path),
    )
    class _WS:
        def __init__(self):
            self.sent: list[dict] = []
            self._sent = asyncio.Condition()

        async def send_json(self, payload):
            async with self._sent:
                self.sent.append(payload)
                self._sent.notify_all()

        async def wait_for_delivery_states(
            self, message_id: str, expected: list[str],
        ) -> None:
            def _delivered_states() -> list[str]:
                return [
                    message["delivery_state"]
                    for payload in self.sent
                    for message in _broadcast_messages(payload)
                    if message["id"] == message_id
                ]

            # Wait for as many states as expected; the asserts below check them.
            async with self._sent:
                await self._sent.wait_for(lambda: len(_delivered_states()) >= len(expected))

    ws = _WS()
    peer_ws = _WS()
    order: list[str] = []
    settled = asyncio.Event()

    async def _after_persist(_target):
        assert len(
            [row for row in _messages("native-ws") if row["role"] == "user"]
        ) == 1
        order.append("after")

    async def _execute_turn(
        *,
        turn_id,
        target,
        start_ack=None,
        persist_input_once=None,
        settle_delivery=None,
        preadmitted_turn=None,
        provider_acceptance=None,
        **_kwargs,
    ):
        order.append("execute")
        assert persist_input_once is not None
        await persist_input_once()
        assert len(
            [row for row in _messages("native-ws") if row["role"] == "user"]
        ) == 1
        if start_ack is not None and not start_ack.done():
            start_ack.set_result(preadmitted_turn)
        if provider_acceptance is not None and not provider_acceptance.done():
            provider_acceptance.set_result(True)
        if settle_delivery is not None:
            await settle_delivery()
        settled.set()

    state._ws_thread_clients["native-ws"] = [ws, peer_ws]

    attachments = [
        {
            "file_id": "fid",
            "filename": "proof.txt",
            "mime_type": "text/plain",
        }
    ]
    try:
        with patch("ws.turn.execute_turn", side_effect=_execute_turn) as execute_turn:
            result = await send_thread_input(
                "native-ws",
                _thread_row("native-ws"),
                "clean user text",
                user={"id": "user-1"},
                metadata={"attachments": attachments},
                native_mode="ws",
                ws=ws,
                requested_model_id="oMLX/qwen",
                attachments=attachments,
                is_retry=False,
                input_id="input-native",
                reasoning_effort="high",
                trim_content=False,
                touch_thread_on_insert=True,
                after_persist_before_dispatch=_after_persist,
                broadcast_exclude_ws=ws,
            )
            await settled.wait()
            # Bounded, so a lost frame fails the test instead of hanging the run.
            await asyncio.wait_for(asyncio.gather(
                ws.wait_for_delivery_states(result.message_id, ["queued", "delivered"]),
                peer_ws.wait_for_delivery_states(
                    result.message_id, ["queued", "queued", "delivered"],
                ),
            ), timeout=5)
    finally:
        state._ws_thread_clients.pop("native-ws", None)

    assert result.driver == "turn_engine"
    assert result.turn_id == execute_turn.call_args.kwargs["turn_id"]
    assert result.turn_order == 1
    assert order == ["after", "execute"]

    rows = [row for row in _messages("native-ws") if row["role"] == "user"]
    assert len(rows) == 1
    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "clean user text"
    assert result.created_at
    metadata = json.loads(rows[0]["metadata"])
    assert metadata == {"attachments": attachments, "reply_via_post_update": True}
    # The sender already owns the optimistic queued row. Peers receive that
    # durable queued row, then every viewer receives the claimed row and its
    # settled state.
    sender_updates = [
        message
        for payload in ws.sent
        for message in _broadcast_messages(payload)
        if message["id"] == result.message_id
    ]
    peer_updates = [
        message
        for payload in peer_ws.sent
        for message in _broadcast_messages(payload)
        if message["id"] == result.message_id
    ]
    assert [message["delivery_state"] for message in sender_updates] == [
        "queued",
        "delivered",
    ]
    assert [message["delivery_state"] for message in peer_updates] == [
        "queued", "queued", "delivered",
    ]
    assert peer_updates[0]["active_turn_id"] is None
    assert sender_updates[0]["active_turn_id"] == result.turn_id
    assert peer_updates[1]["active_turn_id"] == result.turn_id
    message = peer_updates[-1]
    assert message["id"] == result.message_id
    assert message["role"] == "user"
    assert message["content"] == "clean user text"
    assert message["metadata"] == {
        "attachments": attachments,
    }
    assert message["created_at"]
    assert result.created_at == message["created_at"]
    assert message["delivery_state"] == "delivered"
    assert message["delivery_error"] is None
    assert message["delivery_revision"] == 2
    assert message["seq"] == 2
    execute_turn.assert_awaited_once()
    kwargs = execute_turn.await_args.kwargs
    assert kwargs["ws"] is ws
    assert kwargs["user"] == {"id": "user-1"}
    assert kwargs["user_id"] == "user-1"
    assert kwargs["thread_id"] == "native-ws"
    assert "clean user text" in kwargs["content"]
    assert "[PASSIVE CONTEXT DIGEST]" in kwargs["content"]
    assert "Current time:" in kwargs["content"]
    assert kwargs["target"].catalog_id == "oMLX/qwen"
    assert kwargs["target"].transport_model == "oMLX/qwen"
    assert kwargs["attachments"] == attachments
    assert kwargs["is_retry"] is False
    assert kwargs["ingress"] == "ws"
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["persist_input_once"] is not None
    assert kwargs["after_persist_before_dispatch"] is None
    assert "native-ws" not in state._active_turn_tasks


@pytest.mark.asyncio
async def test_native_ws_admission_returns_before_provider_response(
    isolated_db,
):
    from infra.send_service import send_thread_input

    for thread_id in ("native-proof-first", "native-proof-second"):
        _insert_session(
            thread_id,
            user_id="user-1",
            model="oMLX/qwen",
            spawned_by=None,
        )

    release_first = asyncio.Event()
    first_provider_entered = asyncio.Event()
    first_finished = asyncio.Event()

    async def _execute_turn(
        *,
        thread_id,
        start_ack,
        provider_acceptance,
        persist_input_once,
        preadmitted_turn,
        settle_delivery,
        **_kwargs,
    ):
        await persist_input_once()
        start_ack.set_result(preadmitted_turn)
        if thread_id == "native-proof-first":
            first_provider_entered.set()
            await release_first.wait()
        provider_acceptance.set_result(True)
        await settle_delivery()
        if thread_id == "native-proof-first":
            first_finished.set()

    with patch("ws.turn.execute_turn", side_effect=_execute_turn):
        first = await send_thread_input(
            "native-proof-first",
            _thread_row("native-proof-first"),
            "first input",
            user={"id": "user-1"},
            native_mode="ws",
            ws=SimpleNamespace(),
        )
        await first_provider_entered.wait()
        assert first.turn_task is None
        first_row = _messages("native-proof-first")[0]
        assert first_row["delivery_state"] == "queued"
        assert first_row["delivery_error"] is None

        second = await send_thread_input(
            "native-proof-second",
            _thread_row("native-proof-second"),
            "second input",
            user={"id": "user-1"},
            native_mode="ws",
            ws=SimpleNamespace(),
        )
        assert second.turn_id is not None

        release_first.set()
        await first_finished.wait()

    accepted_row = _messages("native-proof-first")[0]
    assert accepted_row["delivery_state"] == "delivered"
    assert accepted_row["delivery_error"] is None


@pytest.mark.asyncio
async def test_native_ws_caller_cancel_after_admission_does_not_terminalize_turn(
    isolated_db,
):
    from core.turn_core import persist_turn_terminal
    from infra.database import get_db
    from infra.send_service import send_thread_input

    thread_id = "native-cancel-after-admission"
    _insert_session(
        thread_id,
        user_id="user-1",
        model="oMLX/qwen",
        spawned_by=None,
    )
    persisted = asyncio.Event()
    release_start = asyncio.Event()
    release_provider = asyncio.Event()

    async def _execute_turn(
        *,
        start_ack,
        provider_acceptance,
        persist_input_once,
        preadmitted_turn,
        settle_delivery,
        **_kwargs,
    ):
        await persist_input_once()
        persisted.set()
        await release_start.wait()
        start_ack.set_result(preadmitted_turn)
        await release_provider.wait()
        provider_acceptance.set_result(True)
        await settle_delivery()

    with patch("ws.turn.execute_turn", side_effect=_execute_turn):
        send = asyncio.create_task(
            send_thread_input(
                thread_id,
                _thread_row(thread_id),
                "cancel caller only",
                user={"id": "user-1"},
                native_mode="ws",
                ws=SimpleNamespace(),
            )
        )
        await persisted.wait()
        send.cancel()
        release_start.set()
        with pytest.raises(asyncio.CancelledError):
            await send

        db = get_db()
        try:
            turn = db.execute(
                "SELECT id,status FROM active_turns WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
        finally:
            db.close()
        assert turn["status"] == "running"
        row = _messages(thread_id)[0]
        assert row["delivery_state"] == "queued"
        assert row["delivery_error"] is None

        release_provider.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            row = _messages(thread_id)[0]
            if row["delivery_state"] == "delivered":
                break
        assert row["delivery_state"] == "delivered"
        persist_turn_terminal(turn["id"], "completed")


@pytest.mark.asyncio
async def test_native_ws_accepted_cancellation_finalizes_committed_send_once(
    isolated_db, monkeypatch,
):
    thread_id = "native-ws-cancel-finalize"
    _insert_session(
        thread_id,
        user_id="user-1",
        model="oMLX/qwen",
        spawned_by=None,
    )
    _set_pending_compaction(thread_id)

    from infra import send_service, state, send_preflight
    from ws import turn as ws_turn

    broadcasts: list[dict] = []
    turn_started = asyncio.Event()
    release_acceptance = asyncio.Event()

    async def _execute_turn(
        *,
        turn_id,
        start_ack,
        provider_acceptance,
        persist_input_once=None,
        settle_delivery=None,
        **_kwargs,
    ):
        assert persist_input_once is not None
        await persist_input_once()
        turn_started.set()
        await release_acceptance.wait()
        provider_acceptance.set_result(True)
        await settle_delivery()
        start_ack.set_result(TurnIdentity(turn_id, 1))

    monkeypatch.setattr(
        send_preflight, "context_preflight", _checkpoint_tail_preflight,
    )
    monkeypatch.setattr(ws_turn, "execute_turn", _execute_turn)
    monkeypatch.setattr(
        state,
        "broadcast_to_thread",
        lambda _sid, payload, **_kwargs: broadcasts.append(payload),
    )

    task = asyncio.create_task(send_service.send_thread_input(
        thread_id,
        _thread_row(thread_id),
        "native accepted then cancelled",
        user={"id": "user-1"},
        native_mode="ws",
        ws=SimpleNamespace(),
    ))
    await turn_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release_acceptance.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _pending_compaction(thread_id) == "manual_semantic"
    rows = [row for row in _messages(thread_id) if row["role"] == "user"]
    assert [row["role"] for row in rows] == ["user"]
    assert rows[0]["delivery_state"] == "delivered"
    assert rows[0]["delivery_revision"] == 2
    # The canonical row is announced on durable admission, claimed by the
    # exact Turn, then updated in place after provider acceptance even when the
    # caller cancels its wait.
    user_broadcasts = [
        message
        for payload in broadcasts
        for message in _broadcast_messages(payload)
        if message["content"] == "native accepted then cancelled"
    ]
    claimed_turn_id = user_broadcasts[1]["active_turn_id"]
    assert claimed_turn_id
    assert [
        (
            message["delivery_state"],
            message["delivery_revision"],
            message["active_turn_id"],
        )
        for message in user_broadcasts
    ] == [
        ("queued", 1, None),
        ("queued", 1, claimed_turn_id),
        ("delivered", 2, claimed_turn_id),
    ]


@pytest.mark.asyncio
async def test_send_preflight_renders_exact_destination_payload(monkeypatch):
    import infra.preflight_render as preflight_render
    from core.context_payload import (
        PreparedContextPayload,
        RenderedDestinationPayload,
    )
    from core.destination_preflight import attach_destination_fit_ledger
    from infra import send_service, send_preflight

    exact = SimpleNamespace(mode="exact", tail=_empty_tail())
    candidates = SimpleNamespace(
        exact=exact,
        checkpoint_tail=None,
        history_epoch=4,
        sealed_boundary=None,
        last_resort=exact,
    )
    # Only the candidate read is stubbed; native never has a provider handle,
    # so the real provider-resume decision returns without touching the DB.
    monkeypatch.setattr(
        send_preflight,
        "run_in_db",
        AsyncMock(side_effect=lambda fn, *args, **kwargs: (
            candidates if fn is send_preflight._read_candidates
            else fn(*args, **kwargs)
        )),
    )
    from llm.resolution import ExecutionTarget

    target = ExecutionTarget(
        catalog_id="codex/gpt-5-codex", harness="codex", provider="codex",
        backend="codex", transport_model="gpt-5-codex",
        expected_reported_model="gpt-5-codex", context_window=200000,
        available=True, availability_reason=None,
        display_name="GPT-5 Codex", supports_reasoning=True,
    )
    prepared = PreparedContextPayload(
        mode="exact",
        target_catalog_id=target.catalog_id,
        model_context_window=target.context_window,
        history_epoch=4,
        sealed_boundary=None,
        replay=exact,
        rendered=RenderedDestinationPayload(
            payload={"history": ["prepared dispatch history"]},
            input_tokens=100,
            accounting_payload={"history": ["prepared dispatch history"]},
            fit_section_tokens={
                "fixed": 0,
                "checkpoint": 0,
                "tail": 0,
                "current_input": 0,
                "tools": 0,
                "provider_wrapper": 100,
            },
            fit_input_identity="next-input:send-window",
            fit_current_input="next turn",
            fit_components={"payload": "prepared dispatch history"},
        ),
    )
    prepared = attach_destination_fit_ledger(
        prepared,
        harness=target.harness,
        proven_target_window_tokens=target.context_window,
    )
    render = AsyncMock(return_value=prepared)
    monkeypatch.setattr(preflight_render, "render_preflight_selection", render)

    thread_row = {"id": "send-window", "model": target.catalog_id}
    runtime_truth = {
        "codex_live": False,
        "codex_has_thread": False,
        "codex_provider_context_untrusted": False,
    }
    result = await send_preflight.context_preflight(
        thread_id="send-window",
        thread_row=thread_row,
        provider="codex",
        target=target,
        runtime_truth=runtime_truth,
        current_user_content="next turn",
        attachments=[{"filename": "proof.txt"}],
    )

    assert result["status"] == "allowed"
    assert result["continuity_mode"] == "exact"
    assert result["total_tokens"] == 100
    render.assert_awaited_once_with(
        thread_id="send-window",
        thread_row=thread_row,
        provider="codex",
        target=target,
        current_user_content="next turn",
        current_user_source_content="next turn",
        agent_envelope=None,
        attachments=[{"filename": "proof.txt"}],
        media_projection=None,
        selection=exact,
        live_handle=False,
        input_identity="next-input:send-window",
    )


@pytest.mark.asyncio
async def test_native_ws_rejection_removes_accepted_input_row(
    isolated_db, monkeypatch
):
    _insert_session(
        "native-ws-error",
        user_id="user-1",
        model="oMLX/qwen",
        spawned_by=None,
        project_id="proj-1",
    )

    from infra import state
    from infra.send_service import SendRejected, send_thread_input

    broadcasts: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        state,
        "broadcast_to_thread",
        lambda thread_id, payload: broadcasts.append((thread_id, payload)),
    )

    with pytest.raises(SendRejected, match="Native WebSocket send missing socket"):
        await send_thread_input(
            "native-ws-error",
            _thread_row("native-ws-error"),
            "clean user text",
            user={"id": "user-1"},
            native_mode="ws",
            ws=None,
            trim_content=False,
            touch_thread_on_insert=True,
        )

    assert _messages("native-ws-error") == []
    assert broadcasts == []
