"""A prepared input cannot retain a superseded selected context limit."""

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("model,harness", [
    ("claude-code/opus", "claude-code"), ("codex/gpt-5-codex", "codex"),
])
@pytest.mark.parametrize("new_limit", [131072, 393216])
async def test_initial_boundary_uses_current_cap_without_replacing_warm_input(
    main_db, monkeypatch, model, harness, new_limit,
):
    from core.checkpoint_resume import CheckpointWaitScope
    from infra.input_render import prepare_claimed_batch
    from llm.resolution import resolve_execution_target
    from subagents.harness_checkpoint_resume import maintain_external_initial_boundary
    from test_provider_continuation_admission import _claim, _thread_row
    from vendor_account_fixtures import signed_in_default

    main_db.execute("UPDATE threads SET model=? WHERE id='main-1'", (model,))
    main_db.execute("UPDATE models SET context_window=1000000 WHERE id=?", (model,))
    main_db.commit()
    signed_in_default(harness, db=main_db)  # the account lists the model at that window
    target = resolve_execution_target(model, db=main_db)
    dispatch = await prepare_claimed_batch(_claim(main_db), thread_row=_thread_row(main_db), target=target)
    prepared = dispatch.prepared_context
    original_ledger = prepared.fit_ledger
    main_db.execute("INSERT INTO model_profiles(model_id,context_window,updated_at) VALUES (?,?,'t2') "
                    "ON CONFLICT(model_id) DO UPDATE SET context_window=excluded.context_window",
                    (model, new_limit))
    main_db.commit()
    boundaries = []

    async def maintain(boundary):
        boundaries.append(boundary)
        return SimpleNamespace(payload=None)

    monkeypatch.setattr("core.checkpoint_resume.maintain_before_provider_call", maintain)
    live = SimpleNamespace(catalog_model_id=model, corsia_turn=SimpleNamespace(turn_id="turn"),
        checkpoint_wait_scope=CheckpointWaitScope())
    assert await maintain_external_initial_boundary(
        live, "main-1", harness, prepared, dispatch.refresh_prepared_context,
    ) is None
    assert boundaries[0].context_window == new_limit
    assert boundaries[0].canonical_input_tokens == original_ledger.rendered_input_tokens
    assert prepared.fit_ledger is original_ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-code/opus", "codex/gpt-5-codex"])
async def test_input_refresh_resolves_changed_catalog_cap(main_db, model):
    from infra.input_render import prepare_claimed_batch
    from llm.resolution import resolve_execution_target
    from test_provider_continuation_admission import _claim, _thread_row

    main_db.execute("UPDATE threads SET model=? WHERE id='main-1'", (model,))
    main_db.commit()
    batch = _claim(main_db)
    target = resolve_execution_target(model, db=main_db)
    prepared = await prepare_claimed_batch(batch, thread_row=_thread_row(main_db), target=target)
    main_db.execute("INSERT INTO model_profiles(model_id,context_window,updated_at) VALUES (?,131072,'t2') "
                    "ON CONFLICT(model_id) DO UPDATE SET context_window=131072", (model,))
    main_db.commit()
    refreshed = await prepared.refresh_prepared_context()
    assert refreshed.fit_ledger.effective_window_tokens == 131072
    assert refreshed.rendered.fit_input_identity == prepared.prepared_context.rendered.fit_input_identity
    assert refreshed.rendered.fit_current_input == prepared.prepared_context.rendered.fit_current_input


@pytest.mark.asyncio
async def test_native_next_call_resolves_changed_catalog_cap(isolated_db, monkeypatch):
    from core.checkpoint_resume import CheckpointWaitScope
    from core.destination_preflight import attach_native_provider_fit_ledger
    from core.turn_iteration_context import maintain_native_checkpoint
    from infra.database import get_db
    from shared_send_fixtures import _insert_native_model

    _insert_native_model()
    with get_db() as db:
        db.execute("INSERT INTO model_profiles(model_id,context_window,updated_at) VALUES ('native/test',131072,'t2')")
        db.commit()
    messages = [{"role": "user", "content": "same input"}]
    ledger = attach_native_provider_fit_ledger(target_catalog_id="native/test",
        proven_target_window_tokens=262144, messages=messages, tools=[],
        input_identity="input", current_input=messages[-1]).fit_ledger
    state = SimpleNamespace(fit_ledger=ledger, messages=messages, tool_definitions=[],
        fit_current_input=messages[-1], context_usage={}, last_measured_call_usage=None,
        last_measured_rendered_input_tokens=None)
    boundaries = []

    async def maintain(boundary):
        boundaries.append(boundary)
        return SimpleNamespace(payload=None)

    monkeypatch.setattr("core.checkpoint_resume.maintain_before_provider_call", maintain)
    monkeypatch.setattr("core.turn_iteration_context.provider_boundary_snapshot", lambda *_a: (None, None, None))
    await maintain_native_checkpoint(agent=None, state=state, thread_id="thread",
        turn_identity=SimpleNamespace(turn_id="turn"),
        cancel_event=SimpleNamespace(checkpoint_wait_scope=CheckpointWaitScope()),
        turn_model="native/test", reasoning_effort=None, project_id=None)
    assert boundaries[0].context_window == 131072
    assert state.fit_ledger.effective_window_tokens == 131072
    assert state.messages == messages


@pytest.mark.asyncio
@pytest.mark.parametrize("model,harness", [
    ("claude-code/opus", "claude-code"), ("codex/gpt-5-codex", "codex"),
])
@pytest.mark.parametrize("release", ("tool", "steer"))
async def test_external_release_uses_raised_catalog_cap(main_db, monkeypatch, model, harness, release):
    """An old handle cap must not cause a premature checkpoint after a profile edit."""
    from core.checkpoint_resume import CheckpointWaitScope
    from subagents.harness_checkpoint_resume import maintain_external_tool_boundary
    from vendor_account_fixtures import signed_in_default

    main_db.execute("UPDATE threads SET model=? WHERE id='main-1'", (model,))
    main_db.execute("UPDATE models SET context_window=1000000 WHERE id=?", (model,))
    main_db.execute("INSERT INTO model_profiles(model_id,context_window,updated_at) VALUES (?,262144,'t2') "
                    "ON CONFLICT(model_id) DO UPDATE SET context_window=262144", (model,))
    main_db.commit()
    signed_in_default(harness, db=main_db)  # the account lists the model at that window
    session = SimpleNamespace(peek_active_usage=lambda: {
        "input_tokens": 200000, "model_context_window": 1000000,
    })
    live = SimpleNamespace(catalog_model_id=model, context_window=131072,
        corsia_turn=SimpleNamespace(turn_id="turn"), checkpoint_wait_scope=CheckpointWaitScope(),
        session=session, last_call_usage={"input_tokens": 200000, "output_tokens": 12})
    if release == "steer":
        from core.fork_cc_context import CCForkContext
        from core.fork_codex_context import CodexForkContext
        from infra.input_render import project_claimed_input
        from infra.preflight_attempt import live_input_fits
        from llm.resolution import resolve_execution_target
        from test_provider_continuation_admission import _claim, _thread_row

        batch = _claim(main_db)
        row = _thread_row(main_db)
        target = resolve_execution_target(model, db=main_db)
        session.tool_context = (
            CCForkContext(target, 200000) if harness == "claude-code"
            else CodexForkContext(target, 200000)
        )
        projection = project_claimed_input(main_db, batch, thread_row=row, target=target)
        assert await live_input_fits(batch, live, row, target, projection)
        assert live.session is session
        return
    boundaries = []

    async def maintain(boundary, **_kwargs):
        boundaries.append(boundary)
        return SimpleNamespace(payload=None)

    monkeypatch.setattr("core.checkpoint_resume.maintain_before_provider_call", maintain)
    assert not await maintain_external_tool_boundary(live, "main-1", harness)
    assert boundaries[0].context_window == 262144
    assert boundaries[0].provider_input_tokens == 200000
    assert live.session is session
