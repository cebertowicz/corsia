"""Restart drain contracts against the canonical durable Turn owner."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest


def _active_turn(model: str = "oMLX/qwen") -> tuple[str, str]:
    from infra.database import get_db
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    thread_id = "restart-thread"
    turn_id = "restart-turn"
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?,?,?,?,?)",
            (thread_id, "user-1", model, "t0", "t0"),
        )
        db.commit()
        admission = admit_input(
            thread_id,
            role="user",
            content="finish before restart",
            intent="apply_now",
            message_id="restart-input",
            turn_id=turn_id,
            boot_epoch="boot-1",
            admitted_at="t1",
            db=db,
        )
        assert admission.batch is not None
        begin_batch_write(
            admission.batch,
            attempt_id="restart-write",
            started_at="t1",
            db=db,
        )
        settle_batch_write("restart-write", outcome="accepted", db=db)
        db.commit()
    finally:
        db.close()
    return thread_id, turn_id


def _finish(turn_id: str, outcome: str = "completed") -> None:
    from core.turn_core import BOOT_EPOCH
    from infra.database import get_db
    from infra.thread_driver import finish_turn

    db = get_db()
    try:
        finish_turn(
            turn_id,
            outcome=outcome,
            ended_at="t2",
            successor_turn_id="unused-successor",
            boot_epoch=BOOT_EPOCH,
            db=db,
        )
    finally:
        db.close()


def _without_projection(monkeypatch) -> None:
    from infra import drain

    monkeypatch.setattr(drain, "start_drain_projection", lambda _reason: {})
    monkeypatch.setattr(drain, "cancel_drain_projection", lambda: None)


def test_restart_cutoff_atomically_holds_an_idle_fifo_from_steering(
    isolated_db,
) -> None:
    """A committed steering cutoff wins before any later idle Turn claim."""
    from automation import self_restart
    from infra.database import get_db
    from infra.thread_driver import admit_input

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('held-thread','user-1','oMLX/qwen','t0','t0')"
        )
        operation_id = self_restart.try_acquire(
            None, None, None, None, "test restart", db=db,
        )
        assert operation_id is not None
        assert self_restart.transition_operation(
            operation_id, expected="holding", target="steering", db=db,
        )
        admission = admit_input(
            "held-thread", role="user", content="wait", intent="apply_now",
            message_id="held-input", turn_id="must-not-start",
            boot_epoch="boot-1", admitted_at="t1", db=db,
        )
        assert admission.held_for_restart is True
        assert admission.batch is None
        row = db.execute(
            "SELECT delivery_state,active_turn_id FROM messages "
            "WHERE id='held-input'"
        ).fetchone()
        assert tuple(row) == ("queued", None)
        assert db.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_turn_admitted_during_holding_is_captured_by_the_later_cutoff(
    isolated_db,
    monkeypatch,
) -> None:
    """CLI pre-acquire reserves first; steering later captures admitted work."""
    from automation import restart, self_restart
    from infra import drain

    _without_projection(monkeypatch)
    operation_id = self_restart.try_acquire(
        None, None, None, None, "test restart",
    )
    assert operation_id is not None
    assert self_restart.restart_holds_new_work() is False
    assert drain.is_draining() is False
    thread_id, turn_id = _active_turn()

    async def decline(_turn: dict, _message: str) -> bool:
        return False

    async def stop(turn: dict) -> bool:
        assert (turn["thread_id"], turn["turn_id"]) == (thread_id, turn_id)
        _finish(turn_id, "cancelled")
        return True

    monkeypatch.setattr(restart, "_steer_turn", decline)
    monkeypatch.setattr(restart, "_force_stop_turn", stop)

    started = await restart.begin_restart_drain(
        "test restart",
        operation_id=operation_id,
        allow_hard_cap=True,
    )
    assert self_restart.restart_holds_new_work() is True
    assert drain.is_draining() is True
    assert started["uncontrolled"] == [thread_id]

    result = await restart.enforce_restart_hard_cap(operation_id)
    assert result == {"force_stop_ready": True, "failures": []}


@pytest.mark.asyncio
async def test_drain_tracks_exact_turn_until_terminal(
    isolated_db,
    monkeypatch,
) -> None:
    """Restart readiness is the durable Turn count, not a sweeper sample."""
    from automation import restart, self_restart

    _without_projection(monkeypatch)
    thread_id, turn_id = _active_turn()
    steered: list[tuple[str, str]] = []

    async def steer(turn: dict, _message: str) -> bool:
        steered.append((turn["thread_id"], turn["turn_id"]))
        return True

    monkeypatch.setattr(restart, "_steer_turn", steer)
    result = await restart.begin_restart_drain("test restart")

    operation_id = result["operation_id"]
    assert steered == [(thread_id, turn_id)]
    assert result["controlled"] == [thread_id]
    assert self_restart.operation_by_id(operation_id)["phase"] == "draining"
    assert restart.advance_restart_readiness(operation_id) is False

    _finish(turn_id)

    assert restart.advance_restart_readiness(operation_id) is True
    assert self_restart.operation_by_id(operation_id)["phase"] == "ready"


@pytest.mark.asyncio
async def test_unbound_drain_refuses_an_existing_operation(
    isolated_db,
    monkeypatch,
) -> None:
    """A helper without an ID acquires its own row instead of adopting another."""
    from automation import restart, self_restart

    _without_projection(monkeypatch)
    existing = self_restart.try_acquire(
        None, None, None, None, "existing restart",
    )
    assert existing is not None

    with pytest.raises(RuntimeError, match="could not acquire restart cutoff"):
        await restart.begin_restart_drain("independent helper")

    operation = self_restart.operation_by_id(existing)
    assert operation["status"] == "in_flight"
    assert operation["phase"] == "holding"


@pytest.mark.asyncio
async def test_replaced_operation_is_refused_before_drain_effects(
    isolated_db,
    monkeypatch,
) -> None:
    """An ID replacement cannot register projection or steer provider work."""
    from automation import restart, self_restart
    from infra import drain

    first = self_restart.try_acquire(None, None, None, None, "first restart")
    assert first is not None
    assert self_restart.abort_operation(first, error="replaced")
    replacement = self_restart.try_acquire(
        None, None, None, None, "replacement restart",
    )
    assert replacement is not None
    assert self_restart.transition_operation(
        replacement, expected="holding", target="steering",
    )
    events: list[str] = []

    def snapshot_active_turns() -> list[dict]:
        events.append("snapshot")
        return []

    monkeypatch.setattr(restart, "_restart_operation", lambda *_args: first)
    monkeypatch.setattr(
        drain,
        "start_drain_projection",
        lambda _reason: events.append("projection"),
    )
    monkeypatch.setattr(
        restart,
        "_snapshot_active_turns",
        snapshot_active_turns,
    )

    with pytest.raises(RuntimeError, match="restart operation changed"):
        await restart.begin_restart_drain("first restart", operation_id=first)

    assert events == []
    assert self_restart.in_flight_request()["id"] == replacement


@pytest.mark.asyncio
async def test_pending_steering_cannot_advance_or_hard_cap(
    isolated_db,
    monkeypatch,
) -> None:
    """Provider decisions settle before readiness or force-stop can take ownership."""
    from automation import restart, self_restart

    _without_projection(monkeypatch)
    operation_id = self_restart.try_acquire(
        None, None, None, None, "test restart",
    )
    assert operation_id is not None
    assert self_restart.transition_operation(
        operation_id, expected="holding", target="steering",
    )

    assert restart.advance_restart_readiness(operation_id) is False
    with pytest.raises(RuntimeError, match="steering is still pending"):
        await restart.enforce_restart_hard_cap(operation_id)


@pytest.mark.asyncio
async def test_steering_attempts_are_concurrent_and_whole_call_bounded(
    isolated_db,
    monkeypatch,
) -> None:
    """Transition locks, pipe drain, and provider ACK share one finite bound."""
    from automation import restart, self_restart

    _without_projection(monkeypatch)
    turns = [
        {
            "turn_id": f"turn-{index}",
            "turn_order": index,
            "thread_id": f"thread-{index}",
            "execution_class": "codex",
        }
        for index in range(2)
    ]
    entered: list[str] = []

    async def never_settles(turn: dict, _message: str) -> bool:
        entered.append(turn["thread_id"])
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(restart, "_snapshot_active_turns", lambda: turns)
    monkeypatch.setattr(restart, "_steer_turn", never_settles)
    monkeypatch.setattr(restart, "STEER_TIMEOUT_SECONDS", 0.01, raising=False)

    result = await asyncio.wait_for(
        restart.begin_restart_drain("test restart", allow_hard_cap=True),
        timeout=0.2,
    )

    assert entered == ["thread-0", "thread-1"]
    assert result["controlled"] == []
    assert result["uncontrolled"] == ["thread-0", "thread-1"]
    assert self_restart.operation_by_id(result["operation_id"])["phase"] == "draining"


@pytest.mark.asyncio
async def test_parent_cancellation_settles_every_steering_task(
    isolated_db,
    monkeypatch,
) -> None:
    """The finite steering owner cannot leave provider tasks running behind it."""
    from automation import restart

    turns = [
        {
            "turn_id": f"turn-{index}",
            "turn_order": index,
            "thread_id": f"thread-{index}",
            "execution_class": "codex",
        }
        for index in range(2)
    ]
    release = asyncio.Event()
    all_entered = asyncio.Event()
    entered: set[str] = set()
    settled: list[str] = []

    async def waits_for_release(turn: dict, _message: str) -> bool:
        entered.add(turn["turn_id"])
        if len(entered) == len(turns):
            all_entered.set()
        try:
            await release.wait()
            return True
        finally:
            settled.append(turn["turn_id"])

    monkeypatch.setattr(restart, "_steer_turn", waits_for_release)
    owner = asyncio.create_task(restart._steer_turns(turns))
    try:
        await asyncio.wait_for(all_entered.wait(), timeout=0.2)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert sorted(settled) == ["turn-0", "turn-1"]
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancelled_drain_releases_its_exact_durable_operation(
    isolated_db,
    monkeypatch,
) -> None:
    """Cancellation cannot strand ingress after the finite owner disappears."""
    from automation import restart, self_restart

    _without_projection(monkeypatch)
    turns = [
        {
            "turn_id": "turn-cancelled",
            "turn_order": 1,
            "thread_id": "thread-cancelled",
            "execution_class": "codex",
        }
    ]
    entered = asyncio.Event()
    settled: list[str] = []

    async def waits_forever(turn: dict, _message: str) -> bool:
        entered.set()
        try:
            await asyncio.Event().wait()
            return True
        finally:
            settled.append(turn["turn_id"])

    monkeypatch.setattr(restart, "_snapshot_active_turns", lambda: turns)
    monkeypatch.setattr(restart, "_steer_turn", waits_forever)
    owner = asyncio.create_task(
        restart.begin_restart_drain("cancelled restart", allow_hard_cap=True)
    )
    await asyncio.wait_for(entered.wait(), timeout=0.2)
    operation = self_restart.in_flight_request()
    assert operation is not None

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    assert settled == ["turn-cancelled"]
    terminal = self_restart.operation_by_id(operation["id"])
    assert terminal["status"] == "cancelled"
    assert terminal["phase"] is None
    assert self_restart.in_flight_request() is None


@pytest.mark.asyncio
async def test_hard_cap_records_recovery_before_exact_stop(
    isolated_db,
    monkeypatch,
) -> None:
    """A bounded hard stop preserves one wake before ending the exact Turn."""
    from automation import restart
    from infra.database import get_db

    _without_projection(monkeypatch)
    thread_id, turn_id = _active_turn()

    async def decline(_turn: dict, _message: str) -> bool:
        return False

    async def stop(turn: dict) -> bool:
        assert (turn["thread_id"], turn["turn_id"]) == (thread_id, turn_id)
        db = get_db()
        try:
            pending = db.execute(
                "SELECT reason FROM restart_wake_registrations "
                "WHERE thread_id=? AND turn_id=? AND status='pending'",
                (thread_id, turn_id),
            ).fetchone()
            assert pending["reason"] == "restart_recovery"
        finally:
            db.close()
        _finish(turn_id, "cancelled")
        return True

    monkeypatch.setattr(restart, "_steer_turn", decline)
    monkeypatch.setattr(restart, "_force_stop_turn", stop)
    started = await restart.begin_restart_drain(
        "test restart",
        allow_hard_cap=True,
    )
    result = await restart.enforce_restart_hard_cap(started["operation_id"])

    assert started["uncontrolled"] == [thread_id]
    assert result == {"force_stop_ready": True, "failures": []}


@pytest.mark.asyncio
async def test_hard_cap_upgrades_an_opted_in_turn_to_recovery(
    isolated_db,
    monkeypatch,
) -> None:
    """Force-stop cannot leave a weaker graceful-resume registration behind."""
    from automation import restart
    from automation.restart_wakes import register_restart_resume
    from infra.database import get_db

    _without_projection(monkeypatch)
    thread_id, turn_id = _active_turn()

    async def steer(_turn: dict, _message: str) -> bool:
        register_restart_resume(thread_id, turn_id)
        return True

    async def stop(_turn: dict) -> bool:
        _finish(turn_id, "cancelled")
        return True

    monkeypatch.setattr(restart, "_steer_turn", steer)
    monkeypatch.setattr(restart, "_force_stop_turn", stop)
    started = await restart.begin_restart_drain(
        "test restart",
        allow_hard_cap=True,
    )
    await restart.enforce_restart_hard_cap(started["operation_id"])

    db = get_db()
    try:
        row = db.execute(
            "SELECT reason,status FROM restart_wake_registrations "
            "WHERE thread_id=? AND turn_id=?",
            (thread_id, turn_id),
        ).fetchone()
        assert tuple(row) == ("restart_recovery", "pending")
    finally:
        db.close()


def test_startup_materializes_each_restart_wake_once(isolated_db) -> None:
    """Restart continuation becomes one ordinary FIFO event, exactly once."""
    from automation import self_restart
    from automation.restart_wakes import (
        materialize_pending_registrations,
        register_restart_resume,
    )
    from infra.database import get_db

    thread_id, turn_id = _active_turn()
    operation_id = self_restart.try_acquire(
        thread_id,
        "user-1",
        "sha",
        "dev",
        "test restart",
    )
    assert operation_id is not None
    register_restart_resume(thread_id, turn_id)

    assert materialize_pending_registrations() == (thread_id,)
    assert materialize_pending_registrations() == ()
    db = get_db()
    try:
        all_rows = db.execute(
            "SELECT id,role,content,delivery_state,input_order,metadata FROM messages "
            "WHERE thread_id=? ORDER BY input_order",
            (thread_id,),
        ).fetchall()
        rows = [
            row for row in all_rows
            if json.loads(row["metadata"] or "{}").get("kind") == "restart_resume"
        ]
        assert len(rows) == 1
        assert "[PASSIVE CONTEXT DIGEST]" in all_rows[0]["content"]
        assert [row["id"] for row in all_rows[1:2]] == ["restart-input"]
        assert (rows[0]["role"], rows[0]["delivery_state"], rows[0]["input_order"]) == (
            "system", "queued", 3,
        )
    finally:
        db.close()


def test_startup_claims_an_idle_fifo_head_once(isolated_db) -> None:
    """A cleanly stopped process cannot leave an accepted input ownerless."""
    from infra.database import get_db
    from infra.thread_recovery import recover_threads
    from infra.input_admission import accept_thread_input

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('idle-thread','user-1','oMLX/qwen','t0','t0')"
        )
        accept_thread_input(
            "idle-thread",
            role="user",
            content="resume me",
            intent="apply_now",
            message_id="idle-input",
            db=db,
        )
        db.commit()
        recovered = recover_threads(
            boot_epoch="new-boot",
            recovered_at="t1",
            successor_id=lambda _thread_id: "startup-turn",
            db=db,
        )
        repeated = recover_threads(
            boot_epoch="new-boot",
            recovered_at="t2",
            successor_id=lambda _thread_id: "duplicate-turn",
            db=db,
        )
        assert [batch.turn_id for batch in recovered.batches] == ["startup-turn"]
        assert repeated.batches == ()
        assert (
            db.execute(
                "SELECT active_turn_id FROM messages WHERE id='idle-input'"
            ).fetchone()[0]
            == "startup-turn"
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_startup_wakes_large_pending_switch_only_after_readiness_phase(
    isolated_db,
    monkeypatch,
) -> None:
    """Reconcile stays database-only; the post-bind drive owns idle selection work."""
    from automation import restart_startup
    from infra.database import get_db
    from thread import model_pending

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,pending_model,"
            "pending_execution_epoch,switch_after_input_order,switch_operation_id,"
            "switch_status,switch_requested_at,created_at,updated_at) "
            "VALUES ('large-thread','user-1','native/large','native/tiny',1,"
            "10000,'switch-1','preparing','t1','t0','t0')"
        )
        db.executemany(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES (?,'large-thread',?,'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 't0',?)",
            (
                (f"large-message-{seq}", "user" if seq % 2 else "assistant", seq)
                for seq in range(1, 10_001)
            ),
        )
        db.commit()
    finally:
        db.close()

    async def no_webhooks() -> None:
        return None

    prepared = []

    async def record_preparation(thread_id: str) -> None:
        prepared.append(thread_id)

    monkeypatch.setattr(restart_startup, "close_out_prior_self_restart", lambda: None)
    monkeypatch.setattr(
        "automation.restart_wakes.materialize_pending_registrations", lambda: ()
    )
    monkeypatch.setattr(restart_startup, "_replay_held_webhooks", no_webhooks)
    monkeypatch.setattr(restart_startup, "_complete_restart_operation", lambda: None)
    monkeypatch.setattr(model_pending, "drive_pending_model_switch", record_preparation)

    logger = SimpleNamespace(info=lambda *_args: None)
    recovered, wakes, _rolled_back = await restart_startup.reconcile_startup_continuity(logger)
    assert prepared == []
    assert wakes == ("large-thread",)
    await restart_startup.drive_startup_continuity(recovered, wakes, logger)
    assert prepared == ["large-thread"]

    db = get_db()
    try:
        row = db.execute(
            "SELECT switch_status FROM threads WHERE id='large-thread'"
        ).fetchone()
        assert row["switch_status"] == "preparing"
        assert db.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0] == 0
    finally:
        db.close()


async def _completed():
    """An already-satisfied awaitable standing in for harness discovery."""
    return None


async def _run_startup_recovery(startup, logger):
    """Run both continuity halves in the order startup runs them."""
    recovered, wakes, _rolled_back = await startup.reconcile_startup_continuity(logger)
    await startup.drive_startup_continuity(recovered, wakes, logger)


def _startup_recovery_probe(
    monkeypatch,
    events,
    *,
    replay_error=None,
    batches=(SimpleNamespace(thread_id="batch-thread", turn_id="batch-turn"),),
    batch_side_effect=None,
):
    from automation import restart_startup, restart_wakes
    from infra import thread_execution, thread_recovery

    async def run_in_db(fn, *args, **kwargs):
        result = fn(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    def close_restart() -> None:
        events.append("adopt_restart")

    def materialize() -> tuple[str, ...]:
        events.append("materialize_wakes")
        return ("wake-thread",)

    def recover(**_kwargs):
        events.append("recover_turns")
        return SimpleNamespace(
            settlements=(SimpleNamespace(thread_id="settled-thread"),),
            ended_turns=(),
            batches=batches,
        )

    async def drive_settlement(_settlement) -> None:
        events.append("drive_settlement")

    async def drive_batch(batch) -> None:
        events.append("drive_batch")
        if batch_side_effect is not None:
            await batch_side_effect(batch)

    async def drive_thread(_thread_id) -> None:
        events.append("drive_wake")

    async def replay() -> None:
        events.append("replay_webhooks")
        if replay_error is not None:
            raise replay_error

    def complete() -> None:
        events.append("release_ingress")

    monkeypatch.setattr(restart_startup, "run_in_db", run_in_db)
    monkeypatch.setattr(restart_startup, "close_out_prior_self_restart", close_restart)
    monkeypatch.setattr(restart_wakes, "materialize_pending_registrations", materialize)
    monkeypatch.setattr(thread_recovery, "recover_threads", recover)
    monkeypatch.setattr(thread_execution, "drive_terminal_settlement", drive_settlement)
    monkeypatch.setattr(thread_execution, "drive_claimed_batch", drive_batch)
    monkeypatch.setattr(thread_execution, "drive_thread", drive_thread)
    monkeypatch.setattr(restart_startup, "_replay_held_webhooks", replay)
    monkeypatch.setattr(restart_startup, "_complete_restart_operation", complete)
    return restart_startup


@pytest.mark.asyncio
async def test_startup_claims_fifo_before_release_and_drives_after_release(
    monkeypatch,
) -> None:
    """Durable claims, not provider completion, protect recovered FIFO heads."""
    events: list[str] = []
    startup = _startup_recovery_probe(monkeypatch, events)

    await _run_startup_recovery(startup, SimpleNamespace(info=lambda *_a: None))

    assert events == [
        "adopt_restart",
        "materialize_wakes",
        "recover_turns",
        "release_ingress",
        "drive_settlement",
        "drive_batch",
        "drive_wake",
        "replay_webhooks",
    ]


@pytest.mark.asyncio
async def test_startup_rejection_does_not_abort_other_recovered_heads(
    monkeypatch,
) -> None:
    """A terminalized provider refusal cannot crash application startup."""
    from infra.send_types import SendRejected

    first = SimpleNamespace(thread_id="thread-a", turn_id="turn-a")
    second = SimpleNamespace(thread_id="thread-b", turn_id="turn-b")

    async def reject_first(batch) -> None:
        if batch is first:
            raise SendRejected(409, "provider context changed")

    events: list[str] = []
    warnings: list[tuple] = []
    startup = _startup_recovery_probe(
        monkeypatch,
        events,
        batches=(first, second),
        batch_side_effect=reject_first,
    )
    logger = SimpleNamespace(
        info=lambda *_args: None,
        warning=lambda *args: warnings.append(args),
    )

    await _run_startup_recovery(startup, logger)

    assert events.count("drive_batch") == 2
    assert events.index("release_ingress") < events.index("drive_batch")
    assert warnings == [(
        "startup continuity: {} was not delivered: {}",
        "Turn admission for thread-a",
        "provider context changed",
    )]


@pytest.mark.asyncio
async def test_startup_replay_failure_is_visible_without_holding_other_threads(monkeypatch) -> None:
    """The webhook journal retains failed replay; unrelated delivery stays open."""
    events: list[str] = []
    startup = _startup_recovery_probe(
        monkeypatch,
        events,
        replay_error=RuntimeError("webhook failure"),
    )

    with pytest.raises(ExceptionGroup) as caught:
        await _run_startup_recovery(
            startup, SimpleNamespace(info=lambda *_a: None, exception=lambda *_a: None)
        )

    assert str(caught.value.exceptions[0]) == "webhook failure"
    assert events[-1] == "replay_webhooks"
    assert "release_ingress" in events and "drive_batch" in events


@pytest.mark.asyncio
async def test_drive_phase_failure_is_not_reported_as_completion(monkeypatch) -> None:
    """A failed drive must fail its task, not look like finished recovery."""
    events: list[str] = []
    startup = _startup_recovery_probe(
        monkeypatch,
        events,
        replay_error=RuntimeError("webhook failure"),
    )
    errors: list[tuple] = []
    logger = SimpleNamespace(
        info=lambda *_a: None,
        warning=lambda *_a: None,
        error=lambda *args: errors.append(args),
        exception=lambda *_a: None,
    )

    recovered, wakes, _rolled_back = await startup.reconcile_startup_continuity(logger)
    with pytest.raises(ExceptionGroup) as caught:
        await startup.run_startup_drive(_completed(), recovered, wakes, _rolled_back, logger)

    assert str(caught.value.exceptions[0]) == "webhook failure"
    assert "release_ingress" in events
    assert errors, "the failure must be reported with its consequence"
    assert "drive failed" in errors[0][0]


@pytest.mark.asyncio
async def test_hard_cap_is_ready_once_recovery_is_durable(
    isolated_db,
    monkeypatch,
) -> None:
    """A Turn that refuses to stop must not veto the operator's hard cap.

    ``register_hard_recovery`` persists continuation intent for every captured
    Turn *before* the stop is attempted, so the work is already conserved
    whether or not the in-process stop lands. Refusing here bought no safety
    and cost the operator their stop: the CLI cancelled and left the service
    running, which is the one outcome a hard cap exists to prevent.
    """
    from automation import restart
    from infra.database import get_db

    _without_projection(monkeypatch)
    thread_id, turn_id = _active_turn()

    async def decline(_turn: dict, _message: str) -> bool:
        return False

    async def refuse_stop(_turn: dict) -> bool:
        return False

    monkeypatch.setattr(restart, "_steer_turn", decline)
    monkeypatch.setattr(restart, "_force_stop_turn", refuse_stop)
    started = await restart.begin_restart_drain(
        "service stop",
        allow_hard_cap=True,
    )
    result = await restart.enforce_restart_hard_cap(started["operation_id"])

    assert result["force_stop_ready"] is True
    assert result["failures"] == [f"{thread_id}:stop"]

    db = get_db()
    try:
        pending = db.execute(
            "SELECT reason,status FROM restart_wake_registrations "
            "WHERE thread_id=? AND turn_id=?",
            (thread_id, turn_id),
        ).fetchone()
        assert tuple(pending) == ("restart_recovery", "pending")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_hard_cap_advances_phase_even_when_a_turn_will_not_stop(
    isolated_db,
    monkeypatch,
) -> None:
    """Force-stop readiness must reach 'ready' so the CLI can mark restarting."""
    from automation import restart, self_restart

    _without_projection(monkeypatch)
    _active_turn()

    async def decline(_turn: dict, _message: str) -> bool:
        return False

    async def refuse_stop(_turn: dict) -> bool:
        return False

    monkeypatch.setattr(restart, "_steer_turn", decline)
    monkeypatch.setattr(restart, "_force_stop_turn", refuse_stop)
    started = await restart.begin_restart_drain(
        "service stop",
        allow_hard_cap=True,
    )
    await restart.enforce_restart_hard_cap(started["operation_id"])

    assert self_restart.in_flight_request()["phase"] == "ready"
    assert restart.mark_restarting(started["operation_id"]) is True


@pytest.mark.asyncio
async def test_abort_releases_the_hold_without_awaiting_resumed_turns(
    isolated_db,
    monkeypatch,
) -> None:
    """Cancelling must report its true result inside the caller's deadline.

    ``drive_thread`` starts a whole Turn — context assembly plus a provider
    call. Awaiting it inside the abort made DELETE /api/system/drain outlive
    the CLI's five-second budget, so a cancel that had already committed was
    reported to the operator as 'Could not cancel drain mode'.
    """
    from automation import restart
    from infra import thread_execution

    _without_projection(monkeypatch)
    thread_id, _turn_id = _active_turn()

    released = asyncio.Event()
    started_drive = asyncio.Event()

    async def slow_drive(driven_thread_id: str) -> None:
        assert driven_thread_id == thread_id
        started_drive.set()
        await released.wait()

    monkeypatch.setattr(thread_execution, "drive_thread", slow_drive)

    async def steer(_turn: dict, _message: str) -> bool:
        from automation.restart_wakes import register_restart_resume

        register_restart_resume(thread_id, _turn_id)
        return True

    monkeypatch.setattr(restart, "_steer_turn", steer)
    started = await restart.begin_restart_drain(
        "cancelled restart",
        allow_hard_cap=True,
    )

    aborted = await asyncio.wait_for(
        restart.abort_restart_control(started["operation_id"], cancelled=True),
        timeout=2,
    )

    assert aborted is True
    assert not released.is_set()
    released.set()
    if restart._RESUME_TASKS:
        await asyncio.gather(*tuple(restart._RESUME_TASKS))
    assert started_drive.is_set()


@pytest.mark.asyncio
async def test_abort_cancelled_mid_release_still_clears_and_resumes(
    isolated_db,
    monkeypatch,
) -> None:
    """A cancel during the release write cannot strand the threads it held.

    The abort is itself cancellation compensation, so a second cancellation
    can land while its release runs on the DB pool. The shielded write still
    commits; the projection clear and the resume must follow it before the
    cancellation reaches the caller.
    """
    import threading

    from automation import restart, self_restart
    from infra import drain, thread_execution

    _without_projection(monkeypatch)
    cleared: list[bool] = []
    monkeypatch.setattr(drain, "cancel_drain_projection", lambda: cleared.append(True))
    driven: list[str] = []

    async def record_drive(driven_thread_id: str) -> None:
        driven.append(driven_thread_id)

    monkeypatch.setattr(thread_execution, "drive_thread", record_drive)
    thread_id, turn_id = _active_turn()

    async def steer(_turn: dict, _message: str) -> bool:
        from automation.restart_wakes import register_restart_resume

        register_restart_resume(thread_id, turn_id)
        return True

    monkeypatch.setattr(restart, "_steer_turn", steer)
    started = await restart.begin_restart_drain("cancelled restart", allow_hard_cap=True)

    entered = threading.Event()
    proceed = threading.Event()
    real_release = restart._release_restart_control

    def release_at_barrier(*args):
        entered.set()
        assert proceed.wait(timeout=5), "test never let the release proceed"
        return real_release(*args)

    monkeypatch.setattr(restart, "_release_restart_control", release_at_barrier)
    caller = asyncio.create_task(
        restart.abort_restart_control(started["operation_id"], cancelled=True)
    )
    assert await asyncio.to_thread(entered.wait, 5)
    caller.cancel()
    proceed.set()

    with pytest.raises(asyncio.CancelledError):
        await caller

    assert cleared == [True]
    if restart._RESUME_TASKS:
        await asyncio.gather(*tuple(restart._RESUME_TASKS))
    assert driven == [thread_id]
    terminal = self_restart.operation_by_id(started["operation_id"])
    assert terminal["status"] == "cancelled"
    assert self_restart.in_flight_request() is None
