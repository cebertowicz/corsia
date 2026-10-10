from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from core.continuity_accounting import ContinuityAccounting
from cc_runtime_fixtures import (  # noqa: F401
    _seed_parent,
    cc_target,
    child_db,
    feed_main_with_persisted_input,
    prepared_cc_input,
    settle_test_input,
)


@pytest.fixture(autouse=True)
def _clean_live():
    import subagents.cc_runtime as rt
    rt._LIVE.reset()
    yield
    rt._LIVE.reset()


class _StopFeedSession:
    returncode = None

    def __init__(self, fed: asyncio.Event, started=None, release=None):
        self._fed = fed
        self._started = started
        self._release = release

    async def _finish_stop(self):
        if self._started is None or self._release is None:
            return
        self._started.set()
        await self._release.wait()
        self.returncode = 0

    async def close(self):
        await self._finish_stop()

    def _request_terminate(self):
        if self._started is not None:
            self._started.set()

    async def terminate(self):
        await self._finish_stop()

    async def feed(self, _text, *, on_write_observed, on_accepted, input_id=None):
        on_write_observed()
        on_accepted()
        self._fed.set()


def _install_stop_feed_stubs(monkeypatch, thread_id, new_session):
    from subagents import (
        cc_accept,
        cc_feed,
        cc_feed_transition,
        cc_replay_authority,
    )

    snapshot = SimpleNamespace(to_dict=lambda: {"snapshot_id": "snap"})
    monkeypatch.setattr(
        cc_feed,
        "build_cc_user_frame",
        lambda **_kw: SimpleNamespace(
            snapshot_id="snap",
            snapshot=snapshot,
            frame_text="frame",
            turn_text="hello",
            continuity_accounting=ContinuityAccounting.not_delivered(),
        ),
    )
    monkeypatch.setattr(cc_accept, "_transcript_artifact", lambda *_a, **_kw: None)
    monkeypatch.setattr(cc_accept, "_source_artifact", lambda **_kw: {"kind": "source"})
    monkeypatch.setattr(cc_feed, "record_user_frame", lambda *_a, **_kw: None)

    async def resume_child(*_args, **_kwargs):
        from subagents import cc_runtime as rt

        live = rt._Live(
            session=new_session,
            seed_file="/dev/null",
            parent_thread_id=None,
            name="test",
            model="opus",
        )
        rt._LIVE.put(thread_id, live)
        return live

    monkeypatch.setattr(cc_feed_transition, "resume_child", resume_child)
    for module in (cc_replay_authority, cc_feed_transition):
        monkeypatch.setattr(
            module, "require_cc_provider_fit", lambda *_args: None,
        )


def _admit_turn(db, thread_id: str, turn_id: str, *, model: str, text: str):
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso

    db.execute("UPDATE threads SET model=? WHERE id=?", (model, thread_id))
    db.commit()
    admission = admit_input(
        thread_id,
        role="user",
        content=text,
        intent="apply_now",
        message_id=f"{turn_id}-input",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at=utc_now_iso(),
        db=db,
    )
    assert admission.batch is not None
    return admission.batch


async def _no_persist() -> None:
    return None


class TestCCWedgeRegression:
    @pytest.mark.asyncio
    async def test_prewrite_feed_error_publishes_unmeasured_terminal_usage(
        self,
        child_db,
    ):
        from subagents import cc_runtime as rt
        from subagents.cc_process_exit import _CommittedTerminalFailure
        from core.turn_core import drive_turn_settlement

        thread_id = "cc-prewrite-feed-error"
        _seed_parent(child_db, sid=thread_id)

        class RejectingSession:
            reconstructed_transcript_path = None

            async def feed(self, _text, *, on_write_observed, on_accepted, input_id=None):
                raise RuntimeError("provider write rejected")

        snapshot = {
            "snapshot_id": "ctx-prewrite-feed-error",
            "content_hash": "ctx-prewrite-feed-error",
            "delivery_leaves": [{
                "harness": "claude-code",
                "destination_mode": "turn-only",
            }],
            "frozen_prompt_material": {"current_user_content": "hello"},
        }
        reconstruction = SimpleNamespace(
            turn_text="hello",
            snapshot_id=snapshot["snapshot_id"],
            snapshot=SimpleNamespace(to_dict=lambda: snapshot),
            continuity_accounting=ContinuityAccounting.not_delivered(),
        )
        live = rt._Live(
            session=RejectingSession(),
            parent_thread_id=None,
            name="cc",
            model="sonnet",
            context_window=200_000,
            last_call_usage={"input_tokens": 999},
            reasoning_provenance={"source_harness": "claude-code"},
        )
        casts: list[tuple[str, dict]] = []
        batch = _admit_turn(
            child_db,
            thread_id,
            "turn-cc-prewrite-error",
            model="claude-code/sonnet",
            text="hello",
        )

        with pytest.raises(_CommittedTerminalFailure) as failure:
            await rt._accept_reconstructed_input(
                thread_id,
                live,
                reconstruction,
                turn_id="turn-cc-prewrite-error",
                preadmitted_turn=batch.identity,
                bcast=lambda sid, message: casts.append((sid, message)),
            )
        assert isinstance(failure.value.publication_error, RuntimeError)
        assert str(failure.value.publication_error) == "provider write rejected"
        await drive_turn_settlement(failure.value.settlement)

        terminals = [
            message for sid, message in casts
            if sid == thread_id and message.get("type") == "turn_complete"
        ]
        assert len(terminals) == 1
        assert terminals[0]["context_usage"]["snapshot_id"] == snapshot["snapshot_id"]
        assert terminals[0]["context_usage"]["token_usage_available"] is False
        assert "token_source" not in terminals[0]["context_usage"]
        assert terminals[0]["context_usage"]["stale_reason"] == (
            "terminal_without_measurement"
        )
        assert live.last_call_usage is None
        assert live.reasoning_provenance is None

    @pytest.mark.asyncio
    async def test_natural_exit_persists_last_call_occupancy_once(self, child_db):
        import json

        from subagents import cc_runtime as rt
        from subagents.child_thread import persist_assistant_text

        thread_id = "cc-measured-natural-exit"
        _seed_parent(child_db, sid=thread_id)
        batch = _admit_turn(
            child_db,
            thread_id,
            "turn-measured-natural-exit",
            model="claude-code/sonnet",
            text="first",
        )
        settle_test_input(batch)
        identity = batch.identity
        assistant_id = persist_assistant_text(
            thread_id, "partial answer",
            turn_id=identity.turn_id,
            db=child_db,
        )
        live = rt._Live(
            session=SimpleNamespace(
                session_id=thread_id,
                transport_generation_id=(
                    "33333333-3333-4333-8333-333333333333"
                ),
            ),
            parent_thread_id=None,
            name="cc",
            model="sonnet",
            catalog_model_id="claude-code/sonnet",
            runtime_context_epoch=4,
            corsia_turn=identity,
            turn_active=True,
            context_window=200_000,
            context_snapshot_id="ctx-measured-natural-exit",
            context_snapshot={"snapshot_id": "ctx-measured-natural-exit"},
            last_assistant_message_id=assistant_id,
            last_call_usage={
                "input_tokens": 80,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 5,
            },
        )
        casts: list[tuple[str, dict]] = []
        bcast = lambda sid, message: casts.append((sid, message))

        first = await rt._finish_process_exit(
            thread_id, live, 1, "provider exited", bcast, child_db,
        )
        duplicate = await rt._finish_process_exit(
            thread_id, live, 1, "provider exited again", bcast, child_db,
        )

        assert first.value == "applied"
        assert duplicate.value == "already_same_outcome"
        terminals = [
            message for sid, message in casts
            if sid == thread_id and message.get("type") == "turn_complete"
        ]
        assert len(terminals) == 1
        assert terminals[0]["context_message_id"] == assistant_id
        assert terminals[0]["context_usage"]["total_used"] == 105
        assert live.last_call_usage is None
        metadata = json.loads(child_db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND id=?",
            (thread_id, assistant_id),
        ).fetchone()["metadata"])
        assert metadata["context_usage"]["total_used"] == 105
        assert metadata["context_usage"]["harness"] == "claude-code"

    @pytest.mark.asyncio
    async def test_natural_exit_releases_event_lock_with_turn_state_settled(
        self, child_db
    ):
        from subagents import cc_runtime as rt

        thread_id = "cc-natural-exit-lock-boundary"
        _seed_parent(child_db, sid=thread_id)
        batch = _admit_turn(
            child_db,
            thread_id,
            "turn-natural-exit-lock-boundary",
            model="claude-code/sonnet",
            text="first",
        )
        settle_test_input(batch)
        identity = batch.identity
        live = rt._Live(
            session=None,
            parent_thread_id=None,
            name="cc",
            corsia_turn=identity,
            turn_active=True,
            interrupt_requested=True,
            apply_now_interrupt_turn=identity,
        )

        class TrackingLock:
            released_state = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                self.released_state = (
                    live.turn_active,
                    live.interrupt_requested,
                    live.apply_now_interrupt_turn,
                )

        lock = TrackingLock()
        live.event_lock = lock

        result = await rt._finish_process_exit(
            thread_id, live, 1, "provider exited", lambda *_args: None, child_db
        )

        assert result.value == "applied"
        assert lock.released_state == (False, False, None)

    @pytest.mark.asyncio
    async def test_feed_never_writes_to_live_while_stop_terminates(
        self, child_db, monkeypatch
    ):
        import subagents.cc_runtime as rt
        from core.turn_core import BOOT_EPOCH
        from infra import thread_execution
        from infra.thread_driver import admit_input
        from infra.time import utc_now_iso

        sid = "stop-feed-race"
        _seed_parent(child_db, sid=sid)
        stop_started = asyncio.Event()
        stop_release = asyncio.Event()
        old_feed_called = asyncio.Event()
        new_feed_called = asyncio.Event()
        old_session = _StopFeedSession(
            old_feed_called, started=stop_started, release=stop_release
        )
        new_session = _StopFeedSession(new_feed_called)
        old_batch = _admit_turn(
            child_db,
            sid,
            "turn-old",
            model="claude-code/opus",
            text="old input",
        )
        settle_test_input(old_batch)

        rt._LIVE.put(sid, rt._Live(
            session=old_session, seed_file="/dev/null",
            parent_thread_id=None, name="test", model="opus",
            corsia_turn=old_batch.identity,
            turn_active=True,
        ))
        _install_stop_feed_stubs(monkeypatch, sid, new_session)
        settlement_ready = asyncio.get_running_loop().create_future()

        async def capture_settlement(settlement):
            settlement_ready.set_result(settlement)

        monkeypatch.setattr(
            thread_execution, "drive_terminal_settlement", capture_settlement,
        )

        stop_task = asyncio.create_task(rt.stop(
            sid, expected_turn_id="turn-old",
        ))
        await asyncio.wait_for(stop_started.wait(), timeout=1)

        queued = admit_input(
            sid,
            role="user",
            content="hello",
            intent="apply_now",
            message_id="turn-stop-feed-successor-input",
            turn_id="unused-while-active",
            boot_epoch=BOOT_EPOCH,
            admitted_at=utc_now_iso(),
            db=child_db,
        )
        assert queued.batch is None

        async def deliver_successor():
            settlement = await settlement_ready
            batch = settlement.successor
            assert batch is not None
            return await rt.ensure_live_then_feed(
                sid,
                "hello",
                target=cc_target("opus"),
                turn_id=batch.turn_id,
                db=None,
                prepared_context=SimpleNamespace(
                    mode="provider_resume", runtime_context_epoch=0,
                    fit_ledger=SimpleNamespace(rendered_input_tokens=100),
                ),
                preadmitted_turn=batch.identity,
                persist_input_once=_no_persist,
                mark_accepted=lambda: settle_test_input(batch),
            )

        feed_task = asyncio.create_task(deliver_successor())
        await asyncio.sleep(0.05)

        assert not old_feed_called.is_set(), (
            "feed wrote to the exact CC stdin while Stop was terminating it"
        )

        stop_release.set()
        assert await feed_task is not None
        assert await stop_task is True
        assert new_feed_called.is_set()

    @pytest.mark.asyncio
    async def test_context_boundary_stops_pending_cc_runtime(self, monkeypatch):
        from thread.runtime_boundary import retire_harness_continuations
        import subagents.cc_runtime as rt
        import subagents.codex_runtime as codex_rt

        retire_calls: list[str] = []
        monkeypatch.setattr(rt, "is_live", lambda sid: False)
        monkeypatch.setattr(rt, "has_pending_or_live", lambda sid: sid == "pending-cc")

        async def _fake_cc_retire(sid, **kw):
            retire_calls.append(sid)
            return True

        async def _fake_codex_retire(sid):
            return False

        monkeypatch.setattr(rt, "retire", _fake_cc_retire)
        monkeypatch.setattr(codex_rt, "retire", _fake_codex_retire)

        effects = await retire_harness_continuations("pending-cc")

        assert retire_calls == ["pending-cc"]
        assert effects["cc_stopped"] is True

    @pytest.mark.asyncio
    async def test_close_live_pops_live_even_if_close_hangs(self, monkeypatch):
        import asyncio
        import subagents.cc_runtime as rt
        import subagents.cc_handle_close as handle_close

        terminate_called: list = []

        class _HangClose:
            returncode = None

            async def close(self):
                await asyncio.sleep(3600)

            async def terminate(self):
                terminate_called.append(True)

        monkeypatch.setattr(handle_close, "_CLOSE_LIVE_TIMEOUT_S", 0.05)

        sid = "hang-c"
        rt._LIVE.put(sid, rt._Live(
            session=_HangClose(), seed_file="/dev/null",
            parent_thread_id=None, name="test", model=None,
        ))

        events: list = []
        result = await rt._close_live(sid, lambda s, m: events.append(m))

        assert result is True
        assert sid not in rt._LIVE, \
            "zombie remained in _LIVE after close() hung (hanging-close guard)"
        assert terminate_called, \
            "terminate() not called when close() timed out"
        assert any(e.get("type") == "subagent_closed" for e in events), \
            "subagent_closed broadcast not sent despite close() hanging"

    @pytest.mark.asyncio
    async def test_feed_main_zombie_cleared_and_respawned(self, monkeypatch):
        import subagents.cc_runtime as rt
        from subagents import cc_feed_transition

        # feed_main now routes through the single accept path; a dead main is
        # cleared and respawned via --resume (_resume_child), not spawn_main.
        resumed: list = []

        async def _fake_resume(sid, **kw):
            resumed.append(sid)
            return None

        monkeypatch.setattr(cc_feed_transition, "resume_child", _fake_resume)
        monkeypatch.setattr(
            cc_feed_transition, "require_cc_provider_fit", lambda *_args: None,
        )

        class _Dead:
            returncode = 1

            async def feed(self, text, *, on_write_observed, on_accepted, input_id=None):
                on_write_observed()
                on_accepted()

            async def close(self):
                pass

        sid = "wedge-main"
        rt._LIVE.put(sid, rt._Live(
            session=_Dead(), seed_file="/dev/null",
            parent_thread_id=None, name="test", model="opus",
        ))

        await rt.feed_main(
            sid, "edit-resend", target=cc_target("opus"),
            turn_id="turn-wedge-main-resend",
            prepared_context=SimpleNamespace(
                mode="live_handle", runtime_context_epoch=0,
            ),
        )

        assert sid not in rt._LIVE, \
            "zombie was not cleared from _LIVE (Defect B - feed_main)"
        assert resumed == [sid], \
            f"_resume_child (--resume) not called after zombie clear; got {resumed}"

    @pytest.mark.asyncio
    async def test_feed_child_zombie_cleared_and_resumed(self, child_db, monkeypatch):
        import subagents.cc_runtime as rt
        from subagents import cc_feed_transition

        resumed: list = []

        async def _fake_resume(child_id, **kw):
            resumed.append(child_id)
            return None

        monkeypatch.setattr(cc_feed_transition, "resume_child", _fake_resume)
        monkeypatch.setattr(
            cc_feed_transition, "require_cc_provider_fit", lambda *_args: None,
        )

        class _Dead:
            returncode = 2

            async def feed(self, text, *, on_write_observed, on_accepted, input_id=None):
                on_write_observed()
                on_accepted()

            async def close(self):
                pass

        sid = "wedge-child"
        child_db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, spawned_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (sid, "steve", "claude-code/sonnet", "parent", "t", "t"),
        )
        child_db.commit()

        rt._LIVE.put(sid, rt._Live(
            session=_Dead(), seed_file="/dev/null",
            parent_thread_id=None, name="test", model=None,
        ))

        result = await rt.feed(
            sid, "steer", target=cc_target("sonnet"),
            turn_id="turn-wedge-child-resume",
            prepared_context=SimpleNamespace(
                mode="live_handle", runtime_context_epoch=0,
            ),
        )

        assert sid not in rt._LIVE, \
            "zombie was not cleared from _LIVE (Defect B - feed)"
        assert resumed == [sid], \
            f"_resume_child not called after zombie clear; got {resumed}"
        assert result is None

    @pytest.mark.asyncio
    async def test_model_switch_retires_codex_continuation(self, monkeypatch):
        from thread.model_runtime import post_model_switch_effects
        import subagents.codex_runtime as codex_rt

        retire_calls: list[str] = []
        source_owner = object()

        async def _fake_retire(sid, **_kwargs):
            retire_calls.append(sid)
            return True

        monkeypatch.setattr(codex_rt, "retire_if_current", _fake_retire)
        monkeypatch.setattr(
            "thread.model_runtime.emit_product_event", lambda *a, **kw: None)

        await post_model_switch_effects(
            "codex-session",
            new_model="anthropic/claude-3-5-sonnet",
            user_id="u1",
            source_harness="codex",
            source_runtime_owner=source_owner,
        )

        assert retire_calls == ["codex-session"], \
            "model switch must retire Codex continuation state too"

    @pytest.mark.asyncio
    async def test_cc_stream_feed_raises_on_dead_process(self):
        from subagents.cc_stream import CCStreamSession

        sess = CCStreamSession.__new__(CCStreamSession)

        class _DeadProc:
            returncode = 1
            stdin = object()

        sess._proc = _DeadProc()

        with pytest.raises(RuntimeError, match="feed to dead CC process"):
            await sess.feed("should raise")

    def test_cc_stream_session_returncode_property_alive(self):
        from subagents.cc_stream import CCStreamSession

        sess = CCStreamSession.__new__(CCStreamSession)

        class _AliveProc:
            returncode = None

        sess._proc = _AliveProc()
        assert sess.returncode is None, "_proc_is_alive: alive process must yield None"

        class _DeadProc:
            returncode = 1

        sess._proc = _DeadProc()
        assert sess.returncode == 1, "_proc_is_alive: dead process must yield exit code"

    @pytest.mark.asyncio
    async def test_cc_zombie_edit_resend_not_wedge(
        self, main_db, fake_claude, tmp_path, monkeypatch
    ):
        import subagents.cc_runtime as rt
        from tests.conftest import _wait

        claude_dir = str(tmp_path / "c")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", claude_dir)
        casts: list = []
        bcast = lambda s, m: casts.append((s, m))  # noqa: E731

        first_turn_id = "turn-wedge-first"
        target = cc_target("opus")
        await feed_main_with_persisted_input(
            "main-1", "first turn",
            binary=fake_claude, target=target,
            turn_id=first_turn_id,
            claude_dir=claude_dir,
            broadcast=bcast,
        )
        assert await _wait(
            lambda: any(m["type"] == "turn_complete" for _, m in casts)
        ), "first turn did not complete"

        await rt.retire("main-1")
        assert not rt.is_live("main-1")

        seed = str(tmp_path / "seed.txt")
        with open(seed, "w") as f:
            f.write("seed")

        class _Dead:
            returncode = 1

            async def feed(self, text, *, on_write_observed, on_accepted, input_id=None):
                on_write_observed()
                on_accepted()

            async def close(self):
                pass

        rt._LIVE.put("main-1", rt._Live(
            session=_Dead(), seed_file=seed,
            parent_thread_id=None, name="conductor", model="opus",
        ))

        casts.clear()
        resend_turn_id = "turn-wedge-resend"
        await feed_main_with_persisted_input(
            "main-1", "edit-resend after model switch",
            binary=fake_claude, target=target,
            turn_id=resend_turn_id,
            claude_dir=claude_dir,
            broadcast=bcast,
        )

        assert await _wait(
            lambda: any(m["type"] == "turn_complete" for _, m in casts)
        ), "edit-resend was silently dropped"
