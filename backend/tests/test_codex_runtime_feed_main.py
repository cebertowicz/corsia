from __future__ import annotations

from llm.context_window import WindowFacts
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from codex_runtime_fixtures import (
    _admit_active_turn,
    _make_mock_app_session,
    _reset_runtime,
    _run,
    _seed_runtime_sessions,
    locked_spawn,
)
from llm.resolution import ExecutionTarget
from subagents import (
    codex_destination_render,
    codex_runtime_support,
    codex_spawn_preflight,
)


def _successor_turn_id() -> str:
    return str(uuid4())


async def _bind_input_turn(_turn_id: str, _expected_turn_id: str | None) -> None:
    """Model the send-service row binding at this runtime unit boundary."""


def _codex_target(selector: str, *, context_window: int = 200000) -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=f"codex/{selector}",
        harness="codex",
        provider="codex",
        backend="codex",
        transport_model=selector,
        expected_reported_model=selector,
        context_window=context_window,
        available=True,
        availability_reason=None,
        display_name=selector,
        supports_reasoning=True,
    )


def _live_overrides(target: ExecutionTarget) -> tuple:
    """The compaction overrides a live session spawned for ``target`` carries."""
    from subagents.codex_runtime_spawn import _compaction_overrides

    return _compaction_overrides(
        target.context_window,
        discovered_context_window=target.discovered_context_window,
    )


def _fake_codex_reconstruction(
    task_text: str,
    *,
    turn_text: str | None = None,
    history_items: tuple[dict, ...] = (),
):
    snapshot = SimpleNamespace(
        to_dict=lambda: {"snapshot_id": "snapshot-test"},
        source_inventory=(),
    )
    return SimpleNamespace(
        task_text=task_text,
        turn_text=task_text if turn_text is None else turn_text,
        snapshot=snapshot,
        snapshot_id="snapshot-test",
        history_format_version=1,
        history_start_exclusive=None,
        history_sealed_boundary=None,
        history_full_raw_replay_available=True,
        history_items=history_items,
        checkpoint_text=None,
    )


def _prepared_codex_context(
    reconstruction,
    *,
    mode: str,
    handle_fingerprint=None,
    tool_registry=None,
    dynamic_tools=(),
    thread_id: str = "prepared-session",
    current_user_text: str = "mutable caller text",
):
    from core.context_payload import (
        PreparedContextPayload,
        RenderedDestinationPayload,
    )
    from subagents.codex_destination import (
        build_prepared_codex_destination,
    )

    frozen_registry = tool_registry if tool_registry is not None else object()
    delivery_mode = "exact_replay" if mode == "exact" else mode
    if mode == "provider_resume":
        from subagents import codex_runtime

        live = codex_runtime._LIVE.get(thread_id)
        if live is not None:
            live.session.tool_registry = frozen_registry
            live.session.dynamic_tools = dynamic_tools
    destination = build_prepared_codex_destination(
        thread_id=thread_id,
        target_catalog_id="codex/gpt-5-codex",
        current_user_text=current_user_text,
        reconstruction=reconstruction,
        delivery_mode=delivery_mode,
        handle_fingerprint=handle_fingerprint,
        tool_registry=frozen_registry,
        dynamic_tools=dynamic_tools,
    )
    return PreparedContextPayload(
        mode=delivery_mode,
        target_catalog_id="codex/gpt-5-codex",
        model_context_window=200_000,
        history_epoch=0,
        sealed_boundary=None,
        replay=None if mode == "provider_resume" else SimpleNamespace(mode=mode),
        rendered=RenderedDestinationPayload(
            payload=destination,
            input_tokens=destination.input_tokens,
        ),
    )


def test_codex_switch_scope_accepts_only_the_explicit_source_model() -> None:
    from subagents import codex_runtime

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, model TEXT, project_id TEXT)"
    )
    db.execute(
        "INSERT INTO threads (id,model,project_id) VALUES (?,?,?)",
        ("switch-thread", "anthropic/claude-3-5-sonnet", "project-1"),
    )
    with patch.object(
        codex_destination_render,
        "working_dir_for_reconstruction",
        return_value="/workspace",
    ):
        with pytest.raises(
            codex_runtime.CodexPreparedContextStale,
            match="model changed",
        ):
            codex_destination_render.codex_destination_scope(
                "switch-thread",
                target_catalog_id="codex/gpt-5-codex",
                db=db,
            )

        assert codex_destination_render.codex_destination_scope(
            "switch-thread",
            target_catalog_id="codex/gpt-5-codex",
            expected_selected_model="anthropic/claude-3-5-sonnet",
            db=db,
        ) == ("project-1", "/workspace")
    db.close()


def test_codex_switch_scope_accepts_an_explicit_unset_legacy_source() -> None:
    from subagents import codex_runtime

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, model TEXT, project_id TEXT)"
    )
    db.execute(
        "INSERT INTO threads (id,model,project_id) VALUES (?,?,?)",
        ("switch-thread", None, "project-1"),
    )
    with patch.object(
        codex_destination_render,
        "working_dir_for_reconstruction",
        return_value="/workspace",
    ):
        assert codex_destination_render.codex_destination_scope(
            "switch-thread",
            target_catalog_id="codex/gpt-5-codex",
            expected_selected_model="",
            db=db,
        ) == ("project-1", "/workspace")
    db.close()


def test_build_codex_reconstruction_uses_one_canonical_projection() -> None:
    from history.types import PortableHistoryProjection
    from subagents import codex_runtime

    projection = PortableHistoryProjection(
        format_version=1,
        thread_id="main-1",
        history_epoch=0,
        start_exclusive=None,
        sealed_boundary=None,
        turns=(),
        full_raw_replay_available=True,
    )
    db = object()
    expected = object()

    with (
        patch(
            "history.projection.render_portable_history",
            return_value=projection,
        ) as render,
        patch(
            "subagents.codex_context_reconstruction.build_codex_context_reconstruction",
            return_value=expected,
        ) as build,
        patch(
            "core.thread_history.compute_memory_window",
            side_effect=AssertionError("legacy window selector must not run"),
        ),
    ):
        result = codex_destination_render.build_codex_reconstruction(
            thread_id="main-1",
            text="current request",
            current_input_message_id="current-u",
            working_dir="/workspace",
            project_id="project-1",
            model="codex/gpt-5-codex",
            context_window=100_000,
            codex_thread_id=None,
            db=db,
        )

    assert result is expected
    render.assert_called_once_with(
        "main-1",
        db=db,
        exclude_message_id="current-u",
    )
    assert build.call_args.kwargs["history_projection"] is projection
    assert "history" not in build.call_args.kwargs


def test_prepare_codex_destination_payload_exposes_exact_preflight_surface() -> None:
    from core.context_replay import ReplaySelection
    from history.types import PortableHistoryProjection
    from subagents import codex_runtime

    projection = PortableHistoryProjection(
        format_version=1,
        thread_id="prepared-session",
        history_epoch=2,
        start_exclusive=None,
        sealed_boundary=None,
        turns=(),
        full_raw_replay_available=True,
    )
    replay = ReplaySelection(
        mode="checkpoint_tail",
        history_epoch=2,
        sealed_boundary=None,
        tail=projection,
        checkpoint_generation_id="generation-2",
        checkpoint_json='{"objective":"continue"}',
    )
    reconstruction = _fake_codex_reconstruction("frozen task")
    tool_registry = object()
    dynamic_tools = (
        {
            "name": "sentinel",
            "description": "Frozen tool",
            "inputSchema": {"type": "object", "properties": {}},
        },
    )

    with (
        patch(
            "subagents.codex_context_reconstruction.build_codex_context_reconstruction",
            return_value=reconstruction,
        ) as build,
        patch(
            "subagents.codex_destination_render.checkpoint_prompt",
            return_value="checkpoint frame",
        ),
        patch(
            "subagents.harness_tool_bridge.build_tool_registry",
            return_value=tool_registry,
        ),
        patch(
            "subagents.harness_tool_bridge.dynamic_tool_definitions",
            return_value=dynamic_tools,
        ),
        patch.object(
            codex_destination_render,
            "codex_destination_scope",
            return_value=("project-1", "/workspace"),
        ),
    ):
        destination = codex_destination_render.prepare_codex_destination_payload(
            thread_id="prepared-session",
            user_text="current request",
            target=_codex_target("gpt-5-codex"),
            replay_selection=replay,
            provider_resume=False,
            history_epoch=2,
            sealed_boundary=None,
            db=object(),
        )

    assert destination.reconstruction is reconstruction
    assert destination.current_user_text == "current request"
    assert destination.provider_text == "frozen task"
    assert destination.tool_registry is tool_registry
    assert destination.dynamic_tools is dynamic_tools
    assert build.call_args.kwargs["history_projection"] is projection
    assert build.call_args.kwargs["checkpoint_text"] == "checkpoint frame"


def test_codex_preflight_api_owns_current_session_scope() -> None:
    import inspect

    from infra.destination_payloads import _prepare_codex_preflight_destination
    from subagents.codex_destination_render import (
        prepare_codex_destination_payload,
    )

    assert (
        "working_dir"
        not in inspect.signature(prepare_codex_destination_payload).parameters
    )
    assert (
        "project_id"
        not in inspect.signature(prepare_codex_destination_payload).parameters
    )
    assert (
        "project_id"
        not in inspect.signature(_prepare_codex_preflight_destination).parameters
    )


def test_prepare_codex_destination_payload_freezes_provider_resume_boundary() -> None:
    from core.turn_core import TurnIdentity
    from history.types import HistoryBoundary
    from subagents import codex_runtime

    _reset_runtime()
    sealed = HistoryBoundary(
        thread_id="prepared-session",
        history_epoch=4,
        seq=12,
        message_id="assistant-12",
    )
    tool_registry = object()
    dynamic_tools = (
        {
            "name": "live_sentinel",
            "description": "Already registered",
            "inputSchema": {"type": "object", "properties": {}},
        },
    )
    session = MagicMock()
    session.thread_id = "provider-thread"
    session.tool_registry = tool_registry
    session.dynamic_tools = dynamic_tools
    session.config_overrides = _live_overrides(_codex_target("gpt-5-codex"))
    codex_runtime._LIVE.put(
        "prepared-session",
        codex_runtime._LiveCodex(
            codex_thread_id="provider-thread",
            session=session,
            parent_thread_id=None,
            name="live",
            model="gpt-5-codex",
            catalog_model_id="codex/gpt-5-codex",
            corsia_turn=TurnIdentity("active-turn", 1),
        ),
    )
    reconstruction = _fake_codex_reconstruction(
        "unused task",
        turn_text="frozen live frame",
    )
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = {"runtime_context_epoch": 0}

    with (
        patch(
            "subagents.codex_context_reconstruction.build_codex_context_reconstruction",
            return_value=reconstruction,
        ) as build,
        patch.object(
            codex_destination_render,
            "codex_destination_scope",
            return_value=("project-1", "/workspace"),
        ),
    ):
        destination = codex_destination_render.prepare_codex_destination_payload(
            thread_id="prepared-session",
            user_text="continue live",
            target=_codex_target("gpt-5-codex"),
            replay_selection=None,
            provider_resume=True,
            history_epoch=4,
            sealed_boundary=sealed,
            db=db,
        )

    projection = build.call_args.kwargs["history_projection"]
    assert projection.thread_id == "prepared-session"
    assert projection.history_epoch == 4
    assert projection.start_exclusive == sealed
    assert projection.sealed_boundary == sealed
    assert projection.turns == ()
    assert destination.provider_text == "frozen live frame"
    assert destination.tool_registry is tool_registry
    assert destination.dynamic_tools is dynamic_tools
    assert destination.handle_fingerprint == (
        codex_runtime.current_handle_fingerprint("prepared-session")
    )


def test_prepare_codex_destination_payload_freezes_persisted_resume_registry() -> None:
    from subagents import codex_runtime

    _reset_runtime()
    codex_runtime._CODEX_THREADS["prepared-session"] = "provider-thread"
    reconstruction = _fake_codex_reconstruction(
        "unused task",
        turn_text="current turn only",
    )
    registry = object()
    dynamic_tools = ({"name": "read_file"},)

    with (
        patch(
            "subagents.codex_context_reconstruction.build_codex_context_reconstruction",
            return_value=reconstruction,
        ),
        patch.object(
            codex_destination_render,
            "codex_destination_scope",
            return_value=("project-1", "/workspace"),
        ),
        patch(
            "subagents.harness_tool_bridge.build_tool_registry",
            return_value=registry,
        ),
        patch(
            "subagents.harness_tool_bridge.dynamic_tool_definitions",
            return_value=dynamic_tools,
        ),
    ):
        destination = codex_destination_render.prepare_codex_destination_payload(
            thread_id="prepared-session",
            user_text="continue after restart",
            target=_codex_target("gpt-5-codex"),
            replay_selection=None,
            provider_resume=True,
            history_epoch=4,
            sealed_boundary=None,
            db=object(),
        )

    assert destination.provider_thread_id == "provider-thread"
    assert destination.provider_text == "current turn only"
    assert destination.history_items == ()
    assert destination.tool_registry is registry
    assert destination.dynamic_tools is dynamic_tools


def test_prepare_live_destination_rejects_different_runtime_model(main_db) -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    main_db.execute(
        "UPDATE threads SET model=? WHERE id=?",
        ("codex/gpt-5.6-sol", "main-1"),
    )
    main_db.commit()
    session = MagicMock()
    session.thread_id = "provider-thread"
    session.working_dir = "/workspace"
    session.tool_registry = object()
    session.dynamic_tools = ()
    codex_runtime._LIVE.put(
        "main-1",
        codex_runtime._LiveCodex(
            codex_thread_id="provider-thread",
            session=session,
            parent_thread_id=None,
            name="live",
            model="gpt-5-codex",
            catalog_model_id="codex/gpt-5-codex",
            expected_reported_model="gpt-5-codex",
            provider_reported_model="gpt-5-codex",
            corsia_turn=TurnIdentity("active-turn", 1),
        ),
    )

    with (
        pytest.raises(
            codex_runtime.CodexPreparedContextStale,
            match="model",
        ),
        patch(
            "subagents.codex_context_reconstruction.build_codex_context_reconstruction",
            side_effect=AssertionError("mismatched handle must fail before rendering"),
        ),
    ):
        codex_destination_render.prepare_codex_destination_payload(
            thread_id="main-1",
            user_text="switch models",
            target=_codex_target("gpt-5.6-sol"),
            replay_selection=None,
            provider_resume=True,
            history_epoch=4,
            sealed_boundary=None,
            db=main_db,
        )


def test_prepare_live_destination_ignores_reported_model_without_target_evidence() -> (
    None
):
    from dataclasses import replace

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    session = MagicMock()
    session.thread_id = "provider-thread"
    session.working_dir = "/workspace"
    session.tool_registry = object()
    session.dynamic_tools = ()
    session.config_overrides = _live_overrides(_codex_target("gpt-5-codex"))
    codex_runtime._LIVE.put(
        "prepared-session",
        codex_runtime._LiveCodex(
            codex_thread_id="provider-thread",
            session=session,
            parent_thread_id=None,
            name="live",
            model="gpt-5-codex",
            catalog_model_id="codex/gpt-5-codex",
            expected_reported_model="gpt-5-codex",
            provider_reported_model="gpt-5-codex",
            corsia_turn=TurnIdentity("active-turn", 1),
        ),
    )
    target = replace(
        _codex_target("gpt-5-codex"),
        expected_reported_model=None,
    )
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = {"runtime_context_epoch": 0}

    with (
        patch(
            "subagents.codex_context_reconstruction.build_codex_context_reconstruction",
            return_value=_fake_codex_reconstruction(
                "unused",
                turn_text="frozen live frame",
            ),
        ),
        patch.object(
            codex_destination_render,
            "codex_destination_scope",
            return_value=("project-1", "/workspace"),
        ),
    ):
        destination = codex_destination_render.prepare_codex_destination_payload(
            thread_id="prepared-session",
            user_text="continue live",
            target=target,
            replay_selection=None,
            provider_resume=True,
            history_epoch=4,
            sealed_boundary=None,
            db=db,
        )

    assert destination.provider_text == "frozen live frame"


def test_prepare_destination_rejects_concurrent_session_scope_change(
    main_db,
    tmp_path,
    monkeypatch,
) -> None:
    from core.context_replay import ReplaySelection
    from history.types import PortableHistoryProjection
    from subagents import codex_runtime
    from tests.project_ref_helpers import project_ref, select_workspace

    from infra.config import WORKSPACE

    select_workspace(monkeypatch, WORKSPACE)
    current_dir = WORKSPACE / "projects" / "current"
    replacement_dir = WORKSPACE / "projects" / "replacement"
    current_dir.mkdir(parents=True)
    replacement_dir.mkdir(parents=True)
    main_db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES (?,?,?,?,?)",
        ("current-project", "Current", project_ref("current"), "t", "t"),
    )
    main_db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES (?,?,?,?,?)",
        ("replacement-project", "Replacement", project_ref("replacement"), "t", "t"),
    )
    main_db.execute(
        "UPDATE threads SET model=?, project_id=? WHERE id=?",
        ("codex/gpt-5-codex", "current-project", "main-1"),
    )
    main_db.commit()
    projection = PortableHistoryProjection(
        format_version=1,
        thread_id="main-1",
        history_epoch=2,
        start_exclusive=None,
        sealed_boundary=None,
        turns=(),
        full_raw_replay_available=True,
    )
    replay = ReplaySelection(
        mode="exact",
        history_epoch=2,
        sealed_boundary=None,
        tail=projection,
    )
    rendered_scope: dict[str, object] = {}

    def mutate_scope_during_render(**kwargs):
        rendered_scope.update(kwargs)
        main_db.execute(
            "UPDATE threads SET model=?, project_id=? WHERE id=?",
            ("codex/gpt-5.6-sol", "replacement-project", "main-1"),
        )
        main_db.commit()
        return _fake_codex_reconstruction("frozen task")

    with (
        pytest.raises(
            codex_runtime.CodexPreparedContextStale,
            match="scope changed",
        ),
        patch(
            "subagents.codex_context_reconstruction.build_codex_context_reconstruction",
            side_effect=mutate_scope_during_render,
        ),
        patch(
            "subagents.harness_tool_bridge.build_tool_registry",
            return_value=object(),
        ),
        patch(
            "subagents.harness_tool_bridge.dynamic_tool_definitions",
            return_value=(),
        ),
    ):
        codex_destination_render.prepare_codex_destination_payload(
            thread_id="main-1",
            user_text="current request",
            target=_codex_target("gpt-5-codex"),
            replay_selection=replay,
            provider_resume=False,
            history_epoch=2,
            sealed_boundary=None,
            db=main_db,
        )

    assert rendered_scope["project_id"] == "current-project"
    assert rendered_scope["working_dir"] == str(current_dir)


def test_prepare_destination_accepts_equivalent_working_dir_spellings() -> None:
    target = _codex_target("gpt-5-codex")

    with patch.object(
        codex_destination_render,
        "codex_destination_scope",
        return_value=("project-1", "/workspace/./"),
    ):
        codex_destination_render._require_unchanged_scope(
            thread_id="prepared-session",
            target=target,
            expected_selected_model=None,
            project_id="project-1",
            working_dir="/workspace",
            db=object(),
        )


@pytest.mark.asyncio
async def test_feed_wrappers_forward_prepared_context() -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    prepared = object()
    refresh = AsyncMock()
    expected = TurnIdentity("prepared-turn", 1)
    ensure = AsyncMock(return_value=expected)

    with patch.object(codex_runtime, "ensure_live_then_feed", new=ensure):
        assert (
            await codex_runtime.feed(
                "prepared-feed",
                "continue",
                target=_codex_target("gpt-5-codex"),
                successor_turn_id="prepared-turn",
                bind_input_turn=_bind_input_turn,
                project_id="project-sub",
                prepared_context=prepared,
                refresh_prepared_context=refresh,
            )
            == expected
        )
        assert (
            await codex_runtime.feed_main(
                "prepared-main",
                "continue",
                target=_codex_target("gpt-5-codex"),
                successor_turn_id="prepared-turn",
                bind_input_turn=_bind_input_turn,
                prepared_context=prepared,
                refresh_prepared_context=refresh,
            )
            == expected
        )

    assert ensure.await_count == 2
    assert all(
        call.kwargs["prepared_context"] is prepared for call in ensure.await_args_list
    )
    assert all(
        call.kwargs["refresh_prepared_context"] is refresh
        for call in ensure.await_args_list
    )
    assert ensure.await_args_list[0].kwargs["project_id"] == "project-sub"


@pytest.mark.asyncio
async def test_cold_feed_main_forwards_prepared_replay_to_spawn() -> None:
    from dataclasses import replace

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    prepared = _prepared_codex_context(
        _fake_codex_reconstruction("frozen cold task"),
        mode="exact",
        thread_id="cold-prepared",
        current_user_text="continue from prepared state",
    )
    expected = TurnIdentity("cold-prepared-turn", 1)
    spawn = AsyncMock(return_value=expected)
    refreshed = replace(prepared)
    refresh = AsyncMock(return_value=refreshed)

    with (
        patch.object(
            codex_runtime,
            "_codex_connection",
            return_value={"status": "available"},
        ),
        patch.object(
            codex_runtime,
            "_working_dir_for_reconstruction",
            return_value="/workspace",
        ),
        patch.object(
            codex_runtime,
            "_spawn_turn",
            new=spawn,
        ),
    ):
        result = await codex_runtime.feed_main(
            "cold-prepared",
            "continue from prepared state",
            target=_codex_target("gpt-5-codex"),
            successor_turn_id=expected.turn_id,
            bind_input_turn=_bind_input_turn,
            prepared_context=prepared,
            refresh_prepared_context=refresh,
        )

    assert result == expected
    refresh.assert_awaited_once()
    assert spawn.await_args.kwargs["codex_thread_id"] is None
    assert spawn.await_args.kwargs["prepared_context"] is refreshed


@pytest.mark.asyncio
async def test_checkpoint_tail_retires_resume_handle_before_cold_spawn() -> None:
    from dataclasses import replace

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = "checkpoint-tail-cutover"
    codex_runtime._CODEX_THREADS[thread_id] = "old-provider-thread"
    prepared = _prepared_codex_context(
        _fake_codex_reconstruction("frozen checkpoint and tail"),
        mode="checkpoint_tail",
        thread_id=thread_id,
        current_user_text="continue from checkpoint",
    )
    expected = TurnIdentity("checkpoint-tail-turn", 1)
    spawn = AsyncMock(return_value=expected)
    refreshed = replace(prepared)
    refresh = AsyncMock(return_value=refreshed)

    async def retire_old_handle(*_args, **_kwargs):
        codex_runtime._CODEX_THREADS.pop(thread_id, None)
        return True

    retire = AsyncMock(side_effect=retire_old_handle)
    with (
        patch.object(codex_runtime, "_retire_locked", new=retire),
        patch.object(
            codex_runtime,
            "_restore_thread_id",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            codex_runtime,
            "_working_dir_for_reconstruction",
            return_value="/workspace",
        ),
        patch.object(codex_runtime, "_spawn_turn", new=spawn),
    ):
        result = await codex_runtime.feed(
            thread_id,
            "continue from checkpoint",
            target=_codex_target("gpt-5-codex"),
            successor_turn_id=expected.turn_id,
            bind_input_turn=_bind_input_turn,
            prepared_context=prepared,
            refresh_prepared_context=refresh,
        )

    assert result == expected
    refresh.assert_awaited_once()
    retire.assert_awaited_once()
    assert spawn.await_args.kwargs["codex_thread_id"] is None
    assert spawn.await_args.kwargs["prepared_context"] is refreshed


@pytest.mark.asyncio
async def test_declined_steer_never_hard_preempts_the_live_turn() -> None:
    """A normal send is never a preempt: a declined steer defers the input.

    The shared send service already owns a durable inbox row for this input;
    terminal promotion starts its successor after the live turn ends.  The
    runtime must therefore return ``None`` without terminalizing, quarantining,
    or replacing the still-running turn.
    """
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    live_session = MagicMock()
    live_session.steer = AsyncMock(return_value=False)
    live_session.interrupt = AsyncMock(return_value=False)
    live_session.start_turn = AsyncMock(return_value=False)
    codex_runtime._LIVE.put(
        "hard-preempt-prepared",
        codex_runtime._LiveCodex(
            codex_thread_id="provider-thread",
            session=live_session,
            parent_thread_id=None,
            name="test",
            model="gpt-5-codex",
            catalog_model_id="codex/gpt-5-codex",
            expected_reported_model="gpt-5-codex",
            provider_reported_model="gpt-5-codex",
            corsia_turn=TurnIdentity("old-turn", 1),
        ),
    )
    prepared = _prepared_codex_context(
        _fake_codex_reconstruction("prepared", turn_text="prepared frame"),
        mode="provider_resume",
        handle_fingerprint=codex_runtime.current_handle_fingerprint(
            "hard-preempt-prepared"
        ),
        thread_id="hard-preempt-prepared",
        current_user_text="try to replace the busy turn",
    )
    boundary_prepared = _prepared_codex_context(
        _fake_codex_reconstruction(
            "boundary",
            turn_text="boundary prepared frame",
        ),
        mode="provider_resume",
        handle_fingerprint=codex_runtime.current_handle_fingerprint(
            "hard-preempt-prepared"
        ),
        tool_registry=live_session.tool_registry,
        dynamic_tools=live_session.dynamic_tools,
        thread_id="hard-preempt-prepared",
        current_user_text="try to replace the busy turn",
    )
    refresh = AsyncMock(return_value=boundary_prepared)

    with pytest.raises(codex_runtime.CodexLiveTurnBusy):
        await codex_runtime.feed_main(
            "hard-preempt-prepared",
            "try to replace the busy turn",
            target=_codex_target("gpt-5-codex"),
            successor_turn_id="never-started-successor",
            bind_input_turn=_bind_input_turn,
            prepared_context=prepared,
            refresh_prepared_context=refresh,
        )

    live_session.steer.assert_awaited_once()
    live_session.interrupt.assert_not_awaited()
    live_session.start_turn.assert_not_awaited()
    # The declined turn keeps its live handle and its original CORSIA Turn.
    live = codex_runtime._LIVE.get("hard-preempt-prepared")
    assert live is not None
    assert live.corsia_turn == TurnIdentity("old-turn", 1)


@pytest.mark.parametrize(
    ("model", "expected_reported_model"),
    [
        ("gpt-5-codex", "gpt-5-codex"),
        ("gpt-5.6-terra", "gpt-5.6-terra"),
    ],
    ids=["same-model", "model-switch"],
)
@pytest.mark.asyncio
async def test_live_session_without_steer_support_declines_input(
    model,
    expected_reported_model,
) -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = "no-steer-support"
    session = SimpleNamespace()
    live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=session,
        parent_thread_id=None,
        name="codex",
        model="gpt-5-codex",
        catalog_model_id="codex/gpt-5-codex",
        expected_reported_model="gpt-5-codex",
        provider_reported_model="gpt-5-codex",
        corsia_turn=TurnIdentity("current-turn", 1),
    )
    codex_runtime._LIVE.put(thread_id, live)

    with patch.object(
        codex_runtime,
        "_live_provider_text",
        new=AsyncMock(return_value="provider frame"),
    ):
        result = await codex_runtime._feed_live(
            thread_id,
            "new input",
            successor_turn_id="successor-turn",
            bind_input_turn=_bind_input_turn,
            model=model,
            expected_reported_model=expected_reported_model,
        )

    assert result is None
    assert codex_runtime._LIVE.get(thread_id) is live


@pytest.mark.asyncio
async def test_hard_preempt_refreshes_after_old_handle_is_proven_dead() -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = "hard-preempt-refresh"
    live = codex_runtime._LiveCodex(
        codex_thread_id="old-provider-thread",
        session=MagicMock(),
        parent_thread_id=None,
        name="old",
        model="gpt-5-codex",
        catalog_model_id="codex/gpt-5-codex",
        corsia_turn=TurnIdentity("old-turn", 1),
    )
    codex_runtime._LIVE.put(thread_id, live)
    prepared = _prepared_codex_context(
        _fake_codex_reconstruction("old", turn_text="old frame"),
        mode="provider_resume",
        handle_fingerprint=codex_runtime.current_handle_fingerprint(thread_id),
        thread_id=thread_id,
    )
    refreshed = _prepared_codex_context(
        _fake_codex_reconstruction("fresh cold task"),
        mode="exact",
        thread_id=thread_id,
    )
    refresh = AsyncMock(return_value=refreshed)
    replacement = TurnIdentity("replacement-turn", 2)
    spawn = AsyncMock(return_value=replacement)

    async def finish_and_remove(*_args, **_kwargs):
        codex_runtime._LIVE.pop(thread_id, None)
        codex_runtime._APP_SESSIONS.pop(thread_id, None)
        return True

    with (
        patch.object(
            codex_runtime,
            "_quiesce_tool_turn",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            codex_runtime,
            "_finish_fenced_live",
            new=AsyncMock(side_effect=finish_and_remove),
        ),
        patch.object(
            codex_runtime,
            "get_thread_project_id",
            return_value=None,
        ),
        patch.object(
            codex_runtime,
            "_working_dir_for_reconstruction",
            return_value="/workspace",
        ),
        patch.object(
            codex_runtime,
            "_spawn_turn",
            new=spawn,
        ),
    ):
        result = await codex_runtime._hard_preempt_live_turn(
            thread_id,
            live,
            successor_turn_id=replacement.turn_id,
            text="mutable caller text",
            project_id=None,
            model="gpt-5-codex",
            catalog_model_id="codex/gpt-5-codex",
            prepared_context=prepared,
            refresh_prepared_context=refresh,
            held_lock=codex_runtime._LIVE.lock(thread_id),
        )

    assert result == replacement
    refresh.assert_awaited_once()
    assert spawn.await_args.kwargs["prepared_context"] is refreshed
    assert spawn.await_args.kwargs["codex_thread_id"] is None


@pytest.mark.asyncio
async def test_hard_preempt_without_refresh_fails_before_replacement_write() -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = "hard-preempt-no-refresh"
    live = codex_runtime._LiveCodex(
        codex_thread_id="old-provider-thread",
        session=MagicMock(),
        parent_thread_id=None,
        name="old",
        model="gpt-5-codex",
        catalog_model_id="codex/gpt-5-codex",
        corsia_turn=TurnIdentity("old-turn", 1),
    )
    codex_runtime._LIVE.put(thread_id, live)
    prepared = _prepared_codex_context(
        _fake_codex_reconstruction("old", turn_text="old frame"),
        mode="provider_resume",
        handle_fingerprint=codex_runtime.current_handle_fingerprint(thread_id),
        thread_id=thread_id,
    )
    spawn = AsyncMock()

    async def finish_and_remove(*_args, **_kwargs):
        codex_runtime._LIVE.pop(thread_id, None)
        codex_runtime._APP_SESSIONS.pop(thread_id, None)
        return True

    with (
        patch.object(
            codex_runtime,
            "_quiesce_tool_turn",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            codex_runtime,
            "_finish_fenced_live",
            new=AsyncMock(side_effect=finish_and_remove),
        ),
        patch.object(
            codex_runtime,
            "get_thread_project_id",
            return_value=None,
        ),
        patch.object(
            codex_runtime,
            "_working_dir_for_reconstruction",
            return_value="/workspace",
        ),
        patch.object(
            codex_runtime,
            "_spawn_turn",
            new=spawn,
        ),
    ):
        with pytest.raises(
            codex_runtime.CodexPreparedContextStale,
            match="re-preflight",
        ):
            await codex_runtime._hard_preempt_live_turn(
                thread_id,
                live,
                successor_turn_id="replacement-turn",
                text="mutable caller text",
                project_id=None,
                model="gpt-5-codex",
                catalog_model_id="codex/gpt-5-codex",
                prepared_context=prepared,
                held_lock=codex_runtime._LIVE.lock(thread_id),
            )

    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_steer_consumes_refreshed_frame_and_declines_without_interrupt() -> (
    None
):
    """The live steer uses the boundary-refreshed payload, never a rebuild.

    When Codex declines that steer, the input is left to the durable inbox and
    terminal promotion — the runtime must not interrupt the live turn, start a
    replacement, or fall back to rebuilding a frame from scratch.
    """
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    _seed_runtime_sessions("healthy-prepared")
    live_session = MagicMock()
    live_session.steer = AsyncMock(return_value=False)
    live_session.interrupt = AsyncMock(return_value=True)
    live_session.start_turn = AsyncMock(return_value=True)
    live = codex_runtime._LiveCodex(
        codex_thread_id="healthy-provider-thread",
        session=live_session,
        parent_thread_id=None,
        name="test",
        model="gpt-5-codex",
        catalog_model_id="codex/gpt-5-codex",
        expected_reported_model="gpt-5-codex",
        provider_reported_model="gpt-5-codex",
        corsia_turn=TurnIdentity("healthy-turn", 1),
    )
    codex_runtime._LIVE.put("healthy-prepared", live)
    prepared = _prepared_codex_context(
        _fake_codex_reconstruction("prepared", turn_text="prepared frame"),
        mode="provider_resume",
        handle_fingerprint=codex_runtime.current_handle_fingerprint("healthy-prepared"),
        thread_id="healthy-prepared",
        current_user_text="continue live",
    )
    refreshed = _prepared_codex_context(
        _fake_codex_reconstruction(
            "refreshed",
            turn_text="refreshed live-boundary frame",
        ),
        mode="provider_resume",
        handle_fingerprint=codex_runtime.current_handle_fingerprint("healthy-prepared"),
        tool_registry=live_session.tool_registry,
        dynamic_tools=live_session.dynamic_tools,
        thread_id="healthy-prepared",
        current_user_text="continue live",
    )
    refresh = AsyncMock(return_value=refreshed)

    with (
        patch(
            "subagents.codex_spawn_preflight.build_codex_reconstruction",
            side_effect=AssertionError("safe boundary must consume refreshed payload"),
        ) as build,
        pytest.raises(codex_runtime.CodexLiveTurnBusy),
    ):
        await codex_runtime.feed_main(
            "healthy-prepared",
            "continue live",
            target=_codex_target("gpt-5-codex"),
            successor_turn_id="unused-successor",
            bind_input_turn=_bind_input_turn,
            prepared_context=prepared,
            refresh_prepared_context=refresh,
        )

    live_session.steer.assert_awaited_once_with(
        "refreshed live-boundary frame",
        on_write_observed=ANY,
        on_drain_confirmed=ANY,
    )
    live_session.interrupt.assert_not_awaited()
    live_session.start_turn.assert_not_awaited()
    build.assert_not_called()


@pytest.mark.asyncio
async def test_declined_steer_never_terminates_provider_resume_for_cold_rebuild() -> None:
    """A declined live steer must not escalate into terminate-and-respawn.

    Even when a refreshed cold payload would be available, an ordinary send
    leaves the running turn alone: the durable inbox retains the input and the
    terminal successor path owns any later rebuild.
    """
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = "interrupt-to-cold"
    provider_session = MagicMock()
    provider_session.steer = AsyncMock(return_value=False)
    provider_session.interrupt = AsyncMock(return_value=True)
    provider_session.start_turn = AsyncMock()
    live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=provider_session,
        parent_thread_id=None,
        name="live",
        model="gpt-5-codex",
        catalog_model_id="codex/gpt-5-codex",
        expected_reported_model="gpt-5-codex",
        provider_reported_model="gpt-5-codex",
        corsia_turn=TurnIdentity("old-turn", 1),
    )
    codex_runtime._LIVE.put(thread_id, live)
    prepared = _prepared_codex_context(
        _fake_codex_reconstruction("live", turn_text="live frame"),
        mode="provider_resume",
        handle_fingerprint=codex_runtime.current_handle_fingerprint(thread_id),
        thread_id=thread_id,
        current_user_text="context-limit input",
    )
    live_boundary = _prepared_codex_context(
        _fake_codex_reconstruction(
            "live boundary",
            turn_text="live boundary frame",
        ),
        mode="provider_resume",
        handle_fingerprint=codex_runtime.current_handle_fingerprint(thread_id),
        tool_registry=provider_session.tool_registry,
        dynamic_tools=provider_session.dynamic_tools,
        thread_id=thread_id,
        current_user_text="context-limit input",
    )
    events: list[str] = []

    async def refresh():
        events.append("refresh")
        return live_boundary

    async def terminate(*_args, **_kwargs):
        events.append("terminate")
        return True

    async def spawn(*_args, **_kwargs):
        events.append("spawn")
        return TurnIdentity("replacement-turn", 2)

    with (
        patch.object(
            codex_runtime,
            "_terminate_exact_live",
            new=AsyncMock(side_effect=terminate),
        ),
        patch.object(
            codex_runtime,
            "_spawn_turn",
            new=AsyncMock(side_effect=spawn),
        ),
        patch.object(
            codex_runtime,
            "get_thread_project_id",
            return_value=None,
        ),
        patch.object(
            codex_runtime,
            "_working_dir_for_reconstruction",
            return_value="/workspace",
        ),
        pytest.raises(codex_runtime.CodexLiveTurnBusy),
    ):
        await codex_runtime.feed_main(
            thread_id,
            "context-limit input",
            target=_codex_target("gpt-5-codex"),
            successor_turn_id="never-started-successor",
            bind_input_turn=_bind_input_turn,
            prepared_context=prepared,
            refresh_prepared_context=refresh,
        )

    provider_session.steer.assert_awaited_once()
    provider_session.interrupt.assert_not_awaited()
    assert "terminate" not in events
    assert "spawn" not in events
    assert codex_runtime._LIVE.get(thread_id) is live
    provider_session.steer.assert_awaited_once_with(
        "live boundary frame",
        on_write_observed=ANY,
        on_drain_confirmed=ANY,
    )
    provider_session.start_turn.assert_not_awaited()


def test_spawn_turn_consumes_the_frozen_cold_destination_without_rebuilding() -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    reconstruction = _fake_codex_reconstruction(
        "frozen cold task",
        history_items=(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "frozen history"}],
            },
        ),
    )
    frozen_registry = object()
    frozen_tools = (
        {
            "name": "preflight_sentinel",
            "description": "Must not be rebuilt",
            "inputSchema": {"type": "object", "properties": {}},
        },
    )
    prepared = _prepared_codex_context(
        reconstruction,
        mode="exact",
        tool_registry=frozen_registry,
        dynamic_tools=frozen_tools,
    )
    spawn = AsyncMock(return_value=TurnIdentity("frozen-turn", 1))

    with (
        patch.object(
            codex_spawn_preflight,
            "codex_connection",
            return_value={"status": "available"},
        ),
        patch(
            "subagents.codex_spawn_preflight.build_codex_reconstruction",
            side_effect=AssertionError("runtime must consume the frozen destination"),
        ),
            patch.object(
                codex_runtime,
                "_spawn_app_server_turn",
                new=spawn,
            ),
        ):
        result = _run(
            locked_spawn(
                codex_runtime._spawn_turn,
                "prepared-session",
                "mutable caller text",
                successor_turn_id="frozen-turn",
                codex_thread_id=None,
                binary="codex",
                catalog_model_id="codex/gpt-5-codex",
                prepared_context=prepared,
            )
        )

    assert result == TurnIdentity("frozen-turn", 1)
    await_args = spawn.await_args
    assert await_args is not None
    assert await_args.args[1] == "frozen cold task"
    assert await_args.kwargs["current_user_text"] == "mutable caller text"
    assert await_args.kwargs["history_items"] is reconstruction.history_items
    assert await_args.kwargs["tool_registry"] is frozen_registry
    assert await_args.kwargs["dynamic_tools"] is frozen_tools


def test_explicit_spawn_binary_still_validates_codex_connection() -> None:
    with patch.object(
        codex_spawn_preflight,
        "codex_connection",
        return_value={"status": "available"},
    ) as connection:
        assert codex_spawn_preflight.spawn_binary("/bin/codex", object()) == (
            "/bin/codex"
        )

    connection.assert_called_once()


def test_spawn_turn_rejects_prepared_payload_for_different_input() -> None:
    from subagents import codex_runtime

    prepared = _prepared_codex_context(
        _fake_codex_reconstruction("frozen task"),
        mode="exact",
        current_user_text="admitted input",
    )
    spawn = AsyncMock()

    with (
        patch.object(
            codex_spawn_preflight,
            "codex_connection",
            return_value={"status": "available"},
        ),
        patch.object(
            codex_runtime,
            "_spawn_app_server_turn",
            new=spawn,
        ),
    ):
        with pytest.raises(
            codex_runtime.CodexPreparedContextStale,
            match="thread/model/input",
        ):
            _run(
                locked_spawn(
                    codex_runtime._spawn_turn,
                    "prepared-session",
                    "different input",
                    successor_turn_id="frozen-turn",
                    codex_thread_id=None,
                    binary="codex",
                    catalog_model_id="codex/gpt-5-codex",
                    prepared_context=prepared,
                )
            )

    spawn.assert_not_awaited()


def test_app_server_consumes_frozen_tool_surface_without_runtime_rebuild() -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    frozen_registry = object()
    changed_registry = object()
    frozen_tools = (
        {
            "name": "preflight_sentinel",
            "description": "Frozen before runtime mutation",
            "inputSchema": {"type": "object", "properties": {}},
        },
    )
    changed_tools = (
        {
            "name": "runtime_mutation",
            "description": "Must not reach Codex",
            "inputSchema": {"type": "object", "properties": {}},
        },
    )
    captured: dict = {}

    def app_ctor(**kwargs):
        captured.update(kwargs)
        return _make_mock_app_session("provider-thread")

    with (
        patch(
            "subagents.codex_app_server.CodexAppServerSession",
            side_effect=app_ctor,
        ),
        patch.object(
            codex_runtime,
            "_start_live_turn",
            new=AsyncMock(return_value=TurnIdentity("frozen-turn", 1)),
        ),
        patch(
            "subagents.harness_tool_bridge.build_tool_registry",
            return_value=changed_registry,
        ) as rebuild_registry,
        patch(
            "subagents.harness_tool_bridge.dynamic_tool_definitions",
            return_value=changed_tools,
        ) as rebuild_definitions,
    ):
        result = _run(
            locked_spawn(
                codex_runtime._spawn_app_server_turn,
                "prepared-session",
                "frozen task",
                current_user_text="frozen task",
                successor_turn_id="frozen-turn",
                codex_thread_id=None,
                binary="codex",
                working_dir="/workspace",
                windows=WindowFacts(200_000, None),
                broadcast=lambda *_args: None,
                tool_registry=frozen_registry,
                dynamic_tools=frozen_tools,
            )
        )

    assert result is not None
    assert captured["tool_registry"] is frozen_registry
    assert captured["dynamic_tools"] is frozen_tools
    rebuild_registry.assert_not_called()
    rebuild_definitions.assert_not_called()


@pytest.mark.asyncio
async def test_same_provider_resume_refreshes_exact_payload_before_provider_write() -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = "same-handle-refresh"
    provider_session = MagicMock()
    provider_session.steer = AsyncMock(return_value=True)
    live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=provider_session,
        parent_thread_id=None,
        name="live",
        model="gpt-5-codex",
        catalog_model_id="codex/gpt-5-codex",
        expected_reported_model="gpt-5-codex",
        provider_reported_model="gpt-5-codex",
        corsia_turn=TurnIdentity("live-turn", 1),
    )
    codex_runtime._LIVE.put(thread_id, live)
    frozen_registry = object()
    frozen_tools = (
        {
            "name": "already_advertised",
            "description": "stable tool surface",
            "inputSchema": {"type": "object", "properties": {}},
        },
    )
    fingerprint = codex_runtime.current_handle_fingerprint(thread_id)
    stale = _prepared_codex_context(
        _fake_codex_reconstruction("stale", turn_text="stale frozen frame"),
        mode="provider_resume",
        handle_fingerprint=fingerprint,
        tool_registry=frozen_registry,
        dynamic_tools=frozen_tools,
        thread_id=thread_id,
        current_user_text="continue live",
    )
    refreshed = _prepared_codex_context(
        _fake_codex_reconstruction(
            "refreshed",
            turn_text="fresh same-handle frame",
        ),
        mode="provider_resume",
        handle_fingerprint=fingerprint,
        tool_registry=frozen_registry,
        dynamic_tools=frozen_tools,
        thread_id=thread_id,
        current_user_text="continue live",
    )
    refresh = AsyncMock(return_value=refreshed)

    result = await codex_runtime.feed_main(
        thread_id,
        "continue live",
        target=_codex_target("gpt-5-codex"),
        successor_turn_id="unused-successor",
        bind_input_turn=_bind_input_turn,
        prepared_context=stale,
        refresh_prepared_context=refresh,
    )

    assert result == live.corsia_turn
    refresh.assert_awaited_once()
    provider_session.steer.assert_awaited_once_with(
        "fresh same-handle frame",
        on_write_observed=ANY,
        on_drain_confirmed=ANY,
    )


@pytest.mark.asyncio
async def test_replaced_provider_resume_refreshes_before_provider_write() -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    first_session = MagicMock()
    first_session.steer = AsyncMock(return_value=True)
    first_live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=first_session,
        parent_thread_id=None,
        name="first",
        model="gpt-5-codex",
        catalog_model_id="codex/gpt-5-codex",
        expected_reported_model="gpt-5-codex",
        provider_reported_model="gpt-5-codex",
        corsia_turn=TurnIdentity("first-turn", 1),
    )
    codex_runtime._LIVE.put("prepared-session", first_live)
    stale = _prepared_codex_context(
        _fake_codex_reconstruction("stale", turn_text="stale frozen frame"),
        mode="provider_resume",
        handle_fingerprint=codex_runtime.current_handle_fingerprint("prepared-session"),
    )

    second_session = MagicMock()
    second_session.steer = AsyncMock(return_value=True)
    second_live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=second_session,
        parent_thread_id=None,
        name="second",
        model="gpt-5-codex",
        catalog_model_id="codex/gpt-5-codex",
        expected_reported_model="gpt-5-codex",
        provider_reported_model="gpt-5-codex",
        corsia_turn=TurnIdentity("second-turn", 2),
    )
    codex_runtime._LIVE.put("prepared-session", second_live)
    refreshed = _prepared_codex_context(
        _fake_codex_reconstruction(
            "refreshed",
            turn_text="refreshed frozen frame",
        ),
        mode="provider_resume",
        handle_fingerprint=codex_runtime.current_handle_fingerprint("prepared-session"),
    )
    refresh = AsyncMock(return_value=refreshed)

    result = await codex_runtime.feed_main(
        "prepared-session",
        "mutable caller text",
        target=_codex_target("gpt-5-codex"),
        successor_turn_id="unused-successor",
        bind_input_turn=_bind_input_turn,
        prepared_context=stale,
        refresh_prepared_context=refresh,
    )

    assert result == second_live.corsia_turn
    refresh.assert_awaited_once()
    first_session.steer.assert_not_awaited()
    second_session.steer.assert_awaited_once_with(
        "refreshed frozen frame",
        on_write_observed=ANY,
        on_drain_confirmed=ANY,
    )


@pytest.mark.asyncio
async def test_replaced_provider_resume_without_refresh_fails_before_provider_write() -> (
    None
):
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime

    _reset_runtime()
    original = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=MagicMock(),
        parent_thread_id=None,
        name="first",
        corsia_turn=TurnIdentity("first-turn", 1),
    )
    codex_runtime._LIVE.put("prepared-session", original)
    stale = _prepared_codex_context(
        _fake_codex_reconstruction("stale", turn_text="stale frame"),
        mode="provider_resume",
        handle_fingerprint=codex_runtime.current_handle_fingerprint("prepared-session"),
    )
    replacement_session = MagicMock()
    replacement_session.steer = AsyncMock(return_value=True)
    codex_runtime._LIVE.put(
        "prepared-session",
        codex_runtime._LiveCodex(
            codex_thread_id="provider-thread",
            session=replacement_session,
            parent_thread_id=None,
            name="replacement",
            model="gpt-5-codex",
            catalog_model_id="codex/gpt-5-codex",
            expected_reported_model="gpt-5-codex",
            provider_reported_model="gpt-5-codex",
            corsia_turn=TurnIdentity("replacement-turn", 2),
        ),
    )

    with pytest.raises(codex_runtime.CodexPreparedContextStale):
        await codex_runtime.feed_main(
            "prepared-session",
            "mutable caller text",
            target=_codex_target("gpt-5-codex"),
            successor_turn_id="unused-successor",
            bind_input_turn=_bind_input_turn,
            prepared_context=stale,
        )

    replacement_session.steer.assert_not_awaited()


class _NoCloseConnection:
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


def _seed_portable_exchange(
    db,
    *,
    turn_id: str,
    seq: int,
    user_id: str,
    user_text: str,
    assistant_id: str,
    assistant_text: str,
    thread_id: str = "main-1",
) -> int:
    """Seed one terminal Turn in the exact-history shape runtime consumes."""
    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch) "
        "VALUES (?,?,0,'codex/gpt-5-codex',?,'start','end','test-boot')",
        (turn_id, thread_id, "completed"),
    )
    metadata = json.dumps({"turn_id": turn_id})
    db.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,'user-time',?)",
        (user_id, thread_id, "user", user_text, metadata, seq),
    )
    db.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,'assistant-time',?)",
        (
            assistant_id,
            thread_id,
            "assistant",
            assistant_text,
            metadata,
            seq + 1,
        ),
    )
    return seq + 2


def _seed_current_user(
    db,
    *,
    message_id: str,
    seq: int,
    content: str,
    thread_id: str = "main-1",
) -> object:
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input

    del seq
    db.commit()
    admission = admit_input(
        thread_id,
        role="user",
        content=content,
        intent="apply_now",
        message_id=message_id,
        turn_id=f"pending-{message_id}",
        boot_epoch=BOOT_EPOCH,
        admitted_at="current-time",
        db=db,
    )
    assert admission.batch is not None
    return admission.batch.identity


class TestFeedMain:
    def setup_method(self):
        _reset_runtime()

    def test_feed_main_returns_none_if_dead(self):
        from subagents import codex_runtime

        codex_runtime._DEAD.add("main-dead")
        result = _run(
            codex_runtime.feed_main(
                "main-dead",
                "hello",
                target=_codex_target("gpt-5-codex"),
                successor_turn_id=_successor_turn_id(),
                bind_input_turn=_bind_input_turn,
            )
        )
        assert result is None

    def test_real_spawn_helpers_return_successor_identity_on_success(self):
        """The app-server helper returns the exact admitted CORSIA Turn."""
        from core.turn_core import TurnIdentity
        from infra.database import get_db
        from subagents import codex_runtime

        def persisted_identity(thread_id: str) -> TurnIdentity:
            db = get_db()
            try:
                row = db.execute(
                    "SELECT id, turn_order FROM active_turns "
                    "WHERE thread_id = ? AND status = 'running'",
                    (thread_id,),
                ).fetchone()
            finally:
                db.close()
            assert row is not None
            return TurnIdentity(turn_id=row["id"], turn_order=row["turn_order"])

        _seed_runtime_sessions("s-app")
        app_successor = _successor_turn_id()
        preadmitted = _admit_active_turn("s-app", turn_id=app_successor)
        with patch(
            "subagents.codex_app_server.CodexAppServerSession",
            side_effect=lambda **_kwargs: _make_mock_app_session("thr"),
        ):
            app_identity = _run(
                locked_spawn(
                    codex_runtime._spawn_app_server_turn,
                    "s-app",
                    "hi",
                    current_user_text="hi",
                    codex_thread_id=None,
                    binary="codex",
                    successor_turn_id=app_successor,
                    working_dir=None,
                    windows=WindowFacts(200_000, None),
                    broadcast=lambda *_args: None,
                    context_snapshot=None,
                    preadmitted_turn=preadmitted,
                )
            )
        stored_app_identity = persisted_identity("s-app")
        assert stored_app_identity.turn_id == app_successor
        assert app_identity == stored_app_identity
        _reset_runtime()

    def test_transport_receives_exact_unprefixed_codex_model(self):
        from subagents import codex_runtime

        _seed_runtime_sessions("app-sol")
        successor_turn_id = _successor_turn_id()
        preadmitted = _admit_active_turn(
            "app-sol", turn_id=successor_turn_id,
        )
        app_kwargs: dict = {}

        def app_ctor(**kwargs):
            app_kwargs.update(kwargs)
            return _make_mock_app_session("thread-sol")

        with patch(
            "subagents.codex_app_server.CodexAppServerSession", side_effect=app_ctor
        ):
            assert _run(
                locked_spawn(
                    codex_runtime._spawn_app_server_turn,
                    "app-sol",
                    "hi",
                    current_user_text="hi",
                    codex_thread_id=None,
                    binary="codex",
                    successor_turn_id=successor_turn_id,
                    working_dir=None,
                    windows=WindowFacts(200_000, None),
                    broadcast=lambda *a: None,
                    model="gpt-5.6-sol",
                    expected_reported_model="gpt-5.6-sol",
                    preadmitted_turn=preadmitted,
                )
            )
        assert app_kwargs["model"] == "gpt-5.6-sol"

    def test_spawn_turn_does_not_duplicate_input_in_history(self, main_db):
        """Codex input persistence stays owned by the upstream send path."""
        from subagents import codex_runtime

        recon = SimpleNamespace(
            task_text="task",
            turn_text="hi",
            snapshot_id="s1",
            snapshot=SimpleNamespace(to_dict=lambda: {"snapshot_id": "s1"}),
            history_format_version=1,
            history_start_exclusive=None,
            history_sealed_boundary=None,
            history_full_raw_replay_available=True,
            history_items=(),
            checkpoint_text=None,
        )
        with (
            patch("subagents.codex_spawn_preflight.codex_connection", return_value={}),
            patch(
                "subagents.codex_spawn_preflight.build_codex_reconstruction",
                return_value=recon,
            ),
            patch(
                "subagents.codex_runtime._spawn_app_server_turn",
                new=AsyncMock(return_value=True),
            ),
        ):
            _run(
                locked_spawn(
                    codex_runtime._spawn_turn,
                    "main-1",
                    "hi",
                    successor_turn_id=_successor_turn_id(),
                    codex_thread_id="thr",
                    binary="codex",
                )
            )

        user_rows = main_db.execute(
            "SELECT content FROM messages WHERE thread_id = 'main-1' AND role = 'user'"
        ).fetchall()
        assert user_rows == []

    def test_spawn_turn_dispatches_current_input_per_turn_and_seed_text_on_seed(
        self, main_db
    ):
        """A resumed provider gets input only; a new provider gets its seed."""
        from subagents import codex_runtime

        recon = SimpleNamespace(
            task_text="static context\n\nUser task:\nhi",
            turn_text="hi",
            snapshot_id="s1",
            snapshot=SimpleNamespace(to_dict=lambda: {"snapshot_id": "s1"}),
            history_format_version=1,
            history_start_exclusive=None,
            history_sealed_boundary=None,
            history_full_raw_replay_available=True,
            history_items=(),
            checkpoint_text=None,
        )
        captured: dict[str, str] = {}

        async def _capture(thread_id, text, **kw):
            captured["text"] = text
            return True

        with (
            patch("subagents.codex_spawn_preflight.codex_connection", return_value={}),
            patch(
                "subagents.codex_spawn_preflight.build_codex_reconstruction",
                return_value=recon,
            ),
            patch("subagents.codex_runtime._spawn_app_server_turn", new=_capture),
        ):
            _run(
                locked_spawn(
                    codex_runtime._spawn_turn,
                    "main-1",
                    "hi",
                    successor_turn_id=_successor_turn_id(),
                    codex_thread_id="thr",
                    binary="codex",
                )
            )
            assert captured["text"] == recon.turn_text

            captured.clear()
            _run(
                locked_spawn(
                    codex_runtime._spawn_turn,
                    "main-2",
                    "hi",
                    successor_turn_id=_successor_turn_id(),
                    codex_thread_id=None,
                    binary="codex",
                )
            )
            assert captured["text"] == recon.task_text

    def test_feed_main_steers_if_already_live(self):
        from subagents import codex_runtime
        from core.turn_core import TurnIdentity

        live_session = MagicMock()
        live_session.steer = AsyncMock(return_value=True)
        codex_runtime._LIVE.put(
            "main-live",
            codex_runtime._LiveCodex(
                codex_thread_id="thread_main_live",
                session=live_session,
                parent_thread_id=None,
                name="test",
                model="gpt-5-codex",
                catalog_model_id="codex/gpt-5-codex",
                expected_reported_model="gpt-5-codex",
                provider_reported_model="gpt-5-codex",
                corsia_turn=TurnIdentity("turn-main-live", 1),
            ),
        )
        result = _run(
            codex_runtime.feed_main(
                "main-live",
                "hello",
                target=_codex_target("gpt-5-codex"),
                successor_turn_id=_successor_turn_id(),
                bind_input_turn=_bind_input_turn,
            )
        )
        assert result is not None
        live_session.steer.assert_awaited_once_with(
            "hello",
            on_write_observed=ANY,
            on_drain_confirmed=ANY,
        )

    def test_feed_main_passes_planning_root_for_project(self, tmp_path, monkeypatch):
        from core.turn_core import TurnIdentity
        from subagents import codex_runtime
        from tests.project_ref_helpers import project_ref, select_workspace

        workspace = tmp_path / "workspace"
        project_dir = workspace / "projects" / "myapp"
        project_dir.mkdir(parents=True)
        monkeypatch.setattr("infra.config.WORKSPACE", workspace)
        select_workspace(monkeypatch, workspace)

        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT)"
        )
        db.execute(
            "CREATE TABLE threads "
            "(id TEXT PRIMARY KEY, runtime_context_epoch INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute(
            "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
            ("proj-123", "My App", project_ref("myapp")),
        )
        db.execute(
            "INSERT INTO threads (id, runtime_context_epoch) VALUES (?, ?)",
            ("wd-sess", 3),
        )
        db.commit()
        spawn = AsyncMock(return_value=TurnIdentity("working-dir-turn", 1))

        try:
            with (
                patch(
                    "subagents.codex_runtime_support.get_db",
                    return_value=_NoCloseConnection(db),
                ),
                patch(
                    "subagents.codex_runtime._restore_thread_id",
                    new=AsyncMock(return_value=None),
                ),
                patch(
                    "subagents.codex_spawn_preflight.thread_runtime_context_epoch",
                    return_value=3,
                ),
                patch(
                    "connections.models.get_connection",
                    return_value={"status": "available", "config": {"binary": "codex"}},
                ),
                patch(
                    "subagents.codex_spawn_preflight.build_codex_reconstruction",
                    return_value=_fake_codex_reconstruction("task"),
                ),
                patch(
                    "subagents.codex_runtime._spawn_app_server_turn",
                    new=spawn,
                ),
            ):
                _run(
                    codex_runtime.feed_main(
                        "wd-sess",
                        "task",
                        target=_codex_target("gpt-5-codex"),
                        successor_turn_id=_successor_turn_id(),
                        bind_input_turn=_bind_input_turn,
                        project_id="proj-123",
                    )
                )
        finally:
            db.close()

        assert spawn.await_args.kwargs["working_dir"] == str(project_dir)
        assert spawn.await_args.kwargs["runtime_context_epoch"] == 3

    def test_build_reconstruction_reuses_owned_db_for_context_collection(self):
        from history.types import PortableHistoryProjection
        from subagents import codex_runtime

        class FakeDb:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        fake_db = FakeDb()
        projection = PortableHistoryProjection(
            format_version=1,
            thread_id="main-owned-db",
            history_epoch=0,
            start_exclusive=None,
            sealed_boundary=None,
            turns=(),
            full_raw_replay_available=True,
        )
        sentinel = object()
        seen = {}

        def fake_build_reconstruction(**kwargs):
            seen.update(kwargs)
            return sentinel

        with patch(
            "subagents.codex_destination_render.get_db",
            return_value=fake_db,
        ):
            with patch(
                "history.projection.render_portable_history",
                return_value=projection,
            ) as render:
                with (
                    patch(
                        "subagents.codex_context_reconstruction.build_codex_context_reconstruction",
                        side_effect=fake_build_reconstruction,
                    ),
                ):
                    result = codex_destination_render.build_codex_reconstruction(
                        thread_id="main-owned-db",
                        text="go",
                        current_input_message_id=None,
                        working_dir=None,
                        project_id=None,
                        model="codex/gpt-5-codex",
                        context_window=200000,
                        codex_thread_id=None,
                        db=None,
                    )

        assert result is sentinel
        render.assert_called_once_with(
            "main-owned-db",
            db=fake_db,
            exclude_message_id=None,
        )
        assert seen["thread_id"] == "main-owned-db"
        assert seen["db"] is fake_db
        assert fake_db.closed is True

    def test_feed_main_project_scope_missing_root_blocks_before_provider(self, main_db):
        from subagents import codex_runtime

        conn = {
            "status": "available",
            "config": {"binary": "codex", "app_server": True},
        }
        with patch("connections.models.get_connection", return_value=conn):
            with patch("subagents.codex_app_server.CodexAppServerSession") as app_ctor:
                with pytest.raises(
                    ValueError, match="Project working directory unresolved"
                ):
                    _run(
                        codex_runtime.feed_main(
                            "main-1",
                            "project task",
                            target=_codex_target("gpt-5-codex"),
                            successor_turn_id=_successor_turn_id(),
                            bind_input_turn=_bind_input_turn,
                            project_id="missing-project",
                        )
                    )

        app_ctor.assert_not_called()

    def test_thread_project_id_db_errors_fail_loud(self):
        from subagents import codex_runtime

        broken_db = MagicMock()
        broken_db.execute.side_effect = sqlite3.OperationalError(
            "no such column: project_id"
        )

        with pytest.raises(sqlite3.OperationalError, match="project_id"):
            codex_runtime.get_thread_project_id("main-1", db=broken_db)

    def test_has_thread_id_restores_persisted_provider_handle(self):
        from subagents import codex_runtime

        with patch(
            "subagents.codex_runtime._restore_thread_id",
            new=AsyncMock(return_value="thread-durable"),
        ) as restore:
            result = _run(codex_runtime.has_thread_id("main-1"))

        assert result is True
        restore.assert_awaited_once_with("main-1", None)

    def test_has_thread_id_preserves_same_process_app_server_handle(self):
        from subagents import codex_runtime

        codex_runtime._APP_SESSIONS["main-1"] = _make_mock_app_session("thread_live")

        with patch("subagents.codex_runtime._codex_connection") as connection:
            result = _run(codex_runtime.has_thread_id("main-1"))

        assert result is True
        connection.assert_not_called()

    def test_feed_main_uses_app_server(self):
        from subagents import codex_runtime

        _seed_runtime_sessions("main-app")
        successor_turn_id = _successor_turn_id()
        preadmitted = _admit_active_turn(
            "main-app", turn_id=successor_turn_id,
        )
        app_session = _make_mock_app_session("thread_app")
        captured_text = (
            "--- BEGIN CORSIA EFFECTIVE CONTEXT DIGEST ---\n"
            "{}\n"
            "--- END CORSIA EFFECTIVE CONTEXT DIGEST ---\n\n"
            "User task:\nfirst"
        )
        app_session.request_log = [
            {"id": 1, "method": "initialize", "params": {}},
            {"id": 2, "method": "thread/start", "params": {"cwd": "/workspace"}},
            {
                "id": 3,
                "method": "turn/start",
                "params": {
                    "threadId": "thread_app",
                    "input": [{"type": "text", "text": captured_text}],
                },
            },
        ]
        conn = {
            "status": "available",
            "config": {"binary": "codex", "app_server": True},
        }
        captured = {}

        def app_ctor(**kwargs):
            captured.update(kwargs)
            return app_session

        with patch("connections.models.get_connection", return_value=conn):
            with patch(
                "subagents.codex_app_server.CodexAppServerSession", side_effect=app_ctor
            ) as app_ctor_mock:
                result = _run(
                    codex_runtime.feed_main(
                        "main-app",
                        "first",
                        target=_codex_target("gpt-5-codex"),
                        successor_turn_id=successor_turn_id,
                        bind_input_turn=_bind_input_turn,
                        preadmitted_turn=preadmitted,
                    )
                )

        assert result is not None
        app_ctor_mock.assert_called_once()
        app_session.start.assert_awaited_once()
        assert codex_runtime._APP_SESSIONS["main-app"] is app_session
        assert codex_runtime._CODEX_THREADS["main-app"] == "thread_app"
        assert "--- BEGIN CORSIA EFFECTIVE CONTEXT DIGEST ---" not in captured["task"]
        assert '"frozen_prompt_material"' not in captured["task"]
        assert "--- CORSIA CONTEXT SNAPSHOT: " in captured["task"]
        live = codex_runtime._LIVE.get("main-app")
        assert live.source_artifacts
        methods = [artifact["method"] for artifact in live.source_artifacts]
        assert methods == ["thread/start", "turn/start"]
        turn_artifact = next(
            a for a in live.source_artifacts if a["method"] == "turn/start"
        )
        assert turn_artifact["snapshot_id"] == live.context_snapshot_id
        assert turn_artifact["params"]["input"][0]["text"] == captured_text

    def test_feed_main_app_server_injects_corsia_history_before_current_turn(
        self, main_db
    ):
        from subagents import codex_runtime

        next_seq = _seed_portable_exchange(
            main_db,
            turn_id="turn-prior",
            seq=1,
            user_id="cu1",
            user_text="prior request",
            assistant_id="ca1",
            assistant_text="prior answer",
        )
        main_db.commit()
        preadmitted = _seed_current_user(
            main_db,
            message_id="cu2",
            seq=next_seq,
            content="current request",
        )
        main_db.commit()

        app_session = _make_mock_app_session("thread_app")
        app_session.request_log = [
            {"id": 1, "method": "initialize", "params": {}},
            {"id": 2, "method": "thread/start", "params": {"cwd": "/workspace"}},
            {
                "id": 3,
                "method": "thread/inject_items",
                "params": {
                    "threadId": "thread_app",
                    "items": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "prior request"}
                            ],
                        },
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "prior answer"}
                            ],
                        },
                    ],
                },
            },
            {
                "id": 4,
                "method": "turn/start",
                "params": {
                    "threadId": "thread_app",
                    "input": [{"type": "text", "text": "wrapped current request"}],
                },
            },
        ]
        conn = {
            "status": "available",
            "config": {"binary": "codex", "app_server": True},
        }
        captured = {}

        def app_ctor(**kwargs):
            captured.update(kwargs)
            return app_session

        with (
            patch("subagents.codex_runtime.get_db", return_value=main_db),
            patch("connections.models.get_connection", return_value=conn),
        ):
            with patch(
                "subagents.codex_app_server.CodexAppServerSession", side_effect=app_ctor
            ):
                with patch(
                    "subagents.codex_runtime._restore_thread_id",
                    new=AsyncMock(return_value=None),
                ):
                    result = _run(
                        codex_runtime.feed_main(
                            "main-1",
                            "current request",
                            target=_codex_target("gpt-5-codex"),
                            successor_turn_id=preadmitted.turn_id,
                            current_input_message_id="cu2",
                            bind_input_turn=_bind_input_turn,
                            project_id=None,
                            preadmitted_turn=preadmitted,
                        )
                    )

        assert result is not None
        assert captured["history_items"] == (
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "prior request"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "prior answer"}],
            },
        )
        assert "current request" not in str(captured["history_items"])

        live = codex_runtime._LIVE.get("main-1")
        methods = [artifact["method"] for artifact in live.source_artifacts]
        assert methods == ["thread/start", "thread/inject_items", "turn/start"]
        inject_artifact = next(
            a for a in live.source_artifacts if a["method"] == "thread/inject_items"
        )
        assert inject_artifact["snapshot_id"] == live.context_snapshot_id
        assert inject_artifact["params"]["items"] == list(captured["history_items"])
        portable_source = live.context_snapshot["frozen_prompt_material"][
            "provider_payload"
        ]["portable_history"]
        assert portable_source["format_version"] == 1
        assert portable_source["sealed_boundary"] == {
            "thread_id": "main-1",
            "history_epoch": 0,
            "seq": 2,
            "message_id": "ca1",
        }
    def test_feed_main_app_server_injects_complete_canonical_history(self, main_db):
        from subagents import codex_runtime

        next_seq = 1
        for i in range(8):
            next_seq = _seed_portable_exchange(
                main_db,
                turn_id=f"old-turn-{i}",
                seq=next_seq,
                user_id=f"old-u-{i}",
                user_text=f"old user {i}",
                assistant_id=f"old-a-{i}",
                assistant_text=f"old answer {i}",
            )
        next_seq = _seed_portable_exchange(
            main_db,
            turn_id="recent-turn",
            seq=next_seq,
            user_id="recent-u",
            user_text="recent anchor",
            assistant_id="recent-a",
            assistant_text="recent answer",
        )
        main_db.commit()
        preadmitted = _seed_current_user(
            main_db,
            message_id="current-u",
            seq=next_seq,
            content="current request",
        )
        main_db.commit()

        app_session = _make_mock_app_session("thread_app")
        conn = {
            "status": "available",
            "config": {"binary": "codex", "app_server": True},
        }
        captured = {}

        def app_ctor(**kwargs):
            captured.update(kwargs)
            return app_session

        with patch("subagents.codex_runtime.get_db", return_value=main_db):
            with patch("connections.models.get_connection", return_value=conn):
                with patch(
                    "subagents.codex_app_server.CodexAppServerSession",
                    side_effect=app_ctor,
                ):
                    with patch(
                        "subagents.codex_runtime._restore_thread_id",
                        new=AsyncMock(return_value=None),
                    ):
                        result = _run(
                            codex_runtime.feed_main(
                                "main-1",
                                "current request",
                                target=_codex_target("gpt-5-codex"),
                                successor_turn_id=preadmitted.turn_id,
                                current_input_message_id="current-u",
                                bind_input_turn=_bind_input_turn,
                                project_id=None,
                                preadmitted_turn=preadmitted,
                            )
                        )

        assert result is not None
        assert len(captured["history_items"]) == 18
        assert "old user 0" in str(captured["history_items"])
        assert "old user 7" in str(captured["history_items"])
        assert "recent anchor" in str(captured["history_items"])
        assert "current request" not in str(captured["history_items"])

    def test_feed_main_app_server_persisted_handle_resumes_without_replay(
        self,
        main_db,
    ):
        from subagents import codex_runtime

        codex_runtime._CODEX_THREADS["main-1"] = "thread_existing"
        _seed_portable_exchange(
            main_db,
            turn_id="turn-rebuild",
            seq=1,
            user_id="ru1",
            user_text="prior request",
            assistant_id="ra1",
            assistant_text="prior answer",
        )
        main_db.commit()
        successor_turn_id = _successor_turn_id()
        preadmitted = _admit_active_turn(
            "main-1",
            turn_id=successor_turn_id,
            content="current request",
            settle_write=False,
        )

        app_session = _make_mock_app_session("thread_existing")
        app_session.request_log = [
            {
                "id": 1,
                "method": "thread/resume",
                "params": {"threadId": "thread_existing"},
            },
            {
                "id": 2,
                "method": "turn/start",
                "params": {
                    "threadId": "thread_existing",
                    "input": [{"type": "text", "text": "wrapped current request"}],
                },
            },
        ]
        conn = {
            "status": "available",
            "config": {"binary": "codex", "app_server": True},
        }
        captured = {}

        def app_ctor(**kwargs):
            captured.update(kwargs)
            return app_session

        with (
            patch("subagents.codex_runtime.get_db", return_value=main_db),
            patch("connections.models.get_connection", return_value=conn),
        ):
            with patch(
                "subagents.codex_app_server.CodexAppServerSession", side_effect=app_ctor
            ):
                result = _run(
                    codex_runtime.feed_main(
                        "main-1",
                        "current request",
                        target=_codex_target("gpt-5-codex"),
                        successor_turn_id=successor_turn_id,
                        bind_input_turn=_bind_input_turn,
                        project_id=None,
                        preadmitted_turn=preadmitted,
                    )
                )

        assert result is not None
        assert captured["codex_thread_id"] == "thread_existing"
        assert captured["history_items"] == ()
        live = codex_runtime._LIVE.get("main-1")
        methods = [artifact["method"] for artifact in live.source_artifacts]
        assert methods == ["thread/resume", "turn/start"]

    def test_feed_main_app_server_persisted_handle_sends_current_turn_only(
        self,
        main_db,
    ):
        from subagents import codex_runtime

        full_task_text = (
            "--- BEGIN CORSIA EFFECTIVE CONTEXT DIGEST ---\n"
            "{}\n"
            "--- END CORSIA EFFECTIVE CONTEXT DIGEST ---\n\n"
            "--- BEGIN CORSIA CONTEXT SOURCES ---\n"
            "prefix required on a fresh tool-bearing start\n"
            "--- END CORSIA CONTEXT SOURCES ---\n\n"
            "User task:\ncurrent request"
        )
        reconstruction = _fake_codex_reconstruction(full_task_text)
        reconstruction.turn_text = "current request"
        successor_turn_id = _successor_turn_id()
        preadmitted = _admit_active_turn(
            "main-1",
            turn_id=successor_turn_id,
            content="current request",
            settle_write=False,
        )

        app_session = _make_mock_app_session("thread_existing")
        conn = {
            "status": "available",
            "config": {"binary": "codex", "app_server": True},
        }
        captured = {}

        def app_ctor(**kwargs):
            captured.update(kwargs)
            return app_session

        with patch("connections.models.get_connection", return_value=conn):
            with patch(
                "subagents.codex_app_server.CodexAppServerSession", side_effect=app_ctor
            ):
                with patch(
                    "subagents.codex_runtime._restore_thread_id",
                    new=AsyncMock(return_value="thread_existing"),
                ):
                    with patch(
                        "subagents.codex_spawn_preflight.build_codex_reconstruction",
                        return_value=reconstruction,
                    ):
                        result = _run(
                            codex_runtime.feed_main(
                                "main-1",
                                "current request",
                                target=_codex_target("gpt-5-codex"),
                                successor_turn_id=successor_turn_id,
                                bind_input_turn=_bind_input_turn,
                                project_id=None,
                                preadmitted_turn=preadmitted,
                            )
                        )

        assert result is not None
        assert captured["codex_thread_id"] == "thread_existing"
        assert captured["task"] == "current request"
        assert captured["history_items"] == ()

    def test_feed_main_after_retire_fresh_starts_and_injects_corsia_history(
        self,
        main_db,
    ):
        from registry import models
        from subagents import codex_runtime

        _seed_portable_exchange(
            main_db,
            turn_id="turn-before-retire",
            seq=1,
            user_id="bu1",
            user_text="original request",
            assistant_id="ba1",
            assistant_text="old answer",
        )
        models.register_artifact(
            integration="codex",
            type="thread",
            external_id="thread_stale_boundary",
            parent_thread_id="main-1",
            db=main_db,
        )
        main_db.commit()
        codex_runtime._CODEX_THREADS["main-1"] = "thread_stale_boundary"

        assert _run(codex_runtime.retire("main-1", db=main_db)) is True
        successor_turn_id = _successor_turn_id()
        preadmitted = _admit_active_turn(
            "main-1",
            turn_id=successor_turn_id,
            content="current request",
            settle_write=False,
        )

        app_session = _make_mock_app_session("thread_after_boundary")
        app_session.request_log = [
            {"id": 1, "method": "initialize", "params": {}},
            {"id": 2, "method": "thread/start", "params": {"cwd": "/workspace"}},
            {
                "id": 3,
                "method": "thread/inject_items",
                "params": {
                    "threadId": "thread_after_boundary",
                    "items": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "original request"}
                            ],
                        },
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "old answer"}],
                        },
                    ],
                },
            },
            {
                "id": 4,
                "method": "turn/start",
                "params": {
                    "threadId": "thread_after_boundary",
                    "input": [{"type": "text", "text": "wrapped current request"}],
                },
            },
        ]
        conn = {
            "status": "available",
            "config": {"binary": "codex", "app_server": True},
        }
        captured = {}

        def app_ctor(**kwargs):
            captured.update(kwargs)
            return app_session

        with (
            patch("subagents.codex_runtime.get_db", return_value=main_db),
            patch("connections.models.get_connection", return_value=conn),
        ):
            with patch(
                "subagents.codex_app_server.CodexAppServerSession", side_effect=app_ctor
            ):
                with patch(
                    "subagents.codex_runtime._restore_thread_id",
                    new=AsyncMock(return_value=None),
                ):
                    result = _run(
                        codex_runtime.feed_main(
                            "main-1",
                            "current request",
                            target=_codex_target("gpt-5-codex"),
                            successor_turn_id=successor_turn_id,
                            bind_input_turn=_bind_input_turn,
                            project_id=None,
                            preadmitted_turn=preadmitted,
                        )
                    )

        assert result is not None
        assert captured["codex_thread_id"] is None
        assert captured["history_items"] == (
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "original request"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "old answer"}],
            },
        )
        assert models.find_for_thread("main-1", "codex", "thread", db=main_db) is None
        live = codex_runtime._LIVE.get("main-1")
        methods = [artifact["method"] for artifact in live.source_artifacts]
        assert methods == ["thread/start", "thread/inject_items", "turn/start"]
        assert codex_runtime._CODEX_THREADS["main-1"] == "thread_after_boundary"

    def test_feed_main_app_server_failure_fails_loud(self):
        from subagents import codex_runtime

        _seed_runtime_sessions("main-fallback")
        successor_turn_id = _successor_turn_id()
        preadmitted = _admit_active_turn(
            "main-fallback",
            turn_id=successor_turn_id,
            content="first",
            settle_write=False,
        )
        app_session = _make_mock_app_session("thread_app")
        app_session.start = AsyncMock(side_effect=RuntimeError("app server boom"))
        conn = {
            "status": "available",
            "config": {"binary": "codex", "app_server": True},
        }

        with patch("connections.models.get_connection", return_value=conn):
            with patch(
                "subagents.codex_app_server.CodexAppServerSession",
                return_value=app_session,
            ):
                with pytest.raises(RuntimeError, match="app server boom"):
                    _run(
                        codex_runtime.feed_main(
                            "main-fallback",
                            "first",
                            target=_codex_target("gpt-5-codex"),
                            successor_turn_id=successor_turn_id,
                            bind_input_turn=_bind_input_turn,
                            preadmitted_turn=preadmitted,
                        )
                    )

        app_session.terminate.assert_awaited_once()
        assert "main-fallback" not in codex_runtime._LIVE
        assert "main-fallback" not in codex_runtime._APP_SESSIONS

    def test_missing_resumed_rollout_retires_durable_handle(self, main_db):
        from registry import models
        from subagents import codex_runtime
        from subagents.codex_app_server_errors import CodexAppServerError

        models.register_artifact(
            integration="codex",
            type="thread",
            external_id="thread-missing-rollout",
            parent_thread_id="main-1",
            db=main_db,
        )
        main_db.commit()
        codex_runtime._CODEX_THREADS["main-1"] = "thread-missing-rollout"
        successor_turn_id = _successor_turn_id()
        preadmitted = _admit_active_turn(
            "main-1",
            turn_id=successor_turn_id,
            content="current request",
            settle_write=False,
        )
        app_session = _make_mock_app_session("thread-missing-rollout")
        app_session.start = AsyncMock(
            side_effect=CodexAppServerError(
                "no rollout found for thread id thread-missing-rollout",
                {"message": "no rollout found for thread id thread-missing-rollout"},
            )
        )
        conn = {
            "status": "available",
            "config": {"binary": "codex", "app_server": True},
        }

        with (
            patch("subagents.codex_runtime.get_db", return_value=main_db),
            patch(
                "connections.models.get_connection",
                return_value=conn,
            ),
            patch(
                "subagents.codex_app_server.CodexAppServerSession",
                return_value=app_session,
            ),
        ):
            with pytest.raises(CodexAppServerError, match="no rollout found"):
                _run(
                    codex_runtime.feed_main(
                        "main-1",
                        "current request",
                        target=_codex_target("gpt-5-codex"),
                        successor_turn_id=successor_turn_id,
                        bind_input_turn=_bind_input_turn,
                        preadmitted_turn=preadmitted,
                    )
                )

        assert "main-1" not in codex_runtime._CODEX_THREADS
        assert (
            models.find_for_thread(
                "main-1",
                "codex",
                "thread",
                db=main_db,
            )
            is None
        )
        assert _run(codex_runtime.has_thread_id("main-1", main_db)) is False

    def test_feed_main_rejects_when_connection_not_available(self):
        from subagents import codex_runtime

        conn = {"status": "needs_auth", "config": {"binary": "codex"}}
        with patch("connections.models.get_connection", return_value=conn):
            with patch("subagents.codex_app_server.CodexAppServerSession") as app_ctor:
                with pytest.raises(codex_runtime.CodexConnectionUnavailable):
                    _run(
                        codex_runtime.feed_main(
                            "main-no-conn",
                            "first",
                            target=_codex_target("gpt-5-codex"),
                            successor_turn_id=_successor_turn_id(),
                            bind_input_turn=_bind_input_turn,
                        )
                    )

        app_ctor.assert_not_called()
        assert "main-no-conn" not in codex_runtime._LIVE
        assert "main-no-conn" not in codex_runtime._APP_SESSIONS

    @pytest.mark.asyncio
    async def test_app_server_expected_turn_mismatch_does_not_replace_owner(self):
        from subagents.codex_app_server import (
            CodexAppServerError,
            CodexAppServerSession,
        )

        payload = {
            "code": -32600,
            "message": ("expected active turn id `old-turn` but found `new-turn`"),
        }
        session = CodexAppServerSession(
            binary="codex",
            task=None,
            codex_thread_id="thread-1",
        )
        session._active_turn_id = "old-turn"

        async def _reject(_method, _params, **_kwargs):
            raise CodexAppServerError(str(payload), payload)

        session._request = _reject

        assert await session.steer("recover me") is False
        assert session._active_turn_id == "old-turn"

    @pytest.mark.asyncio
    async def test_feed_main_stale_turn_mismatch_defers_without_interrupt(self):
        """A stale/not-steerable steer rejection is not a preempt license.

        Codex's ``expected active turn id`` error proves this request never
        entered the running turn.  The runtime leaves that turn untouched and
        returns ``None`` so the durable inbox head waits for its terminal
        successor instead of interrupting live work.
        """
        from subagents import codex_runtime
        from core.turn_core import TurnIdentity
        from subagents.codex_app_server import CodexAppServerError

        _seed_runtime_sessions("main-stale-turn")
        payload = {
            "code": -32600,
            "message": ("expected active turn id `old-turn` but found `new-turn`"),
        }
        live_session = MagicMock()
        live_session.steer = AsyncMock(
            side_effect=CodexAppServerError(str(payload), payload)
        )
        live_session.interrupt = AsyncMock(return_value=True)
        live_session.start_turn = AsyncMock(return_value=True)
        codex_runtime._LIVE.put(
            "main-stale-turn",
            codex_runtime._LiveCodex(
                codex_thread_id="thread_stale",
                session=live_session,
                parent_thread_id=None,
                name="test",
                model="gpt-5-codex",
                catalog_model_id="codex/gpt-5-codex",
                expected_reported_model="gpt-5-codex",
                provider_reported_model="gpt-5-codex",
                corsia_turn=TurnIdentity("turn-stale", 1),
            ),
        )

        reconstruction = _fake_codex_reconstruction(
            "new input", turn_text="new input\n\nrow-7-deterministic-frame"
        )

        with (
            patch(
                "subagents.codex_spawn_preflight.build_codex_reconstruction",
                return_value=reconstruction,
            ),
            pytest.raises(codex_runtime.CodexLiveTurnBusy),
        ):
            await codex_runtime.feed_main(
                "main-stale-turn",
                "new input",
                target=_codex_target("gpt-5-codex"),
                successor_turn_id=_successor_turn_id(),
                bind_input_turn=_bind_input_turn,
            )

        live_session.steer.assert_awaited_once_with(
            "new input",
            on_write_observed=ANY,
            on_drain_confirmed=ANY,
        )
        live_session.interrupt.assert_not_awaited()
        live_session.start_turn.assert_not_awaited()
        live = codex_runtime._LIVE.get("main-stale-turn")
        assert live is not None
        assert live.corsia_turn == TurnIdentity("turn-stale", 1)

    @pytest.mark.asyncio
    async def test_declined_steer_never_restarts(self, main_db):
        """A declined live steer leaves the existing provider turn intact."""
        from subagents import codex_runtime
        from core.turn_core import TurnIdentity

        reconstruction = _fake_codex_reconstruction(
            "new input",
            turn_text="new input",
        )
        live_session = MagicMock()
        live_session.steer = AsyncMock(return_value=False)
        live_session.interrupt = AsyncMock(return_value=True)
        live_session.start_turn = AsyncMock(return_value=True)
        codex_runtime._LIVE.put(
            "main-1",
            codex_runtime._LiveCodex(
                codex_thread_id="thread-row-7",
                session=live_session,
                parent_thread_id=None,
                name="test",
                model="gpt-5-codex",
                catalog_model_id="codex/gpt-5-codex",
                expected_reported_model="gpt-5-codex",
                provider_reported_model="gpt-5-codex",
                corsia_turn=TurnIdentity("turn-interrupted", 1),
            ),
        )

        with (
            patch(
                "subagents.codex_spawn_preflight.build_codex_reconstruction",
                return_value=reconstruction,
            ),
            pytest.raises(codex_runtime.CodexLiveTurnBusy),
        ):
            await codex_runtime.feed_main(
                "main-1",
                "new input",
                target=_codex_target("gpt-5-codex"),
                successor_turn_id=_successor_turn_id(),
                bind_input_turn=_bind_input_turn,
            )

        live_session.interrupt.assert_not_awaited()
        live_session.start_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_model_switch_never_steers_or_interrupts_the_old_codex_turn(self):
        """A live model switch defers until the current turn relinquishes.

        The old turn is neither steered (its provider cannot accept another
        model's frame) nor interrupted (an ordinary send is never destructive).
        The runtime returns ``None`` and keeps the live handle unchanged; the
        queued input's terminal successor will start on the new model.
        """
        from subagents import codex_runtime
        from core.turn_core import TurnIdentity

        _seed_runtime_sessions("main-model-switch")
        live_session = MagicMock()
        live_session.steer = AsyncMock(return_value=True)
        live_session.interrupt = AsyncMock(return_value=True)
        live_session.start_turn = AsyncMock(return_value=True)
        live_session.model = "gpt-5.6-sol"
        live = codex_runtime._LiveCodex(
            codex_thread_id="thread-switch",
            session=live_session,
            parent_thread_id=None,
            name="test",
            model="gpt-5.6-sol",
            catalog_model_id="codex/gpt-5.6-sol",
            context_window=200000,
            expected_reported_model="gpt-5.6-sol",
            provider_reported_model="gpt-5.6-sol",
            corsia_turn=TurnIdentity("turn-model-switch", 1),
        )
        codex_runtime._LIVE.put("main-model-switch", live)

        successor_turn_id = _successor_turn_id()
        bind_input_turn = AsyncMock()
        with pytest.raises(codex_runtime.CodexLiveTurnBusy):
            await codex_runtime.feed_main(
                "main-model-switch",
                "use terra",
                target=_codex_target("gpt-5.6-terra"),
                successor_turn_id=successor_turn_id,
                bind_input_turn=bind_input_turn,
            )

        live_session.steer.assert_not_awaited()
        live_session.interrupt.assert_not_awaited()
        live_session.start_turn.assert_not_awaited()
        bind_input_turn.assert_not_awaited()
        live = codex_runtime._LIVE.get("main-model-switch")
        assert live is not None
        assert live.model == "gpt-5.6-sol"
        assert live.catalog_model_id == "codex/gpt-5.6-sol"
        assert live.expected_reported_model == "gpt-5.6-sol"
        assert live.provider_reported_model == "gpt-5.6-sol"
        assert live.corsia_turn == TurnIdentity("turn-model-switch", 1)

    @pytest.mark.asyncio
    async def test_provider_reroute_never_steers_or_interrupts_live_turn(self):
        """A provider-side reroute mismatch defers exactly like a model switch.

        The live turn is running on a different reported model than the
        selection expects.  Steering would feed the wrong provider context and
        interrupting would destroy live work, so the runtime declines both and
        leaves the durable inbox head for the terminal successor.
        """
        from subagents import codex_runtime
        from core.turn_core import TurnIdentity

        _seed_runtime_sessions("main-reroute")
        live_session = MagicMock()
        live_session.steer = AsyncMock(return_value=True)
        live_session.interrupt = AsyncMock(return_value=True)
        live_session.start_turn = AsyncMock(return_value=True)
        live_session.model = "gpt-5.6-sol"
        live = codex_runtime._LiveCodex(
            codex_thread_id="thread-reroute",
            session=live_session,
            parent_thread_id=None,
            name="test",
            model="gpt-5.6-sol",
            catalog_model_id="codex/gpt-5.6-sol",
            context_window=200000,
            expected_reported_model="gpt-5.6-sol",
            provider_reported_model="gpt-5.6-terra",
            corsia_turn=TurnIdentity("turn-reroute", 1),
        )
        codex_runtime._LIVE.put("main-reroute", live)

        with pytest.raises(codex_runtime.CodexLiveTurnBusy):
            await codex_runtime.feed_main(
                "main-reroute",
                "try sol again",
                target=_codex_target("gpt-5.6-sol"),
                successor_turn_id=_successor_turn_id(),
                bind_input_turn=_bind_input_turn,
            )

        live_session.steer.assert_not_awaited()
        live_session.interrupt.assert_not_awaited()
        live_session.start_turn.assert_not_awaited()
        assert live.provider_reported_model == "gpt-5.6-terra"
        assert codex_runtime._LIVE.get("main-reroute") is live

    @pytest.mark.asyncio
    async def test_concurrent_feed_main_calls_serialize_live_steer(self):
        from subagents import codex_runtime
        from core.turn_core import TurnIdentity

        started = asyncio.Event()
        release = asyncio.Event()
        active = 0
        max_active = 0

        async def _steer(
            _text,
            *,
            on_write_observed,
            on_drain_confirmed,
        ):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            on_write_observed()
            started.set()
            await release.wait()
            on_drain_confirmed()
            active -= 1
            return True

        live_session = MagicMock()
        live_session.steer = AsyncMock(side_effect=_steer)
        codex_runtime._LIVE.put(
            "main-concurrent",
            codex_runtime._LiveCodex(
                codex_thread_id="thread_concurrent",
                session=live_session,
                parent_thread_id=None,
                name="test",
                model="gpt-5-codex",
                catalog_model_id="codex/gpt-5-codex",
                expected_reported_model="gpt-5-codex",
                provider_reported_model="gpt-5-codex",
                corsia_turn=TurnIdentity("turn-concurrent", 1),
            ),
        )

        try:
            first = asyncio.create_task(
                codex_runtime.feed_main(
                    "main-concurrent",
                    "first",
                    target=_codex_target("gpt-5-codex"),
                    successor_turn_id=_successor_turn_id(),
                    bind_input_turn=_bind_input_turn,
                )
            )
            await started.wait()
            second = asyncio.create_task(
                codex_runtime.feed_main(
                    "main-concurrent",
                    "second",
                    target=_codex_target("gpt-5-codex"),
                    successor_turn_id=_successor_turn_id(),
                    bind_input_turn=_bind_input_turn,
                )
            )
            await asyncio.sleep(0)
            release.set()

            assert await asyncio.gather(first, second) == [
                TurnIdentity("turn-concurrent", 1),
                TurnIdentity("turn-concurrent", 1),
            ]
            assert max_active == 1
            assert live_session.steer.await_count == 2
        finally:
            codex_runtime._LIVE.pop("main-concurrent", None)
