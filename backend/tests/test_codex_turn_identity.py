"""Codex live-turn identity + preempt-confirmation regression net.

2026-07-09 concurrent-turn cascades: finalization popped the per-session
live slot by session id alone, so a late terminal from a superseded turn could
deregister a still-streaming successor. The next mid-turn input could then
find no live turn, skip steer, and spawn a parallel turn. Invariant under test:
at most one live turn per thread; a new turn starts only after the previous
turn's terminal is confirmed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infra.turn_volume import CumulativeTurnVolume
from codex_process_fixtures import bind_process_group

from codex_runtime_fixtures import (
    _admit_active_turn,
    _codex_target,
    _make_mock_app_session,
    _reset_runtime,
    _run,
)


def _live_handle(session=None):
    from subagents import codex_runtime

    return codex_runtime._LiveCodex(
        codex_thread_id="t1", session=session, parent_thread_id=None, name="codex",
    )


class TestFinalizeIdentityGuard:
    def setup_method(self):
        _reset_runtime()

    def test_foreign_terminal_does_not_pop_successor(self):
        """A late terminal from a superseded turn must NOT deregister the
        currently-registered turn — the cascade seed."""
        from subagents import codex_runtime, codex_runtime_state

        stale = _live_handle()
        current = _live_handle()
        codex_runtime._LIVE.put("s1", current)
        codex_runtime_state.finalize_live("s1", stale)
        assert codex_runtime._LIVE.get("s1") is current

    def test_own_terminal_pops(self):
        from subagents import codex_runtime, codex_runtime_state

        live = _live_handle()
        codex_runtime._LIVE.put("s1", live)
        codex_runtime_state.finalize_live("s1", live)
        assert "s1" not in codex_runtime._LIVE

    def test_session_teardown_pops_unconditionally(self):
        from subagents import codex_runtime, codex_runtime_state

        codex_runtime._LIVE.put("s1", _live_handle())
        codex_runtime_state.finalize_live("s1")
        assert "s1" not in codex_runtime._LIVE

    def test_on_event_terminal_is_turn_scoped(self):
        """The result-terminal wrapper must finalize its OWN turn only."""
        from subagents import codex_runtime

        stale = _live_handle()
        current = _live_handle()
        with patch("subagents.codex_runtime_events.make_on_event",
                   return_value=AsyncMock()):
            on_event = codex_runtime._make_on_event(
                stale, "s1", lambda sid, m: None)
        codex_runtime._LIVE.put("s1", current)
        _run(on_event({"kind": "result"}))
        assert codex_runtime._LIVE.get("s1") is current

    def test_stale_app_server_terminal_does_not_finalize_successor(self):
        """A late completion for an older provider turn must not touch the
        active successor's persistence buffers or live registration."""
        import asyncio

        from core.turn_core import TurnIdentity
        from subagents import codex_runtime
        from subagents.codex_app_server import CodexAppServerSession

        async def _scenario():
            session = CodexAppServerSession.__new__(CodexAppServerSession)
            session._active_turn_id = "replacement"
            session._completed_turns = {
                "old": asyncio.get_running_loop().create_future(),
                "replacement": asyncio.get_running_loop().create_future(),
            }
            session._usage_by_turn = {}
            session._turn_volume = CumulativeTurnVolume()
            live = _live_handle(session)
            inner = AsyncMock()
            with patch("subagents.codex_runtime_events.make_on_event",
                       return_value=inner):
                session._on_event = codex_runtime._make_on_event(
                    live, "s1", lambda _sid, _msg: None,
                )
            codex_runtime._LIVE.put("s1", live)

            await session._handle_turn_completed(
                {"id": "old", "status": "interrupted"}
            )

            assert codex_runtime._LIVE.get("s1") is live
            assert session._active_turn_id == "replacement"
            assert "replacement" in session._completed_turns
            assert "old" not in session._completed_turns
            inner.assert_not_awaited()

        _run(_scenario())


@pytest.mark.parametrize("method", ["item/started", "item/completed"])
@pytest.mark.parametrize(
    "provider_identity",
    [
        {"threadId": "t1", "turnId": "turn-A"},
        {"threadId": "stale-thread", "turnId": "turn-B"},
        {},
    ],
    ids=["stale-turn", "stale-thread", "missing-identity"],
)
def test_uncorrelated_native_item_notification_cannot_terminate_successor(
    method: str,
    provider_identity: dict[str, str],
) -> None:
    """Late or malformed turn-A item events cannot corrupt active turn B."""
    import asyncio

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime
    from subagents.codex_app_server import CodexAppServerSession

    async def _scenario() -> None:
        session = CodexAppServerSession(
            binary="codex",
            task=None,
            codex_thread_id="t1",
        )
        session._track_active_turn("turn-B")
        session._last_reasoning_summary_item_id = "reasoning-B"
        session._reasoning_summary_parts = {
            "native-action-A": {0: "successor reasoning"},
        }
        session._opaque_reasoning_references = {"native-action-A"}
        live = _live_handle(session)
        live.corsia_turn = TurnIdentity("corsia-turn-B", 2)
        codex_runtime._LIVE.put("s1", live)
        codex_runtime._CODEX_THREADS["s1"] = "t1"
        bcast = MagicMock()
        persist = AsyncMock(return_value="unexpected-marker")
        session.set_callbacks(
            on_event=codex_runtime._make_on_event(live, "s1", bcast),
        )

        params = {
            **provider_identity,
            "item": {
                "id": "native-action-A",
                "type": "commandExecution",
                "command": "redacted by the normalizer",
            },
        }
        with patch(
            "subagents.codex_runtime_events.run_in_db",
            new=persist,
        ):
            await session._handle_notification(method, params)

        assert session.active_turn_id == "turn-B"
        assert codex_runtime._LIVE.get("s1") is live
        assert live.provider_context_untrusted is False
        assert live.tool_boundary_violation_recorded is False
        assert session._last_reasoning_summary_item_id == "reasoning-B"
        assert session._reasoning_summary_parts == {
            "native-action-A": {0: "successor reasoning"},
        }
        assert session._opaque_reasoning_references == {"native-action-A"}
        persist.assert_not_awaited()
        bcast.assert_not_called()

    _run(_scenario())


@pytest.mark.parametrize("method", ["item/started", "item/completed"])
def test_late_turn_started_cannot_rebind_native_action_to_successor(
    method: str,
) -> None:
    """A stale turn-start cannot make turn A terminate CORSIA turn B."""
    import asyncio

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime
    from subagents.codex_app_server import CodexAppServerSession

    async def _scenario() -> None:
        session = CodexAppServerSession(
            binary="codex",
            task=None,
            codex_thread_id="t1",
        )
        session._track_active_turn("turn-B")
        live = _live_handle(session)
        live.corsia_turn = TurnIdentity("corsia-turn-B", 2)
        codex_runtime._LIVE.put("s1", live)
        codex_runtime._CODEX_THREADS["s1"] = "t1"
        bcast = MagicMock()
        persist = AsyncMock(return_value="unexpected-marker")
        session.set_callbacks(
            on_event=codex_runtime._make_on_event(live, "s1", bcast),
        )

        await session._handle_notification(
            "turn/started",
            {"threadId": "t1", "turn": {"id": "turn-A"}},
        )
        with patch(
            "subagents.codex_runtime_events.run_in_db",
            new=persist,
        ):
            await session._handle_notification(method, {
                "threadId": "t1",
                "turnId": "turn-A",
                "item": {
                    "id": "native-action-A",
                    "type": "commandExecution",
                    "command": "redacted by the normalizer",
                },
            })

        assert session.active_turn_id == "turn-B"
        assert codex_runtime._LIVE.get("s1") is live
        assert live.provider_context_untrusted is False
        assert live.tool_boundary_violation_recorded is False
        persist.assert_not_awaited()
        bcast.assert_not_called()

    _run(_scenario())


@pytest.mark.parametrize(
    "provider_identity",
    [
        {"threadId": "t1", "turnId": "turn-A"},
        {"threadId": "stale-thread", "turnId": "turn-B"},
        {},
    ],
    ids=["stale-turn", "stale-thread", "missing-identity"],
)
def test_uncorrelated_native_request_cannot_terminate_successor(
    provider_identity: dict[str, str],
) -> None:
    """Late or malformed native requests are declined without persistence."""
    import asyncio

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime
    from subagents.codex_app_server import CodexAppServerSession

    async def _scenario() -> None:
        session = CodexAppServerSession(
            binary="codex",
            task=None,
            codex_thread_id="t1",
        )
        session._track_active_turn("turn-B")
        live = _live_handle(session)
        live.corsia_turn = TurnIdentity("corsia-turn-B", 2)
        codex_runtime._LIVE.put("s1", live)
        codex_runtime._CODEX_THREADS["s1"] = "t1"
        bcast = MagicMock()
        persist = AsyncMock(return_value="unexpected-marker")
        respond = AsyncMock()
        session._respond = respond
        session.set_callbacks(
            on_event=codex_runtime._make_on_event(live, "s1", bcast),
        )

        with patch(
            "subagents.codex_runtime_events.run_in_db",
            new=persist,
        ):
            await session._handle_server_request({
                "id": 22,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    **provider_identity,
                    "itemId": "native-action-A",
                },
            })

        assert session.active_turn_id == "turn-B"
        assert codex_runtime._LIVE.get("s1") is live
        assert live.provider_context_untrusted is False
        assert live.tool_boundary_violation_recorded is False
        persist.assert_not_awaited()
        bcast.assert_not_called()
        respond.assert_awaited_once_with(
            22,
            result={"decision": "decline"},
            error=None,
        )

    _run(_scenario())


@pytest.mark.parametrize("kind", ["tool_use", "tool_result"])
@pytest.mark.parametrize(
    "provider_identity",
    [
        {"provider_thread_id": "t1", "provider_turn_id": "turn-A"},
        {"provider_thread_id": "stale-thread", "provider_turn_id": "turn-B"},
        {},
    ],
    ids=["stale-turn", "stale-thread", "missing-identity"],
)
def test_uncorrelated_dynamic_lifecycle_cannot_mutate_successor(
    kind: str,
    provider_identity: dict[str, str],
) -> None:
    """Normalized stale lifecycle traffic cannot be rebound to turn B."""
    import asyncio

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime
    from subagents.codex_app_server import CodexAppServerSession

    async def _scenario() -> None:
        session = CodexAppServerSession(
            binary="codex",
            task=None,
            codex_thread_id="t1",
        )
        session._track_active_turn("turn-B")
        live = _live_handle(session)
        live.corsia_turn = TurnIdentity("corsia-turn-B", 2)
        live.tool_names["call-B"] = "read_file"
        live.tool_step_ids["call-B"] = "step-B"
        live.tools_settled.clear()
        codex_runtime._LIVE.put("s1", live)
        codex_runtime._CODEX_THREADS["s1"] = "t1"
        bcast = MagicMock()
        persist = AsyncMock(return_value="unexpected")
        on_event = codex_runtime._make_on_event(live, "s1", bcast)
        event = {
            "kind": kind,
            **provider_identity,
            **(
                {
                    "id": "call-A",
                    "name": "read_file",
                    "input": {"path": "stale"},
                }
                if kind == "tool_use"
                else {
                    "tool_use_id": "call-B",
                    "content": "stale result",
                    "is_error": False,
                }
            ),
        }

        with patch(
            "subagents.codex_runtime_events.run_in_db",
            new=persist,
        ):
            await on_event(event)

        assert live.tool_names == {"call-B": "read_file"}
        assert live.tool_step_ids == {"call-B": "step-B"}
        assert live.tools_settled.is_set() is False
        persist.assert_not_awaited()
        bcast.assert_not_called()

    _run(_scenario())


class TestFeedLivePreemptConfirmation:
    def setup_method(self):
        _reset_runtime()

    def _wire_live(self, session):
        from subagents import codex_runtime

        live = _live_handle(session=session)
        codex_runtime._LIVE.put("s1", live)
        return live

    def test_steer_binds_persisted_input_to_existing_turn(self):
        from core.turn_core import TurnIdentity
        from subagents import codex_runtime

        calls: list[tuple] = []

        async def _bind_input_turn(
            owner_turn_id: str,
            expected_turn_id: str | None = None,
        ) -> None:
            calls.append(("bind", owner_turn_id, expected_turn_id))

        async def _steer(text: str, **_kwargs) -> bool:
            calls.append(("steer", text))
            return True

        session = _make_mock_app_session()
        session.steer = AsyncMock(side_effect=_steer)
        live = self._wire_live(session)
        live.corsia_turn = TurnIdentity("turn-existing", 1)
        bcast = MagicMock()
        assert _run(codex_runtime._feed_live(
            "s1", "more input", broadcast=bcast,
            successor_turn_id="turn-successor",
            bind_input_turn=_bind_input_turn,
        )) == TurnIdentity("turn-existing", 1)
        assert calls == [
            ("bind", "turn-existing", None),
            ("steer", "more input"),
        ]
        session.steer.assert_awaited_once()
        session.interrupt.assert_not_awaited()
        bcast.assert_not_called()

    def test_unsteerable_leaves_the_current_turn_untouched(self):
        """A normal input never interrupts a turn Codex will not steer."""
        from subagents import codex_runtime
        from core.turn_core import TurnIdentity

        calls: list[tuple] = []

        async def _bind_input_turn(
            owner_turn_id: str,
            expected_turn_id: str | None = None,
        ) -> None:
            calls.append(("bind", owner_turn_id, expected_turn_id))

        async def _steer(text: str, **_kwargs) -> bool:
            calls.append(("steer", text))
            return False

        session = _make_mock_app_session()
        session.steer = AsyncMock(side_effect=_steer)

        live = self._wire_live(session)
        live.corsia_turn = TurnIdentity("turn-existing", 1)
        # The deferral is named rather than returned as a silent ``None``: the
        # send seam read that ``None`` as "this thread cannot be continued" and
        # reported a Codex rejection for input Codex never saw.
        with pytest.raises(codex_runtime.CodexLiveTurnBusy):
            _run(codex_runtime._feed_live(
                "s1", "more input",
                successor_turn_id="turn-successor",
                bind_input_turn=_bind_input_turn,
            ))
        assert calls == [
            ("bind", "turn-existing", None),
            ("steer", "more input"),
        ]
        session.interrupt.assert_not_awaited()
        session.start_turn.assert_not_awaited()
        assert codex_runtime._LIVE.get("s1") is live

    def test_live_without_corsia_turn_never_calls_steer(self):
        from subagents import codex_runtime

        session = _make_mock_app_session()
        session.steer = AsyncMock(return_value=True)
        self._wire_live(session)
        bind_input_turn = AsyncMock()

        assert _run(codex_runtime._feed_live(
            "s1", "must fail closed",
            successor_turn_id="turn-successor",
            bind_input_turn=bind_input_turn,
        )) is None
        bind_input_turn.assert_not_awaited()
        session.steer.assert_not_awaited()
        session.interrupt.assert_not_awaited()
        session.start_turn.assert_not_awaited()

    def test_ambiguous_steer_keeps_input_on_existing_turn(self):
        from core.turn_core import TurnIdentity
        from subagents import codex_runtime

        calls: list[tuple] = []

        async def _bind_input_turn(
            owner_turn_id: str,
            expected_turn_id: str | None = None,
        ) -> None:
            calls.append(("bind", owner_turn_id, expected_turn_id))

        async def _ambiguous_steer(text: str, **kwargs) -> bool:
            calls.append(("steer", text))
            kwargs["on_write_observed"]()
            raise ConnectionError("connection lost after write")

        session = _make_mock_app_session()
        session.steer = AsyncMock(side_effect=_ambiguous_steer)
        live = self._wire_live(session)
        live.corsia_turn = TurnIdentity("turn-existing", 1)
        start_live_turn = AsyncMock()

        with patch.object(
            codex_runtime, "_start_live_turn", new=start_live_turn,
        ), pytest.raises(ConnectionError, match="after write"):
            _run(codex_runtime._feed_live(
                "s1", "ambiguous input",
                successor_turn_id="turn-successor",
                bind_input_turn=_bind_input_turn,
                mark_accepted=lambda: calls.append(("accepted",)),
                mark_ambiguous=lambda: calls.append(("ambiguous",)),
            ))

        assert calls == [
            ("bind", "turn-existing", None),
            ("steer", "ambiguous input"),
            ("ambiguous",),
        ]
        assert live.corsia_turn == TurnIdentity("turn-existing", 1)
        assert live.provider_context_untrusted is True
        session.interrupt.assert_not_awaited()
        session.start_turn.assert_not_awaited()
        start_live_turn.assert_not_awaited()

    def test_unsteerable_normal_input_never_hard_preempts(self):
        """Only explicit Stop may preempt; an ordinary send remains queued."""
        from subagents import codex_runtime
        from core.turn_core import TurnIdentity

        session = _make_mock_app_session()
        session.steer = AsyncMock(return_value=False)
        session.interrupt = AsyncMock(return_value=False)
        session.start_turn = AsyncMock(return_value=True)
        session.terminate = AsyncMock()
        live = self._wire_live(session)
        live.corsia_turn = TurnIdentity("turn-old", 1)
        codex_runtime._APP_SESSIONS["s1"] = session
        codex_runtime._CODEX_THREADS["s1"] = "t1"

        with pytest.raises(codex_runtime.CodexLiveTurnBusy):
            _run(codex_runtime.ensure_live_then_feed(
                "s1", "urgent input", allow_fresh=True,
                target=_codex_target(),
                successor_turn_id="turn-successor",
                bind_input_turn=AsyncMock(),
                binary="/custom/codex", effort="xhigh",
            ))

        session.terminate.assert_not_awaited()
        session.interrupt.assert_not_awaited()
        session.start_turn.assert_not_awaited()
        assert codex_runtime._APP_SESSIONS["s1"] is session
        assert codex_runtime._CODEX_THREADS["s1"] == "t1"
        assert codex_runtime._LIVE.get("s1") is live


    def test_unprovable_kill_refuses_input(self):
        """terminate() raising = old runtime not proven dead → refuse the
        input rather than start a turn beside a possibly-live one."""
        from subagents import codex_runtime

        session = _make_mock_app_session()
        session.steer = AsyncMock(return_value=False)
        session.interrupt = AsyncMock(return_value=False)
        session.terminate = AsyncMock(side_effect=RuntimeError("kill failed"))
        session.start_turn = AsyncMock()
        live = self._wire_live(session)
        with patch.object(codex_runtime, "_spawn_turn", new=AsyncMock()) as spawn:
            assert _run(codex_runtime._feed_live(
                "s1",
                "urgent",
                successor_turn_id="turn-successor",
                bind_input_turn=AsyncMock(),
            )) is None
            spawn.assert_not_awaited()
        session.start_turn.assert_not_awaited()
        assert codex_runtime._LIVE.get("s1") is live

    def test_hard_preempt_persists_marker_before_successor(self, main_db):
        """Replacement dispatch observes the interrupted marker first."""
        from subagents import codex_runtime
        from core.turn_core import TurnIdentity

        session = _make_mock_app_session()
        session.terminate = AsyncMock()
        main_db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('s1','steve','codex/gpt-5-codex','t','t')"
        )
        main_db.commit()
        live = self._wire_live(session)
        live.corsia_turn = _admit_active_turn(
            "s1",
            turn_id="turn-s1",
            content="old",
        )
        marker_seen_before_spawn: list[bool] = []

        async def _fake_spawn_turn(*_args, **_kwargs):
            row = main_db.execute(
                "SELECT metadata FROM messages WHERE thread_id = 's1' "
                "AND role = 'system'"
            ).fetchone()
            marker_seen_before_spawn.append(row is not None)
            return TurnIdentity("turn-successor", 2)

        with patch.object(codex_runtime, "get_thread_project_id",
                             return_value=None), \
                patch.object(codex_runtime, "_spawn_turn", _fake_spawn_turn):
            assert _run(codex_runtime._hard_preempt_live_turn(
                "s1", live, text="urgent",
                successor_turn_id="turn-successor",
                project_id=None, model=None,
                held_lock=codex_runtime._LIVE.lock("s1"),
            )) == TurnIdentity("turn-successor", 2)

        assert marker_seen_before_spawn == [True]


class TestStopSerialization:
    def setup_method(self):
        _reset_runtime()

    def test_stop_serializes_with_concurrent_feed(self, main_db):
        """A send arriving while stop awaits its terminal must wait; it may
        start only after stop has removed the predecessor."""
        import asyncio

        from core.turn_core import TurnIdentity
        from subagents import codex_runtime

        main_db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('s1','steve','codex/gpt-5-codex','t','t')"
        )
        main_db.commit()
        identity = _admit_active_turn(
            "s1",
            turn_id="turn-old",
            content="input",
        )

        async def _scenario():
            drain_entered = asyncio.Event()
            release_drain = asyncio.Event()
            events: list[str] = []
            session = _make_mock_app_session()

            def _request_terminate():
                events.append("provider_kill")

            async def _terminate():
                events.append("drain_enter")
                drain_entered.set()
                await release_drain.wait()
                events.append("drain_exit")

            async def _steer(_text):
                events.append("steer_old")
                return True

            async def _spawn(*_args, **_kwargs):
                events.append("spawn_successor")
                return TurnIdentity("turn-successor", 2)

            session._request_terminate = _request_terminate
            session.revoke_dynamic_tool_admission = lambda: None
            session.wait_dynamic_tool_requests_idle = AsyncMock()
            session.terminate = _terminate
            session.steer = _steer
            live = _live_handle(session)
            live.corsia_turn = identity
            codex_runtime._LIVE.put("s1", live)

            with patch.object(codex_runtime, "_codex_connection",
                              return_value={}), \
                    patch.object(codex_runtime, "_restore_thread_id",
                                 new=AsyncMock(return_value=None)), \
                    patch.object(codex_runtime, "_working_dir_for_reconstruction",
                                 return_value="/tmp"), \
                    patch.object(codex_runtime, "_spawn_turn", _spawn):
                stop_task = asyncio.create_task(codex_runtime.stop(
                    "s1", expected_turn_id="turn-old",
                ))
                await drain_entered.wait()
                feed_task = asyncio.create_task(codex_runtime.ensure_live_then_feed(
                    "s1", "next", allow_fresh=True,
                    target=_codex_target(),
                    successor_turn_id="turn-successor",
                    bind_input_turn=AsyncMock(),
                ))
                await asyncio.sleep(0)
                feed_waited = not feed_task.done()
                release_drain.set()
                assert await asyncio.gather(stop_task, feed_task) == [
                    True, TurnIdentity("turn-successor", 2),
                ]

            assert feed_waited is True
            assert events == [
                "provider_kill", "drain_enter", "drain_exit", "spawn_successor"
            ]

        _run(_scenario())

    @pytest.mark.parametrize("stale_by", ["turn_id", "live"])
    def test_stale_stop_cannot_wait_for_locked_successor(self, stale_by):
        import asyncio

        from core.turn_core import TurnIdentity
        from subagents import codex_runtime

        session = _make_mock_app_session()
        session._request_terminate = MagicMock()
        session.interrupt = AsyncMock(return_value=True)
        live = _live_handle(session)
        live.corsia_turn = TurnIdentity("turn-B", 2)
        codex_runtime._LIVE.put("s1", live)

        async def _scenario():
            async with codex_runtime._LIVE.lock("s1"):
                return await asyncio.wait_for(
                    codex_runtime.stop(
                        "s1",
                        expected_turn_id="turn-A" if stale_by == "turn_id" else "turn-B",
                        expected_live=_live_handle() if stale_by == "live" else None,
                    ),
                    timeout=0.1,
                )

        assert _run(_scenario()) is False
        assert codex_runtime._LIVE.get("s1") is live
        session._request_terminate.assert_not_called()
        session.interrupt.assert_not_awaited()


class TestAppServerInterruptConfirmation:
    @pytest.mark.asyncio
    async def test_closing_before_process_spawn_refuses_spawn(self, monkeypatch):
        import asyncio

        from subagents.codex_app_server import CodexAppServerSession

        spawn = AsyncMock()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        session = CodexAppServerSession(binary="codex", task=None)
        session._request_terminate()

        with pytest.raises(RuntimeError, match="terminated before process spawn"):
            await session.start()
        spawn.assert_not_awaited()

    def test_termination_request_kills_process_synchronously(self):
        from subagents.codex_app_server import CodexAppServerSession

        session = CodexAppServerSession(binary="codex", task=None)
        session._closing = False
        session._proc = MagicMock(returncode=None)

        session._request_terminate()

        assert session._closing is True
        session._proc.kill.assert_called_once_with()

    def test_termination_request_accepts_process_already_dead(self):
        from subagents.codex_app_server import CodexAppServerSession

        session = CodexAppServerSession(binary="codex", task=None)
        session._closing = False
        session._proc = MagicMock(returncode=None)
        session._proc.kill.side_effect = ProcessLookupError

        session._request_terminate()

        assert session._closing is True
        session._proc.kill.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_closing_during_process_spawn_kills_before_initialization(
        self,
        monkeypatch,
    ):
        import asyncio

        from subagents import vendor_write_admission
        from subagents.codex_app_server import CodexAppServerSession

        # Spawn ordering is under test, not account binding (proved in test_account_operations_homes).
        monkeypatch.setattr(vendor_write_admission, "prepare_session_launch", lambda *_args: None)
        spawn_entered = asyncio.Event()
        release_spawn = asyncio.Event()
        process = MagicMock(returncode=None)
        process.wait = AsyncMock(return_value=-9)
        bind_process_group(monkeypatch, process)

        async def spawn(*_args, **_kwargs):
            spawn_entered.set()
            await release_spawn.wait()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        session = CodexAppServerSession(binary="codex", task=None)
        session.build_cmd = MagicMock(return_value=["codex"])
        session.build_env = MagicMock(return_value={})
        session._initialize = AsyncMock(side_effect=AssertionError("initialized"))
        session._read_loop = AsyncMock()
        session._drain_stderr = AsyncMock()
        start_task = asyncio.create_task(session.start())
        await asyncio.wait_for(spawn_entered.wait(), timeout=1.0)
        session._request_terminate()
        release_spawn.set()

        with pytest.raises(RuntimeError, match="terminated during process spawn"):
            await asyncio.wait_for(start_task, timeout=1.0)
        process.kill.assert_called_once_with()
        process.wait.assert_awaited_once()
        session._initialize.assert_not_awaited()

    def test_current_generic_error_binds_and_releases_active_turn(self):
        import asyncio

        from subagents.codex_app_server import CodexAppServerSession

        async def _scenario():
            s = CodexAppServerSession(binary="unused", task=None)
            s._active_turn_id = "turn-1"
            waiter = asyncio.get_running_loop().create_future()
            s._completed_turns = {"turn-1": waiter}
            s._usage_by_turn = {"turn-1": {"last": {}}}
            s._turn_volume = CumulativeTurnVolume()
            emitted = []

            async def _on_event(event):
                emitted.append(event)

            s._on_event = _on_event
            await s._handle_notification(
                "error", {
                    "threadId": "t1", "turnId": "turn-1", "willRetry": False,
                    "error": {"message": "provider failed"},
                },
            )

            assert emitted == [{
                "kind": "turn_failed",
                "cause": "process_exited",
                "message": "provider failed",
                # No cumulative counter was reported for this turn, so it
                # carries no volume claim rather than a fabricated zero.
                "token_volume": None,
                "turn_id": "turn-1",
                "is_current_turn": True,
            }]
            assert s._active_turn_id is None
            assert s._completed_turns == {}
            assert s._usage_by_turn == {}
            assert waiter.result()["status"] == "failed"

        _run(_scenario())

    def test_stale_terminal_releases_its_resources_without_replacing_current(self):
        import asyncio

        from subagents.codex_app_server import CodexAppServerSession

        async def _scenario():
            s = CodexAppServerSession.__new__(CodexAppServerSession)
            s._active_turn_id = "new-turn"
            stale = asyncio.get_running_loop().create_future()
            s._completed_turns = {"old-turn": stale}
            s._usage_by_turn = {"old-turn": {"last": {}}}
            s._turn_volume = CumulativeTurnVolume()
            emitted = []

            async def _on_event(event):
                emitted.append(event)

            s._on_event = _on_event
            await s._handle_notification(
                "error", {
                    "threadId": "t1", "turnId": "old-turn", "willRetry": False,
                    "error": {"message": "late failure"},
                },
            )

            assert emitted[0]["turn_id"] == "old-turn"
            assert emitted[0]["is_current_turn"] is False
            assert s._active_turn_id == "new-turn"
            assert s._completed_turns == {}
            assert s._usage_by_turn == {}
            assert stale.result()["status"] == "failed"

            await s._handle_notification(
                "error", {
                    "threadId": "t1", "turnId": "new-turn", "willRetry": True,
                    "error": {"message": "retrying"},
                },
            )
            assert len(emitted) == 1
            assert s._active_turn_id == "new-turn"

        _run(_scenario())

    def test_steer_mismatch_does_not_adopt_provider_reported_turn(self):
        """A provider error cannot replace CORSIA's exact active turn."""
        from subagents.codex_app_server import (
            CodexAppServerError,
            CodexAppServerSession,
        )

        async def _scenario():
            s = CodexAppServerSession(
                binary="codex",
                task=None,
                codex_thread_id="t1",
            )
            s._active_turn_id = "stale-turn"

            async def _req(method, _params=None, **_kwargs):
                assert method == "turn/steer"
                payload = {
                    "code": -32600,
                    "message": (
                        "expected active turn id `stale-turn` but found "
                        "`turn-1`"
                    ),
                }
                raise CodexAppServerError(str(payload), payload)

            s._request = _req
            assert await s.steer("next") is False
            assert s._active_turn_id == "stale-turn"
            assert s._completed_turns == {}

        _run(_scenario())

    def test_interrupt_times_out_returns_false_and_keeps_turn_active(self):
        import asyncio

        from subagents.codex_app_server import CodexAppServerSession

        async def _scenario():
            s = CodexAppServerSession.__new__(CodexAppServerSession)
            s.thread_id = "t1"
            s._active_turn_id = "turn-1"
            s._completed_turns = {
                "turn-1": asyncio.get_running_loop().create_future()}
            s._request = AsyncMock()

            ok = await s.interrupt(timeout=0.05)
            assert ok is False
            assert s._active_turn_id == "turn-1", (
                "an unconfirmed turn must stay active")

        _run(_scenario())

    def test_interrupt_resolves_only_after_terminal_emitted(self):
        """The waiter must resolve AFTER the terminal emit completes —
        resolving early let the successor turn re-register the same handle
        while the old terminal was still in flight (cascade seed)."""
        import asyncio

        from subagents.codex_app_server import CodexAppServerSession

        async def _scenario():
            s = CodexAppServerSession(binary="unused", task=None)
            s.thread_id = "t1"
            s._active_turn_id = "turn-1"
            s._completed_turns = {
                "turn-1": asyncio.get_running_loop().create_future()}
            s._usage_by_turn = {}
            s._turn_volume = CumulativeTurnVolume()
            order = []

            async def _on_event(ev_dict):
                await asyncio.sleep(0.05)  # persistence/finalize latency
                order.append("terminal_emitted")

            s._on_event = _on_event

            async def _req(method, params=None):
                asyncio.get_running_loop().create_task(
                    s._handle_turn_completed(
                        {"id": "turn-1", "status": "interrupted"}))
                return {}

            s._request = _req
            ok = await s.interrupt(timeout=2.0)
            order.append("interrupt_returned")
            assert ok is True
            assert order == ["terminal_emitted", "interrupt_returned"]

        _run(_scenario())

    def test_interrupt_with_no_active_turn_waits_for_pending_terminal(self):
        """The just-completed window: active id cleared, terminal still
        emitting. interrupt() must not return until the terminal is fully
        committed — otherwise the caller hard-preempts a healthy session and
        writes a bogus interrupted marker beside the real terminal."""
        import asyncio

        from subagents.codex_app_server import CodexAppServerSession

        async def _scenario():
            s = CodexAppServerSession.__new__(CodexAppServerSession)
            s.thread_id = "t1"
            s._active_turn_id = None
            fut = asyncio.get_running_loop().create_future()
            s._completed_turns = {"turn-1": fut}

            async def _resolve_later():
                await asyncio.sleep(0.05)
                fut.set_result({"id": "turn-1"})

            asyncio.get_running_loop().create_task(_resolve_later())
            ok = await s.interrupt(timeout=2.0)
            assert ok is True
            assert fut.done(), "interrupt returned before the terminal committed"

        _run(_scenario())

    def test_completed_turn_futures_are_pruned(self):
        """The completion map must not grow for the life of the thread."""
        import asyncio

        from subagents.codex_app_server import CodexAppServerSession

        async def _scenario():
            s = CodexAppServerSession(binary="unused", task=None)
            s.thread_id = "t1"
            s._active_turn_id = "turn-1"
            s._completed_turns = {
                "turn-1": asyncio.get_running_loop().create_future()}
            s._usage_by_turn = {}
            s._turn_volume = CumulativeTurnVolume()
            s._on_event = None
            await s._handle_turn_completed({"id": "turn-1", "status": "ok"})
            assert s._completed_turns == {}, "resolved futures must be pruned"

        _run(_scenario())

    def test_interrupt_with_wedged_pending_terminal_returns_false(self):
        import asyncio

        from subagents.codex_app_server import CodexAppServerSession

        async def _scenario():
            s = CodexAppServerSession.__new__(CodexAppServerSession)
            s.thread_id = "t1"
            s._active_turn_id = None
            s._completed_turns = {
                "turn-1": asyncio.get_running_loop().create_future()}
            ok = await s.interrupt(timeout=0.05)
            assert ok is False

        _run(_scenario())

    def test_interrupt_confirmed_returns_true(self):
        import asyncio

        from subagents.codex_app_server import CodexAppServerSession

        async def _scenario():
            s = CodexAppServerSession.__new__(CodexAppServerSession)
            s.thread_id = "t1"
            s._active_turn_id = "turn-1"
            fut = asyncio.get_running_loop().create_future()
            s._completed_turns = {"turn-1": fut}

            async def _req(method, params=None):
                fut.set_result({"id": "turn-1", "status": "interrupted"})
                return {}

            s._request = _req
            ok = await s.interrupt(timeout=1.0)
            assert ok is True
            assert s._active_turn_id is None

        _run(_scenario())
