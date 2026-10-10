from __future__ import annotations

import asyncio
import json
import stat
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest

from core.continuity_accounting import ContinuityAccounting
from tests.registry_helpers import artifact_rows_for_thread

from cc_runtime_fixtures import (  # noqa: F401
    _done_count,
    _seed_parent,
    _wait,
    cc_target,
    child_db,
    ensure_live_with_persisted_input,
    fake_claude,
    fake_claude_home,
    fake_claude_oneshot,
    feed_with_persisted_input,
    persist_bridge_tool_result,
    prepared_cc_input,
    settle_test_input,
)


def _broadcast_insert_stub(thread_id, message_id, *, db, broadcast):
    broadcast(thread_id, {
        "type": "message_added",
        "message": {"id": message_id},
    })


async def _spawn(parent_id: str, task: str | None, *, model: str, **kwargs):
    import subagents.cc_runtime as rt

    return await rt.spawn(
        parent_id, task, target=cc_target(model), **kwargs,
    )


async def _feed(
    thread_id: str, text: str, *, turn_id: str, model: str = "opus", **kwargs,
):
    target = cc_target(model)
    return await feed_with_persisted_input(
        thread_id, text, target=target, turn_id=turn_id, **kwargs,
    )


async def _ensure_feed(
    thread_id: str, text: str, *, turn_id: str, model: str = "opus", **kwargs,
):
    target = cc_target(model)
    return await ensure_live_with_persisted_input(
        thread_id, text, target=target, turn_id=turn_id, **kwargs,
    )


class TestSpawn:
    @pytest.mark.asyncio
    async def test_spawn_without_task_raises(self, child_db, fake_claude):
        import subagents.cc_runtime as rt
        _seed_parent(child_db)
        with pytest.raises(ValueError):
            await _spawn("parent", None, binary=fake_claude, model="opus")

    @pytest.mark.asyncio
    async def test_mixed_tool_batch_persists_and_finalizes_one_assistant_step(
        self, child_db,
    ):
        import json

        from core.turn_core import TurnIdentity
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        _seed_parent(child_db, sid="cc-step")
        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
            corsia_turn=TurnIdentity("turn-cc-step", 1),
        )
        live.turn_active = True
        casts: list[tuple[str, dict]] = []
        on_event = _make_on_event(
            live, "cc-step", lambda sid, message: casts.append((sid, message)),
            child_db,
        )

        await on_event({
            "kind": "assistant_step",
            "text": "Inspecting both files.",
            "reasoning": "Need both.",
            "tool_uses": [
                {
                    "id": "read-a",
                    "name": "mcp__corsia__read_file",
                    "input": {"file_path": "/a"},
                },
                {
                    "id": "read-b",
                    "name": "mcp__corsia__read_file",
                    "input": {"file_path": "/b"},
                },
            ],
        })
        persist_bridge_tool_result(
            child_db, live, thread_id="cc-step",
            provider_tool_call_id="read-a", content="A",
        )
        await on_event({
            "kind": "tool_result", "tool_use_id": "read-a", "content": "A",
        })
        persist_bridge_tool_result(
            child_db, live, thread_id="cc-step",
            provider_tool_call_id="read-b", content="B",
        )
        await on_event({
            "kind": "tool_result", "tool_use_id": "read-b", "content": "B",
        })

        rows = [dict(row) for row in child_db.execute(
            "SELECT id, role, content, metadata, created_at FROM messages "
            "WHERE thread_id = ? ORDER BY rowid",
            ("cc-step",),
        ).fetchall()]
        assert [row["role"] for row in rows] == ["assistant", "tool", "tool"]
        step_id = rows[0]["id"]
        step_meta = json.loads(rows[0]["metadata"])
        assert rows[0]["content"] == "Inspecting both files."
        assert [call["id"] for call in step_meta["llm_tool_calls"]] == [
            "read-a", "read-b",
        ]
        assert all(
            json.loads(row["metadata"])["assistant_step_id"] == step_id
            for row in rows[1:]
        )
        final = next(
            message for _, message in reversed(casts)
            if message.get("type") == "step_text"
        )
        assert final["message_id"] == step_id
        assert final["created_at"] == rows[0]["created_at"]
        assert final["finalized_tool_ids"] == ["read-a", "read-b"]
        assert [call["result"] for call in final["metadata"]["tool_calls"]] == [
            "A", "B",
        ]
        assert live.tool_step_ids == {}

    @pytest.mark.asyncio
    async def test_step_display_reads_run_off_loop_for_steps_and_settlement(
        self, child_db, monkeypatch,
    ):
        """CC display reads use the DB pool when no connection is owned."""
        import threading

        import subagents.cc_event_steps as event_steps
        import subagents.cc_event_tool_settlement as settlement
        from core.turn_core import TurnIdentity
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _LIVE, _Live

        thread_id = "cc-display-off-loop"
        _seed_parent(child_db, sid=thread_id)
        live = _Live(
            session=None, parent_thread_id=None, name="cc",
            corsia_turn=TurnIdentity("turn-display-off-loop", 1),
        )
        live.turn_active = True
        seen_threads: list[int] = []
        original = event_steps.load_assistant_step_display

        def record_display(*args, **kwargs):
            seen_threads.append(threading.get_ident())
            return original(*args, **kwargs)

        monkeypatch.setattr(event_steps, "load_assistant_step_display", record_display)
        monkeypatch.setattr(settlement, "load_assistant_step_display", record_display)
        on_event = _make_on_event(live, thread_id, lambda *_args: None, None)
        _LIVE.put(thread_id, live)
        try:
            await on_event({
                "kind": "assistant_step", "text": "", "reasoning": "",
                "tool_uses": [{
                    "id": "call-1", "name": "mcp__corsia__read_file", "input": {},
                }],
            })
            await settlement.settle_missing_tool_results(
                live=live, child_id=thread_id, identity=live.corsia_turn,
                bcast=lambda *_args: None, db=None,
            )
            await on_event({
                "kind": "tool_result", "tool_use_id": "call-1",
                "content": "delayed provider echo",
            })
        finally:
            _LIVE.pop(thread_id, None)

        assert seen_threads
        assert all(thread != threading.get_ident() for thread in seen_threads)

class TestArtifactRegistration:

    @pytest.mark.asyncio
    async def test_fresh_spawn_registers_child_id(self, child_db, fake_claude):
        import subagents.cc_runtime as rt
        _seed_parent(child_db)
        child_id = await _spawn("parent", "task", binary=fake_claude,
                                  model="sonnet",
                                  broadcast=lambda s, m: None)
        row = next(
            (
                row for row in artifact_rows_for_thread(child_db, child_id)
                if row["integration"] == "claude-code" and row["type"] == "session"
            ),
            None,
        )
        assert row is not None
        assert row["external_id"] == child_id  # child id IS the --session-id
        await rt.retire(child_id)

    @pytest.mark.asyncio
    async def test_dead_child_resume_uses_registered_id(
        self, child_db, fake_claude, fake_claude_home, monkeypatch
    ):
        import subagents.cc_runtime as rt
        import connections.models as cm
        # Closing the child emits a parent wake. This registry/resume test
        # must not launch parent inference beyond its fake child transport.
        monkeypatch.setattr("infra.thread_execution.drive_thread", AsyncMock())
        monkeypatch.setattr(cm, "get_connection",
                            lambda provider, db=None: {"config": {"binary": fake_claude}})
        # rt.feed() (used below for the resume) has no claude_dir passthrough;
        # it only reaches subagents.cc_sessions.resolve_claude_dir() via
        # CLAUDE_CONFIG_DIR. Isolate it there too, or the resume install
        # falls back to the developer's real ~/.claude.
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", fake_claude_home)
        _seed_parent(child_db)
        child_id = await _spawn(
            "parent", "task", binary=fake_claude,
            model="opus",
            claude_dir=fake_claude_home, broadcast=lambda s, m: None)
        assert await rt._close_live(child_id) is True
        assert not rt.is_live(child_id)
        # Resume needs the transcript in the root the launch resumes from.
        transcript = Path(fake_claude_home) / "projects" / "-child" / f"{child_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("{}\n")

        assert await _ensure_feed(
            child_id, "carry on", turn_id="turn-resume-registered",
        ) is not None
        # The registry row minted at spawn — not a re-derivation — is the
        # provider identity the resumed process is launched under.
        assert rt._LIVE.get(child_id).session.resume_session_id == child_id
        await rt.retire(child_id)


class TestSelfExit:
    @pytest.mark.asyncio
    async def test_self_exit_finalizes(
        self, child_db, fake_claude_oneshot, monkeypatch,
    ):
        import subagents.cc_runtime as rt

        # Self-exit commits a successor wake asynchronously. This test owns the
        # child-process lifecycle only, so do not let that separate parent-turn
        # driver outlive the temporary database during fixture teardown.
        monkeypatch.setattr("infra.thread_execution.drive_thread", AsyncMock())
        _seed_parent(child_db)
        casts: list[tuple[str, dict]] = []
        child_id = await _spawn(
            "parent", "do it once", binary=fake_claude_oneshot,
            model="sonnet",
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        ok = await _wait(lambda: any(sid == child_id and m.get("type") == "subagent_closed"
                                     for sid, m in casts))
        assert ok, "no subagent_closed broadcast on self-exit"
        assert any(sid == "parent" and m.get("type") == "subagent_status"
                   and m.get("status") == "closed" for sid, m in casts)
        assert not rt.is_live(child_id)


class TestContextUsage:
    def test_accepting_frame_does_not_write_retired_tail_state(self, monkeypatch):
        """Provider acceptance has no second context-state database write."""
        from subagents.cc_accept import _DeliveryObservation

        def unexpected_db_open():
            raise AssertionError("accepted CC frame opened a database")

        monkeypatch.setattr("infra.database.get_db", unexpected_db_open)
        accepted: list[bool] = []
        observation = _DeliveryObservation(
            mark_accepted=lambda: accepted.append(True),
        )

        observation.accept()

        assert accepted == [True]

    def test_user_frame_preflight_keeps_source_truth_live(
        self,
        child_db,
    ):
        from core.turn_core import TurnIdentity
        from subagents.cc_accept import _prepare_live_turn
        from subagents.cc_runtime_state import _Live

        _seed_parent(child_db, sid="cc-live-context")
        snapshot = {
            "snapshot_id": "ctx-cc-live-context",
            "content_hash": "a" * 64,
        }
        live = _Live(
            session=SimpleNamespace(reconstructed_transcript_path=None),
            parent_thread_id=None,
            name="cc",
            corsia_turn=TurnIdentity("turn-cc-live-context", 1),
        )
        frame = SimpleNamespace(
            snapshot_id=snapshot["snapshot_id"],
            snapshot=SimpleNamespace(to_dict=lambda: snapshot),
            turn_text="hello",
            continuity_accounting=ContinuityAccounting.not_delivered(),
        )

        _prepare_live_turn(live, frame)

        assert live.context_snapshot == snapshot
        assert live.source_artifacts[0]["payload"]["frame_text"] == "hello"

    def test_cold_seed_keeps_source_truth_in_launch_context(
        self,
        child_db,
        monkeypatch,
    ):
        from subagents import cc_startup_context
        from subagents.cc_startup_context import (
            CCStartupContext,
            record_cc_startup_context,
        )

        _seed_parent(child_db, sid="cc-seed-context")
        monkeypatch.setattr(
            cc_startup_context,
            "_write_seed_file",
            lambda *_: "/tmp/cc-seed-context.jsonl",
        )
        snapshot = {
            "snapshot_id": "ctx-cc-seed-context",
            "content_hash": "b" * 64,
        }
        reconstruction = SimpleNamespace(
            seed_text="seed",
            persisted_seed_text="seed",
            snapshot_id=snapshot["snapshot_id"],
            snapshot=SimpleNamespace(to_dict=lambda: snapshot),
            installed="delivery",
        )
        retained = []
        monkeypatch.setattr(
            cc_startup_context, "persist_installed_delivery",
            lambda delivery, *, mode, db: retained.append((delivery, mode)),
        )
        context = CCStartupContext(
            row=None,
            is_subagent=True,
            model_alias="opus",
            target_context_window=1_000_000,
            expected_reported_model=None,
            working_dir="/tmp",
            cc_session_id="cc-seed-context",
            prior_cc_session_id=None,
            resume_session_id=None,
            reconstructed_transcript=None,
            reconstruction=reconstruction,
            tool_registry=None,
            tool_definitions=(),
            synthetic_resume=False,
            launch_identity=None,
        )

        _, artifacts = record_cc_startup_context(
            "cc-seed-context",
            context,
            db=child_db,
        )

        assert artifacts[0]["snapshot_id"] == snapshot["snapshot_id"]
        # Every process launch reads the seed, so it retains what it delivers.
        assert retained == [("delivery", "launch")]

    def test_result_marker_metadata_keeps_exact_turn_owner(self):
        from core.turn_core import TurnIdentity
        from subagents.cc_event_result import _result_metadata

        identity = TurnIdentity("turn-cc-marker", 7)
        ctx = SimpleNamespace(live=SimpleNamespace(interrupt_requested=False))

        _, metadata = _result_metadata(
            ctx,
            {},
            identity=identity,
            apply_now_interrupt_owns_result=False,
        )

        assert metadata["turn_id"] == "turn-cc-marker"
        assert "cause" not in metadata

    def test_result_marker_classifies_expired_login(self):
        from core.turn_core import TurnIdentity
        from subagents.cc_event_result import _result_metadata

        ctx = SimpleNamespace(live=SimpleNamespace(interrupt_requested=False))
        _, metadata = _result_metadata(
            ctx,
            {
                "is_error": True,
                "result": (
                    "Failed to authenticate: OAuth session expired and could not be refreshed"
                ),
            },
            identity=TurnIdentity("turn-cc-expired-login", 7),
            apply_now_interrupt_owns_result=False,
        )

        assert metadata["cause"] == "sign_in_expired"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("is_error", "assistant_row", "expected_quiet"),
        [(True, False, True), (True, True, False), (False, False, False)],
    )
    async def test_result_forwards_quiet_usage_only_for_an_error_without_output(
        self, monkeypatch, is_error, assistant_row, expected_quiet,
    ):
        from core.turn_core import TurnIdentity
        from subagents import cc_event_result
        from subagents.cc_event_context import CCEventContext
        from subagents.cc_runtime_state import _Live

        identity = TurnIdentity("turn-cc-quiet-usage", 1)
        live = _Live(session=None, parent_thread_id=None, name="cc", corsia_turn=identity)
        live.turn_active = True
        live.last_assistant_message_id = "assistant-row" if assistant_row else None
        finish = AsyncMock()
        monkeypatch.setattr(cc_event_result, "_finish_result_postprocessing", finish)
        monkeypatch.setattr(cc_event_result, "_insert_result_marker", AsyncMock(return_value=("marker", "now")))
        monkeypatch.setattr(cc_event_result, "_announce_result_marker", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(cc_event_result, "_record_plan_error", AsyncMock())
        monkeypatch.setattr(cc_event_result, "_record_plan_completion", AsyncMock())
        monkeypatch.setattr(cc_event_result, "_schedule_sparse_usage_control_read", lambda _ctx: None)

        await cc_event_result.on_result(CCEventContext(live, "cc-thread", lambda *_args: None, None), {
            "_corsia_turn_identity": identity,
            "is_error": is_error,
            "result": "provider failed",
        })

        assert finish.await_args.kwargs["quiet_if_unmeasured"] is expected_quiet


    @pytest.fixture(autouse=True)
    def _applied_terminal_owner(self, monkeypatch):
        """These adapter-effect units run behind an already-proved terminal CAS."""
        from core.turn_core import TurnTerminalResult
        from infra.thread_driver import TerminalSettlement
        from subagents import cc_event_result, cc_runtime_events

        original = cc_runtime_events._make_on_event

        def _owned_event(live, child_id, bcast, db):
            from core.turn_core import TurnIdentity

            live.corsia_turn = live.corsia_turn or TurnIdentity(
                f"test-{child_id}", 1,
            )
            return original(live, child_id, bcast, db)

        monkeypatch.setattr(cc_runtime_events, "_make_on_event", _owned_event)
        # Only the result frame's claim: a real process exit still commits.
        monkeypatch.setattr(
            cc_event_result,
            "claim_terminal",
            AsyncMock(
                return_value=TerminalSettlement(TurnTerminalResult.APPLIED, None)
            ),
        )

    def test_trusted_occupancy_handle_requires_live_exact_process(self):
        from subagents import cc_runtime
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=SimpleNamespace(
                session_id="provider-session",
                resume_session_id=None,
                returncode=None,
                pid=31337,
                transport_generation_id=(
                    "11111111-1111-4111-8111-111111111111"
                ),
            ),
            parent_thread_id=None,
            name="cc",
            model="opus",
            catalog_model_id="claude-code/opus",
            runtime_context_epoch=4,
        )
        cc_runtime._LIVE.reset()
        try:
            cc_runtime._LIVE.put("occupancy-session", live)

            fingerprint = cc_runtime.trusted_handle_fingerprint(
                "occupancy-session",
            )

            assert fingerprint is not None
            assert fingerprint.provider_session_id == "provider-session"
            assert not hasattr(fingerprint, "guard_identity")
            assert not hasattr(fingerprint, "stream_identity")
            assert fingerprint.catalog_model_id == "claude-code/opus"
            assert fingerprint.runtime_context_epoch == 4

            live.provider_context_untrusted = True
            assert (
                cc_runtime.trusted_handle_fingerprint("occupancy-session")
                is None
            )
        finally:
            cc_runtime._LIVE.reset()

    def test_trusted_occupancy_handle_uses_durable_provider_session(self):
        from subagents import cc_runtime
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=SimpleNamespace(
                session_id="provider-session",
                resume_session_id=None,
                returncode=None,
                pid=31337,
            ),
            parent_thread_id=None,
            name="cc",
            model="opus",
            catalog_model_id="claude-code/opus",
            runtime_context_epoch=4,
        )
        cc_runtime._LIVE.reset()
        try:
            cc_runtime._LIVE.put("occupancy-session", live)
            fingerprint = cc_runtime.trusted_handle_fingerprint(
                "occupancy-session"
            )
            assert fingerprint is not None
            assert fingerprint.provider_session_id == "provider-session"
        finally:
            cc_runtime._LIVE.reset()

    def test_provider_occupancy_fingerprint_tracks_context_not_transport(self):
        from infra.context_receipts import producer_handle_fingerprint
        from subagents import cc_runtime
        from subagents.cc_runtime_state import _Live

        def live(token: str, snapshot_id: str = "ctx-a"):
            return _Live(
                session=SimpleNamespace(
                    session_id="provider-session",
                    resume_session_id=None,
                    returncode=None,
                    pid=31337,
                    transport_generation_id=token,
                ),
                parent_thread_id=None,
                name="cc",
                model="opus",
                catalog_model_id="claude-code/opus",
                runtime_context_epoch=4,
                context_snapshot_id=snapshot_id,
            )

        cc_runtime._LIVE.reset()
        try:
            cc_runtime._LIVE.put(
                "occupancy-session",
                live("11111111-1111-4111-8111-111111111111"),
            )
            original = cc_runtime.trusted_handle_fingerprint(
                "occupancy-session",
            )
            cc_runtime._LIVE.put(
                "occupancy-session",
                live("22222222-2222-4222-8222-222222222222"),
            )
            replacement = cc_runtime.trusted_handle_fingerprint(
                "occupancy-session",
            )

            assert original is not None
            assert replacement is not None
            assert replacement.context_snapshot_id == "ctx-a"
            assert producer_handle_fingerprint(original) == (
                producer_handle_fingerprint(replacement)
            )

            changed_context = live(
                "33333333-3333-4333-8333-333333333333",
                "ctx-b",
            )
            cc_runtime._LIVE.put("occupancy-session", changed_context)
            other = cc_runtime.trusted_handle_fingerprint("occupancy-session")
            assert other is not None
            assert producer_handle_fingerprint(other) != (
                producer_handle_fingerprint(replacement)
            )

            changed = live("33333333-3333-4333-8333-333333333333")
            changed.session.session_id = "different-provider-session"
            cc_runtime._LIVE.put("occupancy-session", changed)
            other = cc_runtime.trusted_handle_fingerprint("occupancy-session")
            assert other is not None
            assert producer_handle_fingerprint(other) != (
                producer_handle_fingerprint(replacement)
            )
        finally:
            cc_runtime._LIVE.reset()

    def test_excludes_output_and_reports_exactly_full_window(self):
        from subagents.cc_event_context_usage import context_usage_from_final_call
        cu = context_usage_from_final_call(
            {
                "input_tokens": 150_000,
                "cache_read_input_tokens": 40_000,
                "cache_creation_input_tokens": 10_000,
                "output_tokens": 100_000,
            },
            selected_context_window=200_000,
        )
        assert cu["total_used"] == 200_000
        assert cu["model_context_window"] == 200_000
        assert cu["utilization"] == 1.0

    def test_reports_actual_usage_above_selected_window(self):
        from subagents.cc_event_context_usage import context_usage_from_final_call
        cu = context_usage_from_final_call(
            {"input_tokens": 2_000_000}, selected_context_window=200_000,
        )
        assert cu["total_used"] == 2_000_000
        assert cu["model_context_window"] == 200_000
        assert cu["utilization"] == 10.0

    def test_unreported_window_uses_exact_selected_target_evidence(self):
        from subagents.cc_event_context_usage import context_usage_from_final_call
        cu = context_usage_from_final_call(
            {"input_tokens": 500_000},
            selected_context_window=967_000,
        )
        assert cu["model_context_window"] == 967_000
        assert cu["utilization"] == pytest.approx(500_000 / 967_000)

    def test_no_usage_returns_none(self):
        from subagents.cc_event_context_usage import context_usage_from_final_call
        assert context_usage_from_final_call(None) is None

    @pytest.mark.parametrize(
        "usage",
        [
            {},
            {"input_tokens": "12"},
            {"input_tokens": True},
            {"input_tokens": -1},
            {"input_tokens": float("inf")},
            {"input_tokens": float("nan")},
            {"input_tokens": 12.5},
            {"input_tokens": 12, "cache_read_input_tokens": "3"},
            {"input_tokens": 12, "cache_creation_input_tokens": -1},
        ],
    )
    def test_malformed_final_call_usage_is_unavailable(self, usage):
        from subagents.cc_event_context_usage import context_usage_from_final_call

        assert context_usage_from_final_call(usage) is None

    def test_selected_window_is_the_only_capacity_evidence(self):
        from subagents.cc_event_context_usage import context_usage_from_final_call
        cu = context_usage_from_final_call(
            {"input_tokens": 500_000},
            selected_context_window=967_000,
        )
        assert cu["model_context_window"] == 967_000
        assert cu["utilization"] == pytest.approx(500_000 / 967_000)

    def test_selected_window_does_not_rewrite_catalog(self, main_db):
        from subagents.cc_event_context_usage import context_usage_from_final_call

        cu = context_usage_from_final_call(
            {"input_tokens": 100_000},
            selected_context_window=1_000_000,
        )
        assert cu["model_context_window"] == 1_000_000
        row = main_db.execute(
            "SELECT context_window,metadata_json FROM models "
            "WHERE id='claude-code/opus'"
        ).fetchone()
        assert row["context_window"] == 1_000_000
        assert "context_observation" not in json.loads(row["metadata_json"])

    def test_unknown_window_reports_null_utilization_not_zero(self):
        from subagents.cc_event_context_usage import context_usage_from_final_call
        cu = context_usage_from_final_call(
            {"input_tokens": 1000},
        )
        assert cu["model_context_window"] == 0
        assert cu["utilization"] is None
        assert cu["provider_reported_utilization"] is None

    @pytest.mark.asyncio
    async def test_final_call_usage_updates_widget_with_exact_handle_identity(
        self,
        monkeypatch,
    ):
        from core.turn_core import TurnIdentity
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=SimpleNamespace(
                transport_generation_id="11111111-1111-4111-8111-111111111111",
                session_id="provider-session",
                resume_session_id=None,
                pid=42,
            ),
            parent_thread_id=None,
            name="cc",
            model="opus",
            catalog_model_id="claude-code/opus",
            runtime_context_epoch=3,
            context_window=1_000_000,
            corsia_turn=TurnIdentity("turn-cc-usage", 4),
        )
        live.turn_active = True
        live.context_snapshot = {
            "snapshot_id": "ctx-cc-live",
            "source_inventory": [{"source_id": "cc-system", "label": "System"}],
            "frozen_prompt_material": "must-not-ship",
        }
        live.context_snapshot_id = "ctx-cc-live"
        casts: list[tuple[str, dict]] = []
        on_event = _make_on_event(
            live, "cc-sid", lambda *args: casts.append(args), db=None,
        )

        await on_event({
            "kind": "final_call_usage",
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 37_853,
                "cache_read_input_tokens": 0,
            },
        })

        assert len(casts) == 1
        thread_id, event = casts[0]
        assert thread_id == "cc-sid"
        assert event["type"] == "context_usage_updated"
        assert event["turn_id"] == "turn-cc-usage"
        assert event["turn_order"] == 4
        assert event["context_usage"]["total_used"] == 37_855
        assert "harness" not in event["context_usage"]
        assert "catalog_model_id" not in event["context_usage"]
        assert "producer_handle_fingerprint" not in event["context_usage"]
        assert event["context_usage"]["context_snapshot"] == {
            "snapshot_id": "ctx-cc-live",
            "source_inventory": [{"source_id": "cc-system", "label": "System"}],
        }

    @pytest.mark.asyncio
    async def test_latest_final_call_usage_replaces_prior_calls_and_aggregate(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
            context_window=1_000_000,
        )
        live.turn_active = True
        casts: list[tuple[str, dict]] = []

        with patch("subagents.cc_event_result.insert_message", return_value="msg-id"):
            on_event = _make_on_event(
                live, "cc-sid", lambda *args: casts.append(args), db=None,
            )
            await on_event({
                "kind": "final_call_usage",
                "usage": {
                    "input_tokens": 1248,
                    "cache_creation_input_tokens": 6555,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 185,
                },
            })
            await on_event({
                "kind": "final_call_usage",
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 37_853,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 9,
                },
            })
            await on_event({
                "kind": "result",
                "aggregate_usage": {
                    "input_tokens": 1250,
                    "cache_creation_input_tokens": 44_408,
                    "cache_read_input_tokens": 0,
                },
                "model_usage": {
                    "claude-fable-5": {"contextWindow": 1_000_000},
                },
            })

        usage = next(
            payload["context_usage"] for _, payload in casts
            if payload.get("type") == "turn_complete"
        )
        assert usage["total_used"] == 37_855
        assert usage["provider_reported_input_tokens"] == 2
        assert live.last_call_usage is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "final_usage",
        [
            pytest.param(None, id="absent"),
            pytest.param("malformed", id="malformed"),
        ],
    )
    async def test_unmeasured_final_call_clears_prior_call_and_aggregate_usage(
        self, final_usage,
    ):
        from subagents.cc_events import normalize_event
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        snapshot = {
            "snapshot_id": "ctx-final-call-unmeasured",
            "harness_model_facts": {
                "harness": "claude-code",
                "model": "claude-code/opus",
                "model_context_window": 1_000_000,
            },
        }
        live = _Live(
            session=SimpleNamespace(
                session_id="provider-cc-session",
                resume_session_id=None,
                pid=4242,
                transport_generation_id=(
                    "11111111-1111-4111-8111-111111111111"
                ),
            ),
            parent_thread_id=None,
            name="cc",
            model="opus",
            catalog_model_id="claude-code/opus",
            runtime_context_epoch=9,
            context_window=1_000_000,
            context_snapshot_id=snapshot["snapshot_id"],
            context_snapshot=snapshot,
        )
        live.turn_active = True
        casts: list[tuple[str, dict]] = []
        recorded: list[dict] = []

        def fake_attach(*_args, **_kwargs):
            recorded.append(_kwargs)
            return _kwargs["context_usage"]

        def stream_delta(usage):
            event = {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
            }
            if usage is not None:
                event["usage"] = usage
            return {"type": "stream_event", "event": event}

        with patch("subagents.cc_event_result.insert_message", return_value="msg-id"):
            with patch(
                "subagents.cc_event_context_usage.attach_context_receipt",
                fake_attach,
            ):
                on_event = _make_on_event(
                    live, "cc-sid", lambda sid, message: casts.append((sid, message)),
                    db=None,
                )
                for raw in (
                    stream_delta({"input_tokens": 111}),
                    stream_delta(final_usage),
                    {
                        "type": "result",
                        "usage": {"input_tokens": 999_999},
                        "modelUsage": {
                            "claude-opus-4-8": {"contextWindow": 1_000_000},
                        },
                    },
                ):
                    for event in normalize_event(raw):
                        await on_event(event)

        payload = next(
            message for sid, message in casts
            if sid == "cc-sid" and message.get("type") == "turn_complete"
        )
        assert payload["context_usage"]["token_usage_available"] is False
        assert payload["context_usage"]["total_used"] is None
        assert payload["context_usage"]["stale_reason"] == (
            "terminal_without_measurement"
        )
        assert payload["context_usage"].get("provider_reported_input_tokens") is None
        identity = recorded[0]["identity"]
        assert identity.catalog_model_id == "claude-code/opus"
        assert identity.runtime_context_epoch == 9
        assert len(identity.producer_handle_fingerprint) == 64
        assert live.last_call_usage is None

    @pytest.mark.asyncio
    async def test_error_terminal_retains_then_clears_last_call_usage(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
            context_window=200_000,
        )
        live.turn_active = True
        casts: list[tuple[str, dict]] = []

        with patch("subagents.cc_event_result.insert_message", return_value="msg-id"):
            on_event = _make_on_event(
                live, "cc-sid", lambda *args: casts.append(args), db=None,
            )
            await on_event({
                "kind": "final_call_usage",
                "usage": {
                    "input_tokens": 80,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 5,
                },
            })
            await on_event({
                "kind": "result",
                "is_error": True,
                "result": "provider failed",
                "aggregate_usage": {"input_tokens": 999_999},
                "model_usage": {
                    "claude-opus-4-8": {"contextWindow": 200_000},
                },
            })

        usage = next(
            payload["context_usage"] for _, payload in casts
            if payload.get("type") == "turn_complete"
        )
        assert usage["total_used"] == 105
        assert live.last_call_usage is None

    @pytest.mark.asyncio
    async def test_reasoning_signature_persists_as_canonical_metadata(self, child_db):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        _seed_parent(child_db, sid="cc-reasoning")
        live = _Live(session=None, parent_thread_id=None, name="cc")
        on_event = _make_on_event(live, "cc-reasoning", lambda *_: None, child_db)

        await on_event({
            "kind": "reasoning_provenance",
            "reasoning_provenance": {
                "source_harness": "claude-code",
                "source_provider": "anthropic",
                "representation_kind": "anthropic_thinking",
                "native_signature": "sig-do-not-render",
            },
        })
        await on_event({
            "kind": "assistant_step",
            "text": "Answer",
            "reasoning": "Reasoned answer",
            "tool_uses": [],
        })
        await on_event({
            "kind": "assistant_step",
            "text": "Legacy reasoning",
            "reasoning": "Legacy thought",
            "tool_uses": [],
        })
        await on_event({
            "kind": "assistant_step",
            "text": "No reasoning",
            "reasoning": "",
            "tool_uses": [],
        })

        rows = [dict(row) for row in child_db.execute(
            "SELECT content, metadata FROM messages WHERE thread_id = ? ORDER BY rowid",
            ("cc-reasoning",),
        ).fetchall()]
        first_meta = json.loads(rows[0]["metadata"])
        assert first_meta["reasoning"] == "Reasoned answer"
        assert first_meta["reasoning_provenance"] == {
            "source_harness": "claude-code",
            "source_provider": "anthropic",
            "representation_kind": "anthropic_thinking",
            "native_signature": "sig-do-not-render",
            "native_signatures": ["sig-do-not-render"],
        }
        assert "sig-do-not-render" not in rows[0]["content"]
        assert "sig-do-not-render" not in first_meta["reasoning"]
        assert json.loads(rows[1]["metadata"])["reasoning_provenance"] == {
            "source_harness": "unknown",
            "source_provider": "unknown",
            "representation_kind": "unknown",
        }
        assert "reasoning_provenance" not in json.loads(rows[2]["metadata"])

    @pytest.mark.asyncio
    async def test_streamed_reasoning_keeps_all_ordered_signatures(self, child_db):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        _seed_parent(child_db, sid="cc-reasoning-many")
        live = _Live(session=None, parent_thread_id=None, name="cc")
        on_event = _make_on_event(
            live, "cc-reasoning-many", lambda *_: None, child_db,
        )

        for signature in ("sig-2", "sig-1", "sig-2"):
            await on_event({
                "kind": "reasoning_provenance",
                "reasoning_provenance": {
                    "source_harness": "claude-code",
                    "source_provider": "anthropic",
                    "representation_kind": "anthropic_thinking",
                    "native_signature": signature,
                },
            })
        await on_event({
            "kind": "assistant_step",
            "text": "Answer",
            "reasoning": "Reasoned answer",
            "tool_uses": [],
        })

        row = child_db.execute(
            "SELECT metadata FROM messages WHERE thread_id = ?",
            ("cc-reasoning-many",),
        ).fetchone()
        provenance = json.loads(row["metadata"])["reasoning_provenance"]
        assert provenance == {
            "source_harness": "claude-code",
            "source_provider": "anthropic",
            "representation_kind": "anthropic_thinking",
            "native_signatures": ["sig-2", "sig-1"],
        }

    @pytest.mark.asyncio
    async def test_reasoning_effective_model_and_typed_blocks_survive_persistence(
        self, child_db,
    ):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        _seed_parent(child_db, sid="cc-reasoning-identity")
        live = _Live(session=None, parent_thread_id=None, name="cc")
        on_event = _make_on_event(
            live, "cc-reasoning-identity", lambda *_: None, child_db,
        )
        typed_blocks = [
            {"type": "thinking", "thinking": "first", "signature": "sig-1"},
            {"type": "redacted_thinking", "data": "redacted-payload"},
            {"type": "thinking", "thinking": "second", "signature": "sig-2"},
        ]

        await on_event({
            "kind": "assistant_step",
            "text": "Answer",
            "reasoning": "first\nsecond",
            "tool_uses": [],
            "reasoning_provenance": {
                "source_harness": "claude-code",
                "source_provider": "anthropic",
                "source_effective_model": "claude-fable-5",
                "representation_kind": "anthropic_thinking",
                "native_signatures": ["sig-1", "sig-2"],
                "native_blocks": typed_blocks,
            },
        })

        row = child_db.execute(
            "SELECT metadata FROM messages WHERE thread_id = ?",
            ("cc-reasoning-identity",),
        ).fetchone()
        provenance = json.loads(row["metadata"])["reasoning_provenance"]
        assert provenance["source_effective_model"] == "claude-fable-5"
        assert provenance["native_blocks"] == typed_blocks

    @pytest.mark.asyncio
    async def test_redacted_only_assistant_envelope_persists_before_terminal(
        self, child_db,
    ):
        from subagents.cc_events import normalize_event
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        thread_id = "cc-redacted-only"
        _seed_parent(child_db, sid=thread_id)
        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
            provider_reported_model="claude-fable-5",
        )
        live.turn_active = True
        casts: list[tuple[str, dict]] = []
        on_event = _make_on_event(
            live,
            thread_id,
            lambda sid, message: casts.append((sid, message)),
            child_db,
        )

        raw_assistant = {
            "type": "assistant",
            "message": {
                "model": "claude-fable-5",
                "content": [{
                    "type": "redacted_thinking",
                    "data": "opaque-provider-state",
                }],
            },
        }
        for event in normalize_event(raw_assistant):
            await on_event(event)

        await on_event({
            "kind": "result",
            "is_error": False,
            "result": "",
            "stop_reason": "end_turn",
        })

        rows = [dict(row) for row in child_db.execute(
            "SELECT id, content, metadata FROM messages "
            "WHERE thread_id = ? AND role = 'assistant' ORDER BY rowid",
            (thread_id,),
        ).fetchall()]
        assert len(rows) == 1
        assert rows[0]["content"] == ""
        assert json.loads(rows[0]["metadata"])["reasoning_provenance"] == {
            "source_harness": "claude-code",
            "source_provider": "anthropic",
            "source_effective_model": "claude-fable-5",
            "representation_kind": "anthropic_thinking",
            "native_blocks": [{
                "type": "redacted_thinking",
                "data": "opaque-provider-state",
            }],
        }
        terminal = next(
            message for sid, message in casts
            if sid == thread_id and message.get("type") == "turn_complete"
        )
        assert "context_message_id" not in terminal

    @pytest.mark.asyncio
    async def test_flushed_reasoning_step_records_claude_message_id(self, child_db):
        """The Step synthesized at the result keeps the id Claude's transcript uses."""
        from subagents.cc_events import normalize_event
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        thread_id = "cc-redacted-flush-id"
        _seed_parent(child_db, sid=thread_id)
        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
            provider_reported_model="claude-fable-5",
        )
        live.turn_active = True
        on_event = _make_on_event(live, thread_id, lambda sid, message: None, child_db)

        for event in normalize_event({
            "type": "assistant",
            "message": {
                "id": "msg_01RedactedOnly",
                "model": "claude-fable-5",
                "content": [{"type": "redacted_thinking", "data": "opaque"}],
            },
        }):
            await on_event(event)
        await on_event({
            "kind": "result", "is_error": False, "result": "", "stop_reason": "end_turn",
        })

        (row,) = child_db.execute(
            "SELECT metadata FROM messages WHERE thread_id = ? AND role = 'assistant'",
            (thread_id,),
        ).fetchall()
        assert json.loads(row["metadata"])["provider_message_id"] == "msg_01RedactedOnly"
        assert live.reasoning_provider_message_id is None

    @pytest.mark.asyncio
    async def test_pre_input_result_is_ignored(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(session=None, parent_thread_id=None, name="cc")
        casts: list[tuple[str, dict]] = []

        with patch("subagents.cc_event_result.insert_message") as insert_message:
            on_event = _make_on_event(
                live, "cc-sid", lambda sid, m: casts.append((sid, m)), db=None
            )
            await on_event({"kind": "result", "result": "startup-noop"})

        insert_message.assert_not_called()
        assert casts == []

    @pytest.mark.asyncio
    async def test_unhashable_event_kind_is_ignored(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        casts = []
        on_event = _make_on_event(
            _Live(session=None, parent_thread_id=None, name="cc"),
            "cc-sid",
            lambda sid, message: casts.append((sid, message)),
            db=None,
        )

        await on_event({"kind": []})
        await on_event({"kind": {}})

        assert casts == []

    @pytest.mark.asyncio
    async def test_tool_use_without_name_fails_before_persistence(self, child_db):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        _seed_parent(child_db, sid="cc-sid")
        child_db.execute(
            "UPDATE threads SET model='claude-code/opus' WHERE id='cc-sid'"
        )
        child_db.commit()
        live = _Live(session=None, parent_thread_id=None, name="cc")
        casts = []
        on_event = _make_on_event(
            live,
            "cc-sid",
            lambda sid, message: casts.append((sid, message)),
            db=child_db,
        )
        with pytest.raises(RuntimeError, match="capability boundary"):
            await on_event({
                "kind": "assistant_step",
                "text": "",
                "reasoning": "",
                "tool_uses": [{"id": "tool-1", "name": None, "input": {}}],
            })

        assert live.provider_context_untrusted is True
        assert live.tool_names == {}
        assert all(
            message.get("type") != "tool_start" for _, message in casts
        )

    @pytest.mark.asyncio
    async def test_terminal_usage_attaches_one_compact_receipt(
        self,
        child_db,
    ):
        from infra.queries import insert_message
        from subagents.cc_event_context_usage import (
            consume_and_attach_terminal_context_usage,
        )
        from subagents.cc_runtime_state import _Live

        _seed_parent(child_db, sid="cc-receipt")
        insert_message(
            "cc-receipt",
            "assistant",
            "done",
            {"turn_id": "turn-cc-receipt"},
            msg_id="assistant-cc-receipt",
            db=child_db,
        )
        marker = "must-not-be-duplicated-" + ("x" * 10_000)
        live = _Live(
            session=SimpleNamespace(
                session_id="provider-cc-receipt",
                resume_session_id=None,
                pid=4242,
                transport_generation_id=(
                    "11111111-1111-4111-8111-111111111111"
                ),
            ),
            parent_thread_id=None,
            name="cc",
            model="opus",
            catalog_model_id="claude-code/opus",
            runtime_context_epoch=9,
            context_window=1_000_000,
            context_snapshot_id="ctx-cc-receipt",
            context_snapshot={
                "snapshot_id": "ctx-cc-receipt",
                "frozen_prompt_material": {"messages": [marker]},
            },
            source_artifacts=[{"payload": marker}],
        )
        live.last_call_usage = {
            "input_tokens": 10,
            "cache_read_input_tokens": 90,
        }
        payload: dict = {}

        await consume_and_attach_terminal_context_usage(
            live=live,
            child_id="cc-receipt",
            payload=payload,
            message_id="assistant-cc-receipt",
            db=child_db,
        )

        metadata = json.loads(child_db.execute(
            "SELECT metadata FROM messages WHERE id='assistant-cc-receipt'"
        ).fetchone()["metadata"])
        receipt = metadata["context_usage"]
        assert payload["context_usage"]["context_snapshot"] == {
            "snapshot_id": "ctx-cc-receipt",
        }
        assert "context_snapshot" not in receipt
        assert receipt["harness"] == "claude-code"
        assert receipt["catalog_model_id"] == "claude-code/opus"
        assert receipt["runtime_context_epoch"] == 9
        assert len(receipt["producer_handle_fingerprint"]) == 64
        assert receipt["total_used"] == 100
        assert marker not in json.dumps(receipt)

    @pytest.mark.asyncio
    async def test_assistant_rows_carry_model_identity_and_context_proof(self, child_db):
        from core.token_estimation import _estimate_messages_tokens, _estimate_tokens
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        history = [{"role": "assistant", "content": "historical context " * 20}]
        current_user = "fresh user request"
        snapshot = {
            "snapshot_id": "ctx-cc-footer-proof",
            "source_inventory": [
                {
                    "source_id": "system-source",
                    "origin_section": "system",
                    "inclusion": "included",
                    "prompt_tokens": 42,
                },
                {
                    "source_id": "project-source",
                    "origin_section": "project",
                    "inclusion": "included",
                    "prompt_tokens": 84,
                },
            ],
            "frozen_prompt_material": {
                "history": history,
                "current_user_content": current_user,
            },
            "delivery_leaves": [{
                "harness": "claude-code",
                "destination_mode": "jsonl-seed",
            }],
        }
        live = _Live(
            session=SimpleNamespace(
                session_id="cc-sid",
                resume_session_id=None,
                pid=4242,
                transport_generation_id=(
                    "11111111-1111-4111-8111-111111111111"
                ),
            ),
            parent_thread_id=None,
            name="cc",
            model="sonnet",
            catalog_model_id="claude-code/sonnet",
            runtime_context_epoch=4,
            expected_reported_model="claude-sonnet-4-8",
            context_window=200_000,
            context_snapshot_id=snapshot["snapshot_id"],
            context_snapshot=snapshot,
        )
        live.turn_active = True
        _seed_parent(child_db, sid="cc-sid")
        casts: list[tuple[str, dict]] = []
        on_event = _make_on_event(
            live, "cc-sid", lambda sid, m: casts.append((sid, m)), db=child_db
        )
        await on_event({"kind": "init", "model": "claude-sonnet-4-8"})
        await on_event({
            "kind": "assistant_step", "text": "Hello",
            "reasoning": "", "tool_uses": [],
        })
        await on_event({
            "kind": "final_call_usage",
            "usage": {"input_tokens": 500},
        })
        await on_event({
            "kind": "result",
            "aggregate_usage": {"input_tokens": 50_000},
            "model_usage": {"claude-sonnet-4-8": {"contextWindow": 200_000}},
        })

        step = next(m for sid, m in casts if sid == "cc-sid" and m["type"] == "step_text")
        assert step["metadata"]["harness"] == "claude-code"
        assert step["metadata"]["model"] == "claude-code/claude-sonnet-4-8"
        assert step["metadata"]["provider_reported_model"] == "claude-code/claude-sonnet-4-8"
        assert step["metadata"]["model_identity_mismatch"] is False
        metadata = json.loads(child_db.execute(
            "SELECT metadata FROM messages WHERE id=?", (step["message_id"],),
        ).fetchone()["metadata"])
        usage = metadata["context_usage"]
        expected_history = _estimate_messages_tokens(history)
        expected_user = _estimate_tokens(current_user)
        assert usage["snapshot_id"] == "ctx-cc-footer-proof"
        expected_total = 42 + 84 + expected_history + expected_user
        assert usage["total_used"] == 500
        assert usage["utilization"] == round(500 / usage["model_context_window"], 3)
        assert usage["provider_reported_total_used"] == 500
        assert usage["system_tokens"] == 42
        assert usage["context_source_tokens"] == 84
        assert usage["history_tokens"] == expected_history
        assert usage["history_tokens"] != usage["total_used"]
        assert usage["user_tokens"] == expected_user
        assert usage["attribution_delta_tokens"] == 500 - expected_total

    @pytest.mark.asyncio
    async def test_reused_process_anchors_each_turn_context_to_its_exact_row(
        self, child_db, monkeypatch,
    ):
        from core.turn_core import BOOT_EPOCH
        from infra.thread_driver import admit_input
        from infra.time import utc_now_iso
        from subagents import cc_event_context, cc_event_result, cc_stream_control
        from subagents.cc_runtime import _accept_reconstructed_input
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        _seed_parent(child_db, sid="cc-lineage")
        child_db.execute(
            "UPDATE threads SET model='claude-code/opus' WHERE id='cc-lineage'"
        )
        child_db.commit()
        monkeypatch.setattr(
            cc_event_result, "claim_terminal", cc_event_context.claim_terminal,
        )

        class AcceptedSession:
            reconstructed_transcript_path = None
            session_id = "cc-lineage"
            resume_session_id = None
            pid = 4242
            transport_generation_id = "22222222-2222-4222-8222-222222222222"

            async def feed(self, _text, *, on_write_observed, on_accepted, input_id=None):
                on_write_observed()
                on_accepted()

        def reconstruction(snapshot_id: str, text: str):
            snapshot = {
                "snapshot_id": snapshot_id,
                "content_hash": snapshot_id,
                "delivery_leaves": [{
                    "harness": "claude-code",
                    "destination_mode": "turn-only",
                }],
                "frozen_prompt_material": {"current_user_content": text},
            }
            return SimpleNamespace(
                turn_text=text,
                snapshot_id=snapshot_id,
                snapshot=SimpleNamespace(to_dict=lambda: snapshot),
                continuity_accounting=ContinuityAccounting.not_delivered(),
            )

        live = _Live(
            session=AcceptedSession(),
            parent_thread_id=None,
            name="cc",
            model="opus",
            catalog_model_id="claude-code/opus",
            runtime_context_epoch=7,
            context_window=262_144,
        )
        casts: list[tuple[str, dict]] = []
        bcast = lambda sid, message: casts.append((sid, message))
        on_event = _make_on_event(live, "cc-lineage", bcast, child_db)

        async def drive_terminal(terminal) -> None:
            session = SimpleNamespace(
                corsia_thread_id="cc-lineage", _proc=None, _terminal_driver=None,
            )
            completed = asyncio.get_running_loop().create_future()
            completed.set_result(terminal)
            cc_stream_control.observe_terminal_event(session, completed)
            await session._terminal_driver

        def admit(turn_id: str, text: str):
            admission = admit_input(
                "cc-lineage",
                role="user",
                content=text,
                intent="apply_now",
                message_id=f"{turn_id}-input",
                turn_id=turn_id,
                boot_epoch=BOOT_EPOCH,
                admitted_at=utc_now_iso(),
                db=child_db,
            )
            assert admission.batch is not None
            return admission.batch

        batch_a = admit("turn-cc-lineage-a", "Turn A")
        await _accept_reconstructed_input(
            "cc-lineage", live, reconstruction("ctx-a", "Turn A"),
            turn_id="turn-cc-lineage-a",
            preadmitted_turn=batch_a.identity,
            mark_accepted=lambda: settle_test_input(batch_a),
            bcast=bcast,
        )
        await on_event({
            "kind": "assistant_step",
            "text": "A answer",
            "reasoning": "",
            "tool_uses": [],
        })
        await on_event({
            "kind": "final_call_usage",
            "usage": {"input_tokens": 11},
        })
        terminal_a = await on_event({
            "kind": "result",
            "aggregate_usage": {"input_tokens": 111},
            "model_usage": {"claude-opus-4-8": {"contextWindow": 262_144}},
        })
        await drive_terminal(terminal_a)

        prior_identity = live.corsia_turn
        assert prior_identity is not None
        live.apply_now_interrupt_turn = prior_identity
        assert child_db.execute(
            "SELECT status FROM active_turns WHERE id='turn-cc-lineage-a'"
        ).fetchone()["status"] == "completed"
        batch_b = admit("turn-cc-lineage-b", "Turn B")
        await _accept_reconstructed_input(
            "cc-lineage", live, reconstruction("ctx-b", "Turn B"),
            turn_id="turn-cc-lineage-b",
            preadmitted_turn=batch_b.identity,
            mark_accepted=lambda: settle_test_input(batch_b),
            bcast=bcast,
        )
        assert live.apply_now_interrupt_turn is None
        await on_event({
            "kind": "final_call_usage",
            "usage": {"input_tokens": 22},
        })
        terminal_b = await on_event({
            "kind": "result",
            "result": "B completed without assistant output",
            "aggregate_usage": {"input_tokens": 222},
            "model_usage": {"claude-opus-4-8": {"contextWindow": 262_144}},
        })
        await drive_terminal(terminal_b)

        rows = [dict(row) for row in child_db.execute(
            "SELECT id, role, content, metadata FROM messages "
            "WHERE thread_id = ? ORDER BY rowid",
            ("cc-lineage",),
        ).fetchall()]
        assistant_a = next(row for row in rows if row["role"] == "assistant")
        result_rows = [
            row for row in rows
            if row["role"] == "system"
            and json.loads(row["metadata"] or "{}").get("type") == "result"
        ]
        marker_b = result_rows[-1]
        assistant_a_meta = json.loads(assistant_a["metadata"])
        marker_b_meta = json.loads(marker_b["metadata"])

        assert assistant_a_meta["context_usage"]["snapshot_id"] == "ctx-a"
        assert marker_b_meta["context_usage"]["snapshot_id"] == "ctx-b"
        assert assistant_a_meta["context_usage"]["harness"] == "claude-code"
        assert marker_b_meta["context_usage"]["harness"] == "claude-code"

        terminals = [
            message for sid, message in casts
            if sid == "cc-lineage" and message.get("type") == "turn_complete"
        ]
        assert [message["context_message_id"] for message in terminals] == [
            assistant_a["id"], marker_b["id"],
        ]
        assert [message["context_usage"]["snapshot_id"] for message in terminals] == [
            "ctx-a", "ctx-b",
        ]
        b_marker_event = next(
            index for index, (_, message) in enumerate(casts)
            if message.get("type") == "message_added"
            and message.get("message", {}).get("id") == marker_b["id"]
        )
        b_terminal_event = next(
            index for index, (_, message) in enumerate(casts)
            if message is terminals[1]
        )
        assert b_marker_event < b_terminal_event

    @pytest.mark.asyncio
    async def test_result_postprocessing_propagates_broadcast_failure(
        self,
        monkeypatch,
    ):
        from subagents import cc_event_result
        from subagents.cc_event_context import CCEventContext
        from subagents.cc_runtime_state import _Live

        live = _Live(session=None, parent_thread_id=None, name="cc")
        monkeypatch.setattr(
            cc_event_result,
            "consume_and_attach_terminal_context_usage",
            AsyncMock(),
        )

        def fail_broadcast(*_args):
            raise RuntimeError("turn broadcast failed")

        with pytest.raises(RuntimeError, match="turn broadcast failed"):
            await cc_event_result._finish_result_postprocessing(
                CCEventContext(live, "cc-sid", fail_broadcast, None),
                {"type": "turn_complete"},
                None,
                quiet_if_unmeasured=False,
            )

    @pytest.mark.asyncio
    async def test_result_postprocessing_survives_receipt_failure(
        self,
        monkeypatch,
        caplog,
    ):
        from subagents import cc_event_context_usage, cc_event_result
        from subagents.cc_event_context import CCEventContext
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=SimpleNamespace(
                session_id="provider-session",
                transport_generation_id=(
                    "11111111-1111-4111-8111-111111111111"
                ),
            ),
            parent_thread_id=None,
            name="cc",
            model="opus",
            catalog_model_id="claude-code/opus",
            runtime_context_epoch=4,
            context_window=200_000,
            last_call_usage={"input_tokens": 17},
        )
        casts: list[tuple[str, dict]] = []
        payload = {
            "type": "turn_complete",
            "context_message_id": "assistant-1",
        }

        async def fail_receipt(*_args, **_kwargs):
            raise RuntimeError("message update unavailable")

        monkeypatch.setattr(cc_event_context_usage, "run_in_db", fail_receipt)
        monkeypatch.setattr(
            cc_event_result,
            "record_terminal_llm_usage",
            AsyncMock(),
        )

        await cc_event_result._finish_result_postprocessing(
            CCEventContext(
                live,
                "cc-sid",
                lambda sid, message: casts.append((sid, message)),
                None,
            ),
            payload,
            "assistant-1",
            quiet_if_unmeasured=False,
        )

        assert casts == [("cc-sid", payload)]
        assert payload["context_usage"]["total_used"] == 17
        assert "context_message_id" not in payload
        assert "terminal context receipt failed" in caplog.text
        assert live.last_call_usage is None

    @pytest.mark.asyncio
    async def test_result_marker_failure_still_publishes_terminal_and_releases_barrier(
        self,
    ):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        barrier = asyncio.Event()
        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
            context_window=200_000,
            last_call_usage={"input_tokens": 17},
            reasoning_provenance={"source_harness": "claude-code"},
            turn_active=True,
            turn_terminal=barrier,
            interrupt_requested=True,
        )
        live.thinking_buf = "unfinished"
        casts: list[tuple[str, dict]] = []

        with patch(
            "subagents.cc_event_result.insert_message",
            side_effect=RuntimeError("marker insert failed"),
        ):
            on_event = _make_on_event(
                live, "cc-sid", lambda sid, message: casts.append((sid, message)),
                db=None,
            )
            result = {
                "kind": "result",
                "is_error": True,
                "result": "provider failed",
            }
            await on_event(result)
            await on_event(result)

        terminals = [
            message for sid, message in casts
            if sid == "cc-sid" and message.get("type") == "turn_complete"
        ]
        assert len(terminals) == 1
        assert terminals[0]["context_usage"]["total_used"] == 17
        assert "context_message_id" not in terminals[0]
        assert not any(message.get("type") == "message_added" for _, message in casts)
        assert barrier.is_set()
        assert live.turn_active is False
        assert live.interrupt_requested is False
        assert live.last_call_usage is None
        assert live.thinking_buf == ""
        assert live.reasoning_provenance is None

    @pytest.mark.asyncio
    async def test_result_marker_announcement_failure_still_publishes_terminal(
        self, caplog,
    ):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        barrier = asyncio.Event()
        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
            context_window=200_000,
            last_call_usage={"input_tokens": 17},
            reasoning_provenance={"source_harness": "claude-code"},
            turn_active=True,
            turn_terminal=barrier,
            interrupt_requested=True,
        )
        live.thinking_buf = "unfinished"
        casts: list[tuple[str, dict]] = []
        marker_announcements: list[dict] = []

        def flaky_bcast(thread_id, payload):
            if payload["type"] == "message_added":
                marker_announcements.append(payload)
                raise RuntimeError("marker announcement unavailable")
            casts.append((thread_id, payload))

        # Exercise the caller's failure handling while isolating durable projection.

        with patch(
            "subagents.cc_event_result.insert_message",
        ) as insert_marker, patch(
            "subagents.cc_event_result.broadcast_stored_message",
            side_effect=_broadcast_insert_stub,
        ):
            on_event = _make_on_event(live, "cc-sid", flaky_bcast, db=None)
            result = {
                "kind": "result",
                "is_error": True,
                "result": "provider failed",
            }
            await on_event(result)
            await on_event(result)

        terminals = [
            message for sid, message in casts
            if sid == "cc-sid" and message.get("type") == "turn_complete"
        ]
        insert_marker.assert_called_once()
        assert len(marker_announcements) == 1
        assert len(terminals) == 1
        assert terminals[0]["context_usage"]["total_used"] == 17
        assert "context_message_id" not in terminals[0]
        assert "result marker announcement failed" in caplog.text
        assert barrier.is_set()
        assert live.turn_active is False
        assert live.interrupt_requested is False
        assert live.last_call_usage is None
        assert live.thinking_buf == ""
        assert live.reasoning_provenance is None

    @pytest.mark.asyncio
    async def test_snapshot_without_provider_usage_is_marked_unmeasured_not_zero(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        snapshot = {
            "snapshot_id": "ctx-cc-source-truth",
            "content_hash": "h" * 64,
            "harness_model_facts": {
                "harness": "claude-code",
                "model": "claude-code/opus",
                "model_context_window": 1_000_000,
            },
            "source_inventory": [{"source_id": "source-1"}],
            "delivery_leaves": [{
                "harness": "claude-code",
                "destination_mode": "jsonl-seed",
            }],
        }
        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
            context_snapshot_id=snapshot["snapshot_id"],
            context_snapshot=snapshot,
        )
        live.turn_active = True
        casts: list[tuple[str, dict]] = []

        with patch("subagents.cc_event_result.insert_message", return_value="msg-id"):
            on_event = _make_on_event(
                live, "cc-sid", lambda sid, m: casts.append((sid, m)), db=None
            )
            await on_event({
                "kind": "result",
                "aggregate_usage": {"input_tokens": 999_999},
            })

        payload = next(m for sid, m in casts if sid == "cc-sid" and m["type"] == "turn_complete")
        usage = payload["context_usage"]
        assert usage["snapshot_id"] == "ctx-cc-source-truth"
        assert usage["model_context_window"] == 1_000_000
        assert usage["token_usage_available"] is False
        assert usage["total_used"] is None
        assert usage["utilization"] is None

    @pytest.mark.asyncio
    async def test_prompt_too_long_result_marks_provider_context_untrusted(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
        )
        live.turn_active = True
        casts: list[tuple[str, dict]] = []

        with patch("subagents.cc_event_result.insert_message", return_value="msg-id") as insert:
            on_event = _make_on_event(
                live,
                "cc-sid",
                lambda sid, m: casts.append((sid, m)),
                db=None,
            )
            await on_event({
                "kind": "result",
                "is_error": True,
                "result": "Prompt is too long",
                "stop_reason": "error",
            })

        assert live.provider_context_untrusted is True
        meta = insert.call_args.args[3]
        assert meta["cause"] == "context_window_exceeded"
        assert meta["repair_owner"] == "ctx-stability"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("result", [
        "API Error: 400 messages: text content blocks must be non-empty",
        "API Error: 500 Internal server error",
        "API Error: 400 invalid_request_error: max_tokens too large",
    ])
    async def test_only_the_empty_text_block_400_retires_the_provider_session(self, result):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(session=None, parent_thread_id=None, name="cc")
        live.turn_active = True
        poisoned = "non-empty" in result

        async def pool(fn, *args, **kwargs):
            # The result marker's insert also leaves the loop; only the retirement is counted.
            return fn(*args, **kwargs) if fn is insert else None

        with patch("subagents.cc_event_result.insert_message", return_value="msg-id") as insert, \
                patch("infra.db_async.run_in_db", new=AsyncMock(side_effect=pool)) as run_db:
            on_event = _make_on_event(live, "cc-sid", lambda sid, m: None, db=None)
            await on_event({"kind": "result", "is_error": True, "result": result})

        retire = [c for c in run_db.await_args_list if c.args[0] is not insert]
        assert len(retire) == (1 if poisoned else 0)
        assert live.provider_context_untrusted is poisoned
        assert (insert.call_args.args[3].get("cause") == "provider_transcript_poisoned") is poisoned

    @pytest.mark.asyncio
    async def test_init_with_native_tools_fails_closed_once_and_redacts(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
        )
        casts: list[tuple[str, dict]] = []

        with patch(
            "subagents.cc_event_simple.insert_message", return_value="msg-id",
        ) as insert, patch(
            "subagents.cc_event_simple.broadcast_stored_message",
            side_effect=_broadcast_insert_stub,
        ):
            on_event = _make_on_event(
                live,
                "cc-sid",
                lambda sid, m: casts.append((sid, m)),
                db=None,
            )
            event = {
                "kind": "init",
                "tools": ["Bash"],
                "mcp_servers": [{"name": "corsia"}, {"name": "not-corsia"}],
                "model": "claude-opus",
                "command": "cat /Users/example/.ssh/id_ed25519",
                "output": "secret",
            }
            with pytest.raises(RuntimeError, match="capability boundary"):
                await on_event(event)
            with pytest.raises(RuntimeError, match="capability boundary"):
                await on_event(event)

        assert live.provider_context_untrusted is True
        assert insert.call_count == 1
        meta = insert.call_args.args[3]
        assert meta["event_type"] == "harness.tool_boundary_violation"
        assert meta["harness"] == "claude-code"
        assert meta["native_tools"] == ["Bash"]
        assert meta["non_corsia_mcp_servers"] == ["not-corsia"]
        assert "command" not in meta
        assert "output" not in meta

    @pytest.mark.asyncio
    async def test_init_with_direct_corsia_mcp_tool_is_not_boundary_violation(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
        )
        casts: list[tuple[str, dict]] = []

        with patch("subagents.cc_event_simple.insert_message") as insert:
            on_event = _make_on_event(
                live,
                "cc-sid",
                lambda sid, m: casts.append((sid, m)),
                db=None,
            )
            await on_event({
                "kind": "init",
                "tools": ["mcp__corsia__read_file"],
                "mcp_servers": [{"name": "corsia"}],
                "model": "claude-opus",
            })

        assert live.provider_context_untrusted is False
        assert not insert.called
        assert any(
            sid == "cc-sid" and m.get("type") == "subagent_init"
            for sid, m in casts
        )

    @pytest.mark.asyncio
    async def test_init_with_pending_corsia_mcp_but_no_tools_is_not_boundary_violation(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
        )
        casts: list[tuple[str, dict]] = []

        with patch("subagents.cc_event_simple.insert_message", return_value="msg-id") as insert:
            on_event = _make_on_event(
                live,
                "cc-sid",
                lambda sid, m: casts.append((sid, m)),
                db=None,
            )
            await on_event({
                "kind": "init",
                "tools": [],
                "mcp_servers": [{"name": "corsia", "status": "pending"}],
                "model": "claude-opus",
            })

        assert live.provider_context_untrusted is False
        assert not insert.called
        assert any(
            sid == "cc-sid" and m.get("type") == "subagent_init"
            for sid, m in casts
        )

    @pytest.mark.asyncio
    async def test_init_with_transient_corsia_mcp_status_but_no_tools_is_not_boundary_violation(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
        )
        casts: list[tuple[str, dict]] = []

        with patch("subagents.cc_event_simple.insert_message") as insert:
            on_event = _make_on_event(
                live,
                "cc-sid",
                lambda sid, m: casts.append((sid, m)),
                db=None,
            )
            await on_event({
                "kind": "init",
                "tools": [],
                "mcp_servers": [{"name": "corsia", "status": "initializing"}],
                "model": "claude-opus",
            })

        assert live.provider_context_untrusted is False
        assert not insert.called
        assert any(
            sid == "cc-sid" and m.get("type") == "subagent_init"
            for sid, m in casts
        )

    @pytest.mark.asyncio
    async def test_init_with_connected_corsia_mcp_but_no_tools_records_bridge_unavailable(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
        )
        casts: list[tuple[str, dict]] = []

        with patch(
            "subagents.cc_event_simple.insert_message", return_value="msg-id",
        ) as insert, patch(
            "subagents.cc_event_simple.broadcast_stored_message",
            side_effect=_broadcast_insert_stub,
        ):
            on_event = _make_on_event(
                live,
                "cc-sid",
                lambda sid, m: casts.append((sid, m)),
                db=None,
            )
            with pytest.raises(RuntimeError, match="tool bridge is unavailable"):
                await on_event({
                    "kind": "init",
                    "tools": [],
                    "mcp_servers": [{"name": "corsia", "status": "connected"}],
                    "model": "claude-opus",
                })

        assert live.provider_context_untrusted is True
        meta = insert.call_args.args[3]
        assert meta["event_type"] == "harness.tool_bridge_unavailable"
        assert meta["missing_corsia_tools"] is True
        assert not any(
            sid == "cc-sid" and m.get("type") == "subagent_init"
            for sid, m in casts
        ), "a refused bridge must not announce a ready session"

    @pytest.mark.asyncio
    async def test_init_with_statusless_corsia_mcp_but_no_tools_records_bridge_unavailable(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
        )

        with patch(
            "subagents.cc_event_simple.insert_message", return_value="msg-id",
        ) as insert, patch(
            "subagents.cc_event_simple.broadcast_stored_message",
            side_effect=_broadcast_insert_stub,
        ):
            on_event = _make_on_event(
                live,
                "cc-sid",
                lambda sid, m: None,
                db=None,
            )
            with pytest.raises(RuntimeError, match="tool bridge is unavailable"):
                await on_event({
                    "kind": "init",
                    "tools": [],
                    "mcp_servers": [{"name": "corsia"}],
                    "model": "claude-opus",
                })

        assert live.provider_context_untrusted is True
        meta = insert.call_args.args[3]
        assert meta["event_type"] == "harness.tool_bridge_unavailable"
        assert meta["missing_corsia_tools"] is True

    @pytest.mark.asyncio
    async def test_init_with_failed_corsia_mcp_but_no_tools_records_bridge_unavailable(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
        )

        with patch(
            "subagents.cc_event_simple.insert_message", return_value="msg-id",
        ) as insert, patch(
            "subagents.cc_event_simple.broadcast_stored_message",
            side_effect=_broadcast_insert_stub,
        ):
            on_event = _make_on_event(
                live,
                "cc-sid",
                lambda sid, m: None,
                db=None,
            )
            with pytest.raises(RuntimeError, match="tool bridge is unavailable"):
                await on_event({
                    "kind": "init",
                    "tools": [],
                    "mcp_servers": [{"name": "corsia", "status": "failed"}],
                    "model": "claude-opus",
                })

        assert live.provider_context_untrusted is True
        meta = insert.call_args.args[3]
        assert meta["event_type"] == "harness.tool_bridge_unavailable"
        assert meta["missing_corsia_tools"] is True

    @pytest.mark.asyncio
    async def test_init_with_descriptor_only_plugin_and_agent_stays_trusted(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
        )
        casts: list[tuple[str, dict]] = []

        with patch("subagents.cc_event_simple.insert_message") as insert:
            on_event = _make_on_event(
                live,
                "cc-sid",
                lambda sid, m: casts.append((sid, m)),
                db=None,
            )
            await on_event({
                "kind": "init",
                "tools": ["mcp__corsia__read_file"],
                "mcp_servers": [{"name": "corsia", "status": "connected"}],
                "plugins": ["superpowers@claude-plugins-official"],
                "agents": ["reviewer"],
                "model": "claude-opus",
            })

        assert live.provider_context_untrusted is False
        insert.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("skills", ["superpowers:using-superpowers"]),
            ("slash_commands", ["/review"]),
        ],
    )
    async def test_init_with_callable_provider_capability_fails_closed(
        self,
        field,
        value,
    ):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(session=None, parent_thread_id=None, name="cc")
        event = {
            "kind": "init",
            "tools": ["mcp__corsia__read_file"],
            "mcp_servers": [{"name": "corsia", "status": "connected"}],
            field: value,
            "model": "claude-opus",
        }

        with patch(
            "subagents.cc_event_simple.insert_message",
            return_value="boundary-marker",
        ) as insert, patch(
            "subagents.cc_event_simple.broadcast_stored_message",
            side_effect=_broadcast_insert_stub,
        ):
            on_event = _make_on_event(
                live,
                "cc-sid",
                lambda _sid, _message: None,
                db=None,
            )
            with pytest.raises(RuntimeError, match="capability boundary"):
                await on_event(event)

        assert live.provider_context_untrusted is True
        meta = insert.call_args.args[3]
        assert meta["event_type"] == "harness.tool_boundary_violation"
        assert live.tool_boundary_marker_id == "boundary-marker"

    @pytest.mark.asyncio
    async def test_non_corsia_tool_call_fails_as_one_result_and_turn_continues(
        self, child_db,
    ):
        """A bare tool name never reaches CORSIA's bridge, so it is one failed call.

        Claude Code launches with only ``mcp__corsia__*`` tools and answers any
        other name with its own refusal. CORSIA settles the call with its own
        error row, binds the refusal echo to it without storing the echo text,
        and keeps the handle trusted: no system row, no turn death.
        """
        from core.turn_core import TurnIdentity
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        thread_id = "cc-bare-name"
        _seed_parent(child_db, sid=thread_id)
        live = _Live(
            session=None, parent_thread_id=None, name="cc",
            corsia_turn=TurnIdentity("turn-bare-name", 1),
        )
        live.turn_active = True
        casts: list[tuple[str, dict]] = []
        on_event = _make_on_event(
            live, thread_id, lambda sid, message: casts.append((sid, message)),
            child_db,
        )
        refusal = (
            "<tool_use_error>Error: No such tool available: read_file"
            "</tool_use_error>"
        )

        await on_event({
            "kind": "assistant_step", "text": "", "reasoning": "",
            "tool_uses": [{
                "id": "bare-read", "name": "read_file",
                "input": {"path": "skills/x/SKILL.md"},
            }],
        })
        assert live.tool_step_ids == {}
        assert live.tools_settled.is_set()
        await on_event({
            "kind": "tool_result", "tool_use_id": "bare-read",
            "content": refusal, "is_error": True,
        })
        await on_event({
            "kind": "assistant_step", "text": "", "reasoning": "",
            "tool_uses": [{
                "id": "prefixed-read", "name": "mcp__corsia__read_file",
                "input": {"path": "skills/x/SKILL.md"},
            }],
        })
        persist_bridge_tool_result(
            child_db, live, thread_id=thread_id,
            provider_tool_call_id="prefixed-read", content="skill body",
        )
        await on_event({
            "kind": "tool_result", "tool_use_id": "prefixed-read",
            "content": "skill body",
        })

        assert live.provider_context_untrusted is False
        assert live.tool_boundary_violation_recorded is False
        assert live.tool_boundary_marker_id is None
        assert live.tool_step_ids == {}
        rows = child_db.execute(
            "SELECT role, content, metadata FROM messages WHERE thread_id=? "
            "ORDER BY rowid",
            (thread_id,),
        ).fetchall()
        assert [row["role"] for row in rows] == [
            "assistant", "tool", "assistant", "tool",
        ]
        settled = json.loads(rows[1]["metadata"])
        assert settled["tool_call_id"] == "bare-read"
        assert settled["provider_tool_call_id"] == "bare-read"
        assert settled["synthetic_reason"] == "undispatched_tool"
        assert settled["is_error"] is True
        assert "`read_file`" in rows[1]["content"]
        assert "mcp__corsia__" in rows[1]["content"]
        assert "No such tool available" not in rows[1]["content"]
        assert rows[3]["content"] == "skill body"
        assert not any(
            message.get("type") == "message_added"
            and (message.get("message") or {}).get("role") == "system"
            for _, message in casts
        )
        done = [
            message for _, message in casts
            if message.get("type") == "tool_done"
            and message.get("tool_id") == "bare-read"
        ]
        assert [message["result"] for message in done] == [rows[1]["content"]]
        assert [message["outcome"] for message in done] == ["refused"]
        finalized = [
            message["finalized_tool_ids"] for _, message in casts
            if message.get("type") == "step_text"
            and message.get("finalized_tool_ids")
        ]
        assert finalized == [["bare-read"], ["prefixed-read"]]

    @pytest.mark.asyncio
    async def test_corsia_call_without_bridge_row_settles_from_its_echo(
        self, child_db,
    ):
        """A ``mcp__corsia__*`` call with no bridge row binds from its own echo.

        ``execute_tool`` persists before returning, so a zero-row echo proves
        the call never ran; it becomes that call's final, synthetic answer.
        """
        from core.turn_core import TurnIdentity
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        thread_id = "cc-missing-bridge-row"
        _seed_parent(child_db, sid=thread_id)
        live = _Live(
            session=None, parent_thread_id=None, name="cc",
            corsia_turn=TurnIdentity("turn-missing-row", 1),
        )
        live.turn_active = True
        on_event = _make_on_event(
            live, thread_id, lambda _sid, _message: None, child_db,
        )

        await on_event({
            "kind": "assistant_step", "text": "", "reasoning": "",
            "tool_uses": [{
                "id": "lost-read", "name": "mcp__corsia__read_file",
                "input": {},
            }],
        })
        assert live.tool_step_ids == {"lost-read": ANY}
        await on_event({
            "kind": "tool_result", "tool_use_id": "lost-read",
            "content": (
                "<tool_use_error>Error: No such tool available: "
                "mcp__corsia__read_file</tool_use_error>"
            ),
            "is_error": True,
        })

        assert live.tool_step_ids == {}
        assert live.tools_settled.is_set()
        rows = child_db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND role='tool'",
            (thread_id,),
        ).fetchall()
        assert len(rows) == 1
        settled = json.loads(rows[0]["metadata"])
        assert settled["synthetic_reason"] == "undispatched_tool"
        assert settled["tool_call_id"] == "lost-read"

    @pytest.mark.asyncio
    async def test_served_catalog_decides_which_prefixed_call_the_bridge_owes(
        self, child_db,
    ):
        """One Step mixes a served call and a misspelled sibling.

        The misspelling settles at once; the served name stays owed. Its
        sibling's row means this Step is no longer provably zero-row, so its
        own echo without a bridge row still fails loudly rather than guess.
        """
        from core._tool_result_delivery_support import ToolResultBindingError
        from core.turn_core import TurnIdentity
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _LIVE, _Live

        thread_id = "cc-served-catalog"
        _seed_parent(child_db, sid=thread_id)
        live = _Live(
            session=None, parent_thread_id=None, name="cc",
            corsia_turn=TurnIdentity("turn-served-catalog", 1),
        )
        live.turn_active = True
        live.tool_definitions = ({"function": {"name": "read_file"}},)
        on_event = _make_on_event(
            live, thread_id, lambda _sid, _message: None, child_db,
        )

        _LIVE.put(thread_id, live)
        try:
            await on_event({
                "kind": "assistant_step", "text": "", "reasoning": "",
                "tool_uses": [
                    {"id": "lost-read", "name": "mcp__corsia__read_file",
                     "input": {}},
                    {"id": "typo-read", "name": "mcp__corsia__read_fil",
                     "input": {}},
                ],
            })
        finally:
            _LIVE.pop(thread_id, None)
        assert set(live.tool_step_ids) == {"lost-read"}
        await on_event({
            "kind": "tool_result", "tool_use_id": "typo-read",
            "content": (
                "<tool_use_error>Error: No such tool available: "
                "mcp__corsia__read_fil</tool_use_error>"
            ),
            "is_error": True,
        })
        with pytest.raises(
            ToolResultBindingError, match="does not identify one canonical",
        ):
            await on_event({
                "kind": "tool_result", "tool_use_id": "lost-read",
                "content": "<tool_use_error>lost</tool_use_error>",
                "is_error": True,
            })

        rows = child_db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND role='tool'",
            (thread_id,),
        ).fetchall()
        assert [json.loads(row["metadata"])["tool_call_id"] for row in rows] == [
            "typo-read",
        ]
        assert live.provider_context_untrusted is False

    @pytest.mark.asyncio
    async def test_superseded_handle_keeps_every_prefixed_call_owed(
        self, child_db,
    ):
        """The bridge lists the current handle's catalog, so a handle that is no
        longer current cannot say a prefixed name went unserved: it stays owed,
        as before, rather than risk a second result beside the bridge's. Its
        own later echo still settles it, since a zero-row echo proves the
        bridge never ran it either way.
        """
        from core.turn_core import TurnIdentity
        from subagents.cc_event_tool_settlement import settle_missing_tool_results
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        thread_id = "cc-superseded-handle"
        _seed_parent(child_db, sid=thread_id)
        identity = TurnIdentity("turn-superseded", 1)
        live = _Live(
            session=None, parent_thread_id=None, name="cc",
            corsia_turn=identity,
        )
        live.turn_active = True
        live.tool_definitions = ({"function": {"name": "read_file"}},)
        on_event = _make_on_event(
            live, thread_id, lambda _sid, _message: None, child_db,
        )

        await on_event({
            "kind": "assistant_step", "text": "", "reasoning": "",
            "tool_uses": [{
                "id": "typo-read", "name": "mcp__corsia__read_fil", "input": {},
            }],
        })

        assert set(live.tool_step_ids) == {"typo-read"}
        assert child_db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='tool'",
            (thread_id,),
        ).fetchone()[0] == 0

        await on_event({
            "kind": "tool_result", "tool_use_id": "typo-read",
            "content": (
                "<tool_use_error>Error: No such tool available: "
                "mcp__corsia__read_fil</tool_use_error>"
            ),
            "is_error": True,
        })
        assert live.tool_step_ids == {}
        assert live.tools_settled.is_set()
        assert child_db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='tool'",
            (thread_id,),
        ).fetchone()[0] == 1

        await settle_missing_tool_results(
            live=live, child_id=thread_id, identity=identity,
            bcast=lambda _sid, _message: None, db=child_db,
        )
        assert child_db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='tool'",
            (thread_id,),
        ).fetchone()[0] == 1

    @pytest.mark.asyncio
    async def test_interrupt_after_mixed_step_keeps_the_real_corsia_result(
        self, child_db,
    ):
        """An undispatched call's row counts as answered when settling the rest.

        Otherwise the Step owes one result too many, and closing its CORSIA
        call writes a placeholder that contradicts the durable real result.
        """
        from core.turn_core import TurnIdentity
        from subagents.cc_event_tool_settlement import settle_missing_tool_results
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        thread_id = "cc-mixed-interrupt"
        _seed_parent(child_db, sid=thread_id)
        identity = TurnIdentity("turn-mixed-interrupt", 1)
        live = _Live(
            session=None, parent_thread_id=None, name="cc",
            corsia_turn=identity,
        )
        live.turn_active = True
        on_event = _make_on_event(
            live, thread_id, lambda _sid, _message: None, child_db,
        )

        await on_event({
            "kind": "assistant_step", "text": "", "reasoning": "",
            "tool_uses": [
                {"id": "corsia-read", "name": "mcp__corsia__read_file",
                 "input": {"path": "a"}},
                {"id": "bare-read", "name": "read_file", "input": {"path": "b"}},
            ],
        })
        persist_bridge_tool_result(
            child_db, live, thread_id=thread_id,
            provider_tool_call_id="corsia-read", content="real A",
        )
        assert set(live.tool_step_ids) == {"corsia-read"}
        await settle_missing_tool_results(
            live=live, child_id=thread_id, identity=identity,
            bcast=lambda _sid, _message: None, db=child_db,
        )

        reasons = sorted(
            str(json.loads(row["metadata"]).get("synthetic_reason"))
            for row in child_db.execute(
                "SELECT metadata FROM messages WHERE thread_id=? AND role='tool'",
                (thread_id,),
            ).fetchall()
        )
        assert reasons == ["None", "undispatched_tool"]
        assert live.tool_step_ids == {}
        assert live.tools_settled.is_set()

    @pytest.mark.asyncio
    async def test_prepared_reconstructed_glob_passes_existing_runtime_guard(
        self,
        child_db,
        tmp_path,
        monkeypatch,
    ):
        from core.context_replay import ReplaySelection
        from history.types import (
            HistoryBoundary,
            PortableHistoryProjection,
            PortableMessage,
            PortableTurn,
        )
        from core.turn_core import TurnIdentity
        from subagents import cc_runtime
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        thread_id = "cc-reconstructed-glob"
        _seed_parent(child_db, sid=thread_id)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc-home"))
        child_db.execute(
            "UPDATE threads SET model=? WHERE id=?",
            ("claude-code/opus", thread_id),
        )
        child_db.commit()
        boundary = HistoryBoundary(
            thread_id=thread_id,
            history_epoch=0,
            seq=3,
            message_id="historical-glob-result",
        )
        projection = PortableHistoryProjection(
            format_version=1,
            thread_id=thread_id,
            history_epoch=0,
            start_exclusive=None,
            sealed_boundary=boundary,
            turns=(
                PortableTurn(
                    turn_id="historical-codex-turn",
                    status="completed",
                    messages=(
                        PortableMessage(
                            message_id="historical-user",
                            seq=1,
                            turn_id="historical-codex-turn",
                            role="user",
                            content="find python files",
                            created_at="2026-07-21T00:00:01Z",
                        ),
                        PortableMessage(
                            message_id="historical-assistant",
                            seq=2,
                            turn_id="historical-codex-turn",
                            role="assistant",
                            content="",
                            created_at="2026-07-21T00:00:02Z",
                            tool_calls=(
                                {
                                    "id": "historical-glob-call",
                                    "function": {
                                        "name": "glob",
                                        "arguments": '{"pattern":"**/*.py"}',
                                    },
                                },
                            ),
                        ),
                        PortableMessage(
                            message_id="historical-glob-result",
                            seq=3,
                            turn_id="historical-codex-turn",
                            role="tool",
                            content="backend/app.py",
                            created_at="2026-07-21T00:00:03Z",
                            assistant_step_id="historical-assistant",
                            tool_call_id="historical-glob-call",
                            tool_name="glob",
                        ),
                    ),
                    boundary=boundary,
                ),
            ),
            full_raw_replay_available=True,
        )
        selection = ReplaySelection(
            mode="exact",
            history_epoch=0,
            sealed_boundary=boundary,
            tail=projection,
        )

        destination = cc_runtime.prepare_cc_destination_payload(
            thread_id,
            target=cc_target("opus"),
            current_user_text="continue",
            selection=selection,
            provider_resume=False,
            is_subagent=False,
            db=child_db,
        )
        assistant = next(
            message for message in destination.history
            if message.get("role") == "assistant"
        )
        wire_call = next(
            block for block in assistant["content"]
            if block.get("type") == "tool_use"
        )
        assert wire_call["id"] == "historical-glob-call"
        assert wire_call["name"] == "mcp__corsia__glob"
        assert destination.transcript is not None
        assert '"tool_use_id":"historical-glob-call"' in (
            destination.transcript.content
        )

        identity = TurnIdentity("next-turn", 1)
        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
            corsia_turn=identity,
        )
        live.turn_active = True
        on_event = _make_on_event(
            live,
            thread_id,
            lambda _sid, _message: None,
            child_db,
        )

        await on_event({
            "kind": "assistant_step",
            "text": "",
            "reasoning": "",
            "tool_uses": [
                {
                    "id": "next-glob-call",
                    "name": wire_call["name"],
                    "input": {"pattern": "backend/**/*.py"},
                },
            ],
        })

        assert live.provider_context_untrusted is False
        assert live.tool_names["next-glob-call"] == "mcp__corsia__glob"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_call",
        [
            {
                "id": None,
                "name": "mcp__corsia__read_file",
                "input": {},
            },
            {
                "id": "malformed-input",
                "name": "mcp__corsia__read_file",
                "input": [],
            },
            {
                "id": "normalized-malformed",
                "name": "mcp__corsia__read_file",
                "input": {},
                "malformed": True,
            },
        ],
    )
    async def test_malformed_corsia_tool_fails_through_boundary_path(
        self,
        tool_call,
    ):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(session=None, parent_thread_id=None, name="cc")
        live.turn_active = True
        with patch(
            "subagents.cc_event_steps.persist_assistant_step",
        ) as persist_step, patch(
            "subagents.cc_event_simple.insert_message",
            return_value="boundary-marker",
        ), patch(
            "subagents.cc_event_simple.broadcast_stored_message",
            side_effect=_broadcast_insert_stub,
        ):
            on_event = _make_on_event(
                live,
                "cc-sid",
                lambda _sid, _message: None,
                db=None,
            )
            with pytest.raises(RuntimeError, match="capability boundary"):
                await on_event({
                    "kind": "assistant_step",
                    "text": "",
                    "reasoning": "",
                    "tool_uses": [tool_call],
                })

        assert live.provider_context_untrusted is True
        assert live.tool_boundary_marker_id == "boundary-marker"
        persist_step.assert_not_called()

    @pytest.mark.asyncio
    async def test_boundary_marker_identity_survives_broadcast_failure(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(session=None, parent_thread_id=None, name="cc")

        def broken_broadcast(_sid, _message):
            raise RuntimeError("broadcast unavailable")

        # Exercise the caller's failure handling while isolating durable projection.

        with patch(
            "subagents.cc_event_simple.insert_message",
            return_value="boundary-marker",
        ) as insert, patch(
            "subagents.cc_event_simple.broadcast_stored_message",
            side_effect=_broadcast_insert_stub,
        ):
            on_event = _make_on_event(
                live,
                "cc-sid",
                broken_broadcast,
                db=None,
            )
            with pytest.raises(RuntimeError, match="broadcast unavailable"):
                await on_event({
                    "kind": "tool_boundary_violation",
                    "capability": "hook",
                })

        assert insert.call_count == 1
        assert live.tool_boundary_violation_recorded is True
        assert live.tool_boundary_marker_id == "boundary-marker"

    @staticmethod
    def _write_refusing_claude(
        path, *, after_refusal: str, tool_name: str = "Read",
    ) -> str:
        """Fake Claude Code that refuses a call to ``tool_name`` like the real CLI."""
        path.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json
            import sys
            import time

            def emit(payload):
                sys.stdout.write(json.dumps(payload) + "\\n")
                sys.stdout.flush()

            emit({{
                "type": "system",
                "subtype": "init",
                "session_id": "fake-refusal",
                "model": "m",
                "tools": [],
                "mcp_servers": [],
                "cwd": ".",
            }})
            if sys.stdin.readline():
                time.sleep(0.2)
                emit({{
                    "type": "assistant",
                    "message": {{
                        "content": [{{
                            "type": "tool_use",
                            "id": "native-read",
                            "name": {tool_name!r},
                            "input": {{
                                "file_path": "/Users/example/.ssh/id_ed25519",
                            }},
                        }}],
                    }},
                }})
                emit({{
                    "type": "user",
                    "message": {{"content": [{{
                        "type": "tool_result",
                        "tool_use_id": "native-read",
                        "content": "<tool_use_error>Error: No such tool "
                                   "available: {tool_name}</tool_use_error>",
                        "is_error": True,
                    }}]}},
                }})
                time.sleep(0.2)
                {after_refusal}
            while sys.stdin.readline():
                pass
        """))
        path.chmod(
            path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        )
        return str(path)

    @pytest.mark.asyncio
    async def test_non_corsia_tool_call_settles_and_live_handle_survives(
        self,
        child_db,
        tmp_path,
    ):
        import subagents.cc_runtime as rt

        _seed_parent(child_db)
        binary = self._write_refusing_claude(
            tmp_path / "fake-claude-refusal",
            after_refusal=(
                'emit({"type": "result", "subtype": "success", '
                '"is_error": False, "result": "done", "total_cost_usd": 0.0, '
                '"num_turns": 1, "stop_reason": "end_turn"})'
            ),
        )

        casts: list[tuple[str, dict]] = []
        child_id = await _spawn(
            "parent",
            "attempt native read",
            binary=binary,
            model="opus",
            broadcast=lambda sid, message: casts.append((sid, message)),
        )
        live = rt._LIVE.get(child_id)
        assert live is not None

        def completed() -> bool:
            return any(
                sid == child_id and message.get("type") == "turn_complete"
                for sid, message in casts
            )

        assert await _wait(completed)
        assert rt._LIVE.get(child_id) is live
        assert live.session.returncode is None
        assert live.provider_context_untrusted is False

        rows = child_db.execute(
            "SELECT role, content, metadata FROM messages WHERE thread_id=?",
            (child_id,),
        ).fetchall()
        metadata = [json.loads(row["metadata"] or "{}") for row in rows]
        assert [
            meta["is_error"] for meta in metadata if meta.get("type") == "result"
        ] == [False]
        assert all(
            meta.get("type") != "tool_boundary_violation" for meta in metadata
        )
        tool_rows = [row for row in rows if row["role"] == "tool"]
        assert len(tool_rows) == 1
        tool_meta = json.loads(tool_rows[0]["metadata"])
        assert tool_meta["synthetic_reason"] == "undispatched_tool"
        assert tool_meta["is_error"] is True
        assert tool_meta["name"] == "Read"
        await rt.retire(child_id)

    @pytest.mark.asyncio
    async def test_unserved_corsia_name_settles_as_one_tool_error(
        self,
        child_db,
        tmp_path,
        caplog,
    ):
        """A misspelled ``mcp__corsia__`` name is refused by Claude Code alone.

        The name is not in the catalog the live handle serves, so the bridge
        can never see it. It settles as one error result at step time: the echo
        binds to that row, nothing is logged as a lost result, and the handle
        stays trusted rather than rebuilding its provider context.
        """
        import subagents.cc_runtime as rt

        _seed_parent(child_db)
        binary = self._write_refusing_claude(
            tmp_path / "fake-claude-misspelled",
            tool_name="mcp__corsia__read_fil",
            after_refusal=(
                'emit({"type": "result", "subtype": "success", '
                '"is_error": False, "result": "done", "total_cost_usd": 0.0, '
                '"num_turns": 1, "stop_reason": "end_turn"})'
            ),
        )

        casts: list[tuple[str, dict]] = []
        with caplog.at_level("ERROR"):
            child_id = await _spawn(
                "parent",
                "misspell a corsia tool",
                binary=binary,
                model="opus",
                broadcast=lambda sid, message: casts.append((sid, message)),
            )
            live = rt._LIVE.get(child_id)
            assert live is not None
            assert live.tool_definitions is not None

            assert await _wait(lambda: any(
                sid == child_id and message.get("type") == "turn_complete"
                for sid, message in casts
            ))
        assert rt._LIVE.get(child_id) is live
        assert live.session.returncode is None
        assert live.provider_context_untrusted is False
        assert "could not be bound" not in caplog.text

        tool_rows = [
            row for row in child_db.execute(
                "SELECT content, metadata FROM messages "
                "WHERE thread_id=? AND role='tool'",
                (child_id,),
            ).fetchall()
        ]
        assert len(tool_rows) == 1
        tool_meta = json.loads(tool_rows[0]["metadata"])
        assert tool_meta["synthetic_reason"] == "undispatched_tool"
        assert tool_meta["is_error"] is True
        assert tool_meta["name"] == "mcp__corsia__read_fil"
        assert "No such tool available" not in tool_rows[0]["content"]
        await rt.retire(child_id)

    @pytest.mark.asyncio
    async def test_non_corsia_tool_call_then_unrelated_exit_reports_real_exit(
        self,
        child_db,
        tmp_path,
    ):
        import subagents.cc_runtime as rt

        _seed_parent(child_db)
        binary = self._write_refusing_claude(
            tmp_path / "fake-claude-refusal-then-crash",
            after_refusal=(
                'sys.stderr.write("provider crash: out of memory\\n"); '
                "sys.stderr.flush(); sys.exit(7)"
            ),
        )

        child_id = await _spawn(
            "parent",
            "attempt native read then crash",
            binary=binary,
            model="opus",
            broadcast=lambda _sid, _message: None,
        )

        def terminalized() -> bool:
            turn = child_db.execute(
                "SELECT status FROM active_turns WHERE thread_id=? "
                "ORDER BY turn_order DESC LIMIT 1",
                (child_id,),
            ).fetchone()
            return (
                rt._LIVE.get(child_id) is None
                and turn is not None
                and turn["status"] == "error"
            )

        assert await _wait(terminalized)
        turn = child_db.execute(
            "SELECT error FROM active_turns WHERE thread_id=? "
            "ORDER BY turn_order DESC LIMIT 1",
            (child_id,),
        ).fetchone()
        assert "capability boundary" not in turn["error"]
        assert "out of memory" in turn["error"]
        rows = child_db.execute(
            "SELECT content, metadata FROM messages WHERE thread_id=?",
            (child_id,),
        ).fetchall()
        metadata = [json.loads(row["metadata"] or "{}") for row in rows]
        assert all(
            meta.get("type") != "tool_boundary_violation" for meta in metadata
        )
        exit_rows = [
            row for row, meta in zip(rows, metadata)
            if meta.get("cause") == "process_exited"
        ]
        assert len(exit_rows) == 1
        assert "out of memory" in exit_rows[0]["content"]

    @pytest.mark.asyncio
    async def test_init_with_descriptor_only_plugins_keeps_handle_trusted(self):
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _Live

        live = _Live(
            session=None,
            parent_thread_id=None,
            name="cc",
        )

        with patch("subagents.cc_event_simple.insert_message") as insert:
            on_event = _make_on_event(
                live,
                "cc-sid",
                lambda _sid, _message: None,
                db=None,
            )
            await on_event({
                "kind": "init",
                "tools": ["mcp__corsia__read_file"],
                "mcp_servers": [{"name": "corsia", "status": "connected"}],
                "plugins": [{
                    "name": "superpowers",
                    "source": "superpowers@claude-plugins-official",
                }],
                "skills": [],
                "model": "claude-opus",
            })

        assert live.provider_context_untrusted is False
        insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_next_feed_does_not_preempt_or_replace_a_quarantined_live(self):
        import subagents.cc_runtime as rt
        from subagents.cc_runtime_state import _Live

        old_live = _Live(
            session=SimpleNamespace(returncode=None),
            parent_thread_id=None,
            name="cc",
            model="opus",
            provider_context_untrusted=True,
            turn_active=True,
        )
        rt._LIVE.reset()
        rt._LIVE.put("cc-sid", old_live)
        release = asyncio.Event()
        admitted_task = asyncio.create_task(release.wait())
        old_live.admitted_tool_tasks.add(admitted_task)
        close = AsyncMock(return_value=False)
        preempt = AsyncMock(return_value=None)
        resume = AsyncMock(return_value=SimpleNamespace())

        try:
            with patch("subagents.cc_feed_transition.close_and_finalize", close):
                with patch(
                    "subagents.cc_feed_transition.replay_pending_exit_before_preempt",
                    preempt,
                ):
                    with patch(
                        "subagents.cc_feed_transition.resume_child", resume,
                    ):
                        with pytest.raises(rt.CCLiveTurnBusy):
                            await rt.ensure_live_then_feed(
                                "cc-sid",
                                "continue",
                                target=cc_target("opus"),
                                turn_id="turn-cc-quarantined-live",
                                record=False,
                                db=None,
                                prepared_context=SimpleNamespace(
                                    mode="provider_resume",
                                    runtime_context_epoch=0,
                                ),
                            )
            assert rt._LIVE.get("cc-sid") is old_live
            close.assert_not_awaited()
            preempt.assert_not_awaited()
            resume.assert_not_awaited()
        finally:
            old_live.admitted_tool_tasks.discard(admitted_task)
            release.set()
            await asyncio.wait_for(admitted_task, timeout=1)
            rt._LIVE.reset()

    @pytest.mark.asyncio
    async def test_active_compaction_defers_until_current_turn_is_terminal(self):
        import subagents.cc_runtime as rt
        from core.turn_core import TurnIdentity
        from subagents.cc_runtime_state import _Live

        old_live = _Live(
            session=SimpleNamespace(returncode=None),
            parent_thread_id=None,
            name="cc",
            model="opus",
            provider_context_untrusted=True,
            turn_active=True,
        )
        new_live = _Live(
            session=SimpleNamespace(returncode=None),
            parent_thread_id=None,
            name="cc",
            model="opus",
        )
        rt._LIVE.reset()
        rt._LIVE.put("cc-sid", old_live)
        close = AsyncMock(return_value=True)
        resume = AsyncMock(return_value=new_live)
        accept = AsyncMock(return_value=TurnIdentity("next-turn", 2))
        with patch("subagents.cc_feed_transition.close_and_finalize", close):
            with patch("subagents.cc_feed_transition.resume_child", resume):
                with patch(
                    "subagents.cc_feed.accept_reconstructed_input",
                    accept,
                ):
                    with pytest.raises(rt.CCLiveTurnBusy):
                        await rt.ensure_live_then_feed(
                            "cc-sid",
                            "continue",
                            target=cc_target("opus"),
                            turn_id="turn-cc-active-compaction",
                            record=False,
                            db=None,
                            prepared_context=SimpleNamespace(
                                mode="provider_resume",
                                runtime_context_epoch=0,
                            ),
                        )

        close.assert_not_awaited()
        resume.assert_not_awaited()
        accept.assert_not_awaited()
        rt._LIVE.reset()

    @pytest.mark.asyncio
    async def test_trusted_resumed_feed_sends_turn_only_body(self, child_db):
        import subagents.cc_runtime as rt
        from subagents.cc_runtime_state import _Live

        thread_id = "cc-trusted-resumed-turn"
        _seed_parent(child_db, sid=thread_id)
        child_db.execute(
            "UPDATE threads SET model='claude-code/opus' WHERE id=?",
            (thread_id,),
        )
        child_db.commit()

        session = SimpleNamespace(
            returncode=None,
            close=AsyncMock(),
            feed=AsyncMock(),
        )

        async def _acknowledge_feed(_text, *, on_write_observed, on_accepted):
            on_write_observed()
            on_accepted()

        session.feed.side_effect = _acknowledge_feed
        live = _Live(
            session=session,
            parent_thread_id=None,
            name="cc",
            model="opus",
        )
        rt._LIVE.reset()
        rt._LIVE.put(thread_id, live)

        fake_snapshot = SimpleNamespace(to_dict=lambda: {"snapshot_id": "ctx-turn"})
        fake_frame = SimpleNamespace(
            snapshot_id="ctx-turn",
            snapshot=fake_snapshot,
            continuity_accounting=ContinuityAccounting.not_delivered(),
            frame_text=(
                "--- BEGIN CORSIA CONTEXT SOURCES ---\n"
                "prefix that must not be sent on a trusted resumed turn\n"
                "--- END CORSIA CONTEXT SOURCES ---\n\n"
                "--- BEGIN CORSIA EFFECTIVE CONTEXT DIGEST ---\n{}\n"
                "--- END CORSIA EFFECTIVE CONTEXT DIGEST ---\n\n"
                "User message:\ncontinue"
            ),
            turn_text="continue",
        )

        with patch(
            "subagents.cc_feed.build_cc_user_frame",
            return_value=fake_frame,
        ):
            with patch("subagents.cc_feed_transition.require_cc_provider_fit"):
                with patch("subagents.cc_replay_authority.require_cc_provider_fit"):
                    result = await _ensure_feed(
                        thread_id,
                        "continue",
                        turn_id="turn-cc-trusted-resume",
                        model="opus",
                        record=True,
                        db=None,
                        prepared_context=SimpleNamespace(
                            mode="provider_resume",
                            runtime_context_epoch=0,
                            fit_ledger=SimpleNamespace(rendered_input_tokens=100),
                        ),
                    )

        assert result is not None
        session.feed.assert_awaited_once_with(
            "continue", on_write_observed=ANY, on_accepted=ANY,
        )
        assert child_db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=? "
            "AND role='user' AND content='continue'",
            (thread_id,),
        ).fetchone()[0] == 1
        rt._LIVE.reset()
