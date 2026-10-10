"""Stop uses the durable owner when a post-write runtime has disappeared."""

from __future__ import annotations

import asyncio

import pytest

from test_stop_receipts import _active_turn, _clear_live_turn_state  # noqa: F401


async def _settle_stop_work(before) -> None:
    from ws.ingress import background_tasks

    pending = background_tasks - before
    while pending:
        await asyncio.gather(*pending, return_exceptions=True)
        pending = background_tasks - before


async def _finish_signalled_native(event) -> None:
    await asyncio.wait_for(event.wait(), 2)
    if not event.completion.done():
        event.completion.set_result(True)


async def _no_processes(*_args, **_kwargs) -> None:
    return None


async def _stop(main_db, turn_id):
    from ws.stop_control import do_stop

    owner_id = main_db.execute(
        "SELECT user_id FROM threads WHERE id='main-1'",
    ).fetchone()[0]

    async def owns_thread(user, thread_id):
        return user["id"] == owner_id and thread_id == "main-1"

    return await do_stop(
        {"id": owner_id}, "main-1", None, None, turn_id,
        owns_thread_fn=owns_thread,
    )


@pytest.mark.parametrize("model", ["test-model", "claude-code/opus", "codex/o3"])
@pytest.mark.asyncio
async def test_ownerless_postwrite_stop_reaches_exact_terminal(main_db, model):
    """Removing the durable Stop path strands all three harnesses after write."""
    from infra import state

    main_db.execute("UPDATE threads SET model=? WHERE id='main-1'", (model,))
    main_db.commit()
    identity, _event = _active_turn(main_db, "orphan")
    state._active_turns.pop("main-1")
    state.turn_buffers.init("main-1", identity)
    before = tuple(main_db.execute(
        "SELECT id,seq,input_order,delivery_state,write_outcome FROM messages "
        "WHERE id='orphan:input'",
    ).fetchone())

    receipt = await _stop(main_db, identity.turn_id)

    assert receipt.payload() == {
        "type": "stop_receipt", "thread_id": "main-1",
        "outcome": "stopped", "turn_id": "orphan",
    }
    assert main_db.execute(
        "SELECT status FROM active_turns WHERE id='orphan'",
    ).fetchone()[0] == "cancelled"
    assert tuple(main_db.execute(
        "SELECT id,seq,input_order,delivery_state,write_outcome FROM messages "
        "WHERE id='orphan:input'",
    ).fetchone()) == before
    assert main_db.execute(
        "SELECT COUNT(*) FROM active_turns WHERE status='running'",
    ).fetchone()[0] == 0
    assert state.turn_buffers.get("main-1", identity) is None


@pytest.mark.asyncio
async def test_ownerless_stop_broadcasts_only_after_terminal_commit(main_db, monkeypatch):
    """A second tab must leave busy state, not depend on the requester receipt."""
    from infra import state

    identity, _event = _active_turn(main_db, "orphan")
    state._active_turns.pop("main-1")
    frames = []

    def broadcast(thread_id, user_id, frame):
        assert thread_id == "main-1"
        assert user_id == main_db.execute(
            "SELECT user_id FROM threads WHERE id='main-1'",
        ).fetchone()[0]
        assert main_db.execute(
            "SELECT status FROM active_turns WHERE id='orphan'",
        ).fetchone()[0] == "cancelled"
        frames.append(frame)

    monkeypatch.setattr(state, "broadcast_event", broadcast)
    await _stop(main_db, identity.turn_id)
    await _stop(main_db, identity.turn_id)

    assert frames == [{
        "type": "turn_cancelled", "thread_id": "main-1",
        "turn_id": "orphan", "turn_order": identity.turn_order,
    }]


@pytest.mark.parametrize(("evidence", "settled"), [
    ("unfenced", ("cancelled", "proved_no_write")),
    ("unresolved", ("ambiguous", "ambiguous")),
    ("ambiguous", ("ambiguous", "ambiguous")),
])
@pytest.mark.asyncio
async def test_ownerless_stop_settles_input_from_its_write_evidence(
    main_db, evidence, settled,
):
    """Stop ends the Turn; each input keeps only what its write fence proves."""
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    admission = admit_input(
        "main-1", role="user", content="exact input", intent="apply_now",
        message_id="input", turn_id="orphan", boot_epoch="old-boot",
        admitted_at="t", db=main_db,
    )
    if evidence != "unfenced":
        begin_batch_write(admission.batch, attempt_id="write", started_at="t", db=main_db)
    if evidence == "ambiguous":
        settle_batch_write("write", outcome="ambiguous", db=main_db)

    assert (await _stop(main_db, "orphan")).outcome == "stopped"
    assert main_db.execute(
        "SELECT status FROM active_turns WHERE id='orphan'",
    ).fetchone()[0] == "cancelled"
    assert tuple(main_db.execute(
        "SELECT delivery_state,write_outcome FROM messages WHERE id='input'",
    ).fetchone()) == settled


@pytest.mark.parametrize("owner", [
    "native-event", "native-call", "native-turn", "cc-live", "cc-transition",
    "codex-live", "codex-transition", "codex-session",
])
@pytest.mark.asyncio
async def test_owner_appearing_after_runtime_stop_is_retired_after_commit(
    main_db, monkeypatch, owner,
):
    """An owner that appears after the runtime attempt cannot keep the Turn open."""
    from host_access import processes
    from infra import state
    from subagents import cc_runtime, codex_runtime, live_registry
    from ws.ingress import background_tasks
    import ws.stop_control as control

    identity, event = _active_turn(main_db, "orphan")
    state._active_turns.pop("main-1")
    monkeypatch.setattr(live_registry, "STOP_LOCK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(processes.PROCESS_OWNER, "terminate_turn", _no_processes)
    run_in_db = control.run_in_db
    pending = None
    lock = None
    registry = cc_runtime._LIVE if owner.startswith("cc-") else codex_runtime._LIVE
    appeared = False
    before = set(background_tasks)

    async def appear_before_db(fn, *args, **kwargs):
        nonlocal pending, lock, appeared
        if not appeared:
            appeared = True
            if owner == "native-event":
                state._active_turns["main-1"] = event
            elif owner in {"native-call", "native-turn"}:
                pending = asyncio.create_task(asyncio.Event().wait())
                tasks = state._active_llm_tasks if owner == "native-call" else state._active_turn_tasks
                monkeypatch.setitem(tasks, "main-1", pending)
            elif owner == "codex-session":
                monkeypatch.setitem(codex_runtime._APP_SESSIONS, "main-1", object())
            elif owner.endswith("transition"):
                lock = registry.lock("main-1")
                await lock.acquire()
            else:
                registry.put("main-1", object())
        return await run_in_db(fn, *args, **kwargs)

    monkeypatch.setattr(control, "run_in_db", appear_before_db)
    try:
        assert (await _stop(main_db, identity.turn_id)).outcome == "stopped"
        if owner == "native-event":
            await _finish_signalled_native(event)
        await _settle_stop_work(before)
        assert appeared
        assert event.is_set() is (owner == "native-event")
        assert main_db.execute(
            "SELECT status FROM active_turns WHERE id='orphan'",
        ).fetchone()[0] == "cancelled"
        assert pending is None or not pending.cancelled()
        if lock is not None:
            assert registry.existing_lock("main-1") is not lock
    finally:
        state._active_turns.pop("main-1", None)
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if lock is not None:
            lock.release()
        registry.reset()


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.asyncio
async def test_ownerless_stop_unblocks_existing_model_switch(main_db, monkeypatch, ready):
    """The Quality Gate zero-queue specimen must release the source boundary."""
    from infra import state
    from infra.database import write_transaction
    from infra.model_switches import activate_ready_switch, read_switch, request_switch
    from thread import model_pending, model_runtime
    from ws.ingress import background_tasks

    identity, _event = _active_turn(main_db, "orphan")
    state._active_turns.pop("main-1")
    switch = request_switch(
        "main-1", "test-model", operation_id="switch", requested_at="t",
        db=main_db,
    )
    assert switch.waiting_for_source
    if ready:
        with write_transaction(main_db, scope="test_ownerless_switch_ready") as connection:
            connection.execute(
                "UPDATE threads SET switch_status='ready' "
                "WHERE switch_operation_id='switch'",
            )
            assert activate_ready_switch(
                connection, "main-1", committed_at="t",
            ) is None
    effects = []

    async def prepare_switch(thread_id):
        assert not main_db.in_transaction
        assert not read_switch(thread_id, db=main_db).waiting_for_source
        effects.append("prepare")

    async def apply_switch(transition):
        assert not main_db.in_transaction
        assert transition.committed
        effects.append("apply")

    monkeypatch.setattr(model_pending, "drive_pending_model_switch", prepare_switch)
    monkeypatch.setattr(model_runtime, "apply_switch_transition", apply_switch)
    before = set(background_tasks)
    assert (await _stop(main_db, identity.turn_id)).outcome == "stopped"
    await _settle_stop_work(before)
    assert effects == (["apply"] if ready else ["prepare"])
    row = main_db.execute(
        "SELECT model,pending_model FROM threads WHERE id='main-1'",
    ).fetchone()
    assert tuple(row) == (("test-model", None) if ready else ("claude-code/opus", "test-model"))


@pytest.mark.asyncio
async def test_ownerless_stop_drives_one_fifo_successor_and_never_stops_it(main_db, monkeypatch):
    """The terminal receipt must drive its one admitted FIFO batch after commit."""
    from infra import state, thread_execution
    from infra.input_admission import accept_thread_input
    from ws.ingress import background_tasks

    identity, _event = _active_turn(main_db, "orphan")
    state._active_turns.pop("main-1")
    first_order = main_db.execute(
        "SELECT input_order FROM messages WHERE id='orphan:input'",
    ).fetchone()[0]
    for message_id in ("next-1", "next-2"):
        accept_thread_input(
            "main-1", role="user", content=message_id, intent="queue_after_turn",
            message_id=message_id, db=main_db,
        )
    batches = []

    async def dispatch(batch):
        assert not main_db.in_transaction
        batches.append(batch)

    monkeypatch.setattr(thread_execution, "drive_claimed_batch", dispatch)
    before = set(background_tasks)
    assert (await _stop(main_db, identity.turn_id)).outcome == "stopped"
    assert (await _stop(main_db, identity.turn_id)).outcome == "stale_active"
    await _settle_stop_work(before)
    assert len(batches) == 1
    assert [item.message_id for item in batches[0].items] == ["next-1", "next-2"]
    assert [tuple(row) for row in main_db.execute(
        "SELECT id,delivery_state,input_order FROM messages "
        "WHERE id IN ('orphan:input','next-1','next-2') ORDER BY input_order",
    )] == [
        ("orphan:input", "delivered", first_order),
        ("next-1", "queued", first_order + 1), ("next-2", "queued", first_order + 2),
    ]
    assert [row[0] for row in main_db.execute(
        "SELECT status FROM active_turns ORDER BY turn_order",
    )] == ["cancelled", "running"]


@pytest.mark.parametrize("restart_first", [False, True])
@pytest.mark.asyncio
async def test_ownerless_stop_and_startup_have_one_terminal_winner(main_db, restart_first):
    from infra import state
    from infra.thread_recovery import recover_threads

    identity, _event = _active_turn(main_db, "orphan")
    state._active_turns.pop("main-1")
    if not restart_first:
        assert (await _stop(main_db, identity.turn_id)).outcome == "stopped"
    recovery = recover_threads(
        boot_epoch="new-boot", recovered_at="t2", successor_id=lambda _sid: "next",
        db=main_db,
    )
    assert len(recovery.settlements) == (1 if restart_first else 0)
    assert recovery.batches == ()
    assert (await _stop(main_db, identity.turn_id)).outcome == "already_terminal"
    assert main_db.execute(
        "SELECT status FROM active_turns WHERE id='orphan'",
    ).fetchone()[0] == ("error" if restart_first else "cancelled")


@pytest.mark.asyncio
async def test_cancelled_stop_caller_cannot_abandon_committed_effects(main_db, monkeypatch):
    from infra import state, thread_execution
    from infra.input_admission import accept_thread_input
    from ws.ingress import background_tasks
    import ws.stop_control as control

    identity, _event = _active_turn(main_db, "orphan")
    state._active_turns.pop("main-1")
    accept_thread_input(
        "main-1", role="user", content="next", intent="queue_after_turn",
        message_id="next", db=main_db,
    )
    run_in_db = control.run_in_db
    committed = asyncio.Event()
    release = asyncio.Event()
    dispatch_started, finish_dispatch = asyncio.Event(), asyncio.Event()
    frames = []

    async def hold_committed_result(fn, *args, **kwargs):
        result = await run_in_db(fn, *args, **kwargs)
        committed.set()
        await release.wait()
        return result

    async def dispatch(batch):
        assert batch.items[0].message_id == "next"
        dispatch_started.set()
        await finish_dispatch.wait()

    monkeypatch.setattr(control, "run_in_db", hold_committed_result)
    monkeypatch.setattr(thread_execution, "drive_claimed_batch", dispatch)
    monkeypatch.setattr(state, "broadcast_event", lambda _sid, _uid, frame: frames.append(frame))
    before = set(background_tasks)
    task = asyncio.create_task(_stop(main_db, identity.turn_id))
    try:
        await committed.wait()
        task.cancel()
        release.set()
        done, _pending = await asyncio.wait({task}, timeout=2)
        assert task in done, "Caller cancellation joined unrelated successor work"
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(dispatch_started.wait(), 2)
        assert len(background_tasks - before) == 1
        assert frames[0]["turn_id"] == identity.turn_id
        assert frames[0]["type"] == "turn_cancelled"
    finally:
        release.set()
        finish_dispatch.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*(background_tasks - before), return_exceptions=True)


@pytest.mark.asyncio
async def test_owner_appearing_while_stop_waits_for_db_writer_is_retired(main_db, monkeypatch):
    """An owner bound while Stop waits for the writer is stopped after the commit."""
    from contextlib import contextmanager
    import threading

    from infra import state
    from host_access import processes
    from subagents import live_registry
    from ws.ingress import background_tasks
    import ws.stop_control as control

    identity, event = _active_turn(main_db, "orphan")
    state._active_turns.pop("main-1")
    monkeypatch.setattr(live_registry, "STOP_LOCK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(processes.PROCESS_OWNER, "terminate_turn", _no_processes)
    entered = threading.Event()
    transaction = control.write_transaction
    before = set(background_tasks)

    @contextmanager
    def observe_transaction(db, *, scope):
        entered.set()
        with transaction(db, scope=scope) as connection:
            yield connection

    monkeypatch.setattr(control, "write_transaction", observe_transaction)
    main_db.execute("BEGIN IMMEDIATE")
    task = asyncio.create_task(_stop(main_db, identity.turn_id))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        state._active_turns["main-1"] = event
        main_db.execute("ROLLBACK")
        assert (await task).outcome == "stopped"
        await _finish_signalled_native(event)
        await _settle_stop_work(before)
        assert main_db.execute(
            "SELECT status FROM active_turns WHERE id='orphan'",
        ).fetchone()[0] == "cancelled"
    finally:
        state._active_turns.pop("main-1", None)
        if main_db.in_transaction:
            main_db.execute("ROLLBACK")
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_ownerless_stops_publish_one_terminal(main_db, monkeypatch):
    from infra import state

    identity, _event = _active_turn(main_db, "orphan")
    state._active_turns.pop("main-1")
    frames = []
    monkeypatch.setattr(state, "broadcast_event", lambda _sid, _uid, frame: frames.append(frame))
    receipts = await asyncio.gather(
        _stop(main_db, identity.turn_id), _stop(main_db, identity.turn_id),
    )
    assert sorted(receipt.outcome for receipt in receipts) == ["already_terminal", "stopped"]
    assert len(frames) == 1
