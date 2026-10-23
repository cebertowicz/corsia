from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from subagents.codex_app_server import CodexAppServerSession
from subagents.codex_app_server_events import (
    item_completed_event,
    item_started_event,
)
from subagents.codex_runtime_spawn import (
    _compaction_overrides,
    compaction_event_handler,
    configure_app_server_input,
)


def _real_session(cls, provider_thread_id: str, provider_turn_id: str):
    """A real session, constructed without spawning the provider process."""
    session = cls(binary="unused", task=None, codex_thread_id=provider_thread_id)
    session._track_active_turn(provider_turn_id)
    return session


@pytest.mark.parametrize("cap", [131_072, 262_144, 393_216])
def test_codex_compaction_threshold_does_not_inherit_profile_cap(isolated_db, cap):
    from infra.database import get_db
    from llm.resolution import resolve_execution_target

    model = "codex/gpt-5-codex"
    with get_db() as db:
        db.execute("UPDATE models SET context_window=950000 WHERE id=?", (model,))
        db.execute("INSERT OR REPLACE INTO model_profiles(model_id,context_window) VALUES (?,?)", (model, cap))
        db.commit()
    target = resolve_execution_target(model)
    assert target.context_window == cap
    assert _compaction_overrides(
        target.context_window,
        discovered_context_window=target.discovered_context_window,
    ) == (
        "model_auto_compact_token_limit_scope=body_after_prefix",
        "model_auto_compact_token_limit=950000",
    )


@pytest.mark.asyncio
async def test_codex_spawn_uses_only_selected_raw_window_controls(monkeypatch):
    captured: dict = {}

    class FakeSession:
        thread_id = None

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def set_callbacks(self, **_kwargs):
            pass

        async def start(self, **_kwargs):
            assert self.declared_tool_step_ids is live.tool_step_ids
            captured["started"] = True
            return True

    monkeypatch.setattr(
        "subagents.codex_app_server.CodexAppServerSession",
        FakeSession,
    )
    live = SimpleNamespace(
        session=None, corsia_turn=None, context_window=272_000,
        discovered_context_window=None, tool_step_ids={},
        checkpoint_generation_id=None,
    )

    def bind_app_server_session(session) -> None:
        live.session = session
        session.declared_tool_step_ids = live.tool_step_ids

    live.bind_app_server_session = bind_app_server_session
    _session, start_input = await configure_app_server_input(
        None,
        live=live,
        text="hello",
        binary="codex",
        codex_thread_id=None,
        working_dir="/tmp/project",
        exact_model="gpt-5.6-luna",
        effort="low",
        history_items=(),
        dynamic_tools=(),
        tool_registry=object(),
        thread_id="thread-1",
        project_id="project-1",
        on_event=None,
        on_exit=None,
        media_projection=None,
    )

    await start_input(lambda: None, lambda: None)

    assert captured["started"] is True
    assert captured["config_overrides"] == (
        "model_auto_compact_token_limit_scope=body_after_prefix",
        "model_auto_compact_token_limit=272000",
    )
    rendered = " ".join(captured["config_overrides"])
    assert "max_output" not in rendered
    assert "cache" not in rendered


@pytest.mark.asyncio
async def test_codex_live_handle_and_compaction_read_one_resolved_target(monkeypatch):
    # Both windows come from the resolved target, as for Claude Code: the handle
    # counts against the capped budget and compaction fires at provider capacity
    # without a second catalog read that a refresh could change (#1868).
    from llm.context_window import WindowFacts
    from subagents.codex_runtime_spawn import new_app_server_live

    captured: dict = {}

    class FakeSession:
        thread_id = None

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def set_callbacks(self, **_kwargs):
            pass

    monkeypatch.setattr("subagents.codex_app_server.CodexAppServerSession", FakeSession)
    monkeypatch.setattr(
        "llm.context_window._catalog_row",
        lambda *_a, **_k: pytest.fail("the spawn re-read the catalog"),
    )
    live = new_app_server_live(
        None, codex_thread_id=None, model="gpt-5.6-luna", catalog_model_id="codex/luna",
        runtime_context_epoch=0, windows=WindowFacts(262_144, 400_000),
        expected_reported_model=None, context_snapshot=None, harness="native",
        continuity_accounting=None,
    )
    assert (live.context_window, live.discovered_context_window) == (262_144, 400_000)
    assert live.harness == "native"

    await configure_app_server_input(
        None, live=live, text="hello", binary="codex", codex_thread_id=None,
        working_dir=None, exact_model="gpt-5.6-luna", effort=None, history_items=(),
        dynamic_tools=(), tool_registry=object(), thread_id="thread-1", project_id=None,
        on_event=None, on_exit=None, media_projection=None,
    )
    assert captured["config_overrides"][1] == "model_auto_compact_token_limit=400000"


@pytest.mark.asyncio
async def test_codex_checkpoint_fork_binds_steps_before_resumed_turn_starts() -> None:
    from subagents.codex_runtime_state import _LiveCodex

    class ForkSession:
        def __init__(self) -> None:
            self.config_overrides = (
                "model_auto_compact_token_limit_scope=body_after_prefix",
                "model_auto_compact_token_limit=272000",
            )
            self.declared_tool_step_ids: dict[str, str] = {}
            self.started = False

        def set_callbacks(self, **_kwargs) -> None:
            pass

        async def start_turn(self, _text, **_kwargs) -> bool:
            assert self.declared_tool_step_ids is live.tool_step_ids
            assert self.declared_tool_step_ids == {"call-1": "step-1"}
            self.started = True
            return True

    live = _LiveCodex(
        session=None,
        parent_thread_id=None,
        name="codex",
        catalog_model_id="codex/gpt-5.6-luna",
    )
    live.context_window = 272_000
    live.tool_step_ids["call-1"] = "step-1"
    session = ForkSession()
    _session, start_input = await configure_app_server_input(
        session,
        live=live,
        text="resume checkpoint fork",
        binary="codex",
        codex_thread_id="provider-thread",
        working_dir=None,
        exact_model="codex/gpt-5.6-luna",
        effort=None,
        history_items=(),
        dynamic_tools=(),
        tool_registry=object(),
        thread_id="thread-1",
        project_id=None,
        on_event=None,
        on_exit=None,
        media_projection=None,
    )

    await start_input(lambda: None, lambda: None)
    assert session.started is True


@pytest.mark.asyncio
async def test_codex_spawn_refuses_an_unresolved_compaction_window(monkeypatch):
    class UnexpectedSession:
        def __init__(self, **_kwargs):
            raise AssertionError("spawned without a resolved context window")

    monkeypatch.setattr(
        "subagents.codex_app_server.CodexAppServerSession",
        UnexpectedSession,
    )

    with pytest.raises(ValueError, match="context window"):
        await configure_app_server_input(
            None,
            live=SimpleNamespace(
                session=None, corsia_turn=None, context_window=None,
                discovered_context_window=None,
            ),
            text="hello",
            binary="codex",
            codex_thread_id=None,
            working_dir=None,
            exact_model="gpt-5.6-luna",
            effort=None,
            history_items=(),
            dynamic_tools=(),
            tool_registry=object(),
            thread_id="thread-1",
            project_id=None,
            on_event=None,
            on_exit=None,
            media_projection=None,
        )


def test_codex_compaction_start_is_not_passive() -> None:
    assert item_started_event(
        {"id": "compact-1", "type": "contextCompaction"},
        provider_thread_id="provider-thread-1",
        provider_turn_id="provider-turn-1",
    ) == {
        "kind": "context_compaction_started",
        "provider_thread_id": "provider-thread-1",
        "provider_turn_id": "provider-turn-1",
        "provider_item_id": "compact-1",
    }


def test_codex_completed_compaction_is_only_a_breach_tripwire() -> None:
    assert item_completed_event(
        {
            "id": "compact-1",
            "type": "contextCompaction",
            "summary": "provider-owned summary must not be consumed",
        },
        provider_thread_id="provider-thread-1",
        provider_turn_id="provider-turn-1",
    ) == {
        "kind": "provider_compaction_breach",
        "source": "item/completed",
        "provider_thread_id": "provider-thread-1",
        "provider_turn_id": "provider-turn-1",
        "provider_item_id": "compact-1",
    }


@pytest.mark.asyncio
async def test_correlated_codex_compaction_retires_outside_reader_and_resumes(
    monkeypatch,
) -> None:
    reader_task = asyncio.current_task()
    terminated = asyncio.Event()
    resumed = asyncio.Event()
    termination_tasks: list[asyncio.Task | None] = []

    class Session(CodexAppServerSession):
        async def terminate(self) -> None:
            termination_tasks.append(asyncio.current_task())
            terminated.set()

    session = _real_session(Session, "provider-thread-1", "provider-turn-1")
    live = SimpleNamespace(
        session=session,
        thread_id=session.codex_thread_id,
        corsia_turn=object(),
    )

    async def resume(bound_live, thread_id, harness, *, must_adopt=False):
        assert bound_live is live
        assert thread_id == "thread-1"
        assert harness == "codex"
        assert must_adopt is True
        resumed.set()
        return True

    monkeypatch.setattr(
        "subagents.harness_checkpoint_resume.maintain_external_tool_boundary",
        resume,
    )
    forwarded: list[dict] = []

    async def downstream(event: dict) -> None:
        forwarded.append(event)

    handler = compaction_event_handler(
        thread_id="thread-1",
        live=live,
        session=session,
        downstream=downstream,
    )
    await handler({
        "kind": "context_compaction_started",
        "provider_thread_id": session.codex_thread_id,
        "provider_turn_id": session.active_turn_id,
        "provider_item_id": "compact-1",
    })

    await asyncio.wait_for(terminated.wait(), timeout=1)
    await asyncio.wait_for(resumed.wait(), timeout=1)
    assert termination_tasks == [termination_tasks[0]]
    assert termination_tasks[0] is not reader_task
    assert forwarded == []


@pytest.mark.asyncio
async def test_stale_codex_compaction_does_not_retire_current_handle() -> None:
    class Session(CodexAppServerSession):
        async def terminate(self) -> None:
            raise AssertionError("stale compaction retired current handle")

    session = _real_session(Session, "provider-thread-current", "provider-turn-current")
    forwarded: list[dict] = []
    handler = compaction_event_handler(
        thread_id="thread-1",
        live=SimpleNamespace(
            session=session,
            thread_id=session.codex_thread_id,
            corsia_turn=object(),
        ),
        session=session,
        downstream=forwarded.append,
    )

    await handler({
        "kind": "context_compaction_started",
        "provider_thread_id": "provider-thread-stale",
        "provider_turn_id": session.active_turn_id,
        "provider_item_id": "compact-stale",
    })
    await asyncio.sleep(0)

    assert forwarded == []


@pytest.mark.asyncio
async def test_compaction_start_prevents_local_compact_wire_request(
    monkeypatch,
) -> None:
    class WireEndpoint:
        compact_requests = 0

        async def post_compact(self) -> None:
            self.compact_requests += 1

    endpoint = WireEndpoint()

    class Session(CodexAppServerSession):
        killed = False

        async def terminate(self) -> None:
            self.killed = True

    session = _real_session(Session, "provider-thread-1", "provider-turn-1")
    live = SimpleNamespace(
        session=session,
        thread_id=session.codex_thread_id,
        corsia_turn=object(),
    )

    async def resume(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        "subagents.harness_checkpoint_resume.maintain_external_tool_boundary",
        resume,
    )
    handler = compaction_event_handler(
        thread_id="thread-1",
        live=live,
        session=session,
        downstream=None,
    )

    await handler({
        "kind": "context_compaction_started",
        "provider_thread_id": session.codex_thread_id,
        "provider_turn_id": session.active_turn_id,
        "provider_item_id": "compact-1",
    })
    await asyncio.sleep(0)
    if not session.killed:
        await endpoint.post_compact()
    await asyncio.sleep(0.01)

    assert endpoint.compact_requests == 0


@pytest.mark.asyncio
async def test_real_session_wrong_turn_id_is_not_correlated(monkeypatch) -> None:
    import subagents.codex_runtime_spawn as spawn

    retired: list[str] = []

    async def record_retire(thread_id, live, session) -> None:
        retired.append(thread_id)

    monkeypatch.setattr(spawn, "retire_compacting_handle", record_retire)

    class Session(CodexAppServerSession):
        pass

    session = _real_session(Session, "provider-thread-1", "provider-turn-1")
    handler = compaction_event_handler(
        thread_id="thread-1",
        live=SimpleNamespace(
            session=session, thread_id="provider-thread-1", corsia_turn=object(),
        ),
        session=session,
        downstream=None,
    )
    await handler({
        "kind": "context_compaction_started",
        "provider_thread_id": "provider-thread-1",
        "provider_turn_id": "provider-turn-other",
        "provider_item_id": "compact-1",
    })
    await asyncio.sleep(0)
    assert retired == []


def test_real_session_exposes_provider_thread_id_to_runtime_consumers() -> None:
    from subagents.codex_runtime_spawn import remember_spawned_turn

    session = CodexAppServerSession(binary="unused", task=None, codex_thread_id=None)
    assert session.codex_thread_id is None
    session.thread_id = "provider-thread-1"
    assert session.codex_thread_id == "provider-thread-1"
    live = SimpleNamespace(codex_thread_id="")
    remember_spawned_turn(SimpleNamespace(thread_id="thread-1"), live, session)
    assert live.codex_thread_id == "provider-thread-1"


@pytest.mark.asyncio
async def test_required_adoption_without_legal_cut_raises_checkpoint_unavailable() -> None:
    from core.checkpoint_resume import (
        CheckpointWaitScope, ProviderBoundary, maintain_before_provider_call,
    )
    from core.checkpoint_types import CheckpointUnavailable
    from core.turn_core import TurnIdentity

    boundary = ProviderBoundary(
        thread_id="thread-1", turn=TurnIdentity("turn-1", 1), context_window=1_000,
        canonical_input_tokens=10, provider_input_tokens=10, minimum_input_tokens=10,
        adopted_checkpoint_id=None, active_checkpoint=None, installed_checkpoint=None,
        prepare_checkpoint=None, render_installed=None, wait_scope=CheckpointWaitScope(),
    )
    with pytest.raises(CheckpointUnavailable, match="no checkpoint is admissible"):
        await maintain_before_provider_call(boundary, must_adopt=True)


@pytest.mark.asyncio
async def test_provider_thread_property_registers_the_provider_id_for_runtime_readers() -> None:
    """Starting empty, remember_spawned_turn records the id the real session holds."""
    from subagents.codex_runtime_spawn import remember_spawned_turn
    from subagents.codex_runtime_state import _CODEX_THREADS

    session = CodexAppServerSession(binary="unused", task=None, codex_thread_id=None)
    session.thread_id = "provider-1"
    live = SimpleNamespace(codex_thread_id="")
    _CODEX_THREADS.pop("thread-remember", None)
    try:
        remember_spawned_turn(SimpleNamespace(thread_id="thread-remember"), live, session)
        assert (live.codex_thread_id, _CODEX_THREADS.get("thread-remember")) == (
            "provider-1", "provider-1",
        )
    finally:
        _CODEX_THREADS.pop("thread-remember", None)
