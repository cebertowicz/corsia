"""Approved long-running managed background processes (#2152 slice 1)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.event_routing_helpers import mk_session

pytestmark = pytest.mark.integration


# A hang guard, not a pacing budget: a loaded shard must not cross it while a
# script that sleeps for seconds is still behaving correctly.
HANG_GUARD_SECONDS = 30.0


async def _wait_until(predicate, *, timeout: float = HANG_GUARD_SECONDS) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.02)


async def _record_transition(_record, _kind) -> None:
    """Acknowledge persistence when a test does not need the real DB bridge."""


@pytest.fixture
async def owner(tmp_path):
    from host_access.processes import ProcessOwner

    process_owner = ProcessOwner(
        runtime_dir=tmp_path,
        term_grace_seconds=0.1,
        kill_grace_seconds=1.0,
        long_running_poll_seconds=0.05,
        long_running_tail_bytes=2_048,
    )
    process_owner.configure_transition_callback(_record_transition)
    yield process_owner
    await process_owner.shutdown()


async def _spawn(owner, script: str, *, thread_id: str = "thread-a", **options):
    options.setdefault("timeout_seconds", 30.0)
    return await owner.spawn(
        [sys.executable, "-c", script],
        thread_id=thread_id,
        turn_id="turn-a",
        cwd=Path.cwd(),
        env=os.environ,
        background=True,
        **options,
    )


def _long(description: str = "job", **options):
    return {"long_running": True, "description": description, **options}


def _durable_row(
    handle: str,
    *,
    thread_id: str = "thread-a",
    state: str = "death_unverified",
) -> dict[str, object]:
    return {
        "handle": handle,
        "thread_id": thread_id,
        "turn_id": "turn-before-restart",
        "lease_id": None,
        "description": f"durable {handle}",
        "long_running": 1,
        "pid": os.getpid(),
        "pgid": os.getpid(),
        "creation_fingerprint": "restart-fingerprint",
        "log_path": f"/tmp/{handle}.log",
        "state": state,
        "outcome": "death_unverified" if state == "death_unverified" else None,
        "reason": "backend_restart" if state == "death_unverified" else None,
        "exit_code": None,
        "started_at": "2026-09-29T00:00:00Z",
        "last_output_at": None,
        "stalled_since": None,
        "stall_after_seconds": 900,
        "max_lifetime_seconds": 86400,
        "ended_at": None,
        "log_truncated": 0,
        "event_message_id": None,
    }


def _ended(owner, handle: str):
    return lambda: owner.record(handle).ended_at is not None


def _pid_gone(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _events(thread_id: str) -> list[str]:
    from infra.database import get_db

    db = get_db()
    try:
        rows = db.execute(
            "SELECT content FROM messages WHERE thread_id=? AND role='system' "
            "AND content LIKE '[EVENT: managed_process.%' ORDER BY input_order",
            (thread_id,),
        ).fetchall()
    finally:
        db.close()
    return [str(row["content"]) for row in rows]


@pytest.fixture
def producer(isolated_db, owner, monkeypatch):
    from automation import managed_process_events
    from infra import thread_execution

    monkeypatch.setattr(thread_execution, "drive_thread", AsyncMock())
    owner.configure_transition_callback(managed_process_events.on_background_transition)
    return managed_process_events


async def test_background_log_is_not_created_before_durable_locator(
    owner,
    monkeypatch,
) -> None:
    class CrashCut(RuntimeError):
        pass

    observed: list[Path] = []

    def cut_before_persistence(runtime, _kind):
        observed.append(runtime.record.log_path)
        assert not runtime.record.log_path.exists()
        raise CrashCut

    monkeypatch.setattr(owner, "_start_transition", cut_before_persistence)
    with pytest.raises(CrashCut):
        await _spawn(owner, "print('never executes')")

    assert len(observed) == 1
    assert not observed[0].exists()
    assert owner.live_count() == 0


def test_tail_roll_never_leaves_an_unowned_staging_log(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import managed_log

    tail = tmp_path / "handle.tail.log"
    tail.write_bytes(b"x" * 256)

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(managed_log.os, "replace", fail_replace)
    try:
        managed_log.retain_tail(tail, 128)
    except OSError:
        pass

    assert not (tmp_path / "handle.tail.log.roll").exists()
    assert tail.stat().st_size <= 128


def test_tail_roll_failure_retains_only_the_durably_indexed_path(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import managed_log

    tail = tmp_path / "handle.tail.log"
    tail.write_bytes(b"x" * 256)
    real_open = Path.open

    class FailingTruncate:
        def __init__(self, opened):
            self._opened = opened

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._opened.close()

        def __getattr__(self, name):
            return getattr(self._opened, name)

        def truncate(self):
            raise OSError("simulated truncate failure")

    def failing_open(path, mode="r", *args, **kwargs):
        opened = real_open(path, mode, *args, **kwargs)
        if path == tail and mode == "r+b":
            return FailingTruncate(opened)
        return opened

    monkeypatch.setattr(managed_log.Path, "open", failing_open)
    with pytest.raises(OSError, match="truncate failure"):
        managed_log.retain_tail(tail, 128)

    assert tail.exists()
    assert not (tmp_path / "handle.tail.log.roll").exists()


async def test_long_running_tail_stays_within_configured_capacity(owner) -> None:
    from host_access.managed_runtime import tail_log_path

    owner.long_running_tail_bytes = 128
    started = await _spawn(
        owner,
        "print('x' * 199, flush=True)",
        **_long("bounded tail"),
    )
    await _wait_until(_ended(owner, started.handle))

    assert tail_log_path(owner.record(started.handle).log_path).stat().st_size <= 128


async def test_long_running_ignores_default_timeout_but_obeys_lifetime_cap(owner) -> None:
    from host_access.managed_lifecycle import outcome_for

    outlived = await _spawn(
        owner,
        "import time;time.sleep(0.5)",
        timeout_seconds=0.1,
        **_long("outlived"),
    )
    capped = await _spawn(
        owner,
        "import time;time.sleep(30)",
        **_long("capped", max_lifetime_seconds=0.2),
    )
    await _wait_until(_ended(owner, outlived.handle))
    await _wait_until(_ended(owner, capped.handle))

    assert outcome_for(owner.record(outlived.handle)) == "completed"
    capped_record = owner.record(capped.handle)
    assert capped_record.reason == "max_lifetime"
    assert outcome_for(capped_record) == "failed"


async def test_stall_and_terminal_events_use_only_approved_outcomes(
    owner,
    producer,
) -> None:
    mk_session("thread-a")
    kinds: list[str] = []

    async def record_kind(record, kind) -> None:
        await producer.on_background_transition(record, kind)
        kinds.append(kind)

    owner.configure_transition_callback(record_kind)
    # Quiet time counts from the start until the first output, so a slow
    # interpreter start can add one stall before "one". The two 1.5s silences
    # always stall; any extra stall is resumed, so only the last stays open.
    # Stall messages are keyed by whole-second stalled_since, so an extra stall
    # may share a message with its neighbour: count messages as a range.
    script = (
        "import time;print('one',flush=True);time.sleep(1.5);"
        "print('two',flush=True);time.sleep(1.5)"
    )
    started = await _spawn(
        owner,
        script,
        **_long("stalls", stall_after_seconds=0.5),
    )
    await _wait_until(_ended(owner, started.handle))
    await owner.drain_transitions()

    events = _events("thread-a")
    stalled = sum(item.startswith("[EVENT: managed_process.stalled]") for item in events)
    assert 2 <= stalled <= kinds.count("stalled")
    assert kinds.count("stalled") == kinds.count("resumed") + 1
    assert sum(item.startswith("[EVENT: managed_process.completed]") for item in events) == 1
    assert all("death_unverified" not in item for item in events)


async def test_long_running_limits_are_two_per_thread_and_six_per_instance(owner) -> None:
    from host_access.processes import ProcessCapacityError

    sleeper = "import time;time.sleep(30)"
    first = await _spawn(owner, sleeper, **_long("one"))
    second = await _spawn(owner, sleeper, **_long("two"))
    with pytest.raises(ProcessCapacityError, match="limit is 2"):
        await _spawn(owner, sleeper, **_long("three"))

    handles = [first, second]
    for index in range(2, 6):
        handles.append(
            await _spawn(
                owner,
                sleeper,
                thread_id=f"thread-{index}",
                **_long(f"job-{index}"),
            )
        )
    with pytest.raises(ProcessCapacityError, match="limit is 6"):
        await _spawn(owner, sleeper, thread_id="seventh", **_long("seven"))

    for index, started in enumerate(handles):
        thread_id = "thread-a" if index < 2 else f"thread-{index}"
        await owner.stop(started.handle, thread_id=thread_id)


async def test_restart_unverified_rows_reserve_per_thread_capacity(
    isolated_db,
    owner,
    monkeypatch,
) -> None:
    from automation import managed_process_events
    from host_access import processes
    from host_access.processes import ProcessCapacityError
    from infra.managed_process_store import upsert_transition

    mk_session("thread-a")
    monkeypatch.setattr(processes, "PROCESS_OWNER", owner)
    managed_process_events.install()
    upsert_transition(_durable_row("before-restart-one"))
    upsert_transition(_durable_row("before-restart-two"))

    with pytest.raises(ProcessCapacityError, match="limit is 2") as refused:
        await _spawn(
            owner,
            "import time;time.sleep(30)",
            **_long("after restart"),
        )
    message = str(refused.value)
    assert "death_unverified handle will not send a completion event" in message
    assert "Do not evict user-named work without authorization" in message
    assert "managed_process_stop" in message
    assert "before-restart-one" in message
    assert "before-restart-two" in message


async def test_restart_unverified_rows_reserve_instance_capacity_without_eviction(
    isolated_db,
    owner,
    monkeypatch,
) -> None:
    from automation import managed_process_events
    from host_access import processes
    from host_access.processes import ProcessCapacityError
    from infra.managed_process_store import upsert_transition

    mk_session("thread-a")
    monkeypatch.setattr(processes, "PROCESS_OWNER", owner)
    managed_process_events.install()
    upsert_transition(_durable_row("mine", thread_id="thread-a"))
    for index in range(5):
        upsert_transition(
            _durable_row(f"foreign-{index}", thread_id=f"thread-{index}")
        )

    with pytest.raises(ProcessCapacityError, match="limit is 6") as refused:
        await _spawn(
            owner,
            "import time;time.sleep(30)",
            **_long("after restart"),
        )
    message = str(refused.value)
    assert "Wait for any running process to finish" in message
    assert "managed_process_stop: mine" in message
    assert "foreign-" not in message


async def test_instance_capacity_without_owned_handle_says_wait_or_raise(
    isolated_db,
    owner,
    monkeypatch,
) -> None:
    from automation import managed_process_events
    from host_access import processes
    from host_access.processes import ProcessCapacityError
    from infra.managed_process_store import upsert_transition

    monkeypatch.setattr(processes, "PROCESS_OWNER", owner)
    managed_process_events.install()
    for index in range(6):
        upsert_transition(
            _durable_row(f"foreign-{index}", thread_id=f"thread-{index}")
        )

    with pytest.raises(ProcessCapacityError, match="limit is 6") as refused:
        await _spawn(
            owner,
            "import time;time.sleep(30)",
            thread_id="newcomer",
            **_long("after restart"),
        )
    message = str(refused.value)
    assert "Wait for an owning thread to resolve one" in message
    assert "tell the user that instance capacity is blocked" in message
    assert "do not stop another thread's work" in message
    assert "managed_process_stop" not in message
    assert "foreign-" not in message


async def test_durable_stop_recovers_unowned_restart_row(
    isolated_db,
    owner,
    monkeypatch,
) -> None:
    from automation import managed_process_recovery
    from infra.managed_process_store import get_handle, upsert_transition
    from tools.managed_processes import ManagedProcessStopTool

    upsert_transition(_durable_row("unowned"))
    monkeypatch.setattr(
        managed_process_recovery,
        "_reconcile_orphan_group",
        lambda _row: "stopped",
    )

    result = json.loads(
        await ManagedProcessStopTool(owner).execute(
            "unowned",
            _thread_id="thread-a",
        )
    )

    assert result == {"handle": "unowned", "state": "exited"}
    persisted = get_handle("unowned")
    assert persisted is not None
    assert persisted["outcome"] == "stopped"
    assert persisted["reason"] == "managed_stop"
    assert _events("thread-a") == []


async def test_durable_stop_keeps_capacity_when_death_is_still_unverified(
    isolated_db,
    owner,
    monkeypatch,
) -> None:
    from automation import managed_process_recovery
    from host_access.processes import ProcessCleanupError
    from infra.managed_process_store import get_handle, upsert_transition
    from tools.managed_processes import ManagedProcessStopTool

    upsert_transition(_durable_row("still-unverified"))
    monkeypatch.setattr(
        managed_process_recovery,
        "_reconcile_orphan_group",
        lambda _row: "unverified",
    )

    with pytest.raises(ProcessCleanupError, match="could not be verified") as refused:
        await ManagedProcessStopTool(owner).execute(
            "still-unverified",
            _thread_id="thread-a",
        )
    message = str(refused.value)
    assert "handle, log, and capacity reservation remain" in message
    assert "Do not retry in a loop or touch another thread's work" in message
    assert "tell the user now that this thread's capacity is blocked" in message
    assert "A later managed_process_stop is the only supported agent recovery" in message

    persisted = get_handle("still-unverified")
    assert persisted is not None
    assert persisted["state"] == "death_unverified"
    assert persisted["outcome"] == "death_unverified"


async def test_live_stop_explains_unverified_death_recovery(
    isolated_db,
    owner,
    monkeypatch,
) -> None:
    from host_access.processes import ProcessCleanupError
    from tools.managed_processes import ManagedProcessStopTool

    original = ProcessCleanupError("owned process group 123 survived TERM and KILL")

    async def _unverified_stop(*_args, **_kwargs):
        raise original

    monkeypatch.setattr(owner, "stop", _unverified_stop)
    monkeypatch.setattr(
        owner,
        "status",
        lambda *_args, **_kwargs: SimpleNamespace(terminal_state="death_unverified"),
    )

    with pytest.raises(ProcessCleanupError, match="survived TERM and KILL") as refused:
        await ManagedProcessStopTool(owner).execute(
            "still-live",
            _thread_id="thread-a",
        )
    message = str(refused.value)
    assert refused.value.__cause__ is original
    assert "handle, log, and capacity reservation remain" in message
    assert "Do not retry in a loop or touch another thread's work" in message
    assert "tell the user now that this thread's capacity is blocked" in message
    assert "A later managed_process_stop is the only supported agent recovery" in message


async def test_live_stop_does_not_mislabel_terminal_cleanup_failure(
    isolated_db,
    owner,
    monkeypatch,
) -> None:
    from host_access.processes import ProcessCleanupError
    from tools.managed_processes import ManagedProcessStopTool

    cleanup_error = "owned process retained 1 cleanup path(s)"

    async def _terminal_cleanup_failure(*_args, **_kwargs):
        raise ProcessCleanupError(cleanup_error)

    monkeypatch.setattr(owner, "stop", _terminal_cleanup_failure)
    monkeypatch.setattr(
        owner,
        "status",
        lambda *_args, **_kwargs: SimpleNamespace(terminal_state="exited"),
    )

    with pytest.raises(ProcessCleanupError) as refused:
        await ManagedProcessStopTool(owner).execute(
            "already-terminal",
            _thread_id="thread-a",
        )
    assert str(refused.value) == cleanup_error


async def test_managed_process_list_prioritizes_unsettled_recovery_handles(
    isolated_db,
    owner,
) -> None:
    from infra.managed_process_store import upsert_transition
    from tools.managed_processes import ManagedProcessListTool

    assert "unsettled first" in ManagedProcessListTool.description
    active = {
        **_durable_row("old-active", state="running"),
        "started_at": "2026-09-28T00:00:00Z",
        "outcome": None,
        "reason": None,
    }
    upsert_transition(active)
    for index in range(20):
        upsert_transition(
            {
                **_durable_row(f"new-terminal-{index}", state="exited"),
                "long_running": 0,
                "outcome": "completed",
                "reason": "exit",
                "exit_code": 0,
                "started_at": f"2026-09-29T00:{index:02d}:00Z",
                "ended_at": f"2026-09-29T00:{index:02d}:01Z",
            }
        )

    listed = json.loads(
        await ManagedProcessListTool(owner).execute(_thread_id="thread-a")
    )
    handles = [item["handle"] for item in listed["handles"]]
    assert len(handles) == 20
    assert handles[0] == "old-active"


async def test_head_tail_log_keeps_final_result_and_durable_tools_are_scoped(
    isolated_db,
    owner,
) -> None:
    from infra.managed_process_store import upsert_transition
    from tools.managed_processes import (
        ManagedProcessListTool,
        ManagedProcessLogsTool,
        ManagedProcessStatusTool,
    )

    script = (
        "import sys\n"
        "for _ in range(64): sys.stdout.write('x' * 255 + '\\n')\n"
        "print('FINAL-RESULT-LINE', flush=True)\n"
    )
    started = await _spawn(owner, script, **_long("output"))
    await _wait_until(_ended(owner, started.handle))
    logs = json.loads(
        await ManagedProcessLogsTool(owner).execute(
            started.handle,
            limit_bytes=1_000,
            _thread_id="thread-a",
        )
    )
    assert logs["logs"].rstrip().endswith("FINAL-RESULT-LINE")
    assert logs["log_truncated"] is True

    row = {
        "handle": "persisted",
        "thread_id": "thread-a",
        "turn_id": "turn-a",
        "lease_id": None,
        "description": "persisted job",
        "long_running": 1,
        "pid": 10,
        "pgid": 10,
        "creation_fingerprint": "fingerprint",
        "log_path": str(owner.process_dir / "persisted.log"),
        "state": "interrupted_by_restart",
        "outcome": "interrupted_by_restart",
        "reason": "backend_restart",
        "exit_code": None,
        "started_at": "2026-09-29T00:00:00Z",
        "last_output_at": None,
        "stalled_since": None,
        "stall_after_seconds": 900,
        "max_lifetime_seconds": 86400,
        "ended_at": "2026-09-29T00:01:00Z",
        "log_truncated": 0,
        "event_message_id": None,
    }
    upsert_transition(row)
    upsert_transition({**row, "handle": "theirs", "thread_id": "thread-b"})

    listed = json.loads(
        await ManagedProcessListTool(owner).execute(_thread_id="thread-a")
    )
    assert "theirs" not in {item["handle"] for item in listed["handles"]}
    status = json.loads(
        await ManagedProcessStatusTool(owner).execute(
            "persisted",
            _thread_id="thread-a",
        )
    )
    assert status["outcome"] == "interrupted_by_restart"
    with pytest.raises(PermissionError, match="owned"):
        await ManagedProcessStatusTool(owner).execute(
            "theirs",
            _thread_id="thread-a",
        )


def test_startup_reconciliation_settles_orphan_and_never_emits_fifth_outcome(
    isolated_db,
    tmp_path,
    monkeypatch,
) -> None:
    from automation import managed_process_recovery
    from host_access.processes import _process_creation_fingerprint
    from infra.managed_process_store import get_handle, upsert_transition

    mk_session("thread-a")
    launcher = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import subprocess;"
                "print(subprocess.Popen(['sleep','30'],start_new_session=True,"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).pid)"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pid = int(launcher.stdout)
    row = {
        "handle": "orphan",
        "thread_id": "thread-a",
        "turn_id": "turn-a",
        "lease_id": None,
        "description": "orphan job",
        "long_running": 1,
        "pid": pid,
        "pgid": pid,
        "creation_fingerprint": _process_creation_fingerprint(pid),
        "log_path": str(tmp_path / "orphan.log"),
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
        "event_message_id": None,
    }
    try:
        upsert_transition(row)
        assert managed_process_recovery.reconcile_managed_processes() == ("thread-a",)
        assert _pid_gone(pid)
        assert managed_process_recovery.reconcile_managed_processes() == ()
        persisted = get_handle("orphan")
        assert persisted is not None
        assert persisted["outcome"] == "interrupted_by_restart"
        assert len(_events("thread-a")) == 1
    finally:
        if not _pid_gone(pid, timeout=0):
            os.kill(pid, 9)

    # An EPERM probe is only unverified while a live member keeps the group.
    live = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        unverified = {**row, "handle": "unverified", "pid": live.pid, "pgid": live.pid}
        unverified["creation_fingerprint"] = _process_creation_fingerprint(live.pid)
        upsert_transition(unverified)
        monkeypatch.setattr(
            os,
            "killpg",
            lambda _pgid, _signal: (_ for _ in ()).throw(PermissionError()),
        )
        assert managed_process_recovery.reconcile_managed_processes() == ()
        persisted = get_handle("unverified")
        assert persisted is not None and persisted["state"] == "death_unverified"
        assert all("death_unverified" not in event for event in _events("thread-a"))
    finally:
        live.kill()
        live.wait()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"background": False}, "long_running requires background: true"),
        ({"host": True}, "not available with host: true"),
        ({"timeout": 60}, "omit timeout when using long_running"),
        ({"description": "  "}, "requires a description"),
        (
            {"long_running": False, "stall_after_seconds": 60},
            "stall_after_seconds applies only",
        ),
    ],
)
def test_invalid_long_running_tool_calls_are_refused(overrides, message) -> None:
    from tools.shell_long_running import refusal

    call = {
        "long_running": True,
        "stall_after_seconds": None,
        "background": True,
        "host": False,
        "timeout": None,
        "description": "sweep",
        **overrides,
    }
    assert message in (refusal(**call) or "")


def test_long_running_started_message_qualifies_terminal_event_promise() -> None:
    from tools.shell_long_running import DESCRIPTION_LINE, started_message

    message = started_message("handle", long_running=True)
    assert "event for a verifiable terminal outcome" in message
    assert "event when it ends" not in message
    assert "terminal outcome can be verified" in DESCRIPTION_LINE


def test_permit_binding_distinguishes_long_running_mode(tmp_path) -> None:
    import tools.host_access_gate as gate

    tool = SimpleNamespace(working_dir=str(tmp_path), default_timeout=120)
    params = {"command": "printf ok", "background": True}
    ordinary = gate._bash_action(params, tool=tool)
    long_running = gate._bash_action({**params, "long_running": True}, tool=tool)

    assert ordinary.action_hash != long_running.action_hash
    assert ordinary.review_descriptor() == long_running.review_descriptor()


def test_terminal_event_is_idempotent_and_foreground_prune_unlinks_log(
    isolated_db,
    tmp_path,
) -> None:
    from automation.managed_process_events import apply_transition
    from host_access import processes

    mk_session("thread-a")
    row = {
        "handle": "event-handle",
        "thread_id": "thread-a",
        "turn_id": "turn-a",
        "lease_id": None,
        "description": "done",
        "long_running": 1,
        "pid": 10,
        "pgid": 10,
        "creation_fingerprint": "fingerprint",
        "log_path": str(tmp_path / "event-handle.log"),
        "state": "exited",
        "outcome": "completed",
        "reason": "exit",
        "exit_code": 0,
        "started_at": "2026-09-29T00:00:00Z",
        "last_output_at": None,
        "stalled_since": None,
        "stall_after_seconds": 900,
        "max_lifetime_seconds": 86400,
        "ended_at": "2026-09-29T00:01:00Z",
        "log_truncated": 0,
        "event_message_id": None,
    }
    apply_transition(dict(row), "terminal")
    apply_transition(dict(row), "terminal")
    assert len(_events("thread-a")) == 1

    owner = processes.ProcessOwner(runtime_dir=tmp_path / "runtime", max_records=1)
    log_path = owner._log_path_for("foreground")
    log_path.write_bytes(b"")
    record = processes.ProcessRecord(
        handle="foreground",
        thread_id="thread-a",
        turn_id="turn-a",
        lease_id=None,
        lease_revision=None,
        permit_id=None,
        pid=10,
        pgid=10,
        creation_fingerprint="fingerprint",
        mode="foreground",
        log_path=log_path,
        terminal_state="exited",
    )
    owner._runtimes[record.handle] = processes._RuntimeProcess(
        record=record,
        process=object(),
    )
    owner._prune_terminal_records()
    assert not log_path.exists()
