from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from subagents import codex_runtime_state, codex_runtime_terminal

from codex_runtime_fixtures import (
    _admit_active_turn,
    _codex_target,
    _make_mock_app_session,
    _reset_runtime,
    _run,
    _seed_runtime_sessions,
)


def _successor_turn_id() -> str:
    return str(uuid4())


async def _bind_input_turn(_turn_id: str, _expected_turn_id: str | None) -> None:
    """Model the send-service row binding at this runtime unit boundary."""


async def _await_terminal_event(on_event, live, event: dict) -> None:
    """Wait for the terminal task without making the JSON-RPC reader do so."""
    await on_event(event)
    if live.delivery_event_tail is not None:
        await live.delivery_event_tail


def _run_terminal_event(on_event, live, event: dict) -> None:
    _run(_await_terminal_event(on_event, live, event))


def test_preserve_committed_terminal_distinguishes_applied_results() -> None:
    """Publication errors carry only an already-applied terminal outcome."""
    from core.turn_core import TurnTerminalResult
    from subagents.codex_runtime_terminal import (
        CommittedTerminalFailure,
        preserve_committed_terminal,
    )

    publication_error = RuntimeError("publication failed")
    with pytest.raises(CommittedTerminalFailure) as raised:
        with preserve_committed_terminal(TurnTerminalResult.APPLIED):
            raise publication_error
    assert raised.value.settlement is None
    assert raised.value.publication_error is publication_error

    precommit_error = RuntimeError("pre-commit failure")
    with pytest.raises(RuntimeError) as raised:
        with preserve_committed_terminal(TurnTerminalResult.CONFLICTING_TERMINAL):
            raise precommit_error
    assert raised.value is precommit_error


@pytest.fixture(autouse=True)
def _applied_terminal_owner(monkeypatch):
    """These adapter-effect units run behind an already-proved terminal CAS."""
    from core.turn_core import TurnTerminalResult
    from subagents import codex_runtime, codex_runtime_events

    original = codex_runtime._make_on_event

    def _owned_event(live, thread_id, bcast, **kwargs):
        from core.turn_core import TurnIdentity

        live.corsia_turn = live.corsia_turn or TurnIdentity(
            f"test-{thread_id}",
            1,
        )
        return original(live, thread_id, bcast, **kwargs)

    async def commit_turn_terminal(*_args, **_kwargs):
        from infra.thread_driver import TerminalSettlement

        return TerminalSettlement(TurnTerminalResult.APPLIED, None)

    async def drive_turn_settlement(settlement):
        return settlement.result

    monkeypatch.setattr(codex_runtime, "_make_on_event", _owned_event)
    monkeypatch.setattr(
        codex_runtime_events,
        "commit_turn_terminal",
        commit_turn_terminal,
    )
    monkeypatch.setattr(
        codex_runtime_terminal,
        "drive_turn_settlement",
        drive_turn_settlement,
    )


def test_trusted_occupancy_handle_rejects_thread_only_fallback():
    from subagents import codex_runtime

    _reset_runtime()
    try:
        codex_runtime._CODEX_THREADS["occupancy-session"] = "provider-thread"

        fallback = codex_runtime.current_handle_fingerprint("occupancy-session")

        assert fallback is not None
        assert fallback.session_identity is None
        assert (
            codex_runtime_state.trusted_handle_fingerprint("occupancy-session") is None
        )
    finally:
        _reset_runtime()


def test_trusted_occupancy_handle_accepts_exact_idle_app_session():
    from subagents import codex_runtime

    _reset_runtime()
    try:
        session = SimpleNamespace(
            thread_id="provider-thread",
            catalog_model_id="codex/gpt-5-codex",
            model="gpt-5-codex",
            expected_reported_model="gpt-5-codex",
            provider_reported_model="gpt-5-codex",
            working_dir="/tmp/codex",
            runtime_context_epoch=5,
            returncode=None,
            transport_generation_id=("11111111-1111-4111-8111-111111111111"),
        )
        codex_runtime._APP_SESSIONS["occupancy-session"] = session
        codex_runtime._CODEX_THREADS["occupancy-session"] = "provider-thread"

        fingerprint = codex_runtime_state.trusted_handle_fingerprint(
            "occupancy-session",
        )

        assert fingerprint is not None
        assert fingerprint.codex_thread_id == "provider-thread"
        assert not hasattr(fingerprint, "session_identity")
        assert fingerprint.catalog_model_id == "codex/gpt-5-codex"
        assert fingerprint.runtime_context_epoch == 5
    finally:
        _reset_runtime()


@pytest.mark.asyncio
async def test_codex_checkpoint_identity_is_durable_after_backend_restart(
    isolated_db,
):
    from infra.database import get_db
    from registry.models import find_for_thread
    from subagents.codex_runtime_event_delivery import runtime_event_handler
    from subagents.codex_runtime_state import _CODEX_THREADS

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','codex/test','t0','t0')"
    )
    db.commit()
    live = SimpleNamespace(
        codex_thread_id="", session=SimpleNamespace(effort="low"),
        delivery_event_gate=None,
    )
    db.close()
    handler = runtime_event_handler(
        live, "thread-1", lambda *_args: None,
        checkpoint_generation_id="checkpoint-7",
    )
    await handler({
        "kind": "thread_started",
        "thread_id": "provider-thread",
        "rollout_path": "/tmp/rollout.jsonl",
    })
    _CODEX_THREADS.clear()

    restarted = get_db()
    row = find_for_thread("thread-1", "codex", "thread", db=restarted)
    restarted.close()
    assert json.loads(row["metadata"])["checkpoint_generation_id"] == "checkpoint-7"


def test_codex_thread_registration_is_atomic_and_loud(
    isolated_db, monkeypatch,
) -> None:
    """Identity and checkpoint metadata commit together or not at all."""
    from infra.database import get_db
    from registry import models
    from registry.service import register_codex_thread

    db = get_db()

    def fail_metadata(*_args, **_kwargs):
        raise RuntimeError("metadata write failed")

    monkeypatch.setattr(models, "update_metadata", fail_metadata)
    with pytest.raises(RuntimeError, match="metadata write failed"):
        register_codex_thread(
            "thread-1",
            "provider-thread",
            checkpoint_generation_id="checkpoint-7",
            db=db,
        )

    row = db.execute(
        "SELECT 1 FROM artifact_identities "
        "WHERE integration='codex' AND type='thread' "
        "AND external_id='provider-thread'"
    ).fetchone()
    db.close()
    assert row is None


def test_occupancy_fingerprint_is_stable_when_app_session_becomes_idle():
    from infra.context_receipts import producer_handle_fingerprint
    from subagents import codex_runtime
    from subagents.codex_runtime_spawn import stamp_app_server_context

    _reset_runtime()
    try:
        session = SimpleNamespace(
            thread_id="provider-thread",
            model="gpt-5-codex",
            working_dir="/tmp/codex",
            returncode=None,
            transport_generation_id=("11111111-1111-4111-8111-111111111111"),
        )
        snapshot = {"snapshot_id": "ctx-a"}
        stamp_app_server_context(
            session,
            SimpleNamespace(
                catalog_model_id="codex/gpt-5-codex",
                runtime_context_epoch=7,
                expected_reported_model="gpt-5-codex",
                context_snapshot=snapshot,
            ),
        )
        live = codex_runtime._LiveCodex(
            codex_thread_id="provider-thread",
            session=session,
            parent_thread_id=None,
            name="codex",
            model="gpt-5-codex",
            catalog_model_id="codex/gpt-5-codex",
            runtime_context_epoch=7,
            context_snapshot_id="ctx-a",
            context_snapshot=snapshot,
        )
        codex_runtime._LIVE.put("occupancy-session", live)
        codex_runtime._APP_SESSIONS["occupancy-session"] = session
        codex_runtime._CODEX_THREADS["occupancy-session"] = "provider-thread"

        live_handle = codex_runtime_state.trusted_handle_fingerprint(
            "occupancy-session",
        )
        receipt_identity = codex_runtime_state.context_receipt_identity(
            "occupancy-session",
            live,
        )
        codex_runtime._LIVE.pop("occupancy-session")
        idle_handle = codex_runtime_state.trusted_handle_fingerprint(
            "occupancy-session",
        )

        assert live_handle is not None
        assert idle_handle is not None
        assert live_handle.context_snapshot_id == "ctx-a"
        assert idle_handle.context_snapshot_id == "ctx-a"
        assert producer_handle_fingerprint(live_handle) == (
            producer_handle_fingerprint(idle_handle)
        )
        assert receipt_identity is not None
        assert receipt_identity.producer_handle_fingerprint == (
            producer_handle_fingerprint(idle_handle)
        )
    finally:
        _reset_runtime()


@pytest.mark.parametrize("invalid_epoch", [None, True])
def test_live_occupancy_fingerprint_rejects_invalid_runtime_epoch(invalid_epoch):
    from subagents import codex_runtime

    _reset_runtime()
    try:
        session = SimpleNamespace(
            thread_id="provider-thread",
            catalog_model_id="codex/gpt-5-codex",
            model="gpt-5-codex",
            working_dir="/tmp/codex",
            returncode=None,
            transport_generation_id=("11111111-1111-4111-8111-111111111111"),
        )
        live = codex_runtime._LiveCodex(
            codex_thread_id="provider-thread",
            session=session,
            parent_thread_id=None,
            name="codex",
            catalog_model_id="codex/gpt-5-codex",
            runtime_context_epoch=invalid_epoch,
        )
        codex_runtime._LIVE.put("occupancy-session", live)

        assert (
            codex_runtime_state.trusted_handle_fingerprint("occupancy-session")
            is None
        )
    finally:
        _reset_runtime()


@pytest.mark.parametrize(
    "session_overrides",
    [
        {"returncode": 1},
    ],
    ids=["dead"],
)
def test_trusted_occupancy_handle_rejects_ineligible_idle_transport(
    session_overrides,
):
    from subagents import codex_runtime

    _reset_runtime()
    try:
        values = {
            "thread_id": "provider-thread",
            "catalog_model_id": "codex/gpt-5-codex",
            "model": "gpt-5-codex",
            "working_dir": "/tmp/codex",
            "runtime_context_epoch": 5,
            "returncode": None,
            "transport_generation_id": ("11111111-1111-4111-8111-111111111111"),
        }
        values.update(session_overrides)
        codex_runtime._APP_SESSIONS["occupancy-session"] = SimpleNamespace(
            **values,
        )
        codex_runtime._CODEX_THREADS["occupancy-session"] = "provider-thread"

        assert (
            codex_runtime_state.trusted_handle_fingerprint(
                "occupancy-session",
            )
            is None
        )
    finally:
        _reset_runtime()


def test_provider_occupancy_identity_tracks_context_not_app_transport():
    from infra.context_receipts import producer_handle_fingerprint
    from subagents import codex_runtime

    def session(token: str, snapshot_id: str = "ctx-a"):
        return SimpleNamespace(
            thread_id="provider-thread",
            catalog_model_id="codex/gpt-5-codex",
            model="gpt-5-codex",
            working_dir="/tmp/codex",
            runtime_context_epoch=5,
            context_snapshot_id=snapshot_id,
            returncode=None,
            transport_generation_id=token,
        )

    _reset_runtime()
    try:
        codex_runtime._APP_SESSIONS["occupancy-session"] = session(
            "11111111-1111-4111-8111-111111111111",
        )
        codex_runtime._CODEX_THREADS["occupancy-session"] = "provider-thread"
        original = codex_runtime_state.trusted_handle_fingerprint(
            "occupancy-session",
        )
        codex_runtime._APP_SESSIONS["occupancy-session"] = session(
            "22222222-2222-4222-8222-222222222222",
        )
        replacement = codex_runtime_state.trusted_handle_fingerprint(
            "occupancy-session",
        )

        assert original is not None
        assert replacement is not None
        assert producer_handle_fingerprint(original) == (
            producer_handle_fingerprint(replacement)
        )

        codex_runtime._APP_SESSIONS["occupancy-session"] = session(
            "33333333-3333-4333-8333-333333333333",
            "ctx-b",
        )
        changed_context = codex_runtime_state.trusted_handle_fingerprint(
            "occupancy-session",
        )
        assert changed_context is not None
        assert producer_handle_fingerprint(changed_context) != (
            producer_handle_fingerprint(replacement)
        )

        changed = session("33333333-3333-4333-8333-333333333333")
        changed.codex_thread_id = "different-provider-thread"
        codex_runtime._APP_SESSIONS["occupancy-session"] = changed
        codex_runtime._CODEX_THREADS["occupancy-session"] = (
            "different-provider-thread"
        )
        other = codex_runtime_state.trusted_handle_fingerprint(
            "occupancy-session"
        )
        assert other is not None
        assert producer_handle_fingerprint(other) != (
            producer_handle_fingerprint(replacement)
        )
    finally:
        _reset_runtime()


class TestDeadGuard:
    def setup_method(self):
        _reset_runtime()

    def test_mark_dead_adds_to_dead_set(self):
        from subagents import codex_runtime

        _run(codex_runtime.mark_dead("sess-to-die"))
        assert "sess-to-die" in codex_runtime._DEAD

    def test_mark_dead_removes_from_live(self):
        from subagents import codex_runtime

        codex_runtime._LIVE.put(
            "sess-dying",
            codex_runtime._LiveCodex(
                codex_thread_id="thread_dead",
                session=None,
                parent_thread_id=None,
                name="test",
            ),
        )
        codex_runtime._CODEX_THREADS["sess-dying"] = "thread_dead"
        _run(codex_runtime.mark_dead("sess-dying"))
        assert "sess-dying" not in codex_runtime._LIVE
        assert "sess-dying" not in codex_runtime._CODEX_THREADS

    def test_mark_dead_terminates_live_session(self):
        from subagents import codex_runtime

        mock_session = _make_mock_app_session("thread_term")
        codex_runtime._LIVE.put(
            "term-sess",
            codex_runtime._LiveCodex(
                codex_thread_id="thread_term",
                session=mock_session,
                parent_thread_id=None,
                name="test",
            ),
        )

        _run(codex_runtime.mark_dead("term-sess"))

        mock_session.terminate.assert_called_once()
        assert "term-sess" not in codex_runtime._LIVE
        assert "term-sess" in codex_runtime._DEAD

    def test_mark_dead_terminates_idle_app_server_session(self):
        from subagents import codex_runtime

        app_session = _make_mock_app_session("thread_idle_dead")
        codex_runtime._APP_SESSIONS["idle-dead"] = app_session
        codex_runtime._CODEX_THREADS["idle-dead"] = "thread_idle_dead"

        _run(codex_runtime.mark_dead("idle-dead"))

        app_session.terminate.assert_awaited_once()
        assert "idle-dead" not in codex_runtime._APP_SESSIONS
        assert "idle-dead" not in codex_runtime._CODEX_THREADS
        assert "idle-dead" in codex_runtime._DEAD

    def test_mark_dead_retains_unterminable_app_guards(self):
        from subagents import codex_runtime

        session = object()
        codex_runtime._APP_SESSIONS["unprovable-dead"] = session
        codex_runtime._CODEX_THREADS["unprovable-dead"] = "thread_unprovable"

        _run(codex_runtime.mark_dead("unprovable-dead"))

        assert codex_runtime._APP_SESSIONS["unprovable-dead"] is session
        assert codex_runtime._CODEX_THREADS["unprovable-dead"] == "thread_unprovable"
        assert "unprovable-dead" in codex_runtime._DEAD

    def test_dead_session_never_accepts_feed(self):
        from subagents import codex_runtime

        codex_runtime._DEAD.add("perm-dead")
        codex_runtime._CODEX_THREADS["perm-dead"] = "thread_xxx"
        assert (
            _run(
                codex_runtime.feed(
                    "perm-dead",
                    "retry",
                    target=_codex_target(),
                    successor_turn_id=_successor_turn_id(),
                    bind_input_turn=_bind_input_turn,
                )
            )
            is None
        )

    def test_dead_session_never_accepts_feed_main(self):
        from subagents import codex_runtime

        codex_runtime._DEAD.add("perm-dead-main")
        assert (
            _run(
                codex_runtime.feed_main(
                    "perm-dead-main",
                    "retry",
                    target=_codex_target(),
                    successor_turn_id=_successor_turn_id(),
                    bind_input_turn=_bind_input_turn,
                )
            )
            is None
        )


class TestCauseMap:
    def setup_method(self):
        _reset_runtime()

    def _invoke_turn_failed_event(
        self, thread_id: str, cause: str, reset_at: str | None = None
    ) -> None:
        from subagents import codex_runtime

        live = codex_runtime._LiveCodex(
            codex_thread_id="thread_test",
            session=None,
            parent_thread_id=None,
            name="test",
        )
        bcast = MagicMock()

        with patch(
            "subagents.codex_runtime_events.insert_message", return_value="msg-id"
        ) as insert:
            on_event = codex_runtime._make_on_event(live, thread_id, bcast)
            ev = {"kind": "turn_failed", "cause": cause}
            if reset_at:
                ev["reset_at"] = reset_at
            _run_terminal_event(on_event, live, ev)
        self._last_insert_meta = insert.call_args.args[3]

    def test_rate_limited_does_not_mark_dead(self):
        from subagents import codex_runtime

        self._invoke_turn_failed_event(
            "rate-sess", "rate_limited", reset_at="2026-06-14T13:00:00Z"
        )
        assert "rate-sess" not in codex_runtime._DEAD
        assert self._last_insert_meta["event_type"] == "harness.error"
        assert self._last_insert_meta["harness"] == "codex"
        assert self._last_insert_meta["cause"] == "rate_limited"

    def test_quota_exhausted_marks_dead(self):
        from subagents import codex_runtime

        self._invoke_turn_failed_event("quota-sess", "quota_exhausted")
        assert "quota-sess" in codex_runtime._DEAD

    def test_auth_failed_marks_dead(self):
        from subagents import codex_runtime

        self._invoke_turn_failed_event("auth-sess", "auth_failed")
        assert "auth-sess" in codex_runtime._DEAD

    def test_process_exited_does_not_mark_dead(self):
        from subagents import codex_runtime

        self._invoke_turn_failed_event("crash-sess", "process_exited")
        assert "crash-sess" not in codex_runtime._DEAD

    def test_context_window_failure_marks_provider_context_untrusted(self):
        from subagents import codex_runtime

        codex_runtime._CODEX_THREADS["ctx-limit-sess"] = "thread_stale"
        self._invoke_turn_failed_event(
            "ctx-limit-sess",
            "context_window_exceeded",
        )

        assert "ctx-limit-sess" in codex_runtime._PROVIDER_CONTEXT_UNTRUSTED
        assert "ctx-limit-sess" not in codex_runtime._CODEX_THREADS
        assert "ctx-limit-sess" not in codex_runtime._DEAD
        assert self._last_insert_meta["cause"] == "context_window_exceeded"


class TestResultMarkers:
    def setup_method(self):
        _reset_runtime()

    def test_terminal_usage_attaches_one_compact_receipt_without_archive(
        self,
        main_db,
    ):
        from infra.queries import insert_message
        from subagents import codex_runtime

        main_db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('codex-receipt','test','codex/gpt-5-codex','t','t')"
        )
        insert_message(
            "codex-receipt",
            "assistant",
            "done",
            {"turn_id": "turn-codex-receipt"},
            msg_id="assistant-codex-receipt",
            db=main_db,
        )
        marker = "must-not-be-duplicated-" + ("x" * 10_000)
        session = SimpleNamespace(
            thread_id="provider-thread-receipt",
            returncode=None,
            transport_generation_id="11111111-1111-4111-8111-111111111111",
            working_dir="/tmp",
        )
        live = codex_runtime._LiveCodex(
            codex_thread_id="provider-thread-receipt",
            session=session,
            parent_thread_id=None,
            name="codex",
            model="gpt-5-codex",
            catalog_model_id="codex/gpt-5-codex",
            runtime_context_epoch=4,
            context_window=258_400,
            context_snapshot_id="ctx-codex-receipt",
            context_snapshot={
                "snapshot_id": "ctx-codex-receipt",
                "frozen_prompt_material": {"messages": [marker]},
            },
            source_artifacts=[{"payload": marker}],
        )
        payload: dict = {}

        _run(codex_runtime_terminal.attach_context_usage(
            thread_id="codex-receipt",
            live=live,
            message_id="assistant-codex-receipt",
            payload=payload,
            context_usage={
                "model_context_window": 258_400,
                "total_used": 100,
                "utilization": 100 / 258_400,
                "token_usage_available": True,
                "token_source": "measured",
            },
            db=main_db,
        ))

        metadata = json.loads(main_db.execute(
            "SELECT metadata FROM messages WHERE id='assistant-codex-receipt'"
        ).fetchone()["metadata"])
        receipt = metadata["context_usage"]
        assert payload["context_usage"]["context_snapshot"] == {
            "snapshot_id": "ctx-codex-receipt",
        }
        assert "context_snapshot" not in receipt
        assert receipt["harness"] == "codex"
        assert receipt["catalog_model_id"] == "codex/gpt-5-codex"
        assert receipt["runtime_context_epoch"] == 4
        assert len(receipt["producer_handle_fingerprint"]) == 64
        assert marker not in json.dumps(receipt)

    def test_app_server_request_keeps_source_truth_live(
        self,
        main_db,
    ):
        from subagents import codex_runtime

        main_db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('codex-live-context','test','codex/gpt-5-codex','t','t')"
        )
        main_db.commit()
        session = SimpleNamespace(request_log=[{
            "id": 1,
            "method": "turn/start",
            "params": {"threadId": "provider-thread"},
        }])
        live = codex_runtime._LiveCodex(
            codex_thread_id="provider-thread",
            session=session,
            parent_thread_id=None,
            name="codex",
            context_snapshot_id="ctx-codex-live-context",
            context_snapshot={"snapshot_id": "ctx-codex-live-context"},
        )

        codex_runtime._capture_app_server_request(
            live,
            session,
        )

        assert live.source_artifacts[0]["method"] == "turn/start"

    def test_result_marker_preserves_stop_reason(self):
        from subagents import codex_runtime

        live = codex_runtime._LiveCodex(
            codex_thread_id="thread_test",
            session=None,
            parent_thread_id=None,
            name="test",
        )
        bcast = MagicMock()

        with patch(
            "subagents.codex_runtime_events.insert_message", return_value="msg-id"
        ) as insert:
            on_event = codex_runtime._make_on_event(live, "interrupted-sess", bcast)
            _run_terminal_event(on_event, live, {
                        "kind": "result",
                        "result": "Codex turn interrupted.",
                        "is_error": False,
                        "stop_reason": "interrupted",
                    })

        meta = insert.call_args.args[3]
        assert meta["stop_reason"] == "interrupted"
        assert meta["is_error"] is False
        assert meta["turn_id"] == "test-interrupted-sess"

    def test_assistant_rows_carry_model_identity_and_context_receipt(self):
        from infra.context_receipts import context_usage_summary
        from core.token_estimation import _estimate_messages_tokens, _estimate_tokens
        from subagents import codex_runtime
        from subagents import codex_runtime_events

        history = [{"role": "assistant", "content": "historical codex context " * 20}]
        current_user = "fresh codex request"
        snapshot = {
            "snapshot_id": "ctx-codex-footer-proof",
            "harness_model_facts": {
                "harness": "codex",
                "model": "codex/gpt-5-codex",
                "model_context_window": 200000,
            },
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
            "delivery_leaves": [
                {
                    "harness": "codex",
                    "destination_mode": "thread-start-inject-resume",
                }
            ],
        }
        live = codex_runtime._LiveCodex(
            codex_thread_id="thread_test",
            session=SimpleNamespace(
                transport_generation_id=(
                    "11111111-1111-4111-8111-111111111111"
                ),
                returncode=None,
                working_dir=None,
                dynamic_tools=({"type": "function", "function": {"name": "read_file"}},),
            ),
            parent_thread_id=None,
            name="test",
            model="codex/gpt-5-codex",
            catalog_model_id="codex/gpt-5-codex",
            runtime_context_epoch=3,
            expected_reported_model="gpt-5-codex",
            provider_reported_model="gpt-5-codex",
            context_snapshot_id=snapshot["snapshot_id"],
            context_snapshot=snapshot,
        )
        bcast = MagicMock()
        attached = []

        async def fake_run_in_db(fn, *args, **kwargs):
            if fn.__name__ == "persist_turn_terminal":
                from core.turn_core import TurnTerminalResult

                return TurnTerminalResult.APPLIED
            if fn.__name__ == "persist_assistant_text":
                return "assistant-codex"
            if fn.__name__ == "load_assistant_step_display":
                return {
                    "id": "assistant-codex",
                    "content": "Hello",
                    "metadata": {
                        "codex": True,
                        "harness": "codex",
                        "model": "codex/gpt-5-codex",
                        "model_name": "GPT-5 Codex",
                        "provider_reported_model": "codex/gpt-5-codex",
                        "expected_provider_model": "codex/gpt-5-codex",
                        "model_identity_mismatch": False,
                    },
                    "created_at": "2026-07-14T00:00:00+00:00",
                }
            if fn.__name__ == "attach_context_receipt":
                attached.append((args, kwargs))
                identity = kwargs["identity"]
                return {
                    **context_usage_summary(kwargs["context_usage"]),
                    "harness": identity.harness,
                    "catalog_model_id": identity.catalog_model_id,
                    "runtime_context_epoch": identity.runtime_context_epoch,
                    "producer_handle_fingerprint": (
                        identity.producer_handle_fingerprint
                    ),
                }
            return "msg-id"

        with (
            patch("subagents.codex_runtime_events.run_in_db", fake_run_in_db),
            patch("subagents.codex_runtime_terminal.run_in_db", fake_run_in_db),
        ):
            on_event = codex_runtime._make_on_event(live, "codex-sid", bcast)
            _run_terminal_event(on_event, live, {"kind": "assistant_text", "text": "Hello"})
            _run_terminal_event(on_event, live, {
                        "kind": "result",
                        "usage": {"input_tokens": 500, "cached_input_tokens": 2},
                    })

        step = next(
            call.args[1]
            for call in bcast.call_args_list
            if call.args[1].get("type") == "step_text"
        )
        assert step["metadata"]["harness"] == "codex"
        assert step["metadata"]["model"] == "codex/gpt-5-codex"
        assert step["metadata"]["model_name"] == "GPT-5 Codex"
        assert step["metadata"]["provider_reported_model"] == "codex/gpt-5-codex"
        assert step["metadata"]["model_identity_mismatch"] is False
        assert attached
        assert attached[0][0] == ("codex-sid", "assistant-codex")
        usage = attached[0][1]["context_usage"]
        from core.token_estimation import measure_text
        from infra.context_usage_attribution import _mcp_tool_definition_tokens

        expected_history = _estimate_messages_tokens(history, model="codex/gpt-5-codex")
        expected_user = measure_text(current_user, model="codex/gpt-5-codex").tokens
        expected_tool_def = _mcp_tool_definition_tokens(
            "codex/gpt-5-codex", "codex",
            ({"type": "function", "function": {"name": "read_file"}},),
        )
        assert usage["snapshot_id"] == "ctx-codex-footer-proof"
        estimated_attribution_total = (
            42 + 84 + expected_history + expected_user + expected_tool_def
        )
        assert usage["total_used"] == 500
        assert usage["model_context_window"] == 200_000
        assert usage["utilization"] == round(500 / 200_000, 3)
        assert "provider_reported_model_context_window" not in usage
        assert usage["provider_reported_utilization"] is None
        assert usage["provider_reported_total_used"] == 500
        assert usage["attribution_delta_tokens"] == 500 - estimated_attribution_total
        # Codex reports no native tool-definition source of its own; the CORSIA
        # MCP tool-definition estimate (issue #1817) fills tool_def_tokens instead
        # of leaving it at 0, carved out of the harness's attribution_delta_tokens.
        assert usage["tool_def_tokens"] == expected_tool_def
        assert usage["system_tokens"] == 42
        assert usage["context_source_tokens"] == 84
        assert usage["history_tokens"] == expected_history
        assert usage["history_tokens"] != usage["total_used"]
        assert usage["user_tokens"] == expected_user

    def test_new_turn_binding_clears_partial_prior_turn_reasoning(self, main_db):
        from subagents import codex_runtime

        live = codex_runtime._LiveCodex(
            codex_thread_id="thread-reused",
            session=None,
            parent_thread_id=None,
            name="codex",
            thinking_buf="partial stale reasoning",
            thinking_provenance={"native_references": ["rs_stale"]},
            last_text="stale text",
            turn_has_assistant_row=True,
            dropped_line_this_turn=True,
        )

        turn_id = _successor_turn_id()
        identity = _admit_active_turn(
            "main-1",
            turn_id=turn_id,
            content="new turn",
        )
        _run(
            codex_runtime._start_live_turn(
                "main-1",
                live,
                "new turn",
                successor_turn_id=turn_id,
                bcast=MagicMock(),
                preadmitted_turn=identity,
            )
        )

        assert live.thinking_buf == ""
        assert live.thinking_provenance is None
        assert live.last_text == ""
        assert live.turn_has_assistant_row is False
        assert live.dropped_line_this_turn is False

    def test_reused_process_anchors_each_turn_context_to_its_exact_row(
        self,
        main_db,
        monkeypatch,
    ):
        from core.turn_core import commit_turn_terminal, drive_turn_settlement
        from subagents import codex_runtime, codex_runtime_events

        monkeypatch.setattr(
            codex_runtime_events,
            "commit_turn_terminal",
            commit_turn_terminal,
        )
        monkeypatch.setattr(
            codex_runtime_terminal,
            "drive_turn_settlement",
            drive_turn_settlement,
        )
        live = codex_runtime._LiveCodex(
            codex_thread_id="thread-lineage",
            session=SimpleNamespace(
                transport_generation_id=(
                    "22222222-2222-4222-8222-222222222222"
                ),
                returncode=None,
                working_dir=None,
            ),
            parent_thread_id=None,
            name="codex",
            model="gpt-5-codex",
            catalog_model_id="codex/gpt-5-codex",
            runtime_context_epoch=6,
            context_window=262_144,
            provider_reported_model="gpt-5-codex",
        )
        bcast = MagicMock()
        on_event = codex_runtime._make_on_event(live, "main-1", bcast)

        def snapshot(snapshot_id: str, text: str) -> dict:
            return {
                "snapshot_id": snapshot_id,
                "content_hash": snapshot_id,
                "harness_model_facts": {
                    "harness": "codex",
                    "model": "codex/gpt-5-codex",
                    "model_context_window": 262_144,
                },
                "delivery_leaves": [
                    {
                        "harness": "codex",
                        "destination_mode": "turn-only",
                    }
                ],
                "frozen_prompt_material": {"current_user_content": text},
            }

        live.context_snapshot_id = "ctx-a"
        live.context_snapshot = snapshot("ctx-a", "Turn A")
        live.source_artifacts = [{"harness": "codex", "snapshot_id": "ctx-a"}]
        turn_a_id = _successor_turn_id()
        turn_a = _admit_active_turn(
            "main-1",
            turn_id=turn_a_id,
            content="Turn A",
        )
        _run(
            codex_runtime._start_live_turn(
                "main-1",
                live,
                "Turn A",
                successor_turn_id=turn_a_id,
                bcast=bcast,
                preadmitted_turn=turn_a,
            )
        )
        _run_terminal_event(on_event, live, {"kind": "assistant_text", "text": "A answer"})
        _run_terminal_event(on_event, live, {
            "kind": "result",
            "usage": {
                "input_tokens": 11,
                "cached_input_tokens": 0,
                "model_context_window": 262_144,
            },
        })

        live.context_snapshot_id = "ctx-b"
        live.context_snapshot = snapshot("ctx-b", "Turn B")
        live.source_artifacts = [{"harness": "codex", "snapshot_id": "ctx-b"}]
        turn_b_id = _successor_turn_id()
        turn_b = _admit_active_turn(
            "main-1",
            turn_id=turn_b_id,
            content="Turn B",
        )
        _run(
            codex_runtime._start_live_turn(
                "main-1",
                live,
                "Turn B",
                successor_turn_id=turn_b_id,
                bcast=bcast,
                preadmitted_turn=turn_b,
            )
        )
        _run_terminal_event(on_event, live, {
            "kind": "result",
            "result": "B completed without assistant output",
            "usage": {
                "input_tokens": 22,
                "cached_input_tokens": 0,
                "model_context_window": 262_144,
            },
        })

        rows = [
            dict(row)
            for row in main_db.execute(
                "SELECT id, role, content, metadata FROM messages "
                "WHERE thread_id = ? ORDER BY rowid",
                ("main-1",),
            ).fetchall()
        ]
        assistant_a = next(row for row in rows if row["role"] == "assistant")
        result_rows = [
            row
            for row in rows
            if row["role"] == "system"
            and json.loads(row["metadata"] or "{}").get("type") == "result"
        ]
        marker_b = result_rows[-1]
        assistant_a_meta = json.loads(assistant_a["metadata"])
        marker_b_meta = json.loads(marker_b["metadata"])

        assert assistant_a_meta["context_usage"]["snapshot_id"] == "ctx-a"
        assert marker_b_meta["context_usage"]["snapshot_id"] == "ctx-b"
        assert assistant_a_meta["context_usage"]["harness"] == "codex"
        assert marker_b_meta["context_usage"]["harness"] == "codex"

        events = [call.args[1] for call in bcast.call_args_list]
        terminals = [
            message for message in events if message.get("type") == "turn_complete"
        ]
        assert [message["context_message_id"] for message in terminals] == [
            assistant_a["id"],
            marker_b["id"],
        ]
        assert [message["context_usage"]["snapshot_id"] for message in terminals] == [
            "ctx-a",
            "ctx-b",
        ]
        b_marker_event = next(
            index
            for index, message in enumerate(events)
            if message.get("type") == "message_added"
            and message.get("message", {}).get("id") == marker_b["id"]
        )
        b_terminal_event = events.index(terminals[1])
        assert b_marker_event < b_terminal_event

    def test_snapshot_without_provider_usage_is_marked_unmeasured_not_zero(self):
        from infra.context_receipts import context_usage_summary
        from subagents import codex_runtime

        snapshot = {
            "snapshot_id": "ctx-codex-source-truth",
            "content_hash": "c" * 64,
            "harness_model_facts": {
                "harness": "codex",
                "model": "codex/gpt-5-codex",
                "model_context_window": 200000,
            },
            "source_inventory": [{"source_id": "source-1"}],
            "delivery_leaves": [
                {
                    "harness": "codex",
                    "destination_mode": "thread-start-inject-resume",
                }
            ],
        }
        live = codex_runtime._LiveCodex(
            codex_thread_id="thread_ctx",
            session=SimpleNamespace(
                session_id="thread_ctx",
                working_dir="/tmp/codex",
                returncode=None,
                transport_generation_id=("11111111-1111-4111-8111-111111111111"),
            ),
            parent_thread_id=None,
            name="codex",
            model="gpt-5-codex",
            catalog_model_id="codex/gpt-5-codex",
            runtime_context_epoch=6,
            context_snapshot_id=snapshot["snapshot_id"],
            context_snapshot=snapshot,
        )
        bcast = MagicMock()
        recorded: list[dict] = []

        async def fake_run_in_db(fn, *_args, **_kwargs):
            if fn.__name__ == "persist_turn_terminal":
                from core.turn_core import TurnTerminalResult

                return TurnTerminalResult.APPLIED
            if fn.__name__ == "attach_context_receipt":
                recorded.append(_kwargs)
                identity = _kwargs["identity"]
                return {
                    **context_usage_summary(_kwargs["context_usage"]),
                    "harness": identity.harness,
                    "catalog_model_id": identity.catalog_model_id,
                    "runtime_context_epoch": identity.runtime_context_epoch,
                    "producer_handle_fingerprint": (
                        identity.producer_handle_fingerprint
                    ),
                }
            return "msg-id"

        with (
            patch("subagents.codex_runtime_events.run_in_db", fake_run_in_db),
            patch("subagents.codex_runtime_terminal.run_in_db", fake_run_in_db),
        ):
            on_event = codex_runtime._make_on_event(live, "codex-sid", bcast)
            _run_terminal_event(on_event, live, {"kind": "result"})

        payload = next(
            call.args[1]
            for call in bcast.call_args_list
            if call.args[1].get("type") == "turn_complete"
        )
        usage = payload["context_usage"]
        assert usage["snapshot_id"] == "ctx-codex-source-truth"
        assert usage["model_context_window"] == 200000
        assert usage["token_usage_available"] is False
        assert usage["total_used"] is None
        assert usage["utilization"] is None
        assert usage["stale_reason"] == "terminal_without_measurement"
        identity = recorded[0]["identity"]
        assert identity.catalog_model_id == "codex/gpt-5-codex"
        assert identity.runtime_context_epoch == 6
        assert len(identity.producer_handle_fingerprint) == 64

    def test_observed_protocol_window_sets_denominator(self):
        from subagents import codex_runtime

        live = codex_runtime._LiveCodex(
            codex_thread_id="thread_ctx",
            session=None,
            parent_thread_id=None,
            name="codex",
        )
        bcast = MagicMock()

        async def fake_run_in_db(fn, *_args, **_kwargs):
            if fn.__name__ == "persist_turn_terminal":
                from core.turn_core import TurnTerminalResult

                return TurnTerminalResult.APPLIED
            return "msg-id"

        with patch("subagents.codex_runtime_events.run_in_db", fake_run_in_db):
            on_event = codex_runtime._make_on_event(live, "codex-sid", bcast)
            _run_terminal_event(on_event, live, {
                        "kind": "result",
                        "usage": {
                            "input_tokens": 100_000,
                            "cached_input_tokens": 36_000,
                            "model_context_window": 272_000,
                        },
                    })

        payload = next(
            call.args[1]
            for call in bcast.call_args_list
            if call.args[1].get("type") == "turn_complete"
        )
        usage = payload["context_usage"]
        assert usage["model_context_window"] == 272_000
        assert usage["total_used"] == 100_000
        assert usage["provider_reported_cached_input_tokens"] == 36_000
        assert usage["utilization"] == pytest.approx(100_000 / 272_000)
        assert usage["provider_reported_utilization"] == pytest.approx(
            100_000 / 272_000
        )

    def test_unknown_window_yields_null_utilization_not_zero(self):
        from subagents import codex_runtime

        live = codex_runtime._LiveCodex(
            codex_thread_id="thread_ctx",
            session=None,
            parent_thread_id=None,
            name="codex",
        )
        bcast = MagicMock()

        async def fake_run_in_db(fn, *_args, **_kwargs):
            if fn.__name__ == "persist_turn_terminal":
                from core.turn_core import TurnTerminalResult

                return TurnTerminalResult.APPLIED
            return "msg-id"

        with patch("subagents.codex_runtime_events.run_in_db", fake_run_in_db):
            on_event = codex_runtime._make_on_event(live, "codex-sid", bcast)
            _run_terminal_event(on_event, live, {
                        "kind": "result",
                        "usage": {"input_tokens": 10, "cached_input_tokens": 2},
                    })

        payload = next(
            call.args[1]
            for call in bcast.call_args_list
            if call.args[1].get("type") == "turn_complete"
        )
        usage = payload["context_usage"]
        assert usage["model_context_window"] == 0
        assert "provider_reported_model_context_window" not in usage
        assert usage["utilization"] is None
        assert usage["provider_reported_utilization"] is None


class TestUsagePayload:
    def test_extracts_observed_context_window(self):
        from subagents.codex_app_server_events import usage_payload

        payload = usage_payload(
            {
                "last": {"inputTokens": 5, "cachedInputTokens": 2},
                "modelContextWindow": 272_000,
            }
        )
        assert payload == {
            "input_tokens": 5,
            "cached_input_tokens": 2,
            "model_context_window": 272_000,
        }

    def test_no_window_field_stays_unknown(self):
        from subagents.codex_app_server_events import usage_payload

        payload = usage_payload({"last": {"inputTokens": 5}})
        assert payload == {"input_tokens": 5, "cached_input_tokens": 0}

    def test_uses_last_input_occupancy_not_cumulative_total_or_cached_subset(self):
        from subagents.codex_app_server_events import usage_payload

        payload = usage_payload(
            {
                "total": {"inputTokens": 65_701, "cachedInputTokens": 22_144},
                "last": {"inputTokens": 23_587, "cachedInputTokens": 1_920},
                "modelContextWindow": 258_400,
            }
        )

        assert payload == {
            "input_tokens": 23_587,
            "cached_input_tokens": 1_920,
            "model_context_window": 258_400,
        }

    def test_cumulative_usage_without_last_measurement_is_unavailable(self):
        from subagents.codex_app_server_events import usage_payload

        assert (
            usage_payload(
                {
                    "total": {"inputTokens": 65_701, "cachedInputTokens": 22_144},
                    "modelContextWindow": 258_400,
                }
            )
            is None
        )

    @pytest.mark.parametrize("terminal_kind", ["completed", "failed"])
    @pytest.mark.parametrize(
        "malformed_usage",
        ["not-an-object", ["not-an-object"], True],
        ids=["string", "list", "boolean"],
    )
    def test_truthy_non_dict_usage_publishes_unmeasured_terminal(
        self,
        terminal_kind,
        malformed_usage,
    ):
        from core.turn_core import TurnTerminalResult
        from subagents import codex_runtime
        from subagents.codex_app_server_events import failed_event, result_event

        snapshot = {
            "snapshot_id": "ctx-malformed-codex-usage",
            "source_inventory": [],
            "delivery_leaves": [
                {
                    "harness": "codex",
                    "destination_mode": "thread-start-inject-resume",
                }
            ],
        }
        live = codex_runtime._LiveCodex(
            codex_thread_id="thread-malformed-usage",
            session=None,
            parent_thread_id=None,
            name="codex",
            context_snapshot_id=snapshot["snapshot_id"],
            context_snapshot=snapshot,
        )
        casts: list[dict] = []

        async def fake_run_in_db(fn, *_args, **_kwargs):
            if fn.__name__ == "persist_turn_terminal":
                return TurnTerminalResult.APPLIED
            return "message-id"

        event = (
            result_event({"status": "completed"}, malformed_usage)
            if terminal_kind == "completed"
            else failed_event({"message": "provider failed"}, malformed_usage)
        )
        with patch("subagents.codex_runtime_events.run_in_db", fake_run_in_db):
            on_event = codex_runtime._make_on_event(
                live,
                "codex-sid",
                lambda _sid, payload: casts.append(payload),
            )
            _run_terminal_event(on_event, live, event)

        terminals = [payload for payload in casts if payload["type"] == "turn_complete"]
        assert len(terminals) == 1
        proof = terminals[0]["context_usage"]
        assert proof["token_usage_available"] is False
        assert proof["total_used"] is None
        assert proof["utilization"] is None

    @pytest.mark.parametrize(
        "input_tokens",
        [True, False, "100", None, -1, float("nan"), float("inf"), 1.5],
    )
    def test_malformed_input_occupancy_is_unavailable(self, input_tokens):
        from subagents.codex_app_server_events import usage_payload

        assert (
            usage_payload(
                {
                    "last": {
                        "inputTokens": input_tokens,
                        "cachedInputTokens": 0,
                    },
                    "modelContextWindow": 258_400,
                }
            )
            is None
        )

    def test_boolean_cached_and_window_telemetry_are_not_numbers(self):
        from subagents.codex_app_server_events import usage_payload

        assert usage_payload(
            {
                "last": {"inputTokens": 100, "cachedInputTokens": True},
                "modelContextWindow": True,
            }
        ) == {
            "input_tokens": 100,
            "cached_input_tokens": 0,
        }

    @pytest.mark.parametrize(
        "malformed",
        [True, float("nan"), float("inf"), 1.5],
    )
    def test_optional_malformed_numeric_telemetry_degrades_safely(self, malformed):
        from subagents.codex_app_server_events import usage_payload

        assert usage_payload(
            {
                "last": {"inputTokens": 100, "cachedInputTokens": malformed},
                "modelContextWindow": malformed,
            }
        ) == {"input_tokens": 100, "cached_input_tokens": 0}

    @pytest.mark.parametrize(
        "malformed",
        [True, float("nan"), float("inf"), 1.5],
    )
    def test_direct_context_projection_rejects_malformed_occupancy(self, malformed):
        from subagents.codex_runtime_terminal import (
            provider_context_usage as _provider_context_usage,
        )

        assert (
            _provider_context_usage(
                {"input_tokens": malformed, "model_context_window": 258_400},
            )
            is None
        )

    @pytest.mark.parametrize(
        "malformed",
        [True, float("nan"), float("inf"), 1.5],
    )
    def test_direct_context_projection_degrades_optional_malformed_fields(
        self,
        malformed,
    ):
        from subagents.codex_runtime_terminal import (
            provider_context_usage as _provider_context_usage,
        )

        usage = _provider_context_usage(
            {
                "input_tokens": 100,
                "cached_input_tokens": malformed,
                "model_context_window": malformed,
            },
        )
        assert usage["total_used"] == 100
        assert usage["provider_reported_cached_input_tokens"] == 0
        assert usage["model_context_window"] == 0
        assert usage["utilization"] is None


class TestReasoningProvenance:
    def setup_method(self):
        _reset_runtime()

    def test_codex_reasoning_summary_reference_reaches_assistant_step(self):
        from subagents import codex_runtime

        live = codex_runtime._LiveCodex(
            codex_thread_id="thread-reasoning",
            session=None,
            parent_thread_id=None,
            name="codex",
        )
        bcast = MagicMock()
        persisted: list[tuple[tuple, dict]] = []

        async def fake_run_in_db(fn, *args, **kwargs):
            if fn.__name__ == "persist_assistant_text":
                persisted.append((args, kwargs))
                return "assistant-reasoning"
            if fn.__name__ == "load_assistant_step_display":
                return {
                    "id": "assistant-reasoning",
                    "content": "Answer",
                    "metadata": {},
                    "created_at": "2026-07-16T00:00:00+00:00",
                }
            return "msg-id"

        provenance = {
            "source_harness": "codex",
            "source_provider": "codex",
            "representation_kind": "codex_reasoning_summary",
            "native_reference": "rs_123",
        }
        with patch("subagents.codex_runtime_events.run_in_db", fake_run_in_db):
            on_event = codex_runtime._make_on_event(live, "codex-sid", bcast)
            _run(
                on_event(
                    {
                        "kind": "step_reasoning",
                        "text": "Consider the evidence.",
                        "provenance": provenance,
                    }
                )
            )
            _run(on_event({"kind": "assistant_text", "text": "Answer"}))

        _args, kwargs = persisted[0]
        assert kwargs["reasoning"] == "Consider the evidence."
        assert kwargs["reasoning_provenance"] == {
            "source_harness": "codex",
            "source_provider": "codex",
            "representation_kind": "codex_reasoning_summary",
            "native_references": ["rs_123"],
        }
        assert live.thinking_buf == ""
        assert live.thinking_provenance is None

    def test_multiple_codex_reasoning_items_keep_every_native_reference(self):
        from subagents import codex_runtime

        live = codex_runtime._LiveCodex(
            codex_thread_id="thread-reasoning-many",
            session=None,
            parent_thread_id=None,
            name="codex",
        )
        bcast = MagicMock()
        persisted: list[dict] = []

        async def fake_run_in_db(fn, *args, **kwargs):
            if fn.__name__ == "persist_assistant_text":
                persisted.append(kwargs)
                return "assistant-reasoning-many"
            if fn.__name__ == "load_assistant_step_display":
                return {
                    "id": "assistant-reasoning-many",
                    "content": "Answer",
                    "metadata": {},
                    "created_at": "2026-07-16T00:00:00+00:00",
                }
            return "msg-id"

        def provenance(reference: str, kind: str = "codex_reasoning_summary"):
            return {
                "source_harness": "codex",
                "source_provider": "codex",
                "representation_kind": kind,
                "native_reference": reference,
            }

        with patch("subagents.codex_runtime_events.run_in_db", fake_run_in_db):
            on_event = codex_runtime._make_on_event(live, "codex-sid", bcast)
            _run(
                on_event(
                    {
                        "kind": "step_reasoning",
                        "text": "First summary. ",
                        "provenance": provenance("rs_1"),
                    }
                )
            )
            _run(
                on_event(
                    {
                        "kind": "step_reasoning",
                        "text": "",
                        "provenance": provenance("rs_private", "opaque"),
                    }
                )
            )
            _run(
                on_event(
                    {
                        "kind": "step_reasoning",
                        "text": "Second summary.",
                        "provenance": provenance("rs_2"),
                    }
                )
            )
            _run(on_event({"kind": "assistant_text", "text": "Answer"}))

        assert persisted[0]["reasoning"] == "First summary. Second summary."
        assert persisted[0]["reasoning_provenance"] == {
            "source_harness": "codex",
            "source_provider": "codex",
            "representation_kind": "codex_reasoning_summary",
            "native_references": ["rs_1", "rs_private", "rs_2"],
        }

    def test_canonical_provenance_normalizes_ordered_references_and_signatures(self):
        from core.reasoning_provenance import normalize_reasoning_provenance

        assert normalize_reasoning_provenance(
            {
                "source_harness": "codex",
                "source_provider": "codex",
                "representation_kind": "codex_reasoning_summary",
                "native_references": ["rs_2", "rs_1", "rs_2", ""],
                "native_signatures": ["sig_2", "sig_1", "sig_2", None],
            }
        ) == {
            "source_harness": "codex",
            "source_provider": "codex",
            "representation_kind": "codex_reasoning_summary",
            "native_references": ["rs_2", "rs_1"],
            "native_signatures": ["sig_2", "sig_1"],
        }

    def test_canonical_provenance_retains_singleton_compatibility_fields(self):
        from core.reasoning_provenance import normalize_reasoning_provenance

        assert normalize_reasoning_provenance(
            source_harness="codex",
            source_provider="codex",
            representation_kind="opaque",
            native_reference="rs_legacy",
            native_signature="sig_legacy",
        ) == {
            "source_harness": "codex",
            "source_provider": "codex",
            "representation_kind": "opaque",
            "native_references": ["rs_legacy"],
            "native_reference": "rs_legacy",
            "native_signatures": ["sig_legacy"],
            "native_signature": "sig_legacy",
        }


class TestFailedTurnContextUsage:
    def setup_method(self):
        _reset_runtime()
        _seed_runtime_sessions("codex-sid")

    def test_failed_terminal_keeps_last_observed_context_occupancy(self):
        from subagents import codex_runtime

        live = codex_runtime._LiveCodex(
            codex_thread_id="thread-failed-usage",
            session=None,
            parent_thread_id=None,
            name="codex",
        )
        bcast = MagicMock()

        async def fake_run_in_db(fn, *_args, **_kwargs):
            if fn.__name__ == "persist_turn_terminal":
                from core.turn_core import TurnTerminalResult

                return TurnTerminalResult.APPLIED
            if fn.__name__ == "insert_message":
                return fn(*_args, **_kwargs)
            return "failure-message"

        with patch("subagents.codex_runtime_events.run_in_db", fake_run_in_db):
            on_event = codex_runtime._make_on_event(live, "codex-sid", bcast)
            _run_terminal_event(on_event, live, {
                "kind": "turn_failed",
                "cause": "process_exited",
                "message": "provider failed",
                "usage": {
                    "input_tokens": 23_587,
                    "cached_input_tokens": 1_920,
                    "model_context_window": 258_400,
                },
            })

        terminal = next(
            call.args[1]
            for call in bcast.call_args_list
            if call.args[1].get("type") == "turn_complete"
        )
        assert terminal["context_usage"]["total_used"] == 23_587
        assert terminal["context_usage"]["provider_reported_cached_input_tokens"] == 1_920

    @pytest.mark.parametrize("terminal_kind", ["result", "turn_failed"])
    def test_provider_marker_failure_still_publishes_terminal_once(
        self,
        terminal_kind,
    ):
        from core.turn_core import TurnIdentity, TurnTerminalResult
        from infra.thread_driver import TerminalSettlement
        from subagents import codex_runtime

        live = codex_runtime._LiveCodex(
            codex_thread_id="thread-marker-failure",
            session=None,
            parent_thread_id=None,
            name="codex",
            corsia_turn=TurnIdentity("turn-marker-failure", 1),
            thinking_buf="partial orphan reasoning",
            thinking_provenance={"native_references": ["rs_partial"]},
        )
        casts: list[dict] = []
        claims = 0

        async def fake_claim(*_args, **_kwargs):
            nonlocal claims
            claims += 1
            result = (
                TurnTerminalResult.APPLIED
                if claims == 1
                else TurnTerminalResult.ALREADY_SAME_OUTCOME
            )
            return TerminalSettlement(result, None)

        async def fake_run_in_db(fn, *_args, **_kwargs):
            if fn.__name__ == "insert_message":
                raise RuntimeError("marker failed")
            return "unused"

        event = {
            "kind": terminal_kind,
            "is_error": True,
            "result": "provider failed",
            "message": "provider failed",
            "usage": {
                "input_tokens": 12_345,
                "cached_input_tokens": 2_345,
                "model_context_window": 258_400,
            },
        }
        with (
            patch("subagents.codex_runtime_events.run_in_db", fake_run_in_db),
            patch(
                "subagents.codex_runtime_events.commit_turn_terminal",
                fake_claim,
            ),
        ):
            on_event = codex_runtime._make_on_event(
                live,
                "codex-sid",
                lambda _sid, payload: casts.append(payload),
            )
            _run_terminal_event(on_event, live, event)
            _run_terminal_event(on_event, live, event)

        terminals = [payload for payload in casts if payload["type"] == "turn_complete"]
        assert len(terminals) == 1
        assert terminals[0]["context_usage"]["total_used"] == 12_345
        assert live.thinking_buf == ""
        assert live.thinking_provenance is None

    def test_runtime_terminal_marker_announcement_failure_propagates(self):
        from subagents import codex_runtime

        async def fake_run_in_db(fn, *_args, **_kwargs):
            assert fn.__name__ == "insert_message"
            return fn(*_args, **_kwargs)

        def failed_announcement(_thread_id, _payload):
            raise RuntimeError("marker announcement unavailable")

        with (
            patch("subagents.codex_runtime_events.run_in_db", fake_run_in_db),
            pytest.raises(RuntimeError, match="marker announcement unavailable"),
        ):
            _run(
                codex_runtime.record_terminal_marker(
                    "codex-sid",
                    "Codex exited before the terminal event.",
                    {"codex": True, "type": "cc_error"},
                    failed_announcement,
                )
            )

    def test_runtime_terminal_marker_reuses_persisted_announcement_timestamp(self):
        from subagents import codex_runtime

        persisted: dict = {}
        announced: dict = {}

        async def fake_run_in_db(fn, *_args, **kwargs):
            assert fn.__name__ == "insert_message"
            persisted.update(kwargs)
            return fn(*_args, **kwargs)

        def capture_announcement(_thread_id, payload):
            announced.update(payload)

        with patch("subagents.codex_runtime_events.run_in_db", fake_run_in_db):
            _run(
                codex_runtime.record_terminal_marker(
                    "codex-sid",
                    "Codex exited before the terminal event.",
                    {"codex": True, "type": "cc_error"},
                    capture_announcement,
                )
            )

        assert persisted["created_at"] == announced["message"]["created_at"]

    @pytest.mark.parametrize("terminal_kind", ["result", "turn_failed"])
    def test_provider_marker_announcement_failure_still_publishes_terminal_once(
        self,
        terminal_kind,
        caplog,
    ):
        from core.turn_core import TurnIdentity, TurnTerminalResult
        from infra.thread_driver import TerminalSettlement
        from subagents import codex_runtime

        live = codex_runtime._LiveCodex(
            codex_thread_id="thread-marker-announcement-failure",
            session=None,
            parent_thread_id=None,
            name="codex",
            corsia_turn=TurnIdentity("turn-marker-announcement-failure", 1),
            thinking_buf="partial orphan reasoning",
            thinking_provenance={"native_references": ["rs_partial"]},
        )
        casts: list[dict] = []
        marker_announcements: list[dict] = []
        persisted_markers: list[tuple[tuple, dict]] = []
        claims = 0

        async def fake_claim(*_args, **_kwargs):
            nonlocal claims
            claims += 1
            return TerminalSettlement(TurnTerminalResult.APPLIED, None)

        async def fake_run_in_db(fn, *args, **kwargs):
            if fn.__name__ == "insert_message":
                persisted_markers.append((args, kwargs))
                return fn(*args, **kwargs)
            return "unused"

        def flaky_bcast(_thread_id, payload):
            if payload["type"] == "message_added":
                marker_announcements.append(payload)
                raise RuntimeError("marker announcement unavailable")
            casts.append(payload)

        event = {
            "kind": terminal_kind,
            "is_error": True,
            "result": "provider failed",
            "message": "provider failed",
            "usage": {
                "input_tokens": 12_345,
                "cached_input_tokens": 2_345,
                "model_context_window": 258_400,
            },
        }
        with (
            patch("subagents.codex_runtime_events.run_in_db", fake_run_in_db),
            patch(
                "subagents.codex_runtime_events.commit_turn_terminal",
                fake_claim,
            ),
        ):
            on_event = codex_runtime._make_on_event(
                live,
                "codex-sid",
                flaky_bcast,
            )
            _run_terminal_event(on_event, live, event)

        terminals = [payload for payload in casts if payload["type"] == "turn_complete"]
        assert claims == 1
        assert len(persisted_markers) == 1
        assert len(marker_announcements) == 1
        assert len(terminals) == 1
        assert terminals[0]["context_usage"]["total_used"] == 12_345
        assert "marker announcement failed" in caplog.text
        assert live.thinking_buf == ""
        assert live.thinking_provenance is None


class TestOutOfBandTerminalContextUsage:
    def setup_method(self):
        _reset_runtime()

    @staticmethod
    def _live_with_usage():
        from core.turn_core import TurnIdentity
        from subagents import codex_runtime
        from subagents.codex_app_server import CodexAppServerSession

        session = CodexAppServerSession(binary="codex", task=None)
        session._active_turn_id = "provider-turn"
        session._usage_by_turn["provider-turn"] = {
            "last": {"inputTokens": 12_345, "cachedInputTokens": 2_345},
            "modelContextWindow": 258_400,
        }
        live = codex_runtime._LiveCodex(
            codex_thread_id="provider-thread",
            session=session,
            parent_thread_id=None,
            name="codex",
            corsia_turn=TurnIdentity("corsia-turn", 1),
        )
        return live, session

    @pytest.mark.parametrize(
        ("terminal", "outcome"),
        [
            ("process_exit", "error"),
            ("hard_interrupt", "cancelled"),
            ("rejected_input", "error"),
        ],
    )
    def test_terminal_outside_app_server_notification_retains_usage(
        self,
        terminal,
        outcome,
    ):
        from core.turn_core import TurnTerminalResult
        from subagents import codex_runtime

        live, session = self._live_with_usage()
        bcast = MagicMock()

        with patch.object(
            codex_runtime,
            "_claim_live_terminal",
            AsyncMock(return_value=TurnTerminalResult.APPLIED),
        ) as claim_terminal:
            if terminal == "process_exit":
                _run(
                    codex_runtime._finish_process_exit(
                        "codex-sid",
                        live,
                        7,
                        "provider died",
                        bcast,
                    )
                )
            elif terminal == "hard_interrupt":
                _run(
                    codex_runtime._finish_interrupted_turn(
                        "codex-sid",
                        live,
                        "hard_preempt",
                        bcast,
                    )
                )
            else:
                _run(
                    codex_runtime._finish_rejected_input(
                        "codex-sid",
                        live,
                        live.corsia_turn,
                        "provider rejected input",
                        bcast,
                    )
                )

        terminal_payload = next(
            call.args[1]
            for call in bcast.call_args_list
            if call.args[1].get("type") == "turn_complete"
        )
        assert terminal_payload["context_usage"]["total_used"] == 12_345
        assert (
            terminal_payload["context_usage"][
                "provider_reported_cached_input_tokens"
            ]
            == 2_345
        )
        assert session._usage_by_turn == {}
        assert claim_terminal.await_args.args[1] == outcome

    def test_process_exit_marker_failure_still_publishes_terminal_usage(self):
        from core.turn_core import TurnTerminalResult
        from subagents import codex_runtime

        live, session = self._live_with_usage()
        live.thinking_buf = "unfinished reasoning"
        live.thinking_provenance = {"native_reference": "rs_unfinished"}
        bcast = MagicMock()

        with (
            patch.object(
                codex_runtime,
                "_claim_live_terminal",
                AsyncMock(return_value=TurnTerminalResult.APPLIED),
            ),
            patch.object(
                codex_runtime,
                "insert_message",
                side_effect=RuntimeError("marker insert failed"),
            ),
        ):
            _run(
                codex_runtime._finish_process_exit(
                    "codex-sid",
                    live,
                    7,
                    "provider died",
                    bcast,
                )
            )

        terminal_payloads = [
            call.args[1]
            for call in bcast.call_args_list
            if call.args[1].get("type") == "turn_complete"
        ]
        assert len(terminal_payloads) == 1
        assert terminal_payloads[0]["context_usage"]["total_used"] == 12_345
        assert "context_message_id" not in terminal_payloads[0]
        assert session._usage_by_turn == {}
        assert live.thinking_buf == ""
        assert live.thinking_provenance is None

    def test_caller_owned_db_is_reused_for_terminal_context_receipt(self):
        from core.turn_core import TurnTerminalResult
        from subagents import codex_runtime

        live, _session = self._live_with_usage()
        owned_db = object()
        attach = AsyncMock()

        with (
            patch.object(
                codex_runtime,
                "_claim_live_terminal",
                AsyncMock(return_value=TurnTerminalResult.APPLIED),
            ),
            patch(
                "subagents.codex_runtime_terminal.attach_context_usage",
                attach,
            ),
        ):
            _run(
                codex_runtime._finish_interrupted_turn(
                    "codex-sid",
                    live,
                    "retire_requested",
                    MagicMock(),
                    owned_db,
                    create_marker=False,
                )
            )

        assert attach.await_args.kwargs["db"] is owned_db

    def test_receipt_attachment_failure_keeps_usage_in_terminal_frame(
        self,
        caplog,
    ):
        from core.turn_core import TurnTerminalResult
        from subagents import codex_runtime

        live, session = self._live_with_usage()
        casts: list[dict] = []
        with (
            patch.object(
                codex_runtime,
                "_claim_live_terminal",
                AsyncMock(return_value=TurnTerminalResult.APPLIED),
            ),
            patch.object(
                codex_runtime,
                "record_terminal_marker",
                AsyncMock(return_value="interrupt-marker"),
            ),
            patch.object(
                codex_runtime_terminal,
                "attach_context_usage",
                AsyncMock(side_effect=RuntimeError("proof layer failed")),
            ),
        ):
            _run(
                codex_runtime._finish_interrupted_turn(
                    "codex-sid",
                    live,
                    "hard_preempt",
                    lambda _sid, payload: casts.append(payload),
                )
            )

        terminal = next(
            payload for payload in casts if payload["type"] == "turn_complete"
        )
        assert terminal["context_usage"]["total_used"] == 12_345
        assert "context_message_id" not in terminal
        assert "terminal context receipt failed" in caplog.text
        assert session._usage_by_turn == {}

    def test_hard_interrupt_marker_carries_corsia_turn_id(self):
        from core.turn_core import TurnTerminalResult
        from subagents import codex_runtime

        live, _session = self._live_with_usage()
        marker = AsyncMock(return_value="interrupt-marker")
        with (
            patch.object(
                codex_runtime,
                "_claim_live_terminal",
                AsyncMock(return_value=TurnTerminalResult.APPLIED),
            ),
            patch.object(codex_runtime, "record_terminal_marker", marker),
            patch.object(
                codex_runtime,
                "_publish_claimed_terminal",
                AsyncMock(),
            ),
        ):
            _run(
                codex_runtime._finish_interrupted_turn(
                    "codex-sid",
                    live,
                    "hard_preempt",
                    MagicMock(),
                )
            )

        assert marker.await_args.args[2]["turn_id"] == "corsia-turn"

    def test_catalog_observation_failure_keeps_exact_usage_and_terminal_once(
        self,
        caplog,
    ):
        from core.turn_core import TurnTerminalResult
        from subagents import codex_runtime

        live, session = self._live_with_usage()
        live.model = "gpt-5.4"
        live.provider_reported_model = "gpt-5.4"
        live.thinking_buf = "turn-local reasoning"
        live.thinking_provenance = {"native_reference": "rs_turn_local"}
        casts: list[dict] = []
        attach_usage = AsyncMock()

        with (
            patch.object(
                codex_runtime,
                "_claim_live_terminal",
                AsyncMock(return_value=TurnTerminalResult.APPLIED),
            ) as claim_terminal,
            patch.object(
                codex_runtime_terminal,
                "attach_context_usage",
                attach_usage,
            ),
            patch(
                "connections.harness_models.record_runtime_context_observation",
                side_effect=RuntimeError("catalog unavailable"),
            ),
        ):
            _run(
                codex_runtime._finish_interrupted_turn(
                    "codex-sid",
                    live,
                    "retire_requested",
                    lambda _sid, payload: casts.append(payload),
                    create_marker=False,
                )
            )

        terminals = [payload for payload in casts if payload["type"] == "turn_complete"]
        assert len(terminals) == 1
        proof = terminals[0]["context_usage"]
        assert proof["token_usage_available"] is True
        assert proof["total_used"] == 12_345
        assert proof["provider_reported_cached_input_tokens"] == 2_345
        assert proof["model_context_window"] == 258_400
        assert "runtime context observation failed" in caplog.text
        claim_terminal.assert_awaited_once()
        attach_usage.assert_awaited_once()
        assert attach_usage.await_args.kwargs["context_usage"]["total_used"] == 12_345
        assert session._usage_by_turn == {}
        assert live.thinking_buf == ""
        assert live.thinking_provenance is None


class TestThreadIdCapture:
    def setup_method(self):
        _reset_runtime()

    def test_thread_started_event_captures_thread_id(self):
        from subagents import codex_runtime

        live = codex_runtime._LiveCodex(
            codex_thread_id="",
            session=SimpleNamespace(),
            parent_thread_id=None,
            name="test",
        )
        bcast = MagicMock()

        artifact = {
            "id": "artifact-codex-thread",
            "integration": "codex",
            "type": "thread",
            "external_id": "thread_captured_001",
        }
        with (
            patch(
                "subagents.codex_runtime_events.register_codex_thread",
                return_value=artifact,
            ) as register,
            patch("subagents.codex_runtime_events.notify_changed") as notify,
        ):
            on_event = codex_runtime._make_on_event(live, "sess-capture", bcast)
            _run(
                on_event({"kind": "thread_started", "thread_id": "thread_captured_001"})
            )

        assert live.codex_thread_id == "thread_captured_001"
        assert codex_runtime._CODEX_THREADS["sess-capture"] == "thread_captured_001"
        register.assert_called_once_with(
            "sess-capture",
            "thread_captured_001",
            rollout_path=None,
            reasoning_effort="",
        )
        notify.assert_called_once_with(artifact)

    def test_thread_started_event_registers_reported_rollout_path(self):
        from subagents import codex_runtime

        live = codex_runtime._LiveCodex(
            codex_thread_id="",
            session=None,
            parent_thread_id=None,
            name="test",
        )
        rollout = "/runtime/codex-home/sessions/2026/07/23/rollout-x.jsonl"
        with (
            patch(
                "subagents.codex_runtime_events.register_codex_thread",
                return_value=None,
            ) as register,
            patch("subagents.codex_runtime_events.notify_changed"),
        ):
            on_event = codex_runtime._make_on_event(live, "sess-rollout", MagicMock())
            _run(
                on_event(
                    {
                        "kind": "thread_started",
                        "thread_id": "thread_captured_002",
                        "rollout_path": rollout,
                    }
                )
            )

        register.assert_called_once_with(
            "sess-rollout",
            "thread_captured_002",
            rollout_path=rollout,
            reasoning_effort="",
        )


class TestStop:
    def setup_method(self):
        _reset_runtime()

    def test_stop_returns_false_if_not_live(self):
        from subagents.codex_runtime import stop

        assert _run(stop("not-live-sess")) is False

    def test_stop_kills_active_app_server_turn(self, isolated_db):
        from subagents import codex_runtime

        mock_session = _make_mock_app_session("thread_stop")
        mock_session._request_terminate = MagicMock()
        mock_session.revoke_dynamic_tool_admission = MagicMock()
        mock_session.wait_dynamic_tool_requests_idle = AsyncMock()
        _seed_runtime_sessions("stop-sess")
        identity = _admit_active_turn(
            "stop-sess",
            turn_id="turn-stop",
            content="input",
        )
        codex_runtime._LIVE.put(
            "stop-sess",
            codex_runtime._LiveCodex(
                codex_thread_id="thread_stop",
                session=mock_session,
                parent_thread_id=None,
                name="test",
                corsia_turn=identity,
            ),
        )
        codex_runtime._APP_SESSIONS["stop-sess"] = mock_session
        result = _run(
            codex_runtime.stop(
                "stop-sess",
                expected_turn_id="turn-stop",
            )
        )
        assert result is True
        mock_session._request_terminate.assert_called_once_with()
        mock_session.interrupt.assert_not_awaited()
        mock_session.terminate.assert_awaited_once()
        assert "stop-sess" not in codex_runtime._LIVE
        assert "stop-sess" not in codex_runtime._APP_SESSIONS


class TestRetire:
    def setup_method(self):
        _reset_runtime()

    def test_retire_terminates_live_session_without_marking_dead(self):
        from subagents import codex_runtime

        mock_session = _make_mock_app_session("thread_ctx")
        codex_runtime._LIVE.put(
            "ctx-change",
            codex_runtime._LiveCodex(
                codex_thread_id="thread_ctx",
                session=mock_session,
                parent_thread_id=None,
                name="test",
            ),
        )
        codex_runtime._CODEX_THREADS["ctx-change"] = "thread_ctx"

        result = _run(codex_runtime.retire("ctx-change"))

        assert result is True
        mock_session.terminate.assert_called_once()
        assert "ctx-change" not in codex_runtime._LIVE
        assert "ctx-change" not in codex_runtime._CODEX_THREADS
        assert "ctx-change" not in codex_runtime._DEAD

    def test_retire_terminates_idle_app_server_session(self):
        from subagents import codex_runtime

        app_session = _make_mock_app_session("thread_idle_ctx")
        codex_runtime._APP_SESSIONS["idle-ctx"] = app_session
        codex_runtime._CODEX_THREADS["idle-ctx"] = "thread_idle_ctx"

        result = _run(codex_runtime.retire("idle-ctx"))

        assert result is True
        app_session.terminate.assert_awaited_once()
        assert "idle-ctx" not in codex_runtime._APP_SESSIONS
        assert "idle-ctx" not in codex_runtime._CODEX_THREADS
        assert "idle-ctx" not in codex_runtime._DEAD

    def test_retire_archives_persisted_codex_thread_handle(self, main_db):
        from registry import models
        from subagents import codex_runtime

        models.register_artifact(
            integration="codex",
            type="thread",
            external_id="thread_stale_ctx",
            parent_thread_id="main-1",
            db=main_db,
        )
        codex_runtime._CODEX_THREADS["main-1"] = "thread_stale_ctx"

        result = _run(codex_runtime.retire("main-1", db=main_db))

        assert result is True
        assert "main-1" not in codex_runtime._CODEX_THREADS
        assert models.find_for_thread("main-1", "codex", "thread", db=main_db) is None


class TestShutdownAll:
    def setup_method(self):
        _reset_runtime()

    def test_shutdown_all_terminates_idle_app_server_sessions(self):
        from subagents import codex_runtime

        app_session = _make_mock_app_session("thread_idle_shutdown")
        codex_runtime._APP_SESSIONS["idle-shutdown"] = app_session
        codex_runtime._CODEX_THREADS["idle-shutdown"] = "thread_idle_shutdown"

        _run(codex_runtime.shutdown_all())

        app_session.terminate.assert_awaited_once()
        assert "idle-shutdown" not in codex_runtime._APP_SESSIONS
        assert "idle-shutdown" not in codex_runtime._CODEX_THREADS


class TestSurfaceExitError:
    def test_clean_exit_with_stderr_chatter_is_not_an_error(self):
        from subagents import codex_runtime
        from core.turn_core import TurnIdentity

        bcast = MagicMock()
        with patch("subagents.codex_runtime.insert_message") as ins:
            codex_runtime._surface_exit_error(
                "sess-ok",
                0,
                "Reading additional input from stdin...\n",
                bcast,
                None,
                TurnIdentity("turn-ok", 1),
            )
        ins.assert_not_called()
        bcast.assert_not_called()

    def test_clean_exit_no_stderr_is_not_an_error(self):
        from subagents import codex_runtime
        from core.turn_core import TurnIdentity

        bcast = MagicMock()
        with patch("subagents.codex_runtime.insert_message") as ins:
            codex_runtime._surface_exit_error(
                "sess-ok2",
                0,
                "",
                bcast,
                None,
                TurnIdentity("turn-ok2", 2),
            )
        ins.assert_not_called()

    def test_nonzero_exit_surfaces_error(self):
        from subagents import codex_runtime
        from core.turn_core import TurnIdentity

        _seed_runtime_sessions("sess-fail")
        bcast = MagicMock()
        with patch("subagents.codex_runtime.insert_message", wraps=codex_runtime.insert_message) as ins:
            codex_runtime._surface_exit_error(
                "sess-fail",
                1,
                "fatal: something broke\n",
                bcast,
                None,
                TurnIdentity("turn-fail", 3),
            )
        ins.assert_called_once()
        assert "something broke" in str(ins.call_args)
        payload = bcast.call_args.args[1]
        assert payload["turn_id"] == "turn-fail"
        assert payload["turn_order"] == 3
