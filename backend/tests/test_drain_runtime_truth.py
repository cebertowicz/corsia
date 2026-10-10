from __future__ import annotations

import asyncio
import inspect
import sys
import threading

import pytest


class _ProcessOwner:
    def __init__(
        self,
        *,
        processes: int = 0,
        browser_commands: int = 0,
        idle_browser_sessions: int = 0,
    ) -> None:
        self._processes = processes
        self._browser_commands = browser_commands
        self._idle_browser_sessions = idle_browser_sessions

    def active_process_count(self) -> int:
        return self._processes

    def running_records(self) -> tuple[object, ...]:
        return tuple(object() for _ in range(self._processes))

    def active_browser_command_count(self) -> int:
        return self._browser_commands

    def live_count(self) -> int:
        return self._processes + self._idle_browser_sessions


def _restart_hold_request(*_args, **_kwargs) -> dict[str, object]:
    return {
        "id": "held-operation",
        "phase": "holding",
        "reason": "test",
        "status": "in_flight",
        "requested_at": "2026-08-03T00:00:00+00:00",
        "cutoff_at": "2026-08-03T00:00:00+00:00",
    }


def _seed_restart_hold(monkeypatch) -> None:
    from automation import self_restart

    monkeypatch.setattr(self_restart, "in_flight_request", _restart_hold_request)
    monkeypatch.setattr(
        self_restart, "restart_holds_new_work", lambda *, db=None: True,
    )


def _seed_drain_projection() -> None:
    from infra import state

    state._drain = {
        "reason": "test",
        "started_at": 0,
    }


def test_idle_drain_status_omits_never_set_countdown(isolated_db) -> None:
    from infra import drain

    status = drain.get_drain_status()

    assert status["active"] is False
    assert "countdown" not in status


def _start_test_drain(monkeypatch, *, turns, supervisors, owner) -> None:
    from infra import drain

    _seed_restart_hold(monkeypatch)
    monkeypatch.setattr(
        drain,
        "active_turn_count",
        lambda: len(tuple(turns)),
    )
    monkeypatch.setattr(drain, "active_turn_blockers", lambda: [])
    monkeypatch.setattr(
        drain,
        "host_access_supervisor_count",
        lambda: supervisors,
    )
    monkeypatch.setattr(drain, "PROCESS_OWNER", owner)
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    _seed_drain_projection()


def test_drain_counts_every_durable_or_live_work_owner(monkeypatch) -> None:
    from infra import drain, state

    _start_test_drain(
        monkeypatch,
        turns=("turn-a", "turn-b"),
        supervisors=3,
        owner=_ProcessOwner(processes=4, browser_commands=2),
    )
    try:
        status = drain.get_drain_status()

        assert status["activeProcesses"] == 11
        assert status["activeTurns"] == 2
        assert status["hostAccessSupervisors"] == 3
        assert status["ownedProcessGroups"] == 4
        assert status["browserCommands"] == 2
        assert status["turnAdmissions"] == 0
        assert "countdown" not in status
        assert drain.check_drain_ready() is False
    finally:
        state._drain = None


def test_drain_status_names_the_exact_active_turn_blocker(
    isolated_db,
    monkeypatch,
) -> None:
    from infra import drain, state
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn
    from tests.event_routing_helpers import mk_session

    thread_id = mk_session("blocked-thread")
    db = get_db()
    try:
        db.execute(
            "UPDATE threads SET title='Inspect Batch 2 history/tool storage' "
            "WHERE id=?",
            (thread_id,),
        )
        accept_thread_input(
            thread_id,
            role="user",
            content="still working",
            intent="apply_now",
            message_id="blocked-input",
            db=db,
        )
        db.commit()
        assert start_idle_turn(
            thread_id,
            turn_id="blocked-turn",
            boot_epoch="test",
            started_at="now",
            db=db,
        ) is not None
        db.commit()
    finally:
        db.close()
    _seed_restart_hold(monkeypatch)
    monkeypatch.setattr(drain, "host_access_supervisor_count", lambda: 0)
    monkeypatch.setattr(drain, "PROCESS_OWNER", _ProcessOwner())
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    _seed_drain_projection()
    try:
        status = drain.get_drain_status()
        assert status["blockingTurns"] == [{
            "threadId": thread_id,
            "turnId": "blocked-turn",
            "turnOrder": 1,
            "executionClass": "native",
            "title": "Inspect Batch 2 history/tool storage",
            "startedAt": status["blockingTurns"][0]["startedAt"],
        }]
    finally:
        state._drain = None


def test_drain_status_blocking_turns_scoped_to_requesting_user(
    isolated_db,
    monkeypatch,
) -> None:
    """get_drain_status(user_id=...) must not leak another user's turn."""
    from infra import drain, state
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn
    from tests.event_routing_helpers import mk_session

    own_thread = mk_session("own-thread")
    other_thread = mk_session("other-thread")
    db = get_db()
    try:
        db.execute(
            "UPDATE threads SET user_id='other-user' WHERE id=?",
            (other_thread,),
        )
        for thread_id, turn_id in (
            (own_thread, "own-turn"),
            (other_thread, "other-turn"),
        ):
            accept_thread_input(
                thread_id,
                role="user",
                content="still working",
                intent="apply_now",
                message_id=f"{turn_id}-input",
                db=db,
            )
            db.commit()
            assert start_idle_turn(
                thread_id,
                turn_id=turn_id,
                boot_epoch="test",
                started_at="now",
                db=db,
            ) is not None
        db.commit()
    finally:
        db.close()
    _seed_restart_hold(monkeypatch)
    monkeypatch.setattr(drain, "host_access_supervisor_count", lambda: 0)
    monkeypatch.setattr(drain, "PROCESS_OWNER", _ProcessOwner())
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    _seed_drain_projection()
    try:
        scoped = drain.get_drain_status(user_id="user-1")
        assert [b["threadId"] for b in scoped["blockingTurns"]] == [own_thread]

        unscoped = drain.get_drain_status()
        assert {b["threadId"] for b in unscoped["blockingTurns"]} == {
            own_thread, other_thread,
        }
    finally:
        state._drain = None


def test_start_drain_projection_broadcast_omits_blocking_turns(
    monkeypatch,
    tmp_path,
) -> None:
    """The all-clients broadcast must never carry another user's turn data."""
    from infra import drain, state

    monkeypatch.setattr(drain, "active_turn_count", lambda: 1)
    monkeypatch.setattr(
        drain,
        "active_turn_blockers",
        lambda: [{"threadId": "t1", "turnId": "tn1", "title": "secret"}],
    )
    monkeypatch.setattr(drain, "host_access_supervisor_count", lambda: 0)
    monkeypatch.setattr(drain, "PROCESS_OWNER", _ProcessOwner())
    monkeypatch.setattr(drain, "SIGNAL_FILE", tmp_path / "drain")
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    _seed_restart_hold(monkeypatch)
    state._drain = None

    broadcasts: list[dict] = []
    monkeypatch.setattr(state, "broadcast_all", lambda msg: broadcasts.append(msg))
    try:
        projected = drain.start_drain_projection("test")
        assert projected["blockingTurns"] == [
            {"threadId": "t1", "turnId": "tn1", "title": "secret"},
        ]
        assert len(broadcasts) == 1
        assert broadcasts[0]["blockingTurns"] == []
    finally:
        state._drain = None
        drain.SIGNAL_FILE.unlink(missing_ok=True)


def test_drain_fences_atomically_after_all_pre_drain_admissions_finish(
    monkeypatch,
) -> None:
    from infra import drain, state

    _seed_restart_hold(monkeypatch)
    monkeypatch.setattr(drain, "active_turn_count", lambda: 0)
    monkeypatch.setattr(drain, "active_turn_blockers", lambda: [])
    monkeypatch.setattr(drain, "host_access_supervisor_count", lambda: 0)
    monkeypatch.setattr(drain, "PROCESS_OWNER", _ProcessOwner())
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    state._drain = None
    try:
        admission = drain.begin_turn_admission()
        assert admission is not None
        state._drain = {
            "reason": "test",
            "started_at": 0,
        }
        active = drain.get_drain_status()
        assert active["turnAdmissions"] == 1
        assert active["activeProcesses"] == 1

        second_admission = drain.begin_turn_admission()
        assert second_admission is not None

        drain.finish_turn_admission(admission)
        drain.finish_turn_admission(second_admission)
        ready = drain.get_drain_status()
        assert ready["activeProcesses"] == 0
        assert drain.check_drain_ready() is True
    finally:
        drain._turn_admissions = 0
        state._drain = None


def test_start_drain_projection_reports_live_admission_state_without_gating(
    monkeypatch,
    tmp_path,
) -> None:
    from infra import drain, state

    monkeypatch.setattr(drain, "active_turn_count", lambda: 0)
    monkeypatch.setattr(drain, "active_turn_blockers", lambda: [])
    monkeypatch.setattr(drain, "host_access_supervisor_count", lambda: 0)
    monkeypatch.setattr(drain, "PROCESS_OWNER", _ProcessOwner())
    monkeypatch.setattr(drain, "SIGNAL_FILE", tmp_path / "drain")
    monkeypatch.setattr(state, "broadcast_all", lambda *_args, **_kwargs: None)
    _seed_restart_hold(monkeypatch)
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    state._drain = None

    admission = drain.begin_turn_admission()
    assert admission is not None
    try:
        projected = drain.start_drain_projection("test")
        assert projected["turnAdmissions"] == 1
        assert projected["activeProcesses"] >= 1
        assert drain.check_drain_ready() is False

        drain.finish_turn_admission(admission)

        status = drain.get_drain_status()
        assert status["turnAdmissions"] == 0
        assert drain.check_drain_ready() is True
    finally:
        drain._turn_admissions = 0
        state._drain = None
        drain.SIGNAL_FILE.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_inherited_admission_expires_before_unrelated_work_finishes(
    isolated_db,
    monkeypatch,
) -> None:
    from infra import drain, state
    from tests.event_routing_helpers import mk_session
    from infra.send_service import send_thread_input
    from infra.send_types import InboxQueuedInput

    _seed_restart_hold(monkeypatch)
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    state._drain = None
    release_child = asyncio.Event()
    thread_id = mk_session("held-send-inherited")

    async def _attempt_send_after_owner_finishes() -> object:
        await release_child.wait()
        return await send_thread_input(
            thread_id,
            {"id": thread_id},
            "held-message",
            native_mode="event",
        )

    owner = drain.begin_turn_admission()
    assert owner is not None
    with drain.inherit_turn_admission(owner):
        leaked_child = asyncio.create_task(_attempt_send_after_owner_finishes())

    unrelated = drain.begin_turn_admission()
    assert unrelated is not None
    state._drain = {
        "reason": "test",
        "started_at": 0,
    }
    try:
        drain.finish_turn_admission(owner)
        assert drain._active_work_counts()["turnAdmissions"] == 1

        release_child.set()
        queued = await leaked_child
        assert isinstance(queued, InboxQueuedInput)

        drain.finish_turn_admission(unrelated)
        assert drain._active_work_counts()["turnAdmissions"] == 0
    finally:
        release_child.set()
        drain._turn_admissions = 0
        state._drain = None


@pytest.mark.asyncio
async def test_shared_send_queues_during_restart_hold(
    isolated_db,
    monkeypatch,
) -> None:
    from infra import drain, state
    from infra.send_service import send_thread_input
    from infra.send_types import InboxQueuedInput
    from tests.event_routing_helpers import mk_session

    _seed_restart_hold(monkeypatch)
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    _seed_drain_projection()
    thread_id = mk_session("held-send-shared")
    try:
        result = await send_thread_input(
            thread_id,
            {"id": thread_id},
            "message",
            native_mode="ws",
            ws=object(),
        )

        assert isinstance(result, InboxQueuedInput)
        assert result.receipt.thread_id == thread_id
        assert drain._turn_admissions == 0
    finally:
        state._drain = None


def test_idle_browser_sessions_do_not_permanently_block_drain(
    monkeypatch,
) -> None:
    from infra import drain, state

    _start_test_drain(
        monkeypatch,
        turns=(),
        supervisors=0,
        owner=_ProcessOwner(idle_browser_sessions=7),
    )
    try:
        status = drain.get_drain_status()

        assert status["activeProcesses"] == 0
        assert drain.check_drain_ready() is True
    finally:
        state._drain = None


def test_pending_browser_command_blocks_drain_without_a_process_group(
    monkeypatch,
) -> None:
    from infra import drain, state

    _start_test_drain(
        monkeypatch,
        turns=(),
        supervisors=0,
        owner=_ProcessOwner(browser_commands=1),
    )
    try:
        assert drain.get_drain_status()["activeProcesses"] == 1
        assert drain.check_drain_ready() is False
    finally:
        state._drain = None


def _owner_with_group(tmp_path, terminal_state: str):
    """Build a real ProcessOwner holding one group in an exact terminal state."""
    from host_access import processes

    owner = processes.ProcessOwner(runtime_dir=tmp_path)
    record = processes.ProcessRecord(
        handle="opaque",
        thread_id="thread-a",
        turn_id="turn-a",
        lease_id="lease-a",
        lease_revision=1,
        permit_id="permit-a",
        pid=42,
        pgid=42,
        creation_fingerprint="fingerprint",
        mode="background",
        log_path=tmp_path / "opaque.log",
        terminal_state=terminal_state,
    )
    owner._runtimes[record.handle] = processes._RuntimeProcess(
        record=record,
        process=object(),
    )
    return owner


def test_unverified_death_never_blocks_drain_readiness(
    tmp_path,
    monkeypatch,
) -> None:
    """A group we could not prove dead must not wedge every future stop.

    ``death_unverified`` is terminal: TERM and KILL have already been sent and
    no further in-process action can change it. Counting it as live work made
    ``check_drain_ready`` permanently false for the life of the backend, so
    every later graceful stop, restart and update on that machine failed.
    """
    from infra import drain, state

    _start_test_drain(
        monkeypatch,
        turns=(),
        supervisors=0,
        owner=_owner_with_group(tmp_path, "death_unverified"),
    )
    try:
        status = drain.get_drain_status()

        assert status["ownedProcessGroups"] == 0
        assert status["activeProcesses"] == 0
        assert drain.check_drain_ready() is True
    finally:
        state._drain = None


def test_running_process_group_still_blocks_drain_readiness(
    tmp_path,
    monkeypatch,
) -> None:
    from infra import drain, state

    _start_test_drain(
        monkeypatch,
        turns=(),
        supervisors=0,
        owner=_owner_with_group(tmp_path, "running"),
    )
    try:
        status = drain.get_drain_status()

        assert status["ownedProcessGroups"] == 1
        assert drain.check_drain_ready() is False
    finally:
        state._drain = None


def test_shutdown_still_retries_a_group_the_drain_stopped_counting(
    tmp_path,
) -> None:
    """Drain ignoring it must not make shutdown stop retrying or reporting it."""
    owner = _owner_with_group(tmp_path, "death_unverified")

    assert owner.active_process_count() == 1
    assert owner.live_count() == 1
    assert len(owner.running_records()) == 0
