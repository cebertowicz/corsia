"""A registered idle Codex owner is woken after its runtime context changes.

``window`` is the fail-first repro: the idle app-server was spawned under one
compaction limit, the catalog window then changed, and the send used to fail
after admission with "Codex context changed before provider delivery".
"""

from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

from unittest.mock import AsyncMock, patch

import pytest

from codex_runtime_fixtures import _make_mock_app_session, _reset_runtime

MODEL = "codex/gpt-5-codex"


@pytest.fixture(autouse=True)
def _isolated_runtime():
    _reset_runtime()
    yield
    _reset_runtime()


def _claim(main_db):
    from infra.thread_driver import admit_input

    admission = admit_input(
        "main-1", role="system", content="webhook.github.check_suite_completed",
        intent="apply_now", message_id="input-1", turn_id="turn-1",
        boot_epoch="test-boot", admitted_at="t", db=main_db,
    )
    assert admission.batch is not None
    return admission.batch


def _thread_row(main_db):
    return dict(main_db.execute("SELECT * FROM threads WHERE id='main-1'").fetchone())


@pytest.mark.parametrize("invalidate", ["none", "epoch", "window", "workdir", "exited", "model"])
@pytest.mark.asyncio
async def test_idle_owner_wake_after_context_change_is_delivered(
    main_db, monkeypatch, invalidate,
):
    """Every invalidated idle session is replaced by a fresh one, never rejected."""
    from infra.conductor import set_scope_coordinator
    from infra.thread_execution import execute_claimed_batch
    from llm.resolution import resolve_execution_target
    from registry.service import register_codex_thread
    from subagents.codex_destination_render import codex_destination_scope
    from subagents.codex_runtime_spawn import _compaction_overrides
    from subagents.codex_runtime_state import _APP_SESSIONS, _CODEX_THREADS
    from subagents.harness_tool_bridge import build_tool_registry, dynamic_tool_definitions

    main_db.execute("UPDATE threads SET model=? WHERE id='main-1'", (MODEL,))
    main_db.commit()
    target = resolve_execution_target(MODEL, db=main_db)
    _, working_dir = codex_destination_scope("main-1", target_catalog_id=MODEL, db=main_db)
    registry = build_tool_registry(working_dir)
    idle = _make_mock_app_session("provider-thread-1")
    idle.working_dir = working_dir
    idle.catalog_model_id = MODEL
    idle.model = target.transport_model
    idle.expected_reported_model = target.expected_reported_model
    idle.runtime_context_epoch = 0
    idle.tool_registry = registry
    idle.dynamic_tools = dynamic_tool_definitions(registry)
    idle.config_overrides = _compaction_overrides(
        target.context_window,
        discovered_context_window=target.discovered_context_window,
    )
    register_codex_thread("main-1", idle.codex_thread_id, db=main_db)
    monkeypatch.setitem(_APP_SESSIONS, "main-1", idle)
    monkeypatch.setitem(_CODEX_THREADS, "main-1", idle.codex_thread_id)
    if invalidate == "epoch":
        assert set_corsia_agent("main-1", db=main_db)
    if invalidate == "window":
        main_db.execute(
            "UPDATE models SET context_window=context_window+1000 WHERE id=?",
            (MODEL,),
        )
        main_db.commit()
        target = resolve_execution_target(MODEL, db=main_db)
    if invalidate == "workdir":
        idle.working_dir = "/somewhere/else"
    if invalidate == "exited":
        idle.returncode = 1
    if invalidate == "model":
        idle.model = "other-model"
    batch = _claim(main_db)
    fresh = _make_mock_app_session("provider-thread-1")
    conn = {"status": "available", "config": {"binary": "codex", "app_server": True}}
    with (
        patch("connections.models.get_connection", return_value=conn),
        patch("subagents.codex_app_server.CodexAppServerSession", return_value=fresh),
    ):
        result = await execute_claimed_batch(
            batch, thread_row=_thread_row(main_db), target=target,
            user={"id": "test-user"},
        )
    assert result.turn_id == "turn-1"
    if invalidate in ("epoch", "window"):
        idle.terminate.assert_awaited()
        fresh.start.assert_awaited()


def _target(context_window: int):
    from types import SimpleNamespace

    return SimpleNamespace(
        context_window=context_window, discovered_context_window=None,
    )


def _spawn_overrides(context_window: int):
    from subagents.codex_runtime_spawn import _compaction_overrides

    return _compaction_overrides(context_window)


def test_live_compaction_rejects_list_overrides_with_changed_window():
    """A non-tuple ``config_overrides`` is compared, not silently admitted."""
    from types import SimpleNamespace

    from subagents.codex_destination import validate_live_compaction
    from subagents.codex_runtime_errors import CodexPreparedContextStale

    session = SimpleNamespace(config_overrides=list(_spawn_overrides(200_000)))
    with pytest.raises(CodexPreparedContextStale):
        validate_live_compaction(session, _target(201_000))


def test_live_compaction_admits_list_overrides_with_same_window():
    from types import SimpleNamespace

    from subagents.codex_destination import validate_live_compaction

    session = SimpleNamespace(config_overrides=list(_spawn_overrides(200_000)))
    validate_live_compaction(session, _target(200_000))


def test_live_compaction_rejects_unset_overrides():
    """A session without ``config_overrides`` carries none, so it differs from the target."""
    from types import SimpleNamespace

    from subagents.codex_destination import validate_live_compaction
    from subagents.codex_runtime_errors import CodexPreparedContextStale

    with pytest.raises(CodexPreparedContextStale):
        validate_live_compaction(SimpleNamespace(), _target(200_000))
