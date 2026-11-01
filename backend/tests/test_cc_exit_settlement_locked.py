"""#646: a refused (locked) terminal write must not strand a Claude Code Turn.

The lock is injected at the shared terminal write (``finish_turn``), the one
every harness and Stop path goes through, so these tests exercise the shared
retry rather than any Claude Code-specific one.
"""

import asyncio
import sqlite3

import pytest

from test_cc_tool_failure_fence import _live_with_result_step
from test_cc_tool_result_echo_binding import _seed_thread

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


@pytest.fixture(autouse=True)
def restore_process_owner_admission():
    """Return the process-wide owner to its prior admission state after each test."""
    from tools import shell

    owner = shell.PROCESS_OWNER
    closed_turns = owner._closed_turns.copy()
    closed_leases = owner._closed_leases.copy()
    closing_threads = owner._closing_threads.copy()
    yield
    owner._closed_turns = closed_turns
    owner._closed_leases = closed_leases
    owner._closing_threads = closing_threads


class _Harness:
    """One live CC handle whose terminal write refuses while ``locked`` is set."""

    def __init__(self, monkeypatch, db, live, identity):
        from infra import lock_retry, thread_driver

        self.db, self.live, self.identity = db, live, identity
        self.locked = True
        self.refusals = 0
        real = thread_driver.write_transaction

        def write_transaction(db, *, scope):
            if scope == "finish_turn" and self.locked:
                self.refusals += 1
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return real(db, scope=scope)

        monkeypatch.setattr(thread_driver, "write_transaction", write_transaction)
        monkeypatch.setattr(lock_retry, "FIRST_DELAY_S", 0.05)
        monkeypatch.setattr(lock_retry, "MAX_DELAY_S", 0.2)

    def status(self) -> str:
        return self.db.execute(
            "SELECT status FROM active_turns WHERE id=?", (self.identity.turn_id,),
        ).fetchone()["status"]

    async def refused(self, count: int = 1, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            while self.refusals < count:
                await asyncio.sleep(0.01)

    async def until_terminal(self, timeout: float = 10.0) -> str:
        async with asyncio.timeout(timeout):
            while self.status() == "running":
                await asyncio.sleep(0.05)
        return self.status()


@pytest.fixture
async def harness(isolated_db, monkeypatch):
    from infra.database import get_db
    from subagents.cc_runtime_state import _LIVE

    db = get_db()
    identity = _seed_thread(db)
    broadcast = lambda _thread, _frame: None
    monkeypatch.setattr("infra.state.broadcast_to_thread", broadcast)
    live = await _live_with_result_step(db, identity, broadcast, [], with_step=False)
    held = _Harness(monkeypatch, db, live, identity)
    yield held
    held.locked = False  # a failed test must not leave the reader retrying
    await live.session.terminate()
    _LIVE.pop("thread-1", None)
    db.close()


@pytest.mark.asyncio
async def test_locked_exit_settlement_still_terminalizes_the_turn(harness):
    harness.live.session._proc.kill()
    await harness.refused()
    harness.locked = False
    assert await harness.until_terminal() == "error"


@pytest.mark.asyncio
async def test_lock_held_through_retries_then_clears_starts_the_queued_input(
    harness, monkeypatch,
):
    from core.turn_core import BOOT_EPOCH
    from infra import thread_execution
    from infra.thread_driver import admit_input

    driven = []

    async def capture(settlement):
        driven.append(settlement)

    monkeypatch.setattr(thread_execution, "drive_terminal_settlement", capture)
    queued = admit_input(
        "thread-1", role="user", content="next", intent="apply_now",
        message_id="queued-input", turn_id="turn-2", boot_epoch=BOOT_EPOCH,
        admitted_at="t", db=harness.db,
    )
    assert queued.batch is None
    harness.live.session._proc.kill()
    # More refusals than the removed three-attempt ladder could absorb.
    await harness.refused(4)
    assert harness.status() == "running" and not driven
    harness.locked = False
    assert await harness.until_terminal() == "error"
    async with asyncio.timeout(5):
        while not driven:
            await asyncio.sleep(0.01)
    assert driven[0].successor is not None


@pytest.mark.asyncio
async def test_stop_during_the_retry_completes_once_the_lock_clears(harness):
    """Stop waits behind the retrying exit owner; nothing deadlocks or strands."""
    from subagents import cc_stop

    harness.live.session._proc.kill()
    await harness.refused()
    stop = asyncio.ensure_future(
        cc_stop.stop("thread-1", expected_turn_id=harness.identity.turn_id),
    )
    await harness.refused(harness.refusals + 2)
    assert harness.status() == "running"
    harness.locked = False
    await asyncio.wait_for(stop, 10)
    assert await harness.until_terminal(3) in {"error", "cancelled"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_taken_in_rerun")
async def test_locked_settlement_with_close_in_flight_still_terminalizes(harness):
    from subagents.cc_runtime_quiescence import fence_tool_failure, finish_tool_failure
    from subagents.cc_stream_control import retain_terminal_driver

    live, session = harness.live, harness.live.session
    assert fence_tool_failure("thread-1", live, harness.identity, RuntimeError("tool"))
    assert session._closing
    driver = retain_terminal_driver(
        session, finish_tool_failure("thread-1", live, harness.identity),
    )
    await harness.refused(2)
    assert harness.status() == "running"
    harness.locked = False
    await asyncio.wait_for(asyncio.shield(driver), 10)
    assert harness.status() == "error"
