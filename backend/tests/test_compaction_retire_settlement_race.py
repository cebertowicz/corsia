"""Compaction retirement and the dead-generation owner settle one exit once."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from codex_runtime_fixtures import (
    _FenceSession,
    _active_live,
    _active_live_with_persisted_tool,
    _reset_runtime,
)

pytestmark = pytest.mark.usefixtures('signed_in_defaults')

THREAD_ID = "codex-compaction-settle-race"
ADOPT = "subagents.harness_checkpoint_resume.maintain_external_tool_boundary"


class _TerminatingSession(_FenceSession):
    """A session whose terminate exits the process."""

    async def terminate(self) -> None:
        await super().terminate()
        self.returncode = -15


class _Settlements:
    """Count every entry into the exit settlement and every terminal commit."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from subagents import codex_runtime

        self.process_exits = 0
        self.tool_results = 0
        self.terminals: list[tuple[str, str]] = []
        real_exit = codex_runtime._finish_process_exit
        real_tools = codex_runtime._tool_terminal.settle_codex_results
        real_commit = codex_runtime.commit_turn_terminal

        async def exit_(*args, **kwargs):
            self.process_exits += 1
            return await real_exit(*args, **kwargs)

        async def tools(*args, **kwargs):
            self.tool_results += 1
            return await real_tools(*args, **kwargs)

        async def commit(turn_id, outcome, error=None, **kwargs):
            self.terminals.append((outcome, str(error)))
            return await real_commit(turn_id, outcome, error, **kwargs)

        monkeypatch.setattr(codex_runtime, "_finish_process_exit", exit_)
        monkeypatch.setattr(codex_runtime._tool_terminal, "settle_codex_results", tools)
        monkeypatch.setattr(codex_runtime, "commit_turn_terminal", commit)


async def _pending_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[asyncio.Event, dict[str, object]]:
    """Adoption that blocks until released, then fails or succeeds as told."""
    gate = asyncio.Event()
    outcome: dict[str, object] = {"result": True}

    async def adopt(*_args, **_kwargs):
        await gate.wait()
        result = outcome["result"]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(ADOPT, adopt)
    return gate, outcome


async def _until_terminated(session) -> None:
    for _ in range(20):
        if session.returncode is not None:
            return
        await asyncio.sleep(0)
    raise AssertionError("compaction retirement never terminated the handle")


@pytest.mark.asyncio
async def test_inbox_trigger_does_not_settle_a_turn_while_adoption_is_pending(
    isolated_db, monkeypatch,
) -> None:
    from subagents import codex_runtime
    from subagents.codex_compaction_retire import retire_compacting_handle

    _reset_runtime()
    session = _TerminatingSession(THREAD_ID)
    live = _active_live(THREAD_ID, session, pending_tool=False)
    settlements = _Settlements(monkeypatch)
    gate, _outcome = await _pending_adoption(monkeypatch)
    try:
        retirement = asyncio.ensure_future(
            retire_compacting_handle(THREAD_ID, live, session)
        )
        await _until_terminated(session)

        settled = await codex_runtime.settle_dead_live_generation_if_observed(THREAD_ID)

        assert settled is False
        assert codex_runtime._LIVE.get(THREAD_ID) is live
        assert settlements.terminals == []
        gate.set()
        await retirement
        assert live.exit_owned_by_compaction_retire is False
        assert settlements.terminals == []
    finally:
        _reset_runtime()


@pytest.mark.asyncio
async def test_failed_adoption_and_inbox_trigger_settle_one_exit_once(
    isolated_db, monkeypatch,
) -> None:
    from subagents import codex_runtime
    from subagents.codex_compaction_retire import retire_compacting_handle

    _reset_runtime()
    session = _TerminatingSession(THREAD_ID)
    live, _step = _active_live_with_persisted_tool(THREAD_ID, session)
    settlements = _Settlements(monkeypatch)
    gate, outcome = await _pending_adoption(monkeypatch)
    outcome["result"] = RuntimeError("no checkpoint is admissible")
    try:
        retirement = asyncio.ensure_future(
            retire_compacting_handle(THREAD_ID, live, session)
        )
        await _until_terminated(session)
        gate.set()
        inbox = asyncio.ensure_future(
            codex_runtime.settle_dead_live_generation_if_observed(THREAD_ID)
        )
        results = await asyncio.gather(retirement, inbox, return_exceptions=True)

        assert isinstance(results[0], RuntimeError)
        assert settlements.process_exits == 1
        assert settlements.tool_results == 1
        assert len(settlements.terminals) == 1
        assert settlements.terminals[0][0] == "error"
        assert "no checkpoint is admissible" in settlements.terminals[0][1]
        assert codex_runtime._LIVE.get(THREAD_ID) is None
        assert THREAD_ID not in codex_runtime._APP_SESSIONS
        assert live.exit_owned_by_compaction_retire is False
        assert live.provider_context_untrusted is True
    finally:
        _reset_runtime()


@pytest.mark.asyncio
async def test_cancelled_retirement_settles_with_a_named_reason_and_releases_exit(
    isolated_db, monkeypatch,
) -> None:
    from subagents import codex_runtime
    from subagents.codex_compaction_retire import retire_compacting_handle

    _reset_runtime()
    session = _TerminatingSession(THREAD_ID)
    live = _active_live(THREAD_ID, session, pending_tool=False)
    settlements = _Settlements(monkeypatch)
    await _pending_adoption(monkeypatch)
    try:
        retirement = asyncio.ensure_future(
            retire_compacting_handle(THREAD_ID, live, session)
        )
        await _until_terminated(session)
        retirement.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retirement

        assert live.exit_owned_by_compaction_retire is False
        assert [(o, "cancelled" in e) for o, e in settlements.terminals] == [("error", True)]
        assert codex_runtime._LIVE.get(THREAD_ID) is None
    finally:
        _reset_runtime()


@pytest.mark.asyncio
async def test_adoption_that_never_finishes_is_bounded_and_settled(
    isolated_db, monkeypatch,
) -> None:
    from subagents import codex_compaction_retire, codex_runtime

    _reset_runtime()
    session = _TerminatingSession(THREAD_ID)
    live = _active_live(THREAD_ID, session, pending_tool=False)
    settlements = _Settlements(monkeypatch)
    await _pending_adoption(monkeypatch)
    monkeypatch.setattr(codex_compaction_retire, "ADOPTION_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(TimeoutError):
            await codex_compaction_retire.retire_compacting_handle(
                THREAD_ID, live, session,
            )

        assert live.exit_owned_by_compaction_retire is False
        assert [(o, "timed out after 0.05s" in e) for o, e in settlements.terminals] == [
            ("error", True),
        ]
        assert codex_runtime._LIVE.get(THREAD_ID) is None
    finally:
        _reset_runtime()


@pytest.mark.asyncio
async def test_exit_is_released_even_when_the_owner_cannot_settle_yet(
    isolated_db, monkeypatch,
) -> None:
    from subagents import codex_runtime
    from subagents.codex_compaction_retire import retire_compacting_handle

    _reset_runtime()
    session = _TerminatingSession(THREAD_ID)
    session._reader = asyncio.ensure_future(asyncio.sleep(60))
    live = _active_live(THREAD_ID, session, pending_tool=False)
    monkeypatch.setattr(ADOPT, AsyncMock(side_effect=RuntimeError("no cut")))
    try:
        with pytest.raises(RuntimeError, match="no cut"):
            await retire_compacting_handle(THREAD_ID, live, session)
        assert live.exit_owned_by_compaction_retire is False
        assert codex_runtime._LIVE.get(THREAD_ID) is live

        session._reader.cancel()
        await asyncio.gather(session._reader, return_exceptions=True)
        assert await codex_runtime.settle_dead_live_generation_if_observed(THREAD_ID)
        assert codex_runtime._LIVE.get(THREAD_ID) is None
    finally:
        _reset_runtime()
