"""Stopping an owned group whose leader has exited but is not yet reaped.

macOS answers ``killpg`` with EPERM when the only member left in a process group
is an exited, unreaped child. asyncio reaps its children on a watcher thread, so
a stop that lands between the leader's exit and that reap met a denial for a
group with no live members. These tests hold the real kernel state by pausing
asyncio's own ``waitpid`` and drive the public ``ProcessOwner.stop``.

Six of them need that real denial and carry a darwin skip. The rest are
collected unconditionally; only executed on macOS.
"""

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
import signal
import sys
import threading
import time

import psutil
import pytest

pytestmark = pytest.mark.integration

_needs_real_macos_denial = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="relies on macOS denying killpg for a group left with only an unreaped leader",
)

_WATCHER_THREAD_PREFIX = "asyncio-waitpid"
_LEADER_WITH_GROUP_CHILD = (
    "import subprocess,sys;"
    "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
    "print(child.pid,flush=True)"
)
_LEADER_WITH_TERM_RESISTANT_GROUP_CHILD = (
    "import subprocess,sys;"
    "subprocess.Popen([sys.executable,'-c',"
    "'import os,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
    "print(os.getpid(),flush=True);time.sleep(30)'])"
)


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.02)


def _is_unreaped(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _pid_gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


async def _record_test_transition(_record, _kind) -> None:
    """Acknowledge the durable callback boundary in ProcessOwner unit tests."""


class _ReapGate:
    """Pause asyncio's own child reaps; every other ``waitpid`` passes through."""

    def __init__(self, monkeypatch) -> None:
        self.release = threading.Event()
        self.callers: list[str] = []
        real_waitpid = os.waitpid

        def gated(pid, options):
            name = threading.current_thread().name
            self.callers.append(name)
            if name.startswith(_WATCHER_THREAD_PREFIX):
                self.release.wait(timeout=30)
            return real_waitpid(pid, options)

        monkeypatch.setattr(os, "waitpid", gated)


class _GroupSignals:
    """Record every ``killpg`` and optionally deny or react to an outcome.

    With ``reuse_after_absence`` the id answers again once the kernel has
    reported it gone, as a recycled group would; those calls are recorded and
    never reach a process.
    """

    def __init__(
        self, monkeypatch, *, on_denied=None, on_killed=None, before_signal=None,
    ) -> None:
        self.calls: list[tuple[int, int, str]] = []
        self.deny = lambda _pgid, _signal: False
        self.reuse_after_absence = False
        self.absent_at: int | None = None
        real_killpg = self.real_killpg = os.killpg

        def spy(pgid, selected):
            if self.reuse_after_absence and self.absent_at is not None:
                self.calls.append((pgid, int(selected), "reused"))
                return
            try:
                if self.deny(pgid, selected):
                    raise PermissionError(errno.EPERM, "Operation not permitted")
                if selected != 0 and before_signal is not None:
                    before_signal(pgid, selected)
                real_killpg(pgid, selected)
            except OSError as exc:
                self.calls.append((pgid, int(selected), type(exc).__name__))
                if isinstance(exc, ProcessLookupError) and self.absent_at is None:
                    self.absent_at = len(self.calls)
                if (
                    isinstance(exc, PermissionError)
                    and selected != 0
                    and on_denied is not None
                ):
                    on_denied()
                raise
            self.calls.append((pgid, int(selected), "ok"))
            if selected == signal.SIGKILL and on_killed is not None:
                on_killed()

        monkeypatch.setattr(os, "killpg", spy)

    def delivered(self, selected: signal.Signals) -> bool:
        return any(
            outcome == "ok" and number == int(selected)
            for _pgid, number, outcome in self.calls
        )


@pytest.fixture
def reap_gate(monkeypatch):
    gate = _ReapGate(monkeypatch)
    yield gate
    gate.release.set()


@pytest.fixture
async def owner_factory(tmp_path, reap_gate):
    from host_access import processes

    owners = []

    def build(**overrides):
        options = {"term_grace_seconds": 0.1, "kill_grace_seconds": 5.0}
        options.update(overrides)
        owner = processes.ProcessOwner(runtime_dir=tmp_path, **options)
        owner.configure_transition_callback(_record_test_transition)
        owners.append(owner)
        return owner

    yield build
    reap_gate.release.set()
    for owner in owners:
        try:
            await owner.shutdown()
        except processes.ProcessCleanupError:
            pass


async def _spawn_background(owner, code: str):
    started = await owner.spawn(
        [sys.executable, "-c", code],
        thread_id="thread-a",
        turn_id="turn-a",
        cwd=Path.cwd(),
        env=os.environ,
        timeout_seconds=30.0,
        background=True,
    )
    return started.handle, owner.record(started.handle).pid


def _hold_owner_watcher(owner, monkeypatch) -> None:
    """Keep the owner's exit watcher out, so every group call is the stop's."""
    hold = asyncio.Event()

    async def never_settles(_runtime) -> None:
        await hold.wait()

    monkeypatch.setattr(owner, "_await_background_exit", never_settles)


def _wait_for_real_absence(signals: _GroupSignals, pgid: int) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            signals.real_killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        time.sleep(0.02)
    raise AssertionError("group did not become absent")


async def _stop_and_expect_no_call_after_absence(owner, handle, signals, last):
    signals.reuse_after_absence = True
    try:
        record = await owner.stop(handle, thread_id="thread-a")
    finally:
        signals.reuse_after_absence = False
    assert signals.absent_at is not None
    assert signals.calls[signals.absent_at - 1] == last
    # Absence is final: the old id is neither probed nor signaled again.
    assert signals.calls[signals.absent_at:] == []
    assert record.terminal_state == "terminated"
    assert record.reason == "managed_stop"
    assert owner.live_count() == 0


async def test_term_that_finds_the_group_gone_ends_every_group_call(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    """First-try ESRCH at TERM: no absence poll and no KILL to a reused id."""
    owner = owner_factory(kill_grace_seconds=2.0)
    _hold_owner_watcher(owner, monkeypatch)
    reap_gate.release.set()
    handle, pid = await _spawn_background(owner, "import time;time.sleep(30)")
    died = {"done": False}

    def group_dies_before_the_signal(pgid, _selected) -> None:
        if not died["done"]:
            died["done"] = True
            signals.real_killpg(pgid, signal.SIGKILL)
            _wait_for_real_absence(signals, pgid)

    signals = _GroupSignals(monkeypatch, before_signal=group_dies_before_the_signal)

    await _stop_and_expect_no_call_after_absence(
        owner, handle, signals, (pid, int(signal.SIGTERM), "ProcessLookupError"),
    )


@_needs_real_macos_denial
async def test_term_denial_resolved_by_the_reap_ends_every_group_call(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    """Post-denial ESRCH at TERM: no absence poll and no KILL to a reused id."""
    owner = owner_factory(kill_grace_seconds=2.0)
    _hold_owner_watcher(owner, monkeypatch)
    handle, pid = await _spawn_background(owner, "pass")
    await _wait_until(lambda: _is_unreaped(pid))
    signals = _GroupSignals(monkeypatch, on_denied=reap_gate.release.set)

    await _stop_and_expect_no_call_after_absence(
        owner, handle, signals, (pid, 0, "ProcessLookupError"),
    )
    assert (pid, int(signal.SIGTERM), "PermissionError") in signals.calls


async def test_kill_that_finds_the_group_gone_ends_every_group_call(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    """First-try ESRCH at KILL: no absence poll of a reused id, and not killed."""
    owner = owner_factory(kill_grace_seconds=2.0)
    _hold_owner_watcher(owner, monkeypatch)
    reap_gate.release.set()
    handle, pid = await _spawn_background(
        owner, _LEADER_WITH_TERM_RESISTANT_GROUP_CHILD,
    )
    await _wait_until(
        lambda: owner.read_logs(handle, thread_id="thread-a").strip().isdigit()
    )
    descendant = int(owner.read_logs(handle, thread_id="thread-a").strip())
    assert os.getpgid(descendant) == pid
    await _wait_until(lambda: not psutil.pid_exists(pid))

    def descendant_dies_before_the_kill(pgid, selected) -> None:
        if selected == signal.SIGKILL:
            os.kill(descendant, signal.SIGKILL)
            _wait_for_real_absence(signals, pgid)

    signals = _GroupSignals(monkeypatch, before_signal=descendant_dies_before_the_kill)

    await _stop_and_expect_no_call_after_absence(
        owner, handle, signals, (pid, int(signal.SIGKILL), "ProcessLookupError"),
    )
    assert signals.delivered(signal.SIGTERM)


@_needs_real_macos_denial
async def test_kill_denial_resolved_by_the_reap_ends_every_group_call(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    """Post-denial ESRCH at KILL: no absence poll of a reused id, and not killed."""
    owner = owner_factory(term_grace_seconds=0.5, kill_grace_seconds=2.0)
    _hold_owner_watcher(owner, monkeypatch)
    handle, pid = await _spawn_background(owner, _LEADER_WITH_GROUP_CHILD)
    await _wait_until(lambda: _is_unreaped(pid))
    await _wait_until(
        lambda: owner.read_logs(handle, thread_id="thread-a").strip().isdigit()
    )
    descendant = int(owner.read_logs(handle, thread_id="thread-a").strip())
    assert os.getpgid(descendant) == pid
    signals = _GroupSignals(monkeypatch, on_denied=reap_gate.release.set)

    await _stop_and_expect_no_call_after_absence(
        owner, handle, signals, (pid, 0, "ProcessLookupError"),
    )
    assert signals.delivered(signal.SIGTERM)
    assert (pid, int(signal.SIGKILL), "PermissionError") in signals.calls
    assert _pid_gone(descendant)


async def test_stop_after_a_reaped_unsettled_exit_is_terminated(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    """Control: the terminal an exited leader already gets once it is reaped."""
    owner = owner_factory()
    hold_watcher = asyncio.Event()

    async def never_settles(_runtime) -> None:
        await hold_watcher.wait()

    monkeypatch.setattr(owner, "_await_background_exit", never_settles)
    reap_gate.release.set()
    handle, _pid = await _spawn_background(owner, "pass")
    runtime = owner._runtimes[handle]
    await _wait_until(lambda: runtime.process.returncode is not None)
    assert owner.record(handle).terminal_state == "running"

    record = await owner.stop(handle, thread_id="thread-a")

    assert record.terminal_state == "terminated"
    assert record.reason == "managed_stop"
    assert record.exit_code == 0
    assert owner.live_count() == 0


@_needs_real_macos_denial
async def test_stop_settles_an_exited_unreaped_leader(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    owner = owner_factory()
    handle, pid = await _spawn_background(owner, "pass")
    await _wait_until(lambda: _is_unreaped(pid))
    assert owner.record(handle).terminal_state == "running"
    _GroupSignals(monkeypatch, on_denied=reap_gate.release.set)

    record = await owner.stop(handle, thread_id="thread-a")

    assert record.terminal_state == "terminated"
    assert record.reason == "managed_stop"
    assert record.exit_code == 0
    assert owner.live_count() == 0
    # The leader is asyncio's child: only its watcher may collect the exit.
    assert reap_gate.callers
    assert all(name.startswith(_WATCHER_THREAD_PREFIX) for name in reap_gate.callers)
    await owner.shutdown()


@_needs_real_macos_denial
async def test_stop_signals_live_descendants_of_an_exited_unreaped_leader(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    owner = owner_factory(term_grace_seconds=1.0)
    handle, pid = await _spawn_background(owner, _LEADER_WITH_GROUP_CHILD)
    await _wait_until(lambda: _is_unreaped(pid))
    await _wait_until(
        lambda: owner.read_logs(handle, thread_id="thread-a").strip().isdigit()
    )
    descendant = int(owner.read_logs(handle, thread_id="thread-a").strip())
    assert os.getpgid(descendant) == pid
    signals = _GroupSignals(monkeypatch, on_denied=reap_gate.release.set)

    record = await owner.stop(handle, thread_id="thread-a")

    assert signals.delivered(signal.SIGTERM)
    assert not signals.delivered(signal.SIGKILL)
    assert _pid_gone(descendant)
    assert record.terminal_state == "terminated"
    assert record.reason == "managed_stop"
    assert owner.live_count() == 0
    await owner.shutdown()


async def test_stop_kills_a_term_resistant_descendant_of_an_exited_leader(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    """Control: an exited leader never shortens TERM, grace and KILL."""
    owner = owner_factory()
    handle, pid = await _spawn_background(
        owner, _LEADER_WITH_TERM_RESISTANT_GROUP_CHILD,
    )
    await _wait_until(lambda: _is_unreaped(pid))
    await _wait_until(
        lambda: owner.read_logs(handle, thread_id="thread-a").strip().isdigit()
    )
    descendant = int(owner.read_logs(handle, thread_id="thread-a").strip())
    assert os.getpgid(descendant) == pid
    signals = _GroupSignals(monkeypatch, on_killed=reap_gate.release.set)

    record = await owner.stop(handle, thread_id="thread-a")

    assert signals.delivered(signal.SIGTERM)
    assert signals.delivered(signal.SIGKILL)
    assert _pid_gone(descendant)
    assert record.terminal_state == "killed"
    assert owner.live_count() == 0
    await owner.shutdown()


async def test_denied_signal_to_a_live_leader_stays_denied_and_unverified(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = owner_factory(kill_grace_seconds=0.5)
    reap_gate.release.set()
    handle, pid = await _spawn_background(owner, "import time;time.sleep(30)")
    signals = _GroupSignals(monkeypatch)
    signals.deny = lambda pgid, selected: pgid == pid and selected != 0

    started = time.monotonic()
    with pytest.raises(processes.ProcessCleanupError, match="signal was denied"):
        await owner.stop(handle, thread_id="thread-a")
    elapsed = time.monotonic() - started

    # A running leader is a real denial; the wait for its reap is bounded.
    assert elapsed < 3.0
    assert owner.record(handle).terminal_state == "death_unverified"
    assert not _pid_gone(pid)
    assert owner.live_count() == 1

    signals.deny = lambda _pgid, _signal: False
    record = await owner.stop(handle, thread_id="thread-a")
    assert record.terminal_state == "terminated"
    assert _pid_gone(pid)


async def test_denial_that_outlives_the_reap_is_never_success(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    """An exited leader whose group still refuses signals keeps a live member."""
    from host_access import processes

    owner = owner_factory()
    handle, pid = await _spawn_background(owner, _LEADER_WITH_GROUP_CHILD)
    await _wait_until(
        lambda: owner.read_logs(handle, thread_id="thread-a").strip().isdigit()
    )
    descendant = int(owner.read_logs(handle, thread_id="thread-a").strip())
    await _wait_until(lambda: _is_unreaped(pid))
    signals = _GroupSignals(monkeypatch, on_denied=reap_gate.release.set)
    signals.deny = lambda pgid, _signal: pgid == pid

    with pytest.raises(processes.ProcessCleanupError, match="signal was denied"):
        await owner.stop(handle, thread_id="thread-a")

    assert owner.record(handle).terminal_state == "death_unverified"
    assert owner.live_count() == 1
    assert not any(outcome == "ok" for _pgid, _number, outcome in signals.calls)
    signals.deny = lambda _pgid, _signal: False
    os.kill(descendant, signal.SIGKILL)


@_needs_real_macos_denial
async def test_denial_that_clears_within_the_absence_grace_settles(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    """After the reap, a group still denied is judged by the absence grace."""
    owner = owner_factory(term_grace_seconds=8.0)
    handle, pid = await _spawn_background(
        owner,
        "import subprocess,sys;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "print(child.pid,flush=True)",
    )
    await _wait_until(
        lambda: owner.read_logs(handle, thread_id="thread-a").strip().isdigit()
    )
    descendant = int(owner.read_logs(handle, thread_id="thread-a").strip())
    await _wait_until(lambda: _is_unreaped(pid))
    signals = _GroupSignals(monkeypatch, on_denied=reap_gate.release.set)
    retried = {"denied": False}
    refused = {"signals": 0}

    def deny_the_first_two_signals(pgid, selected) -> bool:
        # The watcher probes this group too, so the script keys on delivered
        # signals rather than on a call count of probes.
        if pgid != pid or selected == 0 or refused["signals"] >= 2:
            return False
        refused["signals"] += 1
        retried["denied"] = refused["signals"] == 2
        if retried["denied"]:
            # The live member goes away only after the retry was refused.
            os.kill(descendant, signal.SIGKILL)
        return True

    signals.deny = deny_the_first_two_signals

    record = await owner.stop(handle, thread_id="thread-a")

    assert retried["denied"]
    assert record.terminal_state == "terminated"
    assert owner.live_count() == 0
    # A group proven absent is never signaled again; the last call is a probe
    # that found no live member (ESRCH, or EPERM for an unreaped zombie).
    assert signals.calls[-1][:2] == (pid, 0)
    assert signals.calls[-1][2] in {"ProcessLookupError", "PermissionError"}
    assert not any(
        outcome == "ok" and number != 0 for _pgid, number, outcome in signals.calls
    )


async def test_unreaped_exited_leader_is_bounded_and_unverified(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = owner_factory(kill_grace_seconds=0.3)
    handle, pid = await _spawn_background(owner, "pass")
    await _wait_until(lambda: _is_unreaped(pid))
    _GroupSignals(monkeypatch)

    started = time.monotonic()
    with pytest.raises(processes.ProcessCleanupError):
        await owner.stop(handle, thread_id="thread-a")
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert owner.record(handle).terminal_state == "death_unverified"
    assert owner.live_count() == 1

    reap_gate.release.set()
    record = await owner.stop(handle, thread_id="thread-a")
    assert record.terminal_state == "terminated"
    assert owner.live_count() == 0


@_needs_real_macos_denial
async def test_reused_leader_identity_after_the_reap_is_not_signaled(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    from host_access import processes

    owner = owner_factory()
    handle, pid = await _spawn_background(owner, "pass")
    await _wait_until(lambda: _is_unreaped(pid))
    signals = _GroupSignals(monkeypatch, on_denied=reap_gate.release.set)
    reused = {"on": False}
    real_process = psutil.Process
    real_fingerprint = processes._process_creation_fingerprint
    real_wait = owner._wait_for_process_exit

    class _Replacement:
        @staticmethod
        def status():
            return psutil.STATUS_RUNNING

    def process_or_replacement(candidate=None):
        if reused["on"] and candidate == pid:
            return _Replacement()
        return real_process(candidate)

    async def reap_then_reuse(runtime, timeout):
        await real_wait(runtime, timeout)
        reused["on"] = True
        signals.calls.clear()

    monkeypatch.setattr(processes.psutil, "Process", process_or_replacement)
    monkeypatch.setattr(
        processes,
        "_process_creation_fingerprint",
        lambda candidate: (
            "replacement"
            if reused["on"] and candidate == pid
            else real_fingerprint(candidate)
        ),
    )
    monkeypatch.setattr(owner, "_wait_for_process_exit", reap_then_reuse)

    try:
        with pytest.raises(processes.ProcessCleanupError, match="identity changed"):
            await owner.stop(handle, thread_id="thread-a")
        assert signals.calls == []
        assert owner.record(handle).terminal_state == "death_unverified"
    finally:
        reused["on"] = False


async def test_injected_denial_then_reap_then_absence_settles_without_a_signal(
    owner_factory,
    reap_gate,
    monkeypatch,
) -> None:
    """Denial, asyncio's reap, then ESRCH: needs no kernel denial and no reap gate."""
    owner = owner_factory(kill_grace_seconds=5.0)
    _hold_owner_watcher(owner, monkeypatch)
    reap_gate.release.set()
    handle, pid = await _spawn_background(owner, "import time;time.sleep(30)")
    signals = _GroupSignals(monkeypatch)
    denied = {"once": False}

    def group_dies_as_the_signal_is_denied(pgid, selected) -> bool:
        if pgid != pid or selected == 0 or denied["once"]:
            return False
        denied["once"] = True
        signals.real_killpg(pgid, signal.SIGKILL)
        return True

    signals.deny = group_dies_as_the_signal_is_denied

    await _stop_and_expect_no_call_after_absence(
        owner, handle, signals, (pid, 0, "ProcessLookupError"),
    )
    assert signals.calls[0] == (pid, 0, "ok")
    assert signals.calls[1] == (pid, int(signal.SIGTERM), "PermissionError")
    assert not any(
        outcome == "ok" and number != 0 for _pgid, number, outcome in signals.calls
    )
