"""#646: every harness's terminal write retries a lock refusal until it commits."""

import asyncio
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from infra.db_async import run_in_db

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


def _locked(code: int = sqlite3.SQLITE_BUSY, text: str = "database is locked"):
    error = sqlite3.OperationalError(text)
    error.sqlite_errorcode = code
    return error


class _Lock:
    """Another writer's lock, refusing the named write scopes while held."""

    def __init__(self, monkeypatch, *scopes: str, module: str = "infra.thread_driver"):
        import importlib

        from infra import lock_retry

        self.scopes, self.held, self.refusals = set(scopes), True, 0
        target = importlib.import_module(module)
        real = target.write_transaction

        def write_transaction(db, *, scope):
            if self.held and scope in self.scopes:
                self.refusals += 1
                raise _locked()
            return real(db, scope=scope)

        monkeypatch.setattr(target, "write_transaction", write_transaction)
        monkeypatch.setattr(lock_retry, "FIRST_DELAY_S", 0.01)
        monkeypatch.setattr(lock_retry, "MAX_DELAY_S", 0.02)

    async def refused(self, count: int) -> None:
        async with asyncio.timeout(5):
            while self.refusals < count:
                await asyncio.sleep(0.005)


def _running_turn(*, unsettled_write: bool = False, queued: bool = False) -> str:
    """One admitted, running Turn; optionally with a provider write never
    settled, or with input queued behind it."""
    from core.turn_core import BOOT_EPOCH
    from infra.database import get_db
    from infra.input_writes import begin_batch_write
    from infra.thread_driver import admit_input

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('thread-1','user-1','claude-code/sonnet','t','t')"
        )
        db.commit()
        admission = admit_input(
            "thread-1", role="user", content="go", intent="apply_now",
            message_id="turn-1:input", turn_id="turn-1", boot_epoch=BOOT_EPOCH,
            admitted_at="t", db=db,
        )
        assert admission.batch is not None
        if unsettled_write:
            begin_batch_write(
                admission.batch, attempt_id="turn-1:write", started_at="t", db=db,
            )
        if queued:
            assert admit_input(
                "thread-1", role="user", content="next", intent="apply_now",
                message_id="queued-input", turn_id="turn-2", boot_epoch=BOOT_EPOCH,
                admitted_at="t", db=db,
            ).batch is None
        db.commit()
    finally:
        db.close()
    return "turn-1"


def _turn_row(turn_id: str):
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(
            "SELECT status,ended_at FROM active_turns WHERE id=?", (turn_id,),
        ).fetchone()
    finally:
        db.close()


def test_a_lock_refusal_is_classified_by_its_code_as_well_as_its_text():
    from infra.database import is_lock_refusal

    assert is_lock_refusal(_locked(sqlite3.SQLITE_BUSY, "busy"))
    assert is_lock_refusal(_locked(sqlite3.SQLITE_BUSY_SNAPSHOT, "snapshot busy"))
    # A table lock within one connection or shared cache: waiting never clears it.
    assert not is_lock_refusal(_locked(sqlite3.SQLITE_LOCKED, "database table is locked"))
    assert not is_lock_refusal(_locked(sqlite3.SQLITE_LOCKED_SHAREDCACHE, "table locked"))
    assert is_lock_refusal(sqlite3.OperationalError("database is locked"))
    assert not is_lock_refusal(_locked(sqlite3.SQLITE_ERROR, "no such table: x"))
    assert not is_lock_refusal(RuntimeError("bridge error: OperationalError: database is locked"))


@pytest.mark.asyncio
async def test_a_refused_terminal_commits_once_the_lock_clears(isolated_db, monkeypatch, caplog):
    """The seam Codex and native settle through directly; CC adds quarantine."""
    from core.turn_core import TurnTerminalResult, commit_turn_terminal

    turn_id = await run_in_db(_running_turn, queued=True)
    lock = _Lock(monkeypatch, "finish_turn")
    caplog.set_level(logging.WARNING, logger="infra.lock_retry")
    task = asyncio.create_task(commit_turn_terminal(turn_id, "error", "exited", ended_at="t-end"))
    await lock.refused(6)
    assert _turn_row(turn_id)["status"] == "running" and not task.done()
    lock.held = False

    settlement = await asyncio.wait_for(task, 5)

    assert settlement.result is TurnTerminalResult.APPLIED
    assert settlement.successor is not None  # the queued input starts next
    assert tuple(_turn_row(turn_id)) == ("error", "t-end")
    errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and r.getMessage().startswith("terminal write")
    ]
    assert len(errors) == 1 and "turn turn-1" in errors[0].getMessage()


@pytest.mark.asyncio
async def test_a_refused_quarantine_is_retried_with_its_terminal(isolated_db, monkeypatch):
    from core.turn_core import TurnTerminalResult, commit_turn_terminal
    from infra.database import get_db

    turn_id = await run_in_db(_running_turn, unsettled_write=True)
    lock = _Lock(monkeypatch, "quarantine_unsettled_writes", module="core.turn_core")
    task = asyncio.create_task(
        commit_turn_terminal(turn_id, "error", "exited", quarantine="ended unsettled"),
    )
    await lock.refused(3)
    lock.held = False

    assert (await asyncio.wait_for(task, 5)).result is TurnTerminalResult.APPLIED
    db = get_db()
    try:
        outcomes = {
            row["write_outcome"] for row in db.execute(
                "SELECT write_outcome FROM messages WHERE delivery_attempt_id='turn-1:write'"
            )
        }
    finally:
        db.close()
    assert outcomes == {"ambiguous"}


@pytest.mark.asyncio
async def test_concurrent_settlers_need_no_handshake_and_one_applies(isolated_db, monkeypatch):
    from core.turn_core import TurnTerminalResult, commit_turn_terminal

    turn_id = await run_in_db(_running_turn)
    lock = _Lock(monkeypatch, "finish_turn")
    exit_owner = asyncio.create_task(commit_turn_terminal(turn_id, "error", "exited"))
    stop_owner = asyncio.create_task(commit_turn_terminal(turn_id, "cancelled"))
    await lock.refused(6)
    lock.held = False

    results = {
        settlement.result for settlement in
        await asyncio.wait_for(asyncio.gather(exit_owner, stop_owner), 5)
    }

    assert results == {TurnTerminalResult.APPLIED, TurnTerminalResult.CONFLICTING_TERMINAL}
    assert _turn_row(turn_id)["status"] in {"error", "cancelled"}


@pytest.mark.asyncio
async def test_native_stop_during_terminal_backoff_settles_once_the_lock_clears(
    isolated_db, monkeypatch,
):
    """Stop waits behind the Native owner's retried terminal; it never ends it."""
    from core import native_turn_runtime
    from core.native_turn_control import TurnCancelEvent
    from core.native_turn_runtime import NativeTurn, run_tracked_turn
    from core.turn_core import TurnIdentity, TurnTerminalResult
    from infra import state
    from ws.stop_runtime import stop_runtimes

    turn_id = await run_in_db(_running_turn, queued=True)
    lock = _Lock(monkeypatch, "finish_turn")
    settled = []

    async def run_native_turn(turn, _effects):
        await turn.cancel_event.wait()  # the provider loop ends on Stop's signal
        return "cancelled"

    async def drive_turn_settlement(settlement):
        settled.append(settlement)

    monkeypatch.setattr(native_turn_runtime, "_run_native_turn", run_native_turn)
    monkeypatch.setattr(native_turn_runtime, "drive_turn_settlement", drive_turn_settlement)
    identity = TurnIdentity(turn_id, 1)
    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(identity)
    published = []

    async def handle(kind, _payload):
        published.append(kind)

    turn = NativeTurn(
        agent=None, thread_id="thread-1", content="go", model="mock/model",
        turn_provider={}, handler=SimpleNamespace(handle=handle),
        cancel_event=cancel_event, identity=identity,
    )
    turn_task = asyncio.create_task(run_tracked_turn(turn))
    state._active_turns["thread-1"] = cancel_event
    state._active_turn_tasks["thread-1"] = turn_task
    try:
        cancel_event.set()
        await lock.refused(3)  # the terminal is in backoff with the lock held
        stop = asyncio.create_task(stop_runtimes(
            "thread-1", state._active_turns.get("thread-1"),
            state._active_llm_tasks.get("thread-1"), turn_id, None,
        ))
        await lock.refused(6)
        assert not stop.done() and not turn_task.done()
        lock.held = False

        assert await asyncio.wait_for(stop, 5) is True
        assert await asyncio.wait_for(turn_task, 5) is True
    finally:
        if not turn_task.done():
            turn_task.cancel()
        state._active_turns.pop("thread-1", None)
        state._active_turn_tasks.pop("thread-1", None)

    assert _turn_row(turn_id)["status"] == "cancelled" and published == ["turn_cancelled"]
    [settlement] = settled
    assert settlement.result is TurnTerminalResult.APPLIED
    assert settlement.successor is not None  # the queued input starts next


@pytest.mark.asyncio
@pytest.mark.parametrize("error", (
    _locked(sqlite3.SQLITE_ERROR, "no such table: active_turns"),
    _locked(sqlite3.SQLITE_LOCKED, "database table is locked"),
), ids=("error", "table-locked"))
async def test_any_other_database_error_is_the_callers_at_once(isolated_db, monkeypatch, error):
    from core import turn_core

    attempts = []

    def broken(*_args, **_kwargs):
        attempts.append(1)
        raise error

    monkeypatch.setattr(turn_core, "persist_turn_settlement", broken)

    with pytest.raises(sqlite3.OperationalError) as raised:
        await asyncio.wait_for(turn_core.commit_turn_terminal("turn-1", "error"), 5)
    assert raised.value is error and attempts == [1]
