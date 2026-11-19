"""Person-comment recovery runs on the startup drive without touching its other work."""

import asyncio
from types import SimpleNamespace

import pytest

from automation import restart_startup, update_verification


class _Log:
    def __init__(self):
        self.lines = []
        self.info = lambda message, *_: self.lines.append(("info", message))
        self.warning = lambda message, *_: self.lines.append(("warning", message))
        self.error = lambda message, *_: self.lines.append(("error", message))
        self.exception = lambda message, *_: self.lines.append(("exception", message))

    def has(self, level, text):
        return any(lvl == level and text in message for lvl, message in self.lines)


@pytest.fixture
def drive_parts(monkeypatch):
    seen = SimpleNamespace(continuity=0, settled=0)

    async def continuity(*_args):
        seen.continuity += 1

    async def settle(*_args):
        seen.settled += 1

    async def done():
        return None

    monkeypatch.setattr(restart_startup, "drive_startup_continuity", continuity)
    monkeypatch.setattr(update_verification, "settle_unconfirmed_update", settle)
    monkeypatch.setattr(restart_startup, "_remove_terminal_lifecycle_results", lambda _logger: None)
    return seen, done


def _recovery(monkeypatch, coroutine):
    from plans import public as plans

    monkeypatch.setattr(plans, "recover_person_comments", coroutine)


async def test_recovery_failure_is_logged_and_does_not_fail_the_drive(isolated_db, monkeypatch, drive_parts):
    seen, done = drive_parts
    log = _Log()

    async def broken():
        raise RuntimeError("recovery failed")

    _recovery(monkeypatch, broken)
    await restart_startup.run_startup_drive(done(), None, (), None, log)
    assert (seen.continuity, seen.settled) == (1, 1)
    assert log.has("info", "restart continuity drive complete")
    assert log.has("exception", "person comment recovery failed")
    assert not log.has("error", "drive failed")


async def test_recovery_hang_does_not_delay_other_startup_work_and_shutdown_cancels_it(
    isolated_db, monkeypatch, drive_parts
):
    seen, done = drive_parts
    log = _Log()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def hung():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    _recovery(monkeypatch, hung)
    task = asyncio.create_task(restart_startup.run_startup_drive(done(), None, (), None, log))
    await asyncio.wait_for(started.wait(), 5)
    for _ in range(200):
        if log.has("info", "restart continuity drive complete"):
            break
        await asyncio.sleep(0.01)
    assert log.has("info", "restart continuity drive complete")
    assert (seen.continuity, seen.settled) == (1, 1)
    assert not task.done() and not cancelled.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


async def test_shutdown_cancel_during_drive_work_still_reaches_recovery(isolated_db, monkeypatch, drive_parts):
    _seen, _done = drive_parts
    log = _Log()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def hung():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    _recovery(monkeypatch, hung)
    never = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(restart_startup.run_startup_drive(never, None, (), None, log))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert not log.has("info", "restart continuity drive complete")
