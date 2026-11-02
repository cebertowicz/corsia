"""A Codex Turn that is ending itself must never look ownerless.

``active_turns.status='active'`` records that a Turn was opened and has not
reached a terminal. Anything that derives liveness from the runtime handles —
restart drain, the head-claim gate, ``violations()`` — reads "active row, no
owner" as an owner that died. So a Codex terminal route has to keep owning its
Turn until that Turn's terminal is durably committed, and release the handle
only after.

Each test below drives one Codex terminal route while an observer samples the
runtime handles at every scheduler yield, and asserts the Turn is owned at every
one of those boundaries up to and including the durable terminal commit.
"""

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


class _OwnerProbe:
    """What an external observer of the runtime handles sees, sampled often.

    ``asyncio`` is cooperative, so a task that samples and then yields runs at
    every scheduler yield of the route under test. That makes the window
    deterministic instead of timing-dependent: if a route retires its handle
    before its terminal and then awaits anything at all, this probe sees it.
    """

    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id
        self.observed: list[bool] = []
        self.attached: list[bool] = []
        self.committed_at: int | None = None
        self.owned_at_commit: bool | None = None
        self._task: asyncio.Task | None = None

    def _observe(self) -> bool:
        """The liveness answer derived from Codex's own runtime handles.

        This is the exact predicate a derived-liveness reader consults for a
        Codex thread — a live handle, a callback-capable app session, or a
        transition in flight — so what it answers here is what restart drain
        and the head-claim gate would answer.
        """
        from subagents import codex_runtime

        return codex_runtime.has_pending_or_live(self._thread_id)

    def _attached(self) -> bool:
        """Handle presence alone, without the transition-lock evidence."""
        from subagents import codex_runtime

        return (
            codex_runtime._LIVE.get(self._thread_id) is not None
            or self._thread_id in codex_runtime._APP_SESSIONS
        )

    def record_commit(self) -> None:
        """Mark the sample index at which this Turn's terminal became durable."""
        if self.committed_at is None:
            self.committed_at = len(self.observed)
            self.owned_at_commit = self._observe() and self._attached()

    async def _watch(self) -> None:
        while True:
            self.observed.append(self._observe())
            self.attached.append(self._attached())
            await asyncio.sleep(0)

    async def __aenter__(self) -> "_OwnerProbe":
        self._task = asyncio.create_task(self._watch())
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def assert_owned_until_terminal(self, route: str) -> None:
        """Fail if any sampled boundary before the terminal saw no owner."""
        assert self.committed_at is not None, (
            f"{route}: no durable terminal was claimed for this Turn"
        )
        assert self.owned_at_commit, (
            f"{route}: the terminal committed for a Turn whose runtime handle "
            f"was already retired"
        )
        ownerless = [
            index
            for index, owned in enumerate(self.observed[: self.committed_at])
            if not owned
        ]
        assert not ownerless, (
            f"{route}: the Turn looked ownerless at await boundaries "
            f"{ownerless} of {self.committed_at} before its terminal committed"
        )
        detached = [
            index
            for index, present in enumerate(self.attached[: self.committed_at])
            if not present
        ]
        assert not detached, (
            f"{route}: the live handle was retired at await boundaries "
            f"{detached} of {self.committed_at} before its terminal committed"
        )


@pytest.fixture
def owner_probe(monkeypatch):
    """Watch one thread's handles and timestamp its durable terminal commit."""

    def _install(thread_id: str) -> _OwnerProbe:
        from core import turn_core
        from subagents import codex_runtime, codex_runtime_events

        probe = _OwnerProbe(thread_id)
        original_sync = turn_core.persist_turn_terminal
        original_commit = turn_core.commit_turn_terminal

        def recording_sync(turn_id, outcome, error=None, **kwargs):
            result = original_sync(turn_id, outcome, error, **kwargs)
            probe.record_commit()
            return result

        async def recording_commit(turn_id, outcome, error=None, **kwargs):
            result = await original_commit(turn_id, outcome, error, **kwargs)
            probe.record_commit()
            return result

        for module in (turn_core, codex_runtime):
            monkeypatch.setattr(module, "persist_turn_terminal", recording_sync)
        for module in (turn_core, codex_runtime, codex_runtime_events):
            monkeypatch.setattr(module, "commit_turn_terminal", recording_commit)
        return probe

    return _install


def _silent(_thread_id: str, _payload: dict) -> None:
    """Broadcasts are not the subject here; ownership ordering is."""


async def _drive(route: str, thread_id: str, live) -> None:
    """Run exactly one Codex terminal route to its durable terminal."""
    from subagents import codex_runtime

    if route in ("natural_result", "turn_failed"):
        from subagents.codex_runtime_events import make_on_event

        on_event = make_on_event(
            live,
            thread_id,
            _silent,
            dead_set=set(),
            thread_registry={thread_id: live.codex_thread_id},
        )
        await on_event(
            {"kind": "result", "is_error": False}
            if route == "natural_result"
            else {"kind": "turn_failed", "cause": "process_exited"}
        )
    elif route == "transport_exit":
        await codex_runtime._finish_transport_exit(
            thread_id, live, live.session, 1, "codex exited", _silent,
        )
    elif route == "interrupted_turn":
        await codex_runtime._finish_interrupted_turn(
            thread_id, live, "stop_requested", _silent,
        )
    elif route == "rejected_input":
        await codex_runtime._finish_rejected_input(
            thread_id, live, live.corsia_turn, "provider rejected", _silent,
        )
    elif route == "unverified_input_delivery":
        await codex_runtime._finish_unverified_input_delivery(
            thread_id, live, live.corsia_turn, _silent,
        )
    elif route == "stop":
        await codex_runtime.stop(
            thread_id, expected_turn_id=live.corsia_turn.turn_id,
        )
    elif route == "retire":
        await codex_runtime.retire(thread_id)
    elif route == "mark_dead":
        await codex_runtime.mark_dead(thread_id)
    elif route == "shutdown_all":
        await codex_runtime.shutdown_all()
    elif route == "hard_preempt":
        await codex_runtime._hard_preempt_live_turn(
            thread_id,
            live,
            successor_turn_id=f"turn-replacement-{thread_id}",
            text="replacement",
            project_id=None,
            model="gpt-5-codex",
            held_lock=codex_runtime._LIVE.lock(thread_id),
        )
    else:  # pragma: no cover - a new route must be added to this test
        raise AssertionError(route)


# Routes that settle real declared-call debt on the way to their terminal keep
# their handle across those awaits; the rest reach the CAS directly.
_ROUTES_WITH_TOOL_DEBT = (
    "transport_exit",
    "interrupted_turn",
    "natural_result",
    "turn_failed",
)
_ROUTES_WITHOUT_TOOL_DEBT = (
    "rejected_input",
    "unverified_input_delivery",
    "stop",
    "retire",
    "mark_dead",
    "shutdown_all",
    "hard_preempt",
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route", [*_ROUTES_WITH_TOOL_DEBT, *_ROUTES_WITHOUT_TOOL_DEBT],
)
async def test_codex_turn_is_owned_until_its_terminal_commits(
    isolated_db, monkeypatch, owner_probe, route,
) -> None:
    """No Codex terminal route makes its own Turn look ownerless."""
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = f"codex-owned-{route}"
    session = _FenceSession(thread_id, request_idle=True)
    if route == "stop":
        session._request_terminate = lambda: session.calls.append("provider_kill")
    monkeypatch.setattr(
        codex_runtime, "_spawn_turn", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        codex_runtime,
        "_retire_registered_thread_artifacts",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr("subagents.codex_shutdown_error.insert_message", lambda *_a, **_k: None)
    if route in _ROUTES_WITH_TOOL_DEBT:
        live, _step_id = _active_live_with_persisted_tool(thread_id, session)
    else:
        live = _active_live(thread_id, session, pending_tool=False)

    probe = owner_probe(thread_id)
    try:
        async with probe:
            await _drive(route, thread_id, live)
    finally:
        _reset_runtime()

    probe.assert_owned_until_terminal(route)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _ROUTES_WITH_TOOL_DEBT)
@pytest.mark.usefixtures("held_wake_drives")
async def test_tool_debt_settlement_happens_inside_the_ownership_window(
    isolated_db, monkeypatch, owner_probe, route,
) -> None:
    """A route that settles debt does it while it still owns the Turn."""
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = f"codex-debt-{route}"
    session = _FenceSession(thread_id, request_idle=True)
    monkeypatch.setattr("subagents.codex_shutdown_error.insert_message", lambda *_a, **_k: None)
    live, step_id = _active_live_with_persisted_tool(thread_id, session)
    settled_while_owned: list[bool] = []
    original_settle = codex_runtime._settle_live_tool_debt

    async def watched_settle(sid, handle, bcast):
        settled_while_owned.append(codex_runtime.has_pending_or_live(sid))
        await original_settle(sid, handle, bcast)

    monkeypatch.setattr(codex_runtime, "_settle_live_tool_debt", watched_settle)

    probe = owner_probe(thread_id)
    try:
        async with probe:
            await _drive(route, thread_id, live)
    finally:
        _reset_runtime()

    assert settled_while_owned == [True], (
        f"{route}: settled this Turn's tool debt without owning the Turn"
    )
    assert probe.committed_at is not None and probe.committed_at >= 1, (
        f"{route}: the probe never saw an await boundary before the terminal, "
        f"so it proves nothing about the window"
    )
    probe.assert_owned_until_terminal(route)
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND role='tool'",
            (thread_id,),
        ).fetchone()
    finally:
        db.close()
    assert row is not None, f"{route}: the declared call never got its result"
    assert step_id


@pytest.mark.asyncio
async def test_the_probe_detects_a_handle_retired_before_its_terminal(
    isolated_db, owner_probe,
) -> None:
    """Control: the instrument fails on exactly the ordering it forbids."""
    from subagents import codex_runtime, codex_runtime_state

    _reset_runtime()
    thread_id = "codex-ownership-control"
    session = _FenceSession(thread_id, request_idle=True)
    live = _active_live(thread_id, session, pending_tool=False)

    probe = owner_probe(thread_id)
    try:
        async with probe:
            codex_runtime_state.finalize_live(thread_id, live)
            codex_runtime._APP_SESSIONS.pop(thread_id, None)
            await asyncio.sleep(0)
            await codex_runtime._claim_live_terminal(live, "cancelled")
    finally:
        _reset_runtime()

    with pytest.raises(AssertionError, match="ownerless|already retired"):
        probe.assert_owned_until_terminal("control")
