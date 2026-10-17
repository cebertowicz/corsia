from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_startup_re_asks_harness_catalogs_whatever_their_age(monkeypatch):
    import app_lifecycle
    import connections.harness_refresh as harness_refresh

    calls: list[str] = []

    async def _refresh(*, reason: str):
        calls.append(reason)
        return [{"provider": "codex", "ok": True}]

    def _stale_only():
        raise AssertionError("startup must not use the age-gated refresh")

    monkeypatch.setattr(harness_refresh, "refresh_harness_catalogs", _refresh)
    monkeypatch.setattr(harness_refresh, "schedule_stale_refresh", _stale_only)

    result = await app_lifecycle._startup_harness_discovery()

    assert calls == ["startup"]
    assert result == [{"provider": "codex", "ok": True}]


@pytest.mark.asyncio
async def test_models_endpoint_schedules_the_stale_check_without_waiting(monkeypatch):
    import api.models as api_models
    import connections.harness_refresh as harness_refresh
    import llm.capabilities as capabilities
    import llm.model_status as model_status

    calls: list[str] = []

    def _schedule():
        calls.append("stale")

    monkeypatch.setattr(harness_refresh, "schedule_stale_refresh", _schedule)
    monkeypatch.setattr(model_status, "get_model_status_rows", lambda: [{"id": "row"}])
    monkeypatch.setattr(capabilities, "get_active_local_threads", lambda _active: [])

    result = await api_models.list_models()

    assert calls == ["stale"]
    assert result == {"models": [{"id": "row"}], "active_local_threads": 0}


@pytest.mark.asyncio
async def test_session_create_refreshes_unknown_harness_selection_without_rewriting(monkeypatch):
    import api.threads as sessions
    import connections.harness_refresh as harness_refresh

    calls: list[str | None] = []
    monkeypatch.setattr(sessions, "authenticate", lambda _token: {"id": "u"})

    async def _refresh(model: str | None):
        calls.append(model)
        return {"ok": True}

    async def _create(**kwargs):
        assert kwargs["model"] == "codex/gpt-5.6-sol"
        return {"id": "session-1"}

    monkeypatch.setattr(harness_refresh, "prepare_selection", _refresh)
    monkeypatch.setattr(sessions, "create_visible_fresh_thread", _create)
    monkeypatch.setattr(sessions, "emit_product_event", lambda *args, **kwargs: None)

    result = await sessions.create_thread(
        "token", {"model": "codex/gpt-5.6-sol", "title": "New Chat"}
    )

    assert calls == ["codex/gpt-5.6-sol"]
    assert result == {"thread_id": "session-1"}


@pytest.mark.asyncio
async def test_session_update_refreshes_requested_harness_selection_without_fallback(monkeypatch):
    import api.thread_mutations as sessions
    import connections.harness_refresh as harness_refresh

    calls: list[str | None] = []
    monkeypatch.setattr(sessions, "authenticate", lambda _token: {"id": "u"})

    async def _refresh(model: str | None):
        calls.append(model)
        return {"ok": False, "error": "offline"}

    async def _run_in_db(_fn):
        return {
            "status": "ok",
            "old_model": "codex/gpt-5.6-sol",
            "model_changed": False,
            "context_boundary_changed": False,
        }

    monkeypatch.setattr(harness_refresh, "prepare_selection", _refresh)
    monkeypatch.setattr(sessions, "run_in_db", _run_in_db)
    monkeypatch.setattr(sessions, "is_dispatchable_model", lambda _model: True)
    from thread.model_state import ModelSwitchOutcome

    # A REAL outcome: the endpoint tells a committed switch from an accepted
    # background operation by type, so a look-alike takes the wrong branch.
    switch = AsyncMock(
        return_value=ModelSwitchOutcome(
            changed=False,
            old_model="codex/gpt-5.6-sol",
            new_model="codex/gpt-5.6-sol",
            preparation=None,
            breadcrumb=None,
            committed_at=None,
            post_effects_error=None,
        )
    )
    monkeypatch.setattr(sessions, "switch_model", switch)

    result = await sessions.update_thread(
        "session-1", "token", {"model": "codex/gpt-5.6-sol"}
    )

    assert calls == ["codex/gpt-5.6-sol"]
    assert result == {"ok": True, "model": "codex/gpt-5.6-sol"}
    switch.assert_awaited_once_with(
        "session-1",
        "codex/gpt-5.6-sol",
        user_id="u",
        actor_kind="user",
        actor_id="u",
        reason="harness_model_switch",
        checkpoint_override=None,
        setup_selection=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("waiting", "status", "checkpoint_required"),
    ((True, "waiting_for_turn", False), (False, "preparing", True)),
)
async def test_session_update_returns_accepted_for_pending_switch(
    monkeypatch, waiting, status, checkpoint_required,
):
    import json

    import api.thread_mutations as sessions
    import connections.harness_refresh as harness_refresh
    from infra.model_switch_types import SwitchRequest

    monkeypatch.setattr(sessions, "authenticate", lambda _token: {"id": "u"})
    monkeypatch.setattr(sessions, "is_dispatchable_model", lambda _model: True)

    async def _refresh(_model: str | None):
        return {"ok": True}

    async def _run_in_db(_fn):
        return True

    pending = SwitchRequest(
        operation_id="operation-b",
        thread_id="session-1",
        source_model="source/a",
        destination_model="target/b",
        source_execution_epoch=0,
        destination_execution_epoch=1,
        switch_after_input_order=0,
        actor_kind="user",
        actor_id="u",
        reason="harness_model_switch",
        checkpoint_choice="current",
        requested_at="t",
        observed_input_order=0,
        waiting_for_source=waiting,
    )
    switch = AsyncMock(return_value=pending)
    monkeypatch.setattr(harness_refresh, "prepare_selection", _refresh)
    monkeypatch.setattr(sessions, "run_in_db", _run_in_db)
    monkeypatch.setattr(sessions, "switch_model", switch)

    response = await sessions.update_thread(
        "session-1",
        "token",
        {"model": "target/b", "checkpoint_model": "current"},
    )

    assert response.status_code == 202
    assert json.loads(response.body) == {
        "operation_id": "operation-b",
        "status": status,
        "pending_model": "target/b",
        "checkpoint_required": checkpoint_required,
    }
    switch.assert_awaited_once_with(
        "session-1",
        "target/b",
        user_id="u",
        actor_kind="user",
        actor_id="u",
        reason="harness_model_switch",
        checkpoint_override="current",
        setup_selection=False,
    )


@pytest.mark.asyncio
async def test_session_list_batches_saved_unknown_selectors_without_rewriting(monkeypatch):
    import api.threads as sessions
    import connections.harness_refresh as harness_refresh

    rows = [
        {"id": "s1", "model": "codex/gpt-5-codex"},
        {"id": "s2", "model": "codex/gpt-5.6-terra"},
        {"id": "s3", "model": "claude-code/claude-fable-5[1m]"},
    ]
    calls: list[list[str | None]] = []
    monkeypatch.setattr(sessions, "authenticate", lambda _token: {"id": "u"})

    async def _run_in_db(_fn):
        return rows

    def _schedule(models):
        calls.append(list(models))

    monkeypatch.setattr(sessions, "run_read", _run_in_db)
    monkeypatch.setattr(harness_refresh, "schedule_refresh_for_selections", _schedule)

    result = await sessions.list_threads("token")

    assert calls == [[row["model"] for row in rows]]
    assert result == {"threads": rows}
