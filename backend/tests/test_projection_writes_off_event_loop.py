"""The account-projection and runtime-context writes stay off the event loop.

Three async paths opened SQLite write transactions on the thread running the
event loop, each reported once by ``infra/db_loop_guard``: ``turn/start``
admission validating the account projection, and a terminal Codex context
observation reaching the model row. (The third, a Codex fork's operation-home
establish and retirement, is proved in
``test_account_operations_homes.py::test_added_account_warm_checkpoint_reaches_a_disposable_launch``.)

With ``CORSIA_DB_LOOP_WRITE_STRICT=1`` an outermost ``write_transaction`` opened
on a loop thread raises ``LoopThreadWriteError`` instead of only reporting, so
each case below drives its real async entry point and fails loudly if the write
moves back onto the loop. Everything the test itself needs to write is written
before the switch is armed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from typing import Literal
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from infra.db_loop_guard import LOOP_WRITE_STRICT_ENV
from tests.test_account_operations_homes import _launched_session  # noqa: F401 - shared home fixture helpers
from vendor_account_fixtures import accounts, vendors  # noqa: F401 - shared signed-in account fixtures


@pytest.mark.asyncio
async def test_turn_start_admission_writes_off_the_event_loop(vendors, tmp_path, monkeypatch):
    """``turn/start`` admission reaches the projection validate off the loop."""
    from subagents.vendor_write_admission import prepare_session_launch

    _db, _row, session = _launched_session(vendors, 'codex', tmp_path)
    prepare_session_launch(session, 'codex')
    writes: list[bytes] = []

    def write(data):
        writes.append(data)
        identity = json.loads(data)['id']
        asyncio.get_running_loop().call_soon(session._pending[identity].set_result, {})

    session._proc = SimpleNamespace(
        stdin=SimpleNamespace(write=write, drain=AsyncMock()), returncode=None,
    )
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, '1')

    await session._request('turn/start', {'input': 'input'})

    assert len(writes) == 1


@pytest.mark.asyncio
async def test_session_closing_during_admission_writes_nothing(vendors, tmp_path, monkeypatch):
    """Close does not take the write lock, so a close that begins during the admission hop still refuses the write."""
    from subagents import codex_requests
    from subagents.vendor_write_admission import prepare_session_launch

    _db, _row, session = _launched_session(vendors, 'codex', tmp_path)
    prepare_session_launch(session, 'codex')
    writes: list[bytes] = []
    session._proc = SimpleNamespace(
        stdin=SimpleNamespace(write=writes.append, drain=AsyncMock()), returncode=None,
    )

    def admit_then_close(admitted, provider):
        admitted._closing = True

    monkeypatch.setattr(codex_requests, 'require_session_write', admit_then_close)

    with pytest.raises(RuntimeError, match='terminated before provider write'):
        await session._request('turn/start', {'input': 'input'})

    assert writes == []


@pytest.fixture
def observed_codex_model(isolated_db):
    """One Codex model row whose reported identity a signed-in account lists now."""
    from vendor_account_fixtures import signed_in_default

    metadata = {"resolved_identity": "reported-model", "harness_version": "test-version"}
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
            "display_name,context_window,metadata_json,created_at,updated_at) "
            "VALUES ('model','codex','codex','selector','codex/selector','Test model',190000,?,'t','t')",
            (json.dumps(metadata),),
        )
        db.commit()
    signed_in_default('codex')
    return isolated_db


def _stored_model(path) -> dict:
    with closing(sqlite3.connect(str(path))) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT context_window,metadata_json FROM models WHERE id='model'").fetchone()
    return {"context_window": row["context_window"], "metadata": json.loads(row["metadata_json"])}


@pytest.mark.asyncio
async def test_terminal_context_observation_writes_off_the_event_loop(observed_codex_model, monkeypatch):
    """A terminal Codex observation reaches the model row from the DB pool."""
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime_terminal as terminal
    from subagents.codex_runtime_state import _LiveCodex

    live = _LiveCodex(
        codex_thread_id="provider-1", session=None, parent_thread_id=None, name="codex",
        model="selector", catalog_model_id="codex/selector",
        provider_reported_model="reported-model",
        context_window=190_000, discovered_context_window=190_000,
    )
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, '1')

    payload = await terminal.publish_claimed_terminal(
        thread_id="thread-1", live=live, identity=TurnIdentity("turn-1", 1),
        bcast=lambda *_args: None, source="codex-app-server:turn/result",
        usage={"input_tokens": 100, "cached_input_tokens": 10, "model_context_window": 180_000},
    )

    assert payload["type"] == "turn_complete"
    stored = _stored_model(observed_codex_model)
    observation = stored["metadata"]["context_observation"]
    assert observation["source"] == "codex-app-server:turn/result"
    assert observation["effective_tokens"] == 180_000
    assert observation["resolved_identity"] == "reported-model"
    # The row's own context_window is the catalog value and stays it: readers
    # apply the observation while the identity matches, they do not rewrite it.
    assert stored["context_window"] == 190_000


class _RecordingDirectory:
    """A sync manager that records which thread entered, ran and exited it."""

    def __init__(self, events: list[str], loop_thread: int) -> None:
        self.events = events
        self.loop_thread = loop_thread

    def __enter__(self) -> str:
        self.events.append(f"enter:{threading.get_ident() != self.loop_thread}")
        return "value"

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        self.events.append(f"exit:{exc_type.__name__ if exc_type else 'none'}")
        return False


@pytest.mark.asyncio
async def test_use_db_context_moves_enter_and_exit_off_the_loop(monkeypatch):
    """The manager enters and retires on the DB pool while the body stays on the loop."""
    from infra.db_async import use_db_context

    events: list[str] = []
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, '1')
    loop_thread = threading.get_ident()

    async with use_db_context(_RecordingDirectory(events, loop_thread)) as value:
        events.append(f"body:{threading.get_ident() != loop_thread}")
        assert value == "value"

    assert events == ["enter:True", "body:False", "exit:none"]

    class _RefusingEnter(_RecordingDirectory):
        def __enter__(self) -> str:
            self.events.append("enter:refused")
            raise RuntimeError("enter refused")

    with pytest.raises(RuntimeError, match="enter refused"):
        async with use_db_context(_RefusingEnter(events, loop_thread)):
            events.append("unreachable")

    # Like `with`, a manager that fails to enter has nothing to exit.
    assert events == ["enter:True", "body:False", "exit:none", "enter:refused"]


@pytest.mark.asyncio
async def test_use_db_context_exit_replaces_the_callers_error(monkeypatch):
    """A failure inside the manager's retirement wins, with the caller's error as context."""
    from infra.db_async import use_db_context

    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, '1')

    class _FailingRetire(_RecordingDirectory):
        def __exit__(self, exc_type, exc, tb) -> Literal[False]:
            raise RuntimeError("retire failed")

    with pytest.raises(RuntimeError, match="retire failed") as raised:
        async with use_db_context(_FailingRetire([], threading.get_ident())):
            raise ValueError("caller failed")

    assert isinstance(raised.value.__context__, ValueError)


@pytest.mark.asyncio
async def test_use_db_context_exits_a_manager_entered_after_cancellation():
    """A caller cancelled while the enter job runs still exits what that job entered."""
    from infra.db_async import use_db_context

    events: list[str] = []
    entering, release = threading.Event(), threading.Event()

    class _SlowEnter(_RecordingDirectory):
        def __enter__(self) -> str:
            entering.set()
            release.wait(5)
            return super().__enter__()

    async def body():
        async with use_db_context(_SlowEnter(events, threading.get_ident())):
            events.append("unreachable")

    task = asyncio.create_task(body())
    await asyncio.to_thread(entering.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == ["enter:True", "exit:CancelledError"]


@pytest.mark.asyncio
async def test_use_db_context_keeps_an_exit_that_suppresses():
    """A manager whose exit returns true suppresses the caller's error, as `with` does."""
    from infra.db_async import use_db_context

    events: list[str] = []

    class _Suppressing:
        def __enter__(self) -> None:
            events.append("enter")

        def __exit__(self, exc_type, exc, tb) -> Literal[True]:
            events.append(f"exit:{exc_type.__name__}")
            return True

    async with use_db_context(_Suppressing()):
        raise ValueError("suppressed")

    assert events == ["enter", "exit:ValueError"]
