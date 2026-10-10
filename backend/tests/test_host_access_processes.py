from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import psutil
import pytest

pytestmark = pytest.mark.integration


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


def _escaped_child_script(*, parent_sleep_seconds: float) -> str:
    return (
        "import os,subprocess,sys,time;"
        "child=subprocess.Popen("
        "[sys.executable,'-c','import time;time.sleep(30)'],"
        "start_new_session=True,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "marker=os.environ.get('CORSIA_PROCESS_OWNER_MARKER','missing');"
        "print(f'{child.pid}:{marker}',flush=True);"
        f"time.sleep({parent_sleep_seconds!r})"
    )


def _escaped_child_pid(output: bytes | str) -> int:
    text = (
        output.decode("utf-8", errors="replace")
        if isinstance(output, bytes)
        else output
    )
    return int(text.strip().split(":", 1)[0])


def _kill_process_if_present(pid: int | None) -> None:
    if pid is None or _pid_absent(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


async def _record_test_transition(_record, _kind) -> None:
    """Acknowledge the durable callback boundary in ProcessOwner unit tests."""


def _enable_background(owner):
    owner.configure_transition_callback(_record_test_transition)
    return owner


async def _spawn_cleanup_test_process(owner, monkeypatch, cleanup_path: Path):
    started = await owner.spawn(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        thread_id="thread-a",
        turn_id="turn-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=30.0,
        background=True,
        cleanup_paths=(cleanup_path,),
    )
    runtime = owner._runtimes[started.handle]

    async def wait_for_leader_reaped(_record, _timeout):
        # This test targets retained cleanup state, not post-SIGTERM PID races.
        await runtime.process.wait()
        return True

    monkeypatch.setattr(
        owner,
        "_wait_for_group_absent",
        wait_for_leader_reaped,
    )
    return started


@pytest.fixture
async def process_owner(tmp_path):
    from host_access.processes import ProcessOwner

    owner = ProcessOwner(
        runtime_dir=tmp_path,
        term_grace_seconds=0.1,
        kill_grace_seconds=1.0,
        max_records=8,
        max_log_bytes=512,
    )
    _enable_background(owner)
    yield owner
    await owner.shutdown()


async def test_foreground_spawn_owns_a_new_process_group_and_mode_0600_log(
    process_owner,
) -> None:
    result = await process_owner.spawn(
        [sys.executable, "-c", "print('owned')"],
        thread_id="thread-a",
        turn_id="turn-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=2.0,
        background=False,
    )

    assert result.stdout == b"owned\n"
    assert result.stderr == b""
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.record.pid == result.record.pgid
    assert result.record.thread_id == "thread-a"
    assert result.record.turn_id == "turn-a"
    assert result.record.mode == "foreground"
    assert result.record.terminal_state == "exited"
    assert stat.S_IMODE(result.record.log_path.stat().st_mode) == 0o600
    assert process_owner.live_count() == 0


async def test_foreground_spawn_uses_identity_bound_cwd_fd_after_path_replacement(
    process_owner,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        type(process_owner),
        "_runtime_marker_processes",
        lambda _self, _runtime: (),
    )
    monkeypatch.setattr(
        type(process_owner),
        "_owned_group_exists",
        lambda _self, _record: False,
    )
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    (reviewed / "marker").write_text("reviewed", encoding="utf-8")
    cwd_fd = os.open(
        reviewed,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    displaced = tmp_path / "displaced"
    reviewed.rename(displaced)
    reviewed.mkdir()
    (reviewed / "marker").write_text("replacement", encoding="utf-8")

    try:
        result = await process_owner.spawn(
            ["/bin/cat", "marker"],
            thread_id="thread-a",
            turn_id="turn-a",
            cwd=reviewed,
            cwd_fd=cwd_fd,
            env=os.environ,
            timeout_seconds=5,
            background=False,
        )
        assert result.stdout == b"reviewed"
        assert os.fstat(cwd_fd).st_ino == os.stat(displaced).st_ino
    finally:
        os.close(cwd_fd)
    assert process_owner.live_count() == 0


async def test_timeout_terminates_the_entire_group_including_grandchild(
    process_owner,
    monkeypatch,
) -> None:
    grandchild_pids: tuple[int, ...] = ()
    original_terminate_locked = process_owner._terminate_locked

    async def capture_group_members(runtime, *, reason):
        nonlocal grandchild_pids
        leader = psutil.Process(runtime.record.pid)
        grandchild_pids = tuple(
            child.pid
            for child in leader.children(recursive=True)
            if os.getpgid(child.pid) == runtime.record.pgid
        )
        return await original_terminate_locked(runtime, reason=reason)

    monkeypatch.setattr(
        process_owner,
        "_terminate_locked",
        capture_group_members,
    )

    result = await process_owner.spawn(
        ["/bin/sh", "-c", "sleep 30 & wait"],
        thread_id="thread-a",
        turn_id="turn-a",
        cwd=Path.cwd(),
        env=os.environ,
        # This is a deadlock guard, not a startup assertion. It leaves a wide
        # margin for the shell to create its child on a loaded runner.
        timeout_seconds=5.0,
        background=False,
    )

    assert result.timed_out is True
    assert result.record.terminal_state in {"terminated", "killed"}
    assert grandchild_pids
    await _wait_until(lambda: all(_pid_absent(pid) for pid in grandchild_pids))
    assert process_owner.live_count() == 0


async def test_caller_cancellation_is_deferred_until_group_death(
    process_owner,
) -> None:
    task = asyncio.create_task(
        process_owner.spawn(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            thread_id="thread-a",
            turn_id="turn-a",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=30.0,
            background=False,
        )
    )
    await _wait_until(lambda: process_owner.live_count() == 1)
    record = process_owner.running_records()[0]

    task.cancel("interrupt")
    with pytest.raises(asyncio.CancelledError, match="interrupt"):
        await task

    assert _pid_absent(record.pid)
    assert process_owner.live_count() == 0
    assert process_owner.record(record.handle).terminal_state in {
        "terminated",
        "killed",
    }


async def test_term_resistant_group_escalates_to_kill(process_owner) -> None:
    script = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "print('ready',flush=True);"
        "time.sleep(30)"
    )
    started = await process_owner.spawn(
        [sys.executable, "-c", script],
        thread_id="thread-a",
        turn_id="turn-a",
        lease_id="lease-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=30.0,
        background=True,
    )
    await _wait_until(
        lambda: "ready" in process_owner.read_logs(
            started.handle,
            thread_id="thread-a",
        )
    )

    status = await process_owner.stop(
        started.handle,
        thread_id="thread-a",
        reason="managed_stop",
    )

    assert status.terminal_state == "killed"
    assert _pid_absent(status.pid)
    assert process_owner.live_count() == 0


async def test_background_cleanup_path_is_owned_until_explicit_stop(
    process_owner,
    tmp_path,
) -> None:
    cleanup_path = tmp_path / "background-envelope.sb"
    cleanup_path.write_text("profile", encoding="utf-8")
    started = await process_owner.spawn(
        [sys.executable, "-c", "import time;print('ready',flush=True);time.sleep(30)"],
        thread_id="thread-a",
        turn_id="turn-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=30.0,
        background=True,
        cleanup_paths=(cleanup_path,),
    )
    await _wait_until(
        lambda: "ready" in process_owner.read_logs(
            started.handle,
            thread_id="thread-a",
        )
    )

    assert cleanup_path.exists()

    await process_owner.stop(
        started.handle,
        thread_id="thread-a",
        reason="managed_stop",
    )

    assert not cleanup_path.exists()


async def test_background_cleanup_path_is_removed_on_natural_exit(
    process_owner,
    tmp_path,
) -> None:
    cleanup_path = tmp_path / "short-envelope.sb"
    cleanup_path.write_text("profile", encoding="utf-8")
    started = await process_owner.spawn(
        [sys.executable, "-c", "print('done')"],
        thread_id="thread-a",
        turn_id="turn-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=5.0,
        background=True,
        cleanup_paths=(cleanup_path,),
    )

    await _wait_until(
        lambda: process_owner.record(started.handle).terminal_state == "exited"
    )

    assert not cleanup_path.exists()


async def test_cleanup_failure_is_retained_and_retried(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access.processes import ProcessCleanupError, ProcessOwner

    owner = ProcessOwner(
        runtime_dir=tmp_path / "runtime",
        term_grace_seconds=0.1,
        kill_grace_seconds=1.0,
        max_records=8,
        max_log_bytes=512,
    )
    _enable_background(owner)
    cleanup_path = tmp_path / "flaky-envelope.sb"
    cleanup_path.write_text("profile", encoding="utf-8")
    real_unlink = Path.unlink
    cleanup_attempts = 0

    def flaky_unlink(path, *args, **kwargs):
        nonlocal cleanup_attempts
        if path == cleanup_path:
            cleanup_attempts += 1
            if cleanup_attempts == 1:
                raise OSError("transient cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    started = await owner.spawn(
        [sys.executable, "-c", "print('done')"],
        thread_id="thread-a",
        turn_id="turn-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=5.0,
        background=True,
        cleanup_paths=(cleanup_path, cleanup_path),
    )

    await _wait_until(
        lambda: owner.record(started.handle).terminal_state == "exited"
    )
    assert owner.live_count() == 0
    assert cleanup_path.exists()

    status = await owner.stop(
        started.handle,
        thread_id="thread-a",
        reason="managed_stop",
    )

    assert status.terminal_state == "exited"
    assert cleanup_attempts == 2
    assert not cleanup_path.exists()
    with pytest.raises(
        ProcessCleanupError,
        match="retained an unverified lifecycle failure",
    ):
        await owner.shutdown()


async def test_explicit_stop_cleanup_failure_preserves_terminal_process_state(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access.processes import (
        ProcessCapacityError,
        ProcessCleanupError,
        ProcessOwner,
    )

    owner = ProcessOwner(
        runtime_dir=tmp_path / "runtime",
        term_grace_seconds=0.1,
        kill_grace_seconds=1.0,
        max_records=1,
        max_log_bytes=512,
    )
    _enable_background(owner)
    cleanup_path = tmp_path / "flaky-stop-envelope.sb"
    cleanup_path.write_text("profile", encoding="utf-8")
    real_unlink = Path.unlink
    cleanup_blocked = True

    def flaky_unlink(path, *args, **kwargs):
        if path == cleanup_path and cleanup_blocked:
            raise OSError("transient cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    started = await _spawn_cleanup_test_process(owner, monkeypatch, cleanup_path)

    with pytest.raises(ProcessCleanupError, match="retained 1 cleanup path"):
        await owner.stop(
            started.handle,
            thread_id="thread-a",
            reason="managed_stop",
        )

    status = owner.record(started.handle)
    assert status.terminal_state == "terminated"
    assert _pid_absent(status.pid)
    assert owner.live_count() == 0
    assert cleanup_path.exists()
    with pytest.raises(ProcessCapacityError, match="capacity is exhausted"):
        await owner.spawn(
            [sys.executable, "-c", "print('must not start')"],
            thread_id="thread-a",
            turn_id="turn-b",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=5.0,
            background=True,
        )

    cleanup_blocked = False
    retried = await owner.stop(
        started.handle,
        thread_id="thread-a",
        reason="managed_stop",
    )

    assert retried.terminal_state == "terminated"
    assert not cleanup_path.exists()

    replacement = await owner.spawn(
        [sys.executable, "-c", "print('replacement')"],
        thread_id="thread-a",
        turn_id="turn-b",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=5.0,
        background=False,
    )
    assert replacement.stdout == b"replacement\n"

    with pytest.raises(
        ProcessCleanupError,
        match="retained an unverified lifecycle failure",
    ):
        await owner.shutdown()


async def test_normal_foreground_exit_stops_undeclared_background_descendant(
    process_owner,
) -> None:
    script = (
        "import os,subprocess,sys;"
        "child=subprocess.Popen("
        "[sys.executable,'-c','import time;time.sleep(30)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "print(child.pid,flush=True)"
    )

    result = await process_owner.spawn(
        [sys.executable, "-c", script],
        thread_id="thread-a",
        turn_id="turn-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=3.0,
        background=False,
    )

    descendant_pid = int(result.stdout.decode().strip())
    assert result.returncode == 0
    assert result.undeclared_background_stopped is True
    assert result.record.terminal_state == "undeclared_background_stopped"
    await _wait_until(lambda: _pid_absent(descendant_pid))


async def test_normal_foreground_exit_stops_new_session_descendant_and_hides_marker(
    process_owner,
) -> None:
    escaped_pid = None
    try:
        result = await process_owner.spawn(
            [
                sys.executable,
                "-c",
                _escaped_child_script(parent_sleep_seconds=0.0),
            ],
            thread_id="thread-a",
            turn_id="turn-a",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=3.0,
            background=False,
        )
        escaped_pid = _escaped_child_pid(result.stdout)

        assert result.returncode == 0
        assert result.undeclared_background_stopped is True
        assert result.record.terminal_state == "undeclared_background_stopped"
        assert b"[REDACTED]" in result.stdout
        assert not hasattr(result.record, "owner_marker")
        assert "CORSIA_PROCESS_OWNER_MARKER" not in repr(result.record)
        await _wait_until(lambda: _pid_absent(escaped_pid))
        assert process_owner.live_count() == 0
    finally:
        _kill_process_if_present(escaped_pid)


async def test_timeout_stops_new_session_descendant(process_owner) -> None:
    escaped_pid = None
    try:
        result = await process_owner.spawn(
            [
                sys.executable,
                "-c",
                _escaped_child_script(parent_sleep_seconds=30.0),
            ],
            thread_id="thread-a",
            turn_id="turn-a",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=0.3,
            background=False,
        )
        escaped_pid = _escaped_child_pid(result.stdout)

        assert result.timed_out is True
        assert result.record.terminal_state in {"terminated", "killed"}
        assert b"[REDACTED]" in result.stdout
        await _wait_until(lambda: _pid_absent(escaped_pid))
        assert process_owner.live_count() == 0
    finally:
        _kill_process_if_present(escaped_pid)


async def test_lease_termination_stops_new_session_descendant(
    process_owner,
) -> None:
    escaped_pid = None
    try:
        started = await process_owner.spawn(
            [
                sys.executable,
                "-c",
                _escaped_child_script(parent_sleep_seconds=0.0),
            ],
            thread_id="thread-a",
            turn_id="turn-a",
            lease_id="lease-a",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=30.0,
            background=True,
        )
        runtime = process_owner._runtimes[started.handle]
        await runtime.process.wait()
        await _wait_until(
            lambda: ":" in process_owner.read_logs(
                started.handle,
                thread_id="thread-a",
            )
        )
        logs = process_owner.read_logs(
            started.handle,
            thread_id="thread-a",
        )
        escaped_pid = _escaped_child_pid(logs)
        await _wait_until(lambda: bool(runtime.marker_bindings))
        assert process_owner.record(started.handle).terminal_state == "running"

        records = await process_owner.terminate_lease("lease-a")

        assert len(records) == 1
        assert "[REDACTED]" in logs
        await _wait_until(lambda: _pid_absent(escaped_pid))
        assert process_owner.live_count() == 0
    finally:
        _kill_process_if_present(escaped_pid)


async def test_shutdown_stops_new_session_descendant(
    process_owner,
    tmp_path,
) -> None:
    cleanup_path = tmp_path / "shutdown-envelope.sb"
    cleanup_path.write_text("profile", encoding="utf-8")
    escaped_pid = None
    try:
        started = await process_owner.spawn(
            [
                sys.executable,
                "-c",
                _escaped_child_script(parent_sleep_seconds=0.0),
            ],
            thread_id="thread-a",
            turn_id="turn-a",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=30.0,
            background=True,
            cleanup_paths=(cleanup_path,),
        )
        runtime = process_owner._runtimes[started.handle]
        await runtime.process.wait()
        await _wait_until(
            lambda: ":" in process_owner.read_logs(
                started.handle,
                thread_id="thread-a",
            )
        )
        escaped_pid = _escaped_child_pid(
            process_owner.read_logs(
                started.handle,
                thread_id="thread-a",
            )
        )
        await _wait_until(lambda: bool(runtime.marker_bindings))
        assert process_owner.record(started.handle).terminal_state == "running"

        await process_owner.shutdown()

        await _wait_until(lambda: _pid_absent(escaped_pid))
        assert process_owner.live_count() == 0
        assert not cleanup_path.exists()
    finally:
        _kill_process_if_present(escaped_pid)


async def test_background_handle_logs_status_stop_and_ownership_are_bounded(
    process_owner,
) -> None:
    script = (
        "import sys,time;"
        "print('x'*2048,flush=True);"
        "time.sleep(30)"
    )
    started = await process_owner.spawn(
        [sys.executable, "-c", script],
        thread_id="thread-a",
        turn_id="turn-a",
        lease_id="lease-expired-is-not-consulted",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=30.0,
        background=True,
    )
    await _wait_until(
        lambda: bool(
            process_owner.read_logs(started.handle, thread_id="thread-a")
        )
    )

    with pytest.raises(PermissionError, match="owned"):
        process_owner.status(started.handle, thread_id="thread-b")
    with pytest.raises(PermissionError, match="owned"):
        process_owner.read_logs(started.handle, thread_id="thread-b")
    with pytest.raises(PermissionError, match="owned"):
        await process_owner.stop(started.handle, thread_id="thread-b")

    logs = process_owner.read_logs(
        started.handle,
        thread_id="thread-a",
        limit_bytes=10_000,
    )
    assert len(logs.encode()) <= process_owner.max_log_bytes
    assert process_owner.status(
        started.handle,
        thread_id="thread-a",
    ).terminal_state == "running"

    first, second = await asyncio.gather(
        process_owner.stop(
            started.handle,
            thread_id="thread-a",
            reason="managed_stop",
        ),
        process_owner.stop(
            started.handle,
            thread_id="thread-a",
            reason="managed_stop",
        ),
    )
    assert first.terminal_state in {"terminated", "killed"}
    assert second.terminal_state == first.terminal_state
    assert process_owner.live_count() == 0


async def test_background_timeout_terminates_the_owned_group(
    process_owner,
    monkeypatch,
    tmp_path,
) -> None:
    cleanup_path = tmp_path / "timeout-envelope.sb"
    cleanup_path.write_text("profile", encoding="utf-8")
    spool_started = asyncio.Event()
    spool_release = asyncio.Event()

    async def held_spool(_runtime) -> None:
        spool_started.set()
        await spool_release.wait()

    monkeypatch.setattr(process_owner, "_spool_background_output", held_spool)
    started = await process_owner.spawn(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        thread_id="thread-a",
        turn_id="turn-a",
        lease_id="lease-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=0.2,
        background=True,
        cleanup_paths=(cleanup_path,),
    )
    record = process_owner.record(started.handle)
    runtime = process_owner._runtimes[started.handle]
    await spool_started.wait()

    try:
        await _wait_until(
            lambda: (
                process_owner.record(started.handle).terminal_state != "running"
            )
        )
        # The timeout path terminates the runtime first and only then cancels
        # and joins the spool task, so the terminal state is observable while
        # that task is still `cancelling`. Wait for the thing being asserted
        # rather than for an earlier step that merely precedes it.
        await _wait_until(
            lambda: runtime.spool_task is not None and runtime.spool_task.done()
        )

        terminal = process_owner.record(started.handle)
        assert terminal.terminal_state in {"terminated", "killed"}
        assert _pid_absent(record.pid)
        assert runtime.spool_task is not None
        assert runtime.spool_task.done()
        assert process_owner.live_count() == 0
        assert not cleanup_path.exists()
    finally:
        spool_release.set()
        if runtime.spool_task is not None:
            await asyncio.gather(runtime.spool_task, return_exceptions=True)


async def test_background_timeout_cleanup_failure_is_retained_and_spool_joined(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(
        runtime_dir=tmp_path,
        term_grace_seconds=0.05,
        kill_grace_seconds=1.0,
    )
    _enable_background(owner)
    spool_started = asyncio.Event()
    spool_release = asyncio.Event()

    async def held_spool(_runtime) -> None:
        spool_started.set()
        await spool_release.wait()

    original_terminate_locked = owner._terminate_locked

    async def fail_cleanup(runtime, *, reason):
        del reason
        owner._set_state(runtime, "death_unverified")
        raise processes.ProcessCleanupError("synthetic timeout cleanup failure")

    monkeypatch.setattr(owner, "_spool_background_output", held_spool)
    monkeypatch.setattr(owner, "_terminate_locked", fail_cleanup)
    started = await owner.spawn(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        thread_id="thread-a",
        turn_id="turn-a",
        lease_id="lease-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=0.2,
        background=True,
    )
    runtime = owner._runtimes[started.handle]
    await spool_started.wait()
    shutdown_attempted = False

    try:
        with pytest.raises(
            processes.ProcessCleanupError,
            match="synthetic timeout cleanup failure",
        ):
            await runtime.watcher_task
        assert runtime.spool_task is not None
        assert runtime.spool_task.done()

        monkeypatch.setattr(owner, "_terminate_locked", original_terminate_locked)
        shutdown_attempted = True
        with pytest.raises(
            processes.ProcessCleanupError,
            match="retained an unverified lifecycle failure",
        ):
            await owner.shutdown()
        assert owner.record(started.handle).terminal_state == "terminated"
        assert owner.live_count() == 0
    finally:
        spool_release.set()
        if runtime.spool_task is not None:
            await asyncio.gather(runtime.spool_task, return_exceptions=True)
        if not shutdown_attempted:
            monkeypatch.setattr(
                owner,
                "_terminate_locked",
                original_terminate_locked,
            )
            try:
                await owner.shutdown()
            except processes.ProcessCleanupError:
                pass


def test_process_discovery_redacts_arguments_and_signal_revalidates_binding() -> None:
    from host_access import processes

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time;time.sleep(30)",
            "--token",
            "literal-secret",
        ]
    )
    try:
        discovered = processes.discover_processes(pid=child.pid, limit=1)
        assert len(discovered) == 1
        metadata = discovered[0]
        assert metadata.pid == child.pid
        assert "literal-secret" not in " ".join(metadata.arguments)
        assert "[REDACTED]" in metadata.arguments
        assert not hasattr(metadata, "environment")

        with pytest.raises(processes.ProcessBindingError, match="creation"):
            processes.signal_bound_process(
                pid=child.pid,
                create_time=metadata.create_time + 1,
                executable_fingerprint=metadata.executable_fingerprint,
                signal_name="terminate",
            )
        assert child.poll() is None

        processes.signal_bound_process(
            pid=child.pid,
            create_time=metadata.create_time,
            executable_fingerprint=metadata.executable_fingerprint,
            signal_name="terminate",
        )
        child.wait(timeout=3)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


@pytest.mark.parametrize("pid", [0, 1, os.getpid()])
def test_process_signal_structurally_rejects_control_plane_pids(pid) -> None:
    from host_access.processes import ProcessBindingError, signal_bound_process

    with pytest.raises(ProcessBindingError, match="protected"):
        signal_bound_process(
            pid=pid,
            create_time=time.time(),
            executable_fingerprint="not-used",
            signal_name="terminate",
        )


def test_process_discovery_structurally_rejects_pid_one_and_backend_pid() -> None:
    from host_access.processes import ProcessBindingError, discover_processes

    with pytest.raises(ProcessBindingError, match="protected"):
        discover_processes(pid=1)
    with pytest.raises(ProcessBindingError, match="protected"):
        discover_processes(pid=os.getpid())


def test_identity_bound_backend_descendant_can_be_signaled() -> None:
    from host_access.processes import discover_processes, signal_bound_process

    child = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"]
    )
    try:
        metadata = discover_processes(pid=child.pid)[0]
        signal_bound_process(
            pid=child.pid,
            create_time=metadata.create_time,
            executable_fingerprint=metadata.executable_fingerprint,
            signal_name="terminate",
        )
        child.wait(timeout=3)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def test_process_discovery_redacts_common_secret_argument_forms() -> None:
    from host_access.processes import _redact_arguments

    redacted = _redact_arguments(
        (
            "curl",
            "--header",
            "Authorization: Bearer literal-header-secret",
            "--user",
            "steve:literal-user-secret",
            "--cookie=literal-cookie-secret",
            "-HAuthorization: Bearer concatenated-header-secret",
            "Authorization:",
            "Bearer",
            "standalone-authorization-secret",
            "auth",
            "Bearer",
            "standalone-auth-secret",
            "authorization=literal-authorization-secret",
            "--client-secret",
            "literal-client-secret",
            "--access-token",
            "literal-access-token",
            "--refresh-token=literal-refresh-token",
            "postgresql://steve:literal-url-secret@localhost/db?token=query-secret",
            "api_key=literal-key-secret",
        )
    )
    rendered = " ".join(redacted)

    assert "literal-header-secret" not in rendered
    assert "literal-user-secret" not in rendered
    assert "literal-cookie-secret" not in rendered
    assert "concatenated-header-secret" not in rendered
    assert "standalone-authorization-secret" not in rendered
    assert "standalone-auth-secret" not in rendered
    assert "literal-authorization-secret" not in rendered
    assert "literal-client-secret" not in rendered
    assert "literal-access-token" not in rendered
    assert "literal-refresh-token" not in rendered
    assert "literal-url-secret" not in rendered
    assert "query-secret" not in rendered
    assert "literal-key-secret" not in rendered
    assert rendered.count("[REDACTED]") >= 5


@pytest.mark.parametrize(
    ("arguments", "secret"),
    [
        (
            ("curl", "--proxy-user", "alice:literal-proxy-secret"),
            "literal-proxy-secret",
        ),
        (
            (
                "security",
                "add-generic-password",
                "-w",
                "literal-keychain-secret",
            ),
            "literal-keychain-secret",
        ),
        (
            (
                "netrc-tool",
                "machine",
                "github.com",
                "login",
                "alice",
                "password",
                "literal-netrc-secret",
            ),
            "literal-netrc-secret",
        ),
    ],
)
def test_process_argument_redaction_covers_additional_credential_forms(
    arguments: tuple[str, ...],
    secret: str,
) -> None:
    from host_access.processes import _redact_arguments

    rendered = " ".join(_redact_arguments(arguments))

    assert secret not in rendered
    assert "[REDACTED]" in rendered


def test_process_argument_redaction_fails_safe_on_malformed_url() -> None:
    from host_access.processes import _redact_arguments

    assert _redact_arguments(("tool", "https://[invalid")) == (
        "tool",
        "[REDACTED]",
    )


@pytest.mark.parametrize("create_time", [math.nan, math.inf, -math.inf])
def test_process_signal_rejects_non_finite_creation_time(create_time) -> None:
    from host_access.processes import ProcessBindingError, signal_bound_process

    with pytest.raises(ProcessBindingError, match="creation time"):
        signal_bound_process(
            pid=42,
            create_time=create_time,
            executable_fingerprint="a" * 64,
            signal_name="terminate",
        )


def test_process_signal_uses_the_immediately_revalidated_process_object(
    monkeypatch,
) -> None:
    from host_access import processes

    events: list[object] = []

    class _Selected:
        def __init__(self, label: str) -> None:
            self.label = label

        def send_signal(self, selected_signal) -> None:
            events.append(("send", self.label, selected_signal))

    selected = iter((_Selected("validated"),))

    def validate(**_kwargs):
        result = next(selected)
        events.append(("validate", result.label))
        return result

    monkeypatch.setattr(processes, "_validate_bound_process", validate)
    monkeypatch.setattr(
        processes.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("bare os.kill must not be used")
        ),
    )

    processes.signal_bound_process(
        pid=42,
        create_time=123.5,
        executable_fingerprint="a" * 64,
        signal_name="terminate",
    )

    assert events == [
        ("validate", "validated"),
        ("send", "validated", signal.SIGTERM),
    ]


async def test_lease_terminal_fences_admission_before_cleanup_snapshot(
    process_owner,
    monkeypatch,
) -> None:
    from host_access import processes

    entered = asyncio.Event()
    release = asyncio.Event()
    original = process_owner._terminate_snapshot

    async def paused(runtimes, *, reason):
        entered.set()
        await release.wait()
        return await original(runtimes, reason=reason)

    monkeypatch.setattr(process_owner, "_terminate_snapshot", paused)
    cleanup = asyncio.create_task(process_owner.terminate_lease("lease-a"))
    await entered.wait()

    with pytest.raises(processes.ProcessOwnerError, match="lease.*closed"):
        await process_owner.spawn(
            [sys.executable, "-c", "print('must-not-start')"],
            thread_id="thread-a",
            turn_id="turn-a",
            lease_id="lease-a",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=1.0,
            background=False,
        )

    release.set()
    assert await cleanup == ()


@pytest.mark.parametrize("terminal_scope", ["turn", "lease"])
async def test_terminal_cleanup_tombstone_rejects_delayed_owner_spawn(
    process_owner,
    terminal_scope,
) -> None:
    from host_access.processes import ProcessOwnerError

    spawn_waiting = asyncio.Event()
    release_spawn = asyncio.Event()

    class _OrderedOwnerLock:
        async def __aenter__(self):
            task = asyncio.current_task()
            if task is not None and task.get_name() == "delayed-owner-spawn":
                spawn_waiting.set()
                await release_spawn.wait()
            return self

        async def __aexit__(self, *_args):
            return False

    process_owner._owner_lock = _OrderedOwnerLock()
    delayed_spawn = asyncio.create_task(
        process_owner.spawn(
            [sys.executable, "-c", "print('must-not-start')"],
            thread_id="thread-a",
            turn_id="turn-a",
            lease_id="lease-a",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=1.0,
            background=False,
        ),
        name="delayed-owner-spawn",
    )
    await spawn_waiting.wait()

    if terminal_scope == "turn":
        assert await process_owner.terminate_turn(
            "thread-a",
            "turn-a",
        ) == ()
    else:
        assert await process_owner.terminate_lease("lease-a") == ()
    release_spawn.set()

    with pytest.raises(
        ProcessOwnerError,
        match=f"{terminal_scope} process admission is closed",
    ):
        await delayed_spawn


async def test_owned_group_signal_rejects_reused_leader_identity(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(runtime_dir=tmp_path)
    record = processes.ProcessRecord(
        handle="opaque",
        thread_id="thread-a",
        turn_id="turn-a",
        lease_id=None,
        lease_revision=None,
        permit_id=None,
        pid=42,
        pgid=42,
        creation_fingerprint="original",
        mode="foreground",
        log_path=tmp_path / "opaque.log",
        terminal_state="running",
    )

    class _Replacement:
        @staticmethod
        def status():
            return psutil.STATUS_RUNNING

    monkeypatch.setattr(
        processes.psutil,
        "Process",
        lambda _pid: _Replacement(),
    )
    monkeypatch.setattr(
        processes,
        "_process_creation_fingerprint",
        lambda _pid: "replacement",
    )
    monkeypatch.setattr(
        processes.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("reused PGID must not be signaled")
        ),
    )

    with pytest.raises(processes.ProcessCleanupError, match="identity changed"):
        await owner._signal_owned_group(
            processes._RuntimeProcess(record=record, process=None), signal.SIGTERM,
        )


def test_owned_group_probe_accepts_leader_that_disappears_during_identity_read(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(runtime_dir=tmp_path)
    record = processes.ProcessRecord(
        handle="opaque",
        thread_id="thread-a",
        turn_id="turn-a",
        lease_id=None,
        lease_revision=None,
        permit_id=None,
        pid=42,
        pgid=42,
        creation_fingerprint="original",
        mode="foreground",
        log_path=tmp_path / "opaque.log",
        terminal_state="running",
    )

    class _LeaderBeforeExit:
        @staticmethod
        def status():
            return psutil.STATUS_RUNNING

    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(processes.psutil, "Process", lambda _pid: _LeaderBeforeExit())
    monkeypatch.setattr(processes.psutil, "pid_exists", lambda _pid: False)
    monkeypatch.setattr(
        processes,
        "_process_creation_fingerprint",
        lambda _pid: (_ for _ in ()).throw(
            processes.ProcessBindingError("process creation identity is unavailable")
        ),
    )

    def absent_group(pgid: int, selected_signal: int) -> None:
        killpg_calls.append((pgid, selected_signal))
        raise ProcessLookupError

    monkeypatch.setattr(processes.os, "killpg", absent_group)

    assert owner._owned_group_exists(record) is False
    assert killpg_calls == [(42, 0)]


def test_owned_group_probe_refuses_live_leader_with_unavailable_identity(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(runtime_dir=tmp_path)
    record = processes.ProcessRecord(
        handle="opaque",
        thread_id="thread-a",
        turn_id="turn-a",
        lease_id=None,
        lease_revision=None,
        permit_id=None,
        pid=42,
        pgid=42,
        creation_fingerprint="original",
        mode="foreground",
        log_path=tmp_path / "opaque.log",
        terminal_state="running",
    )

    class _LiveLeader:
        @staticmethod
        def status():
            return psutil.STATUS_RUNNING

    monkeypatch.setattr(processes.psutil, "Process", lambda _pid: _LiveLeader())
    monkeypatch.setattr(processes.psutil, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        processes,
        "_process_creation_fingerprint",
        lambda _pid: (_ for _ in ()).throw(
            processes.ProcessBindingError("process creation identity is unavailable")
        ),
    )
    monkeypatch.setattr(
        processes.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("live identity uncertainty must fail before signaling")
        ),
    )

    with pytest.raises(processes.ProcessCleanupError, match="identity is unavailable"):
        owner._owned_group_exists(record)


def test_owned_marker_signal_refuses_reused_pid_identity(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(runtime_dir=tmp_path)
    signal_owned = getattr(owner, "_signal_owned_marker_process", None)
    assert signal_owned is not None, "marker-bound cleanup is unavailable"
    binding = SimpleNamespace(pid=42, creation_fingerprint="original")
    monkeypatch.setattr(
        processes,
        "_process_creation_fingerprint",
        lambda _pid: "replacement",
    )
    monkeypatch.setattr(
        processes.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("reused PID must not be signaled")
        ),
    )

    assert signal_owned(binding, signal.SIGTERM) is False


def test_owned_marker_scan_skips_unrelated_opaque_same_user_process(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(runtime_dir=tmp_path)
    scan_owned = getattr(owner, "_owned_marker_processes", None)
    assert scan_owned is not None, "marker-bound scan is unavailable"

    class _OpaqueSameUserProcess:
        pid = 42

        @staticmethod
        def uids():
            return SimpleNamespace(real=os.getuid())

        @staticmethod
        def environ():
            raise psutil.AccessDenied(pid=42)

    monkeypatch.setattr(
        processes.psutil,
        "process_iter",
        lambda *_args, **_kwargs: iter((_OpaqueSameUserProcess(),)),
    )
    monkeypatch.setattr(processes.psutil, "pid_exists", lambda _pid: True)

    assert scan_owned("private-marker") == ()


def test_owned_marker_scan_skips_unrelated_system_error_and_binds_match(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(runtime_dir=tmp_path)

    class _SystemErrorProcess:
        pid = 42

        @staticmethod
        def uids():
            return SimpleNamespace(real=os.getuid())

        @staticmethod
        def environ():
            raise SystemError("proc_environ failed")

    class _MarkerProcess:
        pid = 43

        @staticmethod
        def uids():
            return SimpleNamespace(real=os.getuid())

        @staticmethod
        def environ():
            return {"CORSIA_PROCESS_OWNER_MARKER": "private-marker"}

        @staticmethod
        def status():
            return "running"

    monkeypatch.setattr(
        processes.psutil,
        "process_iter",
        lambda *_args, **_kwargs: iter((_SystemErrorProcess(), _MarkerProcess())),
    )
    monkeypatch.setattr(
        processes,
        "_process_creation_fingerprint",
        lambda pid: f"fingerprint-{pid}",
    )

    assert owner._owned_marker_processes("private-marker") == (
        processes._OwnedMarkerProcess(pid=43, creation_fingerprint="fingerprint-43"),
    )


def test_owned_marker_scan_fails_loud_after_marker_matched_system_error(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(runtime_dir=tmp_path)

    class _MatchedSystemErrorProcess:
        pid = 42

        @staticmethod
        def uids():
            return SimpleNamespace(real=os.getuid())

        @staticmethod
        def environ():
            return {"CORSIA_PROCESS_OWNER_MARKER": "private-marker"}

        @staticmethod
        def status():
            raise SystemError("proc_status failed")

    monkeypatch.setattr(
        processes.psutil,
        "process_iter",
        lambda *_args, **_kwargs: iter((_MatchedSystemErrorProcess(),)),
    )

    with pytest.raises(processes.ProcessCleanupError, match="marker scan"):
        owner._owned_marker_processes("private-marker")


def test_owned_marker_scan_fails_loud_after_positive_marker_match(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(runtime_dir=tmp_path)

    class _MatchedOpaqueProcess:
        pid = 42

        @staticmethod
        def uids():
            return SimpleNamespace(real=os.getuid())

        @staticmethod
        def environ():
            return {"CORSIA_PROCESS_OWNER_MARKER": "private-marker"}

        @staticmethod
        def status():
            raise psutil.AccessDenied(pid=42)

    monkeypatch.setattr(
        processes.psutil,
        "process_iter",
        lambda *_args, **_kwargs: iter((_MatchedOpaqueProcess(),)),
    )

    with pytest.raises(processes.ProcessCleanupError, match="marker scan"):
        owner._owned_marker_processes("private-marker")


def test_owned_marker_scan_fails_loud_when_known_binding_becomes_opaque(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(runtime_dir=tmp_path)
    scan_owned = getattr(owner, "_owned_marker_processes", None)
    assert scan_owned is not None, "marker-bound scan is unavailable"
    binding = SimpleNamespace(pid=42, creation_fingerprint="original")
    monkeypatch.setattr(processes.psutil, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        processes,
        "_process_creation_fingerprint",
        lambda _pid: (_ for _ in ()).throw(
            processes.ProcessBindingError("unavailable")
        ),
    )

    with pytest.raises(processes.ProcessCleanupError, match="marker scan"):
        scan_owned("private-marker", known_bindings=(binding,))


async def test_background_watcher_failure_is_retained_and_shutdown_is_loud(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(
        runtime_dir=tmp_path,
        term_grace_seconds=0.1,
        kill_grace_seconds=1.0,
    )
    _enable_background(owner)

    async def broken_spool(_runtime) -> None:
        raise RuntimeError("spool failed")

    monkeypatch.setattr(owner, "_spool_background_output", broken_spool)
    started = await owner.spawn(
        [sys.executable, "-c", "print('done')"],
        thread_id="thread-a",
        turn_id="turn-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=1.0,
        background=True,
    )
    await _wait_until(
        lambda: (
            owner.record(started.handle).terminal_state
            == "death_unverified"
        )
    )

    with pytest.raises(processes.ProcessCleanupError, match="retained"):
        await owner.shutdown()


async def test_death_unverified_remains_outstanding_and_shutdown_retries(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(runtime_dir=tmp_path)
    _enable_background(owner)
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
        terminal_state="death_unverified",
    )
    runtime = processes._RuntimeProcess(
        record=record,
        process=object(),
    )
    owner._runtimes[record.handle] = runtime
    attempts: list[str] = []

    async def retry(candidate, *, reason):
        attempts.append(reason)
        owner._set_state(candidate, "terminated")
        return candidate.record

    monkeypatch.setattr(owner, "_terminate_runtime", retry)

    assert owner.live_count() == 1
    assert owner.active_process_count() == 1
    assert owner.active_browser_command_count() == 0
    await owner.shutdown()

    assert attempts == ["backend_shutdown"]
    assert owner.record(record.handle).terminal_state == "terminated"
    assert owner.live_count() == 0


async def test_cleanup_reaps_leader_before_final_group_absence_check(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(
        runtime_dir=tmp_path,
        term_grace_seconds=0.01,
        kill_grace_seconds=0.1,
    )
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
        mode="foreground",
        log_path=tmp_path / "opaque.log",
        terminal_state="running",
    )

    class _ControlledProcess:
        returncode = None

        async def wait(self):
            self.returncode = -signal.SIGKILL
            return self.returncode

    process = _ControlledProcess()
    runtime = processes._RuntimeProcess(record=record, process=process)
    owner._runtimes[record.handle] = runtime
    signals: list[signal.Signals] = []

    async def issue_signal(_runtime, selected) -> bool:
        signals.append(selected)
        return True

    monkeypatch.setattr(
        owner,
        "_owned_group_exists",
        lambda _record: process.returncode is None,
    )
    monkeypatch.setattr(owner, "_signal_owned_group", issue_signal)

    async def no_marker_processes(_runtime):
        return False, False

    monkeypatch.setattr(
        owner,
        "_terminate_owned_marker_processes",
        no_marker_processes,
    )

    result = await owner._terminate_locked(runtime, reason="managed_stop")

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert result.terminal_state == "killed"


async def test_cleanup_retains_unreaped_leader_as_death_unverified(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(
        runtime_dir=tmp_path,
        term_grace_seconds=0.01,
        kill_grace_seconds=0.01,
    )
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
        mode="foreground",
        log_path=tmp_path / "opaque.log",
        terminal_state="running",
    )
    release_wait = asyncio.Event()

    class _NeverReapedProcess:
        returncode = None

        async def wait(self):
            await release_wait.wait()
            self.returncode = -signal.SIGKILL
            return self.returncode

    process = _NeverReapedProcess()
    runtime = processes._RuntimeProcess(record=record, process=process)
    owner._runtimes[record.handle] = runtime
    monkeypatch.setattr(owner, "_owned_group_exists", lambda _record: True)
    async def signal_issued(*_args) -> bool:
        return True

    monkeypatch.setattr(owner, "_signal_owned_group", signal_issued)

    try:
        with pytest.raises(processes.ProcessCleanupError, match="was not reaped"):
            await owner._terminate_locked(runtime, reason="managed_stop")
    finally:
        release_wait.set()
        await asyncio.sleep(0)

    assert owner.record(record.handle).terminal_state == "death_unverified"


async def test_direct_cleanup_death_unverified_is_retained_and_shutdown_is_loud(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = processes.ProcessOwner(
        runtime_dir=tmp_path,
        term_grace_seconds=0.05,
        kill_grace_seconds=1.0,
    )
    _enable_background(owner)
    script = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "print('ready',flush=True);"
        "time.sleep(30)"
    )
    started = await owner.spawn(
        [sys.executable, "-c", script],
        thread_id="thread-a",
        turn_id="turn-a",
        lease_id="lease-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=30.0,
        background=True,
    )
    await _wait_until(
        lambda: "ready" in owner.read_logs(started.handle, thread_id="thread-a")
    )
    original_wait = owner._wait_for_group_absent

    async def unverified(_record, _timeout):
        return False

    monkeypatch.setattr(owner, "_wait_for_group_absent", unverified)
    with pytest.raises(processes.ProcessCleanupError, match="owned process group"):
        await owner.stop(started.handle, thread_id="thread-a")
    assert owner.record(started.handle).terminal_state == "death_unverified"

    monkeypatch.setattr(owner, "_wait_for_group_absent", original_wait)
    with pytest.raises(
        processes.ProcessCleanupError,
        match="retained an unverified lifecycle failure",
    ):
        await owner.shutdown()

    assert owner.record(started.handle).terminal_state == "terminated"
    assert owner.live_count() == 0


async def test_process_owner_delegates_browser_commands_without_owning_daemon_groups(
    tmp_path,
) -> None:
    from host_access.processes import ProcessOwner

    calls: list[tuple[str, object]] = []

    class _BrowserOwner:
        live = 1

        def live_count(self) -> int:
            return self.live

        def active_command_count(self) -> int:
            return 0

        async def run(self, thread_id, args, *, lease_id=None, timeout=30,
                      show_window=None):
            calls.append(
                (
                    "run",
                    (thread_id, tuple(args), lease_id, timeout, show_window),
                )
            )
            return "browser-result"

        async def close_thread(self, thread_id):
            calls.append(("close", thread_id))
            self.live = 0
            return "Browser session closed."

        async def terminate_lease(self, lease_id):
            calls.append(("lease", lease_id))

        async def shutdown(self):
            calls.append(("shutdown", None))
            self.live = 0

    browser_owner = _BrowserOwner()
    owner = ProcessOwner(
        runtime_dir=tmp_path,
        browser_owner=browser_owner,
    )

    assert owner.live_count() == 1
    assert owner.active_process_count() == 0
    assert owner.active_browser_command_count() == 0
    assert (
        await owner.run_browser(
            "thread-a",
            ["open", "about:blank"],
            lease_id="lease-a",
            timeout=17,
            show_window=True,
        )
        == "browser-result"
    )
    assert (
        await owner.close_browser("thread-a")
        == "Browser session closed."
    )
    await owner.terminate_lease("lease-a")
    await owner.shutdown()

    assert calls == [
        (
            "run",
            (
                "thread-a",
                ("open", "about:blank"),
                "lease-a",
                17,
                True,
            ),
        ),
        ("close", "thread-a"),
        ("lease", "lease-a"),
        ("shutdown", None),
    ]


async def test_process_snapshot_cleanup_runs_owned_groups_concurrently(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access.processes import ProcessOwner

    owner = ProcessOwner(runtime_dir=tmp_path)
    runtimes = tuple(SimpleNamespace(name=f"runtime-{index}") for index in range(3))
    all_started = asyncio.Event()
    release = asyncio.Event()
    started: list[object] = []

    async def terminate(runtime, *, reason):
        assert reason == "test"
        started.append(runtime)
        if len(started) == len(runtimes):
            all_started.set()
        await release.wait()
        return runtime

    monkeypatch.setattr(owner, "_terminate_runtime", terminate)
    cleanup = asyncio.create_task(
        owner._terminate_snapshot(runtimes, reason="test")
    )
    concurrent = False
    try:
        await asyncio.wait_for(all_started.wait(), timeout=1.0)
        concurrent = True
    finally:
        release.set()
        results = await cleanup

    assert concurrent is True
    assert results == runtimes


async def test_backend_shutdown_starts_process_and_browser_cleanup_together(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access.processes import ProcessOwner

    process_started = asyncio.Event()
    browser_started = asyncio.Event()
    release = asyncio.Event()

    class _BrowserOwner:
        def live_count(self) -> int:
            return 0

        def active_command_count(self) -> int:
            return 0

        async def shutdown(self) -> None:
            browser_started.set()
            await release.wait()

    owner = ProcessOwner(
        runtime_dir=tmp_path,
        browser_owner=_BrowserOwner(),
    )

    async def terminate_snapshot(_runtimes, *, reason):
        assert reason == "backend_shutdown"
        process_started.set()
        await release.wait()
        return ()

    monkeypatch.setattr(owner, "_terminate_snapshot", terminate_snapshot)
    cleanup = asyncio.create_task(owner.shutdown())
    concurrent = False
    try:
        await asyncio.wait_for(
            asyncio.gather(process_started.wait(), browser_started.wait()),
            timeout=1.0,
        )
        concurrent = True
    finally:
        release.set()
        await cleanup

    assert concurrent is True


def _spawn_group_with_unreaped_zombie(*, reap_leader: bool):
    """A killed group whose leader and one member this test has not reaped."""
    leader = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        process_group=0,
    )
    member = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        process_group=leader.pid,
    )
    os.killpg(leader.pid, signal.SIGKILL)
    deadline = time.monotonic() + 5.0
    for child in (leader, member):
        while psutil.Process(child.pid).status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline, "child did not become a zombie"
            time.sleep(0.01)
    if reap_leader:
        leader.wait()
    return leader, member


def test_group_pids_treats_a_zombie_only_group_as_absent() -> None:
    from host_access import processes

    leader, member = _spawn_group_with_unreaped_zombie(reap_leader=True)
    try:
        # The unreaped member keeps the group id allocated, and on macOS
        # killpg(0) answers EPERM for it; it is not a live member.
        assert processes._group_pids(leader.pid) == ()
    finally:
        member.wait()


def test_group_pids_keeps_an_unreaped_zombie_leader_present() -> None:
    from host_access import processes

    leader, member = _spawn_group_with_unreaped_zombie(reap_leader=False)
    try:
        assert processes._group_pids(leader.pid) == (leader.pid,)
    finally:
        leader.wait()
        member.wait()


def test_group_pids_keeps_a_live_member_when_killpg_answers_eperm(
    monkeypatch,
) -> None:
    from host_access import processes

    child = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        start_new_session=True,
    )
    try:
        monkeypatch.setattr(
            processes.os,
            "killpg",
            lambda *_args: (_ for _ in ()).throw(PermissionError()),
        )
        assert processes._group_pids(child.pid) == (child.pid,)
    finally:
        child.kill()
        child.wait()


def test_group_pids_stays_conservative_when_enumeration_is_denied(
    monkeypatch,
) -> None:
    from host_access import processes

    monkeypatch.setattr(
        processes.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError()),
    )
    monkeypatch.setattr(
        processes.psutil,
        "process_iter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            psutil.AccessDenied()
        ),
    )
    assert processes._group_pids(4242) == (4242,)
