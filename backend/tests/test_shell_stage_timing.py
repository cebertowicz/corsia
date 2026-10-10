from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import sys

import pytest

from connections import github_agent
from core.turn_core import TurnIdentity
from tools import shell_stage_timing as sst

_TURN = TurnIdentity("stage-timing-turn", 1)
# A spawn reads the agents' GitHub account once; with the DB stubbed out there is none.
_NO_AGENT_ACCOUNT = github_agent.AgentAccountView("not_installed", absent=True)
_LOGGER = "tools.shell_stage_timing"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch):
    monkeypatch.setattr(sst, "_window_start", 0.0)
    monkeypatch.setattr(sst, "_window_lines", 0)
    monkeypatch.setattr(sst, "_suppressed", 0)


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(sst.time, "perf_counter", fake)
    return fake


def _lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER]


def test_fast_call_emits_nothing(clock, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    timer = sst.ShellStageTimer()
    clock.now += 0.2
    timer.mark("admission")
    timer.emit("returned")
    assert _lines(caplog) == []


def test_slow_call_logs_one_line_with_stage_attribution(clock, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    timer = sst.ShellStageTimer()
    for stage, seconds in (("admission", 1), ("policy", 2), ("envelope", 3)):
        clock.now += seconds
        timer.mark(stage)
    clock.now += 4  # in flight when the call ends
    timer.emit("cancelled")

    (line,) = _lines(caplog)
    fields = dict(part.split("=") for part in line.split()[1:])
    assert line.startswith("bash_stage_timing ")
    assert fields["outcome"] == "cancelled"
    assert fields["last_stage"] == "envelope"
    assert fields["admission_ms"] == "1000"
    assert fields["policy_ms"] == "2000"
    assert fields["guard_ms"] == "0"
    assert fields["envelope_ms"] == "3000"
    assert fields["unmarked_ms"] == "4000"
    assert fields["total_ms"] == "10000"
    assert set(fields) >= {f"{s}_ms" for s in sst.STAGES}


def test_unknown_outcome_is_normalised(clock, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    timer = sst.ShellStageTimer()
    clock.now += 6
    timer.emit("rm -rf /secret command text")
    (line,) = _lines(caplog)
    assert "outcome=exception" in line
    assert "secret" not in line


def test_lines_are_rate_bounded_and_report_suppressed(clock, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    for _ in range(sst.MAX_LINES_PER_WINDOW + 3):
        clock.now = 1000.0
        timer = sst.ShellStageTimer()
        clock.now = 1006.0  # every emit lands inside one window
        timer.emit("returned")
    assert len(_lines(caplog)) == sst.MAX_LINES_PER_WINDOW

    clock.now = 1006.0 + sst.WINDOW_SECONDS
    timer = sst.ShellStageTimer()
    clock.now += 6
    timer.emit("returned")
    assert _lines(caplog)[-1].endswith("suppressed=3")


def test_emit_never_raises(clock, monkeypatch) -> None:
    timer = sst.ShellStageTimer()
    clock.now += 6

    def boom(*_a, **_k):
        raise RuntimeError("limiter failed")

    monkeypatch.setattr(sst, "_admit_line", boom)
    timer.emit("returned")


def test_logging_failure_cannot_change_shell_result(clock, monkeypatch) -> None:
    timer = sst.ShellStageTimer()
    clock.now += 6

    def boom(*_a, **_k):
        raise RuntimeError("log sink failed")

    monkeypatch.setattr(sst.log, "warning", boom)
    timer.emit("returned")


def test_spawn_stage_observer_failure_cannot_abort_spawn() -> None:
    from host_access.spawn_stage import SPAWN_STAGE_HOOK, stage

    def boom(_name):
        raise RuntimeError("timer failed")

    token = SPAWN_STAGE_HOOK.set(boom)
    try:
        stage("exec")
    finally:
        SPAWN_STAGE_HOOK.reset(token)


@pytest.mark.asyncio
async def test_process_owner_reports_lock_wait_then_exec_and_default_is_silent(
    tmp_path,
) -> None:
    from host_access.processes import ProcessOwner
    from host_access.spawn_stage import SPAWN_STAGE_HOOK

    owner = ProcessOwner(
        runtime_dir=tmp_path,
        term_grace_seconds=0.1,
        kill_grace_seconds=1.0,
        max_records=8,
        max_log_bytes=512,
    )
    seen: list[str] = []
    try:
        token = SPAWN_STAGE_HOOK.set(seen.append)
        try:
            result = await owner.spawn(
                [sys.executable, "-c", "print('ok')"],
                thread_id="t",
                turn_id="u",
                cwd=Path.cwd(),
                env=os.environ,
                timeout_seconds=5.0,
                background=False,
            )
        finally:
            SPAWN_STAGE_HOOK.reset(token)
        assert seen == ["lock_wait", "exec"]
        assert result.stdout.strip() == b"ok"

        seen.clear()
        await owner.spawn(
            [sys.executable, "-c", "pass"],
            thread_id="t",
            turn_id="u",
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=5.0,
            background=False,
        )
        assert seen == []
    finally:
        await owner.shutdown()


@pytest.fixture
def bash_env(monkeypatch, tmp_path):
    """Route BashTool.execute through a fake process owner, no real Safehouse."""
    from tools import host_access_gate, shell

    async def consume(**_kwargs):
        return None

    monkeypatch.setattr(host_access_gate, "consume_current_action_permit", consume)
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda **_k: [])
    monkeypatch.setattr(shell, "controlled_fast_forward_policy", lambda *_a, **_k: None)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell, "is_path_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(shell, "execution_envelope_profile", lambda *_a, **_k: "(version 1)\n")
    monkeypatch.setattr(
        shell,
        "safehouse_argv",
        lambda command, _cwd, profile_path, **_k: ["/bin/echo", command],
    )
    monkeypatch.setattr(sst, "SLOW_CALL_SECONDS", 0.0)
    return shell


def _foreground_result(shell):
    from host_access.processes import ForegroundProcessResult

    return ForegroundProcessResult(
        record=None,
        stdout=b"hello\n",
        stderr=b"",
        returncode=0,
        timed_out=False,
        undeclared_background_stopped=False,
    )


async def _run(shell, tmp_path, command="echo SECRET_COMMAND_TEXT"):
    return await shell.BashTool(working_dir=str(tmp_path), default_timeout=30).execute(
        command,
        _thread_id="thread-sensitive-id",
        _host_access_turn_identity=_TURN,
    )


@pytest.mark.asyncio
async def test_bash_logs_stage_line_without_sensitive_data_and_keeps_result(
    bash_env, monkeypatch, tmp_path, caplog
) -> None:
    from host_access.spawn_stage import SPAWN_STAGE_HOOK

    shell = bash_env
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    observed: list[object] = []

    async def spawn(argv, **_kwargs):
        hook = SPAWN_STAGE_HOOK.get()
        observed.append(hook)
        hook("lock_wait")
        hook("exec")
        return _foreground_result(shell)

    async def observe(fn, *_a, **_k):
        return _NO_AGENT_ACCOUNT if fn is github_agent.view else None

    monkeypatch.setattr(shell.PROCESS_OWNER, "spawn", spawn)
    monkeypatch.setattr(shell, "run_in_db", observe)

    result = await _run(shell, tmp_path)

    assert result == "hello\n"
    assert observed and observed[0] is not None
    assert SPAWN_STAGE_HOOK.get() is None  # reset after spawn
    (line,) = _lines(caplog)
    assert "outcome=returned" in line and "last_stage=post" in line
    for stage in sst.STAGES:
        assert f"{stage}_ms=" in line
    for sensitive in ("SECRET_COMMAND_TEXT", "thread-sensitive-id", str(tmp_path), "echo"):
        assert sensitive not in line


@pytest.mark.asyncio
async def test_bash_spawn_failure_logs_exception_and_preserves_error(
    bash_env, monkeypatch, tmp_path, caplog
) -> None:
    shell = bash_env
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    async def spawn(argv, **_kwargs):
        raise RuntimeError("spawn exploded")

    monkeypatch.setattr(shell.PROCESS_OWNER, "spawn", spawn)

    result = await _run(shell, tmp_path)

    assert result == "Error executing command: spawn exploded"
    (line,) = _lines(caplog)
    assert "outcome=exception" in line and "last_stage=envelope" in line


@pytest.mark.asyncio
async def test_bash_cancellation_logs_cancelled_and_still_propagates(
    bash_env, monkeypatch, tmp_path, caplog
) -> None:
    shell = bash_env
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    entered = asyncio.Event()

    async def spawn(argv, **_kwargs):
        entered.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(shell.PROCESS_OWNER, "spawn", spawn)

    task = asyncio.create_task(_run(shell, tmp_path))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (line,) = _lines(caplog)
    assert "outcome=cancelled" in line and "last_stage=envelope" in line


@pytest.mark.asyncio
async def test_bash_fast_call_logs_nothing(bash_env, monkeypatch, tmp_path, caplog) -> None:
    shell = bash_env
    monkeypatch.setattr(sst, "SLOW_CALL_SECONDS", 3600.0)
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    async def spawn(argv, **_kwargs):
        return _foreground_result(shell)

    async def observe(fn, *_a, **_k):
        return _NO_AGENT_ACCOUNT if fn is github_agent.view else None

    monkeypatch.setattr(shell.PROCESS_OWNER, "spawn", spawn)
    monkeypatch.setattr(shell, "run_in_db", observe)

    assert await _run(shell, tmp_path) == "hello\n"
    assert _lines(caplog) == []
