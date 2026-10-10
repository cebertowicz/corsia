"""Initial admission remains the occupancy floor until the first call receipt."""

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("model,harness", [
    ("claude-code/opus", "claude-code"), ("codex/gpt-5-codex", "codex"),
])
async def test_initial_boundary_seeds_the_admitted_payload_before_any_receipt(
    main_db, model, harness,
):
    from infra.input_render import prepare_claimed_batch
    from llm.resolution import resolve_execution_target
    from subagents.cc_stream import CCStreamSession
    from subagents.codex_app_server import CodexAppServerSession
    from test_provider_continuation_admission import _claim, _thread_row

    main_db.execute("UPDATE threads SET model=? WHERE id='main-1'", (model,))
    main_db.commit()
    target = resolve_execution_target(model, db=main_db)
    batch = _claim(main_db)
    dispatch = await prepare_claimed_batch(
        batch, thread_row=_thread_row(main_db), target=target,
    )
    prepared = dispatch.prepared_context
    admitted = prepared.fit_ledger.rendered_input_tokens
    assert admitted > 0
    if harness == "claude-code":
        from subagents.cc_feed import _maintain_initial_boundary

        session = CCStreamSession(binary="unused", session_id="fixture")
        live = SimpleNamespace(session=session, resume_rejected=False)
        transition = SimpleNamespace(refresh_callback=None, target=target)
        frame = object()
        assert await _maintain_initial_boundary(
            transition, prepared, frame, live, None,
        ) == (live, frame)
    else:
        from subagents.codex_turn_transitions import maintain_initial_boundary

        session = CodexAppServerSession(binary="unused", task=None)
        live = SimpleNamespace(session=session)
        spec = SimpleNamespace(prepared_context=prepared,
            refresh_prepared_context=None, catalog_model_id=model)
        assert await maintain_initial_boundary(spec, live) is None
        # turn/start's acknowledgement must not erase the just-admitted seed.
        session._track_active_turn("provider-turn")
    assert session.tool_context.tokens == admitted
    session.tool_context.pending_inputs["new-steer"] = 20
    assert session.tool_context.tokens == admitted + 20
    # Before a provider receipt exists, a steer cannot spend the already-used
    # initial input room again by falling back to a newly rendered cold prefix.
    from dataclasses import replace
    from infra.input_render import project_claimed_input
    from infra.preflight_attempt import live_input_fits

    row = _thread_row(main_db)
    projection = project_claimed_input(
        main_db, batch, thread_row=row, target=target,
    )
    assert not await live_input_fits(
        batch, live, row, replace(target, context_window=admitted + 10),
        projection,
    )
