"""Managed process admission and thread-family retirement are one lifecycle."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import psutil
import pytest

pytestmark = pytest.mark.integration


def _seed_threads(db) -> None:
    db.executemany(
        "INSERT INTO threads "
        "(id,user_id,spawned_by,agent_enabled,model,created_at,updated_at) "
        "VALUES (?,'user',?,?, 'native/test','t','t')",
        (
            ("root", None, 0),
            ("temporary", "root", 0),
            ("durable", "root", 1),
            ("unrelated", None, 0),
        ),
    )
    db.commit()


async def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.02)


def _pid_absent(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.asyncio
async def test_delete_handler_terminates_family_process_and_retires_row_and_logs(
    isolated_db,
    monkeypatch,
    tmp_path,
) -> None:
    """The real delete handler must not orphan family work or touch survivors."""
    from api import thread_mutations
    from automation.managed_process_events import on_background_transition
    from host_access import processes
    from infra.database import get_db

    db = get_db()
    try:
        _seed_threads(db)
    finally:
        db.close()

    owner = processes.ProcessOwner(
        runtime_dir=tmp_path / "runtime",
        term_grace_seconds=0.05,
        kill_grace_seconds=1.0,
    )
    owner.configure_transition_callback(on_background_transition)
    monkeypatch.setattr(processes, "PROCESS_OWNER", owner)
    monkeypatch.setattr(thread_mutations, "authenticate", lambda _token: {"id": "user"})

    async def quiesce(_thread_id: str, *, user_id: str):
        assert user_id == "user"
        return {}

    monkeypatch.setattr(
        thread_mutations.runtime_boundary,
        "quiesce_thread_runtimes",
        quiesce,
    )

    started = {}
    try:
        for thread_id in ("temporary", "durable", "unrelated"):
            result = await owner.spawn(
                [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                thread_id=thread_id,
                turn_id=f"turn-{thread_id}",
                cwd=Path.cwd(),
                env=os.environ,
                timeout_seconds=30,
                background=True,
                **(
                    {"long_running": True, "description": "temporary job"}
                    if thread_id == "temporary"
                    else {}
                ),
            )
            started[thread_id] = result
            await _wait_until(
                lambda result=result, thread_id=thread_id: "ready" in owner.read_logs(
                    result.handle,
                    thread_id=thread_id,
                )
            )
            assert owner.record(result.handle).terminal_state == "running"

        temporary = owner.record(started["temporary"].handle)
        durable = owner.record(started["durable"].handle)
        unrelated = owner.record(started["unrelated"].handle)

        await thread_mutations.delete_thread("root", "token")

        await _wait_until(lambda: _pid_absent(temporary.pid))
        assert not temporary.log_path.exists()
        db = get_db()
        try:
            remaining = {
                row["thread_id"]
                for row in db.execute(
                    "SELECT thread_id FROM managed_process_handles ORDER BY thread_id"
                ).fetchall()
            }
            assert db.execute(
                "SELECT COUNT(*) FROM messages WHERE thread_id IN ('root','temporary')"
            ).fetchone()[0] == 0
        finally:
            db.close()
        assert remaining == {"durable", "unrelated"}
        assert not _pid_absent(durable.pid)
        assert not _pid_absent(unrelated.pid)
        assert durable.log_path.exists()
        assert unrelated.log_path.exists()
    finally:
        try:
            await owner.shutdown()
        except processes.ProcessCleanupError:
            pass


def _seed_one(thread_id: str = "thread") -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,agent_enabled,model,created_at,updated_at) "
            "VALUES (?,'user',0,'native/test','t','t')",
            (thread_id,),
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_background_command_is_persisted_before_requested_exec(
    isolated_db,
    tmp_path,
) -> None:
    from automation.managed_process_events import on_background_transition
    from host_access import processes

    _seed_one()
    owner = processes.ProcessOwner(runtime_dir=tmp_path / "runtime")
    marker = tmp_path / "executed"
    persistence_entered = asyncio.Event()
    release_persistence = asyncio.Event()

    async def delayed_transition(record, kind):
        if kind == "spawned":
            persistence_entered.set()
            await release_persistence.wait()
        await on_background_transition(record, kind)

    owner.configure_transition_callback(delayed_transition)
    launching = asyncio.create_task(
        owner.spawn(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
            thread_id="thread",
            turn_id="turn",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=30,
            background=True,
        )
    )
    await persistence_entered.wait()
    assert not marker.exists()

    release_persistence.set()
    started = await launching
    await _wait_until(marker.exists)
    assert started.handle
    await owner.stop(started.handle, thread_id="thread")
    await owner.shutdown()


@pytest.mark.asyncio
async def test_cancellation_during_spawned_persistence_never_executes_command(
    isolated_db,
    tmp_path,
) -> None:
    from automation.managed_process_events import on_background_transition
    from host_access import processes
    from infra.database import get_db

    _seed_one()
    owner = processes.ProcessOwner(runtime_dir=tmp_path / "runtime")
    marker = tmp_path / "must-not-execute"
    persistence_entered = asyncio.Event()
    release_persistence = asyncio.Event()

    async def delayed_transition(record, kind):
        if kind == "spawned":
            persistence_entered.set()
            await release_persistence.wait()
        await on_background_transition(record, kind)

    owner.configure_transition_callback(delayed_transition)
    launching = asyncio.create_task(
        owner.spawn(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')"],
            thread_id="thread",
            turn_id="turn",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=30,
            background=True,
        )
    )
    await persistence_entered.wait()
    launching.cancel()
    await asyncio.sleep(0)
    assert not marker.exists()
    release_persistence.set()
    with pytest.raises(asyncio.CancelledError):
        await launching
    assert not marker.exists()

    db = get_db()
    try:
        row = db.execute(
            "SELECT outcome,reason FROM managed_process_handles"
        ).fetchone()
        assert tuple(row) == ("failed", "launch_gate_refused")
    finally:
        db.close()
    await owner.shutdown()


@pytest.mark.asyncio
async def test_spawned_persistence_failure_never_executes_or_returns_handle(
    isolated_db,
    monkeypatch,
    tmp_path,
) -> None:
    from host_access import managed_lifecycle, processes
    from infra.database import get_db

    _seed_one()
    owner = processes.ProcessOwner(runtime_dir=tmp_path / "runtime")
    marker = tmp_path / "must-not-execute"

    async def fail_persistence(_record, kind):
        assert kind == "spawned"
        raise RuntimeError("persistence unavailable")

    monkeypatch.setattr(managed_lifecycle, "TRANSITION_RETRY_DELAYS", ())
    owner.configure_transition_callback(fail_persistence)
    with pytest.raises(RuntimeError, match="persistence unavailable"):
        await owner.spawn(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')",
            ],
            thread_id="thread",
            turn_id="turn",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=30,
            background=True,
        )
    assert not marker.exists()
    assert owner._runtimes == {}
    db = get_db()
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM managed_process_handles"
        ).fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_close_barrier_refuses_new_spawn_before_allocating_files(
    isolated_db,
    tmp_path,
) -> None:
    from automation.managed_process_events import on_background_transition
    from host_access import processes

    _seed_one()
    owner = processes.ProcessOwner(runtime_dir=tmp_path / "runtime")
    owner.configure_transition_callback(on_background_transition)
    token = await owner.prepare_thread_close(("thread",))
    before = tuple(owner.process_dir.glob("*"))
    with pytest.raises(processes.ProcessOwnerError, match="admission is closed"):
        await owner.spawn(
            [sys.executable, "-c", "print('must not start')"],
            thread_id="thread",
            turn_id="turn",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=30,
            background=True,
        )
    assert tuple(owner.process_dir.glob("*")) == before
    assert owner._runtimes == {}
    await owner.abort_thread_close(token)


@pytest.mark.asyncio
async def test_dead_gate_broken_pipe_is_failed_not_started_and_returns_no_handle(
    isolated_db,
    monkeypatch,
    tmp_path,
) -> None:
    from automation.managed_process_events import on_background_transition
    from host_access import processes
    from infra.database import get_db

    _seed_one()
    owner = processes.ProcessOwner(runtime_dir=tmp_path / "runtime")
    marker = tmp_path / "must-not-execute"
    watcher_was_installed = False

    async def kill_gate_after_persist(record, kind):
        await on_background_transition(record, kind)
        if kind == "spawned":
            os.kill(record.pid, signal.SIGKILL)
            await asyncio.sleep(0.05)

    import signal

    real_write = os.write

    def observe_broken_pipe(descriptor: int, payload: bytes) -> int:
        nonlocal watcher_was_installed
        runtime = next(iter(owner._runtimes.values()))
        watcher_was_installed = (
            runtime.watcher_task is not None and runtime.spool_task is not None
        )
        return real_write(descriptor, payload)

    monkeypatch.setattr(processes.os, "write", observe_broken_pipe)
    owner.configure_transition_callback(kill_gate_after_persist)
    with pytest.raises(
        processes.ProcessOwnerError,
        match="launch gate refused",
    ):
        await owner.spawn(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')"],
            thread_id="thread",
            turn_id="turn",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=30,
            background=True,
        )
    assert watcher_was_installed is True
    assert not marker.exists()
    db = get_db()
    try:
        row = db.execute(
            "SELECT outcome,reason FROM managed_process_handles"
        ).fetchone()
        assert tuple(row) == ("failed", "launch_gate_refused")
    finally:
        db.close()
    await owner.shutdown()


@pytest.mark.asyncio
async def test_successful_go_has_no_cancellation_ownership_gap(
    isolated_db,
    monkeypatch,
    tmp_path,
) -> None:
    from automation.managed_process_events import on_background_transition
    from host_access import processes

    _seed_one()
    owner = processes.ProcessOwner(runtime_dir=tmp_path / "runtime")
    owner.configure_transition_callback(on_background_transition)
    real_write = os.write
    saw_owned_tasks = False

    def cancel_after_go(descriptor: int, payload: bytes) -> int:
        nonlocal saw_owned_tasks
        runtime = next(iter(owner._runtimes.values()))
        saw_owned_tasks = (
            runtime.watcher_task is not None and runtime.spool_task is not None
        )
        current = asyncio.current_task()
        assert current is not None
        asyncio.get_running_loop().call_soon(current.cancel)
        return real_write(descriptor, payload)

    monkeypatch.setattr(processes.os, "write", cancel_after_go)
    launching = asyncio.create_task(
        owner.spawn(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            thread_id="thread",
            turn_id="turn",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=30,
            background=True,
        )
    )
    started = await launching
    assert saw_owned_tasks
    assert started.handle in owner._runtimes
    await owner.stop(started.handle, thread_id="thread")
    await owner.shutdown()


@pytest.mark.asyncio
async def test_delayed_terminal_transition_cannot_be_pruned_and_log_is_retained(
    isolated_db,
    tmp_path,
) -> None:
    from automation.managed_process_events import on_background_transition
    from host_access import processes

    _seed_one()
    owner = processes.ProcessOwner(
        runtime_dir=tmp_path / "runtime",
        max_records=1,
    )
    terminal_entered = asyncio.Event()
    release_terminal = asyncio.Event()

    async def delayed_terminal(record, kind):
        if kind == "terminal":
            terminal_entered.set()
            await release_terminal.wait()
        await on_background_transition(record, kind)

    owner.configure_transition_callback(delayed_terminal)
    started = await owner.spawn(
        [sys.executable, "-c", "print('done')"],
        thread_id="thread",
        turn_id="turn-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=30,
        background=True,
    )
    runtime = owner._runtimes[started.handle]
    await terminal_entered.wait()
    assert runtime.record.terminal_state == "exited"

    with pytest.raises(processes.ProcessCapacityError, match="capacity"):
        await owner.spawn(
            [sys.executable, "-c", "print('too-early')"],
            thread_id="thread",
            turn_id="turn-b",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=30,
            background=False,
        )

    release_terminal.set()
    assert runtime.transition_task is not None
    await runtime.transition_task
    log_path = runtime.record.log_path
    replacement = await owner.spawn(
        [sys.executable, "-c", "print('replacement')"],
        thread_id="thread",
        turn_id="turn-c",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=30,
        background=False,
    )
    assert replacement.stdout == b"replacement\n"
    assert log_path.exists()
    await owner.shutdown()


def _durable_row(handle: str, thread_id: str, log_path: Path) -> dict:
    return {
        "handle": handle,
        "thread_id": thread_id,
        "turn_id": f"turn-{thread_id}",
        "lease_id": None,
        "description": "job",
        "long_running": 1,
        "pid": 123,
        "pgid": 123,
        "creation_fingerprint": "fingerprint",
        "log_path": str(log_path),
        "state": "running",
        "outcome": None,
        "reason": None,
        "exit_code": None,
        "started_at": "2026-09-29T00:00:00Z",
        "last_output_at": None,
        "stalled_since": None,
        "stall_after_seconds": 900,
        "max_lifetime_seconds": 86400,
        "ended_at": None,
        "log_truncated": 0,
    }


def test_callback_before_and_after_tombstone_cannot_resurrect_handle(
    isolated_db,
    tmp_path,
) -> None:
    from automation.managed_process_events import apply_transition
    from infra.database import get_db
    from infra.managed_process_store import stage_thread_family_retirements

    _seed_one()
    process_dir = tmp_path / "managed-processes"
    process_dir.mkdir()
    head = process_dir / "handle.log"
    head.write_text("head")
    row = _durable_row("handle", "thread", head)
    assert apply_transition(row, "spawned") is None
    assert apply_transition(
        {**row, "state": "exited", "outcome": "completed"},
        "terminal",
    ) == "thread"

    db = get_db()
    try:
        stage_thread_family_retirements(db, ("thread",))
        db.commit()
    finally:
        db.close()

    assert apply_transition(
        {**row, "state": "exited", "outcome": "completed"},
        "terminal",
    ) is None
    db = get_db()
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM managed_process_handles"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_unlink_failure_is_retained_then_startup_drain_retries(
    isolated_db,
    monkeypatch,
    tmp_path,
) -> None:
    from automation.managed_process_events import drain_retirements
    from automation.managed_process_recovery import reconcile_managed_processes
    from host_access import processes
    from infra.database import get_db
    from infra.managed_process_store import (
        stage_thread_family_retirements,
        upsert_transition,
    )

    _seed_one()
    owner = processes.ProcessOwner(runtime_dir=tmp_path / "runtime")
    monkeypatch.setattr(processes, "PROCESS_OWNER", owner)
    owner.process_dir.mkdir(parents=True)
    head = owner.process_dir / "handle.log"
    tail = owner.process_dir / "handle.tail.log"
    head.write_text("head")
    tail.write_text("tail")
    upsert_transition(_durable_row("handle", "thread", head))
    db = get_db()
    try:
        stage_thread_family_retirements(db, ("thread",))
        db.commit()
    finally:
        db.close()

    real_unlink = Path.unlink
    failed = False

    def fail_tail_once(path: Path, *args, **kwargs):
        nonlocal failed
        if path == tail and not failed:
            failed = True
            raise OSError("synthetic unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_tail_once)
    assert drain_retirements() == ()
    assert not head.exists() and tail.exists()
    db = get_db()
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM managed_process_retirements"
        ).fetchone()[0] == 1
    finally:
        db.close()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert reconcile_managed_processes() == ()
    assert not tail.exists()
    db = get_db()
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM managed_process_retirements"
        ).fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_persisted_unverified_row_refuses_delete_and_lifts_barrier(
    isolated_db,
    monkeypatch,
    tmp_path,
) -> None:
    from api import thread_mutations
    from automation.managed_process_events import on_background_transition
    from fastapi import HTTPException
    from host_access import processes
    from infra.database import get_db
    from infra.managed_process_store import upsert_transition

    _seed_one("root")
    owner = processes.ProcessOwner(runtime_dir=tmp_path / "runtime")
    owner.configure_transition_callback(on_background_transition)
    monkeypatch.setattr(processes, "PROCESS_OWNER", owner)
    monkeypatch.setattr(thread_mutations, "authenticate", lambda _token: {"id": "user"})

    async def quiesce(_thread_id: str, *, user_id: str):
        return {}

    monkeypatch.setattr(
        thread_mutations.runtime_boundary,
        "quiesce_thread_runtimes",
        quiesce,
    )
    owner.process_dir.mkdir(parents=True)
    head = owner.process_dir / "orphan.log"
    head.write_text("orphan")
    upsert_transition(_durable_row("orphan", "root", head))

    with pytest.raises(HTTPException) as refused:
        await thread_mutations.delete_thread("root", "token")
    assert refused.value.status_code == 409
    assert owner._closing_threads == set()
    db = get_db()
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM threads WHERE id='root'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM managed_process_handles WHERE handle='orphan'"
        ).fetchone()[0] == 1
    finally:
        db.close()
    assert head.exists()


@pytest.mark.asyncio
async def test_family_race_aborts_process_close_barrier(
    isolated_db,
    monkeypatch,
    tmp_path,
) -> None:
    from api import thread_deletion, thread_mutations
    from automation.managed_process_events import on_background_transition
    from fastapi import HTTPException
    from host_access import processes
    from infra.database import get_db

    _seed_one("root")
    owner = processes.ProcessOwner(runtime_dir=tmp_path / "runtime")
    owner.configure_transition_callback(on_background_transition)
    monkeypatch.setattr(processes, "PROCESS_OWNER", owner)
    monkeypatch.setattr(thread_mutations, "authenticate", lambda _token: {"id": "user"})

    async def quiesce(_thread_id: str, *, user_id: str):
        return {}

    monkeypatch.setattr(
        thread_mutations.runtime_boundary,
        "quiesce_thread_runtimes",
        quiesce,
    )
    started = await owner.spawn(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        thread_id="root",
        turn_id="turn-root",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=30,
        background=True,
        long_running=True,
        description="race truth",
    )
    record = owner.record(started.handle)
    snapshot = thread_deletion.family_versions

    def race_after_snapshot(family):
        versions = snapshot(family)
        from infra.database import get_db

        db = get_db()
        try:
            db.execute(
                "INSERT INTO threads "
                "(id,user_id,spawned_by,agent_enabled,model,created_at,updated_at) "
                "VALUES ('late','user','root',0,'native/test','t','t')"
            )
            db.commit()
        finally:
            db.close()
        return versions

    monkeypatch.setattr(thread_deletion, "family_versions", race_after_snapshot)
    with pytest.raises(HTTPException) as refused:
        await thread_mutations.delete_thread("root", "token")
    assert refused.value.status_code == 409
    assert owner._closing_threads == set()
    await _wait_until(lambda: _pid_absent(record.pid))
    db = get_db()
    try:
        persisted = db.execute(
            "SELECT outcome FROM managed_process_handles WHERE handle=?",
            (started.handle,),
        ).fetchone()
        event = db.execute(
            "SELECT content FROM messages WHERE thread_id='root' "
            "AND content LIKE '[EVENT: managed_process.failed]%'",
        ).fetchone()
    finally:
        db.close()
    assert persisted is not None and persisted["outcome"] == "failed"
    assert event is not None
    await owner.shutdown()


def test_only_four_approved_long_running_wake_outcomes() -> None:
    from automation.managed_process_events import event_for

    row = _durable_row("handle", "thread", Path("/tmp/handle.log"))
    outcomes = set()
    stalled = event_for(
        {
            **row,
            "stalled_since": "2026-09-29T00:15:00Z",
        },
        "stalled",
    )
    assert stalled is not None
    outcomes.add(stalled["internal_event"])
    for outcome in ("completed", "failed", "interrupted_by_restart"):
        event = event_for(
            {
                **row,
                "state": "exited",
                "outcome": outcome,
                "ended_at": "2026-09-29T00:01:00Z",
                "exit_code": 0,
            },
            "terminal",
        )
        assert event is not None
        outcomes.add(event["internal_event"])
    assert outcomes == {
        "managed_process.stalled",
        "managed_process.completed",
        "managed_process.failed",
        "managed_process.interrupted_by_restart",
    }
    assert event_for({**row, "outcome": "death_unverified"}, "terminal") is None
    assert event_for({**row, "outcome": "stopped"}, "terminal") is None
