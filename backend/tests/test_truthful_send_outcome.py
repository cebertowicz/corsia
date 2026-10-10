"""Provider-busy input must be named explicitly, never returned as rejection."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from codex_runtime_fixtures import _make_mock_app_session, _reset_runtime


def test_declined_steer_defers_explicitly_instead_of_returning_none():
    """``_feed_live`` must name the deferral, not hand back a silent ``None``.

    A ``None`` here is indistinguishable from "this thread cannot be
    continued", which is what produced the false provider rejection.
    """
    import asyncio

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    app_session = _make_mock_app_session()
    app_session.steer = AsyncMock(return_value=False)
    live = codex_runtime._LiveCodex(
        codex_thread_id="t1",
        session=app_session,
        parent_thread_id=None,
        name="codex",
    )
    live.corsia_turn = TurnIdentity("turn-existing", 1)
    codex_runtime._LIVE.put("s1", live)

    async def _bind_input_turn(
        owner_turn_id: str, expected_turn_id: str | None = None,
    ) -> None:
        return None

    with pytest.raises(codex_runtime.CodexLiveTurnBusy):
        asyncio.run(codex_runtime._feed_live(
            "s1",
            "more input",
            successor_turn_id="turn-successor",
            bind_input_turn=_bind_input_turn,
        ))

    assert codex_runtime._LIVE.get("s1") is live
    app_session.interrupt.assert_not_awaited()
    app_session.start_turn.assert_not_awaited()
    _reset_runtime()
