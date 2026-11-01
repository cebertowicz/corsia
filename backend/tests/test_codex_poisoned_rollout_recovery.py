"""Codex resume identity and terminal tool-result ordering."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from codex_runtime_fixtures import _FenceSession, _active_live, _reset_runtime

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login

@pytest.mark.asyncio
async def test_transport_exit_preserves_durable_resume_identity(
    monkeypatch,
) -> None:
    from core.turn_core import TurnTerminalResult
    from subagents import codex_runtime

    thread_id = "codex-transport-exit"
    session = object()
    live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=session,
        parent_thread_id=None,
        name="codex",
    )
    monkeypatch.setattr(codex_runtime, "_settle_live_tool_debt", AsyncMock())
    monkeypatch.setattr(
        codex_runtime,
        "_finish_process_exit",
        AsyncMock(return_value=TurnTerminalResult.APPLIED),
    )
    codex_runtime._LIVE.put(thread_id, live)
    codex_runtime._APP_SESSIONS[thread_id] = session
    codex_runtime._CODEX_THREADS[thread_id] = "provider-thread"
    try:
        await codex_runtime._finish_transport_exit(
            thread_id,
            live,
            session,
            1,
            "arbitrary provider transport failure",
            lambda *_args: None,
        )
        assert codex_runtime._CODEX_THREADS[thread_id] == "provider-thread"
    finally:
        codex_runtime._LIVE.pop(thread_id, None)
        codex_runtime._APP_SESSIONS.pop(thread_id, None)
        codex_runtime._CODEX_THREADS.pop(thread_id, None)


@pytest.mark.asyncio
async def test_transport_exit_retires_live_after_committed_publication_failure(
    isolated_db, monkeypatch,
) -> None:
    """A post-commit publication failure cannot strand an exited owner."""
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = "codex-transport-exit-publication-failure"
    session = _FenceSession(thread_id)
    live = _active_live(thread_id, session, pending_tool=False)
    monkeypatch.setattr(
        codex_runtime,
        "_publish_claimed_terminal",
        AsyncMock(side_effect=RuntimeError("publication failed after commit")),
    )
    try:
        with pytest.raises(RuntimeError, match="publication failed after commit"):
            await codex_runtime._finish_transport_exit(
                thread_id, live, session, 1, "provider exited", lambda *_args: None,
            )

        assert codex_runtime._LIVE.get(thread_id) is None
        assert thread_id not in codex_runtime._APP_SESSIONS
    finally:
        _reset_runtime()


@pytest.mark.usefixtures("no_taken_in_rerun")
@pytest.mark.asyncio
async def test_transport_exit_retires_after_commit_without_durability_probe(
    isolated_db, monkeypatch,
) -> None:
    """A committed publication failure retires without re-reading terminal state."""
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = "codex-transport-exit-no-durability-probe"
    session = _FenceSession(thread_id)
    live = _active_live(thread_id, session, pending_tool=False)
    publication_error = RuntimeError("publication failed after commit")
    monkeypatch.setattr(
        codex_runtime,
        "_publish_claimed_terminal",
        AsyncMock(side_effect=publication_error),
    )
    monkeypatch.setattr(
        codex_runtime,
        "run_in_db",
        AsyncMock(side_effect=AssertionError("durability probe must not run")),
        raising=False,
    )
    try:
        with pytest.raises(RuntimeError) as raised:
            await codex_runtime._finish_transport_exit(
                thread_id, live, session, 1, "provider exited", lambda *_args: None,
            )

        assert raised.value is publication_error
        assert codex_runtime._LIVE.get(thread_id) is None
        assert thread_id not in codex_runtime._APP_SESSIONS
    finally:
        _reset_runtime()


@pytest.mark.asyncio
async def test_transport_exit_retires_before_driving_committed_successor(
    isolated_db, monkeypatch,
) -> None:
    """The successor cannot observe the exited Codex generation as live."""
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = "codex-transport-exit-drives-after-retirement"
    session = _FenceSession(thread_id)
    live = _active_live(thread_id, session, pending_tool=False)
    queued = admit_input(
        thread_id,
        role="user",
        content="queued successor",
        intent="apply_now",
        message_id=f"{thread_id}-queued",
        turn_id=f"{thread_id}-successor",
        boot_epoch=BOOT_EPOCH,
        admitted_at=utc_now_iso(),
    )
    assert queued.batch is None
    drives = []

    async def record_drive(settlement) -> None:
        drives.append(settlement)
        assert codex_runtime._LIVE.get(thread_id) is None
        assert thread_id not in codex_runtime._APP_SESSIONS

    monkeypatch.setattr(
        codex_runtime.turn_core, "drive_turn_settlement", record_drive,
    )
    try:
        await codex_runtime._finish_transport_exit(
            thread_id, live, session, 1, "provider exited", lambda *_args: None,
        )

        assert len(drives) == 1
        assert drives[0].successor is not None
        assert codex_runtime._LIVE.get(thread_id) is None
        assert thread_id not in codex_runtime._APP_SESSIONS
    finally:
        _reset_runtime()


@pytest.mark.asyncio
async def test_transport_exit_retires_before_raising_successor_drive(
    isolated_db, monkeypatch,
) -> None:
    """A failed successor drive cannot strand its exited predecessor."""
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso
    from subagents import codex_runtime
    from subagents.codex_runtime_errors import CodexLiveTurnBusy

    _reset_runtime()
    thread_id = "codex-transport-exit-raising-successor-drive"
    session = _FenceSession(thread_id)
    live = _active_live(thread_id, session, pending_tool=False)
    queued = admit_input(
        thread_id,
        role="user",
        content="queued successor",
        intent="apply_now",
        message_id=f"{thread_id}-queued",
        turn_id=f"{thread_id}-successor",
        boot_epoch=BOOT_EPOCH,
        admitted_at=utc_now_iso(),
    )
    assert queued.batch is None
    drives = []

    async def raise_busy(settlement) -> None:
        drives.append(settlement)
        raise CodexLiveTurnBusy("successor dispatch attempted")

    monkeypatch.setattr(
        codex_runtime.turn_core, "drive_turn_settlement", raise_busy,
    )
    try:
        with pytest.raises(CodexLiveTurnBusy, match="successor dispatch attempted"):
            await codex_runtime._finish_transport_exit(
                thread_id, live, session, 1, "provider exited", lambda *_args: None,
            )

        assert len(drives) == 1
        assert drives[0].successor is not None
        assert codex_runtime._LIVE.get(thread_id) is None
        assert thread_id not in codex_runtime._APP_SESSIONS
    finally:
        _reset_runtime()


@pytest.mark.asyncio
async def test_dead_generation_settlement_retains_live_after_precommit_exit_failure(
    isolated_db, monkeypatch,
) -> None:
    """Pre-commit exit finalization failures retain the owner for retry."""
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = "codex-dead-generation-exit-failure"
    session = _FenceSession(thread_id)
    session.returncode = 1
    live = _active_live(thread_id, session, pending_tool=False)
    monkeypatch.setattr(
        codex_runtime,
        "_finish_process_exit",
        AsyncMock(side_effect=RuntimeError("unexpected exit finalization failure")),
    )
    try:
        with pytest.raises(RuntimeError, match="unexpected exit finalization failure"):
            await codex_runtime.settle_dead_live_generation_if_observed(thread_id)

        assert codex_runtime._LIVE.get(thread_id) is live
        assert codex_runtime._APP_SESSIONS.get(thread_id) is session
    finally:
        _reset_runtime()


@pytest.mark.asyncio
async def test_dead_generation_settlement_retires_live_after_committed_exit_failure(
    isolated_db, monkeypatch,
) -> None:
    """A deferred committed publication failure cannot strand an exited owner."""
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = "codex-dead-generation-committed-exit-failure"
    session = _FenceSession(thread_id)
    session.returncode = 1
    live = _active_live(thread_id, session, pending_tool=False)
    publication_error = RuntimeError("committed exit publication failure")
    monkeypatch.setattr(
        codex_runtime,
        "_publish_claimed_terminal",
        AsyncMock(side_effect=publication_error),
    )
    try:
        with pytest.raises(RuntimeError) as raised:
            await codex_runtime.settle_dead_live_generation_if_observed(thread_id)

        assert raised.value is publication_error
        assert codex_runtime._LIVE.get(thread_id) is None
        assert thread_id not in codex_runtime._APP_SESSIONS
    finally:
        _reset_runtime()


@pytest.mark.asyncio
async def test_interrupted_turn_settles_tool_debt_before_terminal(
    monkeypatch,
) -> None:
    """Stop/retire/preempt terminals settle durable debt before the claim."""
    from core.turn_core import TurnIdentity, TurnTerminalResult
    from subagents import codex_runtime

    thread_id = "codex-interrupt-settles-debt"
    live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=object(),
        parent_thread_id=None,
        name="codex",
        corsia_turn=TurnIdentity("codex-interrupt-turn", 1),
    )
    order: list[str] = []

    async def record_settle(child_id, live_arg, bcast) -> None:
        order.append("settle")

    async def record_claim(live_arg, outcome, error=None, db=None, *, identity=None):
        order.append("claim")
        return TurnTerminalResult.CONFLICTING_TERMINAL

    monkeypatch.setattr(codex_runtime, "_settle_live_tool_debt", record_settle)
    monkeypatch.setattr(codex_runtime, "_claim_live_terminal", record_claim)

    result = await codex_runtime._finish_interrupted_turn(
        thread_id, live, "test-interrupt", lambda *_args: None,
    )

    assert order == ["settle", "claim"]
    assert result is TurnTerminalResult.CONFLICTING_TERMINAL
