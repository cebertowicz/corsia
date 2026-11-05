"""Account-limit admission retains FIFO work without creating a Turn (#2477)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from connections import harness_usage
from infra.database import get_db
from infra.thread_driver import admit_input, finish_turn
from infra.thread_recovery import recover_threads
from infra.thread_input_types import ClaimedBatch, HeldAdmission
from infra.thread_inputs import start_idle_turn
from vendor_account_fixtures import default_receipt

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()


def _thread(db, thread_id: str, model: str) -> dict:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, 'user', ?, 't', 't')",
        (thread_id, model),
    )
    db.commit()
    return dict(db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone())


def _admit(db, thread_id: str, message_id: str):
    return admit_input(
        thread_id,
        role="user",
        content="work",
        intent="apply_now",
        message_id=message_id,
        turn_id=f"turn-{message_id}",
        boot_epoch="boot",
        admitted_at="t",
        db=db,
    )


def _queued_without_turn(db, message_id: str) -> None:
    row = db.execute(
        "SELECT delivery_state,active_turn_id FROM messages WHERE id=?", (message_id,),
    ).fetchone()
    assert tuple(row) == ("queued", None)
    assert db.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0] == 0


def test_limited_admission_holds_fifo_without_mutating_turn_state(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db, "sonnet", "claude-code/sonnet")
        reset = _future()
        harness_usage.record_limited(
            provider="claude-code", reason="weekly", resets_at=reset,
            source="test", db=db, receipt=default_receipt("claude-code", db=db),
        )

        admission = _admit(db, "sonnet", "message-1")

        assert isinstance(admission.batch, HeldAdmission)
        assert admission.batch == HeldAdmission(
            "claude-code", "weekly", reset, "sonnet", "claude-code/sonnet",
        )
        _queued_without_turn(db, "message-1")
    finally:
        db.close()


@pytest.mark.parametrize("state", ["allowed", "unknown"])
def test_allowed_and_unknown_availability_still_start(state: str, isolated_db) -> None:
    db = get_db()
    try:
        _thread(db, state, "claude-code/opus")
        if state == "allowed":
            harness_usage.record_allowed(provider="claude-code", source="test", db=db, receipt=default_receipt("claude-code", db=db))

        admission = _admit(db, state, f"message-{state}")

        assert isinstance(admission.batch, ClaimedBatch)
        assert admission.batch.target_model == "claude-code/opus"
    finally:
        db.close()


def test_scoped_sonnet_limit_does_not_hold_opus(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db, "sonnet", "claude-code/sonnet")
        _thread(db, "opus", "claude-code/opus")
        harness_usage.record_limited(
            provider="claude-code", reason="sonnet", resets_at=_future(),
            source="test", scope_model="sonnet", db=db, receipt=default_receipt("claude-code", db=db),
        )

        sonnet = _admit(db, "sonnet", "sonnet-message")
        opus = _admit(db, "opus", "opus-message")

        assert isinstance(sonnet.batch, HeldAdmission)
        assert isinstance(opus.batch, ClaimedBatch)
    finally:
        db.close()


def test_completed_turn_does_not_mark_idle_when_successor_is_held(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db, "thread", "claude-code/sonnet")
        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
            "VALUES ('current','thread',0,'claude-code/sonnet','running','t','boot')",
        )
        db.commit()
        _admit(db, "thread", "successor")
        harness_usage.record_limited(
            provider="claude-code", reason="weekly", resets_at=_future(),
            source="test", db=db, receipt=default_receipt("claude-code", db=db),
        )

        settlement = finish_turn(
            "current", outcome="completed", ended_at="later", successor_turn_id="next",
            boot_epoch="boot", db=db,
        )

        assert isinstance(settlement.successor, HeldAdmission)
        assert settlement.idle_checkpoint is None
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='current'",
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_boot_recovery_retains_limited_fifo_prefix(isolated_db) -> None:
    db = get_db()
    try:
        _thread(db, "thread", "codex/gpt-5-codex")
        harness_usage.record_limited(
            provider="codex", reason="plan", resets_at=None, source="test", db=db, receipt=default_receipt("codex", db=db),
        )
        from infra.input_admission import accept_thread_input

        accept_thread_input(
            "thread", role="user", content="work", intent="apply_now", message_id="message", db=db,
        )
        db.commit()

        recovery = recover_threads(
            boot_epoch="reboot", recovered_at="later", successor_id=lambda _thread: "next", db=db,
        )

        assert len(recovery.batches) == 1
        assert isinstance(recovery.batches[0], HeldAdmission)
        _queued_without_turn(db, "message")
    finally:
        db.close()


def test_model_switch_releases_held_prefix_to_allowed_destination(isolated_db, monkeypatch) -> None:
    from infra.database import write_transaction
    from infra.input_admission import accept_thread_input
    from infra.model_switches import activate_ready_switch, request_switch

    db = get_db()
    try:
        _thread(db, "thread", "claude-code/sonnet")
        accept_thread_input(
            "thread", role="user", content="work", intent="apply_now", message_id="message", db=db,
        )
        harness_usage.record_limited(
            provider="claude-code", reason="sonnet", resets_at=_future(),
            source="test", scope_model="sonnet", db=db, receipt=default_receipt("claude-code", db=db),
        )
        db.commit()
        assert isinstance(
            start_idle_turn("thread", turn_id="held", boot_epoch="boot", started_at="t", db=db),
            HeldAdmission,
        )
        request_switch(
            "thread", "claude-code/opus", operation_id="switch", requested_at="t", db=db,
        )
        with write_transaction(db, scope="held_switch") as connection:
            connection.execute("UPDATE threads SET switch_status='ready' WHERE id='thread'")
            assert activate_ready_switch(connection, "thread", committed_at="t") is not None
        db.commit()

        seen: list[str] = []
        original = harness_usage.connection_availability

        def observed(model, *, db, **kwargs):
            seen.append(model)
            return original(model, db=db, **kwargs)

        monkeypatch.setattr("infra.held_admission.connection_availability", observed)
        released = start_idle_turn(
            "thread", turn_id="released", boot_epoch="boot", started_at="later", db=db,
        )

        assert isinstance(released, ClaimedBatch)
        assert released.target_model == "claude-code/opus"
        assert seen == ["claude-code/opus"]
    finally:
        db.close()


def test_held_line_is_shared_by_send_and_delegation_surfaces(isolated_db) -> None:
    from api.delegation import _queued_send_response
    from infra.send_types import InboxQueuedInput, InputReceipt
    from tools.delegation import _queued_result

    held = HeldAdmission(
        "codex", "plan", "2026-10-01T10:30:00+00:00", "thread", "codex/gpt-5.6-terra",
    )
    queued = InboxQueuedInput(InputReceipt("thread", "message", 1, 1, "t"), held=held)
    reset = datetime.fromisoformat(held.resets_at).astimezone()
    expected = f"Held: account limit on codex, resets {reset:%b} {reset.day}, {reset:%Y at %-I:%M %p %Z}."

    assert queued.held_line == expected
    assert _queued_result("thread", queued)["held"] == expected
    assert _queued_send_response("thread", queued)["held"] == expected
    unknown = InboxQueuedInput(
        queued.receipt,
        held=HeldAdmission("claude-code", "weekly", None, "thread", "claude-code/sonnet"),
    )
    assert unknown.held_line == "Held: account limit on claude-code; reset time unknown."


@pytest.mark.asyncio
async def test_send_returns_typed_held_inbox_result(isolated_db) -> None:
    from infra.send_service import send_thread_input
    from infra.send_types import InboxQueuedInput

    db = get_db()
    try:
        thread = _thread(db, "thread", "claude-code/sonnet")
        harness_usage.record_limited(
            provider="claude-code", reason="weekly", resets_at=None, source="test", db=db, receipt=default_receipt("claude-code", db=db),
        )
    finally:
        db.close()

    result = await send_thread_input(
        "thread", thread, "work", user={"id": "user"}, input_id="message", native_mode="delegation",
    )

    assert isinstance(result, InboxQueuedInput)
    assert result.held is not None
    assert result.held_line == "Held: account limit on claude-code; reset time unknown."


@pytest.mark.asyncio
async def test_delegate_status_shows_the_same_held_line(isolated_db, monkeypatch) -> None:
    import json
    import tools.delegation as delegation

    db = get_db()
    try:
        _thread(db, "thread", "claude-code/sonnet")
        _thread(db, "sender", "claude-code/opus")
        harness_usage.record_limited(
            provider="claude-code", reason="weekly", resets_at=None, source="test", db=db, receipt=default_receipt("claude-code", db=db),
        )
        _admit(db, "thread", "message")
    finally:
        db.close()
    monkeypatch.setattr(
        delegation,
        "_current_thread_context",
        lambda _thread_id: {"id": "sender", "user_id": "user"},
    )
    monkeypatch.setattr(
        delegation,
        "run_in_db",
        AsyncMock(side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)),
    )

    result = await delegation.DelegateStatusTool().execute("thread", _thread_id="sender")

    assert json.loads(result)["held"] == "Held: account limit on claude-code; reset time unknown."


def test_delegate_status_uses_the_same_held_line(isolated_db) -> None:
    from infra.delegation_status import held_admission_status

    db = get_db()
    try:
        thread = _thread(db, "thread", "claude-code/sonnet")
        harness_usage.record_limited(
            provider="claude-code", reason="weekly", resets_at=None, source="test", db=db, receipt=default_receipt("claude-code", db=db),
        )
        _admit(db, "thread", "message")

        held = held_admission_status(db, thread, None)

        assert held is not None
        from infra.thread_input_types import held_admission_line

        assert held_admission_line(held) == "Held: account limit on claude-code; reset time unknown."
    finally:
        db.close()


@pytest.mark.asyncio
async def test_held_admission_drives_a_pending_model_switch(isolated_db, monkeypatch) -> None:
    from infra.turn_admission_driver import drive_turn_admission

    drive = AsyncMock(return_value=None)
    monkeypatch.setattr("thread.model_pending.drive_pending_model_switch", drive)

    held = HeldAdmission("claude-code", None, None, "thread", "claude-code/sonnet")
    assert await drive_turn_admission(held) is None
    drive.assert_awaited_once_with("thread")


@pytest.mark.asyncio
async def test_restart_drives_the_pending_switch_of_a_held_thread(isolated_db, monkeypatch) -> None:
    from types import SimpleNamespace

    from automation.restart_startup import drive_startup_continuity

    drive = AsyncMock(return_value=None)
    monkeypatch.setattr("thread.model_pending.drive_pending_model_switch", drive)
    monkeypatch.setattr("automation.restart_startup._replay_held_webhooks", AsyncMock())
    held = HeldAdmission("claude-code", None, None, "thread", "claude-code/sonnet")
    recovered = SimpleNamespace(batches=[held], settlements=[], ended_turns=())

    await drive_startup_continuity(recovered, {"thread"}, SimpleNamespace(
        info=lambda *_args: None, exception=lambda *_args: None,
    ))

    drive.assert_awaited_once_with("thread")


@pytest.mark.asyncio
async def test_send_to_a_held_thread_drives_its_pending_switch(isolated_db, monkeypatch) -> None:
    from infra.send_service import send_thread_input

    drive = AsyncMock(return_value=None)
    monkeypatch.setattr("thread.model_pending.drive_pending_model_switch", drive)
    db = get_db()
    try:
        thread = _thread(db, "thread", "claude-code/sonnet")
        harness_usage.record_limited(
            provider="claude-code", reason="weekly", resets_at=None, source="test", db=db, receipt=default_receipt("claude-code", db=db),
        )
    finally:
        db.close()

    await send_thread_input(
        "thread", thread, "work", user={"id": "user"}, input_id="message", native_mode="delegation",
    )

    drive.assert_awaited_once_with("thread")
